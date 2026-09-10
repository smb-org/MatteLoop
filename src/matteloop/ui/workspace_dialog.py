"""Asynchronous-friendly promoted cut-set picker dialog."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QSize, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from matteloop.core.errors import AppError
from matteloop.jobs.models.catalog import ModelCatalog
from matteloop.jobs.workspace import (
    WorkspaceSummary,
    detect_external_edits,
    list_workspaces,
)
from matteloop.jobs.workspace_migration import (
    LegacyCutSet,
    MigrationReport,
    MigrationStatus,
    find_legacy_cut_sets,
)
from matteloop.paths import cut_workspace_root
from matteloop.ui.aligned_rows import (
    ROW_DATA_ROLE,
    AlignedRowDelegate,
    install_aligned_row,
)
from matteloop.ui.source_presentation import format_source_file_size
from matteloop.ui.workspace_presentation import present_workspace

SUMMARY_ROLE = ROW_DATA_ROLE + 10


class WorkspacePickerDialog(QDialog):
    """List validated promoted sets and emit actions for the selected row."""

    use_requested = Signal(object)
    open_requested = Signal(object)
    delete_requested = Signal(object)
    move_requested = Signal(object)
    closed = Signal()

    def __init__(self, catalog: ModelCatalog, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._catalog = catalog
        self._summaries: tuple[WorkspaceSummary, ...] = ()
        self._legacy_entries: tuple[LegacyCutSet, ...] = ()
        self._output_directory: Path | None = None
        self._migration_busy = False
        self._build_widgets()
        self._build_layout()
        self._connect_signals()
        self._update_actions()

    def _build_widgets(self) -> None:
        self.setObjectName("workspace_picker")
        self.setWindowTitle(
            QCoreApplication.translate("WorkspacePickerDialog", "Promoted cut sets")
        )
        self.setAccessibleName(
            QCoreApplication.translate("WorkspacePickerDialog", "Promoted cut sets")
        )
        self.resize(980, 360)
        self._message = QLabel()
        self._message.setProperty("secondary", True)
        self._build_migration_widgets()
        self._build_action_widgets()

    def _build_migration_widgets(self) -> None:
        self.legacy_notice = QLabel()
        self.legacy_notice.setObjectName("legacy_notice")
        self.legacy_notice.setWordWrap(True)
        self.legacy_notice.setAccessibleName(
            QCoreApplication.translate(
                "WorkspacePickerDialog", "Legacy cut-set migration offer"
            )
        )
        self.move_button = QPushButton()
        self.move_button.setObjectName("move_cut_sets")
        self.move_button.setAccessibleName(
            QCoreApplication.translate(
                "WorkspacePickerDialog", "Move legacy cut sets to the cache"
            )
        )
        self.legacy_notice.hide()
        self.move_button.hide()

    def _build_action_widgets(self) -> None:
        self.cut_set_list = QListWidget()
        self.cut_set_list.setObjectName("cut_set_list")
        self.cut_set_list.setAccessibleName(
            QCoreApplication.translate("WorkspacePickerDialog", "Promoted cut sets")
        )
        self.cut_set_list.setSelectionMode(QListWidget.SelectionMode.SingleSelection)
        self.cut_set_list.setItemDelegate(AlignedRowDelegate(self.cut_set_list))
        self.cut_set_list.setUniformItemSizes(True)
        self.use_button = QPushButton(
            QCoreApplication.translate("WorkspacePickerDialog", "Use this set")
        )
        self.use_button.setAccessibleName(
            QCoreApplication.translate("WorkspacePickerDialog", "Use selected cut set")
        )
        self.open_button = QPushButton(
            QCoreApplication.translate("WorkspacePickerDialog", "Open folder")
        )
        self.open_button.setAccessibleName(
            QCoreApplication.translate(
                "WorkspacePickerDialog", "Open selected cut folder"
            )
        )
        self.delete_button = QPushButton(
            QCoreApplication.translate("WorkspacePickerDialog", "Delete set")
        )
        self.delete_button.setAccessibleName(
            QCoreApplication.translate(
                "WorkspacePickerDialog", "Delete selected cut set"
            )
        )
        self.close_button = QPushButton(
            QCoreApplication.translate("WorkspacePickerDialog", "Close")
        )

    def _build_layout(self) -> None:
        layout = QVBoxLayout(self)
        layout.addWidget(
            QLabel(
                QCoreApplication.translate(
                    "WorkspacePickerDialog",
                    "Choose a validated cut set.",
                )
            )
        )
        layout.addWidget(self.legacy_notice)
        layout.addWidget(self.move_button)
        layout.addWidget(self._message)
        layout.addWidget(self.cut_set_list, 1)
        actions = QHBoxLayout()
        actions.addWidget(self.use_button)
        actions.addWidget(self.open_button)
        actions.addWidget(self.delete_button)
        actions.addStretch(1)
        actions.addWidget(self.close_button)
        layout.addLayout(actions)

    def _connect_signals(self) -> None:
        self.cut_set_list.currentItemChanged.connect(
            lambda _current, _previous: self._update_actions()
        )
        self.cut_set_list.itemDoubleClicked.connect(lambda _item: self._use_selected())
        self.use_button.clicked.connect(self._use_selected)
        self.open_button.clicked.connect(self._open_selected)
        self.delete_button.clicked.connect(self._delete_selected)
        self.move_button.clicked.connect(
            lambda: self.move_requested.emit(self._legacy_entries)
        )
        self.close_button.clicked.connect(self.close)

    @property
    def summaries(self) -> tuple[WorkspaceSummary, ...]:
        return self._summaries

    @property
    def legacy_entries(self) -> tuple[LegacyCutSet, ...]:
        return self._legacy_entries

    @property
    def output_directory(self) -> Path | None:
        return self._output_directory

    @property
    def message_text(self) -> str:
        return self._message.text()

    def load(self, output_directory: Path) -> None:
        """Reload and re-scan rows so external edits are labelled immediately."""
        self._output_directory = output_directory
        try:
            self._summaries = self._read_summaries(output_directory)
        except (AppError, OSError) as error:
            self._summaries = ()
            self._set_legacy_offer(())
            self._message.setText(
                QCoreApplication.translate(
                    "WorkspacePickerDialog", "Could not read cut sets: %s"
                )
                % error
            )
            self.cut_set_list.clear()
            self._update_actions()
            return
        try:
            legacy_entries = find_legacy_cut_sets(output_directory)
        except (AppError, OSError):
            legacy_entries = ()
        self._set_legacy_offer(legacy_entries)
        self._populate_rows()
        self._show_loaded_message()
        self._update_actions()

    @staticmethod
    def _read_summaries(output_directory: Path) -> tuple[WorkspaceSummary, ...]:
        listing = list_workspaces(output_directory)
        refreshed: list[WorkspaceSummary] = []
        for summary in listing:
            manifest = detect_external_edits(summary.workspace)
            size_bytes = sum(frame.size_bytes for frame in manifest.frames)
            size_bytes += len(manifest.to_json_bytes())
            refreshed.append(replace(summary, manifest=manifest, size_bytes=size_bytes))
        return tuple(refreshed)

    def _populate_rows(self) -> None:
        self.cut_set_list.clear()
        for summary in self._summaries:
            row = present_workspace(summary, self._catalog)
            item = QListWidgetItem(row.display_text)
            install_aligned_row(item, row)
            item.setData(SUMMARY_ROLE, summary)
            item.setSizeHint(QSize(0, 44))
            self.cut_set_list.addItem(item)
        if self.cut_set_list.count():
            self.cut_set_list.setCurrentRow(0)

    def _show_loaded_message(self) -> None:
        if self._summaries:
            summary_count = len(self._summaries)
            self._message.setText(
                self.tr(
                    "%n promoted cut set(s) in MatteLoop's cache (%1)",
                    "",
                    summary_count,
                ).replace("%1", str(cut_workspace_root() / "cuts"))
            )
        else:
            self._message.setText(
                QCoreApplication.translate(
                    "WorkspacePickerDialog", "No promoted cut sets found."
                )
            )

    def set_migration_busy(self, busy: bool) -> None:
        """Disable row actions while the migration worker owns the picker."""
        self._migration_busy = busy
        self._update_actions()

    def show_migration_progress(self, name: str, completed: int, total: int) -> None:
        """Show the set currently being processed by the migration worker."""
        message = QCoreApplication.translate(
            "WorkspacePickerDialog", "Moving %1 (%2 of %3)…"
        )
        self._message.setText(
            message.replace("%1", name)
            .replace("%2", str(completed))
            .replace("%3", str(total))
        )

    def show_migration_report(self, report: MigrationReport) -> None:
        """Render the completed migration and any sets left in place."""
        message = self.tr("Moved %n set(s).", "", report.moved_count).replace(
            "%n", str(report.moved_count)
        )
        for outcome in report.remaining:
            detail = self._migration_outcome_text(outcome.status, outcome.reason)
            if detail:
                message += "\n" + detail.replace("%1", outcome.entry.name).replace(
                    "%2", outcome.reason
                )
        self._message.setText(message)

    def _set_legacy_offer(self, entries: tuple[LegacyCutSet, ...]) -> None:
        self._legacy_entries = entries
        if not entries:
            self.legacy_notice.hide()
            self.move_button.hide()
            return
        total_size = sum(entry.size_bytes for entry in entries)
        notice = self.tr(
            "%n cut set(s) (%1) from an earlier version are stored inside the "
            "output folder, where a sync client can alter them.",
            "",
            len(entries),
        )
        notice = notice.replace("%n", str(len(entries))).replace(
            "%1", format_source_file_size(total_size)
        )
        notice += " " + QCoreApplication.translate(
            "WorkspacePickerDialog",
            "Move them into MatteLoop's cache to use them again.",
        )
        self.legacy_notice.setText(notice)
        self.move_button.setText(
            self.tr("Move %n set(s) to the cache", "", len(entries)).replace(
                "%n", str(len(entries))
            )
        )
        self.legacy_notice.show()
        self.move_button.show()

    @staticmethod
    def _migration_outcome_text(status: MigrationStatus, reason: str) -> str:
        if status is MigrationStatus.KEPT_BOTH:
            return QCoreApplication.translate(
                "WorkspacePickerDialog",
                "%1: already in the cache with different frames — delete the "
                "cache copy in this list and move again to use the folder's copy.",
            )
        if status is MigrationStatus.NO_SPACE:
            return QCoreApplication.translate(
                "WorkspacePickerDialog", "%1: not enough space in MatteLoop's cache."
            )
        if status is MigrationStatus.FAILED:
            return QCoreApplication.translate(
                "WorkspacePickerDialog", "%1: could not be moved: %2"
            )
        if status is MigrationStatus.MOVED_SOURCE_REMAINS:
            return QCoreApplication.translate(
                "WorkspacePickerDialog",
                "%1: moved, but the source folder could not be fully cleaned: %2",
            )
        if status is MigrationStatus.UNREADABLE:
            return QCoreApplication.translate(
                "WorkspacePickerDialog", "%1: could not be read: %2"
            )
        return ""

    def selected_summary(self) -> WorkspaceSummary | None:
        item = self.cut_set_list.currentItem()
        if item is None:
            return None
        value = item.data(SUMMARY_ROLE)
        return value if isinstance(value, WorkspaceSummary) else None

    def _update_actions(self) -> None:
        enabled = not self._migration_busy and self.selected_summary() is not None
        self.use_button.setEnabled(enabled)
        self.open_button.setEnabled(enabled)
        self.delete_button.setEnabled(enabled)
        self.move_button.setEnabled(
            not self._migration_busy and bool(self._legacy_entries)
        )

    def _use_selected(self) -> None:
        summary = self.selected_summary()
        if summary is not None:
            self.use_requested.emit(summary)

    def _open_selected(self) -> None:
        summary = self.selected_summary()
        if summary is not None:
            self.open_requested.emit(summary)

    def _delete_selected(self) -> None:
        summary = self.selected_summary()
        if summary is not None:
            self.delete_requested.emit(summary)

    def closeEvent(self, event: QCloseEvent) -> None:
        self.closed.emit()
        super().closeEvent(event)
