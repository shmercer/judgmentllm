"""Bounded execution and evidence-based recovery; internal Subphase 3 services.

All preparation/repository/receipt operations live on one dedicated I/O thread.
The event loop holds only bounded queues and a fixed set of provider workers.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path

from judgmentllm.config import RunSpec
from judgmentllm.execution.processing import Result, normalize, unsuccessful
from judgmentllm.persistence.artifacts import ArtifactStore
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
    publish_receipt,
    read_acknowledgment,
    read_receipt,
)
from judgmentllm.persistence.repository import Repository
from judgmentllm.planning import stable_id
from judgmentllm.preparation import PreparedRun, RequestDependency, open_prepared_run
from judgmentllm.providers import (
    PreparedRequest,
    ProviderAcknowledgment,
    ProviderAdapter,
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
        spec: RunSpec | None,
        use_frozen_snapshot: bool,
        fault: Fault | None,
    ) -> None:
        self.run_id = run_id
        self.adapter = adapter
        self.fault = fault
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
            )
            self.repository = self.prepared.repository
            self.spec = self.prepared.spec
            adapter.validate(self.spec)
            if any(len(group) != 1 for group in self.prepared.manifest.trait_groups):
                raise IntegrityError("grouped execution is deferred to Subphase 4")
            self.dependencies: Iterator[RequestDependency] = (
                self.prepared.iter_request_dependencies()
            )
            self.ordinal = 0
            self.concurrency = self.spec.execution.concurrency
            with self.transaction():
                state = self.repository.get_run(run_id).state
                if state == "submitted":
                    self.repository.transition("run", run_id, run_id, "running")
                elif state in {"partial", "failed"}:
                    self.repository.transition("run", run_id, run_id, "running")
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

    def next_work(self) -> ExecutionWork | None:
        assert self.prepared is not None
        while True:
            dependency = next(self.dependencies, None)
            if dependency is None:
                return None
            ordinal = self.ordinal
            self.ordinal += 1
            request = dependency.request
            attempt_id = stable_id("attempt", self.run_id, request.request_uid, 1)
            existing = self.repository.get_attempt_for_request(
                self.run_id, request.request_uid
            )
            if existing is not None:
                if existing.attempt_id != attempt_id:
                    raise IntegrityError(
                        "unexpected attempt identity in single-attempt executor"
                    )
                result = self.repository.get_result_for_attempt(self.run_id, attempt_id)
                if result is not None:
                    self.repair_committed(existing, result.status)
                    continue
                if existing.state == "not_submitted":
                    continue
            self.checkpoint("before_request_creation", request.request_uid)
            self.verify()
            prepared = self.adapter.prepare(
                self.spec, dependency, self.prepared.render(dependency)
            )
            if (
                len(prepared.task_ids) != 1
                or prepared.task_ids != tuple(t.task_id for t in request.tasks)
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
                    self.repository.register_artifact(self.run_id, prompt_artifact)
                    self.repository.register_artifact(self.run_id, payload_artifact)
                    for task in request.tasks:
                        self.repository.create_task(
                            TaskRecord(
                                run_id=self.run_id,
                                scheduling_ordinal=ordinal,
                                **task.model_dump(exclude={"schema_version"}),
                            )
                        )
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
            if (
                receipt.observation() != observation
                or acknowledgment.observation_key != observation.observation_key
            ):
                raise IntegrityError(
                    "committed observation conflicts with receipt evidence"
                )
        request = self.repository.get_request(self.run_id, attempt.request_uid)
        task_id = self.repository.attempt_task_ids(self.run_id, attempt.attempt_id)[0]
        task = self.repository.get_task(self.run_id, task_id)
        with self.transaction():
            if task.state in {"active", "blocked"}:
                self.repository.transition("task", self.run_id, task_id, status)
            if request.state in {"active", "blocked"}:
                self.repository.transition(
                    "request",
                    self.run_id,
                    request.request_uid,
                    "completed" if status == "complete" else "failed",
                )

    def boundary(self, work: ExecutionWork) -> bool:
        """Return true only for safe prepared/claimed work; reconcile all other work."""
        assert self.prepared is not None
        attempt = self.repository.get_attempt(self.run_id, work.attempt_id)
        if attempt.state not in {"prepared", "claimed"}:
            return False
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
            if request.state == "prepared":
                self.repository.transition(
                    "request", self.run_id, request.request_uid, "active"
                )
            task_id = work.prepared.task_ids[0]
            if self.repository.get_task(self.run_id, task_id).state == "pending":
                self.repository.transition("task", self.run_id, task_id, "active")
        self.checkpoint("after_claim", work.prepared.request_uid)
        self.verify()
        current = self.adapter.prepare(
            self.spec, work.dependency, self.prepared.render(work.dependency)
        )
        if current != work.prepared:
            raise IntegrityError("payload changed before submission")
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

    def uncertain(self, work: ExecutionWork) -> None:
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

    def block(self, work: ExecutionWork) -> None:
        task_id = work.prepared.task_ids[0]
        request_uid = work.prepared.request_uid
        if self.repository.get_task(self.run_id, task_id).state == "active":
            self.repository.transition("task", self.run_id, task_id, "blocked")
        if self.repository.get_request(self.run_id, request_uid).state == "active":
            self.repository.transition("request", self.run_id, request_uid, "blocked")

    def not_submitted(self, work: ExecutionWork, *, invoked: bool) -> None:
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
                result = unsuccessful(
                    self.spec,
                    work.dependency,
                    self.run_id,
                    work.attempt_id,
                    "errored",
                    "transport_not_submitted",
                )
                result = self.provenance(result, work, None)
                self.repository.append_result(
                    result, failure_submission_id=work.submission_id
                )
                self.repository.transition(
                    "task", self.run_id, work.prepared.task_ids[0], "errored"
                )
                self.repository.transition(
                    "request", self.run_id, work.prepared.request_uid, "failed"
                )
            else:
                self.block(work)

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
        receipt = read_receipt(self.prepared.store, work.submission_id)
        if receipt is None:
            return None
        self.verify_receipt(work, receipt)
        self.verify_observation_key(work, receipt.observation_key)
        if receipt.capture == "minimal":
            return None
        if receipt.kind == "confirmed_cancellation":
            raise IntegrityError("cancellation recovery is deferred")
        return ProviderResponse(
            observation_key=receipt.observation_key,
            kind=receipt.kind,
            raw=self.prepared.store.read(receipt.raw),
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
        raw = (
            self.prepared.store.put(response.raw, "application/json")
            if self.spec.capture == "full"
            else ArtifactStore.hash_only(
                bytes_digest(response.raw), len(response.raw), "application/json"
            )
        )
        existing = read_receipt(self.prepared.store, work.submission_id)
        receipt = ResponseReceipt(
            run_id=self.run_id,
            attempt_id=work.attempt_id,
            submission_id=work.submission_id,
            correlation_id=work.correlation_id,
            payload_sha256=work.prepared.payload_sha256,
            observation_id=stable_id(
                "observation", work.submission_id, response.observation_key
            ),
            observation_key=response.observation_key,
            kind=response.kind,
            raw=raw,
            capture=self.spec.capture,
            received_at=existing.received_at if existing else utc_now(),
        )
        self.verify_receipt(work, receipt)
        if existing is not None and existing != receipt:
            raise IntegrityError("received observation conflicts with durable receipt")
        publish_receipt(self.prepared.store, receipt)
        self.checkpoint("after_receipt", work.prepared.request_uid)
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
                self.repository.register_artifact(self.run_id, raw)
                self.repository.record_observation(observation)
            elif prior != observation:
                raise IntegrityError("observation conflicts with recorded evidence")
            if (
                self.repository.get_attempt(self.run_id, work.attempt_id).state
                != "response_saved"
            ):
                self.repository.transition(
                    "attempt", self.run_id, work.attempt_id, "response_saved"
                )
        self.checkpoint("after_observation", work.prepared.request_uid)
        self.checkpoint("before_parsing", work.prepared.request_uid)
        try:
            parsed = self.adapter.parse(response)
        except ValueError:
            if response.kind != "response":
                raise IntegrityError(
                    "terminal provider evidence could not be parsed"
                ) from None
            parsed = None
        self.checkpoint("before_validation", work.prepared.request_uid)
        try:
            result = (
                normalize(
                    parsed, self.spec, work.dependency, self.run_id, work.attempt_id
                )
                if parsed is not None
                else unsuccessful(
                    self.spec,
                    work.dependency,
                    self.run_id,
                    work.attempt_id,
                    "invalid",
                    "invalid_output",
                )
            )
        except ValueError:
            if response.kind != "response":
                raise IntegrityError(
                    "terminal provider evidence could not be validated"
                ) from None
            result = unsuccessful(
                self.spec,
                work.dependency,
                self.run_id,
                work.attempt_id,
                "invalid",
                "invalid_output",
            )
        result = self.provenance(result, work, raw.relative_path)
        self.checkpoint("after_normalization", work.prepared.request_uid)
        with self.transaction():
            self.repository.append_result(
                result, observation_id=observation.observation_id
            )
            self.checkpoint("during_result_transaction", work.prepared.request_uid)
            self.repository.transition(
                "attempt", self.run_id, work.attempt_id, "normalized"
            )
            self.repository.transition(
                "submission", self.run_id, work.submission_id, "resolved"
            )
            self.repository.transition(
                "task", self.run_id, work.prepared.task_ids[0], result.status
            )
            self.repository.transition(
                "request",
                self.run_id,
                work.prepared.request_uid,
                "completed" if result.status == "complete" else "failed",
            )
        self.checkpoint("after_result", work.prepared.request_uid)

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
        with self.transaction():
            summary = self.repository.summary(self.run_id)
            if summary.state == "running":
                state = (
                    "completed"
                    if summary.successful_results == summary.tasks
                    and not summary.unresolved_submissions
                    else (
                        "partial"
                        if summary.results or summary.unresolved_submissions
                        else "failed"
                    )
                )
                self.repository.transition("run", self.run_id, self.run_id, state)
        return self.repository.summary(self.run_id)
