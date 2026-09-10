"""Offer-only migration of cut sets left in legacy workspace locations.

This module stays beside the workspace package because legacy directories are
not valid ``CutWorkspace`` instances: opening one would reject its old root.
The migration reads and validates those paths through the package's existing
path-based helpers, and only removes a source after a new cache copy has been
verified.
"""

from __future__ import annotations

import errno
import hashlib
import os
import shutil
import stat
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final, Protocol

from matteloop.jobs.workspace import (  # type: ignore[attr-defined]
    _CACHE_KEY_RE,
    _READABLE_WORKSPACE_NAME_RE,
    CutWorkspace,
    _existing_promoted_path,
    _fsync_directory,
    _read_manifest,
    _remove_tree,
    _scan_cut_set,
)
from matteloop.paths import WORKSPACE_NAME, cache_subdirectory, cut_workspace_root

_MANIFEST_FILENAME: Final = "manifest.json"
_MAX_WORKSPACE_ENTRIES: Final = 10_000
_MIGRATION_HEADROOM_BYTES: Final = 256 * 1024**2


class _FrameRecord(Protocol):
    @property
    def filename(self) -> str: ...

    @property
    def sha256(self) -> str: ...


class _ManifestRecord(Protocol):
    @property
    def cache_key(self) -> str: ...

    @property
    def pinned(self) -> bool: ...


class _LegacySource(StrEnum):
    OUTPUT = "output"
    FALLBACK = "fallback"


class MigrationStatus(StrEnum):
    MOVED = "moved"
    REDUNDANT_REMOVED = "redundant-removed"
    KEPT_BOTH = "kept-both"
    NO_SPACE = "no-space"
    FAILED = "failed"
    MOVED_SOURCE_REMAINS = "moved-source-remains"
    UNREADABLE = "unreadable"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class LegacyCutSet:
    """A candidate set found below one of the two legacy roots."""

    path: Path
    cache_key: str
    name: str
    size_bytes: int
    pinned: bool
    source_path: str
    source_root: Path
    source_kind: _LegacySource = _LegacySource.OUTPUT
    unreadable_reason: str | None = None


@dataclass(frozen=True, slots=True)
class MigrationOutcome:
    """The result of one complete or skipped migration attempt."""

    entry: LegacyCutSet
    status: MigrationStatus
    reason: str = ""


@dataclass(frozen=True, slots=True)
class MigrationReport:
    """Results emitted after the worker has processed its discovered sets."""

    outcomes: tuple[MigrationOutcome, ...]

    @property
    def moved_count(self) -> int:
        return sum(
            outcome.status
            in {MigrationStatus.MOVED, MigrationStatus.REDUNDANT_REMOVED}
            for outcome in self.outcomes
        )

    @property
    def remaining(self) -> tuple[MigrationOutcome, ...]:
        return tuple(
            outcome
            for outcome in self.outcomes
            if outcome.status
            not in {MigrationStatus.MOVED, MigrationStatus.REDUNDANT_REMOVED}
        )


def find_legacy_cut_sets(output_directory: Path) -> tuple[LegacyCutSet, ...]:
    """Discover readable and unreadable sets below both legacy roots."""
    output = Path(os.path.abspath(output_directory))
    entries: list[LegacyCutSet] = []
    for root, source_kind in _legacy_cut_roots(output):
        entries.extend(_discover_root(root, source_kind))
    return tuple(entries)


def migrate_cut_set(
    entry: LegacyCutSet, *, cancelled: Callable[[], bool]
) -> MigrationOutcome:
    """Move one legacy set, preserving the source until the target is verified."""
    if not callable(cancelled):
        raise TypeError("cancelled must be callable")
    cache_cuts = cut_workspace_root() / "cuts"
    try:
        cache_cuts.mkdir(parents=True, exist_ok=True)
        _remove_stale_migrations(cache_cuts)
    except Exception as error:
        return MigrationOutcome(entry, MigrationStatus.FAILED, str(error))
    if cancelled():
        return MigrationOutcome(entry, MigrationStatus.CANCELLED)

    try:
        manifest, identity = _read_manifest(entry.path)
    except Exception as error:
        return MigrationOutcome(entry, MigrationStatus.UNREADABLE, str(error))
    try:
        source_frames, _identities = _scan_cut_set(
            entry.path, manifest, identity, compare_recorded=False
        )
    except Exception as error:
        return MigrationOutcome(entry, MigrationStatus.FAILED, str(error))

    cache_key = manifest.cache_key
    target = _existing_promoted_path(cache_cuts, cache_key)
    if target.exists():
        collision = _resolve_collision(entry, manifest, source_frames, target)
        if collision is not None:
            return collision
    if entry.source_kind is _LegacySource.OUTPUT:
        try:
            no_space = _no_space(cache_cuts, entry)
        except OSError as error:
            return MigrationOutcome(entry, MigrationStatus.FAILED, str(error))
        if no_space:
            return MigrationOutcome(entry, MigrationStatus.NO_SPACE)

    temporary = cache_cuts / f".migrating-{cache_key}-{uuid.uuid4().hex}"
    if entry.source_kind is _LegacySource.FALLBACK:
        return _rename_or_copy_fallback(
            entry, manifest, source_frames, temporary, cache_cuts
        )
    return _copy_verify_and_publish(
        entry, manifest, source_frames, temporary, cache_cuts
    )


def _legacy_cut_roots(output: Path) -> tuple[tuple[Path, _LegacySource], ...]:
    digest = hashlib.sha256(os.fsencode(str(output))).hexdigest()
    return (
        (output / WORKSPACE_NAME / "cuts", _LegacySource.OUTPUT),
        (
            cache_subdirectory("workspaces", digest) / "cuts",
            _LegacySource.FALLBACK,
        ),
    )


def _discover_root(root: Path, source_kind: _LegacySource) -> list[LegacyCutSet]:
    try:
        root_info = root.lstat()
    except FileNotFoundError:
        return []
    except OSError:
        return []
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        return []
    try:
        candidates = sorted(root.iterdir(), key=lambda path: path.name)
    except OSError:
        return []
    entries: list[LegacyCutSet] = []
    for candidate in candidates:
        if candidate.name.startswith(".") or not _is_workspace_directory(candidate):
            continue
        if not (
            _CACHE_KEY_RE.fullmatch(candidate.name)
            or _READABLE_WORKSPACE_NAME_RE.fullmatch(candidate.name)
        ):
            continue
        if len(entries) >= _MAX_WORKSPACE_ENTRIES:
            raise OSError("legacy workspace namespace exceeds the listing bound")
        entries.append(_describe_candidate(candidate, root, source_kind))
    return entries


def _is_workspace_directory(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode)


def _describe_candidate(
    path: Path, root: Path, source_kind: _LegacySource
) -> LegacyCutSet:
    try:
        manifest, _identity = _read_manifest(path)
    except Exception as error:
        return LegacyCutSet(
            path,
            "",
            path.name,
            0,
            False,
            "",
            root,
            source_kind,
            str(error),
        )
    size_bytes = sum(frame.size_bytes for frame in manifest.frames)
    size_bytes += len(manifest.to_json_bytes())
    return LegacyCutSet(
        path,
        manifest.cache_key,
        path.name,
        size_bytes,
        manifest.pinned,
        manifest.source_path,
        root,
        source_kind,
    )


def _remove_stale_migrations(cache_cuts: Path) -> None:
    try:
        candidates = tuple(cache_cuts.iterdir())
    except FileNotFoundError:
        return
    for candidate in candidates:
        if candidate.name.startswith(".migrating-") and _is_workspace_directory(
            candidate
        ):
            _remove_tree(candidate)


def _resolve_collision(
    entry: LegacyCutSet,
    manifest: _ManifestRecord,
    source_frames: tuple[_FrameRecord, ...],
    target: Path,
) -> MigrationOutcome | None:
    try:
        target_manifest, target_identity = _read_manifest(target)
        target_frames, _identities = _scan_cut_set(
            target, target_manifest, target_identity, compare_recorded=False
        )
    except Exception as error:
        return MigrationOutcome(entry, MigrationStatus.FAILED, str(error))
    source_hashes = tuple(
        (frame.filename, frame.sha256) for frame in source_frames
    )
    target_hashes = tuple(
        (frame.filename, frame.sha256) for frame in target_frames
    )
    if source_hashes != target_hashes:
        return MigrationOutcome(
            entry,
            MigrationStatus.KEPT_BOTH,
            "already in the cache with different frames",
        )
    if manifest.pinned and not target_manifest.pinned:
        try:
            CutWorkspace.open(entry.path, target_manifest.cache_key).set_pinned(True)
        except Exception as error:
            return MigrationOutcome(entry, MigrationStatus.FAILED, str(error))
    try:
        _copy_sidecar_if_needed(
            entry.source_root, target.parent, target_manifest.cache_key
        )
        _remove_source(entry, manifest.cache_key)
    except Exception as error:
        return MigrationOutcome(entry, MigrationStatus.MOVED_SOURCE_REMAINS, str(error))
    return MigrationOutcome(entry, MigrationStatus.REDUNDANT_REMOVED)


def _no_space(cache_cuts: Path, entry: LegacyCutSet) -> bool:
    usage = shutil.disk_usage(cache_cuts)
    return usage.free < entry.size_bytes + _MIGRATION_HEADROOM_BYTES


def _rename_or_copy_fallback(
    entry: LegacyCutSet,
    manifest: _ManifestRecord,
    source_frames: tuple[_FrameRecord, ...],
    temporary: Path,
    cache_cuts: Path,
) -> MigrationOutcome:
    try:
        os.rename(entry.path, temporary)
    except OSError as error:
        if error.errno != errno.EXDEV:
            return MigrationOutcome(entry, MigrationStatus.FAILED, str(error))
        return _copy_verify_and_publish(
            entry, manifest, source_frames, temporary, cache_cuts
        )
    return _publish_temporary(entry, manifest, temporary, cache_cuts, renamed=True)


def _copy_verify_and_publish(
    entry: LegacyCutSet,
    manifest: _ManifestRecord,
    source_frames: tuple[_FrameRecord, ...],
    temporary: Path,
    cache_cuts: Path,
) -> MigrationOutcome:
    try:
        temporary.mkdir()
        shutil.copy2(entry.path / _MANIFEST_FILENAME, temporary / _MANIFEST_FILENAME)
        for frame in source_frames:
            shutil.copy2(entry.path / frame.filename, temporary / frame.filename)
        _fsync_directory(temporary)
    except Exception as error:
        return MigrationOutcome(entry, MigrationStatus.FAILED, str(error))
    try:
        copied_manifest, copied_identity = _read_manifest(temporary)
        copied_frames, _identities = _scan_cut_set(
            temporary, copied_manifest, copied_identity, compare_recorded=False
        )
        if copied_frames != source_frames:
            raise ValueError("changed while it was copied")
    except Exception as error:
        try:
            _remove_tree(temporary)
        except OSError:
            pass
        return MigrationOutcome(entry, MigrationStatus.FAILED, str(error))
    return _publish_temporary(entry, manifest, temporary, cache_cuts, renamed=False)


def _publish_temporary(
    entry: LegacyCutSet,
    manifest: _ManifestRecord,
    temporary: Path,
    cache_cuts: Path,
    *,
    renamed: bool,
) -> MigrationOutcome:
    target = cache_cuts / entry.name
    if target.exists():
        if renamed:
            restore_error = _restore_renamed_source(entry, temporary)
            if restore_error is not None:
                return MigrationOutcome(
                    entry, MigrationStatus.MOVED_SOURCE_REMAINS, restore_error
                )
        return MigrationOutcome(
            entry,
            MigrationStatus.KEPT_BOTH,
            "the cache path already exists",
        )
    try:
        os.rename(temporary, target)
    except OSError as error:
        if renamed:
            restore_error = _restore_renamed_source(entry, temporary)
            if restore_error is not None:
                return MigrationOutcome(
                    entry,
                    MigrationStatus.MOVED_SOURCE_REMAINS,
                    restore_error,
                )
        return MigrationOutcome(entry, MigrationStatus.FAILED, str(error))
    try:
        _fsync_directory(cache_cuts)
        _copy_sidecar_if_needed(entry.source_root, cache_cuts, manifest.cache_key)
        if not renamed:
            _remove_source(entry, manifest.cache_key)
        else:
            _remove_source_sidecar(entry.source_root, manifest.cache_key)
        if entry.source_kind is _LegacySource.OUTPUT:
            _cleanup_legacy_root(entry.source_root)
    except Exception as error:
        return MigrationOutcome(entry, MigrationStatus.MOVED_SOURCE_REMAINS, str(error))
    return MigrationOutcome(entry, MigrationStatus.MOVED)


def _restore_renamed_source(entry: LegacyCutSet, temporary: Path) -> str | None:
    try:
        os.rename(temporary, entry.path)
    except OSError as error:
        return f"could not restore the source after publication failed: {error}"
    return None


def _copy_sidecar_if_needed(
    source_root: Path, target_root: Path, cache_key: str
) -> None:
    source = _sidecar(source_root, cache_key)
    target = _sidecar(target_root, cache_key)
    if target.exists() or not source.exists():
        return
    shutil.copy2(source, target)
    _fsync_directory(target_root)


def _remove_source(entry: LegacyCutSet, cache_key: str) -> None:
    _remove_tree(entry.path)
    _remove_source_sidecar(entry.source_root, cache_key)


def _remove_source_sidecar(source_root: Path, cache_key: str) -> None:
    if not cache_key:
        return
    try:
        _sidecar(source_root, cache_key).unlink()
    except FileNotFoundError:
        pass


def _sidecar(root: Path, cache_key: str) -> Path:
    return root / f".transform-{cache_key}.json"


def _cleanup_legacy_root(cuts_root: Path) -> None:
    try:
        cuts_root.rmdir()
    except OSError:
        pass
    scratch = cuts_root.parent / "scratch"
    if scratch.exists():
        _remove_tree(scratch)
    try:
        cuts_root.parent.rmdir()
    except OSError:
        pass
