"""Background workers for the in-app update flow."""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from pathlib import Path
from threading import Event
from typing import Protocol

from PySide6.QtCore import QObject, Signal

from matteloop.jobs.models.download import DownloadTransport
from matteloop.paths import cache_subdirectory
from matteloop.ui.update_paths import _update_version
from matteloop.ui.update_velopack import UpdateManager
from matteloop.updates import (
    UpdateDownloadCancelled,
    UpdateOutcome,
    UpdateResult,
    download_update,
)

_LOGGER = logging.getLogger(__name__)


class UpdateReader(Protocol):
    def check(
        self,
        channel: str = "stable",
        current_version: str | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> UpdateResult: ...


class _UpdateWorker(QObject):
    result = Signal(object)

    def __init__(
        self,
        reader: UpdateReader,
        channel: str,
        current_version: str | None,
        cancellation: Event,
    ) -> None:
        super().__init__()
        self._reader = reader
        self._channel = channel
        self._current_version = current_version
        self._cancellation = cancellation

    def run(self) -> None:
        try:
            result = self._reader.check(
                self._channel,
                self._current_version,
                self._cancellation.is_set,
            )
        except Exception as error:
            _LOGGER.info("Update check failed: %s", error)
            result = UpdateResult(UpdateOutcome.FAILED)
        if self._cancellation.is_set():
            return
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


def _remove_package(package: Path) -> None:
    try:
        package.unlink(missing_ok=True)
    except OSError as error:
        _LOGGER.info("Could not remove failed update package %s: %s", package, error)
