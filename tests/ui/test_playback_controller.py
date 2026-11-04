from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest
from PIL import Image
from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QSettings, Qt, Slot
from PySide6.QtGui import QImage, QKeyEvent, QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFileDialog

from matteloop.core.errors import AppError, ErrorCode
from matteloop.core.state import (
    AppState,
    PreviewInvalidationReason,
    PreviewRequested,
    PreviewResult,
    PreviewState,
    PreviewSucceeded,
    SourceLoaded,
    SourceLoadRequested,
    reduce,
)
from matteloop.core.timeline import (
    EndChanged,
    PlayheadChanged,
    SetEndToPlayhead,
    SetStartToPlayhead,
    SourceFrameDecoded,
    StartChanged,
    StepFrame,
)
from matteloop.jobs.playback import PlaybackFrame
from matteloop.jobs.source import probe_source
from matteloop.ui.controller import SourceController
from matteloop.ui.main_window import MainWindow
from matteloop.ui.playback_controller import SourcePlaybackController
from matteloop.ui.ports import ChooseVideoRequested
from matteloop.ui.store import ReducerStore
from matteloop.ui.worker_thread import threads_outliving_shutdown
from tests.fixtures.media_factory import make_video

STEP = Fraction(1, 100)
DECODE_MARKER = "Playback stopped — the video could not be decoded further"
CHANGED_MARKER = "Playback stopped — the video file changed on disk"


@dataclass
class Script:
    """What the next fake decoder does; tests edit it before pressing Play."""

    end: Fraction = Fraction(1)
    pts_list: list[Fraction] | None = None
    gate: threading.Event | None = None
    gate_at: int = -1
    block: threading.Event | None = None
    fail_at: int | None = None
    fail_error: Exception | None = None
    open_error: Exception | None = None
    close_error: Exception | None = None
    convert_delay: float = 0.0
    preroll: float = 0.0


class FakeDecoder:
    def __init__(self, script: Script, log: list[str]) -> None:
        self.script = script
        self.log = log
        self.seeks: list[Fraction] = []
        self.converted: list[Fraction] = []
        self.seek_marks: list[int] = []
        self.yielded = 0
        self._position = Fraction(0)

    def open(self) -> None:
        self.log.append("open")
        if self.script.open_error is not None:
            raise self.script.open_error

    def seek(self, timestamp: Fraction) -> None:
        self.log.append(f"seek {timestamp}")
        self.seeks.append(timestamp)
        self.seek_marks.append(len(self.converted))
        # The real decoder starts at the frame owning the target, never after it.
        self._position = (timestamp // STEP) * STEP

    def frames(self) -> Iterator[tuple[Fraction, object]]:
        script = self.script
        if script.block is not None:
            script.block.wait(5)
        pts = self._position
        index = 0
        if script.preroll:
            time.sleep(script.preroll)
        while True:
            if script.pts_list is not None:
                if index >= len(script.pts_list):
                    return
                current = script.pts_list[index]
            else:
                if pts >= script.end:
                    return
                current = pts
            if script.fail_at == index:
                raise script.fail_error or AppError(
                    ErrorCode.SOURCE_CORRUPT, "s", "s.failed", "boom", "reload"
                )
            if script.gate is not None and script.gate_at == index:
                script.gate.wait(5)
            self.yielded += 1
            yield current, index
            index += 1
            pts += STEP

    def convert(self, frame: object) -> PlaybackFrame:
        if self.script.convert_delay:
            time.sleep(self.script.convert_delay)
        index = int(frame) % 256  # type: ignore[call-overload]
        pts = self._position + STEP * int(frame)  # type: ignore[call-overload]
        self.converted.append(pts)
        return PlaybackFrame(
            pts, bytes([index, 0, 0, 255]) * 16, 4, 4
        )

    def close(self) -> None:
        self.log.append("close")
        if self.script.close_error is not None:
            raise self.script.close_error


class Factory:
    def __init__(self) -> None:
        self.script = Script()
        self.calls: list[tuple[Any, ...]] = []
        self.decoders: list[FakeDecoder] = []
        self.log: list[str] = []

    def __call__(self, path, proof, revision, display_size) -> FakeDecoder:
        self.calls.append((path, proof, revision, display_size))
        decoder = FakeDecoder(self.script, self.log)
        self.decoders.append(decoder)
        return decoder


@dataclass(frozen=True)
class _BareMetadata:
    """Enough for a timeline but no path, revision or validation proof."""

    path: Path = Path("bare.mp4")
    width: int = 16
    height: int = 8
    duration: Fraction = Fraction(1)
    average_rate: Fraction = Fraction(100)


def _finished(thread: Any) -> bool:
    """Whether the thread is done; a retired (deleted) wrapper counts as done."""
    try:
        return bool(thread.isFinished())
    except RuntimeError:
        return True


@dataclass
class Rig:
    store: ReducerStore
    window: MainWindow
    factory: Factory
    controller: SourcePlaybackController
    events: list[object] = field(default_factory=list)
    still: QImage = field(default_factory=QImage)

    @property
    def canvas(self):
        return self.window.original_canvas

    @property
    def controls(self):
        return self.window.playback_controls

    @property
    def timeline(self):
        return self.window.timeline_widget

    def play_until_frame(self, qtbot, frames: int = 1) -> None:
        self.controller.play()
        qtbot.waitUntil(
            lambda: self.factory.decoders and self.factory.decoders[-1].yielded
            >= frames and self.controller.visible_position is not None,
            timeout=5000,
        )


def _settings(name: str) -> QSettings:
    settings = QSettings(
        QSettings.IniFormat, QSettings.UserScope, "matteloop-test", name
    )
    settings.clear()
    return settings


def _state(
    tmp_path: Path,
    *,
    bare: bool = False,
    with_preview: bool = False,
    fps: int | None = None,
):
    path = make_video(
        tmp_path / "source.mp4",
        [Image.new("RGB", (16, 8), "red"), Image.new("RGB", (16, 8), "green")],
        Fraction(2),
    )
    metadata: object = (
        _BareMetadata()
        if bare
        else replace(probe_source(path), average_rate=Fraction(100))
    )
    still = QImage(16, 8, QImage.Format.Format_RGBA8888)
    still.fill(0xFF00FF00)
    state = reduce(AppState(), SourceLoadRequested("source", "load"))
    state = reduce(state, SourceLoaded("source", "load", metadata, still))
    if fps is not None:
        state = replace(state, timeline=replace(state.timeline, fps=fps))
    if with_preview:
        state = reduce(state, PreviewRequested("preview", "preview-request"))
        state = reduce(
            state,
            PreviewSucceeded(
                "preview", PreviewResult("source", "preview-request", "result")
            ),
        )
    return state, still


class _Services:
    def __init__(self) -> None:
        self.controller: Any = None

    def dispatch(self, command: object) -> None:
        pass

    def toggle_playback(self) -> None:
        self.controller.toggle()


@pytest.fixture
def make_rig(qtbot, tmp_path):
    rigs: list[Rig] = []

    def build(name: str = "playback", **state_options: Any) -> Rig:
        state, still = _state(tmp_path, **state_options)
        store = ReducerStore(state)
        services = _Services()
        window = MainWindow(store, services, _settings(name))  # type: ignore[arg-type]
        qtbot.addWidget(window)
        window.show()
        factory = Factory()
        events: list[object] = []
        original = store.dispatch

        def record(event: object) -> None:
            events.append(event)
            original(event)  # type: ignore[arg-type]

        store.dispatch = record  # type: ignore[method-assign]
        controller = SourcePlaybackController(
            store,
            commit=lambda position: store.dispatch(PlayheadChanged(position)),
            decoder_factory=factory,
        )
        controller.attach(
            window.playback_controls,
            window.original_canvas,
            window.timeline_widget,
        )
        services.controller = controller
        rig = Rig(store, window, factory, controller, events, still)
        rigs.append(rig)
        return rig

    yield build
    for rig in rigs:
        rig.controller.shutdown()


def _playhead_events(rig: Rig) -> list[PlayheadChanged]:
    return [e for e in rig.events if isinstance(e, PlayheadChanged)]


def test_play_shows_live_frames_and_holds_marker_and_timecode(qtbot, make_rig) -> None:
    rig = make_rig()
    rig.play_until_frame(qtbot)

    assert rig.canvas.holding_live_frame
    assert rig.timeline.held_playhead == rig.controller.visible_position
    assert rig.controls.position == rig.controller.visible_position
    assert rig.controls.play_button.text() == "Pause"
    assert _playhead_events(rig) == []


def test_pause_commits_exactly_one_playhead_change_and_marks_the_preview_stale_once(
    qtbot, make_rig
) -> None:
    rig = make_rig(with_preview=True)
    assert rig.store.state.preview is PreviewState.CURRENT
    rig.play_until_frame(qtbot, 3)

    rig.controller.pause()

    committed = _playhead_events(rig)
    assert len(committed) == 1
    position = committed[0].timestamp
    assert rig.store.state.timeline.playhead == position
    assert rig.store.state.preview is PreviewState.STALE
    assert rig.store.state.stale_category is PreviewInvalidationReason.PLAYHEAD
    assert rig.controls.play_button.text() == "Play"
    assert rig.canvas.holding_live_frame


def test_the_still_after_the_commit_releases_the_holds(qtbot, make_rig) -> None:
    rig = make_rig()
    rig.play_until_frame(qtbot, 3)
    rig.controller.pause()
    new_image = QImage(16, 8, QImage.Format.Format_RGBA8888)
    new_image.fill(0xFF0000FF)

    rig.store.dispatch(
        SourceFrameDecoded(
            "source", rig.store.state.timeline.generation, new_image
        )
    )

    assert not rig.canvas.holding_live_frame
    assert rig.canvas._frame == new_image
    assert rig.timeline.held_playhead is None
    assert rig.controls.position is None


class _TerminalSpy(QObject):
    """Records terminal signals on the GUI thread, like the real queued slots."""

    def __init__(self, rig: Rig) -> None:
        super().__init__()
        self.rig = rig
        self.seen: list[str] = []

    @Slot(int, str)
    def failed(self, _session: int, token: str) -> None:
        closed = "close" in self.rig.factory.log
        self.seen.append(f"failed:{token}:closed={closed}")

    @Slot(int, object)
    def ended(self, _session: int, _last: object) -> None:
        self.seen.append("ended")


def test_close_failure_after_the_last_frame_reports_the_changed_source(
    qtbot, make_rig
) -> None:
    rig = make_rig()
    gate = threading.Event()
    rig.factory.script = Script(
        end=STEP * 3,
        gate=gate,
        gate_at=0,
        close_error=AppError(
            ErrorCode.SOURCE_CHANGED, "s", "s.changed", "changed", "reload"
        ),
    )
    spy = _TerminalSpy(rig)
    rig.controller.play()
    worker = rig.controller._threads[0][1]
    # The worker waits on the gate, so the spy is connected before any signal.
    worker.failed.connect(spy.failed)
    worker.ended.connect(spy.ended)
    gate.set()
    qtbot.waitUntil(lambda: not rig.controller.playing, timeout=5000)
    qtbot.waitUntil(lambda: bool(spy.seen), timeout=5000)

    assert spy.seen == ["failed:source-changed:closed=True"]
    assert rig.canvas.status_label.text() == CHANGED_MARKER
    assert rig.canvas.status_label.isVisible()


def _replace_source(rig: Rig, name: str) -> None:
    rig.store.dispatch(SourceLoadRequested(name, f"{name}-load"))


def test_the_marker_clears_when_the_video_is_replaced_after_an_open_failure(
    qtbot, make_rig
) -> None:
    rig = make_rig()
    rig.factory.script = Script(
        open_error=AppError(
            ErrorCode.SOURCE_CHANGED, "s", "s.changed", "changed", "reload"
        )
    )
    rig.controller.play()
    qtbot.waitUntil(lambda: not rig.controller.playing, timeout=5000)
    assert rig.canvas.status_label.isVisible()
    rig.store.dispatch(StartChanged(Fraction(1, 10)))
    assert rig.canvas.status_label.isVisible()

    _replace_source(rig, "other")

    assert not rig.canvas.status_label.isVisible()
    assert rig.canvas.status_label.text() == ""


def test_the_marker_clears_when_the_video_is_replaced_after_a_settled_failure(
    qtbot, make_rig
) -> None:
    rig = make_rig()
    rig.factory.script = Script(fail_at=2)
    rig.controller.play()
    qtbot.waitUntil(lambda: not rig.controller.playing, timeout=5000)
    rig.store.dispatch(
        SourceFrameDecoded("source", rig.store.state.timeline.generation, QImage())
    )
    assert not rig.canvas.holding_live_frame
    assert rig.canvas.status_label.text() == DECODE_MARKER

    _replace_source(rig, "other")

    assert not rig.canvas.status_label.isVisible()
    assert rig.canvas.status_label.text() == ""


def test_a_pts_beyond_the_metadata_duration_shows_one_position_everywhere(
    qtbot, make_rig
) -> None:
    rig = make_rig()
    rig.factory.script = Script(pts_list=[Fraction(5)])
    timeline = rig.store.state.timeline
    latest = timeline.duration - timeline.source_frame_interval
    rig.controller.play()
    qtbot.waitUntil(lambda: rig.controller.visible_position is not None, timeout=5000)

    assert rig.controller.visible_position == latest
    assert rig.timeline.held_playhead == latest
    assert rig.controls.position == latest
    qtbot.waitUntil(lambda: not rig.controller.playing, timeout=5000)
    committed = _playhead_events(rig)
    assert [e.timestamp for e in committed] == [latest]
    assert rig.store.state.timeline.playhead == latest


def test_a_state_render_during_playback_keeps_the_live_frame(qtbot, make_rig) -> None:
    rig = make_rig()
    rig.play_until_frame(qtbot, 3)
    held = rig.controller.visible_position
    frame_before = rig.canvas._frame
    row_timecode = rig.controls.timecode.text()

    rig.store.dispatch(StartChanged(Fraction(1, 10)))

    assert rig.canvas.holding_live_frame
    assert rig.canvas._frame is frame_before
    assert rig.controller.visible_position == held
    assert rig.timeline.timecode_label.text() == rig.controls.timecode.text()
    assert rig.controls.timecode.text() == row_timecode


def test_a_decode_finishing_after_pause_changes_nothing(qtbot, make_rig) -> None:
    rig = make_rig()
    gate = threading.Event()
    rig.factory.script = Script(gate=gate, gate_at=2)
    rig.controller.play()
    qtbot.waitUntil(lambda: rig.controller.visible_position is not None, timeout=5000)
    qtbot.waitUntil(
        lambda: rig.factory.decoders[0].yielded >= 2, timeout=5000
    )
    rig.controller.pause()
    frame = rig.canvas._frame
    committed = len(_playhead_events(rig))
    state = rig.store.state

    gate.set()
    qtbot.wait(50)

    assert rig.canvas._frame is frame
    assert len(_playhead_events(rig)) == committed
    assert rig.store.state is state
    assert rig.controller.active_session is None


def test_a_late_ended_or_failed_from_an_old_session_is_ignored(
    qtbot, make_rig
) -> None:
    rig = make_rig()
    rig.factory.script = Script(end=Fraction(5))
    rig.play_until_frame(qtbot)
    old = rig.controller.active_session
    assert old is not None
    rig.controller.pause()
    committed = len(_playhead_events(rig))
    rig.controller.play()
    assert rig.controller.active_session != old

    rig.controller._ended(old, Fraction(1))
    rig.controller._failed(old, "decode")

    assert rig.controller.playing
    assert not rig.canvas.status_label.isVisible()
    assert len(_playhead_events(rig)) == committed


def test_end_of_stream_commits_the_last_frame_and_restores_play(
    qtbot, make_rig
) -> None:
    rig = make_rig()
    rig.factory.script = Script(end=STEP * 5)
    rig.controller.play()
    qtbot.waitUntil(lambda: not rig.controller.playing, timeout=5000)
    assert rig.controls.play_button.text() == "Play"

    committed = _playhead_events(rig)
    assert [e.timestamp for e in committed] == [STEP * 4]
    assert not rig.controls.play_button.isChecked()


def test_play_at_the_end_of_the_video_restarts_from_zero(qtbot, make_rig) -> None:
    rig = make_rig()
    timeline = rig.store.state.timeline
    rig.store.dispatch(PlayheadChanged(timeline.duration - STEP))
    assert rig.store.state.timeline.playhead == timeline.duration - STEP

    rig.controller.play()
    qtbot.waitUntil(lambda: bool(rig.factory.decoders[0].seeks), timeout=5000)

    assert rig.factory.decoders[0].seeks[0] == Fraction(0)


def test_decode_failure_pauses_commits_and_marks_the_canvas(qtbot, make_rig) -> None:
    rig = make_rig()
    rig.factory.script = Script(fail_at=2)
    rig.controller.play()
    qtbot.waitUntil(lambda: not rig.controller.playing, timeout=5000)

    committed = _playhead_events(rig)
    assert [e.timestamp for e in committed] == [STEP]
    assert rig.canvas.status_label.text() == DECODE_MARKER
    assert rig.canvas.status_label.isVisible()


def test_a_changed_source_at_open_marks_the_canvas_without_a_commit(
    qtbot, make_rig
) -> None:
    rig = make_rig()
    rig.factory.script = Script(
        open_error=AppError(
            ErrorCode.SOURCE_CHANGED, "s", "s.changed", "changed", "reload"
        )
    )
    rig.controller.play()
    qtbot.waitUntil(lambda: not rig.controller.playing, timeout=5000)

    assert rig.canvas.status_label.text() == CHANGED_MARKER
    assert _playhead_events(rig) == []
    assert rig.controls.play_button.text() == "Play"
    assert not rig.controls.play_button.isChecked()


def test_the_marker_clears_on_the_next_successful_play(qtbot, make_rig) -> None:
    rig = make_rig()
    rig.factory.script = Script(fail_at=2)
    rig.controller.play()
    qtbot.waitUntil(lambda: not rig.controller.playing, timeout=5000)
    assert rig.canvas.status_label.isVisible()

    rig.factory.script = Script(end=Fraction(5))
    rig.controller.play()

    assert not rig.canvas.status_label.isVisible()
    rig.controller.pause()


def test_late_frames_are_dropped_but_every_fifth_is_shown(
    qtbot, make_rig, caplog
) -> None:
    rig = make_rig()
    rig.factory.script = Script(end=Fraction(5), convert_delay=0.030)
    caplog.set_level(logging.INFO, logger="matteloop.ui.playback_controller")
    rig.controller.play()
    qtbot.wait(300)
    rig.controller.pause()

    decoder = rig.factory.decoders[0]
    qtbot.waitUntil(lambda: rig.factory.log[-1:] == ["close"], timeout=5000)
    assert len(decoder.converted) < decoder.yielded
    assert len(decoder.converted) * 5 >= decoder.yielded - 5
    line = next(
        r.getMessage()
        for r in caplog.records
        if "source playback stopped" in r.getMessage()
    )
    dropped = int(re.search(r"(\d+) dropped", line).group(1))  # type: ignore[union-attr]
    assert dropped > 0


def test_shutdown_with_a_worker_that_ignores_interruption_reports_incomplete_and_retains_the_thread(  # noqa: E501
    qtbot, make_rig, monkeypatch
) -> None:
    monkeypatch.setattr("matteloop.ui.worker_thread.SHUTDOWN_TIMEOUT_MS", 25)
    rig = make_rig()
    release = threading.Event()
    rig.factory.script = Script(block=release)
    before = threads_outliving_shutdown()
    rig.controller.play()
    thread = rig.controller._threads[0][0]
    qtbot.waitUntil(lambda: bool(rig.factory.log), timeout=5000)

    assert rig.controller.shutdown() is False
    assert threads_outliving_shutdown() == before + 1

    release.set()
    qtbot.waitUntil(lambda: _finished(thread), timeout=5000)


def test_clicking_play_when_play_is_a_no_op_leaves_the_button_unchecked(
    qtbot, make_rig
) -> None:
    rig = make_rig(bare=True)
    assert rig.controls.isEnabled()

    rig.controls.play_button.click()

    assert not rig.controller.playing
    assert not rig.controls.play_button.isChecked()
    assert rig.controls.play_button.text() == "Play"


def test_clicking_the_play_button_starts_and_stops_playback(qtbot, make_rig) -> None:
    rig = make_rig()
    rig.factory.script = Script(end=Fraction(5))

    rig.controls.play_button.click()
    qtbot.waitUntil(lambda: rig.controller.visible_position is not None, timeout=5000)
    assert rig.controller.playing
    assert rig.controls.play_button.isChecked()

    rig.controls.play_button.click()
    assert not rig.controller.playing
    assert not rig.controls.play_button.isChecked()
    assert len(_playhead_events(rig)) == 1


class _NoLoadAdapter:
    def load(self, path: Path, request_id: int):
        raise AssertionError("no load expected")


def _source_controller_rig(qtbot, tmp_path, name: str):
    state, _still = _state(tmp_path)
    store = ReducerStore(state)
    factory = Factory()
    controller = SourceController(
        store,
        source_adapter=_NoLoadAdapter(),  # type: ignore[arg-type]
        playback_decoder_factory=factory,
        parent=QApplication.instance(),
    )
    window = MainWindow(store, controller, _settings(name))
    qtbot.addWidget(window)
    controller.set_dialog_parent(window)
    controller.attach_playback(
        window.playback_controls, window.original_canvas, window.timeline_widget
    )
    window.show()
    return store, factory, controller, window


def test_source_controller_stops_playback_before_choosing_a_video(
    qtbot, tmp_path, monkeypatch
) -> None:
    store, factory, controller, _window = _source_controller_rig(
        qtbot, tmp_path, "choose"
    )
    factory.script = Script(end=Fraction(5))
    order: list[str] = []
    original = store.dispatch

    def record(event: object) -> None:
        order.append(type(event).__name__)
        original(event)  # type: ignore[arg-type]

    store.dispatch = record  # type: ignore[method-assign]

    def dialog(*_args: object, **_kwargs: object) -> tuple[str, str]:
        order.append("dialog")
        return "", ""

    monkeypatch.setattr(QFileDialog, "getOpenFileName", dialog)
    controller.toggle_playback()
    qtbot.waitUntil(
        lambda: controller.playback.visible_position is not None, timeout=5000
    )

    controller.dispatch(ChooseVideoRequested())

    assert not controller.playback.playing
    assert order.count("PlayheadChanged") == 1
    assert order.index("PlayheadChanged") < order.index("dialog")
    controller.shutdown()


def test_source_controller_shutdown_includes_playback(
    qtbot, tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr("matteloop.ui.worker_thread.SHUTDOWN_TIMEOUT_MS", 25)
    _store, factory, controller, _window = _source_controller_rig(
        qtbot, tmp_path, "shutdown"
    )
    release = threading.Event()
    factory.script = Script(block=release)
    controller.toggle_playback()
    thread = controller.playback._threads[0][0]
    qtbot.waitUntil(lambda: bool(factory.log), timeout=5000)

    assert controller.shutdown() is False
    assert controller.shutdown_complete is False

    release.set()
    qtbot.waitUntil(lambda: _finished(thread), timeout=5000)


@pytest.fixture
def make_keyed(qtbot, tmp_path):
    """A real SourceController and window, with every dispatched event recorded."""
    controllers: list[SourceController] = []

    def build(name: str, **script: Any):
        store, factory, controller, window = _source_controller_rig(
            qtbot, tmp_path, name
        )
        controllers.append(controller)
        window.raise_()
        window.activateWindow()
        qtbot.waitUntil(window.isActiveWindow, timeout=2000)
        factory.script = Script(end=Fraction(5), **script)
        events: list[object] = []
        original = store.dispatch

        def record(event: object) -> None:
            events.append(event)
            original(event)  # type: ignore[arg-type]

        store.dispatch = record  # type: ignore[method-assign]
        return store, factory, controller, window, events

    yield build
    for controller in controllers:
        controller.shutdown()

def _play_and_wait(qtbot, controller) -> None:
    controller.toggle_playback()
    qtbot.waitUntil(
        lambda: controller.playback.visible_position is not None, timeout=5000
    )


def test_i_and_o_with_focus_on_the_play_button_set_the_range_from_the_visible_frame_while_playing(  # noqa: E501
    qtbot, make_keyed
) -> None:
    store, _factory, controller, window, events = make_keyed("io")
    _play_and_wait(qtbot, controller)
    qtbot.waitUntil(
        lambda: (controller.playback.visible_position or 0) >= 3 * STEP, timeout=5000
    )
    button = window.playback_controls.play_button
    button.setFocus()
    assert QApplication.focusWidget() is button

    visible = controller.playback.visible_position
    QTest.keyClick(button, Qt.Key.Key_I)

    assert store.state.timeline.start == visible
    assert controller.playback.playing
    # The reducer keeps at least one output frame (1/15 s) between IN and OUT.
    far_enough = visible + Fraction(1, 15) + STEP
    qtbot.waitUntil(
        lambda: (controller.playback.visible_position or 0) >= far_enough,
        timeout=5000,
    )
    visible = controller.playback.visible_position
    assert store.state.timeline.playhead == 0
    QTest.keyClick(button, Qt.Key.Key_O)

    assert store.state.timeline.end == visible
    assert controller.playback.playing
    assert [e for e in events if isinstance(e, StartChanged)]
    assert [e for e in events if isinstance(e, EndChanged)]
    assert not [
        e for e in events if isinstance(e, (SetStartToPlayhead, SetEndToPlayhead))
    ]


def test_i_while_paused_with_focus_on_the_play_button_uses_the_reducer_playhead(
    qtbot, make_keyed
) -> None:
    store, _factory, controller, window, events = make_keyed("ip")
    controller.dispatch(PlayheadChanged(Fraction(1, 2)))
    window.playback_controls.play_button.setFocus()

    QTest.keyClick(window.playback_controls.play_button, Qt.Key.Key_I)

    assert store.state.timeline.start == Fraction(1, 2)
    assert any(isinstance(e, SetStartToPlayhead) for e in events)


def test_i_in_an_inspector_spin_box_does_not_touch_the_range(qtbot, make_keyed) -> None:
    store, _factory, controller, window, _events = make_keyed("isp")
    controller.dispatch(PlayheadChanged(Fraction(1, 2)))
    start = store.state.timeline.start
    window.inspector.fps_spinbox.setFocus()

    QTest.keyClick(window.inspector.fps_spinbox, Qt.Key.Key_I)

    assert store.state.timeline.start == start


def test_i_is_ignored_while_the_editor_is_locked(qtbot, make_keyed) -> None:
    store, _factory, controller, window, _events = make_keyed("il")
    controller.dispatch(PlayheadChanged(Fraction(1, 2)))
    start = store.state.timeline.start
    store.dispatch(PreviewRequested("preview", "preview-request"))

    QTest.keyClick(window, Qt.Key.Key_I)

    assert store.state.timeline.start == start


def test_space_on_the_window_toggles_playback_and_auto_repeat_is_ignored(
    qtbot, make_keyed
) -> None:
    _store, _factory, controller, window, _events = make_keyed("sp")
    window.original_canvas.setFocus()

    QTest.keyClick(window.original_canvas, Qt.Key.Key_Space)
    assert controller.playback.playing
    qtbot.waitUntil(
        lambda: controller.playback.visible_position is not None, timeout=5000
    )
    QTest.keyClick(window.original_canvas, Qt.Key.Key_Space)
    assert not controller.playback.playing

    repeat = QKeyEvent(
        QEvent.Type.KeyPress,
        Qt.Key.Key_Space,
        Qt.KeyboardModifier.NoModifier,
        " ",
        True,
    )
    QApplication.sendEvent(window.original_canvas, repeat)
    assert not controller.playback.playing


def test_space_in_a_spin_box_or_on_the_result_canvas_does_not_toggle(
    qtbot, make_keyed
) -> None:
    _store, _factory, controller, window, _events = make_keyed("sps")
    window.inspector.fps_spinbox.setFocus()
    QTest.keyClick(window.inspector.fps_spinbox, Qt.Key.Key_Space)
    assert not controller.playback.playing

    window.result_canvas.setFocus()
    assert QApplication.focusWidget() is window.result_canvas
    QTest.keyClick(window.result_canvas, Qt.Key.Key_Space)
    assert not controller.playback.playing


def test_space_is_ignored_while_the_row_is_disabled(qtbot, make_keyed) -> None:
    store, _factory, controller, window, _events = make_keyed("sd")
    store.dispatch(PreviewRequested("preview", "preview-request"))
    assert not window.playback_controls.isEnabled()

    QTest.keyClick(window, Qt.Key.Key_Space)

    assert not controller.playback.playing
    assert not controller.playback._threads


def test_toggle_playback_does_nothing_after_the_controller_is_closed(
    qtbot, make_keyed
) -> None:
    _store, _factory, controller, window, _events = make_keyed("sc")
    controller.shutdown()

    QTest.keyClick(window, Qt.Key.Key_Space)
    controller.toggle_playback()

    assert not controller.playback.playing
    assert not controller.playback._threads


def test_step_frame_while_playing_pauses_first_and_steps_from_the_visible_frame(
    qtbot, make_keyed
) -> None:
    store, _factory, controller, _window, events = make_keyed("st")
    _play_and_wait(qtbot, controller)
    visible = controller.playback.visible_position
    assert visible is not None
    events.clear()

    controller.dispatch(StepFrame(1))

    assert not controller.playback.playing
    timeline_events = [
        e for e in events if isinstance(e, (PlayheadChanged, StepFrame))
    ]
    assert timeline_events == [PlayheadChanged(visible), StepFrame(1)]
    assert store.state.timeline.playhead == visible + STEP


def test_job_start_stops_playback_without_a_commit(qtbot, make_keyed) -> None:
    gate = threading.Event()
    store, _factory, controller, window, events = make_keyed(
        "job", gate=gate, gate_at=5
    )
    _play_and_wait(qtbot, controller)
    session = controller.playback.active_session
    assert session is not None
    events.clear()

    store.dispatch(PreviewRequested("preview", "preview-request"))

    assert not controller.playback.playing
    assert not [e for e in events if isinstance(e, PlayheadChanged)]
    assert not window.original_canvas.holding_live_frame
    assert window.original_canvas._frame == store.state.source_frame
    assert not window.playback_controls.isEnabled()

    gate.set()
    qtbot.waitUntil(lambda: not controller.playback._threads, timeout=5000)
    QApplication.processEvents()
    controller.playback._frame_available(session)
    assert not window.original_canvas.holding_live_frame
    assert window.original_canvas._frame == store.state.source_frame
    assert not [e for e in events if isinstance(e, PlayheadChanged)]


def test_source_replacement_stops_playback_and_releases_the_holds(
    qtbot, make_keyed
) -> None:
    store, _factory, controller, window, events = make_keyed("rep")
    controller.toggle_playback()
    qtbot.waitUntil(
        lambda: controller.playback.visible_position is not None, timeout=5000
    )
    events.clear()

    store.dispatch(SourceLoadRequested("new", "load"))

    assert not controller.playback.playing
    assert not [e for e in events if isinstance(e, PlayheadChanged)]
    assert not window.original_canvas.holding_live_frame
    assert window.timeline_widget.held_playhead is None
    assert window.playback_controls.position is None


def _auto_repeat(key: Qt.Key, text: str) -> QKeyEvent:
    return QKeyEvent(
        QEvent.Type.KeyPress, key, Qt.KeyboardModifier.NoModifier, text, True
    )


def _set_in_while_playing(qtbot, store, controller, widget) -> Fraction:
    """Play, key `I` once on ``widget``, then let the visible frame move on."""
    _play_and_wait(qtbot, controller)
    qtbot.waitUntil(
        lambda: (controller.playback.visible_position or 0) >= 3 * STEP, timeout=5000
    )
    QTest.keyClick(widget, Qt.Key.Key_I)
    first = store.state.timeline.start
    assert first > 0
    qtbot.waitUntil(
        lambda: (controller.playback.visible_position or 0) >= first + 5 * STEP,
        timeout=5000,
    )
    return first


def test_a_held_i_on_the_window_route_sets_in_once(qtbot, make_keyed) -> None:
    store, _factory, controller, window, _events = make_keyed("rep-window")
    button = window.playback_controls.play_button
    button.setFocus()
    first = _set_in_while_playing(qtbot, store, controller, button)

    QApplication.sendEvent(button, _auto_repeat(Qt.Key.Key_I, "i"))

    assert store.state.timeline.start == first


def test_a_held_i_on_the_timeline_route_sets_in_once(qtbot, make_keyed) -> None:
    store, _factory, controller, window, _events = make_keyed("rep-timeline")
    timeline = window.timeline_widget
    timeline.setFocus()
    assert QApplication.focusWidget() is timeline
    first = _set_in_while_playing(qtbot, store, controller, timeline)

    QApplication.sendEvent(timeline, _auto_repeat(Qt.Key.Key_I, "i"))

    assert store.state.timeline.start == first


def test_clicking_play_moves_focus_to_the_timeline_so_i_and_arrows_act_on_it(
    qtbot, make_keyed
) -> None:
    store, _factory, controller, window, _events = make_keyed("focus")
    picker = window.inspector.model_picker
    model = picker.currentData()
    picker.setFocus()
    assert QApplication.focusWidget() is picker

    window.playback_controls.play_button.click()

    assert QApplication.focusWidget() is window.timeline_widget
    qtbot.waitUntil(
        lambda: (controller.playback.visible_position or 0) >= 3 * STEP, timeout=5000
    )
    QTest.keyClick(QApplication.focusWidget(), Qt.Key.Key_I)
    assert picker.currentData() == model
    assert store.state.timeline.start > 0
    window.playback_controls.play_button.click()
    paused_at = store.state.timeline.playhead

    QTest.keyClick(QApplication.focusWidget(), Qt.Key.Key_Right)

    assert store.state.timeline.playhead == paused_at + STEP


def test_space_does_not_move_focus_off_the_widget_that_had_it(
    qtbot, make_keyed
) -> None:
    _store, _factory, controller, window, _events = make_keyed("space-focus")
    window.original_canvas.setFocus()

    QTest.keyClick(window.original_canvas, Qt.Key.Key_Space)

    assert controller.playback.playing
    assert QApplication.focusWidget() is window.original_canvas


def _strip_point(timeline, fraction: float = 0.5) -> QPoint:
    rect = timeline._geometry.visual["timeline"]
    return QPoint(int(rect.x + rect.width * fraction), int(rect.y + rect.height / 2))


def _drag_to(timeline, point: QPoint) -> None:
    """A move with the left button held; QTest.mouseMove sends no buttons."""
    event = QMouseEvent(
        QEvent.Type.MouseMove,
        QPointF(point),
        QPointF(timeline.mapToGlobal(point)),
        Qt.MouseButton.NoButton,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(timeline, event)


def _press(timeline, point: QPoint) -> None:
    QTest.mousePress(timeline, Qt.MouseButton.LeftButton, pos=point)


def _release(timeline, point: QPoint) -> None:
    QTest.mouseRelease(timeline, Qt.MouseButton.LeftButton, pos=point)


def _playing_keyed(qtbot, make_keyed, name: str):
    store, factory, controller, window, events = make_keyed(name)
    _play_and_wait(qtbot, controller)
    qtbot.waitUntil(
        lambda: (controller.playback.visible_position or 0) >= 3 * STEP, timeout=5000
    )
    return store, factory, controller, window, events


def test_press_move_release_on_the_timeline_suspends_and_resumes_from_the_new_position(  # noqa: E501
    qtbot, make_keyed
) -> None:
    store, factory, controller, window, events = _playing_keyed(
        qtbot, make_keyed, "drag"
    )
    playback = controller.playback
    timeline = window.timeline_widget
    first_session = playback.active_session
    events.clear()

    point = _strip_point(timeline, 0.5)
    _press(timeline, point)

    assert playback.suspended
    assert not window.original_canvas.holding_live_frame
    assert timeline.held_playhead is None
    assert window.playback_controls.play_button.text() == "Pause"
    assert playback.playing
    pressed = store.state.timeline.playhead
    assert Fraction(3, 10) < pressed < Fraction(7, 10)
    assert [e for e in events if isinstance(e, PlayheadChanged)] == [
        PlayheadChanged(pressed)
    ]
    assert len(factory.calls) == 1

    moved = _strip_point(timeline, 0.6)
    _drag_to(timeline, moved)
    _release(timeline, moved)

    released = store.state.timeline.playhead
    assert released > pressed
    qtbot.waitUntil(lambda: len(factory.decoders) == 2, timeout=5000)
    qtbot.waitUntil(lambda: bool(factory.decoders[1].seeks), timeout=5000)
    assert len(factory.calls) == 2
    assert factory.decoders[1].seeks[0] == released
    assert not playback.suspended
    assert playback.playing
    assert playback.active_session not in (None, first_session)
    qtbot.waitUntil(lambda: playback.visible_position is not None, timeout=5000)


def test_the_suspended_worker_converts_no_frame_after_the_resume(
    qtbot, make_keyed
) -> None:
    gate = threading.Event()
    store, factory, controller, window, _events = make_keyed("late")
    factory.script = Script(end=Fraction(5), gate=gate, gate_at=3)
    _play_and_wait(qtbot, controller)
    playback = controller.playback
    timeline = window.timeline_widget
    qtbot.waitUntil(lambda: factory.decoders[0].yielded >= 3, timeout=5000)
    point = _strip_point(timeline, 0.5)
    _press(timeline, point)
    factory.script = Script(end=Fraction(5))
    _release(timeline, point)
    qtbot.waitUntil(lambda: playback.visible_position is not None, timeout=5000)
    resumed_at = store.state.timeline.playhead
    converted_before = len(factory.decoders[0].converted)

    gate.set()
    first_thread = playback._threads[0][0]
    qtbot.waitUntil(lambda: _finished(first_thread), timeout=5000)

    assert len(factory.decoders[0].converted) == converted_before
    assert playback.visible_position >= resumed_at
    assert window.original_canvas.holding_live_frame


def test_a_handle_press_does_not_suspend(qtbot, make_keyed) -> None:
    _store, factory, controller, window, events = _playing_keyed(
        qtbot, make_keyed, "handle"
    )
    timeline = window.timeline_widget
    handle = timeline._geometry.visual["start_handle"]
    point = QPoint(int(handle.x + handle.width / 2), int(handle.y + handle.height / 2))
    assert timeline._hit(QPointF(point)) == "start_handle"
    events.clear()

    _press(timeline, point)

    assert [e for e in events if isinstance(e, StartChanged)]
    assert not controller.playback.suspended
    assert controller.playback.playing
    _release(timeline, point)
    assert len(factory.calls) == 1
    assert controller.playback.playing


def test_a_click_is_a_press_and_release(qtbot, make_keyed) -> None:
    store, factory, controller, window, _events = _playing_keyed(
        qtbot, make_keyed, "click"
    )
    timeline = window.timeline_widget

    qtbot.mouseClick(timeline, Qt.MouseButton.LeftButton, pos=_strip_point(timeline))

    qtbot.waitUntil(lambda: len(factory.decoders) == 2, timeout=5000)
    qtbot.waitUntil(lambda: bool(factory.decoders[1].seeks), timeout=5000)
    assert factory.decoders[1].seeks[0] == store.state.timeline.playhead
    assert controller.playback.playing
    assert not controller.playback.suspended
    assert len(factory.calls) == 2


def test_pause_while_suspended_drops_the_pending_resume(qtbot, make_keyed) -> None:
    _store, factory, controller, window, events = _playing_keyed(
        qtbot, make_keyed, "pause-susp"
    )
    timeline = window.timeline_widget
    events.clear()
    point = _strip_point(timeline)
    _press(timeline, point)
    assert controller.playback.suspended

    controller.playback.pause()
    _release(timeline, point)

    assert len(factory.calls) == 1
    assert not controller.playback.playing
    assert not controller.playback.suspended
    assert window.playback_controls.play_button.text() == "Play"
    assert len([e for e in events if isinstance(e, PlayheadChanged)]) == 1


def test_stop_and_commit_while_suspended_drops_the_pending_resume(
    qtbot, make_keyed
) -> None:
    _store, factory, controller, window, events = _playing_keyed(
        qtbot, make_keyed, "stop-susp"
    )
    timeline = window.timeline_widget
    events.clear()
    point = _strip_point(timeline)
    _press(timeline, point)

    controller.playback.stop_and_commit()
    _release(timeline, point)

    assert len(factory.calls) == 1
    assert not controller.playback.playing
    assert window.playback_controls.play_button.text() == "Play"
    assert len([e for e in events if isinstance(e, PlayheadChanged)]) == 1


def test_a_job_start_while_suspended_drops_the_pending_resume(
    qtbot, make_keyed
) -> None:
    store, factory, controller, window, _events = _playing_keyed(
        qtbot, make_keyed, "job-susp"
    )
    timeline = window.timeline_widget
    point = _strip_point(timeline)
    _press(timeline, point)
    assert controller.playback.suspended

    store.dispatch(PreviewRequested("preview", "preview-request"))
    _release(timeline, point)

    assert len(factory.calls) == 1
    assert not controller.playback.playing
    assert not controller.playback.suspended


def test_a_source_change_while_suspended_drops_the_pending_resume(
    qtbot, make_keyed
) -> None:
    store, factory, controller, window, _events = _playing_keyed(
        qtbot, make_keyed, "src-susp"
    )
    timeline = window.timeline_widget
    point = _strip_point(timeline)
    _press(timeline, point)
    assert controller.playback.suspended

    store.dispatch(SourceLoadRequested("new", "load"))
    _release(timeline, point)

    assert len(factory.calls) == 1
    assert not controller.playback.playing
    assert not controller.playback.suspended


def _set_range(rig: Rig, start: Fraction, end: Fraction) -> None:
    rig.store.dispatch(EndChanged(end))
    rig.store.dispatch(StartChanged(start))
    assert (rig.store.state.timeline.start, rig.store.state.timeline.end) == (
        start,
        end,
    )


def _wait_for_worker_end(qtbot, rig: Rig) -> None:
    qtbot.waitUntil(lambda: not rig.controller._threads, timeout=5000)


def test_selection_only_starts_at_in_when_the_playhead_is_outside(
    qtbot, make_rig
) -> None:
    rig = make_rig()
    _set_range(rig, Fraction(1, 5), Fraction(1, 2))
    rig.store.dispatch(PlayheadChanged(Fraction(3, 5)))
    rig.controls.selection_only.setChecked(True)

    rig.controller.play()
    qtbot.waitUntil(lambda: bool(rig.factory.decoders[0].seeks), timeout=5000)

    assert rig.factory.decoders[0].seeks[0] == Fraction(1, 5)
    assert rig.controller._active[1].range == (Fraction(1, 5), Fraction(1, 2))
    rig.controller.pause()


def test_selection_only_off_plays_from_the_playhead_without_a_range(
    qtbot, make_rig
) -> None:
    rig = make_rig()
    _set_range(rig, Fraction(1, 5), Fraction(1, 2))
    rig.store.dispatch(PlayheadChanged(Fraction(3, 5)))

    rig.controller.play()
    qtbot.waitUntil(lambda: bool(rig.factory.decoders[0].seeks), timeout=5000)

    assert rig.factory.decoders[0].seeks[0] == Fraction(3, 5)
    assert rig.controller._active[1].range is None
    rig.controller.pause()


def test_selection_only_turned_on_while_playing_outside_jumps_to_in(
    qtbot, make_rig
) -> None:
    rig = make_rig()
    rig.factory.script = Script(end=Fraction(5))
    _set_range(rig, Fraction(1, 5), Fraction(1, 2))
    rig.store.dispatch(PlayheadChanged(Fraction(3, 5)))
    rig.play_until_frame(qtbot, 3)
    decoder = rig.factory.decoders[0]

    rig.controls.selection_only.click()

    qtbot.waitUntil(lambda: Fraction(1, 5) in decoder.seeks, timeout=5000)
    mark = decoder.seek_marks[decoder.seeks.index(Fraction(1, 5))]
    qtbot.waitUntil(lambda: len(decoder.converted) >= mark + 5, timeout=5000)
    after = decoder.converted[mark:]
    assert all(Fraction(1, 5) <= pts < Fraction(1, 2) for pts in after)
    rig.controller.pause()


def test_selection_only_turned_off_while_playing_clears_the_range(
    qtbot, make_rig
) -> None:
    rig = make_rig()
    rig.factory.script = Script(end=Fraction(5))
    _set_range(rig, Fraction(1, 5), Fraction(3, 10))
    rig.controls.selection_only.setChecked(True)
    rig.play_until_frame(qtbot, 3)
    worker = rig.controller._active[1]
    assert worker.range is not None

    rig.controls.selection_only.setChecked(False)

    assert worker.range is None
    rig.controller.pause()


def test_selection_only_toggled_while_paused_moves_nothing(qtbot, make_rig) -> None:
    rig = make_rig()
    _set_range(rig, Fraction(1, 5), Fraction(1, 2))
    rig.store.dispatch(PlayheadChanged(Fraction(3, 5)))
    rig.events.clear()

    rig.controls.selection_only.setChecked(True)
    rig.controls.selection_only.setChecked(False)

    assert rig.events == []
    assert rig.factory.calls == []
    assert rig.store.state.timeline.playhead == Fraction(3, 5)


def _loop_until_seeks(qtbot, rig: Rig, count: int) -> tuple[FakeDecoder, float]:
    started = time.perf_counter()
    rig.controller.play()
    qtbot.waitUntil(lambda: bool(rig.factory.decoders), timeout=5000)
    decoder = rig.factory.decoders[0]
    qtbot.waitUntil(lambda: len(decoder.seeks) >= count, timeout=5000)
    rig.controller.pause()
    _wait_for_worker_end(qtbot, rig)
    return decoder, time.perf_counter() - started


def test_the_loop_stays_inside_the_range_and_seeks_once_per_iteration(
    qtbot, make_rig
) -> None:
    rig = make_rig()
    rig.factory.script = Script(end=Fraction(5))
    _set_range(rig, Fraction(1, 5), Fraction(3, 10))
    rig.controls.selection_only.setChecked(True)

    decoder, _elapsed = _loop_until_seeks(qtbot, rig, 3)

    assert decoder.converted
    assert all(Fraction(1, 5) <= pts < Fraction(3, 10) for pts in decoder.converted)
    assert decoder.seeks.count(Fraction(1, 5)) == len(decoder.seeks) >= 3
    assert len(decoder.seeks) <= decoder.yielded / 10 + 2


def test_the_end_of_the_stream_loops_instead_of_ending_while_a_range_is_set(
    qtbot, make_rig
) -> None:
    rig = make_rig()
    rig.factory.script = Script(end=Fraction(1, 5))
    _set_range(rig, Fraction(1, 10), Fraction(1))
    rig.controls.selection_only.setChecked(True)

    rig.controller.play()
    qtbot.waitUntil(lambda: bool(rig.factory.decoders), timeout=5000)
    decoder = rig.factory.decoders[0]
    qtbot.waitUntil(lambda: len(decoder.seeks) >= 3, timeout=5000)

    assert rig.controller.playing
    assert _playhead_events(rig) == []
    rig.controller.pause()


def test_in_out_changed_during_a_loop_reach_the_worker(qtbot, make_rig) -> None:
    rig = make_rig()
    rig.factory.script = Script(end=Fraction(5))
    _set_range(rig, Fraction(1, 5), Fraction(1, 2))
    rig.controls.selection_only.setChecked(True)
    rig.play_until_frame(qtbot, 3)
    decoder = rig.factory.decoders[0]

    rig.store.dispatch(StartChanged(Fraction(1, 4)))

    qtbot.waitUntil(lambda: Fraction(1, 4) in decoder.seeks, timeout=5000)
    mark = decoder.seek_marks[decoder.seeks.index(Fraction(1, 4))]
    qtbot.waitUntil(lambda: len(decoder.converted) >= mark + 5, timeout=5000)
    assert all(pts >= Fraction(1, 4) for pts in decoder.converted[mark:])
    rig.controller.pause()


def _hold_single_frame(qtbot, rig: Rig) -> FakeDecoder:
    """Loop a one-frame range until the worker holds it; return its decoder."""
    rig.factory.script = Script(end=Fraction(5))
    _set_range(rig, Fraction(1, 5), Fraction(1, 5) + STEP)
    rig.controls.selection_only.setChecked(True)
    rig.controller.play()
    qtbot.waitUntil(lambda: bool(rig.factory.decoders), timeout=5000)
    decoder = rig.factory.decoders[0]
    qtbot.waitUntil(lambda: len(decoder.seeks) >= 2, timeout=5000)
    qtbot.wait(150)
    return decoder


def test_a_single_frame_range_is_a_still_without_a_seek_storm(
    qtbot, make_rig
) -> None:
    rig = make_rig(fps=100)
    decoder = _hold_single_frame(qtbot, rig)
    seeks = len(decoder.seeks)
    converted = len(decoder.converted)

    qtbot.wait(300)

    assert len(decoder.seeks) == seeks <= 3
    assert len(decoder.converted) == converted
    assert set(decoder.converted) == {Fraction(1, 5)}
    rig.controller.pause()


def test_a_range_change_ends_the_hold_and_plays_on_from_the_frame_in_hand(
    qtbot, make_rig
) -> None:
    rig = make_rig(fps=100)
    decoder = _hold_single_frame(qtbot, rig)
    seeks = len(decoder.seeks)

    rig.store.dispatch(EndChanged(Fraction(1, 5) + 2 * STEP))

    qtbot.waitUntil(lambda: Fraction(21, 100) in decoder.converted, timeout=5000)
    # The frame in hand was converted before any seek that followed the change.
    shown_at = decoder.converted.index(Fraction(21, 100))
    assert all(mark > shown_at for mark in decoder.seek_marks[seeks:])
    assert max(decoder.converted) < Fraction(22, 100)
    rig.controller.pause()


def test_selection_only_turned_off_during_the_hold_plays_on_without_a_seek(
    qtbot, make_rig
) -> None:
    rig = make_rig(fps=100)
    decoder = _hold_single_frame(qtbot, rig)
    seeks = len(decoder.seeks)

    rig.controls.selection_only.setChecked(False)

    qtbot.waitUntil(lambda: max(decoder.converted) >= Fraction(3, 10), timeout=5000)
    assert len(decoder.seeks) == seeks
    rig.controller.pause()


def test_pause_ends_the_hold(qtbot, make_rig) -> None:
    rig = make_rig(fps=100)
    _hold_single_frame(qtbot, rig)

    rig.controller.pause()

    _wait_for_worker_end(qtbot, rig)


def test_the_frame_owning_in_is_shown_on_every_iteration_despite_a_pre_roll(
    qtbot, make_rig
) -> None:
    rig = make_rig()
    rig.factory.script = Script(end=Fraction(5), preroll=0.05)
    _set_range(rig, Fraction(1, 5), Fraction(3, 10))
    rig.controls.selection_only.setChecked(True)

    decoder, _elapsed = _loop_until_seeks(qtbot, rig, 4)

    marks = [m for m in decoder.seek_marks if m < len(decoder.converted)]
    assert len(marks) >= 3
    assert all(decoder.converted[m] == Fraction(1, 5) for m in marks)


def test_the_frame_owning_a_new_in_is_shown_despite_a_pre_roll(
    qtbot, make_rig
) -> None:
    rig = make_rig()
    rig.factory.script = Script(end=Fraction(5), preroll=0.05)
    _set_range(rig, Fraction(1, 5), Fraction(1, 2))
    rig.controls.selection_only.setChecked(True)
    rig.play_until_frame(qtbot, 3)
    decoder = rig.factory.decoders[0]

    rig.store.dispatch(StartChanged(Fraction(1, 4)))

    qtbot.waitUntil(lambda: Fraction(1, 4) in decoder.seeks, timeout=5000)
    mark = decoder.seek_marks[decoder.seeks.index(Fraction(1, 4))]
    qtbot.waitUntil(lambda: len(decoder.converted) > mark, timeout=5000)
    assert decoder.converted[mark] == Fraction(1, 4)
    rig.controller.pause()


def test_an_in_between_two_frames_still_shows_the_frame_that_owns_it(
    qtbot, make_rig
) -> None:
    rig = make_rig()
    rig.factory.script = Script(end=Fraction(5))
    _set_range(rig, Fraction(205, 1000), Fraction(3, 10))
    rig.controls.selection_only.setChecked(True)

    decoder, elapsed = _loop_until_seeks(qtbot, rig, 4)

    assert min(decoder.converted) == Fraction(1, 5)
    assert max(decoder.converted) < Fraction(3, 10)
    assert len(decoder.converted) >= 3
    assert len(decoder.seeks) <= elapsed / float(STEP) + 2


def test_releasing_a_drag_at_the_last_frame_resumes_there_not_from_zero(
    qtbot, make_keyed
) -> None:
    store, factory, controller, window, _events = _playing_keyed(
        qtbot, make_keyed, "last-frame"
    )
    timeline = window.timeline_widget
    rect = timeline._geometry.visual["timeline"]
    _press(timeline, _strip_point(timeline, 0.5))
    beyond = QPoint(int(rect.x + rect.width) + 40, int(rect.y + rect.height / 2))
    _drag_to(timeline, beyond)
    last = store.state.timeline.duration - store.state.timeline.source_frame_interval
    assert store.state.timeline.playhead == last

    _release(timeline, beyond)

    qtbot.waitUntil(lambda: len(factory.decoders) == 2, timeout=5000)
    qtbot.waitUntil(lambda: bool(factory.decoders[1].seeks), timeout=5000)
    assert factory.decoders[1].seeks[0] == last
    qtbot.waitUntil(lambda: not controller.playback.playing, timeout=5000)
    assert store.state.timeline.playhead == last
    assert window.playback_controls.play_button.text() == "Play"


def _mouse(timeline, kind: QEvent.Type, point: QPoint, held: bool) -> None:
    button = Qt.MouseButton.LeftButton
    event = QMouseEvent(
        kind,
        QPointF(point),
        QPointF(timeline.mapToGlobal(point)),
        button if kind != QEvent.Type.MouseMove else Qt.MouseButton.NoButton,
        button if held else Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(timeline, event)


def test_a_double_click_still_suspends_for_the_drag_after_it(
    qtbot, make_keyed
) -> None:
    _store, factory, controller, window, _events = _playing_keyed(
        qtbot, make_keyed, "dblclick"
    )
    timeline = window.timeline_widget
    point = _strip_point(timeline, 0.5)
    # Qt's real sequence for a double click.
    _mouse(timeline, QEvent.Type.MouseButtonPress, point, True)
    _mouse(timeline, QEvent.Type.MouseButtonRelease, point, False)
    qtbot.waitUntil(lambda: len(factory.decoders) == 2, timeout=5000)
    _mouse(timeline, QEvent.Type.MouseButtonDblClick, point, True)
    assert controller.playback.suspended

    moved = _strip_point(timeline, 0.6)
    _drag_to(timeline, moved)
    _mouse(timeline, QEvent.Type.MouseButtonRelease, moved, False)

    qtbot.waitUntil(lambda: len(factory.decoders) == 3, timeout=5000)
    assert not controller.playback.suspended
    assert controller.playback.playing


def test_a_drag_right_after_pause_releases_the_holds_and_the_marker_follows(
    qtbot, make_keyed
) -> None:
    store, _factory, controller, window, _events = _playing_keyed(
        qtbot, make_keyed, "drag-after-pause"
    )
    timeline = window.timeline_widget

    controller.playback.pause()

    # The pause commit is the controller's own dispatch; it must not release.
    assert window.original_canvas.holding_live_frame
    assert timeline.held_playhead is not None
    point = _strip_point(timeline, 0.5)
    _press(timeline, point)
    moved = _strip_point(timeline, 0.6)
    _drag_to(timeline, moved)

    assert timeline.held_playhead is None
    assert not window.original_canvas.holding_live_frame
    assert window.playback_controls.position is None
    shown = timeline._presentation.state.playhead
    assert shown == store.state.timeline.playhead
    assert Fraction(5, 10) < shown < Fraction(7, 10)
    _release(timeline, moved)
