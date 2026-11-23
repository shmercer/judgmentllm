"""Conflicts preserve proven exports; grouped results and child retries are atomic."""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from test_batch_exports_retry import file_path
from test_batch_recovery import child
from test_native_batch import batch_spec, mock_fixture, service_counts

from judgmentllm import (
    BatchMockOptions,
    RetrySelection,
    collect_batch,
    export_batch_provenance,
    get_batch_status,
    poll_batch,
    prepare_batch,
    resume_batch,
    submit_batch,
)
from judgmentllm.batch_models import BatchStatusEvidenceV1
from judgmentllm.persistence.repository import ReadOnlyRepository
from judgmentllm.providers.batch_mock import MockBatchAdapter
from judgmentllm.providers.protocol import PreparedRequestV2


def test_conflicting_terminal_status_preserves_proven_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = batch_spec(tmp_path)
    root = tmp_path / "store"
    prepare_batch(spec, store=root, run_id="001")
    resume_batch("001", store=root, spec=spec)
    old = export_batch_provenance("001", store=root)
    prior = file_path(old, "results.jsonl").read_bytes()

    async def conflict(
        self: MockBatchAdapter, submission_id: str, remote_id: str
    ) -> BatchStatusEvidenceV1:
        return BatchStatusEvidenceV1(
            submission_id=submission_id, remote_id=remote_id, state="failed"
        )

    monkeypatch.setattr(MockBatchAdapter, "status", conflict)
    result = poll_batch("001", store=root)
    assert result.outcome == "partial" and result.batch.conflicts == 1
    assert get_batch_status("001", store=root).batch.recovery_required
    reopened = export_batch_provenance(
        "001", store=root, bundle_id=old.manifest.bundle_id
    )
    assert file_path(reopened, "results.jsonl").read_bytes() == prior
    assert service_counts(root)["forwards"] == 2


def test_conflicting_duplicate_retains_original_result_and_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = batch_spec(tmp_path, responses=1)
    mock = mock_fixture(
        tmp_path, duplicate_results=True, input_tokens=7, output_tokens=3
    )
    root = tmp_path / "store"
    prepare_batch(spec, store=root, run_id="001", mock=mock)
    submit_batch("001", store=root, spec=spec, mock=mock)
    original = MockBatchAdapter.read_row
    reads = 0

    async def conflicting(
        self: MockBatchAdapter,
        prepared: PreparedRequestV2,
        position: int,
        key: str,
        source: str,
    ) -> bytes | None:
        nonlocal reads
        raw = await original(self, prepared, position, key, source)
        if raw is None:
            return None
        reads += 1
        if reads == 2:
            data = json.loads(raw)
            data["usage"]["input_tokens"] = 99
            return json.dumps(data).encode() + b"\n"
        return raw

    monkeypatch.setattr(MockBatchAdapter, "read_row", conflicting)
    result = collect_batch("001", store=root, spec=spec, mock=mock)
    assert result.outcome == "partial" and result.batch.conflicts == 1
    with ReadOnlyRepository(root) as repository:
        assert repository.summary("001").results == 1
        receipt = json.loads(
            repository.connection.execute(
                "SELECT record_json FROM batch_member_receipts"
            ).fetchone()[0]
        )
        assert (
            receipt["usage"]["input_tokens"],
            receipt["usage"]["output_tokens"],
        ) == (7, 3)


def test_grouped_result_transaction_dies_without_partial_vector(tmp_path: Path) -> None:
    spec = batch_spec(tmp_path, traits=2, grouped=True)
    config = tmp_path / "run.json"
    config.write_text(spec.model_dump_json())
    mock = mock_fixture(tmp_path)
    root = tmp_path / "store"
    prepare_batch(config, store=root, run_id="001", mock=mock)
    assert child(tmp_path, "resume", "during_batch_results").returncode == 73
    with ReadOnlyRepository(root) as repository:
        assert repository.summary("001").results == 0
    result = child(tmp_path, "resume")
    assert result.returncode == 0, result.stderr
    with ReadOnlyRepository(root) as repository:
        assert repository.summary("001").results == 4
        assert [
            len(repository.get_results_for_attempt("001", a.attempt_id))
            for a in repository.iter_attempts("001")
        ] == [2, 2]
    assert service_counts(root)["forwards"] == 2


@pytest.mark.parametrize(
    "stage",
    [
        "after_batch_retry_context",
        "after_batch_retry_attempt",
        "after_batch_retry_shard",
    ],
)
def test_retry_subset_recovers_original_authorization(
    tmp_path: Path, stage: str
) -> None:
    spec = batch_spec(tmp_path, responses=1, traits=2, grouped=True)
    root = tmp_path / "store"
    spec.output_path = root
    configuration = tmp_path / "run.json"
    configuration.write_text(spec.model_dump_json())
    mock = mock_fixture(tmp_path, rows=[{"outcome": "invalid"}])
    prepare_batch(spec, store=root, run_id="001", mock=mock)
    resume_batch("001", store=root, spec=spec, mock=mock)
    with ReadOnlyRepository(root) as repository:
        failed = next(repository.iter_results("001"))
    new_path = tmp_path / "retry.json"
    new_path.write_text('{"schema_version":1,"rows":[{}]}')
    retry_mock = BatchMockOptions(fixture_path=new_path)
    retry = RetrySelection(task_ids=(failed.task_id,), operation_id="subset-001")

    program = """
import os, sys
from pathlib import Path
from judgmentllm import BatchMockOptions, RetrySelection, load_run_spec, resume_batch
root, config, fixture, selection, stage = sys.argv[1:]
def die(current, identity):
    if current == stage: os._exit(73)
resume_batch('001', store=Path(root), spec=load_run_spec(config),
    mock=BatchMockOptions(fixture_path=Path(fixture)),
    retry=RetrySelection.model_validate_json(selection), _fault=die)
"""
    environment = {
        **os.environ,
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    died = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            program,
            str(root),
            str(configuration),
            str(new_path),
            retry.model_dump_json(),
            stage,
        ],
        cwd=tmp_path,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert died.returncode == 73, (died.stdout, died.stderr)
    result = resume_batch("001", store=root, spec=spec, mock=retry_mock, retry=retry)
    assert result.batch.missing_members == 0
    resume_batch("001", store=root, spec=spec, mock=retry_mock, retry=retry)
    with ReadOnlyRepository(root) as repository:
        assert repository.summary("001").attempts == 2
        records = tuple(repository.iter_results("001"))
        assert len(records) == 3 and sum(r.status == "complete" for r in records) == 1
        assert (
            repository.connection.execute(
                "SELECT count(*) FROM batch_preparations"
            ).fetchone()[0]
            == 2
        )
    assert service_counts(root)["forwards"] == 4


def test_sql_rejects_cross_job_member_bridge_and_replacement(tmp_path: Path) -> None:
    spec = batch_spec(tmp_path)
    root = tmp_path / "store"
    prepare_batch(spec, store=root, run_id="001")
    resume_batch("001", store=root, spec=spec)
    from judgmentllm.persistence.repository import Repository

    with Repository(root) as repository:
        row = repository.connection.execute(
            "SELECT * FROM batch_member_receipts LIMIT 1"
        ).fetchone()
        data = list(row)
        data[1] = "wrong-submission"
        with pytest.raises(sqlite3.IntegrityError):
            repository.connection.execute(
                "INSERT INTO batch_member_receipts VALUES (?,?,?,?,?,?,?,?,?)", data
            )
        with pytest.raises(sqlite3.IntegrityError):
            repository.connection.execute(
                "INSERT OR REPLACE INTO batch_member_receipts VALUES (?,?,?,?,?,?,?,?,?)",
                tuple(row),
            )
