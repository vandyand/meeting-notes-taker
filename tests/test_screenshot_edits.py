import sys
import tempfile
import unittest
from pathlib import Path
from PIL import Image
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import screenshot_edits as edits


class ScreenshotEditsTests(unittest.TestCase):
    def test_global_crop_and_mask_preserve_original_and_individual_can_override(self):
        with tempfile.TemporaryDirectory() as temp:
            session = Path(temp)
            (session / 'screenshots').mkdir()
            path = session / 'screenshots' / 'test.png'
            Image.new('RGB', (300, 120), 'white').save(path)
            original = path.read_bytes()
            edits.save_edit(session, None, {'crop': [1/3, 0, 2/3, 1], 'masks': [[1/3, 0, .5, .5]]})
            result = edits.load_image(session, path)
            self.assertEqual(result.size, (100, 120))
            self.assertEqual(result.getpixel((0, 0)), (0, 0, 0))
            self.assertEqual(result.getpixel((70, 70)), (255, 255, 255))
            self.assertEqual(path.read_bytes(), original)
            first = edits.fingerprint(session, path)
            prepared = edits.ocr_input(session, path)
            self.assertNotEqual(prepared, path)
            self.assertTrue(prepared.name.startswith('test-cropped-masked-'))
            self.assertIn('Global crop applied (33%', edits.summary(session))
            edits.save_edit(session, path, {})
            self.assertEqual(edits.load_image(session, path).size, (300, 120))
            self.assertIsNone(edits.fingerprint(session, path))
            edits.save_edit(session, path, None)
            self.assertEqual(edits.fingerprint(session, path), first)

    def test_legacy_derived_file_is_reused_under_readable_name(self):
        import hashlib
        from unittest import mock
        with tempfile.TemporaryDirectory() as temp:
            session = Path(temp)
            (session / 'screenshots').mkdir()
            path = session / 'screenshots' / 'screen.png'
            Image.new('RGB', (100, 100)).save(path)
            edits.save_edit(session, None, {'crop': [0, 0, .5, 1]})
            token = edits.fingerprint(session, path)
            folder = session / 'screenshots' / '.ocr-inputs'
            folder.mkdir()
            legacy = folder / (hashlib.sha256((edits.path_key(path) + token).encode()).hexdigest() + '.png')
            Image.new('RGB', (50, 100)).save(legacy)
            with mock.patch.object(edits, 'load_image', side_effect=AssertionError('Reuse existing bytes')):
                renamed = edits.ocr_input(session, path)
            self.assertEqual(renamed.read_bytes(), legacy.read_bytes())
            self.assertTrue(renamed.name.startswith('screen-cropped-'))

    def test_changing_crop_invalidates_ocr_checkpoint(self):
        import windows_ocr
        import process_latest
        import json
        with tempfile.TemporaryDirectory() as temp:
            session = Path(temp)
            (session / 'screenshots').mkdir()
            (session / 'visual').mkdir()
            (session / 'visual' / 'ocr.md').write_text('OCR')
            (session / 'visual' / 'timeline.md').write_text('Timeline')
            path = session / 'screenshots' / 'test.png'
            Image.new('RGB', (100, 100)).save(path)
            (session / 'screenshots' / 'manifest.json').write_text(json.dumps({'frames': [{'path': str(path), 'selected': True}]}))
            (session / 'visual' / 'ocr.json').write_text(json.dumps({'results': [{'path': str(path), 'ok': True}], 'selected_image_count': 1, 'successful_image_count': 1, 'failed_image_count': 0}))
            self.assertIsNotNone(process_latest.completed_ocr_summary(session))
            edits.save_edit(session, None, {'crop': [0, 0, .5, 1]})
            self.assertIsNone(process_latest.completed_ocr_summary(session))
            import windows_capture_ui
            self.assertEqual(windows_capture_ui.CaptureApp.ocr_counts(None, session)['pending'], 1)

    def test_cloud_ocr_receives_new_cropped_image_file(self):
        import windows_ocr, json, io, contextlib, os
        from unittest import mock
        with tempfile.TemporaryDirectory() as temp:
            session = Path(temp)
            (session / 'screenshots').mkdir()
            original = session / 'screenshots' / 'image.png'
            Image.new('RGB', (300, 120), 'white').save(original)
            edits.save_edit(session, None, {'crop': [1/3, 0, 2/3, 1]})
            received = []
            def cloud(_model, path, _timeout):
                self.assertNotEqual(path, original)
                with Image.open(path) as image:
                    received.append(image.size)
                return {'text': 'Cropped text', 'cost_usd': .001, 'generation_id': 'test'}
            with mock.patch.object(sys, 'argv', ['windows_ocr.py', str(session), '--backend', 'openrouter']), \
                 mock.patch.dict(os.environ, {'OPENROUTER_API_KEY': 'test-only'}), \
                 mock.patch.object(windows_ocr, 'openrouter_generate', side_effect=cloud), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(windows_ocr.main(), 0)
            self.assertEqual(received, [(100, 120)])
            saved = json.loads((session / 'visual' / 'ocr.json').read_text())['results'][0]
            self.assertEqual(saved['path'], str(original))
            self.assertIsNotNone(saved['image_fingerprint'])


if __name__ == '__main__':
    unittest.main()
