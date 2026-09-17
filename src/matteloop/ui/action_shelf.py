"""Fixed editor actions, deliberately outside the inspector scroll area."""

from __future__ import annotations

from PySide6.QtCore import (
    QCoreApplication,
    QEvent,
    QObject,
    QRectF,
    QSettings,
    QSize,
    Qt,
)
from PySide6.QtGui import (
    QAction,
    QColor,
    QContextMenuEvent,
    QIcon,
    QKeySequence,
    QPainter,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QMenu,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from matteloop.core.execution_providers import ProviderOption
from matteloop.ui.ports import StateStore, WindowServices
from matteloop.ui.settings_dialog import SettingsDialog
from matteloop.ui.theme import ACCENT_COLOR, TEXT_COLOR


def _gear_icon() -> QIcon:
    """Paint the preferences glyph without a shipped image asset."""
    pixmap = QPixmap(20, 20)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(TEXT_COLOR))
    painter.translate(10.0, 10.0)
    for tooth in range(8):
        painter.save()
        painter.rotate(tooth * 45.0)
        painter.drawRect(QRectF(-1.35, -8.8, 2.7, 3.4))
        painter.restore()
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.setPen(QPen(QColor(TEXT_COLOR), 1.6))
    painter.drawEllipse(QRectF(-5.8, -5.8, 11.6, 11.6))
    painter.end()
    return QIcon(pixmap)


def _update_icon() -> QIcon:
    """Paint the update affordance without a glyph or shipped image asset."""
    pixmap = QPixmap(20, 20)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(
        QPen(
            QColor(ACCENT_COLOR),
            2.0,
            Qt.PenStyle.SolidLine,
            Qt.PenCapStyle.RoundCap,
            Qt.PenJoinStyle.RoundJoin,
        )
    )
    painter.drawLine(10, 15, 10, 5)
    painter.drawLine(10, 5, 5, 10)
    painter.drawLine(10, 5, 15, 10)
    painter.end()
    return QIcon(pixmap)


def _update_button() -> QPushButton:
    """Build the hidden update affordance beside Preferences."""
    button = QPushButton()
    button.setObjectName("update_action")
    button.setAccessibleName(
        QCoreApplication.translate("UpdateBanner", "Update notice")
    )
    button.setToolTip(
        QCoreApplication.translate("UpdateBanner", "Update notice")
    )
    button.setIcon(_update_icon())
    button.setIconSize(QSize(20, 20))
    button.setFixedWidth(48)
    button.setMinimumHeight(40)
    button.hide()
    return button


def _preferences_button() -> QPushButton:
    """Build the gear button that opens application Preferences."""
    button = QPushButton()
    button.setObjectName("preferences_action")
    button.setAccessibleName(
        QCoreApplication.translate("ActionShelf", "Preferences")
    )
    button.setToolTip(QCoreApplication.translate("ActionShelf", "Preferences"))
    button.setIcon(_gear_icon())
    button.setIconSize(QSize(20, 20))
    button.setFixedWidth(48)
    button.setMinimumHeight(40)
    button.setShortcut(QKeySequence(QKeySequence.StandardKey.Preferences))
    return button


class ActionShelf(QFrame):
    def __init__(
        self,
        store: StateStore,
        services: WindowServices,
        parent: QWidget | None = None,
        *,
        settings: QSettings | None = None,
        provider_options: tuple[ProviderOption, ...] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("action_shelf")
        self.setFixedHeight(104)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        row = QHBoxLayout()
        self.preview_button = QPushButton(
            QCoreApplication.translate("ActionShelf", "Preview Frame")
        )
        self.preview_button.setObjectName("preview_action")
        self.preview_button.setAccessibleName(
            QCoreApplication.translate("ActionShelf", "Preview Frame")
        )
        self.render_button = QPushButton(
            QCoreApplication.translate("ActionShelf", "Render Video")
        )
        self.render_button.setObjectName("render_action")
        self.render_button.setAccessibleName(
            QCoreApplication.translate("ActionShelf", "Render Video")
        )
        self._render_menu: QMenu | None = None
        self.copy_render_settings_action = self._build_copy_render_settings_action()
        self.preferences_button = _preferences_button()
        self.update_button = _update_button()
        for button in (self.preview_button, self.render_button):
            button.setMinimumHeight(40)
            button.setSizePolicy(
                button.sizePolicy().Policy.Expanding,
                button.sizePolicy().Policy.Fixed,
            )
            row.addWidget(button, 1)
        row.addWidget(self.preferences_button)
        row.addWidget(self.update_button)
        layout.addLayout(row)
        self.setTabOrder(self.preview_button, self.render_button)
        self.setTabOrder(self.render_button, self.preferences_button)
        self.setTabOrder(self.preferences_button, self.update_button)
        self.preferences_dialog = self._build_preferences_dialog(
            store, services, settings, provider_options
        )
        self.preferences_button.clicked.connect(self.open_preferences)

    def _build_copy_render_settings_action(self) -> QAction:
        """Build the export action, reachable while Render is disabled.

        Qt drops both a disabled widget's own shortcuts and the `ContextMenu`
        events it would otherwise turn into `customContextMenuRequested`
        (verified offscreen, with the Render button enabled and disabled) --
        so a `QAction` or a context-menu route that lives *on* the Render
        button goes unreachable exactly when this command is most useful:
        while a model download or a job is running and the button is
        disabled.

        The fix has two parts. The shortcut is a `WindowShortcut`-context
        action that `MainWindow` registers on the window itself, not the
        button, so its availability never depends on the button's enabled
        state. The right-click route is delivered through an event filter
        installed on the button (`eventFilter` below): Qt still calls an
        installed filter for a disabled widget's events, even though it
        never calls the widget's own `contextMenuEvent()` or emits its
        `customContextMenuRequested` -- also verified offscreen.
        """
        action = QAction(
            QCoreApplication.translate("ActionShelf", "Copy render settings"), self
        )
        action.setObjectName("copy_render_settings_action")
        action.setShortcut(QKeySequence("Ctrl+Shift+C"))
        action.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
        self.render_button.installEventFilter(self)
        return action

    def _render_context_menu(self) -> QMenu:
        """Return the Render button's popup menu, built once and reused.

        Kept as one instance rather than a fresh `QMenu`
        per right-click. Still a method, not inlined, so tests can inspect
        the menu without triggering a real popup -- `QMenu.exec` blocks
        until the menu closes, which a headless test has no way to do.
        """
        if self._render_menu is None:
            menu = QMenu(self.render_button)
            menu.setToolTipsVisible(True)
            menu.addAction(self.copy_render_settings_action)
            self._render_menu = menu
        return self._render_menu

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self.render_button and isinstance(event, QContextMenuEvent):
            self._render_context_menu().exec(event.globalPos())
            return True
        return super().eventFilter(watched, event)

    def _build_preferences_dialog(
        self,
        store: StateStore,
        services: WindowServices,
        settings: QSettings | None,
        provider_options: tuple[ProviderOption, ...] | None,
    ) -> SettingsDialog:
        return SettingsDialog(
            store,
            services,
            self,
            settings=settings,
            provider_options=provider_options,
        )

    def open_preferences(self) -> None:
        """Reload current state and show the window-modal Preferences dialog."""
        self.preferences_dialog.load()
        self.preferences_dialog.open()
