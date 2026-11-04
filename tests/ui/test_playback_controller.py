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
from PySide6.QtCore import QObject, QSettings, Slot
from PySide6.QtGui import QImage
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
    PlayheadChanged,
    SourceFrameDecoded,
    StartChanged,
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


class FakeDecoder:
    def __init__(self, script: Script, log: list[str]) -> None:
        self.script = script
        self.log = log
        self.seeks: list[Fraction] = []
        self.converted: list[Fraction] = []
        self.yielded = 0
        self._position = Fraction(0)

    def open(self) -> None:
        self.log.append("open")
        if self.script.open_error is not None:
            raise self.script.open_error

    def seek(self, timestamp: Fraction) -> None:
        self.log.append(f"seek {timestamp}")
        self.seeks.append(timestamp)
        self._position = timestamp

    def frames(self) -> Iterator[tuple[Fraction, object]]:
        script = self.script
        if script.block is not None:
            script.block.wait(5)
        pts = self._position
        index = 0
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


def _state(tmp_path: Path, *, bare: bool = False, with_preview: bool = False):
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

    def build(name: str = "playback", **state_options: bool) -> Rig:
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
