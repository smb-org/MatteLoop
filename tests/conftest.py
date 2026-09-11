from __future__ import annotations

from pathlib import Path

import pytest

import matteloop.paths as paths_module


@pytest.fixture(autouse=True)
def cache_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "user-cache"
    monkeypatch.setattr(paths_module, "user_cache_dir", lambda _app: str(root))
    return root
