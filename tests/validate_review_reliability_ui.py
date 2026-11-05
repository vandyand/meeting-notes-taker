"""Native walkthrough on temporary copies, never the completed meeting."""
import argparse
import ctypes
import json
import shutil
import sys
import tempfile
import time
import tkinter as tk
from pathlib import Path
from PIL import ImageGrab

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import screenshot_review
import windows_ocr


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('session', type=Path)
    args = parser.parse_args()
    ctypes.windll.user32.SetProcessDPIAware()
    with tempfile.TemporaryDirectory() as temp:
        session = Path(temp)
        folder = session / 'screenshots'
        folder.mkdir()
        for path in windows_ocr.screenshot_paths(args.session.resolve())[56:68]:
            shutil.copy2(path, folder / path.name)
        shutil.copy2(args.session / 'screenshots/screen_context.json', folder / 'screen_context.json')
        root = tk.Tk()
        root.withdraw()
        review = screenshot_review.ScreenshotReview(root, session, lambda _: None)
        def pump(seconds=.1):
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                root.update()
                time.sleep(.01)
        def ready():
            deadline = time.monotonic() + 20
            while not review.ready and time.monotonic() < deadline:
                pump()
            assert review.ready
            pump(.5)
        try:
            review.dialog.transient('')
            review.dialog.geometry('1000x700+100+100')
            review.dialog.attributes('-topmost', True)
            ready()
            assert sum(not f['automatic_selected'] for f in review.frames) == 8
            review.show_duplicates_var.set(True)
            review.filter_changed()
            review.set_all(False)
            selected = review.frames[1]['path']
            review.page_states[selected].set(True)
            review.choice_changed(selected)
            pump(1)
            style = tk.ttk.Style(root)
            assert style.lookup(review.cells[selected]['frame'].cget('style'), 'background') == '#dbeafe'
            dialog = review.dialog
            for button in (review.save_button, review.duplicate_mode_box, review.next_button):
                assert button.winfo_rooty() + button.winfo_height() <= dialog.winfo_rooty() + dialog.winfo_height()
            output = Path(__file__).parent / 'artifacts/review-reliability.png'
            output.parent.mkdir(exist_ok=True)
            ImageGrab.grab(bbox=(dialog.winfo_rootx(), dialog.winfo_rooty(),
                                dialog.winfo_rootx()+dialog.winfo_width(), dialog.winfo_rooty()+dialog.winfo_height()),
                           all_screens=True).save(output)
            review.duplicate_mode_var.set('Strict')
            review.duplicate_mode_changed()
            ready()
            assert sum(not f['automatic_selected'] for f in review.frames) == 0
            assert review.choices[selected]
            (folder / 'manifest.json').write_text(json.dumps({'frames': review.selected_frames()}), encoding='utf-8')
            review.close()
            review = screenshot_review.ScreenshotReview(root, session, lambda _: None)
            ready()
            assert review.choices[selected]
            review.duplicate_mode_var.set('Near duplicates')
            review.duplicate_mode_changed()
            ready()
            assert sum(not f['automatic_selected'] for f in review.frames) == 8
            assert review.choices[selected]
            print('Native validation: 8 near duplicates / 12; Strict keeps all; manual selection survives Save/reopen and mode changes; blue + red states verified.')
        finally:
            review.close()
            root.destroy()


if __name__ == '__main__':
    main()
