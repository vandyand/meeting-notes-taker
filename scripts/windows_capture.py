#!/usr/bin/env python3
"""Windows WASAPI loopback + microphone capture helper.

This script is intentionally dependency-light and Windows-native. It runs
directly from PowerShell or through the optional Clojure capture-agent component.
"""

from __future__ import annotations

import argparse
import traceback
import json
import math
import os
import signal
import sys
import threading
import time
import wave
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


STOP = False


def _stop(_signum: int, _frame: Any) -> None:
    request_stop()


def request_stop() -> None:
    global STOP
    STOP = True


def reset_stop() -> None:
    global STOP
    STOP = False


signal.signal(signal.SIGINT, _stop)
signal.signal(signal.SIGTERM, _stop)


def require_deps():
    try:
        import numpy as np
        import soundcard as sc
    except ImportError as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "missing_dependency",
                    "message": (
                        "Install deps with: python -m pip install --user "
                        "soundcard numpy"
                    ),
                    "detail": str(exc),
                }
            ),
            file=sys.stderr,
        )
        raise SystemExit(2)
    return np, sc


def device_info(device: Any) -> dict[str, Any]:
    return {
        "name": getattr(device, "name", None),
        "id": getattr(device, "id", None),
    }


def safe_slug(text: str | None, fallback: str) -> str:
    raw = (text or fallback).strip().lower()
    chars = []
    last_dash = False
    for ch in raw:
        if ch.isalnum():
            chars.append(ch)
            last_dash = False
        else:
            if not last_dash:
                chars.append("-")
                last_dash = True
    slug = "".join(chars).strip("-")
    return slug or fallback


def contains(haystack: str | None, needle: str | None) -> bool:
    if not needle:
        return True
    return needle.lower() in (haystack or "").lower()


def choose_by_name(devices: Iterable[Any], name: str | None, label: str) -> Any:
    devices = list(devices)
    if not devices:
        raise RuntimeError(f"no {label} devices found")
    if not name:
        return devices[0]
    matches = [d for d in devices if contains(getattr(d, "name", ""), name)]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        names = [getattr(d, "name", "") for d in devices]
        raise RuntimeError(f"no {label} matching {name!r}; available={names}")
    names = [getattr(d, "name", "") for d in matches]
    raise RuntimeError(f"multiple {label} devices matching {name!r}; matches={names}")


def loopback_for_speaker(sc: Any, speaker_name: str | None) -> Any:
    speakers = sc.all_speakers()
    speaker = choose_by_name(speakers, speaker_name, "speaker") if speaker_name else sc.default_speaker()
    loopbacks = sc.all_microphones(include_loopback=True)

    by_id = [m for m in loopbacks if getattr(m, "id", None) == getattr(speaker, "id", None)]
    if by_id:
        return by_id[0]

    speaker_name_text = getattr(speaker, "name", "")
    by_name = [m for m in loopbacks if contains(getattr(m, "name", ""), speaker_name_text)]
    if by_name:
        return by_name[0]

    # soundcard also supports get_microphone(id=speaker.name, include_loopback=True)
    try:
        return sc.get_microphone(id=speaker_name_text, include_loopback=True)
    except Exception:
        names = [getattr(m, "name", "") for m in loopbacks]
        raise RuntimeError(
            f"could not find loopback microphone for speaker {speaker_name_text!r}; "
            f"loopbacks={names}"
        )


def speaker_loopbacks(sc: Any) -> tuple[list[Any], list[dict[str, str]]]:
    loopbacks = []
    failures = []
    for speaker in sc.all_speakers():
        try:
            loopbacks.append(loopback_for_speaker(sc, getattr(speaker, "name", None)))
        except Exception as exc:
            failures.append(
                {
                    "device": getattr(speaker, "name", ""),
                    "error": type(exc).__name__,
                    "message": str(exc),
                }
            )
    return unique_devices(loopbacks), failures


def default_input_microphones(sc: Any) -> list[Any]:
    return [m for m in sc.all_microphones(include_loopback=False)]


def unique_devices(devices: Iterable[Any]) -> list[Any]:
    seen: set[str] = set()
    result = []
    for device in devices:
        key = str(getattr(device, "id", None) or getattr(device, "name", None))
        if key in seen:
            continue
        seen.add(key)
        result.append(device)
    return result


def microphone_candidates(sc: Any, microphone_name: str | None) -> list[Any]:
    microphones = default_input_microphones(sc)
    if microphone_name:
        return [choose_by_name(microphones, microphone_name, "microphone")]
    return unique_devices([sc.default_microphone(), *microphones])


def enter_first_working_recorder(
    stack: ExitStack,
    devices: Iterable[Any],
    *,
    sample_rate: int,
    channels: int,
    label: str,
) -> tuple[Any, Any, list[dict[str, str]]]:
    failures: list[dict[str, str]] = []
    for device in devices:
        try:
            recorder = device.recorder(samplerate=sample_rate, channels=channels)
            entered = stack.enter_context(recorder)
            return device, entered, failures
        except Exception as exc:
            failures.append(
                {
                    "device": getattr(device, "name", ""),
                    "error": type(exc).__name__,
                    "message": str(exc),
                }
            )
    raise RuntimeError(f"no working {label} recorder; failures={failures}")


def list_devices(_args: argparse.Namespace) -> int:
    _np, sc = require_deps()
    speakers = sc.all_speakers()
    microphones = sc.all_microphones(include_loopback=False)
    loopbacks = sc.all_microphones(include_loopback=True)
    payload = {
        "ok": True,
        "host": "windows",
        "speakers": [device_info(d) for d in speakers],
        "microphones": [device_info(d) for d in microphones],
        "loopbacks": [device_info(d) for d in loopbacks],
        "default_speaker": device_info(sc.default_speaker()),
        "default_microphone": device_info(sc.default_microphone()),
    }
    print(json.dumps(payload, indent=2))
    return 0


def write_frames(
    wav: wave.Wave_write,
    np: Any,
    frames: Any,
    channels: int,
    *,
    audio_tap: Any = None,
    tap_metadata: dict[str, Any] | None = None,
    start_frame: int = 0,
) -> int:
    if frames is None:
        return 0
    arr = np.asarray(frames, dtype=np.float32)
    if arr.size == 0:
        return 0
    if arr.ndim == 1:
        arr = arr.reshape((-1, 1))
    if arr.shape[1] < channels:
        arr = np.pad(arr, ((0, 0), (0, channels - arr.shape[1])), mode="constant")
    elif arr.shape[1] > channels:
        arr = arr[:, :channels]
    pcm = np.clip(arr, -1.0, 1.0)
    pcm = (pcm * 32767.0).astype("<i2")
    pcm_bytes = pcm.tobytes()
    wav.writeframes(pcm_bytes)
    written = int(arr.shape[0])
    if audio_tap is not None:
        try:
            audio_tap({
                **(tap_metadata or {}),
                "start_frame": start_frame,
                "pcm_bytes": pcm_bytes,
            })
        except Exception:
            # Live consumers are optional; their failures must never affect WAV capture.
            pass
    return written


def open_wav(path: Path, sample_rate: int, channels: int) -> wave.Wave_write:
    wav = wave.open(str(path), "wb")
    wav.setnchannels(channels)
    wav.setsampwidth(2)
    wav.setframerate(sample_rate)
    return wav


def capture_loop(
    *,
    name: str,
    recorder: Any,
    wav: wave.Wave_write,
    np: Any,
    channels: int,
    chunk_frames: int,
    max_frames: int | None,
    counters: dict[str, int],
    errors: list[dict[str, str]],
    event_sink: Any = None,
    sample_rate: int = 48000,
    audio_tap: Any = None,
    kind: str | None = None,
    device: Any = None,
) -> None:
    frames = 0
    last_health_at = 0.0
    try:
        while not STOP:
            remaining = None if max_frames is None else max_frames - frames
            if remaining is not None and remaining <= 0:
                break
            frames_to_read = chunk_frames if remaining is None else min(chunk_frames, remaining)
            data = recorder.record(numframes=frames_to_read)
            tap_metadata = {
                "track_id": name,
                "kind": kind,
                "device": device_info(device) if device is not None else None,
                "sample_rate": sample_rate,
                "channels": channels,
            }
            written = write_frames(
                wav,
                np,
                data,
                channels,
                audio_tap=audio_tap,
                tap_metadata=tap_metadata,
                start_frame=frames,
            )
            frames += written
            counters[name] = frames
            now = time.monotonic()
            if written and now - last_health_at >= 1.0:
                arr = np.asarray(data, dtype=np.float32)
                rms = float(np.sqrt(np.mean(np.square(arr)))) if arr.size else 0.0
                level_db = -120.0 if rms <= 0 else max(-120.0, 20.0 * math.log10(rms))
                emit_event(event_sink, {
                    "ok": True,
                    "event": "track_health",
                    "track_id": name,
                    "frames": frames,
                    "seconds": round(frames / sample_rate, 1),
                    "level_db": round(level_db, 1),
                })
                last_health_at = now
    except Exception as exc:
        errors.append(
            {
                "stream": name,
                "error": type(exc).__name__,
                "message": str(exc),
                "traceback": traceback.format_exc(),
            }
        )
    finally:
        counters[name] = frames


def open_track_recorders(
    stack: ExitStack,
    specs: list[dict[str, Any]],
    *,
    sample_rate: int,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    tracks = []
    failures = []
    for spec in specs:
        device = spec["device"]
        try:
            recorder = device.recorder(samplerate=sample_rate, channels=spec["channels"])
            recorder = stack.enter_context(recorder)
            wav = stack.enter_context(open_wav(spec["path"], sample_rate, spec["channels"]))
            tracks.append({**spec, "wav": wav, "recorder": recorder})
        except Exception as exc:
            failures.append(
                {
                    "kind": spec["kind"],
                    "track_id": spec["track_id"],
                    "device": getattr(device, "name", ""),
                    "path": str(spec["path"]),
                    "error": type(exc).__name__,
                    "message": str(exc),
                }
            )
    return tracks, failures


def emit_event(event_sink: Any, payload: dict[str, Any]) -> None:
    if event_sink:
        event_sink(payload)
    else:
        print(json.dumps(payload), flush=True)


def record_tracks(
    *,
    np: Any,
    out_dir: Path,
    specs: list[dict[str, Any]],
    sample_rate: int,
    chunk_frames: int,
    max_frames: int | None,
    event_sink: Any = None,
    audio_tap: Any = None,
) -> tuple[list[dict[str, Any]], list[dict[str, str]], list[dict[str, str]]]:
    counters = {spec["track_id"]: 0 for spec in specs}
    errors: list[dict[str, str]] = []
    with ExitStack() as stack:
        tracks, open_failures = open_track_recorders(stack, specs, sample_rate=sample_rate)
        if not tracks:
            raise RuntimeError(f"no working capture tracks; failures={open_failures}")

        emit_event(
            event_sink,
            {
                "ok": True,
                "event": "recording_started",
                "out_dir": str(out_dir),
                "tracks": [track_metadata(track, 0) for track in tracks],
                "open_failures": open_failures,
                "sample_rate": sample_rate,
                "chunk_frames": chunk_frames,
            },
        )

        threads = [
            threading.Thread(
                target=capture_loop,
                kwargs={
                    "name": track["track_id"],
                    "recorder": track["recorder"],
                    "wav": track["wav"],
                    "np": np,
                    "channels": track["channels"],
                    "chunk_frames": chunk_frames,
                    "max_frames": max_frames,
                    "counters": counters,
                    "errors": errors,
                    "event_sink": event_sink,
                    "sample_rate": sample_rate,
                    "audio_tap": audio_tap,
                    "kind": track["kind"],
                    "device": track["device"],
                },
                daemon=True,
            )
            for track in tracks
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        tracks_by_id = {track["track_id"]: track for track in tracks}
        stream_failures = []
        for error in errors:
            track = tracks_by_id.get(error["stream"])
            stream_failures.append(
                {
                    **error,
                    "kind": track["kind"] if track else "unknown",
                    "track_id": error["stream"],
                    "device": getattr(track["device"], "name", "") if track else "",
                    "path": str(track["path"]) if track else "",
                }
            )
        # A single device or file-write failure must not discard the complete
        # recording.  The WAV handles have been finalized by ExitStack; return
        # the partial result so run_recording can persist a recoverable manifest.
        return (
            [track_metadata(track, counters[track["track_id"]]) for track in tracks],
            open_failures,
            stream_failures,
        )


def track_metadata(track: dict[str, Any], frames: int) -> dict[str, Any]:
    return {
        "kind": track["kind"],
        "track_id": track["track_id"],
        "path": str(track["path"]),
        "frames": frames,
        "channels": track["channels"],
        "device": device_info(track["device"]),
    }


def single_device_specs(
    *,
    sc: Any,
    out_dir: Path,
    speaker: str | None,
    microphone: str | None,
    system_channels: int,
    mic_channels: int,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    loopback = loopback_for_speaker(sc, speaker)
    microphone_device = (
        choose_by_name(default_input_microphones(sc), microphone, "microphone")
        if microphone
        else sc.default_microphone()
    )
    specs = [
        {
            "kind": "system",
            "track_id": "system",
            "device": loopback,
            "path": out_dir / "system.wav",
            "channels": system_channels,
        }
    ]
    failures = []
    specs.append(
        {
            "kind": "microphone",
            "track_id": "microphone",
            "device": microphone_device,
            "path": out_dir / "mic.wav",
            "channels": mic_channels,
        }
    )
    return specs, failures


def all_device_specs(
    *,
    sc: Any,
    out_dir: Path,
    system_channels: int,
    mic_channels: int,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    specs = []
    failures = []

    system_dir = out_dir / "system"
    mic_dir = out_dir / "microphones"
    system_dir.mkdir(parents=True, exist_ok=True)
    mic_dir.mkdir(parents=True, exist_ok=True)

    loopbacks, loopback_failures = speaker_loopbacks(sc)
    failures.extend(loopback_failures)
    for idx, loopback in enumerate(loopbacks, start=1):
        slug = safe_slug(getattr(loopback, "name", None), f"system-{idx}")
        track_id = f"system-{idx}-{slug}"
        specs.append(
            {
                "kind": "system",
                "track_id": track_id,
                "device": loopback,
                "path": system_dir / f"{track_id}.wav",
                "channels": system_channels,
            }
        )

    for idx, mic in enumerate(default_input_microphones(sc), start=1):
        slug = safe_slug(getattr(mic, "name", None), f"mic-{idx}")
        track_id = f"microphone-{idx}-{slug}"
        specs.append(
            {
                "kind": "microphone",
                "track_id": track_id,
                "device": mic,
                "path": mic_dir / f"{track_id}.wav",
                "channels": mic_channels,
            }
        )

    return specs, failures


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_recording(
    args: argparse.Namespace,
    event_sink: Any = None,
    audio_tap: Any = None,
) -> dict[str, Any]:
    reset_stop()
    np, sc = require_deps()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    sample_rate = int(args.sample_rate)
    chunk_frames = max(1, int(sample_rate * (int(args.chunk_ms) / 1000.0)))
    max_frames = None
    if args.seconds is not None:
        max_frames = int(math.ceil(float(args.seconds) * sample_rate))

    metadata_path = out_dir / "metadata.json"
    started_at = iso_now()

    if args.all_devices:
        specs, discovery_failures = all_device_specs(
            sc=sc,
            out_dir=out_dir,
            system_channels=args.system_channels,
            mic_channels=args.mic_channels,
        )
        mode = "all-devices"
    else:
        specs, discovery_failures = single_device_specs(
            sc=sc,
            out_dir=out_dir,
            speaker=args.speaker,
            microphone=args.microphone,
            system_channels=args.system_channels,
            mic_channels=args.mic_channels,
        )
        mode = "single"

    tracks, open_failures, stream_failures = record_tracks(
        np=np,
        out_dir=out_dir,
        specs=specs,
        sample_rate=sample_rate,
        chunk_frames=chunk_frames,
        max_frames=max_frames,
        event_sink=event_sink,
        audio_tap=audio_tap,
    )

    ended_at = iso_now()
    system_tracks = [t for t in tracks if t["kind"] == "system"]
    mic_tracks = [t for t in tracks if t["kind"] == "microphone"]
    primary_system = next((t for t in system_tracks if t["track_id"] == "system"), None)
    primary_mic = next((t for t in mic_tracks if t["track_id"] == "microphone"), None)
    metadata = {
        "ok": not stream_failures,
        "partial": bool(stream_failures),
        "mode": mode,
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_seconds": (max((t["frames"] for t in tracks), default=0) / sample_rate),
        "sample_rate": sample_rate,
        "tracks": tracks,
        "system_tracks": system_tracks,
        "microphone_tracks": mic_tracks,
        "failures": [*discovery_failures, *open_failures, *stream_failures],
    }
    if primary_system:
        metadata["system"] = primary_system
    if primary_mic:
        metadata["microphone"] = primary_mic
    metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    emit_event(event_sink, {"ok": True, "event": "recording_stopped", **metadata})
    return metadata


def record(args: argparse.Namespace) -> int:
    run_recording(args)
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    list_p = sub.add_parser("list-devices", help="List speakers, mics, and loopbacks.")
    list_p.set_defaults(func=list_devices)

    rec = sub.add_parser("record", help="Record system loopback and mic WAV files.")
    rec.add_argument("--out-dir", required=True)
    rec.add_argument("--seconds", type=float)
    rec.add_argument("--speaker", help="Speaker name substring for loopback capture.")
    rec.add_argument("--microphone", help="Microphone name substring.")
    rec.add_argument(
        "--all-devices",
        action="store_true",
        help="Record every speaker loopback and every working physical microphone.",
    )
    rec.add_argument("--sample-rate", type=int, default=48000)
    rec.add_argument("--chunk-ms", type=int, default=100)
    rec.add_argument("--system-channels", type=int, default=2)
    rec.add_argument("--mic-channels", type=int, default=1)
    rec.set_defaults(func=record)
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        return args.func(args)
    except Exception as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": type(exc).__name__,
                    "message": str(exc),
                    "traceback": traceback.format_exc(),
                }
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
