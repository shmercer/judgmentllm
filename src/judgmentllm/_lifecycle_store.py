"""Explicit store discovery and bounded, source-independent durable inspection."""

from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import cast

from judgmentllm.config import RunSpec, parse_json
from judgmentllm.lifecycle_models import (
    ArtifactPaths,
    LifecycleError,
    RunStatus,
    SubmissionCounts,
)
from judgmentllm.models import VersionedModel
from judgmentllm.persistence.hashing import bytes_digest
from judgmentllm.persistence.models import NotFoundError
from judgmentllm.persistence.ownership import executor_is_active
from judgmentllm.persistence.repository import ReadOnlyRepository, Repository
from judgmentllm.preparation import _load_manifest  # pyright: ignore[reportPrivateUsage]


class _Cursor(VersionedModel):
    store: str
    created_at: str
    run_id: str


def discover_store(store: str | Path | None, spec: RunSpec | None = None) -> Path:
    """Explicit argument, environment, configuration, then the local default."""
    if store is not None:
        return Path(store).resolve()
    environment = os.environ.get("JUDGMENTLLM_RUN_HOME")
    if environment:
        return Path(environment).resolve()
    return (spec.output_path if spec is not None else Path("run-data")).resolve()


def require_store(store: Path) -> None:
    if not (store / "registry.sqlite3").is_file():
        raise LifecycleError("not_found", "run store was not found", store=str(store))


def _directory_id(directory: Path, root: Path) -> str:
    path = directory / "manifest.json"
    if path.is_symlink():
        raise LifecycleError("validation", "run directory manifest is invalid")
    if path.is_file():
        data = parse_json(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise LifecycleError("validation", "run directory manifest is invalid")
        fields = cast(dict[str, object], data)
        identifier = fields.get("run_id")
        if not isinstance(identifier, str):
            raise LifecycleError("validation", "run directory manifest is invalid")
        return identifier
    # Unsealed preparations have no manifest. Compare established directory
    # hashes through bounded registry pages, without retaining an ID inventory.
    with ReadOnlyRepository(root) as repository:
        after = ""
        while True:
            rows = repository.connection.execute(
                "SELECT run_id FROM runs WHERE run_id>? ORDER BY run_id LIMIT 128",
                (after,),
            ).fetchall()
            if not rows:
                raise LifecycleError("not_found", "run directory was not found")
            for row in rows:
                if bytes_digest(str(row[0]).encode("utf-8")) == directory.name:
                    return str(row[0])
            after = str(rows[-1][0])


def resolve_run(
    run_id: str | Path,
    store: str | Path | None,
    spec: RunSpec | None = None,
) -> tuple[str, Path]:
    # Existing IDs are arbitrary stable strings, including path-like strings.
    # With an explicit store, an exact registry identity takes precedence.
    if store is not None:
        selected = Path(store).resolve()
        require_store(selected)
        with ReadOnlyRepository(selected) as repository:
            row = repository.connection.execute(
                "SELECT 1 FROM runs WHERE run_id=?", (str(run_id),)
            ).fetchone()
            if row is not None:
                return str(run_id), selected
    reference = Path(run_id)
    if reference.is_absolute():
        directory = reference.resolve()
        if directory.parent.name != "runs":
            raise LifecycleError("not_found", "run directory was not found")
        root = directory.parent.parent
        if store is not None and Path(store).resolve() != root:
            raise LifecycleError("validation", "run directory and store disagree")
        require_store(root)
        identifier = _directory_id(directory, root)
        with ReadOnlyRepository(root) as repository:
            if repository.run_directory(identifier) != directory:
                raise LifecycleError("validation", "run directory identity disagrees")
            if repository.get_run(identifier).manifest_sha256 is not None:
                _load_manifest(repository, identifier, unsealed=False)
        return identifier, root
    root = discover_store(store, spec)
    require_store(root)
    # Resolve only existing, verified identities. Callers that will later export
    # or execute must not reach a write-capable constructor on an unknown run or
    # an uninitialized registry merely to discover that identity.
    with ReadOnlyRepository(root) as repository:
        try:
            repository.get_run(str(run_id))
        except NotFoundError:
            raise LifecycleError(
                "not_found",
                "durable run or resource was not found",
                run_id=str(run_id),
                store=str(root),
            ) from None
    return str(run_id), root


def encode_cursor(store: Path, created_at: str, run_id: str) -> str:
    raw = (
        _Cursor(store=str(store), created_at=created_at, run_id=run_id)
        .model_dump_json()
        .encode()
    )
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str, store: Path) -> tuple[str, str]:
    try:
        if len(cursor) > 4096:
            raise ValueError
        raw = base64.b64decode(
            cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True
        )
        value = _Cursor.model_validate_json(raw)
        if value.store != str(store):
            raise ValueError
        return value.created_at, value.run_id
    except (ValueError, UnicodeError):
        raise LifecycleError(
            "usage", "pagination cursor is invalid for this store"
        ) from None


def inspect_run(repository: Repository, run_id: str) -> RunStatus:
    """Read a short WAL snapshot; source files and result payloads stay unopened."""
    active = executor_is_active(repository, run_id)
    repository.connection.execute("BEGIN")
    try:
        run = repository.get_run(run_id)
        manifest = (
            _load_manifest(repository, run_id, unsealed=False)
            if run.manifest_sha256 is not None
            else None
        )
        summary = repository.summary(run_id)
        row = repository.connection.execute(
            "SELECT (SELECT COUNT(*) FROM requests WHERE run_id=?) requests,"
            "(SELECT COUNT(*) FROM tasks WHERE run_id=? AND state IN ('invalid','refused','errored')) failed,"
            "(SELECT COUNT(*) FROM tasks WHERE run_id=? AND state='cancelled') cancelled,"
            "(SELECT COUNT(*) FROM tasks WHERE run_id=? AND state='blocked') blocked,"
            "(SELECT MAX(occurred_at) FROM events WHERE run_id=?) updated",
            (run_id, run_id, run_id, run_id, run_id),
        ).fetchone()
        assert row is not None
        certainties = {
            str(r[0]): int(r[1])
            for r in repository.connection.execute(
                "SELECT certainty,COUNT(*) FROM submissions WHERE run_id=? GROUP BY certainty",
                (run_id,),
            ).fetchall()
        }
        exports: list[tuple[str, str, str]] = []
        for format, view in (
            ("jsonl", "canonical"),
            ("csv", "canonical"),
            ("csv", "r"),
        ):
            latest = repository.connection.execute(
                "SELECT format,view,artifact_sha256 FROM exports WHERE run_id=? AND format=? AND view=? AND state='published' ORDER BY result_high_water_mark DESC,created_at DESC,export_id DESC LIMIT 1",
                (run_id, format, view),
            ).fetchone()
            if latest is not None:
                exports.append((str(latest[0]), str(latest[1]), str(latest[2])))
    finally:
        repository.connection.execute("ROLLBACK")
    planned_tasks = planned_requests = None
    if manifest is not None:
        count = next(
            source.record_count
            for source in manifest.sources
            if source.role == ("response" if run.task_kind == "rubric" else "pair")
        )
        planned_tasks = (
            count * len(manifest.traits) if run.task_kind == "rubric" else count
        )
        planned_requests = (
            count * len(manifest.trait_groups) if run.task_kind == "rubric" else count
        )
    directory = repository.run_directory(run_id)
    paths: dict[str, str | None] = {
        "store": str(repository.root),
        "run_directory": str(directory) if directory.is_dir() else None,
        "registry": str(repository.root / "registry.sqlite3"),
        "manifest": str(directory / "manifest.json") if manifest is not None else None,
    }
    for format, view, digest in exports:
        name = (
            "results_jsonl"
            if format == "jsonl"
            else ("results_r_csv" if view == "r" else "results_csv")
        )
        path = directory / "objects" / digest[:2] / digest
        if path.is_symlink() or not path.is_file():
            raise LifecycleError(
                "execution",
                "published export bytes are unavailable",
                run_id=run_id,
                store=str(repository.root),
            )
        # Inspection returns a durable reference. export_run independently verifies
        # exact bytes on use; a status query never rehashes whole result exports.
        paths[name] = str(path)
    unfinished = (
        None
        if planned_tasks is None
        else max(
            0,
            planned_tasks
            - summary.logical_completed_tasks
            - int(row["failed"])
            - int(row["cancelled"]),
        )
    )
    return RunStatus(
        run_id=run_id,
        run_status=run.state,
        task_kind=run.task_kind,
        provider=run.provider,
        endpoint=run.endpoint,
        model=run.model,
        capture=run.capture,
        created_at=run.created_at,
        updated_at=row["updated"] or run.created_at,
        planned_tasks=planned_tasks,
        planned_requests=planned_requests,
        materialized_tasks=summary.tasks,
        completed_tasks=summary.logical_completed_tasks,
        failed_tasks=row["failed"],
        cancelled_tasks=row["cancelled"],
        blocked_tasks=row["blocked"],
        pending_tasks=unfinished,
        requests=row["requests"],
        attempts=summary.attempts,
        results=summary.results,
        successful_results=summary.successful_results,
        unsuccessful_results=summary.unsuccessful_results,
        unresolved_submissions=summary.unresolved_submissions,
        retry_operations=summary.retry_operations,
        submission_certainty=SubmissionCounts(**certainties),
        executor_active=active,
        recovery_required=bool(
            summary.unresolved_submissions
            or (active is False and run.state in {"created", "submitted", "running"})
        ),
        artifacts=ArtifactPaths(**paths),
    )
