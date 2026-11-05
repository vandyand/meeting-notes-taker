import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import live_transcription


def block(track_id="mic-1", kind="microphone", device="Headset", start=0, pcm=b"\x01\x00" * 8):
    return {"track_id": track_id, "kind": kind, "device": {"name": device, "id": track_id},
            "sample_rate": 16000, "channels": 1, "start_frame": start, "pcm_bytes": pcm}


class LiveTranscriptionTests(unittest.TestCase):
    def test_revised_boundary_does_not_commit_same_utterance_twice(self):
        first = live_transcription.Segment(0, 8, 16000, [1.0])
        shifted = live_transcription.Segment(0, 9, 16000, [1.0])
        next_piece = live_transcription.Segment(9, 16, 16000, [2.0])

        def segmenter(audio, _rate, *, final=False):
            if final:
                return []
            return [first] if len(audio) == 8 else [shifted, next_piece]

        with tempfile.TemporaryDirectory() as temp:
            coordinator = live_transcription.LiveTranscriptionCoordinator(
                temp, segmenter=segmenter, transcriber=lambda audio: f"piece {int(audio[0])}")
            coordinator.start()
            coordinator.submit(block(pcm=b"\x01\x00" * 8))
            coordinator.submit(block(start=8, pcm=b"\x01\x00" * 8))
            coordinator.stop(timeout=1)
            committed = [r["text"] for r in live_transcription.read_live_records(temp)
                         if r["status"] == "committed"]
            self.assertEqual(committed, ["piece 1", "piece 2"])

    def test_default_live_mode_does_not_repeat_model_inference_for_previews(self):
        segment = live_transcription.Segment(0, 8, 16000, [1.0])
        calls = []
        with tempfile.TemporaryDirectory() as temp:
            coordinator = live_transcription.LiveTranscriptionCoordinator(
                temp, segmenter=lambda _audio, _rate, *, final=False: [segment] if final else [],
                transcriber=lambda audio: calls.append(audio) or "caption")
            coordinator.start()
            coordinator.submit(block())
            coordinator.stop(timeout=1)
            self.assertEqual(len(calls), 1)
            self.assertFalse(any(e["event"] == "live_caption" and e["provisional"]
                                 for e in coordinator.events if e["event"] == "live_caption"))

    def test_model_prepare_failure_records_all_queued_and_later_audio_as_gaps(self):
        class BrokenModel:
            def prepare(self):
                raise RuntimeError("model could not load")
            def transcribe(self, _audio):
                return ""
        with tempfile.TemporaryDirectory() as temp:
            model = BrokenModel()
            coordinator = live_transcription.LiveTranscriptionCoordinator(temp, transcriber=model.transcribe)
            coordinator.start()
            coordinator.submit(block("mic", "microphone", start=0))
            coordinator._thread.join(1)
            self.assertFalse(coordinator.submit(block("mic", "microphone", start=8)))
            self.assertFalse(coordinator.submit(block("system", "system", start=16)))
            coordinator.stop()
            records = live_transcription.read_live_records(temp)
            gaps = [row for row in records if row["status"] == "gap"]
            self.assertEqual({row["track_id"] for row in gaps}, {"mic", "system"})
            self.assertEqual(next(row for row in gaps if row["track_id"] == "mic")["dropped_blocks"], 2)
            self.assertTrue(all(row["reason"] == "model_unavailable" for row in gaps))

    def test_submit_is_bounded_and_queue_overflow_is_persisted_as_gap(self):
        with tempfile.TemporaryDirectory() as temp:
            gate = threading.Event()
            entered = threading.Event()
            segment = live_transcription.Segment(0, 8, 16000, [0.0])
            def transcribe(_audio):
                entered.set()
                gate.wait(2)
                return "speech"
            coordinator = live_transcription.LiveTranscriptionCoordinator(
                temp, segmenter=lambda _audio, _rate: [segment],
                transcriber=transcribe, queue_size=1)
            coordinator.start()
            started = time.perf_counter()
            self.assertTrue(coordinator.submit(block()))
            self.assertTrue(entered.wait(1))
            self.assertTrue(coordinator.submit(block(start=8)))
            elapsed = time.perf_counter() - started
            self.assertLess(elapsed, 0.05)
            self.assertFalse(coordinator.submit(block(start=16)))
            coordinator.stop(timeout=0.01)
            gate.set()
            coordinator.stop(timeout=1)
            records = live_transcription.read_live_records(temp)
            self.assertTrue(any(row["status"] == "gap" for row in records))

    def test_default_queue_holds_five_tracks_during_model_startup(self):
        class SlowModel:
            def __init__(self):
                self.entered = threading.Event()
                self.release = threading.Event()
            def prepare(self):
                self.entered.set()
                self.release.wait(1)
            def transcribe(self, _audio):
                return ""
        with tempfile.TemporaryDirectory() as temp:
            model = SlowModel()
            coordinator = live_transcription.LiveTranscriptionCoordinator(
                temp, segmenter=lambda _audio, _rate: [], transcriber=model.transcribe)
            coordinator.start()
            self.assertTrue(model.entered.wait(1))
            accepted = 0
            for index in range(500):
                source = index % 5
                accepted += coordinator.submit(block(f"track-{source}",
                                                      "microphone" if source < 2 else "system",
                                                      f"Device {source}", start=index * 8))
            self.assertEqual(accepted, 500)
            model.release.set()
            self.assertTrue(coordinator.stop(timeout=3))

    def test_default_device_routing_avoids_stereo_mix_and_emits_source_event(self):
        with tempfile.TemporaryDirectory() as temp:
            coordinator = live_transcription.LiveTranscriptionCoordinator(
                temp, transcriber=lambda _audio: "", default_devices={"microphone": "array-1"})
            coordinator.start()
            coordinator.submit(block("mix", "microphone", "Stereo Mix"))
            coordinator.submit(block("array-1", "microphone", "Built-in Array"))
            coordinator.stop()
            selected = [e for e in coordinator.events if e["event"] == "live_source"]
            self.assertEqual(selected[0]["device"], "Built-in Array")
            self.assertEqual(selected[0]["source"], "microphone")

    def test_committed_caption_is_flushed_with_track_relative_times_and_event(self):
        with tempfile.TemporaryDirectory() as temp:
            segment = live_transcription.Segment(start_sample=0, end_sample=8,
                                                  source_sample_rate=16000, audio_16k=[0.0])
            coordinator = live_transcription.LiveTranscriptionCoordinator(
                temp, segmenter=lambda _audio, _rate: [segment], transcriber=lambda _audio: "hello")
            coordinator.start()
            coordinator.submit(block(start=16000))
            coordinator.stop(timeout=1)
            records = live_transcription.read_live_records(temp)
            caption = next(r for r in records if r["status"] == "committed")
            self.assertEqual((caption["start"], caption["end"]), (1.0, 1.0005))
            self.assertEqual(caption["text"], "hello")
            self.assertTrue(any(e["event"] == "live_caption" and not e["provisional"]
                                for e in coordinator.events))

    def test_segmenter_receives_accumulated_audio_across_tap_blocks(self):
        with tempfile.TemporaryDirectory() as temp:
            segment = live_transcription.Segment(0, 16, 16000, [0.0])
            lengths = []
            def segmenter(audio, _rate):
                lengths.append(len(audio))
                return [segment] if len(audio) >= 16 else []
            coordinator = live_transcription.LiveTranscriptionCoordinator(
                temp, segmenter=segmenter, transcriber=lambda _audio: "two blocks")
            coordinator.start()
            coordinator.submit(block(pcm=b"\x01\x00" * 8))
            coordinator.submit(block(start=8, pcm=b"\x01\x00" * 8))
            coordinator.stop(timeout=1)
            records = live_transcription.read_live_records(temp)
            self.assertIn(16, lengths)
            self.assertEqual(sum(r.get("status") == "committed" for r in records), 1)

    def test_unfinished_piece_is_previewed_once_then_committed_without_duplicate_line(self):
        with tempfile.TemporaryDirectory() as temp:
            segment = live_transcription.Segment(0, 8, 16000, [0.0])
            def segmenter(_audio, _rate, *, final=False):
                return [segment] if final else []
            coordinator = live_transcription.LiveTranscriptionCoordinator(
                temp, segmenter=segmenter, transcriber=lambda _audio: "caption", preview_interval=10)
            coordinator.start()
            coordinator.submit(block())
            coordinator.stop(timeout=1)
            captions = [e for e in coordinator.events if e["event"] == "live_caption"]
            self.assertEqual([e["provisional"] for e in captions], [True, False])
            self.assertEqual(len(live_transcription.read_live_records(temp)), 2)  # committed + stopped

    def test_missing_model_marks_unavailable_and_records_audio_gap(self):
        with tempfile.TemporaryDirectory() as temp:
            coordinator = live_transcription.LiveTranscriptionCoordinator(
                temp, transcriber=None, model_error="model cache missing")
            coordinator.start()
            self.assertFalse(coordinator.submit(block()))
            coordinator.stop(timeout=0.2)
            self.assertTrue(any(e.get("status") == "unavailable" for e in coordinator.events))
            self.assertTrue(any(r.get("status") == "gap" and r.get("reason") == "model_unavailable"
                                for r in live_transcription.read_live_records(temp)))
            self.assertFalse((Path(temp) / "capture.wav").exists())

    def test_lazy_model_prepare_failure_never_emits_active(self):
        class MissingModel:
            def prepare(self):
                raise RuntimeError("cache unavailable")
            def transcribe(self, _audio):
                return ""
        with tempfile.TemporaryDirectory() as temp:
            model = MissingModel()
            coordinator = live_transcription.LiveTranscriptionCoordinator(
                temp, transcriber=model.transcribe)
            coordinator.start()
            coordinator._thread.join(1)
            statuses = [e["status"] for e in coordinator.events if e["event"] == "live_status"]
            self.assertIn("unavailable", statuses)
            self.assertNotIn("active", statuses)

    def test_source_switch_requires_quiet_current_source_and_sustained_alternative(self):
        with tempfile.TemporaryDirectory() as temp:
            coordinator = live_transcription.LiveTranscriptionCoordinator(
                temp, transcriber=lambda _audio: "", switch_after=2, quiet_rms=0.01)
            coordinator.start()
            loud = b"\xff\x3f" * 8
            quiet = b"\x00\x00" * 8
            coordinator.submit(block("mic-1", "microphone", "Headset", pcm=loud))
            coordinator.submit(block("mic-2", "microphone", "Desk", pcm=loud))
            coordinator.submit(block("mic-2", "microphone", "Desk", start=8, pcm=loud))
            time.sleep(0.08)
            switched = [e for e in coordinator.events if e["event"] == "live_source"]
            self.assertEqual(len(switched), 1)
            coordinator.submit(block("mic-1", "microphone", "Headset", start=8, pcm=quiet))
            coordinator.submit(block("mic-2", "microphone", "Desk", start=16, pcm=loud))
            coordinator.submit(block("mic-2", "microphone", "Desk", start=24, pcm=loud))
            coordinator.stop(timeout=1)
            switched = [e for e in coordinator.events if e["event"] == "live_source"]
            self.assertEqual(switched[-1]["device"], "Desk")

    def test_stereo_mix_cannot_displace_a_physical_microphone(self):
        with tempfile.TemporaryDirectory() as temp:
            coordinator = live_transcription.LiveTranscriptionCoordinator(
                temp, transcriber=lambda _audio: "", default_devices={"microphone": "headset"},
                switch_after=2, quiet_rms=0.01)
            coordinator.start()
            quiet = b"\x00\x00" * 8
            loud = b"\xff\x3f" * 8
            coordinator.submit(block("mix", "microphone", "Stereo Mix", pcm=loud))
            coordinator.submit(block("headset", "microphone", "Headset", pcm=quiet))
            coordinator.submit(block("mix", "microphone", "Stereo Mix", start=8, pcm=loud))
            coordinator.submit(block("mix", "microphone", "Stereo Mix", start=16, pcm=loud))
            coordinator.stop(timeout=1)
            sources = [event["device"] for event in coordinator.events if event["event"] == "live_source"]
            self.assertEqual(sources, ["Headset"])

    def test_stop_is_bounded_and_existing_commit_survives_reopen(self):
        with tempfile.TemporaryDirectory() as temp:
            segment = live_transcription.Segment(0, 8, 16000, [0.0])
            coordinator = live_transcription.LiveTranscriptionCoordinator(
                temp, segmenter=lambda _audio, _rate: [segment], transcriber=lambda _audio: "saved")
            coordinator.start()
            coordinator.submit(block())
            coordinator.stop(timeout=1)
            rows = live_transcription.read_live_records(temp)
            self.assertTrue(any(row.get("text") == "saved" for row in rows))
            self.assertEqual(rows[-1]["status"], "stopped")


if __name__ == "__main__":
    unittest.main()
