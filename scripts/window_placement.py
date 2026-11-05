"""Place desktop dialogs relative to their parent window."""


def centered_near_parent(parent_rect, dialog_size, desktop_rect):
    px, py, pw, ph = parent_rect
    width, height = dialog_size
    vx, vy, vw, vh = desktop_rect
    x = max(vx, min(px + (pw - width) // 2, vx + vw - width))
    y = max(vy, min(py + (ph - height) // 2, vy + vh - height))
    return f"{width}x{height}+{x}+{y}"


def place_dialog_near_parent(dialog, parent, width: int, height: int) -> None:
    parent.update_idletasks()
    parent_rect = (parent.winfo_rootx(), parent.winfo_rooty(), parent.winfo_width(), parent.winfo_height())
    desktop_rect = (parent.winfo_vrootx(), parent.winfo_vrooty(),
                    parent.winfo_vrootwidth(), parent.winfo_vrootheight())
    dialog.geometry(centered_near_parent(parent_rect, (width, height), desktop_rect))
