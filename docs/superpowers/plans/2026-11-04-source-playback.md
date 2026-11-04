# Source Video Playback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the user play the open video in the Original pane and set IN/OUT
while it plays, with an optional `Selection only` loop, without touching
segmentation, preview, render or the stored state of a cut.

**Architecture:** A Qt-free `PlaybackDecoder` (`jobs/playback.py`) decodes
sequentially from a seek and converts frames at display size through the same
swscale/LUT/transpose steps the still path uses. A `_PlaybackWorker` on a
`WorkerThread` paces frames by presentation timestamp into a one-slot mailbox;
`SourcePlaybackController` (GUI thread) holds the live frame on the canvas, the
held playhead on a `PlaybackTimeline` subclass and the timecode on a
`PlaybackControls` row, and commits the visible frame to the reducer through
an ordinary `PlayheadChanged` only on pause. Sessions numbered per play drop
every late result. Frozen modules are imported from or subclassed, never
edited.

**Tech Stack:** Python 3.13, PySide6 6.10.3, PyAV 16.1.0 (`VideoReformatter`,
`av.VideoFrame`), Pillow, pytest + pytest-qt (`QT_QPA_PLATFORM=offscreen`),
`uv`.

**Spec:** `docs/superpowers/specs/2026-11-04-source-playback-design.md` — the
plan argues from the spec; cite its sections rather than restating them.
Executors read both. The spec is binding; where this plan names a decision the
spec left open, it says so.

## Global Constraints

Every task's requirements implicitly include this section.

- **Binding documents, in authority order:** `docs/engineering-guardrails.md`,
  `docs/v1-scope.md`, the spec above, `CLAUDE.md`, `CLAUDE.local.md`. Read the
  guardrails and the spec before touching code.
- **Branch:** `feat/source-playback`, already checked out. Never push, never
  merge, never open a pull request from a task; the orchestrator does that.
- **Commit format** (`CLAUDE.md` "Commit messages"): English. Subject
  `<icon> <type>(<module>): summary`, imperative, at most 50 characters, then a
  blank line. Body: bullet points grouped by kind, each group led by its icon,
  concrete statements naming functions and values. Never test or lint results
  in the body. A `fix:` carries a `Trigger:` line (repro or previously failing
  test) after the bullets. One commit per complete change, including its
  tests, docs and configuration. **No attribution trailers**, no
  `Co-Authored-By`, no generator line, no session URL.
- **Third consecutive `fix:` on the same file: stop and ask the user** (G1).
  `jobs/source.py` is already at that point and is not edited by any task.
- **Frozen modules and budgets** (G6, `scripts/check_guardrails.py`): never run
  `check_guardrails.py --update`. `jobs/source.py`, `ui/timeline.py`,
  `core/state.py` are not edited. `ui/main_window.py` keeps its single
  over-budget function (`_build`); nothing added to it may push another
  function over 60 lines. New modules stay under 800 lines, every function
  under 60.
- **Qt translation** (`CLAUDE.md` "Keeping the README honest"): every new
  user-visible string is a literal at its `translate()`/`tr()` call site,
  never a variable, and present in `resources/matteloop_de.ts`. After adding
  strings, regenerate both `.ts` files with the `pyside6-lupdate` command in
  `docs/building.md`, add the German translations, compile with
  `pyside6-lrelease`, and run `tests/test_translations.py`.
- **Platform-specific code** (`CLAUDE.md` "Platform-specific code"): return
  from inside `sys.platform` branches, pin `sys.platform` in tests that depend
  on it, never trust `os.access(..., os.W_OK)` on Windows. No `ctypes`, no
  Win32 calls (G5).
- **Degrade, never refuse** (G4). **No concurrency hardening** beyond what the
  spec names (G3). **Tests are named after behaviour** (G7).
- **Local gate on this Windows machine**, run through the Bash tool (Git
  Bash), from the repository root:
  `uv run ruff check . && uv run mypy src && uv run mypy --platform linux src && uv run mypy --platform win32 src && uv run python scripts/check_guardrails.py && QT_QPA_PLATFORM=offscreen uv run pytest -q tests/jobs tests/core tests/test_resources.py`
  plus the task's own test files, and for UI changes the affected `tests/ui`
  files run separately (`QT_QPA_PLATFORM=offscreen uv run pytest -q tests/ui/<file>`).
  `tests/ui/test_render_controller.py` must be run on its own. Known failures
  on this machine are listed in `CLAUDE.local.md` ("Bekannter Test-Stand");
  only **new** failures count. Full green is proven only by CI.
- **Fresh context:** each task runs in a fresh subagent that sees only this
  header, its task, and the spec. Interfaces blocks carry exact names and
  types; do not rename across tasks.
- **Stop-and-report rule:** when an assumption the spec relies on turns out
  false (a private helper is missing or behaves differently, a thread test
  crashes, a measurement is out of range, a Qt focus assumption fails), stop,
  leave the work uncommitted or committed only as far as it is green, and
  report to the orchestrator. Do not work around it and do not redesign.

---

## File structure

| Path | Responsibility | Task |
|---|---|---|
| `docs/v1-scope.md` | `Playback` row and the window-wide `I`/`O` rule | 1 |
| `src/matteloop/jobs/playback.py` (new) | `PlaybackDecoder`, `PlaybackFrame`: sequential decode and display-size conversion, Qt-free | 2 |
| `tests/jobs/test_playback.py` (new) | decoder behaviour, fidelity, thread hazard | 2 |
| `src/matteloop/ui/preview_canvas.py` | `PreviewCanvas.display_size`, `hold_live_frame`, `release_live_frame`; `PreviewStage` column with the row | 3 |
| `src/matteloop/ui/playback_controls.py` (new) | `PlaybackControls` row, `PlaybackTimeline` subclass | 3 |
| `src/matteloop/ui/main_window.py` | `PlaybackTimeline` swap, `playback_controls` attribute, tab order (3); `keyPressEvent` (5) | 3, 5 |
| `src/matteloop/ui/main_window_render.py` | `playback_controls.apply(model)` | 3 |
| `tests/ui/test_playback_controls.py` (new), `tests/ui/test_preview_canvas.py` | widget behaviour | 3 |
| `src/matteloop/ui/playback_controller.py` (new) | `SourcePlaybackController`, `_PlaybackWorker`, `_FrameMailbox`, `PlaybackSource` protocol | 4, 6 |
| `src/matteloop/ui/controller.py` | owns the playback controller; `attach_playback`, `toggle_playback`, `stop_and_commit` before commands, `shutdown` (4); `_dispatch_timeline` translations (5); drag hook (6) | 4, 5, 6 |
| `src/matteloop/ui/ports.py` | `toggle_playback()` on `WindowServices` | 4 |
| `src/matteloop/app.py` | `controller.attach_playback(...)` | 4 |
| `tests/ui/test_playback_controller.py` (new) | controller behaviour, sessions, keys, drag, loop | 4, 5, 6 |
| `resources/matteloop_en.ts`, `resources/matteloop_de.ts`, `*.qm` | row strings (3), marker strings (4) | 3, 4 |
| `README.md`, `assets/screenshots/main-window.png` | product page | 7 |

---

### Task 1: Record the scope decision in `docs/v1-scope.md`

Lands before any playback code because `docs/v1-scope.md` outranks the spec
(G8; spec "Documentation changes", first bullet).

**Files:**
- Modify: `docs/v1-scope.md` (the "In scope for V1" table, lines 45-59)
- Test: `tests/test_scope_contract.py` (existing; must stay green)

**Interfaces:**
- Consumes: nothing.
- Produces: nothing code-facing. Later tasks cite the `Playback` row.

- [ ] **Step 1: Add the `Playback` row** to the "In scope for V1" table,
  directly after the `Timeline` row, with the exact wording the spec gives:
  "Sequential source playback in the Original pane with a Selection-only loop;
  no audio, no speed control. Scope reopened 2026-11-04 by maintainer decision
  after use on real footage: choosing a cut from stills on a whole Twitch clip
  is impractical."
- [ ] **Step 2: Extend the `Timeline` row** with the window-wide `I`/`O`
  rule from the spec's interaction table: `I`/`O` set IN/OUT from anywhere in
  the window when the key event propagates to it (line edits, spin boxes,
  combo boxes and dialogs consume the letters first); ignored while the editor
  is locked.
- [ ] **Step 3: Update the revision line** at the top of the file to name
  2026-11-04 and the playback decision, keeping the earlier revision note.
- [ ] **Step 4: Run** `uv run pytest -q tests/test_scope_contract.py` and
  `uv run ruff check .` — both green.
- [ ] **Step 5: Commit** as `📝 docs(scope): reopen scope for source playback`
  with a body naming the row added, the rule added to the Timeline row and
  the reason (maintainer decision after use on real footage).

---

### Task 2: `PlaybackDecoder` — prove the decode seams with real code

This is the risk-retiring task. It builds the Qt-free decoder on the private
helpers of `jobs/source.py` (spec "Architecture › `jobs/playback.py`",
"Picture fidelity", "VFR, end of stream, loop boundaries") and measures the
downscale tolerance the spec leaves to measurement. If the preflight or any stop
condition in Step 7 fires, stop and report.

**Preflight, before Step 1, and before writing anything:** with a throwaway
scratchpad script, confirm that every listed private helper exists with the
signature this task assumes, and that `VideoReformatter.reformat` accepts
`width`/`height` together with the colour arguments on one decoded frame of a
fixture. If either fails, stop and report; nothing has been built yet.

**Files:**
- Create: `src/matteloop/jobs/playback.py`
- Create: `tests/jobs/test_playback.py`
- Reference (read, do not edit): `src/matteloop/jobs/source.py` lines
  253-380 (`decode_frame`), 424-455 (`_input_container`, a
  `@contextmanager`), 538-620 (`_stream_from_proof`), 684-758
  (`_seek_for_timestamp`, `_frame_at_timestamp`), 1072 (`_frame_timestamp`),
  1241-1286 (`_validate_frame_color`, `_profile_contract`,
  `_proof_color_contract`), 1397-1443 (`_SWSCALE_WIDTH_ALIGNMENT`,
  `_width_padded_yuv_frame`), 1445-1601 (`_normalized_image`,
  `_convert_bt709_transfer_to_srgb`); `tests/jobs/test_source.py` lines
  41-125 (fixture helpers `_make_lossless_oriented_video`,
  `_make_seekable_video`) and `tests/fixtures/media_factory.py::make_video`.

**Interfaces:**
- Consumes: from `matteloop.jobs.source` — `SourceRevision`,
  `SourceValidationProof`, `decode_frame` (tests only), and the private
  helpers listed in the spec, imported by name:
  `_input_container`, `_stream_from_proof`, `_seek_for_timestamp`,
  `_validate_frame_color`, `_profile_contract`, `_proof_color_contract`,
  `_frame_timestamp`, `_width_padded_yuv_frame`,
  `_convert_bt709_transfer_to_srgb`, `_SWSCALE_WIDTH_ALIGNMENT`.
  `AppError`, `ErrorCode` from `matteloop.core.errors`.
- Produces (used by Task 4 and later):
  - `PlaybackFrame` — frozen dataclass with slots:
    `pts: Fraction`, `rgba: bytes` (tightly packed RGBA8888, `width * 4`
    bytes per row), `width: int`, `height: int`.
  - `PlaybackDecoder.__init__(self, path: Path, proof: SourceValidationProof, revision: SourceRevision, display_size: tuple[int, int]) -> None`
  - `PlaybackDecoder.target_size -> tuple[int, int]` (property): the
    oriented output size of `convert`, fixed at construction.
  - `PlaybackDecoder.open(self) -> None`
  - `PlaybackDecoder.seek(self, timestamp: Fraction) -> None`
  - `PlaybackDecoder.frames(self) -> Iterator[tuple[Fraction, Any]]` — yields
    `(public_pts, av.VideoFrame)`.
  - `PlaybackDecoder.convert(self, frame: Any) -> PlaybackFrame`
  - `PlaybackDecoder.close(self) -> None`
  - Errors escape unchanged: `AppError` (from the helpers, or raised by the
    decoder itself with `ErrorCode.SOURCE_CORRUPT` for a colour-contract
    mismatch, built the way `matteloop.ui.controller._as_app_error` builds
    one), `av.FFmpegError`, `OSError`, `ValueError`. The worker in Task 4
    catches them.

**Contracts (text, not code):**

- Module docstring states that the helpers are the still-decode seams and
  that re-exporting them would grow a frozen module (spec).
- `open()` enters `_input_container(path, revision)` through a
  `contextlib.ExitStack` kept for the decoder's lifetime and resolves the
  stream with `_stream_from_proof(container, proof, revision)`. `close()`
  closes the stack; an `AppError` raised by the container's exit (source
  changed on disk) propagates out of `close()`. `close()` is idempotent.
- `seek(t)` requires `open()`; it calls `_seek_for_timestamp(container,
  stream, t, proof.presentation_origin, proof.duration)` and remembers `t` as
  the pending pre-roll target. Calling `seek` again on the open container
  re-seeks and re-applies the pre-roll.
- `frames()` requires a prior `seek`. It iterates `container.decode(stream)`
  directly (not `_frame_at_timestamp`, which drops the first later frame when
  it breaks). Per decoded frame: `_validate_frame_color(stream, frame)`, and
  `_profile_contract(profile) != _proof_color_contract(proof)` is a decode
  failure (`AppError`); frames with `pts is None` or `time_base is None` are
  skipped; `public_pts = _frame_timestamp(frame) - proof.presentation_origin`;
  negative public pts are skipped. Pre-roll: while `public_pts <= target`
  keep the frame as candidate; at the first `public_pts > target` yield the
  candidate (or, with no candidate yet, this frame) and then this frame; then
  every later frame in order. If the stream ends with only a candidate, yield
  it. If it ends with nothing, the iterator simply ends.
- `target_size` and the conversion geometry: oriented source size is
  `(round(coded_width * pixel_aspect), coded_height)`, swapped when
  `proof.rotation in {90, 270}`. Fit that aspect into `display_size` as
  `Qt.KeepAspectRatio` would (largest size with the same aspect that fits,
  each dimension at least 1; when `display_size` is at least the oriented
  size in both dimensions, use the oriented size — no upscaling). Un-swap to
  get the pre-transpose size `(w_r, h_r)`.
- `convert(frame)`: for `proof.rgb_input`, `frame.reformat(width=w_r,
  height=h_r, format="rgba")`; otherwise, when `(w_r, h_r)` equals the frame
  size, `_width_padded_yuv_frame(frame)` first, `VideoReformatter().reformat(
  padded, width=padded.width, height=h_r, ...)` and a crop to `w_r`, as the
  still path does; when scaling, no padding: `reformat(frame, width=W,
  height=h_r, ...)` with `W` = `w_r` rounded up to a multiple of
  `_SWSCALE_WIDTH_ALIGNMENT`, then a whole-image Pillow `BILINEAR`
  `resize((w_r, h_r))` only if `W != w_r`. The colour arguments are
  `format="rgba", src_colorspace=<ITU709 for matrix 1, ITU601 for 5 and 6>,
  dst_colorspace=Colorspace.ITU709, src_color_range=<JPEG for range 2 else
  MPEG>, dst_color_range=ColorRange.JPEG`.
  Then `_convert_bt709_transfer_to_srgb` when `proof.color_transfer in {1,
  6}`; then the transpose for `proof.rotation` as `_normalized_image` does
  (90 → `ROTATE_90`, 180 → `ROTATE_180`, 270 → `ROTATE_270`). Pack to
  `PlaybackFrame` and close the Pillow intermediates. Interpolation is PyAV's
  default; record which one it is in the module docstring after checking
  `av.video.reformatter`.
- Nothing here checks `pts >= OUT` or `pts < IN`; that is the worker's job
  (spec "Testing", first bullet).

- [ ] **Step 1: Write fixture helpers in the test module** (local, not
  imported from `test_source.py`): a seekable 120-frame 20 fps `libx264rgb`
  clip with GOP 10 and a per-frame red ramp (model on
  `_make_seekable_video`); a `yuv420p` `make_video` clip 64×32 (aligned) and
  one 40×24 (unaligned, exercises the padding path), each 6 frames with
  distinct solid colours; a BT.709-transfer variant of the aligned clip built
  with `av` directly like `make_video` but `color_trc = 1`, 96×64, with a
  horizontal gradient so downscaling has something to blur; the oriented
  clip (`rotate=90` metadata and SAR 2 on a `libx264rgb` 16×8 stream, model
  on `_make_lossless_oriented_video`, which `probe_source` reports as 8×32
  with rotation 270 and pixel aspect 2).
- [ ] **Step 2: Write the failing tests**, each named after behaviour:
  - `test_frames_from_a_seek_start_with_the_owning_frame_and_continue_in_order`:
    on the seekable clip, `seek(Fraction(7, 20) + Fraction(1, 40))` yields a
    first pts equal to `decode_frame(path, same t, 1, validation_proof=...).actual_pts`,
    then every later pts strictly increasing by `1/20` up to `119/20`, with
    `120 - 7` frames in total.
  - `test_convert_matches_decode_frame_exactly_at_source_size`: for the
    aligned, the unaligned and the BT.709-transfer clips, with
    `display_size` equal to `(info.width, info.height)`, `convert(frame).rgba`
    equals `decode_frame(...).image.tobytes()` byte for byte for the frame at
    `Fraction(0)` and for the fourth frame, and `(width, height)` match.
  - `test_convert_keeps_the_stills_orientation_and_aspect`: on the oriented
    clip with `display_size=(8, 32)`, `target_size == (8, 32)`, the converted
    frame's size equals `decode_frame(...).image.size`, and the mean colour of
    each of the four quadrants matches the still's within 2/255 per channel.
  - `test_downscaled_convert_approximates_the_resized_still`: for the
    BT.709-transfer clip at `display_size=(48, 32)`, the oriented clip at
    `(4, 16)` and the non-square-aspect path (the oriented clip counts for
    both rotation and aspect; add a SAR-2 clip without rotation, 32×16 coded,
    at display `(32, 8)`), compare `convert` with the `decode_frame` image
    resized to `decoder.target_size` by Pillow `LANCZOS` (stated in the test
    as the stand-in for the canvas's Qt smooth scaling, which a Qt-free test
    cannot call). Assert the mean absolute per-channel difference is at or
    below a `TOLERANCE` constant recorded in the test together with the
    measured values from Step 6.
  - `test_seek_inside_an_open_container_reapplies_the_preroll`: on the
    seekable clip, after `seek(Fraction(5, 2))` and consuming three frames,
    `seek(Fraction(3, 10))` makes the next `frames()` start with the pts
    `decode_frame` returns for `Fraction(3, 10)`.
  - `test_repeated_open_decode_close_on_worker_threads_is_safe`: twenty
    times, a `threading.Thread` opens the decoder on the aligned clip, seeks
    `0`, decodes and converts three frames, and closes it; each thread is
    joined and any exception it raised is re-raised in the test. One extra
    round runs that thread while a second thread calls `decode_frame` on
    the same file; both finish without error. (This is the observable form of
    the Windows hazard in `.github/workflows/ci.yml:116-120`; it runs in
    `tests/jobs`, which the Windows CI job executes.)
  - `test_close_is_idempotent_and_frames_before_seek_is_an_error`: `close()`
    twice does not raise; `frames()` before `seek()` raises `RuntimeError`
    (a programming error, not a user-facing one).
- [ ] **Step 3: Run** `QT_QPA_PLATFORM=offscreen uv run pytest -q tests/jobs/test_playback.py`
  and confirm every test fails on `ImportError`/`AttributeError`, not on a
  fixture error.
- [ ] **Step 4: Implement `src/matteloop/jobs/playback.py`** to the contracts
  above. Keep every function under 60 lines (the conversion splits naturally
  into geometry, reformat, post-processing).
- [ ] **Step 5: Run the tests** until all but the tolerance test pass.
- [ ] **Step 6: Measure the tolerance.** With a throwaway script in the
  scratchpad (not committed), print the mean absolute difference and the
  maximum per-quadrant mean difference for each of the three downscale cases.
  Record the measured values in a comment in the test and set `TOLERANCE` to
  the largest measured mean rounded up to the next integer plus 2 (in
  1/255 units).
- [ ] **Step 7: Empirical stop conditions — report instead of continuing** if
  any of these holds: the exact-equality test cannot be made to pass
  (the two paths are not the same swscale call); the thread test crashes the
  interpreter or hangs; any measured mean difference exceeds 16/255 or a
  quadrant mean exceeds 24/255 (the picture is not "approximately" the
  still). Report the numbers.
- [ ] **Step 8: Run the local gate** from Global Constraints plus this test
  file. `check_guardrails.py` must pass with the new module unlisted.
- [ ] **Step 9: Commit** as `✨ feat(playback): decode source frames for playback`
  with a body naming the decoder's methods, the helpers it imports, the
  conversion order and the measured tolerance values.

---

### Task 3: Row, holds and the timeline subclass (widgets only)

Builds every widget seam the controller needs (spec "Architecture ›
`ui/playback_controls.py`", "Seams in existing, unfrozen modules" for
`preview_canvas.py`, `main_window_render.py` and the class swap, "User-visible
behaviour" table for enabled/hidden states). After this task the row is
visible and correctly enabled in the running application and does nothing when
clicked.

**Files:**
- Create: `src/matteloop/ui/playback_controls.py`
- Modify: `src/matteloop/ui/preview_canvas.py` (`PreviewCanvas` lines
  118-269, `PreviewStage` lines 272-300)
- Modify: `src/matteloop/ui/main_window.py` (`_build` lines 106-112 for the
  class swap and the `playback_controls` attribute; `_set_tab_order` lines
  256-277)
- Modify: `src/matteloop/ui/main_window_render.py` (`_render_source_and_result`)
- Modify: `resources/matteloop_en.ts`, `resources/matteloop_de.ts`,
  `resources/matteloop_en.qm`, `resources/matteloop_de.qm`
- Create: `tests/ui/test_playback_controls.py`
- Modify: `tests/ui/test_preview_canvas.py`,
  `tests/ui/test_main_window_state_and_source_drop.py` (the `expected` list
  in `test_actual_tab_order_skips_hidden_widgets_and_reaches_success_actions`,
  line 748)
- Reference: `src/matteloop/ui/result_player.py` lines 100-116 and 206-227
  (`_sync_play_button`), `src/matteloop/ui/timeline.py` lines 211-337 and
  569-622, `src/matteloop/ui/source_presentation.py::format_localized_timecode`,
  `src/matteloop/ui/presentation_model.py::PresentationModel`,
  `src/matteloop/ui/timeline_presentation.py::TimelinePresentation`.

**Interfaces:**
- Consumes: `TimelineWidget`, `TimelinePresentation`, `PresentationModel`,
  `format_localized_timecode`, `TimelineState` (`matteloop.core.timeline`).
- Produces:
  - `PlaybackControls(QWidget)` in `matteloop.ui.playback_controls`:
    - attributes `play_button: QPushButton` (object name `source_play`,
      checkable, text `Play`/`Pause`, accessible name `Play the video`/`Pause
      the video`), `timecode: QLabel` (object name `playback_timecode`,
      `mono` property, `MONO_FONT` like the timeline labels),
      `selection_only: QCheckBox` (object name `playback_selection_only`,
      text `Selection only`, accessible name `Loop playback inside the export
      range`). Translation context `PlaybackControls` for all six literals.
    - signals `play_toggled = Signal(bool)` — relays the button's
      `clicked(bool)` (user action only, never emitted by `set_playing`);
      `selection_only_toggled = Signal(bool)` — relays the check box's
      `toggled(bool)`.
    - `apply(self, model: PresentationModel) -> None`: `setVisible(model.show_timeline)`;
      `setEnabled(model.timeline is not None and not model.editor_locked)`;
      when `position is None`, the timecode shows
      `format_localized_timecode(model.timeline.state.playhead)` or
      `format_localized_timecode(Fraction(0))` without a timeline; when a
      position is set, the timecode is left alone.
    - `set_playing(self, playing: bool) -> None`: checked state, text and
      accessible name, as `ResultPlayerCanvas._sync_play_button`.
    - `set_position(self, position: Fraction | None) -> None`: shows the
      position's timecode while set; `None` restores the last applied
      reducer timecode immediately.
    - properties `position: Fraction | None`, `selection_only_enabled: bool`
      (the check box state).
  - `PlaybackTimeline(TimelineWidget)` in the same module:
    - `set_presentation(self, presentation: TimelinePresentation | None) -> None`
      override: remembers the last reducer presentation; when a hold is set
      and the presentation is not `None`, passes
      `replace(presentation, state=replace(presentation.state, playhead=held))`
      to the base class, with `held` clamped to `[0, state.duration]` so
      `TimelineState.__post_init__` never raises (the G4 floor for this widget;
      the single normalised position shown everywhere is produced by the
      controller in Task 4, so timeline and row cannot disagree).
    - `hold_playhead(self, position: Fraction | None) -> None`: stores the
      hold and re-applies the last reducer presentation with or without the
      substitution.
    - property `held_playhead: Fraction | None`.
  - `PreviewCanvas` additions:
    - `display_size(self) -> tuple[int, int]`: the box `_update_pixmap`
      scales into (contents rect minus layout margins minus
      `_reserved_bottom_space()`, each dimension at least 1), in logical
      pixels. `_update_pixmap` is refactored to call it; no behaviour change.
    - `hold_live_frame(self, image: QImage) -> None`: shows the image
      (clears the placeholder text) and marks the canvas held.
    - `release_live_frame(self) -> None`: clears the hold and applies the
      pending still if `set_frame` was called while held — including a
      pending `None`, which restores the placeholder (use a sentinel to
      distinguish "nothing pending" from "pending None").
    - `set_frame` while held only stores the pending image.
    - property `holding_live_frame: bool`.
  - `PreviewStage` additions: `playback_controls: PlaybackControls` placed
    directly below `original_canvas` in a vertical column
    (`original_column: QWidget`, zero margins, the canvas with stretch 1);
    the column takes the canvas's former place in the horizontal layout.
  - `MainWindow` additions: `self.timeline_widget` is a `PlaybackTimeline`
    (`timeline_placeholder` alias unchanged); `self.playback_controls =
    self.preview_stage.playback_controls`; tab order `original_canvas →
    playback_controls.play_button → playback_controls.selection_only →
    result_canvas → timeline_widget`.
  - `main_window_render._render_source_and_result` calls
    `window.playback_controls.apply(model)` after
    `window.timeline_widget.apply_presentation(...)`.

- [ ] **Step 1: Write the failing tests** in `tests/ui/test_playback_controls.py`:
  - `test_play_button_text_and_accessible_name_follow_set_playing`:
    `set_playing(True)` → text `Pause`, accessible name `Pause the video`,
    checked; `set_playing(False)` → `Play`, `Play the video`, unchecked.
  - `test_set_playing_never_emits_play_toggled_but_a_click_does`:
    `set_playing(True)` emits nothing on `play_toggled`;
    `qtbot.mouseClick(play_button, LeftButton)` emits `True`, a second
    click emits `False`.
  - `test_apply_shows_the_reducer_timecode_until_a_position_is_set`: with a
    ready model whose playhead is `Fraction(1)` the timecode reads
    `0:01.000` and the row is enabled; after `set_position(Fraction(3, 2))`
    another `apply` with the same model leaves `0:01.500`;
    `set_position(None)` restores `0:01.000` without a further `apply`.
  - `test_row_is_disabled_while_the_editor_is_locked_or_no_timeline_exists`:
    a model with `editor_locked=True` → disabled; a model with
    `timeline=None` and `show_timeline=True` → disabled and `0:00.000`; a
    model with `show_timeline=False` → hidden.
  - `test_selection_only_relays_the_check_box`: clicking the check box emits
    `selection_only_toggled(True)` and `selection_only_enabled` is `True`.
  - `test_playback_timeline_substitutes_and_releases_the_held_playhead`:
    `set_presentation(p)` with playhead `0` then `hold_playhead(Fraction(2))`
    → `timecode_label` reads `0:02.000`, `frame_label` reads
    `Frame 000061` (source fps 30), `range_status_label` reads `Outside
    export range` for a range `[1/2, 3/2)`; `set_presentation(p2)` with playhead `1` while held keeps
    `0:02.000`; `hold_playhead(None)` shows `0:01.000`.
  - `test_holding_the_playhead_does_not_restart_thumbnails`:
    `_thumbnail_generation` and `_thumbnail_signature` are identical before
    and after `hold_playhead`/release (read the private attributes with
    `# noqa: SLF001`, as `test_preview_canvas.py` does).
  - `test_a_held_playhead_beyond_the_duration_is_clamped`: `hold_playhead`
    with `duration + 1` does not raise and shows the duration's timecode.
  - Build presentations with the `_presentation` helper pattern from
    `tests/ui/test_timeline.py` lines 22-34; build `PresentationModel`
    values through `present(state)` from `matteloop.ui.presenter` on a
    reduced `AppState` (see `tests/ui/test_main_window_state_and_source_drop.py`
    for ready/locked states).
- [ ] **Step 2: Write the failing canvas and stage tests** in
  `tests/ui/test_preview_canvas.py`:
  - `test_hold_live_frame_shows_it_and_defers_stills_until_release`:
    `hold_live_frame(a)` → `holding_live_frame` is `True` and `pixmap()` is
    not null; `set_frame(b)` while held leaves `_frame` equal to `a`
    (compare `QImage` equality); `release_live_frame()` → `_frame` equals
    `b` and `holding_live_frame` is `False`.
  - `test_release_applies_a_pending_cleared_frame`: hold, `set_frame(None)`,
    release → `text()` is the placeholder and `_frame is None`.
  - `test_display_size_matches_the_box_stills_are_scaled_into`: on a shown
    canvas resized to 300×200 with a frame set, `display_size()` equals the
    scaled pixmap's bounding box dimensions for a frame of the same aspect;
    before `show()` it is at least `(1, 1)`.
  - `test_stage_places_the_playback_row_under_the_original_canvas`: after
    `show()` and `processEvents`, `playback_controls.geometry().top() >=
    original_canvas.geometry().bottom()` and both share the same `x()`.
- [ ] **Step 3: Run** both test files; all new tests fail on missing names.
- [ ] **Step 4: Implement** `playback_controls.py`, the `PreviewCanvas` and
  `PreviewStage` changes, the `MainWindow` changes (class swap, attribute,
  tab order) and the `_render_source_and_result` call. In `_build`, the
  class swap is a one-token change; the attribute assignment goes next to
  `self.original_canvas = ...`. Do not add lines to `_build` beyond those
  two.
- [ ] **Step 5: Update the tab-order test** expected list (`original_canvas`,
  `playback_controls.play_button`, `playback_controls.selection_only`,
  `result_canvas`, ...). The test also asserts each widget has an object
  name and an accessible name — the row's widgets have both.
- [ ] **Step 6: Translations.** Run the `pyside6-lupdate` command from
  `docs/building.md`; add to `resources/matteloop_de.ts` under context
  `PlaybackControls`: `Play` → `Abspielen`, `Pause` → `Pause`,
  `Selection only` → `Nur Auswahl`, `Play the video` → `Video abspielen`,
  `Pause the video` → `Video anhalten`, `Loop playback inside the export
  range` → `Wiedergabe im Exportbereich wiederholen`; run `pyside6-lrelease`
  for both catalogues; run `uv run pytest -q tests/test_translations.py`.
- [ ] **Step 7: Run the local gate** plus, separately:
  `tests/ui/test_playback_controls.py`, `tests/ui/test_preview_canvas.py`,
  `tests/ui/test_main_window_state_and_source_drop.py`,
  `tests/ui/test_minimum_layout.py`, `tests/ui/test_timeline.py`,
  `tests/ui/test_result_player.py`, `tests/test_translations.py`. Compare
  failures with the known list in `CLAUDE.local.md`.
- [ ] **Step 8: Launch the application** (`uv run matteloop`), open any
  local MP4 and confirm: the row sits under the Original pane, `Play` is
  enabled, the timecode follows a scrub, the row is disabled while a preview
  runs. Clicking `Play` does nothing yet — that is expected.
- [ ] **Step 9: Commit** as `✨ feat(playback): add the playback row and holds`
  with a body naming `PlaybackControls`, `PlaybackTimeline`, the three
  canvas seams, the tab-order entries and the six German strings.

---

### Task 4: Worker, controller and wiring — Play/Pause with a moving picture

The vertical slice (G2). After this task a user launches the app, opens a
video, presses `Play`, sees the picture move with the marker and timecodes
following, presses `Pause`, and the exact still of the paused frame replaces
the live frame with the reducer playhead on it. Covers spec "Architecture ›
`ui/playback_controller.py`", "Data flow" (Play, Per frame, State renders,
Pause and commit, End of stream), "Threading and shutdown", "Errors and
degradation", and the `SourceController` seams for `attach_playback`,
`stop_and_commit` before commands, `toggle_playback` and `shutdown`.

**Files:**
- Create: `src/matteloop/ui/playback_controller.py`
- Modify: `src/matteloop/ui/controller.py` (`__init__` lines 161-220,
  `dispatch` lines 309-354, `shutdown` lines 356-390; new methods
  `attach_playback`, `toggle_playback`, `playback` property,
  `_commit_playhead`)
- Modify: `src/matteloop/ui/ports.py` (`WindowServices`, line 104)
- Modify: `src/matteloop/app.py` (after `controller.attach_transform_stage`,
  line 100)
- Modify: `resources/matteloop_en.ts`, `resources/matteloop_de.ts`, `*.qm`
- Create: `tests/ui/test_playback_controller.py`
- Reference: `src/matteloop/ui/worker_thread.py` (whole file),
  `src/matteloop/ui/timeline.py` lines 162-209 (`SourceFrameWorker`, the
  interruption idiom), `src/matteloop/ui/controller.py` lines 426-451 and
  581-635 (thread lifecycle idiom), `tests/ui/test_controller.py` lines
  136-255 (shutdown tests, `FakeSourceAdapter`, `_ready_store`).

**Interfaces:**
- Consumes: `PlaybackDecoder`, `PlaybackFrame` (Task 2);
  `PlaybackControls`, `PlaybackTimeline`, `PreviewCanvas.hold_live_frame`,
  `release_live_frame`, `display_size`, `set_status_marker` (Task 3);
  `WorkerThread`, `wait_for_shutdown_worker`; `PlayheadChanged`;
  `StateStore`; `SourceState`, `JobState`.
- Produces (used by Tasks 5 and 6):
  - `PlaybackSource(Protocol)`: `open() -> None`, `seek(timestamp: Fraction) -> None`,
    `frames() -> Iterator[tuple[Fraction, object]]`,
    `convert(frame: object) -> PlaybackFrame`, `close() -> None`.
  - `PlaybackDecoderFactory = Callable[[Path, SourceValidationProof, SourceRevision, tuple[int, int]], PlaybackSource]`.
  - `_FrameMailbox`: `put(session: int, image: QImage, pts: Fraction) -> bool`
    (returns `True` when the slot was empty; overwrites otherwise) and
    `take() -> tuple[int, QImage, Fraction] | None` (clears the slot), both
    under one `threading.Lock`.
  - `_PlaybackWorker(QObject)`: `__init__(self, session: int, decoder: PlaybackSource, start: Fraction, frame_interval: Fraction, range_: tuple[Fraction, Fraction] | None, mailbox: _FrameMailbox) -> None`;
    attribute `range: tuple[Fraction, Fraction] | None` written by the GUI
    thread as a single assignment; signals `frame_available = Signal(int)`,
    `ended = Signal(int, object)` (session, last pts or `None`),
    `failed = Signal(int, str)` (session, token `"decode"` or
    `"source-changed"`), `finished = Signal()`; `run()`; counters
    `shown_frames: int`, `dropped_frames: int` (plain attributes).
  - `SourcePlaybackController(QObject)`:
    - `__init__(self, store: StateStore, *, commit: Callable[[Fraction], None], decoder_factory: PlaybackDecoderFactory | None = None, parent: QObject | None = None) -> None`
    - `attach(self, controls: PlaybackControls, canvas: PreviewCanvas, timeline: PlaybackTimeline) -> None`
    - `play(self) -> None`, `pause(self) -> None`, `toggle(self) -> None`,
      `stop_and_commit(self) -> None`, `shutdown(self) -> bool`
    - properties `playing: bool`, `visible_position: Fraction | None`,
      `active_session: int | None` (for tests).
  - `SourceController`:
    - `__init__(..., playback_decoder_factory: PlaybackDecoderFactory | None = None)`
      keyword-only, creating `self._playback = SourcePlaybackController(store, commit=self._commit_playhead, decoder_factory=playback_decoder_factory, parent=self)`.
    - `_commit_playhead(self, position: Fraction) -> None` calls
      `self._dispatch_timeline(PlayheadChanged(position))`.
    - `attach_playback(self, controls: PlaybackControls, canvas: PreviewCanvas, timeline: PlaybackTimeline) -> None`
    - `toggle_playback(self) -> None`
    - property `playback: SourcePlaybackController`.
  - `WindowServices.toggle_playback(self) -> None` added to the Protocol.
  - Marker strings, translation context `SourcePlaybackController`:
    `Playback stopped — the video could not be decoded further` and
    `Playback stopped — the video file changed on disk`. German (this plan's
    proposal; the spec gives none): `Wiedergabe gestoppt — das Video konnte
    nicht weiter dekodiert werden` and `Wiedergabe gestoppt — die Videodatei
    hat sich auf der Festplatte geändert`.

**Contracts:**

- `play()`: no-op when already playing, when `state.source is not READY`,
  `state.timeline is None`, or `state.source_value` lacks `path: Path`,
  `revision: SourceRevision`, `validation_proof: SourceValidationProof`. Start
  position: `timeline.playhead`, or `0` when the playhead is at or beyond
  `duration - source_frame_interval` (spec: `Play` at the end restarts).
  (`Selection only` rules arrive in Task 6; this task ignores the check
  box.) Display size: `canvas.display_size()`. Build the decoder through
  the factory, a `_FrameMailbox`, a `_PlaybackWorker(session, decoder,
  start, timeline.source_frame_interval, None, mailbox)` and a
  `WorkerThread(worker, self)`; connect `frame_available`, `ended`,
  `failed` to controller slots and `finished` to `thread.quit` and
  `worker.deleteLater`, forget the thread on `thread.finished` as
  `SourceController._forget_frame_thread` does. Increment the session
  counter, set `active_session`, `playing = True`,
  `controls.set_playing(True)`, clear the canvas status marker, start the
  thread.
- Worker `run()`: `decoder.open()`, `decoder.seek(start)`, then the loop
  from spec "Per frame (worker)" with `anchor_wall = time.perf_counter()`
  and `anchor_pts` set at the first frame; late test `now > due +
  frame_interval` drops without converting and counts, except every fifth
  consecutive late frame is converted anyway; sleeping in slices of at most
  10 ms with `QThread.currentThread().isInterruptionRequested()` checked
  each slice; after `convert`, build a `QImage(rgba, width, height,
  width * 4, Format_RGBA8888).copy()`, `mailbox.put` and emit
  `frame_available(session)` only when `put` returned `True`; an overwrite
  counts as dropped. Interruption is checked before and after every decode
  step, before conversion, before publishing, before `ended` and `failed`.
  The loop only records the terminal outcome and emits nothing terminal
  itself: exhausted `frames()` records "ended, `last_pts`"; `AppError` with
  `code is ErrorCode.SOURCE_CHANGED` records `"source-changed"`; any other
  `AppError`, `av.FFmpegError`, `OSError` or `ValueError` records `"decode"`.
  `decoder.close()` always runs in `run()` on the worker thread, inside a
  `try/finally`, **before any terminal signal**, because `_input_container`
  detects a changed file on close. A `SOURCE_CHANGED` from `close()` overrides a
  recorded "ended" or `"decode"`; any other `close()` error overrides only
  "ended", as `"decode"`. Then exactly one of `ended(session, last_pts)` or
  `failed(session, token)` is emitted, unless interrupted, and `finished` is
  emitted last. The range loop (`pts >=
  OUT` / `pts < IN` → seek IN) belongs to Task 6; here `range` is stored
  and unused.
- GUI slot `_frame_available(session)`: ignore unless `session ==
  active_session`; `item = mailbox.take()`; ignore `None` or a different
  session; else normalise once: `position = min(pts, latest)`, with `latest`
  the same upper bound `move_playhead` clamps to in `core/timeline.py` (a decoded
  pts can exceed an underreported metadata duration). Then
  `canvas.hold_live_frame(image)`, `timeline.hold_playhead(position)`,
  `controls.set_position(position)`, `visible_position = position`. Every later
  use (timecode, frame number, IN/OUT, commit) reads this one value, and `ended`
  normalises `last_pts` the same way.
- `pause()`: no-op when not playing. Set `playing = False`,
  `active_session = None`, `thread.requestInterruption()`,
  `controls.set_playing(False)`, log at INFO `"source playback stopped:
  %d frames shown, %d dropped"` from the worker's counters (spec
  Acceptance 2), then if `visible_position is not None`: remember
  `state.source_frame` identity and `commit(visible_position)`; the holds
  stay until release. If `visible_position is None` (no frame arrived),
  release the holds at once.
- Store subscription: (a) when a release is pending and `state.source_frame
  is not` the remembered object → `canvas.release_live_frame()`,
  `canvas.set_frame(state.source_frame)` if it is a `QImage` else
  `set_frame(None)`, `timeline.hold_playhead(None)`,
  `controls.set_position(None)`, `visible_position = None`. (b) when
  playing, or a release is pending, and `state.source_id` differs from the
  one at play, or `state.source is not READY`, or `state.job.phase is not
  IDLE` → stop without commit: interrupt, `playing = False`,
  `active_session = None`, `set_playing(False)`, release all holds,
  `visible_position = None`; on a source-id change also clear the status
  marker (spec "Stops without commit", "Errors": marker clears on source
  change).
- `_ended(session, last_pts)`: ignore unless active; then behave like
  `pause()` with `visible_position = last_pts` when `last_pts` is a
  `Fraction` (the reducer clamps), else like `pause()` with no position.
- `_failed(session, token)`: ignore unless active; behave like `pause()`,
  then `canvas.set_status_marker(<literal chosen by token>)`. A failure
  before any frame (open error) leaves `Play` unchecked and the marker set.
- `toggle()`: `pause()` if playing else `play()`. `stop_and_commit()`:
  `pause()` when playing, else nothing.
- `shutdown() -> bool`: interrupt every thread still known, then
  `wait_for_shutdown_worker(thread, worker, "source playback worker")` for
  each; return whether all completed. Unsubscribe from the store first.
  `SourceController.shutdown` folds the result into `complete` next to the
  transform stage and render controller calls. `SourceController.dispatch`
  calls `self._playback.stop_and_commit()` before routing
  `ChooseVideoRequested`, `VideoDropped`, `PreviewFrameRequested`,
  `RenderVideoRequested`, `RebuildEditedCutsRequested` and
  `ManageWorkspacesRequested` (after the `_closed` guard, before the
  discard confirmation). `toggle_playback()` delegates to
  `self._playback.toggle()`.
- `app.py`: `controller.attach_playback(window.playback_controls,
  window.original_canvas, window.timeline_widget)` after
  `attach_transform_stage`. `MainWindow` wiring of the row: in `_build`'s
  signal section (not in `_build` itself if it would add more than two
  lines — put a `_connect_playback_controls` helper and call it from
  `__init__` after `_build`): `playback_controls.play_toggled` →
  `services.toggle_playback()` via `getattr(self._services,
  "toggle_playback", None)` guarded for stubs that lack it (the pattern
  `closeEvent` uses for `confirm_discard_unsaved_transform`).
- All `tests/ui` tests inject a fake decoder and never touch PyAV for
  playback; they may use a real `make_video` fixture with `probe_source`
  for metadata so `play()` finds a real `SourceInfo` (as
  `tests/ui/test_controller.py::_fixture_result` does).

- [ ] **Step 1: Write the test harness** in `tests/ui/test_playback_controller.py`:
  - `FakeDecoder` implementing `PlaybackSource`: constructed by
    `fake_factory(path, proof, revision, display_size)` which records every
    call; `frames()` yields `(pts, token)` from the last `seek` position in
    steps of `Fraction(1, 100)` up to a configurable `end`; an optional
    `threading.Event` gate the worker blocks on before each yield (so a test
    can hold a decode "in flight" across a pause); `convert` records the
    pts and returns a 4×4 `PlaybackFrame` whose red channel encodes the
    frame index; `seek` and `close` are logged. Timeline `source_fps` is
    `Fraction(100)` via metadata so `frame_interval` is 10 ms and tests run
    in tens of milliseconds.
  - A `ReducerStore` whose READY state comes from `probe_source` on a
    `make_video` fixture (metadata with real `path`, `revision`,
    `validation_proof`, `average_rate=Fraction(100)`) and a `source_frame`
    `QImage`; a `MainWindow(store, services_stub, settings)` so the window
    renders every state change exactly as in production; a
    `SourcePlaybackController(store, commit=lambda p: store.dispatch(PlayheadChanged(p)))`
    attached to `window.playback_controls`, `window.original_canvas`,
    `window.timeline_widget`. Wrap `store.dispatch` to record events.
- [ ] **Step 2: Write the failing tests**:
  - `test_play_shows_live_frames_and_holds_marker_and_timecode`: after
    `play()` and `qtbot.waitUntil(lambda: controller.visible_position is not None)`,
    `canvas.holding_live_frame` is `True`, `timeline.held_playhead ==
    controller.visible_position`, `controls.position` equal, `play_button`
    text is `Pause`, and `store` received no `PlayheadChanged`.
  - `test_pause_commits_exactly_one_playhead_change_and_marks_the_preview_stale_once`:
    with a `CURRENT` preview in the state (reduce `PreviewRequested` then
    `PreviewSucceeded` before play), play, wait for three frames, pause →
    exactly one `PlayheadChanged` recorded with `timestamp ==
    visible_position`, `state.timeline.playhead` equal, `state.preview is
    PreviewState.STALE` with `stale_category is PreviewInvalidationReason.PLAYHEAD`,
    `play_button` text `Play`; holds still set.
  - `test_the_still_after_the_commit_releases_the_holds`: after the pause
    above, dispatch `SourceFrameDecoded(source_id, state.timeline.generation, new_image)`
    → `holding_live_frame` is `False`, `canvas._frame` equals `new_image`,
    `held_playhead is None`, `controls.position is None`.
  - `test_close_failure_after_the_last_frame_reports_the_changed_source`: the
    fake's `frames()` exhausts normally and its `close()` raises
    `AppError(SOURCE_CHANGED)` → exactly one `failed` with `"source-changed"`,
    no `ended`, the canvas shows the source-changed marker, and `close` ran
    before the terminal signal.
  - `test_a_pts_beyond_the_metadata_duration_shows_one_position_everywhere`: the
    fake yields a pts past the state's duration → `visible_position`,
    `timeline.held_playhead` and `controls.position` are the same clamped value,
    and pausing commits that value with no state error.
  - `test_a_state_render_during_playback_keeps_the_live_frame`: while
    playing, dispatch `StartChanged(Fraction(1, 10))` into the store (the
    window re-renders the still and the reducer playhead) → the canvas
    still holds the live frame (`holding_live_frame` is `True` and `_frame`
    is unchanged), the timeline timecode still shows the held pts, and the
    row's timecode label is unchanged.
  - `test_a_decode_finishing_after_pause_changes_nothing`: gate the fake
    before its third frame, pause while it is blocked, record
    `canvas._frame`, `state`, then open the gate and `qtbot.wait(50)` →
    canvas frame identical, no further `PlayheadChanged`, `active_session is
    None`.
  - `test_a_late_ended_or_failed_from_an_old_session_is_ignored`: play,
    pause, play again; emit `ended(old_session, Fraction(1))` and
    `failed(old_session, "decode")` directly on the controller's slots →
    still playing, no marker, no extra `PlayheadChanged`.
  - `test_end_of_stream_commits_the_last_frame_and_restores_play`: fake
    `end` at 5 frames → `qtbot.waitUntil(not playing)`, one
    `PlayheadChanged` with the last pts, button `Play`.
  - `test_play_at_the_end_of_the_video_restarts_from_zero`: reducer playhead
    at `duration - 1/100`; `play()` → the fake's first `seek` is
    `Fraction(0)`.
  - `test_decode_failure_pauses_commits_and_marks_the_canvas`: fake raises
    `AppError(ErrorCode.SOURCE_CORRUPT, ...)` on the third frame → not
    playing, one `PlayheadChanged` with the second frame's pts,
    `canvas.status_label.text()` is `Playback stopped — the video could not
    be decoded further` and visible; a fake raising `AppError` with
    `ErrorCode.SOURCE_CHANGED` from `open()` → marker `Playback stopped — the
    video file changed on disk`, no `PlayheadChanged`, button `Play`.
  - `test_the_marker_clears_on_the_next_successful_play`: after the failure
    above, `play()` with a healthy fake → `status_label` hidden.
  - `test_late_frames_are_dropped_but_every_fifth_is_shown`: a fake whose
    `convert` sleeps 30 ms per frame with a 10 ms interval; after 200 ms,
    pause; `convert` was called fewer times than frames were yielded but at
    least once per five yielded frames, and the INFO log line captured with
    `caplog` reports a dropped count greater than zero.
  - `test_shutdown_with_a_worker_that_ignores_interruption_reports_incomplete_and_retains_the_thread`:
    a fake blocking on an event with no interruption check; `shutdown()`
    returns `False` within the `SHUTDOWN_TIMEOUT_MS` budget and
    `threads_outliving_shutdown()` grew by one; then set the event and
    `qtbot.waitUntil(thread.isFinished)` before the test ends (pattern:
    `tests/ui/test_controller.py::test_shutdown_reports_when_a_load_outlives_its_bounded_wait`).
  - `test_source_controller_stops_playback_before_choosing_a_video`: a real
    `SourceController(store, source_adapter=FakeSourceAdapter(...),
    playback_decoder_factory=fake_factory)` attached to the window; play;
    `dispatch(ChooseVideoRequested())` with `QFileDialog.getOpenFileName`
    monkeypatched to return `("", "")` → not playing and one
    `PlayheadChanged` recorded before the dialog was called (order
    asserted through the recording list).
  - `test_source_controller_shutdown_includes_playback`: with a gated fake,
    `SourceController.shutdown()` returns `False` and
    `shutdown_complete` is `False`; release the gate afterwards.
- [ ] **Step 3: Run** the file; everything fails on missing names.
- [ ] **Step 4: Implement** `playback_controller.py`, then the
  `controller.py`, `ports.py`, `main_window.py` (`_connect_playback_controls`)
  and `app.py` changes.
- [ ] **Step 5: Translations** for the two marker strings (context
  `SourcePlaybackController`): lupdate, German entries, lrelease,
  `tests/test_translations.py`.
- [ ] **Step 6: Run the local gate** plus, separately:
  `tests/ui/test_playback_controller.py`, `tests/ui/test_controller.py`,
  `tests/ui/test_playback_controls.py`,
  `tests/ui/test_main_window_state_and_source_drop.py`,
  `tests/ui/test_worker_thread.py` (it greps `ui/` for raw `.wait(` calls —
  use `wait_for_shutdown_worker` only), `tests/test_translations.py`, and
  `tests/ui/test_render_controller.py` on its own.
- [ ] **Step 7: Walk the slice in the running application**
  (`uv run matteloop`): open a local MP4, press `Play` — the picture moves,
  the marker and both timecodes follow, `Outside export range` follows;
  press `Pause` — the still replaces the live frame with no jump; press
  `Play` again at the end — it restarts; close the window while playing —
  the log shows the stop line and no "outlived shutdown" warning. Report
  what you saw, including the dropped-frame count from the log.
- [ ] **Step 8: Stop conditions:** the live frame does not appear in the
  real app although the tests pass; the app crashes on pause or close while
  playing (container close on the worker thread); CPU use makes the UI
  unresponsive on this desktop. Report rather than hardening.
- [ ] **Step 9: Commit** as `✨ feat(playback): play and pause the source video`
  with a body naming the worker, the mailbox, the session rule, the commit
  path, the marker strings, `attach_playback`, `toggle_playback` and the
  `app.py` wiring.

---

### Task 5: Keyboard, IN/OUT while playing, stepping and job stops

Covers the spec interaction table rows for `I`/`O`, Space, Left/Right, the
`main_window.py` `keyPressEvent` seam, the `_dispatch_timeline` translations,
"Stops without commit" and "Pressing Play while a job runs".

**Files:**
- Modify: `src/matteloop/ui/main_window.py` (new `keyPressEvent`, short; no
  change to `_build`)
- Modify: `src/matteloop/ui/controller.py` (`_dispatch_timeline` lines
  495-506)
- Modify: `tests/ui/test_playback_controller.py`
- Reference: `src/matteloop/ui/timeline.py` lines 482-503 (the timeline's
  own key handling, which keeps precedence when it has focus),
  `src/matteloop/ui/crop_canvas.py` lines 206-231 (keys the canvases
  consume), `src/matteloop/core/timeline_reducer.py` (events are ignored
  when `can_edit` is false).

**Interfaces:**
- Consumes: `SourcePlaybackController.playing`, `visible_position`,
  `pause()`, `toggle()` (Task 4); `SetStartToPlayhead`, `SetEndToPlayhead`,
  `StartChanged`, `EndChanged`, `StepFrame`, `PlayheadChanged`.
- Produces: `MainWindow.keyPressEvent(self, event: QKeyEvent) -> None`;
  no new names elsewhere. (Task 6 adds a drag hook to `_dispatch_timeline`;
  keep the method short enough that it stays under 60 lines after that.)

**Contracts:**

- `MainWindow.keyPressEvent`: for `Key_Space` without modifiers and
  `not event.isAutoRepeat()`, when `QApplication.focusWidget()` is not
  `self.result_canvas`: call `toggle_playback` on the services (via
  `getattr`, as in Task 4) and `accept()`. For `Key_I`/`Key_O` without the
  Control modifier: `services.dispatch(SetStartToPlayhead())` /
  `SetEndToPlayhead()` and `accept()`. Everything else:
  `super().keyPressEvent(event)`. The reducer ignores the range events while
  the editor is locked, so no lock check is needed for `I`/`O`; Space must
  check `self.playback_controls.isEnabled()` and do nothing when the row is
  disabled (spec: "Space is ignored while the row is disabled").
- `SourceController._dispatch_timeline`, while `self._playback.playing`
  and `visible_position` is not `None`: `SetStartToPlayhead` becomes
  `StartChanged(visible_position)`, `SetEndToPlayhead` becomes
  `EndChanged(visible_position)`, and playback continues; a `StepFrame`
  first calls `self._playback.pause()` (which commits the visible frame
  through `_commit_playhead`) and then dispatches the step from the
  committed playhead. The existing post-dispatch frame-timer logic is
  unchanged.

- [ ] **Step 1: Write the failing tests** (same harness as Task 4, but with
  a real `SourceController` as `services` and `window.show()` so focus is
  real; use `QTest.keyClick(widget, key)` on the focused widget):
  - `test_i_and_o_with_focus_on_the_play_button_set_the_range_from_the_visible_frame_while_playing`:
    play, wait for frames, `play_button.setFocus()`, key `I` → `state.timeline.start ==
    visible_position` at the moment of the key and still playing; later key
    `O` → `end` equals the then-visible position; both recorded as
    `StartChanged`/`EndChanged`, no `SetStartToPlayhead` reached the store.
  - `test_i_while_paused_with_focus_on_the_play_button_uses_the_reducer_playhead`:
    not playing, playhead `Fraction(1, 2)`, key `I` on the play button →
    `start == Fraction(1, 2)` via `SetStartToPlayhead`.
  - `test_i_in_an_inspector_spin_box_does_not_touch_the_range`: focus
    `window.inspector.fps_spinbox`, key `I` → `start` unchanged. (If this
    fails because `QSpinBox` propagates the letter, stop and report — the
    spec's focus assumption would be wrong.)
  - `test_i_is_ignored_while_the_editor_is_locked`: reduce
    `PreviewRequested` into the store, key `I` on the window → unchanged.
  - `test_space_on_the_window_toggles_playback_and_auto_repeat_is_ignored`:
    focus `original_canvas`, Space → playing; Space → not playing; a
    `QKeyEvent` constructed with `autorep=True` sent with
    `QApplication.sendEvent` → state unchanged.
  - `test_space_in_a_spin_box_or_on_the_result_canvas_does_not_toggle`:
    focus `fps_spinbox`, Space → not playing; focus `result_canvas`, Space →
    not playing.
  - `test_space_is_ignored_while_the_row_is_disabled`: job running (reduce
    `PreviewRequested`), Space on the window → not playing.
  - `test_step_frame_while_playing_pauses_first_and_steps_from_the_visible_frame`:
    dispatch `StepFrame(1)` through the services while playing → not
    playing, recorded events are `PlayheadChanged(visible)` then
    `StepFrame(1)`, `state.timeline.playhead == visible + 1/100`.
  - `test_job_start_stops_playback_without_a_commit`: reduce
    `PreviewRequested` straight into the store while playing → not playing,
    no `PlayheadChanged`, `holding_live_frame` is `False`, canvas shows
    `state.source_frame`, row disabled; a frame from the old session
    arriving afterwards (gate) changes nothing.
  - `test_source_replacement_stops_playback_and_clears_the_marker`: set a
    marker via a failing fake first, then play with a healthy fake and reduce
    `SourceLoadRequested("new", "load")` into the store → not playing, no
    `PlayheadChanged`, `status_label` hidden, holds released.
- [ ] **Step 2: Run** the file; the new tests fail.
- [ ] **Step 3: Implement** `keyPressEvent` and the `_dispatch_timeline`
  translations.
- [ ] **Step 4: Run the local gate** plus, separately,
  `tests/ui/test_playback_controller.py`, `tests/ui/test_controller.py`,
  `tests/ui/test_timeline.py`, `tests/ui/test_crop_canvas.py`,
  `tests/ui/test_main_window_state_and_source_drop.py`, and
  `tests/ui/test_render_controller.py` on its own.
- [ ] **Step 5: Walk it in the running application:** play, press `I` and
  `O` while watching (IN/OUT jump to the visible frame, playback continues),
  press Space to pause and resume, press Right while playing (pauses and
  steps), press `Preview Frame` while playing (job runs on the frame you
  were looking at).
- [ ] **Step 6: Commit** as `✨ feat(playback): set IN/OUT and pause from the keys`
  with a body naming `keyPressEvent`, the `_dispatch_timeline` translations,
  the StepFrame pause and the stop-without-commit paths.

---

### Task 6: Drag suspension and the `Selection only` loop

Covers spec "Data flow › Drag", "IN/OUT while playing" (the range reaching
the worker), "VFR, end of stream, loop boundaries" (range check, re-anchor,
loop guard), and the interaction rows for `Selection only` and the
single-frame range.

**Files:**
- Modify: `src/matteloop/ui/playback_controller.py` (worker range loop,
  event filter, suspend/resume, selection-only start rule)
- Modify: `src/matteloop/ui/controller.py` (`_dispatch_timeline`: drag hook)
- Modify: `tests/ui/test_playback_controller.py`
- Reference: `tests/ui/test_timeline.py` lines 87-99 (pressing the strip at
  `QPoint(450, 70)` on a 900×176 widget dispatches `PlayheadChanged`),
  `src/matteloop/ui/timeline.py` lines 453-480 (press/move/release).

**Interfaces:**
- Consumes: everything from Task 4; `PlaybackControls.selection_only_enabled`,
  `selection_only_toggled` (Task 3).
- Produces:
  - `SourcePlaybackController.playhead_changed_by_user(self) -> None` —
    called by `SourceController._dispatch_timeline` before the store
    dispatch of a `PlayheadChanged` (suspends when playing with a left press
    pending).
  - property `SourcePlaybackController.suspended: bool`.
  - `SourcePlaybackController.eventFilter(self, watched: QObject, event: QEvent) -> bool`
    installed on the timeline in `attach`; never consumes events.

**Contracts:**

- Event filter: `MouseButtonPress` with the left button → `press_pending =
  True`; `MouseButtonRelease` with the left button → `press_pending =
  False`, and if `resume_pending`, clear it and call `play()` (which starts
  from `state.timeline.playhead`).
- `playhead_changed_by_user()`: when `playing and press_pending and not
  suspended`: interrupt the worker, `active_session = None`, release the
  canvas hold (`release_live_frame`), `timeline.hold_playhead(None)`,
  `controls.set_position(None)`, `visible_position = None`, `suspended =
  True`, `resume_pending = True`; no commit and `set_playing` untouched
  (the row keeps showing `Pause`). `playing` stays `True` while suspended.
- `pause()` while suspended: clear `resume_pending`, `suspended = False`,
  `playing = False`, `set_playing(False)`, no commit (the drag already
  moved the playhead). `stop_and_commit()` and the stop-without-commit path
  treat a suspended state the same way. `play()` from a resume clears
  `suspended`.
- `play()` start rule with `controls.selection_only_enabled`: when the
  chosen start is outside `[start, end)`, start at `timeline.start`; pass
  `range_=(timeline.start, timeline.end)` to the worker; otherwise `None`.
- Store subscription addition: while playing (not suspended) and
  `selection_only_enabled`, when `state.timeline.start` or `.end` changed,
  assign `worker.range = (start, end)`. `selection_only_toggled(True)` while
  playing assigns the current range; `(False)` assigns `None`. Toggling while
  paused changes nothing else.
- Worker range loop: before pacing each decoded frame, read `self.range`
  once; if set and `pts >= OUT` or `pts < IN`: if less than `frame_interval`
  of wall time has passed since the last loop seek, sleep until it has (in
  interruptible slices); then `decoder.seek(IN)`, re-anchor
  (`anchor_pts = IN`, `anchor_wall = now`) and continue with the new
  `frames()` iterator. The single-frame range therefore shows one frame per
  interval with one seek per interval, never a storm.

- [ ] **Step 1: Write the failing tests**:
  - `test_press_move_release_on_the_timeline_suspends_and_resumes_from_the_new_position`:
    with the window shown and the timeline 900 px wide, play, wait for
    frames; `QTest.mousePress(timeline, LeftButton, pos=QPoint(450, 70))`
    → `suspended` is `True`, `holding_live_frame` is `False`,
    `held_playhead is None`, button text still `Pause`, `state.timeline.playhead`
    is the pressed position, no `PlayheadChanged` from the controller (only
    the timeline's own); `QTest.mouseMove` to `QPoint(500, 70)` then
    `QTest.mouseRelease` at it → the fake factory was called again and the
    new fake's first `seek` equals `state.timeline.playhead`, `suspended` is
    `False`, `playing` is `True`, `active_session` is a new number.
  - `test_a_frame_from_the_suspended_session_arriving_after_release_changes_nothing`:
    gate the first fake, suspend, release the gate after the resume →
    canvas holds only frames converted by the second fake (red channel
    encodes the fake's index).
  - `test_a_handle_press_does_not_suspend`: press on the start handle
    (locate it through `timeline._geometry.hit_test` as the timeline tests
    do, or press at the handle's known x for the presentation used) →
    `StartChanged` recorded, `suspended` is `False`, still playing.
  - `test_a_click_is_a_press_and_release`: `qtbot.mouseClick` on the strip
    while playing → playing afterwards from the clicked position, exactly
    one extra factory call.
  - `test_selection_only_starts_at_in_when_the_playhead_is_outside`: range
    `[1/5, 1/2)`, playhead `3/5`, check the box, `play()` → first `seek` is
    `Fraction(1, 5)` and the worker was built with `range_=(1/5, 1/2)`.
  - `test_selection_only_turned_on_while_playing_outside_jumps_to_in`:
    playing from `3/5` with the box off, click the box → the fake records a
    `seek(Fraction(1, 5))` within 50 ms and converted pts afterwards are
    inside `[1/5, 1/2)`.
  - `test_selection_only_toggled_while_paused_moves_nothing`: toggle on and
    off while paused → no `PlayheadChanged`, no factory call, no seek.
  - `test_the_loop_stays_inside_the_range_and_seeks_once_per_iteration`:
    range `[1/5, 3/10)` (10 frames), play for 150 ms → every converted pts
    is in `[1/5, 3/10)`, the fake logged at least two `seek(1/5)` calls, and
    the number of seeks is at most the number of converted frames divided by
    ten plus two.
  - `test_in_out_changed_during_a_loop_reach_the_worker`: while looping,
    dispatch `StartChanged(Fraction(1, 4))` into the store → the fake logs a
    `seek(Fraction(1, 4))` within 100 ms and every pts converted after it
    is at or above `1/4`.
  - `test_a_single_frame_range_is_a_still_without_a_seek_storm`: range
    `[1/5, 1/5 + 1/100)`, play for 100 ms → converted pts all equal the
    frame owning `1/5`, and the fake's seek count is at most 12.
  - `test_pause_while_suspended_drops_the_pending_resume`: press (suspend),
    `pause()`, release → no new factory call, button `Play`, no
    `PlayheadChanged` from the controller.
- [ ] **Step 2: Run** the file; the new tests fail.
- [ ] **Step 3: Implement** the worker loop, the event filter, suspension,
  the start rule and the subscription addition; add the one-line
  `self._playback.playhead_changed_by_user()` call to
  `_dispatch_timeline` for `PlayheadChanged` before the store dispatch.
- [ ] **Step 4: Run the local gate** plus, separately,
  `tests/ui/test_playback_controller.py`, `tests/ui/test_controller.py`,
  `tests/ui/test_timeline.py`, and `tests/ui/test_render_controller.py` on
  its own. Check `check_guardrails.py` still passes (`_dispatch_timeline`
  and `playback_controller.py` functions under 60 lines; split the worker
  loop into named helpers if needed).
- [ ] **Step 5: Walk it in the running application:** drag the marker back
  during playback (picture scrubs, resumes on release), drag a handle while
  playing (no interruption), turn on `Selection only` outside the range
  (jumps to IN and loops), narrow the range to one frame (a still, idle CPU).
- [ ] **Step 6: Commit** as `✨ feat(playback): scrub while playing and loop the selection`
  with a body naming the event filter, suspension/resume, the worker range
  loop, the loop guard and the start rule.

---

### Task 7: README and the product page

The spec's "Documentation changes" for the README and the screenshot. The
German catalogue and the scope entry are already done in Tasks 1, 3 and 4.

**Files:**
- Modify: `README.md` (line 24 bullet; a sentence on `Selection only` in the
  same list)
- Regenerate: `assets/screenshots/main-window.png` via
  `scripts/screenshots.py` — **requires macOS** (`_install_capture_style`
  refuses without the `macos` Qt style, lines 252-259). On this Windows
  machine the script cannot run; see Step 3.

**Interfaces:**
- Consumes: nothing code-facing.
- Produces: nothing code-facing.

- [ ] **Step 1: Edit the README**: "Open a local video and scrub to the part
  you want" becomes "Open a local video, play it, and scrub to the part you
  want"; add one sentence that `Selection only` loops playback inside the
  IN/OUT range so a cut can be judged in motion before rendering. Keep the
  README free of version history.
- [ ] **Step 2: Check** that nothing else in the README describes the
  Original pane as stills only (search for "still", "scrub", "frame").
- [ ] **Step 3: Screenshot.** If running on macOS with the model cache
  present: `QT_QPA_PLATFORM=offscreen uv run python scripts/screenshots.py`
  and confirm `main-window.png` shows the row in its paused state (the
  script's state snapshot has a ready source, so `apply` enables the row
  with `Play`). On Windows: do not hand-edit or hand-capture the image;
  report that the regeneration is pending the user's macOS pass, and list it
  in the final checklist below.
- [ ] **Step 4: Run** `uv run ruff check .` and
  `uv run pytest -q tests/test_scope_contract.py`.
- [ ] **Step 5: Commit** as `📝 docs(readme): describe source playback`
  with a body naming the changed bullet, the new sentence and whether the
  screenshot was regenerated.

---

## Manual acceptance checklist (user, after Task 7)

Not a subagent task. Spec "Acceptance" items 2-4 need real footage, real
hardware and packaged builds:

- [ ] **Packaged builds:** dispatching the release workflow on
  `feat/source-playback` (`CLAUDE.md` "Qualifying a change that ships")
  produces the artifacts without a release. **This dispatch needs the user's
  go**; the orchestrator does not trigger it on its own.
- [ ] Windows laptop (i7 11th gen), packaged build, a real Twitch clip
  (1440p60 H.264, ≥ 60 s): play from the start, set IN and OUT with `I`/`O`
  while playing, drag the marker back five seconds during playback, pause,
  render the range. The rendered range matches the two frames visible when
  `I` and `O` were pressed. Note the dropped-frame count from the log line
  `source playback stopped: N frames shown, M dropped`.
- [ ] macOS, signed bundle: the same run, plus a rotated phone clip (marker
  and crop stay aligned), plus quit while playing (clean exit, no "outlived
  shutdown" warning in the log).
- [ ] Any platform: replace the source while playing, start a preview while
  playing, resize the window while playing — no crash, no stale picture
  after the still arrives.
- [ ] macOS: regenerate `assets/screenshots/main-window.png` with
  `scripts/screenshots.py` if Task 7 could not, and commit it with the
  README commit message shape.
- [ ] CI: `tests/ui/test_playback_*` green on the macOS and Linux jobs,
  `tests/jobs/test_playback.py` green on the Windows job, before the branch
  is proposed.

## Spec coverage map

| Spec section | Task |
|---|---|
| Goal, Non-goals | all (no audio, no speed, no persistence; nothing added to frozen modules) |
| User-visible behaviour table (states) | 3 (hidden/disabled), 4 (ready/playing/locked), 6 (suspended) |
| Interaction table | 4 (Play at end, Pause, Preview/Render pressed, Result preview stale once), 5 (`I`/`O`, Space, Left/Right, job running), 6 (Selection only rows, IN/OUT during loop, drag, single frame) ; crop drag and window resize are existing canvas behaviour exercised in the checklist |
| Architecture: `jobs/playback.py` | 2 |
| Architecture: `ui/playback_controls.py` | 3 |
| Architecture: `ui/playback_controller.py`, sessions | 4, 6 |
| Seams in unfrozen modules | 3 (canvas, stage, render, class swap, tab order), 4 (controller, ports, app), 5 (`keyPressEvent`, `_dispatch_timeline`), 6 (drag hook) |
| Data flow: Play, Per frame, State renders, Pause and commit, End of stream | 4 |
| Data flow: IN/OUT while playing, Drag, Stops without commit | 5 (translations, stops), 6 (drag, range to worker) |
| Threading and shutdown | 2 (thread hazard test), 4 (worker lifecycle, shutdown) |
| Picture fidelity | 2 |
| VFR, end of stream, loop boundaries | 2 (pts pacing input), 4 (end), 6 (loop) |
| Errors and degradation | 4 |
| Frozen files | all (nothing edited) |
| Testing | 2, 3, 4, 5, 6 |
| Documentation changes | 1, 3, 4, 7 |
| Acceptance | checklist |
