"""Controller for the in-app release notice, download, and install flow."""

from __future__ import annotations

import logging
from collections.abc import Callable
from threading import Event
from typing import TYPE_CHECKING, cast

from PySide6.QtCore import (
    QCoreApplication,
    QObject,
    QSettings,
    QThread,
    QTimer,
    QUrl,
    Slot,
)
from PySide6.QtGui import QDesktopServices

from matteloop.core.state import AppState, JobState
from matteloop.jobs.models.download import DownloadTransport
from matteloop.ui.i18n import display_locale
from matteloop.ui.ports import StateStore
from matteloop.ui.preferences import load_check_on_startup, load_update_channel
from matteloop.ui.update_paths import (
    _advisory_update_capability,
    _install_root_is_writable,
    _is_frozen_runtime,
    _physical_executable_path,
    _update_version,
)
from matteloop.ui.update_velopack import (
    UpdateManager,
    _create_update_manager,
    _current_update_version,
    _pending_update,
)
from matteloop.ui.update_workers import (
    UpdateReader,
    _DownloadWorker,
    _UpdateWorker,
)
from matteloop.ui.worker_thread import WorkerThread, wait_for_thread_shutdown
from matteloop.updates import (
    UpdateOutcome,
    UpdateResult,
    release_notes_url,
    releases_url,
)

if TYPE_CHECKING:
    from matteloop.ui.main_window import MainWindow

_LOGGER = logging.getLogger(__name__)
_STARTUP_DELAY_MS = 3000
_DOWNLOAD_SHUTDOWN_TIMEOUT_MS = 5000


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
        source_shutdown_complete: Callable[[], bool] | None = None,
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
        self._source_shutdown_complete = source_shutdown_complete
        self._self_update_advisable = _advisory_update_capability(self._manager)
        self._check_thread: QThread | None = None
        self._check_cancellation: Event | None = None
        self._download_thread: WorkerThread | None = None
        self._download_worker: _DownloadWorker | None = None
        self._download_cancellation: Event | None = None
        self._startup_scheduled = False
        self._startup_check_active = False
        self._startup_offer_shown = False
        self._startup_offer_pending = False
        self._updates_enabled = load_check_on_startup(self._settings)
        self._offer_state: str | None = None
        self._available_version: str | None = None
        self._available_size: int | None = None
        self._download_percent = 0
        self._ready_update: object | None = _pending_update(self._manager)
        self._pending_install: object | None = None
        self._apply_armed = False
        self._shutdown_complete = True
        self._store_unsubscribe = self._store.subscribe(self._state_changed)
        self._connect_widgets()
        self._restore_pending_update()

    def _connect_widgets(self) -> None:
        preferences = self._window.action_shelf.preferences_dialog
        preferences.check_for_updates_button.clicked.connect(self.check_now)
        preferences.updates_check_on_startup.toggled.connect(
            self._startup_setting_changed
        )
        dialog = self._window.update_dialog
        dialog.download_button.clicked.connect(self._download_button_clicked)
        dialog.install_button.clicked.connect(self.install_and_restart)
        dialog.release_notes_button.clicked.connect(self.open_release_notes)
        dialog.open_releases_button.clicked.connect(self.open_releases_page)
        dialog.not_now_button.clicked.connect(self.dismiss_offer)
        dialog.later_button.clicked.connect(self.dismiss_offer)
        dialog.finished.connect(self._dialog_finished)
        self._window.action_shelf.update_button.clicked.connect(self.show_offer)

    def _restore_pending_update(self) -> None:
        self._available_version = _update_version(self._ready_update, None)
        if self._ready_update is not None and self._available_version:
            self._show_ready(self._available_version, show_dialog=False)
            if self._updates_enabled:
                self._startup_offer_pending = True
                self._present_startup_offer()
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
        self._updates_enabled = load_check_on_startup(self._settings)
        if not self._updates_enabled:
            self._startup_offer_pending = False
            self._window.update_dialog.hide()
            self._refresh_update_button()
            self._refresh_install_button()
            return
        if _is_frozen_runtime():
            QTimer.singleShot(_STARTUP_DELAY_MS, self._startup_check)

    @Slot()
    def check_now(self) -> None:
        """Start a manual check, including when MatteLoop runs from source."""
        self._start_check(startup=False)

    @Slot()
    def _startup_check(self) -> None:
        self._updates_enabled = load_check_on_startup(self._settings)
        if not self._updates_enabled:
            self._startup_offer_pending = False
            self._refresh_update_button()
            self._refresh_install_button()
            return
        self._start_check(startup=True)

    def _start_check(self, *, startup: bool) -> None:
        if self.check_in_progress:
            return
        self._startup_check_active = startup
        if not startup:
            self._set_status(
                QCoreApplication.translate("SettingsDialog", "Checking for updates…")
            )
        cancellation = Event()
        worker = _UpdateWorker(
            self._reader,
            load_update_channel(self._settings),
            _current_update_version(self._manager),
            cancellation,
        )
        thread = WorkerThread(worker, self)
        self._check_thread = thread
        self._check_cancellation = cancellation
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
            self._available_size = result.size
            if self._ready_update is None:
                self._available_version = result.version
                message = self._available_message(result.version)
                self._set_status(message)
                self._show_available(message, show_dialog=not startup)
                if startup:
                    self._startup_offer_pending = True
                    self._present_startup_offer()
            else:
                ready_version = _update_version(
                    self._ready_update, self._available_version
                )
                if ready_version:
                    self._available_version = ready_version
                    self._show_ready(ready_version, show_dialog=not startup)
                    if startup:
                        self._startup_offer_pending = True
                        self._present_startup_offer()
        elif result.outcome is UpdateOutcome.NONE:
            self._set_status(
                QCoreApplication.translate("SettingsDialog", "No update found.")
            )
            if self._ready_update is None:
                self._hide_offer()
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
                self._hide_offer()
        thread = self._check_thread
        if thread is not None:
            thread.quit()

    @Slot()
    def _check_thread_finished(self) -> None:
        self._check_thread = None
        self._check_cancellation = None

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
        self._download_percent = 0
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
    def _cancel_check(self) -> bool:
        if self._check_cancellation is not None:
            self._check_cancellation.set()
        thread = self._check_thread
        if thread is None:
            return True
        thread.quit()
        return wait_for_thread_shutdown(
            thread,
            _DOWNLOAD_SHUTDOWN_TIMEOUT_MS,
            description="MatteLoop update check thread",
        )

    @Slot()
    def cancel_download(self) -> bool:
        """Request cancellation and let the transport observe the flag."""
        if self._download_cancellation is not None:
            self._download_cancellation.set()
        thread = self._download_thread
        if thread is not None:
            thread.quit()
            return wait_for_thread_shutdown(
                thread,
                _DOWNLOAD_SHUTDOWN_TIMEOUT_MS,
                description="MatteLoop update download thread",
            )
        return True

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
        self._download_percent = percent
        self._show_downloading(
            self._available_version, percent, show_dialog=False
        )

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
        if not _install_root_is_writable(_physical_executable_path()):
            self._show_install_failed()
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
        if not self._shutdown_complete:
            _LOGGER.warning(
                "MatteLoop update was not armed: update work did not stop "
                "during shutdown"
            )
            return
        if (
            self._source_shutdown_complete is not None
            and not self._source_shutdown_complete()
        ):
            _LOGGER.warning(
                "MatteLoop update was not armed: application work did not stop "
                "during shutdown"
            )
            return
        if not _install_root_is_writable(_physical_executable_path()):
            _LOGGER.warning(
                "MatteLoop update was not armed: install location is not writable"
            )
            return
        self._apply_armed = True
        try:
            # Velopack 1.2.0 exposes these arguments positionally only.
            self._manager.wait_exit_then_apply_updates(pending, False, True)
        except Exception as error:
            _LOGGER.warning("MatteLoop update could not be armed: %s", error)

    @Slot()
    def shutdown(self) -> bool:
        """Cancel update work and wait briefly for its workers to stop."""
        self._shutdown_complete = False
        self._shutdown_complete = all((self._cancel_check(), self.cancel_download()))
        return self._shutdown_complete

    @Slot()
    def dismiss_offer(self) -> None:
        self._startup_offer_pending = False
        self._window.update_dialog.hide()
        self._refresh_update_button()

    @Slot()
    def show_offer(self) -> None:
        """Reopen the current update offer from the action-shelf arrow."""
        if not self._is_job_idle():
            return
        if self._offer_state == "available":
            self._show_available(
                self._available_message(self._available_version or "")
            )
        elif self._offer_state == "downloading" and self._available_version:
            self._show_downloading(self._available_version, self._download_percent)
        elif self._offer_state == "ready" and self._available_version:
            self._show_ready(self._available_version)
        elif self._offer_state == "failed":
            self._show_failed()

    @Slot()
    def open_releases_page(self) -> None:
        QDesktopServices.openUrl(QUrl(releases_url()))

    @Slot()
    def open_release_notes(self) -> None:
        """Open the offered version's release notes, or the index if unknown."""
        version = self._available_version
        url = release_notes_url(version) if version else releases_url()
        QDesktopServices.openUrl(QUrl(url))

    def _state_changed(self, _state: AppState) -> None:
        self._refresh_install_button()
        if self._is_job_idle():
            self._present_startup_offer()
        elif self._window.update_dialog.isVisible():
            self._window.update_dialog.hide()
        self._refresh_update_button()

    @Slot(int)
    def _dialog_finished(self, _result: int) -> None:
        self._refresh_update_button()

    def _refresh_install_button(self) -> None:
        enabled = (
            self._ready_update is not None
            and self._store.state.job.phase is JobState.IDLE
        )
        self._window.update_dialog.install_button.setEnabled(enabled)

    def _show_available(self, message: str, *, show_dialog: bool = True) -> None:
        self._set_offer(message, "available", show_dialog=show_dialog)

    def _show_downloading(
        self, version: str, percent: int, *, show_dialog: bool = True
    ) -> None:
        self._set_offer(
            self._downloading_message(version, percent),
            "downloading",
            show_dialog=show_dialog,
        )

    def _show_ready(self, version: str, *, show_dialog: bool = True) -> None:
        self._set_offer(self._ready_message(version), "ready", show_dialog=show_dialog)

    def _show_failed(self) -> None:
        self._set_offer(
            QCoreApplication.translate(
                "UpdateBanner", "The update couldn’t be downloaded."
            ),
            "failed",
        )

    def _show_install_failed(self) -> None:
        self._set_offer(
            QCoreApplication.translate(
                "UpdateBanner", "MatteLoop cannot update itself from this location."
            ),
            "failed",
        )

    def _set_offer(self, message: str, state: str, *, show_dialog: bool = True) -> None:
        self._offer_state = state
        dialog = self._window.update_dialog
        dialog.message_label.setText(message)
        dialog.message_label.setAccessibleDescription(message)
        size_message = (
            self._download_size_message(self._available_size)
            if self._available_size is not None
            else None
        )
        dialog.set_download_size(size_message)
        if state == "available":
            dialog.show_available_actions(can_download=self._self_update_advisable)
        elif state == "downloading":
            dialog.show_downloading_actions()
        elif state == "ready":
            dialog.show_ready_actions()
        else:
            dialog.show_failed_actions()
        self._refresh_update_button()
        if show_dialog:
            self._show_offer_dialog()
        self._refresh_install_button()

    def _hide_offer(self) -> None:
        self._offer_state = None
        self._window.update_dialog.hide()
        self._window.action_shelf.update_button.hide()
        self._refresh_install_button()

    def _show_offer_dialog(self) -> None:
        if not self._is_job_idle():
            return
        self._window.update_dialog.open()
        self._window.update_dialog.raise_()
        self._window.update_dialog.activateWindow()
        self._refresh_update_button()
        for button in (
            self._window.update_dialog.download_button,
            self._window.update_dialog.install_button,
            self._window.update_dialog.open_releases_button,
        ):
            if button.isVisible() and button.isEnabled():
                button.setFocus()
                break

    def _present_startup_offer(self) -> None:
        if (
            not self._startup_offer_pending
            or self._startup_offer_shown
            or not self._updates_enabled
            or not self._offer_state
            or not self._is_job_idle()
        ):
            return
        self._startup_offer_pending = False
        self._startup_offer_shown = True
        self._show_offer_dialog()

    def _refresh_update_button(self) -> None:
        self._window.action_shelf.update_button.setVisible(
            self._offer_state in ("available", "downloading", "ready", "failed")
            and not self._window.update_dialog.isVisible()
        )

    def _startup_setting_changed(self, enabled: bool) -> None:
        self._updates_enabled = enabled
        if not enabled:
            self._startup_offer_pending = False
        self._refresh_update_button()
        self._refresh_install_button()

    def _is_job_idle(self) -> bool:
        return self._store.state.job.phase is JobState.IDLE

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

    def _download_size_message(self, size: int) -> str:
        megabytes = size // (1024 * 1024)
        template = QCoreApplication.translate("UpdateBanner", "Download size: %1 MB")
        return template.replace("%1", display_locale().toString(megabytes))

    def _set_status(self, message: str) -> None:
        self._window.action_shelf.preferences_dialog.updates_status_label.setText(
            message
        )
