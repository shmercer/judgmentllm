"""Hosted matrix admission preserves full gates for routine and manual lanes."""

from __future__ import annotations

import ast
import json
import re
import tomllib
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

REPOSITORY = Path(__file__).resolve().parents[2]
WORKFLOW = REPOSITORY / ".github/workflows/ci.yml"
ROUTINE = {
    ("ubuntu-latest", "3.11"),
    ("ubuntu-latest", "3.15"),
    ("macos-latest", "3.11"),
    ("macos-latest", "3.15"),
    ("windows-latest", "3.12"),
}


def workflow() -> dict[str, Any]:
    # BaseLoader preserves the literal `on` key instead of YAML 1.1 booleans.
    return cast(dict[str, Any], yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader))


def branches() -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    matrix = workflow()["jobs"]["baseline"]["strategy"]["matrix"]
    assert set(matrix) == {"include"}
    # actionlint independently validates expression syntax/types. This contract
    # protects the event guard, typed boolean and both explicit configuration lists.
    matched = re.fullmatch(
        r"\$\{\{\s*fromJSON\(\s*github\.event_name\s*==\s*'workflow_dispatch'\s*"
        r"&&\s*inputs\.windows_311_only\s*&&\s*'([^']+)'\s*\|\|\s*'([^']+)'\s*\)\s*\}\}",
        matrix["include"],
    )
    assert matched is not None
    return json.loads(matched[1]), json.loads(matched[2])


def test_manual_dispatch_uses_one_shared_full_job_and_boolean_opt_in() -> None:
    parsed = workflow()
    assert set(parsed["jobs"]) == {"baseline"}
    assert "if" not in parsed["jobs"]["baseline"]
    assert parsed["permissions"] == {"contents": "read"}
    assert parsed["on"]["workflow_dispatch"]["inputs"]["windows_311_only"] == {
        "description": "Run only Windows / Python 3.11 troubleshooting",
        "type": "boolean",
        "required": "false",
        "default": "false",
    }


@pytest.mark.parametrize(
    "event,flag,expected",
    [
        ("push", False, ROUTINE),
        ("pull_request", False, ROUTINE),
        ("push", True, ROUTINE),
        ("pull_request", True, ROUTINE),
        ("workflow_dispatch", False, ROUTINE),
        ("workflow_dispatch", True, {("windows-latest", "3.11")}),
    ],
)
def test_matrix_contract(
    event: str, flag: bool, expected: set[tuple[str, str]]
) -> None:
    manual, routine = branches()
    selected = manual if event == "workflow_dispatch" and flag else routine
    actual = {(item["os"], item["python"]) for item in selected}
    assert actual == expected
    assert len(actual) == len(selected)


def test_full_job_quality_r_and_resource_gates_are_preserved() -> None:
    job = workflow()["jobs"]["baseline"]
    assert job["timeout-minutes"] == "20"
    assert job["strategy"]["fail-fast"] == "false"
    steps = job["steps"]
    assert len({step["name"] for step in steps}) == len(steps)
    commands = {
        "Install locked development and optional dependencies": "uv sync --locked --all-extras --group dev",
        "Lint": "uv run --offline --no-sync ruff check .",
        "Check formatting": "uv run --offline --no-sync ruff format --check .",
        "Check types": "uv run --offline --no-sync pyright",
        "Run offline tests with coverage": "uv run --offline --no-sync pytest -ra -Werror::ResourceWarning -Werror::pytest.PytestUnraisableExceptionWarning",
        "Build distributions": "uv build --offline",
        "Validate distribution metadata": "uv run --offline --no-sync twine check --strict dist/*",
        "Verify clean core wheel installation": "uv run --offline --no-sync python scripts/smoke_install.py dist/*.whl",
    }
    for name, command in commands.items():
        step = next(step for step in steps if step["name"] == name)
        assert step["run"] == command
        assert "if" not in step and step.get("continue-on-error", "false") == "false"
    r_gates = [step for step in steps if step["name"].startswith("Verify required ")]
    assert len(r_gates) == 6
    for step in r_gates:
        assert step["if"] == "matrix.os == 'ubuntu-latest' && matrix.python == '3.11'"
        assert step["env"]["JUDGMENTLLM_R_STRICT"] == "1"
    windows = [
        step
        for step in steps
        if step["name"]
        in {
            "Verify Windows export durability and cancellation recovery",
            "Observe Windows file-signature metadata",
            "Verify Windows live privacy and lock behavior",
        }
    ]
    assert len(windows) == 3
    assert all(step["if"] == "runner.os == 'Windows'" for step in windows)
    assert all(
        re.search(r"@[0-9a-f]{40}$", step["uses"]) for step in steps if "uses" in step
    )
    project = tomllib.loads((REPOSITORY / "pyproject.toml").read_text())
    assert project["tool"]["coverage"]["report"]["fail_under"] == 90
    assert "--cov-fail-under=90" in project["tool"]["pytest"]["ini_options"]["addopts"]
    for name in ("test_execution_resources.py", "test_grouped_resources.py"):
        tree = ast.parse((REPOSITORY / "tests" / name).read_text())
        limits = [
            keyword.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "subprocess"
            and node.func.attr == "run"
            for keyword in node.keywords
            if keyword.arg == "timeout"
        ]
        assert limits and all(
            isinstance(limit, ast.Constant) and limit.value == 180 for limit in limits
        )
