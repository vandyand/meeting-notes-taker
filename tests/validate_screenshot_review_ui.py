"""Read-only native review walkthrough; writes only diagnostic screenshots."""
import argparse
import ctypes
import sys
import time
import tkinter as tk
from pathlib import Path
from PIL import ImageGrab

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from screenshot_review import ScreenshotReview


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('session', type=Path)
    args = parser.parse_args()
    ctypes.windll.user32.SetProcessDPIAware()
    root = tk.Tk()
    root.withdraw()
    review = ScreenshotReview(root, args.session.resolve(), lambda _: None)
    review.dialog.transient('')
    review.dialog.deiconify()
    review.dialog.geometry('1000x700+100+100')
    review.dialog.attributes('-topmost', True)
    review.dialog.lift()
    def pump(seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            root.update()
            time.sleep(.01)
    pump(3)
    deadline = time.monotonic() + 10
    while not review.ready and time.monotonic() < deadline:
        pump(.1)
    assert review.ready, 'Wait for completed analysis before validating classification UI'
    output = Path(__file__).parent / 'artifacts'
    output.mkdir(exist_ok=True)
    for mode in ('40', 'All'):
        review.page_size_var.set(mode)
        review.change_page_size()
        pump(1)
        dialog = review.dialog
        for button in (review.global_crop_button, review.page_size_box, review.previous_button,
                       review.next_button, review.save_button):
            assert button.winfo_y() >= 0
            assert button.winfo_rooty() + button.winfo_height() <= dialog.winfo_rooty() + dialog.winfo_height()
        assert len(review.page_states) <= 40
        bbox = (dialog.winfo_rootx(), dialog.winfo_rooty(),
                dialog.winfo_rootx() + dialog.winfo_width(), dialog.winfo_rooty() + dialog.winfo_height())
        ImageGrab.grab(bbox=bbox, all_screens=True).save(output / f'review-{mode.lower()}.png')
        if mode == 'All':
            review.canvas.yview_moveto(1)
            pump(.5)
            if review.display_frames:
                assert review.display_frames[-1]['path'] in review.page_states
    review.show_duplicates_var.set(True)
    review.filter_changed()
    review.canvas.yview_moveto(4 / max(1, (len(review.frames) + 3) // 4))
    pump(1)
    dialog = review.dialog
    bbox = (dialog.winfo_rootx(), dialog.winfo_rooty(),
            dialog.winfo_rootx() + dialog.winfo_width(), dialog.winfo_rooty() + dialog.winfo_height())
    ImageGrab.grab(bbox=bbox, all_screens=True).save(output / 'review-show-duplicates.png')
    print(f'Native review validated: {len(review.frames)} images; {review.mask_summary_var.get()}')
    review.close()
    root.destroy()


if __name__ == '__main__':
    main()
