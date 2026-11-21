"""One-cut audit artifacts and deliberate scientific retries retain all history."""

import csv
import json
from pathlib import Path

import pytest
from test_native_batch import batch_spec, mock_fixture

from judgmentllm import (
    BatchControlOptionsV1,
    RetrySelection,
    collect_batch,
    export_batch_provenance,
    prepare_batch,
    resume_batch,
    submit_batch,
)
from judgmentllm.execution.batch_exports import BatchAuditReportV1
from judgmentllm.lifecycle import export_run_provenance, export_run_results, get_run
from judgmentllm.lifecycle_models import LifecycleError
from judgmentllm.persistence.hashing import bytes_digest
from judgmentllm.persistence.repository import ReadOnlyRepository


def file_path(report: BatchAuditReportV1, name: str) -> Path:
    file = next(f for f in report.manifest.files if f.name == name)
    assert file.artifact.relative_path is not None
    # The manifest lives at <run>/batch/audits/<id>.json.
    return Path(report.manifest_path).parents[2] / file.artifact.relative_path


def test_audit_files_share_one_cut_and_old_bundle_survives_collection(
    tmp_path: Path,
) -> None:
    spec = batch_spec(tmp_path)
    root = tmp_path / "store"
    prepare_batch(spec, store=root, run_id="001")
    submit_batch("001", store=root, spec=spec)
    collect_batch(
        "001", store=root, spec=spec, options=BatchControlOptionsV1(max_result_rows=1)
    )
    first = export_batch_provenance("001", store=root)
    old = file_path(first, "results.jsonl").read_bytes()
    assert len(old.splitlines()) == 1
    for file in first.manifest.files:
        path = file_path(first, file.name)
        assert bytes_digest(path.read_bytes()) == file.artifact.sha256
    resumed = resume_batch("001", store=root, spec=spec)
    assert resumed.batch.missing_members == 0
    second = export_run_provenance("001", store=root)
    assert isinstance(second, BatchAuditReportV1)
    assert len(file_path(second, "results.jsonl").read_bytes().splitlines()) == 2
    reopened = export_batch_provenance(
        "001", store=root, bundle_id=first.manifest.bundle_id
    )
    assert file_path(reopened, "results.jsonl").read_bytes() == old
    members = [
        json.loads(line)
        for line in file_path(second, "members.jsonl").read_text().splitlines()
    ]
    assert sum(len(m["result_ids"]) for m in members) == 2
    assert all(
        m["evidence"]["identity"]["attempt_id"] == m["preparation"]["attempt_id"]
        for m in members
    )
    assert get_run("001", store=root).batch is not None
    assert export_run_results("001", store=root).row_count == 2


def test_explicit_retry_creates_scientific_attempt_and_child_shard(
    tmp_path: Path,
) -> None:
    spec = batch_spec(tmp_path, responses=1)
    root = tmp_path / "store"
    first_mock = mock_fixture(tmp_path, rows=[{"outcome": "invalid"}])
    prepare_batch(spec, store=root, run_id="001", mock=first_mock)
    resume_batch("001", store=root, spec=spec, mock=first_mock)
    with ReadOnlyRepository(root) as repository:
        failed = next(repository.iter_results("001"))
    new_fixture = tmp_path / "retry.json"
    new_fixture.write_text(json.dumps({"schema_version": 1, "rows": [{}]}))
    new_mock = first_mock.model_copy(update={"fixture_path": new_fixture})
    retry = RetrySelection(task_ids=(failed.task_id,), operation_id="retry-01")
    result = resume_batch("001", store=root, spec=spec, mock=new_mock, retry=retry)
    assert result.batch.missing_members == 0
    with ReadOnlyRepository(root) as repository:
        results = tuple(repository.iter_results("001"))
        assert [r.status for r in results] == ["invalid", "complete"]
        assert results[0].task_id == results[1].task_id
        assert results[0].attempt_id != results[1].attempt_id
        assert repository.summary("001").attempts == 2
    resume_batch("001", store=root, spec=spec, mock=new_mock, retry=retry)
    with ReadOnlyRepository(root) as repository:
        assert repository.summary("001").attempts == 2
    with pytest.raises(LifecycleError):
        resume_batch(
            "001",
            store=root,
            spec=spec,
            mock=new_mock,
            retry=RetrySelection(
                task_ids=(failed.task_id,),
                operation_id="different",
                allow_completed=False,
            ),
        )


def test_empty_audit_companions_keep_typed_csv_headers(tmp_path: Path) -> None:
    spec = batch_spec(tmp_path, responses=0)
    root = tmp_path / "store"
    prepare_batch(spec, store=root, run_id="001")
    report = export_batch_provenance("001", store=root)
    for name in ("jobs", "members", "operations", "controls"):
        assert file_path(report, name + ".jsonl").read_bytes() == b""
        with file_path(report, name + ".csv").open(
            newline="", encoding="utf-8"
        ) as stream:
            reader = csv.DictReader(stream)
            assert (
                reader.fieldnames is not None and "schema_version" in reader.fieldnames
            )
            assert list(reader) == []
    assert (
        report.manifest.cut.operation_count
        == report.manifest.cut.member_receipt_count
        == 0
    )
