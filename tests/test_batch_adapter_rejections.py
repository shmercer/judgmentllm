"""The synthetic batch service rejects foreign jobs, cursors and unsafe rows."""

import json
from collections.abc import AsyncIterator
from contextlib import closing
from pathlib import Path
from typing import Any, cast

import pytest
import pytest_asyncio
from test_batch_rejections import prepared_batch as prepared_batch

from judgmentllm import (
    BatchControlOptionsV1,
    BatchLimitsV1,
    BatchMockOptions,
    RunSpecV2,
)
from judgmentllm.batch_models import BatchCursorV1, BatchOperationIntentV1
from judgmentllm.execution.native_batch import BatchSession, InvocationBudget
from judgmentllm.persistence.hashing import bytes_digest, canonical_bytes
from judgmentllm.persistence.models import IntegrityError
from judgmentllm.preparation.batch import members_for, reconstruct
from judgmentllm.providers.batch_mock import MockBatchAdapter, screen_row
from judgmentllm.providers.batch_protocol import BatchEvidenceWithheld
from judgmentllm.providers.protocol import PreparedRequestV2


@pytest_asyncio.fixture
async def batch_session(
    prepared_batch: tuple[RunSpecV2, Path],
) -> AsyncIterator[BatchSession]:
    spec, root = prepared_batch
    adapter = MockBatchAdapter(root / "mock-batch-service.sqlite3", BatchMockOptions())
    with BatchSession(
        "001", root, adapter, InvocationBudget(BatchControlOptionsV1())
    ) as session:
        session.open_sources(spec, False)
        yield session


def member_wire(session: BatchSession) -> tuple[str, PreparedRequestV2]:
    assert session.prepared is not None
    member = next(
        members_for(
            session.prepared,
            next(
                session.repository.connection.execute(
                    "SELECT shard_id FROM batch_preparations"
                )
            )[0],
        )
    )
    return member.member_key, reconstruct(session.prepared, session.adapter, member)[1]


def operation(adapter: MockBatchAdapter) -> BatchOperationIntentV1:
    return BatchOperationIntentV1(
        run_id="001",
        shard_id="shard",
        logical_id="a" * 64,
        instance_id="b" * 64,
        sequence=1,
        kind="upload",
        service_id=adapter.identity,
        manifest_sha256="c" * 64,
        payload_sha256=bytes_digest(b""),
        owner_token="owner",
    )


def test_missing_or_ambiguous_original_service_is_rejected(tmp_path: Path) -> None:
    ledger = tmp_path / "service.sqlite3"
    with pytest.raises(IntegrityError, match="unavailable"):
        MockBatchAdapter(ledger, BatchMockOptions())
    adapter = MockBatchAdapter(ledger, BatchMockOptions(), create=True)
    with closing(adapter.connect()) as connection, connection:
        connection.execute("INSERT INTO service VALUES ('other-service')")
    with pytest.raises(IntegrityError, match="identity is invalid"):
        MockBatchAdapter(ledger, BatchMockOptions())


@pytest.mark.parametrize(
    "value", ["job", "mock_job_" + "z" * 64, "mock_file_" + "a" * 63]
)
def test_remote_identity_is_not_arbitrary_provider_text(value: str) -> None:
    with pytest.raises(IntegrityError, match="unsafe identity"):
        MockBatchAdapter.check_remote(value)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["malformed", "remote", "fence", "cancel-job"])
async def test_control_receipt_rejects_unproven_operation_identity(
    batch_session: BatchSession,
    fault: str,
) -> None:
    adapter = cast(MockBatchAdapter, batch_session.adapter)
    intent = operation(adapter)
    raw = await adapter.fence_unsent(intent)
    fields: dict[str, Any] = json.loads(raw)
    if fault == "malformed":
        fields["outcome"] = "accepted"
        fields["fence"] = None
    elif fault == "fence":
        fields["fence"]["proof_sha256"] = "f" * 64
    else:
        fields.update(outcome="accepted", fence=None, remote_id="mock_job_" + "f" * 64)
        if fault == "cancel-job":
            intent = intent.model_copy(
                update={"kind": "cancel", "remote_id": "mock_job_" + "e" * 64}
            )
            fields["intent_sha256"] = bytes_digest(canonical_bytes(intent))
    with pytest.raises(IntegrityError):
        adapter.decode_receipt(canonical_bytes(fields), intent)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["payload", "service"])
async def test_operation_body_and_service_are_checked_before_forward(
    batch_session: BatchSession,
    fault: str,
) -> None:
    adapter = cast(MockBatchAdapter, batch_session.adapter)
    intent = operation(adapter)
    if fault == "service":
        intent = intent.model_copy(update={"service_id": "other-service"})

    async def payload() -> AsyncIterator[bytes]:
        yield b"wrong" if fault == "payload" else b""

    with pytest.raises(IntegrityError):
        await adapter.invoke(intent, payload(), lambda: None)
    with closing(adapter.connect()) as connection:
        assert connection.execute("SELECT count(*) FROM operations").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_reconciliation_rejects_changed_sender_without_a_second_forward(
    batch_session: BatchSession,
) -> None:
    adapter = cast(MockBatchAdapter, batch_session.adapter)
    intent = operation(adapter)
    await adapter.fence_unsent(intent)
    with pytest.raises(IntegrityError, match="operation identity"):
        await adapter.reconcile_operation(
            intent.model_copy(update={"owner_token": "other"})
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["status", "source", "job", "no-cursor", "cursor"])
async def test_result_reads_require_the_original_accepted_job_and_index(
    batch_session: BatchSession,
    fault: str,
) -> None:
    session = batch_session
    await session.submit()
    adapter = cast(MockBatchAdapter, session.adapter)
    key, wire = member_wire(session)
    submission = next(session.repository.iter_submissions("001"))
    assert submission.remote_id is not None
    members = [(key, 0, wire)]
    if fault == "status":
        with pytest.raises(IntegrityError, match="status job"):
            await adapter.status(submission.submission_id, "mock_job_" + "f" * 64)
    elif fault in ("source", "job"):
        with pytest.raises((IntegrityError, BatchEvidenceWithheld)):
            adapter.source_scope(
                submission.submission_id,
                submission.remote_id if fault == "source" else "mock_job_" + "f" * 64,
                "untrusted" if fault == "source" else "output",
                members,
            )
    else:
        commitment, raw = await adapter.result_index(
            submission.submission_id, submission.remote_id, "output", members
        )
        cursor = None
        if fault == "cursor":
            index = json.loads(raw)
            cursor = BatchCursorV1(
                run_id="001",
                submission_id=submission.submission_id,
                source_id=commitment.source_id,
                position=0,
                prefix_sha256=index["prefix_sha256"][0],
                index_sha256="f" * 64,
            )
        with pytest.raises(IntegrityError, match="cursor"):
            async for _ in adapter.results(
                submission.submission_id,
                submission.remote_id,
                "output",
                members,
                cursor,
            ):
                pytest.fail("unverified cursor yielded a result")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "missing",
        "oversized-row",
        "prefix-bound",
        "index-bound",
        "stored-bound",
        "stored-scope",
    ],
)
async def test_original_result_index_rejects_changed_or_oversized_evidence(
    batch_session: BatchSession,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    session = batch_session
    await session.submit()
    adapter = cast(MockBatchAdapter, session.adapter)
    key, wire = member_wire(session)
    submission = next(session.repository.iter_submissions("001"))
    assert submission.remote_id is not None
    members = [(key, 0, wire)]
    if fault.startswith("stored"):
        await adapter.result_index(
            submission.submission_id, submission.remote_id, "output", members
        )
        with closing(adapter.connect()) as connection, connection:
            connection.execute("DROP TRIGGER result_index_no_update")
            if fault == "stored-bound":
                connection.execute(
                    "UPDATE result_indexes SET record_json=?",
                    (" " * (adapter.options.limits.max_record_bytes + 1),),
                )
            else:
                connection.execute(
                    "UPDATE result_indexes SET scope_sha256=?", ("f" * 64,)
                )
    elif fault in ("missing", "oversized-row"):

        def invalid_row(*args: object) -> bytes | None:
            return (
                None
                if fault == "missing"
                else b"x" * (adapter.options.limits.max_record_bytes + 1)
            )

        monkeypatch.setattr(adapter, "source_row", invalid_row)
    else:
        adapter.options = adapter.options.model_copy(
            update={
                "limits": BatchLimitsV1(
                    max_record_bytes=100 if fault == "prefix-bound" else 150
                )
            }
        )

        def small_row(*args: object) -> bytes:
            return b"{}\n"

        monkeypatch.setattr(adapter, "source_row", small_row)
    with pytest.raises(IntegrityError):
        await adapter.result_index(
            submission.submission_id, submission.remote_id, "output", members
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault", ["json", "root", "body", "output", "probabilities", "indices", "secret"]
)
async def test_row_screening_withholds_unsafe_or_unattributable_material(
    batch_session: BatchSession,
    fault: str,
) -> None:
    adapter = cast(MockBatchAdapter, batch_session.adapter)
    key, wire = member_wire(batch_session)
    raw = adapter.source_row(wire, 0, key, "output")
    assert raw is not None and screen_row(raw, {key: wire}) is not None
    fields: dict[str, Any] = json.loads(raw)
    if fault == "json":
        raw = b"\xff"
    elif fault == "root":
        raw = b"[]"
    elif fault == "body":
        fields["response"] = []
    elif fault == "output":
        fields["response"]["output"] = {"unknown": True}
    elif fault == "probabilities":
        fields["response"]["member_probabilities"] = {"unrequested-trait": []}
    elif fault == "indices":
        fields["response"]["member_expected_indices"] = {"unrequested-trait": 0.5}
    else:
        fields["response"]["observation_key"] = "PROHIBITED_BATCH_ROW"
    if fault not in ("json", "root"):
        raw = canonical_bytes(fields)
    assert screen_row(raw, {key: wire}) is None


def test_fixture_commitment_rejects_prohibited_output_material() -> None:
    from judgmentllm.providers.batch_mock import BatchMockFixtureV1
    from judgmentllm.providers.mock import MockScenario

    fixture = BatchMockFixtureV1(
        rows=(MockScenario(explanation="PROHIBITED_BATCH_VALUE"),)
    )
    with pytest.raises(ValueError, match="prohibited output material"):
        fixture.commitment()


@pytest.mark.asyncio
async def test_connection_setup_failure_closes_the_actual_sqlite_handle(
    batch_session: BatchSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sqlite3
    from unittest.mock import Mock

    adapter = cast(MockBatchAdapter, batch_session.adapter)
    connection = sqlite3.connect(adapter.path)
    failed = Mock(wraps=connection)
    failed.execute.side_effect = OSError("synthetic pragma failure")

    def connect(*args: object, **kwargs: object) -> object:
        return failed

    monkeypatch.setattr(sqlite3, "connect", connect)
    with pytest.raises(OSError, match="pragma failure"):
        adapter.connect()
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")


@pytest.mark.asyncio
async def test_acknowledgment_delay_and_unavailable_or_wrong_result_sources(
    batch_session: BatchSession,
) -> None:
    adapter = cast(MockBatchAdapter, batch_session.adapter)
    intent = operation(adapter).model_copy(update={"kind": "create"})
    adapter.fixture = adapter.fixture.model_copy(update={"ack_delay_seconds": 0.001})

    async def payload() -> AsyncIterator[bytes]:
        yield b""

    receipt = adapter.decode_receipt(
        await adapter.invoke(intent, payload(), lambda: None), intent
    )
    assert receipt.outcome == "accepted"
    key, wire = member_wire(batch_session)
    assert adapter.source_row(wire, 0, key, "error") is None
    adapter.fixture = adapter.fixture.model_copy(update={"missing_positions": (0,)})
    assert adapter.source_row(wire, 0, key, "output") is None
    adapter.options = adapter.options.model_copy(update={"retrieval_available": False})
    with pytest.raises(OSError, match="retrieval unavailable"):
        await adapter.read_row(wire, 0, key, "output")


@pytest.mark.asyncio
async def test_result_stream_rechecks_immutable_source_after_index_admission(
    batch_session: BatchSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = batch_session
    await session.submit()
    adapter = cast(MockBatchAdapter, session.adapter)
    key, wire = member_wire(session)
    submission = next(session.repository.iter_submissions("001"))
    assert submission.remote_id is not None
    members = [(key, 0, wire)]
    commitment, raw = await adapter.result_index(
        submission.submission_id, submission.remote_id, "output", members
    )
    cursor = BatchCursorV1(
        run_id="001",
        submission_id=submission.submission_id,
        source_id=commitment.source_id,
        position=0,
        prefix_sha256=json.loads(raw)["prefix_sha256"][0],
        index_sha256=commitment.index_sha256,
    )

    async def missing(*args: object) -> None:
        return None

    monkeypatch.setattr(adapter, "read_row", missing)
    with pytest.raises(IntegrityError, match="immutable result source changed"):
        async for _ in adapter.results(
            submission.submission_id, submission.remote_id, "output", members, cursor
        ):
            pytest.fail("changed source yielded a result")


@pytest.mark.asyncio
async def test_batch_compiler_rejects_an_incompatible_parent_compiler(
    batch_session: BatchSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from judgmentllm.providers.mock import MockAdapter

    prepared = batch_session.prepared
    assert prepared is not None
    dependency = next(prepared.iter_request_dependencies())
    rendered = prepared.render_attempt(dependency)

    def incompatible(*args: object) -> None:
        return None

    monkeypatch.setattr(MockAdapter, "prepare", incompatible)
    with pytest.raises(IntegrityError, match="V2 compiler"):
        batch_session.adapter.prepare(prepared.spec, dependency, rendered)
