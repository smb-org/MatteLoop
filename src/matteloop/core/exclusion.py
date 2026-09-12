"""Map and apply post-segmentation exclusion regions.

All cut pixels enter framing through three producers:

* ``_produce_cut_frame`` (sites 1, 2)
* ``WorkspacePort.read_cut`` (sites 3, 4)
* ``FrameReader.read`` (sites 5, 6)

The six consumers are:

1. preview (``render.py:913-960``): ``exclude_alpha(cut, boxes)`` once,
   right after ``_produce_cut_frame``; bounds, both ``apply_framing`` calls,
   and both ``_immutable_rgba`` images see it.
2. render loop (``render.py:1108-1112``): stage first, then mask in place.
3. ``_rebuild_union`` (``render.py:1338``): ``exclude_alpha(image, boxes)``
   after ``read_cut``.
4. encoder (``render.py:1386``): ``stage_encoder_frames`` passes boxes to
   ``_stage_frame``, which masks before ``apply_framing``.
5. UI facts (``ui/transform_stage.py:710-720``): ``_iter_stored_frames``
   masks before yielding.
6. result player (``ui/result_player.py:480-490``): ``FrameLoadWorker`` and
   ``_load_one_frame`` mask before ``apply_framing``.

``union_alpha_bounds_or_none`` keeps a genuinely empty alpha union distinct
from malformed or mismatched framing inputs.
"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal

from PIL import Image

from matteloop.core.errors import ErrorCode, ValidationError
from matteloop.core.geometry import PixelBounds, alpha_bounds
from matteloop.core.specs import CropSpec


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


def union_alpha_bounds_or_none(
    frames: Iterable[Image.Image], threshold_percent: Decimal | float | int
) -> PixelBounds | None:
    """Return a valid alpha union, or ``None`` only when it is empty."""
    try:
        iterator = iter(frames)
    except TypeError:
        raise ValidationError(
            ErrorCode.INVALID_FRAMING,
            "framing",
            "framing requires an iterable of RGBA frames",
        ) from None

    union: PixelBounds | None = None
    expected_size: tuple[int, int] | None = None
    for image in iterator:
        bounds = alpha_bounds(image, threshold_percent)
        if expected_size is None:
            expected_size = image.size
        elif image.size != expected_size:
            raise ValidationError(
                ErrorCode.INVALID_FRAMING,
                "framing",
                "all frames must have identical source canvas dimensions",
            )
        if bounds is None:
            continue
        if union is None:
            union = bounds
        else:
            union = PixelBounds(
                min(union.left, bounds.left),
                min(union.top, bounds.top),
                max(union.right, bounds.right),
                max(union.bottom, bounds.bottom),
            )
    return union
