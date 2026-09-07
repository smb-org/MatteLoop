"""GUI-thread render orchestration, confirmations, and output actions.

Shutdown note: every widget this controller creates is destroyed in
``shutdown``. The job dialog survives its job to show the completion summary,
so this controller keeps it alive until the event loop closes.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Thread
from uuid import uuid4

from PySide6.QtCore import QObject, QThread, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QFileDialog, QMessageBox, QWidget

from matteloop.core.errors import AppError
from matteloop.core.parameters import TransformChanged
from matteloop.core.specs import CollisionPolicy, RenderRequest, TransformSpec
from matteloop.core.state import (
    CancelAcknowledged,
    CancelRequested,
    JobKind,
    JobStageChanged,
    JobState,
    PreviewState,
    RebuildRequested,
    RenderFailed,
    RenderPreflightDismissed,
    RenderPreflightRequested,
    RenderRequested,
    RenderSucceeded,
    SourceState,
    capabilities,
)
from matteloop.core.tokens import ProgressStage
from matteloop.jobs.context import (
    CancellationState,
    ExclusiveJobScheduler,
    JobContext,
    ProgressEvent,
)
from matteloop.jobs.workspace import CutWorkspace, WorkspaceSummary
from matteloop.ui.copy import render_copy
from matteloop.ui.ports import (
    ManageWorkspacesRequested,
    OpenOutputFolderRequested,
    OpenOutputRequested,
    RebuildEditedCutsRequested,
    RenderVideoRequested,
    StateStore,
)
from matteloop.ui.preview_controller import PreviewJobDialog, PreviewRuntime
from matteloop.ui.render_worker import RenderRuntime, RenderWorker
from matteloop.ui.request_builder import _preview_inputs, _render_request
from matteloop.ui.worker_thread import WorkerThread
from matteloop.ui.workspace_controller import WorkspacePickerController
from matteloop.ui.workspace_dialog import WorkspacePickerDialog
from matteloop.ui.workspace_presentation import request_for_workspace


class _WorkspaceProbeWorker(QObject):
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


@dataclass(frozen=True, slots=True)
class _PendingRender:
    source_id: str
    request: RenderRequest


class RenderController(QObject):
    """Start render jobs after reducer-approved UI confirmations."""

    provider_ready = Signal(str)
    provider_notice = Signal(str)
    artifact_ready = Signal(object)

    def __init__(
        self,
        store: StateStore,
        *,
        runtime: PreviewRuntime,
        scheduler: ExclusiveJobScheduler,
        preview_callback: Callable[[], None],
        dialog_parent: QWidget | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._store = store
        self._runtime = runtime
        self._scheduler = scheduler
        self._preview_callback = preview_callback
        self._dialog_parent = dialog_parent
        self._contexts: dict[str, JobContext] = {}
        self._threads: dict[str, tuple[QThread, RenderWorker]] = {}
        self._active_job_id: str | None = None
        self._dialog: PreviewJobDialog | None = None
        self._dialog_cancel_connected = False
        self._preflight_dialog: QMessageBox | None = None
        self._preflight_request: _PendingRender | None = None
        self._collision_dialog: QMessageBox | None = None
        self._collision_request: _PendingRender | None = None
        self._collision_workspace: CutWorkspace | None = None
        self._reuse_dialog: QMessageBox | None = None
        self._reuse_request: _PendingRender | None = None
        self._reuse_workspace: CutWorkspace | None = None
        self._probe_thread: QThread | None = None
        self._probe_worker: _WorkspaceProbeWorker | None = None
        self._probe_request: _PendingRender | None = None
        self._workspace_picker: WorkspacePickerController | None = None
        self._active_workspace: CutWorkspace | None = None
        self.transform_restore: Callable[[CutWorkspace], TransformSpec] | None = None
        self.open_cut_key: Callable[[], str | None] | None = None
        self._closed = False

    @property
    def dialog(self) -> PreviewJobDialog | None:
        return self._dialog

    @property
    def preflight_dialog(self) -> QMessageBox | None:
        return self._preflight_dialog

    @property
    def collision_dialog(self) -> QMessageBox | None:
        return self._collision_dialog

    @property
    def reuse_dialog(self) -> QMessageBox | None:
        return self._reuse_dialog

    @property
    def workspace_picker(self) -> WorkspacePickerDialog | None:
        return self._workspace_picker.dialog if self._workspace_picker else None

    @property
    def active_render_count(self) -> int:
        return sum(thread.isRunning() for thread, _worker in self._threads.values())

    def set_dialog_parent(self, parent: QWidget) -> None:
        self._dialog_parent = parent
        if self._dialog is not None:
            self._dialog.setParent(parent)

    def dispatch(self, command: object) -> None:
        if self._closed:
            return
        if isinstance(command, RenderVideoRequested):
            self._request_render()
        elif isinstance(command, RebuildEditedCutsRequested):
            self._open_workspace_picker()
        elif isinstance(command, ManageWorkspacesRequested):
            self._open_workspace_picker()
        elif isinstance(command, OpenOutputRequested):
            self._open_output()
        elif isinstance(command, OpenOutputFolderRequested):
            self._open_output_folder()

    def shutdown(self) -> None:
        self._closed = True
        if self._dialog is not None:
            # Destroyed here, not by a later GC pass -- see the module docstring.
            dialog, self._dialog = self._dialog, None
            dialog.close()
            dialog.deleteLater()
        if self._preflight_dialog is not None:
            self._preflight_dialog.close()
        if self._collision_dialog is not None:
            self._collision_dialog.close()
        if self._reuse_dialog is not None:
            self._reuse_dialog.close()
        if self._workspace_picker is not None:
            self._workspace_picker.close()
        if self._probe_thread is not None:
            self._probe_thread.quit()
        for context in tuple(self._contexts.values()):
            context.request_cancel()
        cancel = getattr(self._runtime, "cancel", None)
        if callable(cancel):
            for job_id in tuple(self._contexts):
                Thread(
                    target=_cancel_runtime,
                    args=(cancel, job_id),
                    name="matteloop-render-cancel",
                    daemon=True,
                ).start()
        for job_id, (thread, _worker) in tuple(self._threads.items()):
            try:
                thread.quit()
            except RuntimeError:
                self._threads.pop(job_id, None)
        if self._probe_thread is not None:
            try:
                self._probe_thread.wait(5000)
            except RuntimeError:
                self._probe_thread = None
        for job_id, (thread, _worker) in tuple(self._threads.items()):
            try:
                thread.wait(5000)
            except RuntimeError:
                self._threads.pop(job_id, None)

    def _request_render(self) -> None:
        state = self._store.state
        if not capabilities(state).can_render:
            return
        source_id = state.source_id
        if source_id is None or state.source is not SourceState.READY:
            return
        try:
            inputs = _preview_inputs(
                state.source_value, state.timeline, state.crop, state.parameters
            )
            request = _render_request(inputs)
        except BaseException:
            return
        pending = _PendingRender(source_id, request)
        if state.preview in {
            PreviewState.NONE,
            PreviewState.STALE,
            PreviewState.ERROR,
        }:
            self._preflight_request = pending
            self._store.dispatch(RenderPreflightRequested())
            if self._store.state.preflight_warning:
                self._show_preflight()
            return
        self._probe_for_reuse(pending)

    def _choice_dialog(
        self,
        name: str,
        title: str,
        text: str,
        informative: str,
        buttons: tuple[tuple[str, str], ...],
        default: int,
        chosen: Callable[[str], None],
        closed: Callable[[], None],
    ) -> QMessageBox:
        """Build one three-choice confirmation, unopened.

        The preflight, reuse and collision prompts differ only in wording and
        in what they do with the answer, so they share this: *buttons* pairs
        each label with the choice *chosen* receives, in display order; the
        last is always the escape button, and *default* indexes the
        preselected one. The caller stores the dialog before opening it --
        *closed* runs while it is still recorded.
        """
        dialog = QMessageBox(self._dialog_parent)
        dialog.setObjectName(name)
        dialog.setWindowTitle(title)
        dialog.setText(text)
        dialog.setInformativeText(informative)
        role = QMessageBox.ButtonRole.AcceptRole
        answers = {dialog.addButton(label, role): choice for label, choice in buttons}
        widgets = tuple(answers)
        dialog.setDefaultButton(widgets[default])
        dialog.setEscapeButton(widgets[-1])
        dialog.buttonClicked.connect(lambda button: chosen(answers[button]))
        dialog.finished.connect(lambda _result: closed())
        return dialog

    def _show_preflight(self) -> None:
        if self._preflight_dialog is not None:
            return
        dialog = self._choice_dialog(
            "render_preflight_dialog",
            render_copy("Preview recommended"),
            render_copy("Preview this frame before rendering?"),
            render_copy(
                "A preview lets you verify the cutout before processing the whole "
                "video."
            ),
            (
                (render_copy("Preview first"), "preview"),
                (render_copy("Render anyway"), "render"),
                (render_copy("Cancel"), "cancel"),
            ),
            0,
            self._finish_preflight,
            self._preflight_closed,
        )
        self._preflight_dialog = dialog
        dialog.open()

    def _preflight_closed(self) -> None:
        if self._preflight_dialog is not None:
            self._finish_preflight("cancel")

    def _finish_preflight(self, choice: str) -> None:
        dialog = self._preflight_dialog
        pending = self._preflight_request
        if dialog is None or pending is None:
            return
        self._preflight_dialog = None
        self._preflight_request = None
        dialog.close()
        self._store.dispatch(RenderPreflightDismissed())
        if choice == "preview":
            self._preview_callback()
        elif choice == "render":
            self._probe_for_reuse(pending)

    def _probe_for_reuse(self, pending: _PendingRender) -> None:
        if self._probe_thread is not None:
            return
        finder = getattr(self._runtime, "find_matching_workspace", None)
        if not callable(finder):
            self._resolve_collision(pending)
            return
        worker = _WorkspaceProbeWorker(pending.request, self._runtime)
        thread = WorkerThread(worker, self)
        worker.result.connect(self._reuse_probe_result)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(lambda: self._probe_finished(thread))
        self._probe_worker = worker
        self._probe_thread = thread
        self._probe_request = pending
        thread.start()

    @Slot(object)
    def _reuse_probe_result(self, value: object) -> None:
        if self._closed:
            return
        pending = self._probe_request
        self._probe_request = None
        if pending is None:
            return
        if isinstance(value, CutWorkspace):
            self._reuse_request = pending
            self._reuse_workspace = value
            self._show_reuse()
        else:
            self._resolve_collision(pending)

    def _probe_finished(self, thread: QThread) -> None:
        if self._probe_thread is thread:
            self._probe_thread = None
            self._probe_worker = None

    def _resolve_collision(
        self,
        pending: _PendingRender,
        workspace: CutWorkspace | None = None,
    ) -> None:
        request = pending.request
        try:
            exists = request.output.path.exists()
        except OSError:
            exists = False
        if not exists:
            self._start(pending, workspace)
            return
        self._collision_request = pending
        self._collision_workspace = workspace
        self._show_collision()

    def _show_reuse(self) -> None:
        if self._reuse_dialog is not None or self._reuse_request is None:
            return
        dialog = self._choice_dialog(
            "render_reuse_dialog",
            render_copy("Matching cut set found"),
            render_copy("A validated cut set matches the current source and settings."),
            render_copy(
                "Rebuild reuses the cuts and only reruns framing and encoding."
            ),
            (
                (render_copy("Rebuild"), "rebuild"),
                (render_copy("Regenerate"), "regenerate"),
                (render_copy("Cancel"), "cancel"),
            ),
            0,
            self._finish_reuse,
            self._reuse_closed,
        )
        self._reuse_dialog = dialog
        dialog.open()

    def _reuse_closed(self) -> None:
        if self._reuse_dialog is not None:
            self._reuse_dialog = None
            self._reuse_request = None
            self._reuse_workspace = None

    def _finish_reuse(self, choice: str) -> None:
        dialog = self._reuse_dialog
        pending = self._reuse_request
        workspace = self._reuse_workspace
        if dialog is None or pending is None:
            return
        self._reuse_dialog = None
        self._reuse_request = None
        self._reuse_workspace = None
        dialog.close()
        if choice == "rebuild" and workspace is not None:
            request = self._transform_for_rebuild(
                replace(pending.request, rebuild=True), workspace
            )
            self._resolve_collision(replace(pending, request=request), workspace)
        elif choice == "regenerate":
            self._resolve_collision(
                replace(pending, request=replace(pending.request, regenerate=True)),
                None,
            )

    def _transform_for_rebuild(
        self, request: RenderRequest, workspace: CutWorkspace
    ) -> RenderRequest:
        """Build a rebuild request with the right cut transform, without dispatching."""
        open_key = self.open_cut_key() if self.open_cut_key is not None else None
        if open_key == workspace.cache_key or self.transform_restore is None:
            return request
        return replace(request, transform=self.transform_restore(workspace))

    def _current_request(self) -> RenderRequest | None:
        state = self._store.state
        if state.source is not SourceState.READY or state.source_value is None:
            return None
        try:
            inputs = _preview_inputs(
                state.source_value, state.timeline, state.crop, state.parameters
            )
            return _render_request(inputs)
        except BaseException:
            return None

    def _open_workspace_picker(self) -> None:
        request = self._current_request()
        if request is None:
            return
        if self._workspace_picker is None:
            picker = WorkspacePickerController(
                dialog_parent=self._dialog_parent,
                request_factory=self._current_request,
                active_workspace=lambda: self._active_workspace,
            )
            picker.dialog.use_requested.connect(self._use_workspace)
            self._workspace_picker = picker
        self._workspace_picker.open(request.output.directory)

    @Slot(object)
    def _use_workspace(self, value: object) -> None:
        if not isinstance(value, WorkspaceSummary):
            return
        request = self._current_request()
        source_id = self._store.state.source_id
        if request is None or source_id is None:
            return
        try:
            selected = request_for_workspace(value.manifest, request)
            selected = self._transform_for_rebuild(selected, value.workspace)
        except (AppError, ValueError, KeyError):
            return
        if self._workspace_picker is not None:
            self._workspace_picker.close()
        self._resolve_collision(_PendingRender(source_id, selected), value.workspace)

    def _show_collision(self) -> None:
        if self._collision_dialog is not None or self._collision_request is None:
            return
        dialog = self._choice_dialog(
            "render_collision_dialog",
            render_copy("Output already exists"),
            render_copy("%s already exists.")
            % self._collision_request.request.output.path,
            render_copy("Choose how to handle the existing output."),
            (
                (render_copy("Replace"), "replace"),
                (render_copy("Choose another name"), "choose"),
                (render_copy("Cancel"), "cancel"),
            ),
            2,
            self._finish_collision,
            self._collision_closed,
        )
        self._collision_dialog = dialog
        dialog.open()

    def _collision_closed(self) -> None:
        if self._collision_dialog is not None:
            self._collision_dialog = None
            self._collision_request = None
            self._collision_workspace = None

    def _finish_collision(self, choice: str) -> None:
        dialog = self._collision_dialog
        pending = self._collision_request
        workspace = self._collision_workspace
        if dialog is None or pending is None:
            return
        self._collision_dialog = None
        self._collision_request = None
        self._collision_workspace = None
        dialog.close()
        if choice == "replace":
            output = replace(
                pending.request.output, collision_policy=CollisionPolicy.REPLACE
            )
            self._start(
                replace(pending, request=replace(pending.request, output=output)),
                workspace,
            )
        elif choice == "choose":
            self._choose_output_name(pending, workspace)

    def _choose_output_name(
        self, pending: _PendingRender, workspace: CutWorkspace | None = None
    ) -> None:
        request = pending.request
        filename, _filter = QFileDialog.getSaveFileName(
            self._dialog_parent,
            render_copy("Choose output name"),
            str(request.output.path),
            render_copy("WebP files (*.webp)"),
        )
        if not filename:
            return
        chosen = Path(filename)
        if chosen.suffix.casefold() != ".webp":
            chosen = (
                chosen.with_suffix(".webp") if chosen.suffix else Path(f"{chosen}.webp")
            )
        try:
            selected = replace(
                request,
                output=replace(
                    request.output,
                    directory=chosen.parent,
                    filename=chosen.name,
                    collision_policy=CollisionPolicy.CANCEL,
                ),
            )
        except AppError:
            return
        self._resolve_collision(replace(pending, request=selected), workspace)

    def _spawn_worker(
        self,
        job_id: str,
        source_id: str,
        request_id: str,
        request: RenderRequest,
        context: JobContext,
        rebuild_workspace: CutWorkspace | None,
    ) -> RenderWorker:
        """Build the worker and its thread, wired to this controller's slots."""
        worker = RenderWorker(
            job_id,
            source_id,
            request_id,
            request,
            self._runtime,
            context,
            rebuild_workspace,
        )
        thread = WorkerThread(worker, self)
        worker.notification.connect(self._notification)
        worker.provider_ready.connect(self._provider_ready)
        worker.provider_notice.connect(self._provider_notice)
        worker.artifact_ready.connect(self.artifact_ready.emit)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(lambda job_id=job_id: self._thread_finished(job_id))
        self._threads[job_id] = (thread, worker)
        return worker

    def _start(
        self, pending: _PendingRender, rebuild_workspace: CutWorkspace | None = None
    ) -> None:
        state = self._store.state
        if pending.source_id != state.source_id:
            return
        source_id = pending.source_id
        request = pending.request
        job_id = uuid4().hex
        request_id = uuid4().hex
        if rebuild_workspace is None:
            self._store.dispatch(RenderRequested(job_id, request_id))
        else:
            request = replace(request, rebuild=True, regenerate=False)
            self._store.dispatch(TransformChanged(request.transform))
            self._store.dispatch(
                RebuildRequested(
                    job_id, request_id, workspace_key=rebuild_workspace.cache_key
                )
            )
        if self._store.state.job.job_id != job_id:
            return
        worker_ref: dict[str, RenderWorker] = {}

        def progress_sink(event: ProgressEvent) -> None:
            worker_ref["worker"].emit_progress(event)

        cancellation = CancellationState()
        lease = self._scheduler.claim(
            JobKind.RENDER,
            job_id,
            workspace=request.output.directory,
            progress_sink=progress_sink,
            cancellation=cancellation,
        )
        context = lease.__enter__()
        worker = self._spawn_worker(
            job_id, source_id, request_id, request, context, rebuild_workspace
        )
        worker_ref["worker"] = worker
        self._contexts[job_id] = context
        self._active_job_id = job_id
        self._active_workspace = rebuild_workspace
        self._open_dialog(
            job_id,
            JobKind.REBUILD if rebuild_workspace else JobKind.RENDER,
            request,
        )
        self._threads[job_id][0].start()

    def _open_dialog(self, job_id: str, kind: JobKind, request: RenderRequest) -> None:
        if self._dialog is None:
            self._dialog = PreviewJobDialog(self._dialog_parent)
        if not self._dialog_cancel_connected:
            self._dialog.cancel_requested.connect(self._cancel_active)
            self._dialog.open_output_requested.connect(self._open_output)
            self._dialog.open_folder_requested.connect(self._open_output_folder)
            self._dialog_cancel_connected = True
        rebuilding = kind is JobKind.REBUILD
        title = render_copy(
            "Rebuilding from edited cuts" if rebuilding else "Rendering video"
        )
        self._dialog.reset(title)
        self._dialog.set_stage("Validation" if rebuilding else ProgressStage.DECODE)
        self._dialog.set_job_details(
            request.segmentation.model_id, request.output.filename
        )
        self._dialog.setProperty("jobId", job_id)
        self._dialog.open()

    @Slot()
    def _cancel_active(self) -> None:
        job_id = self._active_job_id
        if job_id is None:
            return
        context = self._contexts.get(job_id)
        if context is None or not context.request_cancel():
            return
        self._store.dispatch(CancelRequested(job_id))
        if self._dialog is not None:
            self._dialog.set_cancelling()
        cancel = getattr(self._runtime, "cancel", None)
        if callable(cancel):
            Thread(
                target=_cancel_runtime,
                args=(cancel, job_id),
                name="matteloop-render-cancel",
                daemon=True,
            ).start()

    @Slot(object)
    def _notification(self, notification: object) -> None:
        job_id = getattr(notification, "job_id", None)
        if job_id is None or job_id != self._active_job_id:
            return
        if self._store.state.job.job_id != job_id:
            return
        if isinstance(notification, ProgressEvent):
            progress = _normalise_progress(notification)
            state = self._store.state
            self._store.dispatch(
                JobStageChanged(
                    job_id,
                    state.source_id or "",
                    state.job_request_id or "",
                    progress.stage,
                )
            )
            if (
                self._dialog is not None
                and self._store.state.job.phase is not JobState.CANCELLING
            ):
                self._dialog.set_progress(progress)
            return
        if isinstance(
            notification, (RenderSucceeded, RenderFailed, CancelAcknowledged)
        ):
            self._terminal_notification(notification)

    @Slot(str)
    def _provider_ready(self, provider: str) -> None:
        if (
            self._active_job_id is None
            or self._store.state.job.job_id != self._active_job_id
        ):
            return
        if self._dialog is not None:
            self._dialog.set_execution_provider(provider)
        self.provider_ready.emit(provider)

    @Slot(str)
    def _provider_notice(self, notice: str) -> None:
        if (
            self._active_job_id is None
            or self._store.state.job.job_id != self._active_job_id
        ):
            return
        if self._dialog is not None:
            self._dialog.set_provider_notice(notice)

    def _terminal_notification(
        self, notification: RenderSucceeded | RenderFailed | CancelAcknowledged
    ) -> None:
        job_id = getattr(notification, "job_id", None)
        if job_id is None or self._store.state.job.job_id != job_id:
            return
        self._store.dispatch(notification)
        if self._store.state.job.phase is JobState.IDLE:
            if self._dialog is not None:
                if isinstance(notification, RenderSucceeded):
                    result = self._store.state.artifact_result
                    if result is not None:
                        self._dialog.show_completion(result)
                    else:
                        self._dialog.close_for_terminal()
                else:
                    self._dialog.close_for_terminal()
            if self._active_job_id == job_id:
                self._active_job_id = None
                self._active_workspace = None

    def _thread_finished(self, job_id: str) -> None:
        self._contexts.pop(job_id, None)
        self._threads.pop(job_id, None)

    def _open_output(self) -> None:
        state = self._store.state
        if not capabilities(state).can_open_output or state.artifact_result is None:
            return
        path = _path_value(state.artifact_result.output_path)
        if path is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _open_output_folder(self) -> None:
        state = self._store.state
        if not capabilities(state).can_open_folder or state.artifact_result is None:
            return
        path = _path_value(state.artifact_result.output_path)
        if path is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent)))


def _normalise_progress(event: ProgressEvent) -> ProgressEvent:
    if event.stage is not ProgressStage.RENDER_CUT:
        return event
    return replace(event, stage=ProgressStage.DECODE)


def _path_value(value: object) -> Path | None:
    if isinstance(value, Path):
        return value
    if isinstance(value, str) and value:
        return Path(value)
    return None


def _cancel_runtime(cancel: object, job_id: str) -> None:
    try:
        cancel(job_id)  # type: ignore[operator]
    except BaseException:
        return
