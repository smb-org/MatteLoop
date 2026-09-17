"""Versioned JSON codec for exporting and re-importing render settings.

Deliberately excludes the output *location* (directory, filename) and the
job flags ``rebuild``/``regenerate``: those are the caller's decision (where
to write the file, whether to reuse cached frames), not part of the settings
a user wants to copy or replay elsewhere. ``render_settings_from_json``
always rebuilds with ``rebuild=regenerate=False``.

The transform (trim, crop, resize) and the output size budget
(``output.max_bytes``) *are* part of the settings a user is comparing --
they are not tied to a location -- so both round-trip exactly.
``render_settings_from_json`` applies the stored ``max_bytes`` onto the
caller-supplied directory and filename.

Fractions round-trip exactly as ``{"numerator", "denominator"}`` objects,
matching the convention already used for persisted fingerprints in
``core/fingerprints.py``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path

from matteloop.core.errors import ErrorCode, ValidationError
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

FORMAT = "matteloop.render-settings"
VERSION = 1

_MALFORMED_EXCEPTIONS = (
    KeyError,
    TypeError,
    ValueError,
    ZeroDivisionError,
    InvalidOperation,
)


def render_settings_to_json(request: RenderRequest) -> str:
    """Serialise everything about a request except its output location."""
    payload = {
        "format": FORMAT,
        "version": VERSION,
        "source": str(request.source),
        "sampling": _sampling_payload(request.sampling),
        "crop": _crop_payload(request.crop),
        "segmentation": _segmentation_payload(request.segmentation),
        "framing": _framing_payload(request.framing),
        "transform": _transform_payload(request.transform),
        "output": {"max_bytes": request.output.max_bytes},
    }
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def render_settings_from_json(
    text: str,
    *,
    directory: Path,
    filename: str,
    collision_policy: CollisionPolicy = CollisionPolicy.CANCEL,
) -> RenderRequest:
    """Rebuild a request from exported settings, at the given output location.

    ``directory`` and ``filename`` are the caller's decision, matching
    ``render_settings_to_json``'s exclusion of the output location; the
    exported ``output.max_bytes`` setting is applied on top of them. The
    rebuilt request always carries ``rebuild=regenerate=False``; the export
    never carried those.
    """
    payload = _decode_envelope(text)
    try:
        source = Path(_require_str(payload, "source"))
        sampling = _sampling_from_payload(_require_mapping(payload, "sampling"))
        crop = _crop_from_payload(_require_mapping(payload, "crop"))
        segmentation = _segmentation_from_payload(
            _require_mapping(payload, "segmentation")
        )
        framing = _framing_from_payload(_require_mapping(payload, "framing"))
        transform = _transform_from_payload(_require_mapping(payload, "transform"))
        max_bytes = _max_bytes_from_payload(_require_mapping(payload, "output"))
    except _MALFORMED_EXCEPTIONS as error:
        raise _invalid(f"render settings payload is malformed: {error}") from error
    output = OutputSpec(
        directory=directory,
        filename=filename,
        max_bytes=max_bytes,
        collision_policy=collision_policy,
    )
    return RenderRequest(
        source=source,
        sampling=sampling,
        crop=crop,
        segmentation=segmentation,
        framing=framing,
        output=output,
        transform=transform,
        rebuild=False,
        regenerate=False,
    )


def _decode_envelope(text: str) -> Mapping[str, object]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as error:
        raise _invalid(f"render settings are not valid JSON: {error}") from error
    if not isinstance(payload, Mapping):
        raise _invalid("render settings payload must be a JSON object")
    if payload.get("format") != FORMAT:
        raise _invalid(f"render settings format must be {FORMAT!r}")
    version = payload.get("version")
    if version != VERSION:
        raise _invalid(
            f"render settings version {version!r} is not supported; "
            f"expected {VERSION}"
        )
    return payload


def _fraction_payload(value: Fraction) -> dict[str, int]:
    return {"numerator": value.numerator, "denominator": value.denominator}


def _fraction_from_payload(payload: Mapping[str, object], key: str) -> Fraction:
    inner = _require_mapping(payload, key)
    numerator = _require_int(inner, "numerator")
    denominator = _require_int(inner, "denominator")
    return Fraction(numerator, denominator)


def _sampling_payload(sampling: SamplingSpec) -> dict[str, object]:
    return {
        "start": _fraction_payload(sampling.start),
        "end": _fraction_payload(sampling.end),
        "fps": sampling.fps,
    }


def _sampling_from_payload(payload: Mapping[str, object]) -> SamplingSpec:
    return SamplingSpec(
        _fraction_from_payload(payload, "start"),
        _fraction_from_payload(payload, "end"),
        _require_int(payload, "fps"),
    )


def _crop_payload(crop: CropSpec) -> dict[str, int]:
    return {"x": crop.x, "y": crop.y, "width": crop.width, "height": crop.height}


def _crop_from_payload(payload: Mapping[str, object]) -> CropSpec:
    return CropSpec(
        _require_int(payload, "x"),
        _require_int(payload, "y"),
        _require_int(payload, "width"),
        _require_int(payload, "height"),
    )


def _segmentation_payload(segmentation: SegmentationSpec) -> dict[str, object]:
    matting = segmentation.alpha_matting
    return {
        "model_id": segmentation.model_id,
        "edge_mode": segmentation.edge_mode.value,
        "alpha_matting": {
            "foreground_threshold": matting.foreground_threshold,
            "background_threshold": matting.background_threshold,
            "erode_size": matting.erode_size,
        },
        "execution_provider": segmentation.execution_provider,
    }


def _segmentation_from_payload(payload: Mapping[str, object]) -> SegmentationSpec:
    matting_payload = _require_mapping(payload, "alpha_matting")
    matting = AlphaMattingSpec(
        foreground_threshold=_require_int(matting_payload, "foreground_threshold"),
        background_threshold=_require_int(matting_payload, "background_threshold"),
        erode_size=_require_int(matting_payload, "erode_size"),
    )
    return SegmentationSpec(
        model_id=_require_str(payload, "model_id"),
        edge_mode=EdgeMode(_require_str(payload, "edge_mode")),
        alpha_matting=matting,
        execution_provider=_require_str(payload, "execution_provider"),
    )


def _framing_payload(framing: FramingSpec) -> dict[str, object]:
    return {
        "trim": framing.trim,
        "alpha_threshold": str(framing.alpha_threshold),
        "padding": framing.padding,
        "stretch_x": str(framing.stretch_x),
    }


def _framing_from_payload(payload: Mapping[str, object]) -> FramingSpec:
    return FramingSpec(
        trim=_require_bool(payload, "trim"),
        alpha_threshold=Decimal(_require_str(payload, "alpha_threshold")),
        padding=_require_int(payload, "padding"),
        stretch_x=Decimal(_require_str(payload, "stretch_x")),
    )


def _transform_payload(transform: TransformSpec) -> dict[str, object]:
    return {
        "first_frame": transform.first_frame,
        "last_frame": transform.last_frame,
        "crop": None if transform.crop is None else _crop_payload(transform.crop),
        "resize": (
            None if transform.resize is None else _resize_payload(transform.resize)
        ),
    }


def _transform_from_payload(payload: Mapping[str, object]) -> TransformSpec:
    crop_payload = payload.get("crop")
    resize_payload = payload.get("resize")
    return TransformSpec(
        first_frame=_require_int(payload, "first_frame"),
        last_frame=_require_int_or_none(payload, "last_frame"),
        crop=(
            None
            if crop_payload is None
            else _crop_from_payload(_require_mapping(payload, "crop"))
        ),
        resize=(
            None
            if resize_payload is None
            else _resize_from_payload(_require_mapping(payload, "resize"))
        ),
    )


def _resize_payload(resize: ResizeSpec) -> dict[str, object]:
    return {
        "width": resize.width,
        "height": resize.height,
        "mismatch": resize.mismatch.value,
    }


def _resize_from_payload(payload: Mapping[str, object]) -> ResizeSpec:
    return ResizeSpec(
        width=_require_int_or_none(payload, "width"),
        height=_require_int_or_none(payload, "height"),
        mismatch=MismatchMode(_require_str(payload, "mismatch")),
    )


def _max_bytes_from_payload(payload: Mapping[str, object]) -> int | None:
    return _require_int_or_none(payload, "max_bytes")


def _require_mapping(payload: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = payload.get(key)
    if not isinstance(value, Mapping):
        raise ValueError(f"{key} must be an object")
    return value


def _require_str(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    return value


def _require_int(payload: Mapping[str, object], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{key} must be an integer")
    return value


def _require_int_or_none(payload: Mapping[str, object], key: str) -> int | None:
    if key not in payload:
        raise ValueError(f"{key} is required")
    value = payload[key]
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{key} must be an integer or null")
    return value


def _require_bool(payload: Mapping[str, object], key: str) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be a boolean")
    return value


def _invalid(detail: str) -> ValidationError:
    return ValidationError(ErrorCode.RENDER_SETTINGS_INVALID, "request-json", detail)
