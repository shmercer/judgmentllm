"""Bounded execution and evidence-based recovery; internal Subphase 3 services.

All preparation/repository/receipt operations live on one dedicated I/O thread.
The event loop holds only bounded queues and a fixed set of provider workers.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from judgmentllm import gemini_types, vertex_types
from judgmentllm.config import RunSpecification
from judgmentllm.evidence import ReportedUsageInput
from judgmentllm.evidence.usage import InvocationFailureCode
from judgmentllm.execution.durable_evidence import DurableEvidence
from judgmentllm.execution.processing import Result, interpret, unsuccessful
from judgmentllm.execution.retry import (
    DEFAULT_RETRY_POLICY,
    authorize_automatic_retry,
    automatic_retry_id,
    prepare_subset_retry,
)
from judgmentllm.output_commitments import validate_output_commitment
from judgmentllm.persistence.hashing import (
    bytes_digest,
    semantic_digest,
)
from judgmentllm.persistence.models import (
    AttemptRecord,
    ExecutionSummary,
    FailureEvidence,
    IntegrityError,
    ObservationRecord,
    RequestRecord,
    SubmissionRecord,
    TaskRecord,
    utc_now,
)
from judgmentllm.persistence.ownership import ExecutorOwnership
from judgmentllm.persistence.receipts import (
    AcknowledgmentReceipt,
    ResponseReceipt,
    publish_acknowledgment,
    read_acknowledgment,
    read_receipt,
)
from judgmentllm.persistence.repository import Repository
from judgmentllm.planning import stable_id
from judgmentllm.preparation import PreparedRun, RequestDependency, open_prepared_run
from judgmentllm.prompts import RenderedPrompt, RenderedPromptV2
from judgmentllm.providers import (
    PreparedRequest,
    PreparedRequestV2,
    ProviderAcknowledgment,
    ProviderAdapter,
    ProviderError,
    ProviderResponse,
)

Fault = Callable[[str, str], None]


@dataclass(frozen=True)
class ExecutionWork:
    dependency: RequestDependency
    prepared: PreparedRequest
    attempt_id: str
    submission_id: str
    correlation_id: str


class ExecutionSession:
    def __init__(
        self,
        run_id: str,
        store: str | Path,
        adapter: ProviderAdapter,
        spec: RunSpecification | None,
        use_frozen_snapshot: bool,
        fault: Fault | None,
    ) -> None:
        self.run_id = run_id
        self.adapter = adapter
        self.fault = fault
        self.retry_policy = DEFAULT_RETRY_POLICY
        self.bootstrap = Repository(store)
        self.ownership = ExecutorOwnership(self.bootstrap, run_id)
        self.prepared: PreparedRun | None = None
        try:
            owner = self.ownership.acquire()
            self.token = owner.token
            run = self.bootstrap.get_run(run_id)
            profile = adapter.capabilities_for(run.provider, run.endpoint, run.model)
            self.prepared = open_prepared_run(
                run_id,
                store=store,
                spec=spec,
                capabilities=profile,
                use_frozen_snapshot=use_frozen_snapshot,
                owner_token=self.token,
                transport_context=getattr(adapter, "transport_context", None),
            )
            self.repository = self.prepared.repository
            self.spec = self.prepared.spec
            self.evidence = DurableEvidence(self)
            policy = getattr(self.prepared.manifest, "service_policy", None)
            self.tier_review_required = False
            if policy is not None:
                for receipt in self.repository.iter_evidence(run_id):
                    tier = receipt.service_tier
                    if tier is not None:
                        if tier.outcome == "unexpected":
                            raise IntegrityError(
                                "served tier conflicts with the sealed service policy"
                            )
                        self.tier_review_required |= tier.requires_tier_review
            adapter.validate(self.spec)
            self.ordinal = self.repository.get_execution_checkpoint(run_id)
            self.dependencies: Iterator[RequestDependency] = (
                self.prepared.iter_request_dependencies(start_ordinal=self.ordinal)
            )
            self.existing = self.repository.iter_attempts(run_id)
            self.cancelled = run.state == "cancelled"
            self.task_ordinal = int(
                self.repository.connection.execute(
                    "SELECT COALESCE(MAX(scheduling_ordinal)+1,0) FROM tasks WHERE run_id=?",
                    (run_id,),
                ).fetchone()[0]
            )
            self.concurrency = self.spec.execution.concurrency
            if self.cancelled:
                self.cancel()
            with self.transaction():
                state = self.repository.get_run(run_id).state
                if state == "submitted":
                    self.repository.transition("run", run_id, run_id, "running")
                elif state in {"partial", "failed"}:
                    self.repository.transition("run", run_id, run_id, "running")
                elif state == "completed":
                    pending = self.repository.connection.execute(
                        "SELECT authorization_id FROM attempts WHERE run_id=? AND state='prepared' LIMIT 1",
                        (run_id,),
                    ).fetchone()
                    if pending is not None:
                        self.repository.transition(
                            "run",
                            run_id,
                            run_id,
                            "running",
                            authorization_id=pending[0],
                        )
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        try:
            if self.prepared is not None:
                self.prepared.close()
        finally:
            try:
                self.ownership.release()
            finally:
                self.bootstrap.close()

    def cancel(self) -> ExecutionSummary:
        from judgmentllm.execution.cancellation import cancel_owned

        self.cancelled = True
        return cancel_owned(self.repository, self.run_id, self.token)

    def transaction(self) -> AbstractContextManager[None]:
        return self.repository.execution_transaction(self.run_id, self.token)

    def checkpoint(self, stage: str, request_uid: str = "") -> None:
        if self.fault is not None:
            self.fault(stage, request_uid)

    def verify(self) -> None:
        """Recheck the durable seal and current restrictions before more work."""
        assert self.prepared is not None
        reference = self.repository.get_manifest(self.run_id)
        raw = self.prepared.store.read(reference.artifact)
        fixed = self.prepared.store.root / "manifest.json"
        try:
            valid = not fixed.is_symlink() and fixed.read_bytes() == raw
        except OSError:
            valid = False
        if not valid or bytes_digest(raw) != self.prepared.manifest_sha256:
            raise IntegrityError("sealed manifest changed during execution")
        current = self.adapter.capabilities(self.spec)
        self.prepared.verify_compatibility(capabilities=current)
        self.prepared.capabilities = current
        self.adapter.validate(self.spec)

    def prepare_attempt(
        self, dependency: RequestDependency, attempt_id: str, *, initial: bool = False
    ) -> PreparedRequest:
        """Reconstruct pinned V3 bytes; historical rendering keeps its old path."""
        assert self.prepared is not None
        if self.prepared.manifest.schema_version in (3, 4, 5, 6, 7):
            from judgmentllm.transport import physical_identity

            if (
                getattr(self.adapter, "transport_context", None)
                != self.prepared.transport_context
            ):
                raise IntegrityError("transport runtime context changed")
            return self.prepared.prepare_transport_attempt(
                dependency,
                physical_identity(
                    self.run_id, dependency.request.request_uid, attempt_id
                ),
            )
        rendered = (
            self.prepared.render(dependency)
            if initial
            else self.prepared.render_attempt(dependency)
        )
        if type(rendered) not in (RenderedPrompt, RenderedPromptV2):
            raise IntegrityError("historical attempt acquired native rendering")
        return self.adapter.prepare(self.spec, dependency, rendered)

    def verify_native_attempt(self, attempt: AttemptRecord) -> PreparedRequest:
        """Check terminal and pending native attempts against their original seal."""
        assert self.prepared is not None
        from judgmentllm.execution.retry import subset_dependency

        dependency = subset_dependency(
            self.prepared.dependency_for_request(attempt.request_uid),
            self.repository.attempt_task_ids(self.run_id, attempt.attempt_id),
        )
        wire = self.prepare_attempt(dependency, attempt.attempt_id)
        if (
            wire.prompt_sha256 != attempt.prompt_sha256
            or wire.payload_sha256 != attempt.payload_sha256
            or wire.task_ids
            != self.repository.attempt_task_ids(self.run_id, attempt.attempt_id)
            or attempt.membership_sha256
            != semantic_digest("attempt-membership", list(wire.task_ids))
        ):
            raise IntegrityError("native attempt differs from its commitment")
        return wire

    def initial_submission(
        self, attempt: AttemptRecord, prepared: PreparedRequest
    ) -> None:
        """Create intent only for independently verified pre-seal initial work."""
        assert self.prepared is not None
        from judgmentllm.anthropic_transport import PreparedRequestV5
        from judgmentllm.production_transport import PreparedRequestV4
        from judgmentllm.transport import PreparedRequestV3, physical_identity

        if not isinstance(
            prepared,
            (
                PreparedRequestV3,
                PreparedRequestV4,
                PreparedRequestV5,
                vertex_types.PreparedRequestV7,
                gemini_types.PreparedRequestV6,
            ),
        ):
            raise IntegrityError("initial native preparation is unavailable")
        receipt = self.prepared.transport_for_attempt(attempt.attempt_id)
        identity = physical_identity(
            self.run_id, attempt.request_uid, attempt.attempt_id
        )
        from judgmentllm.persistence.evidence import read_evidence_receipt

        if (
            attempt.attempt_number != 1
            or attempt.authorization_id is not None
            or attempt.state != "prepared"
            or receipt.parent_commitment_sha256 is not None
            or receipt != prepared.transport_commitment
            or self.repository.get_submission_for_attempt(
                self.run_id, attempt.attempt_id
            )
            is not None
            or self.repository.get_results_for_attempt(self.run_id, attempt.attempt_id)
            or read_acknowledgment(self.prepared.store, identity.submission_id)
            is not None
            or read_receipt(self.prepared.store, identity.submission_id) is not None
            or read_evidence_receipt(self.prepared.store, identity.submission_id)
            is not None
            or self.repository.connection.execute(
                "SELECT 1 FROM observations WHERE run_id=? AND attempt_id=? LIMIT 1",
                (self.run_id, attempt.attempt_id),
            ).fetchone()
            is not None
        ):
            raise IntegrityError(
                "missing submission does not describe initial native intent"
            )
        # No submission can exist before sealing; all receipt/attempt checks above
        # precede this short owned transaction. This never repairs retry intent.
        with self.transaction():
            current = self.repository.get_attempt(self.run_id, attempt.attempt_id)
            if current != attempt:
                raise IntegrityError("initial native attempt changed")
            self.repository.create_submission(
                SubmissionRecord(
                    run_id=self.run_id,
                    attempt_id=attempt.attempt_id,
                    submission_id=identity.submission_id,
                    correlation_id=identity.correlation_id,
                    payload_sha256=prepared.payload_sha256,
                )
            )
        self.checkpoint("after_initial_submission", attempt.request_uid)

    def next_work(self) -> ExecutionWork | None:
        assert self.prepared is not None
        while True:
            existing = next(self.existing, None)
            if existing is not None:
                if self.prepared.manifest.schema_version in (3, 4, 5, 6, 7):
                    self.verify_native_attempt(existing)
                results = self.repository.get_results_for_attempt(
                    self.run_id, existing.attempt_id
                )
                if results:
                    self.repair_committed(existing, results[0].status)
                    continue
                if existing.state in {
                    "not_submitted",
                    "cancelled",
                    "errored",
                    "normalized",
                }:
                    continue
                if self.cancelled and existing.state in {"prepared", "claimed"}:
                    continue
                if (
                    existing.next_eligible_at is not None
                    and existing.next_eligible_at > utc_now()
                ):
                    continue
                dependency = self.prepared.dependency_for_request(existing.request_uid)
                task_ids = self.repository.attempt_task_ids(
                    self.run_id, existing.attempt_id
                )
                from judgmentllm.execution.retry import subset_dependency

                dependency = subset_dependency(dependency, task_ids)
                ordinal = self.repository.get_request(
                    self.run_id, existing.request_uid
                ).scheduling_ordinal
                attempt_id = existing.attempt_id
            else:
                if self.cancelled:
                    return None
                dependency = next(self.dependencies, None)
                if dependency is None:
                    return None
                if self.prepared.manifest.schema_version in (3, 4, 5, 6, 7):
                    raise IntegrityError("sealed native request was scheduled twice")
                ordinal = self.ordinal
                self.ordinal += 1
                attempt_id = stable_id(
                    "attempt", self.run_id, dependency.request.request_uid, 1
                )
            request = dependency.request
            self.checkpoint("before_request_creation", request.request_uid)
            self.verify()
            prepared = self.prepare_attempt(
                dependency, attempt_id, initial=existing is None
            )
            if (
                prepared.task_ids != tuple(t.task_id for t in request.tasks)
                or prepared.request_uid != request.request_uid
                or prepared.dependency_sha256 != dependency.dependency_sha256
                or (prepared.provider, prepared.endpoint, prepared.model)
                != (self.spec.provider, self.spec.endpoint, self.spec.model)
                or prepared.generation != self.spec.generation
            ):
                raise IntegrityError(
                    "prepared request differs from the frozen dependency"
                )
            submission_id = stable_id("submission", self.run_id, attempt_id)
            correlation_id = stable_id("correlation", self.run_id, submission_id)
            work = ExecutionWork(
                dependency, prepared, attempt_id, submission_id, correlation_id
            )
            if existing is None:
                prompt = self.prepared.render(dependency).text.encode("utf-8")
                if (
                    bytes_digest(prompt) != prepared.prompt_sha256
                    or bytes_digest(prepared.payload) != prepared.payload_sha256
                ):
                    raise IntegrityError("adapter byte commitments do not match")
                store = self.prepared.store
                prompt_artifact = (
                    store.put(prompt, "text/plain")
                    if self.spec.capture == "full"
                    else store.hash_only(
                        prepared.prompt_sha256, len(prompt), "text/plain"
                    )
                )
                payload_artifact = (
                    store.put(prepared.payload, "application/json")
                    if self.spec.capture == "full"
                    else store.hash_only(
                        prepared.payload_sha256,
                        len(prepared.payload),
                        "application/json",
                    )
                )
                self.checkpoint("after_preparation_artifacts", request.request_uid)
                with self.transaction():
                    self.repository._register_artifact(self.run_id, prompt_artifact)  # pyright: ignore[reportPrivateUsage]
                    self.repository._register_artifact(self.run_id, payload_artifact)  # pyright: ignore[reportPrivateUsage]
                    for task in request.tasks:
                        self.repository.create_task(
                            TaskRecord(
                                run_id=self.run_id,
                                scheduling_ordinal=self.task_ordinal,
                                **task.model_dump(exclude={"schema_version"}),
                            )
                        )
                        self.task_ordinal += 1
                    self.repository.create_request(
                        RequestRecord(
                            run_id=self.run_id,
                            request_uid=request.request_uid,
                            scheduling_ordinal=ordinal,
                            dependency_sha256=dependency.dependency_sha256,
                        ),
                        prepared.task_ids,
                    )
                    self.repository.create_attempt(
                        AttemptRecord(
                            run_id=self.run_id,
                            attempt_id=attempt_id,
                            request_uid=request.request_uid,
                            attempt_number=1,
                            prompt_sha256=prepared.prompt_sha256,
                            payload_sha256=prepared.payload_sha256,
                            membership_sha256=semantic_digest(
                                "attempt-membership", list(prepared.task_ids)
                            ),
                        ),
                        prepared.task_ids,
                    )
                    self.repository.create_submission(
                        SubmissionRecord(
                            run_id=self.run_id,
                            submission_id=submission_id,
                            correlation_id=correlation_id,
                            payload_sha256=prepared.payload_sha256,
                            attempt_id=attempt_id,
                        )
                    )
                    self.repository.transition(
                        "request", self.run_id, request.request_uid, "prepared"
                    )
                    self.repository.advance_execution_checkpoint(
                        self.run_id, ordinal + 1, owner_token=self.token
                    )
                self.checkpoint("after_preparation", request.request_uid)
            else:
                submission = self.repository.get_submission_for_attempt(
                    self.run_id, attempt_id
                )
                if submission is None and self.prepared.manifest.schema_version in (
                    3,
                    4,
                    5,
                    6,
                    7,
                ):
                    self.initial_submission(existing, prepared)
                    submission = self.repository.get_submission_for_attempt(
                        self.run_id, attempt_id
                    )
                if (
                    existing.prompt_sha256 != prepared.prompt_sha256
                    or existing.payload_sha256 != prepared.payload_sha256
                    or self.repository.get_request(
                        self.run_id, request.request_uid
                    ).dependency_sha256
                    != dependency.dependency_sha256
                    or self.repository.attempt_task_ids(self.run_id, attempt_id)
                    != prepared.task_ids
                    or submission is None
                    or submission.submission_id != submission_id
                    or submission.correlation_id != correlation_id
                    or submission.payload_sha256 != prepared.payload_sha256
                ):
                    raise IntegrityError("existing preparation commitments differ")
                for digest in (existing.prompt_sha256, existing.payload_sha256):
                    self.prepared.store.verify(
                        self.repository.get_artifact(self.run_id, digest)
                    )
            return work

    def repair_committed(self, attempt: AttemptRecord, status: str) -> None:
        """Committed records are authoritative, including a lost final report."""
        assert self.prepared is not None
        if self.prepared.manifest.schema_version in (3, 4, 5, 6, 7):
            self.verify_native_attempt(attempt)
        for digest in (attempt.prompt_sha256, attempt.payload_sha256):
            self.prepared.store.verify(
                self.repository.get_artifact(self.run_id, digest)
            )
        submission = self.repository.get_submission_for_attempt(
            self.run_id, attempt.attempt_id
        )
        if submission is None:
            raise IntegrityError("committed result has no submission")
        observation = self.repository.get_observation_for_submission(
            self.run_id, submission.submission_id
        )
        if observation is not None:
            acknowledgment = read_acknowledgment(
                self.prepared.store, submission.submission_id
            )
            receipt = read_receipt(self.prepared.store, submission.submission_id)
            if acknowledgment is None or receipt is None:
                raise IntegrityError(
                    "committed provider result is missing receipt evidence"
                )
            acknowledgment.verify_submission(submission)
            receipt.verify_submission(submission)
            if self.repository.evidence_supported:
                evidence = self.repository.get_evidence_for_submission(
                    self.run_id, submission.submission_id
                )
                if (
                    evidence is None
                    or evidence.authorization != self.evidence.authorization
                    or evidence.authorization_sha256
                    != self.evidence.authorization_sha256
                ):
                    raise IntegrityError(
                        "committed observation lacks matching evidence"
                    )
            if (
                receipt.observation() != observation
                or acknowledgment.observation_key != observation.observation_key
            ):
                raise IntegrityError(
                    "committed observation conflicts with receipt evidence"
                )
        if observation is None and self.repository.evidence_supported:
            evidence = self.repository.get_evidence_for_submission(
                self.run_id, submission.submission_id
            )
            if evidence is None or evidence.identity.observation_id is not None:
                raise IntegrityError("committed failure lacks evidence")
        results = self.repository.get_results_for_attempt(
            self.run_id, attempt.attempt_id
        )
        if tuple(r.task_id for r in results) != self.repository.attempt_task_ids(
            self.run_id, attempt.attempt_id
        ):
            raise IntegrityError("committed group membership differs from attempt")

    def boundary(self, work: ExecutionWork) -> bool:
        """Return true only for safe prepared/claimed work; reconcile all other work."""
        assert self.prepared is not None
        attempt = self.repository.get_attempt(self.run_id, work.attempt_id)
        if self.cancelled or attempt.state not in {"prepared", "claimed"}:
            return False
        if self.tier_review_required:
            raise ProviderError(
                "served tier is unknown; further submission requires review"
            )
        with self.transaction():
            if attempt.state == "claimed":
                self.repository.transition(
                    "attempt", self.run_id, work.attempt_id, "prepared"
                )
            self.repository.transition(
                "attempt", self.run_id, work.attempt_id, "claimed"
            )
            request = self.repository.get_request(
                self.run_id, work.prepared.request_uid
            )
            if request.state in {
                "prepared",
                "partial",
                "failed",
                "blocked",
                "completed",
                "cancelled",
            }:
                self.repository.transition(
                    "request",
                    self.run_id,
                    request.request_uid,
                    "active",
                    authorization_id=attempt.authorization_id,
                )
            for task_id in work.prepared.task_ids:
                if self.repository.get_task(self.run_id, task_id).state != "active":
                    self.repository.transition(
                        "task",
                        self.run_id,
                        task_id,
                        "active",
                        authorization_id=attempt.authorization_id,
                    )
        self.checkpoint("after_claim", work.prepared.request_uid)
        self.verify()
        current = self.prepare_attempt(work.dependency, work.attempt_id)
        if current != work.prepared:
            raise IntegrityError("payload changed before submission")
        readiness = getattr(self.adapter, "validate_credentials", None)
        if readiness is not None:
            readiness()
        with self.transaction():
            self.repository.transition(
                "attempt", self.run_id, work.attempt_id, "submitting"
            )
            self.repository.transition(
                "submission", self.run_id, work.submission_id, "submitting"
            )
        self.checkpoint("after_submitting", work.prepared.request_uid)
        return True

    def acknowledge(
        self, work: ExecutionWork, acknowledgment: ProviderAcknowledgment
    ) -> None:
        assert self.prepared is not None
        receipt = AcknowledgmentReceipt(
            run_id=self.run_id,
            attempt_id=work.attempt_id,
            submission_id=work.submission_id,
            correlation_id=work.correlation_id,
            payload_sha256=work.prepared.payload_sha256,
            remote_id=acknowledgment.remote_id,
            observation_key=acknowledgment.observation_key,
        )
        receipt.verify_submission(
            self.repository.get_submission(self.run_id, work.submission_id)
        )
        publish_acknowledgment(self.prepared.store, receipt)
        self.checkpoint("after_acknowledgment_receipt", work.prepared.request_uid)
        with self.transaction():
            submission = self.repository.get_submission(self.run_id, work.submission_id)
            if submission.state in {"submitting", "uncertain"}:
                self.repository.transition(
                    "submission",
                    self.run_id,
                    work.submission_id,
                    "acknowledged",
                    remote_id=acknowledgment.remote_id,
                )
            elif submission.remote_id != acknowledgment.remote_id:
                raise IntegrityError("accepted remote identity conflicts")
            attempt = self.repository.get_attempt(self.run_id, work.attempt_id)
            if attempt.state in {"submitting", "uncertain"}:
                self.repository.transition(
                    "attempt", self.run_id, work.attempt_id, "acknowledged"
                )
        self.checkpoint("after_acknowledgment", work.prepared.request_uid)

    def cancelled_invocation(
        self,
        work: ExecutionWork,
        *,
        acknowledgment: ProviderAcknowledgment | None,
        response: ProviderResponse | None,
        known_invoked: bool,
    ) -> None:
        """Drain received OpenAI evidence without submitting or retrieving work."""
        if self.repository.get_results_for_attempt(self.run_id, work.attempt_id):
            return
        previous = self.evidence.restore(work)
        if previous is not None and previous.identity.observation_id is None:
            # A definitive failure or unresolved invocation is already durable;
            # cancellation never upgrades or overwrites those immutable facts.
            if previous.invocation_acceptance == "unresolved":
                self.uncertain(work)
            return
        if acknowledgment is not None and response is not None:
            self.acknowledge(work, acknowledgment)
            self.process(work, response)
        elif previous is None and known_invoked:
            self.uncertain(work, invoked=True)

    def uncertain(
        self,
        work: ExecutionWork,
        *,
        invoked: bool = False,
        usage: ReportedUsageInput | None = None,
    ) -> None:
        evidence = self.evidence.uncertain(work, usage, known_invoked=invoked)
        verified = (
            self.repository.prepare_evidence_commit(evidence)
            if evidence is not None
            else None
        )
        with self.transaction():
            attempt = self.repository.get_attempt(self.run_id, work.attempt_id)
            submission = self.repository.get_submission(self.run_id, work.submission_id)
            if submission.state in {"submitting", "acknowledged"}:
                self.repository.transition(
                    "submission", self.run_id, work.submission_id, "uncertain"
                )
            if attempt.state in {"submitting", "acknowledged"}:
                self.repository.transition(
                    "attempt", self.run_id, work.attempt_id, "uncertain"
                )
            self.block(work)
            if evidence is not None:
                self.repository.record_evidence(
                    evidence, owner_token=self.token, prepared=verified
                )

    def block(self, work: ExecutionWork) -> None:
        request_uid = work.prepared.request_uid
        for task_id in work.prepared.task_ids:
            if self.repository.get_task(self.run_id, task_id).state == "active":
                self.repository.transition("task", self.run_id, task_id, "blocked")
        if self.repository.get_request(self.run_id, request_uid).state == "active":
            self.repository.transition("request", self.run_id, request_uid, "blocked")

    def not_submitted(
        self,
        work: ExecutionWork,
        *,
        invoked: bool,
        transient: bool = False,
        retry_after: float | None = None,
        usage: ReportedUsageInput | None = None,
        diagnostic_code: InvocationFailureCode | None = None,
    ) -> None:
        evidence = (
            self.evidence.failure(
                work,
                usage,
                transient=transient,
                retry_after=retry_after,
                diagnostic_code=diagnostic_code,
            )
            if invoked
            else None
        )
        if evidence is not None:
            transient, retry_after = (
                evidence.failure_transient,
                evidence.failure_retry_after,
            )
        verified = (
            self.repository.prepare_evidence_commit(evidence)
            if evidence is not None
            else None
        )
        with self.transaction():
            self.repository.transition(
                "submission", self.run_id, work.submission_id, "confirmed_not_submitted"
            )
            self.repository.transition(
                "attempt", self.run_id, work.attempt_id, "not_submitted"
            )
            if invoked:
                self.repository.record_failure(
                    FailureEvidence(
                        run_id=self.run_id,
                        attempt_id=work.attempt_id,
                        submission_id=work.submission_id,
                        kind="transport_not_submitted",
                    )
                )
                if evidence is not None:
                    self.repository.record_evidence(
                        evidence, owner_token=self.token, prepared=verified
                    )
                results: list[Result] = []
                from judgmentllm.execution.retry import subset_dependency

                for task_id in work.prepared.task_ids:
                    result = unsuccessful(
                        self.spec,
                        subset_dependency(work.dependency, (task_id,)),
                        self.run_id,
                        work.attempt_id,
                        "errored",
                        "transport_not_submitted",
                    )
                    results.append(self.provenance(result, work, None))
                self.repository.commit_results(
                    self.run_id,
                    work.attempt_id,
                    results,
                    failure_submission_id=work.submission_id,
                )
                if transient and not self.cancelled:
                    authorize_automatic_retry(
                        self.repository,
                        self.repository.get_attempt(self.run_id, work.attempt_id),
                        owner_token=self.token,
                        policy=self.retry_policy,
                        retry_after=retry_after,
                    )
                    self.checkpoint(
                        "after_automatic_authorization", work.prepared.request_uid
                    )
            else:
                self.block(work)

    def retry_delay(self) -> float | None:
        """Prepare durable authorizations, then expose the nearest eligibility.

        Called after workers drain, never while holding a worker or transaction.
        Each iteration holds only one original group and its exact subset.
        """
        assert self.prepared is not None
        if self.cancelled:
            return None
        for prior in self.repository.iter_attempts(self.run_id):
            if prior.state != "not_submitted" or prior.next_eligible_at is None:
                continue
            operation_id = automatic_retry_id(prior)
            authorized = self.repository.connection.execute(
                "SELECT 1 FROM retry_operations WHERE run_id=? AND operation_id=?",
                (self.run_id, operation_id),
            ).fetchone()
            exists = self.repository.connection.execute(
                "SELECT 1 FROM attempts WHERE run_id=? AND authorization_id=?",
                (self.run_id, operation_id),
            ).fetchone()
            if authorized is not None and exists is None:
                prepare_subset_retry(
                    self.prepared,
                    self.adapter,
                    dependency=self.prepared.dependency_for_request(prior.request_uid),
                    task_ids=self.repository.attempt_task_ids(
                        self.run_id, prior.attempt_id
                    ),
                    operation_id=operation_id,
                    owner_token=self.token,
                    policy=self.retry_policy,
                    next_eligible_at=prior.next_eligible_at,
                    fault=self.fault,
                )
        row = self.repository.connection.execute(
            "SELECT MIN(next_eligible_at) FROM attempts WHERE run_id=? AND state='prepared' AND next_eligible_at IS NOT NULL",
            (self.run_id,),
        ).fetchone()
        if row is None or row[0] is None:
            return None
        instant = datetime.fromisoformat(cast(str, row[0]))
        return max(0.0, (instant - datetime.now(UTC)).total_seconds())

    def restart_retry_wave(self) -> None:
        self.existing = (
            attempt
            for attempt in self.repository.iter_attempts(self.run_id)
            if attempt.state == "prepared"
        )

    def saved_acknowledgment(
        self, work: ExecutionWork
    ) -> ProviderAcknowledgment | None:
        assert self.prepared is not None
        receipt = read_acknowledgment(self.prepared.store, work.submission_id)
        if receipt is None:
            return None
        receipt.verify_submission(
            self.repository.get_submission(self.run_id, work.submission_id)
        )
        return ProviderAcknowledgment(
            remote_id=receipt.remote_id, observation_key=receipt.observation_key
        )

    def saved_response(self, work: ExecutionWork) -> ProviderResponse | None:
        assert self.prepared is not None
        evidence = self.evidence.restore(work)
        if evidence is not None and evidence.identity.observation_id is None:
            if evidence.invocation_acceptance == "unresolved":
                self.uncertain(work)
                return None
            self.not_submitted(work, invoked=True)
            return None
        receipt = read_receipt(self.prepared.store, work.submission_id)
        if evidence is not None and evidence.response_retention == "omitted":
            return None
        if receipt is None:
            return None
        self.verify_receipt(work, receipt)
        self.verify_observation_key(work, receipt.observation_key)
        if receipt.capture == "minimal":
            return None
        return ProviderResponse(
            observation_key=receipt.observation_key,
            kind=receipt.kind,
            raw=self.prepared.store.read(receipt.raw),
        )

    def has_results(self, work: ExecutionWork) -> bool:
        return bool(
            self.repository.get_results_for_attempt(self.run_id, work.attempt_id)
        )

    def verify_receipt(self, work: ExecutionWork, receipt: ResponseReceipt) -> None:
        if (
            receipt.run_id,
            receipt.attempt_id,
            receipt.submission_id,
            receipt.correlation_id,
            receipt.payload_sha256,
            receipt.capture,
        ) != (
            self.run_id,
            work.attempt_id,
            work.submission_id,
            work.correlation_id,
            work.prepared.payload_sha256,
            self.spec.capture,
        ):
            raise IntegrityError("response receipt identity differs from submission")
        if receipt.observation_id != stable_id(
            "observation", work.submission_id, receipt.observation_key
        ):
            raise IntegrityError("response receipt observation identity conflicts")

    def verify_observation_key(self, work: ExecutionWork, key: str) -> None:
        assert self.prepared is not None
        acknowledgment = read_acknowledgment(self.prepared.store, work.submission_id)
        if acknowledgment is None:
            raise IntegrityError("accepted observation lacks acknowledgment identity")
        acknowledgment.verify_submission(
            self.repository.get_submission(self.run_id, work.submission_id)
        )
        if acknowledgment.observation_key != key:
            raise IntegrityError("observation differs from durable acknowledgment")

    def process(self, work: ExecutionWork, response: ProviderResponse) -> None:
        assert self.prepared is not None
        self.verify_observation_key(work, response.observation_key)
        self.checkpoint("after_response", work.prepared.request_uid)
        raw, receipt, evidence = self.evidence.prepare(work, response)
        verified = (
            self.repository.prepare_evidence_commit(evidence)
            if evidence is not None
            else None
        )
        observation = ObservationRecord(
            run_id=self.run_id,
            attempt_id=work.attempt_id,
            submission_id=work.submission_id,
            observation_id=receipt.observation_id,
            observation_key=receipt.observation_key,
            kind=receipt.kind,
            raw_sha256=raw.sha256,
            received_at=receipt.received_at,
        )
        with self.transaction():
            prior = self.repository.get_observation_for_submission(
                self.run_id, work.submission_id
            )
            if prior is None:
                self.evidence.register_response(raw)
                self.repository.record_observation(observation)
            elif prior != observation:
                raise IntegrityError("observation conflicts with recorded evidence")
            if evidence is not None:
                self.repository.record_evidence(
                    evidence, owner_token=self.token, prepared=verified
                )
                self.checkpoint(
                    "during_evidence_transaction", work.prepared.request_uid
                )
            if self.repository.get_attempt(self.run_id, work.attempt_id).state not in {
                "response_saved",
                "normalized",
            }:
                self.repository.transition(
                    "attempt", self.run_id, work.attempt_id, "response_saved"
                )
        self.checkpoint("after_evidence_commit", work.prepared.request_uid)
        needs_tier_review = False
        if evidence is not None and evidence.service_tier is not None:
            tier = evidence.service_tier
            if tier.outcome == "unexpected":
                raise IntegrityError(
                    "served tier conflicts with the sealed service policy"
                )
            needs_tier_review = tier.requires_tier_review
            self.tier_review_required |= needs_tier_review
        self.checkpoint("after_observation", work.prepared.request_uid)
        self.checkpoint("before_parsing", work.prepared.request_uid)
        try:
            parsed = (
                self.adapter.parse(response, strict_output=True)
                if self.spec.schema_version in (2, 3)
                or isinstance(work.prepared, PreparedRequestV2)
                else self.adapter.parse(response)
            )
        except ValueError:
            if response.kind != "response":
                raise IntegrityError(
                    "terminal provider evidence could not be parsed"
                ) from None
            parsed = None
        self.checkpoint("before_validation", work.prepared.request_uid)
        contract = None
        if self.prepared.manifest.schema_version in (3, 4, 5, 6, 7):
            from judgmentllm.anthropic_transport import PreparedRequestV5
            from judgmentllm.production_transport import PreparedRequestV4
            from judgmentllm.transport import PreparedRequestV3

            if not isinstance(
                work.prepared,
                (
                    PreparedRequestV3,
                    PreparedRequestV4,
                    PreparedRequestV5,
                    vertex_types.PreparedRequestV7,
                    gemini_types.PreparedRequestV6,
                ),
            ):
                raise IntegrityError("native execution requires its committed output")
            current = self.prepare_attempt(work.dependency, work.attempt_id)
            if current != work.prepared:
                raise IntegrityError("native commitment changed before interpretation")
            contract = work.prepared.output_commitment.contract
        if self.spec.schema_version == 2 or isinstance(
            work.prepared, PreparedRequestV2
        ):
            if not isinstance(work.prepared, PreparedRequestV2):
                raise IntegrityError(
                    "v2 execution requires a committed output contract"
                )
            commitment = validate_output_commitment(
                work.prepared.output_commitment, self.spec, work.dependency.request
            )
            # Verify the same detached representation still produces the committed bytes.
            current = self.prepare_attempt(work.dependency, work.attempt_id)
            if current != work.prepared:
                raise IntegrityError("output commitment changed before interpretation")
            contract = commitment.contract
        interpreted = interpret(
            parsed,
            self.spec,
            work.dependency,
            self.run_id,
            work.attempt_id,
            contract=contract,
            member_checkpoint=lambda: self.checkpoint(
                "during_member_validation", work.prepared.request_uid
            ),
        )
        results = tuple(
            self.provenance(
                result,
                work,
                None
                if raw.byte_length == 0
                or (evidence is not None and evidence.response_retention == "omitted")
                else raw.relative_path,
            )
            for result in interpreted.results
        )
        self.checkpoint("after_normalization", work.prepared.request_uid)
        with self.transaction():
            self.repository.commit_results(
                self.run_id,
                work.attempt_id,
                results,
                observation_id=observation.observation_id,
                unexpected_members=interpreted.unexpected_members,
                unattributable_members=interpreted.unattributable_members,
                checkpoint=lambda: self.checkpoint(
                    "during_result_transaction", work.prepared.request_uid
                ),
            )
        self.checkpoint("after_result", work.prepared.request_uid)
        if needs_tier_review:
            raise ProviderError(
                "served tier is unknown; further submission requires review"
            )

    def provenance(
        self, result: Result, work: ExecutionWork, raw_path: str | None
    ) -> Result:
        assert self.prepared is not None
        prompt = self.repository.get_artifact(self.run_id, work.prepared.prompt_sha256)
        data = result.model_dump()
        data.update(
            manifest_sha256=self.prepared.manifest_sha256,
            mode=self.spec.execution.mode,
            tier=self.spec.execution.tier,
            prompt_path=prompt.relative_path,
            raw_response_path=raw_path,
        )
        return type(result).model_validate(data)

    def finish(self) -> ExecutionSummary:
        self.checkpoint("before_final_status")
        with self.transaction():
            summary = self.repository.summary(self.run_id)
            if summary.state == "running":
                state = (
                    "completed"
                    if summary.logical_completed_tasks == summary.tasks
                    and not summary.unresolved_submissions
                    else (
                        "partial"
                        if summary.results or summary.unresolved_submissions
                        else "failed"
                    )
                )
                self.repository.transition("run", self.run_id, self.run_id, state)
        return self.repository.summary(self.run_id)
