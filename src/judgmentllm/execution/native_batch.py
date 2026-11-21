"""Owned native batch control: immutable intent, receipt-first recovery, no blind sends."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import AbstractContextManager
from pathlib import Path
from typing import TypeVar

from judgmentllm.batch_models import (
    BatchControlOptionsV1,
    BatchCursorV1,
    BatchDiagnosticV1,
    BatchMemberReceiptV1,
    BatchMemberV1,
    BatchOperationIntentV1,
    BatchOperationReceiptV1,
    BatchShardV1,
    BatchSourceCommitmentV1,
    BatchSourceIndexV1,
    BatchStatusEvidenceV1,
    BatchStopV1,
)
from judgmentllm.config import RunSpecification
from judgmentllm.evidence._base import EvidenceIdentity
from judgmentllm.evidence.usage import ProviderUsage
from judgmentllm.execution.processing import interpret
from judgmentllm.persistence.artifacts import ArtifactStore
from judgmentllm.persistence.hashing import (
    bytes_digest,
    canonical_bytes,
    semantic_digest,
)
from judgmentllm.persistence.models import (
    IntegrityError,
    ObservationRecord,
    SubmissionRecord,
)
from judgmentllm.persistence.native_batch import (
    fact,
    latest,
    put,
    read_reference,
    record_stop,
    register,
    require_storage,
    restore_stops,
    rows,
    stopped,
)
from judgmentllm.persistence.ownership import ExecutorOwnership
from judgmentllm.persistence.repository import Repository
from judgmentllm.preparation import _load_manifest, open_prepared_run  # pyright: ignore[reportPrivateUsage]
from judgmentllm.preparation.batch import encode_row, members_for, reconstruct
from judgmentllm.preparation.manifest import ExecutableRunManifestV12
from judgmentllm.providers.batch_protocol import (
    BatchEvidenceWithheld,
    NativeBatchAdapter,
    result_index_scope,
)

T = TypeVar("T")
Fault = Callable[[str, str], None]


class BudgetExhausted(Exception):
    """Invocation admission ended; accepted or ambiguous work remains recoverable."""


class InvocationBudget:
    def __init__(self, options: BatchControlOptionsV1):
        self.options = BatchControlOptionsV1.model_validate_json(
            options.model_dump_json()
        )
        self.deadline = asyncio.get_running_loop().time() + options.deadline_seconds
        self.steps = self.result_rows = self.bytes = 0
        self.closed = False

    def remaining(self) -> float:
        remaining = self.deadline - asyncio.get_running_loop().time()
        if self.closed or remaining <= 0 or self.steps >= self.options.max_io_steps:
            self.closed = True
            raise BudgetExhausted
        return remaining

    async def call(self, function: Callable[[], Awaitable[T]]) -> T:
        timeout = self.remaining()
        self.steps += 1
        task = asyncio.ensure_future(function())
        try:
            return await asyncio.wait_for(task, timeout)
        except (TimeoutError, asyncio.CancelledError):
            self.closed = True
            task.cancel()
            # Never detach a sender while releasing the lifetime run lock.
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
            if not task.cancelled():
                task.exception()
            raise

    def reconcile_step(self) -> None:
        self.remaining()
        self.steps += 1

    async def payload(self, raw: bytes) -> AsyncIterator[bytes]:
        for position in range(0, len(raw), 65_536):

            async def block(position: int = position) -> bytes:
                await asyncio.sleep(0)
                return raw[position : position + 65_536]

            yield await self.call(block)


class BatchSession(AbstractContextManager["BatchSession"]):
    def __init__(
        self,
        run_id: str,
        root: Path,
        adapter: NativeBatchAdapter,
        budget: InvocationBudget,
        fault: Fault | None = None,
    ):
        self.repository = Repository(root)
        self.ownership = ExecutorOwnership(self.repository, run_id)
        self.prepared = None
        self.run_id, self.root, self.budget, self.fault = run_id, root, budget, fault
        self.block_mutations = False
        self.blocked_logicals: set[str] = set()
        try:
            require_storage(self.repository)
            manifest = _load_manifest(self.repository, run_id, unsealed=False)
            if not isinstance(manifest, ExecutableRunManifestV12):
                raise IntegrityError("run is not a native batch preparation")
            owner = self.ownership.acquire()
            self.token = owner.token
            restore_stops(self.repository, run_id, self.token)
            self.manifest = manifest
            self.store = ArtifactStore(
                self.repository.run_directory(run_id), create=False
            )
            self.manifest_sha256 = self.repository.get_manifest(run_id).artifact.sha256
            self.adapter = adapter
            actual_context = self.adapter.context()
            if (
                actual_context.service_id,
                actual_context.protocol,
                actual_context.limits,
            ) != (
                manifest.batch_context.service_id,
                manifest.batch_context.protocol,
                manifest.batch_context.limits,
            ):
                raise IntegrityError(
                    "batch service, fixture or limits differ from preparation"
                )
        except BaseException:
            self.close()
            raise

    def __enter__(self) -> BatchSession:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        if self.prepared is not None:
            self.prepared.close()
        self.ownership.release()
        self.repository.close()

    def checkpoint(self, stage: str, identity: str = "") -> None:
        if self.fault is not None:
            self.fault(stage, identity)

    def open_sources(
        self, spec: RunSpecification | None, use_frozen_snapshot: bool
    ) -> None:
        self.prepared = open_prepared_run(
            self.run_id,
            store=self.root,
            spec=spec,
            capabilities=self.adapter.capabilities_for(
                self.manifest.generation.provider,
                self.manifest.generation.endpoint,
                self.manifest.generation.model,
            ).model_copy(update={"modes": ("batch_native",)}),
            owner_token=self.token,
            use_frozen_snapshot=use_frozen_snapshot,
        )

    def write_fact(
        self,
        kind: str,
        entity: str,
        model: BatchDiagnosticV1 | BatchStopV1 | BatchCursorV1 | BatchStatusEvidenceV1,
    ) -> None:
        fact(self.repository, self.run_id, self.token, kind, entity, model)

    def stop(self, *, remote: bool) -> None:
        record_stop(self.repository, self.run_id, self.token, remote=remote)
        self.checkpoint("after_batch_cancel_all" if remote else "after_batch_stop")

    @staticmethod
    def receipt_reference(instance_id: str) -> str:
        return f"batch/operations/{instance_id}.json"

    def operation_receipt(
        self, intent: BatchOperationIntentV1
    ) -> BatchOperationReceiptV1 | None:
        receipt = latest(
            self.repository,
            self.run_id,
            "operation",
            intent.instance_id,
            BatchOperationReceiptV1,
        )
        if receipt is None:
            receipt = read_reference(
                self.repository,
                self.run_id,
                self.receipt_reference(intent.instance_id),
                BatchOperationReceiptV1,
            )
        if receipt is not None:
            self.adapter.decode_receipt(canonical_bytes(receipt), intent)
        return receipt

    def accept_receipt(
        self, intent: BatchOperationIntentV1, receipt: BatchOperationReceiptV1
    ) -> None:
        receipt = self.adapter.decode_receipt(canonical_bytes(receipt), intent)
        if receipt.outcome == "unresolved":
            return
        if receipt.fence is not None and (
            receipt.fence.instance_id != intent.instance_id
            or receipt.fence.service_id != intent.service_id
            or receipt.fence.intent_sha256 != bytes_digest(canonical_bytes(intent))
            or receipt.fence.sender_token != intent.owner_token
        ):
            raise IntegrityError("nonacceptance fence differs from earlier sender")
        artifact = put(
            self.repository,
            self.run_id,
            receipt,
            reference=self.receipt_reference(intent.instance_id),
        )
        self.checkpoint("after_batch_operation_receipt", intent.instance_id)
        with self.repository.execution_transaction(self.run_id, self.token):
            register(self.repository, self.run_id, artifact)
            if (
                self.repository.connection.execute(
                    "SELECT 1 FROM batch_facts WHERE run_id=? AND kind='operation' AND entity_id=? AND receipt_sha256=?",
                    (self.run_id, intent.instance_id, artifact.sha256),
                ).fetchone()
                is None
            ):
                self.repository.connection.execute(
                    "INSERT INTO batch_facts(run_id,kind,entity_id,receipt_sha256,record_json) VALUES (?,'operation',?,?,?)",
                    (
                        self.run_id,
                        intent.instance_id,
                        artifact.sha256,
                        canonical_bytes(receipt).decode(),
                    ),
                )
            if intent.kind == "create":
                submission = self.repository.get_submission(
                    self.run_id, intent.instance_id
                )
                target = (
                    "acknowledged"
                    if receipt.outcome == "accepted"
                    else "confirmed_not_submitted"
                )
                if submission.state in ("submitting", "uncertain"):
                    self.repository.transition(
                        "submission",
                        self.run_id,
                        intent.instance_id,
                        target,
                        remote_id=receipt.remote_id,
                    )
                if receipt.outcome == "accepted":
                    self.repository.connection.execute(
                        "UPDATE batches SET remote_id=?,state='submitted' WHERE run_id=? AND batch_uid=? AND (remote_id IS NULL OR remote_id=?)",
                        (
                            receipt.remote_id,
                            self.run_id,
                            intent.shard_id,
                            receipt.remote_id,
                        ),
                    )
                    for member in members_for_manifest(
                        self.repository, self.run_id, intent.shard_id
                    ):
                        attempt = self.repository.get_attempt(
                            self.run_id, member.attempt_id
                        )
                        if attempt.state == "prepared":
                            for state in ("claimed", "submitting", "acknowledged"):
                                self.repository.transition(
                                    "attempt", self.run_id, member.attempt_id, state
                                )
                            request = self.repository.get_request(
                                self.run_id, member.request_uid
                            )
                            if request.state != "active":
                                self.repository.transition(
                                    "request",
                                    self.run_id,
                                    member.request_uid,
                                    "active",
                                    authorization_id=attempt.authorization_id,
                                )
                            for task in self.repository.attempt_task_ids(
                                self.run_id, member.attempt_id
                            ):
                                if (
                                    self.repository.get_task(self.run_id, task).state
                                    != "active"
                                ):
                                    self.repository.transition(
                                        "task",
                                        self.run_id,
                                        task,
                                        "active",
                                        authorization_id=attempt.authorization_id,
                                    )
        self.checkpoint("after_batch_operation_commit", intent.instance_id)

    async def recover_operations(self) -> None:
        for row in self.repository.connection.execute(
            "SELECT record_json,intent_sha256 FROM batch_operations WHERE run_id=? ORDER BY rowid",
            (self.run_id,),
        ):
            self.budget.remaining()
            intent = BatchOperationIntentV1.model_validate_json(row[0])
            if bytes_digest(canonical_bytes(intent)) != row[1]:
                raise IntegrityError("batch operation differs from its durable intent")
            self.store.verify(self.repository.get_artifact(self.run_id, row[1]))
            receipt = self.operation_receipt(intent)
            if receipt is not None:
                if self.repository.connection.execute(
                    "SELECT 1 FROM batch_facts WHERE run_id=? AND kind='operation' AND entity_id=?",
                    (self.run_id, intent.instance_id),
                ).fetchone():
                    continue
                self.budget.reconcile_step()
                self.accept_receipt(intent, receipt)
                if receipt.outcome != "accepted":
                    self.blocked_logicals.add(intent.logical_id)
                continue
            raw = await self.budget.call(
                lambda intent=intent: self.adapter.reconcile_operation(intent)
            )
            if raw is None:
                self.block_mutations = True
                continue
            receipt = self.adapter.decode_receipt(raw, intent)
            self.accept_receipt(intent, receipt)
            # Newly learned nonacceptance is not an automatic successor grant.
            if receipt.outcome != "accepted":
                self.block_mutations = True
                self.blocked_logicals.add(intent.logical_id)

    def forward_guard(self, intent: BatchOperationIntentV1) -> None:
        self.budget.remaining()
        self.repository.require_owner(self.run_id, self.token)
        if intent.owner_token != self.token or (
            intent.kind != "cancel" and stopped(self.repository, self.run_id)
        ):
            raise IntegrityError("batch sender is stopped or stale")
        self.checkpoint("before_batch_forward", intent.instance_id)

    async def mutate(
        self,
        shard: BatchShardV1,
        kind: str,
        payload: bytes,
        remote_id: str | None = None,
    ) -> BatchOperationReceiptV1 | None:
        if (self.block_mutations and kind != "cancel") or (
            kind != "cancel" and stopped(self.repository, self.run_id)
        ):
            return None
        self.budget.remaining()
        logical = semantic_digest(
            "batch-logical-operation-v1", [self.run_id, shard.shard_id, kind, remote_id]
        )
        if logical in self.blocked_logicals:
            return None
        previous = self.repository.connection.execute(
            "SELECT record_json FROM batch_operations WHERE run_id=? AND logical_id=? ORDER BY sequence DESC LIMIT 1",
            (self.run_id, logical),
        ).fetchone()
        predecessor = None
        sequence = 1
        if previous is not None:
            prior = BatchOperationIntentV1.model_validate_json(previous[0])
            receipt = self.operation_receipt(prior)
            if receipt is None or receipt.outcome == "unresolved":
                self.block_mutations = True
                return None
            if receipt.outcome == "accepted":
                return receipt
            if prior.sequence >= 3:
                return None
            # The independent service reasserts the exact durable fence.
            proof = await self.budget.call(
                lambda: self.adapter.reconcile_operation(prior)
            )
            if proof is None or self.adapter.decode_receipt(proof, prior) != receipt:
                self.block_mutations = True
                return None
            predecessor, sequence = (
                bytes_digest(canonical_bytes(receipt)),
                prior.sequence + 1,
            )
        instance = semantic_digest("batch-operation-instance-v1", [logical, sequence])
        intent = BatchOperationIntentV1(
            run_id=self.run_id,
            shard_id=shard.shard_id,
            logical_id=logical,
            instance_id=instance,
            sequence=sequence,
            kind=kind,  # type: ignore[arg-type]
            service_id=self.adapter.identity,
            manifest_sha256=self.manifest_sha256,
            payload_sha256=bytes_digest(payload),
            owner_token=self.token,
            remote_id=remote_id,
            predecessor_sha256=predecessor,
        )
        # Only SQL registration makes this immutable artifact a sender journal.
        artifact = put(self.repository, self.run_id, intent)
        body = (
            self.store.put(payload, "application/json")
            if self.manifest.capture == "full"
            else self.store.hash_only(
                intent.payload_sha256, len(payload), "application/json"
            )
        )
        with self.repository.execution_transaction(self.run_id, self.token):
            register(self.repository, self.run_id, artifact)
            register(self.repository, self.run_id, body)
            self.repository.connection.execute(
                "INSERT INTO batch_operations VALUES (?,?,?,?,?,?,?,?)",
                (
                    self.run_id,
                    logical,
                    instance,
                    sequence,
                    kind,
                    shard.shard_id,
                    artifact.sha256,
                    canonical_bytes(intent).decode(),
                ),
            )
            if kind == "create":
                record = SubmissionRecord(
                    run_id=self.run_id,
                    submission_id=instance,
                    correlation_id=instance,
                    payload_sha256=intent.payload_sha256,
                    batch_uid=shard.shard_id,
                )
                self.repository._insert("submissions", record)  # pyright: ignore[reportPrivateUsage]
                self.repository.transition(
                    "submission", self.run_id, instance, "submitting"
                )
        self.checkpoint("after_batch_intent", instance)
        try:
            raw = await self.budget.call(
                lambda: self.adapter.invoke(
                    intent,
                    self.budget.payload(payload),
                    lambda: self.forward_guard(intent),
                )
            )
            self.checkpoint("after_batch_forward", instance)
            receipt = self.adapter.decode_receipt(raw, intent)
        except (TimeoutError, OSError):
            self.block_mutations = True
            return None
        self.accept_receipt(intent, receipt)
        if receipt.outcome != "accepted":
            self.block_mutations = True
        return receipt

    async def submit(self) -> None:
        if stopped(self.repository, self.run_id):
            return
        assert self.prepared is not None
        for shard in rows(
            self.repository, "batch_preparations", self.run_id, BatchShardV1
        ):
            accepted = self.repository.connection.execute(
                "SELECT 1 FROM submissions WHERE run_id=? AND batch_uid=? AND certainty='accepted'",
                (self.run_id, shard.shard_id),
            ).fetchone()
            if accepted:
                continue
            if shard.context != self.adapter.context():
                raise IntegrityError("unfinished shard requires its original fixture")
            data = bytearray()
            for member in members_for(self.prepared, shard.shard_id):
                _, wire = reconstruct(self.prepared, self.adapter, member)
                data.extend(encode_row(member.member_key, wire))
            if (bytes_digest(bytes(data)), len(data)) != (
                shard.input.sha256,
                shard.input.byte_length,
            ):
                raise IntegrityError("batch shard bytes differ from seal")
            uploaded = await self.mutate(shard, "upload", bytes(data))
            if uploaded is None or uploaded.outcome != "accepted":
                return
            created = await self.mutate(
                shard,
                "create",
                canonical_bytes([self.manifest_sha256, shard, uploaded]),
                uploaded.remote_id,
            )
            if created is None or created.outcome != "accepted":
                return

    async def cancel_known(self) -> None:
        if not stopped(self.repository, self.run_id, "cancel_all"):
            return
        for shard in rows(
            self.repository, "batch_preparations", self.run_id, BatchShardV1
        ):
            submission = self.accepted_submission(shard.shard_id)
            if submission is None:
                continue
            status = latest(
                self.repository,
                self.run_id,
                "status",
                submission.submission_id,
                BatchStatusEvidenceV1,
            )
            if status is not None and status.state in (
                "completed",
                "failed",
                "cancelled",
            ):
                continue
            await self.mutate(
                shard,
                "cancel",
                canonical_bytes(
                    [
                        self.manifest_sha256,
                        submission.submission_id,
                        submission.remote_id,
                    ]
                ),
                submission.remote_id,
            )

    def accepted_submission(self, shard_id: str) -> SubmissionRecord | None:
        row = self.repository.connection.execute(
            "SELECT submission_id FROM submissions WHERE run_id=? AND batch_uid=? AND certainty='accepted'",
            (self.run_id, shard_id),
        ).fetchone()
        return self.repository.get_submission(self.run_id, row[0]) if row else None

    async def poll(self) -> None:
        for poll_number in range(self.budget.options.max_polls):
            for shard in rows(
                self.repository, "batch_preparations", self.run_id, BatchShardV1
            ):
                submission = self.accepted_submission(shard.shard_id)
                if submission is None:
                    continue
                assert submission.remote_id is not None
                remote_id = submission.remote_id
                try:
                    status = await self.budget.call(
                        lambda submission=submission, remote_id=remote_id: (
                            self.adapter.status(submission.submission_id, remote_id)
                        )
                    )
                except OSError:
                    raise
                status = BatchStatusEvidenceV1.model_validate_json(
                    canonical_bytes(status)
                )
                if (status.submission_id, status.remote_id) != (
                    submission.submission_id,
                    remote_id,
                ):
                    self.write_fact(
                        "withheld",
                        submission.submission_id,
                        BatchDiagnosticV1(code="unsafe_evidence"),
                    )
                    continue
                previous = latest(
                    self.repository,
                    self.run_id,
                    "status",
                    submission.submission_id,
                    BatchStatusEvidenceV1,
                )
                if (
                    previous is not None
                    and previous.state in ("completed", "failed", "cancelled")
                    and status.state != previous.state
                ):
                    self.write_fact(
                        "conflict",
                        submission.submission_id,
                        BatchDiagnosticV1(code="conflicting_terminal"),
                    )
                    continue
                self.write_fact("status", submission.submission_id, status)
            if poll_number + 1 < self.budget.options.max_polls:
                await self.budget.call(
                    lambda: asyncio.sleep(self.budget.options.poll_interval_seconds)
                )

    def save_member(
        self,
        member: BatchMemberV1,
        submission: SubmissionRecord,
        source_id: str,
        position: int,
        raw: bytes,
    ) -> str:
        assert self.prepared is not None
        dependency, wire = reconstruct(self.prepared, self.adapter, member)
        screened = self.adapter.screen_result(raw, {member.member_key: wire})
        if screened is None:
            self.write_fact(
                "withheld",
                source_id + ":" + str(position),
                BatchDiagnosticV1(code="unsafe_evidence"),
            )
            return semantic_digest("batch-withheld-v1", [source_id, position])
        _, response, reported, safe, exact = screened
        expected_key = semantic_digest(
            "batch-observation-v1",
            [member.member_key, "error" if response.kind != "response" else "output"],
        )
        if response.observation_key != expected_key:
            raise IntegrityError(
                "batch observation key differs from sealed attribution"
            )
        observation_id = semantic_digest(
            "batch-observation-id-v1", [submission.submission_id, member.member_key]
        )
        identity = EvidenceIdentity(
            run_id=self.run_id,
            request_uid=member.request_uid,
            attempt_id=member.attempt_id,
            submission_id=submission.submission_id,
            observation_id=observation_id,
        )
        usage = ProviderUsage.model_validate(
            {
                **identity.model_dump(exclude={"schema_version"}),
                **reported.model_dump(exclude={"schema_version"}),
                "provider": self.manifest.generation.provider,
                "endpoint": self.manifest.generation.endpoint,
                "requested_model": self.manifest.generation.model,
                "observation_kind": response.kind,
                "invocation_kind": "native_batch",
            }
        )
        prior = read_reference(
            self.repository,
            self.run_id,
            f"batch/members/{member.member_key}.json",
            BatchMemberReceiptV1,
        )
        safe_sha = bytes_digest(safe)
        if prior is not None:
            if (
                prior.retained.sha256 != safe_sha
                or prior.usage != usage
                or prior.identity != identity
            ):
                self.write_fact(
                    "conflict",
                    member.member_key,
                    BatchDiagnosticV1(code="conflicting_member"),
                )
                return safe_sha
            receipt = prior
        else:
            retained = (
                self.store.put(safe, "application/json")
                if self.manifest.capture == "full"
                else self.store.hash_only(safe_sha, len(safe), "application/json")
            )
            receipt = BatchMemberReceiptV1(
                identity=identity,
                member_key=member.member_key,
                source_id=source_id,
                source_position=position,
                observation_key=response.observation_key,
                kind=response.kind,
                capture=self.manifest.capture,
                original=self.store.hash_only(
                    bytes_digest(raw), len(raw), "application/json"
                )
                if exact
                else None,
                retained=retained,
                disposition="exact" if exact else "redacted",
                usage=usage,
            )
        artifact = put(
            self.repository,
            self.run_id,
            receipt,
            reference=f"batch/members/{member.member_key}.json",
        )
        self.checkpoint("after_batch_member_receipt", member.member_key)
        with self.repository.execution_transaction(self.run_id, self.token):
            register(self.repository, self.run_id, receipt.retained)
            register(self.repository, self.run_id, artifact)
            existing = self.repository.connection.execute(
                "SELECT receipt_sha256 FROM batch_member_receipts WHERE run_id=? AND attempt_id=?",
                (self.run_id, member.attempt_id),
            ).fetchone()
            if existing is not None and existing[0] != artifact.sha256:
                raise IntegrityError("member receipt differs from its durable bridge")
            if existing is None:
                self.repository.connection.execute(
                    "INSERT INTO batch_member_receipts VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        self.run_id,
                        submission.submission_id,
                        member.shard_id,
                        member.member_key,
                        member.attempt_id,
                        observation_id,
                        receipt.observation_key,
                        artifact.sha256,
                        canonical_bytes(receipt).decode(),
                    ),
                )
                self.repository.record_observation(
                    ObservationRecord(
                        run_id=self.run_id,
                        observation_id=observation_id,
                        submission_id=submission.submission_id,
                        attempt_id=member.attempt_id,
                        observation_key=receipt.observation_key,
                        kind=receipt.kind,
                        raw_sha256=receipt.retained.sha256,
                    )
                )
                self.repository.transition(
                    "attempt", self.run_id, member.attempt_id, "response_saved"
                )
        self.checkpoint("after_batch_member_observation", member.member_key)
        interpreted = interpret(
            self.adapter.parse(response, strict_output=True),
            self.prepared.spec,
            dependency,
            self.run_id,
            member.attempt_id,
            contract=wire.output_commitment.contract,
        )
        results = tuple(
            result.model_copy(
                update={
                    "mode": "batch_native",
                    "manifest_sha256": self.manifest_sha256,
                    "prompt_path": member.prompt.relative_path,
                    "raw_response_path": receipt.retained.relative_path,
                }
            )
            for result in interpreted.results
        )
        with self.repository.execution_transaction(self.run_id, self.token):
            self.repository.commit_results(
                self.run_id,
                member.attempt_id,
                results,
                observation_id=observation_id,
                checkpoint=lambda: self.checkpoint(
                    "during_batch_results", member.member_key
                ),
            )
        self.checkpoint("after_batch_member_results", member.member_key)
        return safe_sha

    def result_cursor(
        self, shard: BatchShardV1, submission: SubmissionRecord, source: str
    ) -> tuple[str, BatchCursorV1 | None]:
        source_id = semantic_digest(
            "batch-result-source-v1",
            [submission.submission_id, source, shard.context.fixture_sha256],
        )
        cursor = latest(
            self.repository, self.run_id, "cursor", source_id, BatchCursorV1
        )
        if cursor is not None and (
            cursor.run_id,
            cursor.submission_id,
            cursor.source_id,
        ) != (self.run_id, submission.submission_id, source_id):
            raise IntegrityError("batch cursor differs from its accepted source")
        return source_id, cursor

    async def collect(self) -> None:
        assert self.prepared is not None
        for shard in rows(
            self.repository, "batch_preparations", self.run_id, BatchShardV1
        ):
            submission = self.accepted_submission(shard.shard_id)
            if submission is None:
                continue
            members = list(members_for(self.prepared, shard.shard_id))
            complete = all(
                self.repository.get_results_for_attempt(self.run_id, m.attempt_id)
                for m in members
            )
            if complete:
                for member in members:
                    self.budget.remaining()
                    prior = read_reference(
                        self.repository,
                        self.run_id,
                        f"batch/members/{member.member_key}.json",
                        BatchMemberReceiptV1,
                    )
                    if prior is None or (
                        prior.identity.attempt_id,
                        prior.identity.submission_id,
                        prior.member_key,
                    ) != (
                        member.attempt_id,
                        submission.submission_id,
                        member.member_key,
                    ):
                        raise IntegrityError(
                            "completed batch member lacks matching evidence"
                        )
                    self.store.verify(prior.retained)
                source_cursors = (
                    self.result_cursor(shard, submission, source)[1]
                    for source in ("output", "error")
                )
                if all(
                    cursor is not None and cursor.exhausted for cursor in source_cursors
                ):
                    continue
            if shard.context != self.adapter.context():
                raise IntegrityError("unfinished shard requires its original fixture")
            by_key = {m.member_key: m for m in members}
            compiled = [
                (
                    m.member_key,
                    m.position,
                    reconstruct(self.prepared, self.adapter, m)[1],
                )
                for m in members
            ]
            expected_wires = {key: wire for key, _, wire in compiled}
            # Full receipts recover without any service read or new invocation.
            for member in members:
                self.budget.remaining()
                prior = read_reference(
                    self.repository,
                    self.run_id,
                    f"batch/members/{member.member_key}.json",
                    BatchMemberReceiptV1,
                )
                if prior is not None:
                    self.store.verify(prior.retained)
                    if (
                        prior.retained.relative_path is not None
                        and not self.repository.get_results_for_attempt(
                            self.run_id, member.attempt_id
                        )
                    ):
                        self.budget.reconcile_step()
                        self.save_member(
                            member,
                            submission,
                            prior.source_id,
                            prior.source_position,
                            self.store.read(prior.retained),
                        )
            for source in ("output", "error"):
                source_id, cursor = self.result_cursor(shard, submission, source)
                if cursor is not None and cursor.exhausted:
                    continue
                assert submission.remote_id is not None
                remote_id = submission.remote_id
                try:
                    returned_source = await self.budget.call(
                        lambda submission=submission, source=source: (
                            self.adapter.result_source(submission.submission_id, source)
                        )
                    )
                    if returned_source != source_id:
                        raise BatchEvidenceWithheld(
                            "result source differs from immutable scope"
                        )
                    commitment, raw_index = await self.budget.call(
                        lambda submission=submission, remote_id=remote_id, source=source, compiled=compiled: (
                            self.adapter.result_index(
                                submission.submission_id,
                                remote_id,
                                source,
                                compiled,
                            )
                        )
                    )
                    commitment = BatchSourceCommitmentV1.model_validate(commitment)
                    self.budget.bytes += len(raw_index) + len(
                        canonical_bytes(commitment)
                    )
                    if self.budget.bytes > (
                        self.budget.options.max_result_rows
                        * self.manifest.batch_context.limits.max_record_bytes
                    ):
                        raise BudgetExhausted
                    if (
                        len(raw_index)
                        > self.manifest.batch_context.limits.max_record_bytes
                    ):
                        raise IntegrityError("result index exceeds its metadata bound")
                    index = BatchSourceIndexV1.model_validate_json(raw_index)
                    scope = result_index_scope(
                        shard.context.service_id,
                        submission.submission_id,
                        submission.remote_id,
                        source_id,
                        compiled,
                    )
                    # Only typed, scoped metadata may be hashed; the root is service-owned.
                    if (
                        commitment.source_id != source_id
                        or commitment.scope_sha256 != scope
                        or index.scope_sha256 != scope
                        or canonical_bytes(index) != raw_index
                        or bytes_digest(raw_index) != commitment.index_sha256
                        or (
                            cursor is not None
                            and cursor.index_sha256 is not None
                            and cursor.index_sha256 != commitment.index_sha256
                        )
                    ):
                        raise IntegrityError(
                            "batch result index differs from original commitment"
                        )
                except BatchEvidenceWithheld:
                    self.write_fact(
                        "withheld", source_id, BatchDiagnosticV1(code="unsafe_evidence")
                    )
                    continue
                prefix = bytes_digest(b"judgmentllm.batch-result-prefix.v1\n")
                position = cursor.position if cursor is not None else 0
                if index.prefix_sha256[0] != prefix or (
                    position >= len(index.prefix_sha256)
                    or (
                        cursor is not None
                        and cursor.prefix_sha256 != index.prefix_sha256[position]
                    )
                ):
                    raise IntegrityError("batch result prefix differs from its cursor")
                prefix = index.prefix_sha256[position]
                verified_cursor = BatchCursorV1(
                    run_id=self.run_id,
                    submission_id=submission.submission_id,
                    source_id=source_id,
                    position=position,
                    prefix_sha256=prefix,
                    index_sha256=commitment.index_sha256,
                )
                buffer = bytearray()
                stream = self.adapter.results(
                    submission.submission_id,
                    submission.remote_id,
                    source,
                    compiled,
                    verified_cursor,
                )
                try:
                    while True:
                        try:
                            chunk = await self.budget.call(
                                lambda stream=stream: anext(stream)
                            )
                        except StopAsyncIteration:
                            if buffer:
                                raise IntegrityError(
                                    "batch result source ended within a record"
                                ) from None
                            if position != len(index.prefix_sha256) - 1:
                                raise IntegrityError(
                                    "batch source EOF differs from its immutable index"
                                ) from None
                            self.write_fact(
                                "cursor",
                                source_id,
                                BatchCursorV1(
                                    run_id=self.run_id,
                                    submission_id=submission.submission_id,
                                    source_id=source_id,
                                    position=position,
                                    prefix_sha256=prefix,
                                    index_sha256=commitment.index_sha256,
                                    exhausted=True,
                                ),
                            )
                            break
                        self.budget.bytes += len(chunk)
                        if len(chunk) > 65_536:
                            raise IntegrityError(
                                "batch result chunk exceeds the protocol bound"
                            )
                        maximum = (
                            self.budget.options.max_result_rows
                            * self.manifest.batch_context.limits.max_record_bytes
                        )
                        if self.budget.bytes > maximum:
                            raise BudgetExhausted
                        buffer.extend(chunk)
                        if (
                            len(buffer)
                            > self.manifest.batch_context.limits.max_record_bytes
                            and b"\n" not in buffer
                        ):
                            raise IntegrityError(
                                "batch result record exceeds its sealed limit"
                            )
                        while b"\n" in buffer:
                            self.budget.remaining()
                            if (
                                self.budget.result_rows
                                >= self.budget.options.max_result_rows
                            ):
                                raise BudgetExhausted
                            self.budget.result_rows += 1
                            raw, _, rest = buffer.partition(b"\n")
                            buffer = bytearray(rest)
                            if (
                                len(raw)
                                > self.manifest.batch_context.limits.max_record_bytes
                            ):
                                raise IntegrityError(
                                    "batch result record exceeds its sealed limit"
                                )
                            screened = self.adapter.screen_result(
                                bytes(raw), expected_wires
                            )
                            if screened is None:
                                digest = semantic_digest(
                                    "batch-withheld-v1", [source_id, position]
                                )
                            else:
                                digest = bytes_digest(screened[3])
                            next_prefix = semantic_digest(
                                "batch-result-prefix-step-v1",
                                [prefix, position, digest],
                            )
                            if (
                                position + 1 >= len(index.prefix_sha256)
                                or next_prefix != index.prefix_sha256[position + 1]
                            ):
                                if (
                                    screened is not None
                                    and read_reference(
                                        self.repository,
                                        self.run_id,
                                        f"batch/members/{screened[0]}.json",
                                        BatchMemberReceiptV1,
                                    )
                                    is not None
                                ):
                                    self.write_fact(
                                        "conflict",
                                        screened[0],
                                        BatchDiagnosticV1(code="conflicting_member"),
                                    )
                                    return
                                raise IntegrityError(
                                    "batch result row differs from immutable index"
                                )
                            if screened is None:
                                self.write_fact(
                                    "withheld",
                                    source_id + ":" + str(position),
                                    BatchDiagnosticV1(code="unsafe_evidence"),
                                )
                            else:
                                self.save_member(
                                    by_key[screened[0]],
                                    submission,
                                    source_id,
                                    position,
                                    bytes(raw),
                                )
                            prefix = next_prefix
                            position += 1
                            self.write_fact(
                                "cursor",
                                source_id,
                                BatchCursorV1(
                                    run_id=self.run_id,
                                    submission_id=submission.submission_id,
                                    source_id=source_id,
                                    position=position,
                                    prefix_sha256=prefix,
                                    index_sha256=commitment.index_sha256,
                                ),
                            )
                            self.checkpoint("after_batch_cursor", source_id)
                            if (
                                self.budget.result_rows
                                >= self.budget.options.max_result_rows
                            ):
                                raise BudgetExhausted
                finally:
                    await stream.aclose()

    def finish(self) -> None:
        if stopped(self.repository, self.run_id):
            return
        summary = self.repository.summary(self.run_id)
        if summary.results:
            target = (
                "completed"
                if summary.logical_completed_tasks == summary.tasks
                else "partial"
            )
            with self.repository.execution_transaction(self.run_id, self.token):
                state = self.repository.get_run(self.run_id).state
                if state in ("submitted", "partial", "failed"):
                    self.repository.transition(
                        "run", self.run_id, self.run_id, "running"
                    )
                    state = "running"
                if state == "running":
                    self.repository.transition("run", self.run_id, self.run_id, target)


def members_for_manifest(
    repository: Repository, run_id: str, shard_id: str
) -> Iterator[BatchMemberV1]:
    for row in repository.connection.execute(
        "SELECT record_json FROM batch_prepared_members WHERE run_id=? AND shard_id=? ORDER BY position",
        (run_id, shard_id),
    ):
        yield BatchMemberV1.model_validate_json(row[0])
