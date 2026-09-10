from __future__ import annotations

from pathlib import Path
from threading import get_ident

import shiboken6
from PySide6.QtCore import QObject, Signal, Slot

from matteloop.ui.worker_thread import WorkerThread


def test_ui_thread_shutdown_waits_use_the_retention_helper() -> None:
    ui_source = Path(__file__).parents[2] / "src" / "matteloop" / "ui"
    raw_waits = []
    for source_path in sorted(ui_source.rglob("*.py")):
        if source_path.name == "worker_thread.py":
            continue
        for line_number, line in enumerate(
            source_path.read_text(encoding="utf-8").splitlines(), 1
        ):
            if ".wait(" in line:
                location = source_path.relative_to(ui_source)
                raw_waits.append(
                    f"{location}:{line_number}: {line.strip()}"
                )

    assert not raw_waits, (
        "Route every UI thread.wait(...) through "
        "wait_for_thread_shutdown in worker_thread.py:\n"
        + "\n".join(raw_waits)
    )


class _Worker(QObject):
    finished = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.thread_id: int | None = None

    @Slot()
    def run(self) -> None:
        self.thread_id = get_ident()
        self.finished.emit()


def test_worker_runs_off_the_gui_thread_and_the_thread_retires_itself(qtbot) -> None:
    worker = _Worker()
    thread = WorkerThread(worker)
    worker.finished.connect(thread.quit)
    worker.finished.connect(worker.deleteLater)

    # The worker must not be a receiver of this thread: destroying a receiver
    # inside the thread calls disconnectNotify on the sender from there (#39).
    assert thread.receivers("2started()") == 0

    thread.start()

    qtbot.waitUntil(lambda: worker.thread_id is not None, timeout=5000)
    assert worker.thread_id != get_ident()
    qtbot.waitUntil(lambda: not shiboken6.isValid(thread), timeout=5000)
    qtbot.waitUntil(lambda: not shiboken6.isValid(worker), timeout=5000)


def test_immediate_quit_still_runs_worker_and_retires_both(qtbot) -> None:
    worker = _Worker()
    thread = WorkerThread(worker)
    worker.finished.connect(thread.quit)
    worker.finished.connect(worker.deleteLater)

    thread.start()
    thread.quit()

    qtbot.waitUntil(lambda: worker.thread_id is not None, timeout=5000)
    qtbot.waitUntil(lambda: not shiboken6.isValid(thread), timeout=5000)
    qtbot.waitUntil(lambda: not shiboken6.isValid(worker), timeout=5000)
