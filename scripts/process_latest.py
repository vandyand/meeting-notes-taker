#!/usr/bin/env python3
"""Process the latest desktop recording into transcript cleanup and notes."""

from __future__ import annotations

import argparse
import importlib.util
import base64
import io
import json
import math
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import warnings
import wave
import uuid
import screenshot_edits
import cloud_retry
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import datetime
from pathlib import Path
from typing import Any

warnings.filterwarnings("ignore", category=DeprecationWarning, message="'audioop' is deprecated.*")
import audioop


DEFAULT_RECORDINGS_DIR = Path(__file__).resolve().parents[1] / "recordings" / "desktop"
DEFAULT_WHISPER_MODEL = "turbo"
DEFAULT_OPENROUTER_MODEL = "openai/gpt-6-luna"
DEFAULT_CLOUD_TRANSCRIPTION_MODEL = "openai/whisper-large-v3-turbo"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
LAST_PROGRESS: dict[str, Any] = {}
ACTIVE_SESSION: Path | None = None
PROGRESS_LOCK = threading.RLock()
PARALLEL_PROGRESS: dict[str, Any] | None = None


def progress(message: str, **extra: Any) -> None:
    with PROGRESS_LOCK:
        payload = {"progress": message, **extra}
        if PARALLEL_PROGRESS is not None:
            stage = payload.get("stage")
            group = "ocr" if stage == "ocr" else "audio" if stage in ("transcript", "cleanup") else None
            if group:
                PARALLEL_PROGRESS[group] = dict(payload)
                ocr_percent = float(PARALLEL_PROGRESS.get("ocr", {}).get("percent", 12))
                audio_percent = float(PARALLEL_PROGRESS.get("audio", {}).get("percent", 52))
                ocr_fraction = max(0.0, min(1.0, (ocr_percent - 12) / 33))
                audio_fraction = max(0.0, min(1.0, (audio_percent - 52) / 32))
                combined_percent = 10 + 35 * ocr_fraction + 39 * audio_fraction
                PARALLEL_PROGRESS["_max_percent"] = max(
                    float(PARALLEL_PROGRESS.get("_max_percent", 10)), combined_percent)
                payload["percent"] = round(PARALLEL_PROGRESS["_max_percent"], 1)
            payload["parallel_stages"] = {key: dict(value) for key, value in PARALLEL_PROGRESS.items()
                                          if key in ("ocr", "audio")}
        LAST_PROGRESS.clear()
        LAST_PROGRESS.update(payload)
        if ACTIVE_SESSION is not None:
            if write_process_state(ACTIVE_SESSION, None) is False:
                LAST_PROGRESS['status_persistence_warning'] = 'Status file is unavailable; processing continues and live progress remains available.'
                LAST_PROGRESS['progress'] = message + ' · ' + LAST_PROGRESS['status_persistence_warning']
        if os.environ.get("MNT_PROGRESS") == "1":
            print(json.dumps(LAST_PROGRESS), flush=True)


def ocr_worker_error(payload: dict[str, Any], output_lines: list[str], returncode: int) -> str:
    """Extract an actionable worker error instead of replacing it with a generic message."""
    if payload.get("error"):
        return str(payload["error"]).strip()
    for line in reversed(output_lines):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            candidate = json.loads(stripped)
            if isinstance(candidate, dict) and candidate.get("error"):
                return str(candidate["error"]).strip()
            continue
        except json.JSONDecodeError:
            return stripped[-2000:]
    return f"OCR worker exited with code {returncode} without reporting an error"


def write_ocr_worker_log(session_dir: Path, attempts: list[list[str]]) -> Path:
    path = session_dir / "transcripts" / "ocr_worker.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"OCR worker log written {datetime.now().isoformat(timespec='seconds')}"]
    for index, output in enumerate(attempts, start=1):
        lines.extend(["", f"--- worker attempt {index} ---", *[line.rstrip() for line in output]])
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return path


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def windows_secret(name: str) -> str | None:
    """Read one named SecretStore value without emitting it to logs or stdout."""
    if os.name != "nt":
        return None
    # DevSecrets is configured for PowerShell 7.  Windows PowerShell 5.1
    # (`powershell.exe`) cannot see its Unlock-SecretStore cmdlet.
    pwsh = shutil.which("pwsh.exe")
    if not pwsh:
        return None
    script = (
        "$ErrorActionPreference='Stop'; "
        ". (Join-Path $HOME '.config\\powershell\\profile.ps1'); "
        "Unlock-DevSecrets; "
        f"$secret=Get-Secret -Name '{name}' -Vault 'DevSecrets' -AsPlainText; "
        "[Console]::Out.Write($secret)"
    )
    try:
        completed = subprocess.run(
            [pwsh, "-NoProfile", "-NonInteractive", "-Command", script],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return completed.stdout.strip() if completed.returncode == 0 and completed.stdout.strip() else None


def openrouter_api_key() -> str | None:
    key = os.environ.get("OPENROUTER_API_KEY")
    if key:
        return key
    secret_name = os.environ.get("OPENROUTER_SECRET_NAME", "OPENROUTER_API_KEY")
    return windows_secret(secret_name)


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def latest_session(recordings_dir: Path) -> Path:
    candidates = [
        p for p in recordings_dir.iterdir()
        if p.is_dir() and (p / "metadata.json").exists()
    ] if recordings_dir.exists() else []
    if not candidates:
        raise RuntimeError(f"No recording sessions found in {recordings_dir}")
    return max(candidates, key=lambda p: p.stat().st_mtime)


def dbfs(rms: float) -> float:
    if rms <= 0:
        return -math.inf
    return 20.0 * math.log10(rms / 32767.0)


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return -math.inf
    vals = sorted(values)
    if len(vals) == 1:
        return vals[0]
    idx = (len(vals) - 1) * pct
    lo = math.floor(idx)
    hi = math.ceil(idx)
    if lo == hi:
        return vals[int(idx)]
    frac = idx - lo
    return vals[lo] * (1.0 - frac) + vals[hi] * frac


def local_track_path(session_dir: Path, track: dict[str, Any]) -> Path:
    raw = str(track.get("path", ""))
    basename = Path(raw.replace("\\", "/")).name
    subdir = "microphones" if track.get("kind") == "microphone" else "system"
    candidate = session_dir / subdir / basename
    if candidate.exists():
        return candidate
    direct = Path(raw)
    if direct.exists():
        return direct
    return candidate


def wav_stats(path: Path, window_ms: int = 250) -> dict[str, Any]:
    with wave.open(str(path), "rb") as wav:
        sample_width = wav.getsampwidth()
        sample_rate = wav.getframerate()
        total_frames = wav.getnframes()
        window_frames = max(1, int(sample_rate * window_ms / 1000.0))
        values = []
        read_frames = 0
        while read_frames < total_frames:
            frames = wav.readframes(min(window_frames, total_frames - read_frames))
            if not frames:
                break
            values.append(dbfs(audioop.rms(frames, sample_width)))
            read_frames += window_frames
    finite = [v for v in values if v != -math.inf]
    p10 = percentile(finite, 0.10)
    p50 = percentile(finite, 0.50)
    p95 = percentile(finite, 0.95)
    dynamic = p95 - p10 if p10 != -math.inf and p95 != -math.inf else 0.0
    return {
        "p10_db": p10,
        "p50_db": p50,
        "p95_db": p95,
        "dynamic_range_db": dynamic,
        "active": p95 > -80.0 and dynamic >= 6.0,
    }


def score_track(track: dict[str, Any], session_dir: Path) -> dict[str, Any]:
    path = local_track_path(session_dir, track)
    stats = wav_stats(path) if path.exists() else {
        "p10_db": -math.inf,
        "p50_db": -math.inf,
        "p95_db": -math.inf,
        "dynamic_range_db": 0.0,
        "active": False,
    }
    return {**track, "local_path": str(path), "audio_stats": stats}


def selected_tracks(metadata: dict[str, Any], session_dir: Path, scored: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    scored = scored if scored is not None else [score_track(t, session_dir) for t in metadata.get("tracks", [])]
    manual_ids = (metadata.get("processing_selection") or {}).get("track_ids")
    if isinstance(manual_ids, list):
        manual_set = {str(track_id) for track_id in manual_ids}
        return [track for track in scored if str(track.get("track_id")) in manual_set]
    return automatic_selected_tracks(metadata, session_dir, scored)


def automatic_selected_tracks(
    metadata: dict[str, Any], session_dir: Path, scored: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    scored = scored if scored is not None else [score_track(t, session_dir) for t in metadata.get("tracks", [])]
    selected = []
    for kind in ("system", "microphone"):
        candidates = [t for t in scored if t.get("kind") == kind]
        if kind == "microphone":
            non_stereo_mix = [
                t for t in candidates
                if "stereo mix" not in (t.get("device", {}).get("name", "").lower())
            ]
            if non_stereo_mix:
                candidates = non_stereo_mix
        active = [t for t in candidates if t["audio_stats"]["active"]]
        pool = active or candidates
        if pool:
            selected.append(max(pool, key=lambda t: t["audio_stats"]["p95_db"]))
    return selected


def transcript_json_path(session_dir: Path, track: dict[str, Any]) -> Path:
    return session_dir / "transcripts" / f"{Path(track['local_path']).stem}.json"


def transcribe_cloud_chunk(audio: bytes, model: str) -> dict[str, Any]:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("OPENROUTER_API_KEY is unavailable for cloud transcription.")
    request = urllib.request.Request(
        "https://openrouter.ai/api/v1/audio/transcriptions",
        data=json.dumps({
            "model": model,
            "input_audio": {"data": base64.b64encode(audio).decode("ascii"), "format": "wav"},
            "language": "en",
            "response_format": "verbose_json",
            "timestamp_granularities": ["segment"],
        }).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=90) as response:
        data = json.loads(response.read().decode("utf-8"))
    segments = data.get("segments")
    if not isinstance(segments, list) or not segments:
        raise RuntimeError("Cloud transcription returned no segment timestamps. This provider response cannot be safely merged into the meeting transcript.")
    for segment in segments:
        if not isinstance(segment, dict) or not isinstance(segment.get("start"), (int, float)) or not isinstance(segment.get("end"), (int, float)):
            raise RuntimeError("Cloud transcription returned invalid segment timestamps.")
    return data


def run_cloud_transcription(session_dir: Path, tracks: list[dict[str, Any]], model: str) -> list[str]:
    """Checkpoint each bounded WAV chunk so Resume never rebills finished chunks."""
    missing = [track for track in tracks if not transcript_json_path(session_dir, track).exists()]
    if not missing:
        return ["Transcripts already exist for selected tracks."]
    if not os.environ.get("OPENROUTER_API_KEY"):
        raise RuntimeError("OPENROUTER_API_KEY is unavailable for cloud transcription.")
    for track_index, track in enumerate(missing, start=1):
        source = Path(track["local_path"])
        chunk_dir = session_dir / "transcripts" / "cloud_chunks" / source.stem
        chunk_dir.mkdir(parents=True, exist_ok=True)
        all_segments: list[dict[str, Any]] = []
        all_text: list[str] = []
        with wave.open(str(source), "rb") as wav:
            rate = wav.getframerate()
            frame_bytes = wav.getnchannels() * wav.getsampwidth()
            frames_per_chunk = min(rate * 120, max(rate, 20_000_000 // frame_bytes))
            total_chunks = max(1, math.ceil(wav.getnframes() / frames_per_chunk))
            ready: dict[int, dict[str, Any]] = {}
            pending = []
            for index in range(total_chunks):
                checkpoint = chunk_dir / f"{index:05d}.json"
                if checkpoint.exists():
                    data = json.loads(checkpoint.read_text(encoding="utf-8"))
                    if data.get("model") != model:
                        raise RuntimeError(f"Cloud transcript checkpoint uses {data.get('model')}; cannot mix transcription models. Clear or archive {chunk_dir} before retrying with a different cloud model.")
                    ready[index] = data
                else:
                    pending.append(index)

            with ThreadPoolExecutor(max_workers=3, thread_name_prefix='meeting-cloud-audio') as pool:
                remaining = iter(pending)
                active = {}
                failure = None
                def fill():
                    while len(active) < 3:
                        try:
                            index = next(remaining)
                        except StopIteration:
                            return
                        wav.setpos(min(wav.getnframes(), index * frames_per_chunk))
                        pcm = wav.readframes(frames_per_chunk)
                        progress(f"Cloud transcription: audio {track_index} of {len(missing)}, chunk {index + 1} of {total_chunks} · {len(ready)} complete",
                                 stage="transcript", current=track_index, total=len(missing),
                                 item=str(source), item_status="processing", backend="openrouter",
                                 chunk_current=index + 1, chunk_total=total_chunks,
                                 item_percent=100 * len(ready) / total_chunks,
                                 percent=52 + 20 * (track_index - 1 + len(ready) / total_chunks) / len(missing))
                        buffer = io.BytesIO()
                        with wave.open(buffer, "wb") as chunk:
                            chunk.setnchannels(wav.getnchannels())
                            chunk.setsampwidth(wav.getsampwidth())
                            chunk.setframerate(rate)
                            chunk.writeframes(pcm)
                        audio = buffer.getvalue()
                        active[pool.submit(cloud_retry.call_with_retries,
                                           lambda data=audio: transcribe_cloud_chunk(data, model))] = index
                fill()
                while active:
                    finished, _ = wait(active, return_when=FIRST_COMPLETED)
                    for future in finished:
                        index = active.pop(future)
                        try:
                            data = future.result()
                            data['model'] = model
                            checkpoint = chunk_dir / f'{index:05d}.json'
                            temporary = checkpoint.with_suffix('.tmp')
                            temporary.write_text(json.dumps(data, indent=2), encoding='utf-8')
                            temporary.replace(checkpoint)
                            ready[index] = data
                            progress(f"Cloud transcription: audio {track_index} of {len(missing)}, {len(ready)} of {total_chunks} chunks complete",
                                     stage='transcript', current=track_index, total=len(missing),
                                     item=str(source), item_status='processing', backend='openrouter',
                                     chunk_current=index + 1, chunk_total=total_chunks,
                                     item_percent=100 * len(ready) / total_chunks,
                                     percent=52 + 20 * (track_index - 1 + len(ready) / total_chunks) / len(missing))
                        except Exception as exc:
                            failure = failure or exc
                    if failure is None:
                        fill()
                if failure is not None:
                    raise failure
            for index in range(total_chunks):
                data = ready[index]
                offset = index * frames_per_chunk / rate
                all_text.append(str(data.get("text") or "").strip())
                for segment in data["segments"]:
                    all_segments.append({**segment, "start": float(segment["start"]) + offset,
                                         "end": float(segment["end"]) + offset})
        output = transcript_json_path(session_dir, track)
        payload = {"text": " ".join(part for part in all_text if part), "segments": all_segments,
                   "language": "en", "backend": "openrouter", "model": model}
        temporary = output.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temporary.replace(output)
        progress(f"Cloud transcription: audio {track_index} of {len(missing)} complete", stage="transcript",
                 current=track_index, total=len(missing), item=str(source), item_status="complete",
                 item_percent=100, backend="openrouter")
    return [f"Transcribed {len(missing)} track(s) via OpenRouter {model}."]


def whisper_device(requested: str) -> str:
    if requested != "auto":
        return requested
    try:
        import torch
        return "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:
        return "cpu"


def run_whisper(session_dir: Path, tracks: list[dict[str, Any]], model: str, requested_device: str = "auto") -> list[str]:
    missing = [Path(t["local_path"]) for t in tracks if not transcript_json_path(session_dir, t).exists()]
    if not missing:
        return ["Whisper transcripts already exist for selected tracks."]
    # Prefer the current Windows Python's installed module. This is independent
    # of whether its Scripts directory has reached PATH in the desktop app.
    if importlib.util.find_spec("whisper"):
        whisper_cmd = [sys.executable, str(Path(__file__).with_name("whisper_progress.py"))]
    else:
        whisper = shutil.which("whisper")
        if not whisper:
            raise RuntimeError(
                "Whisper is not installed for the Windows Python used by this app "
                f"({sys.executable}). Install it once with: \"{sys.executable}\" -m pip install openai-whisper"
            )
        whisper_cmd = [whisper]
    out_dir = session_dir / "transcripts"
    out_dir.mkdir(parents=True, exist_ok=True)
    device = whisper_device(requested_device)
    cmd = [
        *whisper_cmd,
        *[str(p) for p in missing],
        "--model", model,
        "--model_dir", str(Path.home() / ".cache" / "whisper"),
        "--device", device,
        "--language", "en",
        "--output_format", "all",
        "--output_dir", str(out_dir),
        "--verbose", "False",
    ]
    total = len(missing)
    progress("Loading Whisper model for audio transcription", stage="transcript", percent=52, current=1, total=total, completed=0, model=model, device=device)
    whisper_env = os.environ.copy()
    # Whisper's built-in CLI includes non-ASCII help/output. Force UTF-8 so a
    # Windows CP-1252 console cannot turn an otherwise valid run into an error.
    whisper_env.setdefault("PYTHONUTF8", "1")
    whisper_env.setdefault("PYTHONIOENCODING", "utf-8")
    # Whisper uses CUDA for model inference, but NumPy/OpenBLAS can still
    # create a large CPU thread pool.  Keep that pool deliberately small so a
    # just-finished OCR stage cannot trigger OpenBLAS allocation failures.
    blas_threads = os.environ.get("MNT_BLAS_THREADS", "1")
    for variable in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        whisper_env[variable] = blas_threads
    whisper_process = subprocess.Popen(
        cmd,
        cwd=repo_root(),
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=whisper_env,
    )
    output_lines: list[str] = []
    assert whisper_process.stdout is not None
    decoder_events: queue.Queue = queue.Queue()
    def read_output() -> None:
        for line in whisper_process.stdout:
            output_lines.append(line)
            try:
                event = json.loads(line)
                if isinstance(event, dict) and event.get("whisper_progress"):
                    decoder_events.put(event)
            except (ValueError, TypeError):
                pass
    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()
    active: dict[str, Any] = {"phase": "loading"}
    last_heartbeat = 0.0
    while whisper_process.poll() is None or reader.is_alive() or not decoder_events.empty():
        changed = False
        try:
            while True:
                event = decoder_events.get_nowait()
                # Preparing/saving events must not inherit the prior track's percentage.
                active = event
                changed = True
        except queue.Empty:
            pass
        done = sum(1 for track in tracks if Path(track["local_path"]) in missing and transcript_json_path(session_dir, track).exists())
        now = time.monotonic()
        if changed or now - last_heartbeat >= 1.0:
            current = int(active.get("current", min(done + 1, total)))
            phase = active.get("phase", "loading")
            item_percent = active.get("item_percent")
            if phase == "decoding" and item_percent is not None:
                pos, duration = int(active["audio_seconds"]), int(active["audio_total_seconds"])
                message = (f"Transcribing audio {current} of {total} · {item_percent:.0f}% · "
                           f"Audio {pos // 60:02d}:{pos % 60:02d} / {duration // 60:02d}:{duration % 60:02d} · {device.upper()}")
            elif phase == "loading":
                message = f"Loading Whisper {model} on {device.upper()} — preparing {total} audio file(s)"
            else:
                message = f"{'Saving transcript' if phase == 'saving' else 'Preparing audio'} {current} of {total} · {device.upper()}"
            progress(
                message,
                stage="transcript",
                percent=52 + 20 * max(done, current - 1 + (item_percent or 0) / 100) / total,
                current=current,
                total=total,
                completed=done,
                item_percent=item_percent,
                audio_seconds=active.get("audio_seconds"),
                audio_total_seconds=active.get("audio_total_seconds"),
                model=model,
                device=device,
                item=active.get("item", str(missing[min(done, total - 1)])),
                item_status="processing",
            )
            last_heartbeat = now
        time.sleep(0.1)
    reader.join(timeout=5)
    (out_dir / "whisper_worker.log").write_text("".join(output_lines), encoding="utf-8")
    if whisper_process.returncode:
        raise RuntimeError("Whisper transcription failed: " + ("".join(output_lines).strip() or f"exit code {whisper_process.returncode}"))
    absent = [path.name for path in missing if not (out_dir / f"{path.stem}.json").exists()]
    if absent:
        progress(f"Audio transcription needs attention: {', '.join(absent)}", stage="transcript",
                 failed_items=absent, item=str(next(path for path in missing if path.name == absent[0])))
        raise RuntimeError(f"Whisper finished but transcript output is missing for {', '.join(absent)}. "
                           "See transcripts/whisper_worker.log. Resume processing retries only missing audio.")
    progress("Audio transcription complete", stage="transcript", percent=72)
    return [f"Transcribed {len(missing)} track(s) with Whisper model `{model}` on `{device}`."]


def run_cleanup(session_dir: Path) -> list[str]:
    progress("Cleaning transcript", stage="cleanup", percent=76)
    subprocess.run(
        [sys.executable, "scripts/clean_transcript.py", str(session_dir)],
        cwd=repo_root(),
        text=True,
        capture_output=True,
        check=True,
    )
    progress("Transcript cleanup complete", stage="cleanup", percent=84)
    return [f"Wrote {session_dir / 'transcripts' / 'cleaned.md'}."]


def load_visual_context(session_dir: Path) -> str:
    path = session_dir / "visual" / "ocr.md"
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8").strip()
    return text[:12000]


def extract_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start >= 0 and end > start:
            return json.loads(stripped[start:end + 1])
        raise


def notes_prompt(cleaned: str, visual_context: str) -> list[dict[str, str]]:
    system = (
        "You turn cleaned meeting transcripts into concise, useful engineering meeting notes. "
        "Use only the supplied transcript and visual OCR context. Preserve uncertainty when a detail is unclear. "
        "Prefer concrete decisions, action items, blockers, and technical context over generic summary. "
        "Return one valid JSON object and no prose outside JSON."
    )
    user = (
        "Return JSON with these keys:\n"
        "- title: short recommended meeting title\n"
        "- notes_md: Markdown notes with sections Summary, Decisions, Action Items, Technical Notes, Visual Context, Open Questions\n"
        "- action_items: array of objects with owner, task, due_date, confidence, source\n"
        "- open_questions: array of strings\n\n"
        "Cleaned transcript:\n\n"
        f"{cleaned}\n\n"
        "Visual OCR context from meeting screenshots:\n\n"
        f"{visual_context or 'No visual context captured.'}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def notes_transcript(session_dir: Path) -> str:
    """Send accepted speech, not the cleanup report's rejected-segment audit.

    Short track aliases remove repeated hardware IDs without losing provenance.
    Keep every accepted segment and its time range; never truncate speech to fit
    a credit balance. The full diagnostic report remains unchanged on disk.
    """
    out = session_dir / "transcripts"
    structured = out / "cleaned.json"
    if not structured.exists():
        return (out / "cleaned.md").read_text(encoding="utf-8").split("\n## Dropped Segments", 1)[0]
    data = json.loads(structured.read_text(encoding="utf-8"))
    if not isinstance(data.get("kept"), list):
        raise RuntimeError("Cleaned transcript has no accepted-segment list. Resume processing to rebuild cleanup.")
    aliases: dict[tuple[str, str], str] = {}
    lines = []
    def timestamp(value: float) -> str:
        seconds = max(0, int(value))
        return f"{seconds // 60:02d}:{seconds % 60:02d}"
    for turn in data["kept"]:
        source = (str(turn.get("speaker", "Unknown")), str(turn.get("track_id", "Unknown")))
        alias = aliases.setdefault(source, f"T{len(aliases) + 1}")
        lines.append(f"[{timestamp(turn['start'])}-{timestamp(turn['end'])} {alias}] {turn['text']}")
    legend = [f"{alias}: {speaker} (track: {track})" for (speaker, track), alias in aliases.items()]
    return "\n".join(["Accepted transcript segments. Track labels are sources, not verified speaker identities.", *legend, "", *lines])


def openrouter_notes(session_dir: Path, model: str, include_visual: bool = True) -> dict[str, Any]:
    key = openrouter_api_key()
    if not key:
        raise RuntimeError(
            "OPENROUTER_API_KEY is unavailable. Set it in the Windows environment or repo .env, "
            "or unlock Windows SecretStore vault DevSecrets with a secret named OPENROUTER_API_KEY."
        )
    cleaned = notes_transcript(session_dir)
    visual_context = load_visual_context(session_dir) if include_visual else ""
    body = {
        "model": model,
        "messages": notes_prompt(cleaned, visual_context),
        "temperature": 0.2,
        "max_tokens": 8192,
    }
    progress("Generating notes — waiting for OpenRouter", stage="notes", percent=88, model=model,
             prompt_characters=sum(len(message["content"]) for message in body["messages"]))
    request = urllib.request.Request(
        OPENROUTER_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "X-Title": "Meeting Notes Capture",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=240) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        (session_dir / "transcripts" / "notes_openrouter_error.json").write_text(
            json.dumps({"http_status": exc.code, "response": error_body}, indent=2), encoding="utf-8")
        if exc.code == 402:
            raise RuntimeError(
                "OpenRouter cannot fund this notes request (HTTP 402). Check your balance at "
                "https://openrouter.ai/settings/credits and the API key's spending limit. "
                "Your OCR and transcripts are saved. After adding credits or adjusting the limit, "
                "click Resume processing; completed OCR and audio will not run again. "
                "Technical details: transcripts/notes_openrouter_error.json"
            ) from exc
        raise RuntimeError(f"OpenRouter request failed with HTTP {exc.code}: {error_body}") from exc
    data = json.loads(raw)
    (session_dir / "transcripts" / "notes_openrouter_response.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
    choices = data.get("choices") or []
    content = ((choices[0].get("message") or {}).get("content") if choices else None)
    if not content:
        raise RuntimeError("OpenRouter response did not include message content.")
    if choices[0].get('finish_reason') == 'length':
        raise RuntimeError('OpenRouter truncated the notes response at its output-token limit. OCR and transcripts are saved; Resume processing to retry notes.')
    try:
        structured = extract_json_object(content)
    except (ValueError, TypeError) as exc:
        raise RuntimeError('OpenRouter returned incomplete or invalid notes JSON. OCR and transcripts are saved; Resume processing to retry notes.') from exc
    title = " ".join(str(structured.get("title") or "Untitled meeting").split())[:120]
    notes_md = str(structured.get("notes_md") or content).rstrip()
    action_items = structured.get("action_items") if isinstance(structured.get("action_items"), list) else []
    notes_path = session_dir / "transcripts" / "notes.md"
    manual_notes_path = session_dir / "transcripts" / "notes.manual.md"
    if notes_path.exists() and not manual_notes_path.exists():
        shutil.copy2(notes_path, manual_notes_path)
    notes_path.write_text(notes_md + "\n", encoding="utf-8")
    (session_dir / "transcripts" / "actions.json").write_text(json.dumps(action_items, indent=2), encoding="utf-8")
    action_lines = ["# Action Items", ""]
    if action_items:
        for item in action_items:
            owner = item.get("owner") or "Unassigned"
            task = item.get("task") or ""
            due = item.get("due_date") or "No due date"
            confidence = item.get("confidence") or "unknown"
            source = item.get("source") or ""
            action_lines.append(f"- **{owner}**: {task} _(due: {due}; confidence: {confidence})_")
            if source:
                action_lines.append(f"  Source: {source}")
    else:
        action_lines.append("No explicit action items found.")
    (session_dir / "transcripts" / "actions.md").write_text("\n".join(action_lines).rstrip() + "\n", encoding="utf-8")
    meeting_path = session_dir / 'meeting.json'
    try:
        previous_meeting = json.loads(meeting_path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        previous_meeting = {}
    user_title = bool(previous_meeting.get('title_updated_by_user'))
    meeting = {
        "title": previous_meeting.get('title', title) if user_title else title,
        "recommended_title": title,
        "title_updated_by_user": user_title,
        "model": model,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    meeting_path.write_text(json.dumps(meeting, indent=2), encoding="utf-8")
    return {
        "notes_path": str(notes_path),
        "title": title,
        "action_items_path": str(session_dir / "transcripts" / "actions.md"),
    }


def recover_saved_truncated_notes(session_dir: Path) -> None:
    """Salvage complete title/Markdown fields from a previously truncated response."""
    saved = json.loads((session_dir / 'transcripts' / 'notes_openrouter_response.json').read_text(encoding='utf-8'))
    choices = saved.get('choices') or []
    if not choices or choices[0].get('finish_reason') != 'length':
        raise ValueError('The saved notes response was not truncated.')
    content = (choices[0].get('message') or {}).get('content') or ''
    decoder = json.JSONDecoder()
    def field(name):
        match = re.search(r'"' + re.escape(name) + r'"\s*:', content)
        if not match:
            raise ValueError(f'The saved response has no complete {name} field.')
        value, _ = decoder.raw_decode(content[match.end():].lstrip())
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f'The saved {name} field is empty or invalid.')
        return value
    title = ' '.join(field('title').split())[:120]
    notes = field('notes_md').rstrip()
    if '## Summary' not in notes:
        raise ValueError('The saved response does not contain complete Markdown notes.')
    meeting_path = session_dir / 'meeting.json'
    try:
        meeting = json.loads(meeting_path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        meeting = {}
    meeting['recommended_title'] = title
    if not meeting.get('title_updated_by_user'):
        meeting['title'] = title
    meeting['notes_recovered_from_truncated_response'] = True
    meeting['updated_at'] = datetime.now().isoformat(timespec='seconds')
    actions = notes.split('## Action Items', 1)
    action_text = actions[1].split('\n## ', 1)[0].strip() if len(actions) == 2 else ''
    (session_dir / 'transcripts' / 'notes.md').write_text(notes + '\n', encoding='utf-8')
    (session_dir / 'transcripts' / 'actions.md').write_text(
        '# Action Items\n\n' + (action_text or 'The saved response was truncated; see the notes for any action items.') + '\n',
        encoding='utf-8')
    meeting_path.write_text(json.dumps(meeting, indent=2), encoding='utf-8')


def notes_artifact_complete(session_dir: Path) -> bool:
    if not (session_dir / 'transcripts' / 'notes.md').exists():
        return False
    response_path = session_dir / 'transcripts' / 'notes_openrouter_response.json'
    if not response_path.exists():
        return True
    try:
        response = json.loads(response_path.read_text(encoding='utf-8'))
        choices = response.get('choices') or []
        truncated = bool(choices and choices[0].get('finish_reason') == 'length')
    except (OSError, ValueError, TypeError):
        return True
    try:
        meeting = json.loads((session_dir / 'meeting.json').read_text(encoding='utf-8'))
    except (OSError, ValueError, TypeError):
        meeting = {}
    return not truncated or bool(meeting.get('notes_recovered_from_truncated_response'))


def write_diagnostics(
    session_dir: Path, all_tracks: list[dict[str, Any]], selected: list[dict[str, Any]], automatic: list[dict[str, Any]]
) -> None:
    selected_ids = {t.get("track_id") for t in selected}
    automatic_ids = {t.get("track_id") for t in automatic}
    rows = []
    for track in all_tracks:
        stats = track.get("audio_stats", {})
        rows.append({
            "track_id": track.get("track_id"),
            "kind": track.get("kind"),
            "device": track.get("device"),
            "path": track.get("local_path"),
            "selected": track.get("track_id") in selected_ids,
            "automatic_selected": track.get("track_id") in automatic_ids,
            "active": stats.get("active"),
            "p10_db": stats.get("p10_db"),
            "p50_db": stats.get("p50_db"),
            "p95_db": stats.get("p95_db"),
            "dynamic_range_db": stats.get("dynamic_range_db"),
        })
    out = session_dir / "transcripts" / "diagnostics.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"tracks": rows}, indent=2), encoding="utf-8")
    lines = ["# Capture Diagnostics", "", "| Track | Kind | Auto | Selected | Active | P95 dB | Dynamic Range | Device |", "|---|---|---:|---:|---:|---:|---:|---|"]
    for row in rows:
        lines.append(
            f"| `{row['track_id']}` | {row['kind']} | {row['automatic_selected']} | {row['selected']} | {row['active']} | "
            f"{row['p95_db']:.1f} | {row['dynamic_range_db']:.1f} | {(row.get('device') or {}).get('name', '')} |"
        )
    (session_dir / "transcripts" / "diagnostics.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def generate_diagnostics(session_dir: Path) -> dict[str, Any]:
    metadata = json.loads((session_dir / "metadata.json").read_text(encoding="utf-8"))
    progress("Analyzing audio tracks", stage="diagnostics", percent=5)
    all_tracks = []
    sources = metadata.get("tracks", [])
    for index, track in enumerate(sources, 1):
        progress(f"Analyzing audio track {index} of {len(sources)}", stage="diagnostics", percent=5,
                 current=index, total=len(sources))
        all_tracks.append(score_track(track, session_dir))
    automatic_tracks = automatic_selected_tracks(metadata, session_dir, all_tracks)
    tracks = selected_tracks(metadata, session_dir, all_tracks)
    if not tracks:
        raise RuntimeError(f"No tracks found in {session_dir / 'metadata.json'}")
    write_diagnostics(session_dir, all_tracks, tracks, automatic_tracks)
    progress("Audio track selection ready", stage="diagnostics", percent=10, tracks=len(tracks))
    return {
        "all_tracks": all_tracks,
        "selected_tracks": tracks,
        "diagnostics_path": str(session_dir / "transcripts" / "diagnostics.md"),
    }


def completed_ocr_summary(session_dir: Path) -> dict[str, int] | None:
    """Return a valid OCR checkpoint when it matches the current selection."""
    ocr_json = session_dir / "visual" / "ocr.json"
    ocr_md = session_dir / "visual" / "ocr.md"
    timeline = session_dir / "visual" / "timeline.md"
    manifest_path = session_dir / "screenshots" / "manifest.json"
    if not all(path.exists() for path in (ocr_json, ocr_md, timeline, manifest_path)):
        return None
    try:
        ocr = json.loads(ocr_json.read_text(encoding="utf-8"))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        results = ocr.get("results")
        frames = manifest.get("frames")
        if not isinstance(results, list) or not isinstance(frames, list):
            return None
        selected_paths = {str(Path(frame["path"]).resolve()) for frame in frames if frame.get("selected") and frame.get("path")}
        result_paths = {str(Path(result["path"]).resolve()) for result in results if result.get("path")}
        if selected_paths != result_paths or int(ocr.get("selected_image_count", -1)) != len(results):
            return None
        if any(not result.get("ok") for result in results):
            return None
        edits = screenshot_edits.load_edits(session_dir)
        if any(result.get("image_fingerprint") != screenshot_edits.fingerprint(session_dir, Path(result["path"]), edits) for result in results):
            return None
        return {
            "selected": len(results),
            "succeeded": sum(bool(result.get("ok")) for result in results),
            "failed": sum(not bool(result.get("ok")) for result in results),
        }
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None


def run_ocr(session_dir: Path, args: argparse.Namespace) -> list[str]:
    screenshot_roots = [session_dir / "screenshots", session_dir / "screenshots" / "imported"]
    has_images = any(root.exists() and any(p.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"} for p in root.iterdir()) for root in screenshot_roots)
    if not has_images:
        return ["No screenshots captured for OCR."]
    if args.skip_ocr:
        return ["Skipped OCR by request."]
    checkpoint = None if args.force_ocr else completed_ocr_summary(session_dir)
    if checkpoint:
        progress(
            f"Reusing completed OCR for {checkpoint['selected']} screenshots",
            stage="ocr",
            percent=45,
            current=checkpoint["selected"],
            total=checkpoint["selected"],
            reused=True,
        )
        return [
            f"Reused completed OCR for {checkpoint['selected']} screenshot(s) "
            f"({checkpoint['succeeded']} succeeded, {checkpoint['failed']} failed)."
        ]
    progress("Running visual OCR", stage="ocr", percent=12, model=args.ocr_model)
    ocr_script = repo_root() / "scripts" / "windows_ocr.py"
    cmd = [
        sys.executable,
        str(ocr_script),
        str(session_dir),
        "--model",
        args.ocr_model,
        "--backend", getattr(args, "ocr_backend", "local"),
    ]
    if args.force_ocr:
        cmd.append("--force")
    worker_attempts: list[list[str]] = []
    max_worker_attempts = max(1, int(os.environ.get("MEETING_OCR_WORKER_ATTEMPTS", "2")))
    for worker_attempt in range(1, max_worker_attempts + 1):
        output_lines: list[str] = []
        final_payload: dict[str, Any] = {}
        try:
            ocr_process = subprocess.Popen(
                cmd,
                cwd=repo_root(),
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            assert ocr_process.stdout is not None
            for line in ocr_process.stdout:
                output_lines.append(line)
                try:
                    payload = json.loads(line.strip())
                except json.JSONDecodeError:
                    continue
                if payload.get("error") or "ok" in payload:
                    final_payload = payload
                if "progress" in payload:
                    progress(payload["progress"], **{key: value for key, value in payload.items() if key != "progress"})
            returncode = ocr_process.wait()
        except Exception as exc:
            returncode = -1
            final_payload = {"error": f"Could not run OCR worker: {exc}"}
            output_lines.append(str(final_payload["error"]))

        worker_attempts.append(output_lines)
        log_path = write_ocr_worker_log(session_dir, worker_attempts)
        if returncode == 0:
            progress("Visual OCR complete", stage="ocr", percent=45)
            return [f"Wrote {session_dir / 'visual' / 'ocr.md'}."]

        failed = final_payload.get("failed_image_count")
        detail = ocr_worker_error(final_payload, output_lines, returncode)
        resource_blocked = ("gpu memory" in detail.casefold() or "pause other gpu" in detail.casefold()
                            or "did not become ready" in detail.casefold())
        if worker_attempt < max_worker_attempts and failed is None and not resource_blocked:
            progress(
                f"OCR worker stopped before completion; automatically restarting ({worker_attempt + 1} of {max_worker_attempts})",
                stage="ocr",
                percent=12,
                error=detail,
                worker_attempt=worker_attempt + 1,
                max_worker_attempts=max_worker_attempts,
            )
            time.sleep(2)
            continue

        failure_count = f" {failed} screenshot(s) still failed after image retries." if failed is not None else ""
        raise RuntimeError(
            f"OCR processing failed: {detail}.{failure_count} "
            f"Details: {log_path}. Resume processing to reuse checkpoints and retry."
        )

    raise RuntimeError("OCR processing failed without starting a worker.")


def write_process_metadata(session_dir: Path, data: dict[str, Any]) -> None:
    out = session_dir / "transcripts" / "process_metadata.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, indent=2), encoding="utf-8")


def write_process_state(session_dir: Path, ok: bool | None, error: str | None = None) -> bool:
    out = session_dir / "transcripts" / "process_state.json"
    temporary = out.with_name('process-state-' + uuid.uuid4().hex + '.tmp')
    payload = json.dumps({
        "ok": ok,
        "status": "running" if ok is None else ("complete" if ok else "failed"),
        "error": error,
        "last_progress": LAST_PROGRESS,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }, indent=2)
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(payload, encoding='utf-8')
        for attempt in range(5):
            try:
                temporary.replace(out)
                return True
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(.025 * 2 ** attempt)
    except OSError as exc:
        print(f'Warning: could not save processing status; continuing: {exc}', file=sys.stderr, flush=True)
        return False
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def process(args: argparse.Namespace) -> dict[str, Any]:
    global ACTIVE_SESSION, PARALLEL_PROGRESS
    root = repo_root()
    load_dotenv(root / ".env")
    recordings_dir = Path(args.recordings_dir)
    session_dir = latest_session(recordings_dir) if args.latest else Path(args.session_dir)
    session_dir = session_dir.resolve()
    if args.diagnostics_only:
        diagnostics = generate_diagnostics(session_dir)
        return {
            "ok": True,
            "session_dir": str(session_dir),
            "diagnostics_path": diagnostics["diagnostics_path"],
            "message": f"Wrote diagnostics: {diagnostics['diagnostics_path']}",
        }
    ACTIVE_SESSION = session_dir
    progress("Preparing meeting — checking saved work", stage="preparing", percent=0)
    notes_path = session_dir / "transcripts" / "notes.md"
    screenshot_roots = [session_dir / "screenshots", session_dir / "screenshots" / "imported"]
    has_screenshots = any(
        root.exists() and any(path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"} for path in root.iterdir())
        for root in screenshot_roots
    )
    ocr_ready = args.skip_ocr or not has_screenshots or completed_ocr_summary(session_dir) is not None
    if notes_artifact_complete(session_dir) and not args.force and ocr_ready:
        diagnostics_path = session_dir / "transcripts" / "diagnostics.md"
        if not diagnostics_path.exists():
            generate_diagnostics(session_dir)
        progress("Meeting already processed", stage="complete", percent=100)
        write_process_state(session_dir, True)
        return {
            "ok": True,
            "already_processed": True,
            "session_dir": str(session_dir),
            "message": f"Latest meeting already processed: {notes_path}",
        }

    diagnostics = generate_diagnostics(session_dir)
    tracks = diagnostics["selected_tracks"]

    messages = []
    ocr_error: Exception | None = None
    audio_error: Exception | None = None

    def audio_pipeline() -> list[str]:
        audio_messages = []
        if getattr(args, "transcription_backend", "local") == "openrouter":
            audio_messages.extend(run_cloud_transcription(session_dir, tracks, DEFAULT_CLOUD_TRANSCRIPTION_MODEL))
        else:
            audio_messages.extend(run_whisper(session_dir, tracks, args.whisper_model, args.whisper_device))
        audio_messages.extend(run_cleanup(session_dir))
        return audio_messages

    parallel = (has_screenshots and not args.skip_ocr and
                (getattr(args, "ocr_backend", "local") == "openrouter"
                 or getattr(args, "transcription_backend", "local") == "openrouter"))
    if parallel:
        PARALLEL_PROGRESS = {}
        progress("Running screenshot OCR and audio transcription together", stage="parallel", percent=10)
        try:
            with ThreadPoolExecutor(max_workers=2, thread_name_prefix="meeting-processing") as pool:
                ocr_future = pool.submit(run_ocr, session_dir, args)
                audio_future = pool.submit(audio_pipeline)
                try:
                    messages.extend(ocr_future.result())
                except Exception as exc:
                    ocr_error = exc
                try:
                    messages.extend(audio_future.result())
                except Exception as exc:
                    audio_error = exc
        finally:
            PARALLEL_PROGRESS = None
    else:
        try:
            messages.extend(run_ocr(session_dir, args))
        except Exception as exc:
            ocr_error = exc
            progress("Screenshot OCR needs attention; continuing audio transcription", stage="ocr", percent=45, error=str(exc))
        try:
            messages.extend(audio_pipeline())
        except Exception as exc:
            audio_error = exc
    if ocr_error or audio_error:
        problems = [f"OCR: {ocr_error}" if ocr_error else None,
                    f"Transcription: {audio_error}" if audio_error else None]
        progress("Processing needs attention; notes are waiting for OCR and transcription", stage="ocr" if ocr_error else "transcript",
                 percent=84, error="; ".join(problem for problem in problems if problem))
        raise RuntimeError("; ".join(problem for problem in problems if problem))
    if not (args.skip_ocr or not has_screenshots or completed_ocr_summary(session_dir) is not None):
        raise RuntimeError("OCR is not complete for every selected screenshot; notes remain on hold.")
    model = args.openrouter_model or os.environ.get("OPENROUTER_MODEL") or DEFAULT_OPENROUTER_MODEL
    generated = openrouter_notes(session_dir, model, include_visual=not args.skip_ocr)
    progress("Meeting notes complete", stage="complete", percent=100)
    result = {
        "ok": True,
        "already_processed": False,
        "session_dir": str(session_dir),
        "title": generated.get("title"),
        "selected_tracks": [
            {
                "kind": t.get("kind"),
                "track_id": t.get("track_id"),
                "device": t.get("device"),
                "path": t.get("local_path"),
                "audio_stats": t.get("audio_stats"),
            }
            for t in tracks
        ],
        "notes_path": generated.get("notes_path"),
        "action_items_path": generated.get("action_items_path"),
        "model": model,
        "messages": messages,
        "processed_at": datetime.now().isoformat(timespec="seconds"),
    }
    write_process_metadata(session_dir, result)
    write_process_state(session_dir, True)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session_dir", nargs="?", help="Recording session directory.")
    parser.add_argument("--latest", action="store_true", help="Process the most recent session in recordings dir.")
    parser.add_argument("--recordings-dir", default=str(DEFAULT_RECORDINGS_DIR))
    parser.add_argument("--force", action="store_true", help="Reprocess even when notes.md already exists.")
    parser.add_argument("--whisper-model", default=os.environ.get("WHISPER_MODEL", DEFAULT_WHISPER_MODEL))
    parser.add_argument("--whisper-device", choices=("auto", "cuda", "cpu"), default=os.environ.get("WHISPER_DEVICE", "auto"))
    parser.add_argument("--openrouter-model", default=None)
    parser.add_argument("--ocr-model", default=os.environ.get("OLLAMA_OCR_MODEL", "auto"))
    parser.add_argument("--ocr-backend", choices=("local", "openrouter"), default="local")
    parser.add_argument("--transcription-backend", choices=("local", "openrouter"), default="local")
    parser.add_argument("--skip-ocr", action="store_true")
    parser.add_argument("--force-ocr", action="store_true", help="Regenerate OCR even when a matching completed checkpoint exists.")
    parser.add_argument("--diagnostics-only", action="store_true")
    args = parser.parse_args()
    if not args.latest and not args.session_dir:
        parser.error("Provide a session_dir or --latest.")
    try:
        result = process(args)
        if os.environ.get("MNT_PROGRESS") == "1":
            print(json.dumps(result), flush=True)
        else:
            print(json.dumps(result, indent=2))
        return 0
    except Exception as exc:
        try:
            failed_session = latest_session(Path(args.recordings_dir)) if args.latest else Path(args.session_dir).resolve()
            write_process_state(failed_session, False, str(exc))
        except Exception:
            pass
        if os.environ.get("MNT_PROGRESS") == "1":
            print(json.dumps({"ok": False, "error": str(exc)}), flush=True)
        else:
            print(json.dumps({"ok": False, "error": str(exc)}, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
