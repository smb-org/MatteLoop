"""Controller for the Phase 1 in-app release notice."""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING, Protocol

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

from matteloop.ui.preferences import load_check_on_startup
from matteloop.ui.worker_thread import WorkerThread
from matteloop.updates import GITHUB_RELEASES_URL, UpdateOutcome, UpdateResult

if TYPE_CHECKING:
    from matteloop.ui.main_window import MainWindow

_LOGGER = logging.getLogger(__name__)
_STARTUP_DELAY_MS = 3000


class UpdateReader(Protocol):
    def check(self) -> UpdateResult: ...


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


class UpdateController(QObject):
    """Run release checks away from Qt's GUI thread and drive update widgets."""

    def __init__(
        self,
        window: MainWindow,
        settings: QSettings,
        reader: UpdateReader,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent if parent is not None else window)
        self._window = window
        self._settings = settings
        self._reader = reader
        self._thread: QThread | None = None
        self._startup_scheduled = False
        self._startup_check_active = False
        self._banner_dismissed = False
        preferences = window.action_shelf.preferences_dialog
        preferences.check_for_updates_button.clicked.connect(self.check_now)
        window.update_open_releases_button.clicked.connect(self.open_releases_page)
        window.update_not_now_button.clicked.connect(self.dismiss_banner)

    @property
    def check_in_progress(self) -> bool:
        return self._thread is not None

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
        if self._thread is not None:
            return
        self._startup_check_active = startup
        if not startup:
            self._set_status(
                QCoreApplication.translate("SettingsDialog", "Checking for updates…")
            )
        worker = _UpdateWorker(self._reader)
        thread = WorkerThread(worker, self)
        self._thread = thread
        worker.result.connect(self._check_finished)
        thread.finished.connect(self._thread_finished)
        thread.start()

    @Slot(object)
    def _check_finished(self, value: object) -> None:
        result = value if isinstance(value, UpdateResult) else UpdateResult(
            UpdateOutcome.FAILED
        )
        startup = self._startup_check_active
        if result.outcome is UpdateOutcome.UPDATE and result.version:
            message = self._available_message(result.version)
            self._set_status(message)
            self._show_banner(message)
        elif result.outcome is UpdateOutcome.NONE:
            self._set_status(
                QCoreApplication.translate("SettingsDialog", "No update found.")
            )
            self._window.update_container.hide()
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
            self._window.update_container.hide()
        thread = self._thread
        if thread is not None:
            thread.quit()

    @Slot()
    def _thread_finished(self) -> None:
        self._thread = None

    def _available_message(self, version: str) -> str:
        template = QCoreApplication.translate(
            "UpdateBanner", "MatteLoop %1 is available."
        )
        return template.replace("%1", version)

    def _set_status(self, message: str) -> None:
        self._window.action_shelf.preferences_dialog.updates_status_label.setText(
            message
        )

    def _show_banner(self, message: str) -> None:
        if self._banner_dismissed:
            return
        self._window.update_banner.setText(message)
        self._window.update_banner.setAccessibleDescription(message)
        self._window.update_container.show()

    @Slot()
    def dismiss_banner(self) -> None:
        self._banner_dismissed = True
        self._window.update_container.hide()

    @Slot()
    def open_releases_page(self) -> None:
        QDesktopServices.openUrl(QUrl(GITHUB_RELEASES_URL))


def _is_frozen_runtime() -> bool:
    return getattr(sys, "frozen", False) or globals().get("__compiled__") is not None
