from __future__ import annotations

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QContextMenuEvent, QImage
from PySide6.QtWidgets import QMenu

from matteloop.core.geometry import PointF
from matteloop.core.parameters import ExclusionsChanged
from matteloop.core.specs import CropSpec
from matteloop.ui.crop_presentation import CropPresentation
from matteloop.ui.exclusion_canvas import ExclusionCanvas


def _presentation(
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
        crop=CropSpec(10, 10, 80, 30),
        exclusions=exclusions,
    )


def _canvas(
    qtbot,
    exclusions: tuple[CropSpec, ...] = (),
    *,
    editable: bool = True,
) -> ExclusionCanvas:
    canvas = ExclusionCanvas()
    qtbot.addWidget(canvas)
    canvas.resize(200, 100)
    canvas.set_frame(QImage(100, 50, QImage.Format.Format_RGBA8888))
    canvas.apply_presentation(
        _presentation(exclusions), active=True, editable=editable
    )
    canvas.show()
    return canvas


def _source_pos(canvas: ExclusionCanvas, x: int, y: int) -> QPoint:
    assert canvas._geometry is not None  # noqa: SLF001
    point = canvas._geometry.transform.source_to_widget(PointF(x, y))
    return QPoint(round(point.x), round(point.y))


def _action(menu: QMenu, text: str):
    return next(action for action in menu.actions() if action.text() == text)


def test_context_menu_lists_region_actions_for_empty_and_occupied_canvas_areas(
    qtbot,
) -> None:
    empty_canvas = _canvas(qtbot)
    empty_menu = empty_canvas._build_context_menu(  # noqa: SLF001
        _source_pos(empty_canvas, 5, 5)
    )

    occupied_canvas = _canvas(
        qtbot, exclusions=(CropSpec(20, 10, 30, 20),)
    )
    occupied_menu = occupied_canvas._build_context_menu(  # noqa: SLF001
        _source_pos(occupied_canvas, 25, 15)
    )

    assert [action.text() for action in empty_menu.actions()] == [
        "Exclude this area",
        "Edit exclusion regions",
    ]
    assert [action.text() for action in occupied_menu.actions()] == [
        "Remove this region",
        "Remove all regions",
        "Edit exclusion regions",
    ]
    # A QAction carries no accessible-name API; assistive technology reads the
    # action's text, so that is what has to be present and meaningful.
    assert all(action.text().strip() for action in occupied_menu.actions())
    assert all(not action.isSeparator() for action in occupied_menu.actions())
    assert _action(occupied_menu, "Edit exclusion regions").isCheckable()
    assert not _action(occupied_menu, "Edit exclusion regions").isChecked()

    occupied_canvas.set_exclusion_edit(True)
    checked_menu = occupied_canvas._build_context_menu(  # noqa: SLF001
        _source_pos(occupied_canvas, 25, 15)
    )
    assert _action(checked_menu, "Edit exclusion regions").isChecked()


def test_context_menu_excludes_a_clamped_default_region_with_one_dispatch(
    qtbot,
) -> None:
    canvas = _canvas(qtbot)
    events: list[object] = []
    canvas.command_requested.connect(events.append)

    menu = canvas._build_context_menu(  # noqa: SLF001
        _source_pos(canvas, 95, 45)
    )
    _action(menu, "Exclude this area").trigger()

    assert events == [ExclusionsChanged((CropSpec(80, 40, 20, 10),))]
    assert canvas._exclusion_edit is True  # noqa: SLF001
    assert canvas._selected_exclusion == 0  # noqa: SLF001


def test_context_menu_removes_the_region_under_the_pointer(qtbot) -> None:
    regions = (CropSpec(10, 5, 20, 15), CropSpec(70, 20, 20, 15))
    canvas = _canvas(qtbot, exclusions=regions)
    events: list[object] = []
    canvas.command_requested.connect(events.append)

    menu = canvas._build_context_menu(  # noqa: SLF001
        _source_pos(canvas, 75, 25)
    )
    _action(menu, "Remove this region").trigger()

    assert events == [ExclusionsChanged((regions[0],))]


def test_context_menu_removes_all_regions_with_one_dispatch(qtbot) -> None:
    canvas = _canvas(
        qtbot, exclusions=(CropSpec(10, 5, 20, 15), CropSpec(70, 20, 20, 15))
    )
    events: list[object] = []
    canvas.command_requested.connect(events.append)

    menu = canvas._build_context_menu(  # noqa: SLF001
        _source_pos(canvas, 5, 5)
    )
    _action(menu, "Remove all regions").trigger()

    assert events == [ExclusionsChanged(())]


class _ContextMenuProbe(ExclusionCanvas):
    def __init__(self) -> None:
        super().__init__()
        self.shown_position: QPoint | None = None
        self.shown_actions: list[str] = []

    def _show_context_menu(self, menu: QMenu, position: QPoint) -> None:
        self.shown_position = position
        self.shown_actions = [action.text() for action in menu.actions()]


def test_keyboard_context_menu_uses_the_focused_region_position(qtbot) -> None:
    canvas = _ContextMenuProbe()
    qtbot.addWidget(canvas)
    canvas.resize(200, 100)
    canvas.set_frame(QImage(100, 50, QImage.Format.Format_RGBA8888))
    canvas.apply_presentation(
        _presentation(exclusions=(CropSpec(20, 10, 30, 20),)),
        active=True,
        editable=True,
    )
    canvas.set_exclusion_edit(True)
    canvas._set_exclusion_selection(0)  # noqa: SLF001
    canvas.setFocus(Qt.FocusReason.OtherFocusReason)

    event = QContextMenuEvent(
        QContextMenuEvent.Reason.Keyboard,
        QPoint(0, 0),
        QPoint(0, 0),
    )
    canvas.contextMenuEvent(event)

    assert canvas.shown_position is not None
    assert canvas.shown_position != QPoint(0, 0)
    assert canvas.shown_actions[0] == "Remove this region"
    assert event.isAccepted()


def test_context_menu_offers_no_actions_when_editing_is_unavailable(qtbot) -> None:
    canvas = _canvas(qtbot, exclusions=(CropSpec(20, 10, 30, 20),), editable=False)
    events: list[object] = []
    canvas.command_requested.connect(events.append)

    menu = canvas._build_context_menu(  # noqa: SLF001
        _source_pos(canvas, 25, 15)
    )

    assert menu.actions() == []
    assert events == []
