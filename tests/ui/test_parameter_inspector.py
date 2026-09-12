from __future__ import annotations

from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import pytest
from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QAbstractSpinBox, QComboBox

from matteloop.core.parameters import ExclusionsChanged, ParametersReset, ParameterState
from matteloop.core.specs import CropSpec, EdgeMode, TransformSpec
from matteloop.core.state import AppState, SourceState
from matteloop.core.timeline import TimelineState
from matteloop.ui.aligned_rows import (
    ACCESSIBLE_DESCRIPTION_ROLE,
    ROW_DATA_ROLE,
    STATUS_ROLE,
    RowStatus,
)
from matteloop.ui.controller import SourceController
from matteloop.ui.inspector import Inspector
from matteloop.ui.parameter_presentation import present_parameters
from matteloop.ui.store import ReducerStore


def _settings() -> QSettings:
    settings = QSettings(
        QSettings.IniFormat,
        QSettings.UserScope,
        "matteloop-test",
        "parameter-inspector",
    )
    settings.clear()
    return settings


def test_inspector_exposes_the_thirteen_enabled_models_with_default_selected(
    qtbot,
) -> None:
    inspector = Inspector(
        _settings(), model_options=(("birefnet-portrait", True),)
    )
    qtbot.addWidget(inspector)

    assert inspector.model_picker.count() == 13
    assert [inspector.model_picker.itemData(i) for i in range(13)] == [
        "u2net",
        "u2netp",
        "u2net_human_seg",
        "silueta",
        "isnet-general-use",
        "isnet-anime",
        "birefnet-general",
        "birefnet-general-lite",
        "birefnet-portrait",
        "birefnet-dis",
        "birefnet-hrsod",
        "birefnet-cod",
        "birefnet-massive",
    ]
    assert inspector.model_picker.currentData() == "birefnet-portrait"
    portrait_index = inspector.model_picker.findData("birefnet-portrait")
    assert inspector.model_picker.itemText(portrait_index) == "BiRefNet Portrait"
    assert (
        inspector.model_picker.itemData(portrait_index, STATUS_ROLE)
        is RowStatus.CACHED
    )
    assert "cached locally" in inspector.model_picker.itemData(
        portrait_index, ACCESSIBLE_DESCRIPTION_ROLE
    )
    assert not inspector.model_picker.itemIcon(portrait_index).isNull()
    assert inspector.edge_picker.count() == 2


def test_inspector_disclosure_titles_preserve_literal_ampersands(qtbot) -> None:
    inspector = Inspector(_settings())
    qtbot.addWidget(inspector)

    assert inspector.disclosures["time_sampling"][0].text() == "Time && Sampling"
    assert inspector.disclosures["time_sampling"][0].accessibleName() == (
        "Time & Sampling"
    )
    assert inspector.disclosures["crop_cleanup"][0].text() == "Crop && Cleanup"
    assert inspector.disclosures["crop_cleanup"][0].accessibleName() == (
        "Crop & Cleanup"
    )


@pytest.mark.parametrize(
    ("key", "title", "expanded"),
    [
        ("segmentation", "Segmentation", True),
        ("time_sampling", "Time & Sampling", True),
        ("crop_cleanup", "Crop & Cleanup", False),
        ("transform", "Transform", False),
        ("output", "Output", True),
        ("workspace", "Workspace", False),
    ],
)
def test_inspector_disclosures_show_visual_and_spoken_state(
    qtbot, key: str, title: str, expanded: bool
) -> None:
    inspector = Inspector(_settings())
    qtbot.addWidget(inspector)
    button, _body = inspector.disclosures[key]

    assert button.isChecked() is expanded
    assert button.arrowType() == (
        Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow
    )
    assert button.accessibleDescription() == (
        f"{title}: {'expanded' if expanded else 'collapsed'}"
    )

    button.click()

    assert button.isChecked() is not expanded
    assert button.arrowType() == (
        Qt.ArrowType.RightArrow if expanded else Qt.ArrowType.DownArrow
    )
    assert button.accessibleDescription() == (
        f"{title}: {'collapsed' if expanded else 'expanded'}"
    )


def test_transform_group_is_mounted_inside_its_disclosure_body(qtbot) -> None:
    inspector = Inspector(_settings())
    qtbot.addWidget(inspector)

    _button, body = inspector.disclosures["transform"]

    assert inspector.transform_group.parentWidget() is body


def test_tab_widgets_include_the_transform_disclosure_and_group_in_order(
    qtbot,
) -> None:
    inspector = Inspector(_settings())
    qtbot.addWidget(inspector)

    widgets = inspector.tab_widgets()
    transform_button = inspector.disclosures["transform"][0]
    crop_index = widgets.index(inspector.disclosures["crop_cleanup"][0])
    transform_index = widgets.index(transform_button)
    output_index = widgets.index(inspector.disclosures["output"][0])

    assert crop_index < transform_index < output_index
    group_widgets = inspector.transform_group.tab_widgets()
    assert widgets[transform_index + 1 : transform_index + 1 + len(group_widgets)] == (
        group_widgets
    )


def test_output_directory_middle_elides_while_preserving_full_path_semantics(
    qtbot,
) -> None:
    inspector = Inspector(_settings())
    qtbot.addWidget(inspector)
    path = "/tmp/matteloop/" + "very-long-directory-name/" * 8 + "exports"

    inspector.output_directory_edit.resize(140, 32)
    inspector.output_directory_edit.setText(path)

    assert inspector.output_directory_edit.text() != path
    assert "…" in inspector.output_directory_edit.text()
    assert inspector.output_directory_edit.toolTip() == path
    assert inspector.output_directory_edit.accessibleDescription() == path


def test_inspector_shows_manifest_download_size_and_cache_status_for_each_model(
    qtbot,
) -> None:
    inspector = Inspector(
        _settings(), model_options=(("u2netp", True),)
    )
    qtbot.addWidget(inspector)

    expected_sizes = {
        "birefnet-portrait": "927.6 MiB",
        "u2net": "167.8 MiB",
        "u2netp": "4.4 MiB",
        "u2net_human_seg": "167.8 MiB",
        "silueta": "42.1 MiB",
        "isnet-general-use": "170.4 MiB",
        "isnet-anime": "167.9 MiB",
        "birefnet-general": "927.6 MiB",
        "birefnet-general-lite": "213.6 MiB",
        "birefnet-dis": "927.6 MiB",
        "birefnet-hrsod": "927.6 MiB",
        "birefnet-cod": "927.6 MiB",
        "birefnet-massive": "927.6 MiB",
    }

    for index in range(inspector.model_picker.count()):
        model_id = inspector.model_picker.itemData(index)
        row = inspector.model_picker.itemData(index, ROW_DATA_ROLE)
        assert row.columns[1].text == expected_sizes[model_id]
        expected_status = (
            "cached locally" if model_id == "u2netp" else "not cached yet"
        )
        detail = inspector.model_picker.itemData(
            index, Qt.ItemDataRole.AccessibleTextRole
        )
        assert expected_status in detail


def test_unlimited_output_size_is_spoken_as_unlimited_and_keeps_positive_values(
    qtbot,
) -> None:
    inspector = Inspector(_settings())
    qtbot.addWidget(inspector)
    commands: list[object] = []
    inspector.command_requested.connect(commands.append)

    assert inspector.max_size_spinbox.text() == "Unlimited"
    assert inspector.max_size_spinbox.accessibleName() == "Maximum file size"
    assert "Unlimited" in inspector.max_size_spinbox.accessibleDescription()

    inspector.max_size_spinbox.setValue(1.25)

    assert inspector.max_size_spinbox.text().endswith(" MiB")
    assert inspector.max_size_spinbox.text() != "Unlimited"
    assert commands[-1].value == Decimal("1.25")


def test_uncached_selected_model_shows_full_download_name_and_size(qtbot) -> None:
    inspector = Inspector(_settings(), model_options=(("u2netp", True),))
    qtbot.addWidget(inspector)
    inspector.show()
    inspector.set_model_status("not_cached")

    assert inspector.model_download_notice.text() == (
        "BiRefNet Portrait — 927.6 MiB download required"
    )
    assert inspector.model_download_notice.isVisible()
    assert inspector.model_download_notice.accessibleDescription() == (
        "BiRefNet Portrait — 927.6 MiB download required"
    )

    inspector.model_picker.setCurrentIndex(inspector.model_picker.findData("u2netp"))

    assert not inspector.model_download_notice.isVisible()


def test_inspector_emits_parameter_commands_from_standard_controls(qtbot) -> None:
    inspector = Inspector(_settings())
    qtbot.addWidget(inspector)
    commands: list[object] = []
    inspector.command_requested.connect(commands.append)

    inspector.fps_spinbox.setValue(60)
    inspector.trim_checkbox.setChecked(True)
    inspector.padding_spinbox.setValue(4)

    assert [type(command).__name__ for command in commands] == [
        "OutputFpsChanged",
        "GlobalTrimChanged",
        "PaddingChanged",
    ]


def test_exclusion_controls_toggle_edit_mode_and_clear_regions(qtbot) -> None:
    inspector = Inspector(_settings())
    qtbot.addWidget(inspector)
    state = AppState(
        parameters=ParameterState(exclusions=(CropSpec(10, 10, 20, 20),)),
        timeline=TimelineState(Fraction(4), Fraction(0), Fraction(4), Fraction(0)),
    )
    inspector.apply_parameters(present_parameters(state), editable=True)
    commands: list[object] = []
    modes: list[bool] = []
    inspector.command_requested.connect(commands.append)
    inspector.exclusion_controls.edit_toggled.connect(modes.append)

    controls = inspector.exclusion_controls
    assert controls.edit_button.text() == "Edit"
    assert controls.edit_button.accessibleName() == "Edit exclusion regions"
    assert controls.edit_button.toolTip() == "Edit exclusion regions"
    assert controls.clear_button.text() == "Clear"
    assert controls.clear_button.accessibleName() == "Clear exclusion regions"
    assert controls.count_label.text() == "1 region(s)"
    assert controls.clear_button.isEnabled()

    controls.edit_button.click()
    controls.clear_button.click()

    assert modes == [True]
    assert commands == [ExclusionsChanged(())]

    inspector.apply_parameters(present_parameters(state), editable=False)

    assert modes == [True, False]
    assert not controls.edit_button.isEnabled()
    assert not controls.clear_button.isEnabled()


def test_inspector_emits_edge_mode_from_the_standard_combo(qtbot) -> None:
    inspector = Inspector(_settings())
    qtbot.addWidget(inspector)
    commands: list[object] = []
    inspector.command_requested.connect(commands.append)

    inspector.edge_picker.setCurrentIndex(1)

    assert [type(command).__name__ for command in commands] == ["EdgeModeChanged"]


def test_inspector_exposes_reset_action_and_explains_what_it_preserves(qtbot) -> None:
    inspector = Inspector(_settings())
    qtbot.addWidget(inspector)
    commands: list[object] = []
    inspector.command_requested.connect(commands.append)
    state = AppState(
        parameters=ParameterState(model_id="u2net"),
        timeline=TimelineState(Fraction(4), Fraction(0), Fraction(4), Fraction(0)),
    )
    inspector.apply_parameters(present_parameters(state), editable=True)

    assert inspector.reset_parameters_button.text() == "Reset to defaults"
    assert inspector.reset_parameters_button.accessibleName() == (
        "Reset inspector parameters"
    )
    assert inspector.reset_parameters_button.toolTip() == (
        "Compute acceleration, transform, output directory, and output filename "
        "are not affected."
    )

    inspector.reset_parameters_button.click()

    assert commands == [ParametersReset()]


def test_inspector_reset_updates_widgets_without_disturbing_other_editors(
    qtbot,
) -> None:
    settings = _settings()
    parameters = ParameterState(
        model_id="u2net",
        edge_mode=EdgeMode.DECONTAMINATE_COLORS,
        execution_provider="CUDAExecutionProvider",
        fps=90,
        trim=True,
        alpha_threshold=Decimal("0.4"),
        padding=1,
        stretch_x=Decimal("1.2"),
        output_directory=Path("exports"),
        output_filename="chosen.webp",
        max_mib=Decimal("12.5"),
        transform=TransformSpec(first_frame=1, crop=CropSpec(4, 5, 80, 70)),
    )
    timeline = TimelineState(
        Fraction(4), Fraction(1, 2), Fraction(7, 2), Fraction(1), fps=90
    )
    state = AppState(
        source=SourceState.READY,
        source_id="source",
        source_value=object(),
        timeline=timeline,
        parameters=parameters,
        crop=CropSpec(8, 9, 100, 90),
        crop_enabled=False,
    )
    store = ReducerStore(state)
    controller = SourceController.__new__(SourceController)
    controller._store = store
    controller._settings = settings
    controller._working_provider = parameters.execution_provider
    controller._failed_provider = None
    controller._closed = False
    inspector = Inspector(settings)
    qtbot.addWidget(inspector)

    def render(updated: AppState) -> None:
        inspector.apply_parameters(present_parameters(updated), editable=True)

    store.subscribe(render)
    inspector.command_requested.connect(controller.dispatch)
    inspector.apply_parameters(present_parameters(state), editable=True)
    inspector.reset_parameters_button.click()

    defaults = ParameterState()
    assert inspector.model_picker.currentData() == defaults.model_id
    assert inspector.edge_picker.currentData() == defaults.edge_mode.value
    assert inspector.fps_spinbox.value() == defaults.fps
    assert inspector.trim_checkbox.isChecked() is defaults.trim
    assert inspector.alpha_threshold_spinbox.value() == float(defaults.alpha_threshold)
    assert inspector.padding_spinbox.value() == defaults.padding
    assert inspector.stretch_spinbox.value() == float(defaults.stretch_x)
    assert inspector.max_size_spinbox.value() == float(defaults.max_mib)
    assert store.state.parameters.execution_provider == parameters.execution_provider
    assert store.state.parameters.output_directory == parameters.output_directory
    assert store.state.parameters.output_filename == parameters.output_filename
    assert store.state.parameters.transform == parameters.transform
    assert store.state.crop == state.crop
    assert store.state.crop_enabled is state.crop_enabled
    assert store.state.timeline is not None
    assert store.state.timeline.start == timeline.start
    assert store.state.timeline.end == timeline.end
    assert store.state.timeline.playhead == timeline.playhead


def test_inspector_exposes_and_emits_output_directory_clear(qtbot) -> None:
    inspector = Inspector(_settings())
    qtbot.addWidget(inspector)
    commands: list[object] = []
    inspector.command_requested.connect(commands.append)
    state = AppState(
        parameters=ParameterState(output_directory=Path("exports")),
        timeline=TimelineState(Fraction(4), Fraction(0), Fraction(4), Fraction(0)),
    )
    inspector.apply_parameters(present_parameters(state), editable=True)

    assert inspector.clear_output_directory_button.isEnabled()
    inspector.clear_output_directory_button.click()

    assert [type(command).__name__ for command in commands] == [
        "OutputDirectoryChanged"
    ]
    assert commands[0].directory is None


def test_inspector_disables_clear_without_an_output_directory_override(qtbot) -> None:
    inspector = Inspector(_settings())
    qtbot.addWidget(inspector)
    commands: list[object] = []
    inspector.command_requested.connect(commands.append)
    state = AppState(
        timeline=TimelineState(Fraction(4), Fraction(0), Fraction(4), Fraction(0))
    )
    inspector.apply_parameters(present_parameters(state), editable=True)

    assert not inspector.clear_output_directory_button.isEnabled()
    inspector.clear_output_directory_button.click()

    assert commands == []


def test_inspector_disables_reset_when_all_resettable_values_are_default(qtbot) -> None:
    inspector = Inspector(_settings())
    qtbot.addWidget(inspector)
    state = AppState(
        timeline=TimelineState(Fraction(4), Fraction(0), Fraction(4), Fraction(0))
    )
    inspector.apply_parameters(present_parameters(state), editable=True)

    assert not inspector.reset_parameters_button.isEnabled()


def test_inspector_places_reset_after_the_disclosures_and_at_the_end_of_tab_order(
    qtbot,
) -> None:
    inspector = Inspector(_settings())
    qtbot.addWidget(inspector)

    assert (
        inspector.reset_parameters_button.parentWidget()
        is inspector.scroll_area.widget()
    )
    assert inspector.tab_widgets()[-1] is inspector.reset_parameters_button


def test_inspector_explains_invalid_output_filename_without_emitting_a_command(
    qtbot,
) -> None:
    inspector = Inspector(_settings())
    qtbot.addWidget(inspector)
    commands: list[object] = []
    inspector.command_requested.connect(commands.append)

    inspector.output_filename_edit.setText("not-a-webp.txt")
    inspector.output_filename_edit.editingFinished.emit()

    assert commands == []
    assert inspector.output_filename_edit.property("invalid") is True
    assert "single" in inspector.output_filename_edit.toolTip()


def test_every_inspector_field_refuses_focus_from_the_wheel(qtbot) -> None:
    """Every combo and spin box in the inspector, Transform panel included,
    goes through compact_field: focus comes from a click or Tab only, so a
    wheel tick over an unfocused field scrolls the inspector instead of
    focusing and editing it."""
    inspector = Inspector(_settings())
    qtbot.addWidget(inspector)

    fields = [
        *inspector.findChildren(QComboBox),
        *inspector.findChildren(QAbstractSpinBox),
    ]

    assert len(fields) >= 20
    assert {field.focusPolicy() for field in fields} == {Qt.FocusPolicy.StrongFocus}
