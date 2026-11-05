# Windows Capture Spike

The Windows MVP records local meeting audio without meeting-platform APIs.

## Capture Model

- `system/*.wav`: one WASAPI loopback track for each available speaker/output.
- `microphones/*.wav`: one input track for each microphone the helper can open.
- `screenshots/*.png`: optional automatic screen-context captures.
- `screenshots/imported/*`: screenshots the user saved through Windows during
  the meeting and the app copied into the session.
- `metadata.json`: mode, devices, sample rate, channels, duration, output paths,
  and any devices that failed to open.

This avoids BlackHole/virtual cable style routing. The helper opens devices for
capture only; it does not change Windows default input/output devices.

## Commands

```powershell
python -m pip install --user soundcard numpy pillow
python scripts/windows_capture.py list-devices
python scripts/windows_capture_ui.py
python scripts/windows_capture.py record --seconds 10 --out-dir recordings/spike --all-devices
```

`python scripts/windows_capture_ui.py` launches a small Windows Tkinter app. It shows current speakers and
microphones, starts an all-device capture into a timestamped folder, displays
which devices are actively recording and the file names being written, and
stops gracefully so WAV headers are finalized. It also includes buttons to
process the latest or selected meeting and open the latest notes/transcript
files. The UI has tabs for device status, meeting history, and an embedded notes
editor. Meeting titles are recommended by the LLM after processing and can be
edited in the UI.

When `Capture Screen Context` is enabled, the Windows UI saves periodic
screenshots into the current session. The scope can be `Full desktop`,
`Active window`, or `Selected window`. `Full desktop` captures all screens;
`Active window` captures the current foreground window at each interval;
`Selected window` lets the user choose a visible top-level meeting window before
recording and then repeatedly captures that window's current bounds. The app
also watches the normal Windows Screenshots folders and imports screenshots
saved during the meeting.

`Selected window` is the best no-API approximation for "capture the meeting
window as a whole." It can survive ordinary focus changes and windows covering
the meeting window. Minimized windows and windows moved to another virtual
desktop may still be unreliable because Windows can stop rendering them.

The all-device record command uses `--all-devices` so changing meeting speaker/mic mid-call is
less likely to lose audio. Post-processing can later select or merge the
non-silent tracks.

To select devices by name substring:

```bash
powershell.exe -NoProfile -Command \
  "python scripts/windows_capture.py record --seconds 10 --out-dir recordings/spike --speaker 'Realtek' --microphone 'USB'"
```

The direct `python ... record` command above records only one speaker and one
mic unless `--all-devices` is passed.

On this host, `Microphone (USB CAMERA)` is the Windows default microphone but
the `soundcard` backend raises a MediaFoundation assertion when opening it. The
all-device helper records that failure in `metadata.json` and continues with
working devices such as `Headset (WH202A)`.

To force the old single-device path and pin a working mic:

```bash
powershell.exe -NoProfile -Command \
  "python scripts/windows_capture.py record --seconds 10 --out-dir recordings/spike --microphone 'Headset'"
```

## Current Host Smoke Test

The latest local all-device smoke wrote five valid 2-second WAV files:

- 3 system loopback tracks: LG Ultrawide, WH202A headphones, Realtek digital output.
- 2 microphone tracks: WH202A headset, Realtek Stereo Mix.
- 1 skipped mic: USB camera mic, recorded as an `AssertionError` failure in metadata.

## Transcript Cleanup

After Whisper writes JSON transcripts for selected tracks, run:

```powershell
python scripts/clean_transcript.py recordings/desktop/<session-id>
```

The cleaner is deterministic. It computes short-window RMS values per track,
derives track-local `p10`, `p50`, and `p95` levels, and scores each Whisper
segment relative to that track's own dynamic range. It then drops segments that
combine weak relative audio, suspicious Whisper decode metrics, and repeated
text, especially weak mic text overlapping active remote/system audio.

## Latest Meeting Processing

The UI's `Process Latest` button runs the same native Windows command as:

```powershell
python scripts/process_latest.py --latest
```

The processing script:

- finds the newest `recordings/desktop/<timestamp>` session,
- exits with an "already processed" message if `transcripts/notes.md` exists,
- runs OCR over captured screenshots through the Windows-side Ollama model
  `glm-ocr` when available, falling back to a local vision model such as
  `gemma3:latest`,
- selects the loudest active system track and loudest active non-Stereo-Mix mic,
- runs Whisper only for transcripts that are missing,
- runs deterministic transcript cleanup,
- sends the cleaned transcript to OpenRouter's OpenAI-compatible chat
  completions endpoint with OCR visual context, and
- writes `transcripts/notes.md`, `transcripts/actions.md`,
  `transcripts/diagnostics.md`, `meeting.json`, and
  `transcripts/process_metadata.json`.

Set `OPENROUTER_API_KEY` in the Windows environment or in a repo-local `.env`. The default notes
model is `gpt-5.6-terra`; set `OPENROUTER_MODEL` to change it.

## Product Direction

Keep the Python helper as a spike. If it is reliable, package it. If not, replace
it behind the same Clojure component with:

- NAudio/.NET for a durable Windows-only helper.
- miniaudio for a single cross-platform native helper family.
