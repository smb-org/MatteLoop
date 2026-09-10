from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

import matteloop.core.execution_providers as execution_providers
from matteloop.core.execution_providers import (
    COREML_EXECUTION_PROVIDER,
    CPU_EXECUTION_PROVIDER,
    CUDA_EXECUTION_PROVIDER,
    DML_EXECUTION_PROVIDER,
    MIGRAPHX_EXECUTION_PROVIDER,
    ROCM_EXECUTION_PROVIDER,
    provider_options,
    provider_options_from_runtime,
    select_provider,
)


def test_onnxruntime_loader_disables_telemetry_when_runtime_exposes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[bool] = []
    runtime = SimpleNamespace(disable_telemetry_events=lambda: calls.append(True))
    monkeypatch.setattr(
        execution_providers.importlib,
        "import_module",
        lambda name: runtime if name == "onnxruntime" else None,
    )

    assert execution_providers.load_onnxruntime() is runtime
    assert calls == [True]


def test_onnxruntime_loader_keeps_runtime_usable_without_telemetry_api(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    runtime = SimpleNamespace()
    monkeypatch.setattr(
        execution_providers.importlib,
        "import_module",
        lambda name: runtime if name == "onnxruntime" else None,
    )

    with caplog.at_level(logging.DEBUG, logger=execution_providers.__name__):
        assert execution_providers.load_onnxruntime() is runtime

    assert "disable_telemetry_events" in caplog.text


def test_provider_catalog_exposes_only_allowlisted_local_runtime_providers() -> None:
    options = provider_options(
        [
            "AzureExecutionProvider",
            "FutureExecutionProvider",
            CPU_EXECUTION_PROVIDER,
            COREML_EXECUTION_PROVIDER,
            CUDA_EXECUTION_PROVIDER,
            ROCM_EXECUTION_PROVIDER,
            MIGRAPHX_EXECUTION_PROVIDER,
            DML_EXECUTION_PROVIDER,
        ],
        system="Linux",
        machine="x86_64",
    )

    assert {option.provider for option in options} == {
        CPU_EXECUTION_PROVIDER,
        COREML_EXECUTION_PROVIDER,
        CUDA_EXECUTION_PROVIDER,
        ROCM_EXECUTION_PROVIDER,
        MIGRAPHX_EXECUTION_PROVIDER,
        DML_EXECUTION_PROVIDER,
    }


def test_apple_silicon_prefers_cpu_while_offering_coreml_as_experimental() -> None:
    options = provider_options(
        [CPU_EXECUTION_PROVIDER, COREML_EXECUTION_PROVIDER, "AzureExecutionProvider"],
        system="Darwin",
        machine="arm64",
        model_id="birefnet-portrait",
    )

    assert [option.provider for option in options] == [
        CPU_EXECUTION_PROVIDER,
        COREML_EXECUTION_PROVIDER,
    ]
    assert select_provider(None, options) == CPU_EXECUTION_PROVIDER
    assert options[0].label == "CPU – recommended"
    assert options[1].label == "Apple CoreML – experimental"
    assert options[1].recommended is False


def test_cuda_is_preselected_only_when_the_runtime_reports_cuda() -> None:
    with_cuda = provider_options(
        [CPU_EXECUTION_PROVIDER, CUDA_EXECUTION_PROVIDER],
        system="Linux",
        machine="x86_64",
        hardware="nvidia",
    )
    without_cuda = provider_options(
        [CPU_EXECUTION_PROVIDER],
        system="Linux",
        machine="x86_64",
        hardware="nvidia",
    )

    assert select_provider(None, with_cuda) == CUDA_EXECUTION_PROVIDER
    assert select_provider(None, without_cuda) == CPU_EXECUTION_PROVIDER


def test_windows_directml_is_preselected_without_cuda_or_amd_providers() -> None:
    directml_options = provider_options(
        [DML_EXECUTION_PROVIDER, CPU_EXECUTION_PROVIDER],
        system="Windows",
        machine="AMD64",
    )
    cuda_options = provider_options(
        [DML_EXECUTION_PROVIDER, CPU_EXECUTION_PROVIDER, CUDA_EXECUTION_PROVIDER],
        system="Windows",
        machine="AMD64",
    )

    assert select_provider(None, directml_options) == DML_EXECUTION_PROVIDER
    assert {option.provider for option in directml_options} == {
        DML_EXECUTION_PROVIDER,
        CPU_EXECUTION_PROVIDER,
    }
    assert directml_options[0].label == "GPU over DirectML – recommended"
    assert select_provider(None, cuda_options) == CUDA_EXECUTION_PROVIDER


def test_stored_provider_wins_and_unavailable_choice_uses_preselection() -> None:
    options = provider_options(
        [CPU_EXECUTION_PROVIDER, COREML_EXECUTION_PROVIDER],
        system="Darwin",
        machine="arm64",
    )

    assert (
        select_provider(COREML_EXECUTION_PROVIDER, options) == COREML_EXECUTION_PROVIDER
    )
    assert select_provider(CUDA_EXECUTION_PROVIDER, options) == CPU_EXECUTION_PROVIDER


def test_runtime_provider_options_degrade_to_empty_when_runtime_is_unusable() -> None:
    class BrokenRuntime:
        def get_available_providers(self) -> list[str]:
            raise AttributeError("get_available_providers is missing")

    options = provider_options_from_runtime(BrokenRuntime())

    assert options == ()
    assert select_provider("DmlExecutionProvider", options) == CPU_EXECUTION_PROVIDER
