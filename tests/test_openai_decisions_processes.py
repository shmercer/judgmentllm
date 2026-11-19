"""Real installed-console death and fresh-process recovery of filtered wire output."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from test_openai_decisions_sdk import spec_for

import judgmentllm
from judgmentllm.persistence.repository import Repository

HOOK = Path(__file__).parent / "fixtures/openai_decisions"


def cli(
    root: Path, *args: str, crash: str = "", filtered: bool = True
) -> subprocess.CompletedProcess[str]:
    root.mkdir(parents=True, exist_ok=True)
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
        )
        if key in os.environ
    }
    env.update(
        PYTHONPATH=str(HOOK),
        PYTHONDONTWRITEBYTECODE="1",
        OPENAI_API_KEY="synthetic-decisions-key",
        DECISIONS_FIXTURE_ROOT=str(root),
        DECISIONS_FIXTURE_FILTER="1" if filtered else "0",
        DECISIONS_FIXTURE_CRASH=crash,
    )
    console = Path(sys.executable).with_name(
        "judgmentllm.exe" if os.name == "nt" else "judgmentllm"
    )
    result = subprocess.run(
        [str(console), *args, "--json"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert "UNSAFE_WIRE_CANARY" not in result.stdout + result.stderr
    return result


def control(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    assert result.returncode in (0, 3), (result.stdout, result.stderr)
    data = json.loads(result.stdout)
    assert data["schema_version"] == 1
    return data


@pytest.mark.parametrize(
    "stage",
    [
        "manifest_published",
        "after_evidence_receipt",
        "after_receipt",
        "during_evidence_transaction",
        "during_member_validation",
        "after_decisions_outcome_artifact",
        "during_result_transaction",
    ],
)
def test_filtered_death_replays_same_siblings_without_forwarding(
    tmp_path: Path, stage: str
) -> None:
    spec = spec_for(tmp_path / "inputs").model_copy(
        update={"account_scope": "fixture-scope"}
    )
    config = tmp_path / "run.json"
    config.write_text(spec.model_dump_json())
    store = tmp_path / "store"
    crash = cli(
        tmp_path, "run", "--config", str(config), "--store", str(store), crash=stage
    )
    assert crash.returncode == 79, crash.stderr
    with Repository(store) as repo:
        run_id = str(repo.connection.execute("SELECT run_id FROM runs").fetchone()[0])
    recovered = control(cli(tmp_path, "resume", "--run", run_id, "--store", str(store)))
    assert recovered["data"]["decisions"]["known_invocations"] == 1
    assert recovered["data"]["decisions"]["input_tokens"] == 120
    with Repository(store) as repo:
        rows = list(repo.iter_results(run_id))
        assert [r.status for r in rows] == ["complete", "invalid", "complete"]
        assert rows[1].error_kind == "invalid_output"
        assert all(r.raw_response_path is None for r in rows)
        assert (
            repo.connection.execute("SELECT count(*) FROM usage_revisions").fetchone()[
                0
            ]
            == 1
        )
    exported = control(
        cli(tmp_path, "results", "provenance", "--run", run_id, "--store", str(store))
    )
    manifest = json.loads(Path(exported["data"]["manifest_path"]).read_text())
    physical_file = next(f for f in manifest["files"] if f["name"] == "physical.jsonl")
    run_root = next((store / "runs").iterdir())
    physical = json.loads(
        (run_root / physical_file["artifact"]["relative_path"]).read_text()
    )
    assert (
        physical["original_response"]["sha256"]
        != physical["retained_response"]["sha256"]
    )
    assert physical["retained_kind"] == "filtered_replay"
    validation = physical["validation"]
    assert validation["unexpected_members"] == 1
    assert [m["answer_ordinals"] for m in validation["members"]] == [[0], [2], [3]]
    questions_file = next(
        f for f in manifest["files"] if f["name"] == "questions.jsonl"
    )
    questions = [
        json.loads(line)
        for line in (run_root / questions_file["artifact"]["relative_path"])
        .read_text()
        .splitlines()
    ]
    assert all(q["whole_response_valid"] is False for q in questions)
    control(cli(tmp_path, "resume", "--run", run_id, "--store", str(store)))
    assert len((tmp_path / "forwarding.jsonl").read_text().splitlines()) == 1
    for path in run_root.rglob("*"):
        if path.is_file():
            assert b"UNSAFE_WIRE_CANARY" not in path.read_bytes()
    assert (
        Path((tmp_path / "import-origin.txt").read_text()).resolve()
        == Path(judgmentllm.__file__).resolve()
    )


def test_minimal_uncommitted_response_stays_held(tmp_path: Path) -> None:
    spec = spec_for(tmp_path / "inputs", capture="minimal")
    config = tmp_path / "run.json"
    config.write_text(spec.model_dump_json())
    store = tmp_path / "store"
    assert (
        cli(
            tmp_path,
            "run",
            "--config",
            str(config),
            "--store",
            str(store),
            crash="after_receipt",
        ).returncode
        == 79
    )
    with Repository(store) as repo:
        run_id = str(repo.connection.execute("SELECT run_id FROM runs").fetchone()[0])
    report = control(cli(tmp_path, "resume", "--run", run_id, "--store", str(store)))
    assert report["data"]["recovery_required"] is True
    with Repository(store) as repo:
        assert len(list(repo.iter_results(run_id))) == 0
    assert len((tmp_path / "forwarding.jsonl").read_text().splitlines()) == 1


@pytest.mark.parametrize("target", ["derivative", "evidence", "acknowledgment"])
def test_corrupt_filtered_receipts_never_forward_or_fabricate_results(
    tmp_path: Path, target: str
) -> None:
    spec = spec_for(tmp_path / "inputs")
    config = tmp_path / "run.json"
    config.write_text(spec.model_dump_json())
    store = tmp_path / "store"
    assert (
        cli(
            tmp_path,
            "run",
            "--config",
            str(config),
            "--store",
            str(store),
            crash="after_receipt",
        ).returncode
        == 79
    )
    with Repository(store) as repo:
        run_id = str(repo.connection.execute("SELECT run_id FROM runs").fetchone()[0])
        run_root = repo.run_directory(run_id)
    if target == "derivative":
        evidence = json.loads(next((run_root / "evidence").glob("*.json")).read_text())
        path = run_root / evidence["retained"]["relative_path"]
    else:
        directory = "evidence" if target == "evidence" else "acknowledgments"
        path = next((run_root / directory).glob("*.json"))
    path.write_bytes(path.read_bytes() + b" ")
    result = cli(tmp_path, "resume", "--run", run_id, "--store", str(store))
    assert result.returncode not in (0, 3)
    with Repository(store) as repo:
        assert list(repo.iter_results(run_id)) == []
    assert len((tmp_path / "forwarding.jsonl").read_text().splitlines()) == 1
