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


def test_blank_model_input_fills_each_box_and_nothing_else() -> None:
    from matteloop.core.exclusion import blank_model_input

    image = Image.new("RGBA", (8, 6))
    image.putdata(
        tuple((x, y, x + y, 20 + x * 8 + y) for y in range(6) for x in range(8))
    )
    original = image.copy()
    boxes = (PixelBounds(1, 1, 3, 3), PixelBounds(5, 0, 7, 2))

    blank_model_input(image, boxes)

    for y in range(6):
        for x in range(8):
            inside = (1 <= x < 3 and 1 <= y < 3) or (5 <= x < 7 and y < 2)
            expected = (
                (124, 116, 104, original.getpixel((x, y))[3])
                if inside
                else original.getpixel((x, y))
            )
            assert image.getpixel((x, y)) == expected


def test_blank_model_input_with_no_boxes_leaves_the_image_byte_identical() -> None:
    from matteloop.core.exclusion import blank_model_input

    image = Image.new("RGBA", (4, 3), (10, 20, 30, 40))
    before = image.tobytes()

    blank_model_input(image, ())

    assert image.tobytes() == before


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
