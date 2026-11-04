"""Installed console processes retain immutable outcomes and provider evidence."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts/cli_lifecycle_fixture.py"


def fixture(root: Path, phase: str = "create") -> dict[str, Any]:
    result = subprocess.run(
        [sys.executable, str(HELPER), "--phase", phase, "--output", str(root)],
        capture_output=True,
        text=True,
        timeout=35,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout) if result.stdout else {}


def cli(root: Path, *arguments: str, exit_code: int = 0) -> dict[str, Any]:
    installed = Path(sys.executable).with_name(
        "judgmentllm.exe" if os.name == "nt" else "judgmentllm"
    )
    executable = str(installed) if installed.is_file() else shutil.which("judgmentllm")
    assert executable is not None
    result = subprocess.run(
        [executable, *arguments, "--json"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == exit_code, result.stderr + result.stdout
    assert len(result.stdout.splitlines()) == 1
    value: dict[str, Any] = json.loads(result.stdout)
    assert set(value) == {
        "schema_version",
        "command",
        "status",
        "run_id",
        "run_status",
        "artifacts",
        "data",
        "error",
    }
    assert value["schema_version"] == 1
    for line in result.stderr.splitlines():
        diagnostic = json.loads(line)
        assert diagnostic["schema_version"] == 1
        assert diagnostic["event"] in {"progress", "diagnostic"}
    assert "SOURCE_ONLY_SENTINEL" not in result.stdout + result.stderr
    assert "RAW_ONLY_SENTINEL" not in result.stdout + result.stderr
    assert "Authorized normalized explanation." not in result.stdout + result.stderr
    for path in value["artifacts"].values():
        if path is not None:
            assert Path(path).is_absolute() and Path(path).exists()
    return value


def mock(directory: Path, name: str = "fixture.json") -> list[str]:
    return [
        "--mock-ledger",
        str(directory / "ledger.sqlite3"),
        "--mock-fixture",
        str(directory / name),
    ]


@pytest.mark.parametrize("capture", ["full", "minimal"])
@pytest.mark.parametrize("outcome", ["invalid_category", "missing", "duplicate"])
def test_installed_subset_retry_preserves_prior_observations(
    tmp_path: Path, capture: str, outcome: str
) -> None:
    fixture(tmp_path)
    directory = tmp_path / f"grouped-{capture}"
    scenario_path = directory / "fixture.json"
    scenario = json.loads(scenario_path.read_text(encoding="utf-8"))
    scenario["scenario"]["members"]["003"]["outcome"] = outcome
    scenario_path.write_text(json.dumps(scenario), encoding="utf-8")
    store = str(directory / "store")
    started = cli(
        tmp_path,
        "run",
        "--config",
        str(directory / "config.json"),
        "--store",
        store,
        *mock(directory),
        exit_code=3,
    )
    run_id = started["run_id"]
    identity = ["--run", run_id, "--store", store]
    exported = cli(tmp_path, "results", "export", *identity)
    original_path = Path(exported["artifacts"]["export"])
    original_bytes = original_path.read_bytes()
    records = [json.loads(line) for line in original_bytes.splitlines()]
    failed = [row for row in records if row["status"] != "complete"]
    assert len(records) == 6 and len(failed) == 2
    retry: list[str] = []
    retried: dict[str, Any] = {}
    for index, row in enumerate(failed):
        retry = [
            "resume",
            *identity,
            *mock(directory, "valid.json"),
            "--retry-id",
            f"retry-00{index + 1}",
            "--retry-task",
            row["task_id"],
        ]
        retried = cli(tmp_path, *retry, exit_code=0 if index == len(failed) - 1 else 3)
    assert retried["run_status"] == "completed" and retried["data"]["results"] == 8
    assert cli(tmp_path, *retry)["data"]["results"] == 8
    assert (
        cli(tmp_path, "resume", *identity, *mock(directory, "valid.json"))["data"][
            "results"
        ]
        == 8
    )
    current = cli(tmp_path, "results", "export", *identity)
    current_records = [
        json.loads(line)
        for line in Path(current["artifacts"]["export"])
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert current_records[: len(records)] == records
    conflicting = cli(
        tmp_path,
        "resume",
        *identity,
        *mock(directory, "valid.json"),
        "--retry-id",
        "retry-001",
        "--retry-task",
        records[0]["task_id"],
        "--allow-completed",
        exit_code=2,
    )
    assert conflicting["status"] == "error"
    evidence = fixture(directory, "inspect")
    assert len(evidence["acceptances"]) == 4
    assert all(row["acceptance_count"] == 1 for row in evidence["acceptances"])
    assert len(evidence["retry_operations"]) == 2
    assert original_path.read_bytes() == original_bytes


def test_installed_recovery_requires_authoritative_observation(tmp_path: Path) -> None:
    fixture(tmp_path)
    directory = tmp_path / "recovery-minimal"
    store = str(directory / "store")
    result = cli(
        tmp_path,
        "run",
        "--config",
        str(directory / "config.json"),
        "--store",
        store,
        *mock(directory),
        "--mock-no-retrieval",
        exit_code=3,
    )
    identity = ["--run", result["run_id"], "--store", store]
    unavailable = cli(
        tmp_path,
        "resume",
        *identity,
        *mock(directory),
        "--mock-no-retrieval",
        exit_code=3,
    )
    assert unavailable["data"]["results"] == 0
    assert unavailable["data"]["unresolved_submissions"] == 2
    recovered = cli(tmp_path, "resume", *identity, *mock(directory))
    assert recovered["data"]["results"] == 2
    evidence = fixture(directory, "inspect")
    assert len(evidence["acceptances"]) == 2
    assert all(row["acceptance_count"] == 1 for row in evidence["acceptances"])
    # The complete R lifecycle is a separate mandatory gate, not repeated here.


def test_installed_process_death_does_not_authorize_another_submission(
    tmp_path: Path,
) -> None:
    fixture(tmp_path)
    directory = tmp_path / "interrupted-minimal"
    installed = Path(sys.executable).with_name(
        "judgmentllm.exe" if os.name == "nt" else "judgmentllm"
    )
    executable = str(installed) if installed.is_file() else shutil.which("judgmentllm")
    assert executable is not None
    store = str(directory / "store")
    interrupted = subprocess.run(
        [
            sys.executable,
            str(HELPER),
            "--phase",
            "interrupt",
            "--output",
            str(directory),
            "--executable",
            executable,
        ],
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert interrupted.returncode == 0, interrupted.stderr
    killed = json.loads(interrupted.stdout)
    assert killed["exit_confirmed"] and killed["accepted"] == 1
    identity = ["--run", killed["run_id"], "--store", store]
    status = cli(tmp_path, "runs", "status", *identity)
    assert status["status"] == "ok" and not status["data"]["executor_active"]
    recovered = cli(
        tmp_path, "resume", *identity, *mock(directory, "valid.json"), exit_code=3
    )
    assert recovered["data"]["results"] == 1
    evidence = fixture(directory, "inspect")
    assert sum(row["acceptance_count"] for row in evidence["acceptances"]) == 1


def test_required_r_gate_reports_missing_runtime(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/cli_lifecycle_r_gate.py"),
            "--output",
            str(tmp_path / "r"),
        ],
        env={**os.environ, "PATH": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 2 and "Required Rscript is unavailable" in result.stderr


@pytest.mark.parametrize(
    "outcome,status",
    [
        ("refusal", "refused"),
        ("provider_error", "errored"),
        ("confirmed_cancellation", "cancelled"),
    ],
)
def test_installed_unsuccessful_observations_remain_exportable(
    tmp_path: Path, outcome: str, status: str
) -> None:
    fixture(tmp_path)
    directory = tmp_path / "independent-minimal"
    path = directory / "fixture.json"
    scenario = json.loads(path.read_text(encoding="utf-8"))
    scenario["scenario"]["outcome"] = outcome
    path.write_text(json.dumps(scenario), encoding="utf-8")
    store = str(directory / "store")
    started = cli(
        tmp_path,
        "run",
        "--config",
        str(directory / "config.json"),
        "--store",
        store,
        *mock(directory),
        exit_code=3,
    )
    identity = ["--run", started["run_id"], "--store", store]
    assert cli(tmp_path, "runs", "status", *identity)["status"] == "ok"
    exported = cli(tmp_path, "results", "export", *identity)
    records = [
        json.loads(line)
        for line in Path(exported["artifacts"]["export"])
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert len(records) == 2 and all(row["status"] == status for row in records)
    assert all(
        row["primary_score"] is None and row["category_label"] is None
        for row in records
    )


def test_installed_completed_retry_requires_explicit_permission(tmp_path: Path) -> None:
    fixture(tmp_path)
    directory = tmp_path / "independent-minimal"
    store = str(directory / "store")
    started = cli(
        tmp_path,
        "run",
        "--config",
        str(directory / "config.json"),
        "--store",
        store,
        *mock(directory),
    )
    identity = ["--run", started["run_id"], "--store", store]
    exported = cli(tmp_path, "results", "export", *identity)
    record = json.loads(
        Path(exported["artifacts"]["export"])
        .read_text(encoding="utf-8")
        .splitlines()[0]
    )
    retry = [
        "resume",
        *identity,
        *mock(directory),
        "--retry-task",
        record["task_id"],
        "--retry-id",
        "completed-001",
    ]
    assert cli(tmp_path, *retry, exit_code=2)["status"] == "error"
    assert cli(tmp_path, *retry, "--allow-completed")["data"]["results"] == 3
    assert cli(tmp_path, *retry, "--allow-completed")["data"]["results"] == 3
    evidence = fixture(directory, "inspect")
    assert len(evidence["acceptances"]) == 3
    assert all(row["acceptance_count"] == 1 for row in evidence["acceptances"])
