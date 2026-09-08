import re
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_ci_uses_immutable_actions_and_pinned_uv() -> None:
    workflow = (REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    action_refs = re.findall(
        r"^\s+- uses: (\S+)(?:\s+#.*)?$", workflow, re.MULTILINE
    )

    assert [ref.split("@", maxsplit=1)[0] for ref in action_refs] == [
        "actions/checkout",
        "astral-sh/setup-uv",
        "actions/checkout",
        "astral-sh/setup-uv",
        "actions/checkout",
        "astral-sh/setup-uv",
        "actions/checkout",
        "astral-sh/setup-uv",
    ]
    assert all(
        re.fullmatch(r"[^@]+@[0-9a-f]{40}", ref) for ref in action_refs
    )
    assert workflow.count('version: "0.11.32"') == 4


def test_ci_runs_without_syncing_or_building_dependencies() -> None:
    workflow = (REPOSITORY_ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    run_commands = re.findall(r"^\s+- run: (uv run .+)$", workflow, re.MULTILINE)

    assert run_commands == [
        "uv run --frozen --no-sync --no-build python scripts/check_guardrails.py",
        "uv run --frozen --no-sync --no-build ruff check .",
        "uv run --frozen --no-sync --no-build mypy src",
        "uv run --frozen --no-sync --no-build pytest -q ${{ matrix.paths }}",
        "uv run --frozen --no-sync --no-build pytest -q --durations=15 "
        "tests/jobs tests/core tests/test_resources.py",
    ]


def test_dependabot_updates_github_action_pins() -> None:
    """Assert what the file must do, not how it happens to be written.

    A pin without an updater rots, so the ecosystem and a schedule are the
    contract. Comparing the whole file made every legitimate edit — such as
    raising the pull-request limit — read as a failure.
    """
    dependabot = (REPOSITORY_ROOT / ".github" / "dependabot.yml").read_text(
        encoding="utf-8"
    )

    assert 'package-ecosystem: "github-actions"' in dependabot
    assert 'directory: "/"' in dependabot
    assert "schedule:" in dependabot
    assert "interval:" in dependabot
