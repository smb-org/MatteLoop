"""Inspector controls for the source-frame exclusion-region editor."""

from __future__ import annotations

from PySide6.QtCore import QCoreApplication, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QToolButton, QWidget

from matteloop.core.parameters import ExclusionsChanged
from matteloop.ui.compact_widgets import compact_field
from matteloop.ui.parameter_presentation import ParameterPresentation


class ExclusionControls(QWidget):
    """Small, reducer-facing control row for exclusion regions."""

    edit_toggled = Signal(bool)
    command_requested = Signal(object)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("exclusion_controls")
        self._build_widgets()
        self._build_layout()
        self.edit_button.toggled.connect(self.edit_toggled)
        self.clear_button.clicked.connect(
            lambda: self.command_requested.emit(ExclusionsChanged(()))
        )

    def apply(self, presentation: ParameterPresentation, editable: bool) -> None:
        """Render the region count and current source editability."""
        count = len(presentation.exclusions)
        count_text = self.tr("%n region(s)", "", count)
        self.count_label.setText(count_text)
        self.count_label.setAccessibleDescription(count_text)
        available = editable and presentation.duration is not None
        self.edit_button.setEnabled(available)
        self.clear_button.setEnabled(available and count > 0)
        if not available and self.edit_button.isChecked():
            self.edit_button.setChecked(False)

    def tab_widgets(self) -> tuple[QWidget, ...]:
        """Return the row's keyboard controls in visual order."""
        return (self.edit_button, self.clear_button)

    def _build_widgets(self) -> None:
        self.edit_button = QToolButton()
        self.edit_button.setObjectName("exclusion_edit")
        self.edit_button.setCheckable(True)
        self.edit_button.setText(QCoreApplication.translate("Inspector", "Edit"))
        self.edit_button.setAccessibleName(
            QCoreApplication.translate("Inspector", "Edit exclusion regions")
        )
        self.edit_button.setToolTip(
            QCoreApplication.translate("Inspector", "Edit exclusion regions")
        )
        self.clear_button = QToolButton()
        self.clear_button.setObjectName("exclusion_clear")
        self.clear_button.setText(QCoreApplication.translate("Inspector", "Clear"))
        self.clear_button.setAccessibleName(
            QCoreApplication.translate("Inspector", "Clear exclusion regions")
        )
        self.count_label = compact_field(QLabel())
        self.count_label.setObjectName("exclusion_count")
        self.count_label.setProperty("secondary", True)
        self.count_label.setText(self.tr("%n region(s)", "", 0))
        self.count_label.setAccessibleDescription(self.count_label.text())
        self.edit_button.setEnabled(False)
        self.clear_button.setEnabled(False)

    def _build_layout(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(self.edit_button)
        layout.addWidget(self.clear_button)
        layout.addWidget(self.count_label, 1)
