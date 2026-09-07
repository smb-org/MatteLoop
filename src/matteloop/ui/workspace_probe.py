"""The reuse probe, off the GUI thread like every other worker here."""

from __future__ import annotations

from uuid import uuid4

from PySide6.QtCore import QObject, Signal, Slot

from matteloop.core.specs import RenderRequest
from matteloop.core.state import JobKind
from matteloop.jobs.context import CancellationState, JobContext
from matteloop.jobs.workspace import CutWorkspace
from matteloop.ui.render_worker import RenderRuntime


class WorkspaceProbeWorker(QObject):
    """Ask the runtime whether a cut set can be reused for this request."""

    result = Signal(object)
    finished = Signal()

    def __init__(self, request: RenderRequest, runtime: RenderRuntime) -> None:
        super().__init__()
        self._request = request
        self._runtime = runtime
        self._context = JobContext(
            f"workspace-probe-{uuid4().hex}",
            JobKind.RENDER,
            request.output.directory,
            lambda _event: None,
            CancellationState(),
        )

    @Slot()
    def run(self) -> None:
        workspace: CutWorkspace | None = None
        finder = getattr(self._runtime, "find_matching_workspace", None)
        if callable(finder):
            try:
                candidate = finder(self._request, self._context)
                workspace = candidate if isinstance(candidate, CutWorkspace) else None
            except BaseException:
                workspace = None
        self.result.emit(workspace)
        self.finished.emit()
