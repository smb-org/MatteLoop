"""Custom-painted visual crop editor driven by one immutable geometry snapshot."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QCoreApplication, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QKeyEvent, QMouseEvent, QPainter, QPen
from PySide6.QtWidgets import QWidget

from matteloop.core.crop import (
    crop_from_drag,
    nudge_crop,
    oriented_point_from_widget,
    oriented_rect_to_source_rect,
)
from matteloop.core.crop_state import CropChanged
from matteloop.core.geometry import (
    _MEDIA_INSET,
    CropGeometryState,
    InteractionGeometry,
    MediaTransform,
    PointF,
    RectF,
    SizeF,
    build_crop_geometry,
)
from matteloop.core.parameters import ExclusionsChanged
from matteloop.core.specs import CropSpec
from matteloop.ui.crop_presentation import CropPresentation
from matteloop.ui.preview_canvas import PreviewCanvas
from matteloop.ui.theme import ACCENT_COLOR, CANVAS_COLOR, TEXT_COLOR

_HANDLE_NAMES = (
    "north_west",
    "north",
    "north_east",
    "east",
    "south_east",
    "south",
    "south_west",
    "west",
)


class CropCanvas(PreviewCanvas):
    """Display the source frame and edit its crop in oriented source pixels."""

    command_requested = Signal(object)

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        title: str = "Original",
        object_name: str = "original_canvas",
        runtime_root: Path | None = None,
    ) -> None:
        super().__init__(title, object_name, parent, runtime_root=runtime_root)
        self.layout().setContentsMargins(0, 0, 0, 0)  # type: ignore[union-attr]
        self.status_label.hide()
        self.setMouseTracking(True)
        self._presentation: CropPresentation | None = None
        self._geometry: InteractionGeometry | None = None
        self._active = False
        self._editable = False
        self._focused_target = "crop"
        self._dragged: str | None = None
        self._drag_start = PointF(0, 0)
        self._drag_crop: CropPresentation | None = None
        self._exclusion_edit = False
        self._selected_exclusion: int | None = None
        self._drag_exclusion: int | None = None
        self._drag_origin_region: CropSpec | None = None
        self._drag_region: CropSpec | None = None
        self._rubber_start: PointF | None = None
        self._rubber_current: PointF | None = None

    def set_exclusion_edit(self, enabled: bool) -> None:
        """Toggle the mutually exclusive exclusion-region editing mode."""
        if self._exclusion_edit == enabled:
            return
        self._exclusion_edit = enabled
        self._clear_exclusion_selection()
        self._clear_gesture()
        self._focused_target = "crop"
        self._rebuild_geometry()
        self._announce_crop()
        self.update()

    def set_presentation(
        self,
        presentation: CropPresentation | None,
        active: bool = True,
        editable: bool = True,
    ) -> None:
        """Render a presenter snapshot and apply the reducer-derived editability."""
        self.apply_presentation(presentation, active=active, editable=editable)

    def apply_presentation(
        self,
        presentation: CropPresentation | None,
        *,
        active: bool,
        editable: bool,
    ) -> None:
        self._presentation = presentation
        self._active = active
        self._editable = editable
        if presentation is None:
            self._clear_exclusion_selection()
            self._clear_gesture()
            self._geometry = None
            self.setAccessibleDescription(
                QCoreApplication.translate("CropCanvas", "Original video frame")
            )
        else:
            if self._selected_exclusion is not None and (
                self._selected_exclusion >= len(presentation.exclusions)
            ):
                self._clear_exclusion_selection()
            self._rebuild_geometry()
            self._announce_crop()
        self.update()

    def paintEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        super().paintEvent(event)
        if not self._active or self._geometry is None or self._presentation is None:
            return
        geometry = self._geometry
        transform = geometry.transform
        if not isinstance(transform, MediaTransform):
            return
        content = transform.content_rect
        crop = self._widget_rect(geometry, self._presentation.crop)
        overlay = QColor(CANVAS_COLOR)
        overlay.setAlpha(105)
        painter = QPainter(self)
        self._paint_crop_mask(painter, content, crop, overlay)
        self._paint_exclusions(painter, geometry)
        self._paint_crop_outline(painter, crop)
        if not self._exclusion_edit or self._selected_exclusion is not None:
            self._paint_handles(painter, geometry)
        self._paint_focus_ring(painter, geometry)
        painter.end()

    @staticmethod
    def _paint_crop_mask(
        painter: QPainter, content: RectF, crop: QRectF, overlay: QColor
    ) -> None:
        painter.fillRect(
            QRectF(content.x, content.y, content.width, crop.top() - content.y),
            overlay,
        )
        painter.fillRect(
            QRectF(
                content.x,
                crop.bottom(),
                content.width,
                content.bottom - crop.bottom(),
            ),
            overlay,
        )
        painter.fillRect(
            QRectF(content.x, crop.top(), crop.left() - content.x, crop.height()),
            overlay,
        )
        painter.fillRect(
            QRectF(
                crop.right(),
                crop.top(),
                content.right - crop.right(),
                crop.height(),
            ),
            overlay,
        )

    def _paint_exclusions(
        self, painter: QPainter, geometry: InteractionGeometry
    ) -> None:
        fill = QColor("#E5484D")
        fill.setAlpha(90)
        painter.setPen(QPen(QColor("#E5484D"), 1))
        painter.setBrush(fill)
        for exclusion in self._painted_exclusions():
            painter.drawRect(self._widget_rect(geometry, exclusion))

    def _paint_crop_outline(self, painter: QPainter, crop: QRectF) -> None:
        color = QColor(ACCENT_COLOR)
        if self._exclusion_edit:
            color.setAlpha(135)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(color, 2 if not self._exclusion_edit else 1))
        painter.drawRect(crop)

    @staticmethod
    def _paint_handles(
        painter: QPainter, geometry: InteractionGeometry
    ) -> None:
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(ACCENT_COLOR))
        for name in _HANDLE_NAMES:
            painter.drawRect(_qt_rect(geometry.visual[name]))

    def _paint_focus_ring(
        self, painter: QPainter, geometry: InteractionGeometry
    ) -> None:
        if not self.hasFocus():
            return
        focused = geometry.focus.get(self._focused_target)
        if focused is None or (
            self._exclusion_edit and self._selected_exclusion is None
        ):
            return
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(TEXT_COLOR), 1, Qt.PenStyle.DashLine))
        painter.drawRect(_qt_rect(focused).adjusted(-3, -3, 3, 3))

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if (
            not self._editable
            or not self._active
            or event.button() != Qt.MouseButton.LeftButton
        ):
            super().mousePressEvent(event)
            return
        geometry = self._geometry
        if geometry is None:
            super().mousePressEvent(event)
            return
        position = PointF(event.position().x(), event.position().y())
        if self._exclusion_edit:
            self._press_exclusion(position)
            event.accept()
            return
        target = geometry.hit_test(position)
        if target not in {"crop", *_HANDLE_NAMES} or self._presentation is None:
            super().mousePressEvent(event)
            return
        self._focused_target = target
        self._dragged = target
        self._drag_crop = self._presentation
        self._drag_start = oriented_point_from_widget(geometry, position)
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        self._rebuild_geometry(focused=target, dragged=target)
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if (
            self._dragged is not None
            and self._editable
            and self._active
            and event.buttons() & Qt.MouseButton.LeftButton
        ):
            if self._exclusion_edit:
                self._emit_exclusion_drag(event.position())
            else:
                self._emit_drag(event.position())
            event.accept()
            return
        if (
            self._exclusion_edit
            and self._rubber_start is not None
            and event.buttons() & Qt.MouseButton.LeftButton
        ):
            self._rubber_current = self._oriented_position(event.position())
            self.update()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._dragged is not None:
            if self._exclusion_edit:
                self._finish_exclusion_drag()
                event.accept()
                return
            self._dragged = None
            self._drag_crop = None
            self._rebuild_geometry()
            self._announce_crop()
            event.accept()
            return
        if (
            event.button() == Qt.MouseButton.LeftButton
            and self._exclusion_edit
            and self._rubber_start is not None
        ):
            self._finish_rubber_band()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if not self._editable or not self._active or not self.hasFocus():
            super().keyPressEvent(event)
            return
        if self._exclusion_edit:
            if event.key() in {int(Qt.Key.Key_Delete), int(Qt.Key.Key_Backspace)}:
                self._delete_selected_exclusion(event)
                return
            if event.key() == int(Qt.Key.Key_Escape):
                self._clear_exclusion_selection()
                self._clear_gesture()
                self._rebuild_geometry()
                self._announce_crop()
                self.update()
                event.accept()
                return
            if self._selected_exclusion is None:
                super().keyPressEvent(event)
                return
        delta = _key_delta(event)
        if delta is None or self._presentation is None:
            super().keyPressEvent(event)
            return
        step = 10 if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else 1
        edited = self._edited_rect()
        try:
            crop = nudge_crop(
                edited,
                self._focused_target,
                dx=delta[0] * step,
                dy=delta[1] * step,
                source_width=self._presentation.width,
                source_height=self._presentation.height,
            )
        except ValueError:
            super().keyPressEvent(event)
            return
        crop = self._constrain(crop, self._focused_target)
        if crop != self._presentation.crop:
            self.command_requested.emit(self._crop_event(crop))
            self._announce_crop()
        event.accept()

    def focusInEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        super().focusInEvent(event)
        self._rebuild_geometry(focused=self._focused_target)
        self.update()

    def focusOutEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        self._rebuild_geometry()
        super().focusOutEvent(event)
        self.update()

    def resizeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        super().resizeEvent(event)
        self._rebuild_geometry()

    def _emit_drag(self, position: QPointF) -> None:
        geometry = self._geometry
        presentation = self._drag_crop
        if geometry is None or presentation is None or self._dragged is None:
            return
        current = oriented_point_from_widget(
            geometry, PointF(position.x(), position.y())
        )
        crop = crop_from_drag(
            presentation.crop,
            self._dragged,
            self._drag_start,
            current,
            source_width=presentation.width,
            source_height=presentation.height,
        )
        crop = self._constrain(crop, self._dragged)
        if self._presentation is None or crop != self._presentation.crop:
            self.command_requested.emit(self._crop_event(crop))

    def _press_exclusion(self, position: PointF) -> None:
        if self._presentation is None or self._geometry is None:
            return
        selected = self._selected_exclusion
        if selected is not None:
            target = self._geometry.hit_test(position)
            if target in {"crop", *_HANDLE_NAMES}:
                self._begin_exclusion_drag(selected, target, position)
                return
        oriented = self._oriented_position(position)
        for index in range(len(self._presentation.exclusions) - 1, -1, -1):
            if index == selected:
                continue
            if _contains(self._presentation.exclusions[index], oriented):
                self._selected_exclusion = index
                self._focused_target = "crop"
                self._announce_crop()
                self._begin_exclusion_drag(index, "crop", position)
                return
        self._clear_gesture()
        self._rubber_start = self._clamp_oriented(oriented)
        self._rubber_current = self._rubber_start
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        self.update()

    def _begin_exclusion_drag(
        self, index: int, target: str, position: PointF
    ) -> None:
        if self._presentation is None or self._geometry is None:
            return
        self._drag_exclusion = index
        self._drag_origin_region = self._presentation.exclusions[index]
        self._drag_region = self._drag_origin_region
        self._dragged = target
        self._drag_start = self._oriented_position(position)
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        self._rebuild_geometry(focused=target, dragged=target)
        self.update()

    def _emit_exclusion_drag(self, position: QPointF) -> None:
        if (
            self._geometry is None
            or self._drag_origin_region is None
            or self._dragged is None
        ):
            return
        current = self._oriented_position(position)
        region = crop_from_drag(
            self._drag_origin_region,
            self._dragged,
            self._drag_start,
            current,
            source_width=self._presentation.width if self._presentation else 1,
            source_height=self._presentation.height if self._presentation else 1,
        )
        region = self._constrain(region, self._dragged)
        if region == self._drag_region:
            return
        self._drag_region = region
        self._rebuild_geometry(focused=self._dragged, dragged=self._dragged)
        self._announce_crop()
        self.update()

    def _finish_exclusion_drag(self) -> None:
        index = self._drag_exclusion
        region = self._drag_region
        original = (
            self._presentation.exclusions[index]
            if self._presentation is not None
            and index is not None
            and index < len(self._presentation.exclusions)
            else None
        )
        self._clear_gesture()
        if index is not None and region is not None:
            self._selected_exclusion = index
            if region != original:
                self.command_requested.emit(self._exclusion_event(region))
        self._rebuild_geometry()
        self._announce_crop()
        self.update()

    def _finish_rubber_band(self) -> None:
        region = self._rubber_region()
        self._clear_gesture()
        if (
            region is not None
            and region.width >= 8
            and region.height >= 8
            and self._presentation is not None
        ):
            self._selected_exclusion = len(self._presentation.exclusions)
            self.command_requested.emit(self._exclusion_event(region))
        self._rebuild_geometry()
        self._announce_crop()
        self.update()

    def _delete_selected_exclusion(self, event: QKeyEvent) -> None:
        index = self._selected_exclusion
        if self._presentation is None or index is None:
            super().keyPressEvent(event)
            return
        current = self._presentation.exclusions
        exclusions = current[:index] + current[index + 1 :]
        self._clear_exclusion_selection()
        self._clear_gesture()
        self.command_requested.emit(ExclusionsChanged(exclusions))
        self._rebuild_geometry()
        self._announce_crop()
        self.update()
        event.accept()

    def _exclusion_event(self, region: CropSpec) -> ExclusionsChanged:
        if self._presentation is None or self._selected_exclusion is None:
            return ExclusionsChanged((region,))
        exclusions = list(self._presentation.exclusions)
        if self._selected_exclusion == len(exclusions):
            exclusions.append(region)
        else:
            exclusions[self._selected_exclusion] = region
        return ExclusionsChanged(tuple(exclusions))

    def _oriented_position(self, position: QPointF | PointF) -> PointF:
        if self._geometry is None:
            return PointF(0, 0)
        point = (
            position
            if isinstance(position, PointF)
            else PointF(position.x(), position.y())
        )
        return oriented_point_from_widget(self._geometry, point)

    def _clamp_oriented(self, point: PointF) -> PointF:
        if self._presentation is None:
            return point
        return PointF(
            min(max(point.x, 0), self._presentation.width),
            min(max(point.y, 0), self._presentation.height),
        )

    def _rubber_region(self) -> CropSpec | None:
        if self._rubber_start is None or self._rubber_current is None:
            return None
        start = self._clamp_oriented(self._rubber_start)
        current = self._clamp_oriented(self._rubber_current)
        left, right = sorted((round(start.x), round(current.x)))
        top, bottom = sorted((round(start.y), round(current.y)))
        if right <= left or bottom <= top:
            return None
        return CropSpec(left, top, right - left, bottom - top)

    def _clear_gesture(self) -> None:
        self._dragged = None
        self._drag_crop = None
        self._drag_exclusion = None
        self._drag_origin_region = None
        self._drag_region = None
        self._rubber_start = None
        self._rubber_current = None

    def _clear_exclusion_selection(self) -> None:
        self._selected_exclusion = None

    def _painted_exclusions(self) -> tuple[CropSpec, ...]:
        if self._presentation is None:
            return ()
        exclusions = list(self._presentation.exclusions)
        if (
            self._drag_exclusion is not None
            and self._drag_region is not None
            and self._drag_exclusion < len(exclusions)
        ):
            exclusions[self._drag_exclusion] = self._drag_region
        rubber = self._rubber_region()
        if rubber is not None:
            exclusions.append(rubber)
        return tuple(exclusions)

    def _edited_rect(self) -> CropSpec:
        if self._presentation is None or not self._exclusion_edit:
            return (
                self._presentation.crop
                if self._presentation is not None
                else CropSpec(0, 0, 1, 1)
            )
        index = self._selected_exclusion
        if index is None or index >= len(self._presentation.exclusions):
            return self._presentation.crop
        return self._drag_region or self._presentation.exclusions[index]

    def _widget_rect(
        self, geometry: InteractionGeometry, rect: CropSpec
    ) -> QRectF:
        presentation = self._presentation
        if presentation is None or not isinstance(geometry.transform, MediaTransform):
            return QRectF()
        raw = oriented_rect_to_source_rect(
            rect,
            source_width=presentation.coded_width,
            source_height=presentation.coded_height,
            rotation=presentation.rotation,
            pixel_aspect=presentation.pixel_aspect,
        )
        return _qt_rect(geometry.transform.source_rect_to_widget(raw))

    def _constrain(self, crop: CropSpec, target: str) -> CropSpec:
        """Re-fit a candidate crop before it is compared/emitted.

        Identity by default; ``ResultPlayerCanvas`` overrides this to apply
        an aspect lock.
        """
        return crop

    def _crop_event(self, crop: CropSpec) -> object:
        """Build the command to dispatch for a changed crop.

        Default: ``CropChanged`` (the source crop). ``ResultPlayerCanvas``
        overrides this to dispatch ``TransformChanged`` instead -- routing
        both the drag site and ``keyPressEvent`` through this hook is what
        keeps an arrow-key nudge on the result canvas from dispatching a
        *source* crop change (edge case E30).
        """
        if self._exclusion_edit:
            return self._exclusion_event(crop)
        return CropChanged(crop)

    def _rebuild_geometry(
        self, *, focused: str | None = None, dragged: str | None = None
    ) -> None:
        presentation = self._presentation
        if presentation is None:
            self._geometry = None
            return
        try:
            raw_crop = oriented_rect_to_source_rect(
                self._edited_rect(),
                source_width=presentation.coded_width,
                source_height=presentation.coded_height,
                rotation=presentation.rotation,
                pixel_aspect=presentation.pixel_aspect,
            )
            reserved_bottom = max(0.0, self._reserved_bottom_space())
            self._geometry = build_crop_geometry(
                state=CropGeometryState(
                    source_size=SizeF(
                        presentation.coded_width, presentation.coded_height
                    ),
                    crop=raw_crop,
                    rotation=presentation.rotation,
                    pixel_aspect=presentation.pixel_aspect,
                    inset=_MEDIA_INSET,
                    focused=focused
                    if focused is not None
                    else (self._focused_target if self.hasFocus() else None),
                    dragged=dragged if dragged is not None else self._dragged,
                ),
                viewport=SizeF(
                    max(1, self.width()),
                    max(1.0, self.height() - reserved_bottom),
                ),
                dpr=float(self.devicePixelRatioF()),
            )
            transform = self._geometry.transform
            layout = self.layout()
            assert isinstance(transform, MediaTransform)
            assert layout is not None
            inset = int(transform.inset)
            if layout.contentsMargins().left() != inset:
                layout.setContentsMargins(inset, inset, inset, inset)
                self._update_pixmap()
        except (TypeError, ValueError):
            self._geometry = None

    def _announce_crop(self) -> None:
        presentation = self._presentation
        if presentation is None:
            return
        crop = self._edited_rect()
        if self._exclusion_edit and self._selected_exclusion is not None:
            text = QCoreApplication.translate(
                "CropCanvas",
                "Exclusion region %1 of %2: x %3, y %4, width %5, height %6 "
                "source pixels",
            )
            values = (
                self._selected_exclusion + 1,
                len(presentation.exclusions),
                crop.x,
                crop.y,
                crop.width,
                crop.height,
            )
            for number, value in enumerate(values, start=1):
                text = text.replace(f"%{number}", str(value))
            self.setAccessibleDescription(text)
            return
        self.setAccessibleDescription(
            QCoreApplication.translate(
                "CropCanvas",
                "Crop bounds: x %s, y %s, width %s, height %s source pixels; "
                "source %s × %s",
            )
            % (
                crop.x,
                crop.y,
                crop.width,
                crop.height,
                presentation.width,
                presentation.height,
            )
        )


def _key_delta(event: QKeyEvent) -> tuple[int, int] | None:
    return {
        int(Qt.Key.Key_Left): (-1, 0),
        int(Qt.Key.Key_Right): (1, 0),
        int(Qt.Key.Key_Up): (0, -1),
        int(Qt.Key.Key_Down): (0, 1),
    }.get(event.key())


def _qt_rect(rect: RectF) -> QRectF:
    return QRectF(rect.x, rect.y, rect.width, rect.height)


def _contains(region: CropSpec, point: PointF) -> bool:
    return (
        region.x <= point.x < region.x + region.width
        and region.y <= point.y < region.y + region.height
    )
