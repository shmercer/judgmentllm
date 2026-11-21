"""Process death, late cancellation, immutable evidence and conservative recovery."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_native_batch import batch_spec, mock_fixture, service_counts

from judgmentllm import (
    BatchMockOptions,
    cancel_batch,
    collect_batch,
    get_batch_status,
    load_run_spec,
    poll_batch,
    prepare_batch,
    resume_batch,
    submit_batch,
)
from judgmentllm.config import RunSpecV2
from judgmentllm.lifecycle import cancel_run
from judgmentllm.persistence.repository import ReadOnlyRepository

CHILD = """
import os,sys
from pathlib import Path
from judgmentllm import BatchMockOptions, submit_batch, resume_batch, collect_batch, cancel_batch, load_run_spec
from judgmentllm.persistence.repository import ReadOnlyRepository
action,root,config,fixture,stage,kind=sys.argv[1:]
def fault(current,identity):
    if current != stage: return
    if kind:
        with ReadOnlyRepository(root) as repository:
            row=repository.connection.execute("SELECT kind FROM batch_operations WHERE instance_id=?",(identity,)).fetchone()
        if row is None or row[0] != kind: return
    os._exit(73)
function={"submit":submit_batch,"resume":resume_batch,"collect":collect_batch,"cancel":cancel_batch}[action]
result=function("001",store=Path(root),spec=load_run_spec(config),mock=BatchMockOptions(fixture_path=Path(fixture)),_fault=fault)
print(result.model_dump_json())
"""


def child(
    directory: Path, action: str, stage: str = "", kind: str = ""
) -> subprocess.CompletedProcess[str]:
    environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    # Process correctness deliberately uses source imports; installed qualification
    # remains the independent seventh gate, never credited by this fixture.
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    return subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            CHILD,
            action,
            str(directory / "store"),
            str(directory / "run.json"),
            str(directory / "batch-fixture.json"),
            stage,
            kind,
        ],
        env=environment,
        cwd=directory,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )


def setup(
    directory: Path, *, capture: str = "full", **fixture: object
) -> tuple[RunSpecV2, BatchMockOptions]:
    spec = batch_spec(directory, capture=capture)
    config = directory / "run.json"
    config.write_text(spec.model_dump_json(), encoding="utf-8")
    mock = mock_fixture(directory, **fixture)
    prepare_batch(config, store=directory / "store", run_id="001", mock=mock)
    restored = load_run_spec(config)
    assert isinstance(restored, RunSpecV2)
    return restored, mock


@pytest.mark.parametrize("capture", ["full", "minimal"])
def test_lost_create_ack_stop_late_discovery_single_cancel_and_two_rows(
    tmp_path: Path, capture: str
) -> None:
    _, mock = setup(
        tmp_path,
        capture=capture,
        rows=[{}, {"outcome": "confirmed_cancellation"}],
        input_tokens=7,
        output_tokens=3,
    )
    root = tmp_path / "store"
    assert child(tmp_path, "submit", "after_batch_forward", "create").returncode == 73
    assert service_counts(root) == {"forwards": 2, "jobs": 1, "cancelled": 0}
    stopped = cancel_batch(
        "001",
        store=root,
        mock=mock.model_copy(update={"reconciliation_available": False}),
    )
    assert stopped.batch.local_stop and stopped.batch.cancel_all
    assert stopped.batch.accepted_jobs == 0
    assert stopped.batch.unresolved_operations == 1
    assert service_counts(root)["forwards"] == 2
    recovered = child(tmp_path, "resume")
    assert recovered.returncode == 0, recovered.stderr
    status = get_batch_status("001", store=root)
    assert status.batch.accepted_jobs == 1 and status.batch.missing_members == 0
    assert service_counts(root) == {"forwards": 3, "jobs": 1, "cancelled": 1}
    assert child(tmp_path, "resume").returncode == 0
    with ReadOnlyRepository(root) as repository:
        assert repository.summary("001").attempts == 2
        records = tuple(repository.iter_results("001"))
        assert {r.status for r in records} == {"complete", "cancelled"}
        receipts = [
            json.loads(r[0])
            for r in repository.connection.execute(
                "SELECT record_json FROM batch_member_receipts"
            )
        ]
        assert [r["usage"]["input_tokens"] for r in receipts].count(7) == 1
        assert [r["usage"]["output_tokens"] for r in receipts].count(3) == 1


@pytest.mark.parametrize(
    "stage,kind",
    [
        ("after_batch_intent", "create"),
        ("after_batch_operation_receipt", "create"),
        ("after_batch_operation_commit", "create"),
        ("after_batch_member_receipt", ""),
        ("after_batch_member_observation", ""),
        ("during_batch_results", ""),
        ("after_batch_member_results", ""),
        ("after_batch_cursor", ""),
    ],
)
def test_death_boundaries_never_repeat_a_send(
    tmp_path: Path, stage: str, kind: str
) -> None:
    setup(tmp_path)
    action = "submit" if kind else "resume"
    died = child(tmp_path, action, stage, kind)
    assert died.returncode == 73, (died.stdout, died.stderr)
    before = service_counts(tmp_path / "store")
    recovered = child(tmp_path, "resume")
    assert recovered.returncode == 0, recovered.stderr
    assert service_counts(tmp_path / "store") == before
    status = get_batch_status("001", store=tmp_path / "store")
    if stage == "after_batch_intent":
        assert (
            status.batch.unresolved_operations == 1
            and status.batch.missing_members == 2
        )
    else:
        assert status.batch.missing_members == 0


def test_missing_sibling_after_terminal_job_stays_unresolved(tmp_path: Path) -> None:
    spec, mock = setup(
        tmp_path,
        rows=[{}, {"outcome": "confirmed_cancellation"}],
        missing_positions=[1],
        input_tokens=7,
        output_tokens=3,
    )
    root = tmp_path / "store"
    submit_batch("001", store=root, spec=spec, mock=mock)
    cancel_batch("001", store=root, mock=mock)
    poll_batch("001", store=root, mock=mock)
    result = child(tmp_path, "resume")
    assert result.returncode == 0, result.stderr
    status = get_batch_status("001", store=root)
    assert status.batch.missing_members == 1 and status.batch.recovery_required
    with ReadOnlyRepository(root) as repository:
        records = tuple(repository.iter_results("001"))
        assert len(records) == 1 and records[0].status == "complete"


def test_ambiguous_cancel_not_redelivered_and_local_stop_not_cancel_all(
    tmp_path: Path,
) -> None:
    spec, mock = setup(tmp_path, cancel="uncertain")
    root = tmp_path / "store"
    submit_batch("001", store=root, spec=spec, mock=mock)
    unavailable = mock.model_copy(update={"reconciliation_available": False})
    cancel_batch("001", store=root, mock=unavailable)
    for _ in range(2):
        result = resume_batch("001", store=root, spec=spec, mock=unavailable)
        assert result.batch.unresolved_operations == 1
    assert service_counts(root) == {"forwards": 3, "jobs": 1, "cancelled": 1}
    # Local stop works without constructing an adapter or reopening its fixture.
    other = tmp_path / "other"
    other.mkdir()
    setup(other)
    assert child(other, "submit", "after_batch_forward", "create").returncode == 73
    stopped = cancel_run("001", store=other / "store")
    assert (
        stopped.batch is not None
        and stopped.batch.local_stop
        and not stopped.batch.cancel_all
    )
    assert child(other, "resume").returncode == 0
    assert service_counts(other / "store")["cancelled"] == 0


def test_known_job_503_is_a_safe_read_not_submission_retry(tmp_path: Path) -> None:
    spec, mock = setup(tmp_path, status_failures=1)
    root = tmp_path / "store"
    submit_batch("001", store=root, spec=spec, mock=mock)
    with ReadOnlyRepository(root) as repository:
        original = tuple(
            (s.submission_id, s.remote_id) for s in repository.iter_submissions("001")
        )
    assert poll_batch("001", store=root, mock=mock).outcome == "partial"
    assert child(tmp_path, "resume").returncode == 0
    with ReadOnlyRepository(root) as repository:
        assert (
            tuple(
                (s.submission_id, s.remote_id)
                for s in repository.iter_submissions("001")
            )
            == original
        )
        assert repository.summary("001").attempts == 2
    assert service_counts(root) == {"forwards": 2, "jobs": 1, "cancelled": 0}


@pytest.mark.parametrize("capture", ["full", "minimal"])
def test_error_only_results_are_explicit_outcomes(tmp_path: Path, capture: str) -> None:
    spec, mock = setup(
        tmp_path,
        capture=capture,
        rows=[{"outcome": "provider_error"}],
        reverse_results=True,
        duplicate_results=True,
    )
    submit_batch("001", store=tmp_path / "store", spec=spec, mock=mock)
    assert (
        collect_batch(
            "001", store=tmp_path / "store", spec=spec, mock=mock
        ).batch.missing_members
        == 0
    )
    with ReadOnlyRepository(tmp_path / "store") as repository:
        assert [r.status for r in repository.iter_results("001")] == [
            "errored",
            "errored",
        ]
        assert repository.summary("001").attempts == 2


@pytest.mark.parametrize("kind", ["upload", "create", "cancel"])
@pytest.mark.parametrize(
    "stage",
    [
        "after_batch_intent",
        "after_batch_forward",
        "after_batch_operation_receipt",
        "after_batch_operation_commit",
    ],
)
def test_every_mutation_death_boundary_keeps_one_forward_per_instance(
    tmp_path: Path, kind: str, stage: str
) -> None:
    spec, mock = setup(tmp_path, rows=[{}, {"outcome": "confirmed_cancellation"}])
    root = tmp_path / "store"
    if kind == "cancel":
        submit_batch("001", store=root, spec=spec, mock=mock)
    died = child(tmp_path, "cancel" if kind == "cancel" else "submit", stage, kind)
    assert died.returncode == 73, (died.stdout, died.stderr)
    before = service_counts(root)["forwards"]
    recovered = child(tmp_path, "resume")
    assert recovered.returncode == 0, recovered.stderr
    expected_new = 1 if kind == "upload" and stage != "after_batch_intent" else 0
    assert service_counts(root)["forwards"] == before + expected_new
    import sqlite3

    with sqlite3.connect(root / "mock-batch-service.sqlite3") as connection:
        assert (
            connection.execute(
                "SELECT coalesce(max(forwards),0) FROM operations"
            ).fetchone()[0]
            <= 1
        )
    status = get_batch_status("001", store=root)
    assert status.batch.unresolved_operations == (stage == "after_batch_intent")


@pytest.mark.parametrize("capture", ["full", "minimal"])
def test_member_receipt_recovers_without_retrieval_only_when_retained(
    tmp_path: Path, capture: str
) -> None:
    spec, mock = setup(tmp_path, capture=capture)
    root = tmp_path / "store"
    assert child(tmp_path, "resume", "after_batch_member_receipt").returncode == 73
    unavailable = mock.model_copy(update={"retrieval_available": False})
    result = collect_batch("001", store=root, spec=spec, mock=unavailable)
    assert result.batch.missing_members == (1 if capture == "full" else 2)
    assert child(tmp_path, "resume").returncode == 0
    assert get_batch_status("001", store=root).batch.missing_members == 0
    assert service_counts(root)["forwards"] == 2
