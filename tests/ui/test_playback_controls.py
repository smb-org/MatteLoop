from __future__ import annotations

from dataclasses import replace
from fractions import Fraction
from pathlib import Path

from PySide6.QtCore import Qt

from matteloop.core.state import AppState, SourceLoaded, SourceLoadRequested, reduce
from matteloop.core.timeline import TimelineState
from matteloop.ui.playback_controls import PlaybackControls, PlaybackTimeline
from matteloop.ui.presentation_model import PresentationModel
from matteloop.ui.presenter import present
from matteloop.ui.timeline_presentation import TimelinePresentation


def _presentation(*, playhead: Fraction = Fraction(0)) -> TimelinePresentation:
    state = TimelineState(
        Fraction(4), Fraction(1, 2), Fraction(3, 2), playhead, source_fps=Fraction(30)
    )
    return TimelinePresentation(
        state, "source", Path("source.mp4"), 128, 128, None, None
    )


def _ready_state() -> AppState:
    return reduce(
        reduce(AppState(), SourceLoadRequested("source", "load")),
        SourceLoaded("source", "load", "metadata"),
    )


def _model(
    *,
    playhead: Fraction | None = Fraction(1),
    show_timeline: bool = True,
    editor_locked: bool = False,
) -> PresentationModel:
    return replace(
        present(_ready_state()),
        show_timeline=show_timeline,
        editor_locked=editor_locked,
        timeline=None if playhead is None else _presentation(playhead=playhead),
    )


def test_play_button_text_and_accessible_name_follow_set_playing(qtbot) -> None:
    controls = PlaybackControls()
    qtbot.addWidget(controls)

    controls.set_playing(True)
    assert controls.play_button.text() == "Pause"
    assert controls.play_button.accessibleName() == "Pause the video"
    assert controls.play_button.isChecked()

    controls.set_playing(False)
    assert controls.play_button.text() == "Play"
    assert controls.play_button.accessibleName() == "Play the video"
    assert not controls.play_button.isChecked()


def test_set_playing_never_emits_play_toggled_but_a_click_does(qtbot) -> None:
    controls = PlaybackControls()
    qtbot.addWidget(controls)
    controls.show()
    emitted: list[bool] = []
    controls.play_toggled.connect(emitted.append)

    controls.set_playing(True)
    assert emitted == []
    controls.set_playing(False)

    qtbot.mouseClick(controls.play_button, Qt.MouseButton.LeftButton)
    qtbot.mouseClick(controls.play_button, Qt.MouseButton.LeftButton)
    assert emitted == [True, False]


def test_apply_shows_the_reducer_timecode_until_a_position_is_set(qtbot) -> None:
    controls = PlaybackControls()
    qtbot.addWidget(controls)
    model = _model()

    controls.apply(model)
    assert controls.timecode.text() == "0:01.000"
    assert controls.isEnabled()

    controls.set_position(Fraction(3, 2))
    controls.apply(model)
    assert controls.timecode.text() == "0:01.500"
    assert controls.position == Fraction(3, 2)

    controls.set_position(None)
    assert controls.timecode.text() == "0:01.000"
    assert controls.position is None


def test_row_is_disabled_while_the_editor_is_locked_or_no_timeline_exists(
    qtbot,
) -> None:
    controls = PlaybackControls()
    qtbot.addWidget(controls)
    controls.show()

    controls.apply(_model(editor_locked=True))
    assert not controls.isEnabled()

    controls.apply(_model(playhead=None))
    assert not controls.isEnabled()
    assert controls.timecode.text() == "0:00.000"

    controls.apply(_model(show_timeline=False))
    assert controls.isHidden()


def test_selection_only_relays_the_check_box(qtbot) -> None:
    controls = PlaybackControls()
    qtbot.addWidget(controls)
    controls.show()
    emitted: list[bool] = []
    controls.selection_only_toggled.connect(emitted.append)

    qtbot.mouseClick(controls.selection_only, Qt.MouseButton.LeftButton)

    assert emitted == [True]
    assert controls.selection_only_enabled is True


def test_playback_timeline_substitutes_and_releases_the_held_playhead(qtbot) -> None:
    widget = PlaybackTimeline()
    qtbot.addWidget(widget)
    widget.resize(900, 176)
    widget.set_presentation(_presentation(playhead=Fraction(0)))

    widget.hold_playhead(Fraction(2))
    assert widget.held_playhead == Fraction(2)
    assert widget.timecode_label.text() == "0:02.000"
    assert widget.frame_label.text() == "Frame 000061"
    assert widget.range_status_label.text() == "Outside export range"

    widget.set_presentation(_presentation(playhead=Fraction(1)))
    assert widget.timecode_label.text() == "0:02.000"

    widget.hold_playhead(None)
    assert widget.held_playhead is None
    assert widget.timecode_label.text() == "0:01.000"


def test_holding_the_playhead_does_not_restart_thumbnails(qtbot) -> None:
    widget = PlaybackTimeline()
    qtbot.addWidget(widget)
    widget.resize(900, 176)
    widget.set_presentation(_presentation(playhead=Fraction(0)))
    before = (
        widget._thumbnail_generation,  # noqa: SLF001
        widget._thumbnail_signature,  # noqa: SLF001
    )

    widget.hold_playhead(Fraction(2))
    held = (
        widget._thumbnail_generation,  # noqa: SLF001
        widget._thumbnail_signature,  # noqa: SLF001
    )
    widget.hold_playhead(None)
    released = (
        widget._thumbnail_generation,  # noqa: SLF001
        widget._thumbnail_signature,  # noqa: SLF001
    )

    assert before == held == released


def test_a_held_playhead_beyond_the_duration_is_clamped(qtbot) -> None:
    widget = PlaybackTimeline()
    qtbot.addWidget(widget)
    widget.resize(900, 176)
    widget.set_presentation(_presentation())

    widget.hold_playhead(Fraction(5))

    assert widget.timecode_label.text() == "0:04.000"
