"""Product identity and paths for user-owned data."""

from __future__ import annotations

from pathlib import Path

from platformdirs import user_cache_dir

PRODUCT_NAME = "MatteLoop"
PACKAGE_NAME = "matteloop"
CACHE_NAME = "matteloop"
# Legacy workspace directory name under old output folders; retained for the
# migration that offers to move those sets into the cache.
WORKSPACE_NAME = ".matteloop-work"


def cache_subdirectory(*parts: str) -> Path:
    """Return a cache directory under MatteLoop's user cache."""
    return Path(user_cache_dir(CACHE_NAME)).joinpath(*parts)


def model_cache_root() -> Path:
    """Return MatteLoop's model cache."""
    return cache_subdirectory("models")


def cut_workspace_root() -> Path:
    """Return MatteLoop's durable cut-workspace root."""
    return cache_subdirectory("workspace")
