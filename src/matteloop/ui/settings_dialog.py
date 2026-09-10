"""Preferences dialog for reducer-owned application settings."""

from __future__ import annotations

from PySide6.QtCore import QCoreApplication, QSettings, QSignalBlocker
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
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
from matteloop.ui.copy import provider_label
from matteloop.ui.i18n import (
    SUPPORTED_LANGUAGES,
    persist_language,
    selected_language,
    translate_language_name,
)
from matteloop.ui.ports import StateStore, WindowServices
from matteloop.ui.preferences import (
    load_check_on_startup,
    load_update_channel,
    persist_check_on_startup,
    persist_update_channel,
)


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
        self.setMinimumWidth(640)
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
            self.provider_picker.addItem("", option.provider)
        self.updates_check_on_startup = QCheckBox(
            QCoreApplication.translate(
                "SettingsDialog", "Check for updates when MatteLoop starts"
            )
        )
        self.updates_check_on_startup.setObjectName("updates_check_on_startup")
        self.updates_check_on_startup.setAccessibleName(
            QCoreApplication.translate(
                "SettingsDialog", "Check for updates when MatteLoop starts"
            )
        )
        self._build_update_channel_widgets()

        self.updates_status_label = QLabel()
        self.updates_status_label.setObjectName("updates_status")
        self.updates_status_label.setAccessibleName(
            QCoreApplication.translate("SettingsDialog", "Updates")
        )
        self.updates_status_label.setProperty("secondary", True)
        self.check_for_updates_button = QPushButton(
            QCoreApplication.translate("SettingsDialog", "Check for updates")
        )
        self.check_for_updates_button.setObjectName("check_for_updates")
        self.check_for_updates_button.setAccessibleName(
            QCoreApplication.translate("SettingsDialog", "Check for updates")
        )
        self.button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.button_box.setObjectName("settings_actions")
        self.button_box.setAccessibleName(
            QCoreApplication.translate("SettingsDialog", "Preferences actions")
        )

    def _build_update_channel_widgets(self) -> None:
        self.update_channel_label = QLabel(
            QCoreApplication.translate("SettingsDialog", "Update channel")
        )
        self.update_channel_selector = QComboBox()
        self.update_channel_selector.setObjectName("updates_channel")
        self.update_channel_selector.setAccessibleName(
            QCoreApplication.translate("SettingsDialog", "Update channel")
        )
        self.update_channel_selector.addItem(
            QCoreApplication.translate("SettingsDialog", "Stable"), "stable"
        )
        self.update_channel_selector.addItem(
            QCoreApplication.translate("SettingsDialog", "Beta"), "beta"
        )
        self.update_channel_label.setBuddy(self.update_channel_selector)
        self.update_channel_note = QLabel(
            QCoreApplication.translate(
                "SettingsDialog",
                "Turning beta off does not downgrade this installation; it stays on its beta until a stable release overtakes it.",  # noqa: E501
            )
        )
        self.update_channel_note.setObjectName("updates_channel_note")
        self.update_channel_note.setAccessibleName(
            QCoreApplication.translate("SettingsDialog", "Update channel explanation")
        )
        self.update_channel_note.setProperty("secondary", True)
        self.update_channel_note.setWordWrap(True)

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
        # German runs a third longer than English here; without wrapping the
        # note is cut off at the dialog edge.
        self.language_note_label.setWordWrap(True)

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
        self._add_updates_row(form)

        self.heading_label = QLabel(
            QCoreApplication.translate("SettingsDialog", "Preferences")
        )
        self.heading_label.setObjectName("settings_heading")
        self.heading_label.setProperty("heading", True)

        layout = QVBoxLayout(self)
        layout.addWidget(self.heading_label)
        layout.addWidget(self.description_label)
        layout.addLayout(form)
        layout.addWidget(self.button_box)

    def _add_updates_row(self, form: QFormLayout) -> None:
        updates_row = QWidget()
        updates_layout = QVBoxLayout(updates_row)
        updates_layout.setContentsMargins(0, 0, 0, 0)
        updates_layout.setSpacing(4)
        updates_preferences_row = QWidget()
        updates_preferences_layout = QHBoxLayout(updates_preferences_row)
        updates_preferences_layout.setContentsMargins(0, 0, 0, 0)
        updates_preferences_layout.addWidget(self.updates_check_on_startup)
        updates_preferences_layout.addStretch(1)
        updates_preferences_layout.addWidget(self.update_channel_label)
        updates_preferences_layout.addWidget(self.update_channel_selector)
        updates_layout.addWidget(updates_preferences_row)
        updates_layout.addWidget(self.update_channel_note)
        updates_layout.addWidget(self.updates_status_label)
        check_row = QWidget()
        check_layout = QHBoxLayout(check_row)
        check_layout.setContentsMargins(0, 0, 0, 0)
        check_layout.addWidget(self.check_for_updates_button)
        check_layout.addStretch(1)
        updates_layout.addWidget(check_row)
        updates_label = QLabel(QCoreApplication.translate("SettingsDialog", "Updates"))
        updates_label.setBuddy(self.updates_check_on_startup)
        form.addRow(updates_label, updates_row)

    def _connect_controls(self) -> None:
        self.language_selector.currentIndexChanged.connect(self._language_changed)
        self.provider_picker.currentIndexChanged.connect(self._provider_changed)
        self.updates_check_on_startup.toggled.connect(self._updates_changed)
        self.update_channel_selector.currentIndexChanged.connect(
            self._update_channel_changed
        )
        self.button_box.rejected.connect(self.reject)
        close_button = self.button_box.button(QDialogButtonBox.StandardButton.Close)
        if close_button is not None:
            close_button.setAccessibleName(
                QCoreApplication.translate("SettingsDialog", "Close preferences")
            )
        self.setTabOrder(self.language_selector, self.provider_picker)
        self.setTabOrder(self.provider_picker, self.updates_check_on_startup)
        self.setTabOrder(
            self.updates_check_on_startup, self.update_channel_selector
        )
        self.setTabOrder(
            self.update_channel_selector, self.check_for_updates_button
        )
        if close_button is not None:
            self.setTabOrder(self.check_for_updates_button, close_button)

    def load(self) -> None:
        """Reload the current reducer-owned value before showing the dialog."""
        state = self._store.state
        language = selected_language(self._settings)
        with QSignalBlocker(self.language_selector):
            self.language_selector.setCurrentIndex(
                self.language_selector.findData(language)
            )
        with QSignalBlocker(self.provider_picker):
            for index, option in enumerate(self._provider_options):
                self.provider_picker.setItemText(
                    index,
                    provider_label(
                        option.provider,
                        recommended=option.recommended,
                        model_id=state.parameters.model_id,
                    ),
                )
            provider_index = self.provider_picker.findData(
                state.parameters.execution_provider
            )
            if provider_index >= 0:
                self.provider_picker.setCurrentIndex(provider_index)
        self.provider_picker.setEnabled(state.job.phase is JobState.IDLE)
        with QSignalBlocker(self.updates_check_on_startup):
            self.updates_check_on_startup.setChecked(
                load_check_on_startup(self._settings)
            )
        with QSignalBlocker(self.update_channel_selector):
            self.update_channel_selector.setCurrentIndex(
                self.update_channel_selector.findData(
                    load_update_channel(self._settings)
                )
            )

    def _provider_changed(self, _index: int) -> None:
        provider = self.provider_picker.currentData()
        if is_allowed_provider(provider):
            self._services.dispatch(ExecutionProviderChanged(provider))

    def _language_changed(self, index: int) -> None:
        language = self.language_selector.itemData(index)
        if isinstance(language, str):
            persist_language(self._settings, language)

    def _updates_changed(self, enabled: bool) -> None:
        persist_check_on_startup(self._settings, enabled)

    def _update_channel_changed(self, index: int) -> None:
        channel = self.update_channel_selector.itemData(index)
        if isinstance(channel, str):
            persist_update_channel(self._settings, channel)
