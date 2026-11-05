"""Bounded, capture-safe coordinator for provisional meeting captions.

The capture tap calls ``submit`` with immutable PCM metadata; it never waits for
inference. UI events are small dictionaries: live_status(status, detail),
live_source(source, device), live_caption(source, device, text, provisional,
start, end), and live_gap/live_error(reason, track_id, start, end).
"""

from __future__ import annotations

import json
import inspect
import math
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Segment:
    """Small adapter shape shared with the pause-aware segmenter."""
    start_sample: int
    end_sample: int
    source_sample_rate: int
    audio_16k: object


def read_live_records(session_dir):
    path = Path(session_dir) / "transcripts" / "live_captions.jsonl"
    if not path.exists():
        return []
    records = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


class LiveTranscriptionCoordinator:
    """Routes one mic and one system track to a serialized local transcriber."""

    def __init__(self, session_dir, *, segmenter=None, transcriber=None,
                 event_sink=None, queue_size=512, default_devices=None,
                 switch_after=3, quiet_rms=0.012, preroll_seconds=0.35,
                 model_error=None, preview_interval=None):
        self.session_dir = Path(session_dir)
        self.records_path = self.session_dir / "transcripts" / "live_captions.jsonl"
        self.records_path.parent.mkdir(parents=True, exist_ok=True)
        self.segmenter = segmenter
        self.transcriber = transcriber
        self.model_error = model_error
        self.event_sink = event_sink
        self.queue = queue.Queue(maxsize=max(1, int(queue_size)))
        self.default_devices = default_devices or {}
        self.switch_after = max(1, int(switch_after))
        self.quiet_rms = float(quiet_rms)
        self.preroll_seconds = max(0.0, float(preroll_seconds))
        self.preview_interval = None if preview_interval is None else max(0.0, float(preview_interval))
        self.events = []
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._selected = {}
        self._candidates = {}
        self._tracks = {}
        self._last_segments = {}
        self._overflow_lock = threading.Lock()
        self._overflow = {}
        self._activity = {}
        self._history = {}
        self._worker_unavailable = False

    def _event(self, event, **fields):
        item = {"event": event, **fields}
        with self._lock:
            self.events.append(item)
        if self.event_sink:
            try:
                self.event_sink(item)
            except Exception:
                pass

    def _record(self, status, **fields):
        row = {"schema_version": 1, "status": status, "time": time.time(), **fields}
        with self._lock:
            with self.records_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
                handle.flush()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._event("live_status", status="starting", detail="Preparing local captions")
        if self.transcriber is None:
            self._worker_unavailable = True
            reason = self.model_error or "Local transcription model unavailable"
            self._record("error", reason=reason, error_type="model_unavailable")
            self._event("live_status", status="unavailable", detail=reason)
            return
        self._thread = threading.Thread(target=self._worker, name="live-transcription", daemon=True)
        self._thread.start()

    def submit(self, block):
        """Enqueue a post-WAV PCM block without blocking; return False on overflow."""
        try:
            if self._worker_unavailable:
                self._note_dropped_block(block, "model_unavailable")
                return False
            self.queue.put_nowait(dict(block))
            return True
        except queue.Full:
            self._note_dropped_block(block, "audio_queue_full")
            return False

    def _note_dropped_block(self, block, reason):
        rate = max(1, block.get("sample_rate", 1))
        start = block.get("start_frame", 0) / rate
        frames = len(block.get("pcm_bytes", b"")) // max(2, 2 * block.get("channels", 1))
        fields = {"track_id": block.get("track_id"), "source": block.get("kind"),
                  "device": self._device_name(block), "start": start,
                  "end": start + frames / rate, "reason": reason}
        # The capture callback only updates bounded in-memory state.
        with self._overflow_lock:
            key = fields.get("track_id") or "unknown"
            if key not in self._overflow and len(self._overflow) >= 32:
                key = "__overflow__"
                fields["reason"] = reason + "_multiple_tracks"
            prior = self._overflow.get(key)
            if prior:
                prior["start"] = min(prior["start"], fields["start"])
                prior["end"] = max(prior["end"], fields["end"])
                prior["dropped_blocks"] += 1
            else:
                fields["dropped_blocks"] = 1
                self._overflow[key] = fields

    @staticmethod
    def _device_name(block):
        value = block.get("device")
        return value.get("name", "Unknown device") if isinstance(value, dict) else str(value or "Unknown device")

    @staticmethod
    def _rms(block):
        import numpy as np
        samples = np.frombuffer(block.get("pcm_bytes", b""), dtype="<i2").astype(np.float32)
        return float(np.sqrt(np.mean(samples * samples)) / 32768.0) if samples.size else 0.0

    @staticmethod
    def _segments(segmenter, audio, rate, *, final=False):
        try:
            params = inspect.signature(segmenter).parameters.values()
            supports_final = any(p.name == "final" or p.kind == inspect.Parameter.VAR_KEYWORD
                                 for p in params)
        except (TypeError, ValueError):
            supports_final = False
        return segmenter(audio, rate, final=final) if supports_final else segmenter(audio, rate)

    @staticmethod
    def _already_committed(segment, consumed):
        start, end = int(segment.start_sample), int(segment.end_sample)
        return end <= consumed or (start < consumed and consumed - start >= (end - start) / 2)

    def _persist_overflow(self):
        with self._overflow_lock:
            pending = list(self._overflow.values())
            self._overflow = {}
        for fields in pending:
            self._record("gap", **fields)
            self._event("live_gap", **fields)
            if fields["reason"].startswith("audio_queue_full"):
                self._event("live_status", status="lagging", detail="Audio queue full; WAV remains intact")

    def _route(self, block):
        kind = block.get("kind")
        if kind not in ("microphone", "system"):
            return False
        track = block.get("track_id")
        name = self._device_name(block)
        state = self._tracks.setdefault(kind, {})
        state[track] = {"name": name, "block": block}
        level = self._rms(block)
        self._activity[track] = level
        selected = self._selected.get(kind)
        if selected is None:
            preferred = self.default_devices.get(kind)
            preferred_track = next((tid for tid, val in state.items()
                                    if tid == preferred or (isinstance(val["block"].get("device"), dict)
                                    and val["block"]["device"].get("id") == preferred)), None)
            if preferred is not None and preferred_track is None:
                # Wait for the explicitly preferred input instead of briefly
                # exposing a duplicate loopback/Stereo Mix stream.
                return False
            choices = state.items()
            if kind == "microphone":
                physical = [(tid, val) for tid, val in choices if "stereo mix" not in val["name"].lower()]
                choices = physical or list(state.items())
            chosen = next(((tid, val) for tid, val in choices
                           if tid == preferred or (isinstance(val["block"].get("device"), dict)
                           and val["block"]["device"].get("id") == preferred)), None)
            if chosen is None:
                chosen = next(iter(choices), None)
            if chosen:
                selected = chosen[0]
                self._selected[kind] = selected
                self._event("live_source", source=kind, track_id=selected, device=chosen[1]["name"])
            return track == selected
        if track == selected:
            return True
        selected_name = self._tracks[kind].get(selected, {}).get("name", "")
        if kind == "microphone" and "stereo mix" in name.lower() and \
                "stereo mix" not in selected_name.lower():
            # A loopback input must not displace a real microphone merely
            # because the user's microphone is quiet during other speech.
            return False
        if kind == "microphone" and "stereo mix" in selected_name.lower() and "stereo mix" not in name.lower():
            self._selected[kind] = track
            self._candidates.pop(kind, None)
            self._event("live_source", source=kind, track_id=track, device=name,
                        previous_track_id=selected, preroll_seconds=self.preroll_seconds)
            return True
        if self._activity.get(selected, 0.0) >= self.quiet_rms or level < self.quiet_rms:
            self._candidates.pop(kind, None)
            return False
        candidate = self._candidates.get(kind)
        if candidate and candidate[0] == track:
            candidate = (track, candidate[1] + 1)
        else:
            candidate = (track, 1)
        self._candidates[kind] = candidate
        if candidate[1] >= self.switch_after:
            self._selected[kind] = track
            self._candidates.pop(kind, None)
            self._event("live_source", source=kind, track_id=track, device=name,
                        previous_track_id=selected, preroll_seconds=self.preroll_seconds)
            return True
        return False

    def _worker(self):
        owner = getattr(self.transcriber, "__self__", None)
        prepare = getattr(self.transcriber, "prepare", None) or getattr(owner, "prepare", None)
        if prepare:
            try:
                prepare()
            except Exception as exc:
                self._worker_unavailable = True
                reason = str(exc) or "Local transcription model unavailable"
                self._record("error", reason=reason, error_type="model_unavailable")
                self._event("live_status", status="unavailable", detail=reason)
                return
        self._event("live_status", status="active", detail="Local captions active")
        while not self._stop.is_set() or not self.queue.empty():
            self._persist_overflow()
            try:
                block = self.queue.get(timeout=0.05)
            except queue.Empty:
                continue
            try:
                track = block.get("track_id")
                history = self._history.setdefault(track, [])
                history.append(block)
                rate = max(1, int(block.get("sample_rate", 1)))
                max_frames = int(rate * self.preroll_seconds)
                while len(history) > 1 and sum(
                    len(item.get("pcm_bytes", b"")) // max(2, 2 * item.get("channels", 1))
                    for item in history
                ) > max_frames:
                    history.pop(0)
                previous_source = self._selected.get(block.get("kind"))
                if not self._route(block):
                    continue
                selected = self._selected.get(block.get("kind"))
                batch = list(history) if selected != previous_source else [block]
                for audio_block in batch:
                    self._process(audio_block)
                history.clear()
            except Exception as exc:
                start = block.get("start_frame", 0) / max(1, block.get("sample_rate", 1))
                fields = {"track_id": block.get("track_id"), "source": block.get("kind"),
                          "device": self._device_name(block), "start": start,
                          "end": start + len(block.get("pcm_bytes", b"")) /
                          max(1, block.get("sample_rate", 1) * 2 * block.get("channels", 1)),
                          "reason": str(exc)}
                self._record("error", **fields)
                self._event("live_error", **fields)
            finally:
                self.queue.task_done()
        self._persist_overflow()
        self._flush_buffers()
        self._record("stopped")
        self._event("live_status", status="stopped", detail="Local captions stopped")

    def _flush_buffers(self):
        segmenter = self.segmenter
        if segmenter is None:
            try:
                from live_audio_segments import segment_audio
                segmenter = segment_audio
            except ImportError:
                segmenter = None
        for track, state in self._last_segments.items():
            remaining = len(state["audio"]) - state["consumed"]
            if remaining <= 0:
                continue
            track_info = next((tracks[track] for tracks in self._tracks.values() if track in tracks), {})
            source = next((kind for kind, tracks in self._tracks.items() if track in tracks), None)
            if segmenter is not None:
                try:
                    segments = self._segments(segmenter, state["audio"], state["rate"], final=True)
                    for segment in segments:
                        if self._already_committed(segment, state["consumed"]):
                            continue
                        text = self.transcriber(segment.audio_16k)
                        state["consumed"] = int(segment.end_sample)
                        if text and str(text).strip():
                            fields = {"source": source, "track_id": track,
                                      "device": track_info.get("name"),
                                      "start": (state["base_frame"] + int(segment.start_sample)) / state["rate"],
                                      "end": (state["base_frame"] + int(segment.end_sample)) / state["rate"],
                                      "text": str(text).strip()}
                            self._record("committed", **fields)
                            self._event("live_caption", **fields, provisional=False)
                except Exception as exc:
                    self._record("error", source=source, track_id=track,
                                 reason=f"final_flush_failed: {exc}", error_type="piece_error")
            remaining = len(state["audio"]) - state["consumed"]
            tail = state["audio"][state["consumed"]:]
            if not remaining or not len(tail) or float((tail * tail).mean() ** 0.5) < self.quiet_rms:
                continue
            fields = {"track_id": track, "source": source,
                      "device": track_info.get("name"),
                      "start": (state["base_frame"] + state["consumed"]) / state["rate"],
                      "end": (state["base_frame"] + len(state["audio"])) / state["rate"],
                      "reason": "unfinished_piece_at_stop"}
            self._record("gap", **fields)
            self._event("live_gap", **fields)

    def _process(self, block):
        import numpy as np
        channels = max(1, int(block.get("channels", 1)))
        pcm = np.frombuffer(block.get("pcm_bytes", b""), dtype="<i2")
        pcm = pcm[:len(pcm) // channels * channels]
        if not len(pcm):
            return
        audio = pcm.reshape(-1, channels).astype(np.float32).mean(axis=1) / 32768.0
        rate = int(block["sample_rate"])
        track = block["track_id"]
        start_frame = int(block.get("start_frame", 0))
        state = self._last_segments.setdefault(track, {
            "base_frame": start_frame, "audio": np.empty(0, dtype=np.float32), "consumed": 0,
            "rate": rate,
        })
        if state["rate"] != rate or state["base_frame"] + len(state["audio"]) != start_frame:
            if len(state["audio"]) > state["consumed"]:
                self._record("gap", source=block["kind"], track_id=track,
                             start=state["base_frame"] / state["rate"],
                             end=(state["base_frame"] + len(state["audio"])) / state["rate"],
                             reason="discontinuous_audio")
            state.update(base_frame=start_frame, audio=np.empty(0, dtype=np.float32), consumed=0, rate=rate)
        state["audio"] = np.concatenate((state["audio"], audio))
        segmenter = self.segmenter
        if segmenter is None:
            from live_audio_segments import segment_audio
            segmenter = segment_audio
        segments = self._segments(segmenter, state["audio"], rate, final=False)
        for segment in segments:
            if self._already_committed(segment, state["consumed"]):
                continue
            text = self.transcriber(segment.audio_16k)
            state["consumed"] = int(segment.end_sample)
            if not text or not str(text).strip():
                continue
            source = block["kind"]
            name = self._device_name(block)
            start = (state["base_frame"] + int(segment.start_sample)) / rate
            end = (state["base_frame"] + int(segment.end_sample)) / rate
            fields = {"source": source, "track_id": track, "device": name,
                      "start": start, "end": end, "text": str(text).strip()}
            self._record("committed", **fields)
            self._event("live_caption", **fields, provisional=False)
        now = time.monotonic()
        if self.preview_interval is not None and (self.preview_interval == 0 or
                now - state.get("last_preview", 0.0) >= self.preview_interval) and self.queue.empty():
            preview_segments = self._segments(segmenter, state["audio"], rate, final=True)
            candidate = next((seg for seg in reversed(preview_segments)
                              if int(seg.end_sample) > state["consumed"]), None)
            if candidate is not None:
                preview = self.transcriber(candidate.audio_16k)
                state["last_preview"] = now
                if preview and str(preview).strip():
                    self._event("live_caption", source=block["kind"], track_id=track,
                                device=self._device_name(block),
                                start=(state["base_frame"] + int(candidate.start_sample)) / rate,
                                end=(state["base_frame"] + int(candidate.end_sample)) / rate,
                                text=str(preview).strip(), provisional=True)
        # Keep a bounded rolling window while retaining unconsumed speech.
        limit = rate * 12
        trim = max(0, min(state["consumed"], len(state["audio"]) - limit))
        if trim:
            state["audio"] = state["audio"][trim:]
            state["base_frame"] += trim
            state["consumed"] -= trim

    def stop(self, timeout=2.0):
        if self._thread is None:
            if self._worker_unavailable:
                self._drain_unavailable()
            if self.transcriber is not None:
                self._event("live_status", status="stopped", detail="Local captions stopped")
            return True
        self._stop.set()
        self._thread.join(max(0.0, float(timeout)))
        alive = self._thread.is_alive()
        if alive:
            fields = {"reason": "stop_drain_timeout"}
            self._record("gap", **fields)
            self._event("live_gap", **fields)
            self._event("live_status", status="lagging", detail="Caption drain timed out")
        else:
            if self._worker_unavailable:
                self._drain_unavailable()
        return not alive

    def _drain_unavailable(self):
        while True:
            try:
                block = self.queue.get_nowait()
            except queue.Empty:
                break
            self._note_dropped_block(block, "model_unavailable")
            self.queue.task_done()
        self._persist_overflow()

    def report_tap_error(self, *, reason, source=None, track_id=None):
        """Persist a capture-tap failure from the recording worker, never the audio callback."""
        self._record("error", reason=reason, source=source, track_id=track_id,
                     error_type="audio_tap_failure")
