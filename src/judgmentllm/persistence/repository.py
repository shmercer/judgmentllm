"""Short transactional persistence operations, without execution orchestration.

Callers supply prepared identities and established outcome evidence. This module
does not contact providers, generate tasks, interpret grouped outputs, or decide
whether to retry. Its transaction context composes operations via savepoints.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Generator, Iterator, Sequence
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
    RetryAuthorizationError,
    RunRecord,
    SubmissionRecord,
    TaskRecord,
    utc_now,
    validate_transition,
)
from judgmentllm.persistence.schema import initialize
from judgmentllm.persistence.sources import InputRecord, SourceRecord
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
        self._depth = 0
        self._execution_owner: tuple[str, str] | None = None
        try:
            self.connection.row_factory = sqlite3.Row
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

    @contextmanager
    def execution_transaction(self, run_id: str, owner_token: str) -> Generator[None]:
        """Authorize one short execution write while the caller holds its OS lock.

        The token is checked under the write transaction, so stale executors
        cannot claim or advance work after ownership handoff. This does not
        acquire the lifetime OS lock; use ``ExecutorOwnership`` first. General
        low-level foundation methods remain available for preparation/fixtures.
        """
        previous = self._execution_owner
        if previous is not None and previous != (run_id, owner_token):
            raise OwnershipError("execution transaction owner does not match")
        with self.transaction():
            self.require_owner(run_id, owner_token)
            self._execution_owner = (run_id, owner_token)
            try:
                yield
                self.require_owner(run_id, owner_token)
            finally:
                self._execution_owner = previous

    def require_owner(self, run_id: str, owner_token: str) -> None:
        """Check the active diagnostics token, never heartbeat age or a PID."""
        owner = self.get_owner(run_id)
        if owner is None or owner.token != owner_token:
            raise OwnershipError("execution ownership token does not match")

    def get_execution_checkpoint(self, run_id: str) -> int:
        """Return the next preparation ordinal; this is not completion evidence."""
        row = self._one("SELECT checkpoint FROM runs WHERE run_id=?", (run_id,))
        value = row["checkpoint"]
        if value is None:
            return 0
        if (
            not isinstance(value, str)
            or len(value) > 19
            or not value.isascii()
            or not value.isdecimal()
            or str(int(value)) != value
            or int(value) > 2**63 - 1
        ):
            raise IntegrityError("execution preparation checkpoint is invalid")
        return int(value)

    def advance_execution_checkpoint(
        self, run_id: str, ordinal: int, *, owner_token: str
    ) -> None:
        """Advance prepared progress atomically with the caller's request writes.

        Recovery must still inspect prior unresolved submissions and results;
        this marker never authorizes skipping evidence or another acceptance.
        """
        if type(ordinal) is not int or not 0 <= ordinal <= 2**63 - 1:
            raise ValueError("checkpoint ordinal must be a nonnegative SQLite integer")
        with self.execution_transaction(run_id, owner_token):
            if ordinal < self.get_execution_checkpoint(run_id):
                raise IntegrityError("execution preparation checkpoint cannot decrease")
            self.connection.execute(
                "UPDATE runs SET checkpoint=? WHERE run_id=?", (str(ordinal), run_id)
            )

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
        if self._execution_owner is not None:
            execution_run, execution_token = self._execution_owner
            if run_id != execution_run:
                raise OwnershipError("execution transaction belongs to another run")
            self.require_owner(run_id, execution_token)
            owner_token = execution_token
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

    def iter_artifacts(self, run_id: str) -> Iterator[ArtifactIdentity]:
        """Traverse registered identities without loading every artifact at once."""
        self.get_run(run_id)
        cursor = self.connection.execute(
            "SELECT sha256,byte_length,media_type,relative_path FROM artifacts WHERE run_id=? ORDER BY sha256",
            (run_id,),
        )
        for row in cursor:
            yield ArtifactIdentity.model_validate(dict(row))

    def _source_write_owner(self, run_id: str, owner_token: str) -> None:
        owner = self.get_owner(run_id)
        if owner is None or owner.token != owner_token:
            raise OwnershipError("source preparation ownership token does not match")
        if self.get_run(run_id).state != "created":
            raise IntegrityError("source preparation is already sealed")

    def add_source(self, record: SourceRecord, *, owner_token: str) -> None:
        """Insert frozen original-file identity while holding preparation ownership."""
        record = _checked(record)
        with self.transaction():
            self._source_write_owner(record.run_id, owner_token)
            self._insert("sources", record)

    def get_source(self, run_id: str, source_id: str) -> SourceRecord:
        row = self._one(
            "SELECT * FROM sources WHERE run_id=? AND source_id=?", (run_id, source_id)
        )
        return SourceRecord.model_validate(dict(row))

    def iter_sources(self, run_id: str) -> Iterator[SourceRecord]:
        """Traverse sources in stable discovery ordinal and role order."""
        self.get_run(run_id)
        cursor = self.connection.execute(
            "SELECT * FROM sources WHERE run_id=? ORDER BY ordinal,role,source_id",
            (run_id,),
        )
        for row in cursor:
            yield SourceRecord.model_validate(dict(row))

    def update_source_snapshot(
        self, run_id: str, source_id: str, sha256: str, *, owner_token: str
    ) -> None:
        """Attach already published exact bytes once, before manifest sealing."""
        artifact = self.get_artifact(run_id, sha256)
        if artifact.relative_path is None:
            raise IntegrityError("source snapshot requires retained bytes")
        with self.transaction():
            self._source_write_owner(run_id, owner_token)
            source = self.get_source(run_id, source_id)
            if (source.raw_sha256, source.byte_length) != (
                sha256,
                artifact.byte_length,
            ):
                raise IntegrityError("source snapshot differs from original identity")
            if source.snapshot_sha256 not in (None, sha256):
                raise IntegrityError("source snapshot identity is already committed")
            self.connection.execute(
                "UPDATE sources SET snapshot_sha256=? WHERE run_id=? AND source_id=?",
                (sha256, run_id, source_id),
            )

    def add_input_records(
        self, records: Sequence[InputRecord], *, owner_token: str
    ) -> None:
        """Commit one bounded index batch; uniqueness rejects duplicate IDs."""
        if len(records) > 1024:
            raise ValueError("input index batches must contain at most 1024 records")
        checked = tuple(_checked(record) for record in records)
        with self.transaction():
            for run_id in {record.run_id for record in checked}:
                self._source_write_owner(run_id, owner_token)
            for record in checked:
                source = self.get_source(record.run_id, record.source_id)
                if source.role not in (
                    record.record_kind,
                    "responses" if record.record_kind == "response" else "pairs",
                ):
                    raise IntegrityError("input index kind differs from source role")
                if record.byte_offset + record.byte_length > source.byte_length:
                    raise IntegrityError("input record locator exceeds its source")
                self._insert("input_records", record)

    def get_input_record(
        self, run_id: str, record_kind: Literal["response", "pair"], record_id: str
    ) -> InputRecord:
        row = self._one(
            "SELECT * FROM input_records WHERE run_id=? AND record_kind=? AND record_id=?",
            (run_id, record_kind, record_id),
        )
        return InputRecord.model_validate(dict(row))

    def iter_input_records(
        self,
        run_id: str,
        record_kind: Literal["response", "pair"] | None = None,
        *,
        page_size: int = 128,
    ) -> Iterator[InputRecord]:
        """Traverse source/record order using bounded SQLite cursor pages."""
        if type(page_size) is not int or page_size < 1:
            raise ValueError("input index page size must be a positive integer")
        self.get_run(run_id)
        query = (
            "SELECT i.* FROM input_records i JOIN sources s "
            "ON s.run_id=i.run_id AND s.source_id=i.source_id WHERE i.run_id=?"
        )
        values: tuple[object, ...] = (run_id,)
        if record_kind is not None:
            query += " AND i.record_kind=?"
            values += (record_kind,)
        query += " ORDER BY s.ordinal,s.role,s.source_id,i.ordinal"
        cursor = self.connection.execute(query, values)
        while rows := cursor.fetchmany(page_size):
            for row in rows:
                yield InputRecord.model_validate(dict(row))

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

    def get_task(self, run_id: str, task_id: str) -> TaskRecord:
        return TaskRecord.model_validate(
            dict(
                self._one(
                    "SELECT * FROM tasks WHERE run_id=? AND task_id=?",
                    (run_id, task_id),
                )
            )
        )

    def get_request(self, run_id: str, request_uid: str) -> RequestRecord:
        return RequestRecord.model_validate(
            dict(
                self._one(
                    "SELECT run_id,request_uid,scheduling_ordinal,dependency_sha256,state FROM requests WHERE run_id=? AND request_uid=?",
                    (run_id, request_uid),
                )
            )
        )

    def get_attempt(self, run_id: str, attempt_id: str) -> AttemptRecord:
        return AttemptRecord.model_validate(
            dict(
                self._one(
                    "SELECT run_id,attempt_id,request_uid,attempt_number,prompt_sha256,payload_sha256,membership_sha256,authorization_id,state,next_eligible_at FROM attempts WHERE run_id=? AND attempt_id=?",
                    (run_id, attempt_id),
                )
            )
        )

    def get_submission(self, run_id: str, submission_id: str) -> SubmissionRecord:
        return SubmissionRecord.model_validate(
            dict(
                self._one(
                    "SELECT * FROM submissions WHERE run_id=? AND submission_id=?",
                    (run_id, submission_id),
                )
            )
        )

    def get_observation(self, run_id: str, observation_id: str) -> ObservationRecord:
        return ObservationRecord.model_validate(
            dict(
                self._one(
                    "SELECT * FROM observations WHERE run_id=? AND observation_id=?",
                    (run_id, observation_id),
                )
            )
        )

    def get_attempt_for_request(
        self, run_id: str, request_uid: str, *, attempt_number: int = 1
    ) -> AttemptRecord | None:
        """Look up an existing attempt without allocating a new identity."""
        row = self.connection.execute(
            "SELECT attempt_id FROM attempts WHERE run_id=? AND request_uid=? AND attempt_number=?",
            (run_id, request_uid, attempt_number),
        ).fetchone()
        return self.get_attempt(run_id, _text(row, "attempt_id")) if row else None

    def get_submission_for_attempt(
        self, run_id: str, attempt_id: str
    ) -> SubmissionRecord | None:
        row = self.connection.execute(
            "SELECT submission_id FROM submissions WHERE run_id=? AND attempt_id=?",
            (run_id, attempt_id),
        ).fetchone()
        return self.get_submission(run_id, _text(row, "submission_id")) if row else None

    def get_observation_for_submission(
        self, run_id: str, submission_id: str
    ) -> ObservationRecord | None:
        """Read the sole single-response observation; reject conflicting evidence."""
        rows = self.connection.execute(
            "SELECT observation_id FROM observations WHERE run_id=? AND submission_id=? ORDER BY observation_id LIMIT 2",
            (run_id, submission_id),
        ).fetchall()
        if len(rows) > 1:
            raise IntegrityError(
                "submission has conflicting single-response observations"
            )
        return (
            self.get_observation(run_id, _text(rows[0], "observation_id"))
            if rows
            else None
        )

    def request_task_ids(self, run_id: str, request_uid: str) -> tuple[str, ...]:
        self.get_request(run_id, request_uid)
        return tuple(
            _text(row, "task_id")
            for row in self.connection.execute(
                "SELECT task_id FROM request_tasks WHERE run_id=? AND request_uid=? ORDER BY position",
                (run_id, request_uid),
            )
        )

    def attempt_task_ids(self, run_id: str, attempt_id: str) -> tuple[str, ...]:
        self.get_attempt(run_id, attempt_id)
        return tuple(
            _text(row, "task_id")
            for row in self.connection.execute(
                "SELECT task_id FROM attempt_tasks WHERE run_id=? AND attempt_id=? ORDER BY position",
                (run_id, attempt_id),
            )
        )

    @staticmethod
    def _page_size(page_size: int) -> None:
        if type(page_size) is not int or page_size < 1:
            raise ValueError("page size must be positive")

    def iter_requests(
        self, run_id: str, *, page_size: int = 128
    ) -> Iterator[RequestRecord]:
        """Traverse indexed logical scheduling order using bounded keyset pages."""
        self._page_size(page_size)
        self.get_run(run_id)
        after = -1
        while True:
            rows = self.connection.execute(
                "SELECT run_id,request_uid,scheduling_ordinal,dependency_sha256,state FROM requests WHERE run_id=? AND scheduling_ordinal>? ORDER BY scheduling_ordinal LIMIT ?",
                (run_id, after, page_size),
            ).fetchall()
            if not rows:
                return
            for row in rows:
                record = RequestRecord.model_validate(dict(row))
                yield record
                after = record.scheduling_ordinal

    def iter_attempts(
        self, run_id: str, *, page_size: int = 128
    ) -> Iterator[AttemptRecord]:
        """Traverse attempts in original request order, without task materialization."""
        self._page_size(page_size)
        self.get_run(run_id)
        after = (-1, 0)
        while True:
            rows = self.connection.execute(
                "SELECT r.scheduling_ordinal,a.attempt_number,a.attempt_id FROM attempts a JOIN requests r ON r.run_id=a.run_id AND r.request_uid=a.request_uid WHERE a.run_id=? AND (r.scheduling_ordinal,a.attempt_number)>(?,?) ORDER BY r.scheduling_ordinal,a.attempt_number LIMIT ?",
                (run_id, *after, page_size),
            ).fetchall()
            if not rows:
                return
            for row in rows:
                yield self.get_attempt(run_id, _text(row, "attempt_id"))
                after = (
                    cast(int, row["scheduling_ordinal"]),
                    cast(int, row["attempt_number"]),
                )

    def iter_submissions(
        self, run_id: str, *, page_size: int = 128
    ) -> Iterator[SubmissionRecord]:
        """Traverse prepared live submissions through the bounded attempt index."""
        for attempt in self.iter_attempts(run_id, page_size=page_size):
            submission = self.get_submission_for_attempt(run_id, attempt.attempt_id)
            if submission is not None:
                yield submission

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
                    raise RetryAuthorizationError(
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

    def commit_results(
        self,
        run_id: str,
        attempt_id: str,
        results: Sequence[Result],
        *,
        observation_id: str | None = None,
        failure_submission_id: str | None = None,
        checkpoint: Callable[[], None] | None = None,
        unexpected_members: int = 0,
        unattributable_members: int = 0,
    ) -> bool:
        """Commit one complete physical outcome and its states in one transaction.

        An existing group must match every member, record and evidence identity.
        No partial existing group is repaired by inventing missing records.
        """
        if any(
            type(value) is not int or value < 0
            for value in (unexpected_members, unattributable_members)
        ):
            raise IntegrityError(
                "member diagnostic counts must be nonnegative integers"
            )
        with self.transaction():
            attempt = self.get_attempt(run_id, attempt_id)
            expected = self.attempt_task_ids(run_id, attempt_id)
            if tuple(result.task_id for result in results) != expected or any(
                result.run_id != run_id
                or result.attempt_id != attempt_id
                or result.request_uid != attempt.request_uid
                for result in results
            ):
                raise IntegrityError(
                    "result group differs from immutable attempt membership"
                )
            prior = self.get_results_for_attempt(run_id, attempt_id)
            if prior:
                if prior != tuple(results):
                    raise IntegrityError(
                        "observation result group conflicts with committed outcomes"
                    )
                evidence = self.connection.execute(
                    "SELECT observation_id,failure_submission_id FROM results WHERE run_id=? AND attempt_id=?",
                    (run_id, attempt_id),
                )
                if any(
                    (row[0], row[1]) != (observation_id, failure_submission_id)
                    for row in evidence
                ):
                    raise IntegrityError("committed result evidence conflicts")
                return False
            for record in results:
                self.append_result(
                    record,
                    observation_id=observation_id,
                    failure_submission_id=failure_submission_id,
                )
                if checkpoint is not None:
                    checkpoint()
            submission = self.get_submission_for_attempt(run_id, attempt_id)
            if submission is None:
                raise IntegrityError("result attempt lacks submission")
            if observation_id is not None:
                self.transition("attempt", run_id, attempt_id, "normalized")
                self.transition(
                    "submission", run_id, submission.submission_id, "resolved"
                )
            for result in results:
                task = self.get_task(run_id, result.task_id)
                if task.state != result.status:
                    self.transition("task", run_id, result.task_id, result.status)
            self.repair_request_state(run_id, attempt.request_uid)
            if observation_id is not None and (
                unexpected_members or unattributable_members
            ):
                self._event(
                    run_id,
                    "observation_recorded",
                    "observation",
                    observation_id,
                    target=f"unexpected_members={unexpected_members};unattributable_members={unattributable_members}",
                )
            return True

    def repair_request_state(self, run_id: str, request_uid: str) -> None:
        """Aggregate immutable parent membership, including successful siblings."""
        row = self.connection.execute(
            "SELECT COUNT(*),SUM(t.state='complete') FROM request_tasks rt JOIN tasks t ON t.run_id=rt.run_id AND t.task_id=rt.task_id WHERE rt.run_id=? AND rt.request_uid=?",
            (run_id, request_uid),
        ).fetchone()
        assert row is not None
        total, complete = int(row[0]), int(row[1] or 0)
        target = (
            "completed" if complete == total else "partial" if complete else "failed"
        )
        request = self.get_request(run_id, request_uid)
        if request.state != target:
            self.transition("request", run_id, request_uid, target)

    @staticmethod
    def _result_record(row: sqlite3.Row) -> Result:
        raw = _text(row, "record_json")
        kind = cast(dict[str, object], json.loads(raw)).get("task_kind")
        result = (
            RubricResult.model_validate_json(raw)
            if kind == "rubric"
            else PairwiseResult.model_validate_json(raw)
        )
        if semantic_digest("normalized-result", result) != _text(row, "record_sha256"):
            raise IntegrityError("normalized result digest does not match")
        return result

    def get_results_for_attempt(
        self, run_id: str, attempt_id: str
    ) -> tuple[Result, ...]:
        """Read one bounded physical group in immutable attempt member order."""
        rows = self.connection.execute(
            "SELECT r.record_json,r.record_sha256 FROM results r JOIN attempt_tasks a ON a.run_id=r.run_id AND a.attempt_id=r.attempt_id AND a.task_id=r.task_id WHERE r.run_id=? AND r.attempt_id=? ORDER BY a.position",
            (run_id, attempt_id),
        )
        return tuple(self._result_record(row) for row in rows)

    def get_result_for_attempt(
        self, run_id: str, attempt_id: str, *, task_id: str | None = None
    ) -> Result | None:
        """Read a committed single-member outcome before considering recovery."""
        rows = self.connection.execute(
            "SELECT record_json,record_sha256 FROM results WHERE run_id=? AND attempt_id=? AND (? IS NULL OR task_id=?) ORDER BY result_seq LIMIT 2",
            (run_id, attempt_id, task_id, task_id),
        ).fetchall()
        if len(rows) > 1:
            raise IntegrityError(
                "attempt has multiple results; single-member recovery is required"
            )
        if not rows:
            return None
        return self._result_record(rows[0])

    def iter_results(
        self, run_id: str, *, page_size: int = 128, high_water_mark: int | None = None
    ) -> Iterator[Result]:
        """Read a bounded immutable high-water snapshot, including failures."""
        if type(page_size) is not int or page_size < 1:
            raise ValueError("page size must be positive")
        if high_water_mark is not None and (
            type(high_water_mark) is not int or high_water_mark < 0
        ):
            raise ValueError("result high-water mark must be a nonnegative integer")
        self.get_run(run_id)
        maximum = self.connection.execute(
            "SELECT COALESCE(MAX(result_seq),0) FROM results WHERE run_id=?", (run_id,)
        ).fetchone()
        assert maximum is not None
        high_water = (
            cast(int, maximum[0]) if high_water_mark is None else high_water_mark
        )
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
        counts = self.connection.execute(
            "SELECT (SELECT COUNT(*) FROM tasks WHERE run_id=? AND state='complete') AS logical_completed_tasks,(SELECT COUNT(*) FROM results WHERE run_id=? AND status<>'complete') AS unsuccessful_results,(SELECT COUNT(*) FROM retry_operations WHERE run_id=?) AS retry_operations",
            (run_id, run_id, run_id),
        ).fetchone()
        assert counts is not None
        return ExecutionSummary.model_validate(
            {"run_id": run_id, **dict(row), **dict(counts)}
        )

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
