"""Admission and immutable audit evidence through real bulk-live stores."""

# pyright: reportPrivateUsage=false
from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal

import pytest
from test_bulk_live import bulk_spec, physical_rows
from test_cli_lifecycle import control
from test_openai_decisions_sdk import spec_for
from test_openai_lifecycle import production_spec

from judgmentllm._bulk_dispatch import BulkDispatch, BulkTokenBoundError
from judgmentllm.config import (
    RunSpecification,
    RunSpecV3,
    validate_run_spec,
)
from judgmentllm.decisions import provenance as decisions_provenance
from judgmentllm.evidence.usage import ProviderUsage
from judgmentllm.execution import execute_run, recover_run
from judgmentllm.execution.bulk_provenance import export_bulk_provenance
from judgmentllm.execution.session import ExecutionSession
from judgmentllm.persistence.artifacts import ArtifactStore
from judgmentllm.persistence.bulk import (
    BulkReservation,
    CommittedBulkReservation,
    ReservationRows,
    publish_reservation,
    reservation_reference,
)
from judgmentllm.persistence.hashing import bytes_digest, canonical_bytes
from judgmentllm.persistence.models import ArtifactIdentity, IntegrityError
from judgmentllm.persistence.ownership import executor_is_active
from judgmentllm.persistence.repository import ReadOnlyRepository, Repository
from judgmentllm.preparation import PreparationError, prepare_run
from judgmentllm.production_transport import production_context
from judgmentllm.providers.decisions_mock import MockDecisionsAdapter
from judgmentllm.providers.mock import MockAdapter
from judgmentllm.providers.openai import OpenAIAdapter, OpenAIConfigurationError

_SealedBulk = tuple[RunSpecification, Path, MockAdapter]


@pytest.fixture
def sealed_bulk(tmp_path: Path) -> _SealedBulk:
    spec = bulk_spec(
        tmp_path, limits={"requests_per_minute": 1, "tokens_per_minute": 100_000}
    )
    root = tmp_path / "runtime"
    adapter = MockAdapter(root / "mock-service.sqlite3")
    with prepare_run(
        spec, store=root, run_id="001", capabilities=adapter.capabilities(spec)
    ):
        pass
    return spec, root, adapter


@pytest.mark.asyncio
async def test_bulk_boundary_rejects_wrong_mode_owner_and_missing_bound(
    sealed_bulk: _SealedBulk, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec, root, adapter = sealed_bulk
    session = ExecutionSession("001", root, adapter, spec, False, None)
    try:
        work = session.next_work()
        assert work is not None
        limits = spec.execution.bulk_limits
        budget = BulkDispatch(session.token, limits, prior_debits=False)
        foreign = BulkDispatch("another-owner", limits, prior_debits=False)
        with pytest.raises(IntegrityError, match="mode differs"):
            session.boundary(work, None)
        with pytest.raises(IntegrityError, match="mode differs"):
            session.boundary(
                replace(
                    work, prepared=work.prepared.model_copy(update={"bulk_live": False})
                ),
                budget,
            )
        with pytest.raises(IntegrityError, match="another owner"):
            session.boundary(work, foreign)
        monkeypatch.setattr(adapter, "synthetic_token_bound", None)
        with pytest.raises(BulkTokenBoundError, match="bound is unavailable"):
            session.boundary(work, budget)
        assert (
            session.repository.get_submission("001", work.submission_id).state
            == "prepared"
        )
        assert not budget.pending and adapter.submit_calls == 0
    finally:
        session.close()


@pytest.mark.parametrize("conflict", [False, True])
@pytest.mark.asyncio
async def test_bulk_admission_rechecks_at_owned_debit_transaction(
    sealed_bulk: _SealedBulk, conflict: bool
) -> None:
    spec, root, adapter = sealed_bulk

    def fault(stage: str, _: str) -> None:
        if stage != "after_bulk_reservation":
            return
        if not conflict:
            budget.stop()
            return
        assert session.prepared is not None and work is not None
        store = session.prepared.store
        path = store.reference_path(reservation_reference(work.submission_id))
        receipt = BulkReservation.model_validate_json(path.read_bytes())
        session.repository.register_artifact("001", publish_reservation(store, receipt))

    session = ExecutionSession("001", root, adapter, spec, False, fault)
    try:
        budget = BulkDispatch(
            session.token, spec.execution.bulk_limits, prior_debits=False
        )
        work = session.next_work()
        assert work is not None
        if conflict:
            with pytest.raises(
                IntegrityError, match="conflicts with prepared submission"
            ):
                session.boundary(work, budget)
        else:
            with pytest.raises(asyncio.CancelledError):
                session.boundary(work, budget)
        assert (
            session.repository.get_submission("001", work.submission_id).state
            == "prepared"
        )
        assert not any(
            event.new_state == "submitting"
            for event in session.repository.events("001")
        )
        assert not budget.pending and adapter.submit_calls == 0
    finally:
        session.close()


def _receipt() -> BulkReservation:
    return BulkReservation(
        run_id="001",
        request_uid="request",
        attempt_id="attempt",
        submission_id="submission",
        manifest_sha256="1" * 64,
        payload_sha256="2" * 64,
        limits=None,
        method="request_v1",
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"tokens": 1},
        {"method": "synthetic_utf8_v1"},
        {"limits": {"tokens_per_minute": 1}, "method": "synthetic_utf8_v1"},
        {
            "limits": {"tokens_per_minute": 1},
            "method": "synthetic_utf8_v1",
            "tokens": 2,
        },
        {"limits": {"tokens_per_minute": 1}, "tokens": 1},
    ],
)
def test_reservation_rejects_invalid_token_accounting(changes: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="token reservation|token bound"):
        BulkReservation.model_validate({**_receipt().model_dump(), **changes})


def test_reservation_size_and_order_are_checked_before_publication(
    tmp_path: Path,
) -> None:
    store = ArtifactStore(tmp_path)
    receipt = _receipt()
    oversized = BulkReservation.model_validate(
        {**receipt.model_dump(), "request_uid": "x" * 1_048_577}
    )
    with pytest.raises(IntegrityError, match="exceeds its size limit"):
        publish_reservation(store, oversized)
    assert not store.reference_path(
        reservation_reference(receipt.submission_id)
    ).exists()
    assert not list(tmp_path.glob("objects/*/*"))
    rows = ReservationRows(
        iter(
            [
                CommittedBulkReservation(
                    event_seq=1,
                    receipt_sha256=bytes_digest(canonical_bytes(receipt)),
                    receipt=receipt,
                )
            ]
        )
    )
    assert rows.for_attempt("different-attempt") is None
    with pytest.raises(IntegrityError, match="no ordered audit attempt"):
        rows.finish()


def _damage_row(
    repository: Repository,
    table: Literal["artifacts", "events"],
    statement: str,
    parameters: tuple[object, ...],
) -> None:
    # Model offline damage while retaining the exact schema for reopen.
    trigger = f"{table}_no_update"
    with repository.transaction():
        (sql,) = repository.connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (trigger,)
        ).fetchone()
        repository.connection.execute(f"DROP TRIGGER {trigger}")
        assert repository.connection.execute(statement, parameters).rowcount == 1
        repository.connection.execute(sql)


@pytest.mark.parametrize(
    ("damage", "message"),
    [
        ("count", "journal counts differ"),
        ("size", "duplicated or oversized"),
        ("reference", "reference differs"),
        ("binding", "differs from the sealed submission"),
        ("owner", "lacks its owned debit transaction"),
    ],
)
@pytest.mark.asyncio
async def test_reopen_rejects_corrupt_reservation_journal(
    sealed_bulk: _SealedBulk, damage: str, message: str
) -> None:
    spec, root, adapter = sealed_bulk
    await execute_run("001", spec=spec, store=root, adapter=adapter)
    with Repository(root) as repository:
        store = ArtifactStore(repository.run_directory("001"))
        (submission,) = repository.iter_submissions("001")
        reference_name = reservation_reference(submission.submission_id)
        reference = store.reference_path(reference_name)
        raw = reference.read_bytes()
        artifact = repository.get_artifact("001", bytes_digest(raw))
        (publish_event,) = (
            event
            for event in repository.events("001")
            if event.kind == "artifact_registered"
            and event.entity_id == artifact.sha256
        )
        if damage == "count":
            repository.register_artifact(
                "001", store.put(b"unmatched", artifact.media_type)
            )
        elif damage == "size":
            _damage_row(
                repository,
                "artifacts",
                "UPDATE artifacts SET byte_length=? WHERE sha256=?",
                (1_048_577, artifact.sha256),
            )
        elif damage == "reference":
            reference.unlink()
            reference.write_bytes(raw + b"\n")
        elif damage == "binding":
            receipt = BulkReservation.model_validate_json(raw)
            new_receipt = BulkReservation.model_validate(
                {**receipt.model_dump(), "request_uid": "different-request"}
            )
            other = store.put(canonical_bytes(new_receipt), artifact.media_type)
            reference.unlink()
            store.publish_reference(other, reference_name)
            _damage_row(
                repository,
                "artifacts",
                "UPDATE artifacts SET sha256=?, byte_length=?, relative_path=? WHERE sha256=?",
                (other.sha256, other.byte_length, other.relative_path, artifact.sha256),
            )
            _damage_row(
                repository,
                "events",
                "UPDATE events SET entity_id=? WHERE event_seq=?",
                (other.sha256, publish_event.event_seq),
            )
        else:
            _damage_row(
                repository,
                "events",
                "UPDATE events SET owner_token=? WHERE event_seq=?",
                ("foreign-owner", publish_event.event_seq),
            )
    with pytest.raises(IntegrityError, match=message):
        await recover_run("001", spec=spec, store=root, adapter=adapter)
    assert adapter.submit_calls == 1
    with ReadOnlyRepository(root) as repository:
        assert not executor_is_active(repository, "001")


def test_empty_bulk_provenance_reuses_companions_across_cli_cancel(
    sealed_bulk: _SealedBulk,
) -> None:
    _, root, adapter = sealed_bulk
    before = export_bulk_provenance("001", root=root)
    response, _ = control(["runs", "cancel", "--run", "001", "--store", str(root)])
    assert response["command"] == "runs cancel"
    with ReadOnlyRepository(root) as repository:
        assert repository.get_run("001").state == "cancelled"
        assert not list(repository.iter_submissions("001"))
    assert adapter.submit_calls == 0
    after = export_bulk_provenance("001", root=root)
    assert before.bundle_id != after.bundle_id
    assert before.manifest.files == after.manifest.files
    assert all(companion.rows == 0 for companion in after.manifest.files)


def test_empty_bulk_provenance_rejects_inconsistent_registered_companion(
    sealed_bulk: _SealedBulk,
) -> None:
    _, root, _ = sealed_bulk
    with Repository(root) as repository:
        store = ArtifactStore(repository.run_directory("001"))
        repository.register_artifact(
            "001",
            ArtifactIdentity(
                sha256=bytes_digest(b""),
                byte_length=1,
                media_type="application/x-ndjson",
            ),
        )
    with pytest.raises(IntegrityError, match="artifact length differs"):
        export_bulk_provenance("001", root=root)
    assert not list((store.root / "bulk-provenance").glob("*.json"))


@pytest.mark.parametrize("fault", ["usage", "snapshot"])
@pytest.mark.asyncio
async def test_bulk_provenance_refuses_mismatched_durable_identity(
    sealed_bulk: _SealedBulk, fault: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec, root, adapter = sealed_bulk
    await execute_run("001", store=root, adapter=adapter, spec=spec)
    if fault == "usage":
        authoritative = Repository.authoritative_usage

        def use_wrong_attempt(
            repository: Repository,
            run_id: str,
            submission_id: str,
            *,
            high_water_mark: int | None = None,
        ) -> ProviderUsage:
            usage = authoritative(
                repository, run_id, submission_id, high_water_mark=high_water_mark
            )
            assert usage is not None
            return ProviderUsage.model_validate(
                {**usage.model_dump(), "attempt_id": "wrong-attempt"}
            )

        monkeypatch.setattr(Repository, "authoritative_usage", use_wrong_attempt)
        with pytest.raises(IntegrityError, match="usage differs from its attempt"):
            export_bulk_provenance("001", root=root)
    else:
        first = export_bulk_provenance("001", root=root)
        target = Path(first.manifest_path)
        target.unlink()
        wrong_cut = first.manifest.model_copy(
            update={"event_high_water_mark": first.manifest.event_high_water_mark + 1}
        )
        target.write_bytes(canonical_bytes(wrong_cut))
        with pytest.raises(IntegrityError, match="bundle differs from its snapshot"):
            export_bulk_provenance("001", root=root)


def test_decisions_provenance_republishes_disappearing_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = bulk_spec(tmp_path, decisions=True)
    root = tmp_path / "runtime"
    adapter = MockDecisionsAdapter(root / "mock-service.sqlite3")
    with prepare_run(
        spec, store=root, run_id="001", capabilities=adapter.capabilities(spec)
    ):
        pass
    first = decisions_provenance.export_decisions_provenance("001", store=root)
    reference = Path(first.manifest_path)
    original = reference.read_bytes()
    read = decisions_provenance.read_decisions_provenance
    observed = False

    def disappear_once(repository: Repository, run_id: str, bundle_id: str):
        nonlocal observed
        if not observed:
            reference.unlink()
            observed = True
        return read(repository, run_id, bundle_id)

    monkeypatch.setattr(
        decisions_provenance, "read_decisions_provenance", disappear_once
    )
    same = decisions_provenance.export_decisions_provenance("001", store=root)
    assert observed and same == first
    assert reference.read_bytes() == original


def test_generative_live_has_no_bulk_provenance(tmp_path: Path) -> None:
    doc = bulk_spec(tmp_path).model_dump()
    doc["execution"]["mode"] = "live"
    root = tmp_path / "store"
    with prepare_run(validate_run_spec(doc), store=root, run_id="001"):
        pass
    with pytest.raises(ValueError, match="requires a sealed bulk-live run"):
        export_bulk_provenance("001", root=root)


def test_sealed_native_bulk_attempt_exports_before_submission(tmp_path: Path) -> None:
    doc = production_spec(tmp_path / "inputs").model_dump()
    doc["execution"]["mode"] = "bulk_live"
    spec = validate_run_spec(doc)
    assert isinstance(spec, RunSpecV3)
    adapter = OpenAIAdapter(spec.endpoint, spec.model)
    root = tmp_path / "store"
    with prepare_run(
        spec,
        store=root,
        run_id="001",
        capabilities=adapter.capabilities(spec),
        transport_context=adapter.transport_context,
    ):
        pass
    _, rows = physical_rows("001", root)
    assert rows and all(
        row["submission_id"] is None
        and row["reservation"] is None
        and row["selected_usage"] is None
        for row in rows
    )


def test_direct_bulk_preparation_refuses_qualification_transport_before_insert(
    tmp_path: Path,
) -> None:
    doc = production_spec(tmp_path / "inputs").model_dump()
    doc["execution"]["mode"] = "bulk_live"
    spec = validate_run_spec(doc)
    assert isinstance(spec, RunSpecV3)
    context = replace(
        production_context(spec),
        kind="openai-qualification-v1",
        source_commit="1" * 40,
        qualification_plan_sha256="2" * 64,
    )
    root = tmp_path / "store"
    with pytest.raises(PreparationError):
        prepare_run(spec, store=root, run_id="001", transport_context=context)
    with ReadOnlyRepository(root) as repository:
        assert not repository.connection.execute("SELECT 1 FROM runs").fetchall()


def test_decisions_production_tpm_is_refused_by_direct_preparation(
    tmp_path: Path,
) -> None:
    doc = spec_for(tmp_path / "inputs").model_dump()
    doc["execution"].update(mode="bulk_live", bulk_limits={"tokens_per_minute": 100})
    spec = validate_run_spec(doc)
    root = tmp_path / "store"
    with pytest.raises(PreparationError, match="no verified token bound"):
        prepare_run(spec, store=root, run_id="001")
    with ReadOnlyRepository(root) as repository:
        assert not repository.connection.execute("SELECT 1 FROM runs").fetchall()


def test_openai_adapter_refuses_native_mode_in_matching_route(tmp_path: Path) -> None:
    doc = production_spec(tmp_path / "inputs").model_dump()
    doc["execution"]["mode"] = "batch_native"
    spec = validate_run_spec(doc)
    adapter = OpenAIAdapter(spec.endpoint, spec.model)
    with pytest.raises(OpenAIConfigurationError, match=r"execution\.mode"):
        adapter.validate(spec)
