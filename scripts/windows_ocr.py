#!/usr/bin/env python3
"""Run OCR for meeting screenshots through a local Ollama vision model."""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import numpy as np
from PIL import Image
import screenshot_edits
import cloud_retry
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from screenshot_analysis import analyzed_frames, comparison_options, manual_frames


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
MODEL_PREFERENCES = ["glm-ocr", "glm-ocr:latest", "gemma3:latest", "gemma3", "granite4:latest", "granite4"]
DEFAULT_OCR_CONTEXT_LENGTH = 16384
DEFAULT_OPENROUTER_OCR_MODEL = "openai/gpt-6-luna"


def path_key(path: str | Path) -> str:
    """Match persisted Windows paths regardless of relative form or casing."""
    return str(Path(path).resolve()).casefold()


def screenshot_paths(session_dir: Path, limit: int = 0) -> list[Path]:
    """Return every captured screenshot unless an explicit positive limit is supplied."""
    roots = [session_dir / "screenshots", session_dir / "screenshots" / "imported"]
    images = []
    for root in roots:
        if root.exists():
            images.extend(p for p in root.iterdir() if p.suffix.lower() in IMAGE_EXTENSIONS and p.is_file())
    ordered = sorted(images, key=lambda p: p.stat().st_mtime)
    return ordered[:limit] if limit > 0 else ordered


def timestamp_for(path: Path) -> str:
    name = path.stem
    if name.startswith("screen-"):
        return name.removeprefix("screen-")
    return datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")


def changed_components(mask: np.ndarray, limit: int = 3) -> list[dict[str, int]] | None:
    """Return compact 8-connected difference regions, or None when too complex."""
    height, width = mask.shape
    changed = int(mask.sum())
    if changed > 6_000:
        return None
    visited = np.zeros_like(mask, dtype=bool)
    components: list[dict[str, int]] = []
    for y, x in np.argwhere(mask):
        if visited[y, x]:
            continue
        stack = [(int(y), int(x))]
        visited[y, x] = True
        count = 0
        min_x = max_x = int(x)
        min_y = max_y = int(y)
        while stack:
            cy, cx = stack.pop()
            count += 1
            min_x, max_x = min(min_x, cx), max(max_x, cx)
            min_y, max_y = min(min_y, cy), max(max_y, cy)
            for ny in range(max(0, cy - 1), min(height, cy + 2)):
                for nx in range(max(0, cx - 1), min(width, cx + 2)):
                    if not visited[ny, nx] and mask[ny, nx]:
                        visited[ny, nx] = True
                        stack.append((ny, nx))
        components.append({
            "pixels": count,
            "width": max_x - min_x + 1,
            "height": max_y - min_y + 1,
        })
        if len(components) > limit:
            return None
    return components


def cursor_only_difference(previous: Path, current: Path, image_loader=None, near_duplicate_fraction=0.0) -> tuple[bool, list[dict[str, int]]]:
    """Conservatively identify a pointer move without hiding other UI changes.

    A move produces two compact regions: the old pointer and the new pointer.
    We intentionally reject one-region changes (for example clock text or a UI
    state change) and all complex changes.
    """
    loader = image_loader or Image.open
    return compare_pixels(_rgb_pixels(previous, loader), _rgb_pixels(current, loader), near_duplicate_fraction)


def _rgb_pixels(path: Path, loader) -> np.ndarray:
    with loader(path) as image:
        return np.asarray(image.convert("RGB"), dtype=np.int16)


def compare_pixels(first: np.ndarray, second: np.ndarray, near_duplicate_fraction=0.0) -> tuple[bool, list[dict[str, int]]]:
    if first.shape != second.shape:
        return False, []
    changed = np.max(np.abs(first - second), axis=2) >= 24
    if not changed.any():
        return True, []
    changed_pixels = int(np.count_nonzero(changed))
    if near_duplicate_fraction and changed_pixels <= changed.size * near_duplicate_fraction:
        return True, [{'near_duplicate': True, 'pixels': changed_pixels}]
    components = changed_components(changed)
    if not components or len(components) != 2:
        return False, components or []
    cursor_regions = all(
        12 <= component["width"] <= 96
        and 12 <= component["height"] <= 96
        and component["pixels"] <= 2_500
        for component in components
    )
    return cursor_regions, components


def _perceptual_hash(path: Path, loader) -> tuple[tuple[int, int], int, int]:
    """Return size, difference hash, and mean luminance for adjacent comparisons."""
    with loader(path) as image:
        size = image.size
        small = np.asarray(image.convert('L').resize((9, 8), Image.Resampling.BILINEAR))
    bits = np.packbits((small[:, :-1] > small[:, 1:]).reshape(-1))
    return size, int.from_bytes(bits.tobytes(), 'big'), int(small.mean())


def select_screenshots(images: list[Path], on_progress=None, image_loader=None, near_duplicate_fraction=0.0,
                       perceptual_distance=None) -> tuple[list[Path], list[dict[str, Any]]]:
    """Compare each capture with its immediate predecessor; never delete originals."""
    selected: list[Path] = []
    manifest: list[dict[str, Any]] = []
    previous: Path | None = None
    previous_pixels: np.ndarray | None = None
    previous_hash = None
    loader = image_loader or Image.open
    for image in images:
        # A lone screenshot needs no visual comparison. Preserve that fast
        # path (and let OCR report a corrupt file at the OCR stage).
        pixels = _rgb_pixels(image, loader) if len(images) > 1 and perceptual_distance is None else None
        image_hash = _perceptual_hash(image, loader) if len(images) > 1 and perceptual_distance is not None else None
        entry: dict[str, Any] = {
            "path": str(image),
            "timestamp": timestamp_for(image),
            "selected": True,
            "automatic_selected": True,
            "reason": "initial" if previous is None else "visual-change",
        }
        if previous is not None:
            if perceptual_distance is not None:
                duplicate = (previous_hash[0] == image_hash[0]
                             and (previous_hash[1] ^ image_hash[1]).bit_count() <= perceptual_distance
                             and abs(previous_hash[2] - image_hash[2]) <= 32)
                components = [{'perceptual_distance': (previous_hash[1] ^ image_hash[1]).bit_count()}] if duplicate else []
            else:
                duplicate, components = compare_pixels(previous_pixels, pixels, near_duplicate_fraction)
            if duplicate:
                entry.update({
                    "selected": False,
                    "automatic_selected": False,
                    "reason": ('perceptual-duplicate' if perceptual_distance is not None else
                               "identical" if not components else
                               'near-duplicate' if components[0].get('near_duplicate') else 'cursor-only-change'),
                    "duplicate_of": str(previous),
                    "change_regions": components,
                })
            else:
                entry["change_regions"] = components
        if entry["selected"]:
            selected.append(image)
        manifest.append(entry)
        previous = image
        previous_pixels = pixels
        previous_hash = image_hash
        if on_progress:
            on_progress(len(manifest), len(images))
    return selected, manifest


def manual_overrides(manifest_path: Path) -> dict[str, bool]:
    """Read persisted reviewer choices without treating an old manifest as one."""
    if not manifest_path.exists():
        return {}
    try:
        frames = json.loads(manifest_path.read_text(encoding="utf-8")).get("frames", [])
    except Exception:
        return {}
    return {
        path_key(frame["path"]): bool(frame["manual_selected"])
        for frame in frames
        if isinstance(frame, dict) and "path" in frame and "manual_selected" in frame
    }


def apply_manual_overrides(
    selected: list[Path], manifest: list[dict[str, Any]], overrides: dict[str, bool]
) -> list[Path]:
    all_paths = {entry["path"]: Path(entry["path"]) for entry in manifest}
    for entry in manifest:
        automatic = bool(entry.get("automatic_selected", entry.get("selected", True)))
        entry["automatic_selected"] = automatic
        override = overrides.get(path_key(entry["path"]))
        if override is not None:
            entry["manual_selected"] = override
            entry["selected"] = override
            entry["reason"] = "manually-included" if override else "manually-excluded"
        else:
            entry["selected"] = automatic
    return [all_paths[entry["path"]] for entry in manifest if entry["selected"]]


def ollama_generate(
    host: str, model: str, image_path: Path, timeout: int,
    context_length: int = DEFAULT_OCR_CONTEXT_LENGTH,
) -> str:
    prompt = (
        "Extract useful text from this meeting screenshot. "
        "Return concise OCR notes only: visible slide titles, document headings, ticket IDs, code, errors, names, and key UI text. "
        "If no useful text is visible, say `No useful visible text.`"
    )
    payload = {
        "model": model,
        "prompt": prompt,
        "images": [base64.b64encode(image_path.read_bytes()).decode("ascii")],
        "stream": False,
        # Set this on every request: an already-running tray server does not
        # inherit the environment used by our optional server launcher.
        "options": {"num_ctx": context_length},
    }
    request = urllib.request.Request(
        f"{host.rstrip('/')}/api/generate",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.loads(response.read().decode("utf-8"))
    return (data.get("response") or "").strip()


def openrouter_generate(model: str, image_path: Path, timeout: int) -> dict[str, Any]:
    """Return OCR text and billable metadata from the cloud response."""
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY is unavailable for cloud OCR.")
    image_type = "jpeg" if image_path.suffix.lower() in {".jpg", ".jpeg"} else image_path.suffix.lower().lstrip(".")
    image = base64.b64encode(image_path.read_bytes()).decode("ascii")
    payload = {
        "model": model,
        "usage": {"include": True},
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": (
                "Extract useful text from this meeting screenshot. Return concise OCR notes only: "
                "visible slide titles, document headings, ticket IDs, code, errors, names, and key UI text. "
                "If no useful text is visible, say `No useful visible text.`"
            )},
            {"type": "image_url", "image_url": {"url": f"data:image/{image_type};base64,{image}"}},
        ]}],
    }
    request = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.loads(response.read().decode("utf-8"))
    content = data.get("choices", [{}])[0].get("message", {}).get("content")
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("Cloud OCR returned no text; this screenshot remains pending.")
    cost = (data.get("usage") or {}).get("cost")
    if isinstance(cost, bool) or not isinstance(cost, (int, float)) or cost < 0:
        cost = None
    return {
        "text": content.strip(),
        "cost_usd": cost,
        "generation_id": data.get("id") if isinstance(data.get("id"), str) else None,
    }


def error_message(exc: Exception) -> str:
    """Preserve actionable Ollama error details; never emit an empty error."""
    if isinstance(exc, urllib.error.HTTPError):
        try:
            body = exc.read().decode("utf-8", errors="replace").strip()
        except Exception:
            body = ""
        return f"HTTP {exc.code} {exc.reason}" + (f": {body}" if body else "")
    message = str(exc).strip()
    return message or type(exc).__name__


def existing_results(session_dir: Path) -> dict[str, dict[str, Any]]:
    path = session_dir / "visual" / "ocr.json"
    if not path.exists():
        return {}
    try:
        results = json.loads(path.read_text(encoding="utf-8")).get("results", [])
    except Exception:
        return {}
    return {
        path_key(result["path"]): result
        for result in results
        if isinstance(result, dict) and result.get("path")
    }


def wait_for_ollama(host: str, timeout: int = 30) -> bool:
    """Allow the desktop Ollama service time to recover after a runner crash."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            ollama_models(host, 3)
            return True
        except Exception:
            time.sleep(2)
    return False


def ollama_models(host: str, timeout: int) -> list[str]:
    request = urllib.request.Request(f"{host.rstrip('/')}/api/tags", method="GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.loads(response.read().decode("utf-8"))
    return [m.get("name", "") for m in data.get("models", []) if m.get("name")]


def ollama_running_models(host: str, timeout: int = 10) -> list[str]:
    request = urllib.request.Request(f"{host.rstrip('/')}/api/ps", method="GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = json.loads(response.read().decode("utf-8"))
    return [
        model.get("name") or model.get("model")
        for model in data.get("models", [])
        if model.get("name") or model.get("model")
    ]


def is_transient_ollama_error(exc: Exception) -> bool:
    """Return whether retrying or restoring the Ollama service can help."""
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in {408, 425, 429, 500, 502, 503, 504}
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        return isinstance(reason, (ConnectionError, TimeoutError, OSError))
    return isinstance(exc, (ConnectionError, TimeoutError))


def is_local_ollama_host(host: str) -> bool:
    parsed = urllib.parse.urlparse(host)
    return parsed.hostname in {"127.0.0.1", "localhost", "::1"}


def start_ollama_server(log_path: Path | None = None) -> subprocess.Popen[Any]:
    """Start the installed Windows Ollama API without launching its tray UI."""
    if os.name != "nt":
        raise OSError("Automatic Ollama startup is currently supported only on Windows.")
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    installed = Path(local_app_data) / "Programs" / "Ollama" / "ollama.exe"
    resolved = installed if installed.is_file() else None
    if resolved is None:
        on_path = shutil.which("ollama.exe") or shutil.which("ollama")
        resolved = Path(on_path) if on_path else None
    if resolved is None:
        raise FileNotFoundError(
            "Ollama is not installed. Install Ollama for Windows and ensure ollama.exe is available."
        )

    output: Any = subprocess.DEVNULL
    log_handle = None
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_handle = log_path.open("ab")
        output = log_handle
    env = os.environ.copy()
    env.update({
        "OLLAMA_NUM_PARALLEL": "1",
        "OLLAMA_MAX_LOADED_MODELS": "1",
        "OLLAMA_CONTEXT_LENGTH": "16384",
    })
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        return subprocess.Popen(
            [str(resolved), "serve"],
            cwd=resolved.parent,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )
    finally:
        if log_handle is not None:
            log_handle.close()


def ensure_ollama_ready(
    host: str,
    timeout: int = 180,
    log_path: Path | None = None,
    progress: Callable[..., None] | None = None,
) -> dict[str, Any]:
    """Reuse a healthy Ollama API or start and await the local Windows server."""
    try:
        ollama_models(host, 3)
        return {"ollama_started": False, "ollama_pid": None}
    except Exception as exc:
        initial_error = exc

    if not is_transient_ollama_error(initial_error):
        raise RuntimeError(f"Ollama OCR service health check failed: {error_message(initial_error)}") from initial_error
    connection_failure = (
        isinstance(initial_error, urllib.error.URLError)
        and not isinstance(initial_error, urllib.error.HTTPError)
        and isinstance(initial_error.reason, (ConnectionError, TimeoutError, OSError))
    ) or isinstance(initial_error, (ConnectionError, TimeoutError))

    process: subprocess.Popen[Any] | None = None
    if connection_failure:
        if not is_local_ollama_host(host):
            raise RuntimeError(
                f"The remote OCR service at {host} is unavailable and cannot be started locally: "
                f"{error_message(initial_error)}"
            ) from initial_error
        if progress:
            progress("Starting OCR engine", ollama_host=host)
        try:
            process = start_ollama_server(log_path)
        except Exception as exc:
            raise RuntimeError(f"Could not start the local Ollama OCR service: {error_message(exc)}") from exc
    elif progress:
        progress("Waiting for OCR engine", ollama_host=host, error=error_message(initial_error))

    wait_started = time.monotonic()
    deadline = wait_started + max(1, timeout)
    last_progress_second = -1
    last_error: Exception = initial_error
    while time.monotonic() < deadline:
        time.sleep(1)
        try:
            ollama_models(host, 3)
            if progress:
                progress("OCR engine ready", ollama_host=host, ollama_pid=process.pid if process else None)
            return {
                "ollama_started": process is not None,
                "ollama_pid": process.pid if process else None,
            }
        except Exception as exc:
            last_error = exc
        waited = int(time.monotonic() - wait_started)
        if progress and waited >= 5 and waited // 5 != last_progress_second // 5:
            progress("Waiting for OCR engine to become ready", ollama_host=host,
                     waited_seconds=waited, startup_timeout_seconds=timeout)
            last_progress_second = waited
        # An exiting child may have lost a bind race to another server that is
        # still initializing. Keep probing the API instead of launching again.
    detail = error_message(last_error)
    log_detail = f" See {log_path}." if log_path else ""
    raise RuntimeError(f"Ollama OCR service did not become ready within {timeout} seconds: {detail}.{log_detail}")


def gpu_memory_mb() -> tuple[int, int] | None:
    """Return free and total NVIDIA VRAM without treating GPU utilization as capacity."""
    try:
        completed = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=memory.free,memory.total",
                "--format=csv,noheader,nounits",
            ],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=10,
            check=False,
        )
        if completed.returncode != 0 or not completed.stdout.strip():
            return None
        free, total = completed.stdout.splitlines()[0].split(",", 1)
        return int(free.strip()), int(total.strip())
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def choose_model(host: str, requested: str, timeout: int) -> str:
    names = ollama_models(host, timeout)
    if requested and requested != "auto":
        requested_key = requested.removesuffix(":latest")
        for name in names:
            if name == requested or name.removesuffix(":latest") == requested_key:
                return name
        raise RuntimeError(
            f"OCR model '{requested}' is not installed in Ollama. Install it with: ollama pull {requested}"
        )
    for preferred in MODEL_PREFERENCES:
        if preferred in names:
            return preferred
    if names:
        return names[0]
    return "glm-ocr"


def unload_model(host: str, model: str, timeout: int = 30) -> bool:
    """Release Ollama model memory before the CUDA transcription stage."""
    request = urllib.request.Request(
        f"{host.rstrip('/')}/api/generate",
        data=json.dumps({"model": model, "prompt": "", "stream": False, "keep_alive": 0}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout):
            return True
    except Exception:
        return False


def prepare_ollama_resources(host: str, model: str) -> dict[str, Any]:
    """Give meeting OCR priority inside Ollama without terminating other applications."""
    released = []
    for resident in ollama_running_models(host):
        if unload_model(host, resident):
            released.append(resident)
    memory = gpu_memory_mb()
    minimum_free = int(os.environ.get("MEETING_MIN_GPU_FREE_MB", "3500"))
    if memory and memory[0] < minimum_free:
        raise RuntimeError(
            f"Only {memory[0]} MiB of {memory[1]} MiB GPU memory is free; meeting OCR requires "
            f"approximately {minimum_free} MiB. Pause other GPU workloads, then Resume processing. "
            "The app will not terminate unrelated programs automatically."
        )
    return {
        "model": model,
        "released_models": released,
        "gpu_free_mb": memory[0] if memory else None,
        "gpu_total_mb": memory[1] if memory else None,
    }


def write_outputs(
    session_dir: Path,
    model: str,
    results: list[dict[str, Any]],
    manifest: list[dict[str, Any]],
    requested_context_length: int | None = None,
) -> None:
    visual_dir = session_dir / "visual"
    os.makedirs(str(visual_dir), exist_ok=True)
    payload = {
        "model": model,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source_image_count": len(manifest),
        "selected_image_count": len(results),
        "results": results,
    }
    if requested_context_length is not None:
        payload["requested_context_length"] = requested_context_length
    (visual_dir / "ocr.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    (session_dir / "screenshots" / "manifest.json").write_text(
        json.dumps({
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "source_image_count": len(manifest),
            "selected_image_count": len(results),
            "frames": manifest,
        }, indent=2),
        encoding="utf-8",
    )
    lines = ["# Visual OCR Context", ""]
    for result in results:
        rel = result["path"]
        lines.append(f"## {Path(rel).name}")
        lines.append("")
        if result.get("ok"):
            lines.append(result.get("text") or "No useful visible text.")
        else:
            lines.append(f"OCR failed: {result.get('error')}")
        lines.append("")
    (visual_dir / "ocr.md").write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    timeline = [
        "# Visual Timeline",
        "",
        f"Retained {len(results)} of {len(manifest)} screenshots for OCR. "
        "Frames are deduplicated only when they are pixel-identical or differ by a conservative cursor-only move.",
        "",
    ]
    for result in results:
        rel = result["path"]
        timeline_text = (
            result.get("text") or "No useful visible text."
            if result.get("ok")
            else f"OCR failed: {result.get('error') or 'Unknown OCR error'}"
        )
        timeline.extend([f"## {timestamp_for(Path(rel))} - {Path(rel).name}", "", timeline_text, ""])
    (visual_dir / "timeline.md").write_text("\n".join(timeline).rstrip() + "\n", encoding="utf-8")


def run_cloud_ocr_batch(session_dir, images, pending, results_by_path, edits, model, args, manifest):
    """Run a few cloud images concurrently; checkpoint only on this thread."""
    def ordered_results():
        return [results_by_path[path_key(image)] for image in images]

    def worker(image, previous_attempts):
        attempts = 0
        fingerprint = screenshot_edits.fingerprint(session_dir, image, edits)
        try:
            input_image = screenshot_edits.ocr_input(session_dir, image, edits)
            def request():
                nonlocal attempts
                attempts += 1
                return openrouter_generate(model, input_image, args.timeout)
            cloud = cloud_retry.call_with_retries(request, max(1, args.max_attempts))
            return {'ok': True, 'status': 'complete', 'path': str(image),
                    'image_fingerprint': fingerprint, 'ocr_input_path': str(input_image),
                    'text': cloud['text'], 'backend': 'openrouter', 'model': model,
                    'cost_usd': cloud['cost_usd'], 'generation_id': cloud['generation_id'],
                    'attempts': previous_attempts + attempts,
                    'updated_at': datetime.now().isoformat(timespec='seconds')}
        except Exception as exc:
            return {'ok': False, 'status': 'failed', 'path': str(image),
                    'image_fingerprint': fingerprint, 'error': error_message(exc),
                    'attempts': previous_attempts + attempts,
                    'updated_at': datetime.now().isoformat(timespec='seconds')}

    pending_items = [(index, image) for index, image in enumerate(images, 1) if image in pending]
    remaining = iter(pending_items)
    completed = len(images) - len(pending_items)
    with ThreadPoolExecutor(max_workers=3, thread_name_prefix='meeting-cloud-ocr') as pool:
        active = {}
        def fill():
            while len(active) < 3:
                try:
                    index, image = next(remaining)
                except StopIteration:
                    return
                key = path_key(image)
                previous = int(results_by_path[key].get('attempts', 0) or 0)
                results_by_path[key] = {**results_by_path[key], 'ok': False, 'status': 'processing',
                                        'error': '', 'path': str(image)}
                write_outputs(session_dir, model, ordered_results(), manifest)
                print(json.dumps({'progress': f'Cloud OCR: {completed} of {len(images)} complete · {len(active) + 1} active',
                                  'stage': 'ocr', 'current': index, 'total': len(images),
                                  'item': str(image), 'preview_path': str(image), 'item_status': 'processing'}), flush=True)
                active[pool.submit(worker, image, previous)] = (index, image)
        fill()
        while active:
            finished, _ = wait(active, return_when=FIRST_COMPLETED)
            for future in finished:
                index, image = active.pop(future)
                result = future.result()
                results_by_path[path_key(image)] = result
                completed += 1
                write_outputs(session_dir, model, ordered_results(), manifest)
                print(json.dumps({'progress': f'Cloud OCR: {completed} of {len(images)} complete',
                                  'stage': 'ocr', 'current': index, 'total': len(images),
                                  'completed': completed, 'item': str(image),
                                  'item_status': result['status'], 'attempts': result['attempts'],
                                  **({'error': result['error']} if not result['ok'] else {})}), flush=True)
            fill()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session_dir")
    parser.add_argument("--model", default=os.environ.get("OLLAMA_OCR_MODEL", "auto"))
    parser.add_argument("--backend", choices=("local", "openrouter"), default="local")
    parser.add_argument("--host", default=os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434"))
    parser.add_argument(
        "--limit",
        type=int,
        default=int(os.environ.get("MEETING_OCR_IMAGE_LIMIT", "0")),
        help="Optional diagnostic source-image limit; 0 processes the complete capture (default).",
    )
    parser.add_argument("--timeout", type=int, default=240)
    parser.add_argument(
        "--context-length", type=int,
        default=os.environ.get("MEETING_OCR_CONTEXT_LENGTH", str(DEFAULT_OCR_CONTEXT_LENGTH)),
        help="Context tokens requested for each OCR image (default: 16384; MEETING_OCR_CONTEXT_LENGTH).",
    )
    parser.add_argument("--max-attempts", type=int, default=int(os.environ.get("MEETING_OCR_MAX_ATTEMPTS", "3")))
    parser.add_argument("--retry-delay", type=float, default=float(os.environ.get("MEETING_OCR_RETRY_DELAY", "2")))
    parser.add_argument("--force", action="store_true", help="Discard successful OCR checkpoints and regenerate every selected image.")
    args = parser.parse_args()
    if args.context_length <= 0:
        parser.error("--context-length / MEETING_OCR_CONTEXT_LENGTH must be a positive integer")

    session_dir = Path(args.session_dir).resolve()

    def engine_progress(message: str, **extra: Any) -> None:
        print(json.dumps({"progress": message, "stage": "ocr", "percent": 12, **extra}), flush=True)

    source_images = screenshot_paths(session_dir, args.limit)
    print(json.dumps({
        "progress": f"Preparing screenshot selection ({len(source_images)} captured); reusing valid review analysis",
        "stage": "ocr",
        "percent": 12,
        "source_image_count": len(source_images),
    }), flush=True)
    last_scan_update = 0.0
    def scan_progress(current: int, total: int) -> None:
        nonlocal last_scan_update
        now = time.monotonic()
        if current == total or now - last_scan_update >= 1:
            engine_progress(f"Checking screenshots for visual changes: {current} of {total}",
                            scan_current=current, scan_total=total)
            last_scan_update = now
    edits = screenshot_edits.load_edits(session_dir)
    loader_options = comparison_options(session_dir, edits)
    manifest = manual_frames(session_dir, source_images, edits)
    if manifest is None:
        manifest = analyzed_frames(session_dir, source_images, edits,
            lambda paths, **kw: select_screenshots(paths, **kw, **loader_options), scan_progress)
    images = [Path(frame['path']) for frame in manifest if frame['selected']]
    images = apply_manual_overrides(images, manifest, manual_overrides(session_dir / "screenshots" / "manifest.json"))
    prior = {} if args.force else existing_results(session_dir)
    selected_keys = {path_key(path) for path in images}
    results_by_path: dict[str, dict[str, Any]] = {
        key: value for key, value in prior.items() if key in selected_keys
        and value.get("image_fingerprint") == screenshot_edits.fingerprint(session_dir, Path(value["path"]), edits)
    }
    for image in images:
        key = path_key(image)
        results_by_path.setdefault(key, {
            "ok": False,
            "status": "pending",
            "path": str(image),
            "attempts": 0,
            "error": "Not processed yet",
        })

    def ordered_results() -> list[dict[str, Any]]:
        return [results_by_path[path_key(image)] for image in images]

    pending = [image for image in images if not results_by_path[path_key(image)].get("ok")]
    model = DEFAULT_OPENROUTER_OCR_MODEL if args.backend == "openrouter" else args.model
    context_length = args.context_length if args.backend == "local" else None
    resource_state: dict[str, Any] = {"backend": args.backend}
    if pending and args.backend == "local":
        engine_progress("Checking OCR engine", ollama_host=args.host)
        engine_state = ensure_ollama_ready(
            args.host,
            timeout=int(os.environ.get("MEETING_OLLAMA_STARTUP_TIMEOUT", "180")),
            log_path=session_dir / "transcripts" / "ollama_server.log",
            progress=engine_progress,
        )
        model = choose_model(args.host, args.model, min(args.timeout, 10))
        resource_state.update(engine_state)
        resource_state.update(prepare_ollama_resources(args.host, model))
    if pending and args.backend == "openrouter" and not os.environ.get("OPENROUTER_API_KEY"):
        raise RuntimeError("OPENROUTER_API_KEY is unavailable for cloud OCR.")
    print(json.dumps({
        "progress": "GPU and Ollama resources ready",
        "stage": "ocr",
        "percent": 12,
        **resource_state,
        "requested_context_length": context_length,
    }), flush=True)

    handled_cloud = args.backend == 'openrouter' and bool(pending)
    try:
        if handled_cloud:
            run_cloud_ocr_batch(session_dir, images, pending, results_by_path, edits, model, args, manifest)
        for index, image in enumerate([] if handled_cloud else images, start=1):
            key = path_key(image)
            existing = results_by_path[key]
            if existing.get("ok"):
                input_image = screenshot_edits.ocr_input(session_dir, image, edits)
                existing.update({"status": "complete", "path": str(image)})
                if input_image != image:
                    existing['ocr_input_path'] = str(input_image)
                print(json.dumps({
                    "progress": f"OCR image {index} of {len(images)} already complete",
                    "stage": "ocr", "current": index, "total": len(images),
                    "item": str(image), "preview_path": str(input_image), "item_status": "complete", "reused": True,
                    "attempts": existing.get("attempts", 1),
                }), flush=True)
                continue

            previous_attempts = int(existing.get("attempts", 0) or 0)
            image_fingerprint = screenshot_edits.fingerprint(session_dir, image, edits)
            for attempt in range(1, max(1, args.max_attempts) + 1):
                total_attempt = previous_attempts + attempt
                results_by_path[key] = {
                    **existing,
                    "ok": False,
                    "status": "processing",
                    **({"requested_context_length": context_length} if context_length else {}),
                    "path": str(image),
                    "image_fingerprint": image_fingerprint,
                    "attempts": total_attempt,
                    "error": "",
                    "updated_at": datetime.now().isoformat(timespec="seconds"),
                }
                write_outputs(session_dir, model, ordered_results(), manifest, context_length)
                print(json.dumps({
                    "progress": f"OCR image {index} of {len(images)} · attempt {attempt} of {args.max_attempts}",
                    "stage": "ocr", "current": index, "total": len(images),
                    "item": str(image), "item_status": "processing", "attempt": attempt,
                    "max_attempts": args.max_attempts, "attempts": total_attempt,
                    "requested_context_length": context_length,
                }), flush=True)
                try:
                    input_image = screenshot_edits.ocr_input(session_dir, image, edits)
                    print(json.dumps({"progress": f"OCR image {index} of {len(images)} · attempt {attempt} of {args.max_attempts}",
                                      "stage": "ocr", "current": index, "total": len(images), "item": str(image),
                                      "preview_path": str(input_image), "item_status": "processing"}), flush=True)
                    cloud_result = openrouter_generate(model, input_image, args.timeout) if args.backend == "openrouter" else None
                    text = (cloud_result["text"] if cloud_result is not None
                            else ollama_generate(args.host, model, input_image, args.timeout, args.context_length))
                    results_by_path[key] = {
                        "ok": True,
                        "status": "complete",
                        **({"requested_context_length": context_length} if context_length else {}),
                        "path": str(image),
                        "image_fingerprint": image_fingerprint,
                        "ocr_input_path": str(input_image),
                        "text": text,
                        "backend": args.backend,
                        "model": model,
                        **({"cost_usd": cloud_result["cost_usd"], "generation_id": cloud_result["generation_id"]}
                           if cloud_result is not None else {}),
                        "attempts": total_attempt,
                        "updated_at": datetime.now().isoformat(timespec="seconds"),
                    }
                    write_outputs(session_dir, model, ordered_results(), manifest, context_length)
                    print(json.dumps({
                        "progress": f"OCR image {index} of {len(images)} complete",
                        "stage": "ocr", "current": index, "total": len(images),
                        "item": str(image), "item_status": "complete", "attempts": total_attempt,
                    }), flush=True)
                    break
                except Exception as exc:
                    detail = error_message(exc)
                    results_by_path[key] = {
                        "ok": False,
                        "status": "failed",
                        **({"requested_context_length": context_length} if context_length else {}),
                        "path": str(image),
                        "image_fingerprint": image_fingerprint,
                        "attempts": total_attempt,
                        "error": detail,
                        "updated_at": datetime.now().isoformat(timespec="seconds"),
                    }
                    write_outputs(session_dir, model, ordered_results(), manifest, context_length)
                    print(json.dumps({
                        "progress": f"OCR image {index} of {len(images)} failed on attempt {attempt} of {args.max_attempts}",
                        "stage": "ocr", "current": index, "total": len(images),
                        "item": str(image), "item_status": "failed", "attempt": attempt,
                        "max_attempts": args.max_attempts, "attempts": total_attempt, "error": detail,
                    }), flush=True)
                    if attempt < args.max_attempts and is_transient_ollama_error(exc):
                        engine_progress(
                            f"Recovering OCR engine before retrying image {index} of {len(images)}",
                            current=index,
                            total=len(images),
                            item=str(image),
                            error=detail,
                        )
                        if args.backend == "local":
                            try:
                                ensure_ollama_ready(
                                    args.host,
                                    timeout=int(os.environ.get("MEETING_OLLAMA_STARTUP_TIMEOUT", "180")),
                                    log_path=session_dir / "transcripts" / "ollama_server.log",
                                    progress=engine_progress,
                                )
                            except Exception as recovery_exc:
                                recovery_detail = error_message(recovery_exc)
                                results_by_path[key]["error"] = f"{detail}; recovery failed: {recovery_detail}"
                                write_outputs(session_dir, model, ordered_results(), manifest, context_length)
                        time.sleep(args.retry_delay * attempt)
                    else:
                        break
            existing = results_by_path[key]
        write_outputs(session_dir, model, ordered_results(), manifest, context_length)
    finally:
        if pending and args.backend == "local":
            print(json.dumps({"progress": "Releasing OCR model memory", "stage": "ocr", "percent": 45}), flush=True)
            unload_model(args.host, model)
    results = ordered_results()
    failures = [result for result in results if not result.get("ok")]
    print(json.dumps({
        "ok": not failures,
        "source_image_count": len(source_images),
        "selected_image_count": len(images),
        "successful_image_count": len(results) - len(failures),
        "failed_image_count": len(failures),
        "output": str(session_dir / "visual" / "ocr.md"),
    }), flush=True)
    return 1 if failures else 0


def entrypoint() -> int:
    try:
        return main()
    except KeyboardInterrupt:
        print(json.dumps({"ok": False, "stage": "ocr", "error": "OCR processing was interrupted."}), flush=True)
        return 130
    except Exception as exc:
        print(json.dumps({
            "ok": False,
            "stage": "ocr",
            "error": error_message(exc),
            "exception_type": type(exc).__name__,
        }), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(entrypoint())
