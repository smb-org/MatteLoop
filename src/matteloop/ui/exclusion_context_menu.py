"""Context-menu behavior for the source exclusion canvas."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from PySide6.QtCore import QCoreApplication, QPoint, Qt
from PySide6.QtGui import QAction, QContextMenuEvent, QKeyEvent
from PySide6.QtWidgets import QMenu

from matteloop.core.geometry import MediaTransform, PointF
from matteloop.core.parameters import ExclusionsChanged
from matteloop.core.specs import CropSpec

if TYPE_CHECKING:
    from matteloop.ui.exclusion_canvas import ExclusionCanvas


def handle_context_menu_event(
    canvas: ExclusionCanvas, event: QContextMenuEvent
) -> None:
    if canvas._presentation is None or not canvas._editable:
        event.ignore()
        return
    position = context_menu_position(canvas, event)
    target = (
        selected_context_region(canvas)
        if event.reason() is QContextMenuEvent.Reason.Keyboard
        else None
    )
    menu = build_context_menu(canvas, position, target=target)
    if not menu.actions():
        menu.deleteLater()
        event.ignore()
        return
    try:
        canvas._show_context_menu(menu, position)
    finally:
        menu.deleteLater()
    event.accept()


def build_context_menu(
    canvas: ExclusionCanvas, position: QPoint, *, target: CropSpec | None = None
) -> QMenu:
    menu = QMenu(canvas)
    presentation = canvas._presentation
    if presentation is None or not canvas._editable:
        return menu
    if target is None:
        index = exclusion_at(canvas, position)
        target = presentation.exclusions[index] if index is not None else None
    if target is None:
        add_context_action(
            menu,
            QCoreApplication.translate("ExclusionCanvas", "Exclude this area"),
            "exclude_area",
            lambda: canvas._create_exclusion(position),
        )
    else:
        assert target is not None

        def remove_target() -> None:
            remove_exclusion(canvas, target)

        add_context_action(
            menu,
            QCoreApplication.translate("ExclusionCanvas", "Remove this region"),
            "remove_exclusion",
            remove_target,
        )
    if presentation.exclusions:
        add_context_action(
            menu,
            QCoreApplication.translate("ExclusionCanvas", "Remove all regions"),
            "remove_all_exclusions",
            lambda: remove_all_exclusions(canvas),
        )
    add_edit_context_action(canvas, menu)
    return menu


def add_edit_context_action(canvas: ExclusionCanvas, menu: QMenu) -> None:
    action = add_context_action(
        menu,
        QCoreApplication.translate("ExclusionCanvas", "Edit exclusion regions"),
        "edit_exclusion_regions",
        None,
    )
    action.setCheckable(True)
    action.setChecked(canvas._exclusion_edit)
    action.triggered.connect(lambda enabled: toggle_exclusion_edit(canvas, enabled))


def add_context_action(
    menu: QMenu,
    text: str,
    object_name: str,
    callback: Callable[[], None] | None,
) -> QAction:
    action = QAction(text, menu)
    action.setObjectName(object_name)
    action.setToolTip(text)
    action.setStatusTip(text)
    if callback is not None:

        def trigger(_checked: bool = False) -> None:
            callback()

        action.triggered.connect(trigger)
    menu.addAction(action)
    return action


def context_menu_position(canvas: ExclusionCanvas, event: QContextMenuEvent) -> QPoint:
    if event.reason() is QContextMenuEvent.Reason.Keyboard:
        region = selected_context_region(canvas)
        if region is not None and canvas._geometry is not None:
            rect = canvas._widget_rect(canvas._geometry, region)
            return QPoint(round(rect.center().x()), round(rect.center().y()))
        if canvas._geometry is not None and isinstance(
            canvas._geometry.transform, MediaTransform
        ):
            content = canvas._geometry.transform.content_rect
            return QPoint(
                round(content.x + content.width / 2),
                round(content.y + content.height / 2),
            )
        return canvas.rect().center()
    position = event.pos()
    return position if canvas.rect().contains(position) else canvas.rect().center()


def selected_context_region(canvas: ExclusionCanvas) -> CropSpec | None:
    index = canvas._selected_exclusion
    if (
        canvas._presentation is None
        or index is None
        or index >= len(canvas._presentation.exclusions)
    ):
        return None
    return canvas._presentation.exclusions[index]


def exclusion_at(canvas: ExclusionCanvas, position: QPoint) -> int | None:
    if canvas._presentation is None:
        return None
    oriented = canvas._oriented_position(position)
    for index in range(len(canvas._presentation.exclusions) - 1, -1, -1):
        if contains(canvas._presentation.exclusions[index], oriented):
            return index
    return None


def remove_exclusion(canvas: ExclusionCanvas, target: CropSpec) -> None:
    if canvas._presentation is None or not canvas._editable:
        return
    exclusions = canvas._presentation.exclusions
    try:
        index = exclusions.index(target)
    except ValueError:
        return
    updated = exclusions[:index] + exclusions[index + 1 :]
    if canvas._selected_exclusion == index:
        canvas._clear_exclusion_selection()
    elif (
        canvas._selected_exclusion is not None
        and canvas._selected_exclusion > index
    ):
        canvas._selected_exclusion -= 1
    canvas._clear_gesture()
    canvas._rebuild_geometry()
    canvas._announce_crop()
    canvas.update()
    canvas.command_requested.emit(ExclusionsChanged(updated))


def remove_all_exclusions(canvas: ExclusionCanvas) -> None:
    if (
        canvas._presentation is None
        or not canvas._editable
        or not canvas._presentation.exclusions
    ):
        return
    canvas._clear_exclusion_selection()
    canvas._clear_gesture()
    canvas._rebuild_geometry()
    canvas._announce_crop()
    canvas.update()
    canvas.command_requested.emit(ExclusionsChanged(()))


def toggle_exclusion_edit(canvas: ExclusionCanvas, enabled: bool) -> None:
    if canvas._presentation is not None and canvas._editable:
        canvas.set_exclusion_edit(enabled)


def handle_context_menu_key(canvas: ExclusionCanvas, event: QKeyEvent) -> bool:
    if not (
        event.key() == int(Qt.Key.Key_Menu)
        or (
            event.key() == int(Qt.Key.Key_F10)
            and bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        )
    ):
        return False
    if canvas._presentation is None or not canvas._editable:
        return False
    position = canvas.rect().center()
    context_event = QContextMenuEvent(
        QContextMenuEvent.Reason.Keyboard,
        position,
        canvas.mapToGlobal(position),
    )
    canvas.contextMenuEvent(context_event)
    event.accept()
    return True


def contains(region: CropSpec, point: PointF) -> bool:
    return (
        region.x <= point.x < region.x + region.width
        and region.y <= point.y < region.y + region.height
    )
