"""Still-frame extraction for `scripts/benchmark_models.py`'s HTML report.

Kept separate from `benchmark_models.py` for the same reason
`benchmark_report.py` is: that module stays comfortably under the size that
would call for a package split. This module only reads already-rendered
WebPs -- it never renders or downloads anything -- so it extracts frame 0 of
each model's animated WebP as a still PNG (the report's
IntersectionObserver fallback, since an animated WebP cannot be paused via
the DOM) and can rebuild those stills for `--report-only` without a runtime.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from PIL import Image

if TYPE_CHECKING:
    from scripts.benchmark_models import BenchmarkRun, ModelResult


def extract_still_frame(webp_path: Path, still_path: Path) -> bool:
    """Extract frame 0 of `webp_path` as an RGBA PNG at `still_path`.

    Returns ``False`` without raising on any failure -- a corrupt or
    unreadable WebP degrades that one tile to "always animated" rather than
    aborting the run or the report (docs/engineering-guardrails.md G4).
    """
    try:
        with Image.open(webp_path) as image:
            image.seek(0)
            image.convert("RGBA").save(still_path, format="PNG")
    except Exception:  # noqa: BLE001 - degrades this one tile, see docstring
        return False
    return True


def still_relative_path(model_id: str) -> str:
    return f"{model_id}-still.png"


def write_still_frame(
    out_dir: Path, model_id: str, log: Callable[[str], None]
) -> str | None:
    """Write `model_id`'s still frame next to its already-rendered WebP.

    Shared by a normal run (`benchmark_models._run_one_model`) and
    `--report-only` (`rebuild_stills`), which both start from a finished
    WebP already on disk and want the same output file.
    """
    relative = still_relative_path(model_id)
    if extract_still_frame(out_dir / f"{model_id}.webp", out_dir / relative):
        return relative
    log(f"  {model_id}: could not extract a still frame; tile stays animated")
    return None


def rebuild_stills(
    out_dir: Path, run: BenchmarkRun, log: Callable[[str], None] = print
) -> BenchmarkRun:
    """Re-extract every ok model's still frame from its WebP in `out_dir`.

    Returns `run` with `still_relative_path` refreshed. Only reads the
    WebPs (via `extract_still_frame`), so this can be run repeatedly
    against the same directory without touching the renders.
    """

    def _refreshed(result: ModelResult) -> ModelResult:
        if result.status != "ok":
            return result
        still = write_still_frame(out_dir, result.model_id, log)
        return replace(result, still_relative_path=still)

    return replace(run, results=tuple(_refreshed(result) for result in run.results))
