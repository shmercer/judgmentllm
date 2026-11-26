"""Bounded ready-first collection for immutable OpenAI Files sources.

The append-only selection/admission facts reuse schema 4's cursor namespace;
scientific attempts and submission operation allowances are never reused here.
"""

from __future__ import annotations

import asyncio
from math import ceil
from typing import Any

from judgmentllm.batch_models import (
    BatchCursorV1,
    BatchDiagnosticV1,
    BatchMemberReceiptV1,
    BatchOperationReceiptV1,
    OpenAIBatchAbsentV1,
    OpenAIBatchAdmissionV1,
    OpenAIBatchCollectionV1,
    OpenAIBatchProjectionV1,
    OpenAIBatchReadyV1,
    OpenAIBatchSelectionV1,
    OpenAIBatchSourceBindingV1,
    OpenAIBatchStatusV1,
)
from judgmentllm.config import RunSpecification
from judgmentllm.execution.native_batch import BatchSession, BudgetExhausted
from judgmentllm.openai_batch_transport import ROUTES, require_sdk
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
from judgmentllm.persistence.native_batch import fact as durable_fact
from judgmentllm.persistence.native_batch import latest, put, read_reference, rows
from judgmentllm.persistence.repository import Repository
from judgmentllm.preparation.batch import encode_row, members_for, reconstruct
from judgmentllm.preparation.manifest import ExecutableRunManifestV13
from judgmentllm.providers.batch_protocol import (
    BatchEvidenceWithheld,
    BatchSourceOverrun,
)
from judgmentllm.providers.openai_batch import CONTROL_MAX_BYTES, OpenAIBatchAdapter

_TERMINAL_ERRORS = frozenset(
    {
        "collection_no_progress",
        "source_limit",
        "source_conflict",
        "source_overrun",
        "source_underrun",
        "source_framing",
        "unsafe_metadata",
    }
)


def fact(*args: Any, **kwargs: Any) -> str:
    try:
        return durable_fact(*args, **kwargs)
    except OSError:
        raise PersistenceError(
            "OpenAI Batch evidence could not be durably published"
        ) from None


def source_id(
    manifest_sha256: str, shard_id: str, submission_id: str, role: str
) -> str:
    return semantic_digest(
        "openai-batch-source-slot-v1", [manifest_sha256, shard_id, submission_id, role]
    )


def source_state(
    repository: Repository, run_id: str, source: str
) -> OpenAIBatchAdmissionV1 | None:
    return latest(
        repository,
        run_id,
        "cursor",
        "openai-admission:" + source,
        OpenAIBatchAdmissionV1,
    )


def collection_summary(repository: Repository, run_id: str) -> OpenAIBatchCollectionV1:
    from judgmentllm.batch_models import OpenAIBatchCollectionV1

    failed = pending = 0
    codes: set[str] = set()
    for row in repository.connection.execute(
        "SELECT record_json FROM batch_facts WHERE run_id=? AND kind='cursor' AND entity_id LIKE 'openai-ready:%' ORDER BY fact_seq",
        (run_id,),
    ):
        ready = OpenAIBatchReadyV1.model_validate_json(row[0])
        state = source_state(repository, run_id, ready.source_id)
        if state is not None and state.code in _TERMINAL_ERRORS:
            failed += 1
            codes.add(state.code)
        elif not source_done(repository, run_id, ready.source_id):
            pending += 1
    return OpenAIBatchCollectionV1(
        failed_sources=failed,
        pending_sources=pending,
        codes=tuple(sorted(codes)),
    )


def source_done(repository: Repository, run_id: str, source: str) -> bool:
    cursor = latest(repository, run_id, "cursor", source, BatchCursorV1)
    absent = latest(
        repository, run_id, "cursor", "openai-absent:" + source, OpenAIBatchAbsentV1
    )
    return bool(absent is not None or (cursor is not None and cursor.exhausted))


class OpenAIBatchSession(BatchSession):
    adapter: OpenAIBatchAdapter  # pyright: ignore[reportIncompatibleVariableOverride]
    manifest: ExecutableRunManifestV13  # pyright: ignore[reportIncompatibleVariableOverride]

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        try:
            assert isinstance(self.manifest, ExecutableRunManifestV13)
            assert isinstance(self.adapter, OpenAIBatchAdapter)
            if self.adapter.context() != self.manifest.batch_context:
                raise IntegrityError(
                    "OpenAI Batch context differs from the sealed route"
                )
            self.status_model = OpenAIBatchStatusV1
            self.adapter.budget = self.budget
            self.adapter.upload_resolver = self.upload_bytes
            self.selection_pending = False
            self.selection_progress = False
            self.collection_budget_unfit = False
        except BaseException:
            self.close()
            raise

    def write_fact(self, kind: str, entity: str, model: Any) -> None:
        fact(self.repository, self.run_id, self.token, kind, entity, model)

    def save_member(self, *args: Any, **kwargs: Any) -> str:
        try:
            return super().save_member(*args, **kwargs)
        except OSError:
            raise PersistenceError(
                "OpenAI Batch member evidence could not be durably published"
            ) from None

    def intent_for(
        self, submission: SubmissionRecord
    ) -> tuple[Any, BatchOperationReceiptV1]:
        from judgmentllm.batch_models import BatchOperationIntentV1

        row = self.repository.connection.execute(
            "SELECT record_json,intent_sha256 FROM batch_operations WHERE run_id=? AND instance_id=? AND kind='create'",
            (self.run_id, submission.submission_id),
        ).fetchone()
        if row is None:
            raise IntegrityError("accepted job lacks its original create intent")
        intent = BatchOperationIntentV1.model_validate_json(row[0])
        if bytes_digest(canonical_bytes(intent)) != row[1]:
            raise IntegrityError("accepted job create intent differs")
        self.store.verify(self.repository.get_artifact(self.run_id, row[1]))
        receipt = self.operation_receipt(intent)
        if receipt is None or receipt.remote_id != submission.remote_id:
            raise IntegrityError("accepted job lacks its original receipt")
        from judgmentllm.batch_models import BatchOperationIntentV1

        upload_row = self.repository.connection.execute(
            "SELECT record_json,intent_sha256 FROM batch_operations WHERE run_id=? AND shard_id=? AND kind='upload' ORDER BY sequence DESC LIMIT 1",
            (self.run_id, intent.shard_id),
        ).fetchone()
        if upload_row is None:
            raise IntegrityError("accepted create lacks its upload intent")
        upload = BatchOperationIntentV1.model_validate_json(upload_row[0])
        if bytes_digest(canonical_bytes(upload)) != upload_row[1]:
            raise IntegrityError("accepted upload intent differs")
        self.store.verify(self.repository.get_artifact(self.run_id, upload_row[1]))
        upload_receipt = self.operation_receipt(upload)
        if upload_receipt is None or upload_receipt.remote_id != intent.remote_id:
            raise IntegrityError("accepted create differs from its upload receipt")
        return intent, receipt

    def upload_bytes(self, intent: Any) -> bytes | None:
        if self.prepared is None:
            return None
        row = self.repository.connection.execute(
            "SELECT record_json,receipt_sha256 FROM batch_preparations WHERE run_id=? AND shard_id=?",
            (self.run_id, intent.shard_id),
        ).fetchone()
        if row is None:
            raise IntegrityError("upload recovery lacks its original shard")
        shard = self.shard_model.model_validate_json(row[0])
        if bytes_digest(canonical_bytes(shard)) != row[1]:
            raise IntegrityError("upload recovery shard differs from seal")
        data = bytearray()
        for member in members_for(self.prepared, shard.shard_id):
            self.budget.remaining()
            data.extend(
                encode_row(
                    member.member_key,
                    reconstruct(self.prepared, self.adapter, member)[1],
                )
            )
        raw = bytes(data)
        if (bytes_digest(raw), len(raw)) != (
            shard.input.sha256,
            shard.input.byte_length,
        ):
            raise IntegrityError("upload recovery payload differs from seal")
        return raw

    async def poll(self) -> None:
        for poll_number in range(self.budget.options.max_polls):
            for shard in rows(
                self.repository, "batch_preparations", self.run_id, self.shard_model
            ):
                submission = self.accepted_submission(shard.shard_id)
                if submission is None:
                    continue
                intent, _ = self.intent_for(submission)
                try:
                    status = await self.budget.call(
                        lambda submission=submission: self.adapter.status(
                            submission.submission_id, submission.remote_id
                        )
                    )
                except BatchEvidenceWithheld:
                    self.write_fact(
                        "withheld",
                        submission.submission_id,
                        BatchDiagnosticV1(code="unsafe_evidence"),
                    )
                    raise OSError("OpenAI Batch status evidence was withheld") from None
                status = OpenAIBatchStatusV1.model_validate_json(
                    canonical_bytes(status)
                )
                previous = latest(
                    self.repository,
                    self.run_id,
                    "status",
                    submission.submission_id,
                    OpenAIBatchStatusV1,
                )
                if (
                    status.submission_id != submission.submission_id
                    or status.remote_id != submission.remote_id
                    or status.input_file_id != intent.remote_id
                    or status.endpoint != ROUTES[self.manifest.batch_context.endpoint]
                    or (
                        previous is not None
                        and previous.state != "running"
                        and status != previous
                    )
                ):
                    self.write_fact(
                        "conflict",
                        submission.submission_id,
                        BatchDiagnosticV1(code="conflicting_terminal"),
                    )
                    for role in ("output", "error"):
                        source = source_id(
                            self.manifest_sha256,
                            shard.shard_id,
                            submission.submission_id,
                            role,
                        )
                        self.fail_source(source, "source_conflict")
                    continue
                artifact = put(
                    self.repository,
                    self.run_id,
                    status,
                    reference=(
                        "batch/openai-status/" + submission.submission_id + ".json"
                    )
                    if status.state != "running"
                    else None,
                )
                # One projection transaction makes terminal status and both slots ready.
                with self.repository.execution_transaction(self.run_id, self.token):
                    fact(
                        self.repository,
                        self.run_id,
                        self.token,
                        "status",
                        submission.submission_id,
                        status,
                    )
                    if status.state != "running":
                        for role in ("output", "error"):
                            source = source_id(
                                self.manifest_sha256,
                                shard.shard_id,
                                submission.submission_id,
                                role,
                            )
                            ready = OpenAIBatchReadyV1(
                                run_id=self.run_id,
                                manifest_sha256=self.manifest_sha256,
                                shard_id=shard.shard_id,
                                submission_id=submission.submission_id,
                                source_id=source,
                                role=role,
                                status_sha256=artifact.sha256,
                            )
                            fact(
                                self.repository,
                                self.run_id,
                                self.token,
                                "cursor",
                                "openai-ready:" + source,
                                ready,
                            )
                self.checkpoint("after_openai_batch_ready", submission.submission_id)
            if poll_number + 1 < self.budget.options.max_polls:
                await self.budget.call(
                    lambda: asyncio.sleep(self.budget.options.poll_interval_seconds)
                )

    def recover_ready_projection(self) -> None:
        # Only screened immutable terminal receipts are replayed; this is local.
        state = latest(
            self.repository,
            self.run_id,
            "cursor",
            "openai-projection",
            OpenAIBatchProjectionV1,
        )
        high = self.repository.connection.execute(
            "SELECT coalesce(max(rowid),0) FROM submissions WHERE run_id=? AND certainty='accepted' AND batch_uid IS NOT NULL",
            (self.run_id,),
        ).fetchone()[0]
        after, cut = (
            (0, high)
            if state is None or state.after >= state.cut
            else (state.after, state.cut)
        )
        page = self.repository.connection.execute(
            "SELECT submission_id,batch_uid,rowid FROM submissions WHERE run_id=? AND certainty='accepted' AND batch_uid IS NOT NULL AND rowid>? AND rowid<=? ORDER BY rowid LIMIT 128",
            (self.run_id, after, cut),
        ).fetchall()
        for row in page:
            if not self.selection_step():
                return
            # Consideration is durable before this unrelated local receipt read.
            after = row[2]
            state = OpenAIBatchProjectionV1(
                run_id=self.run_id,
                sequence=1 if state is None else state.sequence + 1,
                after=after,
                cut=cut,
            )
            fact(
                self.repository,
                self.run_id,
                self.token,
                "cursor",
                "openai-projection",
                state,
            )
            self.selection_progress = True
            status = read_reference(
                self.repository,
                self.run_id,
                "batch/openai-status/" + row[0] + ".json",
                OpenAIBatchStatusV1,
            )
            if status is None or status.state == "running":
                continue
            submission = self.accepted_submission(row[1])
            assert submission is not None
            intent, _ = self.intent_for(submission)
            if (
                status.submission_id,
                status.remote_id,
                status.input_file_id,
                status.endpoint,
            ) != (
                submission.submission_id,
                submission.remote_id,
                intent.remote_id,
                ROUTES[self.manifest.batch_context.endpoint],
            ):
                raise IntegrityError(
                    "screened status reference conflicts with accepted job"
                )
            with self.repository.execution_transaction(self.run_id, self.token):
                fact(self.repository, self.run_id, self.token, "status", row[0], status)
                for role in ("output", "error"):
                    source = source_id(self.manifest_sha256, row[1], row[0], role)
                    fact(
                        self.repository,
                        self.run_id,
                        self.token,
                        "cursor",
                        "openai-ready:" + source,
                        OpenAIBatchReadyV1(
                            run_id=self.run_id,
                            manifest_sha256=self.manifest_sha256,
                            shard_id=row[1],
                            submission_id=row[0],
                            source_id=source,
                            role=role,
                            status_sha256=bytes_digest(canonical_bytes(status)),
                        ),
                    )

        self.selection_pending = after < cut

    def selection_step(self) -> bool:
        try:
            self.budget.reconcile_step()
        except BudgetExhausted:
            self.selection_pending = True
            return False
        return True

    def select_ready(self) -> OpenAIBatchReadyV1 | None:
        state = latest(
            self.repository,
            self.run_id,
            "cursor",
            "openai-selector",
            OpenAIBatchSelectionV1,
        )
        high = self.repository.connection.execute(
            "SELECT coalesce(max(fact_seq),0) FROM batch_facts WHERE run_id=? AND kind='cursor' AND entity_id LIKE 'openai-ready:%'",
            (self.run_id,),
        ).fetchone()[0]
        after, cut = (
            (0, high)
            if state is None or state.after >= state.cut
            else (state.after, state.cut)
        )

        def considered(position: int, *, selected: bool = False) -> None:
            nonlocal state
            changed = state is None or (state.after, state.cut) != (position, cut)
            if changed or selected:
                state = OpenAIBatchSelectionV1(
                    run_id=self.run_id,
                    sequence=1 if state is None else state.sequence + 1,
                    after=position,
                    cut=cut,
                )
                fact(
                    self.repository,
                    self.run_id,
                    self.token,
                    "cursor",
                    "openai-selector",
                    state,
                )
                self.selection_progress = True

        # Completed/failed sources cannot consume another invocation's scan budget.
        # Each exclusion uses the existing run/kind/entity index and immutable facts.
        errors = tuple(sorted(_TERMINAL_ERRORS))
        eligible = (
            "SELECT r.fact_seq,r.record_json,r.receipt_sha256 FROM batch_facts r "
            "WHERE r.run_id=? AND r.kind='cursor' AND r.entity_id LIKE 'openai-ready:%' "
            "AND r.fact_seq>? AND r.fact_seq<=? "
            "AND NOT EXISTS(SELECT 1 FROM batch_facts d WHERE d.run_id=r.run_id AND d.kind='cursor' "
            "AND d.entity_id=json_extract(r.record_json,'$.source_id') AND json_extract(d.record_json,'$.exhausted')=1) "
            "AND NOT EXISTS(SELECT 1 FROM batch_facts d WHERE d.run_id=r.run_id AND d.kind='cursor' "
            "AND d.entity_id='openai-absent:'||json_extract(r.record_json,'$.source_id')) "
            "AND NOT EXISTS(SELECT 1 FROM batch_facts d WHERE d.run_id=r.run_id AND d.kind='cursor' "
            "AND d.entity_id='openai-admission:'||json_extract(r.record_json,'$.source_id') "
            "AND json_extract(d.record_json,'$.code') IN ("
            + ",".join("?" for _ in errors)
            + ")) "
            "ORDER BY r.fact_seq LIMIT 128"
        )
        # Tail, then bounded head: wrap before any unrelated local/remote prelude.
        for lower, upper in ((after, cut), (0, min(after, cut))):
            if lower >= upper:
                continue
            if not self.selection_step():
                return None
            page = self.repository.connection.execute(
                eligible, (self.run_id, lower, upper, *errors)
            ).fetchall()
            if page:
                if not self.selection_step():
                    return None
                row = page[0]
                ready = OpenAIBatchReadyV1.model_validate_json(row[1])
                if (
                    bytes_digest(canonical_bytes(ready)) != row[2]
                    or ready.manifest_sha256 != self.manifest_sha256
                ):
                    raise IntegrityError("ready-source projection differs")
                self.store.verify(self.repository.get_artifact(self.run_id, row[2]))
                considered(row[0], selected=True)
                return ready
            # Persist tail exhaustion before a bounded head lookup can yield.
            if upper == cut and after < cut:
                considered(cut)
        return None

    def cursor(self, source: str) -> BatchCursorV1 | None:
        return latest(self.repository, self.run_id, "cursor", source, BatchCursorV1)

    def fail_source(
        self,
        source: str,
        code: Any,
        *,
        sequence: int | None = None,
        stalls: int | None = None,
    ) -> OpenAIBatchAdmissionV1:
        previous = source_state(self.repository, self.run_id, source)
        cursor = self.cursor(source)
        record = OpenAIBatchAdmissionV1(
            run_id=self.run_id,
            source_id=source,
            sequence=sequence or (previous.sequence if previous else 1),
            frontier=cursor.position if cursor else 0,
            stalls=stalls
            if stalls is not None
            else (previous.stalls if previous else 0),
            code=code,
        )
        fact(
            self.repository,
            self.run_id,
            self.token,
            "cursor",
            "openai-admission:" + source,
            record,
        )
        return record

    def admission_stalls(self, ready: OpenAIBatchReadyV1) -> int | None:
        source = ready.source_id
        previous = source_state(self.repository, self.run_id, source)
        cursor = self.cursor(source)
        frontier = cursor.position if cursor else 0
        advanced = previous is not None and (
            frontier > previous.frontier or (cursor is not None and cursor.exhausted)
        )
        stalls = 0 if previous is None or advanced else previous.stalls
        if stalls >= 3:
            self.fail_source(source, "collection_no_progress", stalls=3)
            return None
        return stalls

    def admit(self, ready: OpenAIBatchReadyV1) -> OpenAIBatchAdmissionV1 | None:
        stalls = self.admission_stalls(ready)
        if stalls is None:
            return None
        source = ready.source_id
        previous = source_state(self.repository, self.run_id, source)
        return self.fail_source(
            source,
            "admitted",
            sequence=1 if previous is None else previous.sequence + 1,
            stalls=stalls + 1,
        )

    def selected_job(
        self, ready: OpenAIBatchReadyV1
    ) -> tuple[SubmissionRecord, BatchOperationReceiptV1, OpenAIBatchStatusV1]:
        submission = self.accepted_submission(ready.shard_id)
        if submission is None or submission.submission_id != ready.submission_id:
            raise IntegrityError("ready source differs from its accepted submission")
        intent, receipt = self.intent_for(submission)
        artifact = self.repository.get_artifact(self.run_id, ready.status_sha256)
        status = OpenAIBatchStatusV1.model_validate_json(self.store.read(artifact))
        if (
            status.submission_id,
            status.remote_id,
            status.input_file_id,
            status.endpoint,
        ) != (
            submission.submission_id,
            submission.remote_id,
            intent.remote_id,
            ROUTES[self.manifest.batch_context.endpoint],
        ) or status.state == "running":
            raise IntegrityError("ready source job association differs")
        current = latest(
            self.repository,
            self.run_id,
            "status",
            submission.submission_id,
            OpenAIBatchStatusV1,
        )
        if current != status:
            raise IntegrityError("ready source terminal association changed")
        return submission, receipt, status

    async def collect_ready(
        self, spec: RunSpecification | None, use_frozen_snapshot: bool
    ) -> bool:
        # First inspect the durable ready cut. No unrelated provider poll precedes it.
        while True:
            ready = self.select_ready()
            if ready is None:
                if self.selection_pending:
                    return True
                self.recover_ready_projection()
                ready = self.select_ready()
                if ready is None:
                    return self.selection_pending
            if self.admission_stalls(ready) is None:
                continue
            source = ready.source_id
            try:
                self.budget.remaining()
                try:
                    submission, receipt, status = self.selected_job(ready)
                except OSError:
                    raise PersistenceError(
                        "OpenAI Batch accepted source evidence is unavailable"
                    ) from None
                file_id = getattr(status, ready.role + "_file_id")
                if file_id is None:
                    fact(
                        self.repository,
                        self.run_id,
                        self.token,
                        "cursor",
                        "openai-absent:" + source,
                        OpenAIBatchAbsentV1(ready=ready),
                    )
                    continue
                require_sdk()
                if self.prepared is None:
                    try:
                        self.open_sources(spec, use_frozen_snapshot)
                    except OSError:
                        raise PersistenceError(
                            "OpenAI Batch source reconstruction is unavailable"
                        ) from None
                self.budget.remaining()
                await self.collect_file(ready, submission, receipt, status, file_id)
            except BatchEvidenceWithheld:
                self.fail_source(source, "unsafe_metadata")
            except (BudgetExhausted, TimeoutError, OSError):
                self.selection_pending = True
            return True

    async def collect_file(
        self,
        ready: OpenAIBatchReadyV1,
        submission: SubmissionRecord,
        receipt: BatchOperationReceiptV1,
        status: OpenAIBatchStatusV1,
        file_id: str,
    ) -> None:
        source = ready.source_id
        binding = latest(
            self.repository,
            self.run_id,
            "cursor",
            "openai-binding:" + source,
            OpenAIBatchSourceBindingV1,
        )
        assert self.prepared is not None
        members = list(members_for(self.prepared, ready.shard_id))
        by_key = {member.member_key: member for member in members}
        compiled = {
            member.member_key: reconstruct(self.prepared, self.adapter, member)[1]
            for member in members
        }
        # Full receipts can finish interrupted normalization without the provider row.
        if self.manifest.capture == "full":
            for member in members:
                prior = read_reference(
                    self.repository,
                    self.run_id,
                    "batch/members/" + member.member_key + ".json",
                    BatchMemberReceiptV1,
                )
                if prior is not None and not self.repository.get_results_for_attempt(
                    self.run_id, member.attempt_id
                ):
                    self.save_member(
                        member,
                        submission,
                        prior.source_id,
                        prior.source_position,
                        self.store.read(prior.retained),
                        replay=True,
                    )
        self.budget.remaining()
        try:
            metadata = await self.budget.call(
                lambda: self.adapter.file_metadata(file_id)
            )
        except BatchEvidenceWithheld:
            self.fail_source(
                source, "source_conflict" if binding is not None else "unsafe_metadata"
            )
            return
        candidate = OpenAIBatchSourceBindingV1(
            ready=ready,
            create_receipt_sha256=bytes_digest(canonical_bytes(receipt)),
            job=status,
            file_id=metadata[0],
            purpose=metadata[1],
            byte_size=metadata[2],
        )
        if binding is not None and binding != candidate:
            self.fail_source(source, "source_conflict")
            return
        if binding is None:
            fact(
                self.repository,
                self.run_id,
                self.token,
                "cursor",
                "openai-binding:" + source,
                candidate,
            )
            binding = candidate
        size = binding.byte_size
        if size > self.manifest.batch_context.max_source_bytes:
            self.fail_source(source, "source_limit")
            return
        # Reserve a complete replay, unread tail, real EOF and post-EOF metadata.
        required = 64 + 4 * ceil(size / 65_536) + 2
        # The initial metadata is already charged; C bounds the final metadata.
        required_bytes = 2 * size + CONTROL_MAX_BYTES
        if (
            self.budget.options.max_io_steps - self.budget.steps < required
            or self.budget.byte_limit - self.budget.bytes < required_bytes
        ):
            self.collection_budget_unfit = self.selection_pending = True
            return
        self.budget.remaining()
        admission = self.admit(ready)
        if admission is None:
            return
        self.checkpoint("after_openai_batch_admission", source)
        cursor = self.cursor(source)
        position = received = 0
        prefix = semantic_digest("openai-batch-prefix-v1", [source])
        target = cursor.position if cursor else 0
        buffer = bytearray()
        stream = self.adapter.content(file_id, size)
        eof = False
        try:
            while True:
                self.budget.remaining()
                while b"\n" in buffer or (eof and buffer):
                    if (
                        position >= target
                        and self.budget.result_rows
                        >= self.budget.options.max_result_rows
                    ):
                        self.fail_source(source, "collection_partial", stalls=0)
                        return
                    line, separator, tail = buffer.partition(b"\n")
                    if (
                        len(line) + len(separator)
                        > self.manifest.batch_context.limits.max_record_bytes
                        or not line.strip()
                    ):
                        self.fail_source(source, "source_framing")
                        return
                    raw = bytes(line)
                    screened = self.adapter.screen_result(raw, compiled)
                    if screened is None:
                        digest = semantic_digest(
                            "batch-withheld-v1", [source, position]
                        )
                    elif position < target:
                        digest = bytes_digest(screened[3])
                    else:
                        digest = self.save_member(
                            by_key[screened[0]], submission, source, position, raw
                        )
                    next_prefix = semantic_digest(
                        "batch-result-prefix-step-v1", [prefix, position, digest]
                    )
                    if position < target:
                        if (
                            position + 1 == target
                            and cursor is not None
                            and next_prefix != cursor.prefix_sha256
                        ):
                            self.fail_source(source, "source_conflict")
                            return
                    else:
                        if screened is None:
                            self.write_fact(
                                "withheld",
                                source + ":" + str(position),
                                BatchDiagnosticV1(code="unsafe_evidence"),
                            )
                        new_cursor = BatchCursorV1(
                            run_id=self.run_id,
                            submission_id=submission.submission_id,
                            source_id=source,
                            position=position + 1,
                            prefix_sha256=next_prefix,
                        )
                        self.write_fact("cursor", source, new_cursor)
                        self.checkpoint("after_openai_batch_cursor", source)
                        self.budget.result_rows += 1
                    position += 1
                    prefix = next_prefix
                    buffer = bytearray(tail)
                if eof:
                    if position < target:
                        self.fail_source(source, "source_conflict")
                        return
                    try:
                        final_metadata = await self.budget.call(
                            lambda: self.adapter.file_metadata(file_id)
                        )
                    except BatchEvidenceWithheld:
                        self.fail_source(source, "source_conflict")
                        return
                    if final_metadata != metadata:
                        self.fail_source(source, "source_conflict")
                        return
                    self.write_fact(
                        "cursor",
                        source,
                        BatchCursorV1(
                            run_id=self.run_id,
                            submission_id=submission.submission_id,
                            source_id=source,
                            position=position,
                            prefix_sha256=prefix,
                            exhausted=True,
                        ),
                    )
                    self.fail_source(source, "source_exhausted", stalls=0)
                    return
                if (
                    position < target
                    and self.budget.options.max_io_steps - self.budget.steps
                    <= 32 + 2 * ceil(size / 65_536) + 2
                ):
                    raise BudgetExhausted
                try:
                    chunk = await self.budget.call(lambda: anext(stream))
                except StopAsyncIteration:
                    if received != size:
                        self.fail_source(source, "source_underrun")
                        return
                    eof = True
                    continue
                received += len(chunk)
                if (
                    position >= target
                    and self.budget.result_rows >= self.budget.options.max_result_rows
                    and chunk
                ):
                    self.fail_source(source, "collection_partial", stalls=0)
                    return
                buffer.extend(chunk)
                if (
                    len(buffer) > self.manifest.batch_context.limits.max_record_bytes
                    and b"\n" not in buffer
                ):
                    self.fail_source(source, "source_framing")
                    return
        except BatchSourceOverrun:
            self.fail_source(source, "source_overrun")
        except (BudgetExhausted, TimeoutError, OSError):
            cursor = self.cursor(source)
            advanced = cursor is not None and (
                cursor.position > admission.frontier or cursor.exhausted
            )
            stalls = 0 if advanced else admission.stalls
            self.fail_source(
                source,
                "collection_partial" if stalls < 3 else "collection_no_progress",
                stalls=stalls,
            )
        finally:
            from typing import cast

            from judgmentllm.lifecycle import _close_adapter  # pyright: ignore[reportPrivateUsage]

            await _close_adapter(cast(Any, stream))
