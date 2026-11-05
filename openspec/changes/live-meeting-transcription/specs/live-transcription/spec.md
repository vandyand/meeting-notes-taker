# Spec Delta

## Purpose

Provides timely, source-labeled captions during a meeting while preserving the full audio recording and its separate final transcription workflow.

## ADDED Requirements

### Requirement: Live captions start with recording
The system SHALL offer local live transcription during recording, show whether it is starting, active, lagging, unavailable, or stopped, and keep recording usable when live transcription is unavailable.

#### Scenario: Successful start
- **WHEN** a meeting starts with working microphone and system-output capture tracks and the local transcription model is available
- **THEN** the recording view shows live transcription starting and then active without requiring post-meeting processing

#### Scenario: Model unavailable
- **WHEN** the local transcription model cannot load
- **THEN** the recording continues and the view shows that live captions are unavailable with a diagnostic reason

### Requirement: Both sides are represented without duplicate device streams
The system SHALL present one live microphone source and one live system-audio source at a time, identify their captured devices, and visibly report a source change. Source labels SHALL describe audio origin rather than claim speaker diarization.

#### Scenario: Multiple input and output devices
- **WHEN** several microphone and output tracks are being recorded
- **THEN** the live view identifies the active source for each kind and does not render parallel duplicate caption streams from inactive devices

#### Scenario: Source changes during a meeting
- **WHEN** speech moves to another captured device of the same kind
- **THEN** subsequent live captions use that source and the view reports the change without restarting the recording

### Requirement: Speech is captioned promptly with short, pause-aware pieces
The system SHALL produce provisional text during an unfinished speech piece when inference capacity permits, and commit captions at natural pauses or before any uninterrupted speech piece reaches ten seconds. Committed captions SHALL replace, not duplicate, their provisional text.

#### Scenario: Pause after speech
- **WHEN** speech is followed by a detectable pause
- **THEN** the corresponding source receives a committed caption for that speech piece and its provisional text is cleared

#### Scenario: Continuous speech
- **WHEN** speech continues without a usable pause
- **THEN** the system cuts the speech into pieces shorter than ten seconds and continues captioning the later audio

#### Scenario: Two simultaneous sources
- **WHEN** the microphone and system audio both contain speech
- **THEN** each source advances independently and neither source's text is silently attributed to the other

### Requirement: Live captions have recoverable timing and status
The system SHALL save committed captions per meeting with source identity and track-relative start and end times. Saved captions SHALL remain readable after stopping or reopening the meeting, and an interrupted meeting SHALL retain captions already committed.

#### Scenario: Normal stop
- **WHEN** the user stops a meeting
- **THEN** pending speech is finalized or explicitly marked incomplete, committed captions remain available in the meeting's review, and the live status changes to stopped

#### Scenario: Abrupt application exit
- **WHEN** the application exits after committing one or more captions
- **THEN** reopening the meeting exposes the committed captions without requiring audio reprocessing

### Requirement: Transcription cannot compromise audio capture
The system SHALL not make audio capture wait for model inference, and SHALL keep the saved WAV files authoritative. It SHALL visibly identify any live-caption gap, lag, or failure and preserve the corresponding audio for post-meeting transcription.

#### Scenario: Inference falls behind
- **WHEN** the live worker cannot keep up with captured speech
- **THEN** WAV recording continues, the UI reports lag or a caption gap, and the app does not report the live transcript as complete

#### Scenario: Transcription error
- **WHEN** transcription fails for a piece
- **THEN** the recording continues, the affected interval is marked as a live-caption error, and the failure does not erase earlier captions

### Requirement: Final processing remains separate
The system SHALL keep the existing post-meeting transcription and note-generation flow independent of live captions. Live captions SHALL not be represented as the final transcript or silently substitute for a failed final transcription.

#### Scenario: Processing after a live-captioned meeting
- **WHEN** the user processes a meeting after live transcription
- **THEN** the selected recorded audio tracks are transcribed by the configured final backend and final notes use that final transcript

#### Scenario: Older meeting
- **WHEN** the user opens a meeting captured before this capability exists
- **THEN** existing recording, review, and processing features continue to work without a live-caption artifact
