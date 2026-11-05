"""Remove a meeting screenshot and image-derived review/OCR data."""
from __future__ import annotations

import json
import hashlib
import re
import uuid
from pathlib import Path

import screenshot_edits
import windows_ocr


def delete_screenshot(session: Path, image: Path) -> None:
    root = (session / 'screenshots').resolve()
    image = image.resolve()
    if image.parent not in (root, root / 'imported') or image.suffix.lower() not in {'.png', '.jpg', '.jpeg', '.webp'}:
        raise ValueError('Only an image inside this meeting’s screenshots folder can be deleted.')
    if not image.is_file():
        raise FileNotFoundError(image)
    key = screenshot_edits.path_key(image)
    manifest_path = root / 'manifest.json'
    ocr_path = session / 'visual' / 'ocr.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8')) if manifest_path.exists() else {}
    ocr = json.loads(ocr_path.read_text(encoding='utf-8')) if ocr_path.exists() else {}
    frames = [frame for frame in manifest.get('frames', [])
              if screenshot_edits.path_key(frame['path']) != key]
    results = [result for result in ocr.get('results', [])
               if screenshot_edits.path_key(result['path']) != key]
    removed_results = [result for result in ocr.get('results', [])
                       if screenshot_edits.path_key(result['path']) == key]
    inputs = root / '.ocr-inputs'
    derived = {screenshot_edits.input_path(session, image)}
    token = screenshot_edits.fingerprint(session, image)
    if token:
        derived.add(inputs / (hashlib.sha256((key + token).encode()).hexdigest() + '.png'))
    for result in removed_results:
        if result.get('ocr_input_path'):
            derived.add(Path(result['ocr_input_path']))
    # Older revisions are named by source stem. A same-stem survivor may lose
    # its disposable OCR copy; its original remains and the copy can regenerate.
    if inputs.exists():
        derived.update(inputs.glob(image.stem + '-*.png'))
    # Early versions named OCR copies by an opaque hash, so their source cannot
    # be determined later. Purge all of those legacy cache files on deletion.
    if inputs.exists():
        derived.update(path for path in inputs.glob('*.png')
                       if re.fullmatch(r'[0-9a-f]{64}\.png', path.name))

    staged = image.with_name('.deleting-' + uuid.uuid4().hex)
    image.replace(staged)
    try:
        if ocr_path.exists():
            windows_ocr.write_outputs(session, ocr.get('model', ''), results, frames,
                                      ocr.get('requested_context_length'))
        elif manifest_path.exists():
            manifest.update(frames=frames, source_image_count=len(frames),
                            selected_image_count=sum(bool(frame.get('selected')) for frame in frames))
            temporary = manifest_path.with_suffix('.tmp')
            temporary.write_text(json.dumps(manifest, indent=2), encoding='utf-8')
            temporary.replace(manifest_path)
        edits = screenshot_edits.load_edits(session)
        if key in (edits.get('individual') or {}):
            screenshot_edits.save_edit(session, image, None)
        index_path = root / 'capture_windows.jsonl'
        if image.parent == root and index_path.exists():
            lines = [line for line in index_path.read_text(encoding='utf-8').splitlines()
                     if not _record_matches(line, image.name)]
            temporary = index_path.with_suffix('.tmp')
            temporary.write_text('\n'.join(lines) + ('\n' if lines else ''), encoding='utf-8')
            temporary.replace(index_path)
        (root / '.review-cache' / 'analysis.json').unlink(missing_ok=True)
        thumbnails = root / '.review-cache' / 'thumbnails'
        if thumbnails.exists():
            for thumbnail in thumbnails.glob('*.jpg'):
                thumbnail.unlink()
        for path in derived:
            if path.resolve() != image and path.resolve().parent == inputs.resolve():
                path.unlink(missing_ok=True)
    except Exception:
        staged.replace(image)
        raise
    staged.unlink()


def _record_matches(line: str, name: str) -> bool:
    try:
        return json.loads(line).get('image') == name
    except (ValueError, AttributeError):
        return False
