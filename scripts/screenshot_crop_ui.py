"""Draw normalized crop and blackout areas on an original screenshot."""
import copy
import tkinter as tk
from tkinter import ttk, messagebox
from pathlib import Path
from PIL import Image, ImageTk, ImageOps
import screenshot_edits
from window_placement import place_dialog_near_parent


class CropMaskEditor:
    def __init__(self, parent, session: Path, sample: Path, individual: bool, on_change, samples=None):
        self.session, self.sample = session, sample
        self.scope = sample if individual else None
        self.on_change = on_change
        self.samples = list(samples or [sample])
        if sample not in self.samples:
            self.samples.insert(0, sample)
        self.sample_index = self.samples.index(sample)
        settings = screenshot_edits.load_edits(session)
        self.edit = copy.deepcopy(screenshot_edits.effective_edit(session, sample, settings) if individual else settings.get('global') or {})
        with Image.open(sample) as source:
            self.original = source.convert('RGB')
        self.display = ImageOps.contain(self.original, (900, 430))
        self.dialog = tk.Toplevel(parent)
        self.dialog.title('Manage image masks — this screenshot' if individual else 'Manage image masks — global settings')
        self.dialog.transient(parent)
        place_dialog_near_parent(self.dialog, parent, 940, 740)
        self.dialog.minsize(940, 740)
        self.dialog.editor = self
        self.dialog.bind('<Escape>', lambda _event: self.dialog.destroy())
        frame = ttk.Frame(self.dialog, padding=12)
        frame.pack(fill='both', expand=True)
        title = ('This image overrides global settings: ' + sample.name) if individual else 'Applies to every screenshot, except images with their own override.'
        ttk.Label(frame, text=title).pack(anchor='w')
        if not individual:
            picker = ttk.Frame(frame)
            picker.pack(fill='x', pady=(6, 2))
            ttk.Label(picker, text='Draw using screenshot:').pack(side='left')
            ttk.Button(picker, text='Previous', command=lambda: self.select_sample(self.sample_index - 1)).pack(side='left', padx=(8, 2))
            self.sample_labels = [f'{index + 1} of {len(self.samples)}: {path.name}' for index, path in enumerate(self.samples)]
            self.sample_var = tk.StringVar(master=self.dialog, value=self.sample_labels[self.sample_index])
            self.sample_box = ttk.Combobox(picker, textvariable=self.sample_var, values=self.sample_labels,
                                           state='readonly', width=55)
            self.sample_box.pack(side='left', fill='x', expand=True)
            self.sample_box.bind('<<ComboboxSelected>>', lambda _event: self.select_sample(self.sample_box.current()))
            ttk.Button(picker, text='Next', command=lambda: self.select_sample(self.sample_index + 1)).pack(side='left', padx=(2, 0))
            ttk.Label(frame, text='Changing the preview keeps the crop/mask you are drawing; coordinates apply across the meeting.').pack(anchor='w')
        ttk.Label(frame, text='Drag a rectangle: Crop keeps that area; Blackout hides an area. Original files are preserved.').pack(anchor='w', pady=5)
        tools = ttk.Frame(frame)
        tools.pack(fill='x', pady=(0, 8))
        self.mode = tk.StringVar(master=self.dialog, value='crop')
        ttk.Radiobutton(tools, text='Crop: keep area', value='crop', variable=self.mode).pack(side='left')
        ttk.Radiobutton(tools, text='Blackout: hide area', value='mask', variable=self.mode).pack(side='left', padx=10)
        ttk.Button(tools, text='Middle third', command=self.middle_third).pack(side='left', padx=6)
        ttk.Button(tools, text='Reset crop', command=self.reset_crop).pack(side='left', padx=6)
        ttk.Button(tools, text='Undo blackout', command=self.undo_mask).pack(side='left', padx=6)
        ttk.Button(tools, text='Reset all', command=self.reset_all).pack(side='left', padx=6)
        self.canvas = tk.Canvas(frame, width=self.display.width, height=self.display.height, highlightthickness=1, cursor='crosshair')
        self.canvas.pack(anchor='center')
        self.photo = ImageTk.PhotoImage(self.display, master=self.dialog)
        self.canvas.bind('<ButtonPress-1>', self.press)
        self.canvas.bind('<B1-Motion>', self.drag)
        self.canvas.bind('<ButtonRelease-1>', self.release)
        self.start = None
        self.detail = tk.StringVar(master=self.dialog)
        ttk.Label(frame, textvariable=self.detail).pack(anchor='w', pady=8)
        ttk.Label(frame, text='Coordinates scale with each image. Crops reduce OCR input dimensions; blackouts hide content without resizing.').pack(anchor='w')
        buttons = ttk.Frame(frame)
        buttons.pack(fill='x', side='bottom', pady=8)
        if individual:
            ttk.Button(buttons, text='Use global settings', command=self.use_global).pack(side='left')
            ttk.Button(buttons, text='Apply this crop / mask to all screenshots', command=self.save_global).pack(side='left', padx=8)
        ttk.Button(buttons, text='Show OCR preview', command=self.preview).pack(side='left', padx=8)
        ttk.Button(buttons, text='Save crop / masks', command=self.save).pack(side='right')
        ttk.Button(buttons, text='Cancel', command=self.dialog.destroy).pack(side='right', padx=8)
        self.redraw()

    def select_sample(self, index):
        if not 0 <= index < len(self.samples):
            return
        path = self.samples[index]
        with Image.open(path) as source:
            original = source.convert('RGB')
        self.sample_index = index
        self.sample = path
        self.original = original
        self.display = ImageOps.contain(original, (900, 430))
        self.photo = ImageTk.PhotoImage(self.display, master=self.dialog)
        self.canvas.configure(width=self.display.width, height=self.display.height)
        if self.scope is None:
            self.sample_var.set(self.sample_labels[index])
        self.redraw()

    def middle_third(self):
        self.edit['crop'] = [1/3, 0, 2/3, 1]
        self.redraw()

    def reset_crop(self):
        self.edit.pop('crop', None)
        self.redraw()

    def reset_all(self):
        self.edit = {}
        self.redraw()

    def undo_mask(self):
        masks = self.edit.get('masks') or []
        if masks:
            masks.pop()
        self.redraw()

    def coordinates(self, event):
        return max(0, min(1, event.x / self.display.width)), max(0, min(1, event.y / self.display.height))

    def press(self, event):
        self.start = self.coordinates(event)

    def drag(self, event):
        if self.start is None:
            return
        self.redraw()
        x, y = self.coordinates(event)
        self.canvas.create_rectangle(self.start[0] * self.display.width, self.start[1] * self.display.height,
                                     x * self.display.width, y * self.display.height, outline='cyan', width=2, tags='drag')

    def release(self, event):
        if self.start is None:
            return
        x, y = self.coordinates(event)
        rect = [min(x, self.start[0]), min(y, self.start[1]), max(x, self.start[0]), max(y, self.start[1])]
        self.start = None
        if (rect[2] - rect[0]) * self.display.width >= 3 and (rect[3] - rect[1]) * self.display.height >= 3:
            if self.mode.get() == 'crop':
                self.edit['crop'] = rect
            else:
                self.edit.setdefault('masks', []).append(rect)
        self.redraw()

    def redraw(self):
        canvas, width, height = self.canvas, self.display.width, self.display.height
        canvas.delete('all')
        canvas.create_image(0, 0, image=self.photo, anchor='nw')
        for mask in self.edit.get('masks') or []:
            box = [mask[0]*width, mask[1]*height, mask[2]*width, mask[3]*height]
            canvas.create_rectangle(*box, fill='black', outline='orange')
        crop = self.edit.get('crop')
        if crop:
            left, top, right, bottom = [crop[0]*width, crop[1]*height, crop[2]*width, crop[3]*height]
            for box in [(0, 0, width, top), (0, bottom, width, height), (0, top, left, bottom), (right, top, width, bottom)]:
                canvas.create_rectangle(*box, fill='black', stipple='gray50', outline='')
            canvas.create_rectangle(left, top, right, bottom, outline='#00ff66', width=3)
        pixel_box = screenshot_edits.rect_pixels(crop or [0, 0, 1, 1], self.original.size)
        out_width, out_height = pixel_box[2]-pixel_box[0], pixel_box[3]-pixel_box[1]
        area = out_width*out_height / (self.original.width*self.original.height)
        self.detail.set(f'OCR image: {out_width} × {out_height} pixels · {area:.0%} of original area · {len(self.edit.get("masks") or [])} blackout(s)')

    def preview(self):
        image = screenshot_edits.apply_edit(self.original, self.edit)
        image.thumbnail((900, 500))
        window = tk.Toplevel(self.dialog)
        window.title('OCR input preview')
        window.photo = ImageTk.PhotoImage(image, master=window)
        ttk.Label(window, image=window.photo).pack(padx=10, pady=10)

    def save(self):
        try:
            screenshot_edits.save_edit(self.session, self.scope, self.edit)
            self.on_change()
        except Exception as exc:
            messagebox.showerror('Could not save crop', str(exc), parent=self.dialog)
            return
        self.dialog.destroy()

    def save_global(self):
        try:
            screenshot_edits.promote_edit(self.session, self.sample, self.edit)
            self.on_change()
        except Exception as exc:
            messagebox.showerror('Could not save global crop', str(exc), parent=self.dialog)
            return
        self.dialog.destroy()

    def use_global(self):
        screenshot_edits.save_edit(self.session, self.sample, None)
        self.on_change()
        self.dialog.destroy()
