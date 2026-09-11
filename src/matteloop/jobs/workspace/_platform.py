from __future__ import annotations

from typing import TYPE_CHECKING

from platformdirs import user_cache_dir

from matteloop.paths import cut_workspace_root

# ruff: noqa: F403,F405
from ._common import *  # noqa: F403,F401

if TYPE_CHECKING:
    from ._errors import _unsafe_error
    from ._filesystem import _BoundDirectory
    from ._manifest_validation import _validate_path_value

__all__ = (
    "WorkspaceLayout",
    "_assert_safe_directory",
    "_canonical_output_directory",
    "_create_fallback_workspace",
    "_durable_workspace_root",
    "_workspace_layout",
    "user_cache_dir",
)


@dataclass(frozen=True, slots=True)
class WorkspaceLayout:
    """Workspace paths."""

    output_directory: Path
    workspace_root: Path
    cuts_root: Path
    scratch_root: Path

    def __iter__(self) -> Iterator[Path]:
        """Keep the former four-value unpacking contract for existing callers."""
        yield self.output_directory
        yield self.workspace_root
        yield self.cuts_root
        yield self.scratch_root


def _workspace_layout(
    output_directory: Path, *, create: bool
) -> WorkspaceLayout:
    output = _canonical_output_directory(output_directory)
    root = cut_workspace_root()
    cuts = root / "cuts"
    scratch = root / "scratch"
    if create:
        _create_fallback_workspace(root, cuts, scratch)
    elif root.exists():
        _assert_safe_directory(root)
        if cuts.exists():
            _assert_safe_directory(cuts)
        if scratch.exists():
            _assert_safe_directory(scratch)
    return WorkspaceLayout(output, root, cuts, scratch)


def _durable_workspace_root() -> Path:
    """Return the shared cache workspace for durable cut sets."""
    return cut_workspace_root()


def _create_fallback_workspace(root: Path, cuts: Path, scratch: Path) -> None:
    try:
        root.mkdir(parents=True, exist_ok=True)
        cuts.mkdir(exist_ok=True)
        scratch.mkdir(exist_ok=True)
        _assert_safe_directory(root)
        _assert_safe_directory(cuts)
        _assert_safe_directory(scratch)
    except AppError:
        raise
    except OSError as error:
        raise _unsafe_error(
            f"cannot create local fallback workspace directory: {error}"
        ) from error


def _canonical_output_directory(path: Path) -> Path:
    if not isinstance(path, Path):
        raise _unsafe_error("output directory must be a Path")
    _validate_path_value(path)
    if ".." in path.parts:
        raise _unsafe_error("output directory traversal is not allowed")
    absolute = Path(os.path.abspath(path))
    try:
        with _BoundDirectory.open(absolute):
            pass
    except OSError as error:
        raise _unsafe_error("output directory must be an existing directory") from error
    return absolute


def _assert_safe_directory(path: Path) -> None:
    try:
        with _BoundDirectory.open(path):
            pass
    except AppError:
        raise
    except OSError as error:
        raise _unsafe_error(f"workspace directory is unavailable: {error}") from error
