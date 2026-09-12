"""Region-mode extension for the source crop canvas."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QCoreApplication, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QKeyEvent, QMouseEvent, QPainter, QPen
from PySide6.QtWidgets import QWidget

from matteloop.core.crop import (
    crop_from_drag,
    nudge_crop,
    oriented_point_from_widget,
)
from matteloop.core.geometry import InteractionGeometry, MediaTransform, PointF
from matteloop.core.parameters import ExclusionsChanged
from matteloop.core.specs import CropSpec
from matteloop.ui.crop_canvas import (
    _HANDLE_NAMES,
    CropCanvas,
    _key_delta,
)
from matteloop.ui.crop_presentation import CropPresentation
from matteloop.ui.preview_canvas import PreviewCanvas
from matteloop.ui.theme import ACCENT_COLOR, CANVAS_COLOR


class ExclusionCanvas(CropCanvas):
    """Add source-region editing while retaining CropCanvas's crop hooks."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        title: str = "Original",
        object_name: str = "original_canvas",
        runtime_root: Path | None = None,
    ) -> None:
        super().__init__(
            parent,
            title=title,
            object_name=object_name,
            runtime_root=runtime_root,
        )
        self._exclusion_edit = False
        self._selected_exclusion: int | None = None
        self._selected_exclusion_value: CropSpec | None = None
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

    def apply_presentation(
        self,
        presentation: CropPresentation | None,
        *,
        active: bool,
        editable: bool,
    ) -> None:
        previous = self._presentation
        if presentation is None:
            self._clear_exclusion_selection()
            self._clear_gesture()
        else:
            self._reconcile_exclusion_state(previous, presentation)
        super().apply_presentation(
            presentation, active=active, editable=editable
        )

    def paintEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        PreviewCanvas.paintEvent(self, event)
        if self._geometry is None or self._presentation is None:
            return
        if not (
            self._active
            or self._exclusion_edit
            or self._presentation.exclusions
        ):
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
        if self._active:
            self._paint_crop_mask(painter, content, crop, overlay)
        self._paint_exclusions(painter, geometry)
        if self._active:
            self._paint_crop_outline(painter, crop)
        if not self._exclusion_edit or self._selected_exclusion is not None:
            self._paint_handles(painter, geometry)
        if not self._exclusion_edit or self._selected_exclusion is not None:
            self._paint_focus_ring(painter, geometry)
        painter.end()

    def _paint_crop_outline(self, painter: QPainter, crop: QRectF) -> None:
        color = QColor(ACCENT_COLOR)
        if self._exclusion_edit:
            color.setAlpha(135)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(color, 1 if self._exclusion_edit else 2))
        painter.drawRect(crop)

    def _paint_exclusions(
        self, painter: QPainter, geometry: InteractionGeometry
    ) -> None:
        fill = QColor("#E5484D")
        fill.setAlpha(90)
        painter.setPen(QPen(QColor("#E5484D"), 1))
        painter.setBrush(fill)
        for exclusion in self._painted_exclusions():
            painter.drawRect(self._widget_rect(geometry, exclusion))

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if not self._exclusion_edit:
            super().mousePressEvent(event)
            return
        if (
            not self._editable
            or (not self._active and not self._exclusion_edit)
            or event.button() != Qt.MouseButton.LeftButton
        ):
            super().mousePressEvent(event)
            return
        geometry = self._geometry
        if geometry is None:
            super().mousePressEvent(event)
            return
        self._press_exclusion(
            PointF(event.position().x(), event.position().y())
        )
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if not self._exclusion_edit:
            super().mouseMoveEvent(event)
            return
        if (
            self._drag_exclusion is not None
            and self._dragged is not None
            and self._editable
            and event.buttons() & Qt.MouseButton.LeftButton
        ):
            self._emit_exclusion_drag(event.position())
            event.accept()
            return
        if (
            self._rubber_start is not None
            and event.buttons() & Qt.MouseButton.LeftButton
        ):
            self._rubber_current = self._oriented_position(event.position())
            self.update()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if not self._exclusion_edit:
            super().mouseReleaseEvent(event)
            return
        if event.button() == Qt.MouseButton.LeftButton:
            if self._drag_exclusion is not None:
                self._finish_exclusion_drag()
                event.accept()
                return
            if self._rubber_start is not None:
                self._finish_rubber_band()
                event.accept()
                return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if not self._exclusion_edit:
            super().keyPressEvent(event)
            return
        if not self._editable or not self.hasFocus():
            super().keyPressEvent(event)
            return
        if self._handle_exclusion_key(event):
            return
        super().keyPressEvent(event)

    def _handle_exclusion_key(self, event: QKeyEvent) -> bool:
        if event.key() in {int(Qt.Key.Key_Delete), int(Qt.Key.Key_Backspace)}:
            if self._selected_exclusion is None:
                return False
            self._delete_selected_exclusion(event)
            return True
        if event.key() == int(Qt.Key.Key_Escape):
            self._clear_exclusion_selection()
            self._clear_gesture()
            self._rebuild_geometry()
            self._announce_crop()
            self.update()
            event.accept()
            return True
        if event.key() in {int(Qt.Key.Key_Tab), int(Qt.Key.Key_Backtab)}:
            if self._cycle_exclusion_selection(
                backwards=(
                    event.key() == int(Qt.Key.Key_Backtab)
                    or bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
                )
            ):
                event.accept()
                return True
            return False
        if event.key() in {
            int(Qt.Key.Key_Insert),
            int(Qt.Key.Key_N),
            int(Qt.Key.Key_Space),
        }:
            self._create_exclusion()
            event.accept()
            return True
        if self._selected_exclusion is None:
            return False
        delta = _key_delta(event)
        if delta is None or self._presentation is None:
            return False
        step = 10 if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else 1
        edited = self._edited_rect()
        try:
            region = nudge_crop(
                edited,
                self._focused_target,
                dx=delta[0] * step,
                dy=delta[1] * step,
                source_width=self._presentation.width,
                source_height=self._presentation.height,
            )
        except ValueError:
            return False
        region = self._constrain(region, self._focused_target)
        if region != edited:
            self._set_exclusion_selection(
                self._selected_exclusion, value=region
            )
            self.command_requested.emit(self._crop_event(region))
            self._announce_crop()
        event.accept()
        return True

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
                self._set_exclusion_selection(index)
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
            self._set_exclusion_selection(index, value=region)
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
            self._set_exclusion_selection(
                len(self._presentation.exclusions), value=region
            )
            self.command_requested.emit(self._exclusion_event(region))
        self._rebuild_geometry()
        self._announce_crop()
        self.update()

    def _exclusion_event(self, region: CropSpec) -> ExclusionsChanged:
        if self._presentation is None or self._selected_exclusion is None:
            return ExclusionsChanged((region,))
        exclusions = list(self._presentation.exclusions)
        if self._selected_exclusion == len(exclusions):
            exclusions.append(region)
        elif self._selected_exclusion > len(exclusions):
            return ExclusionsChanged(tuple(exclusions))
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
        self._selected_exclusion_value = None

    def _set_exclusion_selection(
        self, index: int, *, value: CropSpec | None = None
    ) -> None:
        self._selected_exclusion = index
        if value is not None:
            self._selected_exclusion_value = value
        elif self._presentation is not None and index < len(
            self._presentation.exclusions
        ):
            self._selected_exclusion_value = self._presentation.exclusions[index]
        else:
            self._selected_exclusion_value = None

    def _cycle_exclusion_selection(self, *, backwards: bool) -> bool:
        if self._presentation is None or not self._presentation.exclusions:
            return False
        self._clear_gesture()
        count = len(self._presentation.exclusions)
        current = self._selected_exclusion
        if current is None or current >= count:
            index = count - 1 if backwards else 0
        else:
            index = (current - 1 if backwards else current + 1) % count
        self._set_exclusion_selection(index)
        self._focused_target = "crop"
        self._rebuild_geometry()
        self._announce_crop()
        self.update()
        return True

    def _create_exclusion(self) -> None:
        if self._presentation is None:
            return
        width = min(8, self._presentation.width)
        height = min(8, self._presentation.height)
        region = CropSpec(
            (self._presentation.width - width) // 2,
            (self._presentation.height - height) // 2,
            width,
            height,
        )
        self._set_exclusion_selection(
            len(self._presentation.exclusions), value=region
        )
        self.command_requested.emit(self._exclusion_event(region))

    def _reconcile_exclusion_state(
        self,
        previous: CropPresentation | None,
        presentation: CropPresentation,
    ) -> None:
        source_changed = previous is None or (
            previous.source_id != presentation.source_id
            or previous.width != presentation.width
            or previous.height != presentation.height
            or previous.rotation != presentation.rotation
            or previous.pixel_aspect != presentation.pixel_aspect
        )
        if self._drag_exclusion is not None:
            drag_index = self._drag_exclusion
            if (
                source_changed
                or self._drag_origin_region is None
                or drag_index >= len(presentation.exclusions)
                or presentation.exclusions[drag_index] != self._drag_origin_region
            ):
                self._clear_gesture()
        elif self._rubber_start is not None and (
            source_changed
            or previous is None
            or previous.exclusions != presentation.exclusions
        ):
            self._clear_gesture()
        index = self._selected_exclusion
        if index is None:
            return
        value = self._selected_exclusion_value
        if value is None and previous is not None and index < len(
            previous.exclusions
        ):
            value = previous.exclusions[index]
        if value is None:
            self._clear_exclusion_selection()
            return
        if (
            index < len(presentation.exclusions)
            and presentation.exclusions[index] == value
        ):
            self._selected_exclusion_value = value
            return
        try:
            self._selected_exclusion = presentation.exclusions.index(value)
        except ValueError:
            self._clear_exclusion_selection()

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
            return super()._edited_rect()
        index = self._selected_exclusion
        if index is None or index >= len(self._presentation.exclusions):
            return self._presentation.crop
        return self._drag_region or self._presentation.exclusions[index]

    def _crop_event(self, crop: CropSpec) -> object:
        if self._exclusion_edit:
            return self._exclusion_event(crop)
        return super()._crop_event(crop)

    def _announce_crop(self) -> None:
        presentation = self._presentation
        if presentation is None:
            return
        index = self._selected_exclusion
        if self._exclusion_edit and (
            index is None or index >= len(presentation.exclusions)
        ):
            self.setAccessibleDescription(
                QCoreApplication.translate(
                    "CropCanvas",
                    "Exclusion region mode: %1 region(s); no region selected",
                ).replace("%1", str(len(presentation.exclusions)))
            )
            return
        if not self._exclusion_edit:
            super()._announce_crop()
            return
        assert index is not None
        crop = self._edited_rect()
        text = QCoreApplication.translate(
            "CropCanvas",
            "Exclusion region %1 of %2: x %3, y %4, width %5, height %6 "
            "source pixels",
        )
        values = (
            index + 1,
            len(presentation.exclusions),
            crop.x,
            crop.y,
            crop.width,
            crop.height,
        )
        for number, value in enumerate(values, start=1):
            text = text.replace(f"%{number}", str(value))
        self.setAccessibleDescription(text)


def _contains(region: CropSpec, point: PointF) -> bool:
    return (
        region.x <= point.x < region.x + region.width
        and region.y <= point.y < region.y + region.height
    )
