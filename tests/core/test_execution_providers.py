from __future__ import annotations

import importlib
import importlib.util
import logging
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

import matteloop
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


def test_importing_matteloop_disables_onnxruntime_telemetry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ORT_DISABLE_TELEMETRY", raising=False)

    importlib.reload(matteloop)

    assert os.environ["ORT_DISABLE_TELEMETRY"] == "1"


def test_importing_matteloop_overrides_an_inherited_telemetry_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ORT_DISABLE_TELEMETRY", "0")

    importlib.reload(matteloop)

    assert os.environ["ORT_DISABLE_TELEMETRY"] == "1"


def test_onnxruntime_loader_applies_secondary_telemetry_switch(
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


def test_onnxruntime_loader_reports_missing_secondary_telemetry_switch(
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

    assert "secondary telemetry switch unavailable" in caplog.text


# ONNX Runtime independently latches full telemetry suppression whenever any well-known
# CI/build-pipeline variable is set (onnxruntime/core/platform/telemetry_environment.h,
# IsRunningInCI()/IsRunningUnitTests()), regardless of ORT_DISABLE_TELEMETRY. A child
# process that inherits the ambient CI environment this test runs under would suppress
# telemetry either way and make the control/treatment runs indistinguishable, so both
# runs below scrub these.
_CI_ENVIRONMENT_VARIABLES = (
    "CI",
    "TF_BUILD",
    "GITHUB_ACTIONS",
    "GITLAB_CI",
    "CIRCLECI",
    "TRAVIS",
    "JENKINS_URL",
    "CODEBUILD_BUILD_ID",
    "BUILDKITE",
    "TEAMCITY_VERSION",
    "APPVEYOR",
    "BITBUCKET_BUILD_NUMBER",
    "SYSTEM_TEAMFOUNDATIONCOLLECTIONURI",
    "ORT_RUNNING_UNIT_TESTS",
)

_IMPORT_ONNXRUNTIME_SCRIPT = "import onnxruntime\n"


def _telemetry_store_path(home: Path) -> Path:
    """Where PosixTelemetry writes its device-id/telemetry cache for a given $HOME.

    Mirrors DeviceId::GetStorageDirectory(): macOS ignores XDG_CACHE_HOME and uses
    "$HOME/Library/Application Support"; everywhere else (as configured below, via
    XDG_CACHE_HOME) it is "$HOME/.cache".
    """
    if sys.platform == "darwin":
        base = home / "Library" / "Application Support"
    else:
        base = home / ".cache"
    return base / "Microsoft" / "DeveloperTools" / ".onnxruntime" / "onnxruntime.db"


def _import_onnxruntime_with(*, disable_telemetry: bool, home: Path) -> None:
    """Import onnxruntime in a scrubbed subprocess rooted at a throwaway $HOME."""
    home.mkdir()
    environment = os.environ.copy()
    for name in _CI_ENVIRONMENT_VARIABLES:
        environment.pop(name, None)
    if disable_telemetry:
        environment["ORT_DISABLE_TELEMETRY"] = "1"
    else:
        environment.pop("ORT_DISABLE_TELEMETRY", None)
    environment["HOME"] = str(home)
    environment["XDG_CACHE_HOME"] = str(home / ".cache")
    completed = subprocess.run(
        [sys.executable, "-I", "-c", _IMPORT_ONNXRUNTIME_SCRIPT],
        capture_output=True,
        check=False,
        env=environment,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="ORT_DISABLE_TELEMETRY is POSIX-only; Windows telemetry uses ETW",
)
def test_the_switch_keeps_the_runtime_from_creating_a_telemetry_store(
    tmp_path: Path,
) -> None:
    if importlib.util.find_spec("onnxruntime") is None:
        pytest.skip("onnxruntime is not installed")

    without_switch_home = tmp_path / "without-switch"
    _import_onnxruntime_with(disable_telemetry=False, home=without_switch_home)
    baseline_store = _telemetry_store_path(without_switch_home)
    assert baseline_store.exists(), (
        "control run created no telemetry store, so this machine cannot exercise "
        "the mechanism under test; strengthen the setup rather than the environment"
    )

    with_switch_home = tmp_path / "with-switch"
    _import_onnxruntime_with(disable_telemetry=True, home=with_switch_home)
    assert not _telemetry_store_path(with_switch_home).exists()


def test_importing_matteloop_disables_runtime_telemetry_before_any_import() -> None:
    script = """
    import os
    import sys

    import matteloop

    assert "onnxruntime" not in sys.modules
    print(os.environ["ORT_DISABLE_TELEMETRY"])
    """
    environment = os.environ.copy()
    environment.pop("ORT_DISABLE_TELEMETRY", None)
    completed = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)],
        capture_output=True,
        check=False,
        env=environment,
        text=True,
    )

    assert completed.returncode == 0, (
        f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    )
    assert completed.stdout.strip().splitlines()[-1] == "1"


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
