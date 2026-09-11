"""Velopack integration helpers for the in-app update flow."""

from __future__ import annotations

import logging
import sys
from pathlib import Path, PurePath, PureWindowsPath
from typing import Any, Protocol, cast

from matteloop.paths import cache_subdirectory
from matteloop.ui.update_paths import _physical_executable_path
from matteloop.updates import update_repository_url

_LOGGER = logging.getLogger(__name__)


class UpdateManager(Protocol):
    def get_current_version(self) -> str: ...

    def get_update_pending_restart(self) -> object | None: ...

    def wait_exit_then_apply_updates(
        self,
        update: object,
        silent: bool = False,
        restart: bool = True,
        restart_args: object | None = None,
    ) -> None: ...


def _create_update_manager() -> UpdateManager | None:
    """Construct Velopack with the bundle paths this project owns."""
    try:
        from velopack import UpdateManager as VelopackUpdateManager
        from velopack import VelopackLocatorConfig

        locator = _locator_config(_physical_executable_path(), VelopackLocatorConfig)
        if not locator.UpdateExePath.exists() or not locator.ManifestPath.exists():
            return None
        return cast(
            UpdateManager,
            VelopackUpdateManager(update_repository_url(), locator=locator),
        )
    except Exception as error:
        _LOGGER.warning("MatteLoop self-update unavailable: %s", error)
        return None


def _pending_update(manager: UpdateManager | None) -> object | None:
    if manager is None:
        return None
    try:
        pending = manager.get_update_pending_restart()
        _sweep_packages(pending)
        return pending
    except Exception as error:
        _LOGGER.warning("MatteLoop pending update could not be read: %s", error)
        return None


def _locator_config(executable: PurePath, config_type: Any) -> Any:
    """Describe this installation's layout instead of letting Velopack guess."""
    packages = cache_subdirectory("updates")
    # Velopack probes the directory by writing into it, and silently falls back
    # to its own location when that fails — including when the directory simply
    # does not exist yet. It would then look for the package somewhere this
    # application never writes.
    packages.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        root = executable.parents[2]
        return config_type(
            root,
            root / "Contents" / "MacOS" / "UpdateMac",
            packages,
            root / "Contents" / "Resources" / "sq.version",
            root / "Contents" / "MacOS",
            False,
        )
    if sys.platform == "win32":
        windows_executable = executable
        if not isinstance(windows_executable, (Path, PureWindowsPath)):
            windows_executable = PureWindowsPath(str(executable))
        current = windows_executable.parent
        windows_root = current.parent
        return config_type(
            windows_root,
            windows_root / "Update.exe",
            packages,
            current / "sq.version",
            current,
            Path(windows_root / ".portable").exists(),
        )
    raise RuntimeError("Velopack updates are only supported on macOS and Windows")


def _sweep_packages(pending: object | None) -> None:
    directory = cache_subdirectory("updates")
    if not directory.exists():
        return
    pending_filename = _update_filename(pending)
    for pattern in ("*.nupkg", "*.nupkg.part"):
        for package in directory.glob(pattern):
            if package.name == pending_filename:
                continue
            try:
                package.unlink()
            except OSError as error:
                _LOGGER.info(
                    "Could not remove stale update package %s: %s", package, error
                )


def _update_filename(update: object | None) -> str | None:
    if update is None:
        return None
    asset = getattr(update, "TargetFullRelease", update)
    filename = getattr(asset, "FileName", None)
    return filename if isinstance(filename, str) else None


def _current_update_version(manager: UpdateManager | None) -> str | None:
    """Use Velopack's version when available, not the numeric bundle version."""
    if manager is None:
        return None
    try:
        version = manager.get_current_version()
    except Exception as error:
        _LOGGER.info("Velopack current version could not be read: %s", error)
        return None
    return version if isinstance(version, str) else None
