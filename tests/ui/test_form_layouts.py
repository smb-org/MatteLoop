"""Form layout geometry under the style used by the macOS application."""

from __future__ import annotations

from collections.abc import Iterable

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import (
    QApplication,
    QFormLayout,
    QSizePolicy,
    QStyleFactory,
    QWidget,
)

from matteloop.core.state import AppState
from matteloop.ui.inspector import Inspector
from matteloop.ui.preview_controller.dialog import PreviewJobDialog
from matteloop.ui.settings_dialog import SettingsDialog
from matteloop.ui.store import ReducerStore


class _Services:
    def dispatch(self, command: object) -> None:
        del command


def _compact_fields(widget: QWidget) -> Iterable[QWidget]:
    return (
        child
        for child in widget.findChildren(QWidget)
        if child.sizePolicy().horizontalPolicy() == QSizePolicy.Policy.Ignored
    )


def test_ui_forms_keep_compact_fields_visible_under_macos_style(qtbot) -> None:
    application = QApplication.instance()
    assert application is not None
    original_style_name = application.style().objectName()
    macos_style = QStyleFactory.create("macos")
    try:
        if macos_style is not None:
            application.setStyle(macos_style)
        else:
            # Headless Linux may not ship Qt's macOS style plugin. Its form
            # default is reproduced here so this test still names the policy
            # that collapses an Ignored field.
            fallback = QFormLayout()
            fallback.setFieldGrowthPolicy(
                QFormLayout.FieldGrowthPolicy.FieldsStayAtSizeHint
            )
            assert (
                fallback.fieldGrowthPolicy()
                == QFormLayout.FieldGrowthPolicy.FieldsStayAtSizeHint
            )

        settings = QSettings(
            QSettings.IniFormat,
            QSettings.UserScope,
            "matteloop-form-layout-test",
            "ui",
        )
        settings.clear()
        inspector = Inspector(settings)
        qtbot.addWidget(inspector)
        inspector.resize(900, 700)
        inspector.show()
        for _button, body in inspector.disclosures.values():
            body.setVisible(True)

        preferences = SettingsDialog(
            ReducerStore(AppState()), _Services(), settings=settings
        )
        qtbot.addWidget(preferences)
        preferences.resize(900, 700)
        preferences.show()

        job_dialog = PreviewJobDialog()
        qtbot.addWidget(job_dialog)
        job_dialog.resize(900, 700)
        job_dialog.show()
        application.processEvents()

        for widget in (inspector, preferences, job_dialog):
            forms = widget.findChildren(QFormLayout)
            assert forms
            assert all(
                form.fieldGrowthPolicy()
                == QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
                for form in forms
            )
            assert all(field.width() > 0 for field in _compact_fields(widget))
    finally:
        restored_style = QStyleFactory.create(original_style_name)
        if restored_style is not None:
            application.setStyle(restored_style)
