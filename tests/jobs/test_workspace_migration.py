from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

import matteloop.jobs.workspace_migration as migration_module
from matteloop.core.state import JobKind
from matteloop.jobs.workspace import CutManifest, CutWorkspace, validate_cut_set
from matteloop.paths import WORKSPACE_NAME, cut_workspace_root
from tests.jobs.render_support import job, render_service, request


def _rendered_set(
    tmp_path: Path,
    cache_root: Path,
    *,
    job_id: str,
    fallback: bool = False,
    pinned: bool = False,
) -> tuple[Path, Path, str]:
    output = tmp_path / f"output-{job_id}"
    output.mkdir()
    artifact = render_service().render(
        request(output), job(tmp_path, job_id, JobKind.RENDER)
    )
    if pinned:
        artifact.cut_workspace.set_pinned(True)
    promoted = artifact.cut_workspace.path
    if fallback:
        digest = hashlib.sha256(os.fsencode(str(output.absolute()))).hexdigest()
        legacy_root = cache_root / "workspaces" / digest
    else:
        legacy_root = output / WORKSPACE_NAME
    legacy_cuts = legacy_root / "cuts"
    legacy_cuts.mkdir(parents=True)
    legacy_path = legacy_cuts / promoted.name
    promoted.rename(legacy_path)
    return output, legacy_path, artifact.cut_workspace.cache_key


def _copy_to_legacy(
    tmp_path: Path, cache_root: Path, *, job_id: str
) -> tuple[Path, Path, str, Path]:
    output = tmp_path / f"output-{job_id}"
    output.mkdir()
    artifact = render_service().render(
        request(output), job(tmp_path, job_id, JobKind.RENDER)
    )
    promoted = artifact.cut_workspace.path
    legacy_cuts = output / WORKSPACE_NAME / "cuts"
    legacy_cuts.mkdir(parents=True)
    legacy_path = legacy_cuts / promoted.name
    shutil.copytree(promoted, legacy_path)
    return output, legacy_path, artifact.cut_workspace.cache_key, promoted


def _rewrite_frame(path: Path, color: tuple[int, int, int, int]) -> None:
    temporary = path.with_suffix(".editing")
    Image.new("RGBA", (128, 128), color).save(temporary, format="PNG")
    os.replace(temporary, path)


def _outcome(output: Path) -> migration_module.MigrationOutcome:
    entries = migration_module.find_legacy_cut_sets(output)
    assert len(entries) == 1
    return migration_module.migrate_cut_set(entries[0], cancelled=lambda: False)


def test_legacy_cut_set_is_moved_and_verified(tmp_path: Path, cache_root: Path) -> None:
    output, legacy_path, cache_key = _rendered_set(
        tmp_path, cache_root, job_id="moved"
    )
    source_bytes = {
        path.name: path.read_bytes()
        for path in legacy_path.iterdir()
        if path.is_file()
    }

    result = _outcome(output)

    assert result.status is migration_module.MigrationStatus.MOVED
    target = cut_workspace_root() / "cuts" / legacy_path.name
    assert not legacy_path.exists()
    assert {
        path.name: path.read_bytes()
        for path in target.iterdir()
        if path.is_file()
    } == source_bytes
    reopened = CutWorkspace.open(output, cache_key)
    assert validate_cut_set(reopened).to_json_bytes() == source_bytes["manifest.json"]
    assert result.entry.cache_key == cache_key


def test_migration_fsyncs_each_published_file(
    tmp_path: Path, cache_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output, _legacy_path, _cache_key = _rendered_set(
        tmp_path, cache_root, job_id="fsync"
    )
    actual_fsync_file = migration_module._fsync_file
    synced: list[str] = []

    def record_fsync(path: Path) -> None:
        synced.append(path.name)
        actual_fsync_file(path)

    monkeypatch.setattr(migration_module, "_fsync_file", record_fsync)

    result = _outcome(output)

    assert result.status is migration_module.MigrationStatus.MOVED
    assert synced == ["manifest.json", "frame-000000.png", "frame-000001.png"]


def test_identical_collision_removes_legacy_copy_and_carries_pin(
    tmp_path: Path, cache_root: Path
) -> None:
    output, legacy_path, cache_key, _promoted = _copy_to_legacy(
        tmp_path, cache_root, job_id="identical"
    )
    legacy_manifest = CutManifest.from_json_bytes(
        (legacy_path / "manifest.json").read_bytes()
    )
    (legacy_path / "manifest.json").write_bytes(
        replace(legacy_manifest, pinned=True).to_json_bytes()
    )

    result = _outcome(output)

    assert result.status is migration_module.MigrationStatus.REDUNDANT_REMOVED
    assert not legacy_path.exists()
    target = cut_workspace_root() / "cuts" / legacy_path.name
    assert CutManifest.from_json_bytes(
        (target / "manifest.json").read_bytes()
    ).pinned
    assert result.entry.cache_key == cache_key


def test_different_collision_keeps_both_copies_without_copying(
    tmp_path: Path, cache_root: Path
) -> None:
    output, legacy_path, _cache_key, promoted = _copy_to_legacy(
        tmp_path, cache_root, job_id="different"
    )
    _rewrite_frame(legacy_path / "frame-000000.png", (9, 8, 7, 255))
    target_before = (promoted / "frame-000000.png").read_bytes()

    result = _outcome(output)

    assert result.status is migration_module.MigrationStatus.KEPT_BOTH
    assert legacy_path.exists()
    assert promoted.exists()
    assert (promoted / "frame-000000.png").read_bytes() == target_before
    assert not tuple(
        path
        for path in (cut_workspace_root() / "cuts").iterdir()
        if path.name.startswith(".migrating-")
    )


def test_later_frame_collision_keeps_both_copies(
    tmp_path: Path, cache_root: Path
) -> None:
    output, legacy_path, _cache_key, promoted = _copy_to_legacy(
        tmp_path, cache_root, job_id="different-later"
    )
    _rewrite_frame(legacy_path / "frame-000001.png", (9, 8, 7, 255))
    target_before = (promoted / "frame-000001.png").read_bytes()

    result = _outcome(output)

    assert result.status is migration_module.MigrationStatus.KEPT_BOTH
    assert legacy_path.exists()
    assert (promoted / "frame-000001.png").read_bytes() == target_before


def test_unequal_frame_count_collision_keeps_both_copies(
    tmp_path: Path, cache_root: Path
) -> None:
    output, legacy_path, _cache_key, promoted = _copy_to_legacy(
        tmp_path, cache_root, job_id="different-count"
    )
    legacy_manifest = CutManifest.from_json_bytes(
        (legacy_path / "manifest.json").read_bytes()
    )
    (legacy_path / "frame-000001.png").unlink()
    (legacy_path / "manifest.json").write_bytes(
        replace(legacy_manifest, frames=legacy_manifest.frames[:-1]).to_json_bytes()
    )

    result = _outcome(output)

    assert result.status is migration_module.MigrationStatus.KEPT_BOTH
    assert legacy_path.exists()
    assert (legacy_path / "frame-000001.png").exists() is False
    assert (promoted / "frame-000001.png").exists()


def test_frame_rewritten_between_scan_and_copy_leaves_source_and_no_temp(
    tmp_path: Path, cache_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output, legacy_path, _cache_key = _rendered_set(
        tmp_path, cache_root, job_id="rewritten"
    )
    actual_copy2 = migration_module.shutil.copy2

    def rewrite_before_copy(
        source: object, destination: object, *args: object, **kwargs: object
    ) -> str:
        source_path = Path(source)
        if source_path.name == "frame-000000.png":
            _rewrite_frame(source_path, (1, 2, 3, 255))
        return actual_copy2(source, destination, *args, **kwargs)

    monkeypatch.setattr(migration_module.shutil, "copy2", rewrite_before_copy)
    result = _outcome(output)

    assert result.status is migration_module.MigrationStatus.FAILED
    assert "changed while it was copied" in result.reason
    assert legacy_path.exists()
    assert not tuple(
        path
        for path in (cut_workspace_root() / "cuts").iterdir()
        if path.name.startswith(".migrating-")
    )


def test_edit_after_copy_keeps_source_for_normal_migration(
    tmp_path: Path, cache_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output, legacy_path, cache_key = _rendered_set(
        tmp_path, cache_root, job_id="edited-after-copy"
    )
    original_copy_sidecar = migration_module._copy_sidecar_if_needed

    def edit_after_copy(source_root: Path, target_root: Path, key: str) -> None:
        original_copy_sidecar(source_root, target_root, key)
        _rewrite_frame(legacy_path / "frame-000001.png", (4, 5, 6, 255))

    monkeypatch.setattr(
        migration_module, "_copy_sidecar_if_needed", edit_after_copy
    )
    result = _outcome(output)

    assert result.status is migration_module.MigrationStatus.MOVED_SOURCE_REMAINS
    assert legacy_path.exists()
    assert (legacy_path / "frame-000001.png").read_bytes() != (
        cut_workspace_root() / "cuts" / legacy_path.name / "frame-000001.png"
    ).read_bytes()
    assert validate_cut_set(
        CutWorkspace.open(output, cache_key)
    ).cache_key == cache_key


def test_edit_after_collision_equality_keeps_both_copies(
    tmp_path: Path, cache_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output, legacy_path, _cache_key, promoted = _copy_to_legacy(
        tmp_path, cache_root, job_id="edited-after-collision"
    )
    original_copy_sidecar = migration_module._copy_sidecar_if_needed

    def edit_after_equality(source_root: Path, target_root: Path, key: str) -> None:
        original_copy_sidecar(source_root, target_root, key)
        _rewrite_frame(legacy_path / "frame-000001.png", (4, 5, 6, 255))

    monkeypatch.setattr(
        migration_module, "_copy_sidecar_if_needed", edit_after_equality
    )
    result = _outcome(output)

    assert result.status is migration_module.MigrationStatus.KEPT_BOTH
    assert legacy_path.exists()
    assert promoted.exists()


def test_stale_migration_directory_is_removed_on_next_run(
    tmp_path: Path, cache_root: Path
) -> None:
    output, _legacy_path, _cache_key = _rendered_set(
        tmp_path, cache_root, job_id="stale"
    )
    stale = cut_workspace_root() / "cuts" / ".migrating-stale"
    stale.mkdir(parents=True)
    stale.joinpath("partial").write_bytes(b"partial")

    _outcome(output)

    assert not stale.exists()


def test_no_space_skips_one_set_and_continues_with_the_next(
    tmp_path: Path, cache_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_output, _first_path, _first_key = _rendered_set(
        tmp_path, cache_root, job_id="no-space-first"
    )
    second_output, second_path, _second_key = _rendered_set(
        tmp_path, cache_root, job_id="no-space-second"
    )
    first_entry = migration_module.find_legacy_cut_sets(first_output)[0]
    calls = 0

    def disk_usage(_path: object) -> object:
        nonlocal calls
        calls += 1
        if calls == 1:
            return SimpleNamespace(free=first_entry.size_bytes + 256 * 1024**2 - 1)
        return SimpleNamespace(free=10**12)

    monkeypatch.setattr(migration_module.shutil, "disk_usage", disk_usage)
    results = tuple(
        migration_module.migrate_cut_set(entry, cancelled=lambda: False)
        for entry in (
            *migration_module.find_legacy_cut_sets(first_output),
            *migration_module.find_legacy_cut_sets(second_output),
        )
    )

    assert results[0].status is migration_module.MigrationStatus.NO_SPACE
    assert results[1].status is migration_module.MigrationStatus.MOVED
    assert second_path.exists() is False
    assert (cut_workspace_root() / "cuts" / second_path.name).exists()


def test_no_space_reports_clean_outcome_for_fallback_set(
    tmp_path: Path, cache_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output, legacy_path, _cache_key = _rendered_set(
        tmp_path, cache_root, job_id="no-space-fallback", fallback=True
    )
    entry = migration_module.find_legacy_cut_sets(output)[0]

    def disk_usage(_path: object) -> object:
        return SimpleNamespace(free=entry.size_bytes + 256 * 1024**2 - 1)

    monkeypatch.setattr(migration_module.shutil, "disk_usage", disk_usage)

    result = migration_module.migrate_cut_set(entry, cancelled=lambda: False)

    assert result.status is migration_module.MigrationStatus.NO_SPACE
    assert legacy_path.exists()


def test_pin_carry_over_names_the_published_target(
    tmp_path: Path, cache_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output, legacy_path, _cache_key, promoted = _copy_to_legacy(
        tmp_path, cache_root, job_id="pin-carry-over"
    )
    legacy_manifest = CutManifest.from_json_bytes(
        (legacy_path / "manifest.json").read_bytes()
    )
    (legacy_path / "manifest.json").write_bytes(
        replace(legacy_manifest, pinned=True).to_json_bytes()
    )
    pinned_by_output_directory: dict[Path, bool] = {}
    actual_open = migration_module.CutWorkspace.open

    class _PinProbe:
        def __init__(self, output_directory: Path) -> None:
            self._output_directory = Path(output_directory)

        def set_pinned(self, value: bool) -> None:
            pinned_by_output_directory[self._output_directory] = value

    def probing_open(output_directory: Path, cache_key: str) -> object:
        # Exercise the real open too, so a call-site argument that would
        # actually fail validation still fails the same way it would in
        # production; only the returned handle is swapped for a probe.
        actual_open(output_directory, cache_key)
        return _PinProbe(output_directory)

    monkeypatch.setattr(
        migration_module.CutWorkspace, "open", staticmethod(probing_open)
    )

    result = _outcome(output)

    assert result.status is migration_module.MigrationStatus.REDUNDANT_REMOVED
    assert pinned_by_output_directory.get(promoted) is True
    assert legacy_path not in pinned_by_output_directory


def test_transform_sidecar_travels_with_moved_set(
    tmp_path: Path, cache_root: Path
) -> None:
    output, legacy_path, cache_key = _rendered_set(
        tmp_path, cache_root, job_id="sidecar"
    )
    sidecar = legacy_path.parent / f".transform-{cache_key}.json"
    sidecar.write_bytes(b"transform-sidecar")

    _outcome(output)

    target_sidecar = cut_workspace_root() / "cuts" / sidecar.name
    assert target_sidecar.read_bytes() == b"transform-sidecar"
    assert not sidecar.exists()


def test_interrupted_sidecar_copy_is_atomic_and_retry_arrives(
    tmp_path: Path, cache_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output, legacy_path, cache_key = _rendered_set(
        tmp_path, cache_root, job_id="sidecar-interrupted"
    )
    sidecar = legacy_path.parent / f".transform-{cache_key}.json"
    good_sidecar = b"transform-sidecar"
    sidecar.write_bytes(good_sidecar)
    actual_copy2 = migration_module.shutil.copy2
    target_sidecar = cut_workspace_root() / "cuts" / sidecar.name

    def interrupt_sidecar_copy(
        source: object, destination: object, *args: object, **kwargs: object
    ) -> str:
        if Path(source) == sidecar:
            Path(destination).write_bytes(b"partial")
            raise OSError("interrupted sidecar copy")
        return actual_copy2(source, destination, *args, **kwargs)

    monkeypatch.setattr(migration_module.shutil, "copy2", interrupt_sidecar_copy)
    first = _outcome(output)

    assert first.status is migration_module.MigrationStatus.MOVED_SOURCE_REMAINS
    assert legacy_path.exists()
    assert not target_sidecar.exists()
    assert not tuple(
        path
        for path in target_sidecar.parent.iterdir()
        if path.name.startswith(f".{target_sidecar.name}-")
    )

    monkeypatch.setattr(migration_module.shutil, "copy2", actual_copy2)
    second = _outcome(output)

    assert second.status is migration_module.MigrationStatus.REDUNDANT_REMOVED
    assert target_sidecar.read_bytes() == good_sidecar
    assert not sidecar.exists()


def test_empty_legacy_workspace_root_is_removed_after_migration(
    tmp_path: Path, cache_root: Path
) -> None:
    output, _legacy_path, _cache_key = _rendered_set(
        tmp_path, cache_root, job_id="empty-root"
    )

    _outcome(output)

    assert not (output / WORKSPACE_NAME).exists()


def test_legacy_workspace_root_with_stranger_file_is_preserved(
    tmp_path: Path, cache_root: Path
) -> None:
    output, legacy_path, _cache_key = _rendered_set(
        tmp_path, cache_root, job_id="stranger-file"
    )
    stranger = legacy_path.parent / "stranger.txt"
    stranger.write_bytes(b"keep me")

    _outcome(output)

    assert stranger.exists()
    assert (output / WORKSPACE_NAME).exists()


def test_stranger_file_inside_scratch_is_preserved(
    tmp_path: Path, cache_root: Path
) -> None:
    output, _legacy_path, _cache_key = _rendered_set(
        tmp_path, cache_root, job_id="scratch-stranger"
    )
    scratch = output / WORKSPACE_NAME / "scratch"
    scratch.mkdir()
    stranger = scratch / "notes.txt"
    stranger.write_bytes(b"keep me")

    _outcome(output)

    assert stranger.read_bytes() == b"keep me"
    assert (output / WORKSPACE_NAME).exists()


def test_fallback_workspace_set_migrates_through_copy_path(
    tmp_path: Path, cache_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output, legacy_path, _cache_key = _rendered_set(
        tmp_path, cache_root, job_id="fallback", fallback=True
    )

    actual_copy2 = migration_module.shutil.copy2
    copied_frames: list[str] = []

    def record_frame_copy(
        source: object, destination: object, *args: object, **kwargs: object
    ) -> str:
        if Path(source).name.startswith("frame-"):
            copied_frames.append(Path(source).name)
        return actual_copy2(source, destination, *args, **kwargs)

    monkeypatch.setattr(migration_module.shutil, "copy2", record_frame_copy)
    result = _outcome(output)

    assert result.status is migration_module.MigrationStatus.MOVED
    assert not legacy_path.exists()
    assert (cut_workspace_root() / "cuts" / legacy_path.name).exists()
    assert copied_frames == ["frame-000000.png", "frame-000001.png"]


def test_cancellation_is_observed_between_sets_not_during_a_set(
    tmp_path: Path, cache_root: Path
) -> None:
    first_output, first_path, _first_key = _rendered_set(
        tmp_path, cache_root, job_id="cancel-first"
    )
    second_output, second_path, _second_key = _rendered_set(
        tmp_path, cache_root, job_id="cancel-second"
    )
    entries = (
        migration_module.find_legacy_cut_sets(first_output)[0],
        migration_module.find_legacy_cut_sets(second_output)[0],
    )
    cancellation_checks = 0

    def cancel_before_second_set() -> bool:
        nonlocal cancellation_checks
        cancellation_checks += 1
        return cancellation_checks == 2

    results = tuple(
        migration_module.migrate_cut_set(entry, cancelled=cancel_before_second_set)
        for entry in entries
    )

    assert results[0].status is migration_module.MigrationStatus.MOVED
    assert results[1].status is migration_module.MigrationStatus.CANCELLED
    assert cancellation_checks == 2
    assert not first_path.exists()
    assert second_path.exists()


def test_unreadable_legacy_set_is_reported_and_never_deleted(
    tmp_path: Path, cache_root: Path
) -> None:
    output = tmp_path / "output-unreadable"
    output.mkdir()
    legacy_cuts = output / WORKSPACE_NAME / "cuts"
    legacy_path = legacy_cuts / "broken-abcdef12"
    legacy_path.mkdir(parents=True)
    (legacy_path / "manifest.json").write_text("not-json", encoding="utf-8")

    entries = migration_module.find_legacy_cut_sets(output)
    result = migration_module.migrate_cut_set(entries[0], cancelled=lambda: False)

    assert result.status is migration_module.MigrationStatus.UNREADABLE
    assert "manifest" in result.reason
    assert legacy_path.exists()
    assert not (cut_workspace_root() / "cuts" / legacy_path.name).exists()
