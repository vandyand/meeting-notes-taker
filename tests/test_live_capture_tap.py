import sys
import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import windows_capture


class FakeRecorder:
    def __init__(self, chunks):
        self.chunks = iter(chunks)

    def record(self, numframes):
        chunk = next(self.chunks)
        return chunk[:numframes]


class LiveCaptureTapTests(unittest.TestCase):
    def test_tap_receives_written_pcm_with_exact_track_metadata_and_offsets(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "capture.wav"
            wav = windows_capture.open_wav(path, sample_rate=16000, channels=1)
            self.addCleanup(wav.close)
            recorder = FakeRecorder([
                np.array([[0.25], [-0.5]], dtype=np.float32),
                np.array([[0.75], [0.0]], dtype=np.float32),
            ])
            device = type("Device", (), {"name": "Mic", "id": "mic-1"})()
            taps = []
            counters = {}
            errors = []

            windows_capture.capture_loop(
                name="microphone-1-mic",
                recorder=recorder,
                wav=wav,
                np=np,
                channels=1,
                chunk_frames=2,
                max_frames=4,
                counters=counters,
                errors=errors,
                event_sink=lambda _event: None,
                sample_rate=16000,
                audio_tap=taps.append,
                kind="microphone",
                device=device,
            )
            wav.close()

            self.assertEqual(errors, [])
            self.assertEqual(counters["microphone-1-mic"], 4)
            self.assertEqual([tap["start_frame"] for tap in taps], [0, 2])
            self.assertEqual(
                [{k: tap[k] for k in ("track_id", "kind", "device", "sample_rate", "channels")}
                 for tap in taps],
                [{"track_id": "microphone-1-mic", "kind": "microphone",
                  "device": {"name": "Mic", "id": "mic-1"},
                  "sample_rate": 16000, "channels": 1}] * 2,
            )
            with wave.open(str(path), "rb") as saved:
                wav_pcm = saved.readframes(saved.getnframes())
            self.assertEqual(b"".join(tap["pcm_bytes"] for tap in taps), wav_pcm)

    def test_tap_exception_does_not_interrupt_wav_capture(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "capture.wav"
            wav = windows_capture.open_wav(path, sample_rate=16000, channels=1)
            self.addCleanup(wav.close)
            recorder = FakeRecorder([
                np.array([[0.25], [-0.5]], dtype=np.float32),
                np.array([[0.75], [0.0]], dtype=np.float32),
            ])
            counters = {}
            errors = []

            def broken_tap(_block):
                raise RuntimeError("consumer stopped")

            windows_capture.capture_loop(
                name="system-1-output",
                recorder=recorder,
                wav=wav,
                np=np,
                channels=1,
                chunk_frames=2,
                max_frames=4,
                counters=counters,
                errors=errors,
                event_sink=lambda _event: None,
                sample_rate=16000,
                audio_tap=broken_tap,
                kind="system",
                device=type("Device", (), {"name": "Output", "id": "out-1"})(),
            )
            wav.close()

            self.assertEqual(errors, [])
            self.assertEqual(counters["system-1-output"], 4)
            with wave.open(str(path), "rb") as saved:
                self.assertEqual(saved.getnframes(), 4)


if __name__ == "__main__":
    unittest.main()
