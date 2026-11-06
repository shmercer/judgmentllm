"""Real registry/receipt tests over offline synthetic evidence and revisions."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from judgmentllm.evidence._base import EvidenceIdentity
from judgmentllm.evidence.order import ProviderObservationMetadata
from judgmentllm.evidence.reasoning import (
    ReasoningArtifactReference,
    ReasoningAuthorization,
    ReasoningEvidence,
)
from judgmentllm.evidence.usage import (
    AnthropicCacheCreationInput,
    AnthropicMessagesUsageInput,
    ProviderUsage,
    UsageContext,
    extract_anthropic_messages_usage,
)
from judgmentllm.persistence.artifacts import ArtifactStore
from judgmentllm.persistence.evidence import (
    AuthorizationCommitment,
    EvidenceReceipt,
    UsageRevision,
    evidence_digest,
    publish_authorization,
    publish_evidence_receipt,
    publish_usage_revision,
    read_evidence_receipt,
)
from judgmentllm.persistence.hashing import (
    bytes_digest,
    canonical_bytes,
    semantic_digest,
)
from judgmentllm.persistence.models import (
    ArtifactIdentity,
    ArtifactIntegrityError,
    AttemptRecord,
    IntegrityError,
    ManifestReference,
    OwnershipError,
    PersistenceError,
    RequestRecord,
    RunRecord,
    SchemaVersionError,
    SubmissionRecord,
    TaskRecord,
)
from judgmentllm.persistence.ownership import ExecutorOwnership
from judgmentllm.persistence.receipts import (
    AcknowledgmentReceipt,
    ResponseReceipt,
    publish_acknowledgment,
    publish_receipt,
)
from judgmentllm.persistence.repository import ReadOnlyRepository, Repository
from judgmentllm.persistence.schema import (
    _LEGACY_STATEMENTS,  # pyright: ignore[reportPrivateUsage]
    LEGACY_SCHEMA_CHECKSUM,
    _schema_digest,  # pyright: ignore[reportPrivateUsage]
    initialize,
)


@pytest.fixture
def prepared(
    tmp_path: Path, request: pytest.FixtureRequest
) -> Iterator[tuple[Repository, EvidenceReceipt]]:
    variant = getattr(request, "param", "default")
    provider = "anthropic" if variant == "anthropic" else "mock"
    endpoint = "messages" if variant == "anthropic" else "judgments"
    with Repository(tmp_path) as repo:
        repo.create_run(
            RunRecord(
                run_id="r",
                task_kind="rubric",
                provider=provider,
                endpoint=endpoint,
                model="fixture",
            )
        )
        store = ArtifactStore(repo.run_directory("r"))
        manifest = store.put(b"synthetic manifest")
        repo.seal_run("r", ManifestReference(artifact=manifest))
        repo.create_task(
            TaskRecord(
                run_id="r",
                task_id="t",
                trait_id="quality",
                item_id="001",
                scheduling_ordinal=0,
            )
        )
        repo.create_request(
            RequestRecord(
                run_id="r",
                request_uid="q",
                scheduling_ordinal=0,
                dependency_sha256=bytes_digest(b"dep"),
            ),
            ("t",),
        )
        repo.transition("request", "r", "q", "prepared")
        payload = store.put(b"synthetic payload")
        repo.register_artifact("r", payload)
        repo.create_attempt(
            AttemptRecord(
                run_id="r",
                attempt_id="a",
                request_uid="q",
                attempt_number=1,
                prompt_sha256=payload.sha256,
                payload_sha256=payload.sha256,
                membership_sha256=semantic_digest("attempt-membership", ["t"]),
            ),
            ("t",),
        )
        repo.create_submission(
            SubmissionRecord(
                run_id="r",
                submission_id="s",
                attempt_id="a",
                correlation_id="c",
                payload_sha256=payload.sha256,
            )
        )
        for state in ("claimed", "submitting"):
            repo.transition("attempt", "r", "a", state)
        repo.transition("submission", "r", "s", "submitting")
        repo.transition("submission", "r", "s", "acknowledged", remote_id="remote")
        repo.transition("attempt", "r", "a", "acknowledged")
        acknowledgment = AcknowledgmentReceipt(
            run_id="r",
            attempt_id="a",
            submission_id="s",
            correlation_id="c",
            payload_sha256=payload.sha256,
            remote_id="remote",
            observation_key="key",
        )
        publish_acknowledgment(store, acknowledgment)
        authorization = ReasoningAuthorization(
            capture="full",
            retention="provider_text" if variant == "thinking" else "none",
        )
        auth = AuthorizationCommitment(
            run_id="r",
            manifest_sha256=manifest.sha256,
            provider=provider,
            authorization=authorization,
            source="synthetic_fixture" if variant == "thinking" else "ordinary",
        )
        auth_artifact = publish_authorization(store, auth)
        raw = store.put(b'{"scores":[]}', "application/json")
        response = ResponseReceipt(
            run_id="r",
            attempt_id="a",
            submission_id="s",
            correlation_id="c",
            payload_sha256=payload.sha256,
            observation_id="o",
            observation_key="key",
            kind="response",
            capture="full",
            raw=raw,
            received_at="2026-11-06T00:00:00Z",
        )
        identity = EvidenceIdentity(
            run_id="r",
            request_uid="q",
            attempt_id="a",
            submission_id="s",
            observation_id="o",
        )
        usage = ProviderUsage(
            **identity.model_dump(),
            provider=provider,
            endpoint=endpoint,
            requested_model="fixture",
            observation_kind="response",
            usage_source="unknown"
            if variant == "anthropic"
            else "synthetic.reported_usage",
            input_tokens=11,
            output_tokens=7,
        )
        if variant == "anthropic":
            usage = extract_anthropic_messages_usage(
                identity,
                UsageContext(
                    provider="anthropic",
                    endpoint="messages",
                    requested_model="fixture",
                    observation_kind="response",
                ),
                AnthropicMessagesUsageInput(
                    cache_creation_input_tokens=100,
                    cache_creation=AnthropicCacheCreationInput(
                        ephemeral_5m_input_tokens=60, ephemeral_1h_input_tokens=40
                    ),
                ),
            )
        reasoning = ()
        if variant == "thinking":
            artifact = store.put(
                b"explicit synthetic protected reasoning", "text/plain; charset=utf-8"
            )
            reasoning = (
                ReasoningEvidence(
                    **identity.model_dump(),
                    kind="thinking_block",
                    provider_field="anthropic.messages.thinking",
                    block_ordinal=0,
                    availability="retained",
                    artifact=ReasoningArtifactReference(
                        sha256=artifact.sha256, byte_length=artifact.byte_length
                    ),
                    source_bytes_sha256=artifact.sha256,
                ),
            )
        order = ProviderObservationMetadata(
            **identity.model_dump(),
            requested_order="together",
            order_adherence="unknown",
            order_evidence="unavailable",
            unknown_reason="sdk_object_only",
        )
        receipt = EvidenceReceipt(
            identity=identity,
            correlation_id="c",
            payload_sha256=payload.sha256,
            acknowledgment_sha256=bytes_digest(canonical_bytes(acknowledgment)),
            response_receipt_sha256=bytes_digest(canonical_bytes(response)),
            response_receipt=response,
            observation_key="key",
            remote_id="remote",
            capture="full",
            authorization=authorization,
            authorization_sha256=auth_artifact.sha256,
            usage=usage,
            order=order,
            reasoning=reasoning,
            original=ArtifactStore.hash_only(
                raw.sha256, raw.byte_length, raw.media_type
            ),
            retained=raw,
            response_retention="exact",
        )
        publish_evidence_receipt(store, receipt)
        publish_receipt(store, response)
        repo.register_artifact("r", raw)
        repo.record_observation(response.observation())
        yield repo, receipt


def test_owned_commit_is_idempotent_and_reopens_read_only(
    prepared: tuple[Repository, EvidenceReceipt],
) -> None:
    repo, receipt = prepared
    assert repo.evidence_supported
    with pytest.raises(OwnershipError):
        repo.record_evidence(receipt, owner_token="wrong")
    with ExecutorOwnership(repo, "r") as owner:
        assert owner.owner is not None
        repo.record_evidence(receipt, owner_token=owner.owner.token)
        repo.record_evidence(receipt, owner_token=owner.owner.token)
    assert len(list(repo.usage_revision_history("r", "s", page_size=1))) == 1
    with ReadOnlyRepository(repo.root) as reader:
        assert reader.get_evidence_for_submission("r", "s") == receipt
        assert reader.get_evidence_for_observation("r", "o") == receipt
        assert list(reader.iter_evidence("r", page_size=1)) == [receipt]
        assert reader.authoritative_usage("r", "s") == receipt.usage
    assert repo.connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_atomic_rollback_and_missing_companion_are_safe(
    prepared: tuple[Repository, EvidenceReceipt],
) -> None:
    repo, receipt = prepared
    verified = repo.prepare_evidence_commit(receipt)
    with ExecutorOwnership(repo, "r") as owner:
        assert owner.owner is not None
        with pytest.raises(RuntimeError, match="synthetic interruption"):
            with repo.execution_transaction("r", owner.owner.token):
                repo.record_evidence(
                    receipt, owner_token=owner.owner.token, prepared=verified
                )
                raise RuntimeError("synthetic interruption")
    assert repo.get_evidence_for_submission("r", "s") is None
    with pytest.raises(IntegrityError, match="unavailable"):
        repo.get_evidence_for_observation("r", "o")


def test_explicit_revisions_select_snapshot_and_never_sum(
    prepared: tuple[Repository, EvidenceReceipt],
) -> None:
    repo, receipt = prepared
    with ExecutorOwnership(repo, "r") as owner:
        assert owner.owner is not None
        repo.record_evidence(receipt, owner_token=owner.owner.token)
        initial_high = repo.usage_high_water_mark("r")
        revision = UsageRevision(
            usage=receipt.usage.model_copy(update={"input_tokens": 15}),
            revision_id="correction-1",
            revision_number=1,
            source="synthetic.authoritative",
        )
        publish_usage_revision(ArtifactStore(repo.run_directory("r")), revision)
        repo.append_usage_revision(revision, owner_token=owner.owner.token)
        repo.append_usage_revision(revision, owner_token=owner.owner.token)
        assert (
            repo.authoritative_usage("r", "s", high_water_mark=initial_high)
            == receipt.usage
        )
        assert repo.authoritative_usage("r", "s") == revision.usage
        history = list(repo.usage_revision_history("r", "s", page_size=1))
        assert [(row.revision_number, row.source) for row in history] == [
            (0, "initial"),
            (1, "synthetic.authoritative"),
        ]
        conflict = revision.model_copy(update={"revision_id": "conflict"})
        publish_usage_revision(ArtifactStore(repo.run_directory("r")), conflict)
        with pytest.raises(IntegrityError, match="conflicts"):
            repo.append_usage_revision(conflict, owner_token=owner.owner.token)


@pytest.mark.parametrize(
    "table,column,value",
    [
        ("usage_revisions", "usage_json", '{"changed":true}'),
        ("observation_evidence", "order_json", '{"changed":true}'),
        ("evidence_receipts", "receipt_sha256", "a" * 64),
    ],
)
def test_committed_metadata_tampering_is_detected(
    prepared: tuple[Repository, EvidenceReceipt], table: str, column: str, value: str
) -> None:
    repo, receipt = prepared
    with ExecutorOwnership(repo, "r") as owner:
        assert owner.owner is not None
        repo.record_evidence(receipt, owner_token=owner.owner.token)
    with pytest.raises(sqlite3.IntegrityError):
        repo.connection.execute(f"UPDATE {table} SET {column}=?", (value,))
    repo.connection.execute(f"DROP TRIGGER {table}_immutable_update")
    repo.connection.execute("PRAGMA foreign_keys=OFF")
    repo.connection.execute(f"UPDATE {table} SET {column}=?", (value,))
    with pytest.raises(IntegrityError):
        repo.get_evidence_for_submission("r", "s")


def test_conflicting_publication_and_missing_receipt_object(
    prepared: tuple[Repository, EvidenceReceipt],
) -> None:
    repo, receipt = prepared
    store = ArtifactStore(repo.run_directory("r"))
    conflict = receipt.model_copy(
        update={"usage": receipt.usage.model_copy(update={"output_tokens": 8})}
    )
    with pytest.raises(ArtifactIntegrityError):
        publish_evidence_receipt(store, conflict)
    digest = evidence_digest(receipt)
    (store.root / "objects" / digest[:2] / digest).unlink()
    with pytest.raises(ArtifactIntegrityError):
        read_evidence_receipt(store, "s")


@pytest.mark.parametrize("page_size", [0, -1, 10001, True])
def test_bounded_iteration_rejects_invalid_pages(
    prepared: tuple[Repository, EvidenceReceipt], page_size: int
) -> None:
    repo, _ = prepared
    with pytest.raises(ValueError):
        list(repo.iter_evidence("r", page_size=page_size))


def test_true_schema_one_store_is_never_migrated(tmp_path: Path) -> None:
    database = tmp_path / "registry.sqlite3"
    with sqlite3.connect(database, isolation_level=None) as connection:
        for statement in _LEGACY_STATEMENTS:
            connection.execute(statement)
        connection.execute(
            "INSERT INTO schema_migrations VALUES (1,'durable_foundation',?,?,?)",
            (LEGACY_SCHEMA_CHECKSUM, _schema_digest(connection), "2026-11-06"),
        )
        connection.execute("PRAGMA user_version=1")
    before = database.read_bytes()
    with ReadOnlyRepository(tmp_path) as reader:
        assert not reader.evidence_supported
    assert database.read_bytes() == before
    with Repository(tmp_path) as writer:
        assert not writer.evidence_supported
        assert (
            writer.connection.execute(
                "SELECT name FROM sqlite_master WHERE name='evidence_receipts'"
            ).fetchone()
            is None
        )
    with sqlite3.connect(database, isolation_level=None) as connection:
        connection.execute("PRAGMA user_version=3")
        with pytest.raises(SchemaVersionError):
            initialize(connection)


@pytest.mark.parametrize("prepared", ["thinking"], indirect=True)
@pytest.mark.parametrize("damage", ["missing", "changed"])
def test_protected_reasoning_is_verified_on_reopening(
    prepared: tuple[Repository, EvidenceReceipt], damage: str
) -> None:
    repo, receipt = prepared
    with ExecutorOwnership(repo, "r") as owner:
        assert owner.owner is not None
        repo.record_evidence(receipt, owner_token=owner.owner.token)
    artifact = receipt.reasoning[0].artifact
    assert artifact is not None
    path = repo.run_directory("r") / "objects" / artifact.sha256[:2] / artifact.sha256
    if damage == "missing":
        path.unlink()
    else:
        path.write_bytes(b"different protected bytes")
    with ReadOnlyRepository(repo.root) as reader:
        with pytest.raises(ArtifactIntegrityError):
            reader.get_evidence_for_observation("r", "o")


@pytest.mark.parametrize("prepared", ["anthropic"], indirect=True)
def test_anthropic_disjoint_cache_subdivisions_persist_without_addition(
    prepared: tuple[Repository, EvidenceReceipt],
) -> None:
    repo, receipt = prepared
    with ExecutorOwnership(repo, "r") as owner:
        assert owner.owner is not None
        repo.record_evidence(receipt, owner_token=owner.owner.token)
    usage = repo.authoritative_usage("r", "s")
    assert usage is not None and usage.cache_write_tokens == 100
    assert [
        row.value for row in usage.breakdown if row.field.startswith("cache_creation.")
    ] == [60, 40]
    with pytest.raises(ValueError):
        extract_anthropic_messages_usage(
            receipt.identity,
            UsageContext(
                provider="anthropic",
                endpoint="messages",
                requested_model="fixture",
                observation_kind="response",
            ),
            AnthropicMessagesUsageInput(
                cache_creation_input_tokens=100,
                cache_creation=AnthropicCacheCreationInput(
                    ephemeral_5m_input_tokens=60, ephemeral_1h_input_tokens=50
                ),
            ),
        )
    assert len(list(repo.usage_revision_history("r", "s"))) == 1


@pytest.mark.parametrize(
    "stage", ["after_revision_receipt", "during_usage_revision", "after_usage_revision"]
)
def test_revision_interruption_reopens_and_replays_once(
    prepared: tuple[Repository, EvidenceReceipt], stage: str
) -> None:
    repo, receipt = prepared
    revision = UsageRevision(
        usage=receipt.usage.model_copy(update={"output_tokens": 9}),
        revision_id="correction",
        revision_number=1,
        source="synthetic.authoritative",
    )
    publish_usage_revision(ArtifactStore(repo.run_directory("r")), revision)

    def interrupt(checkpoint: str, _identity: str) -> None:
        if checkpoint == stage:
            raise RuntimeError("synthetic interruption")

    with ExecutorOwnership(repo, "r") as owner:
        assert owner.owner is not None
        repo.record_evidence(receipt, owner_token=owner.owner.token)
        with pytest.raises(RuntimeError, match="synthetic interruption"):
            repo.append_usage_revision(
                revision, owner_token=owner.owner.token, fault=interrupt
            )
    with Repository(repo.root) as reopened:
        with ExecutorOwnership(reopened, "r") as owner:
            assert owner.owner is not None
            reopened.append_usage_revision(revision, owner_token=owner.owner.token)
        assert len(list(reopened.usage_revision_history("r", "s", page_size=1))) == 2
        assert reopened.authoritative_usage("r", "s") == revision.usage


@pytest.mark.parametrize("field", ["request_uid", "submission_id", "observation_id"])
def test_wrong_execution_identity_cannot_attach_matching_content(
    prepared: tuple[Repository, EvidenceReceipt], field: str
) -> None:
    repo, receipt = prepared
    identity = receipt.identity.model_copy(update={field: "synthetic-wrong-identity"})
    usage = receipt.usage.model_copy(update={field: "synthetic-wrong-identity"})
    assert receipt.order is not None
    order = receipt.order.model_copy(update={field: "synthetic-wrong-identity"})
    conflicting = receipt.model_copy(
        update={"identity": identity, "usage": usage, "order": order}
    )
    with ExecutorOwnership(repo, "r") as owner:
        assert owner.owner is not None
        with pytest.raises(PersistenceError) as error:
            repo.record_evidence(conflicting, owner_token=owner.owner.token)
        assert "synthetic-wrong-identity" not in str(error.value)
    assert repo.get_evidence_for_submission("r", "s") is None


def test_mismatched_authorization_and_source_version_fail_content_free(
    prepared: tuple[Repository, EvidenceReceipt],
) -> None:
    repo, receipt = prepared
    conflicting = receipt.model_copy(
        update={
            "authorization": ReasoningAuthorization(capture="full", retention="summary")
        }
    )
    with pytest.raises(ArtifactIntegrityError):
        publish_evidence_receipt(ArtifactStore(repo.run_directory("r")), conflicting)
    for version in (True, 2):
        with pytest.raises(ValueError):
            UsageRevision.model_validate(
                {
                    "usage": receipt.usage,
                    "revision_id": "corrected",
                    "revision_number": 1,
                    "source": "synthetic.authoritative",
                    "source_version": version,
                }
            )


def test_revision_history_changes_are_detected(
    prepared: tuple[Repository, EvidenceReceipt],
) -> None:
    repo, receipt = prepared
    revision = UsageRevision(
        usage=receipt.usage.model_copy(update={"output_tokens": 9}),
        revision_id="correction",
        revision_number=1,
        source="synthetic.authoritative",
    )
    publish_usage_revision(ArtifactStore(repo.run_directory("r")), revision)
    with ExecutorOwnership(repo, "r") as owner:
        assert owner.owner is not None
        repo.record_evidence(receipt, owner_token=owner.owner.token)
        repo.append_usage_revision(revision, owner_token=owner.owner.token)
    repo.connection.execute("DROP TRIGGER usage_revisions_immutable_update")
    repo.connection.execute(
        "UPDATE usage_revisions SET source='provider.authoritative' WHERE revision_number=1"
    )
    with pytest.raises(IntegrityError):
        list(repo.usage_revision_history("r", "s"))


def test_new_evidence_schema_mutation_is_rejected_on_read_open(
    prepared: tuple[Repository, EvidenceReceipt],
) -> None:
    repo, _ = prepared
    repo.connection.execute("DROP INDEX idx_usage_revisions_run_revision")
    with pytest.raises(SchemaVersionError):
        ReadOnlyRepository(repo.root)


@pytest.mark.parametrize(
    "stage", ["after_revision_receipt", "during_usage_revision", "after_usage_revision"]
)
def test_process_death_during_revision_recovers_without_duplication(
    prepared: tuple[Repository, EvidenceReceipt], stage: str
) -> None:
    repo, receipt = prepared
    revision = UsageRevision(
        usage=receipt.usage.model_copy(update={"output_tokens": 12}),
        revision_id="process-correction",
        revision_number=1,
        source="synthetic.authoritative",
    )
    publish_usage_revision(ArtifactStore(repo.run_directory("r")), revision)
    with ExecutorOwnership(repo, "r") as owner:
        assert owner.owner is not None
        repo.record_evidence(receipt, owner_token=owner.owner.token)
    child = """import os,sys
from judgmentllm.persistence.repository import Repository
from judgmentllm.persistence.ownership import ExecutorOwnership
from judgmentllm.persistence.evidence import UsageRevision
revision=UsageRevision.model_validate_json(sys.stdin.buffer.read())
with Repository(sys.argv[1]) as repo:
    with ExecutorOwnership(repo,'r') as owner:
        def fault(stage, identity):
            if stage==sys.argv[2]:
                os._exit(87)
        repo.append_usage_revision(revision,owner_token=owner.owner.token,fault=fault)
"""
    result = subprocess.run(
        [sys.executable, "-c", child, str(repo.root), stage],
        input=canonical_bytes(revision),
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 87
    with Repository(repo.root) as reopened:
        with ExecutorOwnership(reopened, "r") as owner:
            assert owner.owner is not None
            reopened.append_usage_revision(revision, owner_token=owner.owner.token)
        assert [
            record.revision_number
            for record in reopened.usage_revision_history("r", "s", page_size=1)
        ] == [0, 1]
        assert reopened.authoritative_usage("r", "s") == revision.usage


def test_verified_evidence_commit_has_no_filesystem_io_inside_transaction(
    prepared: tuple[Repository, EvidenceReceipt], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, receipt = prepared
    verified = repo.prepare_evidence_commit(receipt)

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("filesystem verification entered write transaction")

    with ExecutorOwnership(repo, "r") as owner:
        assert owner.owner is not None
        with monkeypatch.context() as patch:
            patch.setattr(ArtifactStore, "verify", forbidden)
            patch.setattr(ArtifactStore, "read", forbidden)
            patch.setattr(ArtifactStore, "put", forbidden)
            with repo.execution_transaction("r", owner.owner.token):
                repo.record_evidence(
                    receipt, owner_token=owner.owner.token, prepared=verified
                )
                repo.record_evidence(
                    receipt, owner_token=owner.owner.token, prepared=verified
                )
    assert repo.get_evidence_for_submission("r", "s") == receipt


def test_verified_commit_cannot_attach_another_receipt(
    prepared: tuple[Repository, EvidenceReceipt],
) -> None:
    repo, receipt = prepared
    verified = repo.prepare_evidence_commit(receipt)
    changed = receipt.model_copy(
        update={"usage": receipt.usage.model_copy(update={"output_tokens": 18})}
    )
    with ExecutorOwnership(repo, "r") as owner:
        assert owner.owner is not None
        with pytest.raises(IntegrityError, match="verification"):
            repo.record_evidence(
                changed, owner_token=owner.owner.token, prepared=verified
            )
    assert repo.get_evidence_for_submission("r", "s") is None


@pytest.mark.parametrize("prepared", ["thinking"], indirect=True)
def test_evidence_preserves_existing_content_metadata_across_roles(
    prepared: tuple[Repository, EvidenceReceipt],
) -> None:
    repo, receipt = prepared
    store = ArtifactStore(repo.run_directory("r"))
    receipt_bytes = canonical_bytes(receipt)
    receipt_artifact = store.put(receipt_bytes, "application/json")
    reasoning = receipt.reasoning[0].artifact
    assert reasoning is not None
    existing_receipt = ArtifactIdentity(
        sha256=receipt_artifact.sha256,
        byte_length=receipt_artifact.byte_length,
        media_type="application/x-existing-role",
        relative_path=receipt_artifact.relative_path,
    )
    existing_reasoning = ArtifactIdentity(
        sha256=reasoning.sha256,
        byte_length=reasoning.byte_length,
        media_type="application/x-existing-role",
        relative_path=f"objects/{reasoning.sha256[:2]}/{reasoning.sha256}",
    )
    repo.register_artifact("r", existing_receipt)
    repo.register_artifact("r", existing_reasoning)
    verified = repo.prepare_evidence_commit(receipt)
    with ExecutorOwnership(repo, "r") as owner:
        assert owner.owner is not None
        repo.record_evidence(receipt, owner_token=owner.owner.token, prepared=verified)
        repo.record_evidence(receipt, owner_token=owner.owner.token, prepared=verified)
    assert repo.get_artifact("r", receipt_artifact.sha256) == existing_receipt
    assert repo.get_artifact("r", reasoning.sha256) == existing_reasoning
    assert repo.get_evidence_for_observation("r", "o") == receipt


def test_revision_preserves_existing_content_metadata(
    prepared: tuple[Repository, EvidenceReceipt],
) -> None:
    repo, receipt = prepared
    revision = UsageRevision(
        usage=receipt.usage.model_copy(update={"output_tokens": 12}),
        revision_id="cross-role-correction",
        revision_number=1,
        source="synthetic.authoritative",
    )
    artifact = publish_usage_revision(ArtifactStore(repo.run_directory("r")), revision)
    existing = ArtifactIdentity(
        sha256=artifact.sha256,
        byte_length=artifact.byte_length,
        media_type="application/x-existing-role",
        relative_path=artifact.relative_path,
    )
    repo.register_artifact("r", existing)
    with ExecutorOwnership(repo, "r") as owner:
        assert owner.owner is not None
        repo.record_evidence(receipt, owner_token=owner.owner.token)
        repo.append_usage_revision(revision, owner_token=owner.owner.token)
    assert repo.get_artifact("r", artifact.sha256) == existing
    assert repo.authoritative_usage("r", "s") == revision.usage
