from __future__ import annotations

import json
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import pytest

from matteloop.core.errors import ErrorCode, ValidationError
from matteloop.core.request_json import (
    FORMAT,
    VERSION,
    render_settings_from_json,
    render_settings_to_json,
)
from matteloop.core.specs import (
    AlphaMattingSpec,
    CollisionPolicy,
    CropSpec,
    EdgeMode,
    FramingSpec,
    MismatchMode,
    OutputSpec,
    RenderRequest,
    ResizeSpec,
    SamplingSpec,
    SegmentationSpec,
    TransformSpec,
)


def _request(tmp_path: Path, **overrides: object) -> RenderRequest:
    defaults: dict[str, object] = {
        "source": tmp_path / "clip.mp4",
        "sampling": SamplingSpec(
            start=Fraction(7, 1001), end=Fraction(1001, 30000), fps=24
        ),
        "crop": CropSpec(x=4, y=8, width=640, height=360),
        "segmentation": SegmentationSpec(
            model_id="birefnet-general",
            edge_mode=EdgeMode.ALPHA_MATTING,
            alpha_matting=AlphaMattingSpec(
                foreground_threshold=200,
                background_threshold=20,
                erode_size=5,
            ),
            execution_provider="CPUExecutionProvider",
        ),
        "framing": FramingSpec(
            trim=True,
            alpha_threshold=Decimal("12.5"),
            padding=3,
            stretch_x=Decimal("1.25"),
        ),
        "output": OutputSpec(
            directory=tmp_path / "out",
            filename="original.webp",
            collision_policy=CollisionPolicy.REPLACE,
        ),
        "transform": TransformSpec(first_frame=2, last_frame=9),
        "rebuild": True,
    }
    defaults.update(overrides)
    return RenderRequest(**defaults)  # type: ignore[arg-type]


def _rebuild(text: str, tmp_path: Path, **overrides: object) -> RenderRequest:
    kwargs: dict[str, object] = {
        "directory": tmp_path / "elsewhere",
        "filename": "new.webp",
    }
    kwargs.update(overrides)
    return render_settings_from_json(text, **kwargs)  # type: ignore[arg-type]


def test_round_trip_preserves_sampling_crop_segmentation_and_framing(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)

    text = render_settings_to_json(request)
    rebuilt = _rebuild(text, tmp_path)

    assert rebuilt.source == request.source
    assert rebuilt.sampling == request.sampling
    assert rebuilt.crop == request.crop
    assert rebuilt.segmentation == request.segmentation
    assert rebuilt.framing == request.framing


def test_round_trip_preserves_a_non_identity_transform(tmp_path: Path) -> None:
    """Trim, crop, and resize all survive the round trip (issue #170 review)."""
    request = _request(
        tmp_path,
        transform=TransformSpec(
            first_frame=3,
            last_frame=40,
            crop=CropSpec(x=10, y=20, width=200, height=150),
            resize=ResizeSpec(width=480, height=None, mismatch=MismatchMode.COVER),
        ),
    )

    rebuilt = _rebuild(render_settings_to_json(request), tmp_path)

    assert rebuilt.transform == request.transform


def test_round_trip_preserves_a_default_identity_transform(tmp_path: Path) -> None:
    request = _request(tmp_path, transform=TransformSpec())

    rebuilt = _rebuild(render_settings_to_json(request), tmp_path)

    assert rebuilt.transform == TransformSpec()


def test_round_trip_preserves_the_output_size_budget(tmp_path: Path) -> None:
    request = _request(
        tmp_path,
        output=OutputSpec(
            directory=tmp_path / "out", filename="original.webp", max_bytes=5_000_000
        ),
    )

    rebuilt = _rebuild(
        render_settings_to_json(request),
        tmp_path,
        directory=tmp_path / "chosen",
        filename="chosen.webp",
    )

    assert rebuilt.output.max_bytes == 5_000_000
    assert rebuilt.output.directory == tmp_path / "chosen"
    assert rebuilt.output.filename == "chosen.webp"


def test_round_trip_preserves_no_size_budget(tmp_path: Path) -> None:
    request = _request(
        tmp_path,
        output=OutputSpec(
            directory=tmp_path / "out", filename="original.webp", max_bytes=None
        ),
    )

    rebuilt = _rebuild(render_settings_to_json(request), tmp_path)

    assert rebuilt.output.max_bytes is None


def test_round_trip_preserves_non_trivial_fraction_denominators(
    tmp_path: Path,
) -> None:
    request = _request(
        tmp_path,
        sampling=SamplingSpec(
            start=Fraction(1, 3000), end=Fraction(1001, 30000), fps=15
        ),
    )

    rebuilt = _rebuild(render_settings_to_json(request), tmp_path)

    assert rebuilt.sampling.start == Fraction(1, 3000)
    assert rebuilt.sampling.end == Fraction(1001, 30000)
    assert rebuilt.sampling.start.denominator == 3000
    assert rebuilt.sampling.end.denominator == 30000


def test_serialised_fractions_are_exact_numerator_denominator_pairs(
    tmp_path: Path,
) -> None:
    request = _request(
        tmp_path, sampling=SamplingSpec(start=Fraction(0), end=Fraction(1001, 30000))
    )

    payload = json.loads(render_settings_to_json(request))

    assert payload["sampling"]["end"] == {"numerator": 1001, "denominator": 30000}


def test_output_location_and_job_flags_are_not_present_in_the_json(
    tmp_path: Path,
) -> None:
    """The output location and rebuild/regenerate flags are the caller's call;
    transform and the size budget are settings, so they stay in the JSON."""
    request = _request(tmp_path)

    payload = json.loads(render_settings_to_json(request))

    assert "directory" not in payload["output"]
    assert "filename" not in payload["output"]
    assert "collision_policy" not in payload["output"]
    assert "rebuild" not in payload
    assert "regenerate" not in payload
    assert "transform" in payload
    assert "max_bytes" in payload["output"]


def test_rebuilt_request_uses_the_given_output_location_and_flags(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)

    rebuilt = _rebuild(
        render_settings_to_json(request),
        tmp_path,
        directory=tmp_path / "chosen",
        filename="chosen.webp",
        collision_policy=CollisionPolicy.REPLACE,
    )

    assert rebuilt.output.directory == tmp_path / "chosen"
    assert rebuilt.output.filename == "chosen.webp"
    assert rebuilt.output.collision_policy is CollisionPolicy.REPLACE
    assert rebuilt.rebuild is False
    assert rebuilt.regenerate is False


def test_envelope_carries_the_stable_format_and_version(tmp_path: Path) -> None:
    request = _request(tmp_path)

    payload = json.loads(render_settings_to_json(request))

    assert payload["format"] == FORMAT == "matteloop.render-settings"
    assert payload["version"] == VERSION == 1


def test_rejects_wrong_format(tmp_path: Path) -> None:
    text = json.dumps({"format": "something-else", "version": 1})

    with pytest.raises(ValidationError) as excinfo:
        _rebuild(text, tmp_path)

    assert excinfo.value.code is ErrorCode.RENDER_SETTINGS_INVALID


def test_rejects_unsupported_version(tmp_path: Path) -> None:
    request = _request(tmp_path)
    payload = json.loads(render_settings_to_json(request))
    payload["version"] = 2

    with pytest.raises(ValidationError) as excinfo:
        _rebuild(json.dumps(payload), tmp_path)

    assert excinfo.value.code is ErrorCode.RENDER_SETTINGS_INVALID


def test_rejects_malformed_json(tmp_path: Path) -> None:
    with pytest.raises(ValidationError) as excinfo:
        _rebuild("{not json", tmp_path)

    assert excinfo.value.code is ErrorCode.RENDER_SETTINGS_INVALID


def test_rejects_a_json_array_envelope(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        _rebuild("[]", tmp_path)


def test_rejects_missing_required_fields(tmp_path: Path) -> None:
    text = json.dumps({"format": FORMAT, "version": VERSION})

    with pytest.raises(ValidationError) as excinfo:
        _rebuild(text, tmp_path)

    assert excinfo.value.code is ErrorCode.RENDER_SETTINGS_INVALID


def test_rejects_a_missing_transform(tmp_path: Path) -> None:
    request = _request(tmp_path)
    payload = json.loads(render_settings_to_json(request))
    del payload["transform"]

    with pytest.raises(ValidationError) as excinfo:
        _rebuild(json.dumps(payload), tmp_path)

    assert excinfo.value.code is ErrorCode.RENDER_SETTINGS_INVALID


def test_rejects_a_missing_max_bytes(tmp_path: Path) -> None:
    request = _request(tmp_path)
    payload = json.loads(render_settings_to_json(request))
    del payload["output"]["max_bytes"]

    with pytest.raises(ValidationError) as excinfo:
        _rebuild(json.dumps(payload), tmp_path)

    assert excinfo.value.code is ErrorCode.RENDER_SETTINGS_INVALID


def test_rejects_a_zero_denominator_fraction(tmp_path: Path) -> None:
    request = _request(tmp_path)
    payload = json.loads(render_settings_to_json(request))
    payload["sampling"]["start"] = {"numerator": 0, "denominator": 0}

    with pytest.raises(ValidationError) as excinfo:
        _rebuild(json.dumps(payload), tmp_path)

    assert excinfo.value.code is ErrorCode.RENDER_SETTINGS_INVALID


def test_domain_validation_still_applies_to_rebuilt_specs(tmp_path: Path) -> None:
    """An in-envelope but domain-invalid crop still raises, via CropSpec itself."""
    request = _request(tmp_path)
    payload = json.loads(render_settings_to_json(request))
    payload["crop"]["width"] = -1

    with pytest.raises(ValidationError) as excinfo:
        _rebuild(json.dumps(payload), tmp_path)

    assert excinfo.value.code is ErrorCode.INVALID_CROP


def test_domain_validation_still_applies_to_the_rebuilt_transform(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path)
    payload = json.loads(render_settings_to_json(request))
    payload["transform"]["last_frame"] = 0
    payload["transform"]["first_frame"] = 5

    with pytest.raises(ValidationError) as excinfo:
        _rebuild(json.dumps(payload), tmp_path)

    assert excinfo.value.code is ErrorCode.INVALID_TRANSFORM
