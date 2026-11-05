import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import screenshot_window_context as context


class ScreenshotWindowContextTests(unittest.TestCase):
    def test_saved_active_window_groups_frames_and_preserves_unknown_older_frames(self):
        with tempfile.TemporaryDirectory() as temp:
            session = Path(temp)
            folder = session / 'screenshots'
            folder.mkdir()
            first, second, old = (folder / name for name in ('first.png', 'second.png', 'old.png'))
            context.append_capture(session, first, {'hwnd': 123, 'title': 'Meeting - Browser'})
            context.append_capture(session, second, {'hwnd': 123, 'title': 'Other tab - Browser'})
            frames = [{'path': str(path)} for path in (first, second, old)]
            groups, labels = context.window_groups(session, frames)
            self.assertEqual(groups[str(first)], groups[str(second)])
            self.assertNotEqual(groups[str(first)], groups[str(old)])
            self.assertIn('Window unknown', labels[groups[str(old)]])


if __name__ == '__main__':
    unittest.main()
