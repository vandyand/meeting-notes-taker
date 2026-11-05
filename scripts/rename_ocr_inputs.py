"""Upgrade edited OCR filenames without invoking OCR or changing captured media."""
import argparse
import json
import uuid
from pathlib import Path
import screenshot_edits


def rename_saved_inputs(session):
    checkpoint = session / 'visual' / 'ocr.json'
    data = json.loads(checkpoint.read_text(encoding='utf-8'))
    edits = screenshot_edits.load_edits(session)
    changed = 0
    for result in data.get('results', []):
        if not result.get('path') or not result.get('image_fingerprint'):
            continue
        original = Path(result['path'])
        if not original.exists() or result['image_fingerprint'] != screenshot_edits.fingerprint(session, original, edits):
            continue
        target = screenshot_edits.ocr_input(session, original, edits)
        if result.get('ocr_input_path') != str(target):
            result['ocr_input_path'] = str(target)
            changed += 1
    if changed:
        temporary = checkpoint.with_name('ocr-' + uuid.uuid4().hex + '.tmp')
        try:
            temporary.write_text(json.dumps(data, indent=2), encoding='utf-8')
            temporary.replace(checkpoint)
        finally:
            temporary.unlink(missing_ok=True)
    return changed


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('session', type=Path)
    args = parser.parse_args()
    print(f'Updated {rename_saved_inputs(args.session)} OCR input filenames; no model calls.')
