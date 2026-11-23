"""Offline native-batch acceptance through public services and real persistence."""

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest
from test_preparation_rendering import specification

from judgmentllm import (
    BatchControlOptionsV1,
    BatchLimitsV1,
    BatchMockOptions,
    RunSpecV2,
    cancel_batch,
    collect_batch,
    poll_batch,
    prepare_batch,
    resume_batch,
    stop_batch,
    submit_batch,
    validate_run_spec,
)
from judgmentllm.batch_models import BatchRunStatusV1
from judgmentllm.lifecycle_models import LifecycleError
from judgmentllm.persistence.repository import ReadOnlyRepository
from judgmentllm.preparation import prepare_run


def batch_spec(
    directory: Path,
    *,
    capture: str = "full",
    responses: int = 2,
    traits: int = 1,
    grouped: bool = False,
) -> RunSpecV2:
    original = specification(directory / "inputs", responses=responses, traits=traits)
    data = original.model_dump()
    data.update(
        schema_version=2, provider="mock", endpoint="synthetic", capture=capture
    )
    data["execution"].update(mode="batch_native", cache_policy="off")
    if grouped:
        data["execution"]["grouping"] = "grouped"
    checked = validate_run_spec(data)
    assert isinstance(checked, RunSpecV2)
    return checked


def mock_fixture(directory: Path, **fields: object) -> BatchMockOptions:
    path = directory / "batch-fixture.json"
    path.write_text(json.dumps({"schema_version": 1, **fields}), encoding="utf-8")
    return BatchMockOptions(fixture_path=path)


def service_counts(root: Path) -> dict[str, int]:
    with sqlite3.connect(root / "mock-batch-service.sqlite3") as connection:
        # Logical intent hashes remain separate from scientific attempts.
        return {
            "forwards": connection.execute(
                "SELECT coalesce(sum(forwards),0) FROM operations"
            ).fetchone()[0],
            "jobs": connection.execute("SELECT count(*) FROM jobs").fetchone()[0],
            "cancelled": connection.execute(
                "SELECT coalesce(sum(cancelled),0) FROM jobs"
            ).fetchone()[0],
        }


@pytest.mark.parametrize("capture", ["full", "minimal"])
def test_terminal_two_row_partial_replay_and_missing_sibling(
    tmp_path: Path, capture: str
) -> None:
    spec = batch_spec(tmp_path, capture=capture)
    mock = mock_fixture(
        tmp_path,
        rows=[{}, {"outcome": "confirmed_cancellation"}],
        input_tokens=7,
        output_tokens=3,
    )
    root = tmp_path / "store"
    prepared = prepare_batch(spec, store=root, run_id="001", mock=mock)
    assert prepared.batch.jobs == 1
    assert service_counts(root)["forwards"] == 0
    assert submit_batch("001", store=root, spec=spec, mock=mock).outcome == "ok"
    assert cancel_batch("001", store=root, mock=mock).batch.cancel_all
    assert poll_batch("001", store=root, mock=mock).run_status == "cancelled"
    first = collect_batch(
        "001",
        store=root,
        spec=spec,
        mock=mock,
        options=BatchControlOptionsV1(max_result_rows=1),
    )
    assert first.batch.missing_members == 1
    final = resume_batch("001", store=root, spec=spec, mock=mock)
    assert final.batch.missing_members == 0
    counts = service_counts(root)
    resume_batch("001", store=root, spec=spec, mock=mock)
    assert service_counts(root) == counts == {"forwards": 3, "jobs": 1, "cancelled": 1}
    with ReadOnlyRepository(root) as repository:
        records = tuple(repository.iter_results("001"))
        assert len(records) == 2
        assert {r.status for r in records} == {"complete", "cancelled"}
        receipts = [
            json.loads(r[0])
            for r in repository.connection.execute(
                "SELECT record_json FROM batch_member_receipts"
            )
        ]
        usage = [r["usage"] for r in receipts]
        assert sum(u["input_tokens"] or 0 for u in usage) == 7
        assert sum(u["output_tokens"] or 0 for u in usage) == 3
        assert all(u["reported_cost"] is None for u in usage)
        assert repository.summary("001").attempts == 2


def test_unsupported_route_fails_before_store_or_service(tmp_path: Path) -> None:
    spec = batch_spec(tmp_path)
    spec.provider, spec.endpoint = "openai", "responses"
    with pytest.raises(LifecycleError, match="Native batch currently requires"):
        prepare_batch(spec, store=tmp_path / "never-created")
    assert not (tmp_path / "never-created").exists()


def test_deterministic_shards_are_sealed_before_upload(tmp_path: Path) -> None:
    spec = batch_spec(tmp_path, responses=3)
    mock = BatchMockOptions(limits=BatchLimitsV1(max_members=1))
    root = tmp_path / "store"
    prepared = prepare_batch(spec, store=root, run_id="01", mock=mock)
    assert prepared.batch.jobs == 3
    with ReadOnlyRepository(root) as repository:
        manifest = json.loads(
            repository.get_manifest("01").artifact
            and (repository.run_directory("01") / "manifest.json").read_text()
        )
        assert manifest["schema_version"] == 12
        assert manifest["batch_member_count"] == 3
        assert (
            repository.connection.execute(
                "SELECT count(*) FROM submissions"
            ).fetchone()[0]
            == 0
        )


@pytest.mark.parametrize("action", [submit_batch, cancel_batch, stop_batch])
def test_batch_controls_reject_other_manifest_before_control_writes(
    tmp_path: Path, action: Callable[..., BatchRunStatusV1]
) -> None:
    spec = batch_spec(tmp_path)
    spec.execution.mode = "live"
    root = tmp_path / "store"
    with prepare_run(spec, store=root, run_id="plain"):
        pass
    with ReadOnlyRepository(root) as repository:
        before = repository.connection.execute(
            "SELECT count(*) FROM events"
        ).fetchone()[0]
        directory = repository.run_directory("plain")
    with pytest.raises(LifecycleError):
        action("plain", store=root)
    assert not (directory / "batch").exists()
    assert not (root / "mock-batch-service.sqlite3").exists()
    with ReadOnlyRepository(root) as repository:
        assert (
            repository.connection.execute("SELECT count(*) FROM events").fetchone()[0]
            == before
        )
