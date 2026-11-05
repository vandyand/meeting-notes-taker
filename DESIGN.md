# Meeting Notes Capture — Design Contract

## Purpose

Capture a meeting locally, then turn its audio and visible screen context into
reviewable notes without obscuring what was recorded, selected, or skipped.

## Primary user job

After a meeting, a user can quickly confirm that useful audio and screenshots
exist, correct only the automatic choices that look wrong, and start or resume
processing with clear progress and recoverable outputs.

## Interaction model

1. **Capture** — Start recording. The Devices tab shows each audio source,
   whether it is writing, its current level, and a fresh write heartbeat.
   The **Live Transcript** tab has a **Live captions for next recording**
   checkbox, off by default and switchable before or during recording. When enabled, it shows
   local approximate captions from one microphone and one meeting-audio
   device, with the device names visible. Disabled live captions never start
   the model; the installed audio tap is a no-op while captions are off and
   does not affect WAV capture or final transcription. Turning captions off
   mid-meeting stops new caption input, while turning them back on resumes
   local captions for the same recording. Outside a meeting the toggle sets
   the next recording's starting state.
   Only a newly completed utterance is committed: substantially overlapping
   re-segmentation of already committed audio does not create another caption.
   Repeated provisional model inference is off by default to protect capture
   throughput. Starting,
   active, lagging, unavailable, and stopped states are explicit. A gap or
   model failure never stops WAV recording; the user sees that live captions
   are incomplete and can still use final post-meeting transcription.
   **Capture Screen Context** is a live recording control: enabling it starts
   screenshots immediately, including when recording began without them;
   disabling it pauses future screenshots while audio continues. Rapid toggles
   must not create duplicate capture workers or overwrite earlier frames.
   A disabled-when-idle **Take screenshot** action saves one frame to the
   current meeting in the chosen scope even when automatic screenshots are off.
   A labeled gray screenshot indicator flashes green only after a frame is
   saved; it stays amber with the chosen and foreground window names when a
   selected-window frame is skipped, or red on capture error. A separate
   screenshot status remains visible even while audio heartbeat text updates.
   A compact preview beside the indicator shows the most recently saved frame;
   it updates asynchronously without delaying capture and resets for each new
   recording.
   The default scope is **Active window**; full desktop and a selected window
   remain explicit alternatives. **Choose Window** selects one visible window
   for the selected-window scope. The window is rendered from its own buffer
   (PrintWindow with full-content rendering), so it is captured without focus
   and even when other windows, such as always-on-top widgets, overlap it;
   those windows never appear in the image. If an app cannot render that way,
   the window's screen area is captured as shown (overlaps may appear; the user
   deselects such screenshots in review). If the window is minimized, closed,
   or unavailable, the interval is skipped with a visible message and never
   falls back to the desktop. Main section tabs use a bold, filled style with
   the selected tab in solid blue so they read as navigation, not body text. Changing
   the screenshot interval during recording reschedules the active capture
   worker without pausing audio or requiring a capture toggle. The
   chooser puts browser windows first, keeps other windows available, and shows
   native app icons where Windows exposes them. Icon loading must not delay
   opening the chooser; missing icons leave readable titles and sizes.
2. **Orient** — Stop recording and select the meeting from Meetings. A global
   context line names the selected meeting, capture time, title, and whether it
   is ready or processed; all process and output commands act on that meeting.
   On app startup, no meeting is selected even when older recordings exist;
   the latest-captured label remains informational, and meeting-specific actions
   stay unavailable until the user selects a row. Refreshing the list preserves
   an explicit selection but never silently selects the latest recording.
   **Edit title** sits beside that context line, opens a focused title dialog,
   and persists a user title that later note generation must not overwrite.
   Starting a recording selects the new capture and clears the old completion
   bar. With no active processing run, progress follows the selected meeting:
   completed meetings show 100%, new meetings show 0%, and interrupted or failed
   meetings show saved partial progress with a recovery message.
3. **Review exceptions** — Review shows track activity and automatic audio
   selection. Saved live captions are separately reviewable in this tab, with
   source and relative timestamp. They are never labeled as the final transcript
   or fed into final note generation. Older meetings show a clear empty state.
   Screenshot Review shows a visible thumbnail for every frame with automatic
   OCR selection; a reviewer can include/exclude individual frames,
   select all, or deselect all. The persistent **Include selected screenshots
   in OCR** setting in the Processing panel is the single on/off control for
   screenshot OCR, so a user can skip all screenshots without opening the
   thumbnail review. Saving image selections never silently turns OCR on.
   Both decisions persist with the meeting and are used by processing. The
   thumbnail grid scrolls with the mouse wheel anywhere over
   the review dialog, including when the pointer is over an image or checkbox.
   Screenshot Review opens immediately with cancellable background analysis;
   it opens centered near the main app, and Manage image masks opens centered
   near its parent review window, both kept within the visible desktop.
   previews are available to browse while automatic selection is being computed.
   Manual selection and Save are available as soon as the image list loads;
   background analysis must preserve choices made while it runs. Automatic
   selection becomes available when analysis finishes. A complete manual review
   can be saved without waiting for a duplicate check.
   Images per page offers **40**, **100**, and **All**. All virtualizes the grid
   and loads only visible/nearby rows. Cards have independent, fixed canvas
   positions so shifting the mounted row range never temporarily moves retained
   images into another row. Overlapping cards remain mounted during
   scrolling; a bounded 128-preview memory cache restores recent images without
   disk reads. Visible images load before nearby scroll buffers. Previous and Next sit at the bottom left
   and right. The review loads small JPEG previews off the UI thread,
   and caches previews and automatic selection against source file signatures.
   Changed captures invalidate cached analysis; persisted reviewer choices are
   always reapplied. Selections survive page changes, and bulk selection
   controls apply to all pages. Left-clicking a card's image, background, or
   checkbox toggles selection exactly once. Right-clicking its image opens the
   original large view without changing selection. Keyboard activation of the
   preview also toggles selection; Crop / mask retains its own action and never
   toggles selection.
   Right-click opens an in-app large image preview. Permanent deletion is only
   available there, requires a filename-specific confirmation, and is disabled
   during processing. It removes the original, generated image copies, and that
   image's OCR/review checkpoints, then rechecks duplicates because the capture
   sequence changed. Existing prose notes are not silently rewritten; the
   confirmation explains that they may still mention the deleted screenshot.
   **Show duplicates** is off by default. Confirmed duplicates have red image
   borders and a **Duplicate** label; unique images have no border and a
   **Unique** label. Unclassified images remain visible without borders
   until analysis completes. Borders represent automatic duplicate classification,
   never the OCR checkbox.
   Selected cards have a light-blue (`#dbeafe`) background, refreshed for
   individual, bulk, and automatic selections. Checkboxes remain the textual/
   keyboard selection control; red duplicate borders are independent of blue.
   The filter only changes visibility: manual selections are retained,
   and the summary names hidden images and how many remain selected
   for OCR. Select All / Deselect All apply to the entire meeting, including
   hidden images. Filtering preserves the nearest visible scroll/page anchor.
   A **Windows** filter can show any subset of foreground windows recorded
   during capture, independent of duplicate visibility and OCR selection.
   Screenshots from older meetings and manual imports have no reliable
   per-image foreground-window identity and appear under **Window unknown**.
   New automatic captures append the foreground window title and window ID to
   `screenshots/capture_windows.jsonl` after each image is saved. A title change
   within the same window (for example a browser tab switch) stays in that
   window's group. Window filtering never changes saved OCR choices.
   Processing reuses the same valid analysis cache instead of scanning again;
   only source or duplicate-policy changes invalidate it. Crops and masks affect
   OCR inputs and review previews, not duplicate classification. Duplicate checks never remove
   images from the review, only adjust automatic selection. The meeting-persisted
   duplicate mode defaults to **Aggressive**: a 64-bit perceptual hash within
   eight bits of the immediately preceding capture (and similar average brightness)
   is automatically excluded from OCR and hidden in the default review view.
   **Near duplicates** (at most 0.02% of pixels change by 24 or more RGB levels)
   and **Strict** (exact/cursor-only) remain available. Every mode compares only
   adjacent captured frames, even if the preceding frame was excluded. Small
   real changes may be hidden in Aggressive mode; **Show duplicates** exposes them
   for manual selection. Mode changes invalidate analysis, not originals, saved
   manual choices, or OCR checkpoints.
   Window capture uses DWM visible physical-pixel bounds, excluding invisible
   resize borders. For legacy app-captured window images only, duplicate
   comparison ignores the outer eight-pixel band of original captures.
   This never modifies originals, OCR inputs, or normalized crop coordinates;
   desktop and imported images retain edge changes. The versioned analysis
   cache includes this comparison policy and refreshes older classifications
   while retaining explicit reviewer selections.
   **Manage image masks** edits screen context for this meeting using a draggable
   keep-area and optional blackout rectangles. The global editor can preview any
   screenshot with a chooser or Previous/Next; switching previews keeps the draft
   coordinates. A **Middle third** preset is available; each image may override
   the global settings, return to them, or apply its draft to all screenshots
   (except other images with their own overrides).
   Edits use coordinates relative to the original image. Originals are retained;
   A persistent summary names the global crop/area kept, global blackouts, and
   number of individual overrides. Opening the editor manages existing settings,
   not a second crop applied to previously cropped files.
   OCR receives separate PNG files under `screenshots/.ocr-inputs/`, named with
   the original stem, cropped/masked suffix, and revision. Processing displays
   the OCR filename and retains the original filename in row details. Original
   paths remain checkpoint identities; filename changes never require new OCR.
   The review
   grid displays the edited images. Mask changes refresh previews without
   rerunning duplicate analysis, and invalidate affected OCR checkpoints;
   successful transcripts are retained. Global crop
   controls are available while initial screenshot analysis is running.
4. **Process** — A single **Process selected meeting** command acts on the
   visible selection and its screenshot-OCR setting. The Processing panel
   identifies the active stage (audio
   analysis, visual OCR, transcription, cleanup, notes) and its progress. OCR
   names the current image and total image count (for example, “OCR image 7 of
   32”). Transcription likewise identifies the active audio file and total
   count, percentage of audio decoded, and decoded audio position / total
   duration. These are decoder measurements, not wall-clock time or an ETA.
   Percentages advance as Whisper finishes decoding windows; the UI receives
   updates immediately and a one-second worker heartbeat between windows.
   Model loading and file preparation use an animated indicator, not a made-up
   percentage. Each file starts with fresh progress, never the batch's timer.
   Cancellation retains already-created artifacts and enables retry. The
   dedicated **Processing** tab exposes the same run as a five-stage pipeline,
   plus one row per selected screenshot and audio track. Every row has a
   textual state; OCR rows retain attempt count and the latest full error, and
   the OCR pane shows a clickable thumbnail of the exact input being processed,
   including its crop/masks and image number. Preview loading runs off the UI thread.
   audio rows show per-file decoding progress, completion, or a specific error.
   Tables are stacked at full width so progress is not clipped in narrow panes.
   Enter or double-click on any row opens its complete, copyable details.
   Starting/resuming immediately opens Processing and replaces historical
   failure banners with current activity, even before the worker launches.
   Persisted running state replaces the old failure on the first worker event;
   a run interrupted by app closure is labeled interrupted on reopening.
   Processing status uses unique temporary files and bounded retries for Windows
   replacement conflicts. A status-file failure is a live warning, not a fatal
   transcription error; artifact write failures remain fatal and recoverable.
   Active progress is associated with its meeting, not whichever row is later
   selected. The globally visible processing section names the active meeting.
   The action/status area keeps reserved window space; messages wrap below
   the progress bar. The minimum supported window is 1000 x 760.
   Elapsed processing time, approximate estimated total time, and approximate
   time remaining update every second to the right of that bar, including
   during initial preparation. Estimates recalculate from elapsed time and
   observed overall progress; they may grow during a slow model/network step.
   The recording timer at the top remains separate. A transient Processing
   display error is logged and cannot permanently stop the completion-event
   listener. If a worker has exited without its final UI event being applied,
   the app reconciles the view from the saved process state. Completion always
   ends with a full re-render from saved files, even if an earlier UI step
   fails, and a once-per-second check re-renders whenever no run is active but
   a running-only stage label is still displayed. Tk callback exceptions are
   written to `ui-errors.log` in the recordings folder (pythonw has no console).
   The Processing tab offers independent, meeting-persisted Local or OpenRouter
   choices for screenshot OCR and audio transcription. Cloud (OpenRouter) is
   the default for any meeting without a saved choice; an explicitly saved
   Local choice is always kept. Beside the provider choices, the current
   OpenRouter balance (credits purchased minus used, from `/api/v1/credits`)
   is fetched in the background at startup and after each successful run, with
   a manual Refresh. A failed lookup reads "balance unavailable" rather than
   showing an older figure as current; the key never appears in the UI.
   When screenshot OCR is off or no screenshots exist, its provider choice is
   disabled with explanatory text; the saved provider choice is retained for
   when OCR is enabled again. Skipping OCR is not a third provider choice.
   Cloud sends selected screenshots or selected audio tracks to OpenRouter; it
   is an explicit user choice, never an automatic fallback. Changing either
   choice and pressing **Resume processing** retries unfinished items on that
   backend while preserving successful checkpoints. Cloud OCR uses GPT-6 Luna;
   cloud audio uses Whisper Large V3 Turbo and checkpoints bounded WAV chunks.
   Cloud OCR and audio chunks each use a small three-request worker pool. A
   single coordinator writes checkpoints as requests finish, preserving source
   order in final OCR/transcripts. Transient limits retry with backoff and honor
   OpenRouter's Retry-After header; credit exhaustion does not retry blindly.
   A 2026-10-21 replay of 25 OCR images and 40 audio chunks found that eight
   simultaneous cloud requests completed all 65, while 16 and 65 produced
   TLS write timeouts after retries. Keep the two three-request production
   pools below the demonstrated eight-request ceiling; do not submit every
   item at once merely because the API accepts concurrent requests.
   When OCR is needed and at least one backend is cloud, OCR and transcription
   run concurrently after audio-track analysis. Both-local processing remains
   sequential to avoid competing for GPU memory. The Processing view keeps
   each active stage and item visible; its combined progress never goes
   backward when either stage retries. Transcript cleanup follows audio, and
   notes wait until both OCR and audio/cleanup succeed. A failure in one branch
   does not abort the other; Resume reuses completed checkpoints.
   The Processing table displays recorded OpenRouter charges for OCR,
   transcription, and notes, with a known subtotal below it. It reads saved
   per-image OCR costs, saved transcription-chunk costs, and the latest saved
   notes response. Missing historical usage is labeled **Unknown**; the
   subtotal says **at least** when any charge is unknown. The app does not
   rerun a meeting to obtain missing billing data or present unrecorded costs
   as zero. New notes use GPT-6 Luna by default unless explicitly overridden.
   A notes response cut off by the output-token limit or containing invalid
   JSON is an incomplete stage, not a successful meeting. Saved OCR and audio
   remain reusable when the user resumes. The model's recommended title is
   stored separately from a title the user explicitly set.
   Successful stages are checkpoints: **Resume processing** reuses matching completed OCR and
   transcript artifacts, then resumes at the first incomplete stage. An
   explicit maintenance option is required to force expensive regeneration.
   Failed OCR images retry automatically up to three times. If any remain,
   audio transcription and cleanup continue independently, but note generation
   remains on hold until all selected screenshots and selected audio tracks
   succeed. Zero selected screenshots satisfy OCR without starting Ollama.
   Processing records the failed
   stage, and enables **Retry failed OCR + update notes**. That action reuses
   every successful image and transcript, retries only failed or missing OCR,
   then regenerates downstream notes after all selected images succeed.
   Before local model selection, the app checks the Ollama API. If the local Windows
   API is unavailable, it starts `ollama.exe serve`, reports startup and
   readiness as processing states, waits up to 180 seconds for a slow first
   startup, and then validates that the requested OCR
   model is installed. If the API disappears during an image or reports a
   transient overload, the app restores readiness and retries only that image;
   completed image checkpoints remain intact. Configured remote Ollama hosts
   are never started locally. Every OCR request supplies its context size
   explicitly (16,384 tokens by default, configurable with
   `MEETING_OCR_CONTEXT_LENGTH`), so a reused server's smaller default cannot
   override it. Progress and newly attempted image results record the requested
   size; reused checkpoints are not relabeled. Before OCR, the app releases models resident in
   Ollama and verifies that sufficient NVIDIA memory is free. It never terminates unrelated GPU
   applications; insufficient capacity names the measured constraint and asks
   the user to pause other GPU work before resuming. A worker that stops before
   item completion restarts once automatically, while its full output is
   retained with the meeting.
5. **Verify and continue** — Artifact links are disabled until their selected
   meeting has completed processing and the relevant files exist. A visual timeline is the user-facing screen-context
   view; raw OCR remains on disk but is not a competing UI destination. A
   deliberate **Regenerate notes** action preserves manual notes before
   replacing the generated notes artifact.

## Important rules

- Aggressive screenshot deduplication deliberately excludes perceptually similar
  adjacent captures, including some genuine small changes. Near excludes tiny
  changes; Strict excludes only exact/cursor-only duplicates. No mode deletes
  originals or overrides explicit manual choices.
- A selected browser/window capture records the window’s current contents. If
  the visible browser tab changes, the subsequent screenshot captures that tab.
- Manual selections override automatic decisions but never delete raw capture
  files.
- Review and processing operate on the same complete screenshot set; processing
  must not silently truncate a review selection. Completion counts use the
  selected manifest as their denominator, so a selected but missing OCR result
  remains visibly pending rather than being reported as success.
- Visual Timeline is disabled until its artifact exists; reviewing a new
  meeting must not produce a misleading file-not-found modal.
- Refreshing the meeting list preserves an existing selected meeting whenever
  it still exists; it must not silently switch an older selection to latest.
- The screenshot-OCR table column communicates outcome, not merely capture
  volume: processed count and total, skipped and total, or pending and total.
- Partial recordings are preserved for review and processing; a failed device
  cannot invalidate tracks that finalized successfully.
- If a capture ends exceptionally after WAVs are finalized, retain the full
  diagnostic traceback beside the session and reconstruct a manifest marked
  partial so the meeting remains visible. Clear stale recording state in the UI.
- Processing status must say what is happening now, not imply completion before
  final notes exist.
- Processing is complete only when every required selected item succeeds. A
  partial OCR result is a failed checkpoint, never a successful stage.
- Item errors must be non-empty, persisted with the meeting, visible without
  opening a log file, and communicated with text rather than color alone.
- A failure modal must include the underlying worker or resource error and move
  the user to Processing; a generic "OCR failed" message is not sufficient.
- Only one desktop app instance may run at a time, preventing two windows from
  competing to process or mutate the same meeting.
- Intra-file transcription percentage comes from Whisper's decoded mel frames.
  Legacy standalone Whisper installs without this adapter show preparation /
  activity rather than a fabricated percentage. A successful exit is not enough:
  all required transcript JSON files must exist before transcription succeeds.
- OCR releases its Ollama model memory before Whisper begins so sequential
  local AI stages do not compete for GPU/system memory.
- The app resolves Whisper through its own Windows Python installation, not
  only through a shell PATH entry.
- Whisper uses CUDA automatically when the native PyTorch installation can see
  a compatible NVIDIA GPU; it falls back to CPU and identifies the device in
  transcription progress.
- OpenRouter credentials are resolved automatically through the current user's
  DPAPI-protected `DevSecrets` helper and PowerShell 7. The UI must never ask
  the user to unlock the key manually, display it, log it, or persist it.
- Notes requests include all accepted transcript segments with timestamps and
  compact track aliases. Rejected segments remain available in the local cleanup
  audit but are never sent as accepted meeting speech. No speech is truncated to
  meet a credit balance. HTTP 402 names the credit/spending-limit remedy, retains
  technical details on disk, and directs the user to Resume without redoing OCR
  or audio. The app never purchases credits or changes the selected model itself.

## Validation (2026-09-14)

`tests/test_processing_feedback.py` exercises native Tk controls and failure →
resume transitions, selection isolation, regenerated notes, per-file percentage
reset, missing Whisper outputs, compact request contents, and HTTP 402 recovery.
Desktop layout checks cover 1000 x 800 and the default 1120 x 820; no browser or
mobile interface is shipped. Synthetic UI/decoder validation does not claim a
live paid notes request or processing of the user's current meeting.

## Non-goals

- This app does not silently upload raw audio or screenshots.
- It does not claim speaker attribution from separate system/microphone tracks.
