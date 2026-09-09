from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path, PureWindowsPath
from types import ModuleType

import pytest
from PySide6.QtCore import QSettings, QUrl
from PySide6.QtGui import QDesktopServices

from matteloop.core.state import (
    AppState,
    CancelAcknowledged,
    CancelRequested,
    RenderRequested,
    SourceLoaded,
    SourceLoadRequested,
    reduce,
)
from matteloop.ui.main_window import MainWindow
from matteloop.ui.store import ReducerStore
from matteloop.ui.update_controller import (
    UpdateController,
    _create_update_manager,
    _locator_config,
    _pending_update,
)
from matteloop.updates import (
    UpdateOutcome,
    UpdateResult,
    update_feed_url,
    update_package_url,
)
from tests.update_fakes import FakeResponse, FakeTransport


@dataclass(frozen=True)
class _Metadata:
    path: Path
    width: int = 640
    height: int = 360
    duration: Fraction = Fraction(4)


@dataclass(frozen=True)
class _Asset:
    Version: str
    FileName: str = "matteloop-update.nupkg"


@dataclass(frozen=True)
class _UpdateInfo:
    TargetFullRelease: _Asset


class _Manager:
    def __init__(
        self,
        update: object | None = None,
        *,
        pending: object | None = None,
        pending_values: list[object | None] | None = None,
        current_version: str = "0.3.0",
    ) -> None:
        del update
        self.pending = pending
        self.pending_values = pending_values
        self.current_version = current_version
        self.pending_calls = 0
        self.apply_calls: list[tuple[object, bool, bool]] = []

    def get_current_version(self) -> str:
        return self.current_version

    def get_update_pending_restart(self) -> object | None:
        self.pending_calls += 1
        if self.pending_values is not None:
            return self.pending_values.pop(0)
        return self.pending

    def wait_exit_then_apply_updates(
        self,
        update: object,
        silent: bool = False,
        restart: bool = True,
        restart_args: object | None = None,
    ) -> None:
        del restart_args
        self.apply_calls.append((update, silent, restart))


@pytest.fixture(autouse=True)
def _supported_platform(monkeypatch) -> None:
    """Pin the runtime platform: the feed channel is derived from it.

    Without this the suite passes on macOS and fails on Linux, because the
    controller asks for the channel of the host it runs on while these
    fixtures describe the macOS one.
    """
    monkeypatch.setattr(sys, "platform", "darwin")


def _download_transport(
    version: str = "0.4.0",
    package: bytes = b"package",
    *,
    package_response: FakeResponse | BaseException | None = None,
) -> tuple[FakeTransport, _Asset]:
    asset = _Asset(
        version,
        "io.github.smb-org.matteloop-0.4.0-osx-arm64-full.nupkg",
    )
    feed = json.dumps(
        {
            "Assets": [
                {
                    "Version": version,
                    "Type": "Full",
                    "FileName": asset.FileName,
                    "SHA256": hashlib.sha256(package).hexdigest().upper(),
                    "Size": len(package),
                }
            ]
        }
    ).encode()
    return (
        FakeTransport(
            {
                update_feed_url(version, platform="darwin"): FakeResponse(feed),
                update_package_url(version, asset.FileName): (
                    package_response or FakeResponse(package)
                ),
            }
        ),
        asset,
    )


class _Services:
    def dispatch(self, _command: object) -> None:
        return

    def confirm_discard_unsaved_transform(self, _parent: object) -> bool:
        return True


class _Reader:
    def __init__(self, result: UpdateResult) -> None:
        self.result = result
        self.calls = 0
        self.requests: list[tuple[str, str | None]] = []

    def check(
        self, channel: str = "stable", current_version: str | None = None
    ) -> UpdateResult:
        self.calls += 1
        self.requests.append((channel, current_version))
        return self.result


def _settings(name: str) -> QSettings:
    settings = QSettings(
        QSettings.IniFormat,
        QSettings.UserScope,
        "matteloop-test",
        name,
    )
    settings.clear()
    return settings


def _window(qtbot, settings: QSettings) -> MainWindow:
    window = MainWindow(ReducerStore(AppState()), _Services(), settings)
    qtbot.addWidget(window)
    window.show()
    return window


def _controller(
    qtbot,
    settings: QSettings,
    result: UpdateResult,
    *,
    manager: _Manager | None = None,
    store: ReducerStore | None = None,
    transport: FakeTransport | None = None,
) -> tuple[MainWindow, UpdateController, _Reader]:
    if store is None:
        store = ReducerStore(AppState())
    window = MainWindow(store, _Services(), settings)
    qtbot.addWidget(window)
    window.show()
    reader = _Reader(result)
    controller = UpdateController(
        window, settings, reader, manager=manager, transport=transport
    )
    return window, controller, reader


def test_no_update_keeps_the_offer_hidden(qtbot) -> None:
    window, controller, _ = _controller(
        qtbot, _settings("none"), UpdateResult(UpdateOutcome.NONE)
    )

    controller.check_now()
    qtbot.waitUntil(lambda: not controller.check_in_progress)

    assert not window.update_dialog.isVisible()
    assert not window.action_shelf.update_button.isVisible()
    assert window.action_shelf.preferences_dialog.updates_status_label.text() == (
        "No update found."
    )


def test_startup_update_opens_the_offer_once_per_session(monkeypatch, qtbot) -> None:
    settings = _settings("startup-offer")
    window, controller, _ = _controller(
        qtbot,
        settings,
        UpdateResult(UpdateOutcome.UPDATE, "0.4.0"),
    )
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(
        "matteloop.ui.update_controller.QTimer.singleShot",
        lambda _delay, callback: callback(),
    )

    controller.start()
    qtbot.waitUntil(lambda: not controller.check_in_progress)

    assert window.update_dialog.isVisible()
    assert not window.action_shelf.update_button.isVisible()
    window.update_dialog.not_now_button.click()
    assert not window.update_dialog.isVisible()
    assert window.action_shelf.update_button.isVisible()

    controller._state_changed(AppState())
    assert not window.update_dialog.isVisible()


def test_startup_update_does_not_open_while_a_job_runs(
    monkeypatch, qtbot, tmp_path: Path
) -> None:
    settings = _settings("startup-offer-running")
    store = _ready_store(tmp_path)
    store.dispatch(RenderRequested("job", "request"))
    window, controller, _ = _controller(
        qtbot,
        settings,
        UpdateResult(UpdateOutcome.UPDATE, "0.4.0"),
        store=store,
    )
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(
        "matteloop.ui.update_controller.QTimer.singleShot",
        lambda _delay, callback: callback(),
    )

    controller.start()
    qtbot.waitUntil(lambda: not controller.check_in_progress)

    assert not window.update_dialog.isVisible()


def test_dismissed_offer_reopens_from_the_arrow(qtbot) -> None:
    window, controller, _ = _controller(
        qtbot,
        _settings("arrow-reopen"),
        UpdateResult(UpdateOutcome.UPDATE, "0.4.0"),
    )

    controller.check_now()
    qtbot.waitUntil(lambda: not controller.check_in_progress)
    window.update_dialog.not_now_button.click()

    assert window.action_shelf.update_button.isVisible()
    window.action_shelf.update_button.click()

    assert window.update_dialog.isVisible()
    assert window.update_dialog.message_label.text() == (
        "MatteLoop 0.4.0 is available."
    )


def test_startup_check_off_suppresses_offer_and_arrow(monkeypatch, qtbot) -> None:
    settings = _settings("startup-offer-disabled")
    settings.setValue("updates/check_on_startup", False)
    window, controller, reader = _controller(
        qtbot,
        settings,
        UpdateResult(UpdateOutcome.UPDATE, "0.4.0"),
    )
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(
        "matteloop.ui.update_controller.QTimer.singleShot",
        lambda _delay, callback: callback(),
    )

    controller.start()

    assert reader.calls == 0
    assert not window.update_dialog.isVisible()
    assert not window.action_shelf.update_button.isVisible()


def test_turning_startup_check_off_before_start_hides_pending_offer(qtbot) -> None:
    settings = _settings("startup-off-before-start")
    manager = _Manager(pending=_UpdateInfo(_Asset("0.4.0")))
    window, controller, _ = _controller(
        qtbot,
        settings,
        UpdateResult(UpdateOutcome.NONE),
        manager=manager,
    )
    settings.setValue("updates/check_on_startup", False)

    controller.start()

    assert not window.update_dialog.isVisible()
    assert not window.action_shelf.update_button.isVisible()


def test_available_update_shows_versioned_offer(qtbot) -> None:
    window, controller, _ = _controller(
        qtbot,
        _settings("update"),
        UpdateResult(UpdateOutcome.UPDATE, "0.4.0", 380 * 1024 * 1024),
    )

    controller.check_now()
    qtbot.waitUntil(lambda: not controller.check_in_progress)

    assert window.update_dialog.isVisible()
    assert window.update_dialog.message_label.text() == "MatteLoop 0.4.0 is available."
    assert window.update_dialog.size_label.text() == "Download size: 380 MB"
    assert window.update_dialog.size_label.isVisible()
    assert window.update_dialog.open_releases_button.text() == "Open releases page"
    assert window.update_dialog.not_now_button.text() == "Not now"
    assert (
        window.action_shelf.preferences_dialog.updates_status_label.text()
        == "MatteLoop 0.4.0 is available."
    )


def test_update_check_uses_the_persisted_channel_and_velopack_version(qtbot) -> None:
    settings = _settings("beta-reader-inputs")
    settings.setValue("updates/channel", "beta")
    manager = _Manager(current_version="0.4.0-beta.1")
    _, controller, reader = _controller(
        qtbot,
        settings,
        UpdateResult(UpdateOutcome.NONE),
        manager=manager,
    )

    controller.check_now()
    qtbot.waitUntil(lambda: not controller.check_in_progress)

    assert reader.requests == [("beta", "0.4.0-beta.1")]


def test_not_now_hides_the_update_offer(qtbot) -> None:
    window, controller, _ = _controller(
        qtbot,
        _settings("dismiss"),
        UpdateResult(UpdateOutcome.UPDATE, "0.4.0"),
    )

    controller.check_now()
    qtbot.waitUntil(lambda: not controller.check_in_progress)
    window.update_dialog.not_now_button.click()

    assert not window.update_dialog.isVisible()


def test_update_offer_omits_unknown_download_size(qtbot) -> None:
    window, controller, _ = _controller(
        qtbot,
        _settings("unknown-size"),
        UpdateResult(UpdateOutcome.UPDATE, "0.4.0"),
    )

    controller.check_now()
    qtbot.waitUntil(lambda: not controller.check_in_progress)

    assert not window.update_dialog.size_label.isVisible()
    assert window.update_dialog.size_label.text() == ""


def test_manual_check_reports_a_failed_reader(qtbot) -> None:
    window, controller, _ = _controller(
        qtbot, _settings("failed"), UpdateResult(UpdateOutcome.FAILED)
    )

    controller.check_now()
    qtbot.waitUntil(lambda: not controller.check_in_progress)

    assert not window.update_dialog.isVisible()
    assert window.action_shelf.preferences_dialog.updates_status_label.text() == (
        "Couldn’t check for updates. Try again later."
    )


def test_startup_check_requires_the_setting_and_a_frozen_runtime(
    monkeypatch, qtbot
) -> None:
    settings = _settings("startup-gates")
    settings.setValue("updates/check_on_startup", False)
    window, controller, reader = _controller(
        qtbot, settings, UpdateResult(UpdateOutcome.NONE)
    )
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    controller.start()

    assert reader.calls == 0

    settings.setValue("updates/check_on_startup", True)
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    window2, controller2, reader2 = _controller(
        qtbot, settings, UpdateResult(UpdateOutcome.NONE)
    )
    controller2.start()

    assert reader2.calls == 0
    del window, window2


def test_startup_check_runs_in_a_frozen_runtime_when_enabled(
    monkeypatch, qtbot
) -> None:
    settings = _settings("startup-enabled")
    settings.setValue("updates/check_on_startup", True)
    window, controller, reader = _controller(
        qtbot, settings, UpdateResult(UpdateOutcome.NONE)
    )
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(
        "matteloop.ui.update_controller.QTimer.singleShot",
        lambda _delay, callback: callback(),
    )

    controller.start()
    qtbot.waitUntil(lambda: not controller.check_in_progress)

    assert reader.calls == 1
    del window


def test_manual_check_works_when_running_from_source(qtbot) -> None:
    settings = _settings("manual-source")
    window, controller, reader = _controller(
        qtbot, settings, UpdateResult(UpdateOutcome.NONE)
    )

    controller.check_now()
    qtbot.waitUntil(lambda: not controller.check_in_progress)

    assert reader.calls == 1
    del window


def test_open_releases_page_uses_the_release_url(monkeypatch, qtbot) -> None:
    window, controller, _ = _controller(
        qtbot,
        _settings("open-url"),
        UpdateResult(UpdateOutcome.UPDATE, "0.4.0"),
    )
    opened: list[QUrl] = []
    monkeypatch.setattr(QDesktopServices, "openUrl", opened.append)

    controller.check_now()
    qtbot.waitUntil(lambda: not controller.check_in_progress)
    window.update_dialog.open_releases_button.click()

    assert opened == [QUrl("https://github.com/smb-org/MatteLoop/releases")]
    del window


def test_download_progress_reaches_the_offer_and_finishes_ready(
    monkeypatch, qtbot, tmp_path: Path
) -> None:
    transport, asset = _download_transport()
    transport.responses[update_package_url("0.4.0", asset.FileName)] = FakeResponse(
        b"package", on_read=lambda: time.sleep(0.25)
    )
    info = _UpdateInfo(asset)
    manager = _Manager(pending_values=[None, info])
    monkeypatch.setattr(
        "matteloop.ui.update_controller.cache_subdirectory", lambda *_: tmp_path
    )
    window, controller, _ = _controller(
        qtbot,
        _settings("download-progress"),
        UpdateResult(UpdateOutcome.UPDATE, "0.4.0"),
        manager=manager,
        transport=transport,
    )

    controller.check_now()
    qtbot.waitUntil(lambda: not controller.check_in_progress)
    window.update_dialog.download_button.click()
    qtbot.waitUntil(
        lambda: window.update_dialog.message_label.text()
        == "Downloading MatteLoop 0.4.0 (0 %)…"
    )
    qtbot.waitUntil(lambda: not controller.download_in_progress)

    assert window.update_dialog.message_label.text() == (
        "MatteLoop 0.4.0 is ready to install."
    )
    assert window.update_dialog.install_button.isVisible()


def test_failed_download_offers_try_again(monkeypatch, qtbot, tmp_path: Path) -> None:
    transport, asset = _download_transport(package_response=OSError("offline"))
    info = _UpdateInfo(asset)
    manager = _Manager(pending_values=[None, info])
    monkeypatch.setattr(
        "matteloop.ui.update_controller.cache_subdirectory", lambda *_: tmp_path
    )
    window, controller, _ = _controller(
        qtbot,
        _settings("download-failed"),
        UpdateResult(UpdateOutcome.UPDATE, "0.4.0"),
        manager=manager,
        transport=transport,
    )

    controller.check_now()
    qtbot.waitUntil(lambda: not controller.check_in_progress)
    window.update_dialog.download_button.click()
    qtbot.waitUntil(lambda: not controller.download_in_progress)

    assert window.update_dialog.message_label.text() == (
        "The update couldn’t be downloaded."
    )
    assert window.update_dialog.download_button.text() == "Try again"
    assert window.update_dialog.open_releases_button.isVisible()
    window.update_dialog.close()
    qtbot.waitUntil(lambda: window.action_shelf.update_button.isVisible())
    window.action_shelf.update_button.click()
    assert window.update_dialog.isVisible()
    assert window.update_dialog.message_label.text() == (
        "The update couldn’t be downloaded."
    )


def test_downloading_offer_reopens_from_the_arrow(
    monkeypatch, qtbot, tmp_path: Path
) -> None:
    transport, asset = _download_transport()
    transport.responses[update_package_url("0.4.0", asset.FileName)] = FakeResponse(
        b"package", on_read=lambda: time.sleep(0.25)
    )
    manager = _Manager(pending_values=[None, _UpdateInfo(asset)])
    monkeypatch.setattr(
        "matteloop.ui.update_controller.cache_subdirectory", lambda *_: tmp_path
    )
    window, controller, _ = _controller(
        qtbot,
        _settings("downloading-arrow"),
        UpdateResult(UpdateOutcome.UPDATE, "0.4.0"),
        manager=manager,
        transport=transport,
    )

    controller.check_now()
    qtbot.waitUntil(lambda: not controller.check_in_progress)
    window.update_dialog.download_button.click()
    qtbot.waitUntil(
        lambda: window.update_dialog.message_label.text()
        == "Downloading MatteLoop 0.4.0 (0 %)…"
    )
    window.update_dialog.close()
    qtbot.waitUntil(lambda: window.action_shelf.update_button.isVisible())
    window.action_shelf.update_button.click()

    assert window.update_dialog.isVisible()
    assert window.update_dialog.message_label.text() == (
        "Downloading MatteLoop 0.4.0 (0 %)…"
    )
    window.update_dialog.download_button.click()
    qtbot.waitUntil(lambda: not controller.download_in_progress)


def test_self_check_rejecting_the_package_offers_try_again(
    monkeypatch, qtbot, tmp_path: Path
) -> None:
    transport, asset = _download_transport()
    manager = _Manager(
        pending_values=[None, _UpdateInfo(_Asset("0.3.9", asset.FileName))]
    )
    monkeypatch.setattr(
        "matteloop.ui.update_controller.cache_subdirectory", lambda *_: tmp_path
    )
    window, controller, _ = _controller(
        qtbot,
        _settings("download-none"),
        UpdateResult(UpdateOutcome.UPDATE, "0.4.0"),
        manager=manager,
        transport=transport,
    )

    controller.check_now()
    qtbot.waitUntil(lambda: not controller.check_in_progress)
    window.update_dialog.download_button.click()
    qtbot.waitUntil(lambda: not controller.download_in_progress)

    assert window.update_dialog.message_label.text() == (
        "The update couldn’t be downloaded."
    )
    assert not list(tmp_path.glob("*.nupkg"))


def test_cancel_stops_the_transport_and_leaves_no_package(
    monkeypatch, qtbot, tmp_path: Path
) -> None:
    transport, asset = _download_transport()
    transport.responses[update_package_url("0.4.0", asset.FileName)] = FakeResponse(
        b"package", on_read=lambda: time.sleep(0.25)
    )
    manager = _Manager(pending_values=[None, _UpdateInfo(asset)])
    monkeypatch.setattr(
        "matteloop.ui.update_controller.cache_subdirectory", lambda *_: tmp_path
    )
    window, controller, _ = _controller(
        qtbot,
        _settings("download-cancelled"),
        UpdateResult(UpdateOutcome.UPDATE, "0.4.0"),
        manager=manager,
        transport=transport,
    )

    controller.check_now()
    qtbot.waitUntil(lambda: not controller.check_in_progress)
    window.update_dialog.download_button.click()
    qtbot.waitUntil(
        lambda: window.update_dialog.message_label.text()
        == "Downloading MatteLoop 0.4.0 (0 %)…"
    )
    window.update_dialog.download_button.click()
    qtbot.waitUntil(lambda: not controller.download_in_progress)

    assert window.update_dialog.message_label.text() == (
        "MatteLoop 0.4.0 is available."
    )
    assert not list(tmp_path.glob("*.nupkg"))


def _ready_store(tmp_path: Path) -> ReducerStore:
    source = tmp_path / "clip.mp4"
    store = ReducerStore()
    loading = reduce(store.state, SourceLoadRequested("source", "load"))
    ready = reduce(loading, SourceLoaded("source", "load", _Metadata(source)))
    return ReducerStore(ready)


def test_install_button_tracks_idle_state_and_handler_reads_it_again(
    qtbot, tmp_path: Path
) -> None:
    info = _UpdateInfo(_Asset("0.4.0"))
    store = _ready_store(tmp_path)
    manager = _Manager(pending=info)
    window, controller, _ = _controller(
        qtbot,
        _settings("install-idle"),
        UpdateResult(UpdateOutcome.NONE),
        manager=manager,
        store=store,
    )

    store.dispatch(RenderRequested("job", "request"))
    assert not window.update_dialog.install_button.isEnabled()
    controller.install_and_restart()
    assert manager.apply_calls == []
    store.dispatch(CancelRequested("job"))
    store.dispatch(CancelAcknowledged("job"))
    assert window.update_dialog.install_button.isEnabled()


def test_refused_close_drops_pending_install_and_returns_to_ready(qtbot) -> None:
    class _RejectingServices(_Services):
        def confirm_discard_unsaved_transform(self, _parent: object) -> bool:
            return False

    info = _UpdateInfo(_Asset("0.4.0"))
    manager = _Manager(pending=info)
    settings = _settings("install-refused")
    store = ReducerStore(AppState())
    window = MainWindow(store, _RejectingServices(), settings)
    qtbot.addWidget(window)
    window.show()
    UpdateController(
        window,
        settings,
        _Reader(UpdateResult(UpdateOutcome.NONE)),
        manager=manager,
    )

    window.update_dialog.install_button.click()

    assert manager.apply_calls == []
    assert window.update_dialog.message_label.text() == (
        "MatteLoop 0.4.0 is ready to install."
    )
    assert window.update_dialog.install_button.isEnabled()


def test_about_to_quit_arms_once_and_only_for_a_pending_install(qtbot) -> None:
    info = _UpdateInfo(_Asset("0.4.0"))
    manager = _Manager(pending=info)
    window, controller, _ = _controller(
        qtbot,
        _settings("arm-install"),
        UpdateResult(UpdateOutcome.NONE),
        manager=manager,
    )

    assert window.update_dialog.message_label.text() == (
        "MatteLoop 0.4.0 is ready to install."
    )
    controller.arm_pending_install()
    assert manager.apply_calls == []
    controller.install_and_restart()
    controller.arm_pending_install()
    controller.arm_pending_install()

    assert manager.apply_calls == [(info, False, True)]


def test_translocated_install_uses_releases_page(monkeypatch, qtbot) -> None:
    monkeypatch.setattr(sys, "executable", "/tmp/AppTranslocation/a/d/app")
    manager = _Manager()
    window, controller, _ = _controller(
        qtbot,
        _settings("translocated"),
        UpdateResult(UpdateOutcome.UPDATE, "0.4.0"),
        manager=manager,
    )

    controller.check_now()
    qtbot.waitUntil(lambda: not controller.check_in_progress)

    assert window.update_dialog.open_releases_button.isVisible()
    assert not window.update_dialog.download_button.isVisible()


def test_non_writable_install_uses_releases_page(monkeypatch, qtbot) -> None:
    monkeypatch.setattr(os, "access", lambda _path, _mode: False)
    manager = _Manager()
    window, controller, _ = _controller(
        qtbot,
        _settings("non-writable"),
        UpdateResult(UpdateOutcome.UPDATE, "0.4.0"),
        manager=manager,
    )

    controller.check_now()
    qtbot.waitUntil(lambda: not controller.check_in_progress)

    assert window.update_dialog.open_releases_button.isVisible()
    assert not window.update_dialog.download_button.isVisible()


def test_repository_environment_overrides_the_explicit_velopack_source(
    monkeypatch, tmp_path: Path
) -> None:
    bundle = tmp_path / "MatteLoop.app"
    executable = bundle / "Contents" / "MacOS" / "MatteLoop"
    (bundle / "Contents" / "MacOS").mkdir(parents=True)
    (bundle / "Contents" / "Resources").mkdir()
    (bundle / "Contents" / "MacOS" / "UpdateMac").touch()
    (bundle / "Contents" / "Resources" / "sq.version").touch()

    class _FakeSdkManager:
        def __init__(self, source: object, *, locator: object) -> None:
            self.source = source
            self.locator = locator

    class _FakeLocator:
        def __init__(self, *values: object) -> None:
            (
                self.RootAppDir,
                self.UpdateExePath,
                self.PackagesDir,
                self.ManifestPath,
                self.CurrentBinaryDir,
                self.IsPortable,
            ) = values

    fake_velopack = ModuleType("velopack")
    fake_velopack.VelopackLocatorConfig = _FakeLocator  # type: ignore[attr-defined]
    fake_velopack.UpdateManager = _FakeSdkManager  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "velopack", fake_velopack)
    monkeypatch.setenv("MATTELOOP_UPDATE_REPO", "qualification/MatteLoop")
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(sys, "executable", str(executable))
    monkeypatch.setattr(
        "matteloop.ui.update_controller.cache_subdirectory",
        lambda *_: tmp_path / "cache" / "updates",
    )

    manager = _create_update_manager()

    assert isinstance(manager, _FakeSdkManager)
    assert manager.source == "https://github.com/qualification/MatteLoop"
    assert manager.locator.RootAppDir == bundle
    assert manager.locator.UpdateExePath == bundle / "Contents" / "MacOS" / "UpdateMac"
    assert manager.locator.ManifestPath == (
        bundle / "Contents" / "Resources" / "sq.version"
    )
    assert manager.locator.PackagesDir == tmp_path / "cache" / "updates"


def test_windows_locator_uses_the_current_directory_and_portable_marker(
    monkeypatch, tmp_path: Path
) -> None:
    class _FakeLocator:
        def __init__(self, *values: object) -> None:
            (
                self.RootAppDir,
                self.UpdateExePath,
                self.PackagesDir,
                self.ManifestPath,
                self.CurrentBinaryDir,
                self.IsPortable,
            ) = values

    root = PureWindowsPath(r"C:\Users\tester\AppData\Local\io.github.smb-org.matteloop")
    executable = root / "current" / "matteloop.exe"
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(
        "matteloop.ui.update_controller.cache_subdirectory",
        lambda *_: tmp_path / "updates",
    )

    locator = _locator_config(executable, _FakeLocator)

    assert locator.RootAppDir == root
    assert locator.UpdateExePath == root / "Update.exe"
    assert locator.ManifestPath == root / "current" / "sq.version"
    assert locator.CurrentBinaryDir == root / "current"
    assert locator.PackagesDir == tmp_path / "updates"
    assert locator.IsPortable is False
    # Velopack probes the directory by writing into it and silently falls back
    # to its own when that fails, so it has to exist before the manager sees it.
    assert locator.PackagesDir.is_dir()


def test_startup_sweep_keeps_only_the_pending_package(
    monkeypatch, tmp_path: Path
) -> None:
    pending = _UpdateInfo(
        _Asset("0.4.0", "io.github.smb-org.matteloop-0.4.0-osx-arm64-full.nupkg")
    )
    kept = tmp_path / pending.TargetFullRelease.FileName
    stale = tmp_path / "io.github.smb-org.matteloop-0.3.9-osx-arm64-full.nupkg"
    partial = tmp_path / f"{kept.name}.part"
    for path in (kept, stale, partial):
        path.write_bytes(b"package")
    monkeypatch.setattr(
        "matteloop.ui.update_controller.cache_subdirectory", lambda *_: tmp_path
    )

    assert _pending_update(_Manager(pending=pending)) is pending
    assert kept.exists()
    assert not stale.exists()
    assert not partial.exists()
