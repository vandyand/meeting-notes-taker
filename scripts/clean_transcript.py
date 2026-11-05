#!/usr/bin/env python3
"""Clean and merge Whisper transcripts with deterministic audio gates."""

from __future__ import annotations

import argparse
import difflib
import json
import math
import warnings
import wave
from pathlib import Path
from typing import Any

warnings.filterwarnings("ignore", category=DeprecationWarning, message="'audioop' is deprecated.*")

import audioop

ABSOLUTE_SILENCE_DB = -80.0
MIN_DYNAMIC_RANGE_DB = 6.0
SEGMENT_ACTIVE_NORM = 0.25
WEAK_DUPLICATE_NORM = 0.45
DUPLICATE_SIMILARITY = 0.78
MODERATE_DUPLICATE_SIMILARITY = 0.40
WEAK_ACTIVE_RATIO = 0.65
SUSPICIOUS_COMPRESSION_RATIO = 2.2
SUSPICIOUS_AVG_LOGPROB = -0.8


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


def normalize(value_db: float, floor_db: float, ceil_db: float) -> float:
    if value_db == -math.inf:
        return 0.0
    dynamic = max(ceil_db - floor_db, 1e-9)
    return max(0.0, min(1.0, (value_db - floor_db) / dynamic))


def wav_window_rms(path: Path, window_ms: int) -> dict[str, Any]:
    with wave.open(str(path), "rb") as wav:
        channels = wav.getnchannels()
        sample_width = wav.getsampwidth()
        sample_rate = wav.getframerate()
        total_frames = wav.getnframes()
        window_frames = max(1, int(sample_rate * window_ms / 1000.0))
        windows = []
        frame_start = 0
        while frame_start < total_frames:
            frames = wav.readframes(min(window_frames, total_frames - frame_start))
            if not frames:
                break
            rms = audioop.rms(frames, sample_width)
            windows.append(
                {
                    "start": frame_start / sample_rate,
                    "end": min(total_frames, frame_start + window_frames) / sample_rate,
                    "rms_db": dbfs(rms),
                }
            )
            frame_start += window_frames
    rms_values = [w["rms_db"] for w in windows if w["rms_db"] != -math.inf]
    p10 = percentile(rms_values, 0.10)
    p50 = percentile(rms_values, 0.50)
    p95 = percentile(rms_values, 0.95)
    dynamic = p95 - p10 if p10 != -math.inf and p95 != -math.inf else 0.0
    active = p95 > ABSOLUTE_SILENCE_DB and dynamic >= MIN_DYNAMIC_RANGE_DB
    return {
        "path": str(path),
        "channels": channels,
        "sample_rate": sample_rate,
        "duration": total_frames / sample_rate,
        "window_ms": window_ms,
        "windows": windows,
        "p10_db": p10,
        "p50_db": p50,
        "p95_db": p95,
        "dynamic_range_db": dynamic,
        "active": active,
    }


def windows_for_segment(track_stats: dict[str, Any], start: float, end: float) -> list[dict[str, Any]]:
    return [
        w
        for w in track_stats["windows"]
        if w["end"] > start and w["start"] < end
    ]


def segment_audio_stats(track_stats: dict[str, Any], start: float, end: float) -> dict[str, float]:
    windows = windows_for_segment(track_stats, start, end)
    vals = [w["rms_db"] for w in windows if w["rms_db"] != -math.inf]
    if not vals:
        return {"p50_db": -math.inf, "p90_db": -math.inf, "active_ratio": 0.0, "norm_p90": 0.0}
    p50 = percentile(vals, 0.50)
    p90 = percentile(vals, 0.90)
    floor_db = track_stats["p10_db"]
    ceil_db = track_stats["p95_db"]
    active_windows = [
        v for v in vals
        if normalize(v, floor_db, ceil_db) >= SEGMENT_ACTIVE_NORM
    ]
    return {
        "p50_db": p50,
        "p90_db": p90,
        "active_ratio": len(active_windows) / len(vals),
        "norm_p90": normalize(p90, floor_db, ceil_db),
    }


def text_similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def transcript_json_for_track(transcripts_dir: Path, track: dict[str, Any]) -> Path | None:
    stem = Path(track["path"].replace("\\", "/")).stem
    candidate = transcripts_dir / f"{stem}.json"
    return candidate if candidate.exists() else None


def load_segments(session_dir: Path, window_ms: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    metadata = json.loads((session_dir / "metadata.json").read_text(encoding="utf-8"))
    transcripts_dir = session_dir / "transcripts"
    turns = []
    track_reports = []
    for track in metadata.get("tracks", []):
        transcript_path = transcript_json_for_track(transcripts_dir, track)
        if not transcript_path:
            continue
        wav_path = Path(track["path"].split("meeting-notes-taker\\", 1)[-1].replace("\\", "/"))
        if not wav_path.exists():
            wav_path = Path(track["path"])
        track_stats = wav_window_rms(wav_path, window_ms)
        report = {
            "track_id": track["track_id"],
            "kind": track["kind"],
            "device": track["device"],
            "path": str(wav_path),
            "transcript": str(transcript_path),
            "p10_db": track_stats["p10_db"],
            "p50_db": track_stats["p50_db"],
            "p95_db": track_stats["p95_db"],
            "dynamic_range_db": track_stats["dynamic_range_db"],
            "active": track_stats["active"],
        }
        track_reports.append(report)
        data = json.loads(transcript_path.read_text(encoding="utf-8"))
        speaker = "me/mic" if track["kind"] == "microphone" else "remote/system"
        for segment in data.get("segments", []):
            text = " ".join((segment.get("text") or "").split())
            if not text:
                continue
            audio = segment_audio_stats(track_stats, float(segment["start"]), float(segment["end"]))
            turns.append(
                {
                    "speaker": speaker,
                    "track_id": track["track_id"],
                    "start": float(segment["start"]),
                    "end": float(segment["end"]),
                    "text": text,
                    "avg_logprob": segment.get("avg_logprob"),
                    "no_speech_prob": segment.get("no_speech_prob"),
                    "compression_ratio": segment.get("compression_ratio"),
                    "audio": audio,
                    "track_active": track_stats["active"],
                }
            )
    return turns, track_reports, metadata


def classify_turns(turns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    kept_by_track: dict[str, list[str]] = {}
    remote_active_windows = [
        t for t in turns
        if t["speaker"] == "remote/system" and t["audio"]["norm_p90"] >= SEGMENT_ACTIVE_NORM
    ]
    classified = []
    for turn in sorted(turns, key=lambda t: (t["start"], t["speaker"], t["track_id"])):
        prior = kept_by_track.setdefault(turn["track_id"], [])
        duplicate_score = max((text_similarity(turn["text"], p) for p in prior), default=0.0)
        compression = turn.get("compression_ratio")
        avg_logprob = turn.get("avg_logprob")
        norm = turn["audio"]["norm_p90"]
        overlaps_remote = any(
            r["end"] > turn["start"] and r["start"] < turn["end"]
            for r in remote_active_windows
        )
        reasons = []
        keep = True
        confidence = "high"

        if not turn["track_active"]:
            keep = False
            reasons.append("inactive-track")
        if norm < SEGMENT_ACTIVE_NORM:
            confidence = "low"
            reasons.append("low-relative-energy")
        if duplicate_score >= DUPLICATE_SIMILARITY:
            reasons.append(f"near-duplicate:{duplicate_score:.2f}")
        if compression is not None and compression >= SUSPICIOUS_COMPRESSION_RATIO:
            reasons.append(f"high-compression:{compression:.2f}")
        if avg_logprob is not None and avg_logprob <= SUSPICIOUS_AVG_LOGPROB:
            reasons.append(f"low-logprob:{avg_logprob:.2f}")
        if overlaps_remote and turn["speaker"] == "me/mic" and norm < WEAK_DUPLICATE_NORM:
            reasons.append("weak-mic-over-remote")
        if turn["audio"]["active_ratio"] < WEAK_ACTIVE_RATIO:
            reasons.append(f"weak-active-ratio:{turn['audio']['active_ratio']:.2f}")

        suspicious_decode = (
            (compression is not None and compression >= SUSPICIOUS_COMPRESSION_RATIO)
            or (avg_logprob is not None and avg_logprob <= SUSPICIOUS_AVG_LOGPROB)
        )
        if norm < SEGMENT_ACTIVE_NORM and (duplicate_score >= DUPLICATE_SIMILARITY or suspicious_decode):
            keep = False
        if duplicate_score >= DUPLICATE_SIMILARITY and (norm < WEAK_DUPLICATE_NORM or suspicious_decode):
            keep = False
        if turn["speaker"] == "me/mic" and overlaps_remote and duplicate_score >= DUPLICATE_SIMILARITY and suspicious_decode:
            keep = False
        if (turn["speaker"] == "me/mic"
                and overlaps_remote
                and suspicious_decode
                and (duplicate_score >= MODERATE_DUPLICATE_SIMILARITY
                     or turn["audio"]["active_ratio"] < WEAK_ACTIVE_RATIO)):
            keep = False

        turn = {**turn, "keep": keep, "confidence": confidence, "duplicate_score": duplicate_score, "reasons": reasons}
        if keep:
            prior.append(turn["text"])
        classified.append(turn)
    return classified


def fmt_time(seconds: float) -> str:
    minutes = int(seconds // 60)
    secs = seconds - minutes * 60
    return f"{minutes:02d}:{secs:05.2f}"


def write_outputs(session_dir: Path, turns: list[dict[str, Any]], track_reports: list[dict[str, Any]]) -> None:
    out_dir = session_dir / "transcripts"
    kept = [t for t in turns if t["keep"]]
    dropped = [t for t in turns if not t["keep"]]
    report = {
        "track_reports": track_reports,
        "kept": kept,
        "dropped": dropped,
        "rules": {
            "segment_active_norm": SEGMENT_ACTIVE_NORM,
            "weak_duplicate_norm": WEAK_DUPLICATE_NORM,
            "duplicate_similarity": DUPLICATE_SIMILARITY,
            "moderate_duplicate_similarity": MODERATE_DUPLICATE_SIMILARITY,
            "weak_active_ratio": WEAK_ACTIVE_RATIO,
            "suspicious_compression_ratio": SUSPICIOUS_COMPRESSION_RATIO,
            "suspicious_avg_logprob": SUSPICIOUS_AVG_LOGPROB,
            "absolute_silence_db": ABSOLUTE_SILENCE_DB,
            "min_dynamic_range_db": MIN_DYNAMIC_RANGE_DB,
        },
    }
    (out_dir / "cleaned.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    lines = [
        "# Cleaned Merged Transcript",
        "",
        f"Kept {len(kept)} segments; dropped {len(dropped)} suspicious segments.",
        "",
        "| Start | End | Speaker | Track | Text |",
        "|---:|---:|---|---|---|",
    ]
    for t in kept:
        text = t["text"].replace("|", "\\|")
        lines.append(f"| {fmt_time(t['start'])} | {fmt_time(t['end'])} | {t['speaker']} | `{t['track_id']}` | {text} |")
    lines.extend(["", "## Dropped Segments", "", "| Start | End | Speaker | Reasons | Text |", "|---:|---:|---|---|---|"])
    for t in dropped:
        text = t["text"].replace("|", "\\|")
        reasons = ", ".join(t["reasons"])
        lines.append(f"| {fmt_time(t['start'])} | {fmt_time(t['end'])} | {t['speaker']} | {reasons} | {text} |")
    (out_dir / "cleaned.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session_dir", help="Recording session directory containing metadata.json and transcripts/")
    parser.add_argument("--window-ms", type=int, default=250)
    args = parser.parse_args()
    session_dir = Path(args.session_dir)
    turns, track_reports, _metadata = load_segments(session_dir, args.window_ms)
    classified = classify_turns(turns)
    write_outputs(session_dir, classified, track_reports)
    kept = sum(1 for t in classified if t["keep"])
    dropped = len(classified) - kept
    print(f"Wrote {session_dir / 'transcripts' / 'cleaned.md'}")
    print(f"Kept {kept} segments; dropped {dropped}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
