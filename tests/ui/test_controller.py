from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from threading import Event, Thread, get_ident

import pytest
from PIL import Image
from PySide6.QtCore import QSettings
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

from matteloop.core.errors import AppError, ErrorCode
from matteloop.core.parameters import TransformChanged
from matteloop.core.specs import TransformSpec
from matteloop.core.state import (
    AppState,
    JobKind,
    SourceLoaded,
    SourceLoadRequested,
    SourceState,
    reduce,
)
from matteloop.jobs.render import FilesystemWorkspacePort
from matteloop.jobs.source import decode_frame, probe_source
from matteloop.jobs.transform_store import store_transform
from matteloop.ui.controller import SourceController, SourceLoadResult
from matteloop.ui.main_window import MainWindow
from matteloop.ui.ports import ChooseVideoRequested, VideoDropped
from matteloop.ui.source_presentation import format_source_file_size
from matteloop.ui.store import ReducerStore
from tests.fixtures.media_factory import make_video
from tests.jobs.render_support import job, render_service, request


@dataclass
class FakeSourceAdapter:
    results: dict[Path, SourceLoadResult]
    errors: dict[Path, Exception] | None = None

    def __post_init__(self) -> None:
        self.thread_ids: list[int] = []

    def load(self, path: Path, request_id: int) -> SourceLoadResult:
        del request_id
        self.thread_ids.append(get_ident())
        if self.errors and path in self.errors:
            raise self.errors[path]
        return self.results[path]


@dataclass(frozen=True)
class _LoadedMetadata:
    path: Path
    width: int = 128
    height: int = 128
    duration: Fraction = Fraction(2)
    average_rate: Fraction = Fraction(30)


def _ready_store(path: Path) -> ReducerStore:
    loading = reduce(AppState(), SourceLoadRequested("old-source", "old-load"))
    return ReducerStore(
        reduce(
            loading,
            SourceLoaded("old-source", "old-load", _LoadedMetadata(path)),
        )
    )


def _controller_with_unsaved_cut(
    tmp_path: Path, old_path: Path, adapter: FakeSourceAdapter
) -> tuple[SourceController, ReducerStore]:
    artifact = render_service(workspace=FilesystemWorkspacePort()).render(
        request(tmp_path), job(tmp_path, "unsaved-source", JobKind.RENDER)
    )
    store = _ready_store(old_path)
    controller = SourceController(store, source_adapter=adapter)
    stored = TransformSpec(first_frame=2)
    store_transform(artifact.cut_workspace, stored, [])
    controller.transform_stage.open_artifact(artifact)
    store.dispatch(TransformChanged(TransformSpec(first_frame=1)))
    return controller, store


def _settings(name: str) -> QSettings:
    settings = QSettings(
        QSettings.IniFormat, QSettings.UserScope, "matteloop-test", name
    )
    settings.clear()
    return settings


def _fixture_result(path: Path) -> SourceLoadResult:
    metadata = probe_source(path)
    frame = decode_frame(
        path,
        Fraction(0),
        request_id=1,
        expected_revision=metadata.revision,
        validation_proof=metadata.validation_proof,
    )
    return SourceLoadResult(metadata, frame.image)


def test_dropped_video_loads_metadata_and_displays_first_frame(tmp_path, qtbot) -> None:
    path = make_video(
        tmp_path / "source.mp4",
        [Image.new("RGB", (16, 8), "red"), Image.new("RGB", (16, 8), "green")],
        Fraction(2),
    )
    adapter = FakeSourceAdapter({path: _fixture_result(path)})
    store = ReducerStore()
    controller = SourceController(store, source_adapter=adapter)
    window = MainWindow(store, controller, _settings("controller-load"))
    qtbot.addWidget(window)
    window.show()

    controller.dispatch(VideoDropped(path))

    qtbot.waitUntil(lambda: store.state.source is SourceState.READY, timeout=5000)
    qtbot.waitUntil(lambda: controller.active_load_count == 0, timeout=5000)
    metadata = store.state.source_value
    assert metadata is not None
    assert window.source_dimensions.text() == "16 × 8"
    assert window.source_duration.text() == "0:01.000"
    assert window.source_frame_rate.text() == "2 fps"
    assert window.source_file_size.text() == format_source_file_size(metadata.file_size)
    pixmap = window.original_canvas.pixmap()
    assert pixmap is not None and not pixmap.isNull()
    assert adapter.thread_ids and adapter.thread_ids[0] != get_ident()
    assert window.original_canvas.accessibleName() == "Original video frame"


def test_shutdown_waits_for_an_inflight_load_before_returning(qtbot) -> None:
    path = Path("/tmp/shutdown.mp4")

    class GatedAdapter(FakeSourceAdapter):
        def __init__(self) -> None:
            super().__init__(
                {path: SourceLoadResult(object(), Image.new("RGBA", (2, 2), "red"))}
            )
            self.started = Event()
            self.release = Event()
            self.finished = Event()

        def load(self, path: Path, request_id: int) -> SourceLoadResult:
            self.started.set()
            assert self.release.wait(5)
            try:
                return super().load(path, request_id)
            finally:
                self.finished.set()

    adapter = GatedAdapter()
    store = ReducerStore()
    controller = SourceController(
        store, source_adapter=adapter, parent=QApplication.instance()
    )
    controller.dispatch(VideoDropped(path))
    qtbot.waitUntil(adapter.started.is_set, timeout=5000)

    def release_load() -> None:
        Event().wait(0.1)
        adapter.release.set()

    releaser = Thread(target=release_load)
    releaser.start()
    controller.shutdown()
    releaser.join(timeout=1000)

    assert adapter.finished.is_set()
    assert controller.active_load_count == 0


def test_source_load_failure_is_an_app_error_with_recovery_focus(qtbot) -> None:
    path = Path("/tmp/broken.mp4")
    error = AppError(
        ErrorCode.SOURCE_CORRUPT,
        "source.probe",
        "source.probe.corrupt",
        "fixture cannot be decoded",
        "choose-another-file",
    )
    adapter = FakeSourceAdapter({}, {path: error})
    store = ReducerStore()
    controller = SourceController(store, source_adapter=adapter)
    window = MainWindow(store, controller, _settings("controller-error"))
    qtbot.addWidget(window)
    window.show()

    controller.dispatch(VideoDropped(path))

    qtbot.waitUntil(lambda: store.state.source is SourceState.ERROR, timeout=5000)
    assert isinstance(store.state.source_error, AppError)
    assert store.state.source_error is error
    assert window.source_error_heading.isVisible()
    assert window.requested_focus_name() == "source_error_heading"


def test_newer_source_result_wins_when_an_older_worker_finishes_late(qtbot) -> None:
    old_path = Path("/tmp/old.mp4")
    new_path = Path("/tmp/new.mp4")

    @dataclass(frozen=True)
    class Metadata:
        path: Path
        width: int = 2
        height: int = 2
        duration: Fraction = Fraction(1)
        average_rate: Fraction = Fraction(1)
        revision: object = object()

    old_result = SourceLoadResult(
        Metadata(old_path), Image.new("RGBA", (2, 2), "red")
    )
    new_result = SourceLoadResult(
        Metadata(new_path), Image.new("RGBA", (2, 2), "blue")
    )

    class GatedAdapter(FakeSourceAdapter):
        def __init__(self) -> None:
            super().__init__({old_path: old_result, new_path: new_result})
            self.old_started = Event()
            self.release_old = Event()

        def load(self, path: Path, request_id: int) -> SourceLoadResult:
            if path == old_path:
                self.old_started.set()
                assert self.release_old.wait(5)
            return super().load(path, request_id)

    adapter = GatedAdapter()
    store = ReducerStore()
    controller = SourceController(store, source_adapter=adapter)
    controller.dispatch(VideoDropped(old_path))
    qtbot.waitUntil(adapter.old_started.is_set, timeout=5000)

    controller.dispatch(VideoDropped(new_path))
    qtbot.waitUntil(
        lambda: store.state.source is SourceState.READY
        and getattr(store.state.source_value, "path", None) == new_path,
        timeout=5000,
    )
    adapter.release_old.set()
    qtbot.waitUntil(lambda: controller.active_load_count == 0, timeout=5000)

    assert getattr(store.state.source_value, "path", None) == new_path


@pytest.mark.parametrize("replace", [False, True])
def test_open_video_uses_one_caption_for_empty_and_loaded_source(
    monkeypatch: pytest.MonkeyPatch, qtbot, replace: bool
) -> None:
    path = Path("/tmp/chosen.mov")
    adapter = FakeSourceAdapter(
        {path: SourceLoadResult(object(), Image.new("RGBA", (2, 2), "red"))}
    )
    captured: dict[str, str] = {}

    def choose_file(parent, caption, directory, file_filter):
        del parent, directory
        captured["caption"] = caption
        captured["filter"] = file_filter
        return str(path), file_filter

    monkeypatch.setattr(QFileDialog, "getOpenFileName", choose_file)
    store = ReducerStore()
    controller = SourceController(store, source_adapter=adapter)
    controller.dispatch(ChooseVideoRequested(replace=replace))

    qtbot.waitUntil(lambda: store.state.source is SourceState.READY, timeout=5000)
    qtbot.waitUntil(lambda: controller.active_load_count == 0, timeout=5000)
    assert captured == {
        "caption": "Open video",
        "filter": "Video files (*.mp4 *.mov *.webm *.mkv)",
    }


@pytest.mark.parametrize("command", ["choose", "drop"])
def test_replacing_source_cancel_keeps_unsaved_transform_and_source(
    tmp_path, monkeypatch, qtbot, command: str
) -> None:
    old_path = tmp_path / "old.mp4"
    new_path = tmp_path / "new.mp4"
    load_error = AppError(
        ErrorCode.SOURCE_CORRUPT,
        "source.probe",
        "source.probe.corrupt",
        "new source cannot be decoded",
        "choose-another-file",
    )
    adapter = FakeSourceAdapter({}, {new_path: load_error})
    controller, store = _controller_with_unsaved_cut(tmp_path, old_path, adapter)
    confirmation: list[tuple[str, str, object]] = []

    def cancel(_parent, title, body, buttons, default):
        confirmation.append((title, body, default))
        assert buttons == QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        return int(QMessageBox.StandardButton.No)

    monkeypatch.setattr(QMessageBox, "question", cancel)
    if command == "choose":
        monkeypatch.setattr(
            QFileDialog,
            "getOpenFileName",
            lambda *_args, **_kwargs: (str(new_path), "Video files"),
        )
        controller.dispatch(ChooseVideoRequested(replace=True))
    else:
        controller.dispatch(VideoDropped(new_path))

    qtbot.wait(50)
    assert len(confirmation) == 1
    assert "unsaved transform changes" in confirmation[0][1]
    assert confirmation[0][2] == QMessageBox.StandardButton.No
    assert store.state.source is SourceState.READY
    assert getattr(store.state.source_value, "path", None) == old_path
    assert store.state.parameters.transform == TransformSpec(first_frame=1)
    assert adapter.thread_ids == []
    controller.shutdown()


def test_closing_cancel_keeps_unsaved_transform_and_leaves_window_open(
    tmp_path, monkeypatch, qtbot
) -> None:
    old_path = tmp_path / "old.mp4"
    controller, store = _controller_with_unsaved_cut(
        tmp_path, old_path, FakeSourceAdapter({})
    )
    window = MainWindow(store, controller, _settings("close-unsaved"))
    qtbot.addWidget(window)
    window.show()
    confirmation: list[tuple[str, str, object]] = []

    def cancel(_parent, title, body, buttons, default):
        confirmation.append((title, body, default))
        assert buttons == QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        return int(QMessageBox.StandardButton.No)

    monkeypatch.setattr(QMessageBox, "question", cancel)
    event = QCloseEvent()
    window.closeEvent(event)

    assert len(confirmation) == 1
    assert not event.isAccepted()
    assert window.isVisible()
    assert store.state.parameters.transform == TransformSpec(first_frame=1)
    controller.shutdown()


def test_discarded_transform_stays_discarded_when_the_new_source_fails(
    tmp_path, monkeypatch, qtbot
) -> None:
    old_path = tmp_path / "old.mp4"
    new_path = tmp_path / "broken.mp4"
    error = AppError(
        ErrorCode.SOURCE_CORRUPT,
        "source.probe",
        "source.probe.corrupt",
        "broken source",
        "choose-another-file",
    )
    adapter = FakeSourceAdapter({}, {new_path: error})
    controller, store = _controller_with_unsaved_cut(tmp_path, old_path, adapter)
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: int(QMessageBox.StandardButton.Yes),
    )

    controller.dispatch(VideoDropped(new_path))

    qtbot.waitUntil(lambda: store.state.source is SourceState.ERROR, timeout=5000)
    assert store.state.parameters.transform == TransformSpec()
    assert controller.transform_stage.session is None
    controller.shutdown()
