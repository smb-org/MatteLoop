"""Preferences dialog for reducer-owned application settings."""

from __future__ import annotations

from PySide6.QtCore import QCoreApplication, QSettings, QSignalBlocker
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from matteloop.core.execution_providers import (
    CPU_EXECUTION_PROVIDER,
    ProviderOption,
    is_allowed_provider,
)
from matteloop.core.execution_providers import (
    provider_options as build_provider_options,
)
from matteloop.core.parameters import ExecutionProviderChanged
from matteloop.core.state import JobState
from matteloop.ui.compact_widgets import (
    ElidingComboBox,
    compact_field,
    form_layout,
)
from matteloop.ui.i18n import (
    SUPPORTED_LANGUAGES,
    persist_language,
    selected_language,
    translate_language_name,
)
from matteloop.ui.ports import StateStore, WindowServices


class SettingsDialog(QDialog):
    """Edit application-wide interface and compute preferences."""

    def __init__(
        self,
        store: StateStore,
        services: WindowServices,
        parent: QWidget | None = None,
        *,
        settings: QSettings | None = None,
        provider_options: tuple[ProviderOption, ...] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("settings_dialog")
        self.setWindowTitle(QCoreApplication.translate("SettingsDialog", "Preferences"))
        self.setAccessibleName(
            QCoreApplication.translate("SettingsDialog", "Preferences")
        )
        self.setMinimumWidth(560)
        self._store = store
        self._services = services
        self._settings = settings or QSettings()
        self._provider_options = (
            build_provider_options((CPU_EXECUTION_PROVIDER,))
            if provider_options is None
            else provider_options
        )

        self._build_widgets()
        self._build_layout()
        self._connect_controls()
        self.load()

    def _build_widgets(self) -> None:
        self.language_selector = QComboBox()
        self.language_selector.setObjectName("interface_language")
        self.language_selector.setAccessibleName(
            QCoreApplication.translate("SettingsDialog", "Interface language")
        )
        for language in SUPPORTED_LANGUAGES:
            self.language_selector.addItem(translate_language_name(language), language)
        self.provider_picker = compact_field(ElidingComboBox())
        self.provider_picker.setObjectName("provider_picker")
        self.provider_picker.setAccessibleName(
            QCoreApplication.translate("SettingsDialog", "Compute acceleration")
        )
        for option in self._provider_options:
            self.provider_picker.addItem(
                QCoreApplication.translate("ProviderCopy", option.label),
                option.provider,
            )
        self.button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.button_box.setObjectName("settings_actions")
        self.button_box.setAccessibleName(
            QCoreApplication.translate("SettingsDialog", "Preferences actions")
        )

    def _build_layout(self) -> None:
        self.description_label = QLabel()
        self.description_label.setObjectName("settings_description")
        self.description_label.setAccessibleName(
            QCoreApplication.translate("SettingsDialog", "Preferences description")
        )
        self.description_label.setProperty("secondary", True)
        self.description_label.setText(
            QCoreApplication.translate(
                "SettingsDialog", "These settings apply to every clip."
            )
        )
        self.language_note_label = QLabel(
            QCoreApplication.translate(
                "SettingsDialog",
                "Interface language changes apply after restarting MatteLoop.",
            )
        )
        self.language_note_label.setObjectName("language_restart_note")
        self.language_note_label.setAccessibleName(
            QCoreApplication.translate("SettingsDialog", "Language restart note")
        )
        self.language_note_label.setProperty("secondary", True)

        language_row = QWidget()
        language_layout = QVBoxLayout(language_row)
        language_layout.setContentsMargins(0, 0, 0, 0)
        language_layout.setSpacing(4)
        language_layout.addWidget(self.language_selector)
        language_layout.addWidget(self.language_note_label)

        form = form_layout()
        language_label = QLabel(
            QCoreApplication.translate("SettingsDialog", "Interface language")
        )
        language_label.setBuddy(self.language_selector)
        form.addRow(language_label, language_row)
        provider_label_widget = QLabel(
            QCoreApplication.translate("SettingsDialog", "Compute acceleration")
        )
        provider_label_widget.setBuddy(self.provider_picker)
        form.addRow(provider_label_widget, self.provider_picker)

        layout = QVBoxLayout(self)
        layout.addWidget(self.description_label)
        layout.addLayout(form)
        layout.addWidget(self.button_box)

    def _connect_controls(self) -> None:
        self.language_selector.currentIndexChanged.connect(self._language_changed)
        self.provider_picker.currentIndexChanged.connect(self._provider_changed)
        self.button_box.rejected.connect(self.reject)
        close_button = self.button_box.button(QDialogButtonBox.StandardButton.Close)
        if close_button is not None:
            close_button.setAccessibleName(
                QCoreApplication.translate("SettingsDialog", "Close preferences")
            )
        self.setTabOrder(self.language_selector, self.provider_picker)
        if close_button is not None:
            self.setTabOrder(self.provider_picker, close_button)

    def load(self) -> None:
        """Reload the current reducer-owned value before showing the dialog."""
        state = self._store.state
        language = selected_language(self._settings)
        with QSignalBlocker(self.language_selector):
            self.language_selector.setCurrentIndex(
                self.language_selector.findData(language)
            )
        with QSignalBlocker(self.provider_picker):
            provider_index = self.provider_picker.findData(
                state.parameters.execution_provider
            )
            if provider_index >= 0:
                self.provider_picker.setCurrentIndex(provider_index)
        self.provider_picker.setEnabled(state.job.phase is JobState.IDLE)

    def _provider_changed(self, _index: int) -> None:
        provider = self.provider_picker.currentData()
        if is_allowed_provider(provider):
            self._services.dispatch(ExecutionProviderChanged(provider))

    def _language_changed(self, index: int) -> None:
        language = self.language_selector.itemData(index)
        if isinstance(language, str):
            persist_language(self._settings, language)
