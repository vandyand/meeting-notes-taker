"""Run the installed Whisper CLI with structured, per-file decoder progress.

The CLI still owns decoding defaults, model loading (once), and output formats.
Only transcribe's frame progress reporter is replaced, within this child process.
"""
from __future__ import annotations

import importlib
import json
import sys
from types import SimpleNamespace

CONTEXT: dict = {}


def emit(**values) -> None:
    print(json.dumps({"whisper_progress": True, **CONTEXT, **values}), flush=True)


class FrameProgress:
    def __init__(self, *, total, **_kwargs):
        self.total = total
        self.n = 0
        self.update(0)

    def update(self, amount):
        # Whisper skips no-speech windows before its tqdm.update call. Summing
        # those deltas undercounts silence. At this single adapter boundary,
        # read the decoder's absolute seek instead. Do not advance on decode()
        # return: timestamp handling can subsequently choose a smaller seek.
        caller = sys._getframe(1)
        position = self.n + amount
        if caller.f_code.co_name == "transcribe" and "content_frames" in caller.f_locals:
            position = caller.f_locals.get("seek", position)
        del caller
        self.n = max(0, min(self.total, position))
        # Whisper's mel frames are 10 ms each (100 frames per audio second).
        emit(phase="decoding", item_percent=100 * self.n / self.total if self.total else 100,
             audio_seconds=self.n / 100, audio_total_seconds=self.total / 100)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *_args):
        if exc_type is None:
            # Includes all-silent files and trailing silence. Never turn an
            # exception/partial decode into a 100% event.
            self.update(self.total - self.n)
        return False


def main() -> None:
    emit(phase="loading")
    module = importlib.import_module("whisper.transcribe")
    original_transcribe, original_tqdm = module.transcribe, module.tqdm
    current = 0

    def transcribe(model, audio, **kwargs):
        nonlocal current
        current += 1
        CONTEXT.clear()
        CONTEXT.update(item=str(audio), current=current)
        emit(phase="preparing")
        result = original_transcribe(model, audio, **kwargs)
        emit(phase="saving")
        return result

    module.transcribe = transcribe
    module.tqdm = SimpleNamespace(tqdm=FrameProgress)
    try:
        module.cli()
    finally:
        module.transcribe, module.tqdm = original_transcribe, original_tqdm


if __name__ == "__main__":
    main()
