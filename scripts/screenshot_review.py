"""Responsive screenshot review with cached analysis and paged previews."""

from __future__ import annotations

import hashlib
import json
import os
import queue
import threading
import time
import tkinter as tk
from collections import OrderedDict
from math import ceil
from pathlib import Path
from tkinter import messagebox, ttk

from PIL import Image, ImageTk, ImageOps

import windows_ocr
import screenshot_edits
import screenshot_window_context
import screenshot_deletion
from screenshot_analysis import analyzed_frames, comparison_options, comparison_policy, save_duplicate_mode, source_signature
from screenshot_crop_ui import CropMaskEditor
from window_placement import place_dialog_near_parent


class ReviewCancelled(Exception):
    pass


def load_review_frames(session: Path, cancelled: threading.Event, progress) -> list[dict]:
    images = windows_ocr.screenshot_paths(session)
    edits = screenshot_edits.load_edits(session)
    def on_progress(current, total):
        if cancelled.is_set():
            raise ReviewCancelled()
        progress(current, total)
    options = comparison_options(session, edits)
    frames = analyzed_frames(session, images, edits,
                             lambda paths, **kw: windows_ocr.select_screenshots(paths, **kw, **options), on_progress)
    if cancelled.is_set():
        raise ReviewCancelled()
    windows_ocr.apply_manual_overrides([], frames, windows_ocr.manual_overrides(session / 'screenshots' / 'manifest.json'))
    return frames


def cached_thumbnail(path: Path, cache_root: Path) -> Image.Image:
    stat = path.stat()
    session = cache_root.parent.parent
    token = screenshot_edits.fingerprint(session, path)
    identity = f'{path.resolve()}:{stat.st_size}:{stat.st_mtime_ns}:210x140:v1:{token}'
    target = cache_root / 'thumbnails' / (hashlib.sha256(identity.encode()).hexdigest() + '.jpg')
    try:
        with Image.open(target) as preview:
            return preview.copy()
    except (OSError, ValueError):
        pass
    with (screenshot_edits.load_image(session, path) if token else Image.open(path)) as source:
        source.draft('RGB', (210, 140))
        source.thumbnail((210, 140), Image.Resampling.BILINEAR)
        preview = source.convert('RGB')
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        preview.save(target, 'JPEG', quality=75)
    except OSError:
        pass
    return preview


class ScreenshotReview:
    PAGE_SIZE = 40
    ROW_HEIGHT = 230

    def __init__(self, root: tk.Misc, session: Path, on_save, allow_edits=True):
        self.session = session
        self.on_save = on_save
        self.allow_edits = allow_edits
        self.frames: list[dict] = []
        self.display_frames: list[dict] = []
        self.ready = False
        self.choices: dict[str, bool] = {}
        self.window_filter: set[str] | None = None
        self.window_groups: dict[str, str] = {}
        self.window_labels: dict[str, str] = {}
        self.touched = set()
        self.automatic_reset = False
        self.visible_range = None
        self.viewport_job = None
        self.page_states: dict[str, tk.BooleanVar] = {}
        self.thumbnail_buttons = {}
        self.cells = {}
        self.preview_cache = OrderedDict()
        self.photos = []
        self.page = 0
        self.generation = 0
        self.closed = False
        self.cancelled = threading.Event()
        self.preview_cancelled = threading.Event()
        self.analysis_generation = 0
        self.events = queue.Queue()
        self.dialog = tk.Toplevel(root)
        style = ttk.Style(self.dialog)
        style.configure('Selected.Screenshot.TFrame', background='#dbeafe')
        style.configure('Selected.Screenshot.TCheckbutton', background='#dbeafe')
        style.map('Selected.Screenshot.TCheckbutton', background=[('active', '#dbeafe')])
        self.dialog.review = self
        self.dialog.title(f'Screenshot Review — {session.name}')
        place_dialog_near_parent(self.dialog, root, 1000, 700)
        self.dialog.minsize(980, 500)
        self.dialog.transient(root)
        self.dialog.protocol('WM_DELETE_WINDOW', self.close)
        self.dialog.bind('<Escape>', lambda _event: self.close())
        header = ttk.Frame(self.dialog, padding=10)
        header.pack(fill='x')
        self.status_var = tk.StringVar(value='Analyzing screenshots — the window is ready; you can cancel at any time.')
        ttk.Label(header, textvariable=self.status_var, wraplength=950).pack(fill='x')
        ttk.Label(header, text='Click a card to include/exclude it in OCR. Right-click an image for a large preview and permanent delete. Select All / Deselect All apply to the whole meeting.', wraplength=950).pack(fill='x', pady=(5, 0))
        self.loading = ttk.Progressbar(header, mode='indeterminate')
        self.loading.pack(fill='x', pady=(6, 0))
        self.loading.start(20)
        navigation = ttk.Frame(self.dialog, padding=(10, 0, 10, 8))
        navigation.pack(fill='x')
        self.global_crop_button = ttk.Button(navigation, text='Manage image masks', command=lambda: self.edit_image(None), state='disabled')
        self.global_crop_button.pack(side='left')
        ttk.Label(navigation, text='Images per page:').pack(side='left', padx=(12, 4))
        self.page_size_var = tk.StringVar(master=self.dialog, value='40')
        self.page_size_box = ttk.Combobox(navigation, textvariable=self.page_size_var, values=('40', '100', 'All'), state='readonly', width=5)
        self.page_size_box.pack(side='left')
        self.page_size_box.bind('<<ComboboxSelected>>', self.change_page_size)
        self.show_duplicates_var = tk.BooleanVar(master=self.dialog, value=False)
        ttk.Checkbutton(navigation, text='Show duplicates', variable=self.show_duplicates_var,
                        command=self.filter_changed).pack(side='left', padx=(10, 0))
        self.window_filter_button = ttk.Button(navigation, text='Windows: All', command=self.choose_window_filter)
        self.window_filter_button.pack(side='left', padx=(10, 0))
        self.page_var = tk.StringVar(value='Preparing screenshot selection…')
        ttk.Label(navigation, textvariable=self.page_var).pack(side='left', padx=8)
        self.preview_var = tk.StringVar(value='')
        ttk.Label(navigation, textvariable=self.preview_var).pack(side='right')
        self.mask_summary_var = tk.StringVar(master=self.dialog, value=screenshot_edits.summary(session))
        ttk.Label(self.dialog, textvariable=self.mask_summary_var, padding=(10, 0, 10, 8)).pack(fill='x')
        policy_controls = ttk.Frame(self.dialog, padding=(10, 0, 10, 6))
        policy_controls.pack(fill='x')
        ttk.Label(policy_controls, text='Duplicate check:').pack(side='left')
        self.duplicate_mode_var = tk.StringVar(master=self.dialog, value=comparison_policy(session)['mode'])
        self.duplicate_mode_box = ttk.Combobox(policy_controls, textvariable=self.duplicate_mode_var,
                                             values=('Aggressive', 'Near duplicates', 'Strict'), state='readonly', width=17)
        self.duplicate_mode_box.pack(side='left', padx=6)
        self.duplicate_mode_box.bind('<<ComboboxSelected>>', self.duplicate_mode_changed)
        ttk.Label(policy_controls, text='Aggressive: adjacent-frame similarity; small changes may be hidden. Originals are kept.').pack(side='left')
        self.filter_summary_var = tk.StringVar(master=self.dialog, value='Red: duplicate · No border: unique or checking. Hiding does not change OCR selections.')
        ttk.Label(self.dialog, textvariable=self.filter_summary_var, padding=(10, 0, 10, 6), wraplength=950).pack(fill='x')
        body = ttk.Frame(self.dialog, padding=(10, 0, 10, 8))
        body.pack(fill='both', expand=True)
        self.canvas = tk.Canvas(body, highlightthickness=0)
        scrollbar = ttk.Scrollbar(body, orient='vertical', command=self.canvas.yview)
        self.grid = ttk.Frame(self.canvas)
        self.grid.bind('<Configure>', self.grid_configured)
        self.grid_window = self.canvas.create_window((0, 0), window=self.grid, anchor='nw')
        def on_scroll(first, last):
            scrollbar.set(first, last)
            self.schedule_viewport()
        self.canvas.configure(yscrollcommand=on_scroll)
        self.canvas.bind('<Configure>', lambda _event: self.schedule_viewport())
        self.canvas.pack(side='left', fill='both', expand=True)
        scrollbar.pack(side='right', fill='y')
        self.dialog.bind('<MouseWheel>', self.scroll)
        bottom_navigation = ttk.Frame(self.dialog, padding=(10, 0, 10, 8))
        bottom_navigation.pack(fill='x')
        self.previous_button = ttk.Button(bottom_navigation, text='Previous page', command=lambda: self.change_page(-1), state='disabled')
        self.previous_button.pack(side='left')
        self.next_button = ttk.Button(bottom_navigation, text='Next page', command=lambda: self.change_page(1), state='disabled')
        self.next_button.pack(side='right')
        actions = ttk.Frame(self.dialog, padding=(10, 0, 10, 10))
        actions.pack(fill='x')
        self.choice_buttons = []
        for label, command in [('Use Automatic Selection', self.use_automatic), ('Select All', lambda: self.set_all(True)), ('Deselect All', lambda: self.set_all(False))]:
            button = ttk.Button(actions, text=label, command=command, state='disabled')
            button.pack(side='left', padx=(0, 8))
            self.choice_buttons.append(button)
        self.save_button = ttk.Button(actions, text='Save Choices', command=self.save, state='disabled')
        self.save_button.pack(side='right')
        ttk.Button(actions, text='Cancel', command=self.close).pack(side='right', padx=8)
        self.poll_id = self.dialog.after(30, self.poll)
        self.analysis_thread = threading.Thread(target=self.analyze, args=(self.session, self.events, self.cancelled, self.analysis_generation), daemon=True)
        self.analysis_thread.start()

    def scroll(self, event):
        if event.delta:
            self.canvas.yview_scroll(-1 if event.delta > 0 else 1, 'units')
        return 'break'

    @staticmethod
    def analyze(session, events, cancelled, generation):
        last_update = 0.0
        def progress(current, total):
            nonlocal last_update
            now = time.monotonic()
            if now - last_update >= 0.15 or current == total:
                events.put(('analysis', current, total, generation))
                last_update = now
        try:
            images = windows_ocr.screenshot_paths(session)
            events.put(('sources', [{'path': str(path), 'selected': True,
                                         'automatic_selected': True, 'reason': 'Analyzing selection…'}
                                        for path in images], generation))
            frames = load_review_frames(session, cancelled, progress)
            if not cancelled.is_set():
                events.put(('ready', frames, generation))
        except ReviewCancelled:
            pass
        except Exception as exc:
            events.put(('error', str(exc), generation))

    def poll(self):
        if self.closed:
            return
        for _ in range(24):
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            if event[0] in {'sources', 'analysis', 'ready', 'error'} and event[-1] != self.analysis_generation:
                continue
            if event[0] == 'sources' and event[1]:
                self.frames = event[1]
                self.refresh_window_groups()
                overrides = windows_ocr.manual_overrides(self.session / 'screenshots' / 'manifest.json')
                self.choices = {entry['path']: self.choices.get(entry['path'], overrides.get(windows_ocr.path_key(Path(entry['path'])), True)) for entry in self.frames}
                for button in self.choice_buttons[1:] + [self.save_button]:
                    button.configure(state='normal')
                self.render_page()
                self.global_crop_button.configure(state='normal' if self.allow_edits else 'disabled')
            elif event[0] == 'analysis':
                self.status_var.set(f'Duplicate check: {event[1]} of {event[2]}. You can select and save now; no images are removed.')
                self.loading.stop()
                self.loading.configure(mode='determinate', maximum=max(1, event[2]), value=event[1])
            elif event[0] == 'ready':
                self.remember_page()
                self.ready = True
                self.frames = event[1]
                self.refresh_window_groups()
                if self.automatic_reset:
                    for entry in self.frames:
                        entry.pop('manual_selected', None)
                        entry.pop('manual_source', None)
                        entry.pop('manual_edit', None)
                        entry['selected'] = bool(entry['automatic_selected'])
                self.choices = {entry['path']: self.choices[entry['path']] if entry['path'] in self.touched else bool(entry['selected']) for entry in self.frames}
                self.loading.stop()
                self.loading.pack_forget()
                if self.frames:
                    for button in self.choice_buttons + [self.save_button]:
                        button.configure(state='normal')
                    self.global_crop_button.configure(state='normal' if self.allow_edits else 'disabled')
                    self.render_page(preserve_anchor=True)
                else:
                    self.status_var.set('No screenshots captured for this meeting.')
            elif event[0] == 'thumbnail' and event[1] == self.generation:
                _, _, path, preview, current, total = event
                photo = None
                if preview is not None:
                    photo = ImageTk.PhotoImage(preview, master=self.dialog)
                    self.preview_cache[path] = photo
                    self.preview_cache.move_to_end(path)
                    while len(self.preview_cache) > 128:
                        self.preview_cache.popitem(last=False)
                button = self.thumbnail_buttons.get(path)
                if button:
                    if photo is not None:
                        self.cells[path]['photo'] = photo
                        button.configure(image=photo, text='')
                    else:
                        button.configure(text='Preview unavailable\nRight-click to open original')
                self.photos = [cell['photo'] for cell in self.cells.values() if cell['photo'] is not None]
                self.preview_var.set(f'Previews ready: {len(self.photos)} of {len(self.cells)} nearby images')
            elif event[0] == 'error':
                self.loading.stop()
                self.loading.pack_forget()
                self.status_var.set(f'Could not analyze screenshots: {event[1]}. Close and reopen to retry.')
        self.poll_id = self.dialog.after(30, self.poll)

    def remember_page(self):
        for path, state in self.page_states.items():
            self.choices[path] = state.get()

    def choice_changed(self, path):
        self.touched.add(path)
        self.update_summary()

    def toggle_card(self, path):
        state = self.page_states.get(path)
        if state is not None:
            state.set(not state.get())
            self.choice_changed(path)
        return 'break'

    def update_summary(self):
        self.remember_page()
        for path, cell in self.cells.items():
            selected = bool(self.choices.get(path))
            cell['frame'].configure(style='Selected.Screenshot.TFrame' if selected else 'TFrame')
            cell['check'].configure(style='Selected.Screenshot.TCheckbutton' if selected else 'TCheckbutton')
            cell['border'].configure(background='#dbeafe' if selected else self.dialog.cget('background'))
        if self.ready:
            self.status_var.set(f'{len(self.frames)} screenshots · {sum(self.choices.values())} selected for OCR · {self.duplicate_mode_var.get()} mode. Blue cards are selected.')
        visible_paths = {entry['path'] for entry in self.display_frames}
        hidden = [entry for entry in self.frames if self.ready and entry['path'] not in visible_paths]
        hidden_selected = sum(bool(self.choices.get(entry['path'])) for entry in hidden)
        self.filter_summary_var.set(f'Red: duplicate · No border: unique or checking · {len(hidden)} hidden by filters ({hidden_selected} selected for OCR). Bulk selection includes hidden images.')

    def filter_changed(self):
        self.remember_page()
        self.render_page(preserve_anchor=True)

    def refresh_window_groups(self):
        self.window_groups, self.window_labels = screenshot_window_context.window_groups(self.session, self.frames)
        total = len(self.window_labels)
        shown = total if self.window_filter is None else len(self.window_filter & self.window_labels.keys())
        if not total or set(self.window_labels) == {screenshot_window_context.UNKNOWN}:
            self.window_filter_button.configure(text='Windows: Unknown (older captures)', state='disabled')
        else:
            self.window_filter_button.configure(text=f'Windows: All ({total})' if self.window_filter is None else f'Windows: {shown} of {total}', state='normal')

    def set_window_filter(self, groups: set[str] | None):
        self.window_filter = None if groups is None or groups == set(self.window_labels) else set(groups)
        self.refresh_window_groups()
        self.filter_changed()

    def choose_window_filter(self):
        if not self.window_labels:
            return
        dialog = tk.Toplevel(self.dialog)
        dialog.title('Show screenshots from windows')
        dialog.transient(self.dialog)
        dialog.geometry('550x390')
        frame = ttk.Frame(dialog, padding=12)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='Choose one or more active windows. This changes only what is shown, not OCR selections.',
                  wraplength=510).pack(anchor='w', pady=(0, 8))
        list_frame = ttk.Frame(frame)
        list_frame.pack(fill='both', expand=True)
        choices = tk.Listbox(list_frame, selectmode=tk.MULTIPLE, exportselection=False)
        scrollbar = ttk.Scrollbar(list_frame, orient='vertical', command=choices.yview)
        choices.configure(yscrollcommand=scrollbar.set)
        choices.pack(side='left', fill='both', expand=True)
        scrollbar.pack(side='right', fill='y')
        keys = list(self.window_labels)
        counts = {key: sum(group == key for group in self.window_groups.values()) for key in keys}
        for index, key in enumerate(keys):
            choices.insert(tk.END, f'{self.window_labels[key]} ({counts[key]})')
            if self.window_filter is None or key in self.window_filter:
                choices.selection_set(index)
        buttons = ttk.Frame(frame)
        buttons.pack(fill='x', pady=(8, 0))
        ttk.Button(buttons, text='Show all', command=lambda: (self.set_window_filter(None), dialog.destroy())).pack(side='left')
        ttk.Button(buttons, text='Cancel', command=dialog.destroy).pack(side='right')
        ttk.Button(buttons, text='Apply', command=lambda: (self.set_window_filter({keys[index] for index in choices.curselection()}), dialog.destroy())).pack(side='right', padx=8)

    def page_size(self):
        return max(1, len(self.display_frames)) if self.page_size_var.get() == 'All' else int(self.page_size_var.get())

    def change_page_size(self, _event=None):
        self.remember_page()
        self.page = 0
        self.render_page()

    def grid_configured(self, _event=None):
        if self.page_size_var.get() != 'All':
            self.canvas.configure(scrollregion=self.canvas.bbox('all'))

    def schedule_viewport(self):
        if not self.closed and self.page_size_var.get() == 'All' and self.viewport_job is None:
            self.viewport_job = self.dialog.after_idle(self.render_visible)

    def render_visible(self):
        self.viewport_job = None
        if self.closed or self.page_size_var.get() != 'All':
            return
        top = max(0, int(self.canvas.canvasy(0)) // self.ROW_HEIGHT - 2)
        rows = max(1, ceil(self.canvas.winfo_height() / self.ROW_HEIGHT) + 5)
        start, end = top * 4, min(len(self.display_frames), (top + rows) * 4)
        if self.visible_range == (start, end):
            return
        self.remember_page()
        self.visible_range = (start, end)
        self.draw_entries(self.display_frames[start:end], virtual=True, start_index=start)

    def render_page(self, preserve_anchor=False):
        anchor = None
        if preserve_anchor and self.display_frames:
            old_index = (int(max(0, self.canvas.canvasy(0))) // self.ROW_HEIGHT * 4
                         if self.page_size_var.get() == 'All' else self.page * self.page_size())
            anchor = self.display_frames[min(len(self.display_frames) - 1, old_index)]['path']
        self.display_frames = [entry for entry in self.frames
                               if (self.window_filter is None or self.window_groups.get(entry['path'], screenshot_window_context.UNKNOWN) in self.window_filter)
                               and (not self.ready or self.show_duplicates_var.get() or entry['automatic_selected'])]
        size = self.page_size()
        anchor_index = 0
        if anchor:
            raw_order = {entry['path']: n for n, entry in enumerate(self.frames)}
            anchor_order = raw_order.get(anchor, 0)
            anchor_index = next((n for n, entry in enumerate(self.display_frames)
                                 if raw_order[entry['path']] >= anchor_order), max(0, len(self.display_frames) - 1))
            self.page = anchor_index // size
        pages = max(1, (len(self.display_frames) + size - 1) // size)
        self.page = max(0, min(self.page, pages - 1))
        first = self.page * size
        entries = self.display_frames[first:first + size]
        self.page_var.set('No screenshots match current filters' if not self.display_frames else (f'All {len(self.display_frames)} visible screenshots' if self.page_size_var.get() == 'All' else f'Page {self.page + 1} of {pages} · images {first + 1}–{first + len(entries)}'))
        self.previous_button.configure(state='normal' if self.page else 'disabled')
        self.next_button.configure(state='normal' if self.page + 1 < pages else 'disabled')
        self.visible_range = None
        self.canvas.coords(self.grid_window, 0, 0)
        if self.page_size_var.get() == 'All':
            height = ((len(self.display_frames) + 3) // 4) * self.ROW_HEIGHT
            self.canvas.configure(scrollregion=(0, 0, 940, max(1, height)), yscrollincrement=25)
            self.canvas.yview_moveto((anchor_index // 4 * self.ROW_HEIGHT / height) if anchor and height else 0)
            self.render_visible()
        else:
            self.draw_entries(entries)
            self.canvas.configure(yscrollincrement=25, scrollregion=(0, 0, 940, max(1, ceil(len(entries) / 4) * self.ROW_HEIGHT)))
            self.canvas.yview_moveto(0)
        self.update_summary()

    def draw_entries(self, entries, virtual=False, start_index=0):
        self.preview_cancelled.set()
        self.preview_cancelled = threading.Event()
        self.generation += 1
        paths = {entry['path'] for entry in entries}
        for path in list(self.cells):
            if path not in paths:
                removed = self.cells.pop(path)
                self.canvas.delete(removed['window'])
                removed['frame'].destroy()
                self.page_states.pop(path, None)
                self.thumbnail_buttons.pop(path, None)
        pending = []
        for index, entry in enumerate(entries):
            path = entry['path']
            kind = 'Checking…' if not self.ready else ('Unique' if entry['automatic_selected'] else ('Possible duplicate' if entry.get('reason') == 'perceptual-duplicate' else 'Near duplicate' if any(region.get('near_duplicate') for region in entry.get('change_regions', [])) else 'Duplicate'))
            color = '#b91c1c'
            thickness = 3 if self.ready and not entry['automatic_selected'] else 0
            existing = self.cells.get(path)
            if existing is None:
                cell = ttk.Frame(self.canvas, padding=5, relief='ridge', width=228, height=self.ROW_HEIGHT - 8)
                cell.pack_propagate(False)
                border = tk.Frame(cell, highlightthickness=thickness, highlightbackground=color, highlightcolor=color,
                                  width=216, height=146)
                border.pack()
                border.pack_propagate(False)
                cell.bind('<Button-1>', lambda _event, value=path: self.toggle_card(value))
                border.bind('<Button-1>', lambda _event, value=path: self.toggle_card(value))
                button = ttk.Button(border, text='Loading preview…', command=lambda value=path: self.toggle_card(value))
                button.bind('<Button-3>', lambda _event, value=path: (self.open_original(value), 'break')[1])
                button.pack(fill='both', expand=True)
                state = tk.BooleanVar(master=self.dialog, value=self.choices[path])
                check = ttk.Checkbutton(cell, variable=state, command=lambda value=path: self.choice_changed(value))
                check.pack(anchor='w')
                edit_button = ttk.Button(cell, text='Crop / mask this image', command=lambda value=path: self.edit_image(value))
                edit_button.pack(anchor='w', pady=(3, 0))
                existing = self.cells[path] = {'frame': cell, 'border': border, 'check': check,
                                             'edit': edit_button, 'photo': None,
                                             'window': self.canvas.create_window(0, 0, window=cell, anchor='nw', width=228, height=self.ROW_HEIGHT-8)}
                self.thumbnail_buttons[path] = button
                self.page_states[path] = state
            cell = existing['frame']
            position = start_index + index
            self.canvas.coords(existing['window'], (position % 4) * 236 + 4, (position // 4) * self.ROW_HEIGHT + 4)
            button = self.thumbnail_buttons[path]
            self.page_states[path].set(self.choices[path])
            existing['border'].configure(highlightthickness=thickness, highlightbackground=color, highlightcolor=color)
            name = Path(path).name
            display_name = name if len(name) <= 32 else name[:29] + '…'
            existing['check'].configure(text=f'{display_name}\n{kind}')
            existing['edit'].configure(state='normal' if self.ready and self.allow_edits else 'disabled')
            if existing['photo'] is None:
                photo = self.preview_cache.get(path)
                if photo is not None:
                    self.preview_cache.move_to_end(path)
                    existing['photo'] = photo
                    button.configure(image=photo, text='')
                else:
                    pending.append(entry)
        self.photos = [cell['photo'] for cell in self.cells.values() if cell['photo'] is not None]
        self.preview_var.set(f'Previews ready: {len(self.photos)} of {len(entries)} nearby images')
        if pending:
            # Visible rows are loaded first, then the two-row scroll buffer.
            center = len(entries) // 2
            order = {entry['path']: n for n, entry in enumerate(entries)}
            pending.sort(key=lambda entry: abs(order[entry['path']] - center))
            threading.Thread(target=self.load_previews, args=(self.session, self.events, self.preview_cancelled,
                             pending, self.generation), daemon=True).start()
        self.update_summary()

    @staticmethod
    def load_previews(session, events, cancelled, entries, generation):
        for index, entry in enumerate(entries):
            if cancelled.is_set():
                return
            try:
                preview = cached_thumbnail(Path(entry['path']), session / 'screenshots' / '.review-cache')
            except Exception:
                preview = None
            events.put(('thumbnail', generation, entry['path'], preview, index + 1, len(entries)))

    def change_page(self, direction):
        self.remember_page()
        self.page += direction
        self.render_page()

    def set_all(self, selected):
        self.choices = {path: selected for path in self.choices}
        self.touched.update(self.choices)
        for state in self.page_states.values():
            state.set(selected)
        self.update_summary()

    def use_automatic(self):
        self.touched.clear()
        self.automatic_reset = True
        for entry in self.frames:
            entry.pop('manual_selected', None)
            entry.pop('manual_source', None)
            entry.pop('manual_edit', None)
        self.choices = {entry['path']: bool(entry['automatic_selected']) for entry in self.frames}
        for path, state in self.page_states.items():
            state.set(self.choices[path])
        self.update_summary()

    def open_original(self, path):
        try:
            with Image.open(path) as source:
                preview_image = ImageOps.contain(source.convert('RGB'), (980, 620))
            preview = tk.Toplevel(self.dialog)
            preview.title(f'Screenshot preview — {Path(path).name}')
            preview.transient(self.dialog)
            preview.photo = ImageTk.PhotoImage(preview_image, master=preview)
            frame = ttk.Frame(preview, padding=10)
            frame.pack(fill='both', expand=True)
            ttk.Label(frame, image=preview.photo).pack()
            ttk.Label(frame, text=Path(path).name).pack(anchor='w', pady=(6, 0))
            buttons = ttk.Frame(frame)
            buttons.pack(fill='x', pady=(8, 0))
            ttk.Button(buttons, text='Delete screenshot permanently',
                       command=lambda: self.delete_image(path, preview),
                       state='normal' if self.ready and self.allow_edits else 'disabled').pack(side='left')
            ttk.Button(buttons, text='Open full resolution', command=lambda: os.startfile(path)).pack(side='left', padx=8)
            ttk.Button(buttons, text='Close', command=preview.destroy).pack(side='right')
            preview.bind('<Escape>', lambda _event: preview.destroy())
            preview.grab_set()
        except (OSError, ValueError) as exc:
            messagebox.showerror('Could not open screenshot', str(exc), parent=self.dialog)

    def delete_image(self, path, preview) -> bool:
        if not self.ready or not self.allow_edits:
            return False
        name = Path(path).name
        confirmed = messagebox.askyesno('Permanently delete screenshot?',
            f'Delete {name} permanently? The original and generated image copies will be removed. '
            'Existing meeting notes are not rewritten; regenerate them if they mention this image.',
            parent=preview, icon='warning', default='no')
        if not confirmed:
            return False
        try:
            self.on_save(self.selected_frames())
            screenshot_deletion.delete_screenshot(self.session, Path(path))
        except Exception as exc:
            messagebox.showerror('Could not delete screenshot', str(exc), parent=preview)
            return False
        preview.destroy()
        self.frames = [frame for frame in self.frames if frame['path'] != path]
        self.choices.pop(path, None)
        self.touched.discard(path)
        self.refresh_window_groups()
        self.start_reanalysis('Rechecking duplicates after screenshot deletion…')
        return True

    def selected_frames(self):
        self.remember_page()
        for entry in self.frames:
            selected = self.choices[entry['path']]
            automatic = bool(entry['automatic_selected'])
            entry['selected'] = selected
            if not self.ready:
                stat = Path(entry['path']).stat()
                entry.update(manual_selected=selected, manual_source=[stat.st_size, stat.st_mtime_ns],
                             manual_edit=screenshot_edits.effective_edit(self.session, Path(entry['path'])),
                             reason='manually-included' if selected else 'manually-excluded')
            elif selected == automatic and entry['path'] not in self.touched and 'manual_selected' not in entry:
                entry.pop('manual_selected', None)
                if entry.get('reason') in {'manually-included', 'manually-excluded'}:
                    entry['reason'] = 'visual-change' if automatic else ('perceptual-duplicate' if any(region.get('perceptual_distance') is not None for region in entry.get('change_regions', [])) else 'near-duplicate' if any(region.get('near_duplicate') for region in entry.get('change_regions', [])) else ('cursor-only-change' if entry.get('change_regions') else 'identical'))
            else:
                entry['manual_selected'] = selected
                entry['reason'] = 'manually-included' if selected else 'manually-excluded'
        return self.frames

    def edit_image(self, path):
        if not self.frames or not self.allow_edits or (path is not None and not self.ready):
            return
        visible = self.display_frames or self.frames
        index = (int(max(0, self.canvas.canvasy(0))) // self.ROW_HEIGHT * 4
                 if self.page_size_var.get() == 'All' else self.page * self.page_size())
        sample = Path(path or visible[min(index, len(visible) - 1)]['path'])
        try:
            CropMaskEditor(self.dialog, self.session, sample, path is not None, self.edits_changed,
                           samples=[Path(frame['path']) for frame in self.frames] if path is None else None)
        except Exception as exc:
            messagebox.showerror('Could not open crop editor', str(exc), parent=self.dialog)

    def duplicate_mode_changed(self, _event=None):
        try:
            save_duplicate_mode(self.session, self.duplicate_mode_var.get())
        except OSError as exc:
            self.duplicate_mode_var.set(comparison_policy(self.session)['mode'])
            messagebox.showerror('Could not save duplicate mode', str(exc), parent=self.dialog)
            return
        self.edits_changed(policy_only=True)

    def edits_changed(self, policy_only=False):
        self.mask_summary_var.set(screenshot_edits.summary(self.session))
        if not policy_only:
            if self.ready:
                self.on_save(self.selected_frames())
            self.preview_cancelled.set()
            self.preview_cache.clear()
            for path, cell in self.cells.items():
                cell['photo'] = None
                self.thumbnail_buttons[path].configure(image='', text='Loading edited preview…')
            self.photos.clear()
            self.render_page(preserve_anchor=True)
            return
        self.start_reanalysis('Rechecking duplicate selection with the new comparison mode…')

    def start_reanalysis(self, message):
        self.cancelled.set()
        self.cancelled = threading.Event()
        self.analysis_generation += 1
        self.preview_cache.clear()
        for cell in self.cells.values():
            cell['photo'] = None
        for button in self.thumbnail_buttons.values():
            button.configure(image='', text='Loading edited preview…')
        self.photos.clear()
        self.ready = False
        self.generation += 1
        for button in self.choice_buttons + [self.save_button, self.global_crop_button]:
            button.configure(state='disabled')
        self.status_var.set(message)
        self.loading.configure(mode='indeterminate')
        self.loading.pack(fill='x', pady=(6, 0))
        self.loading.start(20)
        self.analysis_thread = threading.Thread(target=self.analyze, args=(self.session, self.events, self.cancelled, self.analysis_generation), daemon=True)
        self.analysis_thread.start()

    def save(self):
        try:
            self.on_save(self.selected_frames())
        except Exception as exc:
            messagebox.showerror('Could not save choices', str(exc), parent=self.dialog)
            return
        self.close()

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.cancelled.set()
        self.preview_cancelled.set()
        self.dialog.after_cancel(self.poll_id)
        if self.viewport_job is not None:
            self.dialog.after_cancel(self.viewport_job)
        self.dialog.review = None
        self.page_states.clear()
        self.photos.clear()
        self.preview_cache.clear()
        self.cells.clear()
        self.dialog.destroy()
