from __future__ import annotations

import os
import sys
import threading
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from types import ModuleType

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
from matteloop.ui.update_controller import UpdateController, _create_update_manager
from matteloop.updates import UpdateOutcome, UpdateResult


@dataclass(frozen=True)
class _Metadata:
    path: Path
    width: int = 640
    height: int = 360
    duration: Fraction = Fraction(4)


@dataclass(frozen=True)
class _Asset:
    Version: str


@dataclass(frozen=True)
class _UpdateInfo:
    TargetFullRelease: _Asset


class _Manager:
    def __init__(
        self,
        update: object | None = None,
        *,
        pending: object | None = None,
        download_error: BaseException | None = None,
        block_download: bool = False,
    ) -> None:
        self.update = update
        self.pending = pending
        self.download_error = download_error
        self.block_download = block_download
        self.progress_seen = threading.Event()
        self.release_download = threading.Event()
        self.download_calls: list[object] = []
        self.apply_calls: list[tuple[object, bool, bool]] = []

    def check_for_updates(self) -> object | None:
        return self.update

    def download_updates(self, update: object, progress_callback: object) -> None:
        self.download_calls.append(update)
        if self.download_error is not None:
            raise self.download_error
        assert callable(progress_callback)
        progress_callback(37, 100)
        self.progress_seen.set()
        if self.block_download:
            self.release_download.wait(2)

    def get_update_pending_restart(self) -> object | None:
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


class _Services:
    def dispatch(self, _command: object) -> None:
        return

    def confirm_discard_unsaved_transform(self, _parent: object) -> bool:
        return True


class _Reader:
    def __init__(self, result: UpdateResult) -> None:
        self.result = result
        self.calls = 0

    def check(self) -> UpdateResult:
        self.calls += 1
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
) -> tuple[MainWindow, UpdateController, _Reader]:
    if store is None:
        store = ReducerStore(AppState())
    window = MainWindow(store, _Services(), settings)
    qtbot.addWidget(window)
    window.show()
    reader = _Reader(result)
    controller = UpdateController(window, settings, reader, manager=manager)
    return window, controller, reader


def test_no_update_keeps_the_banner_hidden(qtbot) -> None:
    window, controller, _ = _controller(
        qtbot, _settings("none"), UpdateResult(UpdateOutcome.NONE)
    )

    controller.check_now()
    qtbot.waitUntil(lambda: not controller.check_in_progress)

    assert not window.update_container.isVisible()
    assert window.action_shelf.preferences_dialog.updates_status_label.text() == (
        "No update found."
    )


def test_available_update_shows_versioned_banner(qtbot) -> None:
    window, controller, _ = _controller(
        qtbot,
        _settings("update"),
        UpdateResult(UpdateOutcome.UPDATE, "0.4.0"),
    )

    controller.check_now()
    qtbot.waitUntil(lambda: not controller.check_in_progress)

    assert window.update_container.isVisible()
    assert window.update_banner.text() == "MatteLoop 0.4.0 is available."
    assert window.update_open_releases_button.text() == "Open releases page"
    assert window.update_not_now_button.text() == "Not now"
    assert (
        window.action_shelf.preferences_dialog.updates_status_label.text()
        == "MatteLoop 0.4.0 is available."
    )


def test_not_now_hides_the_update_banner(qtbot) -> None:
    window, controller, _ = _controller(
        qtbot,
        _settings("dismiss"),
        UpdateResult(UpdateOutcome.UPDATE, "0.4.0"),
    )

    controller.check_now()
    qtbot.waitUntil(lambda: not controller.check_in_progress)
    window.update_not_now_button.click()

    assert not window.update_container.isVisible()


def test_manual_check_reports_a_failed_reader(qtbot) -> None:
    window, controller, _ = _controller(
        qtbot, _settings("failed"), UpdateResult(UpdateOutcome.FAILED)
    )

    controller.check_now()
    qtbot.waitUntil(lambda: not controller.check_in_progress)

    assert not window.update_container.isVisible()
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
    window.update_open_releases_button.click()

    assert opened == [QUrl("https://github.com/smb-org/MatteLoop/releases")]
    del window


def test_download_progress_reaches_the_banner_and_finishes_ready(qtbot) -> None:
    info = _UpdateInfo(_Asset("0.4.0"))
    manager = _Manager(info, block_download=True)
    window, controller, _ = _controller(
        qtbot,
        _settings("download-progress"),
        UpdateResult(UpdateOutcome.UPDATE, "0.4.0"),
        manager=manager,
    )

    controller.check_now()
    qtbot.waitUntil(lambda: not controller.check_in_progress)
    window.update_download_button.click()
    qtbot.waitUntil(manager.progress_seen.is_set)
    qtbot.waitUntil(
        lambda: window.update_banner.text()
        == "Downloading MatteLoop 0.4.0 (37 %)…"
    )

    manager.release_download.set()
    qtbot.waitUntil(lambda: not controller.download_in_progress)

    assert window.update_banner.text() == "MatteLoop 0.4.0 is ready to install."
    assert window.update_install_button.isVisible()


def test_failed_download_offers_try_again(qtbot) -> None:
    info = _UpdateInfo(_Asset("0.4.0"))
    manager = _Manager(info, download_error=OSError("offline"))
    window, controller, _ = _controller(
        qtbot,
        _settings("download-failed"),
        UpdateResult(UpdateOutcome.UPDATE, "0.4.0"),
        manager=manager,
    )

    controller.check_now()
    qtbot.waitUntil(lambda: not controller.check_in_progress)
    window.update_download_button.click()
    qtbot.waitUntil(lambda: not controller.download_in_progress)

    assert window.update_banner.text() == "The update couldn’t be downloaded."
    assert window.update_download_button.text() == "Try again"
    assert window.update_open_releases_button.isVisible()


def test_none_from_sdk_check_is_a_failed_download(qtbot) -> None:
    manager = _Manager(None)
    window, controller, _ = _controller(
        qtbot,
        _settings("download-none"),
        UpdateResult(UpdateOutcome.UPDATE, "0.4.0"),
        manager=manager,
    )

    controller.check_now()
    qtbot.waitUntil(lambda: not controller.check_in_progress)
    window.update_download_button.click()
    qtbot.waitUntil(lambda: not controller.download_in_progress)

    assert window.update_banner.text() == "The update couldn’t be downloaded."


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
    assert not window.update_install_button.isEnabled()
    controller.install_and_restart()
    assert manager.apply_calls == []
    store.dispatch(CancelRequested("job"))
    store.dispatch(CancelAcknowledged("job"))
    assert window.update_install_button.isEnabled()


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

    window.update_install_button.click()

    assert manager.apply_calls == []
    assert window.update_banner.text() == "MatteLoop 0.4.0 is ready to install."
    assert window.update_install_button.isEnabled()


def test_about_to_quit_arms_once_and_only_for_a_pending_install(qtbot) -> None:
    info = _UpdateInfo(_Asset("0.4.0"))
    manager = _Manager(pending=info)
    window, controller, _ = _controller(
        qtbot,
        _settings("arm-install"),
        UpdateResult(UpdateOutcome.NONE),
        manager=manager,
    )

    assert window.update_banner.text() == "MatteLoop 0.4.0 is ready to install."
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

    assert window.update_open_releases_button.isVisible()
    assert not window.update_download_button.isVisible()


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

    assert window.update_open_releases_button.isVisible()
    assert not window.update_download_button.isVisible()


def test_repository_environment_overrides_the_velopack_source(monkeypatch) -> None:
    class _FakeSdkManager:
        def __init__(self, source: object) -> None:
            self.source = source

    fake_velopack = ModuleType("velopack")
    fake_velopack.GithubSource = lambda url: url  # type: ignore[attr-defined]
    fake_velopack.UpdateManager = _FakeSdkManager  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "velopack", fake_velopack)
    monkeypatch.setenv("MATTELOOP_UPDATE_REPO", "qualification/MatteLoop")

    manager = _create_update_manager()

    assert isinstance(manager, _FakeSdkManager)
    assert manager.source == "https://github.com/qualification/MatteLoop"
