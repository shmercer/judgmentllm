"""Fresh installed-console processes exercise durable Gemini Decisions evidence."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from test_decisions_lifecycle import native_spec
from test_gemini_decisions import gemini_spec
from test_typesafe_decisions_processes import control

from judgmentllm.config import RunSpecV4
from judgmentllm.persistence.evidence import DecisionsEvidenceReceiptV5
from judgmentllm.persistence.repository import Repository

HOOK = Path(__file__).parent / "fixtures/gemini_decisions"


def config_path(root: Path, spec: RunSpecV4) -> str:
    path = root / "run.json"
    path.write_text(spec.model_dump_json(exclude_unset=True), encoding="utf-8")
    return str(path)


def cli(
    root: Path,
    *args: str,
    scenario: str = "clean",
    fault: str = "",
    json_output: bool = True,
    console: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {
        name: os.environ[name]
        for name in (
            "PATH",
            "HOME",
            "LANG",
            "LC_ALL",
            "SystemRoot",
            "WINDIR",
            "TEMP",
            "TMP",
            "TMPDIR",
        )
        if name in os.environ
    }
    env.update(
        PYTHONPATH=str(HOOK),
        PYTHONDONTWRITEBYTECODE="1",
        JLLM_GEMINI_DECISIONS_ROOT=str(root),
        JLLM_GEMINI_DECISIONS_SCENARIO=scenario,
        JLLM_GEMINI_DECISIONS_FAULT=fault,
        GOOGLE_GENAI_HTTP_CLIENT="httpx",
        GOOGLE_GENAI_API_VERSION="v1",
    )
    diagnostics = args[0] in {"auth", "providers"}
    if diagnostics or args[0] == "run" or "--retry-task" in args:
        env["GOOGLE_API_KEY"] = "SYNTHETIC_CLI_GEMINI_KEY"
    if console is None:
        console = Path(sys.executable).with_name(
            "judgmentllm.exe" if os.name == "nt" else "judgmentllm"
        )
    result = subprocess.run(
        [
            str(console),
            *args,
            *(["--store", str(root / "store")] if not diagnostics else []),
            *(["--json"] if json_output else []),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert "GEMINI_UNSAFE_CANARY" not in result.stdout + result.stderr
    assert "SYNTHETIC_CLI_GEMINI_KEY" not in result.stdout + result.stderr
    assert "Error in sitecustomize" not in result.stderr
    return result


def test_actual_sdk_diagnostics_through_cli(tmp_path: Path) -> None:
    for command in (("auth", "verify"), ("providers", "models")):
        result = control(
            cli(tmp_path, *command, "--provider", "gemini", scenario="diagnostics")
        )
        assert result["status"] == "ok" and result["data"]["provider"] == "gemini"
    assert (tmp_path / "forwards").read_text().splitlines() == [
        "models-1",
        "models-1000",
    ]


@pytest.mark.parametrize(
    "scenario,code,statuses",
    [
        ("context", "max_tokens_exceeded", ["errored"] * 3),
        ("unknown", "request_rejected", ["errored"] * 3),
        ("truncated", None, []),
    ],
)
def test_context_and_validation_first_forward_and_reopen(
    tmp_path: Path, scenario: str, code: str | None, statuses: list[str]
) -> None:
    spec = gemini_spec(native_spec(tmp_path / "input"))
    first = control(
        cli(tmp_path, "run", "--config", config_path(tmp_path, spec), scenario=scenario)
    )
    run_id = first["run_id"]
    diagnostics: list[dict[str, Any]] | None = first["data"]["decisions"].get(
        "diagnostics"
    )
    if code is None:
        assert diagnostics is None
    else:
        assert diagnostics is not None and len(diagnostics) == 1
        (diagnostic,) = diagnostics
        assert diagnostic["code"] == code and diagnostic["rejected_attempts"] == 1
        assert (
            "fewer grouped questions or smaller shared input" in diagnostic["message"]
        )
        assert ("If context size was the cause" in diagnostic["message"]) == (
            scenario == "unknown"
        )
    for command in (("runs", "status"), ("resume",)):
        reopened = control(cli(tmp_path, *command, "--run", run_id))
        assert reopened["data"]["decisions"].get("diagnostics") == diagnostics
    if scenario == "context":
        human = cli(tmp_path, "resume", "--run", run_id, json_output=False)
        assert (
            "Start a new run with fewer grouped questions or smaller shared input."
            in human.stdout
        )
    assert (tmp_path / "forwards").read_text().splitlines() == ["decisions"]
    with Repository(tmp_path / "store") as repository:
        assert [row.status for row in repository.iter_results(run_id)] == statuses
        (submission,) = repository.iter_submissions(run_id)
        if code is None:
            assert submission.state == "uncertain"
            assert repository.usage_high_water_mark(run_id) == 0
        else:
            assert submission.state == "confirmed_not_submitted"
            (receipt,) = repository.iter_evidence(run_id)
            assert isinstance(receipt, DecisionsEvidenceReceiptV5)
            assert receipt.rejection is not None and receipt.rejection.reason == code
            assert repository.usage_high_water_mark(run_id) == 1
        directory = repository.run_directory(run_id)
        for path in directory.rglob("*"):
            if path.is_file():
                assert b"GEMINI_UNSAFE_CANARY" not in path.read_bytes()


@pytest.mark.parametrize(
    "scenario,fault,statuses,usage",
    [
        ("clean", "forward", [], 0),
        ("filtered", "after_evidence_receipt", ["complete", "invalid", "complete"], 1),
        (
            "filtered",
            "during_result_transaction",
            ["complete", "invalid", "complete"],
            1,
        ),
        ("200-refusal", "after_evidence_receipt", ["refused"] * 3, 1),
        ("400-refusal", "after_failure_evidence_receipt", ["errored"] * 3, 1),
    ],
)
def test_refusal_and_grouped_results_recover_in_fresh_process(
    tmp_path: Path, scenario: str, fault: str, statuses: list[str], usage: int
) -> None:
    spec = gemini_spec(native_spec(tmp_path / "input"))
    result = cli(
        tmp_path,
        "run",
        "--config",
        config_path(tmp_path, spec),
        scenario=scenario,
        fault=fault,
    )
    assert result.returncode == 79, (result.stdout, result.stderr)
    with Repository(tmp_path / "store") as repo:
        (row,) = repo.connection.execute("SELECT run_id FROM runs").fetchall()
        run_id = str(row[0])
    for _ in range(2):
        reopened = control(cli(tmp_path, "resume", "--run", run_id))
        assert reopened["run_id"] == run_id
    assert (tmp_path / "forwards").read_text().splitlines() == ["decisions"]
    with Repository(tmp_path / "store") as repo:
        rows = list(repo.iter_results(run_id))
        assert [row.status for row in rows] == statuses
        assert repo.usage_high_water_mark(run_id) == usage
        if scenario == "filtered":
            assert rows[0].task_kind == "rubric"
            assert rows[0].provider_expected_index == pytest.approx(1.550001)
