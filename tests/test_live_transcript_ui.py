import gc
import json
import argparse
import sys
import tempfile
import tkinter as tk
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import windows_capture_ui as ui


class LiveTranscriptUiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.session = Path(self.temp.name) / "20261017-090000"
        self.session.mkdir()
        (self.session / "metadata.json").write_text("{}", encoding="utf-8")
        self.root = tk.Tk()
        self.root.withdraw()

        def close_root():
            for callback in self.root.tk.call("after", "info"):
                self.root.tk.call("after", "cancel", callback)
            self.root.destroy()
            gc.collect()

        self.addCleanup(close_root)
        with mock.patch.object(ui, "default_recordings_dir", return_value=Path(self.temp.name)), mock.patch.object(ui.CaptureApp, "refresh_devices"):
            self.app = ui.CaptureApp(self.root)

    def test_live_caption_preview_is_replaced_by_commit_without_duplicate_line(self):
        self.app.handle_live_event({"event": "live_caption", "source": "microphone", "device": "Headset Mic", "text": "Early words", "provisional": True, "start": 2.0, "end": 3.0})
        self.assertIn("Early words", self.app.live_preview_var.get())
        self.app.handle_live_event({"event": "live_caption", "source": "microphone", "device": "Headset Mic", "text": "Final words", "provisional": False, "start": 2.0, "end": 3.5})
        self.assertNotIn("Early words", self.app.live_preview_var.get())
        visible = self.app.live_transcript_text.get("1.0", "end")
        self.assertEqual(visible.count("Final words"), 1)
        self.assertNotIn("Early words", visible)
        self.assertIn("Microphone", visible)

    def test_simultaneous_source_previews_do_not_overwrite_each_other(self):
        self.app.handle_live_event({"event": "live_caption", "source": "microphone", "text": "mic preview", "provisional": True})
        self.app.handle_live_event({"event": "live_caption", "source": "system", "text": "system preview", "provisional": True})
        self.assertIn("mic preview", self.app.live_preview_var.get())
        self.assertIn("system preview", self.app.live_preview_var.get())
        self.app.handle_live_event({"event": "live_caption", "source": "microphone", "text": "mic final", "provisional": False})
        self.assertNotIn("mic preview", self.app.live_preview_var.get())
        self.assertIn("system preview", self.app.live_preview_var.get())

    def test_status_and_gap_are_visible_without_claiming_complete(self):
        self.app.handle_live_event({"event": "live_status", "status": "starting", "detail": "Loading local model"})
        self.assertIn("Starting", self.app.live_status_var.get())
        self.app.handle_live_event({"event": "live_gap", "source": "system", "start": 4.0, "end": 6.0, "reason": "queue full"})
        self.assertIn("gap", self.app.live_status_var.get().lower())
        self.app.handle_live_event({"event": "live_status", "status": "stopped", "detail": ""})
        self.assertIn("incomplete", self.app.live_status_var.get().lower())

    def test_source_change_keeps_active_status_and_names_device(self):
        self.app.handle_live_event({"event": "live_status", "status": "active", "detail": "Local captions active"})
        self.app.handle_live_event({"event": "live_source", "source": "system", "device": "Headset", "previous_track_id": "old"})
        self.app.handle_live_event({"event": "live_source", "source": "microphone", "device": "Mic"})
        self.assertIn("Active", self.app.live_status_var.get())
        self.assertIn("Headset", self.app.live_source_var.get())
        self.assertIn("Mic", self.app.live_source_var.get())
        self.assertIn("changed", self.app.live_source_var.get().lower())

    def test_reopening_selected_meeting_shows_saved_live_captions_not_final_transcript(self):
        folder = self.session / "transcripts"
        folder.mkdir()
        with (folder / "live_captions.jsonl").open("w", encoding="utf-8") as stream:
            stream.write(json.dumps({"schema_version": 1, "source": "system", "device": "Speakers", "start": 1.5, "end": 3.0, "text": "Saved live words", "status": "committed"}) + "\n")
        self.app.selected_session = self.session
        self.app.load_selected_notes()
        self.assertIn("Saved live words", self.app.live_review_text.get("1.0", "end"))
        self.assertIn("live", self.app.live_review_status_var.get().lower())
        self.assertNotIn("Saved live words", self.app.notes_text.get("1.0", "end"))

    def test_record_worker_passes_nonblocking_live_tap_and_stops_worker(self):
        args = argparse.Namespace(out_dir=str(self.session))
        self.app.live_enabled_for_recording = True
        coordinator = mock.Mock()
        coordinator.submit.return_value = True
        metadata = {"tracks": [], "failures": []}
        def record_audio(_args, **kwargs):
            deadline = time.monotonic() + 2
            while not coordinator.start.called and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(coordinator.start.called)
            self.assertTrue(callable(kwargs["audio_tap"]))
            self.assertTrue(kwargs["audio_tap"]({"track_id": "mic"}))
            return metadata
        with mock.patch.object(ui, "LiveTranscriptionCoordinator", return_value=coordinator, create=True), \
             mock.patch.object(ui.capture, "run_recording", side_effect=record_audio):
            self.app._record_worker(args)
        coordinator.start.assert_called_once()
        coordinator.stop.assert_called_once()
        self.assertEqual(self.app.events.get_nowait(), ("done", metadata))

    def test_tap_exception_is_visible_and_persisted_without_failing_capture(self):
        args = argparse.Namespace(out_dir=str(self.session))
        self.app.live_enabled_for_recording = True
        coordinator = mock.Mock()
        coordinator.submit.side_effect = RuntimeError("tap crashed")
        metadata = {"tracks": [], "failures": []}
        def record_audio(_args, **kwargs):
            deadline = time.monotonic() + 2
            while not coordinator.start.called and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(coordinator.start.called)
            self.assertFalse(kwargs["audio_tap"]({"track_id": "mic", "kind": "microphone", "start_frame": 0,
                                                   "sample_rate": 16000, "channels": 1, "pcm_bytes": b"\x00\x00"}))
            return metadata
        with mock.patch.object(ui, "LiveTranscriptionCoordinator", return_value=coordinator), \
             mock.patch.object(ui.capture, "run_recording", side_effect=record_audio):
            self.app._record_worker(args)
        self.assertEqual(self.app.events.get_nowait()[1]["event"], "live_error")
        coordinator.report_tap_error.assert_called_once()
        self.assertEqual(self.app.events.get_nowait(), ("done", metadata))

    def test_default_off_records_audio_without_starting_caption_model(self):
        args = argparse.Namespace(out_dir=str(self.session))
        metadata = {"tracks": [], "failures": []}

        def capture_audio(_args, **kwargs):
            self.assertTrue(callable(kwargs["audio_tap"]))
            self.assertFalse(kwargs["audio_tap"]({"track_id": "mic"}))
            return metadata

        with mock.patch.object(ui, "LiveTranscriptionCoordinator", side_effect=AssertionError("captions must stay off")), \
             mock.patch.object(ui.capture, "run_recording", side_effect=capture_audio):
            self.app._record_worker(args)
        self.assertFalse((self.session / "transcripts" / "live_captions.jsonl").exists())
        self.assertEqual(self.app.events.get_nowait(), ("done", metadata))

    def test_live_caption_toggle_can_start_stop_and_restart_within_recording(self):
        args = argparse.Namespace(out_dir=str(self.session))
        self.app.recording = True
        self.app.current_out_dir = self.session
        coordinators = [mock.Mock(), mock.Mock()]
        for coordinator in coordinators:
            coordinator.submit.return_value = True
        metadata = {"tracks": [], "failures": []}

        def wait_for(predicate):
            deadline = time.monotonic() + 2
            while not predicate() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(predicate())

        def record_audio(_args, **kwargs):
            tap = kwargs["audio_tap"]
            self.assertFalse(tap({"track_id": "mic"}))
            self.app.live_enabled_var.set(True)
            self.app.on_live_caption_toggle()
            wait_for(lambda: coordinators[0].start.called)
            self.assertTrue(tap({"track_id": "mic"}))
            self.app.live_enabled_var.set(False)
            self.app.on_live_caption_toggle()
            self.assertFalse(tap({"track_id": "mic"}))
            wait_for(lambda: coordinators[0].stop.called)
            self.app.live_enabled_var.set(True)
            self.app.on_live_caption_toggle()
            wait_for(lambda: coordinators[1].start.called)
            self.assertTrue(tap({"track_id": "mic"}))
            return metadata

        with mock.patch.object(ui, "LiveTranscriptionCoordinator", side_effect=coordinators), \
             mock.patch.object(ui.capture, "run_recording", side_effect=record_audio):
            self.app._record_worker(args)
        self.assertEqual(coordinators[0].submit.call_count, 1)
        self.assertEqual(coordinators[1].submit.call_count, 1)
        coordinators[1].stop.assert_called_once()
        self.assertEqual(self.app.events.get_nowait(), ("done", metadata))

    def test_live_caption_toggle_stays_available_during_recording(self):
        self.app.live_enabled_var.set(False)
        self.app.on_live_caption_toggle()
        self.assertIn("next recording", self.app.live_status_var.get().lower())
        with mock.patch.object(self.app, "_record_worker"), \
             mock.patch.object(self.app, "start_screen_context"), \
             mock.patch.object(self.app, "snapshot_external_screenshots", return_value={}):
            self.app.start()
        self.app.worker.join(1)
        self.assertEqual(str(self.app.live_enabled_checkbox["state"]), "normal")
        self.app.recording = False

    def test_caption_stop_error_does_not_prevent_toggling_back_on(self):
        args = argparse.Namespace(out_dir=str(self.session))
        self.app.recording = True
        first, second = mock.Mock(), mock.Mock()
        first.stop.side_effect = RuntimeError("caption stop failed")
        second.submit.return_value = True
        metadata = {"tracks": [], "failures": []}

        def wait_for(predicate):
            deadline = time.monotonic() + 2
            while not predicate() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(predicate())

        def record_audio(_args, **kwargs):
            self.app.live_enabled_var.set(True)
            self.app.on_live_caption_toggle()
            wait_for(lambda: first.start.called)
            self.app.live_enabled_var.set(False)
            self.app.on_live_caption_toggle()
            wait_for(lambda: first.stop.called)
            self.app.live_enabled_var.set(True)
            self.app.on_live_caption_toggle()
            wait_for(lambda: second.start.called)
            self.assertTrue(kwargs["audio_tap"]({"track_id": "mic"}))
            return metadata

        with mock.patch.object(ui, "LiveTranscriptionCoordinator", side_effect=[first, second]), \
             mock.patch.object(ui.capture, "run_recording", side_effect=record_audio):
            self.app._record_worker(args)
        events = list(self.app.events.queue)
        self.assertTrue(second.start.called)
        self.assertIn(("done", metadata), events)

    def test_default_device_ids_select_matching_captured_tracks(self):
        tracks = [
            {"kind": "system", "track_id": "speakers-a", "device": {"id": "audio-a", "name": "Monitor"}},
            {"kind": "system", "track_id": "speakers-b", "device": {"id": "audio-b", "name": "Headset"}},
            {"kind": "microphone", "track_id": "mic-a", "device": {"id": "mic-a-id", "name": "Stereo Mix"}},
            {"kind": "microphone", "track_id": "mic-b", "device": {"id": "mic-b-id", "name": "Headset Mic"}},
        ]
        self.assertEqual(ui.preferred_live_tracks(tracks, {"system": "audio-b", "microphone": "mic-b-id"}),
                         {"system": "speakers-b", "microphone": "mic-b"})

    def test_stereo_mix_default_prefers_available_physical_microphone(self):
        tracks = [
            {"kind": "microphone", "track_id": "mix", "device": {"id": "default", "name": "Stereo Mix"}},
            {"kind": "microphone", "track_id": "headset", "device": {"id": "headset-id", "name": "Headset"}},
        ]
        self.assertEqual(ui.preferred_live_tracks(tracks, {"microphone": "default"}),
                         {"microphone": "headset"})


if __name__ == "__main__":
    unittest.main()
