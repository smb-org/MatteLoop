from __future__ import annotations

import configparser
import plistlib
import shlex
import subprocess
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QSettings

from matteloop import __version__, application_title
from matteloop.app import _collect_provider_diagnostics, main
from matteloop.core.state import AppState
from matteloop.ui.main_window import MainWindow
from scripts.build import BUNDLE_IDENTIFIER, verify_macos_bundle_signature

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


class _Store:
    state = AppState()

    def dispatch(self, event: object) -> None:
        del event

    def subscribe(self, listener: Callable[[AppState], None]) -> Callable[[], None]:
        del listener
        return lambda: None


class _Services:
    def dispatch(self, command: object) -> None:
        del command


def test_main_window_title_identifies_the_current_build(qtbot) -> None:
    settings = QSettings(
        QSettings.IniFormat, QSettings.UserScope, "matteloop-release-test", "title"
    )
    settings.clear()
    window = MainWindow(_Store(), _Services(), settings)
    qtbot.addWidget(window)

    assert window.windowTitle() == application_title()


def test_version_command_uses_the_application_title(capsys) -> None:
    assert main(["--version"]) == 0

    assert capsys.readouterr().out.strip() == application_title()


def test_provider_diagnostics_identify_the_current_build(
    monkeypatch,
) -> None:
    import matteloop.app as app

    runtime = SimpleNamespace(
        get_device=lambda: "CPU",
        get_available_providers=lambda: ["CPUExecutionProvider"],
    )
    monkeypatch.setattr(app, "_onnxruntime_distribution", lambda: ("fake", "1.2"))

    lines = _collect_provider_diagnostics(runtime)

    assert lines[0] == f"MatteLoop version: {__version__}"


def test_native_packaging_version_matches_the_package_version() -> None:
    parser = configparser.ConfigParser(
        comment_prefixes=("/",), strict=False, allow_no_value=True
    )
    parser.read(
        REPOSITORY_ROOT / "packaging" / "pysidedeploy.spec", encoding="utf-8"
    )
    configured_version = parser.get("app", "version")
    args = set(shlex.split(parser.get("nuitka", "extra_args")))

    assert configured_version == __version__
    assert f"--file-version={configured_version}" in args
    assert f"--product-version={configured_version}" in args
    assert f"--macos-app-version={configured_version}" in args
    assert f"--macos-signed-app-name={BUNDLE_IDENTIFIER}" in args


def test_macos_bundle_metadata_identifies_and_verifies_the_current_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = tmp_path / "MatteLoop.app"
    info_plist = tmp_path / "MatteLoop.app" / "Contents" / "Info.plist"
    info_plist.parent.mkdir(parents=True)
    original = plistlib.dumps(
        {
            "CFBundleExecutable": "MatteLoop",
            "CFBundleShortVersionString": __version__,
            "CFBundleIdentifier": BUNDLE_IDENTIFIER,
        }
    )
    info_plist.write_bytes(original)
    codesign_calls: list[list[str]] = []

    def fake_codesign(
        command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        codesign_calls.append(command)
        return subprocess.CompletedProcess(command, 0, stderr="Info.plist entries=9")

    monkeypatch.setattr("scripts.build.subprocess.run", fake_codesign)

    verify_macos_bundle_signature(bundle)

    # Nothing is written: the plist Nuitka signed is the plist that ships.
    assert plistlib.loads(info_plist.read_bytes()) == plistlib.loads(original)
    assert codesign_calls == [["codesign", "-dv", str(bundle)]]


def test_macos_bundle_metadata_verification_failure_aborts_the_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = tmp_path / "MatteLoop.app"
    info_plist = bundle / "Contents" / "Info.plist"
    info_plist.parent.mkdir(parents=True)
    info_plist.write_bytes(
        plistlib.dumps(
            {
                "CFBundleExecutable": "MatteLoop",
                "CFBundleShortVersionString": __version__,
                "CFBundleIdentifier": BUNDLE_IDENTIFIER,
            }
        )
    )

    def fake_codesign(
        command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        if command[1] == "-dv":
            return subprocess.CompletedProcess(
                command, 0, stderr="Info.plist=not bound"
            )
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr("scripts.build.subprocess.run", fake_codesign)

    with pytest.raises(RuntimeError, match="did not bind"):
        verify_macos_bundle_signature(bundle)
