"""Inspector controls for the source-frame exclusion-region editor."""

from __future__ import annotations

from PySide6.QtCore import QCoreApplication, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from matteloop.core.exclusion import cut_exclusions
from matteloop.core.parameters import (
    ExclusionsBeforeModelChanged,
    ExclusionsChanged,
)
from matteloop.core.specs import CropSpec
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
        self.before_model_checkbox.toggled.connect(
            lambda enabled: self.command_requested.emit(
                ExclusionsBeforeModelChanged(enabled)
            )
        )

    def apply(self, presentation: ParameterPresentation, editable: bool) -> None:
        """Render the region count and current source editability."""
        count = len(presentation.exclusions)
        if presentation.exclusions_before_model:
            count_text = self.tr(
                "%n region(s), blanked before the model", "", count
            )
        else:
            count_text = self.tr("%n region(s)", "", count)
        self.count_label.setText(count_text)
        self.count_label.setAccessibleDescription(count_text)
        available = editable and presentation.duration is not None
        self.edit_button.setEnabled(available)
        self.clear_button.setEnabled(available and count > 0)
        self.before_model_checkbox.setEnabled(available and count > 0)
        blocked = self.before_model_checkbox.blockSignals(True)
        try:
            self.before_model_checkbox.setChecked(
                presentation.exclusions_before_model
            )
        finally:
            self.before_model_checkbox.blockSignals(blocked)
        if not available and self.edit_button.isChecked():
            self.edit_button.setChecked(False)

    def set_selected_exclusion(
        self, exclusion: CropSpec | None, crop: CropSpec | None
    ) -> None:
        """Render the selected stored rectangle and its effective crop status."""
        if exclusion is None or crop is None:
            text = QCoreApplication.translate("Inspector", "No region selected")
        else:
            # One complete sentence per case rather than a stem plus appended
            # fragments: a fragment forces every language into English word
            # order, and a language that must lead with the qualifier or
            # restructure the clause cannot translate it at all.
            text = self._format_selection(
                _selection_template(_crop_relation(exclusion, crop)), exclusion
            )
        self.selected_readout.setText(text)
        self.selected_readout.setToolTip(text)
        self.selected_readout.setAccessibleDescription(text)

    @staticmethod
    def _format_selection(template: str, exclusion: CropSpec) -> str:
        values = (exclusion.x, exclusion.y, exclusion.width, exclusion.height)
        for number, value in enumerate(values, start=1):
            template = template.replace(f"%{number}", str(value))
        return template

    def tab_widgets(self) -> tuple[QWidget, ...]:
        """Return the row's keyboard controls in visual order."""
        return (
            self.edit_button,
            self.clear_button,
            self.before_model_checkbox,
        )

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
        self.before_model_checkbox = QCheckBox(
            QCoreApplication.translate("Inspector", "Blank before the model")
        )
        self.before_model_checkbox.setObjectName("exclusion_before_model")
        self.before_model_checkbox.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        cost = QCoreApplication.translate(
            "Inspector",
            "Re-segments the clip: the stored cut set is not reused",
        )
        self.before_model_checkbox.setToolTip(cost)
        self.before_model_checkbox.setAccessibleDescription(cost)
        self.count_label = QLabel()
        self.count_label.setObjectName("exclusion_count")
        self.count_label.setProperty("secondary", True)
        self.count_label.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        self.count_label.setText(self.tr("%n region(s)", "", 0))
        self.count_label.setAccessibleDescription(self.count_label.text())
        self.selected_readout = QLabel()
        self.selected_readout.setObjectName("exclusion_selected_readout")
        self.selected_readout.setProperty("secondary", True)
        self.selected_readout.setWordWrap(True)
        self.selected_readout.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        self.selected_readout.setAccessibleName(
            QCoreApplication.translate("Inspector", "Selected exclusion region")
        )
        self.set_selected_exclusion(None, None)
        self.edit_button.setEnabled(False)
        self.clear_button.setEnabled(False)
        self.before_model_checkbox.setEnabled(False)

    def _build_layout(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        actions = QHBoxLayout()
        actions.setContentsMargins(0, 0, 0, 0)
        actions.setSpacing(8)
        actions.addWidget(self.edit_button)
        actions.addWidget(self.clear_button)
        layout.addLayout(actions)
        layout.addWidget(self.before_model_checkbox)
        layout.addWidget(self.count_label)
        layout.addWidget(self.selected_readout)


def _crop_relation(exclusion: CropSpec, crop: CropSpec) -> str:
    """Say whether the stored rectangle is fully, partly, or not in the crop."""
    boxes = cut_exclusions((exclusion,), crop)
    if not boxes:
        return "outside"
    box = boxes[0]
    effective = CropSpec(crop.x + box.left, crop.y + box.top, box.width, box.height)
    return "inside" if effective == exclusion else "partial"


def _selection_template(relation: str) -> str:
    """Return one complete translatable sentence for each relation."""
    if relation == "outside":
        return QCoreApplication.translate(
            "Inspector",
            "Selected region: x %1, y %2, width %3, height %4 source pixels;"
            " entirely outside crop",
        )
    if relation == "partial":
        return QCoreApplication.translate(
            "Inspector",
            "Selected region: x %1, y %2, width %3, height %4 source pixels;"
            " partly outside crop",
        )
    return QCoreApplication.translate(
        "Inspector",
        "Selected region: x %1, y %2, width %3, height %4 source pixels",
    )
