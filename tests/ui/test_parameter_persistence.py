from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

from PySide6.QtCore import QSettings
from PySide6.QtGui import QImage

from matteloop.core.execution_providers import (
    CPU_EXECUTION_PROVIDER,
    CUDA_EXECUTION_PROVIDER,
    ProviderOption,
)
from matteloop.core.parameters import (
    OutputFilenameChanged,
    ParametersReset,
    ParameterState,
)
from matteloop.core.specs import CropSpec, EdgeMode, TransformSpec
from matteloop.core.state import (
    AppState,
    SourceLoadRequested,
    SourceState,
    reduce,
)
from matteloop.ui.controller import LoadedSource, SourceController
from matteloop.ui.preferences import load_parameters, persist_parameters
from matteloop.ui.store import ReducerStore


def _settings() -> QSettings:
    settings = QSettings(
        QSettings.IniFormat,
        QSettings.UserScope,
        "matteloop-test",
        "parameter-persistence",
    )
    settings.clear()
    return settings


@dataclass(frozen=True)
class _SourceMetadata:
    width: int
    height: int
    duration: Fraction = Fraction(2)
    average_rate: Fraction = Fraction(30)


def test_parameter_preferences_round_trip_as_qsettings_primitives() -> None:
    settings = _settings()
    expected = ParameterState(
        model_id="isnet-general-use",
        edge_mode=EdgeMode.DECONTAMINATE_COLORS,
        execution_provider=CUDA_EXECUTION_PROVIDER,
        fps=90,
        trim=True,
        alpha_threshold=Decimal("3.5"),
        padding=9,
        stretch_x=Decimal("1.2"),
        exclusions=(CropSpec(8, 9, 40, 50), CropSpec(80, 20, 10, 15)),
        output_directory=Path("/exports"),
        output_filename="last.webp",
        max_mib=Decimal("2.5"),
    )

    persist_parameters(settings, expected)
    actual = load_parameters(
        settings,
        (
            ProviderOption(CPU_EXECUTION_PROVIDER, "CPU"),
            ProviderOption(CUDA_EXECUTION_PROVIDER, "NVIDIA CUDA"),
        ),
    )

    assert actual == expected


def test_exclusions_before_model_persists_as_a_bool() -> None:
    settings = _settings()
    expected = ParameterState(
        exclusions=(CropSpec(8, 9, 40, 50),),
        exclusions_before_model=True,
    )

    persist_parameters(settings, expected)

    assert settings.value("parameters/exclusions_before_model") is True
    assert load_parameters(settings).exclusions_before_model is True


def test_persist_parameters_writes_no_transform_key() -> None:
    settings = _settings()
    non_identity = ParameterState(
        transform=TransformSpec(first_frame=1, crop=CropSpec(0, 0, 4, 4))
    )

    persist_parameters(settings, non_identity)

    assert settings.value("parameters/transform") is None
    assert "transform" not in settings.allKeys()


def test_unavailable_saved_provider_falls_back_to_the_available_cpu_choice() -> None:
    settings = _settings()
    persist_parameters(
        settings,
        ParameterState(execution_provider="CUDAExecutionProvider"),
    )

    actual = load_parameters(
        settings,
        (ProviderOption(CPU_EXECUTION_PROVIDER, "CPU"),),
    )

    assert actual.execution_provider == CPU_EXECUTION_PROVIDER


def test_malformed_exclusions_preference_falls_back_to_empty_regions() -> None:
    settings = _settings()
    settings.setValue("parameters/model_id", "u2net")
    settings.setValue("parameters/exclusions", "8,9,40,50;malformed")

    class ReadingSettings:
        def __init__(self, source: QSettings) -> None:
            self.source = source
            self.keys: list[str] = []

        def value(self, key: str) -> object:
            self.keys.append(key)
            return self.source.value(key)

    reading = ReadingSettings(settings)
    actual = load_parameters(reading)  # type: ignore[arg-type]

    assert actual.model_id == "u2net"
    assert actual.exclusions == ()
    assert "parameters/exclusions" in reading.keys


def test_source_load_persists_clipped_exclusions_for_the_next_start() -> None:
    settings = _settings()
    saved = ParameterState(
        exclusions=(CropSpec(60, 70, 80, 20), CropSpec(120, 0, 10, 10))
    )
    persist_parameters(settings, saved)

    store = ReducerStore(
        reduce(
            AppState(parameters=load_parameters(settings)),
            SourceLoadRequested("smaller", "load-2"),
        )
    )
    controller = SourceController.__new__(SourceController)
    controller._store = store
    controller._settings = settings
    controller._closed = False

    controller._source_loaded(
        "smaller",
        "load-2",
        LoadedSource(
            _SourceMetadata(100, 80),
            QImage(100, 80, QImage.Format.Format_RGBA8888),
        ),
    )

    assert store.state.source is SourceState.READY
    assert store.state.parameters.exclusions == (CropSpec(60, 70, 40, 10),)
    restarted = AppState(parameters=load_parameters(settings))
    assert restarted.parameters.exclusions == (CropSpec(60, 70, 40, 10),)


def test_failed_provider_is_not_reintroduced_by_a_later_parameter_save() -> None:
    settings = _settings()
    store = ReducerStore(
        AppState(
            source=SourceState.READY,
            source_id="source",
            source_value=object(),
            parameters=ParameterState(execution_provider=CUDA_EXECUTION_PROVIDER),
        )
    )
    controller = SourceController.__new__(SourceController)
    controller._store = store
    controller._settings = settings
    controller._working_provider = CPU_EXECUTION_PROVIDER
    controller._failed_provider = CUDA_EXECUTION_PROVIDER

    controller._dispatch_parameter(OutputFilenameChanged("next.webp"))

    assert settings.value("parameters/execution_provider") == CPU_EXECUTION_PROVIDER


def test_reset_persists_inspector_defaults_without_overwriting_excluded_choices(
) -> None:
    settings = _settings()
    saved = ParameterState(
        model_id="u2net",
        edge_mode=EdgeMode.DECONTAMINATE_COLORS,
        execution_provider=CUDA_EXECUTION_PROVIDER,
        fps=90,
        trim=True,
        alpha_threshold=Decimal("0.4"),
        padding=1,
        stretch_x=Decimal("1.2"),
        output_directory=Path("exports"),
        output_filename="chosen.webp",
        max_mib=Decimal("12.5"),
    )
    persist_parameters(settings, saved)
    store = ReducerStore(
        AppState(
            source=SourceState.READY,
            source_id="source",
            source_value=object(),
            parameters=saved,
        )
    )
    controller = SourceController.__new__(SourceController)
    controller._store = store
    controller._settings = settings
    controller._working_provider = CUDA_EXECUTION_PROVIDER
    controller._failed_provider = None

    controller._dispatch_parameter(ParametersReset())
    actual = load_parameters(settings)
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
        assert getattr(actual, name) == getattr(defaults, name)
    assert actual.execution_provider == saved.execution_provider
    assert actual.output_directory == saved.output_directory
    assert actual.output_filename == saved.output_filename
