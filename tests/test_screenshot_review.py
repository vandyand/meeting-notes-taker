import importlib
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import windows_ocr


class ReviewCacheTests(unittest.TestCase):
    def test_processing_reuses_review_cache_without_duplicate_scan_or_model_calls(self):
        import contextlib, io
        review = importlib.import_module('screenshot_review')
        with tempfile.TemporaryDirectory() as temp:
            session = Path(temp)
            root = session / 'screenshots'
            root.mkdir()
            image = root / 'image.png'
            Image.new('RGB', (32, 32), 'white').save(image)
            review.load_review_frames(session, threading.Event(), lambda *_: None)
            (session / 'visual').mkdir()
            (session / 'visual' / 'ocr.json').write_text(json.dumps({'results': [
                {'path': str(image), 'ok': True, 'text': 'Saved text', 'attempts': 1}]}))
            output = io.StringIO()
            with mock.patch.object(sys, 'argv', ['windows_ocr.py', str(session)]), \
                 mock.patch.object(windows_ocr, 'select_screenshots', side_effect=AssertionError('Use cached analysis')), \
                 mock.patch.object(windows_ocr, 'ollama_generate', side_effect=AssertionError('Preserve OCR')), \
                 contextlib.redirect_stdout(output):
                self.assertEqual(windows_ocr.main(), 0)
            self.assertNotIn('Checking screenshots for visual changes', output.getvalue())
            saved = json.loads((session / 'visual' / 'ocr.json').read_text())
            self.assertEqual(saved['results'][0]['text'], 'Saved text')

    def test_full_manual_selection_survives_crop_change(self):
        import screenshot_analysis, screenshot_edits
        with tempfile.TemporaryDirectory() as temp:
            session = Path(temp)
            folder = session / 'screenshots'
            folder.mkdir()
            image = folder / 'image.png'
            Image.new('RGB', (32, 32)).save(image)
            stat = image.stat()
            (folder / 'manifest.json').write_text(json.dumps({'frames': [
                {'path': str(image), 'manual_selected': True, 'automatic_selected': True,
                 'manual_source': [stat.st_size, stat.st_mtime_ns], 'manual_edit': {}}]}))
            self.assertIsNotNone(screenshot_analysis.manual_frames(session, [image], {}))
            screenshot_edits.save_edit(session, None, {'crop': [0, 0, .5, 1]})
            self.assertIsNotNone(screenshot_analysis.manual_frames(session, [image], screenshot_edits.load_edits(session)))

    def test_unchanged_capture_reuses_analysis_but_reloads_manual_choices(self):
        review = importlib.import_module('screenshot_review')
        with tempfile.TemporaryDirectory() as temp:
            session = Path(temp)
            root = session / 'screenshots'
            root.mkdir()
            image = root / 'image.png'
            Image.new('RGB', (32, 32), 'white').save(image)
            frames = review.load_review_frames(session, threading.Event(), lambda *_: None)
            self.assertTrue(frames[0]['selected'])
            (root / 'manifest.json').write_text(json.dumps({'frames': [
                {'path': str(image), 'manual_selected': False}
            ]}))
            with mock.patch.object(windows_ocr, 'select_screenshots', side_effect=AssertionError('analysis must be cached')):
                frames = review.load_review_frames(session, threading.Event(), lambda *_: None)
            self.assertFalse(frames[0]['selected'])

    def test_changed_capture_invalidates_analysis_and_thumbnail_cache(self):
        review = importlib.import_module('screenshot_review')
        with tempfile.TemporaryDirectory() as temp:
            session = Path(temp)
            root = session / 'screenshots'
            root.mkdir()
            image = root / 'image.png'
            Image.new('RGB', (800, 600), 'white').save(image)
            Image.new('RGB', (800, 600), 'white').save(root / 'second.png')
            review.load_review_frames(session, threading.Event(), lambda *_: None)
            thumbnail = review.cached_thumbnail(image, root / '.review-cache')
            self.assertLessEqual(thumbnail.width, 220)
            self.assertLessEqual(thumbnail.height, 150)
            Image.new('RGB', (900, 600), 'black').save(image)
            frames = review.load_review_frames(session, threading.Event(), lambda *_: None)
            self.assertEqual(sum(frame['selected'] for frame in frames), 2)
            thumbnail = review.cached_thumbnail(image, root / '.review-cache')
            self.assertEqual(thumbnail.getpixel((0, 0)), (0, 0, 0))

    def test_mask_edits_keep_duplicate_classification_and_cached_scan(self):
        import screenshot_edits
        review = importlib.import_module('screenshot_review')
        with tempfile.TemporaryDirectory() as temp:
            session = Path(temp)
            root = session / 'screenshots'
            root.mkdir()
            first, second = root / 'first.png', root / 'second.png'
            Image.new('RGB', (100, 100), 'white').save(first)
            Image.new('RGB', (100, 100), 'white').save(second)
            initial = review.load_review_frames(session, threading.Event(), lambda *_: None)
            self.assertFalse(initial[1]['automatic_selected'])
            screenshot_edits.save_edit(session, second, {'crop': [0, 0, .5, 1]})
            with mock.patch.object(windows_ocr, 'select_screenshots', side_effect=AssertionError('mask edit restarted duplicate scan')):
                edited = review.load_review_frames(session, threading.Event(), lambda *_: None)
            self.assertFalse(edited[1]['automatic_selected'])
            self.assertEqual(screenshot_edits.load_image(session, second).size, (50, 100))
            (root / '.review-cache' / 'analysis.json').unlink()
            rescanned = review.load_review_frames(session, threading.Event(), lambda *_: None)
            self.assertFalse(rescanned[1]['automatic_selected'], 'Per-image crops must not change duplicate labels')


if __name__ == '__main__':
    unittest.main()
