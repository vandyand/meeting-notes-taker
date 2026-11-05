# Proposal

## Why

Meeting Notes Capture saves audio reliably, but the user cannot see what either side said until post-meeting processing finishes. Local CPU Moonshine is already available on this Windows PC and is fast enough to provide useful live captions without taking GPU capacity away from OCR or final transcription.

## What Changes

- Show near-real-time, source-labeled captions for the user's microphone and the meeting's system audio while a meeting is being recorded.
- Reuse the dictation app's silence-aware short-chunk approach, including provisional updates during speech and committed captions at pauses or a forced sub-10-second cut.
- Keep the WAV recorder authoritative and non-blocking: transcription lag or failure must not interrupt capture, and gaps or degraded status must be visible.
- Persist committed live captions with source-relative timestamps for review and recovery. Preserve the existing post-meeting Whisper/cloud transcription and note-generation flow as the final transcript path.
- Handle multiple captured devices by choosing one active microphone and one active system-output source for the live view, with source identity visible and changes signaled.

## Capabilities

### New Capabilities

- `live-transcription`: In-meeting two-source captions, source selection, status, persistence, and capture-safe failure handling.

### Modified Capabilities

None; this project has no existing OpenSpec capability specs.

## Impact

The Windows audio capture helper, Tk recording UI, design contract, dependency guidance, and automated capture/transcription/UI tests change. Recorded WAV files and the post-meeting processing artifacts retain their current formats and remain the source of truth.
