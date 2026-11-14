"""Protected-base workflow, exact-head status and complete receipt admission."""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any, cast

import yaml

REPOSITORY = Path(__file__).resolve().parents[2]
WORKFLOW = REPOSITORY / ".github/workflows/ci.yml"
sys.path.insert(0, str(REPOSITORY / "ci"))


def workflow() -> dict[str, Any]:
    return cast(dict[str, Any], yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader))


def test_workflow_is_base_owned_and_only_aggregator_can_write_statuses() -> None:
    data = workflow()
    assert "pull_request_target" in data["on"] and "pull_request" not in data["on"]
    assert data["on"]["schedule"] == [{"cron": "17 8 * * 1"}]
    assert data["permissions"] == {"contents": "read"}
    assert data["concurrency"]["cancel-in-progress"] == "true"
    assert (
        data["env"]["SOURCE_SHA"]
        == "${{ github.event.pull_request.head.sha || github.sha }}"
    )
    assert (
        data["env"]["POLICY_SHA"]
        == "${{ github.event.pull_request.base.sha || github.sha }}"
    )
    assert data["env"]["BASE_REF"] == (
        "${{ github.event.pull_request.base.sha || github.event.before || (github.event_name == 'workflow_dispatch' && 'refs/remotes/origin/main') || github.sha }}"
    )
    for name, job in data["jobs"].items():
        if name != "acceptance":
            assert "permissions" not in job
        for step in job["steps"]:
            if "uses" in step:
                assert re.search(r"@[0-9a-f]{40}$", step["uses"])
            assert step.get("continue-on-error", "false") == "false"
            assert "secrets." not in json.dumps(step)
            if step.get("uses", "").startswith("actions/checkout@"):
                assert step["with"]["persist-credentials"] == "false"
    aggregator = data["jobs"]["acceptance"]
    assert aggregator["permissions"] == {"contents": "read", "statuses": "write"}
    assert aggregator["if"] == "always()"
    assert set(aggregator["needs"]) == {"plan", "verify"}
    status = next(
        step
        for step in aggregator["steps"]
        if step.get("name") == "Publish exact-head required status"
    )
    assert status["if"] == "always()"
    assert "/statuses/$SOURCE_SHA" in status["run"]
    assert 'context="$STATUS_CONTEXT"' in status["run"]
    assert status["env"]["STATUS_CONTEXT"] == (
        "${{ github.event_name == 'workflow_dispatch' && github.ref != 'refs/heads/main' && 'CI candidate policy' || 'CI acceptance' }}"
    )
    assert "job.status == 'success'" in status["env"]["STATE"]


def test_acceptance_status_includes_artifact_upload_outcome_and_is_last() -> None:
    steps = workflow()["jobs"]["acceptance"]["steps"]
    upload, status = steps[-2:]
    assert upload["uses"].startswith("actions/upload-artifact@")
    assert upload["if"] == "always()"
    assert upload["with"] == {
        "name": "acceptance",
        "path": "${{ runner.temp }}/acceptance.json",
        "if-no-files-found": "error",
    }
    # job.status now includes upload failure; no later step can fail after success.
    assert status["name"] == "Publish exact-head required status"
    assert status["if"] == "always()"
    assert status["env"]["STATE"] == (
        "${{ job.status == 'success' && 'success' || 'failure' }}"
    )


def test_quality_artifact_excludes_deliberately_unsafe_fixture_paths() -> None:
    upload = next(
        step
        for step in workflow()["jobs"]["plan"]["steps"]
        if step.get("name") == "Preserve plan and quality evidence"
    )
    assert upload["with"]["path"].splitlines() == [
        "${{ runner.temp }}/p/plan.json",
        "${{ runner.temp }}/p/quality/*.json",
        "${{ runner.temp }}/p/quality/*.log",
    ]
    assert upload["if"] == "always()"
    assert upload["with"]["if-no-files-found"] == "error"


def test_workflow_runs_quality_before_dynamic_matrix_and_keeps_diagnostics() -> None:
    data = workflow()
    plan = data["jobs"]["plan"]
    verify = data["jobs"]["verify"]
    assert verify["needs"] == "plan"
    assert verify["strategy"]["fail-fast"] == "false"
    assert verify["strategy"]["matrix"] == "${{ fromJSON(needs.plan.outputs.matrix) }}"
    assert verify["timeout-minutes"] == "50"
    commands = "\n".join(step.get("run", "") for step in plan["steps"])
    assert "python policy/ci/acceptance.py plan" in commands
    assert "--quality-only" in commands
    classify_step = next(step for step in plan["steps"] if step.get("id") == "plan")
    assert "github.event_name == 'schedule' && '4'" in classify_step["env"]["MINIMUM"]
    lane = next(
        step for step in verify["steps"] if step.get("name") == "Execute planned checks"
    )
    assert "policy/ci/acceptance.py lane" in lane["run"]
    assert "--quality-receipt" in lane["run"]
    for job in (plan, verify):
        install = next(
            step
            for step in job["steps"]
            if step.get("name") == "Prepare locked environment"
        )
        assert "risk != '0'" in install["if"]
    diagnostic = next(
        step
        for step in verify["steps"]
        if step.get("name") == "Preserve diagnostics even on failure"
    )
    assert diagnostic["if"] == "always()"
    assert diagnostic["with"]["if-no-files-found"] == "error"
    assert "suite/*.json" in diagnostic["with"]["path"]
    assert "suite/*.xml" in diagnostic["with"]["path"]
    assert "r_required" not in plan["outputs"]
    assert "Rscript" not in json.dumps(verify["steps"])
    assert "jsonlite" not in json.dumps(verify["steps"])


def test_linux_image_is_pinned_and_r_setup_has_a_bounded_failure() -> None:
    from risk import COMPREHENSIVE, PRIMARY, REPRESENTATIVE

    data = workflow()
    assert data["jobs"]["plan"]["runs-on"] == "ubuntu-24.04"
    assert data["jobs"]["acceptance"]["runs-on"] == "ubuntu-24.04"
    assert PRIMARY == {"id": "linux-3.11", "os": "ubuntu-24.04", "python": "3.11"}
    assert len(REPRESENTATIVE) == 3 and len(COMPREHENSIVE) == 8
    assert all(
        lane["os"] == "ubuntu-24.04"
        for lane in COMPREHENSIVE
        if lane["id"].startswith("linux-")
    )
    verify = data["jobs"]["verify"]
    assert verify["runs-on"] == "${{ matrix.os }}"
    # The historical test ID is retained; R preparation is now explicitly local.
    assert not any(
        step.get("uses", "").startswith("r-lib/") for step in verify["steps"]
    )
    assert "Rscript" not in json.dumps(verify)


def test_offline_package_cache_is_prepared_only_for_planned_installation_lanes() -> (
    None
):
    data = workflow()
    assert data["jobs"]["plan"]["outputs"]["packaging"] == (
        "${{ steps.plan.outputs.packaging }}"
    )
    steps = data["jobs"]["verify"]["steps"]
    prepare = next(
        step
        for step in steps
        if step.get("name") == "Prepare offline core installation cache"
    )
    assert prepare["if"] == (
        "needs.plan.outputs.packaging == 'true' && "
        "(matrix.id == 'linux-3.11' || needs.plan.outputs.risk == '4')"
    )
    assert prepare["shell"] == "bash"
    assert (
        "uv export --project candidate --locked --no-default-groups --no-emit-project"
        in prepare["run"]
    )
    assert (
        '--target "$RUNNER_TEMP/core-dependencies" --require-hashes' in prepare["run"]
    )
    assert '--requirements "$RUNNER_TEMP/core-requirements.txt"' in prepare["run"]
    execute = next(
        step for step in steps if step.get("name") == "Execute planned checks"
    )
    assert steps.index(prepare) < steps.index(execute)
    assert "--offline --no-sync" in execute["run"]


def test_original_coverage_floor_and_resource_deadlines_remain() -> None:
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


def test_preflights_import_reporter_from_uninstalled_checkout(tmp_path: Path) -> None:
    launches = {
        (sys.executable, "-m", "pytest", "--no-cov", "-p", "ci.local.pytest_report")
    }

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
