"""Explicit scientific retry reuses the existing subset authorization and cap."""

from judgmentllm.batch_models import (
    BatchMemberV1,
    member_receipt_model,
    shard_model,
)
from judgmentllm.execution.native_batch import BatchSession
from judgmentllm.execution.retry import prepare_subset_retry, subset_dependency
from judgmentllm.lifecycle_models import RetrySelection
from judgmentllm.persistence.hashing import (
    bytes_digest,
    canonical_bytes,
    semantic_digest,
)
from judgmentllm.persistence.models import IntegrityError, utc_now
from judgmentllm.persistence.native_batch import put, read_reference, register, stopped
from judgmentllm.planning import stable_id
from judgmentllm.preparation.batch import encode_row, frame_shard


def prepare_batch_retry(session: BatchSession, retry: RetrySelection) -> None:
    repository, run_id = session.repository, session.run_id
    prepared = session.prepared
    assert prepared is not None
    if stopped(repository, run_id):
        raise IntegrityError("stopped batch cannot start a scientific retry")
    retry = RetrySelection.model_validate_json(canonical_bytes(retry))
    reference = (
        f"batch/retry-contexts/{bytes_digest(retry.operation_id.encode('utf-8'))}.json"
    )
    context = session.adapter.context()
    previous_context = read_reference(repository, run_id, reference, type(context))
    if previous_context is not None and previous_context != context:
        raise IntegrityError(
            "unfinished scientific retry requires its original fixture"
        )
    put(repository, run_id, context, reference=reference)
    session.checkpoint("after_batch_retry_context", retry.operation_id)
    requests = {
        repository.get_task(run_id, task).task_id: None for task in retry.task_ids
    }
    parents = {
        str(row[0])
        for task in requests
        for row in repository.connection.execute(
            "SELECT request_uid FROM request_tasks WHERE run_id=? AND task_id=?",
            (run_id, task),
        )
    }
    if len(parents) != 1:
        raise ValueError("batch retry must select one original request")
    request_uid = parents.pop()
    dependency = prepared.dependency_for_request(request_uid)
    attempt = prepare_subset_retry(
        prepared,
        session.adapter,
        dependency=dependency,
        task_ids=retry.task_ids,
        operation_id=retry.operation_id,
        owner_token=session.token,
        allow_completed=retry.allow_completed,
    )
    session.checkpoint("after_batch_retry_attempt", retry.operation_id)
    existing = repository.connection.execute(
        "SELECT 1 FROM batch_prepared_members WHERE run_id=? AND attempt_id=?",
        (run_id, attempt.attempt_id),
    ).fetchone()
    if existing:
        return
    prior = repository.get_attempt_for_request(run_id, request_uid, attempt_number=1)
    assert prior is not None
    parent_row = repository.connection.execute(
        "SELECT member_key FROM batch_prepared_members WHERE run_id=? AND attempt_id=?",
        (run_id, prior.attempt_id),
    ).fetchone()
    if parent_row is None:
        raise IntegrityError("batch retry lacks its parent member")
    parent = read_reference(
        repository,
        run_id,
        f"batch/members/{parent_row[0]}.json",
        member_receipt_model(context),
    )
    if parent is None:
        raise IntegrityError("batch retry lacks its proven parent outcome")
    subset = subset_dependency(
        dependency, repository.attempt_task_ids(run_id, attempt.attempt_id)
    )
    wire = session.adapter.prepare(
        prepared.spec, subset, prepared.render_attempt(subset)
    )
    key = semantic_digest(
        "batch-member-key-v1", [run_id, request_uid, attempt.attempt_id]
    )
    row = encode_row(key, wire)
    body, media = frame_shard(context, (row,))
    if len(body) > session.manifest.batch_context.limits.max_input_bytes:
        raise IntegrityError("retry member exceeds sealed batch limit")
    ordinal = int(
        repository.connection.execute(
            "SELECT coalesce(max(ordinal),-1)+1 FROM batch_preparations WHERE run_id=?",
            (run_id,),
        ).fetchone()[0]
    )
    position = int(
        repository.connection.execute(
            "SELECT coalesce(max(position),-1)+1 FROM batch_prepared_members WHERE run_id=?",
            (run_id,),
        ).fetchone()[0]
    )
    shard_id = stable_id("native-batch-retry-shard", run_id, retry.operation_id)
    prompt = repository.get_artifact(run_id, attempt.prompt_sha256)
    payload = repository.get_artifact(run_id, attempt.payload_sha256)
    member = BatchMemberV1(
        run_id=run_id,
        shard_id=shard_id,
        member_key=key,
        request_uid=request_uid,
        attempt_id=attempt.attempt_id,
        position=position,
        dependency_sha256=subset.dependency_sha256,
        membership_sha256=attempt.membership_sha256,
        prompt=prompt,
        payload=payload,
        row_sha256=bytes_digest(row),
        row_bytes=len(row),
        parent_receipt_sha256=bytes_digest(canonical_bytes(parent)),
    )
    source = (
        prepared.store.put(body, media)
        if prepared.spec.capture == "full"
        else prepared.store.hash_only(bytes_digest(body), len(body), media)
    )
    shard = shard_model(context).model_validate(
        dict(
            run_id=run_id,
            shard_id=shard_id,
            ordinal=ordinal,
            member_count=1,
            membership_sha256=semantic_digest("batch-shard-members-v1", [key]),
            input=source,
            context=session.adapter.context(),
        )
    )
    member_artifact, shard_artifact = (
        put(repository, run_id, member),
        put(repository, run_id, shard),
    )
    with repository.execution_transaction(run_id, session.token):
        for artifact in (source, member_artifact, shard_artifact):
            register(repository, run_id, artifact)
        repository.connection.execute(
            "INSERT INTO batches(run_id,batch_uid,state,provider,mode,membership_sha256,created_at) VALUES (?,?,'prepared',?,'batch_native',?,?)",
            (
                run_id,
                shard_id,
                prepared.spec.provider,
                shard.membership_sha256,
                utc_now(),
            ),
        )
        repository.connection.execute(
            "INSERT INTO batch_preparations VALUES (?,?,?,?,?)",
            (
                run_id,
                shard_id,
                ordinal,
                shard_artifact.sha256,
                canonical_bytes(shard).decode(),
            ),
        )
        repository.connection.execute(
            "INSERT INTO batch_members VALUES (?,?,?,0)",
            (run_id, shard_id, attempt.attempt_id),
        )
        repository.connection.execute(
            "INSERT INTO batch_prepared_members VALUES (?,?,?,?,?,?,?)",
            (
                run_id,
                shard_id,
                key,
                attempt.attempt_id,
                position,
                member_artifact.sha256,
                canonical_bytes(member).decode(),
            ),
        )
    session.checkpoint("after_batch_retry_shard", retry.operation_id)


def recover_retry_preparations(session: BatchSession) -> None:
    if stopped(session.repository, session.run_id):
        return
    for row in session.repository.connection.execute(
        "SELECT a.attempt_id,a.authorization_id,r.allow_completed FROM attempts a JOIN retry_operations r "
        "ON r.run_id=a.run_id AND r.operation_id=a.authorization_id WHERE a.run_id=? "
        "AND NOT EXISTS(SELECT 1 FROM batch_members m WHERE m.run_id=a.run_id AND m.attempt_id=a.attempt_id) ORDER BY a.rowid",
        (session.run_id,),
    ):
        session.budget.remaining()
        prepare_batch_retry(
            session,
            RetrySelection(
                task_ids=session.repository.attempt_task_ids(session.run_id, row[0]),
                operation_id=row[1],
                allow_completed=bool(row[2]),
            ),
        )
