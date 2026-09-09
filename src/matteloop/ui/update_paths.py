"""Filesystem and version helpers for the in-app update controller."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path, PurePath


def _physical_executable_path() -> PurePath:
    """Resolve aliases before deriving the install root for advisory checks."""
    reported: PurePath = Path(sys.executable)
    return (
        reported.resolve()
        if _is_frozen_runtime() and isinstance(reported, Path)
        else reported
    )


def _install_root(executable: PurePath) -> PurePath:
    if sys.platform == "darwin":
        for parent in (executable, *executable.parents):
            if parent.name.endswith(".app"):
                return parent
    if executable.parent.name.lower() == "current":
        return executable.parent.parent
    return executable.parent


def _install_root_is_writable(executable: PurePath) -> bool:
    root = _install_root(executable)
    if sys.platform == "win32":
        try:
            with tempfile.TemporaryFile(dir=str(root)):
                return True
        except OSError:
            return False
    if sys.platform == "darwin":
        return os.access(root, os.W_OK) and os.access(root.parent, os.W_OK)
    return os.access(root, os.W_OK)


def _advisory_update_capability(manager: object | None) -> bool:
    """Select download only when the few known replacement blockers are absent."""
    if manager is None:
        return False
    executable = _physical_executable_path()
    if "/AppTranslocation/" in executable.as_posix():
        return False
    return _install_root_is_writable(executable)


def _update_version(update: object | None, fallback: str | None) -> str | None:
    if update is None:
        return fallback
    asset = getattr(update, "TargetFullRelease", update)
    version = getattr(asset, "Version", fallback)
    return version if isinstance(version, str) else fallback


def _is_frozen_runtime() -> bool:
    return getattr(sys, "frozen", False) or globals().get("__compiled__") is not None
