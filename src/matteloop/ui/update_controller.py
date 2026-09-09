"""Controller for the in-app release notice, download, and install flow."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path, PurePath, PureWindowsPath
from threading import Event
from typing import TYPE_CHECKING, Any, Protocol, cast

from PySide6.QtCore import (
    QCoreApplication,
    QObject,
    QSettings,
    QThread,
    QTimer,
    QUrl,
    Signal,
    Slot,
)
from PySide6.QtGui import QDesktopServices

from matteloop.core.state import AppState, JobState
from matteloop.jobs.models.download import DownloadTransport
from matteloop.paths import cache_subdirectory
from matteloop.ui.ports import StateStore
from matteloop.ui.preferences import load_check_on_startup
from matteloop.ui.worker_thread import WorkerThread
from matteloop.updates import (
    UpdateDownloadCancelled,
    UpdateOutcome,
    UpdateResult,
    download_update,
    releases_url,
    update_repository_url,
)

if TYPE_CHECKING:
    from matteloop.ui.main_window import MainWindow

_LOGGER = logging.getLogger(__name__)
_STARTUP_DELAY_MS = 3000
_DOWNLOAD_SHUTDOWN_TIMEOUT_MS = 5000


class UpdateReader(Protocol):
    def check(self) -> UpdateResult: ...


class UpdateManager(Protocol):
    def get_update_pending_restart(self) -> object | None: ...

    def wait_exit_then_apply_updates(
        self,
        update: object,
        silent: bool = False,
        restart: bool = True,
        restart_args: object | None = None,
    ) -> None: ...


class _UpdateWorker(QObject):
    result = Signal(object)

    def __init__(self, reader: UpdateReader) -> None:
        super().__init__()
        self._reader = reader

    def run(self) -> None:
        try:
            result = self._reader.check()
        except Exception as error:
            _LOGGER.info("Update check failed: %s", error)
            result = UpdateResult(UpdateOutcome.FAILED)
        self.result.emit(result)


class _DownloadWorker(QObject):
    progress = Signal(int, int)
    succeeded = Signal(object)
    failed = Signal(object)
    cancelled = Signal()

    def __init__(
        self,
        transport: DownloadTransport,
        manager: UpdateManager,
        version: str,
        cancellation: Event,
    ) -> None:
        super().__init__()
        self._transport = transport
        self._manager = manager
        self._version = version
        self._cancellation = cancellation

    def run(self) -> None:
        package: Path | None = None
        try:
            package = download_update(
                self._transport,
                self._version,
                cache_subdirectory("updates"),
                self.progress.emit,
                self._cancellation.is_set,
                platform=sys.platform,
            )
            pending = self._manager.get_update_pending_restart()
            if _update_version(pending, None) != self._version:
                _remove_package(package)
                raise RuntimeError(
                    "Velopack did not report the downloaded package as pending"
                )
        except Exception as error:
            if isinstance(error, UpdateDownloadCancelled):
                self.cancelled.emit()
            else:
                if package is not None:
                    _remove_package(package)
                self.failed.emit(error)
        else:
            self.succeeded.emit(pending)


class UpdateController(QObject):
    """Run release work away from Qt's GUI thread and drive update widgets."""

    def __init__(
        self,
        window: MainWindow,
        settings: QSettings,
        reader: UpdateReader,
        parent: QObject | None = None,
        *,
        manager: UpdateManager | None = None,
        store: StateStore | None = None,
        transport: DownloadTransport | None = None,
    ) -> None:
        super().__init__(parent if parent is not None else window)
        self._window = window
        self._settings = settings
        self._reader = reader
        self._store = (
            store
            if store is not None
            else cast(StateStore, getattr(window, "_store"))
        )
        self._manager = manager if manager is not None else _create_update_manager()
        self._transport = transport
        self._self_update_advisable = _advisory_update_capability(self._manager)
        self._check_thread: QThread | None = None
        self._download_thread: WorkerThread | None = None
        self._download_worker: _DownloadWorker | None = None
        self._download_cancellation: Event | None = None
        self._startup_scheduled = False
        self._startup_check_active = False
        self._banner_dismissed = False
        self._available_version: str | None = None
        self._ready_update: object | None = _pending_update(self._manager)
        self._pending_install: object | None = None
        self._apply_armed = False
        self._store_unsubscribe = self._store.subscribe(self._state_changed)
        preferences = window.action_shelf.preferences_dialog
        preferences.check_for_updates_button.clicked.connect(self.check_now)
        window.update_download_button.clicked.connect(self._download_button_clicked)
        window.update_install_button.clicked.connect(self.install_and_restart)
        window.update_open_releases_button.clicked.connect(self.open_releases_page)
        window.update_not_now_button.clicked.connect(self.dismiss_banner)
        window.update_later_button.clicked.connect(self.dismiss_banner)
        self._available_version = _update_version(self._ready_update, None)
        if self._ready_update is not None and self._available_version:
            self._show_ready(self._available_version)
        else:
            self._refresh_install_button()

    @property
    def check_in_progress(self) -> bool:
        return self._check_thread is not None or self._download_thread is not None

    @property
    def download_in_progress(self) -> bool:
        return self._download_thread is not None

    def start(self) -> None:
        """Schedule the one optional startup check after the window is shown."""
        if self._startup_scheduled:
            return
        self._startup_scheduled = True
        if load_check_on_startup(self._settings) and _is_frozen_runtime():
            QTimer.singleShot(_STARTUP_DELAY_MS, self._startup_check)

    @Slot()
    def check_now(self) -> None:
        """Start a manual check, including when MatteLoop runs from source."""
        self._start_check(startup=False)

    @Slot()
    def _startup_check(self) -> None:
        self._start_check(startup=True)

    def _start_check(self, *, startup: bool) -> None:
        if self.check_in_progress:
            return
        self._startup_check_active = startup
        if not startup:
            self._set_status(
                QCoreApplication.translate("SettingsDialog", "Checking for updates…")
            )
        worker = _UpdateWorker(self._reader)
        thread = WorkerThread(worker, self)
        self._check_thread = thread
        worker.result.connect(self._check_finished)
        thread.finished.connect(self._check_thread_finished)
        thread.start()

    @Slot(object)
    def _check_finished(self, value: object) -> None:
        result = value if isinstance(value, UpdateResult) else UpdateResult(
            UpdateOutcome.FAILED
        )
        startup = self._startup_check_active
        if result.outcome is UpdateOutcome.UPDATE and result.version:
            if self._ready_update is None:
                self._available_version = result.version
                message = self._available_message(result.version)
                self._set_status(message)
                self._show_available(message)
            else:
                ready_version = _update_version(
                    self._ready_update, self._available_version
                )
                if ready_version:
                    self._available_version = ready_version
                    self._show_ready(ready_version)
        elif result.outcome is UpdateOutcome.NONE:
            self._set_status(
                QCoreApplication.translate("SettingsDialog", "No update found.")
            )
            if self._ready_update is None:
                self._hide_banner()
        else:
            if startup:
                _LOGGER.info("Startup update check failed")
            else:
                self._set_status(
                    QCoreApplication.translate(
                        "SettingsDialog",
                        "Couldn’t check for updates. Try again later.",
                    )
                )
            if self._ready_update is None:
                self._hide_banner()
        thread = self._check_thread
        if thread is not None:
            thread.quit()

    @Slot()
    def _check_thread_finished(self) -> None:
        self._check_thread = None

    @Slot()
    def start_download(self) -> None:
        """Download the update package only after the user asks for it."""
        if self.check_in_progress:
            return
        if (
            self._manager is None
            or self._transport is None
            or not self._self_update_advisable
        ):
            self.open_releases_page()
            return
        version = self._available_version
        if not version:
            return
        self._banner_dismissed = False
        self._show_downloading(version, 0)
        cancellation = Event()
        worker = _DownloadWorker(
            self._transport, self._manager, version, cancellation
        )
        self._download_worker = worker
        self._download_cancellation = cancellation
        thread = WorkerThread(worker, self)
        self._download_thread = thread
        worker.progress.connect(self._download_progress)
        worker.succeeded.connect(self._download_succeeded)
        worker.failed.connect(self._download_failed)
        worker.cancelled.connect(self._download_cancelled)
        thread.finished.connect(self._download_thread_finished)
        thread.start()

    @Slot()
    def cancel_download(self) -> None:
        """Request cancellation and let the transport observe the flag."""
        if self._download_cancellation is not None:
            self._download_cancellation.set()
        thread = self._download_thread
        if thread is not None:
            thread.quit()
            if not thread.wait(_DOWNLOAD_SHUTDOWN_TIMEOUT_MS):
                _LOGGER.warning("MatteLoop update download did not stop after cancel")

    @Slot()
    def _download_button_clicked(self) -> None:
        if self.download_in_progress:
            self.cancel_download()
        else:
            self.start_download()

    @Slot(int, int)
    def _download_progress(self, completed: int, total: int) -> None:
        if self._available_version is None:
            return
        percent = 0 if total <= 0 else min(100, max(0, completed * 100 // total))
        self._show_downloading(self._available_version, percent)

    @Slot(object)
    def _download_succeeded(self, update: object) -> None:
        self._ready_update = update
        self._available_version = _update_version(update, self._available_version)
        if self._available_version:
            self._set_status(self._ready_message(self._available_version))
            self._show_ready(self._available_version)
        self._finish_download_thread()

    @Slot(object)
    def _download_failed(self, error: object) -> None:
        _LOGGER.warning("Update download failed: %s", error)
        self._ready_update = None
        self._show_failed()
        self._finish_download_thread()

    @Slot()
    def _download_cancelled(self) -> None:
        if self._available_version:
            self._show_available(self._available_message(self._available_version))
        self._finish_download_thread()

    def _finish_download_thread(self) -> None:
        thread = self._download_thread
        if thread is not None:
            thread.quit()

    @Slot()
    def _download_thread_finished(self) -> None:
        self._download_thread = None
        self._download_worker = None
        self._download_cancellation = None

    @Slot()
    def install_and_restart(self) -> None:
        """Ask the existing close path to quit with the downloaded update."""
        if self._ready_update is None:
            return
        if self._store.state.job.phase is not JobState.IDLE:
            self._refresh_install_button()
            return
        self._pending_install = self._ready_update
        if not self._window.close():
            self._pending_install = None
            version = _update_version(self._ready_update, self._available_version)
            if version:
                self._show_ready(version)

    @Slot()
    def arm_pending_install(self) -> None:
        """Arm Velopack once, and only for an install requested before quit."""
        pending, self._pending_install = self._pending_install, None
        if pending is None or self._manager is None or self._apply_armed:
            return
        self._apply_armed = True
        try:
            # Velopack 1.2.0 exposes these arguments positionally only.
            self._manager.wait_exit_then_apply_updates(pending, False, True)
        except Exception as error:
            _LOGGER.warning("MatteLoop update could not be armed: %s", error)

    @Slot()
    def shutdown(self) -> None:
        """Cancel an update download and wait briefly for its worker to stop."""
        self.cancel_download()

    @Slot()
    def dismiss_banner(self) -> None:
        self._banner_dismissed = True
        self._hide_banner()

    @Slot()
    def open_releases_page(self) -> None:
        QDesktopServices.openUrl(QUrl(releases_url()))

    def _state_changed(self, _state: AppState) -> None:
        self._refresh_install_button()

    def _refresh_install_button(self) -> None:
        enabled = (
            self._ready_update is not None
            and self._store.state.job.phase is JobState.IDLE
        )
        self._window.update_install_button.setEnabled(enabled)

    def _show_available(self, message: str) -> None:
        self._set_banner(message, "available")

    def _show_downloading(self, version: str, percent: int) -> None:
        self._set_banner(self._downloading_message(version, percent), "downloading")

    def _show_ready(self, version: str) -> None:
        self._set_banner(self._ready_message(version), "ready")

    def _show_failed(self) -> None:
        self._set_banner(
            QCoreApplication.translate(
                "UpdateBanner", "The update couldn’t be downloaded."
            ),
            "failed",
        )

    def _set_banner(self, message: str, state: str) -> None:
        if self._banner_dismissed:
            return
        self._window.update_banner.setText(message)
        self._window.update_banner.setAccessibleDescription(message)
        self._window.update_container.show()
        self._window.update_download_button.setVisible(
            state == "downloading"
            or (state == "available" and self._self_update_advisable)
            or state == "failed"
        )
        self._window.update_open_releases_button.setVisible(
            (state == "available" and not self._self_update_advisable)
            or state == "failed"
        )
        self._window.update_not_now_button.setVisible(state == "available")
        self._window.update_install_button.setVisible(state == "ready")
        self._window.update_later_button.setVisible(state == "ready")
        if state == "downloading":
            download_copy = QCoreApplication.translate("UpdateBanner", "Cancel")
        elif state == "failed":
            download_copy = QCoreApplication.translate("UpdateBanner", "Try again")
        else:
            download_copy = QCoreApplication.translate(
                "UpdateBanner", "Download update"
            )
        self._window.update_download_button.setText(download_copy)
        self._window.update_download_button.setAccessibleName(download_copy)
        self._window.update_install_button.setText(
            QCoreApplication.translate("UpdateBanner", "Install and restart")
        )
        self._window.update_later_button.setText(
            QCoreApplication.translate("UpdateBanner", "Later")
        )
        self._refresh_install_button()

    def _hide_banner(self) -> None:
        self._window.update_container.hide()
        self._refresh_install_button()

    def _available_message(self, version: str) -> str:
        template = QCoreApplication.translate(
            "UpdateBanner", "MatteLoop %1 is available."
        )
        return template.replace("%1", version)

    def _downloading_message(self, version: str, percent: int) -> str:
        template = QCoreApplication.translate(
            "UpdateBanner", "Downloading MatteLoop %1 (%2 %)…"
        )
        return template.replace("%1", version).replace("%2", str(percent))

    def _ready_message(self, version: str) -> str:
        template = QCoreApplication.translate(
            "UpdateBanner", "MatteLoop %1 is ready to install."
        )
        return template.replace("%1", version)

    def _set_status(self, message: str) -> None:
        self._window.action_shelf.preferences_dialog.updates_status_label.setText(
            message
        )


def _create_update_manager() -> UpdateManager | None:
    """Construct Velopack with the bundle paths this project owns."""
    try:
        from velopack import UpdateManager as VelopackUpdateManager
        from velopack import VelopackLocatorConfig

        locator = _locator_config(_physical_executable_path(), VelopackLocatorConfig)
        if not locator.UpdateExePath.exists() or not locator.ManifestPath.exists():
            return None
        return cast(
            UpdateManager,
            VelopackUpdateManager(update_repository_url(), locator=locator),
        )
    except Exception as error:
        _LOGGER.warning("MatteLoop self-update unavailable: %s", error)
        return None


def _pending_update(manager: UpdateManager | None) -> object | None:
    if manager is None:
        return None
    try:
        pending = manager.get_update_pending_restart()
        _sweep_packages(pending)
        return pending
    except Exception as error:
        _LOGGER.warning("MatteLoop pending update could not be read: %s", error)
        return None


def _physical_executable_path() -> PurePath:
    """Resolve aliases before deriving the install root for advisory checks."""
    reported: PurePath = Path(sys.executable)
    return (
        reported.resolve()
        if _is_frozen_runtime() and isinstance(reported, Path)
        else reported
    )


def _install_root(executable: PurePath) -> PurePath:
    if sys.platform == "darwin":
        for parent in (executable, *executable.parents):
            if parent.name.endswith(".app"):
                return parent
    if executable.parent.name.lower() == "current":
        return executable.parent.parent
    return executable.parent


def _locator_config(executable: PurePath, config_type: Any) -> Any:
    """Describe this installation's layout instead of letting Velopack guess."""
    packages = cache_subdirectory("updates")
    if sys.platform == "darwin":
        root = executable.parents[2]
        return config_type(
            root,
            root / "Contents" / "MacOS" / "UpdateMac",
            packages,
            root / "Contents" / "Resources" / "sq.version",
            root / "Contents" / "MacOS",
            False,
        )
    if sys.platform == "win32":
        windows_executable = executable
        if not isinstance(windows_executable, (Path, PureWindowsPath)):
            windows_executable = PureWindowsPath(str(executable))
        current = windows_executable.parent
        windows_root = current.parent
        return config_type(
            windows_root,
            windows_root / "Update.exe",
            packages,
            current / "sq.version",
            current,
            Path(windows_root / ".portable").exists(),
        )
    raise RuntimeError("Velopack updates are only supported on macOS and Windows")


def _sweep_packages(pending: object | None) -> None:
    directory = cache_subdirectory("updates")
    if not directory.exists():
        return
    pending_filename = _update_filename(pending)
    for pattern in ("*.nupkg", "*.nupkg.part"):
        for package in directory.glob(pattern):
            if package.name == pending_filename:
                continue
            try:
                package.unlink()
            except OSError as error:
                _LOGGER.info(
                    "Could not remove stale update package %s: %s", package, error
                )


def _update_filename(update: object | None) -> str | None:
    if update is None:
        return None
    asset = getattr(update, "TargetFullRelease", update)
    filename = getattr(asset, "FileName", None)
    return filename if isinstance(filename, str) else None


def _remove_package(package: Path) -> None:
    try:
        package.unlink(missing_ok=True)
    except OSError as error:
        _LOGGER.info("Could not remove failed update package %s: %s", package, error)


def _advisory_update_capability(manager: UpdateManager | None) -> bool:
    """Select download only when the few known replacement blockers are absent."""
    if manager is None:
        return False
    executable = _physical_executable_path()
    if "/AppTranslocation/" in executable.as_posix():
        return False
    return os.access(_install_root(executable), os.W_OK)


def _update_version(update: object | None, fallback: str | None) -> str | None:
    if update is None:
        return fallback
    asset = getattr(update, "TargetFullRelease", update)
    version = getattr(asset, "Version", fallback)
    return version if isinstance(version, str) else fallback


def _is_frozen_runtime() -> bool:
    return getattr(sys, "frozen", False) or globals().get("__compiled__") is not None
