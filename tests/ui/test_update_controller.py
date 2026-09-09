from __future__ import annotations

import sys

from PySide6.QtCore import QSettings, QUrl
from PySide6.QtGui import QDesktopServices

from matteloop.core.state import AppState
from matteloop.ui.main_window import MainWindow
from matteloop.ui.store import ReducerStore
from matteloop.ui.update_controller import UpdateController
from matteloop.updates import UpdateOutcome, UpdateResult


class _Services:
    def dispatch(self, _command: object) -> None:
        return

    def confirm_discard_unsaved_transform(self, _parent: object) -> bool:
        return True


class _Reader:
    def __init__(self, result: UpdateResult) -> None:
        self.result = result
        self.calls = 0

    def check(self) -> UpdateResult:
        self.calls += 1
        return self.result


def _settings(name: str) -> QSettings:
    settings = QSettings(
        QSettings.IniFormat,
        QSettings.UserScope,
        "matteloop-test",
        name,
    )
    settings.clear()
    return settings


def _window(qtbot, settings: QSettings) -> MainWindow:
    window = MainWindow(ReducerStore(AppState()), _Services(), settings)
    qtbot.addWidget(window)
    window.show()
    return window


def _controller(
    qtbot,
    settings: QSettings,
    result: UpdateResult,
) -> tuple[MainWindow, UpdateController, _Reader]:
    window = _window(qtbot, settings)
    reader = _Reader(result)
    controller = UpdateController(window, settings, reader)
    return window, controller, reader


def test_no_update_keeps_the_banner_hidden(qtbot) -> None:
    window, controller, _ = _controller(
        qtbot, _settings("none"), UpdateResult(UpdateOutcome.NONE)
    )

    controller.check_now()
    qtbot.waitUntil(lambda: not controller.check_in_progress)

    assert not window.update_container.isVisible()
    assert window.action_shelf.preferences_dialog.updates_status_label.text() == (
        "No update found."
    )


def test_available_update_shows_versioned_banner(qtbot) -> None:
    window, controller, _ = _controller(
        qtbot,
        _settings("update"),
        UpdateResult(UpdateOutcome.UPDATE, "0.4.0"),
    )

    controller.check_now()
    qtbot.waitUntil(lambda: not controller.check_in_progress)

    assert window.update_container.isVisible()
    assert window.update_banner.text() == "MatteLoop 0.4.0 is available."
    assert window.update_open_releases_button.text() == "Open releases page"
    assert window.update_not_now_button.text() == "Not now"
    assert (
        window.action_shelf.preferences_dialog.updates_status_label.text()
        == "MatteLoop 0.4.0 is available."
    )


def test_not_now_hides_the_update_banner(qtbot) -> None:
    window, controller, _ = _controller(
        qtbot,
        _settings("dismiss"),
        UpdateResult(UpdateOutcome.UPDATE, "0.4.0"),
    )

    controller.check_now()
    qtbot.waitUntil(lambda: not controller.check_in_progress)
    window.update_not_now_button.click()

    assert not window.update_container.isVisible()


def test_manual_check_reports_a_failed_reader(qtbot) -> None:
    window, controller, _ = _controller(
        qtbot, _settings("failed"), UpdateResult(UpdateOutcome.FAILED)
    )

    controller.check_now()
    qtbot.waitUntil(lambda: not controller.check_in_progress)

    assert not window.update_container.isVisible()
    assert window.action_shelf.preferences_dialog.updates_status_label.text() == (
        "Couldn’t check for updates. Try again later."
    )


def test_startup_check_requires_the_setting_and_a_frozen_runtime(
    monkeypatch, qtbot
) -> None:
    settings = _settings("startup-gates")
    settings.setValue("updates/check_on_startup", False)
    window, controller, reader = _controller(
        qtbot, settings, UpdateResult(UpdateOutcome.NONE)
    )
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    controller.start()

    assert reader.calls == 0

    settings.setValue("updates/check_on_startup", True)
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    window2, controller2, reader2 = _controller(
        qtbot, settings, UpdateResult(UpdateOutcome.NONE)
    )
    controller2.start()

    assert reader2.calls == 0
    del window, window2


def test_startup_check_runs_in_a_frozen_runtime_when_enabled(
    monkeypatch, qtbot
) -> None:
    settings = _settings("startup-enabled")
    settings.setValue("updates/check_on_startup", True)
    window, controller, reader = _controller(
        qtbot, settings, UpdateResult(UpdateOutcome.NONE)
    )
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(
        "matteloop.ui.update_controller.QTimer.singleShot",
        lambda _delay, callback: callback(),
    )

    controller.start()
    qtbot.waitUntil(lambda: not controller.check_in_progress)

    assert reader.calls == 1
    del window


def test_manual_check_works_when_running_from_source(qtbot) -> None:
    settings = _settings("manual-source")
    window, controller, reader = _controller(
        qtbot, settings, UpdateResult(UpdateOutcome.NONE)
    )

    controller.check_now()
    qtbot.waitUntil(lambda: not controller.check_in_progress)

    assert reader.calls == 1
    del window


def test_open_releases_page_uses_the_release_url(monkeypatch, qtbot) -> None:
    window, controller, _ = _controller(
        qtbot,
        _settings("open-url"),
        UpdateResult(UpdateOutcome.UPDATE, "0.4.0"),
    )
    opened: list[QUrl] = []
    monkeypatch.setattr(QDesktopServices, "openUrl", opened.append)

    controller.check_now()
    qtbot.waitUntil(lambda: not controller.check_in_progress)
    window.update_open_releases_button.click()

    assert opened == [QUrl("https://github.com/smb-org/MatteLoop/releases")]
    del window
