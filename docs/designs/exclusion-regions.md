# Design: Exclusion regions

Architecture pass on 2026-09-12 for issue #80
Branch: feat/issue-80-exclusion-regions
Repo: smb-org/MatteLoop
Status: REVISED after an independent adversarial review; no implementation yet.
Every `file:line` below was read on this branch.

## Review findings addressed

An independent review rejected the first draft. All nine findings were
re-verified against the code before designing against them; two turned out to
have a smaller root cause than the review prescribed, and one turned out to be
a defect that already ships.

| # | Finding | Answered by | Verdict on re-check |
|---|---|---|---|
| 1 | The oriented→widget mapping is wrong for anamorphic 0°/180° sources | Decision 2a | **Confirmed, and wider than reported: the crop rectangle is mispainted today.** Root cause is one wrong viewport expression in non-frozen `core/crop.py:260-263`. Scoped out as a prerequisite fix, not folded in. The review's prescription — a new direct mapping for regions — is rejected: it would leave the crop broken and duplicate a transform. |
| 2 | Persisted regions are painted where they do not apply | Decision 1 (*Persistence*), Decision 2b | Confirmed. `build_crop_geometry` slides (`geometry.py:588` → `:387-395`) where `cut_exclusions` intersects. Answered by clipping regions to the source once, on `SourceLoaded`. |
| 3 | One dispatch per pointer move is a worker storm | Decision 6 (*Gesture contract*) | Confirmed, and worse than reported: `_schedule_facts` (`ui/transform_stage.py:412-442`) starts a `QThread` immediately with **no debounce**, and every dispatch rewrites ~12 QSettings keys (`ui/controller.py:486-504`, `ui/preferences.py:44-70`). Answered by commit-on-release. |
| 4 | The empty-union catch swallows corrupt frames | Decision 5 | Confirmed. `INVALID_FRAMING` also covers a non-RGBA frame (`geometry.py:1032-1038`), mismatched canvases (`geometry.py:770-775`) and a plan/frame size disagreement (`geometry.py:802-807`). Answered by a distinct signal, not a caught error code. |
| 5 | The "no re-segmentation" promise is not met | *Constraints* | Confirmed. The promise is narrowed in writing rather than engineered around. |
| 6 | Cache exactness is overstated | Decision 4 (*What this does not achieve*) | Confirmed on all three counts. Only the cheap one is fixed; the other two are documented instead of claimed away. |
| 7 | The downgrade dismissal is too strong | Decision 4 (*Migration*) | Confirmed. Restated as a bounded residual risk with the one guard that is free. |
| 8 | The render-loop copy ignores the ownership contract | Decision 3, site 2 | Confirmed — and dissolved. Staging before masking removes the copy entirely, so there is no second owner to register. |
| 9 | Governance and budget | *Implementation order*, change list | Confirmed. `main_window.py` is 351 lines and the ratchet only applies above 800 (`scripts/check_guardrails.py:124-126`), so it was never a baseline question; that flag is dropped. The scope entry moves first. |

## Problem statement

A burned-in overlay (Twitch chat, alert box, webcam frame, a bystander at the
frame edge) is read as foreground and survives into the cutout. `Crop` cannot
remove it when the overlay sits in front of the subject, and a
largest-component filter was measured to fail in both directions (#80).

The user drags one or more rectangles over the source frame; those areas are
forced to alpha 0 **after segmentation**, as part of `FramingSpec`. Two
decisions are settled by the maintainer and are not revisited here:

- **Rectangles only, several of them.** No polygon, no ellipse.
- **Post-segmentation, not pre-model.** The crop is part of the cut key
  (`core/fingerprints.py:207`), so a pre-segmentation mask would re-segment
  the whole clip on every drag, and rembg models judge the frame globally, so
  blanking an input region is model-dependent. Zeroing alpha afterwards is
  deterministic and reuses the stored cut set.

## What already exists (verified)

**Cut frames are crop-sized.** `_produce_cut_frame` (`jobs/render.py:1529`)
decodes, applies `apply_source_crop` with `request.crop`
(`render.py:1552-1558`), segments, and refuses anything that is not exactly
`(crop.height, crop.width, 4)` (`render.py:1585-1596`). Every stored and every
fresh cut frame is therefore in **cut space**: origin at the crop's top-left.
The crop itself is in oriented source pixels (`core/specs.py:108-109`,
`ui/crop_canvas.py:44`). Claim 1 of the brief holds.

**Six consumers read cut alpha for framing, not five.** The brief listed five;
the sixth is the one a user looks at most:

| # | Site | What it does with the alpha |
|---|---|---|
| 1 | `jobs/render.py:924` | preview: `alpha_bounds(cut, …)`; then `apply_framing` at `:929`/`:945` and `_immutable_rgba(cut)` at `:959` — the preview image itself |
| 2 | `jobs/render.py:1109` | render loop: `alpha_bounds` per frame into the range union; the *same* `cut` object is then stored by `self._workspace.stage` at `:1112` |
| 3 | `jobs/render.py:1338` | `_rebuild_union`: `read_cut` → `alpha_bounds` when the cached union misses |
| 4 | `jobs/render.py:1386-1387` | `stage_encoder_frames(partial(read_cut, private), …)` → `_stage_frame` → `apply_framing` (`jobs/transform_stage.py:122-124`) |
| 5 | `ui/transform_stage.py:705-707` | `union_alpha_bounds(_iter_stored_frames(…))` — the Transform group's framed size |
| 6 | **`ui/result_player.py:488-490`** | `_load_one_frame`: `frame_reader.read` → `apply_framing(cut, plan)` — the frames the result player loops |

Missing site 6 would make the player show the overlay that the encoder
removes; missing site 5 would make the trim readout hug it.

**All cut pixels enter framing through three producers**, each with a bounded
caller list: `_produce_cut_frame` (sites 1, 2), `WorkspacePort.read_cut`
(`render.py:158`; sites 3, 4; `FilesystemWorkspacePort.read_cut` at
`render.py:538-544` returns `opened.copy()` — `workspace/_models.py:245-247`
— so it is a fresh object per call), and `FrameReader.read`
(`ui/result_player.py:55-58`; sites 5, 6; `_DirectFrameReader` at
`ui/transform_stage.py:60-68` opens the file per call). Nothing else in `src/`
calls `alpha_bounds`, `union_alpha_bounds` or `apply_framing` (grep on this
branch).

**The union cache.** `CutUnionMetadata(bounds, alpha_threshold, fingerprint)`
(`workspace/_manifest.py:112-135`) is written at `render.py:1126-1134` and by
CAS at `render.py:1348-1363`; the manifest parser refuses any other key
(`_exact_keys` at `_manifest.py:361-365`), so the format cannot grow.
`union_fingerprint` (`core/fingerprints.py:249-260`) hashes only `cut_key` and
`alpha_threshold`. It is checked in **three** places that do **not** agree:

- `PreviewService._matching_union` (`render.py:989-997`): fingerprint **and**
  threshold text.
- `RenderService._rebuild_union` (`render.py:1326-1334`): fingerprint **and**
  threshold text.
- `ui/transform_stage.py:_resolve_union` (`:695-703`): **threshold only** —
  it never looks at `metadata.fingerprint`. Claim 3 of the brief is therefore
  understated: the UI cache would go stale with exclusions *and* is already
  blind to any other fingerprint drift.

External edits already drop the cache (`workspace/_cut_ops.py:380`), and the
UI skips it for edited manifests (`ui/transform_stage.py:695-696`).

**`FramingSpec`** (`core/specs.py:244-291`) is a frozen dataclass with
`validate()`; it is rebuilt from flat `ParameterState` fields in three places:
`core/parameters.py:72-77` (validation), `ui/request_builder.py:97-102` (the
request), `ui/transform_stage.py:723-729` (the facts worker). Claim 4 holds.

**Empty mask today.** `union_alpha_bounds` raises `INVALID_FRAMING`
(`core/geometry.py:781-786`), `framing_plan` raises when `trim` and the union
is `None` (`jobs/transform_stage.py:40-45`), and the render aborts —
`tests/jobs/test_render.py:1525-1552` pins that. The preview does not abort:
`local_bounds` becomes `None` and the display is `cut.copy()`
(`render.py:924, 941`). The UI facts worker fails and disables the Transform
group (`ui/transform_stage.py:123, 486-490`).

**The crop editor.** `CropCanvas` (`ui/crop_canvas.py`, 359 lines) edits one
rectangle: `build_crop_geometry` (`core/geometry.py:570-615`) fans one
`CropSpec` out into eight handles plus a `"crop"` body with a fixed priority
(`:601-611`); `crop_from_drag`/`nudge_crop` (`core/crop.py:33-71`) move or
resize any rectangle given a target name; the two hooks `_constrain` and
`_crop_event` (`crop_canvas.py:261-280`) let `ResultPlayerCanvas` reuse the
whole machinery for a different rectangle and a different command
(`result_player.py:253-265`). `set_crop_edit` (`result_player.py:166-190`) is
the precedent for a mode switch on this canvas.

**Frozen modules on this path** (`scripts/guardrails-baseline.json`):
`jobs/render.py` 2886, `core/geometry.py` 1141, `core/state.py` 882,
`ui/inspector.py` 897. Growth for a real feature needs the maintainer's
agreement and `check_guardrails.py --update`
(`docs/engineering-guardrails.md:279-303`).

The ratchet only fires **above** the 800-line module budget
(`scripts/check_guardrails.py:124-126`), so a module under 800 is not frozen
however long its baseline entry is. `ui/main_window.py` is 351 lines: the
first draft listed it as frozen at 328 and that was wrong on both counts.

## Constraints

- Regions apply after segmentation and never enter the cut key. A region
  change must not invalidate the stored cut set, and must invalidate the
  preview, the render fingerprint and the union cache.
- **The re-segmentation promise, stated precisely** (review finding 5). What
  holds: the stored cut set stays valid under any regions, so **Render and
  Rebuild never re-segment** for a region change — they reread the stored
  frames and re-measure. What does **not** hold: the *preview* is invalidated
  like any other cleanup change, and `AppState` keeps only the displayed
  `QImage` (`core/state.py:72`, `ui/presenter.py:177`), not the cut behind it,
  so the next Preview press reruns the model on that one frame. Retaining the
  unmasked preview cut in order to re-frame it locally is a larger change than
  this feature (it is a preview-caching feature in its own right) and is
  recorded in *Rejected alternatives*. Nothing in this document may claim that
  a region change is free for the preview.
- No new dependency; Pillow already does everything needed.
- `core/geometry.py` is not touched. Everything new lives in one new
  Qt-free module and the non-frozen files.
- Degrade, never refuse (G4, `docs/engineering-guardrails.md:189`).
- Preferences, cut sets and union metadata written by v0.4.1 keep working.
- String literals at every `translate()` call site; German catalogue entries;
  README and generated screenshots updated (`CLAUDE.md`, *Keeping the README
  honest*).

## Decision 1 — Data model

`FramingSpec` gains one field:

```python
@dataclass(frozen=True)
class FramingSpec:
    trim: bool = False
    alpha_threshold: Decimal = Decimal("2.0")
    padding: int = 0
    stretch_x: Decimal = Decimal("1.0")
    exclusions: tuple[CropSpec, ...] = ()
```

- **Type: `tuple[CropSpec, ...]`.** A region is an axis-aligned integer
  rectangle in oriented source pixels — exactly what `CropSpec` already is
  (`specs.py:108-134`). `crop_from_drag`, `nudge_crop` and `clamp_crop`
  (`core/crop.py:33-90`) are pure rectangle arithmetic and take a region
  unchanged. No `ExclusionRegion` type.

  **Correction (review finding 1).** The first draft also claimed
  `build_crop_geometry` works on a region "with no adaptation". That is
  wrong, and not only for regions: the oriented→raw helper it is fed from is
  broken today. See Decision 2a — it is a prerequisite fix, not part of this
  feature.
- **Validation** (`FramingSpec.validate`): `exclusions` must be a `tuple`,
  every element a `CropSpec` (which already enforces `x, y >= 0`,
  `width, height >= 1`). No upper bound against the source: a region beyond
  the frame is clipped at apply time (Decision 2), and `validate_for_source`
  (`specs.py:665-674`) is left alone so a region persisted from a larger
  video does not refuse a smaller one. No count cap.
- **Ordering: none on the spec.** The tuple keeps user order, because the
  canvas selects a region by index and a reorder under the pointer would be a
  visible jump. Canonicalisation happens once, in `cut_exclusions()`
  (Decision 2): sorted, de-duplicated, clipped. Zeroing is commutative, so
  two orderings of the same regions are the same request everywhere it
  matters — the fingerprint, the union cache, the pixels.
- **Fingerprint encoding.** The *effective* cut-space boxes, never the raw
  regions: `"exclusions": [[left, top, right, bottom], …]` sorted ascending,
  **present only when non-empty**. Rationale in Decision 4.
- **Reducer.** `ParameterState.exclusions: tuple[CropSpec, ...] = ()`
  (`parameters.py:47-60`), validated by passing it into the existing
  `FramingSpec(...)` call at `:72-77`. One new event,
  `ExclusionsChanged(exclusions: tuple[CropSpec, ...])`, reduced like
  `_reduce_cleanup_value` (`:322-337`): reject anything that is not a tuple of
  `CropSpec`, **no-op when the effective boxes are unchanged** (not merely
  when the raw tuples are equal — Decision 4), otherwise
  `PreviewInvalidationReason.CROP_CLEANUP`. `ParametersReset` clears it
  (`:218-252`, add to the reset and to the CROP_CLEANUP comparison).
- **Persistence.** A parameter, like the alpha threshold: it survives a
  source change and a restart. The overlay of a streamer's layout is the same
  on every clip of that stream, which is the case worth carrying. One
  QSettings string `parameters/exclusions` = `"x,y,w,h;x,y,w,h"`;
  `parameters_from_values` (`parameters.py:393-420`) parses it with the
  per-key fallback the file already uses: any malformed entry → `()`.

  **Correction (review finding 2).** The first draft called a stale rectangle
  on an unrelated video "harmless". It is not: a carried region that
  *intersects* the new frame deletes those pixels, and one that sticks out is
  **painted somewhere else than it masks**, because `build_crop_geometry`
  slides a rectangle inward (`geometry.py:588` → `clamp_source_rect`,
  `geometry.py:387-395`) while `cut_exclusions` intersects. Measured: a
  carried `(1700,0,100,100)` on a 640-wide source paints at x 540…640 and
  masks nothing.

  Carrying stays, but the regions are **clipped to the new source once, on
  load** (Decision 2b), so a carried region is either a real rectangle inside
  the frame or gone. Painted and masked can then never disagree, because
  after the clip `clamp_source_rect` has nothing left to slide.

## Decision 2 — Coordinates: stored in oriented source space, mapped once

Regions are stored in **oriented source pixels**, the crop's own space. A
burned-in overlay is fixed in the source frame; if regions lived in cut space,
moving the crop would drag them along. The brief's claim 1 is right.

The one mapping lives in a new Qt-free module, `src/matteloop/core/exclusion.py`:

```python
def cut_exclusions(
    exclusions: tuple[CropSpec, ...], crop: CropSpec
) -> tuple[PixelBounds, ...]:
    """Map oriented-source regions into the crop-sized cut, clipped and canonical."""
    boxes: set[tuple[int, int, int, int]] = set()
    for region in exclusions:
        left = max(region.x, crop.x) - crop.x
        top = max(region.y, crop.y) - crop.y
        right = min(region.x + region.width, crop.x + crop.width) - crop.x
        bottom = min(region.y + region.height, crop.y + crop.height) - crop.y
        if right > left and bottom > top:
            boxes.add((left, top, right, bottom))
    return tuple(PixelBounds(*box) for box in sorted(boxes))


def exclude_alpha(image: Image.Image, boxes: tuple[PixelBounds, ...]) -> None:
    """Zero RGBA inside each box, in place."""
    for box in boxes:
        image.paste((0, 0, 0, 0), (box.left, box.top, box.right, box.bottom))
```

- A region **fully outside the crop** produces no box: it is absent from the
  pixels, from the union cache key and from the render/preview fingerprints.
  Dragging it stales nothing — but only because the reducer compares effective
  boxes rather than raw tuples (Decision 4, false miss 1). The first draft
  claimed this while the reducer still compared raw tuples, and was wrong.
- A region **partly outside** is clipped to its intersection.
- `PixelBounds` (`geometry.py:142`) already refuses empty or negative boxes,
  so the `right > left and bottom > top` guard is the only check needed.
- RGB is zeroed with the alpha, matching the canonical black that
  `_decontaminate_edge_colors_in_place` writes under alpha 0
  (`render.py:1611-1620`), so the two edge modes produce identical bytes in an
  excluded box.
- Alpha 0 is never "visible": the threshold table is strict
  (`geometry.py:1108-1112`), so an excluded box drops out of every bound at
  every threshold including 0.

### Decision 2a — Prerequisite: the oriented→raw mapping is broken today

**This is a defect that already ships, not one this feature introduces.**

`oriented_rect_to_source_rect` (`core/crop.py:94-108`) builds an intermediate
`MediaTransform` through `_orientation_transform` (`core/crop.py:254-266`),
whose viewport is:

```python
viewport=SizeF(
    source_height if rotation in {90, 270} else source_width,
    source_width * pixel_aspect if rotation in {90, 270} else source_height,
),
```

That expression is `MediaTransform.oriented_display_size`
(`core/geometry.py:305-316`) written out by hand — correct for 90°/270°, and
**missing the `* pixel_aspect` for 0°/180°**. The intermediate transform then
fits a 32-wide display into a 16-wide viewport, scale 0.5 instead of 1, and
stops round-tripping.

Measured on this branch over rotation {0, 90, 180, 270} × PAR {1, 2, 1.5,
0.75}, comparing the painted `visual["crop"]` against the oriented rectangle's
true position in `content_rect`: **6 of 16 combinations are wrong — every
0°/180° case with PAR ≠ 1.** The review's worked example reproduces exactly:
coded 16×8, PAR 2, rotation 0, oriented rect (8,0,8,8) → raw
`RectF(8,-4,8,16)`.

The single existing test pins rotation 270 with PAR 2
(`tests/core/test_crop.py:63-70`) — one of the ten passing combinations, which
is why this was never caught.

**The consequence today**, before any region exists: `crop_canvas.py:288`
feeds this helper, so **on an anamorphic un-rotated source the crop rectangle
is painted in the wrong place and at the wrong size.** Anamorphic 0° is not
exotic — it is DVD-era and broadcast 16:9 SD content.

**The fix is one expression** in the non-frozen `core/crop.py`: make the
viewport the oriented display size for every rotation. Verified on this
branch — all 16 combinations pass.

**Scoping.** It is a pre-existing defect with its own trigger, so it lands
**before** this feature, on its own branch, as its own `fix:` commit with its
own `Trigger:` line and its own issue. Exclusion regions depend on it and must
not be implemented on top of the broken helper — but they must also not
smuggle the fix in as a detail of a feature commit.

Once it is in, regions genuinely do reuse `build_crop_geometry` unchanged,
which is what the first draft assumed without checking.

### Decision 2b — Carried regions are clipped to the source on load

`SourceLoaded` (`core/state.py:398-407`) already resets the crop to the full
frame via `default_crop_for_source`. Regions are carried instead of reset, so
they are clipped there in the same `replace(...)` call:

```python
parameters=_parameters.clip_exclusions(state.parameters, event.value),
```

`clip_exclusions` lives in the non-frozen `core/parameters.py` and intersects
every region with `(0, 0, width, height)`, dropping what falls entirely
outside — the same intersect-don't-slide rule as `cut_exclusions`.

- **Why intersect, not `clamp_crop`.** `clamp_crop` *slides* a rectangle
  inward to preserve its size (`core/crop.py:73-90`, deliberately, for issue
  #25). Sliding an exclusion moves it onto content the user wants to keep —
  the opposite of what it is for. Regions intersect; the crop keeps sliding.
- **Why it cannot be bypassed.** Every source that reaches a READY state goes
  through `SourceLoaded`; there is no other writer of `source_value`. A region
  that survives into `state.parameters` is therefore inside the frame by
  construction, which is what makes painted == masked a property rather than a
  convention.
- **Cost.** `core/state.py` is 882 lines and ratcheted, so this is **+1 line
  on a frozen module** and needs the maintainer's agreement plus
  `check_guardrails.py --update`. That is the honest price of the invariant;
  the alternative (clipping only for display) is in *Rejected alternatives*
  with the drag-jump it causes.
- Clipping on load, not on every read, also means the fingerprint sees the
  same rectangles the user sees.

**Which crop.** Preview, render and rebuild use `request.crop` — a rebuild
finds its cut set by a key that includes that crop
(`render.py:561-591`, `fingerprints.py:207`), so the two are equal by
construction. The UI transform stage has no request; it reads the cut's own
crop from `manifest.cache_key_inputs["crop"]` with a `_crop_from_manifest`
helper beside the existing `_fps_from_manifest`
(`ui/transform_stage.py:741-748`), which reads the same mapping.

## Decision 3 — The seam: one function, three producers, six sites

`exclude_alpha` is the only function that zeroes, and `cut_exclusions` the only
function that maps. There is no *single call site* — the pixels arrive through
three producers, and the render loop must measure an excluded frame while
storing the unexcluded one — so the argument is a bounded enumeration, not a
choke point:

| Site | Change |
|---|---|
| 1 preview (`render.py:913-960`) | `exclude_alpha(cut, boxes)` once, right after `_produce_cut_frame`. Bounds, both `apply_framing` calls and both `_immutable_rgba` images see it. |
| 2 render loop (`render.py:1108-1112`) | **stage first, then mask in place** — see below. No copy. |
| 3 `_rebuild_union` (`render.py:1338`) | `exclude_alpha(image, boxes)` after `read_cut`. |
| 4 encoder (`render.py:1386`) | `stage_encoder_frames` gains `exclusions: tuple[PixelBounds, ...]` and `_stage_frame` calls `exclude_alpha(cut, exclusions)` before `apply_framing` (`jobs/transform_stage.py:122-124`). |
| 5 UI facts (`ui/transform_stage.py:710-720`) | `_iter_stored_frames` takes the boxes and calls `exclude_alpha` before `yield`. |
| 6 result player (`ui/result_player.py:480-490`) | `FrameLoadWorker` and `_load_one_frame` take the boxes; `exclude_alpha` before `apply_framing`. |

**Site 2 in detail (review finding 8).** The first draft masked a `cut.copy()`
so the unmasked frame could still be staged. The review was right that a
second full-resolution RGBA owner must be registered with
`RgbaOwnershipTracker` (`core/rgba.py:72-95`), whose peak and current counts
are reported in `RenderArtifact`.

There is no copy to register, because the order can simply be swapped. Today:

```python
bounds = alpha_bounds(cut, request.framing.alpha_threshold)     # :1109
union = _union_bounds(union, bounds)                            # :1110
frame_records.append(self._workspace.stage(staged, index, cut)) # :1112
```

Becomes: stage, then mask `cut` in place, then measure. `stage_cut`
(`workspace/_cut_ops.py:56-100`) opens the frame file and writes the PNG
synchronously inside the call and returns a `CutFrame` record derived from the
written bytes — it retains no reference to the image — so mutating `cut`
afterwards cannot reach the stored file. The existing `finally: cut.close()`
(`render.py:1113-1116`) is unchanged.

Result: one owner, unchanged peak accounting, no tracker change, and one fewer
full-resolution allocation per frame than the first draft proposed. The stored
set still contains unmasked pixels, which is the property the whole design
rests on.

Why the seam is not lower: `read_cut`/`FrameReader.read` also serve identity
work — hashing and rescanning in `workspace/_cut_ops.py`, external-edit
detection, the CAS's `_scan_cut_set` — where the bytes must be the stored
bytes. Zeroing inside the reader would make an edited-cut scan disagree with
the file on disk. Why not higher (inside `FramingPlan`/`apply_framing`):
bounds are computed before any plan exists, so the four measure sites would
still be explicit, and it grows the frozen `geometry.py`.

What makes "missed a site" impossible in practice: the docstring of
`core/exclusion.py` lists the three producers and six sites, and the test plan
has one behavioural test per site that fails when that site is skipped. A new
consumer of cut alpha is a new caller of one of three functions, which a grep
shows.

## Decision 4 — Union-cache invalidation and migration

The manifest format cannot gain a key (`_manifest.py:361-365`), and it does not
need one: the invalidation goes through the `fingerprint` field that already
exists.

`core/fingerprints.py`:

```python
def cut_union_fingerprint(
    framing: FramingSpec, crop: CropSpec, *, cut_key: str
) -> str:
    payload: dict[str, object] = {
        **_schema("cut-union"),
        "cut_key": _validated_sha256(cut_key, "cut_key"),
        "alpha_threshold": _canonical_decimal(framing.alpha_threshold),
    }
    boxes = cut_exclusions(framing.exclusions, crop)
    if boxes:
        payload["exclusions"] = [[b.left, b.top, b.right, b.bottom] for b in boxes]
    return _canonical_hash(payload)


def union_fingerprint(request: RenderRequest, *, cut_key: str) -> str:
    request = _validated_request(request)
    return cut_union_fingerprint(request.framing, request.crop, cut_key=cut_key)
```

`_framing()` (`fingerprints.py:348-354`) takes the request instead of the spec
and adds the same `"exclusions"` entry under the same non-empty rule, so
`preview_fingerprint` and `render_fingerprint` track effective regions.

**One predicate for all three checks.** `jobs/transform_stage.py` (already the
module the UI imports `framing_plan` from) gains:

```python
def cached_union(
    metadata: CutUnionMetadata | None, framing: FramingSpec, crop: CropSpec,
    cut_key: str,
) -> PixelBounds | None:
    if metadata is None or metadata.fingerprint != cut_union_fingerprint(
        framing, crop, cut_key=cut_key
    ):
        return None
    return PixelBounds(*metadata.bounds)
```

`_matching_union`, `_rebuild_union` and the UI's `_resolve_union` all call it.
The separate `alpha_threshold` text comparison in the two render sites is
subsumed (the fingerprint already hashes the canonical threshold, and both
are written from the same value at `render.py:1130-1132`), and the UI stops
being blind to the fingerprint. Three predicates that had already diverged
become one.

**Migration — stated explicitly.** Metadata written by v0.4.1 hashes
`{schema, cut_key, alpha_threshold}`. Because the `"exclusions"` key is
**omitted when empty**, a request without regions computes the byte-identical
fingerprint: **existing union metadata stays a hit**, and existing preview and
render fingerprints are unchanged. A request *with* regions computes a
different fingerprint → miss → the existing recompute-and-CAS path
(`render.py:1336-1363`) overwrites the entry, exactly as a threshold change
does today. Removing the regions again is a miss again and a second CAS;
that is one pass over the stored frames, the same cost as any threshold
change, and it needs no second cache slot.

Preferences: the new key is absent on first launch → `()`; nothing else in
`parameters/` changes. Cut sets: untouched, by design.

**The downgrade risk, stated properly (review finding 7).** The first draft
dismissed it because "the updater refuses downgrades". That is not a
sufficient argument: it does not stop an older binary that is still installed,
restored from a backup, or run as a portable build against the same cache
directory.

The concrete disagreement: a new build writes region-derived bounds into the
shared manifest. An old **Render/Rebuild** compares the fingerprint
(`render.py:989-997`, `:1326-1334`), fails to reproduce it, and recomputes the
unexcluded union — correct. An old **UI facts worker** compares the threshold
only (`ui/transform_stage.py:695-703`) and accepts the narrower
region-derived bounds, then applies them to unmasked stored frames. The
Transform group would show a framed size tighter than what that build would
actually encode.

- **Blast radius:** a wrong readout in one panel of an old build. The bounds
  are narrower, never wider, so nothing is encoded that the user cannot see;
  and the old build's own render recomputes correctly, so no file is wrong.
- **The guard that is free:** the threshold text is written from the same
  value in both builds, so it cannot distinguish them — but this design
  already replaces that blind check with `cached_union` in the *new* build
  (finding: the UI is blind to the fingerprint on `main` today, independent of
  regions). Shipping that fix is what stops the next version of this problem;
  it cannot retroactively fix a binary already on disk.
- **Residual risk accepted:** an older build, run by hand against a cache a
  newer build wrote regions into, shows a stale framed size in the Transform
  group until that cut set is rescanned. Not guarded further; guarding it
  would need a manifest key the parser refuses (`_manifest.py:361-365`).

### What this does not achieve (review finding 6)

No **false hit** was found on re-check: the crop is in the cut key
(`fingerprints.py:207`), so a crop change already changes `cut_key`, and the
effective-box payload matches the pixels exactly. Ordering and exact
duplicates canonicalise away. Rotation is a source property and changes the
source fingerprint.

Three **false misses** remain, and are documented rather than claimed away:

1. **A fully-outside region still stales the preview.** The reducer compares
   raw tuples, so dragging a region that produces no box dispatches a real
   change. Decision 1's earlier claim that "dragging it does not stale
   anything" was wrong about the reducer, though right about the fingerprint.
   **Fixed, because it is cheap:** the reducer no-ops when the *effective*
   boxes are unchanged, comparing `cut_exclusions(new, crop)` against
   `cut_exclusions(old, crop)`. After Decision 2b a fully-outside region only
   exists relative to the crop, not the source.
2. **Equivalent geometries hash differently.** One rectangle plus a rectangle
   contained in it, or two tiles that exactly cover one rectangle, produce
   identical pixels and different fingerprints. Merging overlapping rectangles
   into a canonical cover is real geometry code for a cache miss nobody has
   measured. **Not fixed.** Cost is one recompute.
3. **An empty union cannot be cached at all.** `CutUnionMetadata` refuses
   non-positive bounds (`workspace/_manifest.py:126-129`) and `_rebuild_union`
   publishes only a non-empty union (`render.py:1347`), so a legitimately
   fully-transparent result is rescanned on every facts run. **Not fixed** —
   it needs a manifest key the parser refuses. It is also the pathological
   case (the user excluded everything), and Decision 5 makes it visible rather
   than fatal.

## Decision 5 — Empty mask: trim degrades to the full canvas

A region can cover the whole subject, or the whole frame. Today
`framing_plan` raises and the job aborts (`jobs/transform_stage.py:40-45`,
pinned by `tests/jobs/test_render.py:1525-1552`). The user can fix this from
the UI, so G4 does not strictly apply — but a render that aborts because trim
found nothing is a refusal of the one thing the user asked for, and the
degraded result is trivially correct:

- `framing_plan(source_size, None, framing)` with `trim=True` returns the
  **untrimmed** plan (`global_bounds=None`) instead of raising. The output is
  the crop canvas plus padding; if the mask is genuinely empty, every frame is
  fully transparent — which is what excluding everything means.
- `RenderService` appends `"trim skipped: no pixel above the alpha threshold
  survives the exclusion regions"` to `notes` (`render.py:1033`), the channel
  the disk advisory already uses. Nothing in `ui/` displays `notes` today
  (grep), so this reaches the log, not a dialog. Not adding a UI channel now.
- **What the user sees:** the preview's Result canvas shows the transparent
  frame; the completion summary reports the crop's dimensions rather than a
  trimmed size; the Transform group's framed size equals the crop. The
  rectangles on the Original canvas are the explanation.
- `tests/jobs/test_transform_stage.py:34` and `tests/jobs/test_render.py:1525`
  are updated to pin the new behaviour (test plan). **`test_render.py:1522`'s
  `visible_size=10` minimum-dimension branch must survive** — it pins a
  different refusal (a trimmed box below `MIN_FINAL_DIMENSION`), which is not
  degraded here and must keep failing loudly.

**An empty mask is signalled, not caught (review finding 4).**

The first draft had the UI catch `ValidationError` with `INVALID_FRAMING` and
treat it as an empty union. That code is not specific to an empty mask —
`union_alpha_bounds` and its helpers raise the same code for:

- a non-iterable argument (`geometry.py:748-754`),
- a frame that is not a real Pillow RGBA image (`geometry.py:1032-1038`),
- frames whose canvases disagree (`geometry.py:770-775`),
- and later, in `apply_framing`, a frame that does not match the plan
  (`geometry.py:802-807`).

Failure scenario the blanket catch produces: two externally edited frames have
different dimensions. The facts worker swallows the mismatch as "empty",
reports the full manifest canvas as a valid framed size and starts the player;
`apply_framing` then rejects the real frame size, and `FrameLoadWorker` catches
only filesystem and Pillow decode errors, so the failure surfaces somewhere
unrelated — or not at all. A corrupt cut set would present as a working one.

Instead, the emptiness is carried as a value:

- `core/exclusion.py` gains
  `union_alpha_bounds_or_none(frames, threshold) -> PixelBounds | None`,
  which pre-checks the one condition that means "empty" and lets every other
  `ValidationError` propagate untouched. It lives beside the other new
  helpers rather than in the frozen `core/geometry.py`; `union_alpha_bounds`
  is not modified.
- `_resolve_union` (`ui/transform_stage.py:691-707`) and `_rebuild_union`
  (`render.py:1338`) call it and branch on `None`. Nothing catches
  `INVALID_FRAMING` anywhere in this design.
- `framing_plan` then receives `None` with `trim=True` and returns the
  untrimmed plan.

So an empty mask degrades and a corrupt or inconsistent cut set still fails
loudly — the test plan pins both, in the same pair of tests.

The preview path needs no change: it already shows the cut unframed when no
union matches (`render.py:940-941`).

## Decision 6 — UI: a mode switch on `CropCanvas`, geometry reused per region

**Mode switch, not an extended priority list.** `build_crop_geometry` is in the
frozen `geometry.py` and its priority tuple names one rectangle. Extending it
to N regions means N×9 targets, a priority policy for overlaps, and growth in
a frozen file. A mode switch costs nothing in geometry and answers the overlap
question by construction: **in region mode the crop has no hit targets; in
crop mode regions have no hit targets. A region over a crop handle is never
ambiguous because the two are never live at once.**

**Controls** (Inspector, *Crop & cleanup* section, one form row after
*Horizontal stretch*, label `"Exclusions"`), hosted the way `TransformGroup`
is hosted — a small widget module so the frozen `inspector.py` grows by the
five lines that construct, place, apply and tab-order it:

`ui/exclusion_controls.py` — `ExclusionControls(QWidget)`:
- `edit_button`: checkable `QToolButton`, text `"Edit regions"`, object name
  `exclusion_edit`. Checked = the Original canvas is in region mode. UI-local
  state, like `TransformGroup.crop_edit_toggled`; it is not reducer state.
- `clear_button`: `"Clear"`, object name `exclusion_clear`, enabled only when
  regions exist; dispatches `ExclusionsChanged(())`.
- `count_label`: `translate("Inspector", "%n region(s)", "", n)` — Qt's
  numerus form, literal at the call site.
- Signals: `edit_toggled(bool)`, `command_requested(object)`.
- `apply(presentation, editable)` mirrors `apply_parameters`
  (`inspector.py:487-516`): disabled until a source is loaded; leaving
  region mode is forced when the controls become non-editable (a running job).
- Wiring: `main_window.py:224-225` already connects `command_requested`;
  one more line connects `edit_toggled` to `original_canvas.set_exclusion_edit`.

**Source-canvas context menu.** `ExclusionCanvas.contextMenuEvent` is the
first context-menu pattern in the application and remains deliberately small.
It builds its actions from the oriented source point under the pointer:

- Empty canvas: `Exclude this area`, checkable `Edit exclusion regions`, and
  `Remove all regions` when at least one region exists.
- Existing region: `Remove this region`, `Remove all regions`, and checkable
  `Edit exclusion regions`.

The menu is unavailable while `capabilities(state).can_edit` is false, and
each mutating action emits exactly one `ExclusionsChanged` command for the
reducer. `Exclude this area` creates a default-sized rectangle derived from
the source dimensions (20% of each dimension), centred on the click and
clamped inside the frame. It does not start a rubber band: beginning a drag
from a menu is unreliable, while the existing handles let the user resize the
newly selected region immediately. The menu handles keyboard-origin context
events at the selected region, or the frame centre when no region is selected.

The Inspector row remains the discovery surface and the keyboard path. Its
toggle and the menu's checkable toggle stay synchronized in both directions;
the menu is an accelerator, not a replacement for the Inspector controls.
The checkable Edit action changes UI-local mode and emits no
`ExclusionsChanged`; only actions that change the region tuple dispatch that
reducer command.

**`CropCanvas` in region mode** (`set_exclusion_edit(enabled: bool)`):
- `CropPresentation` gains `exclusions: tuple[CropSpec, ...] = ()`
  (`ui/crop_presentation.py:12-21`), filled by `present_crop` from
  `state.parameters.exclusions`. The default keeps every existing constructor
  call and test valid.
- **Painting, both modes:** every region is painted whenever it exists — a
  translucent fill (`#E5484D` at alpha 90) with a 1 px solid border — so a
  persisted region is always visible on a new video. In crop mode that is all;
  the crop keeps its handles. In region mode the crop is drawn as its dimmed
  outline **without handles**, and the selected region gets the eight handles
  and the focus ring.
- **Geometry:** `_rebuild_geometry` builds `build_crop_geometry` for
  `_edited_rect()` — the selected region in region mode, else the crop. That
  is the entire reuse: the same eight handles, the same
  `crop_from_drag`/`nudge_crop`, the same accessible announcement, aimed at a
  different `CropSpec`.
- **Press** (left button, editable, region mode):
  1. hit-test the selected region's geometry → a handle or `"crop"` starts a
     resize/move drag of that region, exactly as the crop's drag today
     (`crop_canvas.py:142-167`);
  2. else, if the oriented point is inside another region → it becomes
     selected and a move drag starts in the same press;
  3. else → a rubber band starts from the press point.
- **Move:** nothing is dispatched. The region under the pointer and the rubber
  band are both painted from canvas-local state. See *Gesture contract*.
- **Release:** exactly one dispatch. A rubber band smaller than 8×8 oriented
  source pixels is discarded and dispatches nothing; otherwise
  `ExclusionsChanged(regions + (new,))` and the new region is selected. A
  move or resize dispatches `ExclusionsChanged` with that index replaced.
  Regions are intersected with the source, never slid (Decision 2b).

**Gesture contract: one dispatch per gesture (review finding 3).**

The crop dispatches `CropChanged` on every move, and that is affordable
because the crop only re-renders a canvas. A region change is a *framing*
change, and framing changes are expensive on this path:

- `_schedule_facts` (`ui/transform_stage.py:412-442`) starts a `QThread` and a
  `_CutFactsWorker` **immediately — there is no debounce on this path.** The
  module's `_debounce_timer` belongs to the frame worker, not the facts
  worker.
- A superseded worker is cancelled but deliberately **retained until its
  thread finishes**, because dropping the reference used to crash
  (`tests/ui/test_transform_stage.py:506`). A drag would therefore accumulate
  concurrent retiring threads, each scanning stored frames.
- Every parameter dispatch rewrites the whole preference block — about a dozen
  `QSettings.setValue` calls (`ui/controller.py:486-504`,
  `ui/preferences.py:44-70`).

So a 200-sample drag would mean 200 threads, 200 cut scans and ~2 400 settings
writes.

The precedent is already in the same module: the result-crop editor suppresses
reloads while editing and performs exactly one on exit
(`ui/transform_stage.py:300-315`, pinned by
`tests/ui/test_transform_stage.py:811`). Regions adopt the same shape:

- `CropCanvas` holds the in-progress rectangle in local state and repaints
  itself directly; the reducer sees nothing.
- `mouseReleaseEvent` dispatches once — or not at all, if the gesture was a
  discarded rubber band or left the rectangle unchanged.
- Keyboard nudges dispatch per key press. An arrow key is one discrete edit,
  not a stream, so no coalescing is needed there; `Escape` during a drag
  abandons it without dispatching.

This is pinned by a test that counts dispatches across a synthetic
press-move×N-release and asserts exactly one.
- **Keys:** arrows nudge the selected region (Shift = 10 px) through the
  existing `keyPressEvent` path; `Delete`/`Backspace` dispatch
  `ExclusionsChanged` without the selected region; `Escape` clears the
  selection.
- **Hooks:** `_crop_event(rect)` returns `ExclusionsChanged(with index
  replaced)` in region mode and `CropChanged(rect)` otherwise; `_constrain`
  stays identity. In region mode the canvas calls `_crop_event` only on
  release, per the gesture contract. `ResultPlayerCanvas` never enters region
  mode, so it is inert there.
- **Leaving the mode** clears the selection, restores the crop handles and
  keeps painting the regions.
- **Accessibility:** in region mode the description is
  `translate("CropCanvas", "Exclusion region %1 of %2: x %3, y %4, width %5, height %6 source pixels")`.
- Numeric fields for a region: not in this landing (see *Not in scope*).

**New user-visible strings** (all literals, all with German entries):
`"Exclusions"` (form label, via `inspector_label` in `ui/copy.py:632-654`),
`"Edit regions"`, `"Edit exclusion regions"` (accessible name), `"Clear"`
(exists), `"Clear exclusion regions"` (accessible name), `"%n region(s)"`,
the canvas description above, `"Exclude this area"`, `"Remove this region"`,
and `"Remove all regions"` (context-menu actions).

**README and screenshots.** `scripts/screenshots.py:_state` (`:120`) gets one
region in its `ParameterState` so `main-window.png` shows a rectangle on the
Original canvas; README gains a short paragraph in the crop/cleanup
description; regeneration per `README.md:150-159`.

## Not in scope

- Numeric x/y/w/h fields per region, region naming, a region list widget.
- A UI channel for `notes` (the trim-skipped message reaches the log).
- Reset of regions on source change. They are carried and **clipped**
  (Decision 2b).
- A cap on the number of regions.
- Merging overlapping regions into a canonical cover (Decision 4, false miss 2).
- Caching an empty union (Decision 4, false miss 3).
- Retaining the unmasked preview cut so a region change re-frames without the
  model (*Constraints*).
- Touch hit-testing beyond what `InteractionGeometry.hit_test(touch=…)`
  already gives the selected region.
- Anything in `core/geometry.py`, `core/state.py`.

## Failure modes considered

- **Region entirely inside the crop's excluded margin** — no box; the reducer
  no-ops on unchanged effective boxes, so nothing stales; the rectangle is
  still painted, and the pixels under it are gone anyway because the crop
  removed them.
- **Carried region partly outside a smaller new source** — clipped on load to
  its intersection, so what is painted is what is masked.
- **Carried region entirely outside a smaller new source** — dropped on load.
- **Anamorphic source** — correct only after the Decision 2a prerequisite
  lands; regions must not be implemented before it.
- **A drag across 200 pointer samples** — one dispatch, one facts worker.
- **Corrupt or mismatched stored frames while regions exist** — still fails
  loudly; only a genuinely empty mask degrades (Decision 5).
- **Same regions, different order** — identical fingerprint and bytes
  (`cut_exclusions` sorts and de-duplicates).
- **Regions during a running job** — `ExclusionsChanged` goes through the
  existing `can_edit` gate (`parameters.py:181-182`); the controls disable
  and region mode is left.
- **Persisted string malformed** — `()`, per-key fallback.
- **Region mode with no source** — controls disabled; `set_exclusion_edit`
  with `presentation is None` paints nothing.
- **External edit of a stored frame** — the union cache is already dropped
  (`_cut_ops.py:380`); exclusions are applied on read, so edited pixels inside
  a box are zeroed like any other.
- **`render.py` growth** — see the change list. The first draft's "≈0" was a
  guess; this revision budgets +5…+15 and makes measuring it a gate in the
  implementation order, not a hope.

## File-by-file change list

Line deltas are estimates; **bold** = frozen in the baseline.

**Prerequisite, lands separately** (Decision 2a — its own issue, branch and
`fix:` commit, before any of the below):

| File | Now | Δ | Notes |
|---|---|---|---|
| `src/matteloop/core/crop.py` | 291 | ~0 | `_orientation_transform` viewport: oriented display size for every rotation |
| `tests/core/test_crop.py` | — | +25 | rotation × PAR matrix; the current test is one passing cell of it |

**This feature:**

| File | Now | Δ | Notes |
|---|---|---|---|
| `src/matteloop/core/exclusion.py` | new | +95 | `cut_exclusions`, `exclude_alpha`, `union_alpha_bounds_or_none`, docstring enumerating the three producers and six sites |
| `src/matteloop/core/specs.py` | 697 | +14 | `FramingSpec.exclusions` + validation |
| `src/matteloop/core/fingerprints.py` | 465 | +25 | `cut_union_fingerprint`, `_framing(request)` with the non-empty rule |
| `src/matteloop/core/parameters.py` | 553 | +60 | field, `ExclusionsChanged`, reducer comparing **effective** boxes, `clip_exclusions`, reset, settings parse |
| **`src/matteloop/core/state.py`** | 882 | +1 | `clip_exclusions` on `SourceLoaded`. **Flag: frozen; needs `--update` with agreement** |
| `src/matteloop/jobs/transform_stage.py` | 177 | +30 | `cached_union`, degrade in `framing_plan`, `exclusions` through `stage_encoder_frames`/`_stage_frame` |
| **`src/matteloop/jobs/render.py`** | 2886 | **+5 … +15** | sites 1–4 (site 2 is a reorder, not an addition); `_matching_union` and `_rebuild_union` collapse onto `cached_union`; `union_alpha_bounds_or_none` at site 3. **Flag: frozen, and the first draft's "≈0" was not a safe estimate. Measure at step 3 and ask before touching the baseline.** |
| `src/matteloop/ui/preferences.py` | 91 | +6 | key + persist |
| `src/matteloop/ui/request_builder.py` | 112 | +1 | pass `exclusions` |
| `src/matteloop/ui/parameter_presentation.py` | 85 | +2 | field + mapping |
| `src/matteloop/ui/crop_presentation.py` | 63 | +3 | field + mapping |
| `src/matteloop/ui/transform_stage.py` | 721 | **+45** | `_crop_from_manifest`, `cached_union`, `union_alpha_bounds_or_none` branch, boxes through `_iter_stored_frames` and `FrameLoadWorker` — lands at **~766 of 800**. **Flag: tight. If it crosses, split `_CutFactsWorker` into a sibling module rather than raising the budget.** |
| `src/matteloop/ui/result_player.py` | 544 | +8 | boxes through `FrameLoadWorker`/`_load_one_frame` |
| `src/matteloop/ui/crop_canvas.py` | 359 | **+185** | mode, painting, rubber band, select/move/delete, gesture-local state and the release commit, announcement → **~544**. Over half the budget in one file; if it passes ~600, split the region mode into `ui/exclusion_canvas.py` as a `CropCanvas` subclass, the way `ResultPlayerCanvas` already is |
| `src/matteloop/ui/exclusion_controls.py` | new | +80 | the three controls, `apply`, `tab_widgets` |
| **`src/matteloop/ui/inspector.py`** | 897 | +5 | construct, add row, apply, tab order. **Flag: frozen; needs `--update` with agreement** |
| `src/matteloop/ui/main_window.py` | 351 | +1 | connect `edit_toggled`. **Not a baseline question** — the ratchet applies only above 800 lines (`scripts/check_guardrails.py:124-126`) |
| `src/matteloop/ui/copy.py` | 677 | +1 | `"Exclusions"` in `inspector_label` |
| `resources/matteloop_{en,de}.ts`, `.qm` | — | regen | `pyside6-lupdate`/`lrelease` per `docs/building.md:105-111`; German entries by hand |
| `scripts/screenshots.py` | — | +3 | one demo region |
| `README.md`, `assets/screenshots/main-window.png` | — | +6, regen | |
| `docs/v1-scope.md` | — | +4 | **first**, not last (G8) |
| `scripts/guardrails-baseline.json` | — | 2 entries | `core/state.py`, `ui/inspector.py`; only after agreement |

**Two baseline questions for the maintainer, both +1 … +5 lines:**
`core/state.py` (the clip that makes painted == masked a property) and
`ui/inspector.py` (hosting the controls row). `jobs/render.py` is a third only
if it measures positive after the collapse.

## Test plan

Named after behaviour (G7). "Fails without" names the missing piece.

**Cheap, Qt-free**

| Test | Fails without |
|---|---|
| `tests/core/test_crop.py::test_oriented_rect_round_trips_at_every_rotation_and_pixel_aspect` — rotation {0,90,180,270} × PAR {1, 2, 1.5, 0.75}, asserting the painted rect equals the oriented rect's position in `content_rect` | **Decision 2a. Fails on `main` today in 6 of 16 cells.** Belongs to the prerequisite commit |
| `tests/core/test_exclusion.py::test_cut_exclusions_translates_by_the_crop_origin_and_clips_to_the_crop` | the mapping |
| `…::test_cut_exclusions_drops_a_region_outside_the_crop` | the empty guard |
| `…::test_cut_exclusions_is_order_independent_and_deduplicated` | sorting/set |
| `…::test_exclude_alpha_zeroes_rgba_inside_each_box_and_nothing_else` | `exclude_alpha` |
| `tests/core/test_specs.py::test_framing_spec_rejects_exclusions_that_are_not_crop_specs` | validation |
| `tests/core/test_fingerprints.py::test_render_and_preview_identity_track_effective_exclusions_only` | `_framing(request)`; a region outside the crop must **not** change it |
| `…::test_union_identity_tracks_effective_exclusions` | `cut_union_fingerprint` |
| `…::test_identities_without_exclusions_match_the_previous_schema` — asserts the canonical payload has no `"exclusions"` key when empty | the non-empty rule; failing it orphans every v0.4.1 union cache |
| `tests/core/test_parameters.py::test_exclusions_changed_invalidates_the_preview_as_crop_cleanup` | the reducer |
| `…::test_moving_a_region_outside_the_crop_does_not_invalidate_the_preview` | the effective-box comparison (Decision 4, false miss 1) |
| `…::test_source_load_clips_carried_regions_and_drops_those_outside` | `clip_exclusions` (Decision 2b) |
| `tests/core/test_state.py::test_loading_a_smaller_source_leaves_every_region_inside_the_frame` | the `SourceLoaded` call site — the invariant painted == masked rests on |
| `…::test_parameters_reset_clears_exclusions` | reset |
| `…::test_exclusions_settings_string_round_trips_and_a_malformed_entry_falls_back_to_none` | parse |

**Jobs, existing fakes in `tests/jobs/render_support.py`**

| Test | Fails without |
|---|---|
| `tests/jobs/test_preview.py::test_preview_zeroes_excluded_alpha_in_the_cut_image_and_its_bounds` | site 1 |
| `tests/jobs/test_render.py::test_render_stores_unexcluded_cuts_but_unions_and_encodes_without_the_region` — stored PNG keeps alpha; output frame alpha 0 in the box; `union_metadata.bounds` excludes it | sites 2 and 4, **and the stage-before-mask order at site 2**: masking first would write masked PNGs and the assertion on the stored file fails |
| `…::test_render_peak_rgba_owner_count_is_unchanged_by_exclusions` | the no-copy order; a `cut.copy()` at site 2 raises the reported peak |
| `tests/jobs/test_rebuild.py::test_rebuild_with_a_region_misses_the_cached_union_and_recomputes_it` | site 3 + fingerprint |
| `…::test_rebuild_without_regions_reuses_union_metadata_written_before_exclusions_existed` — metadata hand-built with the v0.4.1 payload | the non-empty rule |
| `tests/jobs/test_transform_stage.py::test_framing_plan_skips_trim_without_a_union` (replaces `:34`) | Decision 5 |
| `…::test_stage_encoder_frames_zeroes_excluded_boxes_before_framing` | site 4 |
| `tests/jobs/test_render.py::test_render_with_trim_and_an_empty_mask_encodes_the_untrimmed_canvas_and_notes_it` (replaces the abort half of `:1525-1552`) | Decision 5 |
| `…::test_render_still_refuses_a_trimmed_box_below_the_minimum_dimension` — the `visible_size=10` branch at `:1522`, kept verbatim | that this degradation did **not** swallow the minimum-dimension refusal |
| `tests/core/test_exclusion.py::test_union_alpha_bounds_or_none_returns_none_for_an_empty_mask` | the empty signal |
| `…::test_union_alpha_bounds_or_none_still_raises_for_mismatched_canvases_and_non_rgba_frames` | **review finding 4** — a blanket `INVALID_FRAMING` catch passes the first of these two and fails this one |

**Qt, offscreen**

| Test | Fails without |
|---|---|
| `tests/ui/test_transform_stage.py::test_facts_ignore_cached_union_metadata_whose_fingerprint_does_not_match` | `cached_union` in the UI — **fails on `main` today** |
| `…::test_region_change_recomputes_facts_and_reloads_player_frames_without_the_region` | sites 5 and 6 |
| `…::test_empty_union_degrades_to_the_untrimmed_framed_size` | the `None` branch in `_resolve_union` |
| `…::test_mismatched_stored_frames_still_fail_the_facts_worker` | **review finding 4** — passes on a blanket catch, which is the point |
| `tests/ui/test_crop_canvas.py::test_a_region_drag_dispatches_exactly_once_on_release` — press, 20 moves, release; count dispatches | **review finding 3**; a per-move dispatch scores 21 |
| `…::test_an_abandoned_rubber_band_dispatches_nothing` | the release guard |
| `tests/ui/test_crop_canvas.py::test_region_mode_rubber_band_adds_a_region_on_release` | rubber band |
| `…::test_region_mode_discards_a_rubber_band_below_eight_pixels` | the threshold |
| `…::test_region_mode_gives_the_crop_no_hit_targets` — press on a crop handle under a region starts nothing for the crop | the mode switch |
| `…::test_click_selects_a_region_and_the_same_drag_moves_it` | press rule 2 |
| `…::test_delete_removes_the_selected_region` | keys |
| `…::test_leaving_region_mode_restores_crop_handles_and_keeps_painting_regions` | mode exit |
| `…::test_region_mode_announces_the_selected_region_bounds` | accessibility |
| `tests/ui/test_parameter_inspector.py::test_exclusion_controls_toggle_edit_mode_and_clear_regions` | controls |
| `tests/ui/test_parameter_persistence.py::test_exclusions_persist_as_one_settings_string` | preferences |
| `tests/ui/test_parameter_requests.py` (+1 assertion) | request builder |
| `tests/test_translations.py::test_catalogues_match_lupdate_extraction` (existing) | German entries |

Screenshot regeneration and a real render on the #80 clip are manual.

## Implementation order

**0. `docs/v1-scope.md` first.** G8 (`docs/engineering-guardrails.md:244`)
makes that file authoritative and forbids implementing an out-of-scope design
requirement, so the scope entry is step zero, not a tidy-up at the end. Nothing
below starts until it is recorded and the two baseline questions
(`core/state.py`, `ui/inspector.py`) have the maintainer's answer.

**1. The prerequisite, on its own branch and its own issue.** The
`_orientation_transform` viewport fix in `core/crop.py` plus the rotation × PAR
matrix (Decision 2a). It is a `fix:` with a `Trigger:` line naming the
anamorphic repro, it stands alone, and it must not be mixed into a feature
commit.

Then, on this branch:

2. `core/exclusion.py`, `specs.py`, `fingerprints.py` + their tests.
3. `jobs/transform_stage.py` (`cached_union`, degrade, encoder boxes) +
   `render.py` sites 1–4 + jobs tests. **Measure `render.py`'s net delta here
   and stop for the maintainer if it is positive.**
4. `parameters.py` (including `clip_exclusions` and the effective-box
   comparison), the `core/state.py` line, `preferences.py`,
   `request_builder.py`, `parameter_presentation.py`, `crop_presentation.py`
   + tests.
5. `ui/transform_stage.py`, `result_player.py` (sites 5, 6) + tests. **Check
   the line count against the 800 budget here**, and split rather than raise
   it.
6. `crop_canvas.py` region mode and the gesture contract + tests. **Check the
   line count here too.**
7. `exclusion_controls.py`, the five inspector lines, the main-window line,
   `copy.py`, catalogues.
8. Screenshots, README.

Each step is verified with the standard command from `CLAUDE.md`.

## Rejected alternatives

- **Regions in cut space.** Moving the crop would drag the overlay mask with
  it; the overlay is fixed in the source frame.
- **A pre-segmentation mask.** Settled by the maintainer: re-segments on every
  drag (crop is in the cut key) and is model-dependent.
- **Zeroing inside `read_cut` / `FrameReader.read`.** Would corrupt the
  identity consumers (hash scans, external-edit detection, CAS) that share the
  readers and must see the stored bytes.
- **Exclusions on `FramingPlan`, applied inside `apply_framing`.** Covers the
  two apply sites but none of the four measure sites, and grows the frozen
  `geometry.py`. The seam would be no narrower.
- **Baking exclusions into the stored cut frames.** Makes the cut set depend
  on framing; a region change would re-segment or orphan the set.
- **Always emitting `"exclusions": []` in fingerprints.** Cleaner JSON, but it
  turns every existing union cache and every existing preview/render
  fingerprint into a miss on upgrade for users who never draw a region.
- **A new manifest key for the exclusions.** The parser refuses unknown keys
  (`_manifest.py:361-365`) and the workspace modules are frozen; the
  fingerprint field already carries the identity.
- **Raw (unclipped) regions in the fingerprint.** A region outside the crop
  would stale the preview and the render for a pixel-identical result.
- **An `ExclusionRegion` type.** `CropSpec` is the same rectangle with the
  same validation; a second type would need its own drag/nudge/clamp helpers.
- **Extending `build_crop_geometry`'s priority to N regions.** N×9 targets, an
  overlap policy, and growth in frozen `geometry.py`; the mode switch makes
  the overlap question disappear.
- **Reducer state for the edit mode.** `AppState` is in frozen `state.py`;
  `TransformGroup.crop_edit_toggled` already shows UI-local mode works.
- **Emitting the rubber band live.** Would put half-drawn regions through the
  reducer and stale the preview per mouse move; a second dispatch would be
  needed to discard a tiny one. One dispatch on release is smaller.
- **Resetting regions on source load.** Loses the case the issue describes —
  the same stream layout across clips. Clipping costs the same one line in
  `state.py` and keeps it.
- **A new oriented→widget mapping just for regions** (the review's
  prescription for finding 1). It would leave the crop mispainted on
  anamorphic sources, and put two transforms where the codebase has one. The
  root cause is one wrong expression; fix that instead.
- **Folding the `core/crop.py` fix into this feature.** It has its own trigger
  and its own repro and predates this work; a reviewer must be able to see it
  alone. Scoped out as a prerequisite (Decision 2a).
- **Clipping carried regions only for display**, leaving the spec unclipped.
  Avoids the frozen `state.py` line, but the first drag of a clipped region
  would snap it to the clipped size — a visible jump — and the fingerprint
  would hash a rectangle the user cannot see.
- **Using `clamp_crop` to fit carried regions.** It slides to preserve size
  (deliberately, for issue #25); sliding an exclusion moves it onto wanted
  content.
- **Catching `INVALID_FRAMING` to detect an empty mask** (the first draft).
  The code also means non-RGBA, mismatched canvases and a plan mismatch, so
  the catch would present a corrupt cut set as a working one (Decision 5).
- **Masking a `cut.copy()` in the render loop** (the first draft). Staging
  before masking removes the copy, the extra full-resolution allocation and
  the ownership-tracker question together.
- **Dispatching region drags live, like the crop.** Measured cost: one QThread
  and ~12 QSettings writes per pointer sample, on a path with no debounce
  (Decision 6).
- **Merging overlapping regions into a canonical cover.** Would close false
  miss 2, but it is real geometry code for a cache miss nobody has measured.
- **Retaining the unmasked preview cut to re-frame locally.** Would make a
  region change free for the preview, but it is a preview-caching feature in
  its own right and touches frozen `state.py` far more than one line.
- **Keeping the empty-union refusal.** Zero lines, and the user can fix it —
  but the untrimmed canvas is exactly what "exclude everything" means, and
  the render must not abort for it. Recorded as the fork; degrade chosen.
- **A region count cap.** Nothing measured needs one; clipping bounds the
  cost per region to one `paste`.
