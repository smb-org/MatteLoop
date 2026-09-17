"""GUI-thread export of the current render request as versioned JSON.

Kept as its own module rather than a `RenderController` responsibility:
`render_controller.py` is already close to the 800-line module budget in
docs/engineering-guardrails.md G6, and this command has nothing to do with
running a job.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, QPoint
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QToolTip, QWidget

from matteloop.core.request_json import render_settings_to_json
from matteloop.ui.ports import CopyRenderSettingsRequested, StateStore
from matteloop.ui.request_builder import _try_export_request


class RenderSettingsExportController(QObject):
    """Copies the current render request to the clipboard as versioned JSON."""

    def __init__(
        self,
        store: StateStore,
        *,
        dialog_parent: QWidget | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._store = store
        self._dialog_parent = dialog_parent
        self._anchor_widget: QWidget | None = None

    def set_dialog_parent(self, parent: QWidget) -> None:
        """Follow the window used for anchoring the copy confirmation."""
        self._dialog_parent = parent

    def set_anchor_widget(self, widget: QWidget | None) -> None:
        """Anchor the copy confirmation near the Render button, if known."""
        self._anchor_widget = widget

    def dispatch(self, command: CopyRenderSettingsRequested) -> None:
        del command
        state = self._store.state
        if state.source_value is None:
            return
        request = _try_export_request(
            state.source_value, state.timeline, state.crop, state.parameters
        )
        if request is None:
            return
        text = render_settings_to_json(request)
        clipboard = QGuiApplication.clipboard()
        if clipboard is None:
            return
        clipboard.setText(text)
        # Confirm only what actually happened: some platforms (headless
        # clipboards, a clipboard manager intercepting the change) can leave
        # the clipboard holding something other than what was just set.
        if clipboard.text() != text:
            return
        self._show_confirmation()

    def _show_confirmation(self) -> None:
        message = self.tr("Copied render settings to clipboard")
        anchor = self._anchor_widget or self._dialog_parent
        if anchor is not None:
            position = anchor.mapToGlobal(QPoint(0, anchor.height()))
            QToolTip.showText(position, message, anchor)
        else:
            QToolTip.showText(QPoint(0, 0), message)
