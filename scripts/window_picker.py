"""Fast, best-effort browser-first window listing and native icon loading."""

from __future__ import annotations

import os


BROWSER_PROCESSES = {
    'chrome.exe', 'msedge.exe', 'brave.exe', 'firefox.exe',
    'opera.exe', 'vivaldi.exe', 'arc.exe', 'chromium.exe',
    'waterfox.exe', 'librewolf.exe', 'tor.exe',
}
BROWSER_TITLE_SUFFIXES = (
    ' - Google Chrome', ' - Microsoft Edge', ' - Brave',
    ' - Mozilla Firefox', ' - Opera', ' - Vivaldi', ' - Arc',
)


def window_process_name(hwnd: int) -> str:
    if os.name != 'nt':
        return ''
    try:
        import psutil
        import win32process
        _thread_id, process_id = win32process.GetWindowThreadProcessId(hwnd)
        return psutil.Process(process_id).name().casefold()
    except Exception:
        return ''


def is_browser_window(window: dict) -> bool:
    name = str(window.get('process_name') or '').casefold()
    if name:
        return name in BROWSER_PROCESSES
    return str(window.get('title') or '').endswith(BROWSER_TITLE_SUFFIXES)


def sort_window_choices(windows: list[dict]) -> list[dict]:
    return sorted(windows, key=lambda item: (
        0 if is_browser_window(item) else 1,
        str(item.get('title') or '').casefold(),
        int(item.get('hwnd') or 0),
    ))


def render_hicon(hicon: int):
    """Copy a borrowed HICON into a small RGB image on a white background."""
    from PIL import Image
    import win32con
    import win32gui
    import win32ui

    screen_dc = win32gui.GetDC(0)
    dc = win32ui.CreateDCFromHandle(screen_dc)
    memory_dc = dc.CreateCompatibleDC()
    bitmap = win32ui.CreateBitmap()
    bitmap.CreateCompatibleBitmap(dc, 20, 20)
    previous = memory_dc.SelectObject(bitmap)
    try:
        memory_dc.FillSolidRect((0, 0, 20, 20), 0xFFFFFF)
        win32gui.DrawIconEx(memory_dc.GetSafeHdc(), 0, 0, hicon, 20, 20,
                            0, None, win32con.DI_NORMAL)
        return Image.frombuffer('RGB', (20, 20), bitmap.GetBitmapBits(True),
                                'raw', 'BGRX', 0, 1).copy()
    finally:
        memory_dc.SelectObject(previous)
        memory_dc.DeleteDC()
        win32gui.DeleteObject(bitmap.GetHandle())
        win32gui.ReleaseDC(0, screen_dc)


def window_icon_image(hwnd: int):
    """Return the window's own icon, or None without delaying the chooser."""
    if os.name != 'nt':
        return None
    try:
        import win32con
        import win32gui
        for size in (2, win32con.ICON_SMALL, win32con.ICON_BIG):
            try:
                ok, icon = win32gui.SendMessageTimeout(
                    hwnd, win32con.WM_GETICON, size, 0, win32con.SMTO_ABORTIFHUNG, 80)
            except Exception:
                continue
            if ok and icon:
                return render_hicon(icon)
        for kind in (win32con.GCL_HICONSM, win32con.GCL_HICON):
            icon = win32gui.GetClassLong(hwnd, kind)
            if icon:
                return render_hicon(icon)
    except Exception:
        pass
    return None
