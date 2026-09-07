"""Inspector action for restoring creative parameter defaults."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QPushButton

from matteloop.core.parameters import ParametersReset


def build_reset_button(emit: Callable[[object], None]) -> QPushButton:
    """Build the reset action with an honest description of its scope."""
    button = QPushButton(
        QCoreApplication.translate("Inspector", "Reset to defaults")
    )
    button.setObjectName("reset_parameters")
    button.setAccessibleName(
        QCoreApplication.translate("Inspector", "Reset inspector parameters")
    )
    scope = QCoreApplication.translate(
        "Inspector",
        "Compute acceleration, output directory, and output filename are not "
        "affected.",
    )
    button.setToolTip(scope)
    button.setAccessibleDescription(scope)
    button.clicked.connect(lambda: emit(ParametersReset()))
    return button
