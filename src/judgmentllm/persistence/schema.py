"""Version-one SQLite schema and atomic, checksummed initialization.

This module contains persistence only. Reserved source/batch/export tables have
no ingestion, provider, scheduling, or lifecycle service implementation.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

from judgmentllm.persistence.models import TRANSITIONS, SchemaVersionError, utc_now

SCHEMA_VERSION = 1


def _choices(values: tuple[str, ...] | list[str]) -> str:
    return ",".join(f"'{value}'" for value in values)


def _states(entity: str) -> str:
    return _choices(list(TRANSITIONS[entity]))


def _hash(column: str) -> str:
    return f"length({column})=64 AND {column} NOT GLOB '*[^0-9a-f]*'"


_TABLES = (
    """CREATE TABLE schema_migrations (
        version INTEGER PRIMARY KEY CHECK(version > 0),
        name TEXT NOT NULL,
        checksum TEXT NOT NULL,
        schema_sha256 TEXT NOT NULL,
        applied_at TEXT NOT NULL
    )""",
    f"""CREATE TABLE runs (
        run_id TEXT PRIMARY KEY NOT NULL,
        task_kind TEXT NOT NULL CHECK(task_kind IN ('rubric','pairwise')),
        provider TEXT NOT NULL, endpoint TEXT NOT NULL, model TEXT NOT NULL,
        capture TEXT NOT NULL CHECK(capture IN ('full','minimal')),
        state TEXT NOT NULL DEFAULT 'created' CHECK(state IN ({_states("run")})),
        manifest_sha256 TEXT CHECK({_hash("manifest_sha256")}),
        created_at TEXT NOT NULL,
        owner_token TEXT, owner_pid INTEGER CHECK(owner_pid > 0), owner_host TEXT,
        owner_acquired_at TEXT, owner_heartbeat_at TEXT, checkpoint TEXT,
        CHECK((owner_token IS NULL AND owner_pid IS NULL AND owner_host IS NULL
          AND owner_acquired_at IS NULL AND owner_heartbeat_at IS NULL)
          OR (owner_token IS NOT NULL AND owner_pid IS NOT NULL AND owner_host IS NOT NULL
          AND owner_acquired_at IS NOT NULL AND owner_heartbeat_at IS NOT NULL)),
        CHECK(state='created' OR manifest_sha256 IS NOT NULL OR state IN ('failed','cancelled')),
        FOREIGN KEY(run_id, manifest_sha256) REFERENCES artifacts(run_id, sha256)
    )""",
    f"""CREATE TABLE artifacts (
        run_id TEXT NOT NULL, sha256 TEXT NOT NULL CHECK({_hash("sha256")}),
        byte_length INTEGER NOT NULL CHECK(byte_length >= 0),
        media_type TEXT NOT NULL, relative_path TEXT,
        PRIMARY KEY(run_id, sha256),
        FOREIGN KEY(run_id) REFERENCES runs(run_id)
    )""",
    f"""CREATE TABLE sources (
        run_id TEXT NOT NULL, source_id TEXT NOT NULL, role TEXT NOT NULL,
        ordinal INTEGER NOT NULL CHECK(ordinal >= 0), original_location TEXT NOT NULL,
        raw_sha256 TEXT NOT NULL CHECK({_hash("raw_sha256")}),
        byte_length INTEGER NOT NULL CHECK(byte_length >= 0),
        canonical_sha256 TEXT CHECK({_hash("canonical_sha256")}),
        format TEXT NOT NULL, snapshot_sha256 TEXT, generation_json TEXT,
        PRIMARY KEY(run_id, source_id), UNIQUE(run_id, role, ordinal),
        FOREIGN KEY(run_id) REFERENCES runs(run_id),
        FOREIGN KEY(run_id, snapshot_sha256) REFERENCES artifacts(run_id, sha256),
        CHECK(generation_json IS NULL OR json_valid(generation_json))
    )""",
    f"""CREATE TABLE input_records (
        run_id TEXT NOT NULL, record_kind TEXT NOT NULL CHECK(record_kind IN ('response','pair')),
        record_id TEXT NOT NULL, source_id TEXT NOT NULL,
        ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
        record_sha256 TEXT NOT NULL CHECK({_hash("record_sha256")}),
        byte_offset INTEGER NOT NULL CHECK(byte_offset >= 0),
        byte_length INTEGER NOT NULL CHECK(byte_length >= 0),
        structural_json TEXT NOT NULL CHECK(json_valid(structural_json)),
        snapshot_sha256 TEXT,
        PRIMARY KEY(run_id, record_kind, record_id), UNIQUE(run_id, source_id, ordinal),
        FOREIGN KEY(run_id, source_id) REFERENCES sources(run_id, source_id),
        FOREIGN KEY(run_id, snapshot_sha256) REFERENCES artifacts(run_id, sha256)
    )""",
    f"""CREATE TABLE tasks (
        run_id TEXT NOT NULL, task_id TEXT NOT NULL, trait_id TEXT NOT NULL,
        item_id TEXT, pair_uid TEXT, pair_id TEXT,
        scheduling_ordinal INTEGER NOT NULL CHECK(scheduling_ordinal >= 0),
        state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ({_states("task")})),
        PRIMARY KEY(run_id, task_id), UNIQUE(run_id, scheduling_ordinal),
        CHECK((item_id IS NOT NULL AND pair_uid IS NULL AND pair_id IS NULL)
          OR (item_id IS NULL AND pair_uid IS NOT NULL AND pair_id IS NOT NULL)),
        FOREIGN KEY(run_id) REFERENCES runs(run_id)
    )""",
    f"""CREATE TABLE requests (
        run_id TEXT NOT NULL, request_uid TEXT NOT NULL,
        scheduling_ordinal INTEGER NOT NULL CHECK(scheduling_ordinal >= 0),
        dependency_sha256 TEXT NOT NULL CHECK({_hash("dependency_sha256")}),
        state TEXT NOT NULL DEFAULT 'planned' CHECK(state IN ({_states("request")})),
        membership_sealed INTEGER NOT NULL DEFAULT 0 CHECK(membership_sealed IN (0,1)),
        PRIMARY KEY(run_id, request_uid), UNIQUE(run_id, scheduling_ordinal),
        FOREIGN KEY(run_id) REFERENCES runs(run_id)
    )""",
    f"""CREATE TABLE retry_operations (
        run_id TEXT NOT NULL, operation_id TEXT NOT NULL,
        selector_sha256 TEXT NOT NULL CHECK({_hash("selector_sha256")}),
        allow_completed INTEGER NOT NULL CHECK(allow_completed IN (0,1)),
        created_at TEXT NOT NULL,
        PRIMARY KEY(run_id, operation_id), FOREIGN KEY(run_id) REFERENCES runs(run_id)
    )""",
    f"""CREATE TABLE attempts (
        run_id TEXT NOT NULL, attempt_id TEXT NOT NULL, request_uid TEXT NOT NULL,
        attempt_number INTEGER NOT NULL CHECK(attempt_number >= 1),
        prompt_sha256 TEXT NOT NULL, payload_sha256 TEXT NOT NULL,
        membership_sha256 TEXT NOT NULL CHECK({_hash("membership_sha256")}),
        authorization_id TEXT,
        state TEXT NOT NULL DEFAULT 'prepared' CHECK(state IN ({_states("attempt")})),
        next_eligible_at TEXT,
        input_tokens INTEGER CHECK(input_tokens >= 0),
        output_tokens INTEGER CHECK(output_tokens >= 0),
        cache_read_tokens INTEGER CHECK(cache_read_tokens >= 0),
        cache_write_tokens INTEGER CHECK(cache_write_tokens >= 0),
        membership_sealed INTEGER NOT NULL DEFAULT 0 CHECK(membership_sealed IN (0,1)),
        PRIMARY KEY(run_id, attempt_id), UNIQUE(run_id, request_uid, attempt_number),
        UNIQUE(run_id, attempt_id, request_uid),
        FOREIGN KEY(run_id, request_uid) REFERENCES requests(run_id, request_uid),
        FOREIGN KEY(run_id, prompt_sha256) REFERENCES artifacts(run_id, sha256),
        FOREIGN KEY(run_id, payload_sha256) REFERENCES artifacts(run_id, sha256),
        FOREIGN KEY(run_id, authorization_id) REFERENCES retry_operations(run_id, operation_id)
    )""",
    """CREATE TABLE attempt_tasks (
        run_id TEXT NOT NULL, attempt_id TEXT NOT NULL, request_uid TEXT NOT NULL,
        task_id TEXT NOT NULL, position INTEGER NOT NULL CHECK(position >= 0),
        PRIMARY KEY(run_id, attempt_id, task_id), UNIQUE(run_id, attempt_id, position),
        FOREIGN KEY(run_id, attempt_id, request_uid)
          REFERENCES attempts(run_id, attempt_id, request_uid),
        FOREIGN KEY(run_id, request_uid, task_id)
          REFERENCES request_tasks(run_id, request_uid, task_id)
    )""",
    """CREATE TABLE request_tasks (
        run_id TEXT NOT NULL, request_uid TEXT NOT NULL, task_id TEXT NOT NULL,
        position INTEGER NOT NULL CHECK(position >= 0),
        PRIMARY KEY(run_id, request_uid, task_id), UNIQUE(run_id, request_uid, position),
        FOREIGN KEY(run_id, request_uid) REFERENCES requests(run_id, request_uid),
        FOREIGN KEY(run_id, task_id) REFERENCES tasks(run_id, task_id)
    )""",
    f"""CREATE TABLE batches (
        run_id TEXT NOT NULL, batch_uid TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'prepared',
        provider TEXT NOT NULL, mode TEXT NOT NULL CHECK(mode='batch_native'),
        membership_sha256 TEXT NOT NULL CHECK({_hash("membership_sha256")}),
        remote_id TEXT, created_at TEXT NOT NULL,
        PRIMARY KEY(run_id, batch_uid), FOREIGN KEY(run_id) REFERENCES runs(run_id)
    )""",
    """CREATE TABLE batch_members (
        run_id TEXT NOT NULL, batch_uid TEXT NOT NULL, attempt_id TEXT NOT NULL,
        position INTEGER NOT NULL CHECK(position >= 0),
        PRIMARY KEY(run_id, batch_uid, attempt_id), UNIQUE(run_id, batch_uid, position),
        UNIQUE(run_id, attempt_id),
        FOREIGN KEY(run_id, batch_uid) REFERENCES batches(run_id, batch_uid),
        FOREIGN KEY(run_id, attempt_id) REFERENCES attempts(run_id, attempt_id)
    )""",
    f"""CREATE TABLE submissions (
        run_id TEXT NOT NULL, submission_id TEXT NOT NULL,
        correlation_id TEXT NOT NULL UNIQUE,
        attempt_id TEXT, batch_uid TEXT, payload_sha256 TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'prepared' CHECK(state IN ({_states("submission")})),
        certainty TEXT NOT NULL DEFAULT 'not_started'
          CHECK(certainty IN ('not_started','not_submitted','unknown','accepted')),
        remote_id TEXT,
        PRIMARY KEY(run_id, submission_id), UNIQUE(run_id, attempt_id),
        UNIQUE(run_id, batch_uid), UNIQUE(run_id, submission_id, attempt_id),
        CHECK((attempt_id IS NULL) != (batch_uid IS NULL)),
        CHECK((state='prepared' AND certainty='not_started' AND remote_id IS NULL)
          OR (state='submitting' AND certainty='unknown')
          OR (state='acknowledged' AND certainty='accepted')
          OR (state='confirmed_not_submitted' AND certainty='not_submitted' AND remote_id IS NULL)
          OR (state='uncertain' AND certainty IN ('unknown','accepted'))
          OR (state='resolved' AND certainty IN ('not_submitted','accepted'))),
        FOREIGN KEY(run_id, attempt_id) REFERENCES attempts(run_id, attempt_id),
        FOREIGN KEY(run_id, batch_uid) REFERENCES batches(run_id, batch_uid),
        FOREIGN KEY(run_id, payload_sha256) REFERENCES artifacts(run_id, sha256)
    )""",
    """CREATE TABLE observations (
        run_id TEXT NOT NULL, observation_id TEXT NOT NULL, submission_id TEXT NOT NULL,
        attempt_id TEXT NOT NULL, observation_key TEXT NOT NULL,
        kind TEXT NOT NULL CHECK(kind IN ('response','refusal','provider_error','confirmed_cancellation')),
        raw_sha256 TEXT, received_at TEXT NOT NULL,
        PRIMARY KEY(run_id, observation_id), UNIQUE(run_id, submission_id, observation_key),
        UNIQUE(run_id, observation_id, attempt_id),
        FOREIGN KEY(run_id, submission_id, attempt_id)
          REFERENCES submissions(run_id, submission_id, attempt_id),
        FOREIGN KEY(run_id, raw_sha256) REFERENCES artifacts(run_id, sha256)
    )""",
    """CREATE TABLE events (
        event_seq INTEGER PRIMARY KEY CHECK(event_seq >= 1), run_id TEXT NOT NULL,
        kind TEXT NOT NULL CHECK(kind IN ('created','transition','manifest_sealed',
          'artifact_registered','observation_recorded','result_recorded','failure_recorded',
          'retry_authorized','owner_acquired','owner_recovered','owner_released')),
        entity_kind TEXT NOT NULL CHECK(entity_kind IN ('run','request','task','attempt',
          'submission','artifact','observation','result','retry')),
        entity_id TEXT NOT NULL, previous_state TEXT, new_state TEXT,
        occurred_at TEXT NOT NULL, owner_token TEXT,
        UNIQUE(run_id, event_seq), FOREIGN KEY(run_id) REFERENCES runs(run_id)
    )""",
    """CREATE TABLE submission_failures (
        run_id TEXT NOT NULL, attempt_id TEXT NOT NULL, submission_id TEXT NOT NULL,
        kind TEXT NOT NULL CHECK(kind IN ('transport_not_submitted','confirmed_cancellation')),
        event_seq INTEGER NOT NULL,
        PRIMARY KEY(run_id, attempt_id), UNIQUE(run_id, attempt_id, submission_id),
        FOREIGN KEY(run_id, submission_id, attempt_id)
          REFERENCES submissions(run_id, submission_id, attempt_id),
        FOREIGN KEY(run_id, event_seq) REFERENCES events(run_id, event_seq)
    )""",
    f"""CREATE TABLE results (
        result_seq INTEGER PRIMARY KEY CHECK(result_seq >= 1), run_id TEXT NOT NULL, task_id TEXT NOT NULL,
        request_uid TEXT NOT NULL, attempt_id TEXT NOT NULL, observation_id TEXT,
        failure_submission_id TEXT,
        status TEXT NOT NULL CHECK(status IN ('complete','invalid','refused','errored','cancelled')),
        record_json TEXT NOT NULL CHECK(json_valid(record_json) AND json_type(record_json)='object'),
        record_sha256 TEXT NOT NULL CHECK({_hash("record_sha256")}),
        UNIQUE(run_id, task_id, attempt_id),
        CHECK((observation_id IS NULL) != (failure_submission_id IS NULL)),
        FOREIGN KEY(run_id, attempt_id, task_id) REFERENCES attempt_tasks(run_id, attempt_id, task_id),
        FOREIGN KEY(run_id, observation_id, attempt_id)
          REFERENCES observations(run_id, observation_id, attempt_id),
        FOREIGN KEY(run_id, attempt_id, failure_submission_id)
          REFERENCES submission_failures(run_id, attempt_id, submission_id)
    )""",
    """CREATE TABLE exports (
        run_id TEXT NOT NULL, export_id TEXT NOT NULL,
        format TEXT NOT NULL CHECK(format IN ('jsonl','csv')),
        view TEXT NOT NULL CHECK(view IN ('canonical','r')),
        result_high_water_mark INTEGER NOT NULL CHECK(result_high_water_mark >= 0),
        row_count INTEGER NOT NULL CHECK(row_count >= 0), artifact_sha256 TEXT,
        state TEXT NOT NULL CHECK(state IN ('preparing','published')),
        created_at TEXT NOT NULL,
        PRIMARY KEY(run_id, export_id), FOREIGN KEY(run_id) REFERENCES runs(run_id),
        FOREIGN KEY(run_id, artifact_sha256) REFERENCES artifacts(run_id, sha256),
        CHECK(state!='published' OR artifact_sha256 IS NOT NULL)
    )""",
)


def _index(table: str, suffix: str, columns: str) -> str:
    return f"CREATE INDEX idx_{table}_{suffix} ON {table} ({columns})"


_INDEXES = (
    _index("runs", "state_time", "state, created_at"),
    _index("runs", "pagination", "created_at, run_id"),
    _index("artifacts", "retention", "run_id, relative_path"),
    _index("input_records", "ordered", "run_id, source_id, ordinal"),
    _index("tasks", "state", "run_id, state, scheduling_ordinal"),
    _index("tasks", "item", "run_id, item_id"),
    _index("tasks", "pair", "run_id, pair_uid"),
    _index("requests", "state", "run_id, state, scheduling_ordinal"),
    _index("request_tasks", "task", "run_id, task_id"),
    _index("attempts", "eligible", "run_id, state, next_eligible_at"),
    _index("attempt_tasks", "task", "run_id, task_id"),
    _index("batches", "state", "run_id, state"),
    _index("submissions", "state", "run_id, state"),
    _index("submissions", "remote", "run_id, remote_id"),
    _index("observations", "attempt", "run_id, attempt_id"),
    _index("results", "run_order", "run_id, result_seq"),
    _index("results", "task_history", "run_id, task_id, result_seq"),
    _index("exports", "time", "run_id, created_at"),
    _index("events", "run_order", "run_id, event_seq"),
    _index("events", "entity", "run_id, entity_kind, entity_id, event_seq"),
)


def _guard(name: str, operation: str, table: str, condition: str = "1") -> str:
    return f"""CREATE TRIGGER {name} BEFORE {operation} ON {table}
        WHEN {condition} BEGIN SELECT RAISE(ABORT, 'immutable persistence record'); END"""


def _immutable_columns(table: str, columns: tuple[str, ...]) -> str:
    condition = " OR ".join(f"OLD.{column} IS NOT NEW.{column}" for column in columns)
    return _guard(f"{table}_identity", "UPDATE", table, condition)


def _transition_trigger(entity: str, table: str) -> str:
    allowed = " OR ".join(
        f"(OLD.state='{previous}' AND NEW.state IN ({_choices(list(targets))}))"
        for previous, targets in TRANSITIONS[entity].items()
        if targets
    )
    return f"""CREATE TRIGGER {table}_state BEFORE UPDATE OF state ON {table}
        WHEN OLD.state IS NOT NEW.state AND NOT ({allowed})
        BEGIN SELECT RAISE(ABORT, 'invalid state transition'); END"""


def _no_replace(table: str, keys: tuple[tuple[str, ...], ...]) -> str:
    conditions = " OR ".join(
        "(" + " AND ".join(f"{column}=NEW.{column}" for column in key) + ")"
        for key in keys
    )
    return _guard(
        f"{table}_no_replace",
        "INSERT",
        table,
        f"EXISTS(SELECT 1 FROM {table} WHERE {conditions})",
    )


_UNIQUE_IDENTITIES = {
    "schema_migrations": (("version",),),
    "runs": (("run_id",),),
    "artifacts": (("run_id", "sha256"),),
    "sources": (("run_id", "source_id"), ("run_id", "role", "ordinal")),
    "input_records": (
        ("run_id", "record_kind", "record_id"),
        ("run_id", "source_id", "ordinal"),
    ),
    "tasks": (("run_id", "task_id"), ("run_id", "scheduling_ordinal")),
    "requests": (("run_id", "request_uid"), ("run_id", "scheduling_ordinal")),
    "request_tasks": (
        ("run_id", "request_uid", "task_id"),
        ("run_id", "request_uid", "position"),
    ),
    "attempts": (("run_id", "attempt_id"), ("run_id", "request_uid", "attempt_number")),
    "attempt_tasks": (
        ("run_id", "attempt_id", "task_id"),
        ("run_id", "attempt_id", "position"),
    ),
    "retry_operations": (("run_id", "operation_id"),),
    "batches": (("run_id", "batch_uid"),),
    "batch_members": (
        ("run_id", "batch_uid", "attempt_id"),
        ("run_id", "attempt_id"),
        ("run_id", "batch_uid", "position"),
    ),
    "submissions": (
        ("run_id", "submission_id"),
        ("correlation_id",),
        ("run_id", "attempt_id"),
        ("run_id", "batch_uid"),
    ),
    "observations": (
        ("run_id", "observation_id"),
        ("run_id", "submission_id", "observation_key"),
    ),
    "submission_failures": (("run_id", "attempt_id"),),
    "results": (("result_seq",), ("run_id", "task_id", "attempt_id")),
    "events": (("event_seq",),),
    "exports": (("run_id", "export_id"),),
}


_APPEND_ONLY = (
    "schema_migrations",
    "artifacts",
    "input_records",
    "request_tasks",
    "attempt_tasks",
    "observations",
    "submission_failures",
    "results",
    "retry_operations",
    "events",
    "batch_members",
)
_TRIGGERS: tuple[str, ...] = (
    *(_no_replace(table, keys) for table, keys in _UNIQUE_IDENTITIES.items()),
    *(_guard(f"{table}_no_update", "UPDATE", table) for table in _APPEND_ONLY),
    *(_guard(f"{table}_no_delete", "DELETE", table) for table in _APPEND_ONLY),
    *(
        _guard(f"{table}_no_delete", "DELETE", table)
        for table in ("runs", "tasks", "requests", "attempts", "submissions", "batches")
    ),
    *(
        _transition_trigger(entity, table)
        for entity, table in (
            ("run", "runs"),
            ("task", "tasks"),
            ("request", "requests"),
            ("attempt", "attempts"),
            ("submission", "submissions"),
        )
    ),
    _immutable_columns(
        "runs",
        (
            "run_id",
            "task_kind",
            "provider",
            "endpoint",
            "model",
            "capture",
            "created_at",
        ),
    ),
    _immutable_columns(
        "tasks",
        (
            "run_id",
            "task_id",
            "trait_id",
            "item_id",
            "pair_uid",
            "pair_id",
            "scheduling_ordinal",
        ),
    ),
    _immutable_columns(
        "requests", ("run_id", "request_uid", "scheduling_ordinal", "dependency_sha256")
    ),
    _immutable_columns(
        "attempts",
        (
            "run_id",
            "attempt_id",
            "request_uid",
            "attempt_number",
            "prompt_sha256",
            "payload_sha256",
            "membership_sha256",
            "authorization_id",
        ),
    ),
    _immutable_columns(
        "submissions",
        (
            "run_id",
            "submission_id",
            "correlation_id",
            "attempt_id",
            "batch_uid",
            "payload_sha256",
        ),
    ),
    _immutable_columns(
        "batches",
        ("run_id", "batch_uid", "provider", "mode", "membership_sha256", "created_at"),
    ),
    _guard(
        "runs_manifest_immutable",
        "UPDATE",
        "runs",
        "OLD.manifest_sha256 IS NOT NULL AND OLD.manifest_sha256 IS NOT NEW.manifest_sha256",
    ),
    _guard(
        "submissions_remote_immutable",
        "UPDATE",
        "submissions",
        "OLD.remote_id IS NOT NULL AND OLD.remote_id IS NOT NEW.remote_id",
    ),
    _guard(
        "submissions_acceptance_immutable",
        "UPDATE",
        "submissions",
        "OLD.certainty='accepted' AND NEW.certainty!='accepted'",
    ),
    """CREATE TRIGGER runs_manifest_seal BEFORE UPDATE OF manifest_sha256 ON runs
        WHEN OLD.manifest_sha256 IS NULL AND NEW.manifest_sha256 IS NOT NULL
          AND (NEW.state!='submitted' OR NOT EXISTS(SELECT 1 FROM artifacts a
            WHERE a.run_id=NEW.run_id AND a.sha256=NEW.manifest_sha256 AND a.relative_path IS NOT NULL))
        BEGIN SELECT RAISE(ABORT, 'manifest requires retained artifact and submitted state'); END""",
    """CREATE TRIGGER runs_insert_created BEFORE INSERT ON runs
        WHEN NEW.state!='created' OR NEW.manifest_sha256 IS NOT NULL
        BEGIN SELECT RAISE(ABORT, 'run must begin created without a manifest'); END""",
    _guard("tasks_insert_pending", "INSERT", "tasks", "NEW.state!='pending'"),
    """CREATE TRIGGER tasks_match_run BEFORE INSERT ON tasks
        WHEN EXISTS(SELECT 1 FROM runs WHERE run_id=NEW.run_id AND
          ((task_kind='rubric' AND NEW.item_id IS NULL) OR (task_kind='pairwise' AND NEW.pair_uid IS NULL)))
        BEGIN SELECT RAISE(ABORT, 'task target differs from run kind'); END""",
    _guard(
        "requests_insert_planned",
        "INSERT",
        "requests",
        "NEW.state!='planned' OR NEW.membership_sealed!=0",
    ),
    _guard(
        "attempts_insert_prepared",
        "INSERT",
        "attempts",
        "NEW.state!='prepared' OR NEW.membership_sealed!=0",
    ),
    """CREATE TRIGGER attempts_cancellation_evidence BEFORE UPDATE OF state ON attempts
        WHEN NEW.state='cancelled' AND OLD.state IN ('submitting','acknowledged','uncertain')
          AND NOT EXISTS(SELECT 1 FROM submissions s WHERE s.run_id=NEW.run_id
            AND s.attempt_id=NEW.attempt_id AND s.state='resolved'
            AND s.certainty IN ('accepted','not_submitted'))
        BEGIN SELECT RAISE(ABORT, 'attempt cancellation needs conclusive evidence'); END""",
    """CREATE TRIGGER attempts_error_evidence BEFORE UPDATE OF state ON attempts
        WHEN NEW.state='errored' AND NOT EXISTS(SELECT 1 FROM observations o
          WHERE o.run_id=NEW.run_id AND o.attempt_id=NEW.attempt_id AND o.kind='provider_error')
        BEGIN SELECT RAISE(ABORT, 'attempt error needs provider observation'); END""",
    *(
        _guard(
            f"{table}_cannot_unseal",
            "UPDATE",
            table,
            "OLD.membership_sealed=1 AND NEW.membership_sealed!=1",
        )
        for table in ("requests", "attempts")
    ),
    """CREATE TRIGGER request_tasks_sealed BEFORE INSERT ON request_tasks
        WHEN (SELECT membership_sealed FROM requests
          WHERE run_id=NEW.run_id AND request_uid=NEW.request_uid)=1
        BEGIN SELECT RAISE(ABORT, 'request membership is sealed'); END""",
    """CREATE TRIGGER attempt_tasks_sealed BEFORE INSERT ON attempt_tasks
        WHEN (SELECT membership_sealed FROM attempts
          WHERE run_id=NEW.run_id AND attempt_id=NEW.attempt_id)=1
        BEGIN SELECT RAISE(ABORT, 'attempt membership is sealed'); END""",
    """CREATE TRIGGER requests_seal_nonempty BEFORE UPDATE OF membership_sealed ON requests
        WHEN OLD.membership_sealed=0 AND NEW.membership_sealed=1
          AND NOT EXISTS(SELECT 1 FROM request_tasks WHERE run_id=NEW.run_id AND request_uid=NEW.request_uid)
        BEGIN SELECT RAISE(ABORT, 'request membership is empty'); END""",
    """CREATE TRIGGER attempts_seal_nonempty BEFORE UPDATE OF membership_sealed ON attempts
        WHEN OLD.membership_sealed=0 AND NEW.membership_sealed=1
          AND NOT EXISTS(SELECT 1 FROM attempt_tasks WHERE run_id=NEW.run_id AND attempt_id=NEW.attempt_id)
        BEGIN SELECT RAISE(ABORT, 'attempt membership is empty'); END""",
    """CREATE TRIGGER attempts_require_request BEFORE INSERT ON attempts
        WHEN NOT EXISTS(SELECT 1 FROM requests WHERE run_id=NEW.run_id
          AND request_uid=NEW.request_uid AND membership_sealed=1)
        BEGIN SELECT RAISE(ABORT, 'attempt requires sealed request membership'); END""",
    """CREATE TRIGGER submissions_require_preparation BEFORE INSERT ON submissions
        WHEN NEW.state!='prepared' OR NEW.certainty!='not_started' OR
          (NEW.attempt_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM attempts a
            JOIN runs r ON r.run_id=a.run_id WHERE a.run_id=NEW.run_id
              AND a.attempt_id=NEW.attempt_id AND a.membership_sealed=1
              AND a.payload_sha256=NEW.payload_sha256 AND r.manifest_sha256 IS NOT NULL))
        BEGIN SELECT RAISE(ABORT, 'submission requires immutable prepared work'); END""",
    """CREATE TRIGGER observations_require_acceptance BEFORE INSERT ON observations
        WHEN NOT EXISTS(SELECT 1 FROM submissions s JOIN attempts a
          ON a.run_id=s.run_id AND a.attempt_id=s.attempt_id
          WHERE s.run_id=NEW.run_id AND s.submission_id=NEW.submission_id
            AND s.attempt_id=NEW.attempt_id AND s.state IN ('acknowledged','resolved')
            AND s.certainty='accepted' AND a.state IN ('submitting','acknowledged','response_saved','normalized'))
        BEGIN SELECT RAISE(ABORT, 'observation requires accepted submission'); END""",
    """CREATE TRIGGER failures_require_evidence BEFORE INSERT ON submission_failures
        WHEN NOT EXISTS(SELECT 1 FROM events e JOIN submissions s
          ON s.run_id=NEW.run_id AND s.submission_id=NEW.submission_id
          JOIN attempts a ON a.run_id=s.run_id AND a.attempt_id=s.attempt_id
          WHERE e.run_id=NEW.run_id AND e.event_seq=NEW.event_seq
            AND e.kind='failure_recorded' AND e.entity_kind='attempt' AND e.entity_id=NEW.attempt_id
            AND a.attempt_id=NEW.attempt_id AND (
              (NEW.kind='transport_not_submitted' AND s.state='confirmed_not_submitted'
                AND s.certainty='not_submitted' AND a.state='not_submitted')
              OR (NEW.kind='confirmed_cancellation' AND s.state='resolved' AND a.state='cancelled')))
        BEGIN SELECT RAISE(ABORT, 'submission failure requires conclusive evidence'); END""",
    """CREATE TRIGGER results_identity BEFORE INSERT ON results
        WHEN json_type(NEW.record_json,'$.schema_version') IS NOT 'integer'
          OR json_extract(NEW.record_json,'$.schema_version') IS NOT 1
          OR json_extract(NEW.record_json,'$.run_id') IS NOT NEW.run_id
          OR json_extract(NEW.record_json,'$.task_id') IS NOT NEW.task_id
          OR json_extract(NEW.record_json,'$.request_uid') IS NOT NEW.request_uid
          OR json_extract(NEW.record_json,'$.attempt_id') IS NOT NEW.attempt_id
          OR json_extract(NEW.record_json,'$.status') IS NOT NEW.status
          OR EXISTS(SELECT 1 FROM runs r WHERE r.run_id=NEW.run_id AND (
            (json_extract(NEW.record_json,'$.manifest_sha256') IS NOT NULL AND json_extract(NEW.record_json,'$.manifest_sha256') IS NOT r.manifest_sha256)
            OR (json_extract(NEW.record_json,'$.provider') IS NOT NULL AND json_extract(NEW.record_json,'$.provider') IS NOT r.provider)
            OR (json_extract(NEW.record_json,'$.endpoint') IS NOT NULL AND json_extract(NEW.record_json,'$.endpoint') IS NOT r.endpoint)
            OR (json_extract(NEW.record_json,'$.model') IS NOT NULL AND json_extract(NEW.record_json,'$.model') IS NOT r.model)))
          OR NOT EXISTS(SELECT 1 FROM tasks t JOIN attempt_tasks m
            ON m.run_id=t.run_id AND m.task_id=t.task_id
            WHERE t.run_id=NEW.run_id AND t.task_id=NEW.task_id
              AND m.attempt_id=NEW.attempt_id AND m.request_uid=NEW.request_uid
              AND json_extract(NEW.record_json,'$.trait_id') IS t.trait_id
              AND ((t.item_id IS NOT NULL AND json_extract(NEW.record_json,'$.task_kind')='rubric'
                AND json_extract(NEW.record_json,'$.item_id') IS t.item_id)
              OR (t.pair_uid IS NOT NULL AND json_extract(NEW.record_json,'$.task_kind')='pairwise'
                AND json_extract(NEW.record_json,'$.pair_uid') IS t.pair_uid
                AND json_extract(NEW.record_json,'$.pair_id') IS t.pair_id)))
          OR (NEW.status!='complete' AND (json_extract(NEW.record_json,'$.primary_score') IS NOT NULL
            OR json_extract(NEW.record_json,'$.primary_score_kind') IS NOT NULL))
        BEGIN SELECT RAISE(ABORT, 'normalized result identity is inconsistent'); END""",
    """CREATE TRIGGER results_evidence BEFORE INSERT ON results
        WHEN (NEW.observation_id IS NOT NULL AND NOT EXISTS(
          SELECT 1 FROM observations o JOIN submissions s
            ON s.run_id=o.run_id AND s.submission_id=o.submission_id
          WHERE o.run_id=NEW.run_id AND o.observation_id=NEW.observation_id
            AND o.attempt_id=NEW.attempt_id AND s.state IN ('acknowledged','resolved')
            AND s.certainty='accepted' AND (
              (o.kind='response' AND NEW.status IN ('complete','invalid'))
              OR (o.kind='refusal' AND NEW.status='refused')
              OR (o.kind='provider_error' AND NEW.status='errored')
              OR (o.kind='confirmed_cancellation' AND NEW.status='cancelled'))))
          OR (NEW.failure_submission_id IS NOT NULL AND NOT EXISTS(
            SELECT 1 FROM submission_failures f WHERE f.run_id=NEW.run_id
              AND f.attempt_id=NEW.attempt_id AND f.submission_id=NEW.failure_submission_id
              AND json_extract(NEW.record_json,'$.error_kind') IS f.kind
              AND ((f.kind='transport_not_submitted' AND NEW.status='errored')
                OR (f.kind='confirmed_cancellation' AND NEW.status='cancelled'))))
        BEGIN SELECT RAISE(ABORT, 'normalized result needs established outcome evidence'); END""",
    *(
        _guard(
            f"sources_frozen_{operation.lower()}",
            operation,
            "sources",
            "(SELECT state FROM runs WHERE run_id="
            + ("NEW" if operation == "INSERT" else "OLD")
            + ".run_id)!='created'",
        )
        for operation in ("INSERT", "UPDATE", "DELETE")
    ),
    _guard(
        "input_records_frozen",
        "INSERT",
        "input_records",
        "(SELECT state FROM runs WHERE run_id=NEW.run_id)!='created'",
    ),
    _guard("exports_published_update", "UPDATE", "exports", "OLD.state='published'"),
    _guard("exports_published_delete", "DELETE", "exports", "OLD.state='published'"),
)

_STATEMENTS = (*_TABLES, *_INDEXES, *_TRIGGERS)
SCHEMA_CHECKSUM = hashlib.sha256("\n".join(_STATEMENTS).encode("utf-8")).hexdigest()


def _schema_digest(connection: sqlite3.Connection) -> str:
    objects = connection.execute(
        "SELECT type,name,sql FROM sqlite_master "
        "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY type,name"
    ).fetchall()
    payload = json.dumps([list(row) for row in objects], separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def verify(connection: sqlite3.Connection) -> None:
    """Verify an existing schema without initializing, migrating or writing.

    The caller supplies its read transaction, so version, migration evidence and
    actual structure are checked against the same committed database snapshot.
    """
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version != SCHEMA_VERSION:
        raise SchemaVersionError("unsupported database schema version")
    migrations = connection.execute(
        "SELECT version,checksum,schema_sha256 FROM schema_migrations ORDER BY version"
    ).fetchall()
    if len(migrations) != 1 or tuple(migrations[0][:2]) != (
        SCHEMA_VERSION,
        SCHEMA_CHECKSUM,
    ):
        raise SchemaVersionError("database migration checksum is inconsistent")
    if migrations[0][2] != _schema_digest(connection):
        raise SchemaVersionError("database schema integrity check failed")


def initialize(connection: sqlite3.Connection) -> None:
    """Create or verify schema in one transaction; never silently migrate.

    Call with autocommit enabled and no open transaction. The repository owns
    connection WAL/synchronous/busy-timeout configuration. Foreign keys are
    enabled here as well so direct schema users cannot omit them accidentally.
    """
    if connection.in_transaction:
        raise SchemaVersionError("schema initialization needs an idle connection")
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        initial_version = connection.execute("PRAGMA user_version").fetchone()[0]
        connection.execute("BEGIN IMMEDIATE" if initial_version == 0 else "BEGIN")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, SCHEMA_VERSION):
            raise SchemaVersionError("unsupported database schema version")
        if version == 0:
            if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
            ).fetchone():
                raise SchemaVersionError(
                    "unversioned database already contains a schema"
                )
            for statement in _STATEMENTS:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations(version,name,checksum,schema_sha256,applied_at) VALUES (?,?,?,?,?)",
                (
                    SCHEMA_VERSION,
                    "durable_foundation",
                    SCHEMA_CHECKSUM,
                    _schema_digest(connection),
                    utc_now(),
                ),
            )
            connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        else:
            verify(connection)
        connection.commit()
    except sqlite3.DatabaseError as exc:
        connection.rollback()
        raise SchemaVersionError("database schema initialization failed") from exc
    except BaseException:
        connection.rollback()
        raise
