"""Non-destructive, normalized screenshot crops and blackout rectangles."""
from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from PIL import Image, ImageDraw


def path_key(path):
    return str(Path(path).resolve()).casefold()


def load_edits(session: Path) -> dict:
    try:
        return json.loads((session / 'screenshots' / 'edits.json').read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {}


def effective_edit(session: Path, path: Path, edits=None) -> dict:
    edits = load_edits(session) if edits is None else edits
    return (edits.get('individual') or {}).get(path_key(path), edits.get('global') or {})


def rect_pixels(rect, size):
    if len(rect) != 4 or not all(isinstance(value, (int, float)) and 0 <= value <= 1 for value in rect):
        raise ValueError('Crop/mask coordinates must be four values between 0 and 1.')
    left, top, right, bottom = rect
    if right <= left or bottom <= top:
        raise ValueError('Select a non-empty crop/mask rectangle.')
    width, height = size
    return (min(width - 1, int(left * width)), min(height - 1, int(top * height)),
            max(1, round(right * width)), max(1, round(bottom * height)))


def apply_edit(image: Image.Image, edit: dict) -> Image.Image:
    result = image.convert('RGB')
    for mask in edit.get('masks') or []:
        left, top, right, bottom = rect_pixels(mask, result.size)
        ImageDraw.Draw(result).rectangle((left, top, right - 1, bottom - 1), fill='black')
    if edit.get('crop'):
        result = result.crop(rect_pixels(edit['crop'], result.size))
    return result


def load_image(session: Path, path: Path, edits=None) -> Image.Image:
    with Image.open(path) as source:
        return apply_edit(source, effective_edit(session, path, edits))


def fingerprint(session: Path, path: Path, edits=None) -> str | None:
    edit = effective_edit(session, path, edits)
    if not edit.get('crop') and not edit.get('masks'):
        return None  # Preserve legacy, unedited OCR checkpoints.
    stat = path.stat()
    value = [1, stat.st_size, stat.st_mtime_ns, edit]
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def input_path(session: Path, path: Path, edits=None) -> Path:
    """The OCR filename, without decoding or writing an image."""
    token = fingerprint(session, path, edits)
    if token is None:
        return path
    revision = hashlib.sha256((path_key(path) + token).encode()).hexdigest()[:16]
    edit = effective_edit(session, path, edits)
    suffix = 'cropped-masked' if edit.get('crop') and edit.get('masks') else ('cropped' if edit.get('crop') else 'masked')
    return session / 'screenshots' / '.ocr-inputs' / f'{path.stem}-{suffix}-{revision}.png'


def ocr_input(session: Path, path: Path, edits=None) -> Path:
    target = input_path(session, path, edits)
    if target == path:
        return path
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        token = fingerprint(session, path, edits)
        legacy = target.parent / (hashlib.sha256((path_key(path) + token).encode()).hexdigest() + '.png')
        temporary = target.with_name(target.stem + '-' + uuid.uuid4().hex + '.tmp.png')
        try:
            if legacy.exists():
                # Retain old files referenced by existing artifacts/checkpoints.
                import shutil
                shutil.copyfile(legacy, temporary)
            else:
                load_image(session, path, edits).save(temporary)
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
    return target


def summary(session: Path) -> str:
    edits = load_edits(session)
    global_edit = edits.get('global') or {}
    crop = global_edit.get('crop')
    crop_text = 'No global crop'
    if crop:
        left, top, right, bottom = crop
        crop_text = f'Global crop applied ({(right-left)*(bottom-top):.0%} area kept)'
    masks = len(global_edit.get('masks') or [])
    overrides = len(edits.get('individual') or {})
    return f'{crop_text} · {masks} global blackout(s) · {overrides} individual override(s)'


def save_edit(session: Path, path: Path | None, edit: dict | None) -> None:
    edits = load_edits(session)
    if path is None:
        edits['global'] = edit or {}
    elif edit is None:
        (edits.get('individual') or {}).pop(path_key(path), None)
    else:
        edits.setdefault('individual', {})[path_key(path)] = edit
    _write_edits(session, edits)


def promote_edit(session: Path, path: Path, edit: dict) -> None:
    """Make an image's draft the global edit and remove that image's override."""
    edits = load_edits(session)
    edits['global'] = edit
    (edits.get('individual') or {}).pop(path_key(path), None)
    _write_edits(session, edits)


def _write_edits(session: Path, edits: dict) -> None:
    target = session / 'screenshots' / 'edits.json'
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix('.tmp')
    temporary.write_text(json.dumps(edits, indent=2), encoding='utf-8')
    temporary.replace(target)
