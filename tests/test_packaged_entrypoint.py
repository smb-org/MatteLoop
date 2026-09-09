from __future__ import annotations

import importlib.util
import runpy
import sys
from pathlib import Path
from types import ModuleType

import pytest


def _entrypoint_module() -> ModuleType:
    path = Path(__file__).parents[1] / "packaging" / "entrypoint.py"
    spec = importlib.util.spec_from_file_location("test_packaged_entrypoint", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_resource_tracker_interpreter_arguments_are_consumed() -> None:
    module = _entrypoint_module()
    argv = [
        "matteloop",
        "-S",
        "-s",
        "-c",
        "from multiprocessing.resource_tracker import main;main(0)",
    ]

    descriptor = module._prepare_multiprocessing_payload(argv)

    assert descriptor == 0
    assert argv == ["matteloop"]


def test_non_multiprocessing_interpreter_payload_is_rejected() -> None:
    module = _entrypoint_module()
    argv = ["matteloop", "-c", "print('arbitrary code')"]

    with pytest.raises(ValueError, match="refusing to execute"):
        module._prepare_multiprocessing_payload(argv)

    assert argv == ["matteloop", "-c", "print('arbitrary code')"]


def test_application_arguments_without_interpreter_code_are_preserved() -> None:
    module = _entrypoint_module()
    argv = ["matteloop", "--version"]

    payload = module._prepare_multiprocessing_payload(argv)

    assert payload is None
    assert argv == ["matteloop", "--version"]


def test_plain_application_argv_runs_velopack_before_the_application(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []

    class FakeApp:
        def set_auto_apply_on_startup(self, enabled: bool) -> FakeApp:
            calls.append(("auto_apply", enabled))
            return self

        def run(self) -> None:
            calls.append("run")

    def fake_main(argv: list[str] | None = None) -> int:
        calls.append(("main", argv))
        return 0

    velopack = ModuleType("velopack")
    velopack.App = FakeApp  # type: ignore[attr-defined]
    matteloop = ModuleType("matteloop")
    app = ModuleType("matteloop.app")
    app.main = fake_main  # type: ignore[attr-defined]
    matteloop.app = app  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "velopack", velopack)
    monkeypatch.setitem(sys.modules, "matteloop", matteloop)
    monkeypatch.setitem(sys.modules, "matteloop.app", app)
    monkeypatch.setattr(sys, "argv", ["matteloop", "--version"])

    with pytest.raises(SystemExit) as exited:
        runpy.run_path(
            str(Path(__file__).parents[1] / "packaging" / "entrypoint.py"),
            run_name="__main__",
        )

    assert exited.value.code == 0
    assert calls == [("auto_apply", False), "run", ("main", None)]
