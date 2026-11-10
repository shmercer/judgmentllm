"""Explicit, provider-free retry authorization and immutable subset preparation.

No function in this module submits or retrieves provider work. Authorization,
the exact attempt subset, and submission intent commit together, so a crash
cannot leave an authorization whose membership must later be guessed.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, cast

from pydantic import Field

from judgmentllm.config import RunSpecification
from judgmentllm.persistence.hashing import (
    bytes_digest,
    canonical_bytes,
    semantic_digest,
)
from judgmentllm.persistence.models import (
    AttemptRecord,
    ExecutionModel,
    IntegrityError,
    RetryAuthorization,
    RetryAuthorizationError,
    SubmissionRecord,
)
from judgmentllm.persistence.ownership import ExecutorOwnership
from judgmentllm.persistence.repository import Repository
from judgmentllm.planning import stable_id
from judgmentllm.preparation import PreparedRun, RequestDependency, open_prepared_run
from judgmentllm.prompts import RenderedPrompt, RenderedPromptV2
from judgmentllm.providers import ProviderAdapter


class RetryPolicy(ExecutionModel):
    """Bounded physical submissions; backoff is computed once then persisted."""

    max_attempts: int = Field(default=3, ge=1, le=3)
    initial_backoff: float = Field(default=1.0, ge=0, allow_inf_nan=False)
    max_backoff: float = Field(default=30.0, ge=0, le=30, allow_inf_nan=False)
    jitter: Literal["none", "full"] = "none"

    def delay(
        self,
        attempt_number: int,
        *,
        retry_after: float | None = None,
        jitter_fraction: float = 1.0,
    ) -> float:
        """Delay after a failed physical attempt, honoring authoritative retry-after."""
        if type(attempt_number) is not int or attempt_number < 1:
            raise ValueError("attempt number must be positive")
        if not math.isfinite(jitter_fraction) or not 0 <= jitter_fraction <= 1:
            raise ValueError("jitter fraction must lie between zero and one")
        if retry_after is not None and (
            not math.isfinite(retry_after) or retry_after < 0
        ):
            raise ValueError("retry-after must be finite and nonnegative")
        delay = min(
            self.max_backoff, self.initial_backoff * 2 ** min(attempt_number - 1, 32)
        )
        if self.jitter == "full":
            delay *= jitter_fraction
        return max(delay, retry_after or 0.0)

    def eligible_at(
        self,
        attempt_number: int,
        *,
        now: datetime | None = None,
        retry_after: float | None = None,
        jitter_fraction: float = 1.0,
    ) -> str:
        instant = now or datetime.now(UTC)
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError("retry clock must have a timezone")
        return (
            instant.astimezone(UTC)
            + timedelta(
                seconds=self.delay(
                    attempt_number,
                    retry_after=retry_after,
                    jitter_fraction=jitter_fraction,
                )
            )
        ).isoformat()


def automatic_retry_id(attempt: AttemptRecord) -> str:
    """One authorization identity for each definitively unaccepted prior attempt."""
    return stable_id("automatic-retry", attempt.run_id, attempt.attempt_id)


DEFAULT_RETRY_POLICY = RetryPolicy()


def _no_checkpoint(_stage: str, _request: str) -> None:
    pass


def subset_dependency(
    dependency: RequestDependency, task_ids: Sequence[str]
) -> RequestDependency:
    """Keep the logical request identity and select its original ordered members."""
    selected = tuple(task_ids)
    if not selected or len(set(selected)) != len(selected):
        raise RetryAuthorizationError("retry selection requires unique task IDs")
    membership = tuple(t.task_id for t in dependency.request.tasks)
    if any(task_id not in membership for task_id in selected):
        raise RetryAuthorizationError("retry task is outside the original request")
    tasks = tuple(t for t in dependency.request.tasks if t.task_id in selected)
    # Callers may supply IDs in arbitrary order; wire membership always follows
    # the immutable original request, never selector or response array order.
    return RequestDependency.model_validate(
        dependency.model_copy(
            update={
                "request": dependency.request.model_copy(
                    update={
                        "tasks": tasks,
                        "trait_ids": tuple(t.trait_id for t in tasks),
                    }
                )
            }
        ).model_dump()
    )


class PriorOutcome(ExecutionModel):
    """Content-free exact prior evidence committed by an authorization digest."""

    task_id: str
    attempt_id: str
    result_sha256: str
    status: str


def _prior_outcomes(
    repository: Repository,
    run_id: str,
    request_uid: str,
    task_ids: tuple[str, ...],
    before_attempt: int,
) -> tuple[PriorOutcome, ...]:
    outcomes: list[PriorOutcome] = []
    for task_id in task_ids:
        row = repository.connection.execute(
            "SELECT r.attempt_id,r.record_sha256,r.status FROM results r JOIN attempts a "
            "ON a.run_id=r.run_id AND a.attempt_id=r.attempt_id "
            "WHERE r.run_id=? AND r.request_uid=? AND r.task_id=? AND a.attempt_number<? "
            "ORDER BY a.attempt_number DESC LIMIT 1",
            (run_id, request_uid, task_id, before_attempt),
        ).fetchone()
        if row is None:
            raise RetryAuthorizationError(
                "retry member requires an established prior outcome"
            )
        outcomes.append(
            PriorOutcome(
                task_id=task_id,
                attempt_id=cast(str, row["attempt_id"]),
                result_sha256=cast(str, row["record_sha256"]),
                status=cast(str, row["status"]),
            )
        )
    return tuple(outcomes)


def _authorization(
    run_id: str,
    operation_id: str,
    request_uid: str,
    task_ids: tuple[str, ...],
    prior: tuple[PriorOutcome, ...],
    allow_completed: bool,
    policy: RetryPolicy,
) -> RetryAuthorization:
    return RetryAuthorization(
        run_id=run_id,
        operation_id=operation_id,
        allow_completed=allow_completed,
        selector_sha256=semantic_digest(
            "retry-selection",
            {
                "request_uid": request_uid,
                "task_ids": list(task_ids),
                "prior_outcomes": [p.model_dump() for p in prior],
                "allow_completed": allow_completed,
                "policy": policy.model_dump(),
            },
        ),
    )


def authorize_automatic_retry(
    repository: Repository,
    prior_attempt: AttemptRecord,
    *,
    owner_token: str,
    policy: RetryPolicy = DEFAULT_RETRY_POLICY,
    retry_after: float | None = None,
    now: datetime | None = None,
) -> str | None:
    """Freeze auto eligibility after *authoritative transient* nonacceptance.

    The executor calls this only when its adapter supplies typed transient
    nonacceptance evidence. Durable definitive failure evidence is independently
    checked here. SQL-only authorization may compose with the failure/result
    transaction; later preparation performs all rendering and artifact I/O.
    """
    policy = RetryPolicy.model_validate_json(canonical_bytes(policy))
    run_id = prior_attempt.run_id
    with repository.execution_transaction(run_id, owner_token):
        current = repository.get_attempt(run_id, prior_attempt.attempt_id)
        submission = repository.get_submission_for_attempt(run_id, current.attempt_id)
        failure = repository.connection.execute(
            "SELECT kind FROM submission_failures WHERE run_id=? AND attempt_id=?",
            (run_id, current.attempt_id),
        ).fetchone()
        if (
            current.state != "not_submitted"
            or submission is None
            or submission.certainty != "not_submitted"
            or failure is None
            or failure[0] != "transport_not_submitted"
        ):
            raise IntegrityError(
                "automatic retry lacks definitive nonacceptance evidence"
            )
        if current.attempt_number >= policy.max_attempts:
            return None
        selected = repository.attempt_task_ids(run_id, current.attempt_id)
        for task_id in selected:
            if (
                repository.connection.execute(
                    "SELECT 1 FROM results WHERE run_id=? AND task_id=? AND status='complete' LIMIT 1",
                    (run_id, task_id),
                ).fetchone()
                is not None
            ):
                # Retrying an already successful member requires a fresh
                # explicit authorization; a transport retry cannot grant it.
                return None
        prior = _prior_outcomes(
            repository,
            run_id,
            current.request_uid,
            selected,
            current.attempt_number + 1,
        )
        if any(
            p.attempt_id != current.attempt_id or p.status == "complete" for p in prior
        ):
            raise IntegrityError("automatic retry basis differs from failed attempt")
        operation_id = automatic_retry_id(current)
        repository.authorize_retry(
            _authorization(
                run_id,
                operation_id,
                current.request_uid,
                selected,
                prior,
                False,
                policy,
            )
        )
        if current.next_eligible_at is None:
            # Hash-derived jitter is independent of assessment content and is
            # computed once. Recovery retains the persisted timestamp unchanged.
            fraction = int(bytes_digest(current.attempt_id.encode())[:13], 16) / (
                16**13 - 1
            )
            eligible = policy.eligible_at(
                current.attempt_number,
                now=now,
                retry_after=retry_after,
                jitter_fraction=fraction,
            )
            repository.connection.execute(
                "UPDATE attempts SET next_eligible_at=? WHERE run_id=? AND attempt_id=? AND next_eligible_at IS NULL",
                (eligible, run_id, current.attempt_id),
            )
        return operation_id


def prepare_subset_retry(
    prepared_run: PreparedRun,
    adapter: ProviderAdapter,
    *,
    dependency: RequestDependency,
    task_ids: Sequence[str],
    operation_id: str,
    owner_token: str,
    allow_completed: bool = False,
    policy: RetryPolicy = DEFAULT_RETRY_POLICY,
    next_eligible_at: str | None = None,
    fault: Callable[[str, str], None] | None = None,
) -> AttemptRecord:
    """Authorize and prepare one exact subset, without provider operations.

    The caller must hold ExecutorOwnership throughout this operation. Only one
    request's bounded trait group is held in memory; no retry candidate scan is
    performed. The operation ID resumes its original attempt even after later
    results have arrived.
    """
    policy = RetryPolicy.model_validate_json(canonical_bytes(policy))
    run_id = prepared_run.manifest.run_id
    repository = prepared_run.repository
    request_uid = dependency.request.request_uid
    subset = subset_dependency(dependency, task_ids)
    selected = tuple(t.task_id for t in subset.request.tasks)
    checkpoint = fault or _no_checkpoint
    with repository.execution_transaction(run_id, owner_token):
        existing_rows = repository.connection.execute(
            "SELECT attempt_id FROM attempts WHERE run_id=? AND authorization_id=? LIMIT 2",
            (run_id, operation_id),
        ).fetchall()
        if len(existing_rows) > 1:
            raise IntegrityError("retry authorization has conflicting attempts")
        existing = (
            repository.get_attempt(run_id, cast(str, existing_rows[0][0]))
            if existing_rows
            else None
        )
    if existing is not None:
        if (
            existing.request_uid != request_uid
            or repository.attempt_task_ids(run_id, existing.attempt_id) != selected
        ):
            raise RetryAuthorizationError("retry operation selection conflicts")
        # Rendering and artifact verification stay outside write transactions.
        if prepared_run.manifest.schema_version in (3, 4, 5, 6):
            from judgmentllm.transport import physical_identity

            wire = prepared_run.prepare_transport_attempt(
                subset,
                physical_identity(run_id, request_uid, existing.attempt_id),
                frozen=prepared_run.transport_for_attempt(existing.attempt_id),
            )
            if (wire.prompt_sha256, wire.payload_sha256) != (
                existing.prompt_sha256,
                existing.payload_sha256,
            ):
                raise IntegrityError("existing retry transport commitment differs")
        with repository.execution_transaction(run_id, owner_token):
            if repository.get_attempt(run_id, existing.attempt_id) != existing:
                raise IntegrityError("existing retry changed during verification")
            prior = _prior_outcomes(
                repository, run_id, request_uid, selected, existing.attempt_number
            )
            repository.authorize_retry(
                _authorization(
                    run_id,
                    operation_id,
                    request_uid,
                    selected,
                    prior,
                    allow_completed,
                    policy,
                )
            )
        return existing
    with repository.execution_transaction(run_id, owner_token):
        row = repository.connection.execute(
            "SELECT COALESCE(MAX(attempt_number),0) FROM attempts WHERE run_id=? AND request_uid=?",
            (run_id, request_uid),
        ).fetchone()
        attempt_number = cast(int, row[0]) + 1
        if attempt_number < 2 or attempt_number > policy.max_attempts:
            raise RetryAuthorizationError("retry submission budget is exhausted")
        previous = repository.get_attempt_for_request(
            run_id, request_uid, attempt_number=attempt_number - 1
        )
        if previous is not None and previous.next_eligible_at is not None:
            reserved_id = automatic_retry_id(previous)
            reserved = repository.connection.execute(
                "SELECT 1 FROM retry_operations r WHERE r.run_id=? AND r.operation_id=? "
                "AND NOT EXISTS(SELECT 1 FROM attempts a WHERE a.run_id=r.run_id AND a.authorization_id=r.operation_id)",
                (run_id, reserved_id),
            ).fetchone()
            if reserved is not None and reserved_id != operation_id:
                raise RetryAuthorizationError(
                    "a durable retry authorization already reserves this prior basis"
                )
        unresolved = repository.connection.execute(
            "SELECT 1 FROM attempts WHERE run_id=? AND request_uid=? "
            "AND state NOT IN ('normalized','not_submitted','errored','cancelled') LIMIT 1",
            (run_id, request_uid),
        ).fetchone()
        if unresolved is not None:
            raise RetryAuthorizationError("retry cannot replace unresolved prior work")
        prior = _prior_outcomes(
            repository, run_id, request_uid, selected, attempt_number
        )
        if not allow_completed:
            for task_id in selected:
                completed = repository.connection.execute(
                    "SELECT 1 FROM results WHERE run_id=? AND task_id=? AND status='complete' LIMIT 1",
                    (run_id, task_id),
                ).fetchone()
                if completed is not None:
                    raise RetryAuthorizationError(
                        "retry of completed tasks requires explicit permission"
                    )
    checkpoint("before_retry_preparation", request_uid)
    prepared_run.verify_compatibility(
        capabilities=adapter.capabilities(prepared_run.spec)
    )
    adapter.validate(prepared_run.spec)
    attempt_id = stable_id("attempt", run_id, request_uid, attempt_number)
    native_artifacts = ()
    if prepared_run.manifest.schema_version in (3, 4, 5, 6):
        from judgmentllm.preparation.native import publish_prepared_transport
        from judgmentllm.transport import physical_identity

        original = repository.get_attempt_for_request(
            run_id, request_uid, attempt_number=1
        )
        if original is None:
            raise IntegrityError("native retry lacks its original attempt")
        wire = prepared_run.prepare_transport_attempt(
            subset,
            physical_identity(run_id, request_uid, attempt_id),
            parent=prepared_run.transport_for_attempt(original.attempt_id),
        )
        prompt_bytes = wire.rendered_text.encode("utf-8")
        native_artifacts = publish_prepared_transport(
            repository, prepared_run.store, wire, prepared_run.spec.capture
        )
    else:
        rendered = prepared_run.render_attempt(subset)
        if type(rendered) not in (RenderedPrompt, RenderedPromptV2):
            raise IntegrityError("historical retry acquired native rendering")
        wire = adapter.prepare(prepared_run.spec, subset, rendered)
        prompt_bytes = rendered.text.encode("utf-8")
    if (
        wire.request_uid != request_uid
        or wire.task_ids != selected
        or wire.dependency_sha256 != dependency.dependency_sha256
        or bytes_digest(prompt_bytes) != wire.prompt_sha256
        or bytes_digest(wire.payload) != wire.payload_sha256
        or (wire.provider, wire.endpoint, wire.model)
        != (
            prepared_run.spec.provider,
            prepared_run.spec.endpoint,
            prepared_run.spec.model,
        )
        or wire.generation != prepared_run.spec.generation
    ):
        raise IntegrityError("retry preparation differs from its frozen subset")
    store = prepared_run.store
    prompt = (
        store.put(prompt_bytes, "text/plain")
        if prepared_run.spec.capture == "full"
        else store.hash_only(wire.prompt_sha256, len(prompt_bytes), "text/plain")
    )
    payload = (
        store.put(wire.payload, "application/json")
        if prepared_run.spec.capture == "full"
        else store.hash_only(wire.payload_sha256, len(wire.payload), "application/json")
    )
    checkpoint("after_retry_preparation_artifacts", request_uid)
    attempt = AttemptRecord(
        run_id=run_id,
        attempt_id=attempt_id,
        request_uid=request_uid,
        attempt_number=attempt_number,
        prompt_sha256=wire.prompt_sha256,
        payload_sha256=wire.payload_sha256,
        membership_sha256=semantic_digest("attempt-membership", list(selected)),
        authorization_id=operation_id,
        next_eligible_at=next_eligible_at,
    )
    with repository.execution_transaction(run_id, owner_token):
        # Freeze the exact prior-result basis under the same owner and reject
        # unexpected changes rather than silently selecting newer outcomes.
        if (
            _prior_outcomes(repository, run_id, request_uid, selected, attempt_number)
            != prior
        ):
            raise IntegrityError("retry prior outcome basis changed")
        repository.authorize_retry(
            _authorization(
                run_id,
                operation_id,
                request_uid,
                selected,
                prior,
                allow_completed,
                policy,
            )
        )
        checkpoint("during_retry_authorization", request_uid)
        repository._register_artifact(run_id, prompt)  # pyright: ignore[reportPrivateUsage]
        repository._register_artifact(run_id, payload)  # pyright: ignore[reportPrivateUsage]
        for artifact in native_artifacts:
            repository._register_artifact(run_id, artifact)  # pyright: ignore[reportPrivateUsage]
        repository.create_attempt(attempt, selected)
        checkpoint("during_retry_preparation", request_uid)
        submission_id = stable_id("submission", run_id, attempt_id)
        repository.create_submission(
            SubmissionRecord(
                run_id=run_id,
                submission_id=submission_id,
                correlation_id=stable_id("correlation", run_id, submission_id),
                payload_sha256=wire.payload_sha256,
                attempt_id=attempt_id,
            )
        )
        if repository.get_run(run_id).state == "completed":
            repository.transition(
                "run", run_id, run_id, "running", authorization_id=operation_id
            )
            repository.transition("run", run_id, run_id, "partial")
    checkpoint("after_retry_preparation", request_uid)
    return attempt


def authorize_subset_retry(
    run_id: str,
    *,
    store: str | Path,
    adapter: ProviderAdapter,
    task_ids: Sequence[str],
    operation_id: str,
    spec: RunSpecification | None = None,
    allow_completed: bool = False,
    policy: RetryPolicy = DEFAULT_RETRY_POLICY,
    use_frozen_snapshot: bool = False,
    fault: Callable[[str, str], None] | None = None,
) -> AttemptRecord:
    """Provider-free owned operation selecting tasks from one logical request."""
    selected = tuple(task_ids)
    if not selected or len(set(selected)) != len(selected):
        raise RetryAuthorizationError("retry selection requires unique task IDs")
    with Repository(store) as bootstrap, ExecutorOwnership(bootstrap, run_id) as owner:
        assert owner.owner is not None
        owner_token = owner.owner.token
        run = bootstrap.get_run(run_id)
        capabilities = adapter.capabilities_for(run.provider, run.endpoint, run.model)
        with open_prepared_run(
            run_id,
            store=store,
            spec=spec,
            capabilities=capabilities,
            use_frozen_snapshot=use_frozen_snapshot,
            owner_token=owner_token,
            transport_context=getattr(adapter, "transport_context", None),
        ) as prepared:
            request_uid: str | None = None
            for task_id in selected:
                rows = prepared.repository.connection.execute(
                    "SELECT request_uid FROM request_tasks WHERE run_id=? AND task_id=? LIMIT 2",
                    (run_id, task_id),
                ).fetchall()
                if len(rows) != 1:
                    raise RetryAuthorizationError(
                        "retry task has no unique original request"
                    )
                parent = cast(str, rows[0][0])
                if request_uid is not None and parent != request_uid:
                    raise RetryAuthorizationError(
                        "retry operation must select one original request"
                    )
                request_uid = parent
            assert request_uid is not None
            return prepare_subset_retry(
                prepared,
                adapter,
                dependency=prepared.dependency_for_request(request_uid),
                task_ids=selected,
                operation_id=operation_id,
                owner_token=owner_token,
                allow_completed=allow_completed,
                policy=policy,
                fault=fault,
            )
