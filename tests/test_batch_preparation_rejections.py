"""Frozen batch membership, parent evidence and schema markers fail closed."""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from test_batch_adapter_rejections import batch_session as batch_session
from test_batch_rejections import prepared_batch as prepared_batch
from test_native_batch import batch_spec

from judgmentllm import (
    BatchLimitsV1,
    BatchMockOptions,
    RetrySelection,
    RunSpecV2,
    prepare_batch,
)
from judgmentllm.batch_models import BatchMemberReceiptV1, BatchMemberV1, BatchShardV1
from judgmentllm.execution.native_batch import BatchSession
from judgmentllm.lifecycle_models import LifecycleError
from judgmentllm.persistence.hashing import bytes_digest, canonical_bytes
from judgmentllm.persistence.models import AttemptRecord, IntegrityError
from judgmentllm.persistence.native_batch import register, rows
from judgmentllm.persistence.repository import Repository
from judgmentllm.preparation import PreparationError, _load_manifest, batch, prepare_run  # pyright: ignore[reportPrivateUsage]
from judgmentllm.preparation.batch_retry import prepare_batch_retry
from judgmentllm.preparation.manifest import ExecutableRunManifestV12
from judgmentllm.providers.batch_mock import MockBatchAdapter


def corrupt_row(
    root: Path, table: str, statement: str, parameters: tuple[object, ...] = ()
) -> None:
    """Model external data corruption while keeping the genuine schema guards."""
    with Repository(root) as repository, repository.transaction():
        connection = repository.connection
        name = table + "_immutable_update"
        original = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name=?", (name,)
        ).fetchone()[0]
        connection.execute(f"DROP TRIGGER {name}")
        connection.execute(statement, parameters)
        connection.execute(original)


@pytest.mark.parametrize("fault", ["compiler", "input-limit", "explicit-adapter"])
def test_preparation_rejects_incompatible_compiler_and_unsealed_admission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    spec = batch_spec(tmp_path, responses=1)
    root = tmp_path / "store"
    if fault == "explicit-adapter":
        with pytest.raises(PreparationError, match="explicit batch preparation"):
            with prepare_run(spec, store=root, run_id="001"):
                pytest.fail("implicit native preparation was accepted")
    else:
        if fault == "compiler":

            def wrong_compiler(*args: object) -> None:
                return None

            monkeypatch.setattr(MockBatchAdapter, "prepare", wrong_compiler)
        with pytest.raises(LifecycleError):
            prepare_batch(
                spec,
                store=root,
                run_id="001",
                mock=BatchMockOptions(
                    limits=BatchLimitsV1(max_input_bytes=1)
                    if fault == "input-limit"
                    else BatchLimitsV1()
                ),
            )
    with Repository(root) as repository:
        assert (
            repository.connection.execute(
                "SELECT count(*) FROM submissions"
            ).fetchone()[0]
            == 0
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "child-order",
        "shard-order",
        "context",
        "membership",
        "plan",
        "crosswalk",
        "position",
        "attempt",
        "index",
        "reconstruct",
    ],
)
async def test_initial_batch_crosschecks_every_frozen_membership_layer(
    batch_session: BatchSession,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    session = batch_session
    prepared = session.prepared
    assert prepared is not None
    shard = next(rows(session.repository, "batch_preparations", "001", BatchShardV1))
    member = next(batch.members_for(prepared, shard.shard_id))
    if fault == "reconstruct":
        with pytest.raises(IntegrityError, match="reconstruction"):
            batch.reconstruct(
                prepared,
                session.adapter,
                member.model_copy(update={"row_sha256": "f" * 64}),
            )
        return
    if fault == "child-order":
        shard = shard.model_copy(update={"ordinal": 2})
    elif fault == "shard-order":
        shard = shard.model_copy(update={"ordinal": 1})
        prepared.manifest = session.manifest.model_copy(update={"batch_shard_count": 2})
    elif fault == "context":
        shard = shard.model_copy(
            update={
                "context": shard.context.model_copy(update={"fixture_sha256": "f" * 64})
            }
        )
    elif fault == "membership":
        shard = shard.model_copy(update={"member_count": 2})
    elif fault == "plan":
        monkeypatch.setattr(prepared, "iter_request_dependencies", lambda: iter(()))
    elif fault in ("crosswalk", "position", "attempt"):
        changes: dict[str, Any] = (
            {"dependency_sha256": "f" * 64}
            if fault == "crosswalk"
            else {"position": 7}
            if fault == "position"
            else {"payload": member.payload.model_copy(update={"sha256": "f" * 64})}
        )
        member = member.model_copy(update=changes)

        def altered_members(*args: object) -> Iterator[BatchMemberV1]:
            yield member

        monkeypatch.setattr(batch, "members_for", altered_members)
    else:
        dependencies = tuple(prepared.iter_request_dependencies())
        monkeypatch.setattr(
            prepared, "iter_request_dependencies", lambda: iter(dependencies)
        )
        prepared.manifest = session.manifest.model_copy(
            update={"batch_index_sha256": "f" * 64}
        )

    def altered_shards(*args: object) -> Iterator[BatchShardV1]:
        yield shard

    monkeypatch.setattr(batch, "rows", altered_shards)
    messages = {
        "child-order": "retry shard order",
        "shard-order": "batch shard order",
        "context": "different context",
        "membership": "shard membership",
        "plan": "original request plan",
        "crosswalk": "task crosswalk",
        "position": "member order",
        "attempt": "attempt differs",
        "index": "preparation index",
    }
    with pytest.raises(IntegrityError, match=messages[fault]):
        batch.verify_batches(prepared)
    assert (
        session.repository.connection.execute(
            "SELECT count(*) FROM submissions"
        ).fetchone()[0]
        == 0
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "member-count",
        "authorization",
        "context",
        "parent-missing",
        "parent-identity",
        "crosswalk",
    ],
)
async def test_child_shard_requires_its_original_authorization_and_parent(
    batch_session: BatchSession,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    session = batch_session
    prepared = session.prepared
    assert prepared is not None
    await session.submit()
    await session.collect()
    original = next(session.repository.iter_results("001"))
    prepare_batch_retry(
        session,
        RetrySelection(
            task_ids=(original.task_id,), operation_id="retry-001", allow_completed=True
        ),
    )
    shard = tuple(rows(session.repository, "batch_preparations", "001", BatchShardV1))[
        -1
    ]
    member = next(batch.members_for(prepared, shard.shard_id))
    batch.verify_child_shard(prepared, shard, session.manifest)
    if fault == "member-count":
        shard = shard.model_copy(update={"member_count": 2})
    elif fault == "authorization":
        get_attempt = prepared.repository.get_attempt

        def unauthorized(run_id: str, attempt_id: str) -> AttemptRecord:
            return get_attempt(run_id, attempt_id).model_copy(
                update={"authorization_id": None}
            )

        monkeypatch.setattr(prepared.repository, "get_attempt", unauthorized)
    elif fault == "context":
        shard = shard.model_copy(
            update={
                "context": shard.context.model_copy(update={"fixture_sha256": "f" * 64})
            }
        )
    else:
        if fault == "parent-missing":
            member = member.model_copy(update={"parent_receipt_sha256": None})
        elif fault == "parent-identity":
            assert member.parent_receipt_sha256 is not None
            parent = BatchMemberReceiptV1.model_validate_json(
                prepared.store.read(
                    session.repository.get_artifact("001", member.parent_receipt_sha256)
                )
            )
            parent = parent.model_copy(
                update={
                    "identity": parent.identity.model_copy(
                        update={"request_uid": "other-request"}
                    )
                }
            )
            artifact = prepared.store.put(canonical_bytes(parent), "application/json")
            register(session.repository, "001", artifact)
            member = member.model_copy(
                update={"parent_receipt_sha256": artifact.sha256}
            )
        else:
            member = member.model_copy(update={"position": member.position + 1})

        def altered_members(*args: object) -> Iterator[BatchMemberV1]:
            yield member

        monkeypatch.setattr(batch, "members_for", altered_members)
    with pytest.raises(IntegrityError):
        batch.verify_child_shard(prepared, shard, session.manifest)
    assert len(tuple(session.repository.iter_results("001"))) == 1


@pytest.mark.parametrize(
    "field",
    [
        "schema_version",
        "run_spec_version",
        "output_contract_version",
        "request_identity_version",
    ],
)
def test_manifest_versions_cannot_be_coerced_or_silently_upgraded(
    prepared_batch: tuple[RunSpecV2, Path],
    field: str,
) -> None:
    _, root = prepared_batch
    with Repository(root) as repository:
        raw = (repository.run_directory("001") / "manifest.json").read_bytes()
    fields = json.loads(raw)
    fields[field] = 12.0 if field == "schema_version" else 2.0
    with pytest.raises(ValueError):
        ExecutableRunManifestV12.model_validate_json(json.dumps(fields))
    assert bytes_digest(
        canonical_bytes(ExecutableRunManifestV12.model_validate_json(raw))
    ) == bytes_digest(raw)


@pytest.mark.parametrize("fault", ["marker", "context"])
def test_database_batch_marker_cannot_replace_the_frozen_manifest(
    prepared_batch: tuple[RunSpecV2, Path],
    fault: str,
) -> None:
    _, root = prepared_batch
    with Repository(root) as repository:
        connection = repository.connection
        connection.execute("BEGIN")
        try:
            if fault == "marker":
                connection.execute("DROP TRIGGER batch_runs_immutable_delete")
                connection.execute("DELETE FROM batch_runs")
            else:
                connection.execute("DROP TRIGGER batch_runs_immutable_update")
                connection.execute("UPDATE batch_runs SET context_json='{}'")
            with pytest.raises(PreparationError):
                _load_manifest(repository, "001", unsealed=False)
        finally:
            connection.execute("ROLLBACK")


@pytest.mark.asyncio
async def test_member_index_hash_is_verified_before_reconstruction(
    batch_session: BatchSession,
) -> None:
    session = batch_session
    assert session.prepared is not None
    shard = next(rows(session.repository, "batch_preparations", "001", BatchShardV1))
    corrupt_row(
        session.root,
        "batch_prepared_members",
        "UPDATE batch_prepared_members SET record_json=json_set(record_json,'$.position',7)",
    )
    with pytest.raises(IntegrityError, match="member receipt differs from its index"):
        tuple(batch.members_for(session.prepared, shard.shard_id))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault", ["stop", "parent-member", "parent-outcome", "input-bound"]
)
async def test_retry_admission_requires_original_parent_and_sealed_limits(
    batch_session: BatchSession,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    import sqlite3
    from contextlib import closing

    from judgmentllm.preparation import batch_retry

    session = batch_session
    await session.submit()
    await session.collect()
    original = next(session.repository.iter_results("001"))
    retry = RetrySelection(
        task_ids=(original.task_id,), operation_id="retry-001", allow_completed=True
    )
    if fault == "stop":
        session.stop(remote=False)
    elif fault == "parent-member":
        # External corruption can bypass SQL; restore the exact guard before readback.
        with (
            closing(sqlite3.connect(session.root / "registry.sqlite3")) as connection,
            connection,
        ):
            name = "batch_prepared_members_immutable_delete"
            guard = connection.execute(
                "SELECT sql FROM sqlite_master WHERE name=?", (name,)
            ).fetchone()[0]
            connection.execute(f"DROP TRIGGER {name}")
            connection.execute("DELETE FROM batch_prepared_members")
            connection.execute(guard)
    elif fault == "parent-outcome":
        member = next(
            rows(session.repository, "batch_prepared_members", "001", BatchMemberV1)
        )
        session.store.reference_path(f"batch/members/{member.member_key}.json").unlink()
    else:

        def oversized(*args: object) -> bytes:
            return b"x" * (session.manifest.batch_context.limits.max_input_bytes + 1)

        monkeypatch.setattr(batch_retry, "encode_row", oversized)
    messages = {
        "stop": "stopped batch",
        "parent-member": "parent member",
        "parent-outcome": "proven parent outcome",
        "input-bound": "sealed batch limit",
    }
    with pytest.raises(IntegrityError, match=messages[fault]):
        prepare_batch_retry(session, retry)
    assert len(tuple(session.repository.iter_results("001"))) == 1
