from __future__ import annotations

from decimal import Decimal

import pytest
from PIL import Image

from matteloop.core.errors import ErrorCode, ValidationError
from matteloop.core.exclusion import (
    cut_exclusions,
    exclude_alpha,
    union_alpha_bounds_or_none,
)
from matteloop.core.geometry import PixelBounds
from matteloop.core.specs import CropSpec


def test_cut_exclusions_translates_by_the_crop_origin_and_clips_to_the_crop() -> None:
    crop = CropSpec(10, 20, 100, 80)
    exclusions = (
        CropSpec(0, 10, 30, 30),
        CropSpec(80, 60, 60, 60),
    )

    assert cut_exclusions(exclusions, crop) == (
        PixelBounds(0, 0, 20, 20),
        PixelBounds(70, 40, 100, 80),
    )


def test_cut_exclusions_drops_a_region_outside_the_crop() -> None:
    assert cut_exclusions((CropSpec(0, 0, 5, 5),), CropSpec(10, 10, 20, 20)) == ()


def test_cut_exclusions_is_order_independent_and_deduplicated() -> None:
    crop = CropSpec(10, 10, 100, 100)
    first = CropSpec(40, 50, 20, 30)
    second = CropSpec(20, 20, 10, 10)

    assert cut_exclusions((first, second, first), crop) == cut_exclusions(
        (second, first), crop
    )


def test_exclude_alpha_zeroes_rgba_inside_each_box_and_nothing_else() -> None:
    image = Image.new("RGBA", (6, 6), (10, 20, 30, 255))
    boxes = (PixelBounds(1, 1, 3, 3), PixelBounds(4, 0, 6, 2))

    exclude_alpha(image, boxes)

    for y in range(6):
        for x in range(6):
            excluded = (1 <= x < 3 and 1 <= y < 3) or (4 <= x < 6 and y < 2)
            assert image.getpixel((x, y)) == (
                (0, 0, 0, 0) if excluded else (10, 20, 30, 255)
            )


def test_union_alpha_bounds_or_none_returns_none_for_an_empty_mask() -> None:
    image = Image.new("RGBA", (8, 8), (20, 30, 40, 0))

    assert union_alpha_bounds_or_none((image,), Decimal("2")) is None


@pytest.mark.parametrize(
    "frames",
    [
        (Image.new("RGBA", (8, 8)), Image.new("RGBA", (9, 8))),
        (Image.new("RGB", (8, 8)),),
    ],
    ids=["mismatched-canvases", "non-rgba-frame"],
)
def test_union_alpha_bounds_or_none_propagates_invalid_frame_errors(
    frames: tuple[Image.Image, ...],
) -> None:
    with pytest.raises(ValidationError) as exc:
        union_alpha_bounds_or_none(frames, Decimal("2"))

    assert exc.value.code is ErrorCode.INVALID_FRAMING
