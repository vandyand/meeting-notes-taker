# Tasks

## 1. Capture-safe audio feed

- [x] 1.1 Add an optional post-WAV-write PCM tap with track metadata and sample offsets, isolating tap failures from capture; verify fake-recorder tests prove byte identity, ordering, and continued WAV writing on tap failure.
- [x] 1.2 Wire the tap through all-device recording and stop/finalization without changing metadata or WAV formats; verify capture and recovery tests cover normal stop, failed track, and missing live consumer.

## 2. Local short-piece transcription

- [x] 2.1 Add and test a self-contained 16 kHz mono, pause-aware segmenter adapted from the dictation app, with silence trim, source-relative boundaries, and forced cuts below ten seconds; verify synthetic silence, continuous speech, two tracks, and resampling tests.
- [x] 2.2 Add a cached, local-only CPU Moonshine adapter using one serialized model instance, with explicit missing-dependency/model errors; verify fake-model tests and a no-text-output real eight-second WAV smoke test.

## 3. Coordinator, source routing, and durable captions

- [x] 3.1 Implement bounded non-blocking audio intake and one inference worker, prioritizing committed pieces over disposable previews; verify queue-saturation tests show capture callback latency remains bounded and gaps/lag become observable.
- [x] 3.2 Implement active microphone/system source selection with default-device preference, Stereo Mix avoidance, hysteresis, pre-roll, and source-change events; verify multi-device and mid-meeting switch tests avoid duplicate caption streams.
- [x] 3.3 Persist committed captions and error/gap records with track-relative times, flush on commit, and bounded stop behavior; verify normal stop, reopen, abrupt-exit simulation, and failed-piece tests retain prior captions and mark incomplete intervals.

## 4. Native recording and review experience

- [x] 4.1 Add a Live Transcript view with clear source/device labels, provisional versus committed text, and starting/active/lagging/unavailable/stopped states; verify Tk UI tests for event routing, no duplicate provisional lines, and a responsive stop.
- [x] 4.2 Add selected-meeting review of saved live captions without presenting them as the final transcript; update `DESIGN.md` and `README.md` with status, privacy, dependency, and recovery behavior; verify reopen/older-meeting UI tests and a native walkthrough.

## 5. Integrated acceptance

- [x] 5.1 Run an offline two-track saved-WAV replay through the real coordinator and Moonshine, measuring source attribution, committed timing, no-text-leak output, and sustained throughput; verify the report shows no silent gaps and inference remains ahead of recorded time under the tested load.
- [x] 5.2 Run `python -m unittest discover -s tests -p 'test_*.py'`, `bb test`, `openspec validate live-meeting-transcription --strict`, and a capture/review/final-processing regression walkthrough; verify all pass or record exact failures before claiming completion.
- [x] 5.3 Verify the installed app is idle before restarting its exact PID, then exercise a short live microphone/system capture in the restarted app and confirm WAV integrity, captions, source labels, stop/reopen behavior, and unchanged final processing; if a safe live-device run is unavailable, leave this task open and report the specific blocker.
