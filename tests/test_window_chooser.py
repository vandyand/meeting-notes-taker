import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from window_picker import render_hicon, sort_window_choices, window_icon_image, window_process_name
from unittest import mock


class WindowChooserTests(unittest.TestCase):
    def test_browser_processes_appear_before_other_windows(self):
        windows = [
            {'hwnd': 1, 'title': 'A budget - Notepad', 'process_name': 'notepad.exe'},
            {'hwnd': 2, 'title': 'Z planning - Google Chrome', 'process_name': 'chrome.exe'},
            {'hwnd': 3, 'title': 'B call - Brave', 'process_name': 'brave.exe'},
            {'hwnd': 4, 'title': 'C chat - Slack', 'process_name': 'slack.exe'},
        ]
        self.assertEqual([item['hwnd'] for item in sort_window_choices(windows)], [3, 2, 1, 4])

    def test_unknown_process_uses_browser_title_as_fallback(self):
        windows = [
            {'hwnd': 1, 'title': 'A note', 'process_name': ''},
            {'hwnd': 2, 'title': 'B meeting - Microsoft Edge', 'process_name': ''},
        ]
        self.assertEqual([item['hwnd'] for item in sort_window_choices(windows)], [2, 1])

    @unittest.skipUnless(sys.platform == 'win32', 'Windows icon rendering')
    def test_renders_real_windows_icon_for_treeview(self):
        import win32con
        import win32gui
        hicon = win32gui.LoadIcon(0, win32con.IDI_APPLICATION)
        image = render_hicon(hicon)
        self.assertIsNotNone(image)
        self.assertEqual(image.size, (20, 20))
        self.assertGreater(len(image.getcolors(maxcolors=400)), 1)

    @unittest.skipUnless(sys.platform == 'win32', 'Windows icon lookup')
    def test_window_without_accessible_icon_does_not_block_chooser(self):
        import pywintypes
        import win32gui
        with mock.patch.object(win32gui, 'SendMessageTimeout', side_effect=pywintypes.error(0, 'SendMessageTimeout', 'No error')), \
             mock.patch.object(win32gui, 'GetClassLong', return_value=0):
            self.assertIsNone(window_icon_image(123))

    @unittest.skipUnless(sys.platform == 'win32', 'Windows process lookup')
    def test_inaccessible_process_falls_back_to_window_title(self):
        import psutil
        import win32process
        with mock.patch.object(win32process, 'GetWindowThreadProcessId', return_value=(1, 456)), \
             mock.patch.object(psutil, 'Process', side_effect=psutil.AccessDenied(456)):
            self.assertEqual(window_process_name(123), '')


if __name__ == '__main__':
    unittest.main()
