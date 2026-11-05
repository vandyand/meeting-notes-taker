# Meeting Notes Taker

Native Windows local meeting recorder.

The first implementation avoids Zoom/Meet/Slack/Teams APIs and avoids virtual
audio devices. A small Windows-native helper captures:

- every available speaker/output through WASAPI loopback
- every working microphone/input through normal capture
- optional screen-context screenshots during recording

The Clojure/Polylith side launches the helper and post-processes its artifacts.

## Quick Start

From PowerShell in the project folder:

```bash
python -m pip install --user soundcard numpy pillow
# Optional local live captions (CPU; cached model, no cloud upload):
python -m pip install --user useful-moonshine-onnx huggingface-hub
# Optional: native app icons and process-aware browser ordering in Choose Window
python -m pip install --user pywin32 psutil
python scripts/windows_capture.py list-devices
python scripts/windows_capture_ui.py
# Optional processing dependencies:
python -m pip install openai-whisper
python scripts/process_latest.py --latest
.\scripts\create_windows_shortcut.ps1
```

The processor invokes Whisper through this same Python installation, so the
`whisper.exe` Scripts directory does not need to be on `PATH`.

Expected outputs:

- `recordings/spike/system/*.wav`
- `recordings/spike/microphones/*.wav`
- `recordings/spike/metadata.json`

Run unit tests:

```bash
bb test
```

The desktop UI shows detected speakers/microphones, starts an all-device
recording, shows which devices are recording and the file names being written,
live audio levels and write heartbeats, and stops cleanly so WAV files are
finalized. It also has meeting history, recommended-title editing, an embedded
notes editor, screenshot and audio-track review, processing progress with
cancel/retry, and buttons to open notes, transcripts, action items, diagnostics,
visual OCR, and folders. See [DESIGN.md](DESIGN.md) for the interaction contract.
The Screenshot Review dialog displays thumbnails for every captured frame and
can select all or deselect all without deleting the original screenshots.
It opens immediately, reports background analysis progress, and loads cached
previews in pages of 40. Click a preview to open the original screenshot.
Use **Crop / mask all** to keep a useful region for every screenshot (including
a **Middle third** preset), or **Crop / mask this image** for individual edits.
Originals are preserved; OCR reads new files in `screenshots/.ocr-inputs/`.
The Processing tab previews the exact image being OCR'd. New captures default
to **Active window**.
Use **Include selected screenshots in OCR** in the processing panel to skip
OCR for the whole meeting. The Local/Cloud OCR choice is unavailable while
OCR is off. **Capture Screen Context** can be turned on or off during a
recording without interrupting audio.
Starting a new recording clears the previous completion bar. Selecting a
completed meeting restores its full completion bar.

### Live captions during recording

The **Live Transcript** tab displays local CPU Moonshine captions for one
microphone and one meeting-audio device, identifying the device rather than
claiming person-level diarization. Text still being spoken is marked
**Provisional** and is replaced by a committed caption after a pause or short
forced cut. The model must already be cached locally; recording does not
download it or upload audio to a cloud service. If the model is missing,
inference lags, or a caption interval fails, the app shows an unavailable or
incomplete state while continuing to save the WAV files. The **Review** tab
can reopen committed captions from `transcripts/live_captions.jsonl`, even
after an interrupted capture. These approximate captions are separate from
the final transcript used for notes; the normal post-meeting transcription
and processing settings are unchanged.

After transcribing useful tracks with Whisper, clean and merge the transcript:

```bash
bb transcript:clean recordings/desktop/<session-id>
```

The cleanup step uses per-track RMS percentiles to identify active segments,
then combines that with Whisper decode quality and repeated-text checks to drop
likely silence hallucinations.

To process the most recent desktop capture end-to-end:

```bash
OPENROUTER_API_KEY=... bb process:latest
```

The processor imports visual context from captured screenshots, OCRs it through
the Windows-side Ollama model `glm-ocr` when available, falls back to an
available local vision model such as `gemma3:latest`, selects the strongest
system/output track and strongest physical mic track, runs Whisper for missing
transcripts, runs deterministic cleanup, and writes OpenRouter-generated notes
to `transcripts/notes.md`. The model defaults to `openai/gpt-6-luna`; override it
with `OPENROUTER_MODEL` if needed. If `notes.md` already exists, it exits
successfully with an "already processed" message instead of overwriting it.

Important generated files:

- `meeting.json`: recommended title and user-edited title metadata
- `transcripts/notes.md`: editable final meeting notes
- `transcripts/live_captions.jsonl`: recoverable approximate live captions and
  gap/error records, when live transcription runs
- `transcripts/actions.md` and `actions.json`: extracted action items
- `transcripts/diagnostics.md`: track activity/selection diagnostics
- `visual/ocr.md`: OCR text extracted from screenshots
- `screenshots/*.png`: automatic screen-context captures from full desktop,
  active-window, or selected-window scope
- `screenshots/manifest.json`: automatic screenshot selection plus any manual
  include/exclude overrides made in the Review tab

## Notes

The recorder and processor run with the Windows Python installation. Optional
OCR uses a locally running Ollama service; notes generation requires
`OPENROUTER_API_KEY` in the Windows environment or a repo-local `.env` file.

By default, all new recordings and processed artifacts are stored under
`C:\Users\vandy\meeting-notes-taker\recordings\desktop`. You can still choose
another destination in the app's `Recordings` field.

Install the preferred local OCR model on Windows with:

```powershell
winget install --id Ollama.Ollama --exact
& "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe" pull glm-ocr
```

The processor automatically prefers `glm-ocr` when it is available. Before
OCR begins, it checks the Ollama HTTP API. If the local Windows API is not
running, the processor starts `ollama.exe serve` in the background, waits for
the model catalog to become ready, and reports that startup in processing
progress. A transient connection loss or server-overload response during OCR
recovers the service and retries only the current screenshot; completed OCR
checkpoints are retained. The OCR model is unloaded after the visual stage so
Whisper can use the GPU, while the lightweight Ollama API may remain running.
Automatic startup applies only to localhost; a configured remote Ollama host
must be managed on its own machine.

Each OCR request explicitly asks Ollama for a 16,384-token context, including
when reusing an already-running server. Override it with
`MEETING_OCR_CONTEXT_LENGTH` in the processing environment or
`--context-length` when running `scripts/windows_ocr.py` directly. The value
must be a positive integer; larger contexts require more GPU memory and must
fit the selected model's capabilities. The requested value is logged in OCR
progress and `visual/ocr.json` as `requested_context_length`, both for the run
and each newly attempted image. Older successful checkpoints keep their
original metadata and are reused when you resume processing.

### OpenRouter credentials on Windows

The processor looks first for `OPENROUTER_API_KEY` in the environment or a
repo-local `.env`. On Windows it also tries the default PowerShell SecretStore
vault `DevSecrets` with a secret named `OPENROUTER_API_KEY`; set a different
secret name with `OPENROUTER_SECRET_NAME`.

This Windows setup automatically uses the DPAPI-protected `Unlock-DevSecrets`
helper from `$HOME\.config\powershell\profile.ps1`, keeps the retrieved API key
only in app memory, and passes it to the processing child. No manual unlock
button is needed. Use DevSecrets, not a plaintext `.env`, for Windows credentials.

### Processing feedback and recovery

**Resume processing** immediately opens Processing with an animated preparation
indicator. Completed OCR and audio checkpoints are reused. Old failure banners
are replaced by the current run, and an interrupted run can be resumed after
reopening. Messages wrap below the progress bar; Enter or double-click on a
stage/image/audio row opens its full details.

Audio progress shows **file x of y**, **% of audio decoded**, and **decoded audio
position / duration**. It is not an elapsed processing timer or predicted finish
time. Decoder updates arrive as windows finish, with a one-second heartbeat in
between. Loading/preparation is explicitly indeterminate. Missing transcript
outputs fail visibly even if the Whisper CLI returns a successful exit code.

OpenRouter HTTP **402** is a funding/spending-limit problem, not a vault unlock
problem. Check [credits](https://openrouter.ai/settings/credits), add funds if
needed, then Resume. Technical response details are saved in
`transcripts/notes_openrouter_error.json`; no successful OCR or audio output is
deleted. Notes prompts contain every accepted transcript segment with compact
track aliases; rejected segments stay in the local cleanup audit and are not
sent to the notes model. The app does not silently truncate speech or switch
models to fit a credit balance.

### GPU Whisper acceleration

On this Windows machine, the processor automatically uses CUDA whenever
PyTorch can see the NVIDIA GPU; progress will say `on CUDA`. The verified
CUDA-enabled PyTorch install is:

```powershell
python -m pip install --user --force-reinstall torch==2.7.1 --index-url https://download.pytorch.org/whl/cu118
```

Use `--whisper-device cpu` to force CPU, or `--whisper-device cuda` to require
GPU. The default `auto` falls back safely to CPU when CUDA is unavailable.

When using **Selected window**, the window is rendered from its own contents,
so it is captured even without keyboard focus and even when another window
(for example an always-on-top widget) overlaps it; the overlapping window does
not appear. The screenshot follows the currently visible browser tab. A
minimized window is skipped for that interval. Screenshot interval changes
take effect during recording without stopping audio. Automatic screenshots
are saved under each meeting's `recordings/desktop/<session-id>/screenshots/`,
not under Windows `Pictures/Screenshots`; screenshots made separately with
Windows tools may be copied into the meeting's `screenshots/imported/` folder.

The **Live Transcript** tab offers **Live captions for next recording**, off by
default. It uses the local CPU Moonshine model when enabled. This optional live
view is approximate; it never replaces the saved WAV audio or the final
post-meeting transcription, and changing the checkbox takes effect on the next
recording rather than the one already in progress.

If an interrupted capture leaves finalized WAV files but no `metadata.json`,
recover it before processing:

```powershell
python scripts/recover_session.py recordings/desktop/<session-id>
```
