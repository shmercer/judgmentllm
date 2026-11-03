"""Real process death and independent reopening of the mock execution journal."""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Literal

import pytest
from test_preparation_rendering import specification

from judgmentllm.config import RunSpec
from judgmentllm.persistence.artifacts import ArtifactStore
from judgmentllm.persistence.hashing import bytes_digest
from judgmentllm.persistence.receipts import read_receipt
from judgmentllm.persistence.repository import Repository
from judgmentllm.preparation import prepare_run
from judgmentllm.providers.mock import MockAdapter

CHILD = """
import asyncio
import os
import sys
import time
from pathlib import Path
from judgmentllm.config import load_run_spec
from judgmentllm.execution import execute_run, recover_run
from judgmentllm.providers.mock import MockAdapter

store, ledger, configuration, stage, mode, marker = sys.argv[1:]
adapter = MockAdapter(ledger, retrieval_available=mode != 'unavailable')
def fault(current, request_uid):
    if current == stage:
        if mode == 'hold':
            Path(marker).write_text(str(os.getpid()), encoding='ascii')
            time.sleep(60)
        else:
            os._exit(73)
try:
    operation = execute_run if stage else recover_run
    result = asyncio.run(operation('001', store=store, adapter=adapter,
        spec=load_run_spec(configuration), fault=fault if stage else None))
    print(result.model_dump_json())
except BaseException as error:
    print(type(error).__name__, file=sys.stderr)
    sys.exit(1)
"""


def _fixture(
    directory: Path, capture: Literal["full", "minimal"]
) -> tuple[Path, Path, Path]:
    spec = specification(directory / "inputs", responses=1, traits=1)
    spec = RunSpec.model_validate(
        {
            **spec.model_dump(),
            "provider": "mock",
            "endpoint": "synthetic",
            "capture": capture,
        }
    )
    spec.execution.concurrency = 1
    spec.output_path = (directory / "output").resolve()
    configuration = directory / "configuration.json"
    configuration.write_text(spec.model_dump_json(), encoding="utf-8")
    store, ledger = directory / "runtime", directory / "service.sqlite3"
    adapter = MockAdapter(ledger)
    with prepare_run(
        spec, store=store, run_id="001", capabilities=adapter.capabilities(spec)
    ):
        pass
    return store, ledger, configuration


def _command(
    fixture: tuple[Path, Path, Path],
    stage: str = "",
    mode: str = "normal",
    marker: Path | None = None,
) -> list[str]:
    return [
        sys.executable,
        "-c",
        CHILD,
        *(str(path) for path in fixture),
        stage,
        mode,
        str(marker or "unused"),
    ]


def _run(
    fixture: tuple[Path, Path, Path], stage: str = "", mode: str = "normal"
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        _command(fixture, stage, mode), capture_output=True, text=True, timeout=30
    )


def _accepted(ledger: Path) -> int:
    # Read the independent service database directly, without the adapter or
    # executor repository asserting what it thinks was accepted.
    with sqlite3.connect(ledger) as connection:
        return int(
            connection.execute(
                "SELECT COALESCE(SUM(acceptance_count),0) FROM acceptances"
            ).fetchone()[0]
        )


@pytest.mark.parametrize("capture", ["full", "minimal"])
@pytest.mark.parametrize(
    "stage",
    [
        "before_request_creation",
        "after_preparation_artifacts",
        "after_preparation",
        "after_claim",
        "after_submitting",
        "after_submit",
        "after_acknowledgment",
        "after_response",
        "after_receipt",
        "after_observation",
        "after_normalization",
        "during_result_transaction",
        "after_result",
    ],
)
def test_process_death_recovers_evidence_without_another_acceptance(
    tmp_path: Path, capture: Literal["full", "minimal"], stage: str
) -> None:
    fixture = _fixture(tmp_path, capture)
    crashed = _run(fixture, stage)
    assert crashed.returncode == 73, crashed.stderr
    assert not crashed.stdout and not crashed.stderr
    before_acceptance = stage in {
        "before_request_creation",
        "after_preparation_artifacts",
        "after_preparation",
        "after_claim",
        "after_submitting",
    }
    assert _accepted(fixture[1]) == (0 if before_acceptance else 1)
    with Repository(fixture[0]) as repository:
        before = list(repository.iter_results("001"))
        assert len(before) == (1 if stage == "after_result" else 0)
        prepared = list(repository.iter_submissions("001"))
        commitments = [
            (s.submission_id, s.correlation_id, s.payload_sha256) for s in prepared
        ]
    recovered = _run(fixture)
    assert recovered.returncode == 0, recovered.stderr
    report: dict[str, Any] = json.loads(recovered.stdout)
    if stage == "after_submitting":
        # Durable submitting means the call may have occurred. Authoritative
        # absence closes this attempt; Subphase 3 does not orchestrate a retry.
        assert _accepted(fixture[1]) == 0
        assert report["summary"]["results"] == 0
        with Repository(fixture[0]) as repository:
            assert next(repository.iter_submissions("001")).certainty == "not_submitted"
            assert next(repository.iter_attempts("001")).state == "not_submitted"
    else:
        assert _accepted(fixture[1]) == 1
        assert report["summary"]["results"] == 1
    with Repository(fixture[0]) as repository:
        if commitments:
            assert [
                (s.submission_id, s.correlation_id, s.payload_sha256)
                for s in repository.iter_submissions("001")
            ] == commitments
        after = list(repository.iter_results("001"))
        if before:
            assert before == after
        assert any(e.kind == "owner_recovered" for e in repository.events("001"))
    # A second independent recovery retains the same records and acceptance.
    again = _run(fixture, mode="unavailable")
    assert again.returncode == 0, again.stderr
    assert _accepted(fixture[1]) == (0 if stage == "after_submitting" else 1)


def test_lost_minimal_response_requires_original_authoritative_retrieval(
    tmp_path: Path,
) -> None:
    fixture = _fixture(tmp_path, "minimal")
    assert _run(fixture, "after_submit").returncode == 73
    unavailable = _run(fixture, mode="unavailable")
    assert unavailable.returncode == 0, unavailable.stderr
    report: dict[str, Any] = json.loads(unavailable.stdout)
    assert report["recovery_required"]
    assert report["summary"]["results"] == 0
    assert report["summary"]["unresolved_submissions"] == 1
    assert _accepted(fixture[1]) == 1
    with Repository(fixture[0]) as repository:
        assert not list(repository.iter_results("001"))
        assert next(repository.iter_submissions("001")).certainty == "accepted"
    recovered = _run(fixture)
    assert recovered.returncode == 0, recovered.stderr
    assert json.loads(recovered.stdout)["summary"]["results"] == 1
    assert _accepted(fixture[1]) == 1


def test_competing_process_cannot_advance_owned_execution(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path, "minimal")
    marker = tmp_path / "holding.pid"
    process = subprocess.Popen(
        _command(fixture, "after_claim", "hold", marker),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 15
        while (
            not marker.exists()
            and process.poll() is None
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        assert marker.exists(), "executor did not reach the holding boundary"
        assert int(marker.read_text(encoding="ascii")) > 0
        competing = _run(fixture)
        assert competing.returncode == 1
        assert competing.stderr.strip() == "OwnershipError"
        assert _accepted(fixture[1]) == 0
    finally:
        if marker.exists():
            # Virtualenv launchers on Windows can differ from the worker PID.
            try:
                os.kill(int(marker.read_text(encoding="ascii")), signal.SIGTERM)
            except ProcessLookupError:
                pass
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)
    recovered = _run(fixture)
    assert recovered.returncode == 0, recovered.stderr
    assert _accepted(fixture[1]) == 1


@pytest.mark.parametrize("capture", ["full", "minimal"])
def test_changed_sources_block_reopening_before_acceptance(
    tmp_path: Path, capture: Literal["full", "minimal"]
) -> None:
    fixture = _fixture(tmp_path, capture)
    assert _run(fixture, "after_preparation").returncode == 73
    source = tmp_path / "inputs" / "responses.jsonl"
    original = source.read_text(encoding="utf-8")
    source.write_text(
        original.replace("Synthetic response.", "Different response."), encoding="utf-8"
    )
    assert _run(fixture).returncode == 1
    assert _accepted(fixture[1]) == 0
    with Repository(fixture[0]) as repository:
        assert not list(repository.iter_results("001"))


@pytest.mark.parametrize("corrupt_raw", [False, True])
def test_corrupt_published_full_receipt_blocks_recovery(
    tmp_path: Path, corrupt_raw: bool
) -> None:
    fixture = _fixture(tmp_path, "full")
    assert _run(fixture, "after_receipt").returncode == 73
    with Repository(fixture[0]) as repository:
        submission = next(repository.iter_submissions("001"))
        artifacts = ArtifactStore(repository.run_directory("001"))
        receipt = read_receipt(artifacts, submission.submission_id)
        assert receipt is not None
        target = (
            artifacts.verify(receipt.raw)
            if corrupt_raw
            else artifacts.reference_path(
                f"receipts/{bytes_digest(submission.submission_id.encode())}.json"
            )
        )
        assert target is not None
        target.write_bytes(b"corrupt")
    recovered = _run(fixture)
    assert recovered.returncode == 1
    assert _accepted(fixture[1]) == 1
    with Repository(fixture[0]) as repository:
        assert not list(repository.iter_results("001"))
