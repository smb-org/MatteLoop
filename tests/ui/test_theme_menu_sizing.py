"""Regression test for issue #170.

Right-clicking the Render button opened the "Copy render settings" context
menu with its shortcut text ("Ctrl+Shift+C") rendered overlapping the label.
Qt's `windows11` style (the Windows 11 default since Qt 6.7) under-sizes
`CT_MenuItem` for this label/shortcut/font combination when no stylesheet
rule targets `QMenu::item` -- the reserved item width comes out narrower
than the text it must hold, so the right-aligned shortcut is drawn over the
tail of the left-aligned label. Styling `QMenu::item` explicitly (theme.py)
hands the item's box model to Qt's stylesheet-aware style, which measures it
correctly.

A plain `sizeHint().width() >= label_width + shortcut_width` check does not
catch this: measured on this repository, the *buggy* sizeHint (220px) is
already wider than the raw label+shortcut text sum (193px) -- the deficit is
in an internal reservation (icon/checkbox column) that a text-sum comparison
never sees, even though the rendered text visibly overlaps. So this test
renders the real menu offscreen and looks for the gap between the label's
ink and the shortcut's ink directly, which is what a person looking at the
menu actually judges. Measured gaps on this repository: 4px without the fix
(letter-spacing noise only, no separating gap) vs. 12px with it.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtGui import QFont, QImage
from PySide6.QtWidgets import QApplication, QStyleFactory

from matteloop.ui.controller import SourceController
from matteloop.ui.main_window import MainWindow
from matteloop.ui.store import ReducerStore
from matteloop.ui.theme import install_theme

# Between the measured 4px (defect present) and 12px (fixed) gap -- see the
# module docstring for how those numbers were obtained.
_MIN_LABEL_SHORTCUT_GAP_PX = 8
# The menu's 1px frame border and any anti-aliasing bleed sit in these
# outermost columns; excluding them keeps the border out of the ink scan.
_BORDER_MARGIN_PX = 4


def _settings(name: str) -> QSettings:
    settings = QSettings(
        QSettings.IniFormat, QSettings.UserScope, "matteloop-test", name
    )
    settings.clear()
    return settings


def _window(qtbot, name: str) -> MainWindow:
    store = ReducerStore()
    controller = SourceController(store)
    window = MainWindow(store, controller, _settings(name))
    qtbot.addWidget(window)
    window.show()
    return window


@contextmanager
def _themed_app_on_windows11() -> Iterator[QApplication]:
    """Apply the real production theme under the style that exposes the
    defect, then put the shared session `QApplication` back the way this
    test found it -- other tests share the same instance."""
    app = QApplication.instance()
    assert app is not None
    if "windows11" not in {name.lower() for name in QStyleFactory.keys()}:
        pytest.skip("the windows11 style, which exposes the defect, is not installed")
    original_style_name = app.style().objectName()
    original_stylesheet = app.styleSheet()
    original_font = QFont(app.font())
    app.setStyle("windows11")
    install_theme(app)
    try:
        yield app
    finally:
        app.setStyleSheet(original_stylesheet)
        app.setFont(original_font)
        if original_style_name:
            app.setStyle(original_style_name)


def _max_gap_between_ink_clusters(image: QImage) -> int:
    """Return the widest run of background-only columns between two runs of
    non-background ("ink") columns, scanned across the item's vertical
    centre. A correctly laid out "label   shortcut" item has one wide gap
    between the two words; an overlapping one only has narrow inter-letter
    gaps."""
    width, height = image.width(), image.height()
    background = image.pixelColor(2, 2)

    def is_ink(x: int, y: int) -> bool:
        pixel = image.pixelColor(x, y)
        diff = (
            abs(pixel.red() - background.red())
            + abs(pixel.green() - background.green())
            + abs(pixel.blue() - background.blue())
        )
        return diff > 30

    mid = height // 2
    band = range(max(0, mid - 6), min(height, mid + 6))
    scan_end = max(_BORDER_MARGIN_PX, width - _BORDER_MARGIN_PX)
    scan_range = range(_BORDER_MARGIN_PX, scan_end)
    ink_columns = [any(is_ink(x, y) for y in band) for x in scan_range]

    clusters: list[tuple[int, int]] = []
    cluster_start: int | None = None
    for offset, ink in enumerate(ink_columns):
        if ink and cluster_start is None:
            cluster_start = offset
        elif not ink and cluster_start is not None:
            clusters.append((cluster_start, offset - 1))
            cluster_start = None
    if cluster_start is not None:
        clusters.append((cluster_start, len(ink_columns) - 1))

    if len(clusters) < 2:
        return 0
    return max(b[0] - a[1] for a, b in zip(clusters, clusters[1:]))


def test_render_context_menu_does_not_overlap_label_and_shortcut(qtbot) -> None:
    """A visible gap must separate the label from its shortcut -- see the
    module docstring for why this is checked by rendering rather than by
    comparing `sizeHint()` against a text-width sum."""
    with _themed_app_on_windows11():
        window = _window(qtbot, "theme-menu-sizing")
        menu = window.action_shelf._render_context_menu()
        try:
            menu.resize(menu.sizeHint())
            image = menu.grab().toImage()

            gap = _max_gap_between_ink_clusters(image)

            assert gap >= _MIN_LABEL_SHORTCUT_GAP_PX, (
                f"widest gap between ink clusters in the render context menu "
                f"is {gap}px (need >= {_MIN_LABEL_SHORTCUT_GAP_PX}px) -- the "
                "shortcut text is overlapping the label instead of sitting "
                "in its own column"
            )
        finally:
            menu.close()
