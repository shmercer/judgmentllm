"""Short transactional persistence operations, without execution orchestration.

Callers supply prepared identities and established outcome evidence. This module
does not contact providers, generate tasks, interpret grouped outputs, or decide
whether to retry. Its transaction context composes operations via savepoints.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Generator, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Literal, TypeVar, cast

from judgmentllm.io import Result, revalidate_result
from judgmentllm.persistence.artifacts import ArtifactStore
from judgmentllm.persistence.hashing import (
    bytes_digest,
    canonical_bytes,
    semantic_digest,
)
from judgmentllm.persistence.models import (
    ArtifactIdentity,
    AttemptRecord,
    EventKind,
    ExecutionEvent,
    ExecutionModel,
    ExecutionSummary,
    FailureEvidence,
    IntegrityError,
    ManifestReference,
    NotFoundError,
    ObservationRecord,
    OwnerInfo,
    OwnershipError,
    PersistenceError,
    RequestRecord,
    RetryAuthorization,
    RunRecord,
    SubmissionRecord,
    TaskRecord,
    utc_now,
    validate_transition,
)
from judgmentllm.persistence.schema import initialize
from judgmentllm.results import PairwiseResult, RubricResult

RecordT = TypeVar("RecordT", bound=ExecutionModel)
Entity = Literal["run", "request", "task", "attempt", "submission"]
_TABLES: dict[str, tuple[str, str]] = {
    "run": ("runs", "run_id"),
    "request": ("requests", "request_uid"),
    "task": ("tasks", "task_id"),
    "attempt": ("attempts", "attempt_id"),
    "submission": ("submissions", "submission_id"),
}


def _checked(record: RecordT) -> RecordT:
    return type(record).model_validate_json(canonical_bytes(record))


def _text(row: sqlite3.Row, key: str) -> str:
    return cast(str, row[key])


class Repository:
    """One process/thread's connection to a local durable registry.

    Do not share a connection across threads. Create another Repository instead.
    The connection attribute is exposed for internal migrations/constraint tests;
    applications should use the typed operations. Raw SQL is not a scoring API.
    """

    def __init__(self, root: str | Path, *, busy_timeout_ms: int = 5000) -> None:
        if type(busy_timeout_ms) is not int or busy_timeout_ms < 0:
            raise ValueError("busy timeout must be a nonnegative integer")
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(
            self.root / "registry.sqlite3",
            isolation_level=None,
            timeout=busy_timeout_ms / 1000,
        )
        self.connection.row_factory = sqlite3.Row
        self._depth = 0
        try:
            self.connection.execute("PRAGMA foreign_keys=ON")
            self.connection.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
            mode = self.connection.execute("PRAGMA journal_mode=WAL").fetchone()
            if mode is None or str(mode[0]).lower() != "wal":
                raise PersistenceError("WAL mode is unavailable")
            self.connection.execute("PRAGMA synchronous=FULL")
            initialize(self.connection)
        except BaseException:
            self.connection.close()
            raise

    def __enter__(self) -> Repository:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self.connection.close()

    @contextmanager
    def transaction(self) -> Generator[None]:
        """Compose short writes; any exception rolls back their local boundary."""
        depth = self._depth
        savepoint = f"operation_{depth}"
        try:
            self.connection.execute(
                "BEGIN IMMEDIATE" if depth == 0 else f"SAVEPOINT {savepoint}"
            )
            self._depth += 1
            try:
                yield
                self.connection.execute(
                    "COMMIT" if depth == 0 else f"RELEASE {savepoint}"
                )
            except BaseException:
                if self.connection.in_transaction:
                    if depth == 0:
                        self.connection.rollback()
                    else:
                        self.connection.execute(f"ROLLBACK TO {savepoint}")
                        self.connection.execute(f"RELEASE {savepoint}")
                raise
            finally:
                self._depth -= 1
        except sqlite3.IntegrityError:
            raise IntegrityError("database constraint rejected the operation") from None
        except sqlite3.Error:
            raise PersistenceError("database operation failed") from None

    def _one(self, sql: str, values: Sequence[object]) -> sqlite3.Row:
        row = cast(sqlite3.Row | None, self.connection.execute(sql, values).fetchone())
        if row is None:
            raise NotFoundError("durable record was not found")
        return row

    def _insert(self, table: str, record: ExecutionModel) -> None:
        data = record.model_dump(exclude={"schema_version"})
        names = ",".join(data)
        placeholders = ",".join("?" for _ in data)
        self.connection.execute(
            f"INSERT INTO {table} ({names}) VALUES ({placeholders})",
            tuple(data.values()),
        )

    def _event(
        self,
        run_id: str,
        kind: EventKind,
        entity_kind: str,
        entity_id: str,
        previous: str | None = None,
        target: str | None = None,
        owner_token: str | None = None,
    ) -> int:
        cursor = self.connection.execute(
            "INSERT INTO events(run_id,kind,entity_kind,entity_id,previous_state,new_state,occurred_at,owner_token) VALUES (?,?,?,?,?,?,?,?)",
            (
                run_id,
                kind,
                entity_kind,
                entity_id,
                previous,
                target,
                utc_now(),
                owner_token,
            ),
        )
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    def run_directory(self, run_id: str) -> Path:
        """Avoid using caller-supplied identifiers as filesystem components."""
        self.get_run(run_id)
        return self.root / "runs" / bytes_digest(run_id.encode("utf-8"))

    def create_run(self, record: RunRecord) -> None:
        record = _checked(record)
        if record.state != "created" or record.manifest_sha256 is not None:
            raise IntegrityError("new runs must be unsealed and created")
        with self.transaction():
            self._insert("runs", record)
            self._event(record.run_id, "created", "run", record.run_id)

    def get_run(self, run_id: str) -> RunRecord:
        row = self._one(
            "SELECT run_id,task_kind,provider,endpoint,model,capture,state,manifest_sha256,created_at FROM runs WHERE run_id=?",
            (run_id,),
        )
        return RunRecord.model_validate(dict(row))

    def register_artifact(self, run_id: str, identity: ArtifactIdentity) -> None:
        identity = _checked(identity)
        ArtifactStore(self.run_directory(run_id)).verify(identity)
        with self.transaction():
            self._register_artifact(run_id, identity)

    def _register_artifact(self, run_id: str, identity: ArtifactIdentity) -> None:
        """Register an already verified identity, without filesystem operations."""
        row = self.connection.execute(
            "SELECT sha256,byte_length,media_type,relative_path FROM artifacts WHERE run_id=? AND sha256=?",
            (run_id, identity.sha256),
        ).fetchone()
        if row is not None:
            if ArtifactIdentity.model_validate(dict(row)) != identity:
                raise IntegrityError(
                    "artifact identity is already registered differently"
                )
            return
        self.connection.execute(
            "INSERT INTO artifacts(run_id,sha256,byte_length,media_type,relative_path) VALUES (?,?,?,?,?)",
            (
                run_id,
                identity.sha256,
                identity.byte_length,
                identity.media_type,
                identity.relative_path,
            ),
        )
        self._event(run_id, "artifact_registered", "artifact", identity.sha256)

    def seal_run(self, run_id: str, manifest: ManifestReference) -> None:
        """Attach retained manifest bytes once; manifest contents arrive in phase 2."""
        manifest = _checked(manifest)
        ArtifactStore(self.run_directory(run_id)).verify(manifest.artifact)
        with self.transaction():
            current = self.get_run(run_id)
            if current.state != "created" or current.manifest_sha256 is not None:
                raise IntegrityError(
                    "manifest can only be sealed once on a created run"
                )
            self._register_artifact(run_id, manifest.artifact)
            self.connection.execute(
                "UPDATE runs SET state='submitted',manifest_sha256=? WHERE run_id=?",
                (manifest.artifact.sha256, run_id),
            )
            self._event(
                run_id, "manifest_sealed", "run", run_id, "created", "submitted"
            )

    def get_artifact(self, run_id: str, sha256: str) -> ArtifactIdentity:
        """Load an immutable identity and verify any bytes promised retained."""
        row = self._one(
            "SELECT sha256,byte_length,media_type,relative_path FROM artifacts WHERE run_id=? AND sha256=?",
            (run_id, sha256),
        )
        identity = ArtifactIdentity.model_validate(dict(row))
        ArtifactStore(self.run_directory(run_id)).verify(identity)
        return identity

    def get_manifest(self, run_id: str) -> ManifestReference:
        """Return a verified frozen reference; never silently repair its bytes."""
        run = self.get_run(run_id)
        if run.manifest_sha256 is None:
            raise NotFoundError("run has no sealed manifest")
        return ManifestReference(
            artifact=self.get_artifact(run_id, run.manifest_sha256)
        )

    def create_task(self, record: TaskRecord) -> None:
        record = _checked(record)
        if record.state != "pending":
            raise IntegrityError("new task must be pending")
        with self.transaction():
            self._insert("tasks", record)
            self._event(record.run_id, "created", "task", record.task_id)

    def create_request(self, record: RequestRecord, task_ids: Sequence[str]) -> None:
        record = _checked(record)
        if (
            record.state != "planned"
            or not task_ids
            or len(set(task_ids)) != len(task_ids)
        ):
            raise IntegrityError("new request requires unique ordered tasks")
        with self.transaction():
            self._insert("requests", record)
            self.connection.executemany(
                "INSERT INTO request_tasks(run_id,request_uid,task_id,position) VALUES (?,?,?,?)",
                (
                    (record.run_id, record.request_uid, task_id, position)
                    for position, task_id in enumerate(task_ids)
                ),
            )
            self.connection.execute(
                "UPDATE requests SET membership_sealed=1 WHERE run_id=? AND request_uid=?",
                (record.run_id, record.request_uid),
            )
            self._event(record.run_id, "created", "request", record.request_uid)

    def authorize_retry(self, authorization: RetryAuthorization) -> None:
        """Record an identity, not a retry decision or execution operation."""
        authorization = _checked(authorization)
        with self.transaction():
            row = self.connection.execute(
                "SELECT run_id,operation_id,selector_sha256,allow_completed FROM retry_operations WHERE run_id=? AND operation_id=?",
                (authorization.run_id, authorization.operation_id),
            ).fetchone()
            if row is not None:
                data = dict(row)
                data["allow_completed"] = bool(data["allow_completed"])
                if RetryAuthorization.model_validate(data) != authorization:
                    raise IntegrityError(
                        "retry identity is already authorized differently"
                    )
                return
            self.connection.execute(
                "INSERT INTO retry_operations(run_id,operation_id,selector_sha256,allow_completed,created_at) VALUES (?,?,?,?,?)",
                (
                    authorization.run_id,
                    authorization.operation_id,
                    authorization.selector_sha256,
                    int(authorization.allow_completed),
                    utc_now(),
                ),
            )
            self._event(
                authorization.run_id,
                "retry_authorized",
                "retry",
                authorization.operation_id,
            )

    def create_attempt(self, record: AttemptRecord, task_ids: Sequence[str]) -> None:
        record = _checked(record)
        if (
            record.state != "prepared"
            or not task_ids
            or len(set(task_ids)) != len(task_ids)
        ):
            raise IntegrityError("new attempt requires unique ordered task membership")
        if (
            semantic_digest("attempt-membership", list(task_ids))
            != record.membership_sha256
        ):
            raise IntegrityError("attempt membership digest does not match")
        with self.transaction():
            self._insert("attempts", record)
            self.connection.executemany(
                "INSERT INTO attempt_tasks(run_id,attempt_id,request_uid,task_id,position) VALUES (?,?,?,?,?)",
                (
                    (
                        record.run_id,
                        record.attempt_id,
                        record.request_uid,
                        task_id,
                        position,
                    )
                    for position, task_id in enumerate(task_ids)
                ),
            )
            self.connection.execute(
                "UPDATE attempts SET membership_sealed=1 WHERE run_id=? AND attempt_id=?",
                (record.run_id, record.attempt_id),
            )
            self._event(record.run_id, "created", "attempt", record.attempt_id)

    def create_submission(self, record: SubmissionRecord) -> None:
        record = _checked(record)
        if (
            record.state != "prepared"
            or record.certainty != "not_started"
            or record.remote_id is not None
            or record.batch_uid is not None
        ):
            raise IntegrityError("new submission must be a prepared local attempt")
        with self.transaction():
            self._insert("submissions", record)
            self._event(record.run_id, "created", "submission", record.submission_id)

    def transition(
        self,
        entity: Entity,
        run_id: str,
        entity_id: str,
        target: str,
        *,
        authorization_id: str | None = None,
        remote_id: str | None = None,
    ) -> None:
        """Persist an explicitly supplied transition; never infer provider facts."""
        if entity not in _TABLES:
            raise IntegrityError("unknown execution entity")
        table, column = _TABLES[entity]
        with self.transaction():
            row = self._one(
                f"SELECT * FROM {table} WHERE run_id=? AND {column}=?",
                (run_id, entity_id),
            )
            previous = _text(row, "state")
            validate_transition(entity, previous, target)
            if previous in {"complete", "completed"}:
                auth = self.connection.execute(
                    "SELECT allow_completed FROM retry_operations WHERE run_id=? AND operation_id=?",
                    (run_id, authorization_id),
                ).fetchone()
                if auth is None or not auth[0]:
                    raise IntegrityError(
                        "completed work requires explicit retry authorization"
                    )
            if entity == "submission":
                certainty = {
                    "submitting": "unknown",
                    "acknowledged": "accepted",
                    "confirmed_not_submitted": "not_submitted",
                    "resolved": "accepted",
                }.get(target, _text(row, "certainty"))
                self.connection.execute(
                    "UPDATE submissions SET state=?,certainty=?,remote_id=COALESCE(?,remote_id) WHERE run_id=? AND submission_id=?",
                    (target, certainty, remote_id, run_id, entity_id),
                )
            else:
                if remote_id is not None:
                    raise IntegrityError("remote identity belongs to a submission")
                self.connection.execute(
                    f"UPDATE {table} SET state=? WHERE run_id=? AND {column}=?",
                    (target, run_id, entity_id),
                )
            self._event(run_id, "transition", entity, entity_id, previous, target)

    def record_observation(self, record: ObservationRecord) -> None:
        record = _checked(record)
        with self.transaction():
            self._insert("observations", record)
            self._event(
                record.run_id,
                "observation_recorded",
                "observation",
                record.observation_id,
            )

    def record_failure(self, evidence: FailureEvidence) -> None:
        evidence = _checked(evidence)
        with self.transaction():
            event_seq = self._event(
                evidence.run_id, "failure_recorded", "attempt", evidence.attempt_id
            )
            self.connection.execute(
                "INSERT INTO submission_failures(run_id,attempt_id,submission_id,kind,event_seq) VALUES (?,?,?,?,?)",
                (
                    evidence.run_id,
                    evidence.attempt_id,
                    evidence.submission_id,
                    evidence.kind,
                    event_seq,
                ),
            )

    def append_result(
        self,
        record: Result,
        *,
        observation_id: str | None = None,
        failure_submission_id: str | None = None,
    ) -> int:
        """Append validated outcomes only when actual attempt evidence exists."""
        try:
            record = revalidate_result(record)
        except (ValueError, TypeError):
            raise IntegrityError("normalized result validation failed") from None
        if (observation_id is None) == (failure_submission_id is None):
            raise IntegrityError(
                "result requires exactly one established outcome evidence source"
            )
        data = canonical_bytes(record).decode("utf-8")
        with self.transaction():
            cursor = self.connection.execute(
                "INSERT INTO results(run_id,task_id,request_uid,attempt_id,observation_id,failure_submission_id,status,record_json,record_sha256) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    record.run_id,
                    record.task_id,
                    record.request_uid,
                    record.attempt_id,
                    observation_id,
                    failure_submission_id,
                    record.status,
                    data,
                    semantic_digest("normalized-result", record),
                ),
            )
            assert cursor.lastrowid is not None
            self._event(
                record.run_id, "result_recorded", "result", str(cursor.lastrowid)
            )
            return cursor.lastrowid

    def iter_results(self, run_id: str, *, page_size: int = 128) -> Iterator[Result]:
        """Read a bounded immutable high-water snapshot, including failures."""
        if type(page_size) is not int or page_size < 1:
            raise ValueError("page size must be positive")
        self.get_run(run_id)
        maximum = self.connection.execute(
            "SELECT COALESCE(MAX(result_seq),0) FROM results WHERE run_id=?", (run_id,)
        ).fetchone()
        assert maximum is not None
        high_water = cast(int, maximum[0])
        after = 0
        while True:
            rows = self.connection.execute(
                "SELECT result_seq,record_json,record_sha256 FROM results WHERE run_id=? AND result_seq>? AND result_seq<=? ORDER BY result_seq LIMIT ?",
                (run_id, after, high_water, page_size),
            ).fetchall()
            if not rows:
                return
            for row in rows:
                raw = _text(row, "record_json")
                # The table CHECK restricts task kind; model validation remains
                # the complete schema boundary even after database corruption.
                kind = cast(dict[str, object], json.loads(raw)).get("task_kind")
                record = (
                    RubricResult.model_validate_json(raw)
                    if kind == "rubric"
                    else PairwiseResult.model_validate_json(raw)
                )
                if semantic_digest("normalized-result", record) != _text(
                    row, "record_sha256"
                ):
                    raise IntegrityError("normalized result digest does not match")
                yield record
                after = cast(int, row["result_seq"])

    def events(self, run_id: str) -> Iterator[ExecutionEvent]:
        self.get_run(run_id)
        cursor = self.connection.execute(
            "SELECT * FROM events WHERE run_id=? ORDER BY event_seq", (run_id,)
        )
        for row in cursor:
            yield ExecutionEvent.model_validate(dict(row))

    def summary(self, run_id: str) -> ExecutionSummary:
        row = self._one(
            "SELECT r.state,(SELECT COUNT(*) FROM tasks WHERE run_id=r.run_id) AS tasks,(SELECT COUNT(*) FROM attempts WHERE run_id=r.run_id) AS attempts,(SELECT COUNT(*) FROM results WHERE run_id=r.run_id) AS results,(SELECT COUNT(*) FROM results WHERE run_id=r.run_id AND status='complete') AS successful_results,(SELECT COUNT(*) FROM submissions WHERE run_id=r.run_id AND state IN ('submitting','uncertain','acknowledged')) AS unresolved_submissions FROM runs r WHERE r.run_id=?",
            (run_id,),
        )
        return ExecutionSummary.model_validate({"run_id": run_id, **dict(row)})

    def get_owner(self, run_id: str) -> OwnerInfo | None:
        row = self._one(
            "SELECT owner_token,owner_pid,owner_host,owner_acquired_at,owner_heartbeat_at FROM runs WHERE run_id=?",
            (run_id,),
        )
        if row["owner_token"] is None:
            return None
        return OwnerInfo(
            token=row["owner_token"],
            pid=row["owner_pid"],
            host=row["owner_host"],
            acquired_at=row["owner_acquired_at"],
            heartbeat_at=row["owner_heartbeat_at"],
        )

    def set_owner(self, run_id: str, owner: OwnerInfo) -> None:
        """Called only while holding the lifetime OS lock; no lease takeover."""
        owner = _checked(owner)
        with self.transaction():
            previous = self.get_owner(run_id)
            self.connection.execute(
                "UPDATE runs SET owner_token=?,owner_pid=?,owner_host=?,owner_acquired_at=?,owner_heartbeat_at=? WHERE run_id=?",
                (
                    owner.token,
                    owner.pid,
                    owner.host,
                    owner.acquired_at,
                    owner.heartbeat_at,
                    run_id,
                ),
            )
            self._event(
                run_id,
                "owner_recovered" if previous else "owner_acquired",
                "run",
                run_id,
                owner_token=owner.token,
            )

    def clear_owner(self, run_id: str, token: str) -> None:
        with self.transaction():
            cursor = self.connection.execute(
                "UPDATE runs SET owner_token=NULL,owner_pid=NULL,owner_host=NULL,owner_acquired_at=NULL,owner_heartbeat_at=NULL WHERE run_id=? AND owner_token=?",
                (run_id, token),
            )
            if cursor.rowcount != 1:
                raise OwnershipError("ownership token does not match")
            self._event(run_id, "owner_released", "run", run_id, owner_token=token)

    def heartbeat_owner(self, run_id: str, token: str) -> None:
        with self.transaction():
            cursor = self.connection.execute(
                "UPDATE runs SET owner_heartbeat_at=? WHERE run_id=? AND owner_token=?",
                (utc_now(), run_id, token),
            )
            if cursor.rowcount != 1:
                raise OwnershipError("ownership token does not match")
