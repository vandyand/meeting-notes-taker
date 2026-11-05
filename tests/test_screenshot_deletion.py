import json
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import screenshot_deletion
import screenshot_edits


class ScreenshotDeletionTests(unittest.TestCase):
    def test_removes_source_derivatives_and_checkpoint_without_touching_other_image(self):
        with tempfile.TemporaryDirectory() as temp:
            session = Path(temp)
            folder = session / 'screenshots'
            folder.mkdir()
            first, second = folder / 'first.png', folder / 'second.png'
            for path in (first, second):
                Image.new('RGB', (40, 20), 'white').save(path)
            screenshot_edits.save_edit(session, first, {'crop': [0, 0, .5, 1]})
            derived = screenshot_edits.ocr_input(session, first)
            legacy = folder / '.ocr-inputs' / (hashlib.sha256((screenshot_edits.path_key(first) + screenshot_edits.fingerprint(session, first)).encode()).hexdigest() + '.png')
            legacy.write_bytes(b'old image copy')
            older_opaque_copy = folder / '.ocr-inputs' / ('f' * 64 + '.png')
            older_opaque_copy.write_bytes(b'older image copy')
            cache = folder / '.review-cache'
            (cache / 'thumbnails').mkdir(parents=True)
            (cache / 'thumbnails' / 'hidden.jpg').write_bytes(b'preview')
            (cache / 'analysis.json').write_text('{}')
            (folder / 'manifest.json').write_text(json.dumps({'frames': [
                {'path': str(first), 'selected': True}, {'path': str(second), 'selected': True}]}))
            visual = session / 'visual'
            visual.mkdir()
            (visual / 'ocr.json').write_text(json.dumps({'model': 'test', 'results': [
                {'path': str(first), 'ok': True, 'text': 'Delete me', 'ocr_input_path': str(derived)},
                {'path': str(second), 'ok': True, 'text': 'Keep me'}]}))
            screenshot_deletion.delete_screenshot(session, first)
            self.assertFalse(first.exists())
            self.assertFalse(derived.exists())
            self.assertFalse(legacy.exists())
            self.assertFalse(older_opaque_copy.exists())
            self.assertFalse((cache / 'analysis.json').exists())
            self.assertFalse((cache / 'thumbnails' / 'hidden.jpg').exists())
            self.assertTrue(second.exists())
            self.assertEqual(len(json.loads((folder / 'manifest.json').read_text())['frames']), 1)
            self.assertEqual(len(json.loads((visual / 'ocr.json').read_text())['results']), 1)
            self.assertNotIn('Delete me', (visual / 'ocr.md').read_text())
            self.assertEqual(screenshot_edits.load_edits(session).get('individual'), {})

    def test_refuses_to_delete_file_outside_meeting_screenshots(self):
        with tempfile.TemporaryDirectory() as temp:
            session = Path(temp) / 'meeting'
            session.mkdir()
            outside = Path(temp) / 'outside.png'
            outside.write_bytes(b'keep')
            with self.assertRaises(ValueError):
                screenshot_deletion.delete_screenshot(session, outside)
            self.assertTrue(outside.exists())

    def test_same_stem_cache_is_purged_but_other_original_survives(self):
        with tempfile.TemporaryDirectory() as temp:
            session = Path(temp)
            folder = session / 'screenshots'
            folder.mkdir()
            original = folder / 'frame.png'
            other = folder / 'frame.jpg'
            Image.new('RGB', (20, 20)).save(original)
            Image.new('RGB', (20, 20)).save(other)
            cache = folder / '.ocr-inputs'
            cache.mkdir()
            old_revision = cache / 'frame-cropped-old.png'
            old_revision.write_bytes(b'old image data')
            screenshot_deletion.delete_screenshot(session, original)
            self.assertFalse(old_revision.exists())
            self.assertTrue(other.exists())
