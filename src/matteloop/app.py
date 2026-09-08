"""Command-line entry points for MatteLoop."""

from __future__ import annotations

import argparse
import json
import logging
import multiprocessing
import os
import platform
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Any

from matteloop import __version__, application_title
from matteloop.core.execution_providers import onnxruntime_repair_command

_LOGGER = logging.getLogger(__name__)
_ONNXRUNTIME_DISTRIBUTIONS = (
    "onnxruntime-directml",
    "onnxruntime-gpu",
    "onnxruntime",
)


@dataclass(frozen=True, slots=True)
class _ProviderDiagnostics:
    """Collected provider facts plus the state needed by each caller."""

    lines: tuple[str, ...]
    runtime_usable: bool
    total_failure: bool
    partial_failure: bool
    failure_reason: str | None
    distribution: str
    distribution_version: str
    device: str
    available_providers: tuple[str, ...]


def _run_gui() -> int:
    """Lazily import Qt only for the normal graphical launch path."""
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    from matteloop.core.execution_providers import provider_options_from_runtime
    from matteloop.core.parameters import V1_MODEL_IDS
    from matteloop.core.state import AppState, ModelAvailabilityChanged
    from matteloop.logs import configure_logging
    from matteloop.ui.controller import SourceController
    from matteloop.ui.i18n import (
        configure_locale,
        install_translators,
        selected_language,
    )
    from matteloop.ui.main_window import MainWindow
    from matteloop.ui.preferences import load_parameters
    from matteloop.ui.store import ReducerStore
    from matteloop.ui.theme import install_theme

    configure_logging()
    application = QApplication.instance()
    if not isinstance(application, QApplication):
        application = QApplication(["matteloop"])
    application.setApplicationName("MatteLoop")
    application.setApplicationDisplayName("MatteLoop")
    application.setOrganizationName("MatteLoop")
    application.setApplicationVersion(__version__)
    settings = QSettings()
    language = selected_language(settings)
    configure_locale(language)
    _translators = install_translators(application, language)
    install_theme(application)
    stored_model = settings.value("parameters/model_id")
    model_id = stored_model if stored_model in V1_MODEL_IDS else "birefnet-portrait"
    runtime_usable = _log_runtime_diagnostics()
    provider_options = provider_options_from_runtime(model_id=model_id)
    store = ReducerStore(
        AppState(parameters=load_parameters(settings, provider_options))
    )
    controller = SourceController(store, settings=settings, parent=application)
    availability = dict(controller.model_options).get(
        store.state.parameters.model_id, False
    )
    store.dispatch(ModelAvailabilityChanged(availability))
    window = MainWindow(
        store,
        controller,
        settings,
        model_options=controller.model_options,
        provider_options=provider_options,
        runtime_unavailable=not runtime_usable,
    )
    controller.set_dialog_parent(window)
    controller.attach_transform_stage(
        window.inspector.transform_group, window.result_canvas
    )
    application.aboutToQuit.connect(controller.shutdown)
    window.show()
    return application.exec()


def _load_onnxruntime() -> object:
    import onnxruntime  # type: ignore[import-untyped]

    return onnxruntime


def _onnxruntime_flavor(device: str) -> str:
    if "DML" in device:
        return "onnxruntime-directml"
    if "GPU" in device:
        return "onnxruntime-gpu"
    return "onnxruntime"


def _onnxruntime_distribution() -> tuple[str, str]:
    for distribution in _ONNXRUNTIME_DISTRIBUTIONS:
        try:
            return distribution, metadata.version(distribution)
        except metadata.PackageNotFoundError:
            continue
    # Frozen bundles (Nuitka standalone) carry no dist-info for onnxruntime,
    # so fall back to facts read straight off the loaded module.
    try:
        import onnxruntime

        version = str(onnxruntime.__version__)
        device = str(onnxruntime.get_device())
    except Exception:
        return "none", "none"
    return _onnxruntime_flavor(device), version


def _error_reason(error: BaseException) -> str:
    return str(error) or type(error).__name__


def _log_runtime_diagnostics() -> bool:
    report = _collect_provider_diagnostics_report(include_video_adapters=False)
    if report.total_failure:
        _LOGGER.error(
            "ONNX Runtime startup diagnostics failed: %s. Repair with: %s",
            report.failure_reason or "no execution providers were reported",
            onnxruntime_repair_command(),
        )
    elif report.partial_failure:
        _LOGGER.warning(
            "Could not collect complete ONNX Runtime startup diagnostics: %s",
            report.failure_reason or "an optional runtime detail was unavailable",
        )
    else:
        _LOGGER.info(
            "ONNX Runtime distribution=%s version=%s device=%s available_providers=%s",
            report.distribution,
            report.distribution_version,
            report.device,
            report.available_providers,
        )
    return report.runtime_usable


def _runtime_diagnostic_report(
    runtime: object | None, runtime_error: str | None
) -> _ProviderDiagnostics:
    if runtime is None:
        return _unavailable_runtime_report(runtime_error or "runtime unavailable")

    device, device_error = _runtime_device(runtime)
    available_providers, providers_error = _runtime_providers(runtime)
    if providers_error is not None:
        return _provider_failure_report(device, providers_error, device_error)

    return _available_provider_report(device, device_error, available_providers)


def _unavailable_runtime_report(reason: str) -> _ProviderDiagnostics:
    return _ProviderDiagnostics(
        (
            f"onnxruntime device: unavailable ({reason})",
            f"onnxruntime available providers: unavailable ({reason})",
            f"provider options: unavailable ({reason})",
        ),
        False,
        True,
        False,
        reason,
        "none",
        "none",
        f"unavailable ({reason})",
        (),
    )


def _provider_failure_report(
    device: str, providers_error: str, device_error: str | None
) -> _ProviderDiagnostics:
    return _ProviderDiagnostics(
        (
            f"onnxruntime device: {device}",
            f"onnxruntime available providers: unavailable ({providers_error})",
            f"provider options: unavailable ({providers_error})",
        ),
        False,
        True,
        device_error is not None,
        providers_error,
        "unknown",
        "unknown",
        device,
        (),
    )


def _available_provider_report(
    device: str, device_error: str | None, available_providers: tuple[str, ...]
) -> _ProviderDiagnostics:
    from matteloop.core.execution_providers import provider_options

    available_text = repr(list(available_providers))
    options_error: str | None = None
    try:
        options = provider_options(available_providers, device=device)
    except Exception as error:
        options = ()
        options_error = _error_reason(error)
    lines = [
        f"onnxruntime device: {device}",
        f"onnxruntime available providers: {available_text}",
    ]
    if options_error is not None:
        lines.append(f"provider options: unavailable ({options_error})")
    elif options:
        lines.append("provider options:")
        lines.extend(
            f"  {option.provider}: {option.label}"
            + (" [recommended]" if option.recommended else "")
            for option in options
        )
    else:
        lines.append("provider options: none")
    failure_reason = options_error or device_error
    if not available_providers:
        failure_reason = "get_available_providers() returned no providers"
    return _ProviderDiagnostics(
        tuple(lines),
        bool(available_providers),
        not available_providers,
        failure_reason is not None and bool(available_providers),
        failure_reason,
        "unknown",
        "unknown",
        device,
        available_providers,
    )


def _runtime_device(runtime: object) -> tuple[str, str | None]:
    try:
        return str(getattr(runtime, "get_device")()), None
    except Exception as error:
        reason = _error_reason(error)
        return f"unavailable ({reason})", reason


def _runtime_providers(runtime: object) -> tuple[tuple[str, ...], str | None]:
    try:
        return (
            tuple(
                str(provider)
                for provider in getattr(runtime, "get_available_providers")()
            ),
            None,
        )
    except Exception as error:
        return (), _error_reason(error)


def _runtime_diagnostic_lines(
    runtime: object | None, runtime_error: str | None
) -> tuple[str, ...]:
    return _runtime_diagnostic_report(runtime, runtime_error).lines


def _collect_provider_diagnostics_report(
    runtime: object | None = None,
    *,
    include_video_adapters: bool = True,
) -> _ProviderDiagnostics:
    """Collect facts and the usability result shared by CLI, logging, and GUI."""
    distribution_error: str | None = None
    try:
        distribution, distribution_version = _onnxruntime_distribution()
        distribution_text = (
            "none"
            if distribution == "none"
            else f"{distribution} {distribution_version}"
        )
    except Exception as error:
        distribution = "unavailable"
        distribution_version = "unavailable"
        distribution_text = f"unavailable ({_error_reason(error)})"
        distribution_error = _error_reason(error)
    lines = [
        f"MatteLoop version: {__version__}",
        f"Python/platform: {platform.python_version()} / {platform.platform()}",
        f"ONNX Runtime distribution: {distribution_text}",
    ]
    runtime_error: str | None = None
    if runtime is None:
        try:
            runtime = _load_onnxruntime()
        except Exception as error:
            runtime_error = _error_reason(error)
            lines.append(f"onnxruntime: unavailable ({runtime_error})")
    report = _runtime_diagnostic_report(runtime, runtime_error)
    lines.extend(report.lines)
    if include_video_adapters and sys.platform == "win32":
        lines.extend(_video_adapter_diagnostics())
    failure_reason = report.failure_reason or distribution_error
    return _ProviderDiagnostics(
        tuple(lines),
        report.runtime_usable,
        report.total_failure,
        report.partial_failure or distribution_error is not None,
        failure_reason,
        distribution,
        distribution_version,
        report.device,
        report.available_providers,
    )


def _collect_provider_diagnostics(runtime: object | None = None) -> tuple[str, ...]:
    """Collect headless runtime facts without creating an ONNX session."""
    return _collect_provider_diagnostics_report(runtime).lines


def _video_adapter_diagnostics() -> tuple[str, ...]:
    powershell = os.path.join(
        os.environ.get("SystemRoot", r"C:\Windows"),
        "System32",
        "WindowsPowerShell",
        "v1.0",
        "powershell.exe",
    )
    command = (
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
        "$ErrorActionPreference='Stop'; "
        "Get-CimInstance Win32_VideoController | Select-Object Name,DriverVersion"
    )
    run_kwargs: dict[str, Any] = {
        "capture_output": True,
        "check": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": 10,
    }
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", None)
    if creationflags is not None:
        run_kwargs["creationflags"] = creationflags
    try:
        result = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
            **run_kwargs,
        )
        output = " ".join(result.stdout.split())
        return (f"video adapters: {output or 'none'}",)
    except Exception as error:
        return (f"video adapters: unavailable ({_error_reason(error)})",)


def _run_provider_command() -> int:
    report = _collect_provider_diagnostics_report()
    for line in report.lines:
        print(line)
    if report.runtime_usable:
        return 0
    print(
        "ONNX Runtime unusable: "
        f"{report.failure_reason or 'no execution providers were reported'}. "
        f"Repair with: {onnxruntime_repair_command()}"
    )
    return 1


def main(argv: Sequence[str] | None = None) -> int:
    """Run MatteLoop, handling headless diagnostics before Qt is imported."""
    multiprocessing.freeze_support()
    parser = argparse.ArgumentParser(prog="matteloop")
    parser.add_argument("--version", action="store_true", help="show the version")
    parser.add_argument(
        "--providers",
        action="store_true",
        help="show the installed ONNX Runtime providers",
    )
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help="verify the executable's headless startup surface",
    )
    args = parser.parse_args(argv)

    if args.version:
        print(application_title())
        return 0
    if args.providers:
        return _run_provider_command()
    if args.smoke_test:
        from matteloop.smoke import run_smoke

        configured_work_dir = os.environ.get("MATTELOOP_SMOKE_WORK_DIR")
        try:
            if configured_work_dir:
                result = run_smoke(Path(configured_work_dir), use_fake_model=True)
            else:
                with tempfile.TemporaryDirectory(prefix="matteloop-smoke-cli-") as raw:
                    result = run_smoke(Path(raw), use_fake_model=True)
        except Exception as error:
            print(
                json.dumps(
                    {
                        "error": {
                            "message": str(error),
                            "type": type(error).__name__,
                        },
                        "ok": False,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            return 1
        print(
            json.dumps(
                {"ok": True, "result": result.to_primitives()},
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 0

    return _run_gui()
