import json
import queue
import sys
import tempfile
import threading
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import recover_session
import windows_capture_ui


class RecoverSessionTests(unittest.TestCase):
    def test_interrupted_capture_is_listed_as_partial_not_clean(self):
        with tempfile.TemporaryDirectory() as temp:
            session = Path(temp) / '20261015-145058'
            audio = session / 'system' / 'system-1-test.wav'
            audio.parent.mkdir(parents=True)
            with wave.open(str(audio), 'wb') as stream:
                stream.setnchannels(1)
                stream.setsampwidth(2)
                stream.setframerate(48000)
                stream.writeframes(b'\0\0' * 48000)

            recover_session.recover(session, overwrite=False)

            metadata = json.loads((session / 'metadata.json').read_text())
            self.assertFalse(metadata['ok'])
            self.assertTrue(metadata['partial'])
            self.assertTrue(metadata['recovered'])
            self.assertEqual(metadata['duration_seconds'], 1)
            self.assertEqual(len(metadata['tracks']), 1)

    def test_recording_exception_preserves_diagnostics_and_recovers_finalized_wav(self):
        with tempfile.TemporaryDirectory() as temp:
            session = Path(temp) / '20261015-145058'
            audio = session / 'system' / 'system-1-test.wav'
            audio.parent.mkdir(parents=True)
            with wave.open(str(audio), 'wb') as stream:
                stream.setnchannels(1)
                stream.setsampwidth(2)
                stream.setframerate(48000)
                stream.writeframes(b'\0\0' * 48000)
            app = SimpleNamespace(events=queue.Queue(), live_enabled_for_recording=False,
                                  live_control_stop=threading.Event(), live_control_event=threading.Event(),
                                  live_control_thread=None, live_coordinator=None, live_generation=0)
            args = SimpleNamespace(out_dir=str(session))
            with mock.patch.object(windows_capture_ui.capture, 'run_recording', side_effect=RuntimeError('device closed')), \
                 mock.patch.object(windows_capture_ui, 'LiveTranscriptionCoordinator'):
                windows_capture_ui.CaptureApp._record_worker(app, args)

            self.assertEqual(app.events.get_nowait()[0], 'error')
            self.assertTrue((session / 'metadata.json').exists())
            diagnostics = json.loads((session / 'capture_error.json').read_text())
            self.assertEqual(diagnostics['type'], 'RuntimeError')
            self.assertEqual(diagnostics['message'], 'device closed')
            self.assertIn('RuntimeError: device closed', diagnostics['traceback'])


if __name__ == '__main__':
    unittest.main()
