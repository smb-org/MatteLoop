# Source Video Playback — Design

**Date:** 2026-11-04
**Status:** Draft for review
**Binding context:** `docs/engineering-guardrails.md`, `docs/v1-scope.md`, the
Qt-translation, README and platform rules in `CLAUDE.md`.

## Goal

Let the user watch the open video in the Original pane and set IN/OUT while it
plays, instead of hunting for the right cut through stills. Playback is a
viewing aid for the timeline: it never touches segmentation, the preview job,
the render pipeline or the stored state of a cut.

## Non-goals

No audio. No speed control, no frame-accurate stepping while
playing, no proxy files, no hardware decoding, no QtMultimedia, no change to the
packaged FFmpeg. No persistence of playback state. No accessibility tree for the
new row beyond standard-widget names (`docs/v1-scope.md` defers the custom
tree). Nothing in `jobs/source.py`, `ui/timeline.py` or `core/state.py` grows.

## User-visible behaviour

A slim row sits directly below the Original canvas, inside the left pane:
`Play`/`Pause` (one checkable button, mirroring the result player's), the
timecode of the visible frame, and a `Selection only` toggle. Timeline, Set
IN/Set OUT and Reset Range stay where they are.

| State | Original canvas | Timeline marker, timecodes | Row | Entered by | Left by |
|---|---|---|---|---|---|
| No source / loading / error | placeholder or still | — | hidden with the stage | — | source ready |
| Ready, paused | still at the reducer playhead | reducer playhead | `Play`, enabled | source ready; any stop | `Play`, Space |
| Playing | live frames, scaled on the worker | follow the displayed frame; `Outside export range` follows too | `Pause`, checked | `Play`, Space, drag release | `Pause`, Space, end of video, drag, job start, source change, decode error |
| Suspended by drag | scrub still (existing debounce path) | drag position | still shows `Pause` | left press on the timeline that moves the playhead, while playing | left release: resumes from the new position (seek) |
| Locked (job running) | still | reducer playhead | disabled, same flag as the timeline (`editor_locked`) | job start | job end → Ready, paused |

Playback runs from the current playhead to the end of the video and stops
there. With `Selection only` on it loops the export range `[IN, OUT)`.

Interaction decisions (each with its reason):

| Interaction | Decision |
|---|---|
| `Play` at the end of the video, `Selection only` off | Restart from 0. Playing one frame and stopping is the only other meaning, and it is useless. |
| `Play` with `Selection only` on and the playhead outside `[IN, OUT)` | Start at IN. |
| `Selection only` turned on while playing outside the range | Jump to IN at once: the constraint is now active. |
| `Selection only` toggled while paused | No playhead movement; the toggle is a mode, not a navigation. It is session-only, not persisted. |
| IN/OUT changed during a loop (handle drag or Set IN/OUT) | The worker reads the range before every frame; a frame outside the new range triggers a seek to IN. Handle drags never suspend playback. |
| `I`/`O` and Set IN/Set OUT while playing | Use the visible frame's timestamp and keep playing. |
| `I`/`O` without timeline focus, playing or paused | They work anywhere in the window when the key event propagates to it: after a click on `Play` the button has focus, and setting cuts while watching is the point of this feature. Line edits, spin boxes, combo boxes and dialogs consume the letters before that, so typing is unaffected. Ignored while the editor is locked. |
| Space | Toggles play/pause when the key event propagates to the main window. Buttons, line edits, spin boxes and dialogs consume Space before that, so they keep it; with focus on the Result canvas Space is left alone, reserved for hold-to-compare (`matteloop-desktop-app.md:302`), which is not implemented yet. Auto-repeat from a held key is ignored, so holding Space toggles once. |
| Left/Right (Shift+) while playing | Pause first (commit the visible frame), then step from it. Stepping means "show me exactly this frame". |
| Click or drag on the timeline while playing | Suspend; the picture follows the scrub as today; on release playback resumes from the new position. A click is a press and release. |
| Crop drag during playback | Allowed and playback continues; the live frame has the still's orientation and aspect, so the crop geometry is unchanged. |
| Window resize during playback | Frames keep the size chosen at play start and the canvas rescales them; the next play or resume adopts the new size. |
| Timecode and frame number | Both show the displayed frame's timestamp (row timecode, timeline telemetry, `Frame N` via `absolute_frame_number`). |
| Very short range, single frame | The frame owning IN is shown; a loop iteration never restarts before one source frame interval has passed, so a one-frame range is a still with no seek storm. |
| Pressing `Play` while a job runs | Impossible: the row is disabled with the editor. Space is ignored while the row is disabled. |
| Preview or Render pressed while playing | Playback stops and commits the visible frame before the request is built, so the job uses the frame the user was looking at. |
| Pause | The visible frame stays on screen and becomes the playhead, with no jump in time or position (see holds below). When the exact still replaces it, sharpness and colour may change minimally (see Picture fidelity). |
| Result preview | Becomes stale once, on stop, through the ordinary `PlayheadChanged`; nothing is dispatched per frame. The result loop (`ResultPlayerCanvas`) may run at the same time. |

## Architecture

Four new modules; frozen modules are only imported from or subclassed.

### `jobs/playback.py` — `PlaybackDecoder` (Qt-free)

Purpose: decode one source sequentially from a start timestamp and hand back
display-sized RGBA frames with their presentation timestamps.

Interface: `PlaybackDecoder(path, proof: SourceValidationProof, revision:
SourceRevision, display_size: tuple[int, int])`; `open()`, `seek(timestamp)`,
`frames() -> Iterator[tuple[Fraction, Any]]` yielding `(pts, av.VideoFrame)`
starting with the frame that owns the seek timestamp (last frame with
`pts <= t`, as `decode_frame`), then every later frame in order;
`convert(frame) -> PlaybackFrame(pts, rgba_bytes, width, height)`; `close()`.
`seek` within an open container re-seeks and re-applies the pre-roll.
Conversion is a separate call so a late frame can be dropped before it costs
anything.

Dependencies: `jobs.source` private helpers, imported by name — `_input_container`
(entered through an `ExitStack` for the decoder's lifetime), `_stream_from_proof`,
`_seek_for_timestamp`, `_validate_frame_color`, `_profile_contract`,
`_proof_color_contract`, `_frame_timestamp`, `_width_padded_yuv_frame`,
`_convert_bt709_transfer_to_srgb`, `_SWSCALE_WIDTH_ALIGNMENT`. The module
docstring states that these are the still-decode seams and that the alternative,
re-exporting them, would grow a frozen module.

### `ui/playback_controls.py` — `PlaybackControls`, `PlaybackTimeline`

`PlaybackControls(QWidget)`: the row. `play_button` (checkable `QPushButton`,
object name `source_play`), `timecode` (`QLabel`, mono), `selection_only`
(`QCheckBox`, object name `playback_selection_only`). Signals
`play_toggled(bool)`, `selection_only_toggled(bool)`. Methods `apply(model)`
(enabled flag and reducer timecode from the presentation model),
`set_playing(bool)` (checked state, text and accessible name, like
`ResultPlayerCanvas._sync_play_button`), `set_position(Fraction | None)` — while
a position is set, `apply` leaves the timecode alone. Depends on
`format_localized_timecode`.

`PlaybackTimeline(TimelineWidget)`: overrides `set_presentation` to substitute a
held playhead (`replace(presentation, state=replace(state, playhead=held))`) and
adds `hold_playhead(Fraction | None)`, which re-applies the last reducer
presentation with or without substitution. Thumbnails are untouched because the
layout signature does not include the playhead. `MainWindow` instantiates this
class instead of `TimelineWidget`.

### `ui/playback_controller.py` — `SourcePlaybackController`, `_PlaybackWorker`

`SourcePlaybackController(QObject)`: owns playback state on the GUI thread.
`attach(controls, canvas, timeline)`, `play()`, `pause()`, `toggle()`,
`stop_and_commit()` (pause plus commit; a no-op when not playing),
`shutdown() -> bool`, properties `playing`, `visible_position: Fraction | None`.
Constructor takes `store`, `commit: Callable[[Fraction], None]` (the source
controller's timeline dispatch) and `decoder_factory` (defaults to
`PlaybackDecoder`; tests inject a synthetic one). It subscribes to the store and
installs an event filter on the timeline for left press/release.

`_PlaybackWorker(QObject)`: runs on a `WorkerThread`; owns the decoder, the clock
and the mailbox. Signals `frame_available()`, `ended(object)` (last pts),
`failed(str)`, `finished()`. Inputs written by the GUI thread as single attribute
assignments: `range: tuple[Fraction, Fraction] | None` (`None` = play to the
end). One worker per play or resume; the thread lives exactly while playing.

Every play or resume gets a new session number, carried by the worker, its
mailbox and all its signals. The controller drops every mailbox frame and every
`ended` or `failed` whose number is not the active session's; after a stop no
session is active. A worker interrupted during a decode call therefore cannot
replace a scrub still, restore a hold, or commit a position. This answers a
sequence the design itself creates, since stopping does not wait for the worker.

### Seams in existing, unfrozen modules

- `ui/preview_canvas.py`: `PreviewCanvas.hold_live_frame(QImage)` shows the
  image and marks the canvas held; `set_frame` while held only stores the image
  as pending; `release_live_frame()` clears the hold and shows the pending still
  if any. `PreviewStage` wraps the original canvas in a column with
  `PlaybackControls` below it and exposes `playback_controls`.
- `ui/main_window_render.py`: `_render_source_and_result` calls
  `playback_controls.apply(model)`.
- `ui/main_window.py`: construct `PlaybackTimeline`; add the two row widgets to
  the tab order after the Original canvas; `keyPressEvent` forwards an
  unhandled Space to `services.toggle_playback()` unless the focus widget is the
  result canvas, and an unhandled `I`/`O` to `services.dispatch` as
  `SetStartToPlayhead`/`SetEndToPlayhead`, the events the timeline's own keys
  emit, so the playing translation below applies to both. `_build` is not
  touched beyond the class name, so its
  over-budget function count stays at one.
- `ui/controller.py`: `attach_playback(controls, canvas, timeline)`; `dispatch`
  calls `stop_and_commit()` before routing `ChooseVideoRequested`,
  `VideoDropped`, `PreviewFrameRequested`, `RenderVideoRequested`,
  `RebuildEditedCutsRequested` and `ManageWorkspacesRequested` (a rebuild can
  start from the picker); `_dispatch_timeline` translates, while playing,
  `SetStartToPlayhead`/`SetEndToPlayhead` into `StartChanged`/`EndChanged` with
  the visible position, pauses before a `StepFrame`, and tells the playback
  controller about a `PlayheadChanged` (drag suspension); `shutdown` includes
  the playback controller. `toggle_playback()` is added to `WindowServices`.
- `app.py`: `controller.attach_playback(...)` after the window exists.

## Data flow

**Play.** The controller reads `state.timeline` and `state.source_value`,
chooses the start (playhead; IN if `Selection only` and outside; 0 at the end),
the display size (the canvas content rect in logical pixels, matching the
still path's scaling), builds the worker and starts its thread. It calls
`canvas.hold_live_frame` with the first frame when it arrives, `set_playing(True)`
at once.

**Per frame (worker).** Decode the next frame; `due = anchor_wall + (pts -
anchor_pts)` on `time.perf_counter()`. If `now > due + one source frame
interval`, the frame is late: drop it without converting, unless four frames in a
row were dropped — then convert it anyway so an overloaded machine still shows
progress. Otherwise sleep until `due` in slices of at most 10 ms, checking
`isInterruptionRequested()` each slice, then `convert`, build the `QImage`
(`Format_RGBA8888`, copied), write it and `pts` into the one-slot mailbox under a
lock and emit `frame_available` only if the slot was empty. The GUI slot takes
the slot's content and clears it; a frame overwritten before the GUI took it is
dropped. This bounds memory to one frame and never queues behind a stalled GUI.

**Per frame (GUI).** `canvas.hold_live_frame(image)`,
`timeline.hold_playhead(pts)`, `controls.set_position(pts)`. No store dispatch.

**State renders during playback.** Any dispatch (crop drag, inspector change,
IN/OUT, update banner) re-renders the window with the reducer's playhead and
still. The three holds absorb it: the canvas keeps the live frame and stores the
still as pending, `PlaybackTimeline.set_presentation` substitutes the held
playhead, `PlaybackControls.apply` leaves the timecode alone. The holds make the
result independent of store subscription order.

**Pause and commit.** `pause()` sets `playing = False`, requests interruption,
calls `set_playing(False)`, then `commit(PlayheadChanged(visible_position))`.
The reducer moves the playhead, bumps `generation` and marks the preview stale
with `PLAYHEAD`; `SourceController._dispatch_timeline` starts the 125 ms still
decode as for any scrub. The holds stay. When `state.source_frame` changes
identity afterwards, the controller releases: `canvas.release_live_frame()`,
`canvas.set_frame(state.source_frame)`, `timeline.hold_playhead(None)`,
`controls.set_position(None)`. The still for the same frame replaces the live
frame of the same frame, so nothing moves in time or position; only the
differences described under Picture fidelity can show. If the still decode fails silently
(as `SourceFrameWorker` does today) the live frame simply stays.

**IN/OUT while playing.** `SetStartToPlayhead` → `StartChanged(visible_position)`
(likewise OUT). The reducer clamps as usual. The controller's store subscription
sees the new range and writes it to the worker's `range` when `Selection only`
is on.

**Drag.** The event filter notes a left press. A `PlayheadChanged` reaching
`_dispatch_timeline` while playing and a press is pending suspends the worker
(interrupt, no commit — the drag dispatch moves the playhead itself) and
remembers to resume. The scrub still arrives through the normal debounce and is
displayed because `hold_live_frame` is not active while suspended (the controller
releases the canvas on suspend). On release with a resume pending, `play()` runs
from `state.timeline.playhead`. A press that moves only a handle dispatches no
`PlayheadChanged` and nothing is suspended.

**Stops without commit.** Source id change, source leaving `READY`, or job
phase leaving `IDLE` observed in the subscription: interrupt the worker and
release all holds; the reducer's playhead cannot be moved (`can_edit` is false
once a job runs), so the picture returns to the reducer's still. This is the
fallback for a job started outside `SourceController.dispatch`; every known path
goes through `dispatch` and commits first.

**End of stream.** The worker emits `ended(last_pts)` and returns; the controller
pauses and commits `last_pts` (the reducer clamps to the latest frame).

## Threading and shutdown

The worker opens, decodes and closes the container on its own thread, inside
`run()`, never from the GUI thread, a destructor or interpreter teardown
(`.github/workflows/ci.yml:116-120` records an access violation on a worker's
container close; `jobs/source.py` is already the third consecutive `fix:` target,
so it is not touched). `WorkerThread` guarantees `run` returns before `exec` and
retires after a real join. `SourcePlaybackController.shutdown()` interrupts and
waits through `wait_for_shutdown_worker`, so a thread that misses the budget is
retained and `app.py` takes the `os._exit` path described in the guardrails.
Interruption is checked before and immediately after every decode call, before
conversion, before publishing to the mailbox, before `ended` and `failed`, and
in every sleep slice, so stop latency is one decode call. Whatever still slips
through is dropped by the session check. Threading of the codec context is left at
PyAV's default, as in `decode_frame`; the repository sets none.

## Picture fidelity

Playback is an **intentionally approximate viewing transform**, not a second
exact decode. Orientation, aspect, geometry, colour matrix and range match the
still; pixels do not exactly. The still path converts at full resolution, applies
the nonlinear transfer LUT, and only then resizes, while playback scales during
conversion and applies the LUT to the small image, so fine detail differs slightly
at display sizes. Keeping the still's order would mean a full-resolution
conversion per frame, which spends the headroom the weaker machines need. The cut
itself is unaffected: exports and the paused still keep using `decode_frame`, and
only the timestamp crosses from playback into the state.

The still path (`_normalized_image`) is: swscale `reformat` to RGBA with the
proof's matrix and range, the BT.709→sRGB LUT when the transfer is 1 or 6, a
LANCZOS resize for a non-square pixel aspect, a transpose for rotation.
`PlaybackDecoder.convert` takes the same steps with the proof's values, scaled:
target size = display size fitted to the oriented aspect (pixel aspect folded in).
At source size the still's route is kept: the frame is padded through
`_width_padded_yuv_frame`, converted unscaled and cropped (the overrun from
`a1cd2b8` is an observed libswscale failure on the RGBA destination). When
scaling, the frame is never padded, since swscale's bilinear filter would blend
the copied stride tail into the last columns; the destination width is rounded
up to `_SWSCALE_WIDTH_ALIGNMENT` and a whole-image Pillow `BILINEAR` resize
brings it to the exact width when they differ;
`VideoReformatter().reformat(
frame, width, height, format="rgba", src_colorspace, dst_colorspace=ITU709,
src_color_range, dst_color_range=JPEG)`; the LUT via
`_convert_bt709_transfer_to_srgb` on the small image; rotation by a transpose of
the small image. Each decoded frame's colour profile is checked against the
proof as `_frame_at_timestamp` does; a mismatch is a decode failure. Frames are
decoded at logical, not device, pixel size: that is what the canvas does with
stills today, and sharper HiDPI stills are a separate matter.

## VFR, end of stream, loop boundaries

Pacing is by presentation timestamps, so variable frame rate needs nothing
extra; the still path treats timestamps as authoritative and so does this one.
A frame with `pts is None` is skipped. End of stream ends playback (above).
With a range set, a frame whose `pts >= OUT` or `pts < IN` triggers
`seek(IN)` and re-anchors the clock at IN, so the seek cost (27–174 ms measured)
is a single hitch per loop and does not accumulate. The loop guard for short
ranges is in the table above.

## Errors and degradation (G4)

- Decode failure mid-file (`AppError`, `av.FFmpegError`, `OSError`): the worker
  emits `failed(message)` and returns. The controller pauses and commits the
  last displayed frame, and shows the message as the Original canvas status
  marker (`set_status_marker`, the same mechanism as the Result canvas):
  "Playback stopped — the video could not be decoded further". Scrubbing,
  stills, preview and render are unaffected; the marker clears on the next
  successful play or a source change.
- Source file changed on disk: `_input_container` raises on open or close; same
  path with "Playback stopped — the video file changed on disk". No reload is
  attempted; the user replaces the video from the UI.
- Machine too slow: late frames are dropped (worker and mailbox); the clock keeps
  real time and the visible rate degrades. No warning and no speed control: the
  only measurement so far is the desktop, and the laptop acceptance run decides
  whether anything more is needed.
- A failed `play()` (open error) behaves like a decode failure before the first
  frame; the button returns to `Play`.

## Frozen files

| File | Ratchet | How it is touched |
|---|---|---|
| `jobs/source.py` | 1708 lines, frozen | Not edited. Private helpers imported by `jobs/playback.py`. |
| `ui/timeline.py` | 852 of 855 | Not edited. `PlaybackTimeline` subclasses `TimelineWidget` and overrides `set_presentation`; the controller's event filter watches mouse press/release. |
| `core/state.py` | 882, frozen | Not edited. No new events; commits are `PlayheadChanged`, range edits are `StartChanged`/`EndChanged`. |
| `ui/main_window.py` | recorded for one over-long function, lines not ratcheted below 800 | Class swap, two tab-order entries, a short `keyPressEvent`. |
| `ui/controller.py` | 659, free | About 40 lines as listed under seams. |

## Testing

All UI tests run with `QT_QPA_PLATFORM=offscreen` and inject a synthetic decoder
through `decoder_factory`, so they never touch PyAV.

- `tests/jobs/test_playback.py` (Qt-free, in the Windows CI job's scope):
  sequential frames from a seek yield every pts in order, starting with the
  frame `decode_frame` returns for the same timestamp; with display size equal to
  the source size, pixel aspect 1 and rotation 0, `convert` equals
  `decode_frame(...).image` exactly (same swscale call, same LUT); with the
  rotation sidecar fixture and a non-square aspect fixture, the output dimensions
  match `decode_frame`'s and each quadrant's mean colour matches within 2/255; at
  a typical downscaled display size, for BT.709, rotated and non-square fixtures,
  `convert` is compared with `decode_frame`'s image resized the way the canvas
  resizes stills, within a tolerance set from measuring these fixtures and recorded
  in the test;
  `seek` inside an open container re-applies the pre-roll; a range check at
  `pts >= OUT` is the caller's, so the decoder test only covers seek.
- The Windows thread hazard, observable: a test opens the decoder, decodes
  frames and closes it on a `threading.Thread`, twenty times, and also closes
  it while a second thread decodes a still from the same file. It runs in
  `tests/jobs`, so the Windows job sees it; `tests/ui` does not run there.
- `tests/ui/test_playback_controls.py`: button text and accessible name follow
  `set_playing`; `apply` does not overwrite a set position; `PlaybackTimeline`
  substitutes and releases the playhead without restarting thumbnails.
- `tests/ui/test_playback_controller.py` (behaviour-named, G7): play then pause
  dispatches exactly one `PlayheadChanged` with the last displayed pts and marks
  a current preview stale once; a decode that returns after pause, after drag
  suspension, after source replacement and after a subsequent resume changes
  neither canvas nor state, and a late `ended` or `failed` from an old session is
  ignored; `I` and `O` with focus on the play button set the range and in a spin
  box do not; a state render during playback leaves the live
  frame on the canvas; the still arriving after the commit releases the hold and
  the canvas shows `state.source_frame`; `I` while playing sets IN to the
  visible frame and keeps playing; `StepFrame` pauses first; press+move+release
  on the timeline suspends and resumes; Space on the window toggles, Space with
  focus in an inspector spin box does not, Space on the Result canvas does not;
  job start stops playback; source replacement stops without a commit; `ended`
  commits the last frame and restores `Play`; `failed` pauses and sets the
  canvas marker; end-of-video `Play` restarts at 0; `Selection only` outside the
  range starts at IN; `shutdown` with a worker that ignores interruption reports
  incomplete and retains the thread.
- Guardrails: all four new modules under 800 lines, every function under 60;
  `check_guardrails.py` unchanged, no `--update`.

The local gate on Windows is the one in `CLAUDE.local.md`; the full suite is
proven by CI, and `tests/ui/test_playback_*` must be green on the macOS and Linux
jobs before the branch is proposed.

## Documentation changes

- `docs/v1-scope.md`, **landing before any playback code**, because that file
  outranks this spec (G8): a `Playback` row in the V1 table — "Sequential source
  playback in the Original pane with a Selection-only loop; no audio, no speed
  control. Scope reopened 2026-11-04 by maintainer decision after use on real
  footage: choosing a cut from stills on a whole Twitch clip is impractical." — and
  the window-wide `I`/`O` rule next to the Timeline row. Playback is a new item, not
  one from the Deferred table, so this is a recorded maintainer scope decision.
- README: "Open a local video and scrub to the part you want" becomes "...,
  play it, and scrub"; a sentence on `Selection only`; `assets/screenshots/
  main-window.png` regenerated by `scripts/screenshots.py`, which shows the new
  row in its paused state through the existing state snapshot and needs no
  playback seam.
- German catalogue (`resources/matteloop_de.ts`, context `PlaybackControls`):
  `Play` → `Abspielen`, `Pause` → `Pause`, `Selection only` → `Nur Auswahl`,
  `Play the video` → `Video abspielen`, `Pause the video` → `Video anhalten`,
  `Loop playback inside the export range` → `Wiedergabe im Exportbereich
  wiederholen`; context `SourcePlaybackController` for the two marker strings.
  All literals sit at the call site; the English `.ts` is regenerated with the
  `pyside6-lupdate` command in `docs/building.md`.
- `docs/designs/matteloop-desktop-app.md` is historical and not edited; the
  keyboard contract there already reserves Space from "timeline playback
  controls".

## Acceptance

1. All test gates green on CI for macOS, Linux and Windows; the local Windows
   gate shows no failure outside the known list in `CLAUDE.local.md`.
2. A real Twitch clip (1440p60 H.264, at least 60 s) in a packaged build on the
   i7-11th-generation laptop: play from the start, set IN and OUT with `I`/`O`
   while playing, drag the marker back five seconds during playback, pause, and
   render the range. The rendered range matches the two frames that were visible
   when `I` and `O` were pressed. Dropped frames are counted by the worker and
   logged at stop; the acceptance notes record the count.
3. The same on macOS with the signed bundle, plus a rotated phone clip (marker
   and crop stay aligned with the picture) and a quit while playing (clean exit,
   no retained thread in the log).
4. Replace the source while playing, start a preview while playing, and resize
   the window while playing: no crash, no stale picture after the still arrives.
