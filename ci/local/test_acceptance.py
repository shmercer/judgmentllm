"""Real small orchestration runs and deliberate evidence/credential failures."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import acceptance
from risk import admit, make_plan


def freeze(source: Path) -> str:
    subprocess.run(["git", "add", "."], cwd=source, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=CI fixture",
            "-c",
            "user.email=ci@example.invalid",
            "commit",
            "-qm",
            "Synthetic documentation",
        ],
        cwd=source,
        check=True,
    )
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=source, text=True
    ).strip()


@pytest.fixture
def documentation(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    (source / "README.md").write_text("First description\n")
    base = freeze(source)
    (source / "README.md").write_text("Second description\n")
    plan = make_plan(source, base, freeze(source))
    # Test the runner with the actual test interpreter, regardless of local patch.
    plan["lanes"][0] = {
        **plan["lanes"][0],
        "python": f"{sys.version_info.major}.{sys.version_info.minor}",
    }
    return source, plan


@pytest.mark.skipif(sys.platform != "linux", reason="primary runner uses Linux")
def test_documentation_profile_runs_end_to_end(
    documentation: Any, tmp_path: Path
) -> None:
    source, plan = documentation
    output = tmp_path / "run"
    assert acceptance.run_lane(plan, source, output, "linux-3.11", "run:1") == 0
    receipt = json.loads((output / "receipt.json").read_text())
    assert [step["name"] for step in receipt["steps"]] == ["docs"]
    assert not admit(plan, [receipt], "run:1")
    assert not (output / "suite").exists()


@pytest.mark.skipif(sys.platform != "linux", reason="primary runner uses Linux")
def test_missing_link_fails_lane_and_admission(
    documentation: Any, tmp_path: Path
) -> None:
    source, plan = documentation
    (source / "README.md").write_text("[missing](missing.md)\n")
    plan["source_sha"] = freeze(source)
    output = tmp_path / "run"
    assert acceptance.run_lane(plan, source, output, "linux-3.11", "run:1") == 1
    receipt = json.loads((output / "receipt.json").read_text())
    assert receipt["steps"][0]["exit_code"] != 0
    assert admit(plan, [receipt], "run:1")


@pytest.mark.skipif(sys.platform != "linux", reason="primary runner uses Linux")
def test_incorrect_head_cannot_execute_or_qualify(
    documentation: Any, tmp_path: Path
) -> None:
    source, plan = documentation
    plan["source_sha"] = "0" * 40
    output = tmp_path / "run"
    assert acceptance.run_lane(plan, source, output, "linux-3.11", "run:1") == 1
    receipt = json.loads((output / "receipt.json").read_text())
    assert not receipt["source_verified"] and not receipt["steps"]


@pytest.mark.skipif(sys.platform != "linux", reason="primary runner uses Linux")
def test_stale_quality_reuse_is_rejected(documentation: Any, tmp_path: Path) -> None:
    source, plan = documentation
    output = tmp_path / "quality"
    assert (
        acceptance.run_lane(
            plan, source, output, "linux-3.11", "run:1", quality_only=True
        )
        == 0
    )
    prior = output / "quality.json"
    assert (
        acceptance.run_lane(
            plan,
            source,
            tmp_path / "complete",
            "linux-3.11",
            "run:2",
            quality_receipt=prior,
        )
        == 1
    )
    # A matching receipt is reused without duplicating the documentation check.
    assert (
        acceptance.run_lane(
            plan,
            source,
            tmp_path / "valid",
            "linux-3.11",
            "run:1",
            quality_receipt=prior,
        )
        == 0
    )


@pytest.mark.skipif(sys.platform != "linux", reason="primary runner uses Linux")
def test_quality_receipt_cannot_cross_execution_scopes(
    documentation: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    source, plan = documentation
    quality = tmp_path / "local-quality"
    assert (
        acceptance.run_lane(
            plan,
            source,
            quality,
            "linux-3.11",
            "run:1",
            quality_only=True,
            execution_scope="local",
        )
        == 0
    )
    output = tmp_path / "hosted"
    assert (
        acceptance.run_lane(
            plan,
            source,
            output,
            "linux-3.11",
            "run:1",
            quality_receipt=quality / "quality.json",
            execution_scope="hosted",
        )
        == 1
    )
    receipt = json.loads((output / "receipt.json").read_text())
    assert "Quality receipt" in receipt["error"]
    assert receipt["steps"] == []


def test_missing_required_r_executable_is_not_a_skip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    calls: list[list[str]] = []

    def missing(command: list[str], **kwargs: Any) -> Any:
        assert kwargs["env"]["JUDGMENTLLM_R_STRICT"] == "1"
        calls.append(command)
        if command[0] == "Rscript":
            raise FileNotFoundError("Synthetic missing required Rscript")
        return subprocess.CompletedProcess[str](command, 0)

    monkeypatch.setattr(acceptance.subprocess, "run", missing)
    with pytest.raises(FileNotFoundError, match="required Rscript"):
        acceptance.r_gate("exports", tmp_path, tmp_path / "out")
    assert len(calls) == 2


@pytest.mark.parametrize("packaging", [False, True])
def test_plan_exposes_conditional_package_preparation_to_workflow(
    documentation: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    packaging: bool,
) -> None:
    source, plan = documentation
    plan["packaging"] = packaging

    def planned(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return plan

    monkeypatch.setattr(acceptance, "make_plan", planned)
    destination = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(destination))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "acceptance.py",
            "plan",
            "--source",
            str(source),
            "--output",
            str(tmp_path / "plan.json"),
        ],
    )
    assert acceptance.main() == 0
    fields = dict(line.split("=", 1) for line in destination.read_text().splitlines())
    assert fields["packaging"] == str(packaging).lower()


def test_credentials_and_execution_overrides_do_not_reach_tests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GH_TOKEN",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "PYTEST_ADDOPTS",
        "COVERAGE_RCFILE",
    ):
        monkeypatch.setenv(name, "synthetic-never-forward")
    environment = acceptance.clean_environment()
    assert "synthetic-never-forward" not in environment.values()
    assert environment["UV_OFFLINE"] == "1"
    assert environment["JUDGMENTLLM_REQUIRE_NATIVE_SDK_PROBES"] == "1"
    assert environment["PATH"]
    assert os.environ["GH_TOKEN"] == "synthetic-never-forward"


def test_actual_r_gate_is_forbidden_in_github_actions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    with pytest.raises(ValueError, match="run locally"):
        acceptance.r_gate("exports", tmp_path, tmp_path / "output")
    assert not (tmp_path / "output").exists()


@pytest.mark.skipif(sys.platform != "linux", reason="primary runner uses Linux")
@pytest.mark.parametrize("fault", ["hosted-coverage", "deferred-local-r"])
def test_suite_cannot_cross_local_check_boundaries(
    documentation: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    source, plan = documentation
    scope = "hosted" if fault == "hosted-coverage" else "local"
    output = tmp_path / "run"
    original = subprocess.run

    def required(*args: Any, **kwargs: Any) -> set[str]:
        return {"tests"}

    def execute(command: list[str], **kwargs: Any) -> Any:
        if command[0] == "git":
            return cast(Any, original(command, **kwargs))
        suite = output / "suite"
        suite.mkdir()
        (suite / "summary.json").write_text(
            json.dumps(
                {
                    "status": "passed",
                    "selection_complete": True,
                    "source": {"commit": plan["source_sha"], "dirty": False},
                    "coverage_enabled": fault == "hosted-coverage",
                    "deferred_r_cases": ["synthetic R check"]
                    if scope == "local"
                    else [],
                }
            )
        )
        return subprocess.CompletedProcess[str](command, 0)

    monkeypatch.setattr(acceptance, "required_steps", required)
    monkeypatch.setattr(acceptance.subprocess, "run", execute)
    assert (
        acceptance.run_lane(
            plan, source, output, "linux-3.11", "run:1", execution_scope=scope
        )
        == 1
    )
    receipt = json.loads((output / "receipt.json").read_text())
    expected = (
        "must not measure coverage" if scope == "hosted" else "R cases were deferred"
    )
    assert expected in receipt["error"]


def test_pr_planning_does_not_write_bytecode_to_its_policy_copy(
    documentation: Any, tmp_path: Path
) -> None:
    source, plan = documentation
    policy = Path(acceptance.__file__).parent
    for name in ("check.py", "acceptance.py", "risk.py", "verification-map.json"):
        destination = source / "ci" / name
        destination.parent.mkdir(exist_ok=True)
        destination.write_bytes((policy / name).read_bytes())
    freeze(source)
    result = subprocess.run(
        [
            sys.executable,
            str(source / "ci/check.py"),
            "--profile",
            "pr",
            "--plan-only",
            "--changed-since",
            plan["source_sha"],
            "--output",
            str(tmp_path / "plan"),
        ],
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "0"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert not list((source / "ci").rglob("*.pyc"))
    assert not subprocess.check_output(
        ["git", "status", "--porcelain"], cwd=source, text=True
    ).strip()


@pytest.mark.skipif(sys.platform != "linux", reason="primary runner uses Linux")
@pytest.mark.parametrize("installer_exit", [0, 2])
def test_package_installation_is_offline_and_missing_cache_fails_closed(
    documentation: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    installer_exit: int,
) -> None:
    source, plan = documentation
    (source / ".gitignore").write_text("dist/\n")
    plan.update(source_sha=freeze(source), packaging=True)
    original = subprocess.run
    installers: list[list[str]] = []

    def execute(command: list[str], **kwargs: Any) -> Any:
        if command[0] == "git" or any(arg.endswith("docs_check.py") for arg in command):
            return cast(Any, original(command, **kwargs))
        assert kwargs["env"]["UV_OFFLINE"] == "1"
        if command[:2] == ["uv", "build"]:
            assert "--offline" in command
            (source / "dist").mkdir()
            for name in ("synthetic.whl", "synthetic.tar.gz"):
                (source / "dist" / name).write_bytes(b"synthetic package")
        elif "scripts/smoke_install.py" in command:
            assert "--offline" in command
            installers.append(command)
            return subprocess.CompletedProcess[str](command, installer_exit)
        else:
            assert command[1:5] == ["-m", "twine", "check", "--strict"]
        return subprocess.CompletedProcess[str](command, 0)

    monkeypatch.setattr(acceptance.subprocess, "run", execute)
    output = tmp_path / "packages"
    assert acceptance.run_lane(plan, source, output, "linux-3.11", "run:1") == bool(
        installer_exit
    )
    assert len(installers) == (1 if installer_exit else 2)
    receipt = json.loads((output / "receipt.json").read_text())
    assert receipt["status"] == ("failed" if installer_exit else "passed")
    assert receipt["steps"][-1]["name"] == ("wheel" if installer_exit else "sdist")


@pytest.mark.skipif(sys.platform != "linux", reason="primary runner uses Linux")
def test_packaging_failure_prevents_expensive_tests(
    documentation: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, plan = documentation
    plan.update(risk=3, packaging=True)
    quality = tmp_path / "quality.json"
    quality.write_text(
        json.dumps(
            {
                "source_sha": plan["source_sha"],
                "plan_sha256": plan["plan_sha256"],
                "lane": "linux-3.11",
                "run_id": "run:1",
                "phase": "quality",
                "execution_scope": "hosted",
                "status": "passed",
                "source_verified": True,
                "steps": [
                    {"name": name, "exit_code": 0}
                    for name in ("docs", "ruff", "format", "pyright", "infrastructure")
                ],
            }
        )
    )
    original = subprocess.run

    def fail_build(command: list[str], **kwargs: Any) -> Any:
        if command[0] == "git":
            return cast(Any, original(command, **kwargs))
        assert command[:2] == ["uv", "build"], "expensive tests ran before packaging"
        return subprocess.CompletedProcess[str](command, 1)

    monkeypatch.setattr(acceptance.subprocess, "run", fail_build)
    output = tmp_path / "run"
    assert (
        acceptance.run_lane(
            plan,
            source,
            output,
            "linux-3.11",
            "run:1",
            quality_receipt=quality,
        )
        == 1
    )
    receipt = json.loads((output / "receipt.json").read_text())
    assert receipt["steps"][-1]["name"] == "build"
    assert not (output / "suite").exists()


@pytest.mark.skipif(sys.platform != "linux", reason="primary runner uses Linux")
def test_local_receipt_cannot_establish_hosted_acceptance(
    documentation: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    source, plan = documentation
    output = tmp_path / "local"
    assert (
        acceptance.run_lane(
            plan, source, output, "linux-3.11", "run:1", execution_scope="local"
        )
        == 0
    )
    receipt = json.loads((output / "receipt.json").read_text())
    assert receipt["execution_scope"] == "local"
    assert admit(plan, [receipt], "run:1")


def test_github_cannot_request_local_measurements(
    documentation: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, plan = documentation
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    with pytest.raises(ValueError, match="cannot run in GitHub Actions"):
        acceptance.run_lane(
            plan,
            source,
            tmp_path / "forbidden",
            "linux-3.11",
            "run:1",
            execution_scope="local",
        )
    assert not (tmp_path / "forbidden").exists()


@pytest.mark.skipif(sys.platform != "linux", reason="primary runner uses Linux")
@pytest.mark.parametrize("scope", ["local", "hosted"])
def test_resource_measurement_selection_follows_execution_scope(
    documentation: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scope: str,
) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    source, plan = documentation
    plan.update(risk=1, resources=True)
    quality = tmp_path / "quality.json"
    quality.write_text(
        json.dumps(
            {
                "source_sha": plan["source_sha"],
                "plan_sha256": plan["plan_sha256"],
                "lane": "linux-3.11",
                "run_id": "run:1",
                "phase": "quality",
                "execution_scope": scope,
                "status": "passed",
                "source_verified": True,
                "steps": [
                    {"name": name, "exit_code": 0}
                    for name in ("docs", "ruff", "format", "pyright")
                ],
            }
        )
    )
    output = tmp_path / scope
    original = subprocess.run
    selections: list[list[str]] = []

    def execute(command: list[str], **kwargs: Any) -> Any:
        if command[0] == "git":
            return cast(Any, original(command, **kwargs))
        assert command[1].endswith("suite.py")
        selections.append(command)
        suite = output / "suite"
        suite.mkdir()
        (suite / "summary.json").write_text(
            json.dumps(
                {
                    "status": "passed",
                    "selection_complete": True,
                    "source": {"commit": plan["source_sha"], "dirty": False},
                    "deferred_resource_cases": ["synthetic resource workload"]
                    if scope == "hosted"
                    else [],
                    "coverage_enabled": False,
                    "deferred_r_cases": ["synthetic R case"]
                    if scope == "hosted"
                    else [],
                }
            )
        )
        return subprocess.CompletedProcess[str](command, 0)

    monkeypatch.setattr(acceptance.subprocess, "run", execute)
    assert (
        acceptance.run_lane(
            plan,
            source,
            output,
            "linux-3.11",
            "run:1",
            quality_receipt=quality,
            execution_scope=scope,
        )
        == 0
    )
    assert len(selections) == 1
    assert ("--skip-resources" in selections[0]) == (scope == "hosted")
    assert ("--skip-r" in selections[0]) == (scope == "hosted")
    receipt = json.loads((output / "receipt.json").read_text())
    assert ("resources" in {step["name"] for step in receipt["steps"]}) == (
        scope == "local"
    )
    assert receipt["deferred"]["resource_measurements"] == (scope == "hosted")
