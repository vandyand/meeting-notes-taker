#!/usr/bin/env python3
"""Rebuild metadata.json for an interrupted capture with finalized WAV files."""

from __future__ import annotations

import argparse
import json
import re
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path


def display_name(track_id: str) -> str:
    names = {
        "system-1-lg-ultrawide-nvidia-high-definition-audio": "LG ULTRAWIDE (NVIDIA High Definition Audio)",
        "system-2-headphones-wh202a": "Headphones (WH202A)",
        "system-3-realtek-digital-output-realtek-r-audio": "Realtek Digital Output (Realtek(R) Audio)",
        "microphone-1-headset-wh202a": "Headset (WH202A)",
        "microphone-2-microphone-usb-camera": "Microphone (USB CAMERA)",
        "microphone-3-stereo-mix-realtek-r-audio": "Stereo Mix (Realtek(R) Audio)",
    }
    return names.get(track_id, track_id.replace("-", " ").title())


def session_started_at(session: Path, duration_seconds: float) -> str:
    match = re.fullmatch(r"(\d{8})-(\d{6})", session.name)
    if match:
        local = datetime.strptime("".join(match.groups()), "%Y%m%d%H%M%S").astimezone()
        return local.astimezone(timezone.utc).isoformat()
    latest = max((path.stat().st_mtime for path in session.rglob("*.wav")), default=datetime.now().timestamp())
    return datetime.fromtimestamp(latest - duration_seconds, timezone.utc).isoformat()


def track(kind: str, path: Path) -> dict:
    with wave.open(str(path), "rb") as wav:
        return {
            "kind": kind,
            "track_id": path.stem,
            "path": str(path.resolve()),
            "frames": wav.getnframes(),
            "channels": wav.getnchannels(),
            "device": {"name": display_name(path.stem)},
            "sample_rate": wav.getframerate(),
        }


def recover(session: Path, overwrite: bool) -> Path:
    metadata_path = session / "metadata.json"
    if metadata_path.exists() and not overwrite:
        raise RuntimeError(f"{metadata_path} already exists; use --overwrite to replace it")
    tracks = [
        *(track("system", path) for path in sorted((session / "system").glob("*.wav"))),
        *(track("microphone", path) for path in sorted((session / "microphones").glob("*.wav"))),
    ]
    if not tracks:
        raise RuntimeError(f"No WAV files found in {session}")
    sample_rate = tracks[0]["sample_rate"]
    duration_seconds = max(item["frames"] / item["sample_rate"] for item in tracks)
    started_at = session_started_at(session, duration_seconds)
    ended_at = (datetime.fromisoformat(started_at) + timedelta(seconds=duration_seconds)).isoformat()
    for item in tracks:
        item.pop("sample_rate")
    system_tracks = [item for item in tracks if item["kind"] == "system"]
    microphone_tracks = [item for item in tracks if item["kind"] == "microphone"]
    metadata = {
        "ok": False,
        "partial": True,
        "recovered": True,
        "recovery_reason": "metadata.json was missing after an interrupted capture; reconstructed from finalized WAV files",
        "mode": "all-devices",
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_seconds": round(duration_seconds, 3),
        "sample_rate": sample_rate,
        "tracks": tracks,
        "system_tracks": system_tracks,
        "microphone_tracks": microphone_tracks,
        "failures": [],
    }
    metadata_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    return metadata_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session_dir", type=Path)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    print(recover(args.session_dir.resolve(), args.overwrite))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
