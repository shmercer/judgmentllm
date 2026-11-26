"""Owned append-only native batch metadata and immutable receipt publication."""

from __future__ import annotations

from collections.abc import Iterator
from typing import TypeVar

from judgmentllm.batch_models import BatchDetailV1, BatchStopV1
from judgmentllm.evidence._base import EvidenceModel
from judgmentllm.persistence.artifacts import ArtifactStore
from judgmentllm.persistence.hashing import bytes_digest, canonical_bytes
from judgmentllm.persistence.models import (
    ArtifactIdentity,
    IntegrityError,
    NotFoundError,
)
from judgmentllm.persistence.repository import Repository

T = TypeVar("T", bound=EvidenceModel)


def require_storage(repository: Repository) -> None:
    if repository.connection.execute("PRAGMA user_version").fetchone()[0] != 4:
        raise IntegrityError("native batch requires a fresh schema-4 store")


def put(
    repository: Repository,
    run_id: str,
    model: EvidenceModel,
    *,
    reference: str | None = None,
) -> ArtifactIdentity:
    """Only already authorized typed facts reach this boundary."""
    model = type(model).model_validate_json(canonical_bytes(model))
    store = ArtifactStore(repository.run_directory(run_id), create=False)
    identity = store.put(canonical_bytes(model), "application/json")
    if reference is not None:
        store.publish_reference(identity, reference)
    return identity


def register(repository: Repository, run_id: str, identity: ArtifactIdentity) -> None:
    try:
        old = repository.get_artifact(run_id, identity.sha256)
    except NotFoundError:
        repository.register_artifact(run_id, identity)
    else:
        # A permitted export/body may share source bytes with another media
        # type; its publication never rewrites the original artifact metadata.
        if old.byte_length != identity.byte_length:
            raise IntegrityError("batch artifact registration conflicts")


def read_reference(
    repository: Repository, run_id: str, reference: str, model: type[T]
) -> T | None:
    store = ArtifactStore(repository.run_directory(run_id), create=False)
    path = store.reference_path(reference)
    if not path.exists():
        return None
    if path.is_symlink():
        raise IntegrityError("batch receipt reference is invalid")
    with path.open("rb") as stream:
        raw = stream.read(4_194_305)
    if len(raw) > 4_194_304:
        raise IntegrityError("batch metadata receipt exceeds its bound")
    value = model.model_validate_json(raw)
    if canonical_bytes(value) != raw:
        raise IntegrityError("batch receipt encoding conflicts")
    store.verify(
        ArtifactIdentity(
            sha256=bytes_digest(raw),
            byte_length=len(raw),
            media_type="application/json",
            relative_path=f"objects/{bytes_digest(raw)[:2]}/{bytes_digest(raw)}",
        )
    )
    return value


def fact(
    repository: Repository,
    run_id: str,
    token: str,
    kind: str,
    entity: str,
    model: EvidenceModel,
    *,
    reference: str | None = None,
) -> str:
    artifact = put(repository, run_id, model, reference=reference)
    with repository.execution_transaction(run_id, token):
        register(repository, run_id, artifact)
        if (
            repository.connection.execute(
                "SELECT 1 FROM batch_facts WHERE run_id=? AND kind=? AND entity_id=? AND receipt_sha256=?",
                (run_id, kind, entity, artifact.sha256),
            ).fetchone()
            is None
        ):
            repository.connection.execute(
                "INSERT INTO batch_facts(run_id,kind,entity_id,receipt_sha256,record_json) VALUES (?,?,?,?,?)",
                (
                    run_id,
                    kind,
                    entity,
                    artifact.sha256,
                    canonical_bytes(model).decode(),
                ),
            )
    return artifact.sha256


def latest(
    repository: Repository, run_id: str, kind: str, entity: str, model: type[T]
) -> T | None:
    row = repository.connection.execute(
        "SELECT record_json,receipt_sha256 FROM batch_facts WHERE run_id=? AND kind=? AND entity_id=? ORDER BY fact_seq DESC LIMIT 1",
        (run_id, kind, entity),
    ).fetchone()
    if row is None:
        return None
    result = model.model_validate_json(row[0])
    if bytes_digest(canonical_bytes(result)) != row[1]:
        raise IntegrityError("batch fact identity conflicts")
    ArtifactStore(repository.run_directory(run_id), create=False).verify(
        repository.get_artifact(run_id, row[1])
    )
    return result


def stopped(repository: Repository, run_id: str, kind: str = "stop") -> bool:
    return (
        repository.connection.execute(
            "SELECT 1 FROM batch_facts WHERE run_id=? AND kind=? LIMIT 1",
            (run_id, kind),
        ).fetchone()
        is not None
    )


def rows(
    repository: Repository, table: str, run_id: str, model: type[T]
) -> Iterator[T]:
    # Callers choose a static table; keyset pages never retain the run product.
    columns = {"batch_preparations": "ordinal", "batch_prepared_members": "position"}
    column = columns[table]
    after = -1
    while True:
        page = repository.connection.execute(
            f"SELECT {column},record_json,receipt_sha256 FROM {table} WHERE run_id=? AND {column}>? ORDER BY {column} LIMIT 128",
            (run_id, after),
        ).fetchall()
        if not page:
            return
        for row in page:
            result = model.model_validate_json(row[1])
            if bytes_digest(canonical_bytes(result)) != row[2]:
                raise IntegrityError("batch preparation receipt conflicts")
            ArtifactStore(repository.run_directory(run_id), create=False).verify(
                repository.get_artifact(run_id, row[2])
            )
            yield result
        after = int(page[-1][0])


def record_stop(
    repository: Repository, run_id: str, token: str, *, remote: bool
) -> None:
    """Recoverable command reference, then atomic independent scheduling facts."""
    command = BatchStopV1(run_id=run_id, kind="cancel_all" if remote else "stop")
    artifact = put(repository, run_id, command, reference=f"batch/{command.kind}.json")
    with repository.execution_transaction(run_id, token):
        register(repository, run_id, artifact)
        if remote:
            fact(repository, run_id, token, "cancel_all", run_id, command)
        fact(
            repository,
            run_id,
            token,
            "stop",
            run_id,
            BatchStopV1(run_id=run_id, kind="stop"),
        )
        if repository.get_run(run_id).state not in ("completed", "cancelled"):
            repository.transition("run", run_id, run_id, "cancelled")


def restore_stops(repository: Repository, run_id: str, token: str) -> None:
    for kind in ("cancel_all", "stop"):
        command = read_reference(repository, run_id, f"batch/{kind}.json", BatchStopV1)
        if command is not None:
            if command.run_id != run_id or command.kind != kind:
                raise IntegrityError("batch stop reference conflicts with its run")
            record_stop(repository, run_id, token, remote=kind == "cancel_all")


def detail(repository: Repository, run_id: str, shard_count: int) -> BatchDetailV1:
    connection = repository.connection
    shard_count = int(
        connection.execute(
            "SELECT count(*) FROM batch_preparations WHERE run_id=?", (run_id,)
        ).fetchone()[0]
    )
    accepted = int(
        connection.execute(
            "SELECT count(*) FROM submissions WHERE run_id=? AND batch_uid IS NOT NULL AND certainty='accepted'",
            (run_id,),
        ).fetchone()[0]
    )
    unresolved = int(
        connection.execute(
            "SELECT count(*) FROM batch_operations o WHERE o.run_id=? AND NOT EXISTS(SELECT 1 FROM batch_facts f WHERE f.run_id=o.run_id AND f.kind='operation' AND f.entity_id=o.instance_id)",
            (run_id,),
        ).fetchone()[0]
    )
    missing = int(
        connection.execute(
            "SELECT count(*) FROM attempts a WHERE a.run_id=? AND NOT EXISTS(SELECT 1 FROM results r WHERE r.run_id=a.run_id AND r.attempt_id=a.attempt_id)",
            (run_id,),
        ).fetchone()[0]
    )
    conflicts = int(
        connection.execute(
            "SELECT count(*) FROM batch_facts WHERE run_id=? AND kind='conflict'",
            (run_id,),
        ).fetchone()[0]
    )
    unread_sources = bool(
        connection.execute(
            "SELECT EXISTS(SELECT 1 FROM submissions s LEFT JOIN ("
            "SELECT json_extract(f.record_json,'$.submission_id') submission_id,count(*) sources "
            "FROM batch_facts f WHERE f.run_id=? AND f.kind='cursor' "
            "AND json_extract(f.record_json,'$.exhausted')=1 AND NOT EXISTS(SELECT 1 FROM batch_facts n "
            "WHERE n.run_id=f.run_id AND n.kind=f.kind AND n.entity_id=f.entity_id AND n.fact_seq>f.fact_seq) "
            "GROUP BY submission_id) e ON e.submission_id=s.submission_id "
            "WHERE s.run_id=? AND s.batch_uid IS NOT NULL AND s.certainty='accepted' AND coalesce(e.sources,0)<2)",
            (run_id, run_id),
        ).fetchone()[0]
    )
    context = connection.execute(
        "SELECT context_json FROM batch_runs WHERE run_id=?", (run_id,)
    ).fetchone()
    if context is not None and '"openai-native-batch-v1"' in context[0]:
        from judgmentllm.batch_models import OpenAIBatchReadyV1
        from judgmentllm.execution.openai_batch import collection_summary, source_done

        ready = [
            OpenAIBatchReadyV1.model_validate_json(row[0])
            for row in connection.execute(
                "SELECT record_json FROM batch_facts WHERE run_id=? AND kind='cursor' AND entity_id LIKE 'openai-ready:%'",
                (run_id,),
            )
        ]
        unread_sources = len(ready) < 2 * accepted or any(
            not source_done(repository, run_id, source.source_id) for source in ready
        )
        collection = collection_summary(repository, run_id)
        unread_sources |= bool(collection.failed_sources or collection.prelude_failed)
    if context is not None and '"anthropic-native-batch-v1"' in context[0]:
        from judgmentllm.execution.anthropic_batch import collection_summary

        ready_count = connection.execute(
            "SELECT count(*) FROM batch_facts WHERE run_id=? AND kind='cursor' AND entity_id LIKE 'anthropic-ready:%'",
            (run_id,),
        ).fetchone()[0]
        collection = collection_summary(repository, run_id)
        unread_sources = ready_count < accepted or bool(
            collection.pending_sources or collection.failed_sources
        )
    return BatchDetailV1(
        local_stop=stopped(repository, run_id),
        cancel_all=stopped(repository, run_id, "cancel_all"),
        jobs=shard_count,
        accepted_jobs=accepted,
        unresolved_operations=unresolved,
        missing_members=missing,
        conflicts=conflicts,
        recovery_required=bool(unresolved or missing or conflicts or unread_sources),
    )
