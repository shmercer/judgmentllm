"""Real process death and credential-free installed-console Jev recovery."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from test_typesafe_decisions import CONTEXT_MESSAGE, jev_spec, native_spec

from judgmentllm.config import RunSpecV4
from judgmentllm.persistence.repository import Repository

HOOK = Path(__file__).parent / "fixtures/typesafe_decisions"


def cli(
    root: Path,
    *args: str,
    fault: str = "",
    response: str = "filtered",
) -> subprocess.CompletedProcess[str]:
    env = {
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
            "TMPDIR",
        )
        if key in os.environ
    }
    env.update(
        PYTHONPATH=str(HOOK),
        PYTHONDONTWRITEBYTECODE="1",
        JLLM_TYPESAFE_FIXTURE_ROOT=str(root),
        JLLM_TYPESAFE_FIXTURE_FAULT=fault,
        JLLM_TYPESAFE_FIXTURE_RESPONSE=response,
    )
    if args[0] == "run" or "--retry-task" in args:
        env["TYPESAFE_API_KEY"] = "synthetic-jev-key"
    console = Path(sys.executable).with_name(
        "judgmentllm.exe" if os.name == "nt" else "judgmentllm"
    )
    result = subprocess.run(
        [str(console), *args, "--store", str(root / "store"), "--json"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert "UNSAFE_JEV_CANARY" not in result.stdout + result.stderr
    assert "Error in sitecustomize" not in result.stderr
    return result


def control(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    assert result.returncode in (0, 3), (result.stdout, result.stderr)
    data = json.loads(result.stdout)
    assert data["schema_version"] == 1
    return data


def config_path(root: Path, spec: RunSpecV4) -> str:
    path = root / "run.json"
    path.write_text(spec.model_dump_json(), encoding="utf-8")
    return str(path)


@pytest.mark.parametrize(
    "fault,capture,statuses",
    [
        ("forward", "full", []),
        ("after_acknowledgment_receipt", "full", []),
        ("after_evidence_receipt", "full", ["complete", "invalid", "complete"]),
        ("during_result_transaction", "full", ["complete", "invalid", "complete"]),
        ("after_evidence_receipt", "minimal", []),
        ("after_failure_evidence_receipt", "full", ["errored"] * 3),
    ],
)
def test_fresh_process_recovery(
    tmp_path: Path,
    fault: str,
    capture: str,
    statuses: list[str],
) -> None:
    spec = jev_spec(native_spec(tmp_path / "input", capture=capture))
    crash = cli(
        tmp_path,
        "run",
        "--config",
        config_path(tmp_path, spec),
        fault=fault,
        response="rejection"
        if fault == "after_failure_evidence_receipt"
        else "filtered",
    )
    assert crash.returncode == 79, crash.stderr
    store = tmp_path / "store"
    with Repository(store) as repository:
        run_id = str(
            repository.connection.execute("SELECT run_id FROM runs").fetchone()[0]
        )
    first = control(cli(tmp_path, "resume", "--run", run_id))
    second = control(cli(tmp_path, "resume", "--run", run_id))
    detail = second["data"]["decisions"]
    assert detail == first["data"]["decisions"]
    assert second["data"]["recovery_required"] == (not statuses)
    if fault == "after_failure_evidence_receipt":
        assert detail["diagnostics"][0]["message"] == CONTEXT_MESSAGE
    with Repository(store) as repository:
        assert [row.status for row in repository.iter_results(run_id)] == statuses
        count = repository.connection.execute(
            "SELECT count(*) FROM usage_revisions"
        ).fetchone()[0]
        assert count == int(bool(statuses) or capture == "minimal")
        for path in repository.run_directory(run_id).rglob("*"):
            if path.is_file():
                assert b"UNSAFE_JEV_CANARY" not in path.read_bytes()
    assert (tmp_path / "forwards").read_text().splitlines() == ["systemone"]
