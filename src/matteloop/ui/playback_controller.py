"""Source video playback: the paced worker and the GUI-thread controller.

The worker decodes, paces and converts frames on its own thread and publishes
them through a one-slot mailbox. The controller shows the newest frame as a live
hold on the canvas, timeline and timecode row without touching the reducer, and
commits the visible position through an ordinary ``PlayheadChanged`` only when
playback stops. Every play gets a session number; results of any other session
are dropped on the GUI thread.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterator
from fractions import Fraction
from pathlib import Path
from typing import Protocol

import av
from PySide6.QtCore import (
    QCoreApplication,
    QEvent,
    QObject,
    Qt,
    QThread,
    Signal,
    Slot,
)
from PySide6.QtGui import QImage, QMouseEvent

from matteloop.core.errors import AppError, ErrorCode
from matteloop.core.state import AppState, JobState, SourceState
from matteloop.core.timeline import TimelineState
from matteloop.jobs.playback import PlaybackDecoder, PlaybackFrame
from matteloop.jobs.source import SourceRevision, SourceValidationProof
from matteloop.ui.playback_controls import PlaybackControls, PlaybackTimeline
from matteloop.ui.ports import StateStore
from matteloop.ui.preview_canvas import PreviewCanvas
from matteloop.ui.worker_thread import WorkerThread, wait_for_shutdown_worker

_LOGGER = logging.getLogger(__name__)
_SLEEP_SLICE_SECONDS = 0.010
_CONVERT_EVERY_LATE_FRAMES = 5
_DECODE_ERRORS = (AppError, av.FFmpegError, OSError, ValueError)


class PlaybackSource(Protocol):
    """The decoder surface the worker uses; ``PlaybackDecoder`` implements it."""

    def open(self) -> None: ...

    def seek(self, timestamp: Fraction) -> None: ...

    def frames(self) -> Iterator[tuple[Fraction, object]]: ...

    def convert(self, frame: object) -> PlaybackFrame: ...

    def close(self) -> None: ...


type PlaybackDecoderFactory = Callable[
    [Path, SourceValidationProof, SourceRevision, tuple[int, int]], PlaybackSource
]


class _FrameMailbox:
    """One slot shared by the worker and the GUI thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._item: tuple[int, QImage, Fraction] | None = None

    def put(self, session: int, image: QImage, pts: Fraction) -> bool:
        """Store a frame; return True when the slot was empty."""
        with self._lock:
            was_empty = self._item is None
            self._item = (session, image, pts)
            return was_empty

    def take(self) -> tuple[int, QImage, Fraction] | None:
        with self._lock:
            item, self._item = self._item, None
            return item


def _failure_token(error: BaseException) -> str:
    if isinstance(error, AppError) and error.code is ErrorCode.SOURCE_CHANGED:
        return "source-changed"
    return "decode"


class _PlaybackWorker(QObject):
    """Decode, pace and publish frames of one play session."""

    frame_available = Signal(int)
    ended = Signal(int, object)
    failed = Signal(int, str)
    finished = Signal()

    def __init__(
        self,
        session: int,
        decoder: PlaybackSource,
        start: Fraction,
        frame_interval: Fraction,
        range_: tuple[Fraction, Fraction] | None,
        mailbox: _FrameMailbox,
    ) -> None:
        super().__init__()
        self._session = session
        self._decoder = decoder
        self._start = start
        self._frame_interval = frame_interval
        self._mailbox = mailbox
        self.range = range_
        self.shown_frames = 0
        self.dropped_frames = 0
        self._anchor_wall = 0.0
        self._anchor_pts: Fraction | None = None
        self._late_run = 0
        self._last_loop_seek: float | None = None

    @Slot()
    def run(self) -> None:
        outcome: tuple[str, object] | None = None
        try:
            try:
                self._decoder.open()
                self._decoder.seek(self._start)
                outcome = self._decode_loop()
            except _DECODE_ERRORS as error:
                outcome = ("failed", _failure_token(error))
            finally:
                outcome = self._close_decoder(outcome)
            if outcome is not None and not self._interrupted():
                kind, value = outcome
                if kind == "ended":
                    self.ended.emit(self._session, value)
                else:
                    self.failed.emit(self._session, str(value))
        finally:
            self.finished.emit()

    def _close_decoder(
        self, outcome: tuple[str, object] | None
    ) -> tuple[str, object] | None:
        """Close on this thread, before any terminal signal, and fold in errors."""
        try:
            self._decoder.close()
        except _DECODE_ERRORS as error:
            token = _failure_token(error)
            if token == "source-changed" and outcome is not None:
                return ("failed", token)
            if outcome is not None and outcome[0] == "ended":
                return ("failed", token)
        return outcome

    @staticmethod
    def _interrupted() -> bool:
        return QThread.currentThread().isInterruptionRequested()

    def _decode_loop(self) -> tuple[str, object] | None:
        last_pts: Fraction | None = None
        interval = float(self._frame_interval)
        frames = self._ranged_frames()
        while True:
            if self._interrupted():
                return None
            try:
                pts, frame = next(frames)
            except StopIteration:
                return ("ended", last_pts)
            if self._interrupted():
                return None
            last_pts = pts
            if self._anchor_pts is None:
                self._anchor_wall, self._anchor_pts = time.perf_counter(), pts
            due = self._anchor_wall + float(pts - self._anchor_pts)
            if time.perf_counter() > due + interval:
                self._late_run += 1
                if self._late_run < _CONVERT_EVERY_LATE_FRAMES:
                    self.dropped_frames += 1
                    continue
                self._late_run = 0
            else:
                self._late_run = 0
                if not self._sleep_until(due):
                    return None
            if self._interrupted():
                return None
            self._publish(pts, frame)

    def _publish(self, pts: Fraction, frame: object) -> None:
        converted = self._decoder.convert(frame)
        image = QImage(
            converted.rgba,
            converted.width,
            converted.height,
            converted.width * 4,
            QImage.Format.Format_RGBA8888,
        ).copy()
        if self._interrupted():
            return
        if self._mailbox.put(self._session, image, pts):
            self.shown_frames += 1
            self.frame_available.emit(self._session)
        else:
            self.dropped_frames += 1

    def _ranged_frames(self) -> Iterator[tuple[Fraction, object]]:
        """Yield decoded frames, looping back to IN at a range boundary.

        The range is read once per decoded frame. The frame right after a seek
        is the one owning the target, so it is never rejected for starting
        before IN: a non-aligned IN would otherwise seek again forever. When a
        loop seek produced at most one frame and the range is still the one it
        sought, there is nothing to replay: hold until the range changes.
        """
        iterator = iter(self._decoder.frames())
        fresh = True
        yielded = 0
        sought: tuple[Fraction, Fraction] | None = None
        while True:
            item = next(iterator, None)
            if self._interrupted():
                return
            window = self.range
            at_boundary = item is None or self._outside(item[0], window, fresh)
            if at_boundary and window is not None and yielded <= 1 and sought == window:
                if not self._hold_while(window):
                    return
                window = self.range
            if item is not None and not self._outside(item[0], window, fresh):
                fresh = False
                yielded += 1
                yield item
                continue
            if window is None:
                return
            if not self._loop_back(window[0]):
                return
            iterator = iter(self._decoder.frames())
            fresh, yielded, sought = True, 0, window

    def _hold_while(self, window: tuple[Fraction, Fraction]) -> bool:
        """Wait for the range to change; False when interrupted."""
        while self.range == window:
            if self._interrupted():
                return False
            time.sleep(_SLEEP_SLICE_SECONDS)
        # The frame in hand plays on, paced from its own arrival.
        self._anchor_pts = None
        self._late_run = 0
        return not self._interrupted()

    @staticmethod
    def _outside(
        pts: Fraction, window: tuple[Fraction, Fraction] | None, fresh: bool
    ) -> bool:
        if window is None:
            return False
        return pts >= window[1] or (pts < window[0] and not fresh)

    def _loop_back(self, start: Fraction) -> bool:
        """Seek to IN, at most once per frame interval; False when interrupted."""
        if self._last_loop_seek is not None:
            earliest = self._last_loop_seek + float(self._frame_interval)
            if not self._sleep_until(earliest):
                return False
        self._last_loop_seek = time.perf_counter()
        self._decoder.seek(start)
        # Anchor on the arrival of the first frame: the pre-roll decode must not
        # make the frames that matter late.
        self._anchor_pts = None
        self._late_run = 0
        return not self._interrupted()

    def _sleep_until(self, due: float) -> bool:
        """Sleep in short slices; return False when interrupted."""
        while True:
            remaining = due - time.perf_counter()
            if remaining <= 0:
                return True
            if self._interrupted():
                return False
            time.sleep(min(remaining, _SLEEP_SLICE_SECONDS))


class SourcePlaybackController(QObject):
    """Own playback on the GUI thread: live holds, commit on stop, shutdown."""

    def __init__(
        self,
        store: StateStore,
        *,
        commit: Callable[[Fraction], None],
        decoder_factory: PlaybackDecoderFactory | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._store = store
        self._commit = commit
        self._decoder_factory: PlaybackDecoderFactory = (
            decoder_factory or PlaybackDecoder
        )
        self._controls: PlaybackControls | None = None
        self._canvas: PreviewCanvas | None = None
        self._timeline: PlaybackTimeline | None = None
        self._playing = False
        self._visible: Fraction | None = None
        self._session = 0
        self._active_session: int | None = None
        self._mailbox: _FrameMailbox | None = None
        self._active: tuple[QThread, _PlaybackWorker] | None = None
        self._threads: list[tuple[QThread, _PlaybackWorker]] = []
        self._play_source_id: str | None = None
        self._release_pending = False
        self._remembered_frame: object | None = None
        self._press_pending = False
        self._suspended = False
        self._resume_pending = False
        self._committing = False
        self._unsubscribe: Callable[[], None] | None = store.subscribe(
            self._state_changed
        )

    @property
    def playing(self) -> bool:
        return self._playing

    @property
    def visible_position(self) -> Fraction | None:
        return self._visible

    @property
    def active_session(self) -> int | None:
        return self._active_session

    @property
    def suspended(self) -> bool:
        return self._suspended

    def attach(
        self,
        controls: PlaybackControls,
        canvas: PreviewCanvas,
        timeline: PlaybackTimeline,
    ) -> None:
        self._controls = controls
        self._canvas = canvas
        self._timeline = timeline
        timeline.installEventFilter(self)
        controls.selection_only_toggled.connect(self._selection_only_toggled)

    def toggle(self) -> None:
        if self._playing:
            self.pause()
        else:
            self.play()
        # A click already flipped the button; a no-op play must not leave it checked.
        if self._controls is not None:
            self._controls.set_playing(self._playing)

    def stop_and_commit(self) -> None:
        if self._playing:
            self.pause()

    def play(self, *, resume: bool = False) -> None:
        request = self._play_request()
        if request is None or self._canvas is None or self._controls is None:
            return
        path, proof, revision, source_id, timeline = request
        interval = timeline.source_frame_interval
        start, range_ = self._start_and_range(timeline, resume)
        decoder = self._decoder_factory(
            path, proof, revision, self._canvas.display_size()
        )
        self._session += 1
        session = self._session
        mailbox = _FrameMailbox()
        worker = _PlaybackWorker(session, decoder, start, interval, range_, mailbox)
        thread = WorkerThread(worker, self)
        worker.frame_available.connect(self._frame_available)
        worker.ended.connect(self._ended)
        worker.failed.connect(self._failed)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(lambda thread=thread: self._forget_thread(thread))
        self._threads.append((thread, worker))
        self._active = (thread, worker)
        self._mailbox = mailbox
        self._active_session = session
        self._play_source_id = source_id
        self._playing = True
        self._suspended = False
        self._resume_pending = False
        self._release_pending = False
        self._visible = None
        self._controls.set_playing(True)
        self._canvas.set_status_marker(None)
        thread.start()

    def _start_and_range(
        self, timeline: TimelineState, resume: bool
    ) -> tuple[Fraction, tuple[Fraction, Fraction] | None]:
        """Pick where to start and, with `Selection only`, the loop window."""
        playhead = timeline.playhead
        interval = timeline.source_frame_interval
        # Only a press on Play restarts at the end; a drag release resumes there.
        at_end = playhead >= timeline.duration - interval
        start = Fraction(0) if at_end and not resume else playhead
        if self._controls is None or not self._controls.selection_only_enabled:
            return start, None
        window = (timeline.start, timeline.end)
        return (start if window[0] <= start < window[1] else window[0]), window

    def _play_request(
        self,
    ) -> tuple[Path, SourceValidationProof, SourceRevision, str, TimelineState] | None:
        state = self._store.state
        metadata = state.source_value
        timeline = state.timeline
        path = getattr(metadata, "path", None)
        revision = getattr(metadata, "revision", None)
        proof = getattr(metadata, "validation_proof", None)
        if (
            (self._playing and not self._suspended)
            or state.source is not SourceState.READY
            or state.source_id is None
            or timeline is None
            or not isinstance(path, Path)
            or not isinstance(revision, SourceRevision)
            or not isinstance(proof, SourceValidationProof)
        ):
            return None
        return path, proof, revision, state.source_id, timeline

    def pause(self) -> None:
        if not self._playing:
            return
        self._stop_worker()
        self._finish_playback()

    def _stop_worker(self) -> None:
        self._interrupt_worker()
        self._playing = False
        self._suspended = False
        self._resume_pending = False
        if self._controls is not None:
            self._controls.set_playing(False)

    def _interrupt_worker(self) -> None:
        active, self._active = self._active, None
        self._active_session = None
        self._mailbox = None
        if active is not None:
            active[0].requestInterruption()
            _LOGGER.info(
                "source playback stopped: %d frames shown, %d dropped",
                active[1].shown_frames,
                active[1].dropped_frames,
            )

    def playhead_changed_by_user(self) -> None:
        """Suspend playback while the user drags the marker; resume on release.

        A drag right after Pause, while the commit still waits for its still,
        releases the holds at once: each move restarts the still's debounce, so
        the marker would otherwise stay frozen for the whole drag.
        """
        if self._release_pending and not self._committing:
            self._release_holds()
            return
        if not (self._playing and self._press_pending and not self._suspended):
            return
        self._interrupt_worker()
        self._visible = None
        self._suspended = True
        self._resume_pending = True
        if self._canvas is not None:
            self._canvas.release_live_frame()
        if self._timeline is not None:
            self._timeline.hold_playhead(None)
        if self._controls is not None:
            self._controls.set_position(None)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        """Note the left button on the timeline; never consume an event."""
        left = (
            isinstance(event, QMouseEvent)
            and event.button() == Qt.MouseButton.LeftButton
        )
        if left and event.type() in (
            QEvent.Type.MouseButtonPress,
            QEvent.Type.MouseButtonDblClick,
        ):
            self._press_pending = True
        elif left and event.type() == QEvent.Type.MouseButtonRelease:
            self._press_pending = False
            if self._resume_pending:
                self._resume_pending = False
                self.play(resume=True)
        return False

    @Slot(bool)
    def _selection_only_toggled(self, enabled: bool) -> None:
        timeline = self._store.state.timeline
        if self._active is None or self._suspended or timeline is None:
            return
        self._active[1].range = (
            (timeline.start, timeline.end) if enabled else None
        )

    def _sync_range(self, state: AppState) -> None:
        """Hand a changed IN/OUT to the running loop."""
        timeline = state.timeline
        if (
            self._active is None
            or self._suspended
            or timeline is None
            or self._controls is None
            or not self._controls.selection_only_enabled
        ):
            return
        window = (timeline.start, timeline.end)
        if self._active[1].range != window:
            self._active[1].range = window

    def _finish_playback(self) -> None:
        """Commit the visible frame and wait for its still, or release at once."""
        position = self._visible
        if position is None:
            self._release_holds()
            return
        before = self._store.state
        self._remembered_frame = before.source_frame
        self._release_pending = True
        self._committing = True
        try:
            self._commit(position)
        finally:
            self._committing = False
        if self._store.state is before:
            self._release_pending = False
            self._release_holds()

    def _release_holds(self) -> None:
        self._release_pending = False
        self._visible = None
        if self._canvas is not None:
            frame = self._store.state.source_frame
            self._canvas.release_live_frame()
            self._canvas.set_frame(frame if isinstance(frame, QImage) else None)
        if self._timeline is not None:
            self._timeline.hold_playhead(None)
        if self._controls is not None:
            self._controls.set_position(None)

    def _latest_position(self) -> Fraction | None:
        timeline = self._store.state.timeline
        if timeline is None:
            return None
        return max(Fraction(0), timeline.duration - timeline.source_frame_interval)

    def _normalised(self, pts: Fraction) -> Fraction:
        latest = self._latest_position()
        return pts if latest is None else min(pts, latest)

    @Slot(int)
    def _frame_available(self, session: int) -> None:
        mailbox = self._mailbox
        if session != self._active_session or mailbox is None:
            return
        item = mailbox.take()
        if item is None or item[0] != session or item[1].isNull():
            return
        _session, image, pts = item
        if self._canvas is None or self._timeline is None or self._controls is None:
            return
        position = self._normalised(pts)
        self._canvas.hold_live_frame(image)
        self._timeline.hold_playhead(position)
        self._controls.set_position(position)
        self._visible = position

    @Slot(int, object)
    def _ended(self, session: int, last_pts: object) -> None:
        if session != self._active_session:
            return
        if isinstance(last_pts, Fraction):
            self._visible = self._normalised(last_pts)
        self.pause()

    @Slot(int, str)
    def _failed(self, session: int, token: str) -> None:
        if session != self._active_session:
            return
        self.pause()
        if self._canvas is None:
            return
        if token == "source-changed":
            marker = QCoreApplication.translate(
                "SourcePlaybackController",
                "Playback stopped — the video file changed on disk",
            )
        else:
            marker = QCoreApplication.translate(
                "SourcePlaybackController",
                "Playback stopped — the video could not be decoded further",
            )
        self._canvas.set_status_marker(marker)

    def _state_changed(self, state: AppState) -> None:
        if self._release_pending and state.source_frame is not self._remembered_frame:
            self._release_holds()
        # Replacing the video is the recovery from a failure marker, so a new
        # source id clears it even after the stop has long settled.
        replaced = (
            self._play_source_id is not None
            and state.source_id != self._play_source_id
        )
        active = self._playing or self._release_pending
        if active and (
            replaced
            or state.source is not SourceState.READY
            or state.job.phase is not JobState.IDLE
        ):
            self._stop_without_commit(clear_marker=replaced)
        elif replaced and self._canvas is not None:
            self._canvas.set_status_marker(None)
        if replaced:
            self._play_source_id = None
        self._sync_range(state)

    def _stop_without_commit(self, *, clear_marker: bool) -> None:
        if self._playing:
            self._stop_worker()
        self._release_holds()
        if clear_marker and self._canvas is not None:
            self._canvas.set_status_marker(None)

    def shutdown(self) -> bool:
        """Interrupt and join every playback thread within the shared budget."""
        if self._unsubscribe is not None:
            unsubscribe, self._unsubscribe = self._unsubscribe, None
            unsubscribe()
        self._playing = False
        self._suspended = False
        self._resume_pending = False
        self._active_session = None
        self._active = None
        self._mailbox = None
        threads = tuple(self._threads)
        for thread, _worker in threads:
            thread.requestInterruption()
            thread.quit()
        complete = True
        for thread, worker in threads:
            if not wait_for_shutdown_worker(
                thread, worker, "source playback worker"
            ):
                complete = False
        return complete

    def _forget_thread(self, thread: QThread) -> None:
        self._threads = [
            (current, worker)
            for current, worker in self._threads
            if current is not thread
        ]
