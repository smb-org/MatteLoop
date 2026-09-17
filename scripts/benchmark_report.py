"""Self-contained HTML report for a `scripts/benchmark_models.py` run.

Kept separate from `benchmark_models.py` so that module stays comfortably
under the size that would call for a package split. This file only turns
already-computed results into one static page: inline CSS and JavaScript,
relative image paths, no external resources, so the report keeps working if
the output directory is copied or zipped elsewhere.
"""

from __future__ import annotations

from collections.abc import Sequence
from html import escape
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scripts.benchmark_models import BenchmarkRun, ModelResult

_BACKGROUNDS = ("checkerboard", "black", "white", "green")
_SORT_OPTIONS = (
    ("render", "Render time (fastest first)"),
    ("name", "Model name"),
    ("size", "File size"),
)

_CSS = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body {
  margin: 0;
  font-family: -apple-system, "Segoe UI", Roboto, sans-serif;
  background: #14161a;
  color: #f2f2f2;
}
header {
  position: sticky;
  top: 0;
  z-index: 2;
  padding: 12px 16px;
  background: #1e2128;
  border-bottom: 1px solid #2a2e37;
  display: flex;
  align-items: center;
  gap: 16px;
  flex-wrap: wrap;
}
h1 { font-size: 16px; margin: 0; }
.controls { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.meta { color: #9aa0aa; font-size: 12px; }
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: 16px;
  padding: 16px;
}
.tile {
  background: #1e2128;
  border: 1px solid #2a2e37;
  border-radius: 8px;
  padding: 12px;
}
.tile-error { border-color: #a33; }
.image-wrap {
  aspect-ratio: 4 / 3;
  border-radius: 4px;
  overflow: hidden;
  display: flex;
  align-items: center;
  justify-content: center;
  background-color: #808080;
}
.grid.bg-checkerboard .image-wrap {
  background-image:
    linear-gradient(45deg, #666 25%, transparent 25%),
    linear-gradient(-45deg, #666 25%, transparent 25%),
    linear-gradient(45deg, transparent 75%, #666 75%),
    linear-gradient(-45deg, transparent 75%, #666 75%);
  background-size: 20px 20px;
  background-position: 0 0, 0 10px, 10px -10px, -10px 0px;
  background-color: #999;
}
.grid.bg-black .image-wrap { background-color: #000; }
.grid.bg-white .image-wrap { background-color: #fff; }
.grid.bg-green .image-wrap { background-color: #0f0; }
.result-image { max-width: 100%; max-height: 100%; }
.model-id { color: #9aa0aa; font-size: 12px; margin: 2px 0 8px; }
.stats {
  display: grid;
  grid-template-columns: auto auto;
  gap: 2px 8px;
  font-size: 12px;
  margin: 8px 0 0;
}
.stats dt { color: #9aa0aa; }
.stats dd { margin: 0; }
.error { color: #f88; font-size: 13px; }
.warnings {
  margin: 0;
  padding: 8px 16px;
  background: #3a2f14;
  color: #f0c674;
  font-size: 12px;
}
.warnings ul { margin: 4px 0 0; padding-left: 18px; }
.cache-hit { color: #f0c674; }
button, select {
  font: inherit;
  background: #14161a;
  color: #f2f2f2;
  border: 1px solid #2a2e37;
  border-radius: 4px;
  padding: 4px 8px;
}
"""

_SCRIPT = """
(function () {
  var grid = document.getElementById("grid");
  var bgSelect = document.getElementById("bg-select");
  var sortSelect = document.getElementById("sort-select");
  var restartButton = document.getElementById("restart-all");

  function applyBackground() {
    grid.className = "grid bg-" + bgSelect.value;
  }

  function sortValue(tile, key) {
    if (key === "name") {
      return (tile.getAttribute("data-model") || "").toLowerCase();
    }
    var attribute = key === "size" ? "data-file-size" : "data-render-seconds";
    if (!tile.hasAttribute(attribute)) {
      // No measurement (a failed model) always sorts last, regardless of
      // which numeric column is selected.
      return Infinity;
    }
    return parseFloat(tile.getAttribute(attribute));
  }

  function applySort() {
    var key = sortSelect.value;
    var tiles = Array.prototype.slice.call(grid.querySelectorAll(".tile"));
    tiles.sort(function (a, b) {
      var va = sortValue(a, key);
      var vb = sortValue(b, key);
      if (va < vb) {
        return -1;
      }
      if (va > vb) {
        return 1;
      }
      return 0;
    });
    // Reorders the existing tile nodes in place -- no re-rendering.
    tiles.forEach(function (tile) {
      grid.appendChild(tile);
    });
  }

  function restartAll() {
    // Preload every image off-DOM first, then swap all visible <img> tags
    // to the reloaded sources together, so the animations start back in
    // step (best effort -- network timing can still drift once playing).
    var stamp = Date.now();
    var images = Array.prototype.slice.call(
      grid.querySelectorAll("img.result-image")
    );
    if (images.length === 0) {
      return;
    }
    var remaining = images.length;
    var settle = function () {
      remaining -= 1;
      if (remaining === 0) {
        images.forEach(function (img) {
          img.src = img.getAttribute("data-restart-src");
        });
      }
    };
    images.forEach(function (img) {
      var url = img.getAttribute("data-src") + "?t=" + stamp;
      img.setAttribute("data-restart-src", url);
      var preload = new Image();
      preload.onload = settle;
      preload.onerror = settle;
      preload.src = url;
    });
  }

  bgSelect.addEventListener("change", applyBackground);
  sortSelect.addEventListener("change", applySort);
  restartButton.addEventListener("click", restartAll);
  applyBackground();
})();
"""


def sort_by_render_time(results: Sequence[ModelResult]) -> list[ModelResult]:
    """Sort `results` by render time ascending, failed models last.

    Shared by this module's server-side tile order -- so the no-JS view
    already matches the report's default sort -- and by the console summary
    table in `benchmark_models.py`, which reuses this instead of sorting a
    second way.
    """

    def key(result: ModelResult) -> tuple[int, float]:
        if result.status != "ok" or result.render_seconds is None:
            return (1, 0.0)
        return (0, result.render_seconds)

    return sorted(results, key=key)


def render_report_html(run: BenchmarkRun) -> str:
    """Render the full self-contained results page for one benchmark run."""
    tiles = "\n".join(_tile_html(result) for result in sort_by_render_time(run.results))
    header = (
        "<h1>MatteLoop model benchmark</h1>"
        '<div class="controls">'
        "<label>Sort "
        f'<select id="sort-select">{_sort_options_html()}</select>'
        "</label>"
        "<label>Background "
        f'<select id="bg-select">{_background_options_html()}</select>'
        "</label>"
        '<button id="restart-all" type="button">Restart all</button>'
        f'<span class="meta">Provider: {escape(run.provider)}'
        f" &middot; Generated {escape(run.generated_at)}</span>"
        "</div>"
    )
    warnings = _warnings_html(run.warnings)
    return (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n<meta charset="utf-8" />\n'
        "<title>MatteLoop model benchmark</title>\n"
        f"<style>{_CSS}</style>\n</head>\n<body>\n"
        f"<header>{header}</header>\n"
        f"{warnings}"
        f'<main class="grid" id="grid">\n{tiles}\n</main>\n'
        f"<script>{_SCRIPT}</script>\n</body>\n</html>\n"
    )


def _warnings_html(warnings: tuple[str, ...]) -> str:
    if not warnings:
        return ""
    items = "".join(f"<li>{escape(warning)}</li>" for warning in warnings)
    return f'<div class="warnings">Warnings<ul>{items}</ul></div>\n'


def _background_options_html() -> str:
    options = []
    for index, name in enumerate(_BACKGROUNDS):
        selected = ' selected="selected"' if index == 0 else ""
        options.append(f'<option value="{name}"{selected}>{name.capitalize()}</option>')
    return "".join(options)


def _sort_options_html() -> str:
    options = []
    for index, (value, label) in enumerate(_SORT_OPTIONS):
        selected = ' selected="selected"' if index == 0 else ""
        options.append(f'<option value="{value}"{selected}>{escape(label)}</option>')
    return "".join(options)


def _sort_attributes_html(result: ModelResult) -> str:
    """`data-render-seconds`/`data-file-size` for the report's sort control.

    Left out when the underlying value is unknown -- a failed model has no
    render time or file size -- so the script can treat a missing attribute
    as "sort last" (see `sortValue` in `_SCRIPT`) instead of inventing a
    numeric sentinel here that the script would have to know about too.
    """
    attributes = ""
    if result.render_seconds is not None:
        attributes += f' data-render-seconds="{escape(str(result.render_seconds))}"'
    if result.file_size_bytes is not None:
        attributes += f' data-file-size="{escape(str(result.file_size_bytes))}"'
    return attributes


def _tile_html(result: ModelResult) -> str:
    title = escape(result.display_name or result.model_id)
    model_id = escape(result.model_id)
    sort_attributes = _sort_attributes_html(result)
    if result.status != "ok":
        error = escape(result.error or "unknown error")
        return (
            f'<article class="tile tile-error" data-model="{model_id}"'
            f"{sort_attributes}>"
            f"<h2>{title}</h2>"
            f'<p class="model-id">{model_id}</p>'
            f'<p class="error">Failed: {error}</p>'
            "</article>"
        )
    src = escape(result.output_relative_path or "")
    provider_row = _provider_row(result.active_provider, result.fallback_notice)
    cache_class = ' class="cache-hit"' if result.cache_hit else ""
    return (
        f'<article class="tile" data-model="{model_id}"{sort_attributes}>'
        f'<div class="image-wrap"><img class="result-image" '
        f'src="{src}" data-src="{src}" alt="{title} result" /></div>'
        f"<h2>{title}</h2>"
        f'<p class="model-id">{model_id}</p>'
        f'<dl class="stats">'
        f"<dt>Weight</dt><dd>{_format_mb(result.weight_size_bytes)}</dd>"
        f"<dt>Render</dt><dd>{_format_seconds(result.render_seconds)}</dd>"
        f"<dt>Prepare</dt><dd>{_format_seconds(result.prepare_seconds)}</dd>"
        f"<dt>File size</dt><dd>{_format_file_size(result.file_size_bytes)}</dd>"
        f"<dt>Frames</dt><dd>{_format_int(result.frame_count)}</dd>"
        f"<dt>Duration</dt><dd>{_format_ms(result.duration_ms)}</dd>"
        f"<dt>Cache</dt><dd{cache_class}>{_cache_hit_label(result.cache_hit)}</dd>"
        f"{provider_row}"
        "</dl>"
        "</article>"
    )


def _provider_row(active_provider: str | None, fallback_notice: str | None) -> str:
    if active_provider is None:
        return ""
    value = escape(active_provider)
    if fallback_notice:
        value += f" ({escape(fallback_notice)})"
    return f"<dt>Provider</dt><dd>{value}</dd>"


def _cache_hit_label(cache_hit: bool | None) -> str:
    if cache_hit is None:
        return "unknown"
    return "hit — may be edited" if cache_hit else "miss"


def _format_seconds(value: float | None) -> str:
    return "–" if value is None else f"{value:.1f}s"


def _format_ms(value: int | None) -> str:
    return "–" if value is None else f"{value} ms"


def _format_int(value: int | None) -> str:
    return "–" if value is None else str(value)


def _format_mb(value: int | None) -> str:
    return "–" if value is None else f"{value / (1024 * 1024):.1f} MB"


def _format_file_size(value: int | None) -> str:
    if value is None:
        return "–"
    if value >= 1024 * 1024:
        return f"{value / (1024 * 1024):.2f} MB"
    return f"{value / 1024:.1f} KB"
