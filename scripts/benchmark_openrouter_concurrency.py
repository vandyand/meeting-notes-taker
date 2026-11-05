#!/usr/bin/env python3
"""One-shot OpenRouter OCR/audio concurrency probe; never writes meeting files."""

from __future__ import annotations

import argparse
import io
import json
import math
import os
import statistics
import threading
import time
import urllib.error
import wave
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import cloud_retry
import process_latest
import windows_ocr


def jobs_for(session: Path) -> list[tuple[str, Path, int | None, int | None]]:
    manifest = json.loads((session / "screenshots" / "manifest.json").read_text(encoding="utf-8"))
    ocr = json.loads((session / "visual" / "ocr.json").read_text(encoding="utf-8"))
    results = {str(Path(row["path"]).resolve()).casefold(): row for row in ocr["results"]}
    jobs = []
    for frame in manifest["frames"]:
        if not frame.get("selected"):
            continue
        row = results[str(Path(frame["path"]).resolve()).casefold()]
        image = Path(row.get("ocr_input_path") or row["path"])
        if not image.is_file():
            raise FileNotFoundError(image)
        jobs.append(("ocr", image, None, None))
    diagnostics = json.loads((session / "transcripts" / "diagnostics.json").read_text(encoding="utf-8"))
    for track in diagnostics["tracks"]:
        if not track.get("selected"):
            continue
        source = Path(track["path"])
        with wave.open(str(source), "rb") as wav:
            rate = wav.getframerate()
            frame_bytes = wav.getnchannels() * wav.getsampwidth()
            frames_per_chunk = min(rate * 120, max(rate, 20_000_000 // frame_bytes))
            for index in range(math.ceil(wav.getnframes() / frames_per_chunk)):
                jobs.append(("audio", source, index, frames_per_chunk))
    return jobs


def audio_chunk(source: Path, index: int, frames_per_chunk: int) -> bytes:
    with wave.open(str(source), "rb") as wav:
        wav.setpos(min(wav.getnframes(), index * frames_per_chunk))
        pcm = wav.readframes(frames_per_chunk)
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as chunk:
            chunk.setnchannels(wav.getnchannels())
            chunk.setsampwidth(wav.getsampwidth())
            chunk.setframerate(wav.getframerate())
            chunk.writeframes(pcm)
        return buffer.getvalue()


def run_job(job: tuple, active: dict, lock: threading.Lock) -> dict:
    kind, path, index, frames_per_chunk = job
    attempts = 0
    statuses = []
    started = time.perf_counter()
    with lock:
        active["now"] += 1
        active["peak"] = max(active["peak"], active["now"])
    try:
        def request():
            nonlocal attempts
            attempts += 1
            try:
                if kind == "ocr":
                    return windows_ocr.openrouter_generate("openai/gpt-6-luna", path, 120)
                return process_latest.transcribe_cloud_chunk(
                    audio_chunk(path, index, frames_per_chunk),
                    process_latest.DEFAULT_CLOUD_TRANSCRIPTION_MODEL,
                )
            except urllib.error.HTTPError as error:
                statuses.append(error.code)
                raise

        response = cloud_retry.call_with_retries(request, max_attempts=3)
        usage = response.get("usage") or {}
        cost = response.get("cost_usd") if kind == "ocr" else usage.get("cost")
        return {"kind": kind, "ok": True, "attempts": attempts,
                "statuses": statuses, "seconds": time.perf_counter() - started,
                "cost_usd": cost if isinstance(cost, (int, float)) else None}
    except Exception as error:
        return {"kind": kind, "ok": False, "attempts": attempts,
                "statuses": statuses, "seconds": time.perf_counter() - started,
                "error_type": type(error).__name__, "error_message": str(error)[:240]}
    finally:
        with lock:
            active["now"] -= 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--workers", type=int, default=0, help="Maximum simultaneous requests; 0 submits all jobs")
    args = parser.parse_args()
    jobs = jobs_for(args.session.resolve())
    counts = Counter(job[0] for job in jobs)
    if args.dry_run:
        print(json.dumps({"jobs": len(jobs), "ocr": counts["ocr"], "audio": counts["audio"]}))
        return 0
    if not os.environ.get("OPENROUTER_API_KEY"):
        raise RuntimeError("OPENROUTER_API_KEY must be available only to this benchmark process")
    active = {"now": 0, "peak": 0}
    lock = threading.Lock()
    results = []
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=args.workers or len(jobs), thread_name_prefix="meeting-benchmark") as pool:
        for future in as_completed(pool.submit(run_job, job, active, lock) for job in jobs):
            results.append(future.result())
            if len(results) % 10 == 0 or len(results) == len(jobs):
                print(json.dumps({"finished": len(results), "total": len(jobs),
                                  "failed": sum(not row["ok"] for row in results)}), flush=True)
    durations = sorted(row["seconds"] for row in results)
    costs = [row["cost_usd"] for row in results if row.get("cost_usd") is not None]
    summary = {
        "jobs": len(jobs), "ocr": counts["ocr"], "audio": counts["audio"],
        "success": sum(row["ok"] for row in results),
        "failed": sum(not row["ok"] for row in results),
        "retries": sum(row["attempts"] - 1 for row in results),
        "http_errors": dict(Counter(str(code) for row in results for code in row["statuses"])),
        "other_errors": dict(Counter(row.get("error_type") for row in results if not row["ok"] and not row["statuses"])),
        "error_samples": list(dict.fromkeys(row.get("error_message", "") for row in results if not row["ok"]))[:5],
        "peak_tasks": active["peak"],
        "wall_seconds": round(time.perf_counter() - started, 1),
        "median_item_seconds": round(statistics.median(durations), 1),
        "p95_item_seconds": round(durations[math.ceil(.95 * len(durations)) - 1], 1),
        "known_cost_usd": round(sum(costs), 6),
        "unknown_cost_count": len(results) - len(costs),
    }
    print(json.dumps({"summary": summary}), flush=True)
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
