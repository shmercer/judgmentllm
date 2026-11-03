"""Owner-checked cancellation of local scheduling, with conservative evidence.

Cancellation is durable before individual work is drained. Repeating the
operation finishes an interrupted drain. It never asserts provider cancellation.
"""

from pathlib import Path

from judgmentllm.persistence.models import ExecutionSummary
from judgmentllm.persistence.ownership import ExecutorOwnership
from judgmentllm.persistence.repository import Repository


def cancel_owned(
    repository: Repository, run_id: str, owner_token: str
) -> ExecutionSummary:
    """Stop scheduling under the executor's lifetime lock; retain remote certainty."""
    with repository.execution_transaction(run_id, owner_token):
        state = repository.get_run(run_id).state
        if state == "completed":
            return repository.summary(run_id)
        if state != "cancelled":
            repository.transition("run", run_id, run_id, "cancelled")
    for attempt in repository.iter_attempts(run_id):
        with repository.execution_transaction(run_id, owner_token):
            current = repository.get_attempt(run_id, attempt.attempt_id)
            submission = repository.get_submission_for_attempt(
                run_id, attempt.attempt_id
            )
            if current.state in {"prepared", "claimed"}:
                repository.transition(
                    "attempt", run_id, attempt.attempt_id, "cancelled"
                )
                for task_id in repository.attempt_task_ids(run_id, attempt.attempt_id):
                    if repository.get_task(run_id, task_id).state in {
                        "pending",
                        "active",
                        "blocked",
                    }:
                        repository.transition("task", run_id, task_id, "cancelled")
                request = repository.get_request(run_id, attempt.request_uid)
                if request.state in {"planned", "prepared", "active", "blocked"}:
                    repository.transition(
                        "request", run_id, request.request_uid, "cancelled"
                    )
            elif current.state in {"submitting", "acknowledged", "uncertain"}:
                if current.state != "uncertain":
                    repository.transition(
                        "attempt", run_id, attempt.attempt_id, "uncertain"
                    )
                if submission is not None and submission.state in {
                    "submitting",
                    "acknowledged",
                }:
                    repository.transition(
                        "submission", run_id, submission.submission_id, "uncertain"
                    )
                for task_id in repository.attempt_task_ids(run_id, attempt.attempt_id):
                    if repository.get_task(run_id, task_id).state == "active":
                        repository.transition("task", run_id, task_id, "blocked")
                request = repository.get_request(run_id, attempt.request_uid)
                if request.state == "active":
                    repository.transition(
                        "request", run_id, request.request_uid, "blocked"
                    )
    return repository.summary(run_id)


def cancel_run(run_id: str, *, store: str | Path) -> ExecutionSummary:
    """Cancel an idle/interrupted run; a competing live executor retains its lock.

    Live callers cancel the execute_run coroutine. Its cleanup drains writes
    and invokes this same operation before releasing exclusive ownership.
    """
    with Repository(store) as repository:
        ownership = ExecutorOwnership(repository, run_id)
        owner = ownership.acquire()
        try:
            return cancel_owned(repository, run_id, owner.token)
        finally:
            ownership.release()
