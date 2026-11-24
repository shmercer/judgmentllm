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
import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast
from unittest.mock import Mock, patch

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
    assert (
        data["concurrency"]["cancel-in-progress"]
        == "${{ github.event_name == 'pull_request_target' }}"
    )
    assert "github.run_id" in data["concurrency"]["group"]
    assert (
        data["env"]["PURPOSE"]
        == "${{ github.event_name == 'push' && 'main-linux' || 'pr' }}"
    )
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
    assert verify["timeout-minutes"] == (
        "${{ (matrix.id == 'windows-3.11' || matrix.id == 'windows-3.12') && matrix.profile == 'full' && matrix.partition == 'ordinary' && 120 || 90 }}"
    )
    assert plan["timeout-minutes"] == "15"
    assert data["jobs"]["acceptance"]["timeout-minutes"] == "5"
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
    assert 'partition=(--partition "${{ matrix.partition }}")' in lane["run"]
    assert "matrix.partition" in verify["name"]
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
        "needs.plan.outputs.packaging == 'true' && matrix.partition != 'serial' && "
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


class WindowsSmokeChecks(unittest.TestCase):
    """Offline policy tests; native ownership proof is explicitly platform-bound."""

    def setUp(self) -> None:
        import windows_smoke

        self.smoke = windows_smoke
        temporary = TemporaryDirectory(prefix="windows-smoke-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.nodes = windows_smoke.expected_nodes()
        self.source: dict[str, Any] = {
            "commit": "a" * 40,
            "dirty": False,
            "lockfile_sha256": "b" * 64,
            "dependencies": {
                name: "synthetic"
                for name in (
                    "pytest",
                    "pytest-xdist",
                    "pytest-cov",
                    "coverage",
                    "openai",
                    "pydantic",
                    "psutil",
                )
            },
        }
        self.expected = windows_smoke.receipt_provenance(
            self.source, "123:1:windows-3.12:ordinary:windows-smoke", "attempt"
        )
        collected: list[dict[str, object]] = [
            {"nodeid": node, "markers": []} for node in sorted(self.nodes)
        ]
        reports: list[dict[str, object]] = [
            {
                "nodeid": node,
                "phase": phase,
                "outcome": "passed",
                "xfail": False,
            }
            for node in sorted(self.nodes)
            for phase in ("setup", "call", "teardown")
        ]
        self.report: dict[str, Any] = {
            "schema_version": 1,
            "exit_code": 0,
            "execution_group": "windows-smoke",
            "provenance": self.expected,
            "collected": collected,
            "reports": reports,
        }

    def test_scope_order_timeouts_and_existing_acceptance(self) -> None:
        from risk import COMPREHENSIVE

        data = workflow()
        verify = data["jobs"]["verify"]
        steps = verify["steps"]
        by_name = {step.get("name"): step for step in steps}
        smoke = by_name["Early offline Windows smoke"]
        condition = "(matrix.id == 'windows-3.11' || matrix.id == 'windows-3.12') && matrix.partition == 'ordinary'"
        self.assertEqual(smoke["if"], condition)
        self.assertEqual(smoke["timeout-minutes"], "6")
        self.assertEqual(self.smoke.WORK_SECONDS, 300)
        self.assertLess(
            steps.index(by_name["Seed Windows ordinary summary"]),
            steps.index(by_name["Check out trusted policy"]),
        )
        self.assertLess(
            steps.index(by_name["Prepare locked environment"]), steps.index(smoke)
        )
        self.assertLess(
            steps.index(smoke),
            steps.index(by_name["Prepare offline core installation cache"]),
        )
        self.assertLess(
            steps.index(smoke), steps.index(by_name["Execute planned checks"])
        )
        self.assertTrue(
            by_name["Summarize Windows ordinary diagnostics"]["if"].startswith(
                "always()"
            )
        )
        self.assertEqual(
            {row["id"] for row in COMPREHENSIVE if row["id"].startswith("windows-")},
            {"windows-3.11", "windows-3.12"},
        )
        self.assertEqual(set(data["jobs"]), {"plan", "verify", "acceptance"})
        self.assertNotIn("windows_smoke", json.dumps(data["jobs"]["acceptance"]))
        for step in (smoke, by_name["Summarize Windows ordinary diagnostics"]):
            command = step["run"]
            self.assertIn("policy/ci/windows_smoke.py", command)
            self.assertNotIn("candidate/ci/windows_smoke.py", command)
            self.assertLess(
                command.index("Get-ChildItem Env:"),
                command.index("& $python -I $helper @arguments"),
            )
            self.assertNotIn("${{", command)
        self.assertIn("exit $code", smoke["run"])
        self.assertIn("$code = 1", smoke["run"])
        paths = by_name["Preserve diagnostics even on failure"]["with"]["path"]
        self.assertIn("windows-smoke/cases.json", paths)
        self.assertNotIn("windows-smoke/t", paths)

    def test_final_summary_wrapper_cannot_fail_job(self) -> None:
        steps = workflow()["jobs"]["verify"]["steps"]
        summary_step = next(
            step
            for step in steps
            if step.get("name") == "Summarize Windows ordinary diagnostics"
        )
        script = summary_step["run"]
        self.assertTrue(summary_step["if"].startswith("always()"))
        self.assertEqual(summary_step["shell"], "pwsh")
        self.assertTrue(script.startswith("try {\n  $ErrorActionPreference = 'Stop'\n"))
        self.assertTrue(script.endswith("} finally {\n  exit 0\n}\n"))
        self.assertEqual(script.count("exit 0"), 1)
        self.assertIn(
            '} catch {\n  try {\n    [IO.File]::AppendAllText($summary, "', script
        )
        self.assertIn("\n  } catch {}\n} finally {\n  exit 0\n}", script)
        self.assertNotIn("$_", script)
        self.assertNotIn("Write-Error", script)

        pwsh = shutil.which("pwsh")
        if pwsh is None:
            self.skipTest(
                "Static assertions passed; native pwsh unavailable: behavior pending"
            )
        native = Path(pwsh).resolve()
        trusted = (
            Path("/usr/bin"),
            Path("/usr/local/bin"),
            Path("/opt/microsoft/powershell"),
            Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "PowerShell",
        )
        if not any(native.is_relative_to(path) for path in trusted):
            self.skipTest(
                "Static assertions passed; native pwsh path untrusted: behavior pending"
            )
        magic = native.read_bytes()[:4]
        self.assertTrue(magic == b"\x7fELF" or magic[:2] == b"MZ")
        fallback = self.smoke.FALLBACK
        scenarios = (("success", 0), ("native-failure", 7), ("fallback-failure", 7))
        for name, renderer_exit in scenarios:
            with self.subTest(name=name):
                workspace = self.root / name
                python = workspace / "candidate/.venv/Scripts/python.exe"
                python.parent.mkdir(parents=True)
                try:
                    python.symlink_to(sys.executable)
                except OSError:
                    self.skipTest(
                        "Native executable link unavailable: behavior pending"
                    )
                helper = workspace / "policy/ci/windows_smoke.py"
                helper.parent.mkdir(parents=True)
                helper.write_text(
                    "import sys\nfrom pathlib import Path\n"
                    f"code = {renderer_exit}\n"
                    "if code == 0:\n"
                    "    Path(sys.argv[sys.argv.index('--summary') + 1]).write_text('rendered\\n')\n"
                    "raise SystemExit(code)\n"
                )
                output = workspace / (
                    "missing/summary.md" if name == "fallback-failure" else "summary.md"
                )
                temporary = workspace / "temp"
                temporary.mkdir()
                environment = self.smoke.clean_environment()
                environment.update(
                    GITHUB_WORKSPACE=str(workspace),
                    RUNNER_TEMP=str(temporary),
                    GITHUB_STEP_SUMMARY=str(output),
                    SOURCE_SHA="a" * 40,
                    LANE_ID="windows-3.12",
                    RUN_ID="123:1",
                    CHECKS_OUTCOME="failure",
                    UPLOAD_OUTCOME="failure",
                    HOME=str(workspace),
                    TMPDIR=str(temporary),
                    TMP=str(temporary),
                    TEMP=str(temporary),
                )
                wrapper = workspace / "summary.ps1"
                wrapper.write_text(
                    "$ErrorActionPreference = 'stop'\n"
                    + script
                    + "\nif ((Test-Path -LiteralPath variable:\\LASTEXITCODE)) { exit $LASTEXITCODE }\n"
                )
                result = subprocess.run(
                    [
                        str(native),
                        "-NoLogo",
                        "-NoProfile",
                        "-NonInteractive",
                        "-File",
                        str(wrapper),
                    ],
                    cwd=workspace,
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                )
                self.assertEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, "")
                if name == "fallback-failure":
                    self.assertFalse(output.exists())
                else:
                    self.assertEqual(
                        output.read_text(),
                        "rendered\n" if renderer_exit == 0 else fallback,
                    )

    def test_exact_fourteen_passes(self) -> None:
        self.assertEqual(len(self.nodes), 14)
        self.smoke.validate_report(self.report, self.expected, self.nodes)

    def test_zero_exit_cannot_hide_incomplete_or_unsuccessful_cases(self) -> None:
        for fault in (
            "skip",
            "xfail",
            "missing-call",
            "missing-teardown",
            "duplicate-phase",
            "duplicate-node",
            "missing-node",
            "extra-node",
            "isolation",
            "markers-not-list",
            "markers-not-string",
            "exit-bool",
            "schema-bool",
            "group",
            "phase",
            "xfail-type",
        ):
            with self.subTest(fault=fault):
                report = deepcopy(self.report)
                if fault == "skip":
                    report["reports"][1]["outcome"] = "skipped"
                elif fault == "xfail":
                    report["reports"][1]["xfail"] = True
                elif fault == "missing-call":
                    report["reports"].pop(1)
                elif fault == "missing-teardown":
                    report["reports"].pop(2)
                elif fault == "duplicate-phase":
                    report["reports"].append(report["reports"][0])
                elif fault == "duplicate-node":
                    report["collected"].append(report["collected"][0])
                elif fault == "missing-node":
                    report["collected"].pop()
                elif fault == "extra-node":
                    report["collected"][0]["nodeid"] = (
                        "tests/test_untrusted.py::test_decoy"
                    )
                elif fault == "isolation":
                    report["collected"][0]["markers"] = ["serial"]
                elif fault == "markers-not-list":
                    report["collected"][0]["markers"] = "serial"
                elif fault == "markers-not-string":
                    report["collected"][0]["markers"] = [1]
                elif fault == "exit-bool":
                    report["exit_code"] = False
                elif fault == "schema-bool":
                    report["schema_version"] = True
                elif fault == "group":
                    report["execution_group"] = "ordinary"
                elif fault == "phase":
                    report["reports"][0]["phase"] = "unknown"
                else:
                    report["reports"][0]["xfail"] = "false"
                with self.assertRaises((ValueError, KeyError, TypeError)):
                    self.smoke.validate_report(report, self.expected, self.nodes)

    def test_every_provenance_component_is_compared(self) -> None:
        fields = [(key,) for key in self.expected]
        fields += [("source", key) for key in self.expected["source"]]
        fields += [("interpreter", key) for key in self.expected["interpreter"]]
        fields += [
            ("interpreter", "dependencies", key)
            for key in self.expected["interpreter"]["dependencies"]
        ]
        for parts in fields:
            with self.subTest(parts=parts):
                report = deepcopy(self.report)
                parent = report["provenance"]
                for part in parts[:-1]:
                    parent = parent[part]
                parent[parts[-1]] = "foreign"
                with self.assertRaises(ValueError):
                    self.smoke.validate_report(report, self.expected, self.nodes)

    def test_documents_fail_closed(self) -> None:
        path = self.root / "cases.json"
        for content in ('{"exit_code":0,"exit_code":1}', "[", "[]"):
            path.write_text(content)
            with self.assertRaises((ValueError, TypeError)):
                self.smoke.document(path)
        path.unlink()
        with self.assertRaises(ValueError):
            self.smoke.document(path)
        target = self.root / "target.json"
        target.write_text("{}")
        path.symlink_to(target)
        with self.assertRaises(ValueError):
            self.smoke.document(path)

    def test_single_close_and_remaining_budget_on_every_return_path(self) -> None:
        import psutil

        for fault in ("none", "timeout", "close", "ownership", "expired"):
            with self.subTest(fault=fault):
                owned = Mock(pid=42)
                owned.owns.return_value = fault != "ownership"
                process = Mock()
                process.wait.return_value = 0
                if fault == "timeout":
                    process.wait.side_effect = psutil.TimeoutExpired(9)
                if fault == "close":
                    owned.close.side_effect = TimeoutError("synthetic cleanup")
                status = {"cleanup": "unconfirmed"}
                with (
                    patch.object(self.smoke, "WindowsProcess", return_value=owned),
                    patch.object(psutil, "Process", return_value=process),
                    patch.object(self.smoke.time, "monotonic", return_value=1),
                    (self.root / "log").open("wb") as stream,
                ):
                    if fault == "none":
                        self.assertEqual(
                            self.smoke.execute(
                                ["python"], self.root, stream, 10, status
                            ),
                            0,
                        )
                        process.wait.assert_called_once_with(timeout=9)
                    else:
                        with self.assertRaises(
                            (ValueError, TimeoutError, psutil.TimeoutExpired)
                        ):
                            self.smoke.execute(
                                ["python"],
                                self.root,
                                stream,
                                0 if fault == "expired" else 10,
                                status,
                            )
                owned.close.assert_called_once_with()
                owned.terminate.assert_not_called()
                self.assertEqual(
                    status["cleanup"],
                    "unconfirmed" if fault == "close" else "confirmed",
                )

    def test_constructor_failure_has_no_second_caller_cleanup(self) -> None:
        with (
            patch.object(
                self.smoke,
                "WindowsProcess",
                side_effect=OSError("synthetic constructor failure"),
            ) as constructor,
            (self.root / "log").open("wb") as stream,
        ):
            with self.assertRaises(OSError):
                self.smoke.execute(["python"], self.root, stream, 10, {})
            constructor.assert_called_once()
        tree = ast.parse((REPOSITORY / "scripts/cli_process_windows.py").read_text())
        methods = {
            node.name: node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
        }
        self.assertEqual(ast.literal_eval(methods["terminate"].args.defaults[0]), 10)
        constructor_source = ast.get_source_segment(
            (REPOSITORY / "scripts/cli_process_windows.py").read_text(),
            methods["__init__"],
        )
        self.assertIn(
            "WaitForSingleObject(self._process, 10000)", constructor_source or ""
        )
        self.assertEqual((constructor_source or "").count("self.close()"), 1)

    def test_run_checks_reports_even_with_zero_process_exit(self) -> None:
        from local.preflight_provenance import ATTEMPT_ENV, RUN_ID_ENV

        source = self.root / "source"
        source.mkdir()
        plan = self.root / "plan.json"
        plan.write_text(
            json.dumps(
                {
                    "source_sha": "a" * 40,
                    "lockfile_sha256": "b" * 64,
                    "windows_crlf_lockfile_sha256": "c" * 64,
                    "lanes": [
                        {"id": "windows-3.12", "os": "windows-latest", "python": "3.12"}
                    ],
                    "hosted_tasks": [{"id": "windows-3.12", "partition": "ordinary"}],
                }
            )
        )
        for fault in ("none", "skip", "absent", "source-change"):
            with self.subTest(fault=fault):
                output = self.root / fault

                def executed(
                    command: list[str],
                    cwd: Path,
                    stream: Any,
                    deadline: float,
                    status: dict[str, str],
                    fault: str = fault,
                    output: Path = output,
                ) -> int:
                    self.assertEqual(os.environ["PYTHONPATH"], str(REPOSITORY))
                    self.assertNotIn("UNTRUSTED_SECRET", os.environ)
                    self.assertEqual(
                        command[:6],
                        [
                            sys.executable,
                            "-P",
                            "-m",
                            "pytest",
                            "-p",
                            "ci.local.pytest_report",
                        ],
                    )
                    report = deepcopy(self.report)
                    report["provenance"] = self.smoke.receipt_provenance(
                        self.source, os.environ[RUN_ID_ENV], os.environ[ATTEMPT_ENV]
                    )
                    if fault == "skip":
                        report["reports"][1]["outcome"] = "skipped"
                    if fault != "absent":
                        (output / "cases.json").write_text(json.dumps(report))
                    status["cleanup"] = "confirmed"
                    return 0

                after = (
                    {**self.source, "dirty": True}
                    if fault == "source-change"
                    else self.source
                )
                with (
                    patch.dict(os.environ, {"UNTRUSTED_SECRET": "synthetic"}),
                    patch.object(
                        self.smoke,
                        "sys",
                        Mock(
                            platform="win32",
                            version_info=Mock(major=3, minor=12),
                            executable=sys.executable,
                        ),
                    ),
                    patch.object(
                        self.smoke, "source_record", side_effect=[self.source, after]
                    ),
                    patch.object(self.smoke, "execute", side_effect=executed),
                ):
                    code = self.smoke.run_smoke(
                        source, output, plan, "a" * 40, "windows-3.12", "123:1"
                    )
                self.assertEqual(code, int(fault != "none"))

    def test_rendering_cannot_mask_the_check_result_or_leak_raw_fields(self) -> None:
        status = self.root / "status.json"
        sentinel = "SECRET_SENTINEL\n::error:: <script> `unsafe`"
        status.write_text(
            json.dumps({"outcome": "failed", "cleanup": "confirmed", "error": sentinel})
        )
        with self.assertRaises(ValueError):
            self.smoke.summary_text(
                self.root,
                self.root / "v",
                "a" * 40,
                "windows-3.12",
                "123:1",
                "failure",
                "failure",
            )
        status.write_text(json.dumps({"outcome": "failed", "cleanup": "confirmed"}))
        text = self.smoke.summary_text(
            self.root,
            self.root / "v",
            "a" * 40,
            "windows-3.12",
            "123:1",
            "failure",
            "failure",
        )
        self.assertIn("unexecuted/incomplete", text)
        self.assertNotIn(sentinel, text)
        for code in (0, 1):
            summary = self.root / f"summary-{code}"
            arguments = [
                "windows_smoke.py",
                "run",
                "--source",
                str(self.root),
                "--output",
                str(self.root / "out"),
                "--plan",
                str(self.root / "plan"),
                "--head",
                "a" * 40,
                "--lane",
                "windows-3.12",
                "--run-id",
                "123:1",
                "--summary",
                str(summary),
            ]
            with (
                patch.object(sys, "argv", arguments),
                patch.object(self.smoke, "run_smoke", return_value=code),
                patch.object(
                    self.smoke, "summary_text", side_effect=ValueError(sentinel)
                ),
            ):
                self.assertEqual(self.smoke.main(), code)
            self.assertEqual(summary.read_text(), self.smoke.FALLBACK)

    def test_isolated_helper_ignores_candidate_and_inherited_pythonpath(self) -> None:
        candidate = self.root / "candidate"
        decoy = candidate / "ci/windows_smoke.py"
        decoy.parent.mkdir(parents=True)
        decoy.write_text("raise SystemExit('UNTRUSTED_HELPER')\n")
        (candidate / "sitecustomize.py").write_text(
            "raise SystemExit('UNTRUSTED_STARTUP')\n"
        )
        summary = self.root / "summary"
        environment = self.smoke.clean_environment()
        environment["PYTHONPATH"] = str(candidate)
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                str(REPOSITORY / "ci/windows_smoke.py"),
                "summary",
                "--source",
                str(candidate),
                "--output",
                str(self.root / "missing"),
                "--head",
                "a" * 40,
                "--lane",
                "windows-3.12",
                "--run-id",
                "123:1",
                "--summary",
                str(summary),
            ],
            cwd=candidate,
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(summary.read_text(), self.smoke.FALLBACK)
        self.assertNotIn("UNTRUSTED", result.stdout + result.stderr)

    def test_full_summary_requires_consistent_existing_completion(self) -> None:
        (self.root / "status.json").write_text(
            json.dumps({"outcome": "passed", "cleanup": "confirmed"})
        )
        evidence = self.root / "v"
        (evidence / "suite").mkdir(parents=True)
        receipt = {
            "source_sha": "a" * 40,
            "lane": "windows-3.12",
            "run_id": "123:1",
            "partition": "ordinary",
            "status": "passed",
            "phase": "complete",
            "execution_scope": "hosted",
            "source_verified": True,
            "error": "SECRET_SENTINEL ::error:: <script>",
        }
        (evidence / "suite/summary.json").write_text(
            json.dumps(
                {
                    "status": "passed",
                    "selection_complete": True,
                    "source": {"commit": "a" * 40, "dirty": False},
                }
            )
        )
        path = evidence / "receipt.json"
        path.write_text(json.dumps(receipt))
        text = self.smoke.summary_text(
            self.root, evidence, "a" * 40, "windows-3.12", "123:1", "success", "success"
        )
        self.assertIn("completion reported", text)
        self.assertNotIn("SECRET_SENTINEL", text)
        for key, value in (
            ("source_verified", 1),
            ("partition", "serial"),
            ("run_id", "123:2"),
            ("source_sha", "d" * 40),
            ("phase", "quality"),
        ):
            with self.subTest(key=key):
                path.write_text(json.dumps({**receipt, key: value}))
                with self.assertRaises(ValueError):
                    self.smoke.summary_text(
                        self.root,
                        evidence,
                        "a" * 40,
                        "windows-3.12",
                        "123:1",
                        "success",
                        "success",
                    )

    @unittest.skipUnless(
        sys.platform == "win32", "requires actual Windows Job Object ownership"
    )
    def test_native_timeout_joins_descendants_and_preserves_sentinel(self) -> None:
        import psutil

        with (self.root / "native.log").open("wb") as stream:
            sentinel = self.smoke.WindowsProcess(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                self.root,
                stream,
                stream,
            )
            child = self.root / "child.pid"
            command = [
                sys.executable,
                "-c",
                "import pathlib,subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); pathlib.Path(sys.argv[1]).write_text(str(p.pid)); time.sleep(30)",
                str(child),
            ]
            try:
                status = {"cleanup": "unconfirmed"}
                with self.assertRaises(psutil.TimeoutExpired):
                    self.smoke.execute(
                        command,
                        self.root,
                        stream,
                        self.smoke.time.monotonic() + 2,
                        status,
                    )
                self.assertEqual(status["cleanup"], "confirmed")
                self.assertTrue(sentinel.owns(sentinel.pid))
                self.assertTrue(
                    child.exists(), "child must have started before timeout"
                )
                self.assertFalse(psutil.pid_exists(int(child.read_text())))
            finally:
                sentinel.close()
