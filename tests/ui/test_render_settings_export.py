from __future__ import annotations

import json
from dataclasses import dataclass, replace
from fractions import Fraction
from pathlib import Path

from PySide6.QtCore import QPoint, QSettings, Qt
from PySide6.QtGui import QContextMenuEvent, QGuiApplication
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from matteloop.core.execution_providers import CPU_EXECUTION_PROVIDER
from matteloop.core.request_json import render_settings_from_json
from matteloop.core.specs import CropSpec, EdgeMode, SegmentationSpec
from matteloop.core.state import AppState, SourceLoaded, SourceLoadRequested, reduce
from matteloop.ui.controller import SourceController
from matteloop.ui.main_window import MainWindow
from matteloop.ui.ports import CopyRenderSettingsRequested
from matteloop.ui.render_settings_export import RenderSettingsExportController
from matteloop.ui.store import ReducerStore


@dataclass(frozen=True)
class _Metadata:
    path: Path
    width: int = 128
    height: int = 64
    duration: Fraction = Fraction(2)
    average_rate: Fraction = Fraction(30)


def _settings(name: str) -> QSettings:
    settings = QSettings(
        QSettings.IniFormat, QSettings.UserScope, "matteloop-test", name
    )
    settings.clear()
    return settings


def _ready_store(path: Path) -> ReducerStore:
    loading = reduce(AppState(), SourceLoadRequested("source", "load"))
    return ReducerStore(
        reduce(loading, SourceLoaded("source", "load", _Metadata(path)))
    )


def _window(
    qtbot, store: ReducerStore, name: str
) -> tuple[MainWindow, SourceController]:
    controller = SourceController(store)
    window = MainWindow(store, controller, _settings(name))
    qtbot.addWidget(window)
    window.show()
    return window, controller


_DISABLED_REASON = "Open a video with a valid range to copy its render settings."


def test_copy_render_settings_action_is_disabled_without_a_source(qtbot) -> None:
    store = ReducerStore()
    window, _controller = _window(qtbot, store, "export-no-source")

    assert window.copy_render_settings_action.isEnabled() is False
    assert window.copy_render_settings_action.toolTip() == _DISABLED_REASON


def test_copy_render_settings_action_is_enabled_with_a_loaded_source(
    qtbot, tmp_path: Path
) -> None:
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"not a real video, just a path for the request builder")
    store = _ready_store(source)
    window, _controller = _window(qtbot, store, "export-ready")

    assert window.copy_render_settings_action.isEnabled() is True
    assert window.copy_render_settings_action.toolTip() != _DISABLED_REASON


def test_render_button_exposes_a_context_menu_with_the_copy_action(
    qtbot, tmp_path: Path
) -> None:
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"stand-in bytes")
    store = _ready_store(source)
    window, _controller = _window(qtbot, store, "export-menu")

    menu = window.action_shelf._render_context_menu()
    try:
        assert window.copy_render_settings_action in menu.actions()
        assert menu.toolTipsVisible() is True
    finally:
        menu.close()


def test_triggering_the_action_copies_json_that_decodes_back_to_the_same_settings(
    qtbot, tmp_path: Path
) -> None:
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"stand-in bytes")
    store = _ready_store(source)
    window, controller = _window(qtbot, store, "export-copy")

    controller.dispatch(CopyRenderSettingsRequested())

    clipboard = QGuiApplication.clipboard()
    assert clipboard is not None
    text = clipboard.text()
    rebuilt = render_settings_from_json(text, directory=tmp_path, filename="out.webp")

    assert rebuilt.source == source
    assert rebuilt.sampling.start == Fraction(0)
    assert rebuilt.sampling.end == Fraction(2)
    assert rebuilt.sampling.fps == 15
    assert rebuilt.crop == CropSpec(0, 0, 128, 64)
    assert rebuilt.segmentation == SegmentationSpec(
        model_id="birefnet-portrait",
        edge_mode=EdgeMode.STANDARD,
        execution_provider=CPU_EXECUTION_PROVIDER,
    )


def test_triggering_the_action_without_a_source_does_not_touch_the_clipboard(
    qtbot,
) -> None:
    store = ReducerStore()
    _unused_window, controller = _window(qtbot, store, "export-noop")
    clipboard = QGuiApplication.clipboard()
    assert clipboard is not None
    clipboard.setText("sentinel")

    controller.dispatch(CopyRenderSettingsRequested())

    assert clipboard.text() == "sentinel"


def _disabled_render_store(source: Path) -> ReducerStore:
    """A ready source whose Render button is disabled (model not available)."""
    return ReducerStore(replace(_ready_store(source).state, model_available=False))


def test_render_button_is_disabled_but_the_export_action_stays_enabled(
    qtbot, tmp_path: Path
) -> None:
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"stand-in bytes")
    store = _disabled_render_store(source)
    window, _controller = _window(qtbot, store, "export-render-disabled")

    assert window.render_button.isEnabled() is False
    assert window.copy_render_settings_action.isEnabled() is True


def test_shortcut_triggers_the_export_while_render_is_disabled(
    qtbot, tmp_path: Path
) -> None:
    """Issue #170 review: a disabled Render button must not swallow the
    export shortcut. The action lives on the window (WindowShortcut), not
    on the button, precisely so this keeps working."""
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"stand-in bytes")
    store = _disabled_render_store(source)
    window, _controller = _window(qtbot, store, "export-shortcut-disabled")
    window.activateWindow()
    QTest.qWaitForWindowExposed(window)
    assert window.render_button.isEnabled() is False
    clipboard = QGuiApplication.clipboard()
    assert clipboard is not None
    clipboard.setText("sentinel")

    QTest.keyClick(
        window,
        Qt.Key.Key_C,
        Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier,
    )
    QApplication.processEvents()

    assert clipboard.text() != "sentinel"
    assert json.loads(clipboard.text())["source"] == str(source)


class _StubMenu:
    """A duck-typed popup that records `.exec()` without blocking.

    `QMenu.exec()` opens a real nested event loop that never returns
    without a selection or dismissal, and monkeypatching `QMenu.exec` at
    the class level does not reach PySide6's C++-backed method dispatch --
    so the stub replaces `_render_context_menu()`'s return value instead of
    trying to intercept the real popup.
    """

    def __init__(self) -> None:
        self.shown: list[QPoint] = []

    def exec(self, pos: QPoint) -> None:
        self.shown.append(pos)


def test_right_click_context_menu_reaches_the_action_while_render_is_disabled(
    qtbot, tmp_path: Path, monkeypatch
) -> None:
    """Issue #170 review: Qt drops a disabled widget's own ContextMenu event
    and customContextMenuRequested signal, so the right-click route has to
    go through an event filter instead -- verified here by feeding the
    button's installed filter the same event Qt would deliver."""
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"stand-in bytes")
    store = _disabled_render_store(source)
    window, _controller = _window(qtbot, store, "export-context-menu-disabled")
    assert window.render_button.isEnabled() is False

    stub = _StubMenu()
    monkeypatch.setattr(window.action_shelf, "_render_context_menu", lambda: stub)

    event = QContextMenuEvent(
        QContextMenuEvent.Reason.Mouse, QPoint(5, 5), QPoint(100, 100)
    )
    handled = window.action_shelf.eventFilter(window.render_button, event)

    assert handled is True
    assert stub.shown == [QPoint(100, 100)]


def test_confirmation_is_anchored_to_the_given_widget_not_the_cursor(
    qtbot, tmp_path: Path, monkeypatch
) -> None:
    """Issue #170 review, item 12: position the confirmation near the
    Render button/window, not wherever the cursor happens to be."""
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"stand-in bytes")
    store = _ready_store(source)
    anchor = QWidget()
    anchor.resize(80, 40)
    qtbot.addWidget(anchor)
    anchor.show()
    controller = RenderSettingsExportController(store)
    controller.set_anchor_widget(anchor)

    shown_positions: list[QPoint] = []
    monkeypatch.setattr(
        "matteloop.ui.render_settings_export.QToolTip.showText",
        staticmethod(lambda pos, *_args, **_kwargs: shown_positions.append(pos)),
    )

    controller.dispatch(CopyRenderSettingsRequested())

    assert shown_positions == [anchor.mapToGlobal(QPoint(0, anchor.height()))]


def test_no_confirmation_when_the_clipboard_does_not_actually_match(
    qtbot, tmp_path: Path, monkeypatch
) -> None:
    """Issue #170 review, item 12: only confirm what actually happened --
    not every platform's clipboard reliably holds what was just set."""
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"stand-in bytes")
    store = _ready_store(source)
    controller = RenderSettingsExportController(store)

    class _LossyClipboard:
        def setText(self, _text: str) -> None:
            pass

        def text(self) -> str:
            return "something else entirely"

    monkeypatch.setattr(
        QGuiApplication, "clipboard", staticmethod(lambda: _LossyClipboard())
    )
    shown: list[object] = []
    monkeypatch.setattr(
        "matteloop.ui.render_settings_export.QToolTip.showText",
        staticmethod(lambda *args, **kwargs: shown.append(args)),
    )

    controller.dispatch(CopyRenderSettingsRequested())

    assert shown == []
