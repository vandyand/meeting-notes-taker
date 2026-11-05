import importlib
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import screenshot_review
import screenshot_edits
import screenshot_analysis
import windows_capture_ui


class CaptureBorderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.session = Path(self.temp.name)
        self.folder = self.session / 'screenshots'
        self.folder.mkdir()
        self.first = self.folder / 'screen-20261007-120000-0000.png'
        self.second = self.folder / 'screen-20261007-120005-0001.png'
        Image.new('RGB', (100, 100), 'white').save(self.first)
        changed = Image.new('RGB', (100, 100), 'white')
        ImageDraw.Draw(changed).rectangle((96, 30, 99, 40), fill='black')
        changed.save(self.second)
        # Border-specific assertions use the conservative policy; the app's
        # aggressive default intentionally hides small real changes.
        screenshot_analysis.save_duplicate_mode(self.session, 'Strict')

    def context(self, **values):
        (self.folder / 'screen_context.json').write_text(json.dumps(values))

    def analyze(self):
        return screenshot_review.load_review_frames(self.session, threading.Event(), lambda *_: None)

    def test_old_window_border_changes_are_duplicates_without_altering_originals(self):
        self.context(scope='Active window')
        before = [path.read_bytes() for path in (self.first, self.second)]
        frames = self.analyze()
        self.assertFalse(frames[1]['automatic_selected'])
        self.assertEqual(before, [path.read_bytes() for path in (self.first, self.second)])
        self.assertIsNone(screenshot_edits.fingerprint(self.session, self.second))

    def test_real_interior_changes_are_retained(self):
        self.context(scope='Selected window')
        changed = Image.open(self.second).convert('RGB')
        ImageDraw.Draw(changed).rectangle((30, 30, 40, 40), fill='black')
        changed.save(self.second)
        self.assertTrue(self.analyze()[1]['automatic_selected'])

    def test_border_handling_does_not_shift_normalized_user_crops(self):
        self.context(scope='Active window')
        screenshot_edits.save_edit(self.session, None, {'crop': [.8, .2, 1, .6]})
        self.assertFalse(self.analyze()[1]['automatic_selected'])
        self.assertEqual(screenshot_edits.load_image(self.session, self.second).size, (20, 40))

    def test_desktop_new_window_and_unknown_captures_keep_edge_changes(self):
        for context in ({'scope': 'Full desktop'}, {'scope': 'Active window', 'window_bounds_version': 2}, {}):
            with self.subTest(context=context):
                self.context(**context)
                self.assertTrue(self.analyze()[1]['automatic_selected'])

    def test_imported_images_keep_edge_changes_even_in_window_meeting(self):
        self.context(scope='Active window')
        imported = self.folder / 'imported'
        imported.mkdir()
        self.first.replace(imported / self.first.name)
        self.second.replace(imported / self.second.name)
        self.assertTrue(self.analyze()[1]['automatic_selected'])

    def test_comparison_policy_change_invalidates_current_cache(self):
        self.context(scope='Active window')
        self.assertFalse(self.analyze()[1]['automatic_selected'])
        self.context(scope='Active window', window_bounds_version=2)
        self.assertTrue(self.analyze()[1]['automatic_selected'])

    def test_old_analysis_cache_rechecks_and_retains_manual_overrides(self):
        self.context(scope='Active window')
        cache = self.folder / '.review-cache' / 'analysis.json'
        cache.parent.mkdir()
        images = [self.first, self.second]
        frames = [{'path': str(path), 'selected': True, 'automatic_selected': True,
                   'reason': 'visual-change'} for path in images]
        cache.write_text(json.dumps({'version': 1, 'sources': screenshot_review.source_signature(images),
                                     'edits': {}, 'frames': frames}))
        (self.folder / 'manifest.json').write_text(json.dumps({'frames': [
            {'path': str(self.second), 'manual_selected': True}]}))
        updated = self.analyze()
        self.assertFalse(updated[1]['automatic_selected'])
        self.assertTrue(updated[1]['selected'])


class VisibleBoundsTests(unittest.TestCase):
    def fake_apis(self, visible=(-100, 50, 500, 600), hr=0):
        def fill(pointer, bounds):
            pointer._obj.left, pointer._obj.top, pointer._obj.right, pointer._obj.bottom = bounds
        def raw(_hwnd, pointer):
            fill(pointer, (-108, 42, 508, 608))
            return True
        def dwm(_hwnd, attribute, pointer, size):
            self.assertEqual(attribute, 9)
            fill(pointer, visible)
            return hr
        return SimpleNamespace(IsWindow=lambda _: True, IsIconic=lambda _: False, GetWindowRect=raw), SimpleNamespace(DwmGetWindowAttribute=dwm)

    def test_capture_uses_visible_physical_bounds_instead_of_resize_border(self):
        import ctypes
        user32, dwmapi = self.fake_apis()
        with mock.patch.object(ctypes.windll, 'user32', user32), mock.patch.object(ctypes.windll, 'dwmapi', dwmapi):
            self.assertEqual(windows_capture_ui.CaptureApp.window_bbox(None, 123), (-100, 50, 500, 600))

    def test_invalid_or_unavailable_dwm_bounds_fall_back_safely(self):
        import ctypes
        for visible, hr in (((0, 0, 0, 0), 0), ((0, 0, 0, 0), -1)):
            user32, dwmapi = self.fake_apis(visible, hr)
            with mock.patch.object(ctypes.windll, 'user32', user32), mock.patch.object(ctypes.windll, 'dwmapi', dwmapi):
                self.assertEqual(windows_capture_ui.CaptureApp.window_bbox(None, 123), (-108, 42, 508, 608))

    def test_fallback_restores_previous_thread_dpi_context(self):
        import ctypes
        user32, dwmapi = self.fake_apis(hr=-1)
        calls = []
        def set_context(context):
            calls.append(context)
            return 1234
        user32.SetThreadDpiAwarenessContext = set_context
        with mock.patch.object(ctypes.windll, 'user32', user32), mock.patch.object(ctypes.windll, 'dwmapi', dwmapi):
            self.assertEqual(windows_capture_ui.CaptureApp.window_bbox(None, 123), (-108, 42, 508, 608))
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1], 1234)


if __name__ == '__main__':
    unittest.main()
