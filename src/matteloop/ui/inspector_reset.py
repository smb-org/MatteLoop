"""Inspector action for restoring creative parameter defaults."""

from __future__ import annotations

from collections.abc import Callable
from fractions import Fraction

from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import QPushButton

from matteloop.core.parameters import ParametersReset, ParameterState
from matteloop.ui.parameter_presentation import ParameterPresentation


def can_reset_parameters(presentation: ParameterPresentation) -> bool:
    """Return whether reset would change a value without changing the range."""
    defaults = ParameterState()
    if any(
        current != default
        for current, default in (
            (presentation.model_id, defaults.model_id),
            (presentation.edge_mode, defaults.edge_mode),
            (presentation.trim, defaults.trim),
            (presentation.alpha_threshold, defaults.alpha_threshold),
            (presentation.padding, defaults.padding),
            (presentation.stretch_x, defaults.stretch_x),
            (presentation.exclusions, defaults.exclusions),
            (presentation.max_mib, defaults.max_mib),
        )
    ):
        return True
    return presentation.fps != defaults.fps and (
        presentation.duration is not None
        and presentation.duration >= Fraction(1, defaults.fps)
    )


def action_availability(
    presentation: ParameterPresentation,
) -> tuple[bool, bool]:
    """Return whether Clear and Reset each have work to perform."""
    return (
        presentation.output_directory_override is not None,
        can_reset_parameters(presentation),
    )


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
        "Compute acceleration, transform, output directory, and output filename "
        "are not affected.",
    )
    button.setToolTip(scope)
    button.setAccessibleDescription(scope)
    button.setEnabled(False)
    button.clicked.connect(lambda: emit(ParametersReset()))
    return button
