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
from matteloop.jobs.workspace import CutManifest
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

    result = _outcome(output)

    assert result.status is migration_module.MigrationStatus.MOVED
    target = cut_workspace_root() / "cuts" / legacy_path.name
    assert target.exists()
    assert not legacy_path.exists()
    assert target.joinpath("manifest.json").is_file()
    assert target.joinpath("frame-000000.png").is_file()
    assert result.entry.cache_key == cache_key


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


def test_fallback_workspace_set_is_renamed_without_copying_frames(
    tmp_path: Path, cache_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output, legacy_path, _cache_key = _rendered_set(
        tmp_path, cache_root, job_id="fallback", fallback=True
    )

    def reject_frame_copy(
        source: object, destination: object, *args: object, **kwargs: object
    ) -> str:
        if Path(source).name.startswith("frame-"):
            raise AssertionError("fallback migration copied a frame")
        return shutil.copy2(source, destination, *args, **kwargs)

    monkeypatch.setattr(migration_module.shutil, "copy2", reject_frame_copy)
    result = _outcome(output)

    assert result.status is migration_module.MigrationStatus.MOVED
    assert not legacy_path.exists()
    assert (cut_workspace_root() / "cuts" / legacy_path.name).exists()


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
