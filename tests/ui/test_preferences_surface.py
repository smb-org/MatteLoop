from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QDialogButtonBox, QFileDialog

from matteloop.core.execution_providers import (
    COREML_EXECUTION_PROVIDER,
    CPU_EXECUTION_PROVIDER,
    ProviderOption,
)
from matteloop.core.parameters import ParameterState
from matteloop.core.state import (
    AppState,
    RenderRequested,
    SourceLoaded,
    SourceLoadRequested,
    reduce,
)
from matteloop.ui.controller import SourceController
from matteloop.ui.inspector import Inspector
from matteloop.ui.main_window import MainWindow
from matteloop.ui.parameter_presentation import present_parameters
from matteloop.ui.settings_dialog import SettingsDialog
from matteloop.ui.store import ReducerStore


@dataclass(frozen=True)
class Metadata:
    path: Path
    width: int = 640
    height: int = 360
    duration: Fraction = Fraction(4)


class Services:
    def __init__(self) -> None:
        self.commands: list[object] = []

    def dispatch(self, command: object) -> None:
        self.commands.append(command)


def _settings(name: str) -> QSettings:
    settings = QSettings(
        QSettings.IniFormat,
        QSettings.UserScope,
        "matteloop-test",
        name,
    )
    settings.clear()
    return settings


def _ready_store(source: Path, output_directory: Path | None = None) -> ReducerStore:
    state = AppState(parameters=ParameterState(output_directory=output_directory))
    loading = reduce(state, SourceLoadRequested("source", "load"))
    ready = reduce(loading, SourceLoaded("source", "load", Metadata(source)))
    return ReducerStore(ready)


def test_preferences_button_opens_dialog_and_keeps_shortcut(qtbot) -> None:
    store = ReducerStore()
    window = MainWindow(
        store,
        Services(),
        _settings("button"),
        provider_options=(
            ProviderOption(CPU_EXECUTION_PROVIDER, "CPU – recommended", True),
            ProviderOption(COREML_EXECUTION_PROVIDER, "Apple CoreML – experimental"),
        ),
    )
    qtbot.addWidget(window)
    window.show()

    button = window.action_shelf.preferences_button
    assert button.objectName() == "preferences_action"
    assert button.accessibleName() == "Preferences"
    assert button.toolTip() == "Preferences"
    assert button.width() < window.render_button.width()
    assert button.isEnabled()
    assert not window.render_button.isEnabled()
    assert button.shortcut() == QKeySequence(QKeySequence.StandardKey.Preferences)

    button.click()

    qtbot.waitUntil(window.action_shelf.preferences_dialog.isVisible)
    assert window.action_shelf.preferences_dialog.windowTitle() == "Preferences"
    assert window.action_shelf.preferences_dialog.provider_picker.count() == 2


def test_inspector_output_directory_can_be_chosen_and_cleared(
    monkeypatch: pytest.MonkeyPatch, qtbot, tmp_path: Path
) -> None:
    source = tmp_path / "source" / "clip.mp4"
    source.parent.mkdir()
    chosen = tmp_path / "exports"
    chosen.mkdir()
    settings = _settings("shared-directory")
    store = _ready_store(source)
    controller = SourceController(store, settings=settings)
    inspector = Inspector(settings)
    qtbot.addWidget(inspector)
    inspector.command_requested.connect(controller.dispatch)
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *_args: str(chosen))

    inspector.output_directory_button.click()

    assert store.state.parameters.output_directory == chosen
    inspector.apply_parameters(present_parameters(store.state), editable=True)
    assert inspector.output_directory_edit.toolTip() == str(chosen)
    assert inspector.clear_output_directory_button.isEnabled()
    assert settings.value("parameters/output_directory") == str(chosen)

    inspector.clear_output_directory_button.click()

    assert store.state.parameters.output_directory is None
    inspector.apply_parameters(present_parameters(store.state), editable=True)
    assert inspector.output_directory_edit.toolTip() == str(source.parent)
    assert settings.value("parameters/output_directory") is None
    controller.shutdown()



def test_preferences_disable_provider_picker_while_a_job_runs(
    qtbot, tmp_path: Path
) -> None:
    source = tmp_path / "source" / "clip.mp4"
    source.parent.mkdir()
    store = _ready_store(source, tmp_path / "exports")
    store.dispatch(RenderRequested("job", "request"))
    dialog = SettingsDialog(
        store,
        Services(),
        provider_options=(
            ProviderOption(CPU_EXECUTION_PROVIDER, "CPU – recommended", True),
            ProviderOption(COREML_EXECUTION_PROVIDER, "Apple CoreML – experimental"),
        ),
    )
    qtbot.addWidget(dialog)

    dialog.load()

    assert not dialog.provider_picker.isEnabled()
    assert dialog.language_selector.isEnabled()


def test_preferences_exposes_and_emits_the_selected_execution_provider(qtbot) -> None:
    services = Services()
    dialog = SettingsDialog(
        ReducerStore(AppState()),
        services,
        provider_options=(
            ProviderOption(CPU_EXECUTION_PROVIDER, "CPU – recommended", True),
            ProviderOption(COREML_EXECUTION_PROVIDER, "Apple CoreML – experimental"),
        ),
    )
    qtbot.addWidget(dialog)

    assert dialog.provider_picker.accessibleName() == "Compute acceleration"
    assert dialog.provider_picker.currentData() == CPU_EXECUTION_PROVIDER
    assert dialog.provider_picker.itemText(0) == "CPU – recommended"
    assert dialog.provider_picker.itemText(1) == "Apple CoreML – experimental"
    dialog.provider_picker.setCurrentIndex(1)

    assert [type(command).__name__ for command in services.commands] == [
        "ExecutionProviderChanged"
    ]
    assert services.commands[0].execution_provider == COREML_EXECUTION_PROVIDER


def test_preferences_changes_provider_before_a_video_is_loaded(qtbot) -> None:
    settings = _settings("provider-before-source")
    store = ReducerStore(AppState())
    controller = SourceController(store, settings=settings)
    dialog = SettingsDialog(
        store,
        controller,
        settings=settings,
        provider_options=(
            ProviderOption(CPU_EXECUTION_PROVIDER, "CPU – recommended", True),
            ProviderOption(COREML_EXECUTION_PROVIDER, "Apple CoreML – experimental"),
        ),
    )
    qtbot.addWidget(dialog)

    dialog.provider_picker.setCurrentIndex(1)

    assert store.state.parameters.execution_provider == COREML_EXECUTION_PROVIDER
    assert settings.value("parameters/execution_provider") == COREML_EXECUTION_PROVIDER
    controller.shutdown()


def test_preferences_offers_no_cancel_it_cannot_honour(qtbot) -> None:
    """Every change applies at once, so a Cancel button would be a lie."""
    dialog = SettingsDialog(ReducerStore(AppState()), Services())
    qtbot.addWidget(dialog)

    assert dialog.button_box.button(QDialogButtonBox.StandardButton.Close)
    assert dialog.button_box.button(QDialogButtonBox.StandardButton.Cancel) is None
    assert dialog.button_box.button(QDialogButtonBox.StandardButton.Ok) is None


def test_preferences_persists_language_for_the_next_restart(qtbot) -> None:
    settings = _settings("language")
    # Start from English explicitly: without a stored value the dialog opens on
    # the system language, so on a German machine selecting German is a no-op
    # and the test would only ever pass on an English host.
    settings.setValue("ui/language", "en")
    dialog = SettingsDialog(ReducerStore(AppState()), Services(), settings=settings)
    qtbot.addWidget(dialog)
    assert dialog.language_selector.currentData() == "en"

    dialog.language_selector.setCurrentIndex(dialog.language_selector.findData("de"))

    assert settings.value("ui/language") == "de"
    assert (
        dialog.language_note_label.text()
        == "Interface language changes apply after restarting MatteLoop."
    )
