from __future__ import annotations

from pathlib import Path

from matteloop.core.state import JobKind
from matteloop.jobs.models.catalog import ModelCatalog
from matteloop.jobs.workspace_migration import (
    LegacyCutSet,
    MigrationOutcome,
    MigrationReport,
    MigrationStatus,
)
from matteloop.ui.aligned_rows import (
    ACCESSIBLE_DESCRIPTION_ROLE,
    STATUS_ROLE,
    RowStatus,
)
from matteloop.ui.workspace_dialog import SUMMARY_ROLE, WorkspacePickerDialog
from tests.jobs.render_support import job, render_service, request


def test_cut_set_picker_lists_manifest_fields_and_external_edit_status(
    tmp_path, qtbot
) -> None:
    render_request = request(tmp_path)
    artifact = render_service().render(
        render_request, job(tmp_path, "picker", JobKind.RENDER)
    )
    with artifact.cut_workspace.read_promoted_cut(0) as frame:
        frame.save(artifact.cut_workspace.path / "frame-000000.png")

    dialog = WorkspacePickerDialog(ModelCatalog.load_resource())
    qtbot.addWidget(dialog)
    dialog.load(tmp_path)

    assert len(dialog.summaries) == 1
    assert dialog.summaries[0].manifest.edited is True
    item = dialog.cut_set_list.item(0)
    assert item.data(SUMMARY_ROLE) == dialog.summaries[0]
    assert "source.mp4" in item.text()
    assert item.data(STATUS_ROLE) is RowStatus.EDITED
    assert "edited" in item.data(ACCESSIBLE_DESCRIPTION_ROLE)
    assert "frames" in item.data(ACCESSIBLE_DESCRIPTION_ROLE)
    assert dialog.use_button.isEnabled()
    assert dialog.open_button.isEnabled()
    assert dialog.delete_button.isEnabled()


def test_cut_set_picker_shows_migration_offer_only_for_legacy_sets(
    tmp_path: Path, monkeypatch, qtbot
) -> None:
    dialog = WorkspacePickerDialog(ModelCatalog.load_resource())
    qtbot.addWidget(dialog)
    monkeypatch.setattr(
        "matteloop.ui.workspace_dialog.find_legacy_cut_sets", lambda _path: ()
    )

    dialog.load(tmp_path)

    assert dialog.legacy_notice.isHidden()
    assert dialog.move_button.isHidden()

    entry = LegacyCutSet(
        path=tmp_path / "legacy" / "cuts" / "set-abcdef12",
        cache_key="a" * 64,
        name="set-abcdef12",
        size_bytes=100,
        pinned=False,
        source_path="source.mp4",
        source_root=tmp_path / "legacy" / "cuts",
    )
    monkeypatch.setattr(
        "matteloop.ui.workspace_dialog.find_legacy_cut_sets", lambda _path: (entry,)
    )
    dialog.load(tmp_path)

    assert not dialog.legacy_notice.isHidden()
    assert not dialog.move_button.isHidden()
    assert "earlier version" in dialog.legacy_notice.text()


def test_cut_set_picker_reports_mixed_migration_results(
    tmp_path: Path, monkeypatch, qtbot
) -> None:
    dialog = WorkspacePickerDialog(ModelCatalog.load_resource())
    qtbot.addWidget(dialog)
    entry = LegacyCutSet(
        path=tmp_path / "legacy" / "cuts" / "set-abcdef12",
        cache_key="a" * 64,
        name="set-abcdef12",
        size_bytes=100,
        pinned=False,
        source_path="source.mp4",
        source_root=tmp_path / "legacy" / "cuts",
    )
    monkeypatch.setattr(
        "matteloop.ui.workspace_dialog.find_legacy_cut_sets", lambda _path: ()
    )
    dialog.load(tmp_path)
    report = MigrationReport(
        (
            MigrationOutcome(entry, MigrationStatus.MOVED),
            MigrationOutcome(
                entry,
                MigrationStatus.KEPT_BOTH,
                "already in the cache with different frames",
            ),
        )
    )

    dialog.show_migration_report(report)

    assert "Moved 1 set(s)." in dialog.message_text
    assert "different frames" in dialog.message_text
