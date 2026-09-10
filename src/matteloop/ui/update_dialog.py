"""Modal offer for a discovered MatteLoop update."""

from __future__ import annotations

from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


class UpdateDialog(QDialog):
    """Present the current update state and its existing update actions."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("update_dialog")
        self.setWindowTitle(
            QCoreApplication.translate("UpdateBanner", "Update notice")
        )
        self.setAccessibleName(
            QCoreApplication.translate("UpdateBanner", "Update notice")
        )
        self.setModal(True)
        self.setMinimumWidth(440)
        self._build_widgets()
        self._build_layout()
        self._set_tab_order()

    def _make_button(self, text: str, object_name: str) -> QPushButton:
        button = QPushButton(text)
        button.setObjectName(object_name)
        button.setAccessibleName(text)
        return button

    def _build_widgets(self) -> None:
        notice_name = QCoreApplication.translate("UpdateBanner", "Update notice")
        self.heading_label = QLabel(
            QCoreApplication.translate("UpdateBanner", "Software update")
        )
        self.heading_label.setObjectName("update_heading")
        self.heading_label.setProperty("heading", True)
        self.heading_label.setAccessibleName(
            QCoreApplication.translate("UpdateBanner", "Software update")
        )
        self.message_label = QLabel()
        self.message_label.setObjectName("update_message")
        self.message_label.setAccessibleName(notice_name)
        self.message_label.setWordWrap(True)
        self.size_label = QLabel()
        self.size_label.setObjectName("update_size")
        self.size_label.setProperty("secondary", True)
        self.size_label.setAccessibleName(
            QCoreApplication.translate("UpdateBanner", "Download size")
        )
        self.size_label.hide()

        self.download_button = self._make_button(
            QCoreApplication.translate("UpdateBanner", "Download update"),
            "update_download",
        )
        self.install_button = self._make_button(
            QCoreApplication.translate("UpdateBanner", "Install and restart"),
            "update_install",
        )
        self.release_notes_button = self._make_button(
            QCoreApplication.translate("UpdateBanner", "Release notes"),
            "update_release_notes",
        )
        self.open_releases_button = self._make_button(
            QCoreApplication.translate("UpdateBanner", "Open releases page"),
            "update_open_releases",
        )
        self.not_now_button = self._make_button(
            QCoreApplication.translate("UpdateBanner", "Not now"),
            "update_not_now",
        )
        self.later_button = self._make_button(
            QCoreApplication.translate("UpdateBanner", "Later"),
            "update_later",
        )

    def _build_layout(self) -> None:
        layout = QVBoxLayout(self)
        layout.addWidget(self.heading_label)
        layout.addWidget(self.message_label)
        layout.addWidget(self.size_label)
        actions = QHBoxLayout()
        actions.addStretch(1)
        actions.addWidget(self.download_button)
        actions.addWidget(self.install_button)
        actions.addWidget(self.release_notes_button)
        actions.addWidget(self.open_releases_button)
        actions.addWidget(self.not_now_button)
        actions.addWidget(self.later_button)
        layout.addLayout(actions)
        self._hide_actions()

    def set_download_size(self, message: str | None) -> None:
        """Show the localized package-size sentence when the reader knows it."""
        if message is None:
            self.size_label.clear()
            self.size_label.hide()
        else:
            self.size_label.setText(message)
            self.size_label.show()

    def _set_tab_order(self) -> None:
        buttons = (
            self.download_button,
            self.install_button,
            self.release_notes_button,
            self.open_releases_button,
            self.not_now_button,
            self.later_button,
        )
        for before, after in zip(buttons, buttons[1:], strict=False):
            self.setTabOrder(before, after)

    def _hide_actions(self) -> None:
        for button in (
            self.download_button,
            self.install_button,
            self.release_notes_button,
            self.open_releases_button,
            self.not_now_button,
            self.later_button,
        ):
            button.hide()

    def show_available_actions(self, *, can_download: bool) -> None:
        self._hide_actions()
        self.download_button.setText(
            QCoreApplication.translate("UpdateBanner", "Download update")
        )
        self.download_button.setAccessibleName(
            QCoreApplication.translate("UpdateBanner", "Download update")
        )
        if can_download:
            self.download_button.show()
            self.release_notes_button.show()
        else:
            self.open_releases_button.show()
        self.not_now_button.show()

    def show_downloading_actions(self) -> None:
        self._hide_actions()
        self.download_button.setText(
            QCoreApplication.translate("UpdateBanner", "Cancel")
        )
        self.download_button.setAccessibleName(
            QCoreApplication.translate("UpdateBanner", "Cancel")
        )
        self.download_button.show()

    def show_ready_actions(self) -> None:
        self._hide_actions()
        self.install_button.show()
        self.release_notes_button.show()
        self.later_button.show()

    def show_failed_actions(self) -> None:
        self._hide_actions()
        self.download_button.setText(
            QCoreApplication.translate("UpdateBanner", "Try again")
        )
        self.download_button.setAccessibleName(
            QCoreApplication.translate("UpdateBanner", "Try again")
        )
        self.download_button.show()
        self.open_releases_button.show()
