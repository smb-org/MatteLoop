"""Render one clip with every catalog model and produce a comparison report.

Renders the request exported through the Render button's "Copy render
settings" context-menu action (`ui/render_settings_export.py`,
`core/request_json.py`) through every -- or a chosen subset of -- V1
segmentation model, holding source, sampling, crop, framing, transform, and
output size budget constant.

This reuses the application's own wiring rather than a copy of it:
`ui.preview_controller.runtime.ProductionPreviewRuntime` is the same class
`ui/main_window.py` builds for the real preview and render jobs, so model
download, session preparation, and rendering behave identically to a run
started from the GUI. In particular, model weights still download through
`ui/download_transport.py`'s Qt transport -- see CLAUDE.md, "One network
path" -- this script never opens a socket of its own.

Cut-workspace caveat (issue #170 review, item 7): every render -- benchmark
or otherwise -- promotes its segmented cuts into the single durable
workspace cache all of MatteLoop shares (`paths.cut_workspace_root()`,
fixed, not overridable). There is no supported way to give this script its
own isolated workspace root without changing `jobs/render.py` (frozen) and
the workspace-root validation in `jobs/workspace/_platform.py` and
`_models.py`, which treats the shared root as a canonical, checked
invariant, not a parameter. Short of that change, this script instead
minimises the damage: it never forces re-segmentation over an existing cut
set (there is no `--regenerate`; a previous CLI version's flag detected the
destructive case reviewers actually hit -- overwriting a hand-edited cut
workspace with fresh, unedited segmentation output -- and removing it
removes that risk), and it detects and loudly flags a cache hit instead of
reporting it as if it were the raw model's output. See the module docstring
note in `_predict_cache_hit` below and the "STOP" discussion in the review
for the options that would need a maintainer decision to isolate this fully.

This is a developer tool under `scripts/`, not a V1 product feature (see
docs/v1-scope.md). It does not download models or render anything by itself
when imported for its argument-parsing and reporting logic; a real run is a
deliberate, separate act driven from the command line.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from matteloop.core.errors import AppError
from matteloop.core.execution_providers import (
    ALLOWED_EXECUTION_PROVIDERS,
    is_allowed_provider,
    provider_options_from_runtime,
    select_provider,
)
from matteloop.core.parameters import V1_MODEL_IDS
from matteloop.core.request_json import render_settings_from_json
from matteloop.core.specs import CollisionPolicy, OutputSpec, RenderRequest
from matteloop.core.state import JobKind
from matteloop.jobs.context import CancellationState, JobContext
from matteloop.jobs.models.catalog import ModelCatalog
from matteloop.jobs.render import (
    FilesystemWorkspacePort,
    LocalSourcePort,
    RenderArtifact,
    find_matching_cut_workspace,
)

# `pytest` imports this module as `scripts.benchmark_models` (repository root
# on sys.path, see tests/scripts/__init__.py); running it directly as
# `python scripts/benchmark_models.py` instead puts `scripts/` itself on
# sys.path, the same dual-mode split already used by scripts/build.py and
# scripts/qt_source.py. Both spellings have to resolve.
try:
    from scripts.benchmark_report import render_report_html
except ImportError:
    from benchmark_report import render_report_html  # type: ignore[no-redef]

_LICENSED_MODEL_ID = "bria-rmbg"


class BenchmarkError(Exception):
    """A user-facing problem with the benchmark's arguments or input."""


class Clipboard(Protocol):
    def text(self) -> str: ...


class BenchmarkRuntime(Protocol):
    """The subset of `ProductionPreviewRuntime` this script depends on."""

    def prepare(
        self, model_id: str, extras: dict[str, object], context: JobContext
    ) -> object: ...

    def render(self, request: RenderRequest, context: JobContext) -> RenderArtifact: ...


@dataclass(frozen=True)
class ModelResult:
    """One model's benchmark outcome, ready for `results.json` and the report."""

    model_id: str
    display_name: str
    status: str  # "ok" or "error"
    error: str | None = None
    weight_size_bytes: int | None = None
    prepare_seconds: float | None = None
    render_seconds: float | None = None
    cache_hit: bool | None = None
    active_provider: str | None = None
    fallback_notice: str | None = None
    file_size_bytes: int | None = None
    frame_count: int | None = None
    duration_ms: int | None = None
    output_relative_path: str | None = None


@dataclass(frozen=True)
class BenchmarkRun:
    """Everything the report and `results.json` need about one run."""

    generated_at: str
    provider: str
    request_settings: Mapping[str, object]
    results: tuple[ModelResult, ...]
    warnings: tuple[str, ...] = ()


def select_models(catalog: ModelCatalog, requested: str | None) -> tuple[str, ...]:
    """Resolve `--models`, defaulting to the app's own V1 model set.

    `bria-rmbg` (model-specific licence terms) and `u2net_cloth_seg` (needs
    a clothing-category input the UI cannot provide) are excluded from that
    default, matching docs/v1-scope.md; both still work when named
    explicitly via `--models`.
    """
    if requested is None:
        return V1_MODEL_IDS
    ids = tuple(item.strip() for item in requested.split(",") if item.strip())
    if not ids:
        raise BenchmarkError("--models must name at least one model id")
    unknown = sorted(set(ids) - set(catalog.ids))
    if unknown:
        valid = ", ".join(sorted(catalog.ids))
        raise BenchmarkError(
            f"unknown model id(s): {', '.join(unknown)}; valid ids are: {valid}"
        )
    return ids


def resolve_provider(requested: str | None) -> str:
    """Resolve `--provider`, defaulting to the app's own recommendation."""
    if requested is not None:
        if not is_allowed_provider(requested):
            valid = ", ".join(ALLOWED_EXECUTION_PROVIDERS)
            raise BenchmarkError(
                f"unknown execution provider {requested!r}; "
                f"valid providers are: {valid}"
            )
        return requested
    return select_provider(None, provider_options_from_runtime())


def load_request_text(path: Path | None, clipboard: Clipboard) -> str:
    """Read the exported render-settings JSON from a file or the clipboard."""
    if path is not None:
        try:
            return path.read_text(encoding="utf-8")
        except OSError as error:
            raise BenchmarkError(
                f"could not read request file {path}: {error}"
            ) from error
    text = clipboard.text()
    if not text.strip():
        raise BenchmarkError(
            "the clipboard is empty; use the Render button's \"Copy render "
            'settings" context-menu action first, or pass --request'
        )
    return text


def build_base_request(request_text: str, out_dir: Path) -> RenderRequest:
    """Parse the exported settings once, with a placeholder output.

    `_model_request` replaces `output` for every model with a real filename
    under `out_dir`, carrying over this base request's `output.max_bytes` --
    the only part of the placeholder output that is ever read.
    """
    return render_settings_from_json(
        request_text,
        directory=out_dir,
        filename="_placeholder.webp",
        collision_policy=CollisionPolicy.REPLACE,
    )


def run_benchmark(
    *,
    base_request: RenderRequest,
    request_settings: Mapping[str, object],
    model_ids: Sequence[str],
    provider: str,
    out_dir: Path,
    catalog: ModelCatalog,
    runtime: BenchmarkRuntime,
    clock: Callable[[], float] = time.perf_counter,
    log: Callable[[str], None] = print,
) -> BenchmarkRun:
    """Render `model_ids` sequentially through `runtime` and collect results.

    Writes `results.json` and `index.html` after every model, not just at
    the end, so a Ctrl+C partway through keeps every tile already rendered
    (issue #170 review, item 8) instead of losing the whole run.
    """
    if not model_ids:
        raise BenchmarkError("model_ids must not be empty")
    out_dir.mkdir(parents=True, exist_ok=True)
    generated_at = datetime.now(UTC).isoformat(timespec="seconds")
    results: list[ModelResult] = []
    run = BenchmarkRun(generated_at, provider, request_settings, ())
    for index, model_id in enumerate(model_ids, start=1):
        results.append(
            _run_one_model(
                base_request=base_request,
                model_id=model_id,
                index=index,
                total=len(model_ids),
                provider=provider,
                out_dir=out_dir,
                catalog=catalog,
                runtime=runtime,
                clock=clock,
                log=log,
            )
        )
        run = BenchmarkRun(
            generated_at=generated_at,
            provider=provider,
            request_settings=request_settings,
            results=tuple(results),
            warnings=_consistency_warnings(results),
        )
        write_results(out_dir, run)
        write_report(out_dir, run)
    return run


@dataclass(frozen=True)
class _ModelTiming:
    prepare_seconds: float
    render_seconds: float
    cache_hit: bool | None
    active_provider: str | None
    fallback_notice: str | None


def _model_request(
    base_request: RenderRequest, model_id: str, provider: str, out_dir: Path
) -> RenderRequest:
    return replace(
        base_request,
        segmentation=replace(
            base_request.segmentation, model_id=model_id, execution_provider=provider
        ),
        output=OutputSpec(
            out_dir,
            f"{model_id}.webp",
            max_bytes=base_request.output.max_bytes,
            collision_policy=CollisionPolicy.REPLACE,
        ),
    )


def _execute_model(
    runtime: BenchmarkRuntime,
    request: RenderRequest,
    model_id: str,
    context: JobContext,
    clock: Callable[[], float],
) -> tuple[_ModelTiming, RenderArtifact]:
    prepare_start = clock()
    runtime.prepare(
        model_id,
        {"execution_provider": request.segmentation.execution_provider},
        context,
    )
    prepare_seconds = clock() - prepare_start
    cache_hit = _predict_cache_hit(runtime, request, context)
    render_start = clock()
    artifact = runtime.render(request, context)
    render_seconds = clock() - render_start
    timing = _ModelTiming(
        prepare_seconds=prepare_seconds,
        render_seconds=render_seconds,
        cache_hit=cache_hit,
        active_provider=getattr(runtime, "active_provider", None),
        fallback_notice=getattr(runtime, "fallback_notice", None),
    )
    return timing, artifact


def _run_one_model(
    *,
    base_request: RenderRequest,
    model_id: str,
    index: int,
    total: int,
    provider: str,
    out_dir: Path,
    catalog: ModelCatalog,
    runtime: BenchmarkRuntime,
    clock: Callable[[], float],
    log: Callable[[str], None],
) -> ModelResult:
    log(f"[{index}/{total}] {model_id} ...")
    spec = catalog.get(model_id)
    weight_size = spec.artifact.size_bytes if spec.artifact is not None else None
    request = _model_request(base_request, model_id, provider, out_dir)
    context = _new_job_context(out_dir, model_id)
    try:
        timing, artifact = _execute_model(runtime, request, model_id, context, clock)
    except Exception as error:  # noqa: BLE001 - recorded per model, loop continues
        log(f"  {model_id}: failed -- {error}")
        return ModelResult(
            model_id=model_id,
            display_name=spec.display_name,
            status="error",
            error=str(error),
            weight_size_bytes=weight_size,
        )
    log(
        f"  {model_id}: ok -- prepare {timing.prepare_seconds:.1f}s, "
        f"render {timing.render_seconds:.1f}s"
    )
    return ModelResult(
        model_id=model_id,
        display_name=spec.display_name,
        status="ok",
        weight_size_bytes=weight_size,
        prepare_seconds=timing.prepare_seconds,
        render_seconds=timing.render_seconds,
        cache_hit=timing.cache_hit,
        active_provider=timing.active_provider,
        fallback_notice=timing.fallback_notice,
        file_size_bytes=artifact.file_size,
        frame_count=artifact.frame_count,
        duration_ms=artifact.duration_ms,
        output_relative_path=f"{model_id}.webp",
    )


def _new_job_context(out_dir: Path, model_id: str) -> JobContext:
    return JobContext(
        job_id=f"benchmark-{model_id}",
        kind=JobKind.RENDER,
        workspace=out_dir,
        progress_sink=lambda _event: None,
        cancellation=CancellationState(),
    )


def _predict_cache_hit(
    runtime: BenchmarkRuntime, request: RenderRequest, context: JobContext
) -> bool | None:
    """Predict, without changing `jobs/render.py`, whether segmentation will
    be skipped in favour of an already-promoted cut workspace.

    `RenderService.render()` makes exactly this check internally
    (`find_matching_cut_workspace`, frozen per docs/engineering-guardrails.md)
    before deciding to segment or reuse; calling the same public function
    first lets the script report the outcome instead of guessing at it. A
    hit means this tile may show a previously hand-edited cut set rather
    than the model's raw output (issue #170 review, item 7) -- surfaced as a
    run-level warning by `_consistency_warnings`, since there is no isolated
    workspace to render into instead. Returns ``None`` when the runtime does
    not expose a `ModelCatalog` to look the model's weight hash up in --
    true for a test double that has no reason to carry one.
    """
    catalog = getattr(runtime, "catalog", None)
    if not isinstance(catalog, ModelCatalog):
        return None
    spec = catalog.get(request.segmentation.model_id)
    if spec.artifact is None:
        return None
    workspace = find_matching_cut_workspace(
        LocalSourcePort(),
        FilesystemWorkspacePort(),
        request,
        model_weight_sha256=spec.artifact.sha256,
        rembg_version=catalog.rembg_version,
        context=context,
    )
    return workspace is not None


def _consistency_warnings(results: Sequence[ModelResult]) -> tuple[str, ...]:
    """Flag anything that would make the comparison misleading.

    Every model renders the same sampling, crop, and framing, so successful
    models should agree on frame count and duration; a mismatch usually
    means a model dropped or duplicated frames. A cache hit means the tile
    may not be this model's raw output at all (see `_predict_cache_hit`).
    """
    warnings: list[str] = []
    ok = [result for result in results if result.status == "ok"]
    frame_counts = {result.frame_count for result in ok}
    if len(frame_counts) > 1:
        pairs = ", ".join(f"{result.model_id}={result.frame_count}" for result in ok)
        warnings.append(f"frame_count differs across models: {pairs}")
    durations = {result.duration_ms for result in ok}
    if len(durations) > 1:
        pairs = ", ".join(f"{result.model_id}={result.duration_ms}" for result in ok)
        warnings.append(f"duration_ms differs across models: {pairs}")
    reused = [result.model_id for result in ok if result.cache_hit]
    if reused:
        warnings.append(
            "reused an existing durable cut workspace for: "
            + ", ".join(reused)
            + " -- these tiles may show a previously hand-edited cut set, "
            "not the model's raw output"
        )
    return tuple(warnings)


def write_results(out_dir: Path, run: BenchmarkRun) -> Path:
    path = out_dir / "results.json"
    payload = {
        "generated_at": run.generated_at,
        "provider": run.provider,
        "request": run.request_settings,
        "warnings": list(run.warnings),
        "models": [asdict(result) for result in run.results],
    }
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return path


def write_report(out_dir: Path, run: BenchmarkRun) -> Path:
    path = out_dir / "index.html"
    path.write_text(render_report_html(run), encoding="utf-8")
    return path


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Render one clip with every V1 segmentation model, using "
            "settings copied from the Render button, and produce a "
            "side-by-side comparison report."
        )
    )
    parser.add_argument(
        "--request",
        type=Path,
        default=None,
        help=(
            "path to exported render-settings JSON; default: read the "
            "clipboard. On Linux or a headless machine, prefer --request: "
            "clipboard access there depends on a running desktop session "
            "and is not reliable otherwise."
        ),
    )
    parser.add_argument(
        "--models",
        type=str,
        default=None,
        help=(
            "comma-separated model ids; default: the app's own 13-model V1 "
            "set. 'bria-rmbg' and 'u2net_cloth_seg' are excluded from that "
            "default but work if named explicitly; selecting 'bria-rmbg' "
            "prints its licence note before rendering."
        ),
    )
    parser.add_argument(
        "--provider",
        type=str,
        default=None,
        help="execution provider; default: this machine's recommended provider",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="output directory; default: ./benchmark-<timestamp>",
    )
    return parser


def _default_out_dir() -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return Path.cwd() / f"benchmark-{stamp}"


def _print_license_note_if_selected(
    catalog: ModelCatalog,
    model_ids: Sequence[str],
    log: Callable[[str], None] = print,
) -> None:
    """Print `bria-rmbg`'s licence note before rendering it (issue #170, item 3).

    Split out from `main()` so the decision -- and the note it prints -- is
    testable without building a real runtime.
    """
    if _LICENSED_MODEL_ID in model_ids:
        log(f"licence: {catalog.get(_LICENSED_MODEL_ID).license_note}")


class _TextClipboard:
    """Adapts Qt's clipboard to the `Clipboard` protocol, importing Qt lazily
    so argument-parsing and reporting logic can be exercised without it."""

    def text(self) -> str:
        from PySide6.QtGui import QGuiApplication

        app = QGuiApplication.instance()
        if app is None:
            app = QGuiApplication(sys.argv[:1])
        clipboard = app.clipboard()
        if clipboard is None:
            raise BenchmarkError("no clipboard is available on this platform")
        return clipboard.text()


def _ensure_qt_application(*, needs_clipboard: bool) -> object:
    """Guarantee a Qt application instance exists before any network I/O.

    `QtNetworkDownloadTransport` pumps a `QEventLoop` while it waits for a
    download; without an application that loop never dispatches and the
    first uncached model hangs. Only reading the clipboard needs a
    `QGuiApplication`, which requires a display backend, so a `--request`
    run gets a plain `QCoreApplication` and stays usable on a headless
    machine. An existing instance is reused.
    """
    from PySide6.QtCore import QCoreApplication

    existing = QCoreApplication.instance()
    if existing is not None:
        return existing
    if needs_clipboard:
        from PySide6.QtGui import QGuiApplication

        return QGuiApplication(sys.argv[:1])
    return QCoreApplication(sys.argv[:1])


def main(argv: Sequence[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    _ensure_qt_application(needs_clipboard=args.request is None)
    try:
        request_text = load_request_text(args.request, _TextClipboard())
        catalog = ModelCatalog.load_resource()
        model_ids = select_models(catalog, args.models)
        provider = resolve_provider(args.provider)
        out_dir = args.out if args.out is not None else _default_out_dir()
        base_request = build_base_request(request_text, out_dir)
        request_settings = json.loads(request_text)
    except (BenchmarkError, AppError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    _print_license_note_if_selected(catalog, model_ids)

    from matteloop.ui.preview_controller.runtime import ProductionPreviewRuntime

    runtime = ProductionPreviewRuntime()
    try:
        run = run_benchmark(
            base_request=base_request,
            request_settings=request_settings,
            model_ids=model_ids,
            provider=provider,
            out_dir=out_dir,
            catalog=catalog,
            runtime=runtime,
        )
    except KeyboardInterrupt:
        print(
            "interrupted; results for models completed so far were already "
            f"written to {out_dir}",
            file=sys.stderr,
        )
        return 130
    finally:
        runtime.close()

    for warning in run.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    succeeded = sum(1 for result in run.results if result.status == "ok")
    print(f"{succeeded}/{len(run.results)} models succeeded; wrote {out_dir}")
    return 0 if succeeded == len(run.results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
