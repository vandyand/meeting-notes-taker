import sys
import tempfile
import tkinter as tk
import unittest
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from window_placement import centered_near_parent
from screenshot_review import ScreenshotReview
from screenshot_crop_ui import CropMaskEditor


class WindowPlacementTests(unittest.TestCase):
    def test_centers_dialog_over_parent(self):
        self.assertEqual(
            centered_near_parent((1000, 100, 1120, 820), (1000, 700), (0, 0, 5120, 1440)),
            '1000x700+1060+160',
        )

    def test_clamps_dialog_to_visible_desktop(self):
        self.assertEqual(
            centered_near_parent((4700, 1000, 400, 300), (1000, 700), (0, 0, 5120, 1440)),
            '1000x700+4120+740',
        )

    def test_review_and_mask_dialogs_open_near_app(self):
        with tempfile.TemporaryDirectory() as temp:
            session = Path(temp)
            screenshots = session / 'screenshots'
            screenshots.mkdir()
            sample = screenshots / 'sample.png'
            Image.new('RGB', (100, 100)).save(sample)
            root = tk.Tk()
            root.geometry('1120x820+1000+100')
            root.deiconify()
            review = None
            editor = None
            try:
                root.update()
                review = ScreenshotReview(root, session, lambda _frames: None)
                root.update()
                editor = CropMaskEditor(review.dialog, session, sample, False, lambda: None)
                root.update()
                self.assertGreater(review.dialog.winfo_rootx(), 800)
                self.assertGreater(editor.dialog.winfo_rootx(), 800, editor.dialog.wm_geometry())
            finally:
                if editor is not None:
                    editor.dialog.destroy()
                if review is not None:
                    review.close()
                root.destroy()


if __name__ == '__main__':
    unittest.main()
