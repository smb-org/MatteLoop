"""Source identity row and empty/error recovery surface."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from PySide6.QtCore import QCoreApplication, QEvent, QObject, Qt, QUrl, Signal
from PySide6.QtGui import (
    QDragEnterEvent,
    QDragLeaveEvent,
    QDragMoveEvent,
    QDropEvent,
)
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

SUPPORTED_VIDEO_SUFFIXES = frozenset({".mp4", ".mov", ".webm", ".mkv"})


class SourcePresentationValues(Protocol):
    @property
    def source_filename(self) -> str: ...

    @property
    def source_dimensions(self) -> str: ...

    @property
    def source_duration(self) -> str: ...

    @property
    def source_frame_rate(self) -> str: ...

    @property
    def source_file_size(self) -> str: ...

    @property
    def source_path(self) -> str | None: ...


class SourceStrip(QWidget):
    """A compact metadata strip that only reveals metadata actually provided."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("source_strip")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(12)
        self.filename = QLabel()
        self.filename.setObjectName("source_filename")
        self.filename.setAccessibleName(
            QCoreApplication.translate("SourceStrip", "Source video")
        )
        self.filename.setProperty("mono", True)
        self.dimensions = self._label("source_dimensions")
        self.duration = self._label("source_duration")
        self.frame_rate = self._label("source_frame_rate")
        self.file_size = self._label("source_file_size")
        self.replace_button = QPushButton(
            QCoreApplication.translate("SourceStrip", "Open Video…")
        )
        self.replace_button.setObjectName("replace_video")
        self.replace_button.setAccessibleName(
            QCoreApplication.translate("SourceStrip", "Open Video")
        )
        for widget in (
            self.filename,
            self.dimensions,
            self.duration,
            self.frame_rate,
            self.file_size,
        ):
            layout.addWidget(widget)
        layout.addStretch(1)
        layout.addWidget(self.replace_button)

    def _label(self, name: str) -> QLabel:
        value = QLabel()
        value.setObjectName(name)
        value.setProperty("secondary", True)
        value.setProperty("mono", True)
        return value

    def set_presented_metadata(self, values: SourcePresentationValues) -> None:
        """Apply presenter-owned source values to the strip widgets."""
        self.filename.setText(values.source_filename)
        self.dimensions.setText(values.source_dimensions)
        self.duration.setText(values.source_duration)
        self.frame_rate.setText(values.source_frame_rate)
        self.file_size.setText(values.source_file_size)
        self.filename.setToolTip(values.source_path or "")
        self.filename.setAccessibleDescription(values.source_path or "")


class SourceDropSurface(QWidget):
    """Restrained empty/recovery source-selection surface."""

    video_dropped = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("source_drop_target")
        self.setAccessibleName(
            QCoreApplication.translate("SourceDropSurface", "Video drop area")
        )
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAcceptDrops(True)
        self._drop_enabled = True
        self._drop_targets: list[QWidget] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(8)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.heading = QLabel(
            QCoreApplication.translate("SourceDropSurface", "Drop a video here")
        )
        self.heading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.heading.setAccessibleName(
            QCoreApplication.translate("SourceDropSurface", "Open a video")
        )
        self.button = QPushButton(
            QCoreApplication.translate("SourceDropSurface", "Open Video…")
        )
        self.button.setObjectName("choose_video")
        self.button.setAccessibleName(
            QCoreApplication.translate("SourceDropSurface", "Open Video")
        )
        self.button.setMinimumHeight(44)
        layout.addWidget(self.heading)
        layout.addWidget(self.button, alignment=Qt.AlignmentFlag.AlignCenter)

    def install_drop_target(self, widget: QWidget) -> None:
        """Extend this surface's drop handling to another visible widget."""
        if widget in self._drop_targets:
            return
        self._drop_targets.append(widget)
        widget.setAcceptDrops(True)
        widget.installEventFilter(self)

    def set_drop_enabled(self, enabled: bool) -> None:
        """Enable drops from presenter-owned source capabilities."""
        self._drop_enabled = enabled
        if not enabled:
            self._clear_drop_feedback()

    def _set_drop_feedback(self, widget: QWidget, active: bool) -> None:
        widget.setProperty("dropActive", active)
        widget.style().unpolish(widget)
        widget.style().polish(widget)
        widget.update()

    def _clear_drop_feedback(self) -> None:
        self._set_drop_feedback(self, False)
        for widget in self._drop_targets:
            self._set_drop_feedback(widget, False)

    def _handle_drag_enter(
        self, widget: QWidget, event: QDragEnterEvent
    ) -> None:
        accepted = self._drop_enabled and self._drop_path(event.mimeData()) is not None
        self._set_drop_feedback(widget, accepted)
        if accepted:
            event.acceptProposedAction()
        else:
            event.ignore()

    def _handle_drag_move(self, widget: QWidget, event: QDragMoveEvent) -> None:
        accepted = self._drop_enabled and self._drop_path(event.mimeData()) is not None
        self._set_drop_feedback(widget, accepted)
        if accepted:
            event.acceptProposedAction()
        else:
            event.ignore()

    def _handle_drop(self, widget: QWidget, event: QDropEvent) -> None:
        path = self._drop_path(event.mimeData())
        accepted = self._drop_enabled and path is not None
        self._set_drop_feedback(widget, False)
        if not accepted:
            event.ignore()
            return
        self.video_dropped.emit(path)
        event.acceptProposedAction()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if not isinstance(watched, QWidget) or watched not in self._drop_targets:
            return super().eventFilter(watched, event)
        if event.type() == QEvent.Type.DragEnter and isinstance(event, QDragEnterEvent):
            self._handle_drag_enter(watched, event)
            return True
        if event.type() == QEvent.Type.DragMove and isinstance(event, QDragMoveEvent):
            self._handle_drag_move(watched, event)
            return True
        if event.type() == QEvent.Type.DragLeave and isinstance(event, QDragLeaveEvent):
            self._set_drop_feedback(watched, False)
            return True
        if event.type() == QEvent.Type.Drop and isinstance(event, QDropEvent):
            self._handle_drop(watched, event)
            return True
        return super().eventFilter(watched, event)

    @staticmethod
    def _drop_path(mime_data: object) -> Path | None:
        if not hasattr(mime_data, "urls"):
            return None
        urls = mime_data.urls()
        if len(urls) != 1:
            return None
        url = urls[0]
        if not isinstance(url, QUrl) or not url.isLocalFile():
            return None
        path = Path(url.toLocalFile())
        if path.suffix.casefold() not in SUPPORTED_VIDEO_SUFFIXES:
            return None
        try:
            if not path.is_file():
                return None
        except OSError:
            return None
        return path

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        self._handle_drag_enter(self, event)

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        self._handle_drag_move(self, event)

    def dragLeaveEvent(self, event: QDragLeaveEvent) -> None:
        self._set_drop_feedback(self, False)
        event.accept()

    def dropEvent(self, event: QDropEvent) -> None:
        self._handle_drop(self, event)
