import json
import sys
from pathlib import Path

import pytest

from scripts import check_guardrails


def _prepare_checker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path]:
    root = tmp_path / "project"
    source_dir = root / "src"
    scripts_dir = root / "scripts"
    source_dir.mkdir(parents=True)
    scripts_dir.mkdir()
    module_path = source_dir / "fixture.py"
    baseline_path = scripts_dir / "guardrails-baseline.json"
    monkeypatch.setattr(check_guardrails, "ROOT", root)
    monkeypatch.setattr(check_guardrails, "BASELINE_PATH", baseline_path)
    monkeypatch.setattr(sys, "argv", ["check_guardrails.py"])
    return module_path, baseline_path


def _write_fixture(
    module_path: Path, function_count: int, extra_lines: int = 0
) -> None:
    body = "    value = 0\n" * 61
    functions = "\n".join(
        f"def long_function_{index}():\n{body}    return value"
        for index in range(function_count)
    )
    growth = "# growth\n" * extra_lines
    module_path.write_text(f"{functions}\n{growth}", encoding="utf-8")


def _write_baseline(baseline_path: Path) -> None:
    baseline_path.write_text(
        json.dumps(check_guardrails._collect()), encoding="utf-8"
    )


def test_under_budget_module_growth_does_not_fail_line_ratchet(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module_path, baseline_path = _prepare_checker(tmp_path, monkeypatch)
    _write_fixture(module_path, function_count=1)
    _write_baseline(baseline_path)

    _write_fixture(module_path, function_count=1, extra_lines=3)

    assert check_guardrails.main() == 0
    assert (
        "Guardrail check passed (0 module(s) over budget)."
        in capsys.readouterr().out
    )


def test_under_budget_module_still_ratchets_new_long_functions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    module_path, baseline_path = _prepare_checker(tmp_path, monkeypatch)
    _write_fixture(module_path, function_count=1)
    _write_baseline(baseline_path)

    _write_fixture(module_path, function_count=2)

    assert check_guardrails.main() == 1
    assert "over-long functions grew from 1 to 2" in capsys.readouterr().err
