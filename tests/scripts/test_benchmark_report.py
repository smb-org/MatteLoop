from __future__ import annotations

from scripts.benchmark_models import BenchmarkRun, ModelResult
from scripts.benchmark_report import render_report_html

_MALICIOUS_ERROR = '<script>alert("boom")</script>'


def _run(*results: ModelResult, warnings: tuple[str, ...] = ()) -> BenchmarkRun:
    return BenchmarkRun(
        generated_at="2026-09-17T12:00:00+00:00",
        provider="CPUExecutionProvider",
        request_settings={"source": "clip.mp4"},
        results=results,
        warnings=warnings,
    )


def _ok_result(model_id: str, **overrides: object) -> ModelResult:
    defaults: dict[str, object] = dict(
        model_id=model_id,
        display_name=model_id.replace("-", " ").title(),
        status="ok",
        weight_size_bytes=176 * 1024 * 1024,
        prepare_seconds=4.2,
        render_seconds=11.8,
        cache_hit=False,
        active_provider="CPUExecutionProvider",
        fallback_notice=None,
        file_size_bytes=612_000,
        frame_count=30,
        duration_ms=2000,
        output_relative_path=f"{model_id}.webp",
    )
    defaults.update(overrides)
    return ModelResult(**defaults)  # type: ignore[arg-type]


def test_report_contains_one_tile_per_model() -> None:
    run = _run(_ok_result("birefnet-general"), _ok_result("birefnet-portrait"))

    html = render_report_html(run)

    assert html.count('class="tile') == 2
    assert 'data-model="birefnet-general"' in html
    assert 'data-model="birefnet-portrait"' in html


def test_report_references_relative_webp_paths() -> None:
    run = _run(_ok_result("isnet-anime"))

    html = render_report_html(run)

    assert 'data-src="isnet-anime.webp"' in html
    # Never an absolute filesystem path or a path outside the output directory.
    assert "C:\\" not in html
    assert "/isnet-anime.webp" not in html


def test_report_puts_the_image_src_in_markup_so_it_loads_without_js() -> None:
    run = _run(_ok_result("isnet-anime"))

    html = render_report_html(run)

    assert 'src="isnet-anime.webp" data-src="isnet-anime.webp"' in html


def test_report_shows_the_duration() -> None:
    run = _run(_ok_result("silueta", duration_ms=1234))

    html = render_report_html(run)

    assert "1234 ms" in html


def test_report_shows_the_active_provider_and_fallback_notice() -> None:
    run = _run(
        _ok_result(
            "silueta",
            active_provider="CPUExecutionProvider",
            fallback_notice="CUDA unavailable; using CPU",
        )
    )

    html = render_report_html(run)

    assert "CPUExecutionProvider" in html
    assert "CUDA unavailable; using CPU" in html


def test_report_flags_a_cache_hit_as_possibly_edited() -> None:
    run = _run(_ok_result("silueta", cache_hit=True))

    html = render_report_html(run)

    assert "cache-hit" in html
    assert "may be edited" in html


def test_report_shows_run_level_warnings() -> None:
    run = _run(
        _ok_result("silueta"),
        warnings=("frame_count differs across models: silueta=30, u2net=29",),
    )

    html = render_report_html(run)

    assert "frame_count differs across models" in html


def test_report_omits_the_warnings_block_when_there_are_none() -> None:
    run = _run(_ok_result("silueta"))

    html = render_report_html(run)

    assert '<div class="warnings"' not in html


def test_report_escapes_a_malicious_error_string() -> None:
    failed = ModelResult(
        model_id="u2net",
        display_name="U2Net",
        status="error",
        error=_MALICIOUS_ERROR,
    )

    html = render_report_html(_run(failed))

    assert _MALICIOUS_ERROR not in html
    assert "&lt;script&gt;" in html
    assert 'data-model="u2net"' in html


def test_report_has_a_background_toggle_and_a_restart_button() -> None:
    html = render_report_html(_run(_ok_result("silueta")))

    assert 'id="bg-select"' in html
    assert 'id="restart-all"' in html
    for background in ("checkerboard", "black", "white", "green"):
        assert f'value="{background}"' in html


def test_report_is_self_contained_with_no_external_resources() -> None:
    html = render_report_html(_run(_ok_result("bria-rmbg")))

    assert "http://" not in html
    assert "https://" not in html
    assert "<link " not in html
