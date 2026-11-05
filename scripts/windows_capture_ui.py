#!/usr/bin/env python3
"""Tiny Windows desktop UI for all-device meeting capture."""

from __future__ import annotations

import argparse
import ctypes
import json
import math
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
from datetime import datetime
from pathlib import Path
from tkinter import BOTH, DISABLED, END, LEFT, NORMAL, RIGHT, W, X, filedialog, messagebox
import tkinter as tk
from tkinter import ttk
from PIL import ImageTk

import windows_capture as capture
import windows_ocr as visual_ocr
import screenshot_edits
import screenshot_window_context
import recover_session
import window_picker
from live_audio_segments import segment_audio
from live_transcription import LiveTranscriptionCoordinator
from moonshine_live import MoonshineLive
from window_placement import place_dialog_near_parent
from screenshot_review import ScreenshotReview


OPENROUTER_CREDITS_URL = "https://openrouter.ai/api/v1/credits"
CLOUD_CHOICE = "Cloud (OpenRouter)"


def fetch_openrouter_balance(key: str, timeout: float = 15) -> float:
    """Return remaining OpenRouter account credits in USD (purchased minus used)."""
    request = urllib.request.Request(OPENROUTER_CREDITS_URL, headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.loads(response.read().decode("utf-8")).get("data") or {}
    return float(data["total_credits"]) - float(data["total_usage"])


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def default_recordings_dir() -> Path:
    return project_root() / "recordings" / "desktop"


def timestamp_dir(base_dir: Path) -> Path:
    return base_dir / datetime.now().strftime("%Y%m%d-%H%M%S")


def basename(path: str | None) -> str:
    return Path((path or "").replace("\\", "/")).name


def preferred_live_tracks(tracks: list[dict], default_device_ids: dict[str, str]) -> dict[str, str]:
    """Map OS default device IDs to the matching opened capture track IDs."""
    preferred = {}
    physical_mics = [
        track for track in tracks
        if track.get("kind") == "microphone"
        and "stereo mix" not in str((track.get("device") or {}).get("name", "")).lower()
    ]
    for track in tracks:
        kind = track.get("kind")
        device_id = (track.get("device") or {}).get("id")
        if device_id and device_id == default_device_ids.get(kind):
            if kind == "microphone" and physical_mics and track not in physical_mics:
                continue
            preferred[kind] = track["track_id"]
    if "microphone" not in preferred and physical_mics:
        preferred["microphone"] = physical_mics[0]["track_id"]
    return preferred


def bind_vertical_mousewheel(container: tk.Misc, canvas: tk.Canvas):
    """Scroll a canvas from anywhere inside its containing dialog on Windows."""
    def scroll(event: tk.Event) -> str:
        delta = int(getattr(event, "delta", 0))
        if delta:
            canvas.yview_scroll(-1 if delta > 0 else 1, "units")
        return "break"

    container.bind("<MouseWheel>", scroll)
    return scroll


def acquire_single_instance() -> tuple[int | None, bool]:
    """Use a named Windows mutex so one meeting cannot be processed by duplicate app windows."""
    if os.name != "nt":
        return None, False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel32.CreateMutexW(None, False, "Local\\MeetingNotesCapture.App")
    already_exists = ctypes.get_last_error() == 183
    if already_exists and handle:
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        return None, True
    return int(handle) if handle else None, False


def release_single_instance(handle: int | None) -> None:
    if handle and os.name == "nt":
        ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(handle))


def fmt_session_time(path: Path | None) -> str:
    if not path:
        return ""
    metadata_path = path / "metadata.json"
    if metadata_path.exists():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            value = metadata.get("started_at")
            if value:
                return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone().strftime("%b %d %I:%M %p")
        except Exception:
            pass
    try:
        return datetime.strptime(path.name, "%Y%m%d-%H%M%S").strftime("%b %d %I:%M %p")
    except ValueError:
        return path.name


def latest_session(base_dir: Path) -> Path | None:
    if not base_dir.exists():
        return None
    candidates = [p for p in base_dir.iterdir() if p.is_dir() and (p / "metadata.json").exists()]
    return max(candidates, key=lambda p: p.stat().st_mtime) if candidates else None


def session_dirs(base_dir: Path) -> list[Path]:
    if not base_dir.exists():
        return []
    return sorted(
        [p for p in base_dir.iterdir() if p.is_dir() and (p / "metadata.json").exists()],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )


def windows_screenshot_dirs() -> list[Path]:
    roots = []
    user = os.environ.get("USERPROFILE")
    one_drive = os.environ.get("OneDrive") or os.environ.get("OneDriveConsumer")
    for base in [one_drive, user]:
        if base:
            roots.append(Path(base) / "Pictures" / "Screenshots")
    return [p for p in roots if p.exists()]


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def ocr_result_current(session: Path, result: dict, edits: dict) -> bool:
    try:
        return result.get('image_fingerprint') == screenshot_edits.fingerprint(session, Path(result['path']), edits)
    except (OSError, KeyError):
        return False


def recorded_cloud_costs(session: Path) -> dict[str, object]:
    """Summarize only costs retained in this meeting's API responses."""
    stages: dict[str, dict[str, float | int]] = {
        name: {"known": 0.0, "unknown": 0, "requests": 0}
        for name in ("ocr", "transcript", "notes")
    }

    def add(stage: str, value: object) -> None:
        entry = stages[stage]
        entry["requests"] += 1
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            entry["unknown"] += 1
        else:
            entry["known"] += float(value)

    for result in read_json(session / "visual" / "ocr.json").get("results", []):
        if isinstance(result, dict) and result.get("backend") == "openrouter":
            add("ocr", result.get("cost_usd"))
    chunk_root = session / "transcripts" / "cloud_chunks"
    if chunk_root.exists():
        for path in chunk_root.rglob("*.json"):
            add("transcript", (read_json(path).get("usage") or {}).get("cost"))
    notes_response = session / "transcripts" / "notes_openrouter_response.json"
    if notes_response.exists():
        add("notes", (read_json(notes_response).get("usage") or {}).get("cost"))

    def label(entry: dict[str, float | int]) -> str:
        amount = float(entry["known"])
        missing = int(entry["unknown"])
        if missing and amount:
            return f"${amount:.6f} + {missing} unknown"
        if missing:
            return f"Unknown ({missing})"
        if entry["requests"]:
            return f"${amount:.6f}"
        return "—"

    labels = {name: label(entry) for name, entry in stages.items()}
    known_total = sum(float(entry["known"]) for entry in stages.values())
    unknown_total = sum(int(entry["unknown"]) for entry in stages.values())
    if unknown_total:
        summary = f"Recorded cloud cost: at least ${known_total:.6f} · {unknown_total} unmetered request(s)"
    elif any(entry["requests"] for entry in stages.values()):
        summary = f"Recorded cloud cost: ${known_total:.6f}"
    else:
        summary = "No cloud cost recorded"
    return {"labels": labels, "summary": summary}


def visible_windows() -> list[dict[str, object]]:
    if os.name != "nt":
        return []
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        result: list[dict[str, object]] = []

        enum_proc_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def callback(hwnd: int, _lparam: int) -> bool:
            if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            title = buffer.value.strip()
            if not title:
                return True
            rect = wintypes.RECT()
            if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                return True
            width = rect.right - rect.left
            height = rect.bottom - rect.top
            if width < 160 or height < 120:
                return True
            result.append({"hwnd": int(hwnd), "title": title, "size": f"{width}x{height}"})
            return True

        user32.EnumWindows(enum_proc_type(callback), 0)
        return sorted(result, key=lambda item: str(item["title"]).lower())
    except Exception:
        return []


def render_has_content(image: object) -> bool:
    """False for the solid black/blank frames some apps return from PrintWindow."""
    from PIL import ImageStat
    sample = image.convert("L").resize((64, 64))
    return max(ImageStat.Stat(sample).stddev) > 1.0


def print_window_image(hwnd: int | None, bbox: tuple[int, int, int, int]) -> object | None:
    """Render a window's own contents, unaffected by windows layered over it.

    Uses PrintWindow with PW_RENDERFULLCONTENT (works for Chromium/Brave and
    other DWM-composed apps). Returns an image cropped to ``bbox`` (the DWM
    visible frame, in physical pixels) or None if rendering is unavailable.
    """
    if os.name != "nt" or not hwnd:
        return None
    try:
        from ctypes import wintypes
        from PIL import Image

        user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
        user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        user32.GetWindowDC.argtypes = [wintypes.HWND]
        user32.GetWindowDC.restype = wintypes.HDC
        user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
        user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
        gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
        gdi32.CreateCompatibleDC.restype = wintypes.HDC
        gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
        gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
        gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
        gdi32.SelectObject.restype = wintypes.HGDIOBJ
        gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
        gdi32.DeleteDC.argtypes = [wintypes.HDC]
        gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
                                    ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                        ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD),
                        ("biCompression", wintypes.DWORD), ("biSizeImage", wintypes.DWORD),
                        ("biXPelsPerMeter", wintypes.LONG), ("biYPelsPerMeter", wintypes.LONG),
                        ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]

        previous_context = None
        set_context = getattr(user32, "SetThreadDpiAwarenessContext", None)
        if set_context:
            set_context.argtypes = [ctypes.c_void_p]
            set_context.restype = ctypes.c_void_p
            previous_context = set_context(ctypes.c_void_p(-4))  # per-monitor v2: physical pixels
        try:
            rect = wintypes.RECT()
            if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                return None
            width, height = rect.right - rect.left, rect.bottom - rect.top
            if width <= 0 or height <= 0:
                return None
            window_dc = user32.GetWindowDC(hwnd)
            memory_dc = gdi32.CreateCompatibleDC(window_dc)
            bitmap = gdi32.CreateCompatibleBitmap(window_dc, width, height)
            old = gdi32.SelectObject(memory_dc, bitmap)
            try:
                if not user32.PrintWindow(hwnd, memory_dc, 2):  # PW_RENDERFULLCONTENT
                    return None
                header = BITMAPINFOHEADER()
                header.biSize = ctypes.sizeof(BITMAPINFOHEADER)
                header.biWidth, header.biHeight = width, -height
                header.biPlanes, header.biBitCount = 1, 32
                buffer = ctypes.create_string_buffer(width * height * 4)
                if not gdi32.GetDIBits(memory_dc, bitmap, 0, height, buffer, ctypes.byref(header), 0):
                    return None
            finally:
                gdi32.SelectObject(memory_dc, old)
                gdi32.DeleteObject(bitmap)
                gdi32.DeleteDC(memory_dc)
                user32.ReleaseDC(hwnd, window_dc)
        finally:
            if previous_context:
                set_context(previous_context)
        image = Image.frombuffer("RGB", (width, height), buffer, "raw", "BGRX", 0, 1)
        # Trim the invisible resize frame: crop to the DWM visible bounds.
        crop = (max(0, bbox[0] - rect.left), max(0, bbox[1] - rect.top),
                min(width, bbox[2] - rect.left), min(height, bbox[3] - rect.top))
        if crop[2] > crop[0] and crop[3] > crop[1]:
            image = image.crop(crop)
        return image if render_has_content(image) else None
    except Exception:
        return None


class CaptureApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Meeting Notes Capture")
        self.root.geometry("1120x820")
        self.root.minsize(1000, 760)
        self.events: queue.Queue = queue.Queue()
        self.process_events: queue.Queue = queue.Queue()
        self.worker: threading.Thread | None = None
        self.process_worker: threading.Thread | None = None
        self.processing_process: subprocess.Popen[str] | None = None
        self.processing_cancelled = False
        self.processing_running = False
        self.processing_session: Path | None = None
        self.processing_started_at: float | None = None
        self.processing_started_wall_at: datetime | None = None
        self.processing_elapsed_seconds = 0
        self.processing_estimate_percent = 0.0
        self.active_progress_payload: dict = {}
        self.progress_display_key = None
        self.ocr_preview_key = None
        self.ocr_preview_photo = None
        self.ocr_preview_path = None
        self.openrouter_key: str | None = None
        self.recording = False
        self.capture_stopping = False
        self.started_at: float | None = None
        self.current_out_dir: Path | None = None
        self.metadata: dict | None = None
        self.close_after_stop = False
        self.device_rows: dict[tuple[str, str], str] = {}
        self.history_rows: dict[str, Path] = {}
        self.selected_session: Path | None = None
        self.screenshot_stop = threading.Event()
        self.screenshot_worker: threading.Thread | None = None
        self.screenshot_capture_index = 0
        self.screenshot_save_lock = threading.Lock()
        self.capture_indicator_reset: str | None = None
        self.capture_preview_key: str | None = None
        self.capture_preview_path: Path | None = None
        self.capture_preview_photo: ImageTk.PhotoImage | None = None
        self.screenshot_source_snapshot: dict[Path, float] = {}
        self.selected_window_hwnd: int | None = None
        self.selected_window_title: str = ""
        self.track_rows: dict[str, str] = {}
        self.live_coordinator = None
        self.live_incomplete = False
        self.live_enabled_for_recording = False
        self.live_generation = 0
        self.live_control_event = threading.Event()
        self.live_control_stop = threading.Event()
        self.live_control_thread: threading.Thread | None = None

        self.out_dir_var = tk.StringVar(value=str(default_recordings_dir()))
        self.status_var = tk.StringVar(value="Idle")
        self.elapsed_var = tk.StringVar(value="00:00:00")
        self.summary_var = tk.StringVar(value="Ready")
        self.latest_var = tk.StringVar(value="")
        self.selected_meeting_var = tk.StringVar(value="No meeting selected")
        self.process_var = tk.StringVar(value="No meeting processed in this session")
        self.process_stage_var = tk.StringVar(value="Ready to process")
        self.process_progress_var = tk.DoubleVar(value=0)
        self.processing_timer_var = tk.StringVar(value="Elapsed 00:00:00  ·  Est. total ~00:00:00  ·  Remaining ~00:00:00")
        self.processing_detail_var = tk.StringVar(value="Select a meeting to inspect processing status.")
        self.include_screenshot_ocr_var = tk.BooleanVar(value=True)
        self.ocr_backend_var = tk.StringVar(value=CLOUD_CHOICE)
        self.ocr_backend_hint_var = tk.StringVar(value="")
        self.transcription_backend_var = tk.StringVar(value=CLOUD_CHOICE)
        self.openrouter_balance_var = tk.StringVar(value="OpenRouter balance: checking...")
        self.balance_thread: threading.Thread | None = None
        self.balance_result: tuple[float | None, str | None] | None = None
        self.capture_screen_var = tk.BooleanVar(value=True)
        self.screenshot_interval_var = tk.IntVar(value=15)
        self.screenshot_interval_seconds = 15
        self.screenshot_interval_var.trace_add("write", self.on_screenshot_interval_change)
        self.screen_scope_var = tk.StringVar(value="Active window")
        self.selected_window_var = tk.StringVar(value="No meeting window selected")
        self.title_var = tk.StringVar(value="")
        self.live_status_var = tk.StringVar(value="Live captions stopped")
        self.live_enabled_var = tk.BooleanVar(value=False)
        self.live_source_var = tk.StringVar(value="No audio source selected yet")
        self.live_sources: dict[str, str] = {}
        self.live_previews: dict[str, str] = {}
        self.live_preview_var = tk.StringVar(value="")
        self.live_review_status_var = tk.StringVar(value="Select a meeting to review saved live captions. These are not the final transcript.")

        self._build()
        self.refresh_devices()
        self.refresh_history()
        self.root.after(250, self.poll_events)
        self.root.after(500, self.poll_process_events)
        self.root.after(1000, self.tick)
        self.schedule_startup_balance_check()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        # pythonw has no console: without this, Tk callback errors vanish.
        self.root.report_callback_exception = self.log_callback_exception

    def configure_tab_style(self) -> str:
        """Bold, filled tabs so section tabs stand apart from surrounding text.

        The native Windows theme ignores tab colours, so the tab element is
        borrowed from ``clam`` for this notebook only.
        """
        style = ttk.Style(self.root)
        try:
            style.element_create("Main.tab", "from", "clam")
        except tk.TclError:
            pass  # already created for this interpreter
        style.layout("Main.TNotebook.Tab", [("Main.tab", {"sticky": "nswe", "children": [
            ("Notebook.padding", {"side": "top", "sticky": "nswe", "children": [
                ("Notebook.label", {"side": "top", "sticky": ""})]})]})])
        style.configure("Main.TNotebook", tabmargins=(2, 6, 2, 0))
        style.configure("Main.TNotebook.Tab", font=("Segoe UI", 10, "bold"), padding=(18, 7),
                        background="#e2e8f0", foreground="#334155",
                        bordercolor="#94a3b8", lightcolor="#e2e8f0", darkcolor="#cbd5e1")
        style.map("Main.TNotebook.Tab",
                  background=[("selected", "#2563eb"), ("active", "#bfdbfe")],
                  lightcolor=[("selected", "#2563eb"), ("active", "#bfdbfe")],
                  foreground=[("selected", "#ffffff")],
                  expand=[("selected", (1, 2, 1, 0))])
        return "Main.TNotebook"

    def _build(self) -> None:
        top = ttk.Frame(self.root, padding=12)
        top.pack(fill=X)

        ttk.Label(top, text="Recordings").pack(side=LEFT)
        out_entry = ttk.Entry(top, textvariable=self.out_dir_var)
        out_entry.pack(side=LEFT, fill=X, expand=True, padx=8)
        ttk.Button(top, text="Browse", command=self.choose_dir).pack(side=LEFT)

        controls = ttk.Frame(self.root, padding=(12, 0, 12, 8))
        controls.pack(fill=X)
        self.start_button = ttk.Button(controls, text="Start Recording", command=self.start)
        self.start_button.pack(side=LEFT)
        self.stop_button = ttk.Button(controls, text="Stop", command=self.stop, state=DISABLED)
        self.stop_button.pack(side=LEFT, padx=8)
        ttk.Button(controls, text="Refresh Devices", command=self.refresh_devices).pack(side=LEFT)
        ttk.Button(controls, text="Open Latest Folder", command=self.open_latest_folder).pack(side=LEFT, padx=8)
        self.capture_screen_button = ttk.Checkbutton(
            controls, text="Capture Screen Context", variable=self.capture_screen_var,
            command=self.on_capture_screen_toggle,
        )
        self.capture_screen_button.pack(side=LEFT, padx=(12, 4))
        ttk.Label(controls, text="Every").pack(side=LEFT)
        ttk.Spinbox(controls, from_=5, to=120, increment=5, width=4, textvariable=self.screenshot_interval_var).pack(side=LEFT, padx=4)
        ttk.Label(controls, text="sec").pack(side=LEFT)
        self.screen_scope = ttk.Combobox(
            controls,
            textvariable=self.screen_scope_var,
            values=("Full desktop", "Active window", "Selected window"),
            width=30,
            state="readonly",
        )
        self.screen_scope.pack(side=LEFT, padx=(8, 0))
        ttk.Button(controls, text="Choose Window", command=self.choose_capture_window).pack(side=LEFT, padx=(8, 0))
        ttk.Label(controls, text="A selected browser window records whichever tab is visible.").pack(side=LEFT, padx=(10, 0))

        screenshot_actions = ttk.Frame(self.root, padding=(12, 0, 12, 4))
        screenshot_actions.pack(fill=X)
        self.manual_screenshot_button = ttk.Button(
            screenshot_actions, text="Take screenshot", command=self.take_manual_screenshot, state=DISABLED,
        )
        self.manual_screenshot_button.pack(side=LEFT)
        self.capture_indicator = tk.Label(screenshot_actions, text="● Screenshot", foreground="#6b7280")
        self.capture_indicator.pack(side=LEFT, padx=(12, 0))
        preview_frame = ttk.Frame(screenshot_actions, width=124, height=76)
        preview_frame.pack(side=LEFT, padx=(8, 0))
        preview_frame.pack_propagate(False)
        self.capture_preview_label = ttk.Label(preview_frame, text="No screenshot yet", anchor="center")
        self.capture_preview_label.pack(fill=BOTH, expand=True)
        self.capture_status_var = tk.StringVar(value="")
        ttk.Label(screenshot_actions, textvariable=self.capture_status_var,
                  wraplength=700, justify=LEFT).pack(side=LEFT, padx=(8, 0), fill=X, expand=True)

        status = ttk.Frame(self.root, padding=(12, 0, 12, 8))
        status.pack(fill=X)
        ttk.Label(status, textvariable=self.status_var).pack(side=LEFT)
        ttk.Label(status, textvariable=self.elapsed_var).pack(side=RIGHT)

        context = ttk.Frame(self.root, padding=(12, 0, 12, 8))
        context.pack(fill=X)
        latest = ttk.Frame(context)
        latest.pack(fill=X)
        ttk.Label(latest, text="Latest captured meeting:").pack(side=LEFT)
        ttk.Label(latest, textvariable=self.latest_var).pack(side=LEFT, padx=8)
        ttk.Label(latest, text="Screen source:").pack(side=LEFT, padx=(16, 0))
        ttk.Label(latest, textvariable=self.selected_window_var).pack(side=LEFT, padx=8)
        selected = ttk.Frame(context)
        selected.pack(fill=X, pady=(3, 0))
        ttk.Label(selected, text="Currently selected meeting:").pack(side=LEFT)
        ttk.Label(selected, textvariable=self.selected_meeting_var).pack(side=LEFT, padx=8)
        self.edit_title_button = ttk.Button(selected, text="Edit title", command=self.edit_selected_title, state=DISABLED)
        self.edit_title_button.pack(side=RIGHT)

        self.notebook = ttk.Notebook(self.root, style=self.configure_tab_style())
        self.notebook.pack(fill=BOTH, expand=True, padx=12, pady=(0, 8))

        devices_frame = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(devices_frame, text="Devices")
        self.devices = ttk.Treeview(
            devices_frame,
            columns=("kind", "role", "name", "recording", "file", "level", "status", "latest"),
            show="headings",
            height=14,
        )
        for col, label, width in [
            ("kind", "Kind", 110),
            ("role", "Role", 130),
            ("name", "Device", 330),
            ("recording", "Recording Now?", 120),
            ("file", "Recording To File", 210),
            ("level", "Live level", 95),
            ("status", "Status", 130),
            ("latest", "Latest Capture", 130),
        ]:
            self.devices.heading(col, text=label)
            self.devices.column(col, width=width, anchor=W)
        self.devices.pack(fill=BOTH, expand=True)

        meetings_frame = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(meetings_frame, text="Meetings")
        meetings_top = ttk.Frame(meetings_frame)
        meetings_top.pack(fill=X, pady=(0, 8))
        ttk.Button(meetings_top, text="Refresh", command=self.refresh_history).pack(side=LEFT)
        ttk.Button(meetings_top, text="Open Folder", command=self.open_selected_folder).pack(side=LEFT, padx=8)
        ttk.Button(meetings_top, text="Open Diagnostics", command=self.open_diagnostics).pack(side=LEFT)
        self.history = ttk.Treeview(
            meetings_frame,
            columns=("time", "title", "duration", "processed", "screenshots"),
            show="headings",
            height=10,
        )
        for col, label, width in [
            ("time", "Captured", 160),
            ("title", "Title", 340),
            ("duration", "Duration", 110),
            ("processed", "Processed", 130),
            ("screenshots", "Screenshot OCR", 220),
        ]:
            self.history.heading(col, text=label)
            self.history.column(col, width=width, anchor=W)
        self.history.pack(fill=BOTH, expand=True)
        self.history.bind("<<TreeviewSelect>>", self.on_history_select)

        notes_frame = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(notes_frame, text="Notes")
        title_row = ttk.Frame(notes_frame)
        title_row.pack(fill=X, pady=(0, 8))
        ttk.Label(title_row, text="Title").pack(side=LEFT)
        ttk.Entry(title_row, textvariable=self.title_var).pack(side=LEFT, fill=X, expand=True, padx=8)
        ttk.Button(title_row, text="Save Title", command=self.save_title).pack(side=LEFT)
        ttk.Button(title_row, text="Save Notes", command=self.save_notes).pack(side=LEFT, padx=8)
        ttk.Button(title_row, text="Copy Notes", command=self.copy_notes).pack(side=LEFT)
        self.notes_text = tk.Text(notes_frame, wrap="word", height=14, undo=True)
        self.notes_text.pack(fill=BOTH, expand=True)

        review_frame = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(review_frame, text="Review")
        track_top = ttk.Frame(review_frame)
        track_top.pack(fill=X, pady=(0, 6))
        ttk.Label(track_top, text="Audio tracks — the automatic choices are based on measured activity. Change them before processing if needed.").pack(side=LEFT)
        ttk.Button(track_top, text="Analyze Tracks", command=self.analyze_tracks).pack(side=RIGHT)
        self.track_review = ttk.Treeview(
            review_frame,
            columns=("include", "kind", "device", "activity", "peak"),
            show="headings",
            height=5,
        )
        for col, label, width in [
            ("include", "Process?", 75), ("kind", "Kind", 100), ("device", "Device", 400),
            ("activity", "Active", 75), ("peak", "Peak", 90),
        ]:
            self.track_review.heading(col, text=label)
            self.track_review.column(col, width=width, anchor=W)
        self.track_review.pack(fill=X)
        track_actions = ttk.Frame(review_frame)
        track_actions.pack(fill=X, pady=(5, 10))
        ttk.Button(track_actions, text="Include / Exclude Selected", command=self.toggle_selected_track).pack(side=LEFT)
        ttk.Button(track_actions, text="Use Automatic Track Selection", command=self.use_automatic_tracks).pack(side=LEFT, padx=8)

        screenshot_top = ttk.LabelFrame(review_frame, text="Screenshots", padding=8)
        screenshot_top.pack(fill=X)
        self.screenshot_review_var = tk.StringVar(value="Select a meeting to review its screen context.")
        ttk.Label(screenshot_top, textvariable=self.screenshot_review_var).pack(side=LEFT, fill=X, expand=True)
        ttk.Button(screenshot_top, text="Review Screenshots", command=self.review_screenshots).pack(side=RIGHT)
        self.open_timeline_button = ttk.Button(screenshot_top, text="Open Visual Timeline", command=lambda: self.open_selected_file("visual/timeline.md"), state=DISABLED)
        self.open_timeline_button.pack(side=RIGHT, padx=8)

        live_review_frame = ttk.LabelFrame(review_frame, text="Saved live captions (not final transcript)", padding=6)
        live_review_frame.pack(fill=BOTH, expand=True, pady=(8, 0))
        ttk.Label(live_review_frame, textvariable=self.live_review_status_var).pack(fill=X)
        self.live_review_text = tk.Text(live_review_frame, wrap="word", height=5, state=DISABLED)
        self.live_review_text.pack(fill=BOTH, expand=True)

        live_frame = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(live_frame, text="Live Transcript")
        self.live_enabled_checkbox = ttk.Checkbutton(
            live_frame, text="Live captions (uses CPU)", variable=self.live_enabled_var,
            command=self.on_live_caption_toggle)
        self.live_enabled_checkbox.pack(fill=X)
        ttk.Label(live_frame, text="Turn on or off anytime. Outside a meeting, this sets the next recording. WAV capture and final transcription work either way.").pack(fill=X)
        ttk.Label(live_frame, textvariable=self.live_status_var).pack(fill=X, pady=(6, 4))
        ttk.Label(live_frame, textvariable=self.live_source_var).pack(fill=X, pady=(0, 4))
        self.live_transcript_text = tk.Text(live_frame, wrap="word", state=DISABLED)
        self.live_transcript_text.pack(fill=BOTH, expand=True)
        ttk.Label(live_frame, textvariable=self.live_preview_var, wraplength=850, justify=LEFT).pack(fill=X, pady=(6, 0))

        processing_frame = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(processing_frame, text="Processing")
        processing_top = ttk.Frame(processing_frame)
        processing_top.pack(fill=X, pady=(0, 6))
        detail_label = ttk.Label(processing_frame, textvariable=self.processing_detail_var)
        detail_label.pack(fill=X, before=processing_top, pady=(0, 6))
        detail_label.bind("<Configure>", lambda event: detail_label.configure(wraplength=max(100, event.width - 8)))
        provider_row = ttk.Frame(processing_frame)
        provider_row.pack(fill=X, before=processing_top, pady=(0, 6))
        ttk.Label(provider_row, text="Screenshot OCR:").pack(side=LEFT)
        self.ocr_backend_choice = ttk.Combobox(provider_row, textvariable=self.ocr_backend_var,
                                                values=("Local", "Cloud (OpenRouter)"), state="readonly", width=20)
        self.ocr_backend_choice.pack(side=LEFT, padx=(5, 16))
        self.ocr_backend_hint_label = ttk.Label(processing_frame, textvariable=self.ocr_backend_hint_var)
        ttk.Label(provider_row, text="Audio transcription:").pack(side=LEFT)
        self.transcription_backend_choice = ttk.Combobox(provider_row, textvariable=self.transcription_backend_var,
                                                          values=("Local", "Cloud (OpenRouter)"), state="readonly", width=20)
        self.transcription_backend_choice.pack(side=LEFT, padx=5)
        self.refresh_balance_button = ttk.Button(provider_row, text="Refresh", command=self.refresh_openrouter_balance)
        self.refresh_balance_button.pack(side=RIGHT)
        ttk.Label(provider_row, textvariable=self.openrouter_balance_var).pack(side=RIGHT, padx=(16, 5))
        for choice in (self.ocr_backend_choice, self.transcription_backend_choice):
            choice.bind("<<ComboboxSelected>>", self.save_processing_backends)
        self.cloud_provider_hint_label = ttk.Label(processing_frame, text="Cloud sends selected screenshots/audio to OpenRouter. Change either choice, then Resume to retry unfinished work.")
        self.cloud_provider_hint_label.pack(fill=X, before=processing_top, pady=(0, 5))
        ttk.Button(processing_top, text="Refresh status", command=self.refresh_processing_details).pack(side=RIGHT)
        self.retry_failed_ocr_button = ttk.Button(
            processing_top,
            text="Retry failed OCR + update notes",
            command=self.retry_failed_ocr,
            state=DISABLED,
        )
        self.retry_failed_ocr_button.pack(side=RIGHT, padx=8)

        self.stage_status = ttk.Treeview(
            processing_frame,
            columns=("stage", "status", "cost", "detail"),
            show="headings",
            height=5,
        )
        for col, label, width in [
            ("stage", "Stage", 160), ("status", "Status", 100),
            ("cost", "Cloud cost", 185), ("detail", "Detail", 510),
        ]:
            self.stage_status.heading(col, text=label)
            self.stage_status.column(col, width=width, anchor=W)
        self.stage_status.pack(fill=X)

        item_panes = ttk.Frame(processing_frame)
        item_panes.pack(fill=BOTH, expand=True, pady=(8, 0))
        ocr_frame = ttk.LabelFrame(item_panes, text="Screenshot OCR", padding=5)
        audio_frame = ttk.LabelFrame(processing_frame, text="Audio transcription", padding=5)
        audio_frame.pack(fill=X, pady=(8, 0), before=item_panes)
        ocr_frame.pack(fill=BOTH, expand=True)
        self.ocr_preview_frame = ttk.Frame(ocr_frame)
        self.ocr_preview_var = tk.StringVar(value='')
        ttk.Label(self.ocr_preview_frame, textvariable=self.ocr_preview_var, wraplength=155).pack()
        self.ocr_preview_button = ttk.Button(self.ocr_preview_frame, text='Loading preview…', command=self.open_ocr_preview)
        self.ocr_preview_button.pack(pady=4)

        ocr_scroll = ttk.Scrollbar(ocr_frame, orient="vertical")
        self.ocr_status = ttk.Treeview(
            ocr_frame,
            columns=("image", "status", "attempts", "error", "original"),
            displaycolumns=("image", "status", "attempts", "error"),
            show="headings",
            height=4,
            yscrollcommand=ocr_scroll.set,
        )
        ocr_scroll.configure(command=self.ocr_status.yview)
        self.ocr_status.heading('original', text='Original capture')
        for col, label, width in [
            ("image", "Image", 250), ("status", "Status", 90),
            ("attempts", "Attempts", 70), ("error", "Last error", 360),
        ]:
            self.ocr_status.heading(col, text=label)
            self.ocr_status.column(col, width=width, anchor=W)
        self.ocr_status.pack(side=LEFT, fill=BOTH, expand=True)
        ocr_scroll.pack(side=RIGHT, fill="y")

        audio_scroll = ttk.Scrollbar(audio_frame, orient="vertical")
        self.audio_status = ttk.Treeview(
            audio_frame,
            columns=("track", "kind", "status", "progress", "detail"),
            show="headings",
            height=2,
            yscrollcommand=audio_scroll.set,
        )
        for col, label, width in [
            ("track", "Track", 210), ("kind", "Kind", 85), ("status", "Status", 90),
            ("progress", "Progress", 75), ("detail", "Detail", 230),
        ]:
            self.audio_status.heading(col, text=label)
            self.audio_status.column(col, width=width, anchor=W)
        audio_scroll.configure(command=self.audio_status.yview)
        audio_scroll.pack(side=RIGHT, fill="y")
        self.audio_status.pack(fill=BOTH, expand=True)
        for tree in (self.stage_status, self.ocr_status, self.audio_status):
            tree.bind("<Double-1>", lambda _event, target=tree: self.show_processing_row(target))
            tree.bind("<Return>", lambda _event, target=tree: self.show_processing_row(target))
        ttk.Label(processing_top, text="Select a row and press Enter (or double-click) for full details.").pack(side=LEFT)

        self.processing = ttk.LabelFrame(self.root, text="Process selected meeting", padding=8)
        # Reserve the action/status area before giving the notebook its space.
        # Otherwise large table requests can push feedback off a short window.
        self.processing.pack(side="bottom", fill=X, padx=12, pady=(0, 12), before=self.notebook)
        processing_actions = ttk.Frame(self.processing)
        processing_actions.pack(fill=X)
        self.process_selected_button = ttk.Button(processing_actions, text="Process selected meeting", command=self.process_selected_meeting)
        self.process_selected_button.pack(side=LEFT)
        self.retry_process_button = ttk.Button(processing_actions, text="Resume processing", command=self.process_selected_meeting, state=DISABLED)
        self.retry_process_button.pack(side=LEFT, padx=(8, 0))
        self.regenerate_notes_button = ttk.Button(processing_actions, text="Regenerate notes", command=self.regenerate_selected_notes)
        self.regenerate_notes_button.pack(side=LEFT, padx=(8, 0))
        self.cancel_process_button = ttk.Button(processing_actions, text="Cancel processing", command=self.cancel_processing, state=DISABLED)
        self.cancel_process_button.pack(side=LEFT, padx=(8, 0))
        output_actions = ttk.Frame(self.processing)
        output_actions.pack(fill=X, pady=(6, 0))
        self.open_notes_button = ttk.Button(output_actions, text="Open notes", command=lambda: self.open_selected_file("transcripts/notes.md"), state=DISABLED)
        self.open_notes_button.pack(side=LEFT)
        self.open_transcript_button = ttk.Button(output_actions, text="Open transcript", command=lambda: self.open_selected_file("transcripts/cleaned.md"), state=DISABLED)
        self.open_transcript_button.pack(side=LEFT, padx=(8, 0))
        self.open_actions_button = ttk.Button(output_actions, text="Open actions", command=lambda: self.open_selected_file("transcripts/actions.md"), state=DISABLED)
        self.open_actions_button.pack(side=LEFT, padx=(8, 0))

        screenshot_mode = ttk.Frame(self.processing)
        screenshot_mode.pack(fill=X, pady=(8, 0))
        self.include_screenshot_ocr_button = ttk.Checkbutton(
            screenshot_mode,
            text="Include selected screenshots in OCR",
            variable=self.include_screenshot_ocr_var,
            command=self.save_screenshot_ocr_mode,
        )
        self.include_screenshot_ocr_button.pack(side=LEFT)
        self.screenshot_processing_var = tk.StringVar(value="")
        ttk.Label(screenshot_mode, textvariable=self.screenshot_processing_var).pack(side=LEFT, padx=8)

        progress_row = ttk.Frame(self.processing)
        progress_row.pack(fill=X, pady=(8, 0))
        ttk.Label(progress_row, textvariable=self.process_stage_var, width=18).pack(side=LEFT)
        self.processing_bar = ttk.Progressbar(progress_row, variable=self.process_progress_var, maximum=100)
        self.processing_bar.pack(side=LEFT, fill=X, expand=True, padx=8)
        self.processing_timer = ttk.Label(progress_row, textvariable=self.processing_timer_var)
        self.processing_timer.pack(side=RIGHT)
        self.processing_message = ttk.Label(self.processing, textvariable=self.process_var, anchor=W, justify=LEFT)
        self.processing_message.pack(fill=X, pady=(6, 0))
        self.processing_message.bind("<Configure>", lambda event: self.processing_message.configure(wraplength=max(100, event.width - 8)))

        bottom = ttk.Frame(self.root, padding=(12, 0, 12, 12))
        bottom.pack(fill=X)
        ttk.Label(bottom, textvariable=self.summary_var).pack(side=LEFT)

    def show_processing_row(self, tree: ttk.Treeview) -> None:
        selection = tree.selection()
        if not selection:
            return
        values = tree.item(selection[0], "values")
        dialog = tk.Toplevel(self.root)
        dialog.title(f"Processing details — {values[0]}")
        dialog.geometry("760x420")
        text = tk.Text(dialog, wrap="word", padx=12, pady=12)
        scroll = ttk.Scrollbar(dialog, command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        scroll.pack(side=RIGHT, fill="y")
        text.pack(fill=BOTH, expand=True)
        for column, value in zip(tree["columns"], values):
            text.insert(END, f"{tree.heading(column, 'text')}: {value}\n\n")
        text.configure(state=DISABLED)
        text.focus_set()
        dialog.bind("<Escape>", lambda _event: dialog.destroy())

    def choose_dir(self) -> None:
        selected = filedialog.askdirectory(initialdir=self.out_dir_var.get())
        if selected:
            self.out_dir_var.set(selected)
            self.refresh_latest()

    def row_key(self, kind: str, name: str) -> tuple[str, str]:
        return kind, name.lower()

    def upsert_device(self, kind: str, role: str, name: str, recording: str = "No",
                      file_name: str = "", status: str = "Ready", latest: str = "", level: str = "") -> None:
        key = self.row_key(kind, name)
        values = (kind, role, name, recording, file_name, level, status, latest)
        row = self.device_rows.get(key)
        if row and self.devices.exists(row):
            self.devices.item(row, values=values)
        else:
            self.device_rows[key] = self.devices.insert("", END, values=values)

    def refresh_devices(self) -> None:
        for row in self.devices.get_children():
            self.devices.delete(row)
        self.device_rows.clear()
        latest_text = fmt_session_time(latest_session(Path(self.out_dir_var.get())))
        try:
            _np, sc = capture.require_deps()
            default_speaker = capture.device_info(sc.default_speaker()).get("name")
            default_mic = capture.device_info(sc.default_microphone()).get("name")
            for speaker in sc.all_speakers():
                info = capture.device_info(speaker)
                role = "default output" if info.get("name") == default_speaker else "output"
                self.upsert_device("speaker", role, info.get("name", ""), latest=latest_text)
            for mic in capture.default_input_microphones(sc):
                info = capture.device_info(mic)
                role = "default mic" if info.get("name") == default_mic else "mic"
                self.upsert_device("microphone", role, info.get("name", ""), latest=latest_text)
            self.summary_var.set("Device list refreshed")
        except Exception as exc:
            self.summary_var.set(f"Device refresh failed: {exc}")
            messagebox.showerror("Device refresh failed", str(exc))

    def refresh_latest(self) -> Path | None:
        latest = latest_session(Path(self.out_dir_var.get()))
        self.latest_var.set(f"{fmt_session_time(latest)}  {latest}" if latest else "None")
        return latest

    def session_title(self, session: Path) -> str:
        meeting = read_json(session / "meeting.json")
        if meeting.get("title"):
            return meeting["title"]
        return session.name

    def screenshot_count(self, session: Path) -> int:
        roots = [session / "screenshots", session / "screenshots" / "imported"]
        count = 0
        for root in roots:
            if root.exists():
                count += sum(1 for p in root.iterdir() if p.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"})
        return count

    def ocr_counts(self, session: Path) -> dict[str, int]:
        results = read_json(session / "visual" / "ocr.json").get("results", [])
        frames = read_json(session / "screenshots" / "manifest.json").get("frames", [])
        selected_paths = {
            str(Path(str(frame["path"])).resolve()).casefold()
            for frame in frames
            if frame.get("selected") and frame.get("path")
        }
        if selected_paths:
            results = [
                result for result in results
                if result.get("path") and str(Path(str(result["path"])).resolve()).casefold() in selected_paths
            ]
        required = len(selected_paths) if selected_paths else len(results)
        edits = screenshot_edits.load_edits(session)
        results = [result for result in results if ocr_result_current(session, result, edits)]
        complete = sum(bool(result.get("ok")) for result in results)
        failed = sum(
            result.get("status") == "failed"
            or (not result.get("ok") and result.get("status") not in {"pending", "processing"})
            for result in results
        )
        return {
            "total": required,
            "complete": complete,
            "failed": failed,
            "pending": max(0, required - complete - failed),
        }

    def screenshot_ocr_enabled(self, session: Path) -> bool:
        metadata = read_json(session / "metadata.json")
        value = (metadata.get("screen_context_processing") or {}).get("ocr_enabled")
        if value is not None:
            return bool(value)
        # Interpret prior completed runs correctly: versions before the toggle
        # recorded an explicit skip in process metadata rather than metadata.
        process_metadata = read_json(session / "transcripts" / "process_metadata.json")
        return "Skipped OCR by request." not in (process_metadata.get("messages") or [])

    def set_screenshot_ocr_enabled(self, session: Path, enabled: bool) -> None:
        metadata_path = session / "metadata.json"
        metadata = read_json(metadata_path)
        metadata["screen_context_processing"] = {
            "ocr_enabled": enabled,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }
        metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")

    def processing_failed(self, session: Path) -> bool:
        if self is not None and self.is_processing_session(session):
            return False
        return read_json(session / "transcripts" / "process_state.json").get("status") in ("failed", "running")

    def is_processing_session(self, session: Path | None) -> bool:
        active_session = getattr(self, "processing_session", None)
        return bool(getattr(self, "processing_running", False) and session and active_session
                    and session.resolve() == active_session.resolve())

    def screenshot_ocr_cell(self, session: Path) -> str:
        total = self.screenshot_count(session)
        if not total:
            return "No screenshots"
        ocr = read_json(session / "visual" / "ocr.json")
        if ocr:
            counts = self.ocr_counts(session)
            if counts["failed"]:
                return f"{counts['complete']} ok, {counts['failed']} failed / {total}"
            if counts["pending"]:
                return f"{counts['complete']} ok, {counts['pending']} pending / {total}"
            return f"{counts['complete']} processed / {total}"
        if not self.screenshot_ocr_enabled(session):
            return f"Skipped / {total}"
        manifest = read_json(session / "screenshots" / "manifest.json")
        if manifest.get("frames"):
            selected = sum(1 for frame in manifest["frames"] if frame.get("selected"))
            return f"{selected} selected / {total}"
        return f"Pending / {total}"

    def selected_meeting_text(self, session: Path | None) -> str:
        if not session:
            return "No meeting selected"
        needs_attention = self.ocr_counts(session)["failed"] or self.processing_failed(session)
        state = "Needs attention" if needs_attention else ("Processed" if (session / "transcripts" / "notes.md").exists() else "Ready to process")
        if self.is_processing_session(session):
            state = "Processing"
        elif self.recording and session == self.current_out_dir:
            state = "Recording"
        elif read_json(session / "metadata.json").get("partial"):
            state = f"Partial capture · {state}"
        return f"{fmt_session_time(session)}  ·  {self.session_title(session)}  ·  {state}"

    def update_artifact_controls(self) -> None:
        session = self.selected_or_latest()
        meeting_processed = bool(session and (session / "transcripts" / "notes.md").exists())
        paths = {
            self.open_notes_button: "transcripts/notes.md",
            self.open_transcript_button: "transcripts/cleaned.md",
            self.open_actions_button: "transcripts/actions.md",
        }
        for button, relative_path in paths.items():
            button.configure(state=NORMAL if meeting_processed and (session / relative_path).exists() else DISABLED)
        self.regenerate_notes_button.configure(
            state=NORMAL if not self.processing_running and session and (session / "transcripts" / "cleaned.md").exists() else DISABLED
        )

    def update_processing_context(self) -> None:
        session = self.selected_or_latest()
        self.selected_meeting_var.set(self.selected_meeting_text(session))
        self.edit_title_button.configure(state=NORMAL if session else DISABLED)
        if not session:
            self.processing.configure(text="Process selected meeting")
            self.include_screenshot_ocr_button.configure(state=DISABLED)
            self.ocr_backend_choice.configure(state="disabled")
            self.transcription_backend_choice.configure(state="disabled")
            self.ocr_backend_hint_var.set("Select a meeting to choose screenshot OCR.")
            self.ocr_backend_hint_label.pack(fill=X, before=self.cloud_provider_hint_label, pady=(0, 5))
            self.retry_process_button.configure(state=DISABLED)
            self.screenshot_processing_var.set("Select a meeting to choose screenshot processing.")
            self.update_artifact_controls()
            self.update_processing_progress(session)
            return
        options = read_json(session / "metadata.json").get("processing_options") or {}
        # Cloud is the default; only an explicit saved "local" choice selects Local.
        self.ocr_backend_var.set("Local" if options.get("ocr_backend") == "local" else CLOUD_CHOICE)
        self.transcription_backend_var.set("Local" if options.get("transcription_backend") == "local" else CLOUD_CHOICE)
        self.transcription_backend_choice.configure(state="disabled" if self.processing_running else "readonly")
        processed = (session / "transcripts" / "notes.md").exists()
        ocr_failed = self.ocr_counts(session)["failed"]
        process_failed = self.processing_failed(session)
        needs_attention = ocr_failed or process_failed
        self.processing.configure(text=f"Processing: {self.session_title(self.processing_session)}" if self.processing_running else ("Processing needs attention" if needs_attention else ("Processed meeting" if processed else "Process selected meeting")))
        running = self.processing_running
        self.retry_process_button.configure(state=NORMAL if process_failed and not running else DISABLED)
        total = self.screenshot_count(session)
        enabled = self.screenshot_ocr_enabled(session)
        self.ocr_backend_choice.configure(state="readonly" if total and enabled and not running else "disabled")
        self.ocr_backend_hint_var.set(
            "Screenshot OCR is off for this meeting — enable it in the processing area below to choose Local or Cloud."
            if total and not enabled else
            "No screenshots captured for this meeting." if not total else ""
        )
        if self.ocr_backend_hint_var.get():
            self.ocr_backend_hint_label.pack(fill=X, before=self.cloud_provider_hint_label, pady=(0, 5))
        else:
            self.ocr_backend_hint_label.pack_forget()
        self.include_screenshot_ocr_var.set(enabled)
        self.include_screenshot_ocr_button.configure(state=NORMAL if total and not running else DISABLED)
        if not total:
            self.screenshot_processing_var.set("No screenshots captured")
        elif enabled:
            manifest = read_json(session / "screenshots" / "manifest.json")
            selected = sum(1 for frame in manifest.get("frames", []) if frame.get("selected"))
            counts = self.ocr_counts(session)
            if counts["failed"]:
                selection_text = f"{counts['complete']} OCR complete · {counts['failed']} failed"
            else:
                selection_text = f"{selected} selected of {total}" if manifest.get("frames") else f"{total} captured; automatic selection will be used"
            self.screenshot_processing_var.set(f"{selection_text}")
        else:
            self.screenshot_processing_var.set(f"Skipped — {total} captured screenshots will not be OCR'd")
        self.update_artifact_controls()
        self.update_processing_progress(session)

    def update_processing_progress(self, session: Path | None) -> None:
        """Render saved progress for the selection; preserve a live processing run."""
        if self.processing_running:
            return
        recording = bool(self.recording and session == self.current_out_dir)
        state = read_json(session / "transcripts" / "process_state.json") if session else {}
        failed = bool(session and (state.get("status") == "failed" or self.ocr_counts(session)["failed"]))
        complete = bool(session and (session / "transcripts" / "notes.md").exists() and not failed)
        self.processing_bar.stop()
        self.processing_bar.configure(mode="determinate")
        if recording:
            label, percent, message = "Recording", 0, "Recording in progress; processing will be available after Stop."
            self.processing.configure(text="Recording meeting")
        elif failed or state.get("status") == "running":
            label = "Needs attention" if failed else "Interrupted"
            value = (state.get("last_progress") or {}).get("percent", 0)
            percent = max(0, min(99, value)) if isinstance(value, (int, float)) else 0
            message = state.get("error") or "Previous processing was interrupted. Resume to continue saved work."
        elif complete:
            label, percent, message = "Complete", 100, "Selected meeting is fully processed."
        else:
            label, percent, message = "Ready to process", 0, "Selected meeting is ready to process." if session else "Select a meeting to process."
        self.process_stage_var.set(label)
        self.process_progress_var.set(percent)
        self.process_selected_button.configure(state=DISABLED if recording or not session else NORMAL)
        key = (session, label, message)
        if key != self.progress_display_key:
            self.process_var.set(message)
            self.progress_display_key = key

    def refresh_processing_details(self) -> None:
        saved_views = {}
        for tree in (self.stage_status, self.ocr_status, self.audio_status):
            saved_views[tree] = (tree.yview(), [tree.item(row, "values")[0] for row in tree.selection()])
            for row in tree.get_children():
                tree.delete(row)
        session = self.selected_or_latest()
        if not session:
            self.processing_detail_var.set("Select a meeting to inspect processing status.")
            self.retry_failed_ocr_button.configure(state=DISABLED)
            return

        costs = recorded_cloud_costs(session)
        cost_labels = costs["labels"]
        diagnostics = read_json(session / "transcripts" / "diagnostics.json")
        ocr = read_json(session / "visual" / "ocr.json")
        process_state = read_json(session / "transcripts" / "process_state.json")
        running_here = self.is_processing_session(session)
        active = self.active_progress_payload if running_here else {}
        parallel_stages = active.get("parallel_stages") or {}
        active_ocr = parallel_stages.get("ocr", {}) if parallel_stages else active
        active_audio = parallel_stages.get("audio", {}) if parallel_stages else active
        self.update_ocr_preview(session, active_ocr)
        counts = self.ocr_counts(session)
        selected_tracks = [track for track in diagnostics.get("tracks", []) if track.get("selected")]
        complete_tracks = 0
        active_item = str(active_audio.get("item", ""))

        for track in selected_tracks:
            source = Path(str(track.get("path", "")))
            transcript = session / "transcripts" / f"{source.stem}.json"
            complete = transcript.exists()
            complete_tracks += int(complete)
            is_active = bool(active_item and Path(active_item).name == source.name and active_audio.get("stage") == "transcript")
            status = "Complete" if complete else ("Processing" if is_active else "Pending")
            progress_text = "100%" if complete else ("In progress" if is_active else "0%")
            detail = (track.get("device") or {}).get("name", "")
            if is_active and not complete:
                if isinstance(active_audio.get("item_percent"), (int, float)):
                    progress_text = f"{active_audio['item_percent']:.0f}%"
                    position = int(active_audio.get("audio_seconds", 0))
                    duration = int(active_audio.get("audio_total_seconds", 0))
                    detail = f"Audio decoded: {position // 60:02d}:{position % 60:02d} / {duration // 60:02d}:{duration % 60:02d}"
                else:
                    progress_text = "Preparing"
                    detail = "Loading model / preparing this audio file"
            failed_progress = process_state.get("last_progress") or {}
            if (not running_here and not complete and process_state.get("status") == "failed"
                    and failed_progress.get("stage") == "transcript"
                    and (source.name in failed_progress.get("failed_items", [])
                         or not failed_progress.get("item") or Path(failed_progress["item"]).name == source.name)):
                status, detail = "Failed", process_state.get("error", "Transcription stopped; resume to retry")
            self.audio_status.insert("", END, values=(source.name, track.get("kind", ""), status, progress_text, detail))

        for result in ocr.get("results", []):
            status_value = result.get("status") or ("complete" if result.get("ok") else "failed")
            current_result = ocr_result_current(session, result, screenshot_edits.load_edits(session))
            self.ocr_status.insert("", END, values=(
                screenshot_edits.input_path(session, Path(result['path'])).name if result.get('path') and Path(result['path']).exists() else Path(str(result.get('ocr_input_path') or result.get('path', ''))).name,
                status_value.title() if current_result else "Pending",
                result.get("attempts", 1 if result.get("ok") else 0),
                result.get("error", "") if current_result else "Crop / mask changed; resume to process updated image",
                result.get('path', ''),
            ))

        if not diagnostics:
            audio_status, audio_detail = "Pending", "Audio tracks have not been analyzed"
        elif selected_tracks and complete_tracks == len(selected_tracks):
            audio_status, audio_detail = "Complete", f"{complete_tracks} of {len(selected_tracks)} tracks transcribed"
        elif active_audio.get("stage") == "transcript":
            audio_status, audio_detail = "Processing", f"{complete_tracks} of {len(selected_tracks)} tracks complete"
        else:
            audio_status, audio_detail = "Pending", f"{complete_tracks} of {len(selected_tracks)} tracks complete"

        if not self.screenshot_ocr_enabled(session):
            ocr_status, ocr_detail = "Skipped", "Screenshot OCR disabled for this meeting"
        elif ocr and not counts["total"]:
            ocr_status, ocr_detail = "Complete", "No screenshots selected; OCR is satisfied"
        elif not ocr:
            ocr_status, ocr_detail = "Pending", "No selected screenshots processed yet"
        elif counts["failed"]:
            ocr_status, ocr_detail = "Failed", f"{counts['complete']} complete · {counts['failed']} failed"
        elif counts["pending"] or active_ocr.get("stage") == "ocr":
            ocr_status, ocr_detail = "Processing", f"{counts['complete']} complete · {counts['pending']} pending"
        else:
            ocr_status, ocr_detail = "Complete", f"All {counts['complete']} selected screenshots succeeded"

        stage_rows = [
            ("Audio analysis", "Complete" if diagnostics else "Pending", "—", f"{len(selected_tracks)} tracks selected" if diagnostics else "Not analyzed"),
            ("Screenshot OCR", ocr_status, cost_labels["ocr"], ocr_detail),
            ("Audio transcription", audio_status, cost_labels["transcript"], audio_detail),
            ("Transcript cleanup", "Complete" if (session / "transcripts" / "cleaned.md").exists() else "Pending", "—", "Cleaned transcript ready" if (session / "transcripts" / "cleaned.md").exists() else "Waiting for audio"),
            (
                "Notes and actions",
                "Needs update" if counts["failed"] and (session / "transcripts" / "notes.md").exists() else ("Complete" if (session / "transcripts" / "notes.md").exists() and (session / "transcripts" / "actions.md").exists() else "Pending"),
                cost_labels["notes"],
                "Existing notes omit failed OCR images" if counts["failed"] and (session / "transcripts" / "notes.md").exists() else ("Generated outputs ready" if (session / "transcripts" / "notes.md").exists() else "Waiting for prior stages"),
            ),
        ]
        labels = {"diagnostics": "Audio analysis", "ocr": "Screenshot OCR", "transcript": "Audio transcription", "cleanup": "Transcript cleanup", "notes": "Notes and actions"}
        if active:
            completion_percent = {"diagnostics": 10, "ocr": 45, "transcript": 72, "cleanup": 84, "notes": 100}
            active_stages = (active_ocr, active_audio) if parallel_stages else (active,)
            for stage_progress in active_stages:
                stage_name = stage_progress.get("stage")
                stage_rows = [
                    (stage, "Processing", cost, stage_progress.get("progress", detail))
                    if stage == labels.get(stage_name) and stage_progress.get("percent", 0) < completion_percent.get(stage_name, 100)
                    else (stage, status, cost, detail)
                    for stage, status, cost, detail in stage_rows
                ]
        if not running_here and process_state.get("status") == "failed":
            failed_stage = (process_state.get("last_progress") or {}).get("stage")
            stage_rows = [
                (stage, "Failed", cost, process_state.get("error", detail)) if stage == labels.get(failed_stage) and not (failed_stage == "ocr" and status in ("Complete", "Skipped")) else (stage, status, cost, detail)
                for stage, status, cost, detail in stage_rows
            ]
        for row in stage_rows:
            self.stage_status.insert("", END, values=row)

        summary_parts = [f"OCR: {counts['complete']} complete"]
        if counts["failed"]:
            summary_parts.append(f"{counts['failed']} failed")
        if running_here:
            summary_parts.insert(0, "Processing now")
        elif process_state.get("status") == "failed":
            failed_stage = (process_state.get("last_progress") or {}).get("stage")
            summary_parts.insert(0, f"Stopped at {labels.get(failed_stage, 'preparation')} — Resume to retry")
        elif process_state.get("status") == "running":
            summary_parts.insert(0, "Previous run interrupted — Resume to continue")
        summary_parts.append(f"Audio: {complete_tracks} of {len(selected_tracks)} complete")
        summary_parts.append(str(costs["summary"]))
        self.processing_detail_var.set(" · ".join(summary_parts))
        running = self.processing_running
        self.retry_failed_ocr_button.configure(state=NORMAL if counts["failed"] and not running else DISABLED)
        for tree, (view, selected) in saved_views.items():
            if view:
                tree.yview_moveto(view[0])
            for row in tree.get_children():
                if tree.item(row, "values")[0] in selected:
                    tree.selection_add(row)

    def save_screenshot_ocr_mode(self) -> None:
        session = self.selected_or_latest()
        if not session:
            return
        enabled = self.include_screenshot_ocr_var.get()
        self.set_screenshot_ocr_enabled(session, enabled)
        self.refresh_screenshot_summary()
        self.refresh_history()
        self.update_processing_context()
        self.process_var.set(
            "Screenshot OCR will use the selected images." if enabled
            else "Screenshot OCR will be skipped for this meeting."
        )

    def save_processing_backends(self, _event: object = None) -> None:
        session = self.selected_or_latest()
        if not session or self.processing_running:
            return
        metadata_path = session / "metadata.json"
        metadata = read_json(metadata_path)
        metadata["processing_options"] = {
            "ocr_backend": "openrouter" if self.ocr_backend_var.get() == "Cloud (OpenRouter)" else "local",
            "transcription_backend": "openrouter" if self.transcription_backend_var.get() == "Cloud (OpenRouter)" else "local",
        }
        metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        self.process_var.set("Processing choices saved for this meeting. Resume retries only unfinished work.")

    def refresh_history(self) -> None:
        previous_selection = self.selected_session
        for row in self.history.get_children():
            self.history.delete(row)
        self.history_rows.clear()
        sessions = session_dirs(Path(self.out_dir_var.get()))
        for session in sessions:
            metadata = read_json(session / "metadata.json")
            duration = metadata.get("duration_seconds")
            duration_text = ""
            if isinstance(duration, (int, float)):
                minutes, seconds = divmod(int(duration), 60)
                duration_text = f"{minutes}m {seconds:02d}s"
            needs_attention = self.ocr_counts(session)["failed"] or self.processing_failed(session)
            processed = "Needs attention" if needs_attention else ("Yes" if (session / "transcripts" / "notes.md").exists() else "No")
            row = self.history.insert(
                "",
                END,
                values=(fmt_session_time(session), self.session_title(session), duration_text, processed, self.screenshot_ocr_cell(session)),
            )
            self.history_rows[row] = session
        self.refresh_latest()
        selected = next(
            (path for path in self.history_rows.values()
             if previous_selection and path.resolve() == previous_selection.resolve()),
            None,
        )
        if self.recording and previous_selection == self.current_out_dir:
            selected = self.current_out_dir
        self.selected_session = selected
        if selected:
            for row, path in self.history_rows.items():
                if path == selected:
                    self.history.selection_set(row)
                    self.history.focus(row)
                    break
        self.load_selected_notes()

    def on_history_select(self, _event: object | None = None) -> None:
        selection = self.history.selection()
        if not selection:
            return
        self.selected_session = self.history_rows.get(selection[0])
        self.load_selected_notes()

    def selected_or_latest(self) -> Path | None:
        return self.selected_session

    def load_selected_notes(self) -> None:
        session = self.selected_or_latest()
        self.notes_text.delete("1.0", END)
        self.load_saved_live_captions(session)
        if not session:
            self.title_var.set("")
            self.screenshot_review_var.set("Select a meeting to review its screen context.")
            self.update_visual_output_controls()
            self.update_processing_context()
            self.refresh_processing_details()
            return
        self.title_var.set(self.session_title(session))
        notes = session / "transcripts" / "notes.md"
        if notes.exists():
            self.notes_text.insert("1.0", notes.read_text(encoding="utf-8"))
        self.refresh_track_review()
        self.refresh_screenshot_summary()
        self.update_visual_output_controls()
        self.update_processing_context()
        self.refresh_processing_details()

    @staticmethod
    def live_caption_line(record: dict) -> str:
        source = "Microphone" if record.get("source") in ("microphone", "mic") else "Meeting audio"
        device = record.get("device") or "Unknown device"
        start = float(record.get("start", 0))
        minutes, seconds = divmod(int(start), 60)
        return f"[{minutes:02d}:{seconds:02d}] {source} · {device}: {record.get('text', '')}\n"

    @staticmethod
    def set_readonly_text(widget: tk.Text, value: str) -> None:
        widget.configure(state=NORMAL)
        widget.delete("1.0", END)
        widget.insert(END, value)
        widget.configure(state=DISABLED)

    def load_saved_live_captions(self, session: Path | None) -> None:
        path = session / "transcripts" / "live_captions.jsonl" if session else None
        records = []
        incomplete = False
        if path and path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    incomplete = True
                    continue
                if record.get("status") == "committed" or record.get("type") == "caption" or record.get("event") == "live_caption":
                    records.append(record)
                elif record.get("status") in ("gap", "error") or record.get("type") in ("gap", "error") or record.get("event") in ("live_gap", "live_error"):
                    incomplete = True
        self.set_readonly_text(self.live_review_text, "".join(self.live_caption_line(item) for item in records))
        if not session:
            message = "Select a meeting to review saved live captions. These are not the final transcript."
        elif not path or not path.exists():
            message = "No saved live captions for this meeting. Final transcript is separate."
        else:
            message = f"{len(records)} saved live caption(s)" + (" · incomplete intervals" if incomplete else "") + ". Final transcript is separate."
        self.live_review_status_var.set(message)

    def handle_live_event(self, event: dict) -> None:
        kind = event.get("event")
        if kind == "live_status":
            status = str(event.get("status", "unknown"))
            detail = str(event.get("detail", "") or "")
            label = status.replace("_", " ").capitalize()
            if status == "stopped" and self.live_incomplete:
                label += " · incomplete live captions (recorded WAV is intact)"
            self.live_status_var.set(f"{label}: {detail}" if detail else label)
        elif kind == "live_caption":
            source = str(event.get("source") or "unknown")
            if event.get("provisional"):
                self.live_previews[source] = "Provisional · " + self.live_caption_line(event).strip()
            else:
                self.live_previews.pop(source, None)
                self.live_transcript_text.configure(state=NORMAL)
                self.live_transcript_text.insert(END, self.live_caption_line(event))
                self.live_transcript_text.see(END)
                self.live_transcript_text.configure(state=DISABLED)
            self.live_preview_var.set("\n".join(self.live_previews[key] for key in ("microphone", "system", "unknown")
                                                if key in self.live_previews))
        elif kind == "live_source":
            source = "Microphone" if event.get("source") == "microphone" else "Meeting audio"
            changed = " changed" if event.get("previous_track_id") else ""
            self.live_sources[str(event.get("source"))] = f"{source} source{changed}: {event.get('device', 'Unknown device')}"
            self.live_source_var.set(" · ".join(self.live_sources[key] for key in ("microphone", "system") if key in self.live_sources))
        elif kind in ("live_gap", "live_error"):
            self.live_incomplete = True
            reason = event.get("reason") or event.get("detail") or "Unknown reason"
            self.live_status_var.set(f"Live caption gap/error: {reason}. Recorded audio remains available.")

    def update_visual_output_controls(self) -> None:
        session = self.selected_or_latest()
        self.open_timeline_button.configure(state=NORMAL if session and (session / "visual" / "timeline.md").exists() else DISABLED)

    def refresh_screenshot_summary(self) -> None:
        session = self.selected_or_latest()
        if not session:
            return
        if not self.screenshot_ocr_enabled(session):
            self.screenshot_review_var.set(
                f"Screenshot OCR is skipped for this meeting. {self.screenshot_count(session)} screenshots were captured."
            )
            return
        manifest = read_json(session / "screenshots" / "manifest.json")
        frames = manifest.get("frames", [])
        if frames:
            included = sum(1 for frame in frames if frame.get("selected"))
            counts = self.ocr_counts(session)
            if counts["failed"]:
                self.screenshot_review_var.set(
                    f"OCR needs attention: {counts['complete']} succeeded and {counts['failed']} failed. Open Processing to inspect or retry failures."
                )
            else:
                self.screenshot_review_var.set(f"{included} of {len(frames)} screenshots selected for OCR. Review to change the automatic selection.")
        else:
            self.screenshot_review_var.set(f"{self.screenshot_count(session)} screenshots captured. Review to see automatic OCR selection.")

    def refresh_track_review(self) -> None:
        for row in self.track_review.get_children():
            self.track_review.delete(row)
        self.track_rows.clear()
        session = self.selected_or_latest()
        if not session:
            return
        metadata = read_json(session / "metadata.json")
        selected_ids = (metadata.get("processing_selection") or {}).get("track_ids")
        diagnostics = read_json(session / "transcripts" / "diagnostics.json")
        diagnostic_by_id = {item.get("track_id"): item for item in diagnostics.get("tracks", [])}
        for track in metadata.get("tracks", []):
            track_id = str(track.get("track_id", ""))
            diagnostic = diagnostic_by_id.get(track_id, {})
            automatic = bool(diagnostic.get("automatic_selected", diagnostic.get("selected", track.get("kind") in {"system", "microphone"})))
            included = track_id in selected_ids if isinstance(selected_ids, list) else automatic
            stats = diagnostic
            peak = stats.get("p95_db")
            peak_text = f"{peak:.1f} dB" if isinstance(peak, (int, float)) else "Analyze"
            row = self.track_review.insert("", END, values=("Yes" if included else "No", track.get("kind", ""), (track.get("device") or {}).get("name", ""), "Yes" if stats.get("active") else ("No" if stats else "Analyze"), peak_text))
            self.track_rows[row] = track_id

    def analyze_tracks(self) -> None:
        session = self.selected_or_latest()
        if not session:
            return
        self.process_var.set("Analyzing audio activity for track review...")
        def worker() -> None:
            completed = subprocess.run([
                sys.executable, str(Path(__file__).with_name("process_latest.py")), str(session), "--diagnostics-only"
            ], text=True, capture_output=True, check=False)
            self.process_events.put(("diagnostics_done", completed.returncode, completed.stdout, completed.stderr))
        threading.Thread(target=worker, daemon=True).start()

    def save_track_selection(self, track_ids: list[str] | None) -> None:
        session = self.selected_or_latest()
        if not session:
            return
        metadata_path = session / "metadata.json"
        metadata = read_json(metadata_path)
        if track_ids is None:
            metadata.pop("processing_selection", None)
            self.process_var.set("Automatic audio-track selection restored")
        else:
            metadata["processing_selection"] = {"track_ids": track_ids, "updated_at": datetime.now().isoformat(timespec="seconds")}
            self.process_var.set(f"Saved {len(track_ids)} selected audio track(s)")
        metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        self.refresh_track_review()

    def toggle_selected_track(self) -> None:
        selection = self.track_review.selection()
        if not selection:
            messagebox.showinfo("Select a track", "Select an audio track first.")
            return
        current = {self.track_rows[row] for row in self.track_review.get_children() if self.track_review.item(row, "values")[0] == "Yes"}
        track_id = self.track_rows[selection[0]]
        if track_id in current:
            current.remove(track_id)
        else:
            current.add(track_id)
        self.save_track_selection(sorted(current))

    def use_automatic_tracks(self) -> None:
        self.save_track_selection(None)

    def review_screenshots(self) -> None:
        session = self.selected_or_latest()
        if not session:
            messagebox.showinfo("No meeting", "No captured meeting found.")
            return
        manifest_path = session / "screenshots" / "manifest.json"
        def save(frames: list[dict]) -> None:
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(json.dumps({"generated_at": datetime.now().isoformat(timespec="seconds"), "source_image_count": len(frames), "selected_image_count": sum(1 for entry in frames if entry["selected"]), "frames": frames}, indent=2), encoding="utf-8")
            self.refresh_screenshot_summary()
            self.update_processing_context()
            self.refresh_processing_details()
            self.process_var.set("Screenshot choices saved. Screenshot OCR remains off for this meeting."
                                 if not self.screenshot_ocr_enabled(session) else
                                 "Screenshot choices saved; processing will use them.")
        editing_safe = not self.processing_running and not (self.recording and session == self.current_out_dir)
        ScreenshotReview(self.root, session, save, allow_edits=editing_safe)

    def update_ocr_preview(self, session: Path, active: dict) -> None:
        item = active.get('item') if active.get('stage') == 'ocr' else None
        if not item:
            self.ocr_preview_key = None
            self.ocr_preview_frame.pack_forget()
            self.ocr_preview_photo = None
            return
        key = (str(session), str(item), active.get('preview_path'))
        display_path = Path(active['preview_path']) if active.get('preview_path') else screenshot_edits.input_path(session, Path(item))
        self.ocr_preview_var.set(f"OCR image {active.get('current', '')} of {active.get('total', '')}\n{display_path.name}")
        self.ocr_preview_frame.pack(side=RIGHT, padx=(6, 0))
        if key == self.ocr_preview_key:
            return
        self.ocr_preview_key = key
        self.ocr_preview_path = None
        self.ocr_preview_button.configure(image='', text='Loading preview…')
        def worker():
            try:
                from PIL import Image
                path = Path(active['preview_path']) if active.get('preview_path') else screenshot_edits.ocr_input(session, Path(item))
                with Image.open(path) as image:
                    image.thumbnail((155, 85))
                    preview = image.convert('RGB')
                self.process_events.put(('ocr_preview', key, preview, str(path)))
            except Exception:
                self.process_events.put(('ocr_preview', key, None, None))
        threading.Thread(target=worker, daemon=True).start()

    def open_ocr_preview(self) -> None:
        if self.ocr_preview_path:
            os.startfile(self.ocr_preview_path)

    def save_title(self) -> None:
        session = self.selected_or_latest()
        if not session:
            return
        title = " ".join(self.title_var.get().split()) or session.name
        meeting_path = session / "meeting.json"
        meeting = read_json(meeting_path)
        meeting.update({
            "title": title,
            "title_updated_by_user": True,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        })
        meeting_path.write_text(json.dumps(meeting, indent=2), encoding="utf-8")
        self.refresh_history()
        self.update_processing_context()
        self.process_var.set("Title saved")

    def edit_selected_title(self) -> None:
        session = self.selected_or_latest()
        if not session:
            return
        dialog = tk.Toplevel(self.root)
        dialog.title('Edit meeting title')
        dialog.transient(self.root)
        dialog.resizable(False, False)
        frame = ttk.Frame(dialog, padding=12)
        frame.pack(fill=X)
        ttk.Label(frame, text='Meeting title').pack(anchor=W)
        title = tk.StringVar(master=dialog, value=self.session_title(session))
        entry = ttk.Entry(frame, textvariable=title, width=58)
        entry.pack(fill=X, pady=(5, 10))
        buttons = ttk.Frame(frame)
        buttons.pack(fill=X)
        ttk.Button(buttons, text='Cancel', command=dialog.destroy).pack(side=RIGHT)
        def save() -> None:
            self.title_var.set(title.get())
            self.save_title()
            dialog.destroy()
        ttk.Button(buttons, text='Save title', command=save).pack(side=RIGHT, padx=(0, 8))
        entry.focus_set()
        entry.selection_range(0, END)
        dialog.grab_set()

    def save_notes(self) -> None:
        session = self.selected_or_latest()
        if not session:
            return
        notes = session / "transcripts" / "notes.md"
        notes.parent.mkdir(parents=True, exist_ok=True)
        notes.write_text(self.notes_text.get("1.0", END).rstrip() + "\n", encoding="utf-8")
        self.refresh_history()
        self.process_var.set("Notes saved")

    def copy_notes(self) -> None:
        text = self.notes_text.get("1.0", END).strip()
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.process_var.set("Notes copied to clipboard")

    def choose_capture_window(self) -> None:
        windows = window_picker.sort_window_choices([
            {**window, 'process_name': window_picker.window_process_name(int(window['hwnd']))}
            for window in visible_windows()
        ])
        if not windows:
            messagebox.showinfo("No windows", "No visible windows were found.")
            return
        dialog = tk.Toplevel(self.root)
        dialog.title("Choose Screen Context Window")
        place_dialog_near_parent(dialog, self.root, 720, 420)
        dialog.transient(self.root)
        dialog.grab_set()

        frame = ttk.Frame(dialog, padding=10)
        frame.pack(fill=BOTH, expand=True)
        tree = ttk.Treeview(frame, columns=("title", "size"), show="tree headings")
        tree.heading("#0", text="")
        # Treeview adds indentation before each item image; leave room for
        # that and the 20px app icon before the window title begins.
        tree.column("#0", width=52, minwidth=52, stretch=False)
        tree.heading("title", text="Window")
        tree.heading("size", text="Size")
        tree.column("title", width=536, anchor=W)
        tree.column("size", width=110, anchor=W)
        tree.pack(fill=BOTH, expand=True)
        row_to_window: dict[str, dict[str, object]] = {}
        for window in windows:
            row = tree.insert("", END, values=(window["title"], window["size"]))
            row_to_window[row] = window

        icon_events: queue.Queue = queue.Queue()
        icon_stop = threading.Event()
        dialog.window_icons = []
        icon_poll_id = None

        def load_icons() -> None:
            for row, window in row_to_window.items():
                if icon_stop.is_set():
                    return
                image = window_picker.window_icon_image(int(window['hwnd']))
                if image is not None:
                    icon_events.put((row, image))

        def poll_icons() -> None:
            nonlocal icon_poll_id
            while True:
                try:
                    row, image = icon_events.get_nowait()
                except queue.Empty:
                    break
                if tree.exists(row):
                    photo = ImageTk.PhotoImage(image, master=dialog)
                    dialog.window_icons.append(photo)
                    tree.item(row, image=photo)
            if icon_thread.is_alive() or not icon_events.empty():
                icon_poll_id = dialog.after(50, poll_icons)

        def close() -> None:
            icon_stop.set()
            if icon_poll_id is not None:
                dialog.after_cancel(icon_poll_id)
            dialog.destroy()

        icon_thread = threading.Thread(target=load_icons, daemon=True)
        icon_thread.start()
        icon_poll_id = dialog.after(50, poll_icons)
        dialog.protocol('WM_DELETE_WINDOW', close)

        buttons = ttk.Frame(frame)
        buttons.pack(fill=X, pady=(8, 0))

        def select() -> None:
            selection = tree.selection()
            if not selection:
                return
            window = row_to_window[selection[0]]
            self.selected_window_hwnd = int(window["hwnd"])
            self.selected_window_title = str(window["title"])
            label = self.selected_window_title
            if len(label) > 70:
                label = label[:67] + "..."
            self.selected_window_var.set(label)
            self.screen_scope_var.set("Selected window")
            close()

        ttk.Button(buttons, text="Use Selected Window", command=select).pack(side=LEFT)
        ttk.Button(buttons, text="Cancel", command=close).pack(side=RIGHT)
        tree.bind("<Double-1>", lambda _event: select())
        if tree.get_children():
            first = tree.get_children()[0]
            tree.selection_set(first)
            tree.focus(first)

    def start(self) -> None:
        if self.recording:
            return
        base = Path(self.out_dir_var.get())
        out_dir = timestamp_dir(base)
        self.current_out_dir = out_dir
        self.live_incomplete = False
        self.live_previews.clear()
        self.live_preview_var.set("")
        self.live_enabled_for_recording = bool(self.live_enabled_var.get())
        self.live_generation += 1
        self.live_status_var.set("Starting local live captions…" if self.live_enabled_for_recording
                                 else "Live captions off for this recording; WAV capture continues")
        self.live_sources.clear()
        self.live_source_var.set("No audio source selected yet")
        self.set_readonly_text(self.live_transcript_text, "")
        self.metadata = None
        self.status_var.set(f"Starting: {out_dir}")
        self.summary_var.set("Opening all available audio devices")
        self.process_var.set("Recording in progress")
        self.start_button.configure(state=DISABLED)
        self.stop_button.configure(state=NORMAL)
        self.manual_screenshot_button.configure(state=NORMAL)
        self.recording = True
        self.capture_stopping = False
        if not self.processing_running:
            self.selected_session = out_dir
            self.history.selection_remove(*self.history.selection())
            self.load_selected_notes()
        self.started_at = time.time()
        self.screenshot_capture_index = 0
        self.capture_preview_key = None
        self.capture_preview_path = None
        self.capture_preview_photo = None
        self.capture_preview_label.configure(image="", text="No screenshot yet")
        self.screenshot_source_snapshot = self.snapshot_external_screenshots()

        for row in self.devices.get_children():
            values = list(self.devices.item(row, "values"))
            values[3] = "Opening"
            values[4] = ""
            values[5] = ""
            values[6] = "Opening"
            self.devices.item(row, values=values)

        args = argparse.Namespace(
            out_dir=str(out_dir),
            seconds=None,
            speaker=None,
            microphone=None,
            all_devices=True,
            sample_rate=48000,
            chunk_ms=100,
            system_channels=2,
            mic_channels=1,
        )
        self.worker = threading.Thread(target=self._record_worker, args=(args,), daemon=True)
        self.worker.start()
        self.start_screen_context(out_dir)

    def on_live_caption_toggle(self) -> None:
        enabled = bool(self.live_enabled_var.get())
        if self.recording and not self.capture_stopping:
            self.live_enabled_for_recording = enabled
            self.live_generation += 1
            self.live_status_var.set("Starting local live captions…" if enabled
                                     else "Live captions off; WAV capture continues")
            if not enabled:
                self.live_previews.clear()
                self.live_preview_var.set("")
            self.live_control_event.set()
        else:
            self.live_status_var.set("Live captions on for next recording" if enabled
                                     else "Live captions off for next recording")

    def _record_worker(self, args: argparse.Namespace) -> None:
        metadata = None
        recording_error = None
        tap_error = None
        default_device_ids = {}
        preferred_tracks = {}
        try:
            try:
                _np, sc = capture.require_deps()
                default_device_ids = {
                    "system": getattr(sc.default_speaker(), "id", None),
                    "microphone": getattr(sc.default_microphone(), "id", None),
                }
            except Exception:
                pass  # Device enumeration is optional for live source preference.

            self.live_control_stop.clear()
            self.live_control_event.set()

            def manage_live_captions() -> None:
                nonlocal tap_error
                coordinator = None
                failed_generation = None
                try:
                    while not self.live_control_stop.is_set():
                        if self.live_enabled_for_recording and coordinator is None and failed_generation != self.live_generation:
                            generation = self.live_generation
                            try:
                                coordinator = LiveTranscriptionCoordinator(
                                    args.out_dir,
                                    segmenter=segment_audio,
                                    transcriber=MoonshineLive().transcribe,
                                    event_sink=lambda event, token=generation: self.events.put(("live", {**event, "_generation": token})),
                                    queue_size=512,
                                    default_devices=preferred_tracks or default_device_ids,
                                )
                                self.live_coordinator = coordinator
                                coordinator.start()
                            except Exception as live_error:
                                self.live_coordinator = None
                                coordinator = None
                                failed_generation = generation
                                self.events.put(("live", {"event": "live_status", "status": "unavailable",
                                                          "detail": str(live_error), "_generation": generation}))
                        elif not self.live_enabled_for_recording and coordinator is not None:
                            self.live_coordinator = None
                            stopped_coordinator = coordinator
                            coordinator = None
                            try:
                                if tap_error is not None:
                                    error = tap_error
                                    tap_error = None
                                    stopped_coordinator.report_tap_error(**error)
                                stopped_coordinator.stop(timeout=10.0)
                            except Exception as live_error:
                                self.events.put(("live", {"event": "live_error", "reason": str(live_error)}))
                        self.live_control_event.wait(0.2)
                        self.live_control_event.clear()
                finally:
                    self.live_coordinator = None
                    if coordinator is not None:
                        try:
                            if tap_error is not None:
                                error = tap_error
                                tap_error = None
                                coordinator.report_tap_error(**error)
                            coordinator.stop(timeout=10.0)
                        except Exception as live_error:
                            self.events.put(("live", {"event": "live_error", "reason": str(live_error)}))

            self.live_control_thread = threading.Thread(target=manage_live_captions, daemon=True)
            self.live_control_thread.start()

            def capture_event(event: dict) -> None:
                if event.get("event") == "recording_started":
                    preferred_tracks.update(preferred_live_tracks(event.get("tracks", []), default_device_ids))
                    if self.live_coordinator:
                        self.live_coordinator.default_devices.update(preferred_tracks)
                self.events.put(("event", event))

            def safe_audio_tap(block: dict) -> bool:
                nonlocal tap_error
                coordinator = self.live_coordinator if self.live_enabled_for_recording else None
                if coordinator is None:
                    return False
                try:
                    return coordinator.submit(block)
                except Exception as exc:
                    if tap_error is None:
                        tap_error = {"reason": f"Live audio tap failed: {exc}",
                                     "source": block.get("kind"), "track_id": block.get("track_id")}
                        self.events.put_nowait(("live", {"event": "live_error", **tap_error}))
                    return False

            metadata = capture.run_recording(
                args, event_sink=capture_event,
                audio_tap=safe_audio_tap,
            )
        except Exception as exc:
            recording_error = exc
            session = Path(args.out_dir)
            diagnostics = {"type": type(exc).__name__, "message": str(exc), "traceback": traceback.format_exc()}
            if not (session / "metadata.json").exists():
                try:
                    recover_session.recover(session, overwrite=False)
                    diagnostics["recovered_metadata"] = True
                except Exception as recovery_error:
                    diagnostics["recovery_error"] = str(recovery_error)
            try:
                (session / "capture_error.json").write_text(json.dumps(diagnostics, indent=2), encoding="utf-8")
            except OSError:
                pass
        finally:
            self.live_control_stop.set()
            self.live_control_event.set()
            if self.live_control_thread:
                self.live_control_thread.join(15.0)
                self.live_control_thread = None
            self.live_coordinator = None
        if recording_error is not None:
            self.events.put(("error", recording_error))
        elif metadata is not None:
            self.events.put(("done", metadata))

    def stop(self) -> None:
        if not self.recording:
            return
        self.capture_stopping = True
        self.status_var.set("Stopping and finalizing WAV files...")
        self.stop_button.configure(state=DISABLED)
        self.manual_screenshot_button.configure(state=DISABLED)
        self.stop_screen_context()
        capture.request_stop()

    def take_manual_screenshot(self) -> None:
        if not self.recording or self.capture_stopping or not self.current_out_dir:
            return
        out_dir = self.current_out_dir
        self.summary_var.set("Taking screenshot…")
        threading.Thread(target=self._manual_screenshot_worker, args=(out_dir,), daemon=True).start()

    def _manual_screenshot_worker(self, out_dir: Path) -> None:
        try:
            from PIL import ImageGrab
            path, window_index_error = self.save_screenshot(out_dir, ImageGrab, manual=True)
            self.events.put(("screen", {"ok": True, "path": str(path), "manual": True,
                                        "window_index_error": window_index_error}))
        except Exception as exc:
            self.events.put(("screen", {"ok": False, "manual": True, "message": str(exc)}))

    def save_screenshot(self, out_dir: Path, image_grab: object, *, manual: bool = False) -> tuple[Path, str]:
        with self.screenshot_save_lock:
            active_window = self.foreground_window_info()
            image = self.grab_screen_image(image_grab, active_window)
            screenshots_dir = out_dir / "screenshots"
            screenshots_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            suffix = "-manual" if manual else ""
            path = screenshots_dir / f"screen-{stamp}-{self.screenshot_capture_index:04d}{suffix}.png"
            image.save(path)
            self.screenshot_capture_index += 1
            window_index_error = ""
            try:
                screenshot_window_context.append_capture(out_dir, path, active_window)
            except OSError as exc:
                window_index_error = str(exc)
            return path, window_index_error

    def start_screen_context(self, out_dir: Path) -> None:
        if self.capture_stopping:
            return
        if not self.capture_screen_var.get():
            self.summary_var.set("Recording audio; screen context disabled")
            return
        if self.screenshot_worker and self.screenshot_worker.is_alive():
            if self.screenshot_stop.is_set():
                self.root.after(50, lambda: self.start_screen_context(out_dir)
                                if self.recording and not self.capture_stopping and self.capture_screen_var.get() else None)
            return
        self.screenshot_stop.clear()
        screenshots_dir = out_dir / "screenshots"
        screenshots_dir.mkdir(parents=True, exist_ok=True)
        (screenshots_dir / "screen_context.json").write_text(json.dumps(
            {
                "scope": self.screen_scope_var.get(),
                "selected_window_title": self.selected_window_title,
                "selected_window_hwnd": self.selected_window_hwnd,
                "interval_seconds": self.screenshot_interval_seconds,
                "window_bounds_version": 2,
            },
            indent=2,
        ), encoding="utf-8")
        self.screenshot_worker = threading.Thread(
            target=self._screen_context_worker,
            args=(out_dir, self.screenshot_interval_seconds),
            daemon=True,
        )
        self.screenshot_worker.start()

    def stop_screen_context(self) -> None:
        self.screenshot_stop.set()

    def on_screenshot_interval_change(self, *_args) -> None:
        try:
            interval = int(self.screenshot_interval_var.get())
        except (ValueError, tk.TclError):
            return  # Spinbox text is temporarily incomplete while editing.
        self.screenshot_interval_seconds = max(5, min(120, interval))
        if self.recording and self.current_out_dir:
            path = self.current_out_dir / "screenshots" / "screen_context.json"
            if path.exists():
                context = read_json(path)
                context["interval_seconds"] = self.screenshot_interval_seconds
                try:
                    path.write_text(json.dumps(context, indent=2), encoding="utf-8")
                except OSError as exc:
                    self.events.put(("screen", {"ok": False, "message": f"Could not save screenshot interval: {exc}"}))

    def on_capture_screen_toggle(self) -> None:
        if not self.recording or self.capture_stopping:
            return
        if self.capture_screen_var.get() and self.current_out_dir:
            self.summary_var.set("Starting screen capture; audio continues")
            self.start_screen_context(self.current_out_dir)
        else:
            self.stop_screen_context()
            self.summary_var.set("Screen capture paused; audio continues")

    def snapshot_external_screenshots(self) -> dict[Path, float]:
        snapshot = {}
        for root in windows_screenshot_dirs():
            for path in root.iterdir():
                if path.is_file() and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
                    snapshot[path] = path.stat().st_mtime
        return snapshot

    def import_external_screenshots(self, out_dir: Path) -> int:
        imported = 0
        dest = out_dir / "screenshots" / "imported"
        dest.mkdir(parents=True, exist_ok=True)
        meeting_start = self.started_at or time.time()
        for root in windows_screenshot_dirs():
            for path in root.iterdir():
                if not path.is_file() or path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
                    continue
                mtime = path.stat().st_mtime
                if mtime < meeting_start - 2:
                    continue
                if self.screenshot_source_snapshot.get(path) == mtime:
                    continue
                target = dest / path.name
                if not target.exists():
                    shutil.copy2(path, target)
                    imported += 1
        return imported

    def _screen_context_worker(self, out_dir: Path, _initial_interval: int) -> None:
        try:
            from PIL import ImageGrab
        except Exception:
            self.events.put(("screen", {"ok": False, "message": "Install Pillow for automatic screenshots: python -m pip install --user pillow"}))
            return

        screenshots_dir = out_dir / "screenshots"
        last_capture_at = None
        while not self.screenshot_stop.is_set():
            if last_capture_at is not None:
                remaining = last_capture_at + self.screenshot_interval_seconds - time.monotonic()
                if remaining > 0:
                    self.screenshot_stop.wait(min(0.1, remaining))
                    continue
            try:
                path, window_index_error = self.save_screenshot(out_dir, ImageGrab)
                imported = self.import_external_screenshots(out_dir)
                self.events.put(("screen", {"ok": True, "path": str(path), "imported": imported,
                                             "window_index_error": window_index_error}))
            except Exception as exc:
                self.events.put(("screen", {"ok": False, "message": str(exc)}))
            last_capture_at = time.monotonic()
        imported = self.import_external_screenshots(out_dir)
        self.events.put(("screen", {"ok": True, "stopped": True, "imported": imported}))

    def grab_screen_image(self, image_grab: object, active_window: dict | None = None) -> object:
        scope = self.screen_scope_var.get()
        if scope.startswith("Selected window"):
            bbox = self.selected_window_bbox()
            if not bbox:
                raise RuntimeError("Selected window is minimized or unavailable; restore it or choose a window.")
            # Render the window from its own buffer so always-on-top widgets or
            # other windows over it never appear and never block a capture.
            image = print_window_image(self.selected_window_hwnd, bbox)
            if image is not None:
                return image
            # Rare apps refuse to render offscreen; capture that screen area as
            # shown instead (overlapping windows may appear, which is accepted).
            return image_grab.grab(bbox=bbox, all_screens=True)
        if scope != "Active window":
            return image_grab.grab(all_screens=True)
        hwnd = (active_window or {}).get('hwnd')
        bbox = self.window_bbox(hwnd) if hwnd else self.foreground_window_bbox()
        if not bbox:
            return image_grab.grab(all_screens=True)
        return image_grab.grab(bbox=bbox, all_screens=True)

    def selected_window_bbox(self) -> tuple[int, int, int, int] | None:
        hwnd = self.selected_window_hwnd
        if not hwnd and self.selected_window_title:
            hwnd = self.find_window_by_title(self.selected_window_title)
            self.selected_window_hwnd = hwnd
        if not hwnd:
            return None
        bbox = self.window_bbox(hwnd)
        if bbox:
            return bbox
        if self.selected_window_title:
            hwnd = self.find_window_by_title(self.selected_window_title)
            self.selected_window_hwnd = hwnd
            if hwnd:
                return self.window_bbox(hwnd)
        return None

    def find_window_by_title(self, title: str) -> int | None:
        matches = [w for w in visible_windows() if str(w.get("title")) == title]
        if matches:
            return int(matches[0]["hwnd"])
        title_lower = title.lower()
        partial = [w for w in visible_windows() if title_lower in str(w.get("title", "")).lower()]
        if partial:
            return int(partial[0]["hwnd"])
        return None

    def foreground_window_bbox(self) -> tuple[int, int, int, int] | None:
        if os.name != "nt":
            return None
        try:
            import ctypes
            hwnd = ctypes.windll.user32.GetForegroundWindow()
            if not hwnd:
                return None
            return self.window_bbox(hwnd)
        except Exception:
            return None

    def foreground_window_info(self) -> dict[str, object]:
        """Best-effort identity of the active window at screenshot time."""
        if os.name != 'nt':
            return {}
        try:
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            user32.GetForegroundWindow.restype = wintypes.HWND
            hwnd = user32.GetForegroundWindow()
            if not hwnd:
                return {}
            length = user32.GetWindowTextLengthW(hwnd)
            title = ''
            if length > 0:
                buffer = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buffer, length + 1)
                title = buffer.value.strip()
            return {'hwnd': int(hwnd), 'title': title}
        except Exception:
            return {}

    def window_bbox(self, hwnd: int) -> tuple[int, int, int, int] | None:
        if os.name != "nt":
            return None
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            user32.IsWindow.argtypes = [wintypes.HWND]
            user32.IsIconic.argtypes = [wintypes.HWND]
            user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
            if not user32.IsWindow(hwnd) or user32.IsIconic(hwnd):
                return None
            rect = wintypes.RECT()
            try:
                # DWM returns visible bounds in physical pixels, excluding the
                # invisible resize frame and slivers of the desktop behind it.
                get_bounds = ctypes.windll.dwmapi.DwmGetWindowAttribute
                get_bounds.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
                get_bounds.restype = ctypes.c_long
                if (get_bounds(hwnd, 9, ctypes.byref(rect), ctypes.sizeof(rect)) == 0
                        and rect.right > rect.left and rect.bottom > rect.top):
                    return rect.left, rect.top, rect.right, rect.bottom
            except (AttributeError, OSError):
                pass
            # Keep fallback coordinates physical without changing Tk's DPI mode.
            previous_context = None
            try:
                set_context = user32.SetThreadDpiAwarenessContext
                set_context.argtypes = [ctypes.c_void_p]
                set_context.restype = ctypes.c_void_p
                previous_context = set_context(ctypes.c_void_p(-4))
            except (AttributeError, OSError):
                pass
            try:
                if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                    return None
            finally:
                if previous_context:
                    set_context(previous_context)
            width = rect.right - rect.left
            height = rect.bottom - rect.top
            if width <= 0 or height <= 0:
                return None
            return rect.left, rect.top, rect.right, rect.bottom
        except Exception:
            return None

    def poll_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "event":
                    self.handle_event(payload)
                elif kind == "done":
                    self.handle_done(payload)
                elif kind == "error":
                    self.handle_error(payload)
                elif kind == "screen":
                    self.handle_screen_event(payload)
                elif kind == "screen_preview":
                    self.handle_screen_preview(*payload)
                elif kind == "live":
                    generation = payload.get("_generation")
                    if generation is None or generation == self.live_generation:
                        self.handle_live_event(payload)
        except queue.Empty:
            pass
        self.root.after(250, self.poll_events)

    def handle_screen_event(self, event: dict) -> None:
        if event.get("ok"):
            if event.get("stopped"):
                if not self.recording:
                    self.summary_var.set(f"Screen context finalized; imported {event.get('imported', 0)} manual screenshot(s)")
                elif not self.capture_screen_var.get():
                    self.summary_var.set("Screen capture paused; audio continues")
            else:
                self.capture_indicator.configure(text="● Screenshot saved", foreground="#15803d")
                self.capture_status_var.set(f"Last saved {datetime.now().strftime('%H:%M:%S')}")
                if event.get("path"):
                    self.load_capture_preview(Path(event["path"]))
                if self.capture_indicator_reset:
                    self.root.after_cancel(self.capture_indicator_reset)
                self.capture_indicator_reset = self.root.after(700, self.reset_capture_indicator)
                detail = ' · window filter metadata unavailable' if event.get('window_index_error') else ''
                action = "Manual screenshot saved" if event.get("manual") else "Captured screen context"
                self.summary_var.set(f"{action}: {basename(event.get('path'))}{detail}")
        else:
            message = event.get("message", "Screen context capture failed")
            self.summary_var.set(message)
            self.capture_status_var.set(message)
            self.capture_indicator.configure(
                text="● Screenshot skipped" if "skipped" in message.lower() else "● Screenshot error",
                foreground="#b45309" if "skipped" in message.lower() else "#b91c1c")

    def load_capture_preview(self, path: Path) -> None:
        key = str(path)
        self.capture_preview_key = key

        def worker() -> None:
            try:
                from PIL import Image
                with path.open("rb") as handle:
                    with Image.open(handle) as source:
                        source.draft("RGB", (120, 72))
                        source.thumbnail((120, 72), Image.Resampling.BILINEAR)
                        preview = source.convert("RGB")
                self.events.put(("screen_preview", (key, preview)))
            except (OSError, ValueError):
                self.events.put(("screen_preview", (key, None)))

        threading.Thread(target=worker, daemon=True).start()

    def handle_screen_preview(self, key: str, preview: object | None) -> None:
        if key != self.capture_preview_key:
            return
        if preview is None:
            self.capture_preview_photo = None
            self.capture_preview_path = None
            self.capture_preview_label.configure(image="", text="Preview unavailable")
            return
        self.capture_preview_photo = ImageTk.PhotoImage(preview, master=self.root)
        self.capture_preview_path = Path(key)
        self.capture_preview_label.configure(image=self.capture_preview_photo, text="")

    def reset_capture_indicator(self) -> None:
        self.capture_indicator.configure(text="● Screenshot", foreground="#6b7280")
        self.capture_indicator_reset = None

    def track_device_name(self, track: dict) -> str:
        return (track.get("device") or {}).get("name", "")

    def ui_kind_for_track(self, track: dict) -> str:
        return "speaker" if track.get("kind") == "system" else track.get("kind", "")

    def set_track_status(self, track: dict, recording: str, status: str, latest: str = "", level: str = "") -> None:
        kind = self.ui_kind_for_track(track)
        name = self.track_device_name(track)
        key = self.row_key(kind, name)
        row = self.device_rows.get(key)
        file_name = basename(track.get("path"))
        if row and self.devices.exists(row):
            values = list(self.devices.item(row, "values"))
            values[3] = recording
            values[4] = file_name
            values[5] = level
            values[6] = status
            if latest:
                values[7] = latest
            self.devices.item(row, values=values)
        else:
            self.upsert_device(kind, "", name, recording, file_name, status, latest, level)

    def handle_event(self, event: dict) -> None:
        if event.get("event") == "recording_started":
            self.active_tracks = event.get("tracks", [])
            for row in self.devices.get_children():
                values = list(self.devices.item(row, "values"))
                values[3] = "No"
                values[4] = ""
                values[5] = ""
                values[6] = "Idle"
                self.devices.item(row, values=values)
            for track in event.get("tracks", []):
                self.set_track_status(track, "Yes", "Recording")
            for failure in event.get("open_failures", []):
                kind = "speaker" if failure.get("kind") == "system" else failure.get("kind", "")
                name = failure.get("device", "")
                self.upsert_device(kind, "", name, "No", basename(failure.get("path")), f"Skipped: {failure.get('error', '')}")
            self.status_var.set("Recording")
            self.summary_var.set(f"Recording {len(event.get('tracks', []))} tracks")
        elif event.get("event") == "track_health":
            track_id = event.get("track_id")
            track = next((item for item in getattr(self, "active_tracks", []) if item.get("track_id") == track_id), None)
            if track:
                level = float(event.get("level_db", -120))
                seconds = float(event.get("seconds", 0))
                self.set_track_status(track, "Yes", f"Writing {seconds:.0f}s", level=f"{level:.1f} dB")
                self.summary_var.set(f"Recording — last audio write just now ({seconds:.0f}s)")
        elif event.get("event") == "recording_stopped":
            self.metadata = event
            self.status_var.set("Finalized")

    def handle_done(self, metadata: dict) -> None:
        self.stop_screen_context()
        self.metadata = metadata
        self.recording = False
        self.started_at = None
        self.start_button.configure(state=NORMAL)
        self.stop_button.configure(state=DISABLED)
        self.manual_screenshot_button.configure(state=DISABLED)
        self.elapsed_var.set("00:00:00")
        latest_text = fmt_session_time(self.current_out_dir)
        for track in metadata.get("tracks", []):
            self.set_track_status(track, "No", "Saved", latest_text)
        for failure in metadata.get("failures", []):
            kind = "speaker" if failure.get("kind") == "system" else failure.get("kind", "")
            prefix = "Stopped" if failure.get("stream") else "Skipped"
            self.upsert_device(kind, "", failure.get("device", ""), "No", basename(failure.get("path")), f"{prefix}: {failure.get('error', '')}", latest_text)
        self.status_var.set("Idle")
        if metadata.get("partial"):
            self.summary_var.set(
                f"Saved a partial recording with {len(metadata.get('failures', []))} failure(s) to {self.current_out_dir}"
            )
            self.process_var.set("Partial recording is saved and ready to process")
        else:
            self.summary_var.set(
                f"Saved {len(metadata.get('tracks', []))} tracks to {self.current_out_dir}"
            )
            self.process_var.set("Ready to process latest meeting")
        self.refresh_history()
        if self.selected_session == self.current_out_dir:
            self.load_saved_live_captions(self.current_out_dir)
        if self.close_after_stop:
            self.root.destroy()

    def handle_error(self, exc: Exception) -> None:
        self.stop_screen_context()
        self.recording = False
        self.started_at = None
        self.start_button.configure(state=NORMAL)
        self.stop_button.configure(state=DISABLED)
        self.manual_screenshot_button.configure(state=DISABLED)
        self.elapsed_var.set("00:00:00")
        self.refresh_history()
        if self.selected_session == self.current_out_dir:
            self.load_saved_live_captions(self.current_out_dir)
        self.status_var.set("Error")
        self.summary_var.set(str(exc))
        messagebox.showerror("Recording failed", str(exc))
        if self.close_after_stop:
            self.root.destroy()

    def tick(self) -> None:
        try:
            if self.recording and self.started_at:
                elapsed = int(time.time() - self.started_at)
                h, rem = divmod(elapsed, 3600)
                m, s = divmod(rem, 60)
                self.elapsed_var.set(f"{h:02d}:{m:02d}:{s:02d}")
            self.reconcile_processing_completion()
            if not self.processing_running and self.process_stage_var.get() in self.RUNNING_STAGE_LABELS:
                self.render_saved_processing_state()
            self.update_processing_timer()
        except Exception:
            self.log_ui_error("tick failed")
        finally:
            self.root.after(1000, self.tick)

    # Stage labels shown only while a run is active; seeing one with no run
    # active means the end-of-run render was interrupted.
    RUNNING_STAGE_LABELS = frozenset({
        "Preparing", "OCR + transcription", "Visual OCR", "Screenshot OCR", "Transcription",
        "Cleanup", "Notes", "Audio analysis", "Diagnostics", "Parallel", "Cancelling",
    })

    def render_saved_processing_state(self) -> None:
        """Re-render the Processing area from the selected meeting's saved files."""
        if self.processing_running:
            return
        self.active_progress_payload = {}
        for step in (self.update_processing_context, self.refresh_processing_details, self.update_processing_timer):
            try:
                step()
            except Exception:
                self.log_ui_error(f"rendering saved processing state failed in {step.__name__}")

    def log_ui_error(self, context: str, exc_info: tuple | None = None) -> None:
        try:
            log_path = Path(self.out_dir_var.get()) / "ui-errors.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as error_log:
                error_log.write(f"{datetime.now().isoformat()} {context}\n")
                if exc_info:
                    traceback.print_exception(*exc_info, file=error_log)
                else:
                    traceback.print_exc(file=error_log)
        except Exception:
            pass

    def log_callback_exception(self, exc_type, exc_value, exc_traceback) -> None:
        self.log_ui_error("Tk callback raised", (exc_type, exc_value, exc_traceback))

    @staticmethod
    def format_duration(seconds: float) -> str:
        hours, remainder = divmod(max(0, int(seconds)), 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    def update_processing_timer(self) -> None:
        elapsed = (time.monotonic() - self.processing_started_at
                   if self.processing_running and self.processing_started_at is not None
                   else self.processing_elapsed_seconds)
        elapsed = max(0, elapsed)
        if self.processing_running:
            # Early milestones are approximate, but always present a number.
            fraction = max(0.10, min(0.99, self.processing_estimate_percent / 100))
            estimated_total = elapsed / fraction
            remaining = max(0, estimated_total - elapsed)
        else:
            estimated_total, remaining = elapsed, 0
        self.processing_timer_var.set(
            f"Elapsed {self.format_duration(elapsed)}  ·  "
            f"Est. total ~{self.format_duration(estimated_total)}  ·  "
            f"Remaining ~{self.format_duration(remaining)}"
        )

    def reconcile_processing_completion(self) -> None:
        """Recover if the child exited but its final UI event was not applied."""
        worker = self.process_worker
        if not self.processing_running or worker is None or worker.is_alive() or self.processing_process is not None:
            return
        if not self.process_events.empty():
            return
        session = self.processing_session
        if not session:
            return
        state = read_json(session / "transcripts" / "process_state.json")
        try:
            updated_at = datetime.fromisoformat(str(state.get("updated_at", "")))
        except ValueError:
            return
        if not self.processing_started_wall_at or updated_at < self.processing_started_wall_at.replace(microsecond=0):
            return
        if state.get("status") == "complete":
            self.handle_process_done(0, json.dumps({"ok": True, "message": "Meeting processing complete"}), "")
        elif state.get("status") == "failed":
            self.handle_process_done(1, json.dumps({"ok": False, "error": state.get("error") or "Processing failed"}), "")

    def process_latest_meeting(self) -> None:
        """Legacy entry point retained for callers; visible UI always processes selection."""
        self.selected_session = self.refresh_latest()
        self.process_selected_meeting()

    def set_processing_controls(self, running: bool) -> None:
        if running and not self.processing_running:
            self.processing_started_at = time.monotonic()
            self.processing_started_wall_at = datetime.now()
            self.processing_elapsed_seconds = 0
            self.processing_estimate_percent = 0.0
        elif not running and self.processing_running and self.processing_started_at is not None:
            self.processing_elapsed_seconds = max(0, time.monotonic() - self.processing_started_at)
            self.processing_started_at = None
        self.processing_running = running
        if running:
            self.active_progress_payload = {"stage": "preparing", "progress": "Preparing meeting — checking saved work"}
            self.processing_session = self.selected_or_latest()
            self.notebook.select(self.stage_status.master)
            self.processing_bar.configure(mode="indeterminate")
            self.processing_bar.start(20)
        else:
            self.active_progress_payload = {}
            self.processing_bar.stop()
            self.processing_bar.configure(mode="determinate")
        self.process_selected_button.configure(state=DISABLED if running else NORMAL)
        self.regenerate_notes_button.configure(state=DISABLED if running else NORMAL)
        self.retry_process_button.configure(state=DISABLED)
        self.cancel_process_button.configure(state=NORMAL if running else DISABLED)
        self.include_screenshot_ocr_button.configure(state=DISABLED if running else NORMAL)
        self.retry_failed_ocr_button.configure(state=DISABLED)
        self.update_processing_context()
        self.refresh_processing_details()
        self.update_processing_timer()

    def retry_failed_ocr(self) -> None:
        if self.process_worker and self.process_worker.is_alive():
            return
        session = self.selected_or_latest()
        if not session:
            messagebox.showinfo("No meeting", "No captured meeting found.")
            return
        failed = self.ocr_counts(session)["failed"]
        if not failed:
            messagebox.showinfo("OCR complete", "This meeting has no failed OCR images.")
            return
        self.set_processing_controls(True)
        self.processing_cancelled = False
        self.active_progress_payload = {}
        self.process_progress_var.set(12)
        self.process_stage_var.set("Screenshot OCR")
        self.process_var.set(f"Retrying {failed} failed OCR image(s); successful images and transcripts will be reused...")
        self.notebook.select(self.stage_status.master)
        self.process_worker = threading.Thread(target=self._process_worker, args=(session, True, False,
            "openrouter" if self.ocr_backend_var.get() == "Cloud (OpenRouter)" else "local",
            "openrouter" if self.transcription_backend_var.get() == "Cloud (OpenRouter)" else "local"), daemon=True)
        self.process_worker.start()

    def process_selected_meeting(self) -> None:
        if self.process_worker and self.process_worker.is_alive():
            return
        session = self.selected_or_latest()
        if not session:
            messagebox.showinfo("No meeting", "No captured meeting found.")
            return
        skip_ocr = not self.include_screenshot_ocr_var.get()
        self.set_processing_controls(True)
        self.processing_cancelled = False
        self.active_progress_payload = {}
        self.process_progress_var.set(0)
        self.process_stage_var.set("Preparing")
        suffix = " Screenshot OCR will be skipped." if skip_ocr else " Selected screenshots will be OCR'd."
        self.process_var.set(f"Processing {self.session_title(session)}.{suffix}")
        self.process_worker = threading.Thread(target=self._process_worker, args=(session, False, skip_ocr,
            "openrouter" if self.ocr_backend_var.get() == "Cloud (OpenRouter)" else "local",
            "openrouter" if self.transcription_backend_var.get() == "Cloud (OpenRouter)" else "local"), daemon=True)
        self.process_worker.start()

    def regenerate_selected_notes(self) -> None:
        if self.process_worker and self.process_worker.is_alive():
            return
        session = self.selected_or_latest()
        if not session:
            messagebox.showinfo("No meeting", "No captured meeting found.")
            return
        self.set_processing_controls(True)
        self.processing_cancelled = False
        self.active_progress_payload = {}
        self.process_progress_var.set(84)
        self.process_stage_var.set("Notes")
        self.process_var.set(f"Regenerating notes for {session.name}; existing manual notes will be preserved...")
        self.process_worker = threading.Thread(target=self._process_worker, args=(session, True, True,
            "openrouter" if self.ocr_backend_var.get() == "Cloud (OpenRouter)" else "local",
            "openrouter" if self.transcription_backend_var.get() == "Cloud (OpenRouter)" else "local"), daemon=True)
        self.process_worker.start()

    def _process_worker(self, session: Path | None, force: bool = False, skip_ocr: bool = False,
                        ocr_backend: str = "local", transcription_backend: str = "local") -> None:
        cmd = [
            sys.executable,
            str(Path(__file__).with_name("process_latest.py")),
        ]
        if session:
            cmd.append(str(session))
        else:
            cmd.append("--latest")
        if force:
            cmd.append("--force")
        if skip_ocr:
            cmd.append("--skip-ocr")
        cmd.extend(("--ocr-backend", ocr_backend, "--transcription-backend", transcription_backend))
        try:
            env = os.environ.copy()
            env["MNT_PROGRESS"] = "1"
            # Resolve the vault secret before starting OCR/Whisper.  Besides
            # providing a reliable child-process handoff, this makes an
            # unavailable credential fail immediately rather than after the
            # expensive local work has finished.
            if not self.openrouter_key:
                key, error = self._read_openrouter_secret()
                if not key:
                    raise RuntimeError(
                        "OpenRouter could not be unlocked before processing. "
                        "Verify the local DevSecrets setup and try again."
                        + (f" ({error})" if error else "")
                    )
                self.openrouter_key = key
            env["OPENROUTER_API_KEY"] = self.openrouter_key
            if self.processing_cancelled:
                self.process_events.put(("done", 1, "", ""))
                return
            process = subprocess.Popen(cmd, text=True, encoding="utf-8", errors="replace", stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
            self.processing_process = process
            final_line = ""
            assert process.stdout is not None
            for line in process.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                    if "progress" in payload:
                        self.process_events.put(("progress", payload))
                    else:
                        final_line = line
                except Exception:
                    self.process_events.put(("progress", line))
            returncode = process.wait()
            self.processing_process = None
            self.process_events.put(("done", returncode, final_line, ""))
        except Exception as exc:
            self.process_events.put(("error", exc))

    def poll_process_events(self) -> None:
        try:
            while True:
                event = self.process_events.get_nowait()
                if event[0] == 'ocr_preview':
                    if event[1] == self.ocr_preview_key:
                        if event[2] is not None:
                            from PIL import ImageTk
                            self.ocr_preview_photo = ImageTk.PhotoImage(event[2], master=self.root)
                            self.ocr_preview_path = event[3]
                            self.ocr_preview_button.configure(image=self.ocr_preview_photo, text='')
                        else:
                            self.ocr_preview_button.configure(image='', text='Preview unavailable')
                elif event[0] == "done":
                    _kind, returncode, stdout, stderr = event
                    self.handle_process_done(returncode, stdout, stderr)
                elif event[0] == "error":
                    self.handle_process_error(event[1])
                elif event[0] == "diagnostics_done":
                    _kind, returncode, stdout, stderr = event
                    if returncode == 0:
                        self.refresh_track_review()
                        self.process_var.set("Track activity analysis complete")
                    else:
                        self.process_var.set(stderr.strip() or stdout.strip() or "Track analysis failed")
                elif event[0] == "progress":
                    payload = event[1]
                    if not isinstance(payload, dict):
                        self.process_var.set(str(payload))
                        continue
                    parallel_stages = payload.get("parallel_stages") or {}
                    if parallel_stages:
                        parts = [parallel_stages[key].get("progress", key.title())
                                 for key in ("ocr", "audio") if key in parallel_stages]
                        self.process_var.set("  |  ".join(parts))
                    else:
                        self.process_var.set(str(payload.get("progress", "Working")))
                    self.active_progress_payload = payload
                    stage = payload.get("stage")
                    if stage:
                        label = ("OCR + transcription" if parallel_stages or stage == "parallel" else
                                 {"diagnostics": "Audio analysis", "ocr": "Visual OCR", "transcript": "Transcription", "cleanup": "Cleanup", "notes": "Notes", "complete": "Complete"}.get(stage, str(stage).title()))
                        self.process_stage_var.set(label)
                    if isinstance(payload.get("percent"), (int, float)):
                        self.process_progress_var.set(payload["percent"])
                        self.processing_estimate_percent = max(self.processing_estimate_percent, float(payload["percent"]))
                    elif payload.get("stage") == "ocr" and payload.get("total"):
                        self.process_progress_var.set(12 + 33 * float(payload.get("current", 0)) / float(payload["total"]))
                        self.processing_estimate_percent = max(self.processing_estimate_percent, float(self.process_progress_var.get()))
                    measured = isinstance(payload.get("item_percent"), (int, float)) or (stage == "ocr" and payload.get("current") and payload.get("total")) or stage == "complete"
                    mode = "determinate" if measured else "indeterminate"
                    if str(self.processing_bar.cget("mode")) != mode:
                        self.processing_bar.stop()
                        self.processing_bar.configure(mode=mode)
                        if not measured:
                            self.processing_bar.start(20)
                    self.refresh_processing_details()
                    self.update_processing_context()
        except queue.Empty:
            pass
        except Exception:
            # A transient rendering error must not permanently stop the event
            # pump and strand a completed meeting in the Processing state.
            self.log_ui_error("process event rendering failed")
        finally:
            self.root.after(100, self.poll_process_events)

    def handle_process_done(self, returncode: int, stdout: str, stderr: str) -> None:
        try:
            self._handle_process_done(returncode, stdout, stderr)
        finally:
            # Whatever happened above, finish by rendering the saved meeting
            # state so a mid-run display can never outlive the run.
            self.processing_running = False
            self.root.after(0, self.render_saved_processing_state)

    def _handle_process_done(self, returncode: int, stdout: str, stderr: str) -> None:
        try:
            self.set_processing_controls(False)
        except Exception:
            self.processing_running = False
            self.log_ui_error("ending processing controls failed")
        self.active_progress_payload = {}
        text = stdout.strip() or stderr.strip()
        try:
            payload = json.loads(text)
        except Exception:
            payload = {"ok": returncode == 0, "message": text}
        if returncode == 0 and payload.get("ok"):
            message = payload.get("message")
            if not message:
                message = f"Processed latest meeting with {payload.get('model', 'OpenRouter')}"
            self.process_var.set(message)
            self.process_stage_var.set("Complete")
            self.process_progress_var.set(100)
            self.refresh_history()
            self.load_selected_notes()
            self.refresh_openrouter_balance()
        else:
            message = "Processing cancelled; completed artifacts were kept. You can retry processing." if self.processing_cancelled else (payload.get("error") or stderr.strip() or "Processing failed")
            self.process_var.set(message)
            self.process_stage_var.set("Cancelled" if self.processing_cancelled else "Failed")
            self.retry_process_button.configure(state=NORMAL)
            if not self.processing_cancelled:
                self.notebook.select(self.stage_status.master)
                messagebox.showerror(
                    "Processing failed",
                    message
                    + "\n\nThe Processing tab shows the failed stage and saved item details. "
                    "Resume processing will reuse completed OCR and transcript stages.",
                )
            self.refresh_processing_details()

    def handle_process_error(self, exc: Exception) -> None:
        self.set_processing_controls(False)
        self.active_progress_payload = {}
        self.retry_process_button.configure(state=NORMAL)
        self.process_var.set(str(exc))
        self.notebook.select(self.stage_status.master)
        messagebox.showerror(
            "Processing failed",
            str(exc)
            + "\n\nThe Processing tab shows the failed stage and saved item details. "
            "Resume processing will reuse completed OCR and transcript stages.",
        )
        self.refresh_processing_details()

    def cancel_processing(self) -> None:
        process = self.processing_process
        if self.processing_running and not process:
            self.processing_cancelled = True
            self.process_var.set("Cancelling preparation; saved work is unchanged...")
            return
        if not process or process.poll() is not None:
            return
        self.processing_cancelled = True
        self.process_stage_var.set("Cancelling")
        self.process_var.set("Stopping processing; completed artifacts will be kept...")
        self.cancel_process_button.configure(state=DISABLED)
        process.terminate()

    def schedule_startup_balance_check(self) -> None:
        self.root.after(1500, self.refresh_openrouter_balance)

    def refresh_openrouter_balance(self) -> None:
        """Fetch the OpenRouter balance off the Tk thread; the key never reaches the UI."""
        if self.balance_thread and self.balance_thread.is_alive():
            return
        self.openrouter_balance_var.set("OpenRouter balance: checking...")
        self.refresh_balance_button.configure(state=DISABLED)
        self.balance_result = None

        def worker() -> None:
            try:
                if not self.openrouter_key:
                    key, error = self._read_openrouter_secret()
                    if not key:
                        self.balance_result = (None, "key unavailable")
                        return
                    self.openrouter_key = key
                self.balance_result = (fetch_openrouter_balance(self.openrouter_key), None)
            except Exception as exc:
                status = getattr(exc, "code", None)
                self.balance_result = (None, f"HTTP {status}" if status else type(exc).__name__)

        self.balance_thread = threading.Thread(target=worker, daemon=True)
        self.balance_thread.start()
        self.root.after(200, self.poll_openrouter_balance)

    def poll_openrouter_balance(self) -> None:
        if self.balance_result is None:
            if self.balance_thread and self.balance_thread.is_alive():
                self.root.after(200, self.poll_openrouter_balance)
            return
        balance, error = self.balance_result
        self.balance_result = None
        checked = datetime.now().strftime("%I:%M %p").lstrip("0")
        if balance is None:
            self.openrouter_balance_var.set(f"OpenRouter balance unavailable ({error})")
        else:
            self.openrouter_balance_var.set(f"OpenRouter balance: ${balance:,.2f} (checked {checked})")
        self.refresh_balance_button.configure(state=NORMAL)

    def _read_openrouter_secret(self) -> tuple[str | None, str | None]:
        secret_name = os.environ.get("OPENROUTER_SECRET_NAME", "OPENROUTER_API_KEY")
        # DevSecrets lives in PowerShell 7.  Do not fall back to legacy
        # powershell.exe: it cannot load Unlock-SecretStore on this machine.
        pwsh = shutil.which("pwsh.exe")
        if not pwsh:
            return None, "PowerShell 7 (pwsh.exe) is not available to unlock DevSecrets."
        script = (
            "$ErrorActionPreference='Stop'; "
            ". (Join-Path $HOME '.config\\powershell\\profile.ps1'); "
            "Unlock-DevSecrets; "
            f"$secret=Get-Secret -Name '{secret_name}' -Vault 'DevSecrets' -AsPlainText; "
            "[Console]::Out.Write($secret)"
        )
        env = os.environ.copy()
        try:
            result = subprocess.run(
                [pwsh, "-NoProfile", "-NonInteractive", "-Command", script],
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=20,
                env=env,
                check=False,
            )
        except Exception as exc:
            return None, f"Could not start DevSecrets: {exc}"
        if result.returncode != 0 or not result.stdout.strip():
            detail = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else None
            return None, detail or f"DevSecrets could not provide {secret_name}"
        return result.stdout.strip(), None

    def open_path(self, path: Path) -> None:
        if os.name == "nt":
            os.startfile(str(path))
        else:
            subprocess.Popen(["explorer.exe", str(path)])

    def open_latest_folder(self) -> None:
        path = self.current_out_dir or self.refresh_latest() or Path(self.out_dir_var.get())
        Path(path).mkdir(parents=True, exist_ok=True)
        self.open_path(Path(path))

    def open_selected_folder(self) -> None:
        path = self.selected_or_latest()
        if not path:
            messagebox.showinfo("No meeting", "No captured meeting found.")
            return
        self.open_path(path)

    def open_latest_file(self, relative: str) -> None:
        latest = self.refresh_latest()
        if not latest:
            messagebox.showinfo("No meeting", "No captured meeting found.")
            return
        path = latest / relative
        if not path.exists():
            messagebox.showinfo("File not found", f"{path} does not exist yet.")
            return
        self.open_path(path)

    def open_selected_file(self, relative: str) -> None:
        session = self.selected_or_latest()
        if not session:
            messagebox.showinfo("No meeting", "No captured meeting found.")
            return
        path = session / relative
        if not path.exists():
            messagebox.showinfo("File not found", f"{path} does not exist yet.")
            return
        self.open_path(path)

    def open_diagnostics(self) -> None:
        session = self.selected_or_latest()
        if not session:
            messagebox.showinfo("No meeting", "No captured meeting found.")
            return
        path = session / "transcripts" / "diagnostics.md"
        if not path.exists():
            self.process_var.set("Generating diagnostics...")
            cmd = [
                sys.executable,
                str(Path(__file__).with_name("process_latest.py")),
                str(session),
                "--diagnostics-only",
            ]
            completed = subprocess.run(cmd, text=True, capture_output=True, check=False)
            if completed.returncode != 0:
                message = completed.stderr.strip() or completed.stdout.strip() or "Diagnostics generation failed"
                self.process_var.set(message)
                messagebox.showerror("Diagnostics failed", message)
                return
        self.open_path(path)

    def on_close(self) -> None:
        if self.recording:
            if not messagebox.askyesno("Stop recording?", "Stop recording and close the app?"):
                return
            self.close_after_stop = True
            self.stop()
        else:
            self.root.destroy()


def main() -> int:
    mutex, already_running = acquire_single_instance()
    if already_running:
        ctypes.windll.user32.MessageBoxW(
            None,
            "Meeting Notes Capture is already running. Use the existing window to record or process meetings.",
            "Meeting Notes Capture",
            0x40,
        )
        return 0
    try:
        root = tk.Tk()
        CaptureApp(root)
        root.mainloop()
        return 0
    finally:
        release_single_instance(mutex)


if __name__ == "__main__":
    raise SystemExit(main())
