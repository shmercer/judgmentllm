"""Hosted matrix admission preserves full gates for routine and manual lanes."""

from __future__ import annotations

import ast
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
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


def test_hosted_preflights_share_only_the_current_job_attempt_identity() -> None:
    job = workflow()["jobs"]["baseline"]
    assert job["env"]["JUDGMENTLLM_CI_RUN_ID"] == (
        "${{ github.run_id }}:${{ github.run_attempt }}:${{ matrix.os }}:${{ matrix.python }}"
    )
    for step in job["steps"]:
        if "--ci-report" in step.get("run", "") or "ci/local/suite.py" in step.get(
            "run", ""
        ):
            assert "JUDGMENTLLM_CI_RUN_ID" not in step.get("env", {})


def test_full_job_quality_r_and_resource_gates_are_preserved() -> None:
    job = workflow()["jobs"]["baseline"]
    # Fifty minutes is the approved hosted ceiling; the target stays under twenty.
    # The resource fixtures below retain their independent 180-second deadline.
    assert job["timeout-minutes"] == "50"
    assert job["strategy"]["fail-fast"] == "false"
    steps = job["steps"]
    assert len({step["name"] for step in steps}) == len(steps)
    commands = {
        "Install locked development and optional dependencies": "uv sync --locked --all-extras --group dev",
        "Lint": "uv run --offline --no-sync ruff check .",
        "Check formatting": "uv run --offline --no-sync ruff format --check .",
        "Check types": "uv run --offline --no-sync pyright",
        "Build distributions": "uv build --offline",
        "Validate distribution metadata": "uv run --offline --no-sync twine check --strict dist/*",
        "Verify clean core wheel installation": "uv run --offline --no-sync python scripts/smoke_install.py dist/*.whl",
    }
    for name, command in commands.items():
        step = next(step for step in steps if step["name"] == name)
        assert step["run"] == command
        assert "if" not in step and step.get("continue-on-error", "false") == "false"
    r_gates = [step for step in steps if step["name"].startswith("Verify required ")]
    assert len(r_gates) == 7
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


@pytest.mark.parametrize(
    "os_name,python", sorted(ROUTINE | {("windows-latest", "3.11")})
)
def test_exactly_one_full_suite_with_correct_coverage(
    os_name: str, python: str
) -> None:
    steps = workflow()["jobs"]["baseline"]["steps"]
    primary = os_name == "ubuntu-latest" and python == "3.11"
    full_tests = [
        step for step in steps if step["name"].startswith("Run offline tests ")
    ]
    assert len(full_tests) == 2
    coverage, plain = full_tests
    assert coverage["name"] == "Run offline tests with coverage"
    assert coverage["if"] == "matrix.os == 'ubuntu-latest' && matrix.python == '3.11'"
    assert plain["name"] == "Run offline tests without coverage"
    assert plain["if"] == "matrix.os != 'ubuntu-latest' || matrix.python != '3.11'"
    # The two complementary conditions select exactly one complete suite in
    # routine and optional manual configurations; no targets/filters are added.
    chosen = coverage if primary else plain
    expected = 'uv run --offline --no-sync python ci/local/suite.py --output "${{ runner.temp }}/judgmentllm-suite" --workers 4 --bound-workers --contract ci/test-contract.json '
    expected += (
        "--baseline ci/coverage-floor.json tests"
        if primary
        else '--no-coverage --preflight-root "${{ runner.temp }}/judgmentllm-preflights" tests'
    )
    assert chosen["run"] == expected
    assert all(step.get("continue-on-error", "false") == "false" for step in full_tests)


def test_coverage_configuration_and_failed_primary_evidence() -> None:
    steps = {step["name"]: step for step in workflow()["jobs"]["baseline"]["steps"]}
    artifact = steps["Preserve coverage report"]
    assert (
        artifact["if"]
        == "always() && matrix.os == 'ubuntu-latest' && matrix.python == '3.11'"
    )
    assert (
        artifact["with"]["path"] == "${{ runner.temp }}/judgmentllm-suite/coverage.xml"
    )
    codecov = steps["Upload coverage to Codecov when connected"]
    assert (
        codecov["if"]
        == "runner.os == 'Linux' && matrix.python == '3.11' && vars.CODECOV_ENABLED == 'true'"
    )
    assert codecov["with"] == {
        "files": "${{ runner.temp }}/judgmentllm-suite/coverage.xml",
        "disable_search": "true",
        "fail_ci_if_error": "false",
        "token": "${{ secrets.CODECOV_TOKEN }}",
    }
    project = tomllib.loads((REPOSITORY / "pyproject.toml").read_text())
    assert project["tool"]["pytest"]["ini_options"]["addopts"].split() == [
        "--strict-config",
        "--strict-markers",
        "--cov=judgmentllm",
        "--cov-branch",
        "--cov-report=term-missing",
        "--cov-report=xml",
        "--cov-fail-under=90",
    ]


def test_preflights_import_reporter_from_uninstalled_checkout(tmp_path: Path) -> None:
    launches: set[tuple[str, ...]] = set()
    preflights = [
        step
        for step in workflow()["jobs"]["baseline"]["steps"]
        if "--ci-report" in step.get("run", "")
    ]
    assert len(preflights) == 2
    for step in preflights:
        arguments = shlex.split(step["run"])
        assert arguments[:4] == ["uv", "run", "--offline", "--no-sync"]
        launches.add(tuple(arguments[4 : arguments.index("--ci-report")]))

    checkout = tmp_path / "uninstalled-checkout"
    plugin = checkout / "ci/local/pytest_report.py"
    plugin.parent.mkdir(parents=True)
    shutil.copyfile(REPOSITORY / "ci/local/pytest_report.py", plugin)
    shutil.copyfile(
        REPOSITORY / "ci/local/preflight_provenance.py",
        plugin.with_name("preflight_provenance.py"),
    )
    (checkout / "test_probe.py").write_text(
        "from pathlib import Path\n"
        "def test_reporter_is_from_checkout():\n"
        "    import ci.local.pytest_report as reporter\n"
        "    assert Path(reporter.__file__).resolve().is_relative_to(Path.cwd())\n"
    )
    environment = {
        key: os.environ[key]
        for key in (
            "PATH",
            "HOME",
            "LANG",
            "LC_ALL",
            "SystemRoot",
            "WINDIR",
            "TEMP",
            "TMP",
        )
        if key in os.environ
    }
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["JUDGMENTLLM_CI_RUN_ID"] = "workflow-reporter-regression"
    # Equivalent launch prefixes need only one real child startup. The checkout
    # is deliberately uninstalled and PYTHONPATH is absent, like hosted CI.
    for index, launch in enumerate(sorted(launches)):
        executable = (
            Path(sys.executable)
            if launch[0] == "python"
            else Path(sys.executable).parent
            / (launch[0] + (".exe" if os.name == "nt" else ""))
        )
        receipt = tmp_path / f"preflight-{index}.json"
        completed = subprocess.run(
            [
                str(executable),
                *launch[1:],
                f"--ci-report={receipt}",
                "-q",
                "-o",
                "addopts=",
                "-o",
                f"cache_dir={tmp_path / 'cache'}",
                f"--basetemp={tmp_path / ('child-' + str(index))}",
                "test_probe.py",
            ],
            cwd=checkout,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        reported = json.loads(receipt.read_text())
        assert reported["exit_code"] == 0
        assert (
            reported["provenance"]["qualification_run_id"]
            == environment["JUDGMENTLLM_CI_RUN_ID"]
        )
        assert [item["nodeid"] for item in reported["collected"]] == [
            "test_probe.py::test_reporter_is_from_checkout"
        ]
        assert [
            report["outcome"]
            for report in reported["reports"]
            if report["phase"] == "call"
        ] == ["passed"]
