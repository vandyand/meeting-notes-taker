"""Regression checks for manual processing backends and checkpointed recovery."""

import contextlib
import io
import json
import os
import sys
import threading
import tempfile
import unittest
import urllib.error
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import process_latest
import windows_ocr


class BackendTests(unittest.TestCase):
    def test_slow_ollama_startup_waits_past_60_seconds_without_second_launch(self):
        clock = {"seconds": 0}
        updates = []
        unavailable = urllib.error.URLError(ConnectionRefusedError("not ready"))

        def models(_host, _timeout):
            if clock["seconds"] < 65:
                raise unavailable
            return ["glm-ocr:latest"]

        with (
            mock.patch.object(windows_ocr, "ollama_models", side_effect=models),
            mock.patch.object(windows_ocr, "start_ollama_server", return_value=SimpleNamespace(pid=4321)) as start,
            mock.patch.object(windows_ocr.time, "monotonic", side_effect=lambda: clock["seconds"]),
            mock.patch.object(windows_ocr.time, "sleep", side_effect=lambda duration: clock.__setitem__("seconds", clock["seconds"] + duration)),
        ):
            state = windows_ocr.ensure_ollama_ready("http://127.0.0.1:11434", progress=lambda message, **_extra: updates.append(message))
        self.assertEqual(state["ollama_pid"], 4321)
        self.assertGreaterEqual(clock["seconds"], 65)
        start.assert_called_once()
        self.assertIn("Waiting for OCR engine to become ready", updates)

    def test_zero_selected_images_writes_satisfied_checkpoint_without_ollama(self):
        with tempfile.TemporaryDirectory() as temporary:
            session = Path(temporary)
            (session / "screenshots").mkdir()
            with (
                mock.patch.object(sys, "argv", ["windows_ocr.py", str(session)]),
                mock.patch.object(windows_ocr, "ensure_ollama_ready", side_effect=AssertionError("must not start")),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(windows_ocr.main(), 0)
            self.assertEqual(process_latest.completed_ocr_summary(session), {"selected": 0, "succeeded": 0, "failed": 0})

    def test_cloud_ocr_uses_same_text_shape_as_local_ocr(self):
        captured = {}

        class Response:
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def read(self): return b'{"id":"gen-test-ocr","choices":[{"message":{"content":"Quarterly plan"}}],"usage":{"cost":0.000123}}'

        def urlopen(request, timeout):
            captured.update(json.loads(request.data))
            self.assertEqual(timeout, 40)
            return Response()

        with tempfile.TemporaryDirectory() as temporary:
            image = Path(temporary) / "image.png"
            image.write_bytes(b"png")
            with (mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-only"}),
                  mock.patch.object(windows_ocr.urllib.request, "urlopen", side_effect=urlopen)):
                self.assertEqual(windows_ocr.openrouter_generate("openai/gpt-6-luna", image, 40), {
                    "text": "Quarterly plan", "cost_usd": 0.000123, "generation_id": "gen-test-ocr",
                })
        self.assertEqual(captured["model"], "openai/gpt-6-luna")
        self.assertEqual(captured["usage"], {"include": True})
        self.assertEqual(captured["messages"][0]["content"][1]["type"], "image_url")

    def test_cloud_ocr_checkpoint_persists_response_cost(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as temporary:
            session = Path(temporary)
            screenshots = session / "screenshots"
            screenshots.mkdir()
            Image.new("RGB", (20, 20), "white").save(screenshots / "screen-20260929-120000-0000.png")
            with (
                mock.patch.object(sys, "argv", ["windows_ocr.py", str(session), "--backend", "openrouter"]),
                mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-only"}),
                mock.patch.object(windows_ocr, "openrouter_generate", return_value={
                    "text": "Visible title", "cost_usd": 0.000123, "generation_id": "gen-test-ocr",
                }),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(windows_ocr.main(), 0)
            saved = json.loads((session / "visual" / "ocr.json").read_text())["results"][0]
            self.assertEqual(saved["text"], "Visible title")
            self.assertEqual(saved["cost_usd"], 0.000123)
            self.assertEqual(saved["generation_id"], "gen-test-ocr")

    def test_cloud_ocr_requests_overlap_but_results_keep_capture_order(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as temporary:
            session = Path(temporary)
            screenshots = session / 'screenshots'
            screenshots.mkdir()
            first, second = screenshots / 'first.png', screenshots / 'second.png'
            Image.new('RGB', (50, 50), 'white').save(first)
            Image.new('RGB', (50, 50), 'black').save(second)
            second_started = threading.Event()
            overlap = {'yes': False}
            def cloud(_model, path, _timeout):
                if path.name == 'first.png':
                    overlap['yes'] = second_started.wait(1)
                    return {'text': 'First', 'cost_usd': 0.001, 'generation_id': 'one'}
                second_started.set()
                return {'text': 'Second', 'cost_usd': 0.002, 'generation_id': 'two'}
            with mock.patch.object(sys, 'argv', ['windows_ocr.py', str(session), '--backend', 'openrouter']), \
                 mock.patch.dict(os.environ, {'OPENROUTER_API_KEY': 'test-only'}), \
                 mock.patch.object(windows_ocr, 'openrouter_generate', side_effect=cloud), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(windows_ocr.main(), 0)
            self.assertTrue(overlap['yes'], 'Cloud OCR calls should overlap')
            results = json.loads((session / 'visual' / 'ocr.json').read_text())['results']
            self.assertEqual([row['text'] for row in results], ['First', 'Second'])

    def test_default_notes_request_uses_gpt_6_luna(self):
        with tempfile.TemporaryDirectory() as temporary:
            session = Path(temporary)
            args = SimpleNamespace(recordings_dir=str(session.parent), latest=False, session_dir=str(session),
                                   diagnostics_only=False, skip_ocr=True, force=False, force_ocr=False,
                                   ocr_model="auto", ocr_backend="local", transcription_backend="local",
                                   whisper_model="turbo", whisper_device="auto", openrouter_model=None)
            with (
                mock.patch.dict(os.environ, {"OPENROUTER_MODEL": ""}),
                mock.patch.object(process_latest, "generate_diagnostics", return_value={"selected_tracks": []}),
                mock.patch.object(process_latest, "run_ocr", return_value=[]),
                mock.patch.object(process_latest, "run_whisper", return_value=[]),
                mock.patch.object(process_latest, "run_cleanup", return_value=[]),
                mock.patch.object(process_latest, "openrouter_notes", return_value={"title": "Test"}),
            ):
                result = process_latest.process(args)
            self.assertEqual(result["model"], "openai/gpt-6-luna")

    def test_resume_does_not_accept_truncated_saved_notes_response(self):
        with tempfile.TemporaryDirectory() as temporary:
            session = Path(temporary)
            transcripts = session / 'transcripts'
            transcripts.mkdir()
            (transcripts / 'notes.md').write_text('Old notes')
            (transcripts / 'notes_openrouter_response.json').write_text(json.dumps({
                'choices': [{'finish_reason': 'length', 'message': {'content': '{"title":'}}]}))
            args = SimpleNamespace(recordings_dir=str(session.parent), latest=False, session_dir=str(session),
                                   diagnostics_only=False, skip_ocr=True, force=False, force_ocr=False,
                                   ocr_model='auto', ocr_backend='local', transcription_backend='local',
                                   whisper_model='turbo', whisper_device='auto', openrouter_model=None)
            with mock.patch.object(process_latest, 'generate_diagnostics', return_value={'selected_tracks': []}), \
                 mock.patch.object(process_latest, 'run_ocr', return_value=[]), \
                 mock.patch.object(process_latest, 'run_whisper', return_value=[]), \
                 mock.patch.object(process_latest, 'run_cleanup', return_value=[]), \
                 mock.patch.object(process_latest, 'openrouter_notes', return_value={'title': 'Fresh title'}) as notes:
                process_latest.process(args)
            notes.assert_called_once()

    def test_ocr_failure_still_transcribes_and_holds_notes(self):
        with tempfile.TemporaryDirectory() as temporary:
            session = Path(temporary)
            (session / "screenshots").mkdir()
            (session / "screenshots" / "image.png").touch()
            args = SimpleNamespace(recordings_dir=str(session.parent), latest=False, session_dir=str(session),
                                   diagnostics_only=False, skip_ocr=False, force=False, force_ocr=False,
                                   ocr_model="auto", ocr_backend="local", transcription_backend="local",
                                   whisper_model="turbo", whisper_device="auto", openrouter_model=None)
            with (
                mock.patch.object(process_latest, "generate_diagnostics", return_value={"selected_tracks": []}),
                mock.patch.object(process_latest, "run_ocr", side_effect=RuntimeError("OCR unavailable")),
                mock.patch.object(process_latest, "run_whisper", return_value=["audio done"]) as audio,
                mock.patch.object(process_latest, "run_cleanup", return_value=["cleanup done"]) as cleanup,
                mock.patch.object(process_latest, "openrouter_notes", side_effect=AssertionError("notes must wait")),
            ):
                with self.assertRaisesRegex(RuntimeError, "OCR unavailable"):
                    process_latest.process(args)
            audio.assert_called_once()
            cleanup.assert_called_once()

    def test_cloud_ocr_and_local_audio_overlap_but_notes_wait_for_both(self):
        with tempfile.TemporaryDirectory() as temporary:
            session = Path(temporary)
            screenshots = session / 'screenshots'
            screenshots.mkdir()
            (screenshots / 'image.png').touch()
            args = SimpleNamespace(recordings_dir=str(session.parent), latest=False, session_dir=str(session),
                                   diagnostics_only=False, skip_ocr=False, force=False, force_ocr=False,
                                   ocr_model='auto', ocr_backend='openrouter', transcription_backend='local',
                                   whisper_model='turbo', whisper_device='auto', openrouter_model=None)
            audio_started = threading.Event()
            ocr_started = threading.Event()
            audio_done = threading.Event()
            overlap = {'yes': False}

            def ocr(_session, _args):
                ocr_started.set()
                overlap['yes'] = audio_started.wait(1)
                return ['ocr done']

            def audio(_session, _tracks, _model, _device):
                audio_started.set()
                self.assertTrue(ocr_started.wait(1))
                audio_done.set()
                return ['audio done']

            def notes(_session, _model, **_kwargs):
                self.assertTrue(audio_done.is_set())
                self.assertTrue(ocr_started.is_set())
                return {'title': 'Ready'}

            with mock.patch.object(process_latest, 'generate_diagnostics', return_value={'selected_tracks': []}), \
                 mock.patch.object(process_latest, 'completed_ocr_summary', return_value={'selected': 1}), \
                 mock.patch.object(process_latest, 'run_ocr', side_effect=ocr), \
                 mock.patch.object(process_latest, 'run_whisper', side_effect=audio), \
                 mock.patch.object(process_latest, 'run_cleanup', return_value=['cleanup done']), \
                 mock.patch.object(process_latest, 'openrouter_notes', side_effect=notes):
                result = process_latest.process(args)
            self.assertTrue(overlap['yes'])
            self.assertEqual(result['messages'], ['ocr done', 'audio done', 'cleanup done'])

    def test_both_local_processing_keeps_ocr_before_audio(self):
        with tempfile.TemporaryDirectory() as temporary:
            session = Path(temporary)
            screenshots = session / 'screenshots'
            screenshots.mkdir()
            (screenshots / 'image.png').touch()
            args = SimpleNamespace(recordings_dir=str(session.parent), latest=False, session_dir=str(session),
                                   diagnostics_only=False, skip_ocr=False, force=False, force_ocr=False,
                                   ocr_model='auto', ocr_backend='local', transcription_backend='local',
                                   whisper_model='turbo', whisper_device='auto', openrouter_model=None)
            order = []
            with mock.patch.object(process_latest, 'generate_diagnostics', return_value={'selected_tracks': []}), \
                 mock.patch.object(process_latest, 'completed_ocr_summary', return_value={'selected': 1}), \
                 mock.patch.object(process_latest, 'run_ocr', side_effect=lambda *_args: order.append('ocr') or []), \
                 mock.patch.object(process_latest, 'run_whisper', side_effect=lambda *_args: order.append('audio') or []), \
                 mock.patch.object(process_latest, 'run_cleanup', side_effect=lambda *_args: order.append('cleanup') or []), \
                 mock.patch.object(process_latest, 'openrouter_notes', side_effect=lambda *_args, **_kwargs: order.append('notes') or {'title': 'Done'}):
                process_latest.process(args)
            self.assertEqual(order, ['ocr', 'audio', 'cleanup', 'notes'])

    def test_parallel_progress_never_moves_backward_when_a_stage_retries(self):
        with mock.patch.object(process_latest, 'PARALLEL_PROGRESS', {}), \
             mock.patch.object(process_latest, 'ACTIVE_SESSION', None), \
             mock.patch.dict(os.environ, {'MNT_PROGRESS': '0'}):
            percents = []
            for message, stage, percent in [
                ('OCR image 1', 'ocr', 30),
                ('Audio chunk 2', 'transcript', 68),
                ('Retry OCR image 1', 'ocr', 12),
            ]:
                process_latest.progress(message, stage=stage, percent=percent)
                percents.append(process_latest.LAST_PROGRESS['percent'])
            self.assertEqual(percents, sorted(percents))
            self.assertEqual(set(process_latest.LAST_PROGRESS['parallel_stages']), {'ocr', 'audio'})
            self.assertEqual(process_latest.LAST_PROGRESS['parallel_stages']['ocr']['progress'], 'Retry OCR image 1')

    def test_cloud_audio_resume_reuses_chunk_and_stitches_timestamps(self):
        with tempfile.TemporaryDirectory() as temporary:
            session = Path(temporary)
            source = session / "track.wav"
            with wave.open(str(source), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(1000)
                wav.writeframes(b"\x00\x00" * 121000)
            track = {"local_path": str(source)}
            responses = [
                {"text": "First", "segments": [{"start": 0.0, "end": 1.0, "text": "First"}]},
                {"text": "Second", "segments": [{"start": 0.0, "end": 1.0, "text": "Second"}]},
            ]
            with (mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-only"}),
                  mock.patch.object(process_latest, "transcribe_cloud_chunk", side_effect=responses) as transcribe):
                process_latest.run_cloud_transcription(session, [track], "openai/whisper-large-v3-turbo")
            self.assertEqual(transcribe.call_count, 2)
            result = json.loads((session / "transcripts" / "track.json").read_text())
            self.assertEqual([segment["start"] for segment in result["segments"]], [0.0, 120.0])
            (session / "transcripts" / "track.json").unlink()
            with (mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-only"}),
                  mock.patch.object(process_latest, "transcribe_cloud_chunk", side_effect=AssertionError("must reuse"))):
                process_latest.run_cloud_transcription(session, [track], "openai/whisper-large-v3-turbo")

    def test_cloud_audio_chunks_overlap_and_stitch_in_time_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            session = Path(temporary)
            source = session / 'track.wav'
            with wave.open(str(source), 'wb') as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(1000)
                wav.writeframes(b'\x01\x00' * 120000 + b'\x02\x00' * 1000)
            second_started = threading.Event()
            overlap = {'yes': False}
            def transcribe(audio, _model):
                marker = audio[-2:]
                if marker == b'\x01\x00':
                    overlap['yes'] = second_started.wait(1)
                    return {'text': 'First', 'segments': [{'start': 0, 'end': 1, 'text': 'First'}]}
                second_started.set()
                return {'text': 'Second', 'segments': [{'start': 0, 'end': 1, 'text': 'Second'}]}
            with mock.patch.dict(os.environ, {'OPENROUTER_API_KEY': 'test-only'}), \
                 mock.patch.object(process_latest, 'transcribe_cloud_chunk', side_effect=transcribe):
                process_latest.run_cloud_transcription(session, [{'local_path': str(source)}], 'test-model')
            self.assertTrue(overlap['yes'], 'Cloud audio calls should overlap')
            data = json.loads((session / 'transcripts' / 'track.json').read_text())
            self.assertEqual(data['text'], 'First Second')
            self.assertEqual([segment['start'] for segment in data['segments']], [0.0, 120.0])


if __name__ == "__main__":
    unittest.main()
