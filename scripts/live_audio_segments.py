"""Pause-aware mono audio segmentation for low-latency, per-track transcription.

Public API: ``segment_audio(audio, sample_rate) -> list[Segment]``. Boundaries are
sample offsets in the original track; ``audio_16k`` is the trimmed model input.
Call once per track so timing and adaptive noise estimates remain independent.
"""
from dataclasses import dataclass

import numpy as np

MODEL_RATE = 16_000
FRAME_SECONDS = .030
FRAME = int(MODEL_RATE * FRAME_SECONDS)
MIN_SECONDS = 4.0
MAX_SECONDS = 8.0
PAUSE_SECONDS = .45
LEAD_SECONDS = .30
TAIL_SECONDS = .80
_DIGITAL_SILENCE_DB = -120.0


@dataclass(frozen=True)
class Segment:
    start_sample: int
    end_sample: int
    source_sample_rate: int
    audio_16k: np.ndarray


def _mono_float(audio):
    data = np.asarray(audio)
    if data.ndim == 2:
        # Accept either [frames, channels] or [channels, frames].
        axis = 1 if data.shape[1] <= 8 else 0
        data = data.astype(np.float32).mean(axis=axis)
    if data.ndim != 1:
        raise ValueError("audio must be mono or a two-dimensional channel array")
    data = data.astype(np.float32, copy=False)
    if np.issubdtype(np.asarray(audio).dtype, np.integer):
        info = np.iinfo(np.asarray(audio).dtype)
        data = data / max(abs(info.min), info.max)
    return np.nan_to_num(data, copy=False)


def _resample(audio, sample_rate):
    if sample_rate == MODEL_RATE:
        return np.asarray(audio, dtype=np.float32)
    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive")
    if sample_rate % MODEL_RATE == 0:
        factor = sample_rate // MODEL_RATE
        usable = len(audio) // factor * factor
        if usable == 0:
            return np.zeros(0, dtype=np.float32)
        return audio[:usable].reshape(-1, factor).mean(axis=1, dtype=np.float32)
    count = round(len(audio) * MODEL_RATE / sample_rate)
    if count == 0:
        return np.zeros(0, dtype=np.float32)
    old = np.arange(len(audio), dtype=np.float64)
    new = np.arange(count, dtype=np.float64) * sample_rate / MODEL_RATE
    return np.interp(new, old, audio).astype(np.float32)


def _frame_db(audio):
    count = len(audio) // FRAME
    if not count:
        return np.zeros(0, dtype=np.float32)
    frames = audio[:count * FRAME].reshape(count, FRAME)
    rms = np.sqrt(np.mean(frames * frames, axis=1))
    return 20 * np.log10(rms + 1e-7)


def _threshold(db):
    if not len(db):
        return float("inf")
    live = db[db > _DIGITAL_SILENCE_DB]
    floor = np.percentile(live, 3) if len(live) >= 10 else float(db.min())
    loud = np.percentile(db, 95)
    if loud - floor < 10:
        # Flat-but-audible input still needs maximum-duration cuts. Flat digital
        # silence must remain unclassified as speech.
        return loud - 6.0 if loud > -80.0 else float("inf")
    return float(floor + .35 * (loud - floor))


def _cuts(audio, db, threshold, min_seconds, max_seconds, pause_seconds):
    min_frames = max(1, int(min_seconds * MODEL_RATE / FRAME))
    max_frames = max(1, int(max_seconds * MODEL_RATE / FRAME))
    pause_frames = max(1, int(pause_seconds * MODEL_RATE / FRAME))
    cuts, segment_start, quiet_start, heard, speech_frames = [], 0, None, False, 0
    frame = 0
    while frame < len(db):
        if db[frame] < threshold:
            if quiet_start is None:
                quiet_start = frame
            if heard and frame - quiet_start + 1 >= pause_frames:
                cut = quiet_start + pause_frames // 2
                # A clear pause closes any meaningful utterance. min_seconds is
                # a preference for natural grouping, not a delay imposed on captions.
                if speech_frames >= 5:
                    cuts.append(cut * FRAME)
                    segment_start, heard, speech_frames = cut, False, 0
        else:
            quiet_start, heard = None, True
            speech_frames += 1
        if frame - segment_start + 1 >= max_frames:
            lo = min(frame, segment_start + max_frames * 2 // 3)
            cut = lo + int(np.argmin(db[lo:frame + 1]))
            cuts.append(cut * FRAME)
            segment_start, quiet_start, heard, speech_frames = cut, None, True, 1
            frame = cut
        frame += 1
    return cuts


def segment_audio(audio, sample_rate, *, min_seconds=MIN_SECONDS,
                  max_seconds=MAX_SECONDS, pause_seconds=PAUSE_SECONDS, final=False):
    """Return completed trimmed pieces; set ``final`` to flush the unfinished tail.

    This distinction makes repeated calls on a growing per-track buffer safe: only
    pieces closed by a pause or forced-duration cut are returned until stream end.
    """
    if max_seconds <= 0 or min_seconds < 0 or min_seconds > max_seconds:
        raise ValueError("require 0 <= min_seconds <= max_seconds")
    source = _mono_float(audio)
    mono = _resample(source, int(sample_rate))
    if not len(mono):
        return []
    db = _frame_db(mono)
    threshold = _threshold(db)
    cuts = _cuts(mono, db, threshold, min_seconds, max_seconds, pause_seconds)
    bounds = [0, *cuts]
    if final:
        bounds.append(len(mono))
    result = []
    ratio = sample_rate / MODEL_RATE
    for start, end in zip(bounds, bounds[1:]):
        piece_db = _frame_db(mono[start:end])
        if not len(piece_db) or np.count_nonzero(piece_db >= threshold) < 5:
            continue
        loud = np.flatnonzero(piece_db >= threshold)
        lo = max(start, start + int(loud[0] * FRAME - LEAD_SECONDS * MODEL_RATE))
        hi = min(end, start + int((loud[-1] + 1) * FRAME + TAIL_SECONDS * MODEL_RATE))
        # Map model offsets to exact source offsets; 48 kHz is exactly divisible by 16 kHz.
        source_lo = max(0, int(round(lo * ratio)))
        source_hi = min(len(source), int(round(hi * ratio)))
        if source_hi <= source_lo:
            continue
        result.append(Segment(source_lo, source_hi, int(sample_rate), mono[lo:hi].copy()))
    return result
