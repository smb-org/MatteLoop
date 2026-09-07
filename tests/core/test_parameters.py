from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import pytest

from matteloop.core.parameters import (
    V1_MODEL_IDS,
    AlphaThresholdChanged,
    EdgeModeChanged,
    ExecutionProviderChanged,
    GlobalTrimChanged,
    ModelChanged,
    OutputDirectoryChanged,
    OutputFilenameChanged,
    OutputFpsChanged,
    OutputMaxSizeChanged,
    PaddingChanged,
    ParametersReset,
    ParameterState,
    StretchChanged,
    TransformChanged,
    parameters_from_values,
)
from matteloop.core.specs import CropSpec, EdgeMode, TransformSpec
from matteloop.core.state import (
    AppState,
    PreviewInvalidationReason,
    PreviewRequested,
    PreviewResult,
    PreviewState,
    PreviewSucceeded,
    RenderRequested,
    SourceLoaded,
    SourceLoadRequested,
    reduce,
)
from matteloop.jobs.models.catalog import ModelCatalog


@dataclass(frozen=True)
class Metadata:
    path: Path = Path("source.mp4")
    width: int = 128
    height: int = 128
    duration: Fraction = Fraction(4)
    average_rate: Fraction = Fraction(30)


def _ready() -> AppState:
    loading = reduce(AppState(), SourceLoadRequested("source", "load"))
    return reduce(loading, SourceLoaded("source", "load", Metadata()))


def _current() -> AppState:
    running = reduce(_ready(), PreviewRequested("job", "preview"))
    return reduce(
        running,
        PreviewSucceeded("job", PreviewResult("source", "preview", "cutout")),
    )


def test_parameter_defaults_match_the_v1_mapping() -> None:
    parameters = ParameterState()

    assert parameters.model_id == "birefnet-portrait"
    assert parameters.edge_mode is EdgeMode.STANDARD
    assert parameters.fps == 15
    assert parameters.trim is False
    assert parameters.alpha_threshold == Decimal("2.0")
    assert parameters.padding == 0
    assert parameters.stretch_x == Decimal("1.0")
    assert parameters.max_mib == Decimal("0")


def test_each_enabled_model_id_resolves_in_the_authoritative_catalog() -> None:
    catalog = ModelCatalog.load_resource()

    enabled_ids = tuple(catalog.get(model_id).id for model_id in V1_MODEL_IDS)

    assert enabled_ids == (
        "u2net",
        "u2netp",
        "u2net_human_seg",
        "silueta",
        "isnet-general-use",
        "isnet-anime",
        "birefnet-general",
        "birefnet-general-lite",
        "birefnet-portrait",
        "birefnet-dis",
        "birefnet-hrsod",
        "birefnet-cod",
        "birefnet-massive",
    )


@pytest.mark.parametrize("model_id", ("bria-rmbg", "u2net_cloth_seg"))
def test_excluded_models_are_rejected_by_v1_parameters(model_id: str) -> None:
    with pytest.raises(ValueError, match="outside the V1 catalog"):
        ParameterState(model_id=model_id)


def test_segmentation_parameter_changes_stale_the_current_preview() -> None:
    changed = reduce(_current(), ModelChanged("u2net"))

    assert changed.parameters.model_id == "u2net"
    assert changed.preview is PreviewState.STALE
    assert changed.stale_category is PreviewInvalidationReason.SEGMENTATION
    assert changed.model_available is False

    changed_again = reduce(changed, EdgeModeChanged(EdgeMode.DECONTAMINATE_COLORS))
    assert changed_again.parameters.edge_mode is EdgeMode.DECONTAMINATE_COLORS
    assert changed_again.preview is PreviewState.STALE


def test_execution_provider_changes_stale_the_current_preview() -> None:
    changed = reduce(_current(), ExecutionProviderChanged("CUDAExecutionProvider"))

    assert changed.parameters.execution_provider == "CUDAExecutionProvider"
    assert changed.preview is PreviewState.STALE
    assert changed.stale_category is PreviewInvalidationReason.COMPUTE_ACCELERATION


def test_execution_provider_can_change_before_a_video_is_loaded() -> None:
    changed = reduce(AppState(), ExecutionProviderChanged("CUDAExecutionProvider"))

    assert changed.parameters.execution_provider == "CUDAExecutionProvider"


def test_execution_provider_change_is_ignored_while_a_job_runs() -> None:
    running = reduce(_ready(), RenderRequested("job", "request"))

    assert reduce(running, ExecutionProviderChanged("CUDAExecutionProvider")) is running


def test_cleanup_parameter_changes_stale_the_current_preview() -> None:
    state = _current()

    for event in (
        GlobalTrimChanged(True),
        PaddingChanged(8),
        StretchChanged(Decimal("1.25")),
    ):
        state = reduce(state, event)

    assert state.parameters.trim is True
    assert state.parameters.padding == 8
    assert state.parameters.stretch_x == Decimal("1.25")
    assert state.preview is PreviewState.STALE
    assert state.stale_category is PreviewInvalidationReason.CROP_CLEANUP


def test_output_fps_changes_sampling_and_stales_the_current_preview() -> None:
    changed = reduce(_current(), OutputFpsChanged(90))

    assert changed.parameters.fps == 90
    assert changed.timeline is not None
    assert changed.timeline.fps == 90
    assert changed.preview is PreviewState.STALE
    assert changed.stale_category is PreviewInvalidationReason.SAMPLING


def test_reset_restores_inspector_parameters_and_preserves_other_state() -> None:
    current = _current()
    assert current.timeline is not None
    parameters = ParameterState(
        model_id="u2net",
        edge_mode=EdgeMode.DECONTAMINATE_COLORS,
        execution_provider="CUDAExecutionProvider",
        fps=90,
        trim=True,
        alpha_threshold=Decimal("0.4"),
        padding=1,
        stretch_x=Decimal("1.2"),
        output_directory=Path("exports"),
        output_filename="chosen.webp",
        max_mib=Decimal("12.5"),
        transform=TransformSpec(first_frame=1, crop=CropSpec(4, 5, 80, 70)),
    )
    timeline = replace(
        current.timeline,
        start=Fraction(1, 2),
        end=Fraction(7, 2),
        playhead=Fraction(1),
        fps=90,
    )
    state = replace(
        current,
        parameters=parameters,
        timeline=timeline,
        crop=CropSpec(8, 9, 100, 90),
        crop_enabled=False,
    )

    reset = reduce(state, ParametersReset())
    defaults = ParameterState()

    for name in (
        "model_id",
        "edge_mode",
        "fps",
        "trim",
        "alpha_threshold",
        "padding",
        "stretch_x",
        "max_mib",
    ):
        assert getattr(reset.parameters, name) == getattr(defaults, name)
    assert reset.parameters.execution_provider == "CUDAExecutionProvider"
    assert reset.parameters.output_directory == Path("exports")
    assert reset.parameters.output_filename == "chosen.webp"
    assert reset.parameters.transform == parameters.transform
    assert reset.source_id == state.source_id
    assert reset.source_value is state.source_value
    assert reset.crop == state.crop
    assert reset.crop_enabled is state.crop_enabled
    assert reset.timeline is not None
    assert reset.timeline.start == state.timeline.start
    assert reset.timeline.end == state.timeline.end
    assert reset.timeline.playhead == state.timeline.playhead
    assert reset.timeline.fps == defaults.fps
    assert reset.preview is PreviewState.STALE
    assert reset.stale_category is PreviewInvalidationReason.CROP_CLEANUP


def test_reset_parameters_is_ignored_while_a_job_runs() -> None:
    running = reduce(_ready(), RenderRequested("job", "request"))

    assert reduce(running, ParametersReset()) is running


def test_output_parameter_changes_keep_a_current_preview_current() -> None:
    state = _current()

    state = reduce(state, OutputDirectoryChanged(Path("exports")))
    state = reduce(state, OutputFilenameChanged("result.webp"))
    state = reduce(state, OutputMaxSizeChanged(Decimal("1.5")))

    assert state.parameters.output_directory == Path("exports")
    assert state.parameters.output_filename == "result.webp"
    assert state.parameters.max_mib == Decimal("1.5")
    assert state.preview is PreviewState.CURRENT
    assert state.stale_category is None


def test_invalid_parameter_events_leave_reducer_state_unchanged() -> None:
    state = _current()

    for event in (
        AlphaThresholdChanged(Decimal("101")),
        PaddingChanged(-1),
        StretchChanged(Decimal("0")),
        OutputMaxSizeChanged(Decimal("-1")),
    ):
        assert reduce(state, event) is state


def test_transform_changed_replaces_transform_without_invalidating_preview() -> None:
    state = _current()
    transform = TransformSpec(first_frame=1, last_frame=3)

    changed = reduce(state, TransformChanged(transform))

    assert changed.parameters.transform == transform
    assert changed.preview is PreviewState.CURRENT
    assert changed.stale_category is None


def test_transform_changed_is_ignored_when_the_value_is_unchanged() -> None:
    state = _current()

    assert reduce(state, TransformChanged(TransformSpec())) is state


def test_transform_changed_is_ignored_while_a_job_runs() -> None:
    running = reduce(_ready(), RenderRequested("job", "req"))

    assert running.job.phase.value != "idle"
    assert (
        reduce(running, TransformChanged(TransformSpec(first_frame=2))) is running
    )


def test_output_directory_changed_is_ignored_while_a_job_runs() -> None:
    running = reduce(_ready(), RenderRequested("job", "req"))

    assert reduce(running, OutputDirectoryChanged(Path("exports"))) is running


def test_parameter_state_default_transform_is_identity() -> None:
    assert ParameterState().transform == TransformSpec()
    assert ParameterState().transform.is_identity


def test_parameters_from_values_yields_an_identity_transform() -> None:
    parameters = parameters_from_values({"fps": "60"})

    assert parameters.transform == TransformSpec()


def test_invalid_saved_values_fall_back_independently() -> None:
    parameters = parameters_from_values(
        {
            "model_id": "not-a-v1-model",
            "edge_mode": "not-an-edge-mode",
            "fps": "241",
            "trim": "not-a-bool",
            "alpha_threshold": "101",
            "padding": "-1",
            "stretch_x": "0",
            "max_mib": "NaN",
            "output_directory": "https://example.test/out",
            "output_filename": "not-valid.txt",
        }
    )

    assert parameters == ParameterState()


def test_unrelated_invalid_saved_value_does_not_reset_valid_preferences() -> None:
    parameters = parameters_from_values(
        {"fps": "60", "alpha_threshold": "not-a-number", "padding": "12"}
    )

    assert parameters.fps == 60
    assert parameters.alpha_threshold == Decimal("2.0")
    assert parameters.padding == 12
