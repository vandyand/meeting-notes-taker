"""Shared screenshot analysis cache based on original captures, not OCR edits."""
import json
import uuid
from pathlib import Path


def comparison_policy(session):
    """Legacy GetWindowRect captures can contain eight pixels of desktop fringe."""
    try:
        context = json.loads((session / 'screenshots' / 'screen_context.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        context = {}
    scope = context.get('scope', '')
    legacy = (scope == 'Active window' or scope.startswith('Selected window')) and context.get('window_bounds_version', 1) == 1
    try:
        mode = json.loads((session / 'screenshots' / 'duplicate_policy.json').read_text(encoding='utf-8')).get('mode')
    except (OSError, ValueError):
        mode = None
    mode = mode if mode in ('Strict', 'Near duplicates', 'Aggressive') else 'Aggressive'
    return {'version': 4, 'legacy_border_pixels': 8 if legacy else 0,
            'mode': mode,
            'near_duplicate_fraction': .0002 if mode == 'Near duplicates' else 0,
            'perceptual_distance': 8 if mode == 'Aggressive' else None}


def save_duplicate_mode(session, mode):
    if mode not in ('Strict', 'Near duplicates', 'Aggressive'):
        raise ValueError('Choose Strict, Near duplicates, or Aggressive.')
    target = session / 'screenshots' / 'duplicate_policy.json'
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name('duplicate-policy-' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_text(json.dumps({'mode': mode}), encoding='utf-8')
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def comparison_options(session, edits):
    from PIL import Image, ImageDraw
    policy = comparison_policy(session)
    border = policy['legacy_border_pixels']
    options = {'near_duplicate_fraction': policy['near_duplicate_fraction'],
               'perceptual_distance': policy['perceptual_distance']}
    if not border:
        return options

    def load(path):
        with Image.open(path) as source:
            image = source.convert('RGB')
        # Ignore capture fringe without changing the original or OCR input.
        if (border and path.parent.resolve() == (session / 'screenshots').resolve()
                and path.name.startswith('screen-') and min(image.size) > 2 * border):
            width, height = image.size
            draw = ImageDraw.Draw(image)
            for rectangle in ((0, 0, width - 1, border - 1),
                              (0, height - border, width - 1, height - 1),
                              (0, 0, border - 1, height - 1),
                              (width - border, 0, width - 1, height - 1)):
                draw.rectangle(rectangle, fill='black')
        return image
    return dict(options, image_loader=load)


def source_signature(images):
    return [[str(path.resolve()), path.stat().st_size, path.stat().st_mtime_ns] for path in images]


def manual_frames(session, images, edits):
    """A full manual review needs no duplicate scan if captures are unchanged."""
    import screenshot_edits
    try:
        saved = json.loads((session / 'screenshots' / 'manifest.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    by_path = {screenshot_edits.path_key(frame['path']): frame
               for frame in saved.get('frames', []) if isinstance(frame, dict) and frame.get('path')}
    frames = []
    for path in images:
        frame = by_path.get(screenshot_edits.path_key(path), {})
        stat = path.stat()
        if (not isinstance(frame.get('manual_selected'), bool)
                or frame.get('manual_source') != [stat.st_size, stat.st_mtime_ns]):
            return None
        frames.append(dict(frame, selected=frame['manual_selected']))
    return frames


def analyzed_frames(session, images, edits, selector, progress):
    signature = source_signature(images)
    policy = comparison_policy(session)
    cache = session / 'screenshots' / '.review-cache' / 'analysis.json'
    try:
        saved = json.loads(cache.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        saved = {}
    frames = saved.get('frames')
    raw_cache = saved.get('version') == 4
    if (raw_cache and saved.get('policy') == policy and saved.get('sources') == signature
            and isinstance(frames, list)
            and len(frames) == len(images)
            and all(isinstance(frame, dict) and frame.get('path') == str(path)
                    and 'automatic_selected' in frame for frame, path in zip(frames, images))):
        return frames
    _, frames = selector(images, on_progress=progress)
    temporary = cache.with_name('analysis-' + uuid.uuid4().hex + '.tmp')
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps({'version': 4, 'policy': policy, 'sources': signature,
                                         'frames': frames}), encoding='utf-8')
        temporary.replace(cache)
    except OSError:
        pass
    finally:
        temporary.unlink(missing_ok=True)
    return frames
