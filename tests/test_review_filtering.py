"""Native review regressions: classification, selection safety, and scroll reuse."""
import gc
import sys
import tempfile
import threading
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest import mock
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import screenshot_review as review_module
import screenshot_edits
import screenshot_window_context


class ReviewFilteringTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.session = Path(self.temp.name)
        self.root = tk.Tk()
        self.root.withdraw()
        self.review = None
        self.gate = threading.Event()

    def tearDown(self):
        if self.review and not self.review.closed:
            self.review.close()
        self.gate.set()
        if self.review:
            self.review.analysis_thread.join(2)
        self.review = None
        for callback in self.root.tk.call('after', 'info'):
            self.root.tk.call('after', 'cancel', callback)
        self.root.destroy()
        self.root = None
        gc.collect()
        self.temp.cleanup()

    def pump_until(self, condition):
        deadline = time.monotonic() + 3
        while not condition() and time.monotonic() < deadline:
            self.root.update()
            time.sleep(.01)
        self.assertTrue(condition())

    def open_review(self, count=4, pending=False):
        (self.session / 'screenshots').mkdir(exist_ok=True)
        frames = [{'path': str(self.session / 'screenshots' / f'image-{n}.png'), 'selected': True,
                   'automatic_selected': n != 1, 'reason': 'visual-change' if n != 1 else 'identical'}
                  for n in range(count)]
        # Avoid expensive full-resolution analysis; keep real Tk rendering and
        # actual thumbnail image events, which are the behavior under test.
        def analyze(*_):
            if pending:
                self.gate.wait(5)
            return frames
        patches = [mock.patch.object(review_module.windows_ocr, 'screenshot_paths', return_value=[Path(f['path']) for f in frames]),
                   mock.patch.object(review_module, 'load_review_frames', side_effect=analyze),
                   mock.patch.object(review_module, 'cached_thumbnail', side_effect=lambda *_: Image.new('RGB', (210, 140), 'white'))]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.review = review_module.ScreenshotReview(self.root, self.session, lambda _: None)
        self.pump_until(lambda: bool(self.review.frames) if pending else self.review.ready)
        return frames

    def test_default_filter_hides_duplicates_without_changing_manual_selection(self):
        frames = self.open_review()
        self.assertNotIn(frames[1]['path'], self.review.page_states)
        self.assertTrue(self.review.choices[frames[1]['path']])
        self.assertIn('1 hidden', self.review.filter_summary_var.get())
        self.assertIn('1 selected', self.review.filter_summary_var.get())
        self.review.show_duplicates_var.set(True)
        self.review.filter_changed()
        self.assertIn(frames[1]['path'], self.review.page_states)
        self.review.page_states[frames[1]['path']].set(False)
        self.review.choice_changed(frames[1]['path'])
        self.review.show_duplicates_var.set(False)
        self.review.filter_changed()
        saved = self.review.selected_frames()
        self.assertEqual(len(saved), 4)
        self.assertFalse(saved[1]['selected'])

    def test_border_and_label_follow_duplicate_classification_not_ocr_checkbox(self):
        frames = self.open_review()
        self.review.show_duplicates_var.set(True)
        self.review.filter_changed()
        unique = self.review.thumbnail_buttons[frames[0]['path']].master
        duplicate = self.review.thumbnail_buttons[frames[1]['path']].master
        self.assertEqual(unique.winfo_class(), 'Frame')
        self.assertEqual(duplicate.winfo_class(), 'Frame')
        self.assertEqual(int(unique.cget('highlightthickness')), 0)
        self.assertEqual(int(duplicate.cget('highlightthickness')), 3)
        self.assertEqual(duplicate.cget('highlightbackground'), '#b91c1c')
        self.review.set_all(False)
        self.assertEqual(int(unique.cget('highlightthickness')), 0)
        self.assertEqual(duplicate.cget('highlightbackground'), '#b91c1c')

    def test_unknown_images_remain_visible_and_neutral_until_check_finishes(self):
        frames = self.open_review(pending=True)
        self.assertEqual(len(self.review.page_states), 4)
        border = self.review.thumbnail_buttons[frames[0]['path']].master
        self.assertEqual(border.winfo_class(), 'Frame')
        self.assertEqual(int(border.cget('highlightthickness')), 0)
        self.review.set_all(True)
        self.gate.set()
        self.pump_until(lambda: self.review.ready)
        self.assertNotIn(frames[1]['path'], self.review.page_states)
        self.assertTrue(self.review.choices[frames[1]['path']])

    def test_all_scrolling_retains_overlapping_thumbnail_widgets_and_images(self):
        self.open_review(count=1150, pending=True)
        self.review.page_size_var.set('All')
        self.review.change_page_size()
        self.pump_until(lambda: bool(self.review.photos))
        before = dict(self.review.thumbnail_buttons)
        self.review.canvas.yview_moveto(.008)
        self.review.render_visible()
        shared = set(before) & set(self.review.thumbnail_buttons)
        self.assertTrue(shared)
        self.assertTrue(all(before[path] is self.review.thumbnail_buttons[path] for path in shared),
                        'Scrolling must not recreate overlapping thumbnails')
        self.assertLess(len(self.review.page_states), 40)

    def test_recent_previews_reappear_immediately_when_scrolling_back(self):
        frames = self.open_review(count=1150, pending=True)
        self.review.page_size_var.set('All')
        self.review.change_page_size()
        first = frames[0]['path']
        self.pump_until(lambda: bool(self.review.thumbnail_buttons[first].cget('image')))
        before = self.review.thumbnail_buttons[first].cget('image')
        self.review.canvas.yview_moveto(.1)
        self.review.render_visible()
        self.review.canvas.yview_moveto(0)
        self.review.render_visible()
        self.assertEqual(self.review.thumbnail_buttons[first].cget('image'), before,
                         'Recently viewed previews should return without async reload')

    def test_selected_card_background_tracks_checkbox_and_bulk_selection(self):
        frames = self.open_review()
        path = frames[0]['path']
        style = review_module.ttk.Style(self.root)
        cell = self.review.cells[path]['frame']
        self.assertEqual(style.lookup(cell.cget('style'), 'background'), '#dbeafe')
        self.review.page_states[path].set(False)
        self.review.choice_changed(path)
        self.assertNotEqual(style.lookup(cell.cget('style'), 'background'), '#dbeafe')
        self.review.set_all(True)
        self.assertEqual(style.lookup(cell.cget('style'), 'background'), '#dbeafe')

    def test_virtual_cards_keep_absolute_positions_without_layout_settle(self):
        self.open_review(count=1150, pending=True)
        r = self.review
        r.page_size_var.set('All')
        r.change_page_size()
        self.root.update()
        first = next(iter(r.cells.values()))
        self.assertIn('window', first, 'Virtual cards need independent stable canvas positions')
        before = {path: r.canvas.coords(cell['window']) for path, cell in r.cells.items()}
        r.canvas.yview_moveto(.008)
        r.render_visible()
        shared = set(before) & set(r.cells)
        self.assertTrue(shared)
        for path in shared:
            self.assertEqual(r.canvas.coords(r.cells[path]['window']), before[path])

    def test_manual_choice_survives_save_when_new_mode_agrees_with_it(self):
        frames = self.open_review()
        r = self.review
        r.show_duplicates_var.set(True)
        r.filter_changed()
        path = frames[1]['path']
        r.page_states[path].set(True)
        r.choice_changed(path)
        # Strict agrees with an image explicitly included under Near mode.
        r.frames[1]['automatic_selected'] = True
        saved = r.selected_frames()
        self.assertTrue(saved[1].get('manual_selected'),
                        'Save must not erase explicit choices merely because the current mode agrees')
        r.use_automatic()
        self.assertNotIn('manual_selected', r.selected_frames()[1])

    def test_automatic_reset_is_not_undone_by_rechecking_saved_manual_overrides(self):
        frames = self.open_review()
        r = self.review
        r.use_automatic()
        rechecked = [dict(frame) for frame in frames]
        rechecked[1].update(manual_selected=True, selected=True)
        r.events.put(('ready', rechecked, r.analysis_generation))
        r.poll()
        self.assertFalse(r.choices[frames[1]['path']],
                         'Reanalysis must not restore a saved override explicitly reset before Save')
        self.assertNotIn('manual_selected', r.selected_frames()[1])

    def click(self, widget, number=1):
        widget.event_generate('<Enter>')
        widget.event_generate(f'<ButtonPress-{number}>', x=5, y=5)
        widget.event_generate(f'<ButtonRelease-{number}>', x=5, y=5)
        self.root.update()

    def test_image_left_click_toggles_once_and_right_click_opens_large_preview(self):
        frames = self.open_review()
        path = frames[0]['path']
        r = self.review
        Image.new('RGB', (300, 200), 'white').save(path)
        self.click(r.thumbnail_buttons[path])
        self.assertFalse(r.choices[path])
        self.click(r.thumbnail_buttons[path])
        self.assertTrue(r.choices[path])
        self.click(r.thumbnail_buttons[path], 3)
        self.assertTrue(r.choices[path])
        preview = next(child for child in r.dialog.winfo_children() if isinstance(child, tk.Toplevel))
        self.assertIn('image-0.png', preview.title())
        with mock.patch.object(review_module.messagebox, 'askyesno', return_value=False):
            self.assertFalse(r.delete_image(path, preview))
        self.assertTrue(Path(path).exists())
        preview.destroy()

    def test_confirmed_delete_removes_image_and_card(self):
        frames = self.open_review()
        path = frames[0]['path']
        Image.new('RGB', (300, 200), 'white').save(path)
        self.review.open_original(path)
        preview = next(child for child in self.review.dialog.winfo_children() if isinstance(child, tk.Toplevel))
        with mock.patch.object(review_module.messagebox, 'askyesno', return_value=True), \
             mock.patch.object(self.review, 'start_reanalysis'):
            self.assertTrue(self.review.delete_image(path, preview))
        self.assertFalse(Path(path).exists())
        self.assertNotIn(path, self.review.choices)
        self.assertNotIn(path, {frame['path'] for frame in self.review.frames})

    def test_card_padding_and_border_toggle_but_checkbox_does_not_double_toggle(self):
        frames = self.open_review()
        path = frames[0]['path']
        r = self.review
        self.click(r.cells[path]['frame'])
        self.assertFalse(r.choices[path])
        self.click(r.cells[path]['border'])
        self.assertTrue(r.choices[path])
        self.click(r.cells[path]['check'])
        self.assertFalse(r.choices[path])
        self.assertIn(path, r.touched)

    def test_crop_button_does_not_toggle_selection(self):
        frames = self.open_review()
        path = frames[0]['path']
        with mock.patch.object(self.review, 'edit_image') as edit:
            self.click(self.review.cells[path]['edit'])
            self.assertTrue(self.review.choices[path])
            edit.assert_called_once_with(path)

    def test_window_filter_hides_other_windows_without_changing_ocr_choices(self):
        folder = self.session / 'screenshots'
        folder.mkdir()
        paths = [folder / f'image-{n}.png' for n in range(4)]
        screenshot_window_context.append_capture(self.session, paths[0], {'hwnd': 100, 'title': 'Browser'})
        screenshot_window_context.append_capture(self.session, paths[1], {'hwnd': 200, 'title': 'Slides'})
        frames = self.open_review()
        self.review.show_duplicates_var.set(True)
        self.review.filter_changed()
        self.review.set_window_filter({'hwnd:100'})
        self.assertEqual(set(self.review.page_states), {frames[0]['path']})
        self.assertEqual(len(self.review.selected_frames()), 4)
        self.assertTrue(all(frame['selected'] for frame in frames))
        self.review.set_window_filter(None)
        self.assertEqual(len(self.review.page_states), 4)

    def test_window_filter_dialog_applies_multiple_selected_windows(self):
        folder = self.session / 'screenshots'
        folder.mkdir()
        for index, hwnd in enumerate((100, 200, 300)):
            screenshot_window_context.append_capture(self.session, folder / f'image-{index}.png',
                                                     {'hwnd': hwnd, 'title': f'Window {hwnd}'})
        frames = self.open_review()
        self.review.show_duplicates_var.set(True)
        self.review.filter_changed()
        self.review.choose_window_filter()
        dialog = next(child for child in self.review.dialog.winfo_children() if isinstance(child, tk.Toplevel))
        listbox = next(widget for widget in dialog.winfo_children()[0].winfo_children()[1].winfo_children()
                       if isinstance(widget, tk.Listbox))
        listbox.selection_clear(0, tk.END)
        listbox.selection_set(0)
        listbox.selection_set(2)
        buttons = dialog.winfo_children()[0].winfo_children()[2]
        next(button for button in buttons.winfo_children() if button.cget('text') == 'Apply').invoke()
        self.assertEqual(set(self.review.page_states), {frames[0]['path'], frames[2]['path']})

    def test_mask_save_refreshes_previews_without_restarting_duplicate_check(self):
        frames = self.open_review()
        for frame in frames:
            Image.new('RGB', (100, 100), 'white').save(frame['path'])
        r = self.review
        before = r.analysis_generation
        screenshot_edits.save_edit(self.session, frames[0]['path'], {'crop': [0, 0, .5, 1]})
        r.edits_changed()
        self.assertTrue(r.ready)
        self.assertEqual(r.analysis_generation, before)
        self.assertFalse(r.frames[1]['automatic_selected'])

    def test_global_editor_can_choose_another_screenshot_and_promote_an_individual_crop(self):
        frames = self.open_review()
        for frame in frames:
            Image.new('RGB', (300, 120), 'white').save(frame['path'])
        r = self.review
        r.edit_image(None)
        windows = [widget for widget in r.dialog.winfo_children() if isinstance(widget, tk.Toplevel)]
        self.assertTrue(windows)
        global_editor = windows[0].editor
        global_editor.select_sample(2)
        self.assertEqual(global_editor.sample, Path(frames[2]['path']))
        global_editor.dialog.destroy()
        r.edit_image(frames[2]['path'])
        windows = [widget for widget in r.dialog.winfo_children() if isinstance(widget, tk.Toplevel)]
        individual = windows[0].editor
        individual.middle_third()
        individual.save_global()
        edits = screenshot_edits.load_edits(self.session)
        self.assertEqual(edits['global']['crop'], [1/3, 0, 2/3, 1])
        self.assertNotIn(screenshot_edits.path_key(frames[2]['path']), edits.get('individual', {}))
        self.assertEqual(screenshot_edits.load_image(self.session, Path(frames[0]['path'])).size, (100, 120))


if __name__ == '__main__':
    unittest.main()
