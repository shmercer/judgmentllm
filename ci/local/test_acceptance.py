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


def test_missing_required_r_executable_is_not_a_skip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
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
