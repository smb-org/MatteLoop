"""Picker lifecycle and safe actions for promoted cut sets."""

from __future__ import annotations

from PySide6.QtWidgets import QMessageBox

from matteloop.core.specs import TransformSpec
from matteloop.core.state import JobKind
from matteloop.jobs.render import FilesystemWorkspacePort
from matteloop.jobs.transform_store import (
    load_transform,
    store_transform,
    transform_sidecar_path,
)
from matteloop.jobs.workspace import WorkspaceSummary
from matteloop.ui.workspace_controller import WorkspacePickerController
from tests.jobs.render_support import job, render_service, request


def test_delete_selected_removes_the_transform_sidecar_with_the_cut(
    tmp_path, monkeypatch, qtbot
) -> None:
    artifact = render_service(workspace=FilesystemWorkspacePort()).render(
        request(tmp_path), job(tmp_path, "seed-delete", JobKind.RENDER)
    )
    workspace = artifact.cut_workspace
    store_transform(workspace, TransformSpec(first_frame=1), [])
    sidecar = transform_sidecar_path(workspace)
    assert sidecar.exists()

    controller = WorkspacePickerController(
        dialog_parent=None,
        request_factory=lambda: request(tmp_path),
        active_workspace=lambda: None,
    )
    qtbot.addWidget(controller.dialog)
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args, **_kwargs: int(QMessageBox.StandardButton.Yes),
    )

    controller._delete_selected(  # noqa: SLF001
        WorkspaceSummary(workspace, artifact.manifest, 0)
    )

    assert not workspace.path.exists()
    assert not sidecar.exists()


def test_delete_unpinned_set_keeps_frames_and_transform_when_confirmation_is_cancelled(
    tmp_path, monkeypatch, qtbot
) -> None:
    artifact = render_service(workspace=FilesystemWorkspacePort()).render(
        request(tmp_path), job(tmp_path, "cancel-delete", JobKind.RENDER)
    )
    workspace = artifact.cut_workspace
    stored = TransformSpec(first_frame=1)
    store_transform(workspace, stored, [])
    sidecar = transform_sidecar_path(workspace)
    confirmation: list[tuple[str, str, object]] = []

    def cancel(_parent, title, body, buttons, default):
        confirmation.append((title, body, default))
        assert buttons == QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        return int(QMessageBox.StandardButton.No)

    monkeypatch.setattr(QMessageBox, "question", cancel)
    controller = WorkspacePickerController(
        dialog_parent=None,
        request_factory=lambda: request(tmp_path),
        active_workspace=lambda: None,
    )
    qtbot.addWidget(controller.dialog)

    controller._delete_selected(  # noqa: SLF001
        WorkspaceSummary(workspace, artifact.manifest, 0)
    )

    assert confirmation
    assert "source.mp4" in confirmation[0][1]
    assert f"{artifact.manifest.frame_count} frames" in confirmation[0][1]
    assert "BiRefNet Portrait" in confirmation[0][1]
    assert "Stored frames" in confirmation[0][1]
    assert "saved transform" in confirmation[0][1]
    assert "background removal again" in confirmation[0][1]
    assert confirmation[0][2] == QMessageBox.StandardButton.No
    assert workspace.path.exists()
    assert sidecar.exists()
    assert load_transform(workspace) == stored


def test_delete_pinned_set_confirmation_also_warns_about_pinning(
    tmp_path, monkeypatch, qtbot
) -> None:
    artifact = render_service(workspace=FilesystemWorkspacePort()).render(
        request(tmp_path), job(tmp_path, "pinned-delete", JobKind.RENDER)
    )
    workspace = artifact.cut_workspace
    pinned_manifest = workspace.set_pinned(True)
    confirmation: list[str] = []

    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda _parent, _title, body, *_args: (
            confirmation.append(body) or int(QMessageBox.StandardButton.No)
        ),
    )
    controller = WorkspacePickerController(
        dialog_parent=None,
        request_factory=lambda: request(tmp_path),
        active_workspace=lambda: None,
    )
    qtbot.addWidget(controller.dialog)

    controller._delete_selected(  # noqa: SLF001
        WorkspaceSummary(workspace, pinned_manifest, 0)
    )

    assert len(confirmation) == 1
    assert "This set is pinned. Delete it anyway?" in confirmation[0]
    assert workspace.path.exists()
