"""Fixed editor actions, deliberately outside the inspector scroll area."""

from __future__ import annotations

from PySide6.QtCore import QCoreApplication, QRectF, QSettings, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QKeySequence, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
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
