# Design

## Context

See `proposal.md` for motivation and `specs/live-transcription/spec.md` for the behavioral contract. The Tk app currently records every available microphone and speaker loopback as 48 kHz WAV tracks, with one capture thread per track and 100 ms reads. The UI receives low-rate events through a thread-safe queue. Post-meeting Whisper/cloud transcription is separate. On this PC, cached CPU Moonshine processed 96 seconds of alternating speech clips in 13.86 seconds after model load; the latest meeting had five recorded devices but only one active headset mic and one active output in the sampled interval.

## Goals / Non-Goals

**Goals:** Keep WAV writing authoritative; provide timely, source-labeled provisional and committed captions; bound CPU/memory work; recover committed text and expose incomplete intervals; validate capture safety and two-track latency with synthetic and real saved audio.

**Non-Goals:** Person-level diarization within system audio, replacing final Whisper/cloud transcription, cloud upload, real-time translation, or changing the recorded WAV format. Live text is an aid during the meeting, not the final transcript used for notes.

## Decisions

### 1. Tap normalized PCM after a successful WAV write

`write_frames` will form one immutable PCM16 byte string, write it to WAV, then pass that same payload plus track identity, format, and starting frame offset to an optional non-throwing tap. The tap performs only bounded, non-blocking enqueue; capture threads never load a model, resample audio, wait on inference, or send raw PCM through the Tk event queue. A tap exception becomes a live-transcription error, not a capture-stream failure. Alternative: tail a growing WAV; rejected because it repeatedly reads an open file, complicates timing and finalization, and adds disk I/O.

### 2. One coordinator and one CPU inference worker

A coordinator receives immutable blocks from all opened tracks through a bounded queue. It downmixes and resamples outside capture threads, maintains per-track sample cursors and short rolling buffers, and serializes model inference through one prepared Moonshine instance. It reuses the dictation app's 30 ms energy frames, adaptive noise threshold, 450 ms pause detection, silence trimming, and quietest-point forced cuts, with an eight-second maximum and a four-second preferred minimum. The meeting project will carry its own tested adaptation of this algorithm, avoiding a runtime dependency on a sibling repository. Alternative: a model per track or concurrent calls to one model; rejected for avoidable CPU/memory use and uncertain ONNX thread safety.

### 3. Select one source of each kind with hysteresis and pre-roll

The recorder still saves all devices. Live inference follows one microphone and one system-output track. Initial preference is the default physical microphone and default output when present; a Stereo Mix input is not preferred as the user's mic. The coordinator watches short-term speech activity on all tracks and changes source only when the current source is quiet and an alternative has sustained activity, then includes a short buffered pre-roll. It emits the chosen device names and source-change events. Alternative: transcribe all devices and deduplicate text later; rejected because loopback and Stereo Mix can produce duplicate captions and waste CPU. Source labels say “Microphone” and “Meeting audio,” not person names.

### 4. Separate provisional display from durable captions

When the worker has capacity, it refreshes a provisional caption for an unfinished speech piece at most once every 1.5 seconds. Completed pieces replace that provisional text and append a JSONL record under the meeting's `transcripts/` directory with schema version, source kind, track ID/device name, track-relative start/end seconds, text, and status. Each committed record is flushed. The UI gets only compact text/status events and displays a scrollable two-source timeline plus current provisional lines. Reopening Review loads the committed records; it does not infer a completed final transcript from them. Alternative: update one growing Markdown file on every preview; rejected because frequent rewrites are fragile and blur provisional versus committed text.

### 5. Backpressure and failures preserve capture

The queue and per-track buffers have explicit small limits. Under pressure, low-priority provisional inference is skipped first. If committed speech cannot be queued, the coordinator records an interval gap and reports lag/incomplete status rather than blocking a capture thread or silently dropping text. The saved WAV retains that interval for final processing. Model load failures, per-piece errors, queue overflow, and stop-time flush timeout produce visible status and persisted diagnostic records. The UI never reports “complete” when a gap remains. A stop request finalizes WAVs first, then gives the live worker a bounded drain/flush window; timeout does not hold the app indefinitely or discard prior captions.

### 6. Preserve the final processing boundary

No existing final-transcript JSON or note-generation input changes. The UI distinguishes “Live captions” from “Final transcript”; processing still uses selected WAV tracks and the chosen final backend. Old meetings without a live sidecar continue through the existing path.

## Risks / Trade-offs

- [Moonshine may hallucinate or omit words, especially with two simultaneous speakers] → Label captions provisional, keep sources separate, preserve final transcription.
- [A selected audio device can change mid-meeting] → Activity hysteresis, source-change event, pre-roll, and tests with device switches.
- [Preview inference can compete with committed work] → One serialized worker; committed pieces take priority and previews are dropped under lag.
- [A full queue cannot guarantee every live word] → Explicit gap intervals and incomplete state while WAV capture and final transcription remain intact.
- [Model cache/package may be absent on another Windows install] → Lazy local-only preflight, clear install guidance, no network or model download during recording.
- [Tk and model work share a process] → No inference on Tk or capture threads; bounded text events and lifecycle tests.

## Migration Plan

No recording-format migration. Add the optional live-caption sidecar and UI; existing meetings without it remain valid. Validate offline with fake recorders/models, a saved two-track meeting sample, a stop/reopen walkthrough, and full project tests. Deploy by restarting only the known meeting app after verifying it is idle; do not stop it while it is recording. Rollback is code rollback/removal of the optional live path, leaving all WAV and final transcripts intact.
