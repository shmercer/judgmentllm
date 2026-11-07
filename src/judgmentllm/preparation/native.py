"""Bounded native preparation layered on existing requests, attempts and artifacts."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from hashlib import sha256
from typing import TYPE_CHECKING, Literal

from judgmentllm.persistence.artifacts import ArtifactStore
from judgmentllm.persistence.hashing import (
    bytes_digest,
    canonical_bytes,
    semantic_digest,
)
from judgmentllm.persistence.models import (
    ArtifactIdentity,
    AttemptRecord,
    IntegrityError,
    RequestRecord,
    TaskRecord,
)
from judgmentllm.persistence.transport_receipts import (
    iter_transport_commitments,
    publish_transport_commitment,
)
from judgmentllm.planning import plan_record, stable_id
from judgmentllm.preparation.freezing import PreparationError

if TYPE_CHECKING:
    from judgmentllm.config import RunSpecV3
    from judgmentllm.persistence.repository import Repository
    from judgmentllm.preparation.capture import SourceBuilder
    from judgmentllm.preparation.manifest import ExecutableRunManifestV3
    from judgmentllm.preparation.session import PreparedRun
    from judgmentllm.transport import PreparedRequestV3, TransportContext


class InitialTransportDigest:
    """Ordered streaming digest; no request contents or growing collection."""

    def __init__(self) -> None:
        self.count = 0
        self._digest = sha256(b"judgmentllm.initial-transports.v1\n")

    def add(
        self,
        ordinal: int,
        request: RequestRecord,
        attempt: AttemptRecord,
        receipt_sha256: str,
    ) -> None:
        if ordinal != self.count or request.scheduling_ordinal != ordinal:
            raise IntegrityError("initial transport order is inconsistent")
        self._digest.update(
            canonical_bytes(
                [
                    ordinal,
                    request.request_uid,
                    request.dependency_sha256,
                    attempt.attempt_id,
                    attempt.membership_sha256,
                    attempt.prompt_sha256,
                    attempt.payload_sha256,
                    receipt_sha256,
                ]
            )
            + b"\n"
        )
        self.count += 1

    def hexdigest(self) -> str:
        return self._digest.hexdigest()


def publish_prepared_transport(
    repository: Repository,
    store: ArtifactStore,
    wire: PreparedRequestV3,
    capture: Literal["full", "minimal"],
) -> tuple[ArtifactIdentity, ...]:
    """Publish bytes before a transaction; caller registers returned identities.

    Minimal capture hashes transient bytes directly, never staging those bytes.
    The safe receipt is always retained and never replaces an earlier receipt.
    """
    identities: list[ArtifactIdentity] = []
    for name, content in wire.content_artifacts().items():
        media = "text/plain" if name == "prompt" else "application/json"
        identity = (
            store.put(content, media)
            if capture == "full"
            else store.hash_only(bytes_digest(content), len(content), media)
        )
        identities.append(identity)
    identities.append(publish_transport_commitment(store, wire.transport_commitment))
    return tuple(identities)


def _input_ids(
    repository: Repository, run_id: str, kind: Literal["response", "pair"]
) -> Iterator[str]:
    after = -1
    while True:
        rows = repository.connection.execute(
            "SELECT record_id,ordinal FROM input_records WHERE run_id=? AND record_kind=? AND ordinal>? ORDER BY ordinal LIMIT 128",
            (run_id, kind, after),
        ).fetchall()
        if not rows:
            return
        for row in rows:
            yield str(row["record_id"])
        after = int(rows[-1]["ordinal"])


def preseal_transports(
    builder: SourceBuilder,
    spec: RunSpecV3,
    template: str | None,
    manifest: ExecutableRunManifestV3,
    context: TransportContext,
    checkpoint: Callable[[str, str | None], None],
) -> tuple[int, str]:
    """Prepare first attempts while created; submission remains impossible."""
    from judgmentllm.preparation.session import _request_dependency  # pyright: ignore[reportPrivateUsage]
    from judgmentllm.transport import physical_identity, prepare_transport_request

    repository = builder.repository
    digest = InitialTransportDigest()
    task_ordinal = 0
    kind = "response" if spec.task_kind == "rubric" else "pair"
    for record_id in _input_ids(repository, manifest.run_id, kind):
        record = builder.lookup(kind, record_id)
        for planned in plan_record(
            spec, record, template_text=template, capabilities=manifest.capabilities
        ):
            pair = planned.pair
            left = builder.lookup("response", pair.left_item_id) if pair else None
            right = builder.lookup("response", pair.right_item_id) if pair else None
            from judgmentllm.models import ResponseItem

            if left is not None and not isinstance(left, ResponseItem):
                raise PreparationError("invalid pair dependency")
            if right is not None and not isinstance(right, ResponseItem):
                raise PreparationError("invalid pair dependency")
            dependency = _request_dependency(manifest, planned, record, left, right)
            attempt_id = stable_id("attempt", manifest.run_id, planned.request_uid, 1)
            identity = physical_identity(
                manifest.run_id, planned.request_uid, attempt_id
            )
            checkpoint("before_transport_preparation", planned.request_uid)
            wire = prepare_transport_request(
                spec,
                dependency,
                identity,
                context,
                template=template,
                lookup=builder.lookup,
            )
            artifacts = publish_prepared_transport(
                repository, builder.store, wire, spec.capture
            )
            checkpoint("after_transport_artifacts", planned.request_uid)
            request = RequestRecord(
                run_id=manifest.run_id,
                request_uid=planned.request_uid,
                scheduling_ordinal=digest.count,
                dependency_sha256=dependency.dependency_sha256,
            )
            attempt = AttemptRecord(
                run_id=manifest.run_id,
                attempt_id=attempt_id,
                request_uid=planned.request_uid,
                attempt_number=1,
                prompt_sha256=wire.prompt_sha256,
                payload_sha256=wire.payload_sha256,
                membership_sha256=semantic_digest(
                    "attempt-membership", list(wire.task_ids)
                ),
            )
            with repository.execution_transaction(manifest.run_id, builder.owner_token):
                for artifact in artifacts:
                    repository._register_artifact(manifest.run_id, artifact)  # pyright: ignore[reportPrivateUsage]
                for task in planned.tasks:
                    repository.create_task(
                        TaskRecord(
                            run_id=manifest.run_id,
                            scheduling_ordinal=task_ordinal,
                            **task.model_dump(exclude={"schema_version"}),
                        )
                    )
                    task_ordinal += 1
                repository.create_request(request, wire.task_ids)
                repository.create_attempt(attempt, wire.task_ids)
                repository.transition(
                    "request", manifest.run_id, planned.request_uid, "prepared"
                )
                repository.advance_execution_checkpoint(
                    manifest.run_id, digest.count + 1, owner_token=builder.owner_token
                )
            digest.add(
                digest.count,
                request,
                attempt,
                bytes_digest(canonical_bytes(wire.transport_commitment)),
            )
            checkpoint("after_transport_preparation", planned.request_uid)
    return digest.count, digest.hexdigest()


def verify_initial_transports(prepared: PreparedRun) -> None:
    """Prove plan/request/first-attempt/receipt bijection before any execution."""
    from judgmentllm.preparation.manifest import ExecutableRunManifestV3
    from judgmentllm.transport import physical_identity

    manifest = prepared.manifest
    if not isinstance(manifest, ExecutableRunManifestV3):
        return
    repository = prepared.repository
    run_id = manifest.run_id
    digest = InitialTransportDigest()
    task_ordinal = 0
    for dependency in prepared.iter_request_dependencies():
        request_uid = dependency.request.request_uid
        request = repository.get_request(run_id, request_uid)
        attempt_id = stable_id("attempt", run_id, request_uid, 1)
        attempt = repository.get_attempt(run_id, attempt_id)
        members = tuple(task.task_id for task in dependency.request.tasks)
        if (
            request.dependency_sha256 != dependency.dependency_sha256
            or repository.request_task_ids(run_id, request_uid) != members
            or repository.attempt_task_ids(run_id, attempt_id) != members
            or attempt.request_uid != request_uid
            or attempt.attempt_number != 1
            or attempt.authorization_id is not None
            or attempt.membership_sha256
            != semantic_digest("attempt-membership", list(members))
        ):
            raise IntegrityError("initial transport membership is inconsistent")
        for task in dependency.request.tasks:
            persisted = repository.get_task(run_id, task.task_id)
            if persisted.scheduling_ordinal != task_ordinal or persisted.model_dump(
                exclude={"schema_version", "run_id", "scheduling_ordinal", "state"}
            ) != task.model_dump(exclude={"schema_version"}):
                raise IntegrityError("initial task identity is inconsistent")
            task_ordinal += 1
        receipt = prepared.transport_for_attempt(attempt_id)
        if receipt.parent_commitment_sha256 is not None:
            raise IntegrityError("initial transport cannot inherit another attempt")
        wire = prepared.prepare_transport_attempt(
            dependency,
            physical_identity(run_id, request_uid, attempt_id),
            frozen=receipt,
        )
        if (
            wire.prompt_sha256 != attempt.prompt_sha256
            or wire.payload_sha256 != attempt.payload_sha256
        ):
            raise IntegrityError("reconstructed transport differs from initial attempt")
        # Frozen preparation already verified every content artifact and its
        # capture identity; do not reopen the same objects a second time here.
        digest.add(
            digest.count, request, attempt, bytes_digest(canonical_bytes(receipt))
        )
    receipt_count = 0
    for receipt in iter_transport_commitments(prepared.store):
        if receipt.parent_commitment_sha256 is None:
            first = repository.get_attempt(run_id, receipt.identity.attempt_id)
            if (
                first.attempt_number != 1
                or first.request_uid != receipt.identity.request_uid
            ):
                raise IntegrityError("initial receipt has no matching original attempt")
            receipt_count += 1
    counts = repository.connection.execute(
        "SELECT (SELECT COUNT(*) FROM requests WHERE run_id=?),"
        "(SELECT COUNT(*) FROM attempts WHERE run_id=? AND attempt_number=1),"
        "(SELECT COUNT(*) FROM tasks WHERE run_id=?)",
        (run_id, run_id, run_id),
    ).fetchone()
    if (
        receipt_count != digest.count
        or counts is None
        or tuple(counts) != (digest.count, digest.count, task_ordinal)
        or digest.count != manifest.initial_transport_count
        or digest.hexdigest() != manifest.initial_transport_sha256
        or repository.get_execution_checkpoint(run_id) != digest.count
    ):
        raise IntegrityError(
            "initial transport index differs from the immutable manifest"
        )
