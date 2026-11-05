import io
import gc
import json
import sys
import tempfile
import threading
import time
import tkinter as tk
from tkinter import ttk
import unittest
import urllib.error
from types import SimpleNamespace
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import process_latest as processor
import windows_capture_ui as ui
import windows_ocr as ocr


class NotesInputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.session = Path(self.temp.name)
        self.out = self.session / 'transcripts'
        self.out.mkdir()
        (self.out / 'cleaned.md').write_text('Accepted speech\n## Dropped Segments\nRejected hallucination', encoding='utf-8')

    def test_request_uses_all_kept_speech_not_rejected_audit_or_repeated_device_ids(self):
        (self.out / 'cleaned.json').write_text(json.dumps({'kept': [
            {'text': 'Ship Friday.', 'speaker': 'System', 'track_id': 'long-device-id', 'start': 2, 'end': 5},
            {'text': 'Only after QA.', 'speaker': 'Mic', 'track_id': 'mic-id', 'start': 6, 'end': 8},
        ], 'dropped': [{'text': 'Rejected hallucination'}]}))
        requests = []
        def respond(request, **kwargs):
            requests.append(json.loads(request.data))
            return io.BytesIO(json.dumps({'choices': [{'message': {'content': json.dumps({'notes_md': 'Notes', 'title': 'Test'})}}]}).encode())
        with mock.patch.object(processor, 'openrouter_api_key', return_value='test-only'), mock.patch.object(processor.urllib.request, 'urlopen', side_effect=respond):
            processor.openrouter_notes(self.session, 'test-model')
        prompt = requests[0]['messages'][1]['content']
        self.assertIn('Ship Friday.', prompt)
        self.assertIn('Only after QA.', prompt)
        self.assertIn('00:02', prompt)
        self.assertNotIn('Rejected hallucination', prompt)
        self.assertEqual(prompt.count('long-device-id'), 1)

    def test_402_is_actionable_and_does_not_delete_checkpoints(self):
        checkpoint = self.out / 'audio.json'
        checkpoint.write_text('{"text":"saved"}')
        error = urllib.error.HTTPError('https://test', 402, 'credits', {}, io.BytesIO(b'{"error":{"message":"Prompt tokens limit exceeded: 72515 > 27730","metadata":{"limit_source":"openrouter_credits"}}}'))
        with mock.patch.object(processor, 'openrouter_api_key', return_value='test-only'), mock.patch.object(processor.urllib.request, 'urlopen', side_effect=error):
            with self.assertRaises(RuntimeError) as caught:
                processor.openrouter_notes(self.session, 'test-model')
        self.assertIn('Resume processing', str(caught.exception))
        self.assertIn('https://openrouter.ai/settings/credits', str(caught.exception))
        self.assertEqual(checkpoint.read_text(), '{"text":"saved"}')
        self.assertFalse((self.out / 'notes.md').exists())

    def test_truncated_notes_response_is_not_marked_complete(self):
        incomplete = '{"title":"Useful title","notes_md":"## Summary\\nUseful","action_items":['
        response = {'choices': [{'message': {'content': incomplete}, 'finish_reason': 'length'}]}
        with mock.patch.object(processor, 'openrouter_api_key', return_value='test-only'), \
             mock.patch.object(processor.urllib.request, 'urlopen', return_value=io.BytesIO(json.dumps(response).encode())):
            with self.assertRaisesRegex(RuntimeError, 'truncat|length'):
                processor.openrouter_notes(self.session, 'test-model')
        self.assertFalse((self.out / 'notes.md').exists())
        self.assertFalse((self.session / 'meeting.json').exists())
        self.assertTrue((self.out / 'notes_openrouter_response.json').exists())

    def test_generated_notes_keep_user_edited_meeting_title(self):
        (self.session / 'meeting.json').write_text(json.dumps({'title': 'My meeting title', 'title_updated_by_user': True}))
        response = {'choices': [{'message': {'content': json.dumps({'title': 'Model suggestion', 'notes_md': '## Summary\nDone'})},
                                 'finish_reason': 'stop'}]}
        with mock.patch.object(processor, 'openrouter_api_key', return_value='test-only'), \
             mock.patch.object(processor.urllib.request, 'urlopen', return_value=io.BytesIO(json.dumps(response).encode())):
            processor.openrouter_notes(self.session, 'test-model')
        meeting = json.loads((self.session / 'meeting.json').read_text())
        self.assertEqual(meeting['title'], 'My meeting title')
        self.assertEqual(meeting['recommended_title'], 'Model suggestion')
        self.assertTrue(meeting['title_updated_by_user'])

    def test_recover_saved_truncated_notes_without_network(self):
        notes = '## Summary\nUseful.\n\n## Action Items\n- **Andrew:** Follow up.\n\n## Open Questions\n- When?'
        incomplete = '{"title":"Recovered title","notes_md":' + json.dumps(notes) + ',"action_items":['
        response = {'choices': [{'message': {'content': incomplete}, 'finish_reason': 'length'}], 'model': 'test-model'}
        (self.out / 'notes_openrouter_response.json').write_text(json.dumps(response))
        (self.out / 'notes.md').write_text(incomplete)
        (self.session / 'meeting.json').write_text(json.dumps({'title': 'Untitled meeting', 'title_updated_by_user': False}))
        processor.recover_saved_truncated_notes(self.session)
        self.assertEqual((self.out / 'notes.md').read_text(), notes + '\n')
        self.assertIn('Follow up.', (self.out / 'actions.md').read_text())
        self.assertEqual(json.loads((self.session / 'meeting.json').read_text())['title'], 'Recovered title')

    def test_resumed_progress_replaces_persisted_failure(self):
        processor.write_process_state(self.session, False, 'old error')
        with mock.patch.object(processor, 'ACTIVE_SESSION', self.session):
            processor.progress('Loading audio', stage='transcript', item='mic.wav')
        state = json.loads((self.out / 'process_state.json').read_text())
        self.assertEqual(state['status'], 'running')
        self.assertIsNone(state['error'])
        self.assertEqual(state['last_progress']['item'], 'mic.wav')


class DecoderProgressTests(unittest.TestCase):
    def test_screenshot_selection_decodes_each_source_once(self):
        from PIL import Image, ImageDraw
        with tempfile.TemporaryDirectory() as temp:
            images = [Path(temp) / f'image-{n}.png' for n in range(4)]
            for path in images:
                image = Image.new('RGB', (40, 40), 'white')
                if path in images[2:]:
                    ImageDraw.Draw(image).rectangle((8, 8, 31, 31), fill='black')
                image.save(path)
            opened = []
            def load(path):
                opened.append(path)
                return Image.open(path)
            selected, _ = ocr.select_screenshots(
                images, image_loader=load, near_duplicate_fraction=.0002)
            self.assertEqual(selected, [images[0], images[2]])
            self.assertEqual(opened, images)

    def test_screenshot_analysis_reports_examined_frames_before_ocr(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as temp:
            images = [Path(temp) / f'image-{n}.png' for n in range(3)]
            for path in images:
                Image.new('RGB', (20, 20)).save(path)
            seen = []
            selected, _ = ocr.select_screenshots(images, on_progress=lambda current, total: seen.append((current, total)))
            self.assertEqual(seen, [(1, 3), (2, 3), (3, 3)])
            self.assertEqual(selected, images[:1])

    def test_audio_position_percent_is_per_file_and_resets(self):
        import whisper_progress
        events = []
        with mock.patch.object(whisper_progress, 'emit', side_effect=lambda **event: events.append(event)):
            with whisper_progress.FrameProgress(total=12000, unit='frames', disable=False) as bar:
                bar.update(3000)
            with whisper_progress.FrameProgress(total=6000, unit='frames', disable=False) as bar:
                bar.update(600)
        self.assertEqual(events[1]['item_percent'], 25)
        self.assertEqual(events[1]['audio_seconds'], 30)
        self.assertEqual(events[1]['audio_total_seconds'], 120)
        # Each file starts at zero regardless of the previous file's final event.
        self.assertEqual([e['item_percent'] for e in events if e['audio_seconds'] == 0], [0, 0])
        self.assertTrue(any(e['item_percent'] == 10 and e['audio_total_seconds'] == 60 for e in events))

    def test_real_whisper_loop_counts_skipped_silence_in_audio_position(self):
        import importlib
        import numpy as np
        import torch
        import whisper_progress
        from whisper.tokenizer import get_tokenizer
        module = importlib.import_module('whisper.transcribe')
        tokenizer = get_tokenizer(False, language='en', task='transcribe')
        calls = []
        def decode(_mel, _options):
            silent = not calls
            calls.append(True)
            return SimpleNamespace(tokens=tokenizer.encode('Fixture speech.'), text='Fixture speech.',
                                   avg_logprob=-2 if silent else 0, no_speech_prob=1 if silent else 0,
                                   compression_ratio=1, temperature=0)
        model = SimpleNamespace(device=torch.device('cpu'), is_multilingual=False, num_languages=99,
                                dims=SimpleNamespace(n_mels=80, n_audio_ctx=1500, n_text_ctx=448), decode=decode)
        events = []
        with mock.patch.object(module, 'tqdm', SimpleNamespace(tqdm=whisper_progress.FrameProgress)), mock.patch.object(whisper_progress, 'emit', side_effect=lambda **event: events.append(event)), mock.patch.object(torch.cuda, 'is_available', return_value=False):
            module.transcribe(model, np.zeros(33 * 16000, dtype=np.float32), verbose=False, language='en', fp16=False)
        self.assertEqual(len(calls), 2)
        self.assertEqual(events[-1]['audio_seconds'], 33)
        self.assertEqual(events[-1]['item_percent'], 100)

    def test_cli_zero_exit_without_output_is_not_success(self):
        with tempfile.TemporaryDirectory() as temp:
            session = Path(temp)
            process = SimpleNamespace(stdout=io.StringIO(''), poll=lambda: 0, returncode=0)
            with mock.patch.object(processor.subprocess, 'Popen', return_value=process), mock.patch.object(processor, 'whisper_device', return_value='cpu'):
                with self.assertRaisesRegex(RuntimeError, 'missing.*mic'):
                    processor.run_whisper(session, [{'local_path': str(session / 'mic.wav')}], 'turbo')
        self.assertEqual(processor.LAST_PROGRESS['failed_items'], ['mic.wav'])

    def test_failed_decode_does_not_publish_100_percent(self):
        import whisper_progress
        events = []
        with mock.patch.object(whisper_progress, 'emit', side_effect=lambda **event: events.append(event)):
            with self.assertRaises(RuntimeError):
                with whisper_progress.FrameProgress(total=6000) as bar:
                    bar.update(1000)
                    raise RuntimeError('decoder failed')
        self.assertLess(events[-1]['item_percent'], 100)

    def test_all_silent_decode_reports_completion_on_successful_exit(self):
        import whisper_progress
        events = []
        with mock.patch.object(whisper_progress, 'emit', side_effect=lambda **event: events.append(event)):
            with whisper_progress.FrameProgress(total=6000):
                pass
        self.assertEqual(events[-1]['audio_seconds'], 60)
        self.assertEqual(events[-1]['item_percent'], 100)


class FeedbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.session = Path(self.temp.name) / '20260914-110401'
        self.session.mkdir()
        (self.session / 'transcripts').mkdir()
        (self.session / 'metadata.json').write_text('{}')
        self.write_state('failed', 'ocr', 'old HTTP 400 error')
        root = tk.Tk()
        root.withdraw()
        def close_root():
            for callback in root.tk.call('after', 'info'):
                root.tk.call('after', 'cancel', callback)
            root.destroy()
            self.app = None
            self.root = None
            # Tk objects must be reclaimed on the thread that owns Tcl, not by
            # the next test's thumbnail worker triggering cyclic collection.
            gc.collect()
        self.addCleanup(close_root)
        with mock.patch.object(ui, 'default_recordings_dir', return_value=Path(self.temp.name)), mock.patch.object(ui.CaptureApp, 'refresh_devices'),                 mock.patch.object(ui.CaptureApp, 'schedule_startup_balance_check'):
            self.app = ui.CaptureApp(root)
        self.root = root
        self.startup_selection = self.app.selected_session
        self.startup_rows = self.app.history.selection()
        self.startup_context = self.app.selected_meeting_var.get()
        self.app.selected_session = self.session
        self.app.load_selected_notes()

    def write_state(self, status, stage, error=None):
        (self.session / 'transcripts' / 'process_state.json').write_text(json.dumps({'status': status, 'error': error, 'last_progress': {'stage': stage}}))

    def test_startup_has_no_selected_meeting_until_user_selects_one(self):
        self.assertIsNone(self.startup_selection)
        self.assertEqual(self.startup_rows, ())
        self.assertEqual(self.startup_context, 'No meeting selected')
        self.app.selected_session = None
        self.app.refresh_history()
        self.assertIsNone(self.app.selected_session)
        self.assertEqual(self.app.history.selection(), ())
        self.assertEqual(self.app.selected_meeting_var.get(), 'No meeting selected')
        self.assertEqual(self.app.process_progress_var.get(), 0)
        self.assertEqual(str(self.app.process_selected_button['state']), 'disabled')
        self.assertEqual(str(self.app.edit_title_button['state']), 'disabled')
        self.assertEqual(str(self.app.open_notes_button['state']), 'disabled')
        self.app.refresh_latest()
        self.assertIsNone(self.app.selected_session)
        self.assertEqual(self.app.selected_meeting_var.get(), 'No meeting selected')
        row = self.app.history.get_children()[0]
        self.app.history.selection_set(row)
        self.app.on_history_select()
        self.assertEqual(self.app.selected_session, self.session)
        self.assertNotEqual(self.app.selected_meeting_var.get(), 'No meeting selected')

    def test_progress_follows_selected_meeting(self):
        (self.session / 'transcripts' / 'notes.md').write_text('Finished notes')
        self.write_state('complete', 'complete')
        self.app.load_selected_notes()
        self.assertEqual(self.app.process_progress_var.get(), 100)
        self.assertEqual(self.app.process_stage_var.get(), 'Complete')
        fresh = Path(self.temp.name) / 'new-meeting'
        fresh.mkdir()
        (fresh / 'metadata.json').write_text('{}')
        self.app.selected_session = fresh
        self.app.load_selected_notes()
        self.assertEqual(self.app.process_progress_var.get(), 0)
        self.assertNotEqual(self.app.process_stage_var.get(), 'Complete')
        self.app.selected_session = self.session
        self.app.load_selected_notes()
        self.assertEqual(self.app.process_progress_var.get(), 100)

    def test_selected_meeting_has_visible_edit_title_action(self):
        self.app.load_selected_notes()
        self.app.edit_title_button.invoke()
        dialog = next(widget for widget in self.root.winfo_children() if isinstance(widget, tk.Toplevel))
        entry = next(widget for widget in dialog.winfo_children()[0].winfo_children() if isinstance(widget, ui.ttk.Entry))
        entry.delete(0, tk.END)
        entry.insert(0, 'Trading Risk Planning')
        buttons = next(widget for widget in dialog.winfo_children()[0].winfo_children() if isinstance(widget, ui.ttk.Frame))
        next(button for button in buttons.winfo_children() if button.cget('text') == 'Save title').invoke()
        self.assertEqual(json.loads((self.session / 'meeting.json').read_text())['title'], 'Trading Risk Planning')
        self.assertIn('Trading Risk Planning', self.app.selected_meeting_var.get())

    def test_current_recording_disables_destructive_screenshot_edits(self):
        self.app.recording = True
        self.app.current_out_dir = self.session
        with mock.patch.object(ui, 'ScreenshotReview') as review:
            self.app.review_screenshots()
        self.assertFalse(review.call_args.kwargs['allow_edits'])
        self.app.recording = False

    def test_ocr_preview_shows_cropped_input_and_clears_after_ocr(self):
        from PIL import Image
        import screenshot_edits
        (self.session / 'screenshots').mkdir()
        original = self.session / 'screenshots' / 'image.png'
        Image.new('RGB', (300, 120), 'white').save(original)
        screenshot_edits.save_edit(self.session, None, {'crop': [1/3, 0, 2/3, 1]})
        image = screenshot_edits.ocr_input(self.session, original)
        self.app.processing_running = True
        self.app.processing_session = self.session
        self.app.active_progress_payload = {'stage': 'ocr', 'item': str(original), 'preview_path': str(image), 'current': 2, 'total': 8}
        self.app.update_ocr_preview(self.session, self.app.active_progress_payload)
        deadline = time.monotonic() + 2
        while not self.app.ocr_preview_photo and time.monotonic() < deadline:
            self.root.update()
            time.sleep(.01)
        self.assertEqual(self.app.ocr_preview_path, str(image))
        self.assertIn('2 of 8', self.app.ocr_preview_var.get())
        self.app.update_ocr_preview(self.session, {'stage': 'transcript'})
        self.assertEqual(self.app.ocr_preview_frame.winfo_manager(), '')

    def test_ocr_table_shows_actual_input_and_keeps_original_out_of_error_column(self):
        from PIL import Image
        import screenshot_edits
        (self.session / 'screenshots').mkdir()
        original = self.session / 'screenshots' / 'screen.png'
        Image.new('RGB', (100, 100)).save(original)
        screenshot_edits.save_edit(self.session, None, {'crop': [0, 0, .5, 1]})
        prepared = screenshot_edits.ocr_input(self.session, original)
        (self.session / 'visual').mkdir()
        (self.session / 'visual' / 'ocr.json').write_text(json.dumps({'results': [
            {'path': str(original), 'ocr_input_path': str(prepared), 'ok': True,
             'image_fingerprint': screenshot_edits.fingerprint(self.session, original), 'error': ''}]}))
        self.app.refresh_processing_details()
        values = self.app.ocr_status.item(self.app.ocr_status.get_children()[0], 'values')
        self.assertEqual(values[0], prepared.name)
        self.assertEqual(values[3], '')
        self.assertEqual(values[4], str(original))

    def test_crop_editor_middle_third_saves_global_crop(self):
        from PIL import Image
        from screenshot_crop_ui import CropMaskEditor
        import screenshot_edits
        (self.session / 'screenshots').mkdir()
        path = self.session / 'screenshots' / 'image.png'
        Image.new('RGB', (300, 120), 'white').save(path)
        editor = CropMaskEditor(self.root, self.session, path, False, lambda: None)
        editor.middle_third()
        editor.save()
        self.assertEqual(screenshot_edits.load_image(self.session, path).size, (100, 120))

    def test_new_recording_resets_completed_progress_and_selects_capture(self):
        self.app.process_progress_var.set(100)
        self.app.process_stage_var.set('Complete')
        fresh = Path(self.temp.name) / 'new-recording'
        with mock.patch.object(ui, 'timestamp_dir', return_value=fresh), \
             mock.patch.object(self.app, '_record_worker'), \
             mock.patch.object(self.app, 'start_screen_context'), \
             mock.patch.object(self.app, 'snapshot_external_screenshots', return_value={}):
            self.app.start()
        self.app.worker.join(1)
        self.assertEqual(self.app.process_progress_var.get(), 0)
        self.assertEqual(self.app.process_stage_var.get(), 'Recording')
        self.assertEqual(self.app.selected_session, fresh)
        self.app.refresh_history()
        self.assertEqual(self.app.selected_session, fresh)
        self.app.recording = False

    def test_processing_backends_persist_per_meeting_and_reload(self):
        self.app.ocr_backend_var.set('Cloud (OpenRouter)')
        self.app.transcription_backend_var.set('Local')
        self.app.save_processing_backends()
        saved = json.loads((self.session / 'metadata.json').read_text())
        self.assertEqual(saved['processing_options'], {
            'ocr_backend': 'openrouter', 'transcription_backend': 'local',
        })
        self.app.ocr_backend_var.set('Local')
        self.app.update_processing_context()
        self.assertEqual(self.app.ocr_backend_var.get(), 'Cloud (OpenRouter)')

    def test_unconfigured_meeting_defaults_to_cloud_but_saved_local_is_kept(self):
        self.app.ocr_backend_var.set('Local')
        self.app.transcription_backend_var.set('Local')
        self.app.update_processing_context()
        self.assertEqual(self.app.ocr_backend_var.get(), 'Cloud (OpenRouter)')
        self.assertEqual(self.app.transcription_backend_var.get(), 'Cloud (OpenRouter)')
        (self.session / 'metadata.json').write_text(json.dumps({'processing_options': {
            'ocr_backend': 'local', 'transcription_backend': 'local'}}))
        self.app.update_processing_context()
        self.assertEqual(self.app.ocr_backend_var.get(), 'Local')
        self.assertEqual(self.app.transcription_backend_var.get(), 'Local')

    def test_openrouter_balance_shows_remaining_credits(self):
        self.app.openrouter_key = 'test-key'
        with mock.patch.object(ui, 'fetch_openrouter_balance', return_value=1.3912) as fetch:
            self.app.refresh_openrouter_balance()
            self.app.balance_thread.join(5)
            self.app.poll_openrouter_balance()
        fetch.assert_called_once_with('test-key')
        self.assertIn('$1.39', self.app.openrouter_balance_var.get())
        self.assertNotIn('test-key', self.app.openrouter_balance_var.get())

    def test_openrouter_balance_failure_says_unavailable_not_stale_number(self):
        self.app.openrouter_key = 'test-key'
        self.app.openrouter_balance_var.set('OpenRouter balance: $5.00')
        with mock.patch.object(ui, 'fetch_openrouter_balance', side_effect=OSError('offline')):
            self.app.refresh_openrouter_balance()
            self.app.balance_thread.join(5)
            self.app.poll_openrouter_balance()
        self.assertIn('unavailable', self.app.openrouter_balance_var.get())
        self.assertNotIn('$5.00', self.app.openrouter_balance_var.get())

    def test_fetch_openrouter_balance_subtracts_usage(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return json.dumps({'data': {'total_credits': 725, 'total_usage': 723.61}}).encode()
        with mock.patch.object(ui.urllib.request, 'urlopen', return_value=Response()) as opener:
            self.assertAlmostEqual(ui.fetch_openrouter_balance('k'), 1.39, places=2)
        request = opener.call_args[0][0]
        self.assertEqual(request.full_url, 'https://openrouter.ai/api/v1/credits')
        self.assertEqual(request.get_header('Authorization'), 'Bearer k')

    def test_ocr_provider_is_unavailable_when_ocr_is_off(self):
        screenshots = self.session / 'screenshots'
        screenshots.mkdir()
        (screenshots / 'screen.png').write_bytes(b'fixture')
        self.app.update_processing_context()
        self.assertEqual(str(self.app.ocr_backend_choice.cget('state')), 'readonly')
        self.assertEqual(self.app.ocr_backend_hint_label.winfo_manager(), '')
        self.app.include_screenshot_ocr_button.invoke()
        self.assertEqual(str(self.app.ocr_backend_choice.cget('state')), 'disabled')
        self.assertIn('off', self.app.ocr_backend_hint_var.get().lower())
        self.assertEqual(self.app.ocr_backend_hint_label.winfo_manager(), 'pack')
        self.app.include_screenshot_ocr_button.invoke()
        self.assertEqual(str(self.app.ocr_backend_choice.cget('state')), 'readonly')
        self.assertEqual(self.app.ocr_backend_hint_label.winfo_manager(), '')

    def test_saving_screenshot_choices_preserves_ocr_off(self):
        from PIL import Image
        screenshots = self.session / 'screenshots'
        screenshots.mkdir()
        image = screenshots / 'screen.png'
        Image.new('RGB', (8, 8)).save(image)
        self.app.set_screenshot_ocr_enabled(self.session, False)
        frame = {'path': str(image), 'selected': True, 'automatic_selected': True, 'reason': 'first'}
        with mock.patch.object(ui.visual_ocr, 'select_screenshots', return_value=([image], [frame])):
            self.app.review_screenshots()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                self.root.update()
                dialogs = [widget for widget in self.root.winfo_children() if isinstance(widget, tk.Toplevel)]
                if dialogs and getattr(dialogs[0], 'review', None) and dialogs[0].review.ready:
                    break
                time.sleep(0.01)
        dialog = next(widget for widget in self.root.winfo_children() if isinstance(widget, tk.Toplevel))
        def find_button(widget, label):
            for child in widget.winfo_children():
                if isinstance(child, ui.ttk.Button) and child.cget('text') == label:
                    return child
                found = find_button(child, label)
                if found:
                    return found
            return None
        find_button(dialog, 'Save Choices').invoke()
        self.assertFalse(self.app.screenshot_ocr_enabled(self.session))
        self.assertEqual(str(self.app.ocr_backend_choice.cget('state')), 'disabled')

    def test_screenshot_review_opens_before_slow_analysis_finishes(self):
        from PIL import Image
        root = self.session / 'screenshots'
        root.mkdir()
        Image.new('RGB', (32, 32)).save(root / 'screen.png')
        gate = threading.Event()
        entered = threading.Event()
        original = ui.visual_ocr.select_screenshots
        def slow_analysis(*args, **kwargs):
            entered.set()
            gate.wait(2)
            return original(*args, **kwargs)
        with mock.patch.object(ui.visual_ocr, 'select_screenshots', side_effect=slow_analysis):
            try:
                started = time.monotonic()
                self.app.review_screenshots()
                # Native Tk dialog creation varies with desktop load; this
                # must finish before the deliberately blocked 2-second scan.
                self.assertLess(time.monotonic() - started, 1.5)
                dialog = next(widget for widget in self.root.winfo_children() if isinstance(widget, tk.Toplevel))
                self.assertIn('Analyzing', dialog.review.status_var.get())
                self.assertTrue(entered.wait(1))
                deadline = time.monotonic() + 1
                while not dialog.review.photos and time.monotonic() < deadline:
                    self.root.update()
                    time.sleep(0.01)
                self.assertTrue(dialog.review.photos)
                self.assertEqual(str(dialog.review.save_button.cget('state')), 'normal')
                dialog.review.set_all(False)
                dialog.review.save()
                saved = json.loads((root / 'manifest.json').read_text())
                self.assertFalse(saved['frames'][0]['manual_selected'])
                import screenshot_analysis
                self.assertIsNotNone(screenshot_analysis.manual_frames(self.session, [root / 'screen.png'], {}))
            finally:
                gate.set()

    def test_paged_review_preserves_choices_and_bulk_actions_cover_all_images(self):
        import screenshot_review
        frames = [{'path': str(self.session / 'screenshots' / f'image-{n}.png'),
                   'selected': True, 'automatic_selected': True, 'reason': 'visual-change'}
                  for n in range(85)]
        with mock.patch.object(screenshot_review, 'load_review_frames', return_value=frames):
            self.app.review_screenshots()
            dialog = next(widget for widget in self.root.winfo_children() if isinstance(widget, tk.Toplevel))
            review = dialog.review
            deadline = time.monotonic() + 3
            while not review.ready and time.monotonic() < deadline:
                self.root.update()
                time.sleep(0.01)
        self.assertEqual(len(review.page_states), 40)
        first = frames[0]['path']
        review.page_states[first].set(False)
        review.change_page(1)
        review.change_page(-1)
        self.assertFalse(review.page_states[first].get())
        review.set_all(False)
        review.change_page(1)
        review.change_page(1)
        self.assertEqual(len(review.page_states), 5)
        self.assertFalse(any(state.get() for state in review.page_states.values()))
        last = frames[-1]['path']
        review.page_states[last].set(True)
        review.save()
        saved = json.loads((self.session / 'screenshots' / 'manifest.json').read_text())
        self.assertEqual(saved['source_image_count'], 85)
        self.assertEqual(saved['selected_image_count'], 1)
        self.assertEqual([entry['path'] for entry in saved['frames'] if entry['selected']], [last])

    def test_all_images_view_loads_only_visible_rows_and_preserves_choices(self):
        import screenshot_review
        frames = [{'path': str(self.session / 'screenshots' / f'image-{n}.png'),
                   'selected': True, 'automatic_selected': True, 'reason': 'visual-change'}
                  for n in range(1000)]
        with mock.patch.object(screenshot_review, 'load_review_frames', return_value=frames):
            self.app.review_screenshots()
            dialog = next(widget for widget in self.root.winfo_children() if isinstance(widget, tk.Toplevel))
            review = dialog.review
            deadline = time.monotonic() + 3
            while not review.ready and time.monotonic() < deadline:
                self.root.update()
                time.sleep(0.01)
        review.set_all(False)
        review.page_size_var.set('All')
        review.change_page_size()
        self.root.update()
        self.assertLess(len(review.page_states), 40)
        first = frames[0]['path']
        review.page_states[first].set(True)
        review.canvas.yview_moveto(1)
        self.root.update()
        self.assertIn(frames[-1]['path'], review.page_states)
        self.assertTrue(review.choices[first])
        self.assertLess(len(review.page_states), 40)
        self.assertEqual(str(review.next_button.cget('state')), 'disabled')
        review.page_size_var.set('100')
        review.change_page_size()
        self.assertEqual(len(review.page_states), 100)
        self.assertTrue(review.page_states[first].get())
        review.close()

    def test_background_duplicate_analysis_preserves_changes_made_during_review(self):
        import screenshot_review
        from PIL import Image
        folder = self.session / 'screenshots'
        folder.mkdir()
        for name in ('first.png', 'second.png'):
            Image.new('RGB', (32, 32)).save(folder / name)
        gate = threading.Event()
        original = screenshot_review.load_review_frames
        def blocked(*args):
            gate.wait(3)
            return original(*args)
        with mock.patch.object(screenshot_review, 'load_review_frames', side_effect=blocked):
            self.app.review_screenshots()
            dialog = next(widget for widget in self.root.winfo_children() if isinstance(widget, tk.Toplevel))
            review = dialog.review
            deadline = time.monotonic() + 2
            while not review.frames and time.monotonic() < deadline:
                self.root.update()
                time.sleep(.01)
            review.set_all(True)
            gate.set()
            while not review.ready and time.monotonic() < deadline:
                self.root.update()
                time.sleep(.01)
        self.assertTrue(review.ready)
        self.assertTrue(all(review.choices.values()))
        self.assertEqual(sum(entry['automatic_selected'] for entry in review.frames), 1)
        review.close()

    def test_screen_capture_can_start_pause_and_resume_during_recording(self):
        class Frame:
            def save(self, path):
                path.write_bytes(b'frame')

        self.app.recording = True
        self.app.current_out_dir = self.session
        self.app.capture_screen_var.set(False)
        def wait_for(condition):
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                self.root.update()
                if condition():
                    return True
                time.sleep(0.01)
            return False
        with mock.patch.object(self.app, 'snapshot_external_screenshots', return_value={}), \
             mock.patch.object(self.app, 'grab_screen_image', return_value=Frame()), \
             mock.patch.object(self.app, 'foreground_window_info', return_value={'hwnd': 42, 'title': 'Browser'}), \
             mock.patch.object(self.app, 'import_external_screenshots', return_value=0):
            self.app.start_screen_context(self.session)
            self.assertEqual(list((self.session / 'screenshots').glob('*.png')) if (self.session / 'screenshots').exists() else [], [])
            self.app.capture_screen_button.invoke()
            self.assertTrue(wait_for(lambda: len(list((self.session / 'screenshots').glob('*.png'))) == 1))
            first_worker = self.app.screenshot_worker
            self.app.capture_screen_button.invoke()
            self.app.capture_screen_button.invoke()
            self.assertTrue(wait_for(lambda: len(list((self.session / 'screenshots').glob('*.png'))) == 2))
            self.assertTrue(wait_for(lambda: not first_worker.is_alive()))
            self.assertEqual(len(list((self.session / 'screenshots').glob('*.png'))), 2)
            self.app.capture_screen_button.invoke()
            self.assertTrue(wait_for(lambda: not self.app.screenshot_worker.is_alive()))
        import screenshot_window_context
        paths = sorted((self.session / 'screenshots').glob('*.png'))
        groups, labels = screenshot_window_context.window_groups(
            self.session, [{'path': str(path)} for path in paths])
        self.assertEqual(set(groups.values()), {'hwnd:42'})
        self.assertIn('Browser', labels['hwnd:42'])
        self.app.recording = False

    def test_stopping_recording_cancels_deferred_screen_restart(self):
        class Frame:
            def save(self, path):
                path.write_bytes(b'frame')

        self.app.recording = True
        self.app.current_out_dir = self.session
        self.app.capture_screen_var.set(True)
        with mock.patch.object(self.app, 'grab_screen_image', return_value=Frame()), \
             mock.patch.object(self.app, 'import_external_screenshots', return_value=0), \
             mock.patch.object(ui.capture, 'request_stop'):
            self.app.start_screen_context(self.session)
            original_worker = self.app.screenshot_worker
            self.app.capture_screen_button.invoke()
            self.app.capture_screen_button.invoke()
            self.app.stop()
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                self.root.update()
                time.sleep(0.01)
            self.assertIs(self.app.screenshot_worker, original_worker)
            self.assertFalse(original_worker.is_alive())
        self.app.recording = False

    def test_changing_interval_reschedules_existing_screenshot_worker(self):
        self.app.screenshot_interval_seconds = 0.4
        self.app.screenshot_stop.clear()
        capture_times = []

        def save(_out_dir, _grab):
            capture_times.append(time.monotonic())
            if len(capture_times) == 2:
                self.app.screenshot_stop.set()
            return self.session / 'frame.png', ''

        with mock.patch.object(self.app, 'save_screenshot', side_effect=save), \
             mock.patch.object(self.app, 'import_external_screenshots', return_value=0):
            worker = threading.Thread(target=self.app._screen_context_worker, args=(self.session, 0.4))
            worker.start()
            deadline = time.monotonic() + 1
            while len(capture_times) < 1 and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertEqual(len(capture_times), 1)
            self.app.screenshot_interval_seconds = 0.08
            worker.join(0.3)
            self.app.screenshot_stop.set()
            worker.join(1)
        self.assertEqual(len(capture_times), 2)
        self.assertLess(capture_times[1] - capture_times[0], 0.3)

    def test_interval_control_changes_live_worker_setting(self):
        self.app.screenshot_interval_var.set(5)
        self.assertTrue(hasattr(self.app, 'screenshot_interval_seconds'))
        self.assertEqual(self.app.screenshot_interval_seconds, 5)
        self.app.screenshot_interval_var.set(10)
        self.assertEqual(self.app.screenshot_interval_seconds, 10)

    def test_manual_screenshot_works_with_automatic_capture_off_and_indicates_saved_frame(self):
        class Frame:
            def save(self, path):
                path.write_bytes(b'manual frame')

        self.app.recording = True
        self.app.current_out_dir = self.session
        self.app.capture_screen_var.set(False)
        self.app.manual_screenshot_button.configure(state=tk.NORMAL)
        with mock.patch.object(self.app, 'grab_screen_image', return_value=Frame()), \
             mock.patch.object(self.app, 'foreground_window_info', return_value={'hwnd': 42, 'title': 'Browser'}):
            self.app.manual_screenshot_button.invoke()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and self.app.capture_indicator.cget('foreground') != '#15803d':
                self.root.update()
                time.sleep(0.01)
        images = list((self.session / 'screenshots').glob('*.png'))
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0].read_bytes(), b'manual frame')
        self.assertFalse(self.app.capture_screen_var.get())
        self.assertEqual(self.app.capture_indicator.cget('foreground'), '#15803d')
        self.assertIn('manual', self.app.summary_var.get().lower())
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and self.app.capture_preview_label.cget('text') == 'No screenshot yet':
            self.root.update()
            time.sleep(0.01)
        self.assertEqual(self.app.capture_preview_label.cget('text'), 'Preview unavailable')
        index = (self.session / 'screenshots' / 'capture_windows.jsonl').read_text()
        self.assertIn('Browser', index)

    def test_manual_screenshot_is_unavailable_when_not_recording(self):
        self.assertEqual(str(self.app.manual_screenshot_button.cget('state')), 'disabled')

    def test_saved_screenshot_shows_compact_preview_beside_indicator(self):
        from PIL import Image
        image_path = self.session / 'preview.png'
        Image.new('RGB', (800, 400), 'blue').save(image_path)
        self.app.handle_screen_event({'ok': True, 'path': str(image_path)})
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and self.app.capture_preview_photo is None:
            self.root.update()
            time.sleep(0.01)
        self.assertIsNotNone(self.app.capture_preview_photo)
        self.assertLessEqual(self.app.capture_preview_photo.width(), 120)
        self.assertLessEqual(self.app.capture_preview_photo.height(), 72)
        self.assertEqual(self.app.capture_preview_path, image_path)
        self.assertEqual(self.app.capture_indicator.cget('foreground'), '#15803d')

    def test_selected_window_capture_renders_window_itself_even_when_covered(self):
        self.app.screen_scope_var.set('Selected window')
        self.app.selected_window_hwnd = 42
        frame = object()
        grabber = SimpleNamespace(grab=mock.Mock())
        with mock.patch.object(self.app, 'selected_window_bbox', return_value=(10, 20, 110, 120)),              mock.patch.object(ui, 'print_window_image', return_value=frame) as render:
            result = self.app.grab_screen_image(grabber, {'hwnd': 99, 'title': 'Codes Inbox'})
        self.assertIs(result, frame)
        render.assert_called_once_with(42, (10, 20, 110, 120))
        grabber.grab.assert_not_called()

    def test_selected_window_capture_falls_back_to_screen_area_when_render_fails(self):
        self.app.screen_scope_var.set('Selected window')
        self.app.selected_window_hwnd = 42
        frame = object()
        grabber = SimpleNamespace(grab=mock.Mock(return_value=frame))
        with mock.patch.object(self.app, 'selected_window_bbox', return_value=(10, 20, 110, 120)),              mock.patch.object(ui, 'print_window_image', return_value=None):
            result = self.app.grab_screen_image(grabber, {'hwnd': 99, 'title': 'Codes Inbox'})
        self.assertIs(result, frame)
        grabber.grab.assert_called_once_with(bbox=(10, 20, 110, 120), all_screens=True)

    def test_selected_window_capture_does_not_fall_back_to_desktop_if_target_missing(self):
        self.app.screen_scope_var.set('Selected window')
        grabber = SimpleNamespace(grab=mock.Mock())
        with mock.patch.object(self.app, 'selected_window_bbox', return_value=None),              mock.patch.object(ui, 'print_window_image') as render:
            with self.assertRaisesRegex(RuntimeError, 'minimized|unavailable'):
                self.app.grab_screen_image(grabber, {'hwnd': 42, 'title': 'Other app'})
        grabber.grab.assert_not_called()
        render.assert_not_called()

    def test_selected_window_scope_label_no_longer_requires_uncovered(self):
        values = self.app.screen_scope.cget('values')
        self.assertIn('Selected window', values)
        self.assertFalse(any('uncovered' in str(v) for v in values))

    def _write_complete_meeting(self):
        transcripts = self.session / 'transcripts'
        for name in ('notes.md', 'cleaned.md', 'actions.md'):
            (transcripts / name).write_text('done')
        self.write_state('complete', 'complete')

    def test_completion_renders_final_state_even_if_a_refresh_step_fails(self):
        self.app.set_processing_controls(True)
        self._write_complete_meeting()
        real = self.app.update_processing_context
        calls = {'n': 0}
        def flaky():
            calls['n'] += 1
            if calls['n'] == 1:
                raise RuntimeError('transient render failure')
            return real()
        with mock.patch.object(self.app, 'update_processing_context', side_effect=flaky),              mock.patch.object(self.app, 'refresh_openrouter_balance'):
            self.app.process_events.put(('done', 0, json.dumps({'ok': True, 'message': 'Meeting processing complete'}), ''))
            self.app.poll_process_events()
            self.root.update()
            self.app.render_saved_processing_state()
        self.assertFalse(self.app.processing_running)
        self.assertEqual(self.app.process_stage_var.get(), 'Complete')
        self.assertEqual(self.app.process_progress_var.get(), 100)
        self.assertEqual(str(self.app.regenerate_notes_button.cget('state')), 'normal')
        self.assertEqual(str(self.app.open_notes_button.cget('state')), 'normal')

    def test_tick_self_heals_stale_running_display_after_run_ended(self):
        self._write_complete_meeting()
        self.app.processing_running = False
        self.app.process_stage_var.set('OCR + transcription')
        self.app.process_progress_var.set(10)
        self.app.process_var.set('Cloud OCR: 5 of 21 complete · 3 active')
        self.app.regenerate_notes_button.configure(state='disabled')
        self.app.tick()
        self.assertEqual(self.app.process_stage_var.get(), 'Complete')
        self.assertEqual(self.app.process_progress_var.get(), 100)
        self.assertNotIn('active', self.app.process_var.get())
        self.assertEqual(str(self.app.regenerate_notes_button.cget('state')), 'normal')

    def test_tk_callback_exceptions_are_logged_not_lost(self):
        self.app.out_dir_var.set(self.temp.name)
        try:
            raise ValueError('boom in callback')
        except ValueError as exc:
            self.root.report_callback_exception(type(exc), exc, exc.__traceback__)
        log = (Path(self.temp.name) / 'ui-errors.log').read_text(encoding='utf-8')
        self.assertIn('boom in callback', log)

    def test_main_tabs_use_distinct_filled_style(self):
        self.assertEqual(str(self.app.notebook.cget('style')), 'Main.TNotebook')
        style = ttk.Style(self.root)
        self.assertIn('bold', str(style.lookup('Main.TNotebook.Tab', 'font')))
        self.assertEqual(style.lookup('Main.TNotebook.Tab', 'background', ['selected']), '#2563eb')

    def test_print_window_blank_render_is_rejected(self):
        from PIL import Image
        self.assertFalse(ui.render_has_content(Image.new('RGB', (50, 50), (0, 0, 0))))
        img = Image.new('RGB', (50, 50), (255, 255, 255))
        img.putpixel((10, 10), (0, 0, 0))
        self.assertTrue(ui.render_has_content(img))

    def test_choose_window_shows_browser_first_with_app_icon(self):
        import window_picker
        from PIL import Image
        windows = [
            {'hwnd': 1, 'title': 'A note - Notepad', 'size': '900x700'},
            {'hwnd': 2, 'title': 'Z meeting - Brave', 'size': '1000x800'},
        ]
        with mock.patch.object(ui, 'visible_windows', return_value=windows), \
             mock.patch.object(window_picker, 'window_process_name', side_effect=lambda hwnd: 'brave.exe' if hwnd == 2 else 'notepad.exe'), \
             mock.patch.object(window_picker, 'window_icon_image', return_value=Image.new('RGB', (20, 20), 'red')):
            self.app.choose_capture_window()
            dialog = next(child for child in self.root.winfo_children() if isinstance(child, tk.Toplevel))
            try:
                tree = next(child for frame in dialog.winfo_children() for child in frame.winfo_children()
                            if isinstance(child, ui.ttk.Treeview))
                rows = tree.get_children()
                self.assertEqual([tree.item(row, 'values')[0] for row in rows],
                                 ['Z meeting - Brave', 'A note - Notepad'])
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline and not tree.item(rows[0], 'image'):
                    self.root.update()
                    time.sleep(0.01)
                self.assertTrue(tree.item(rows[0], 'image'))
                # Treeview reserves an indent before its 20px item image; the
                # icon column must contain both without covering the title.
                self.assertGreaterEqual(tree.column('#0', 'width'), 48)
            finally:
                dialog.destroy()

    def test_recording_error_clears_stale_recording_state_and_shows_recovered_session(self):
        (self.session / 'metadata.json').write_text(json.dumps({'partial': True, 'recovered': True}))
        self.app.recording = True
        self.app.current_out_dir = self.session
        self.app.selected_session = self.session
        self.app.started_at = time.time() - 41 * 60
        self.app.elapsed_var.set('00:41:00')
        with mock.patch.object(ui.messagebox, 'showerror'):
            self.app.handle_error(RuntimeError('device closed'))
        self.assertFalse(self.app.recording)
        self.assertEqual(self.app.elapsed_var.get(), '00:00:00')
        self.assertNotIn('Recording', self.app.selected_meeting_var.get())
        self.assertIn('Partial capture', self.app.selected_meeting_var.get())
        self.assertTrue(any(path == self.session for path in self.app.history_rows.values()))

    def test_processing_costs_show_known_subtotal_without_hiding_unmetered_ocr(self):
        (self.session / 'screenshots').mkdir()
        (self.session / 'visual').mkdir()
        paths = [str(self.session / 'screenshots' / f'image-{i}.png') for i in range(2)]
        (self.session / 'screenshots' / 'manifest.json').write_text(json.dumps({'frames': [
            {'path': path, 'selected': True} for path in paths
        ]}))
        (self.session / 'visual' / 'ocr.json').write_text(json.dumps({'results': [
            {'path': paths[0], 'ok': True, 'backend': 'openrouter', 'cost_usd': 0.0012},
            {'path': paths[1], 'ok': True, 'backend': 'openrouter'},
        ]}))
        (self.session / 'transcripts' / 'diagnostics.json').write_text(json.dumps({'tracks': [
            {'path': str(self.session / 'mic.wav'), 'kind': 'microphone', 'selected': True},
        ]}))
        chunk_dir = self.session / 'transcripts' / 'cloud_chunks' / 'mic'
        chunk_dir.mkdir(parents=True)
        (chunk_dir / '00000.json').write_text(json.dumps({'usage': {'cost': 0.002}}))
        (chunk_dir / '00001.json').write_text(json.dumps({'usage': {'cost': 0.003}}))
        (self.session / 'transcripts' / 'notes_openrouter_response.json').write_text(json.dumps({'usage': {'cost': 0.004}}))
        self.app.refresh_processing_details()
        rows = {self.app.stage_status.item(item, 'values')[0]: self.app.stage_status.item(item, 'values')
                for item in self.app.stage_status.get_children()}
        self.assertEqual(rows['Screenshot OCR'][2], '$0.001200 + 1 unknown')
        self.assertEqual(rows['Audio transcription'][2], '$0.005000')
        self.assertEqual(rows['Notes and actions'][2], '$0.004000')
        self.assertIn('at least $0.010200', self.app.processing_detail_var.get())

    def test_resume_clears_failure_before_worker_can_start(self):
        gate = threading.Event()
        with mock.patch.object(self.app, '_process_worker', side_effect=lambda *a: gate.wait(3)):
            try:
                self.app.process_selected_meeting()
                self.app.refresh_processing_details()
                self.assertNotIn('attention', self.app.processing.cget('text'))
                self.assertNotIn('Last run failed', self.app.processing_detail_var.get())
                self.assertEqual(str(self.app.retry_process_button.cget('state')), 'disabled')
                self.assertEqual(self.app.notebook.select(), str(self.app.stage_status.master))
            finally:
                gate.set()
                self.app.process_worker.join(3)

    def test_active_transcription_supersedes_old_ocr_failure_and_shows_percent(self):
        diagnostics = {'tracks': [{'path': 'mic.wav', 'kind': 'microphone', 'selected': True}]}
        (self.session / 'transcripts' / 'diagnostics.json').write_text(json.dumps(diagnostics))
        self.app.processing_session = self.session
        self.app.processing_running = True
        self.app.active_progress_payload = {'stage': 'transcript', 'item': 'mic.wav', 'item_percent': 25, 'audio_seconds': 30, 'audio_total_seconds': 120}
        self.app.refresh_processing_details()
        rows = [self.app.stage_status.item(row, 'values') for row in self.app.stage_status.get_children()]
        self.assertFalse(any(row[1] == 'Failed' for row in rows))
        audio = self.app.audio_status.item(self.app.audio_status.get_children()[0], 'values')
        self.assertEqual(audio[3], '25%')
        self.assertIn('00:30', audio[4])
        self.assertNotIn('elapsed', audio[4])

    def test_parallel_progress_shows_ocr_and_audio_active_together(self):
        from PIL import Image
        screenshots = self.session / 'screenshots'
        screenshots.mkdir()
        image_path = screenshots / 'current.png'
        Image.new('RGB', (100, 50), 'white').save(image_path)
        (self.session / 'transcripts' / 'diagnostics.json').write_text(json.dumps({'tracks': [
            {'path': str(self.session / 'mic.wav'), 'kind': 'microphone', 'selected': True},
        ]}))
        self.app.processing_session = self.session
        self.app.processing_running = True
        self.app.active_progress_payload = {
            'stage': 'transcript', 'progress': 'Transcribing mic',
            'parallel_stages': {
                'ocr': {'stage': 'ocr', 'progress': 'OCR image 1 of 2', 'percent': 25,
                        'item': str(image_path), 'preview_path': str(image_path), 'current': 1, 'total': 2},
                'audio': {'stage': 'transcript', 'progress': 'Transcribing mic', 'percent': 60,
                          'item': str(self.session / 'mic.wav'), 'item_percent': 35,
                          'audio_seconds': 21, 'audio_total_seconds': 60},
            },
        }
        self.app.refresh_processing_details()
        stages = {self.app.stage_status.item(row, 'values')[0]: self.app.stage_status.item(row, 'values')
                  for row in self.app.stage_status.get_children()}
        self.assertEqual(stages['Screenshot OCR'][1], 'Processing')
        self.assertEqual(stages['Audio transcription'][1], 'Processing')
        audio = self.app.audio_status.item(self.app.audio_status.get_children()[0], 'values')
        self.assertEqual(audio[3], '35%')
        self.assertEqual(self.app.ocr_preview_frame.winfo_manager(), 'pack')

    def test_other_selected_meeting_does_not_show_active_track(self):
        self.app.processing_running = True
        self.app.processing_session = Path(self.temp.name) / 'another-meeting'
        self.app.active_progress_payload = {'stage': 'notes', 'progress': 'Generating notes'}
        self.app.refresh_processing_details()
        rows = [self.app.stage_status.item(row, 'values') for row in self.app.stage_status.get_children()]
        self.assertFalse(any(row[1] == 'Processing' for row in rows))

    def test_regenerating_existing_notes_shows_processing_not_complete(self):
        for name in ('notes.md', 'actions.md'):
            (self.session / 'transcripts' / name).write_text('previous output')
        self.app.processing_session = self.session
        self.app.processing_running = True
        self.app.active_progress_payload = {'stage': 'notes', 'percent': 88, 'progress': 'Generating notes'}
        self.app.refresh_processing_details()
        notes = [self.app.stage_status.item(row, 'values') for row in self.app.stage_status.get_children()][-1]
        self.assertEqual(notes[1], 'Processing')

    def test_failed_audio_item_is_not_left_pending(self):
        (self.session / 'transcripts' / 'diagnostics.json').write_text(json.dumps({'tracks': [{'path': 'mic.wav', 'selected': True}]}))
        self.write_state('failed', 'transcript', 'decoder stopped')
        self.app.refresh_processing_details()
        audio = self.app.audio_status.item(self.app.audio_status.get_children()[0], 'values')
        self.assertEqual(audio[2], 'Failed')
        self.assertIn('decoder stopped', audio[4])

    def test_earlier_failed_audio_is_marked_after_later_audio_succeeds(self):
        (self.session / 'transcripts' / 'diagnostics.json').write_text(json.dumps({'tracks': [{'path': 'mic.wav', 'selected': True}, {'path': 'system.wav', 'selected': True}]}))
        (self.session / 'transcripts' / 'system.json').write_text('{}')
        (self.session / 'transcripts' / 'process_state.json').write_text(json.dumps({'status': 'failed', 'error': 'mic failed', 'last_progress': {'stage': 'transcript', 'item': 'system.wav', 'failed_items': ['mic.wav']}}))
        self.app.refresh_processing_details()
        rows = [self.app.audio_status.item(row, 'values') for row in self.app.audio_status.get_children()]
        self.assertEqual(rows[0][2], 'Failed')
        self.assertEqual(rows[1][2], 'Complete')

    def test_progress_message_is_below_bar_and_wrapped(self):
        self.root.deiconify()
        self.root.geometry('1000x800')
        self.root.update()
        self.assertGreaterEqual(self.app.processing_message.winfo_rooty(), self.app.processing_bar.winfo_rooty() + self.app.processing_bar.winfo_height())
        self.assertGreater(int(self.app.processing_message.cget('wraplength')), 600)

    def test_successful_resume_clears_attention_and_enables_artifacts(self):
        for name in ('notes.md', 'actions.md', 'cleaned.md'):
            (self.session / 'transcripts' / name).write_text('fixture')
        self.app.set_processing_controls(True)
        self.write_state('complete', 'complete')
        self.app.handle_process_done(0, json.dumps({'ok': True, 'message': 'Meeting complete'}), '')
        self.assertEqual(self.app.processing.cget('text'), 'Processed meeting')
        self.assertNotIn('Stopped at', self.app.processing_detail_var.get())
        self.assertEqual(str(self.app.open_notes_button.cget('state')), 'normal')
        self.assertEqual(self.app.process_progress_var.get(), 100)

    def test_processing_timer_is_beside_bar_and_updates_estimates(self):
        self.app.set_processing_controls(True)
        self.app.processing_started_at = time.monotonic() - 120
        self.app.processing_estimate_percent = 50
        self.app.process_progress_var.set(50)
        self.app.update_processing_timer()
        timer = self.app.processing_timer_var.get()
        self.assertIn('Elapsed 00:02:00', timer)
        self.assertIn('Est. total ~00:04:00', timer)
        self.assertIn('Remaining ~00:02:00', timer)
        self.assertEqual(self.app.processing_timer.master, self.app.processing_bar.master)
        self.app.process_progress_var.set(80)  # Indeterminate animation may move the bar variable.
        self.app.update_processing_timer()
        self.assertIn('Est. total ~00:04:00', self.app.processing_timer_var.get())
        self.app.processing_estimate_percent = 0
        self.app.process_progress_var.set(0)
        self.app.update_processing_timer()
        self.assertIn('Est. total ~', self.app.processing_timer_var.get())
        self.assertIn('Remaining ~', self.app.processing_timer_var.get())
        self.assertNotIn('calculating', self.app.processing_timer_var.get())

    def test_finished_worker_recovers_stale_processing_ui_from_saved_state(self):
        for name in ('notes.md', 'actions.md', 'cleaned.md'):
            (self.session / 'transcripts' / name).write_text('fixture')
        self.app.set_processing_controls(True)
        self.app.process_worker = threading.Thread(target=lambda: None)
        self.app.process_worker.start()
        self.app.process_worker.join()
        self.write_state('complete', 'complete')
        state_file = self.session / 'transcripts' / 'process_state.json'
        state = json.loads(state_file.read_text())
        state['updated_at'] = __import__('datetime').datetime.now().isoformat()
        state_file.write_text(json.dumps(state))
        self.app.reconcile_processing_completion()
        self.assertFalse(self.app.processing_running)
        self.assertEqual(self.app.process_stage_var.get(), 'Complete')
        self.assertEqual(str(self.app.cancel_process_button.cget('state')), 'disabled')

    def test_old_complete_checkpoint_does_not_end_new_processing_run(self):
        self.app.set_processing_controls(True)
        self.app.process_worker = threading.Thread(target=lambda: None)
        self.app.process_worker.start()
        self.app.process_worker.join()
        state_file = self.session / 'transcripts' / 'process_state.json'
        state_file.write_text(json.dumps({'status': 'complete', 'updated_at': '2020-01-01T00:00:00'}))
        self.app.reconcile_processing_completion()
        self.assertTrue(self.app.processing_running)

    def test_transient_progress_render_error_does_not_drop_completion_event(self):
        for name in ('notes.md', 'actions.md', 'cleaned.md'):
            (self.session / 'transcripts' / name).write_text('fixture')
        self.app.set_processing_controls(True)
        self.write_state('complete', 'complete')
        self.app.process_events.put(('progress', {'stage': 'transcript', 'progress': 'chunk 3 of 19', 'percent': 70}))
        self.app.process_events.put(('done', 0, json.dumps({'ok': True, 'message': 'Meeting complete'}), ''))
        original = self.app.refresh_processing_details
        calls = 0
        def fail_once():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError('temporary progress render failure')
            return original()
        with mock.patch.object(self.app, 'refresh_processing_details', side_effect=fail_once):
            self.app.poll_process_events()
            self.app.poll_process_events()
        self.assertFalse(self.app.processing_running)
        self.assertEqual(self.app.process_stage_var.get(), 'Complete')


if __name__ == '__main__':
    unittest.main()
