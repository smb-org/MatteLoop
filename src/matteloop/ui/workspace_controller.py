"""Picker lifecycle and safe actions for promoted cut sets."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import (
    QCoreApplication,
    QObject,
    QUrl,
    Signal,
    Slot,
)
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QMessageBox, QWidget

from matteloop.core.errors import AppError, ErrorCode
from matteloop.core.specs import RenderRequest
from matteloop.jobs.context import CancellationState
from matteloop.jobs.models.catalog import ModelCatalog
from matteloop.jobs.transform_store import discard_transform
from matteloop.jobs.workspace import CutWorkspace, WorkspaceSummary, delete_workspace
from matteloop.jobs.workspace_migration import (
    LegacyCutSet,
    MigrationReport,
    migrate_cut_set,
)
from matteloop.paths import cut_workspace_root
from matteloop.ui.copy import model_display_name
from matteloop.ui.source_presentation import (
    format_source_file_size,
    format_source_filename,
)
from matteloop.ui.worker_thread import WorkerThread, wait_for_shutdown_worker
from matteloop.ui.workspace_dialog import WorkspacePickerDialog


class _WorkspaceMigrationWorker(QObject):
    """Run one complete migration pass away from the Qt GUI thread."""

    progressed = Signal(int, int, str)
    finished = Signal(object)

    def __init__(
        self, entries: tuple[LegacyCutSet, ...], cancel: CancellationState
    ) -> None:
        super().__init__()
        self._entries = entries
        self._cancel = cancel

    @Slot()
    def run(self) -> None:
        outcomes = []
        total = len(self._entries)
        for index, entry in enumerate(self._entries, start=1):
            if self._cancel.requested:
                break
            self.progressed.emit(index, total, entry.name)
            outcomes.append(
                migrate_cut_set(entry, cancelled=lambda: self._cancel.requested)
            )
        self.finished.emit(MigrationReport(tuple(outcomes)))


class WorkspacePickerController(QObject):
    """Own the picker widget and its read/open/delete actions."""

    def __init__(
        self,
        *,
        dialog_parent: QWidget | None,
        request_factory: Callable[[], RenderRequest | None],
        active_workspace: Callable[[], CutWorkspace | None],
    ) -> None:
        super().__init__(dialog_parent)
        self._catalog = ModelCatalog.load_resource()
        self.dialog = WorkspacePickerDialog(self._catalog, dialog_parent)
        self._request_factory = request_factory
        self._active_workspace = active_workspace
        self._migration_cancel: CancellationState | None = None
        self._migration_thread: WorkerThread | None = None
        self._migration_worker: _WorkspaceMigrationWorker | None = None
        self.dialog.open_requested.connect(self._open_selected)
        self.dialog.delete_requested.connect(self._delete_selected)
        self.dialog.move_requested.connect(self._move_requested)
        self.dialog.closed.connect(self._dialog_closed)

    def open(self, output_directory: Path) -> None:
        self.dialog.load(output_directory)
        self.dialog.open()

    def close(self) -> bool:
        if self._migration_cancel is not None:
            self._migration_cancel.request()
        thread = self._migration_thread
        if thread is not None:
            thread.quit()
            complete = wait_for_shutdown_worker(
                thread, self._migration_worker, "workspace migration worker"
            )
            self._migration_thread = None
            self._migration_worker = None
            self._migration_cancel = None
            self.dialog.set_migration_busy(False)
        else:
            complete = True
        self.dialog.close()
        return complete

    def _dialog_closed(self) -> None:
        if self._migration_cancel is not None:
            self._migration_cancel.request()

    def _move_requested(self, value: object) -> None:
        if self._migration_thread is not None:
            return
        entries = value if isinstance(value, tuple) else self.dialog.legacy_entries
        if not entries or not all(
            isinstance(entry, LegacyCutSet) for entry in entries
        ):
            return
        typed_entries = tuple(
            entry for entry in entries if isinstance(entry, LegacyCutSet)
        )
        if not self._confirm_migration(typed_entries):
            return
        cancel = CancellationState()
        worker = _WorkspaceMigrationWorker(typed_entries, cancel)
        thread = WorkerThread(worker, self.dialog)
        worker.progressed.connect(self._migration_progressed)
        worker.finished.connect(self._migration_finished)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(self._migration_thread_finished)
        self._migration_cancel = cancel
        self._migration_worker = worker
        self._migration_thread = thread
        self.dialog.set_migration_busy(True)
        thread.start()

    @Slot(int, int, str)
    def _migration_progressed(self, completed: int, total: int, name: str) -> None:
        if self._migration_cancel is None or self._migration_cancel.requested:
            return
        self.dialog.show_migration_progress(name, completed, total)

    @Slot(object)
    def _migration_finished(self, value: object) -> None:
        if not isinstance(value, MigrationReport):
            return
        self.dialog.set_migration_busy(False)
        output = self.dialog.output_directory
        if output is not None:
            self.dialog.load(output)
        self.dialog.show_migration_report(value)

    @Slot()
    def _migration_thread_finished(self) -> None:
        self._migration_thread = None
        self._migration_worker = None
        self._migration_cancel = None

    def _confirm_migration(self, entries: tuple[LegacyCutSet, ...]) -> bool:
        output = self.dialog.output_directory
        if output is None:
            return False
        total_size = sum(entry.size_bytes for entry in entries)
        message = self.tr(
            "Move %n cut set(s) (%1) from\n%2\ninto\n%3?",
            "",
            len(entries),
        )
        message = message.replace("%n", str(len(entries))).replace(
            "%1", format_source_file_size(total_size)
        )
        message = message.replace("%2", str(output))
        message = message.replace("%3", str(cut_workspace_root() / "cuts"))
        message += "\n" + QCoreApplication.translate(
            "WorkspacePicker",
            "Each set is copied, verified, then removed from the output folder.",
        )
        message += "\n" + QCoreApplication.translate(
            "WorkspacePicker", "Externally edited frames are kept."
        )
        message += "\n" + QCoreApplication.translate(
            "WorkspacePicker",
            "A set that is already in the cache is left where it is.",
        )
        answer = QMessageBox.question(
            self.dialog,
            QCoreApplication.translate(
                "WorkspacePicker", "Move cut sets into the cache?"
            ),
            message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _open_selected(self, value: object) -> None:
        if isinstance(value, WorkspaceSummary):
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(value.workspace.path)))

    def _delete_selected(self, value: object) -> None:
        if not isinstance(value, WorkspaceSummary):
            return
        active = self._active_workspace()
        if active is not None and active.path == value.workspace.path:
            QMessageBox.warning(
                self.dialog,
                QCoreApplication.translate("WorkspacePicker", "Cut set is in use"),
                QCoreApplication.translate(
                    "WorkspacePicker", "This cut set is being used by a running job."
                ),
            )
            return
        if not self._confirm_delete(value):
            return
        allow_pinned = value.pinned
        try:
            delete_workspace(value.workspace, allow_pinned=allow_pinned)
            discard_transform(value.workspace)
        except AppError as error:
            if error.code is not ErrorCode.CUT_WORKSPACE_PINNED or allow_pinned:
                QMessageBox.warning(
                    self.dialog,
                    QCoreApplication.translate(
                        "WorkspacePicker", "Could not delete cut set"
                    ),
                    str(error),
                )
                return
            if not self._confirm_pinned_delete():
                return
            try:
                delete_workspace(value.workspace, allow_pinned=True)
                discard_transform(value.workspace)
            except AppError:
                return
        request = self._request_factory()
        if request is not None:
            self.dialog.load(request.output.directory)

    def _confirm_delete(self, value: WorkspaceSummary) -> bool:
        model = self._catalog.get(value.manifest.model_id)
        model_name = model_display_name(
            value.manifest.model_id, model.display_name
        )
        message = QCoreApplication.translate(
            "WorkspacePicker",
            "Delete %1 (%2 frames, %3)?\nStored frames and the saved transform "
            "will be removed. Recreating them requires background removal again.",
        )
        message = message.replace("%1", format_source_filename(value.source_path))
        message = message.replace("%2", str(value.manifest.frame_count))
        message = message.replace("%3", model_name)
        if value.pinned:
            message += "\n\n" + QCoreApplication.translate(
                "WorkspacePicker", "This set is pinned. Delete it anyway?"
            )
        answer = QMessageBox.question(
            self.dialog,
            QCoreApplication.translate("WorkspacePicker", "Delete cut set?"),
            message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _confirm_pinned_delete(self) -> bool:
        answer = QMessageBox.question(
            self.dialog,
            QCoreApplication.translate("WorkspacePicker", "Delete pinned cut set?"),
            QCoreApplication.translate(
                "WorkspacePicker", "This set is pinned. Delete it anyway?"
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes
