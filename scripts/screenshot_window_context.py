"""Per-screenshot foreground-window metadata for non-destructive review filters."""
from __future__ import annotations

import json
from pathlib import Path


INDEX_NAME = 'capture_windows.jsonl'
UNKNOWN = 'unknown'


def append_capture(session: Path, image: Path, active_window: dict) -> None:
    """Record the foreground window seen when an automatic screenshot was taken."""
    target = session / 'screenshots' / INDEX_NAME
    target.parent.mkdir(parents=True, exist_ok=True)
    record = {'image': image.name,
              'active_window_hwnd': active_window.get('hwnd'),
              'active_window_title': active_window.get('title') or ''}
    with target.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(record, ensure_ascii=False) + '\n')


def window_groups(session: Path, frames: list[dict]) -> tuple[dict[str, str], dict[str, str]]:
    """Return original image path → window group and human-readable group labels."""
    records = {}
    try:
        with (session / 'screenshots' / INDEX_NAME).open(encoding='utf-8') as stream:
            for line in stream:
                try:
                    item = json.loads(line)
                    name = item.get('image')
                    if isinstance(name, str) and Path(name).name == name:
                        records[str((session / 'screenshots' / name).resolve()).casefold()] = item
                except (ValueError, AttributeError):
                    continue
    except OSError:
        pass
    groups: dict[str, str] = {}
    titles: dict[str, set[str]] = {}
    for frame in frames:
        path = frame['path']
        item = records.get(str(Path(path).resolve()).casefold(), {})
        hwnd = item.get('active_window_hwnd')
        group = f'hwnd:{hwnd}' if isinstance(hwnd, int) and hwnd > 0 else UNKNOWN
        groups[path] = group
        title = item.get('active_window_title')
        if isinstance(title, str) and title.strip():
            titles.setdefault(group, set()).add(title.strip())
        else:
            titles.setdefault(group, set())
    labels = {}
    for group, names in titles.items():
        if group == UNKNOWN:
            labels[group] = 'Window unknown (older/imported screenshots)'
        else:
            first = sorted(names)[0] if names else 'Untitled window'
            changed = ' · titles changed' if len(names) > 1 else ''
            labels[group] = f'{first}{changed} · window {group[5:]}'
    return groups, labels
