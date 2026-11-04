"""Playback row under the Original pane and the timeline that can hold a position."""

from __future__ import annotations

from dataclasses import replace
from fractions import Fraction

from PySide6.QtCore import QCoreApplication, Qt, Signal
from PySide6.QtGui import QFont, QKeyEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QWidget,
)

from matteloop.ui.presentation_model import PresentationModel
from matteloop.ui.source_presentation import format_localized_timecode
from matteloop.ui.theme import MONO_FONT
from matteloop.ui.timeline import TimelineWidget
from matteloop.ui.timeline_presentation import TimelinePresentation


class PlaybackControls(QWidget):
    """Play/Pause, a timecode and the ``Selection only`` loop switch."""

    play_toggled = Signal(bool)
    selection_only_toggled = Signal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("playback_controls")
        self._reducer_timecode = format_localized_timecode(Fraction(0))
        self._position: Fraction | None = None
        self.play_button = QPushButton(
            QCoreApplication.translate("PlaybackControls", "Play"), self
        )
        self.play_button.setObjectName("source_play")
        self.play_button.setAccessibleName(
            QCoreApplication.translate("PlaybackControls", "Play the video")
        )
        self.play_button.setCheckable(True)
        self.timecode = QLabel(self._reducer_timecode, self)
        self.timecode.setObjectName("playback_timecode")
        self.timecode.setFont(QFont(MONO_FONT, 9))
        self.timecode.setProperty("mono", True)
        self.selection_only = QCheckBox(
            QCoreApplication.translate("PlaybackControls", "Selection only"), self
        )
        self.selection_only.setObjectName("playback_selection_only")
        self.selection_only.setAccessibleName(
            QCoreApplication.translate(
                "PlaybackControls", "Loop playback inside the export range"
            )
        )
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        layout.addWidget(self.play_button)
        layout.addWidget(self.timecode)
        layout.addStretch(1)
        layout.addWidget(self.selection_only)
        self.play_button.clicked.connect(self.play_toggled)
        self.selection_only.toggled.connect(self.selection_only_toggled)

    @property
    def position(self) -> Fraction | None:
        return self._position

    @property
    def selection_only_enabled(self) -> bool:
        return self.selection_only.isChecked()

    def apply(self, model: PresentationModel) -> None:
        """Follow the reducer's visibility and lock state and its playhead."""
        self.setVisible(model.show_timeline)
        self.setEnabled(model.timeline is not None and not model.editor_locked)
        playhead = (
            model.timeline.state.playhead if model.timeline is not None else Fraction(0)
        )
        self._reducer_timecode = format_localized_timecode(playhead)
        if self._position is None:
            self.timecode.setText(self._reducer_timecode)

    def set_playing(self, playing: bool) -> None:
        """Keep checked state, text and accessible name in step; never emits."""
        if self.play_button.isChecked() != playing:
            self.play_button.setChecked(playing)
        self.play_button.setText(
            QCoreApplication.translate("PlaybackControls", "Pause")
            if playing
            else QCoreApplication.translate("PlaybackControls", "Play")
        )
        self.play_button.setAccessibleName(
            QCoreApplication.translate("PlaybackControls", "Pause the video")
            if playing
            else QCoreApplication.translate("PlaybackControls", "Play the video")
        )

    def set_position(self, position: Fraction | None) -> None:
        self._position = position
        self.timecode.setText(
            self._reducer_timecode
            if position is None
            else format_localized_timecode(position)
        )


class PlaybackTimeline(TimelineWidget):
    """Timeline that can show a held playhead without touching the reducer."""

    def __init__(self, parent: QFrame | None = None) -> None:
        super().__init__(parent)
        self._reducer_presentation: TimelinePresentation | None = None
        self._held: Fraction | None = None

    @property
    def held_playhead(self) -> Fraction | None:
        return self._held

    def hold_playhead(self, position: Fraction | None) -> None:
        self._held = position
        self.set_presentation(self._reducer_presentation)

    def set_presentation(self, presentation: TimelinePresentation | None) -> None:
        self._reducer_presentation = presentation
        if self._held is not None and presentation is not None:
            state = presentation.state
            held = min(max(self._held, Fraction(0)), state.duration)
            presentation = replace(
                presentation, state=replace(state, playhead=held)
            )
        super().set_presentation(presentation)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        """A held `I` or `O` sets the range once, like the window route."""
        if event.isAutoRepeat() and event.key() in (Qt.Key.Key_I, Qt.Key.Key_O):
            event.accept()
            return
        super().keyPressEvent(event)
