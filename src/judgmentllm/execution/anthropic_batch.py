"""Owned collection and recovery for an accepted Anthropic Message Batch."""

from __future__ import annotations

import asyncio
from collections import Counter
from pathlib import Path
from typing import Any, cast

from judgmentllm.anthropic_batch_transport import (
    BATCH_ORIGIN,
    BATCH_PATH,
    batch_source_id,
)
from judgmentllm.batch_models import (
    AnthropicBatchAdmissionV1,
    AnthropicBatchCancelObservedV1,
    AnthropicBatchCreateReceiptV1,
    AnthropicBatchHTTPDiagnosticV1,
    AnthropicBatchInterruption,
    AnthropicBatchMemberReceiptV1,
    AnthropicBatchReadyV1,
    AnthropicBatchSelectionV1,
    AnthropicBatchShardV1,
    AnthropicBatchStatusV1,
    BatchCursorV1,
    BatchDiagnosticV1,
    BatchOperationIntentV1,
    BatchOperationReceiptV1,
    BatchShardV1,
    OpenAIBatchCollectionV1,
)
from judgmentllm.config import RunSpecification
from judgmentllm.evidence._base import EvidenceModel
from judgmentllm.execution.native_batch import (
    BatchSession,
    BudgetExhausted,
    Fault,
    InvocationBudget,
)
from judgmentllm.persistence.hashing import (
    bytes_digest,
    canonical_bytes,
    semantic_digest,
)
from judgmentllm.persistence.models import (
    IntegrityError,
    PersistenceError,
    SubmissionRecord,
)
from judgmentllm.persistence.native_batch import (
    fact,
    latest,
    put,
    read_reference,
    register,
    rows,
)
from judgmentllm.persistence.repository import Repository
from judgmentllm.preparation.batch import members_for
from judgmentllm.preparation.manifest import ExecutableRunManifestV14
from judgmentllm.providers.anthropic_batch import (
    CHUNK_BYTES,
    CONTROL_BYTES,
    AcceptedJob,
    AnthropicBatchAdapter,
    SourceError,
)
from judgmentllm.providers.anthropic_batch_response import ScreenedAnthropicBatchRowV1
from judgmentllm.providers.batch_protocol import BatchAdapter, BatchEvidenceWithheld
from judgmentllm.providers.protocol import ProviderAdapter, ProviderResponse

_READY = "anthropic-ready:"
_ADMISSION = "anthropic-admission:"
_FAILED = {
    "collection_no_progress",
    "source_limit",
    "source_framing",
    "source_conflict",
    "source_unavailable",
    "unsafe_metadata",
}


def _admission(
    repository: Repository, run_id: str, source: str
) -> AnthropicBatchAdmissionV1 | None:
    return latest(
        repository, run_id, "cursor", _ADMISSION + source, AnthropicBatchAdmissionV1
    )


def collection_summary(repository: Repository, run_id: str) -> OpenAIBatchCollectionV1:
    pending = failed = 0
    codes: set[str] = set()
    for row in repository.connection.execute(
        "SELECT record_json FROM batch_facts WHERE run_id=? AND kind='cursor' AND entity_id LIKE 'anthropic-ready:%' ORDER BY fact_seq",
        (run_id,),
    ):
        ready = AnthropicBatchReadyV1.model_validate_json(row[0])
        cursor = latest(repository, run_id, "cursor", ready.source_id, BatchCursorV1)
        admission = _admission(repository, run_id, ready.source_id)
        if (admission is None or admission.code not in _FAILED) and (
            cursor is None or not cursor.exhausted
        ):
            pending += 1
    for row in repository.connection.execute(
        "SELECT f.record_json FROM batch_facts f WHERE f.run_id=? AND f.kind='cursor' AND f.entity_id LIKE 'anthropic-admission:%' "
        "AND NOT EXISTS(SELECT 1 FROM batch_facts n WHERE n.run_id=f.run_id AND n.kind=f.kind AND n.entity_id=f.entity_id AND n.fact_seq>f.fact_seq)",
        (run_id,),
    ):
        admission = AnthropicBatchAdmissionV1.model_validate_json(row[0])
        if admission.code in _FAILED:
            failed += 1
            codes.add(admission.code)
    return OpenAIBatchCollectionV1(
        failed_sources=failed, pending_sources=pending, codes=tuple(sorted(codes))
    )


def _terminal(status: AnthropicBatchStatusV1) -> tuple[object, ...]:
    return status.created_at, status.expires_at, status.ended_at, status.counts


class AnthropicBatchSession(BatchSession):
    adapter: AnthropicBatchAdapter  # pyright: ignore[reportIncompatibleVariableOverride]

    def __init__(
        self,
        run_id: str,
        root: Path,
        adapter: BatchAdapter,
        budget: InvocationBudget,
        fault: Fault | None = None,
    ):
        if not isinstance(adapter, AnthropicBatchAdapter):
            raise IntegrityError("Anthropic Batch session requires its route adapter")
        super().__init__(run_id, root, adapter, budget, fault)
        try:
            if (
                not isinstance(self.manifest, ExecutableRunManifestV14)
                or self.manifest.batch_context != adapter.context()
            ):
                raise IntegrityError("Anthropic Batch session differs from seal")
            self.status_model = AnthropicBatchStatusV1
            self.adapter.budget = budget
            self.adapter.publish_diagnostic = self.publish_diagnostic
            self.adapter.cancel_job = self.cancel_job
            self.selection_pending = False
            self.collection_budget_unfit = False
        except BaseException:
            self.close()
            raise

    def write_fact(self, kind: str, entity: str, model: EvidenceModel) -> None:
        try:
            fact(self.repository, self.run_id, self.token, kind, entity, model)
        except OSError:
            raise PersistenceError(
                "Anthropic Batch evidence could not be durably published"
            ) from None

    def publish_diagnostic(
        self, fields: dict[str, object], retained: bytes | None
    ) -> str:
        try:
            body = None
            if retained is not None:
                if self.manifest.capture == "full":
                    body = self.store.put(retained, "application/json")
                else:
                    body = self.store.hash_only(
                        bytes_digest(retained), len(retained), "application/json"
                    )
            diagnostic = AnthropicBatchHTTPDiagnosticV1.model_validate(
                {**fields, "body": body}
            )
            with self.repository.execution_transaction(self.run_id, self.token):
                if diagnostic.body is not None:
                    register(self.repository, self.run_id, diagnostic.body)
                digest = fact(
                    self.repository,
                    self.run_id,
                    self.token,
                    "status",
                    "anthropic-http:" + diagnostic.call_id,
                    diagnostic,
                )
        except OSError:
            raise PersistenceError(
                "Anthropic Batch HTTP diagnostic could not be durably published"
            ) from None
        if diagnostic.phase == "headers":
            self.checkpoint("after_anthropic_batch_headers", diagnostic.stage)
        return digest

    async def submit_shard(
        self, shard: BatchShardV1, payload: bytes
    ) -> BatchOperationReceiptV1 | None:
        return await self.mutate(shard, "create", payload)

    def _job_is_shared(self, shard_id: str, job_id: str) -> bool:
        return (
            self.repository.connection.execute(
                "SELECT 1 FROM submissions WHERE run_id=? AND batch_uid<>? AND remote_id=? AND certainty='accepted' LIMIT 1",
                (self.run_id, shard_id, job_id),
            ).fetchone()
            is not None
        )

    def accept_receipt(
        self, intent: BatchOperationIntentV1, receipt: BatchOperationReceiptV1
    ) -> None:
        if isinstance(receipt, AnthropicBatchCreateReceiptV1) and self._job_is_shared(
            intent.shard_id, receipt.remote_id
        ):
            raise BatchEvidenceWithheld(
                "Anthropic Batch job is already bound to another shard"
            )
        super().accept_receipt(intent, receipt)

    def build_member_receipt(
        self, response: ProviderResponse, **fields: Any
    ) -> AnthropicBatchMemberReceiptV1:
        return AnthropicBatchMemberReceiptV1.model_validate(
            {
                **fields,
                "provider_result": ScreenedAnthropicBatchRowV1.model_validate_json(
                    response.raw
                ).provider_result,
            }
        )

    def _shard(self, shard_id: str) -> BatchShardV1:
        row = self.repository.connection.execute(
            "SELECT record_json,receipt_sha256 FROM batch_preparations WHERE run_id=? AND shard_id=?",
            (self.run_id, shard_id),
        ).fetchone()
        if row is None or bytes_digest(row[0].encode()) != row[1]:
            raise IntegrityError("Anthropic Batch shard proof is unavailable")
        self.store.verify(self.repository.get_artifact(self.run_id, row[1]))
        shard = AnthropicBatchShardV1.model_validate_json(row[0])
        if (
            shard.run_id != self.run_id
            or shard.shard_id != shard_id
            or shard.context != self.manifest.batch_context
        ):
            raise IntegrityError("Anthropic Batch shard context differs")
        return shard

    def _job(
        self, shard: BatchShardV1, submission: SubmissionRecord
    ) -> tuple[AcceptedJob, str]:
        row = self.repository.connection.execute(
            "SELECT record_json,intent_sha256 FROM batch_operations WHERE run_id=? AND instance_id=? AND kind='create'",
            (self.run_id, submission.submission_id),
        ).fetchone()
        if row is None or bytes_digest(row[0].encode()) != row[1]:
            raise IntegrityError("Anthropic Batch create intent is unavailable")
        self.store.verify(self.repository.get_artifact(self.run_id, row[1]))
        intent = BatchOperationIntentV1.model_validate_json(row[0])
        receipt = self.operation_receipt(intent)
        if (
            not isinstance(receipt, AnthropicBatchCreateReceiptV1)
            or receipt.remote_id != submission.remote_id
            or submission.remote_id is None
            or intent.shard_id != shard.shard_id
            or intent.manifest_sha256 != self.manifest_sha256
            or intent.instance_id != submission.submission_id
            or intent.payload_sha256 != shard.input.sha256
            or intent.service_id != self.adapter.identity
            or intent.remote_id is not None
            or submission.payload_sha256 != shard.input.sha256
            or self._job_is_shared(shard.shard_id, receipt.remote_id)
        ):
            raise IntegrityError("Anthropic Batch job differs from the accepted create")
        return AcceptedJob(
            submission.submission_id,
            receipt.remote_id,
            shard.member_count,
            (receipt.created_at, receipt.expires_at),
        ), bytes_digest(canonical_bytes(receipt))

    def cancel_job(self, intent: BatchOperationIntentV1) -> AcceptedJob:
        submission = self.accepted_submission(intent.shard_id)
        if submission is None or submission.remote_id != intent.remote_id:
            raise IntegrityError("Anthropic Batch cancel differs from accepted job")
        return self._job(self._shard(intent.shard_id), submission)[0]

    async def reconcile_operation(self, intent: BatchOperationIntentV1) -> None:
        if intent.kind != "cancel":
            return
        job = self.cancel_job(intent)
        entity = "anthropic-cancel:" + intent.instance_id
        observed = latest(
            self.repository,
            self.run_id,
            "cursor",
            entity,
            AnthropicBatchCancelObservedV1,
        )
        if observed is not None:
            if (
                observed.intent_id,
                observed.status.submission_id,
                observed.status.remote_id,
            ) != (intent.instance_id, job.submission_id, job.job_id) or (
                observed.status.created_at,
                observed.status.expires_at,
            ) != job.timestamps:
                raise IntegrityError("Anthropic Batch cancel observation differs")
            return
        try:
            status = await self.adapter.retrieve(
                job, stage="cancel_reconciliation", intent_id=intent.instance_id
            )
        except (OSError, BatchEvidenceWithheld):
            return
        if (
            status.provider_state == "canceling"
            or status.cancel_initiated_at is not None
        ):
            self.write_fact(
                "cursor",
                entity,
                AnthropicBatchCancelObservedV1(
                    intent_id=intent.instance_id, status=status
                ),
            )

    def _status(self, job: AcceptedJob) -> AnthropicBatchStatusV1 | None:
        return latest(
            self.repository,
            self.run_id,
            "status",
            job.submission_id,
            AnthropicBatchStatusV1,
        )

    def _cursor(self, source: str) -> BatchCursorV1 | None:
        return latest(self.repository, self.run_id, "cursor", source, BatchCursorV1)

    def _admission(self, source: str) -> AnthropicBatchAdmissionV1 | None:
        return _admission(self.repository, self.run_id, source)

    def _fail(self, source: str, code: Any) -> None:
        prior = self._admission(source)
        if prior is not None and prior.code in _FAILED:
            return
        cursor = self._cursor(source)
        self.write_fact(
            "cursor",
            _ADMISSION + source,
            AnthropicBatchAdmissionV1(
                sequence=prior.sequence if prior else 1,
                frontier=cursor.position if cursor else 0,
                stalls=prior.stalls if prior else 0,
                code=code,
            ),
        )

    def _project_ready(self, ready: AnthropicBatchReadyV1) -> None:
        with self.repository.execution_transaction(self.run_id, self.token):
            self.write_fact("status", ready.status.submission_id, ready.status)
            self.write_fact("cursor", _READY + ready.status.submission_id, ready)

    def _recover_ready(self, submission_id: str) -> AnthropicBatchReadyV1 | None:
        ready = read_reference(
            self.repository,
            self.run_id,
            f"batch/anthropic-terminal/{submission_id}.json",
            AnthropicBatchReadyV1,
        )
        if ready is not None:
            if ready.status.submission_id != submission_id:
                raise IntegrityError("Anthropic Batch ready reference differs")
            self._verify_ready(ready)
            self._project_ready(ready)
        return ready

    def _observe_status(
        self,
        shard: BatchShardV1,
        job: AcceptedJob,
        receipt_sha256: str,
        status: AnthropicBatchStatusV1,
    ) -> None:
        source = batch_source_id(
            self.manifest_sha256, shard.shard_id, job.submission_id
        )
        ready = latest(
            self.repository,
            self.run_id,
            "cursor",
            _READY + job.submission_id,
            AnthropicBatchReadyV1,
        )
        if ready is None:
            ready = self._recover_ready(job.submission_id)
        previous = self._status(job)
        if previous is not None:
            if (previous.created_at, previous.expires_at) != (
                status.created_at,
                status.expires_at,
            ) or (
                previous.provider_state == "ended"
                and status.provider_state == "ended"
                and _terminal(previous) != _terminal(status)
            ):
                self.write_fact(
                    "conflict",
                    job.submission_id,
                    BatchDiagnosticV1(code="conflicting_terminal"),
                )
                self._fail(source, "source_conflict")
                return
            if previous.provider_state == "ended" and (
                status.provider_state != "ended"
                or ready is not None
                and status.results_url is None
                and status.archived_at is None
            ):
                return
        if status.archived_at is not None:
            cursor = self._cursor(source)
            if cursor is None or not cursor.exhausted:
                self._fail(source, "source_unavailable")
            self.write_fact("status", job.submission_id, status)
        elif (
            status.provider_state == "ended"
            and status.results_url is not None
            and ready is None
        ):
            ready = AnthropicBatchReadyV1(
                shard_id=shard.shard_id,
                source_id=source,
                manifest_sha256=self.manifest_sha256,
                receipt_sha256=receipt_sha256,
                status=status,
            )
            put(
                self.repository,
                self.run_id,
                ready,
                reference=f"batch/anthropic-terminal/{job.submission_id}.json",
            )
            self.checkpoint("after_anthropic_terminal_receipt", job.submission_id)
            self._project_ready(ready)
        else:
            self.write_fact("status", job.submission_id, status)

    async def poll(self) -> None:
        for round_number in range(self.budget.options.max_polls):
            for shard in rows(
                self.repository, "batch_preparations", self.run_id, self.shard_model
            ):
                self.budget.remaining()
                submission = self.accepted_submission(shard.shard_id)
                if submission is None:
                    continue
                job, receipt = self._job(shard, submission)
                try:
                    status = await self.budget.call(
                        lambda job=job: self.adapter.retrieve(job)
                    )
                except SourceError as error:
                    if error.code != "source_conflict":
                        raise
                    self.write_fact(
                        "conflict",
                        job.submission_id,
                        BatchDiagnosticV1(code="conflicting_terminal"),
                    )
                    self._fail(
                        batch_source_id(
                            self.manifest_sha256, shard.shard_id, job.submission_id
                        ),
                        "source_conflict",
                    )
                    continue
                self._observe_status(shard, job, receipt, status)
            if round_number + 1 < self.budget.options.max_polls:
                await self.budget.call(
                    lambda: asyncio.sleep(self.budget.options.poll_interval_seconds)
                )

    def _verify_ready(
        self, ready: AnthropicBatchReadyV1
    ) -> tuple[BatchShardV1, SubmissionRecord, AcceptedJob]:
        shard = self._shard(ready.shard_id)
        submission = self.accepted_submission(shard.shard_id)
        if submission is None:
            raise IntegrityError("Anthropic Batch ready source lacks acceptance")
        job, receipt = self._job(shard, submission)
        if (
            ready.manifest_sha256 != self.manifest_sha256
            or ready.receipt_sha256 != receipt
            or ready.source_id
            != batch_source_id(self.manifest_sha256, shard.shard_id, job.submission_id)
            or (ready.status.submission_id, ready.status.remote_id)
            != (job.submission_id, job.job_id)
            or (ready.status.created_at, ready.status.expires_at) != job.timestamps
            or ready.status.provider_state != "ended"
            or ready.status.counts.total != job.member_count
            or ready.status.results_url
            != f"{BATCH_ORIGIN}{BATCH_PATH}/{job.job_id}/results"
        ):
            raise IntegrityError(
                "Anthropic Batch ready source differs from the accepted job"
            )
        return shard, submission, job

    def _select(self) -> AnthropicBatchReadyV1 | None:
        selector = latest(
            self.repository,
            self.run_id,
            "cursor",
            "anthropic-selection",
            AnthropicBatchSelectionV1,
        )
        after = selector.after if selector else 0
        errors = tuple(sorted(_FAILED))
        for boundary in (after, 0) if after else (0,):
            self.budget.reconcile_step()
            row = self.repository.connection.execute(
                "SELECT r.fact_seq,r.record_json,r.receipt_sha256 FROM batch_facts r "
                "WHERE r.run_id=? AND r.kind='cursor' AND r.entity_id LIKE 'anthropic-ready:%' AND r.fact_seq>? "
                "AND NOT EXISTS(SELECT 1 FROM batch_facts d WHERE d.run_id=r.run_id AND d.kind='cursor' "
                "AND d.entity_id=json_extract(r.record_json,'$.source_id') AND json_extract(d.record_json,'$.exhausted')=1) "
                "AND NOT EXISTS(SELECT 1 FROM batch_facts d WHERE d.run_id=r.run_id AND d.kind='cursor' "
                "AND d.entity_id='anthropic-admission:'||json_extract(r.record_json,'$.source_id') "
                "AND json_extract(d.record_json,'$.code') IN ("
                + ",".join("?" for _ in errors)
                + ")) "
                "ORDER BY r.fact_seq LIMIT 1",
                (self.run_id, boundary, *errors),
            ).fetchone()
            if row is not None:
                if bytes_digest(row[1].encode()) != row[2]:
                    raise IntegrityError("Anthropic Batch ready projection differs")
                self.store.verify(self.repository.get_artifact(self.run_id, row[2]))
                ready = AnthropicBatchReadyV1.model_validate_json(row[1])
                self.write_fact(
                    "cursor",
                    "anthropic-selection",
                    AnthropicBatchSelectionV1(
                        sequence=selector.sequence + 1 if selector else 1, after=row[0]
                    ),
                )
                return ready
        return None

    def _restore_ready(self) -> bool:
        marker = latest(
            self.repository,
            self.run_id,
            "cursor",
            "anthropic-projection",
            AnthropicBatchSelectionV1,
        )
        after = marker.after if marker else 0
        page = self.repository.connection.execute(
            "SELECT s.rowid,s.submission_id FROM submissions s WHERE s.run_id=? AND s.certainty='accepted' AND s.batch_uid IS NOT NULL AND s.rowid>? "
            "AND NOT EXISTS(SELECT 1 FROM batch_facts f WHERE f.run_id=s.run_id AND f.kind='cursor' AND f.entity_id='anthropic-ready:'||s.submission_id) "
            "ORDER BY s.rowid LIMIT 129",
            (self.run_id, after),
        ).fetchall()
        for row in page[:128]:
            self.budget.reconcile_step()
            if self._recover_ready(row[1]) is not None:
                return True
        if len(page) > 128:
            self.write_fact(
                "cursor",
                "anthropic-projection",
                AnthropicBatchSelectionV1(
                    sequence=marker.sequence + 1 if marker else 1, after=page[127][0]
                ),
            )
            self.selection_pending = True
        elif marker is not None and after:
            # Revisit the head after a later poll can publish new references.
            self.write_fact(
                "cursor",
                "anthropic-projection",
                AnthropicBatchSelectionV1(sequence=marker.sequence + 1, after=0),
            )
        return False

    async def collect_ready(
        self, spec: RunSpecification | None, use_frozen_snapshot: bool
    ) -> bool:
        ready = self._select()
        if ready is None and self._restore_ready():
            ready = self._select()
        if ready is None:
            return self.selection_pending
        if self.prepared is None:
            self.open_sources(spec, use_frozen_snapshot)
        shard, submission, job = self._verify_ready(ready)
        try:
            await self._collect(ready, shard, submission, job)
        except SourceError as error:
            self._fail(ready.source_id, error.code)
        except BatchEvidenceWithheld:
            self._fail(ready.source_id, "unsafe_metadata")
        except (BudgetExhausted, TimeoutError, OSError):
            self.selection_pending = True
            admission = self._admission(ready.source_id)
            cursor = self._cursor(ready.source_id)
            if (
                admission is not None
                and admission.stalls >= 3
                and (cursor is None or cursor.position <= admission.frontier)
            ):
                self._fail(ready.source_id, "collection_no_progress")
        return True

    async def _collect(
        self,
        ready: AnthropicBatchReadyV1,
        shard: BatchShardV1,
        submission: SubmissionRecord,
        job: AcceptedJob,
    ) -> None:
        assert self.prepared is not None
        source = ready.source_id
        saved = self._cursor(source)
        target = saved.position if saved else 0
        if saved is not None and (
            saved.run_id,
            saved.submission_id,
            saved.source_id,
        ) != (self.run_id, job.submission_id, source):
            raise IntegrityError("Anthropic Batch cursor differs from its source")
        by_key = {
            member.member_key: member
            for member in members_for(self.prepared, shard.shard_id)
        }
        if len(by_key) != job.member_count:
            raise IntegrityError("Anthropic Batch member count differs from the seal")
        receipts: dict[str, AnthropicBatchMemberReceiptV1] = {}
        ahead: AnthropicBatchMemberReceiptV1 | None = None
        for member in by_key.values():
            self.budget.remaining()
            receipt = read_reference(
                self.repository,
                self.run_id,
                f"batch/members/{member.member_key}.json",
                AnthropicBatchMemberReceiptV1,
            )
            if receipt is None:
                continue
            if (receipt.member_key, receipt.source_id) != (
                member.member_key,
                source,
            ) or (
                receipt.identity.run_id,
                receipt.identity.submission_id,
                receipt.identity.attempt_id,
                receipt.identity.request_uid,
            ) != (
                self.run_id,
                job.submission_id,
                member.attempt_id,
                member.request_uid,
            ):
                raise IntegrityError(
                    "Anthropic Batch member receipt differs from its source"
                )
            receipts[member.member_key] = receipt
            if receipt.source_position >= target:
                if receipt.source_position != target or ahead is not None:
                    raise IntegrityError(
                        "Anthropic Batch receipt is outside its cursor frontier"
                    )
                ahead = receipt
            if (
                receipt.capture == "full"
                and not self.repository.get_results_for_attempt(
                    self.run_id, member.attempt_id
                )
            ):
                self.save_member(
                    member,
                    submission,
                    source,
                    receipt.source_position,
                    self.store.read(receipt.retained),
                    replay=True,
                )

        previous = self._admission(source)
        stalls = (
            0 if previous is None or target > previous.frontier else previous.stalls
        )
        if stalls >= 3:
            self._fail(source, "collection_no_progress")
            return

        def validate_status(status: AnthropicBatchStatusV1) -> None:
            if status.provider_state != "ended":
                raise OSError("Anthropic Batch terminal metadata is pending")
            if _terminal(status) != _terminal(ready.status):
                raise SourceError("source_conflict")
            if status.archived_at is not None:
                raise SourceError("source_unavailable")
            if status.results_url is None:
                raise OSError("Anthropic Batch result URL is pending")
            if status.results_url != ready.status.results_url:
                raise SourceError("source_conflict")

        def admit() -> None:
            source_limit = self.adapter.context().max_source_bytes
            if (
                self.budget.options.max_io_steps - self.budget.steps
                < 64 + 4 * (source_limit // CHUNK_BYTES) + 2
                or self.budget.byte_limit - self.budget.bytes
                < 2 * source_limit + CONTROL_BYTES
            ):
                self.selection_pending = self.collection_budget_unfit = True
                raise BudgetExhausted
            self.repository.require_owner(self.run_id, self.token)
            self.write_fact(
                "cursor",
                _ADMISSION + source,
                AnthropicBatchAdmissionV1(
                    sequence=previous.sequence + 1 if previous else 1,
                    frontier=target,
                    stalls=stalls + 1,
                    code="admitted",
                ),
            )
            self.checkpoint("after_anthropic_batch_admission", source)

        prefix = semantic_digest("anthropic-batch-prefix-v1", [source])
        position = 0
        buffer = bytearray()
        seen: dict[str, tuple[str, str]] = {}
        counts: Counter[str] = Counter()
        close_code: AnthropicBatchInterruption | None = None
        stream = self.adapter.content(
            job, source, validate_status, admit, lambda: close_code
        )

        def process(line: bytes) -> None:
            nonlocal prefix, position
            self.budget.remaining()
            screened = self.adapter.screen_result(line, by_key)
            digest = semantic_digest("batch-withheld-v1", [source, position])
            first = False
            if screened is not None:
                member, response, _, safe, _ = screened
                outcome = ScreenedAnthropicBatchRowV1.model_validate_json(
                    response.raw
                ).provider_result
                digest = bytes_digest(safe)
                prior = receipts.get(member)
                first = member not in seen
                if (
                    member in seen
                    and seen[member] != (digest, outcome)
                    or prior is not None
                    and (
                        prior.retained.sha256 != digest
                        or prior.provider_result != outcome
                        or (member not in seen and prior.source_position != position)
                    )
                ):
                    self.write_fact(
                        "conflict", member, BatchDiagnosticV1(code="conflicting_member")
                    )
                    raise SourceError("source_conflict")
                if position < target and prior is None:
                    raise SourceError("source_conflict")
                if first:
                    counts[outcome] += 1
                    if counts[outcome] > getattr(ready.status.counts, outcome):
                        raise SourceError("source_conflict")
                seen[member] = digest, outcome
            if (
                ahead is not None
                and position == target
                and (
                    screened is None
                    or screened[0] != ahead.member_key
                    or digest != ahead.retained.sha256
                )
            ):
                raise SourceError("source_conflict")
            next_prefix = semantic_digest(
                "batch-result-prefix-step-v1", [prefix, position, digest]
            )
            if position < target:
                if (
                    position + 1 == target
                    and saved is not None
                    and next_prefix != saved.prefix_sha256
                ):
                    raise SourceError("source_conflict")
            else:
                self.budget.remaining()
                if screened is None:
                    self.write_fact(
                        "withheld",
                        f"{source}:{position}",
                        BatchDiagnosticV1(code="unsafe_evidence"),
                    )
                elif first:
                    self.save_member(
                        by_key[screened[0]], submission, source, position, line
                    )
                self.write_fact(
                    "cursor",
                    source,
                    BatchCursorV1(
                        run_id=self.run_id,
                        submission_id=job.submission_id,
                        source_id=source,
                        position=position + 1,
                        prefix_sha256=next_prefix,
                    ),
                )
                self.checkpoint("after_batch_cursor", source)
                self.budget.result_rows += 1
            position += 1
            prefix = next_prefix

        try:
            while True:
                if (
                    position >= target
                    and self.budget.result_rows >= self.budget.options.max_result_rows
                ):
                    close_code = "row_quota"
                    return
                try:
                    chunk = await self.budget.call(lambda: anext(stream))
                except StopAsyncIteration:
                    if buffer:
                        process(bytes(buffer))
                    break
                for index, part in enumerate(chunk.split(b"\n")):
                    if index:
                        if (
                            len(buffer)
                            >= self.manifest.batch_context.limits.max_record_bytes
                        ):
                            raise SourceError("source_framing")
                        process(bytes(buffer))
                        buffer.clear()
                        if (
                            position >= target
                            and self.budget.result_rows
                            >= self.budget.options.max_result_rows
                        ):
                            close_code = "row_quota"
                            return
                    if (
                        len(buffer) + len(part)
                        > self.manifest.batch_context.limits.max_record_bytes
                    ):
                        raise SourceError("source_framing")
                    buffer.extend(part)
        except SourceError:
            close_code = "unsafe_response"
            raise
        except BudgetExhausted:
            close_code = "budget"
            raise
        except TimeoutError:
            close_code = "deadline"
            raise
        except asyncio.CancelledError:
            close_code = "cancelled"
            raise
        finally:
            from judgmentllm.lifecycle import _close_adapter  # pyright: ignore[reportPrivateUsage]

            await _close_adapter(cast(ProviderAdapter, stream))
        if position < target or ahead is not None and target >= position:
            raise SourceError("source_conflict")
        await self.budget.call(
            lambda: self.adapter.retrieve(
                job,
                stage="results_confirmation",
                source_id=source,
                check=validate_status,
            )
        )
        self.write_fact(
            "cursor",
            source,
            BatchCursorV1(
                run_id=self.run_id,
                submission_id=job.submission_id,
                source_id=source,
                position=position,
                prefix_sha256=prefix,
                exhausted=True,
            ),
        )
