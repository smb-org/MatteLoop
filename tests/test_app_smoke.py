from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import textwrap
from importlib.metadata import version
from pathlib import Path
from types import SimpleNamespace

import pytest

from matteloop.app import main

_GUARDED_HEADLESS_SCRIPT = """
import builtins
import os
import socket
import sys

FORBIDDEN_ROOTS = {"PySide6", "rembg", "onnxruntime"}
MODEL_PATH_MARKERS = (".u2net", "onnx", "rembg")

class ForbiddenImportFinder:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.partition(".")[0] in FORBIDDEN_ROOTS:
            raise RuntimeError(f"forbidden import: {fullname}")
        return None

sys.meta_path.insert(0, ForbiddenImportFinder())

def forbidden_network(*args, **kwargs):
    raise RuntimeError("network access is forbidden in headless smoke commands")

socket.socket = forbidden_network
socket.create_connection = forbidden_network

original_open = builtins.open
def guarded_open(file, *args, **kwargs):
    try:
        candidate = os.fspath(file).lower()
    except TypeError:
        candidate = ""
    if any(marker in candidate for marker in MODEL_PATH_MARKERS):
        raise RuntimeError(f"model access is forbidden: {candidate}")
    return original_open(file, *args, **kwargs)

builtins.open = guarded_open

from matteloop.app import main

raise SystemExit(main([sys.argv[1]]))
"""

_GUI_QUIT_SCRIPT = """
import sys
from pathlib import Path
from threading import Event

from PySide6.QtCore import QObject, QCoreApplication, QThread, QTimer
from PySide6.QtWidgets import QApplication, QWidget

import matteloop.app as app_module
import matteloop.core.execution_providers as execution_providers
import matteloop.logs as logs_module
import matteloop.ui.controller as controller_module
import matteloop.ui.i18n as i18n_module
import matteloop.ui.main_window as main_window_module
import matteloop.ui.theme as theme_module
from matteloop.ui.worker_thread import wait_for_thread_shutdown

mode = sys.argv[2]
log_path = Path(sys.argv[1])


class UncooperativeThread(QThread):
    def __init__(self):
        super().__init__()
        self.started = Event()
        self.release = Event()

    def run(self):
        self.started.set()
        self.release.wait(5)


class SourceController(QObject):
    def __init__(self, _store, settings=None, parent=None):
        super().__init__(parent)
        self.model_options = ()
        self._thread = UncooperativeThread() if mode == "stalled" else None
        self.shutdown_complete = True

    def set_dialog_parent(self, _parent):
        pass

    def attach_transform_stage(self, _group, _canvas):
        pass

    def shutdown(self):
        if self._thread is None:
            return True
        self._thread.start()
        if not self._thread.started.wait(1):
            raise AssertionError("the uncooperative worker did not start")
        self._thread.requestInterruption()
        self.shutdown_complete = wait_for_thread_shutdown(
            self._thread, 25, description="the uncooperative worker"
        )
        return self.shutdown_complete


class Timeline:
    shutdown_complete = True


class Inspector:
    transform_group = object()


class MainWindow(QWidget):
    def __init__(self, *_args, **_kwargs):
        super().__init__()
        self.inspector = Inspector()
        self.result_canvas = None
        self.timeline_widget = Timeline()


logs_module.log_file = lambda: log_path
controller_module.SourceController = SourceController
main_window_module.MainWindow = MainWindow
execution_providers.provider_options_from_runtime = lambda model_id: ()
i18n_module.configure_locale = lambda language: None
i18n_module.install_translators = lambda application, language: ()
i18n_module.selected_language = lambda settings: "en"
theme_module.install_theme = lambda application: None
app_module._log_runtime_diagnostics = lambda: True
app_module._start_update_controller = lambda *args: None

application = QApplication([])
QTimer.singleShot(0, lambda: QCoreApplication.exit(17))
exit_code = app_module.main([])
print("main-returned")
raise SystemExit(exit_code)
"""

_REAL_SOURCE_CONTROLLER_QUIT_SCRIPT = """
import sys
from pathlib import Path
from threading import Event

from PySide6.QtCore import QCoreApplication, QTimer
from PySide6.QtWidgets import QApplication, QWidget

import matteloop.app as app_module
import matteloop.core.execution_providers as execution_providers
import matteloop.logs as logs_module
import matteloop.ui.controller as controller_module
import matteloop.ui.i18n as i18n_module
import matteloop.ui.main_window as main_window_module
import matteloop.ui.theme as theme_module
from matteloop.ui.ports import VideoDropped
from matteloop.ui.transform_group import TransformGroup

log_path = Path(sys.argv[1])
source_path = Path(sys.argv[2])


class BlockingSourceAdapter:
    started = Event()
    release = Event()

    def load(self, _path, _request_id):
        self.started.set()
        self.release.wait(5)
        raise RuntimeError("the blocking source adapter was released")


class Timeline:
    shutdown_complete = True


class Inspector:
    def __init__(self):
        self.transform_group = TransformGroup(lambda _command: None)


class MainWindow(QWidget):
    def __init__(self, _store, source_controller, *_args, **_kwargs):
        super().__init__()
        self.inspector = Inspector()
        self.result_canvas = None
        self.timeline_widget = Timeline()
        source_controller.dispatch(VideoDropped(source_path))
        self._quit_when_started()

    def _quit_when_started(self):
        if BlockingSourceAdapter.started.is_set():
            QCoreApplication.exit(17)
        else:
            QTimer.singleShot(1, self._quit_when_started)


logs_module.log_file = lambda: log_path
controller_module.PyAVSourceAdapter = BlockingSourceAdapter
controller_module._THREAD_SHUTDOWN_TIMEOUT_MS = 25
main_window_module.MainWindow = MainWindow
execution_providers.provider_options_from_runtime = lambda model_id: ()
i18n_module.configure_locale = lambda language: None
i18n_module.install_translators = lambda application, language: ()
i18n_module.selected_language = lambda settings: "en"
theme_module.install_theme = lambda application: None
app_module._log_runtime_diagnostics = lambda: True
app_module._start_update_controller = lambda *args: None

application = QApplication([])
exit_code = app_module.main([])
print("main-returned")
raise SystemExit(exit_code)
"""


def _run_guarded_smoke_command(
    tmp_path: Path, argument: str
) -> subprocess.CompletedProcess[str]:
    environment = os.environ | {
        "DISPLAY": "matteloop-smoke-display-must-not-open",
        "HOME": str(tmp_path),
        "QT_QPA_PLATFORM": "matteloop-smoke-platform-must-not-initialize",
        "U2NET_HOME": str(tmp_path / "model-guard"),
    }
    return subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            textwrap.dedent(_GUARDED_HEADLESS_SCRIPT),
            argument,
        ],
        capture_output=True,
        check=False,
        env=environment,
        text=True,
    )


def _run_gui_quit_subprocess(
    tmp_path: Path, mode: str
) -> subprocess.CompletedProcess[str]:
    environment = os.environ | {"QT_QPA_PLATFORM": "offscreen"}
    return subprocess.run(
        [
            sys.executable,
            "-c",
            textwrap.dedent(_GUI_QUIT_SCRIPT),
            str(tmp_path / "matteloop.log"),
            mode,
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        check=False,
        env=environment,
        text=True,
        timeout=10,
    )


def _run_real_source_controller_quit_subprocess(
    tmp_path: Path,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ | {
        "HOME": str(tmp_path),
        "QT_QPA_PLATFORM": "offscreen",
    }
    return subprocess.run(
        [
            sys.executable,
            "-c",
            textwrap.dedent(_REAL_SOURCE_CONTROLLER_QUIT_SCRIPT),
            str(tmp_path / "matteloop.log"),
            str(tmp_path / "blocking-source.mp4"),
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        check=False,
        env=environment,
        text=True,
        timeout=10,
    )


def test_main_reports_version_without_opening_qt(capsys):
    assert main(["--version"]) == 0
    assert capsys.readouterr().out.strip().startswith("MatteLoop ")


def test_main_delegates_normal_launch_to_lazy_gui_seam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import matteloop.app as app

    calls: list[bool] = []
    monkeypatch.setattr(
        app, "_run_gui", lambda: calls.append(True) or 23, raising=False
    )

    assert app.main([]) == 23
    assert calls == [True]


def test_quit_with_an_unjoined_worker_exits_without_a_qt_abort(
    tmp_path: Path,
) -> None:
    result = _run_gui_quit_subprocess(tmp_path, "stalled")

    assert result.returncode == 17, result.stderr
    assert result.stdout == ""
    assert "worker(s) outlived their bounded shutdown wait" in (
        tmp_path / "matteloop.log"
    ).read_text(encoding="utf-8")


def test_quit_with_joined_workers_returns_the_application_exit_code(
    tmp_path: Path,
) -> None:
    result = _run_gui_quit_subprocess(tmp_path, "clean")

    assert result.returncode == 17, result.stderr
    assert result.stdout.strip() == "main-returned"
    log = tmp_path / "matteloop.log"
    if log.exists():
        assert "outlived their bounded shutdown wait" not in log.read_text(
            encoding="utf-8"
        )


def test_quit_with_real_source_controller_does_not_abort_on_source_timeout(
    tmp_path: Path,
) -> None:
    result = _run_real_source_controller_quit_subprocess(tmp_path)

    assert result.returncode == 17, result.stderr
    assert result.stdout == ""
    assert "worker(s) outlived their bounded shutdown wait" in (
        tmp_path / "matteloop.log"
    ).read_text(encoding="utf-8")


def test_main_smoke_test_prints_machine_readable_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from matteloop import smoke

    monkeypatch.setattr(
        smoke,
        "run_smoke",
        lambda work_dir, use_fake_model=True: smoke.SmokeResult(
            qt_platform="offscreen",
            qt_image_formats=("png", "webp"),
            video_decoded=True,
            webp_frames=2,
            webp_has_alpha=True,
            spawn_start_method="spawn",
            shared_memory_roundtrip=True,
            shared_memory_unlinked=True,
            fake_session_used=True,
            rembg_session_classes=13,
            peak_full_res_rgba_owners=2,
        ),
    )
    monkeypatch.setenv("MATTELOOP_SMOKE_WORK_DIR", str(tmp_path))

    assert main(["--smoke-test"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["result"]["webp_frames"] == 2


def test_main_smoke_test_prints_structured_nonzero_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from matteloop import smoke

    def fail(_work_dir: Path, use_fake_model: bool = True) -> smoke.SmokeResult:
        del use_fake_model
        raise RuntimeError("intentional smoke failure")

    monkeypatch.setattr(smoke, "run_smoke", fail)
    monkeypatch.setenv("MATTELOOP_SMOKE_WORK_DIR", str(tmp_path))

    assert main(["--smoke-test"]) != 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "error": {
            "message": "intentional smoke failure",
            "type": "RuntimeError",
        },
        "ok": False,
    }


def test_provider_command_prints_fake_runtime_facts(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import matteloop.app as app
    import matteloop.core.execution_providers as execution_providers

    runtime = SimpleNamespace(
        get_device=lambda: "GPU",
        get_available_providers=lambda: [
            "DmlExecutionProvider",
            "CPUExecutionProvider",
        ],
    )
    monkeypatch.setattr(execution_providers.platform, "system", lambda: "Windows")
    monkeypatch.setattr(execution_providers.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(app, "_onnxruntime_distribution", lambda: ("fake", "1.2"))
    monkeypatch.setattr(app, "_load_onnxruntime", lambda: runtime)

    assert app.main(["--providers"]) == 0
    output = capsys.readouterr().out
    assert "ONNX Runtime distribution: fake 1.2" in output
    assert "onnxruntime device: GPU" in output
    assert (
        "DmlExecutionProvider: GPU over DirectML – recommended [recommended]"
        in output
    )
    assert "CPUExecutionProvider: CPU" in output


@pytest.mark.parametrize(
    ("platform_name", "repair_distribution"),
    [
        ("win32", "onnxruntime-directml"),
        ("darwin", "onnxruntime"),
        ("linux", "onnxruntime"),
    ],
)
def test_runtime_repair_advice_matches_the_platform_distribution(
    platform_name: str,
    repair_distribution: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import matteloop.app as app
    from matteloop.ui.copy import runtime_banner_copy

    class BrokenRuntime:
        def get_device(self) -> str:
            raise AttributeError("get_device is missing")

        def get_available_providers(self) -> list[str]:
            raise AttributeError("get_available_providers is missing")

    monkeypatch.setattr(app.sys, "platform", platform_name)
    monkeypatch.setattr(app, "_onnxruntime_distribution", lambda: ("fake", "1.2"))
    monkeypatch.setattr(app, "_load_onnxruntime", lambda: BrokenRuntime())
    monkeypatch.setattr(app, "_video_adapter_diagnostics", lambda: ())
    expected_command = f"uv sync --reinstall-package {repair_distribution}"

    assert app.main(["--providers"]) == 1
    assert expected_command in capsys.readouterr().out
    assert expected_command in runtime_banner_copy()


def test_onnxruntime_distribution_falls_back_to_loaded_module_when_metadata_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import matteloop.app as app

    def missing(name: str) -> str:
        raise app.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(app.metadata, "version", missing)
    fake_runtime = SimpleNamespace(__version__="1.24.4", get_device=lambda: "CPU-DML")
    monkeypatch.setitem(sys.modules, "onnxruntime", fake_runtime)

    distribution, distribution_version = app._onnxruntime_distribution()

    assert f"{distribution} {distribution_version}" == "onnxruntime-directml 1.24.4"


def test_provider_command_reports_unavailable_windows_video_adapters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import matteloop.app as app

    runtime = SimpleNamespace(
        get_device=lambda: "GPU",
        get_available_providers=lambda: ["CPUExecutionProvider"],
    )

    def fail(*_args: object, **_kwargs: object) -> object:
        raise OSError("PowerShell unavailable")

    monkeypatch.setattr(app.sys, "platform", "win32")
    monkeypatch.setattr(app.subprocess, "run", fail)

    lines = app._collect_provider_diagnostics(runtime)

    assert "video adapters: unavailable (PowerShell unavailable)" in lines


def test_runtime_diagnostic_lines_keep_partial_facts_when_runtime_probes_fail() -> None:
    import matteloop.app as app

    class BrokenRuntime:
        def get_device(self) -> str:
            raise AttributeError("get_device is missing")

        def get_available_providers(self) -> list[str]:
            raise AttributeError("get_available_providers is missing")

    lines = app._runtime_diagnostic_lines(BrokenRuntime(), None)
    report = app._runtime_diagnostic_report(BrokenRuntime(), None)

    assert lines == (
        "onnxruntime device: unavailable (get_device is missing)",
        "onnxruntime available providers: unavailable ("
        "get_available_providers is missing)",
        "provider options: unavailable (get_available_providers is missing)",
    )
    assert report.runtime_usable is False
    assert report.total_failure is True


def test_provider_diagnostics_report_unimportable_runtime_without_dropping_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import matteloop.app as app

    monkeypatch.setattr(app, "_onnxruntime_distribution", lambda: ("fake", "1.2"))

    def fail() -> object:
        raise ImportError("onnxruntime import failed")

    monkeypatch.setattr(app, "_load_onnxruntime", fail)

    lines = app._collect_provider_diagnostics()

    assert lines[:3] == (
        f"MatteLoop version: {app.__version__}",
        f"Python/platform: {app.platform.python_version()} / {app.platform.platform()}",
        "ONNX Runtime distribution: fake 1.2",
    )
    assert "onnxruntime: unavailable (onnxruntime import failed)" in lines
    assert (
        "onnxruntime available providers: unavailable (onnxruntime import failed)"
        in lines
    )
    report = app._collect_provider_diagnostics_report()
    assert report.runtime_usable is False
    assert report.total_failure is True


def test_provider_command_reports_unusable_runtime_and_returns_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import matteloop.app as app
    from matteloop.core.execution_providers import onnxruntime_repair_command

    class BrokenRuntime:
        def get_device(self) -> str:
            raise AttributeError("get_device is missing")

        def get_available_providers(self) -> list[str]:
            raise AttributeError("get_available_providers is missing")

    monkeypatch.setattr(app, "_onnxruntime_distribution", lambda: ("fake", "1.2"))
    monkeypatch.setattr(app, "_load_onnxruntime", lambda: BrokenRuntime())

    assert app.main(["--providers"]) == 1
    output = capsys.readouterr().out
    assert "MatteLoop version:" in output
    assert "Python/platform:" in output
    assert "ONNX Runtime distribution: fake 1.2" in output
    assert output.splitlines()[-1] == (
        "ONNX Runtime unusable: get_available_providers is missing. "
        f"Repair with: {onnxruntime_repair_command()}"
    )


def test_provider_command_reports_empty_provider_list_and_returns_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import matteloop.app as app

    runtime = SimpleNamespace(
        get_device=lambda: "CPU",
        get_available_providers=lambda: [],
    )
    monkeypatch.setattr(app, "_onnxruntime_distribution", lambda: ("fake", "1.2"))
    monkeypatch.setattr(app, "_load_onnxruntime", lambda: runtime)

    assert app.main(["--providers"]) == 1
    assert "onnxruntime available providers: []" in capsys.readouterr().out


def test_startup_runtime_diagnostics_log_total_failure_at_error(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import matteloop.app as app
    from matteloop.core.execution_providers import onnxruntime_repair_command

    monkeypatch.setattr(app, "_onnxruntime_distribution", lambda: ("fake", "1.2"))

    def fail() -> object:
        raise ImportError("onnxruntime import failed")

    monkeypatch.setattr(app, "_load_onnxruntime", fail)
    with caplog.at_level(logging.INFO, logger="matteloop.app"):
        assert app._log_runtime_diagnostics() is False

    record = caplog.records[-1]
    assert record.levelno == logging.ERROR
    assert onnxruntime_repair_command() in record.message


def test_startup_runtime_diagnostics_log_partial_failure_at_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import matteloop.app as app

    class DeviceUnavailableRuntime:
        def get_device(self) -> str:
            raise AttributeError("get_device is missing")

        def get_available_providers(self) -> list[str]:
            return ["CPUExecutionProvider"]

    monkeypatch.setattr(app, "_onnxruntime_distribution", lambda: ("fake", "1.2"))
    monkeypatch.setattr(app, "_load_onnxruntime", lambda: DeviceUnavailableRuntime())
    with caplog.at_level(logging.INFO, logger="matteloop.app"):
        assert app._log_runtime_diagnostics() is True

    record = caplog.records[-1]
    assert record.levelno == logging.WARNING
    assert "get_device is missing" in record.message


@pytest.mark.parametrize(
    ("argument", "expected_output"),
    [
        ("--version", f"MatteLoop {version('matteloop')}"),
        ("--providers", "onnxruntime: unavailable (forbidden import: onnxruntime)"),
    ],
)
def test_headless_commands_run_in_fresh_guarded_interpreters(
    tmp_path: Path,
    argument: str,
    expected_output: str,
):
    result = _run_guarded_smoke_command(tmp_path, argument)

    expected_returncode = 1 if argument == "--providers" else 0
    assert result.returncode == expected_returncode, result.stderr
    if argument == "--providers":
        assert expected_output in result.stdout
    else:
        assert result.stdout.strip() == expected_output
