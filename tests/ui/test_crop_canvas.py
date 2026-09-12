from __future__ import annotations

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QImage

from matteloop.core.crop_state import CropChanged
from matteloop.core.geometry import MediaTransform, PointF
from matteloop.core.parameters import ExclusionsChanged
from matteloop.core.specs import CropSpec
from matteloop.ui.crop_canvas import CropCanvas
from matteloop.ui.crop_presentation import CropPresentation
from matteloop.ui.preview_canvas import PreviewStage


def _presentation(
    crop: CropSpec = CropSpec(10, 10, 40, 20),
    exclusions: tuple[CropSpec, ...] = (),
) -> CropPresentation:
    return CropPresentation(
        source_id="source",
        width=100,
        height=50,
        coded_width=100,
        coded_height=50,
        rotation=0,
        pixel_aspect=1,
        crop=crop,
        exclusions=exclusions,
    )


def _canvas(qtbot) -> CropCanvas:
    canvas = CropCanvas()
    qtbot.addWidget(canvas)
    canvas.resize(200, 100)
    canvas.set_frame(QImage(100, 50, QImage.Format.Format_RGBA8888))
    canvas.apply_presentation(_presentation(), active=True, editable=True)
    canvas.show()
    return canvas


def _source_pos(canvas: CropCanvas, x: int, y: int) -> QPoint:
    assert canvas._geometry is not None
    point = canvas._geometry.transform.source_to_widget(PointF(x, y))
    return QPoint(round(point.x), round(point.y))


def test_crop_canvas_drag_emits_oriented_crop_changes_from_shared_geometry(
    qtbot,
) -> None:
    canvas = _canvas(qtbot)
    events: list[object] = []
    canvas.command_requested.connect(events.append)

    qtbot.mousePress(canvas, Qt.MouseButton.LeftButton, pos=QPoint(60, 80))
    qtbot.mouseMove(canvas, QPoint(80, 90))
    qtbot.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=QPoint(80, 90))

    assert isinstance(events[-1], CropChanged)
    assert events[-1].crop == CropSpec(22, 16, 40, 20)


def test_a_region_drag_dispatches_exactly_once_on_release(qtbot) -> None:
    canvas = _canvas(qtbot)
    canvas.apply_presentation(
        _presentation(exclusions=(CropSpec(20, 10, 30, 20),)),
        active=True,
        editable=True,
    )
    canvas.set_exclusion_edit(True)
    events: list[object] = []
    canvas.command_requested.connect(events.append)

    qtbot.mousePress(
        canvas,
        Qt.MouseButton.LeftButton,
        pos=_source_pos(canvas, 25, 15),
    )
    for x in range(26, 46):
        qtbot.mouseMove(canvas, _source_pos(canvas, x, 15))

    assert events == []
    qtbot.mouseRelease(
        canvas,
        Qt.MouseButton.LeftButton,
        pos=_source_pos(canvas, 45, 15),
    )

    assert len(events) == 1
    assert events[0] == ExclusionsChanged((CropSpec(40, 10, 30, 20),))


def test_an_abandoned_rubber_band_dispatches_nothing(qtbot) -> None:
    canvas = _canvas(qtbot)
    canvas.set_exclusion_edit(True)
    canvas.activateWindow()
    canvas.setFocus()
    events: list[object] = []
    canvas.command_requested.connect(events.append)

    qtbot.mousePress(
        canvas,
        Qt.MouseButton.LeftButton,
        pos=_source_pos(canvas, 10, 10),
    )
    qtbot.mouseMove(canvas, _source_pos(canvas, 30, 25))
    canvas.setFocus()
    qtbot.waitUntil(canvas.hasFocus, timeout=1000)
    assert canvas._rubber_start is not None
    qtbot.keyClick(canvas, Qt.Key.Key_Escape)
    qtbot.mouseRelease(
        canvas,
        Qt.MouseButton.LeftButton,
        pos=_source_pos(canvas, 30, 25),
    )

    assert events == []


def test_region_mode_rubber_band_adds_a_region_on_release(qtbot) -> None:
    canvas = _canvas(qtbot)
    canvas.set_exclusion_edit(True)
    events: list[object] = []
    canvas.command_requested.connect(events.append)

    qtbot.mousePress(
        canvas,
        Qt.MouseButton.LeftButton,
        pos=_source_pos(canvas, 10, 10),
    )
    qtbot.mouseMove(canvas, _source_pos(canvas, 30, 25))
    qtbot.mouseRelease(
        canvas,
        Qt.MouseButton.LeftButton,
        pos=_source_pos(canvas, 30, 25),
    )

    assert events == [ExclusionsChanged((CropSpec(10, 10, 20, 15),))]


def test_region_mode_discards_a_rubber_band_below_eight_pixels(qtbot) -> None:
    canvas = _canvas(qtbot)
    canvas.set_exclusion_edit(True)
    events: list[object] = []
    canvas.command_requested.connect(events.append)

    qtbot.mousePress(
        canvas,
        Qt.MouseButton.LeftButton,
        pos=_source_pos(canvas, 10, 10),
    )
    qtbot.mouseMove(canvas, _source_pos(canvas, 17, 18))
    qtbot.mouseRelease(
        canvas,
        Qt.MouseButton.LeftButton,
        pos=_source_pos(canvas, 17, 18),
    )

    assert events == []


def test_region_mode_gives_the_crop_no_hit_targets(qtbot) -> None:
    canvas = _canvas(qtbot)
    canvas.apply_presentation(
        _presentation(exclusions=(CropSpec(70, 30, 20, 10),)),
        active=True,
        editable=True,
    )
    canvas.set_exclusion_edit(True)
    events: list[object] = []
    canvas.command_requested.connect(events.append)

    qtbot.mouseClick(
        canvas,
        Qt.MouseButton.LeftButton,
        pos=_source_pos(canvas, 10, 10),
    )

    assert events == []
    assert canvas._dragged is None


def test_click_selects_a_region_and_the_same_drag_moves_it(qtbot) -> None:
    canvas = _canvas(qtbot)
    canvas.apply_presentation(
        _presentation(exclusions=(CropSpec(20, 10, 30, 20),)),
        active=True,
        editable=True,
    )
    canvas.set_exclusion_edit(True)
    events: list[object] = []
    canvas.command_requested.connect(events.append)

    qtbot.mousePress(
        canvas,
        Qt.MouseButton.LeftButton,
        pos=_source_pos(canvas, 25, 15),
    )
    qtbot.mouseMove(canvas, _source_pos(canvas, 35, 15))
    qtbot.mouseRelease(
        canvas,
        Qt.MouseButton.LeftButton,
        pos=_source_pos(canvas, 35, 15),
    )

    assert events == [ExclusionsChanged((CropSpec(30, 10, 30, 20),))]


def test_delete_removes_the_selected_region(qtbot) -> None:
    canvas = _canvas(qtbot)
    canvas.apply_presentation(
        _presentation(exclusions=(CropSpec(20, 10, 30, 20),)),
        active=True,
        editable=True,
    )
    canvas.set_exclusion_edit(True)
    canvas.activateWindow()
    canvas.setFocus()
    events: list[object] = []
    canvas.command_requested.connect(events.append)

    qtbot.mouseClick(
        canvas,
        Qt.MouseButton.LeftButton,
        pos=_source_pos(canvas, 25, 15),
    )
    canvas.setFocus()
    qtbot.waitUntil(canvas.hasFocus, timeout=1000)
    assert canvas._selected_exclusion == 0
    qtbot.keyClick(canvas, Qt.Key.Key_Delete)

    assert events == [ExclusionsChanged(())]


def test_arrow_key_nudges_the_selected_region_once(qtbot) -> None:
    canvas = _canvas(qtbot)
    canvas.apply_presentation(
        _presentation(exclusions=(CropSpec(20, 10, 30, 20),)),
        active=True,
        editable=True,
    )
    canvas.set_exclusion_edit(True)
    qtbot.mouseClick(
        canvas,
        Qt.MouseButton.LeftButton,
        pos=_source_pos(canvas, 25, 15),
    )
    canvas.setFocus()
    qtbot.waitUntil(canvas.hasFocus, timeout=1000)
    events: list[object] = []
    canvas.command_requested.connect(events.append)

    qtbot.keyClick(canvas, Qt.Key.Key_Right)

    assert events == [ExclusionsChanged((CropSpec(21, 10, 30, 20),))]


def test_leaving_region_mode_restores_crop_handles_and_keeps_painting_regions(
    qtbot,
) -> None:
    region = CropSpec(20, 10, 30, 20)
    canvas = _canvas(qtbot)
    canvas.apply_presentation(
        _presentation(exclusions=(region,)), active=True, editable=True
    )
    canvas.set_exclusion_edit(True)
    qtbot.mouseClick(
        canvas,
        Qt.MouseButton.LeftButton,
        pos=_source_pos(canvas, 25, 15),
    )
    canvas.set_exclusion_edit(False)

    assert canvas._selected_exclusion is None
    assert canvas._painted_exclusions() == (region,)
    assert canvas._geometry is not None
    assert set(canvas._geometry.visual) >= {
        "crop",
        "north_west",
        "north",
        "north_east",
    }


def test_region_mode_announces_the_selected_region_bounds(qtbot) -> None:
    canvas = _canvas(qtbot)
    canvas.apply_presentation(
        _presentation(exclusions=(CropSpec(20, 10, 30, 20),)),
        active=True,
        editable=True,
    )
    canvas.set_exclusion_edit(True)

    qtbot.mouseClick(
        canvas,
        Qt.MouseButton.LeftButton,
        pos=_source_pos(canvas, 25, 15),
    )

    assert canvas.accessibleDescription() == (
        "Exclusion region 1 of 1: x 20, y 10, width 30, height 20 source pixels"
    )


def test_crop_canvas_keyboard_moves_overlay_by_one_source_pixel(qtbot) -> None:
    canvas = _canvas(qtbot)
    events: list[object] = []
    canvas.command_requested.connect(events.append)
    canvas.activateWindow()
    canvas.setFocus()
    qtbot.waitUntil(canvas.hasFocus, timeout=1000)

    qtbot.keyClick(canvas, Qt.Key.Key_Left)
    canvas.apply_presentation(
        _presentation(CropSpec(9, 10, 40, 20)), active=True, editable=True
    )
    qtbot.keyClick(canvas, Qt.Key.Key_Up, Qt.KeyboardModifier.ShiftModifier)

    assert [event.crop for event in events if isinstance(event, CropChanged)] == [
        CropSpec(9, 10, 40, 20),
        CropSpec(9, 0, 40, 20),
    ]


def test_crop_canvas_focused_handle_resizes_with_shift_keyboard_step(qtbot) -> None:
    canvas = _canvas(qtbot)
    events: list[object] = []
    canvas.command_requested.connect(events.append)

    qtbot.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=QPoint(100, 80))
    qtbot.waitUntil(canvas.hasFocus, timeout=1000)
    qtbot.keyClick(canvas, Qt.Key.Key_Right, Qt.KeyboardModifier.ShiftModifier)

    assert isinstance(events[-1], CropChanged)
    assert events[-1].crop == CropSpec(10, 10, 50, 20)


def test_crop_canvas_announces_current_oriented_bounds_on_its_standard_widget(
    qtbot,
) -> None:
    canvas = _canvas(qtbot)

    assert "x 10" in canvas.accessibleDescription()
    assert "100 × 50" in canvas.accessibleDescription()


def _large_source_canvas(qtbot, crop: CropSpec) -> tuple[PreviewStage, CropCanvas]:
    stage = PreviewStage()
    qtbot.addWidget(stage)
    canvas = stage.original_canvas
    canvas.resize(478, 574)
    canvas.set_frame(QImage(1280, 720, QImage.Format.Format_RGBA8888))
    canvas.apply_presentation(
        CropPresentation(
            source_id="source",
            width=1280,
            height=720,
            coded_width=1280,
            coded_height=720,
            rotation=0,
            pixel_aspect=1,
            crop=crop,
        ),
        active=True,
        editable=True,
    )
    canvas.show()
    qtbot.wait(10)
    return stage, canvas


def test_crop_canvas_fits_source_corners_inside_the_painted_pixmap(qtbot) -> None:
    _stage, canvas = _large_source_canvas(qtbot, CropSpec(0, 0, 1280, 720))
    assert canvas._geometry is not None
    transform = canvas._geometry.transform
    assert isinstance(transform, MediaTransform)

    for corner in (PointF(0, 0), PointF(1280, 720)):
        mapped = transform.source_to_widget(corner)
        assert 0 <= mapped.x <= canvas.width()
        assert 0 <= mapped.y <= canvas.height()

    assert transform.scale == pytest.approx(446 / 1280)
    assert transform.content_rect.width == pytest.approx(
        canvas.pixmap().width(), abs=0.01
    )
    assert transform.content_rect.height == pytest.approx(
        canvas.pixmap().height(), abs=1.0
    )
    assert transform.content_rect.x == pytest.approx(
        (canvas.width() - canvas.pixmap().width()) / 2, abs=0.5
    )
    assert transform.content_rect.y == pytest.approx(
        (canvas.height() - canvas.pixmap().height()) / 2, abs=0.5
    )


def test_crop_canvas_drag_uses_the_fitted_frame_scale(qtbot) -> None:
    _stage, canvas = _large_source_canvas(qtbot, CropSpec(100, 100, 400, 200))
    assert canvas._geometry is not None
    transform = canvas._geometry.transform
    assert isinstance(transform, MediaTransform)
    events: list[object] = []
    canvas.command_requested.connect(events.append)

    qtbot.mousePress(
        canvas,
        Qt.MouseButton.LeftButton,
        pos=QPoint(80, 230),
    )
    qtbot.mouseMove(canvas, QPoint(160, 230))
    qtbot.mouseRelease(
        canvas,
        Qt.MouseButton.LeftButton,
        pos=QPoint(160, 230),
    )

    assert isinstance(events[-1], CropChanged)
    assert events[-1].crop == CropSpec(330, 100, 400, 200)


def test_crop_canvas_drag_from_the_media_gutter_emits_crop_changed(qtbot) -> None:
    _stage, canvas = _large_source_canvas(qtbot, CropSpec(0, 0, 1280, 720))
    events: list[object] = []
    canvas.command_requested.connect(events.append)

    qtbot.mousePress(
        canvas,
        Qt.MouseButton.LeftButton,
        pos=QPoint(8, 287),
    )
    qtbot.mouseMove(canvas, QPoint(20, 287))
    qtbot.mouseRelease(
        canvas,
        Qt.MouseButton.LeftButton,
        pos=QPoint(20, 287),
    )

    assert any(isinstance(event, CropChanged) for event in events)


def test_crop_canvas_accepts_title_and_object_name_overrides(qtbot) -> None:
    canvas = CropCanvas(title="Result", object_name="x")
    qtbot.addWidget(canvas)

    assert canvas.objectName() == "x"
    assert canvas.accessibleName() == "Background-removed result"


class _ConstrainedCanvas(CropCanvas):
    """A subclass exercising the reuse hooks a real result canvas overrides."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.events: list[object] = []

    def _constrain(self, crop, target):
        return CropSpec(crop.x, crop.y, crop.width, crop.width)

    def _crop_event(self, crop):
        event = ("constrained", crop)
        self.events.append(event)
        return event


def test_crop_canvas_drag_routes_through_the_constrain_and_event_hooks(qtbot) -> None:
    canvas = _ConstrainedCanvas()
    qtbot.addWidget(canvas)
    canvas.resize(200, 100)
    canvas.set_frame(QImage(100, 50, QImage.Format.Format_RGBA8888))
    canvas.apply_presentation(_presentation(), active=True, editable=True)
    canvas.show()

    qtbot.mousePress(canvas, Qt.MouseButton.LeftButton, pos=QPoint(60, 80))
    qtbot.mouseMove(canvas, QPoint(80, 90))
    qtbot.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=QPoint(80, 90))

    assert canvas.events, "the drag must reach the overridden hooks"
    kind, crop = canvas.events[-1]
    assert kind == "constrained"
    assert crop.width == crop.height


def test_crop_canvas_keyboard_nudge_routes_through_the_constrain_and_event_hooks(
    qtbot,
) -> None:
    canvas = _ConstrainedCanvas()
    qtbot.addWidget(canvas)
    canvas.resize(200, 100)
    canvas.set_frame(QImage(100, 50, QImage.Format.Format_RGBA8888))
    canvas.apply_presentation(_presentation(), active=True, editable=True)
    canvas.show()
    canvas.activateWindow()
    canvas.setFocus()
    qtbot.waitUntil(canvas.hasFocus, timeout=1000)

    qtbot.keyClick(canvas, Qt.Key.Key_Left)

    assert canvas.events, "the keyboard nudge must reach the overridden hooks"
    kind, crop = canvas.events[-1]
    assert kind == "constrained"
    assert crop.width == crop.height


def test_crop_canvas_uses_the_clamped_inset_for_a_tiny_canvas(qtbot) -> None:
    canvas = CropCanvas()
    qtbot.addWidget(canvas)
    canvas.setMinimumSize(0, 0)
    canvas.resize(20, 10)
    canvas.set_frame(QImage(100, 50, QImage.Format.Format_RGBA8888))
    canvas.apply_presentation(_presentation(), active=True, editable=True)
    canvas.show()
    qtbot.wait(10)

    assert canvas._geometry is not None
    transform = canvas._geometry.transform
    assert isinstance(transform, MediaTransform)
    assert transform.inset == pytest.approx(2.0)
    assert transform.content_rect.width == pytest.approx(
        canvas.pixmap().width(), abs=0.01
    )
    assert transform.content_rect.height == pytest.approx(
        canvas.pixmap().height(), abs=1.0
    )
    assert transform.content_rect.x == pytest.approx(
        (canvas.width() - canvas.pixmap().width()) / 2, abs=0.5
    )
    assert transform.content_rect.y == pytest.approx(
        (canvas.height() - canvas.pixmap().height()) / 2, abs=0.5
    )
