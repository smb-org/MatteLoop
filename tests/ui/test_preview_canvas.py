from __future__ import annotations

from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from matteloop.ui.preview_canvas import PreviewCanvas, PreviewStage


def test_checkerboard_predicate_follows_the_property_alone(qtbot) -> None:
    """A plain PreviewCanvas has no frame session to hold, so its checkerboard
    predicate must keep following the ``checkerboard`` property exactly as it
    did before ``ResultPlayerCanvas`` gained an overridable reason of its
    own (see test_result_player.py's player-holding-frames case)."""
    canvas = PreviewCanvas("Original", "original_canvas")
    qtbot.addWidget(canvas)

    canvas.setProperty("checkerboard", False)
    assert canvas._should_paint_checkerboard() is False  # noqa: SLF001

    canvas.setProperty("checkerboard", True)
    assert canvas._should_paint_checkerboard() is True  # noqa: SLF001


def test_result_preview_uses_fit_scaling(qtbot) -> None:
    stage = PreviewStage()
    qtbot.addWidget(stage)

    assert stage.original_canvas._cover_frame is False  # noqa: SLF001
    assert stage.result_canvas._cover_frame is False  # noqa: SLF001


def _image(color: int, width: int = 160, height: int = 90) -> QImage:
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(color)
    return image


def test_hold_live_frame_shows_it_and_defers_stills_until_release(qtbot) -> None:
    canvas = PreviewCanvas("Original", "original_canvas")
    qtbot.addWidget(canvas)
    live, still = _image(0xFF0000), _image(0x0000FF)
    canvas.set_frame(_image(0x00FF00))

    canvas.hold_live_frame(live)
    assert canvas.holding_live_frame is True
    assert canvas.pixmap() is not None and not canvas.pixmap().isNull()

    canvas.set_frame(still)
    assert canvas._frame == live  # noqa: SLF001

    canvas.release_live_frame()
    assert canvas._frame == still  # noqa: SLF001
    assert canvas.holding_live_frame is False


def test_release_applies_a_pending_cleared_frame(qtbot) -> None:
    canvas = PreviewCanvas("Original", "original_canvas")
    qtbot.addWidget(canvas)
    canvas.hold_live_frame(_image(0xFF0000))

    canvas.set_frame(None)
    canvas.release_live_frame()

    assert canvas.text() == "Original"
    assert canvas._frame is None  # noqa: SLF001


def test_display_size_matches_the_box_stills_are_scaled_into(qtbot) -> None:
    canvas = PreviewCanvas("Original", "original_canvas")
    qtbot.addWidget(canvas)
    width, height = canvas.display_size()
    assert width >= 1 and height >= 1

    canvas.resize(300, 200)
    canvas.show()
    qtbot.waitExposed(canvas)
    size = canvas.display_size()
    canvas.set_frame(_image(0xFF0000, *size))

    assert canvas.pixmap() is not None
    assert size == (canvas.pixmap().width(), canvas.pixmap().height())


def test_stage_places_the_playback_row_under_the_original_canvas(qtbot) -> None:
    stage = PreviewStage()
    qtbot.addWidget(stage)
    stage.resize(900, 500)
    stage.show()
    QApplication.processEvents()

    canvas, row = stage.original_canvas, stage.playback_controls
    assert row.geometry().top() >= canvas.geometry().bottom()
    assert row.x() == canvas.x()
