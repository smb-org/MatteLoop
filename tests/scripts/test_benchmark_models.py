from __future__ import annotations

import json
import os
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

import pytest
from PIL import Image

from matteloop.core.errors import ValidationError
from matteloop.core.execution_providers import CPU_EXECUTION_PROVIDER
from matteloop.core.parameters import V1_MODEL_IDS
from matteloop.core.request_json import render_settings_to_json
from matteloop.core.specs import (
    CropSpec,
    EdgeMode,
    FramingSpec,
    OutputSpec,
    RenderRequest,
    SamplingSpec,
    SegmentationSpec,
)
from matteloop.jobs.models.catalog import ModelCatalog
from matteloop.jobs.render import RenderArtifact
from scripts.benchmark_models import (
    BenchmarkError,
    BenchmarkInterrupted,
    BenchmarkRun,
    ModelResult,
    _print_license_note_if_selected,
    build_base_request,
    format_summary_table,
    load_request_text,
    load_run_from_results_json,
    main,
    resolve_provider,
    run_benchmark,
    run_report_only,
    select_models,
    write_results,
)


class _StubClipboard:
    def __init__(self, text: str) -> None:
        self._text = text

    def text(self) -> str:
        return self._text


class _FakeRuntime:
    """A `BenchmarkRuntime` double with no network, model, or Qt dependency."""

    def __init__(
        self,
        out_dir: Path,
        *,
        fail_models: frozenset[str] = frozenset(),
        active_provider: str = CPU_EXECUTION_PROVIDER,
        fallback_notice: str | None = None,
    ):
        self._out_dir = out_dir
        self._fail_models = fail_models
        self.active_provider = active_provider
        self.fallback_notice = fallback_notice
        self.prepared: list[str] = []
        self.rendered: list[str] = []
        self.rendered_requests: list[RenderRequest] = []

    def prepare(self, model_id, extras, context):  # noqa: ANN001, ANN201
        del extras, context
        self.prepared.append(model_id)
        return None

    def render(self, request, context):  # noqa: ANN001, ANN201
        del context
        model_id = request.segmentation.model_id
        self.rendered.append(model_id)
        self.rendered_requests.append(request)
        if model_id in self._fail_models:
            raise RuntimeError(f"boom: {model_id}")
        return RenderArtifact(
            output_path=self._out_dir / f"{model_id}.webp",
            fingerprint="fake-fingerprint",
            cut_workspace=None,  # type: ignore[arg-type]
            manifest=None,  # type: ignore[arg-type]
            frame_count=3,
            width=64,
            height=48,
            file_size=1024,
            duration_ms=200,
            requested_timestamps=(),
            actual_pts=None,
            delays_ms=(),
            ownership_peak=0,
            ownership_current=0,
            rebuilt=False,
        )

    def close(self) -> None:
        pass


def _request(tmp_path: Path) -> RenderRequest:
    return RenderRequest(
        source=tmp_path / "clip.mp4",
        sampling=SamplingSpec(start=Fraction(0), end=Fraction(1), fps=15),
        crop=CropSpec(0, 0, 64, 48),
        segmentation=SegmentationSpec(
            model_id="birefnet-portrait", edge_mode=EdgeMode.STANDARD
        ),
        framing=FramingSpec(),
        output=OutputSpec(tmp_path / "out", "out.webp"),
    )


def _request_text(tmp_path: Path) -> str:
    return render_settings_to_json(_request(tmp_path))


def _base_request(tmp_path: Path, out_dir: Path) -> tuple[RenderRequest, dict]:
    text = _request_text(tmp_path)
    return build_base_request(text, out_dir), json.loads(text)


def test_default_model_selection_is_the_apps_own_v1_set() -> None:
    catalog = ModelCatalog.load_resource()

    selected = select_models(catalog, None)

    assert selected == V1_MODEL_IDS
    assert "u2net_cloth_seg" not in selected
    assert "bria-rmbg" not in selected
    assert len(selected) == 13


def test_unknown_model_id_is_rejected_with_the_valid_ids_listed() -> None:
    catalog = ModelCatalog.load_resource()

    with pytest.raises(BenchmarkError) as excinfo:
        select_models(catalog, "birefnet-general,not-a-model")

    message = str(excinfo.value)
    unknown_part, _, valid_part = message.partition(";")
    assert "not-a-model" in unknown_part
    assert "birefnet-general" not in unknown_part
    for model_id in catalog.ids:
        assert model_id in valid_part


def test_explicit_models_may_include_the_default_exclusions() -> None:
    catalog = ModelCatalog.load_resource()

    selected = select_models(catalog, "u2net_cloth_seg,bria-rmbg")

    assert selected == ("u2net_cloth_seg", "bria-rmbg")


def test_empty_models_argument_is_rejected() -> None:
    catalog = ModelCatalog.load_resource()

    with pytest.raises(BenchmarkError):
        select_models(catalog, "   ")


def test_ensure_qt_application_creates_a_core_application_for_a_file_request() -> None:
    """main()'s first act
    must guarantee a Qt application exists before the runtime (and its
    download transport) is built -- but a `--request <file>` run never
    touches the clipboard, so it must not require a display/GUI backend, only
    the event loop `QtNetworkDownloadTransport` pumps while waiting for a
    downloaded chunk. A plain `QCoreApplication` provides that; forcing a
    `QGuiApplication` here is what made a headless `--request` run on Linux
    contradict the README's documented headless workflow.

    Run in a subprocess, not in-process: this repository's own test session
    shares one Qt application across every test file, and creating a bare
    ``QGuiApplication`` here (instead of the ``QApplication`` the widget
    tests need) would leave that singleton behind for every test that runs
    afterwards -- which is exactly the kind of cross-test pollution that
    made later UI tests in this file hang when this test was first added
    in-process.
    """
    script = (
        "from PySide6.QtCore import QCoreApplication\n"
        "from PySide6.QtGui import QGuiApplication\n"
        "from scripts.benchmark_models import _ensure_qt_application\n"
        "app = _ensure_qt_application(needs_clipboard=False)\n"
        "assert QCoreApplication.instance() is app\n"
        "assert not isinstance(app, QGuiApplication)\n"
        "print('ok')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        check=False,
        env=os.environ | {"QT_QPA_PLATFORM": "offscreen"},
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_ensure_qt_application_creates_a_gui_application_for_the_clipboard() -> None:
    """The clipboard path (`_TextClipboard.text()`) calls `QGuiApplication
    .instance().clipboard()`, which plain `QCoreApplication` does not have,
    so reading from the clipboard still needs the GUI application -- only the
    `--request <file>` path may downgrade to `QCoreApplication`.
    """
    script = (
        "from PySide6.QtCore import QCoreApplication\n"
        "from PySide6.QtGui import QGuiApplication\n"
        "from scripts.benchmark_models import _ensure_qt_application\n"
        "app = _ensure_qt_application(needs_clipboard=True)\n"
        "assert QCoreApplication.instance() is app\n"
        "assert isinstance(app, QGuiApplication)\n"
        "print('ok')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        check=False,
        env=os.environ | {"QT_QPA_PLATFORM": "offscreen"},
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_print_license_note_when_bria_rmbg_is_selected() -> None:
    catalog = ModelCatalog.load_resource()
    logged: list[str] = []

    _print_license_note_if_selected(catalog, ("u2net", "bria-rmbg"), log=logged.append)

    assert len(logged) == 1
    assert catalog.get("bria-rmbg").license_note in logged[0]


def test_no_license_note_when_bria_rmbg_is_not_selected() -> None:
    catalog = ModelCatalog.load_resource()
    logged: list[str] = []

    _print_license_note_if_selected(catalog, ("u2net", "u2netp"), log=logged.append)

    assert logged == []


def test_resolve_provider_rejects_an_unknown_provider() -> None:
    with pytest.raises(BenchmarkError) as excinfo:
        resolve_provider("TotallyMadeUpExecutionProvider")

    assert "TotallyMadeUpExecutionProvider" in str(excinfo.value)


def test_resolve_provider_accepts_an_allowlisted_provider() -> None:
    assert resolve_provider(CPU_EXECUTION_PROVIDER) == CPU_EXECUTION_PROVIDER


def test_load_request_text_reads_a_file(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text("stand-in text", encoding="utf-8")

    assert load_request_text(path, _StubClipboard("unused")) == "stand-in text"


def test_load_request_text_falls_back_to_the_clipboard(tmp_path: Path) -> None:
    del tmp_path
    text = load_request_text(None, _StubClipboard("clipboard payload"))

    assert text == "clipboard payload"


def test_load_request_text_rejects_an_empty_clipboard() -> None:
    with pytest.raises(BenchmarkError):
        load_request_text(None, _StubClipboard("   "))


def test_load_request_text_reports_a_missing_file_clearly(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.json"

    with pytest.raises(BenchmarkError) as excinfo:
        load_request_text(missing, _StubClipboard("unused"))

    assert str(missing) in str(excinfo.value)


def test_build_base_request_rejects_a_malformed_request_with_a_clear_error(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValidationError) as excinfo:
        build_base_request("{not json", tmp_path / "out")

    assert "JSON" in excinfo.value.technical_detail


def test_run_benchmark_rejects_empty_model_ids(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    base_request, request_settings = _base_request(tmp_path, out_dir)

    with pytest.raises(BenchmarkError):
        run_benchmark(
            base_request=base_request,
            request_settings=request_settings,
            model_ids=(),
            provider=CPU_EXECUTION_PROVIDER,
            out_dir=out_dir,
            catalog=ModelCatalog.load_resource(),
            runtime=_FakeRuntime(out_dir),
            log=lambda _message: None,
        )


def test_run_benchmark_records_a_failing_model_and_keeps_going(
    tmp_path: Path,
) -> None:
    out_dir = tmp_path / "out"
    runtime = _FakeRuntime(out_dir, fail_models=frozenset({"u2net"}))
    base_request, request_settings = _base_request(tmp_path, out_dir)

    run = run_benchmark(
        base_request=base_request,
        request_settings=request_settings,
        model_ids=("u2net", "u2netp"),
        provider=CPU_EXECUTION_PROVIDER,
        out_dir=out_dir,
        catalog=ModelCatalog.load_resource(),
        runtime=runtime,
        log=lambda _message: None,
    )

    by_id = {result.model_id: result for result in run.results}
    assert runtime.rendered == ["u2net", "u2netp"]
    assert by_id["u2net"].status == "error"
    assert "boom: u2net" in (by_id["u2net"].error or "")
    assert by_id["u2net"].file_size_bytes is None
    assert by_id["u2netp"].status == "ok"
    assert by_id["u2netp"].file_size_bytes == 1024
    assert by_id["u2netp"].frame_count == 3
    assert by_id["u2netp"].duration_ms == 200
    assert by_id["u2netp"].output_relative_path == "u2netp.webp"
    assert by_id["u2netp"].active_provider == CPU_EXECUTION_PROVIDER


def test_run_benchmark_writes_results_after_every_model_not_only_at_the_end(
    tmp_path: Path,
) -> None:
    """A Ctrl+C partway through must not lose
    tiles already rendered, so results.json/index.html are written
    incrementally rather than only once at the end of the loop."""
    out_dir = tmp_path / "out"
    base_request, request_settings = _base_request(tmp_path, out_dir)
    written_after_first: list[int] = []

    class _RecordingRuntime(_FakeRuntime):
        def render(self, request, context):  # noqa: ANN001, ANN201
            artifact = super().render(request, context)
            results_path = out_dir / "results.json"
            if results_path.exists():
                written_after_first.append(
                    len(json.loads(results_path.read_text())["models"])
                )
            else:
                written_after_first.append(0)
            return artifact

    runtime = _RecordingRuntime(out_dir)

    run_benchmark(
        base_request=base_request,
        request_settings=request_settings,
        model_ids=("u2net", "u2netp"),
        provider=CPU_EXECUTION_PROVIDER,
        out_dir=out_dir,
        catalog=ModelCatalog.load_resource(),
        runtime=runtime,
        log=lambda _message: None,
    )

    # Before the second model rendered, the first model's result was
    # already on disk.
    assert written_after_first == [0, 1]
    assert (out_dir / "results.json").exists()
    assert (out_dir / "index.html").exists()


def test_run_benchmark_carries_the_output_size_budget_to_every_model(
    tmp_path: Path,
) -> None:
    out_dir = tmp_path / "out"
    request = RenderRequest(
        source=tmp_path / "clip.mp4",
        sampling=SamplingSpec(start=Fraction(0), end=Fraction(1), fps=15),
        crop=CropSpec(0, 0, 64, 48),
        segmentation=SegmentationSpec(
            model_id="birefnet-portrait", edge_mode=EdgeMode.STANDARD
        ),
        framing=FramingSpec(),
        output=OutputSpec(tmp_path / "out", "out.webp", max_bytes=5_000_000),
    )
    text = render_settings_to_json(request)
    base_request = build_base_request(text, out_dir)
    runtime = _FakeRuntime(out_dir)

    run_benchmark(
        base_request=base_request,
        request_settings=json.loads(text),
        model_ids=("u2net", "u2netp"),
        provider=CPU_EXECUTION_PROVIDER,
        out_dir=out_dir,
        catalog=ModelCatalog.load_resource(),
        runtime=runtime,
        log=lambda _message: None,
    )

    assert [r.output.max_bytes for r in runtime.rendered_requests] == [
        5_000_000,
        5_000_000,
    ]


def test_run_benchmark_applies_the_same_settings_to_every_model_except_selection(
    tmp_path: Path,
) -> None:
    """Only model id, provider, and output may vary between models; every
    other setting (sampling, crop, framing, edge mode, transform) must be
    identical, and the requested provider must actually be applied."""
    out_dir = tmp_path / "out"
    base_request, request_settings = _base_request(tmp_path, out_dir)
    runtime = _FakeRuntime(out_dir)

    run_benchmark(
        base_request=base_request,
        request_settings=request_settings,
        model_ids=("u2net", "u2netp", "silueta"),
        provider=CPU_EXECUTION_PROVIDER,
        out_dir=out_dir,
        catalog=ModelCatalog.load_resource(),
        runtime=runtime,
        log=lambda _message: None,
    )

    requests = runtime.rendered_requests
    assert len(requests) == 3
    for request in requests:
        assert request.sampling == base_request.sampling
        assert request.crop == base_request.crop
        assert request.framing == base_request.framing
        assert request.transform == base_request.transform
        assert request.segmentation.edge_mode == base_request.segmentation.edge_mode
        assert request.segmentation.execution_provider == CPU_EXECUTION_PROVIDER
        assert request.output.max_bytes == base_request.output.max_bytes
    assert [r.segmentation.model_id for r in requests] == [
        "u2net",
        "u2netp",
        "silueta",
    ]
    assert [r.output.filename for r in requests] == [
        "u2net.webp",
        "u2netp.webp",
        "silueta.webp",
    ]


def test_run_benchmark_flags_a_frame_count_mismatch(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    base_request, request_settings = _base_request(tmp_path, out_dir)

    class _MismatchedRuntime(_FakeRuntime):
        def render(self, request, context):  # noqa: ANN001, ANN201
            artifact = super().render(request, context)
            if request.segmentation.model_id == "u2netp":
                from dataclasses import replace

                artifact = replace(artifact, frame_count=99)
            return artifact

    run = run_benchmark(
        base_request=base_request,
        request_settings=request_settings,
        model_ids=("u2net", "u2netp"),
        provider=CPU_EXECUTION_PROVIDER,
        out_dir=out_dir,
        catalog=ModelCatalog.load_resource(),
        runtime=_MismatchedRuntime(out_dir),
        log=lambda _message: None,
    )

    assert any("frame_count differs" in warning for warning in run.warnings)


def test_results_json_has_the_documented_shape(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    base_request, request_settings = _base_request(tmp_path, out_dir)
    runtime = _FakeRuntime(out_dir)

    run_benchmark(
        base_request=base_request,
        request_settings=request_settings,
        model_ids=("u2net",),
        provider=CPU_EXECUTION_PROVIDER,
        out_dir=out_dir,
        catalog=ModelCatalog.load_resource(),
        runtime=runtime,
        log=lambda _message: None,
    )

    payload = json.loads((out_dir / "results.json").read_text(encoding="utf-8"))

    assert payload["provider"] == CPU_EXECUTION_PROVIDER
    assert "regenerate" not in payload
    assert payload["request"]["segmentation"]["model_id"] == "birefnet-portrait"
    assert payload["warnings"] == []
    assert len(payload["models"]) == 1
    model_payload = payload["models"][0]
    assert model_payload["model_id"] == "u2net"
    assert model_payload["status"] == "ok"
    assert model_payload["file_size_bytes"] == 1024
    assert model_payload["active_provider"] == CPU_EXECUTION_PROVIDER


def _summary_result(model_id: str, **overrides: object) -> ModelResult:
    defaults: dict[str, object] = dict(
        model_id=model_id,
        display_name=model_id,
        status="ok",
        prepare_seconds=1.0,
        render_seconds=2.0,
        frame_count=30,
        file_size_bytes=1024,
        active_provider=CPU_EXECUTION_PROVIDER,
    )
    defaults.update(overrides)
    return ModelResult(**defaults)  # type: ignore[arg-type]


def test_format_summary_table_sorts_by_render_time_with_failed_last() -> None:
    results = (
        _summary_result("slow", render_seconds=20.0),
        ModelResult(
            model_id="broken", display_name="Broken", status="error", error="boom"
        ),
        _summary_result("fast", render_seconds=1.0),
    )

    table = format_summary_table(results)

    lines = table.splitlines()
    data_rows = lines[2:]
    assert [line.split()[0] for line in data_rows] == ["fast", "slow", "broken"]
    assert "failed" in data_rows[2]


def test_format_summary_table_marks_a_fallback_provider() -> None:
    result = _summary_result("silueta", fallback_notice="CUDA unavailable; using CPU")

    table = format_summary_table((result,))

    assert "CPU (fallback)" in table


def test_format_summary_table_marks_a_cache_hit_as_reused_cuts() -> None:
    result = _summary_result("silueta", cache_hit=True)

    table = format_summary_table((result,))

    assert "reused cuts" in table


def test_format_summary_table_uses_the_reports_file_size_formatting() -> None:
    result = _summary_result("silueta", file_size_bytes=1024)

    table = format_summary_table((result,))

    assert "1.0 KB" in table


def test_format_summary_table_has_column_headers() -> None:
    table = format_summary_table((_summary_result("silueta"),))

    header = table.splitlines()[0]
    columns = (
        "model",
        "prepare (s)",
        "render (s)",
        "frames",
        "size",
        "provider",
        "status",
    )
    for column in columns:
        assert column in header


def test_run_benchmark_raises_benchmark_interrupted_with_completed_results(
    tmp_path: Path,
) -> None:
    out_dir = tmp_path / "out"
    base_request, request_settings = _base_request(tmp_path, out_dir)

    class _InterruptingRuntime(_FakeRuntime):
        def render(self, request, context):  # noqa: ANN001, ANN201
            if request.segmentation.model_id == "u2netp":
                raise KeyboardInterrupt
            return super().render(request, context)

    runtime = _InterruptingRuntime(out_dir)

    with pytest.raises(BenchmarkInterrupted) as excinfo:
        run_benchmark(
            base_request=base_request,
            request_settings=request_settings,
            model_ids=("u2net", "u2netp"),
            provider=CPU_EXECUTION_PROVIDER,
            out_dir=out_dir,
            catalog=ModelCatalog.load_resource(),
            runtime=runtime,
            log=lambda _message: None,
        )

    partial = excinfo.value.run
    assert [result.model_id for result in partial.results] == ["u2net"]
    assert partial.results[0].status == "ok"
    # The results completed before the interrupt were still written to disk.
    assert (out_dir / "results.json").exists()


def _write_animated_webp(path: Path) -> None:
    frame0 = Image.new("RGBA", (4, 4), (255, 0, 0, 255))
    frame1 = Image.new("RGBA", (4, 4), (0, 255, 0, 255))
    frame0.save(
        path,
        format="WEBP",
        save_all=True,
        append_images=[frame1],
        duration=100,
        loop=0,
    )


def _finished_run(out_dir: Path) -> BenchmarkRun:
    _write_animated_webp(out_dir / "silueta.webp")
    return BenchmarkRun(
        generated_at="2026-09-17T12:00:00+00:00",
        provider=CPU_EXECUTION_PROVIDER,
        request_settings={"source": "clip.mp4"},
        results=(
            ModelResult(
                model_id="silueta",
                display_name="Silueta",
                status="ok",
                file_size_bytes=1234,
                frame_count=2,
                duration_ms=200,
                output_relative_path="silueta.webp",
            ),
        ),
    )


def test_run_report_only_rebuilds_the_report_and_stills_without_rendering(
    tmp_path: Path,
) -> None:
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    write_results(out_dir, _finished_run(out_dir))
    webp_bytes_before = (out_dir / "silueta.webp").read_bytes()

    exit_code = run_report_only(out_dir, log=lambda _message: None)

    assert exit_code == 0
    assert (out_dir / "silueta-still.png").exists()
    html = (out_dir / "index.html").read_text(encoding="utf-8")
    assert 'data-still="silueta-still.png"' in html
    # A report-only rebuild only reads the WebP to make the still, never
    # overwrites or deletes it.
    assert (out_dir / "silueta.webp").read_bytes() == webp_bytes_before


def test_run_report_only_fails_clearly_on_a_missing_results_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    exit_code = run_report_only(out_dir)

    assert exit_code == 1
    captured = capsys.readouterr()
    assert str(out_dir / "results.json") in captured.err


def test_run_report_only_fails_clearly_on_unreadable_json(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "results.json").write_text("{not json", encoding="utf-8")

    with pytest.raises(BenchmarkError) as excinfo:
        load_run_from_results_json(out_dir / "results.json")

    assert "results.json" in str(excinfo.value)


def test_main_report_only_never_builds_a_qt_application(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    write_results(out_dir, _finished_run(out_dir))

    def _fail_if_called(*, needs_clipboard: bool) -> object:
        raise AssertionError("--report-only must not build a Qt application")

    monkeypatch.setattr(
        "scripts.benchmark_models._ensure_qt_application", _fail_if_called
    )

    exit_code = main(["--report-only", str(out_dir)])

    assert exit_code == 0
    assert (out_dir / "index.html").exists()
