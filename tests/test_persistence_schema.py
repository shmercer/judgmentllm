"""Direct SQLite guards complement typed repository boundary checks."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest

from judgmentllm.models import Level, Trait
from judgmentllm.persistence.models import SchemaVersionError
from judgmentllm.persistence.schema import SCHEMA_CHECKSUM, SCHEMA_VERSION, initialize
from judgmentllm.results import RubricResult, normalize_rubric_result

DIGEST = "a" * 64
NOW = "2026-11-02T00:00:00+00:00"


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    with closing(
        sqlite3.connect(tmp_path / "registry.sqlite3", isolation_level=None)
    ) as conn:
        initialize(conn)
        yield conn


def create_run(conn: sqlite3.Connection, run_id: str = "run") -> None:
    conn.execute(
        "INSERT INTO runs(run_id,task_kind,provider,endpoint,model,capture,created_at) "
        "VALUES (?,'rubric','synthetic','test','test','full',?)",
        (run_id, NOW),
    )


def prepare(conn: sqlite3.Connection) -> None:
    create_run(conn)
    conn.execute(
        "INSERT INTO artifacts VALUES (?,?,?,?,?)",
        ("run", DIGEST, 1, "application/json", f"objects/aa/{DIGEST}"),
    )
    conn.execute(
        "UPDATE runs SET state='submitted',manifest_sha256=? WHERE run_id='run'",
        (DIGEST,),
    )
    conn.execute(
        "INSERT INTO tasks(run_id,task_id,trait_id,item_id,scheduling_ordinal) "
        "VALUES ('run','task','quality','001',0)"
    )
    conn.execute(
        "INSERT INTO requests(run_id,request_uid,scheduling_ordinal,dependency_sha256) "
        "VALUES ('run','request',0,?)",
        (DIGEST,),
    )
    conn.execute("INSERT INTO request_tasks VALUES ('run','request','task',0)")
    conn.execute("UPDATE requests SET membership_sealed=1 WHERE run_id='run'")
    conn.execute(
        "INSERT INTO attempts(run_id,attempt_id,request_uid,attempt_number,"
        "prompt_sha256,payload_sha256,membership_sha256) VALUES ('run','attempt','request',1,?,?,?)",
        (DIGEST, DIGEST, DIGEST),
    )
    conn.execute(
        "INSERT INTO attempt_tasks VALUES ('run','attempt','request','task',0)"
    )
    conn.execute("UPDATE attempts SET membership_sealed=1 WHERE run_id='run'")
    conn.execute(
        "INSERT INTO submissions(run_id,submission_id,correlation_id,attempt_id,payload_sha256) "
        "VALUES ('run','submission','correlation','attempt',?)",
        (DIGEST,),
    )


def start_submission(conn: sqlite3.Connection) -> None:
    conn.execute("UPDATE attempts SET state='claimed' WHERE run_id='run'")
    conn.execute("UPDATE attempts SET state='submitting' WHERE run_id='run'")
    conn.execute(
        "UPDATE submissions SET state='submitting',certainty='unknown' WHERE run_id='run'"
    )


def observe(conn: sqlite3.Connection, kind: str = "response") -> None:
    start_submission(conn)
    conn.execute(
        "UPDATE submissions SET state='acknowledged',certainty='accepted',remote_id='remote' WHERE run_id='run'"
    )
    conn.execute("UPDATE attempts SET state='acknowledged' WHERE run_id='run'")
    conn.execute(
        "INSERT INTO observations VALUES ('run','observation','submission','attempt','one',?,NULL,?)",
        (kind, NOW),
    )
    conn.execute("UPDATE attempts SET state='response_saved' WHERE run_id='run'")


def failure(conn: sqlite3.Connection) -> None:
    start_submission(conn)
    conn.execute(
        "UPDATE submissions SET state='confirmed_not_submitted',certainty='not_submitted' WHERE run_id='run'"
    )
    conn.execute("UPDATE attempts SET state='not_submitted' WHERE run_id='run'")
    conn.execute(
        "INSERT INTO events(run_id,kind,entity_kind,entity_id,occurred_at) "
        "VALUES ('run','failure_recorded','attempt','attempt',?)",
        (NOW,),
    )
    conn.execute(
        "INSERT INTO submission_failures VALUES ('run','attempt','submission','transport_not_submitted',?)",
        (conn.execute("SELECT max(event_seq) FROM events").fetchone()[0],),
    )


def result_record(
    status: str = "complete", error_kind: str | None = None
) -> RubricResult:
    trait = Trait(
        trait_id="quality",
        name="Quality",
        description="Synthetic",
        levels=[Level(label="low"), Level(label="high")],
    )
    if status == "complete":
        return normalize_rubric_result(
            trait,
            run_id="run",
            task_id="task",
            request_uid="request",
            attempt_id="attempt",
            item_id="001",
            category_label="high",
        )
    return RubricResult.model_validate(
        {
            "run_id": "run",
            "task_id": "task",
            "request_uid": "request",
            "attempt_id": "attempt",
            "item_id": "001",
            "trait_id": "quality",
            "resolved_levels": [level.model_dump() for level in trait.resolved_levels],
            "status": status,
            "error_kind": error_kind,
        }
    )


def insert_result(
    conn: sqlite3.Connection,
    record: RubricResult,
    observation_id: str | None = "observation",
    failure_submission_id: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO results(run_id,task_id,request_uid,attempt_id,observation_id,"
        "failure_submission_id,status,record_json,record_sha256) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            record.run_id,
            record.task_id,
            record.request_uid,
            record.attempt_id,
            observation_id,
            failure_submission_id,
            record.status,
            record.model_dump_json(),
            DIGEST,
        ),
    )


def test_schema_initialization_and_reopen(connection: sqlite3.Connection) -> None:
    assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    row = connection.execute(
        "SELECT version,checksum FROM schema_migrations"
    ).fetchone()
    assert row == (SCHEMA_VERSION, SCHEMA_CHECKSUM)
    initialize(connection)
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert {
        "sources",
        "input_records",
        "batches",
        "batch_members",
        "exports",
        "submission_failures",
    } <= tables


def test_schema_rejects_active_transaction(connection: sqlite3.Connection) -> None:
    connection.execute("BEGIN")
    with pytest.raises(SchemaVersionError, match="idle"):
        initialize(connection)
    connection.rollback()


def test_schema_verification_does_not_need_a_writer_lock(tmp_path: Path) -> None:
    path = tmp_path / "concurrent.sqlite3"
    with closing(sqlite3.connect(path, isolation_level=None)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        initialize(writer)
        create_run(writer)
        writer.execute("BEGIN IMMEDIATE")
        create_run(writer, "uncommitted")
        with closing(
            sqlite3.connect(path, timeout=0.01, isolation_level=None)
        ) as reader:
            initialize(reader)
            assert reader.execute("SELECT count(*) FROM runs").fetchone()[0] == 1
        writer.rollback()


@pytest.mark.parametrize("version", [-1, 2, 99])
def test_schema_rejects_unknown_versions(
    connection: sqlite3.Connection, version: int
) -> None:
    connection.execute(f"PRAGMA user_version={version}")
    with pytest.raises(SchemaVersionError, match="unsupported"):
        initialize(connection)


def test_schema_rejects_unversioned_objects(tmp_path: Path) -> None:
    with closing(
        sqlite3.connect(tmp_path / "unknown.sqlite3", isolation_level=None)
    ) as conn:
        conn.execute("CREATE TABLE existing(value TEXT)")
        with pytest.raises(SchemaVersionError, match="unversioned"):
            initialize(conn)


def test_schema_detects_dropped_guard(connection: sqlite3.Connection) -> None:
    connection.execute("DROP TRIGGER results_no_delete")
    with pytest.raises(SchemaVersionError, match="integrity"):
        initialize(connection)


def test_schema_detects_modified_migration_checksum(
    connection: sqlite3.Connection,
) -> None:
    connection.execute("DROP TRIGGER schema_migrations_no_update")
    connection.execute("UPDATE schema_migrations SET checksum='changed'")
    with pytest.raises(SchemaVersionError, match="checksum"):
        initialize(connection)


def test_schema_detects_missing_migration_table(connection: sqlite3.Connection) -> None:
    connection.execute("DROP TABLE schema_migrations")
    with pytest.raises(SchemaVersionError, match="initialization failed"):
        initialize(connection)


def test_schema_initialization_rolls_back_all_ddl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def interrupted(_connection: sqlite3.Connection) -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr("judgmentllm.persistence.schema._schema_digest", interrupted)
    with closing(
        sqlite3.connect(tmp_path / "interrupted.sqlite3", isolation_level=None)
    ) as conn:
        with pytest.raises(KeyboardInterrupt):
            initialize(conn)
        assert not conn.in_transaction
        assert conn.execute("SELECT name FROM sqlite_master").fetchall() == []
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 0


def test_sqlite_constraints_and_cross_run_identity(
    connection: sqlite3.Connection,
) -> None:
    prepare(connection)
    create_run(connection, "other")
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO request_tasks VALUES ('other','request','task',0)"
        )
    with pytest.raises(sqlite3.IntegrityError):
        create_run(connection)
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO artifacts VALUES ('run','not-a-hash',0,'application/json',NULL)"
        )
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize(
    "table,state",
    [
        ("runs", "completed"),
        ("tasks", "complete"),
        ("requests", "completed"),
        ("attempts", "normalized"),
        ("submissions", "resolved"),
    ],
)
def test_transition_guards(
    connection: sqlite3.Connection, table: str, state: str
) -> None:
    prepare(connection)
    with pytest.raises(sqlite3.IntegrityError, match="state transition"):
        connection.execute(f"UPDATE {table} SET state=? WHERE run_id='run'", (state,))


@pytest.mark.parametrize("table", ["requests", "attempts"])
def test_membership_cannot_be_unsealed(
    connection: sqlite3.Connection, table: str
) -> None:
    prepare(connection)
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(f"UPDATE {table} SET membership_sealed=0 WHERE run_id='run'")


def test_membership_cannot_grow_after_sealing(connection: sqlite3.Connection) -> None:
    prepare(connection)
    connection.execute(
        "INSERT INTO tasks(run_id,task_id,trait_id,item_id,scheduling_ordinal) VALUES ('run','second','other','001',1)"
    )
    with pytest.raises(sqlite3.IntegrityError, match="sealed"):
        connection.execute(
            "INSERT INTO request_tasks VALUES ('run','request','second',1)"
        )
    with pytest.raises(sqlite3.IntegrityError, match="sealed"):
        connection.execute(
            "INSERT INTO attempt_tasks VALUES ('run','attempt','request','second',1)"
        )


@pytest.mark.parametrize(
    "table,column,value",
    [
        ("runs", "model", "changed"),
        ("requests", "dependency_sha256", "b" * 64),
        ("tasks", "item_id", "002"),
        ("attempts", "payload_sha256", "b" * 64),
        ("submissions", "correlation_id", "changed"),
    ],
)
def test_identity_columns_immutable(
    connection: sqlite3.Connection, table: str, column: str, value: str
) -> None:
    prepare(connection)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        connection.execute(
            f"UPDATE {table} SET {column}=? WHERE run_id='run'", (value,)
        )


def test_manifest_reference_cannot_be_replaced(connection: sqlite3.Connection) -> None:
    prepare(connection)
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("UPDATE runs SET manifest_sha256=NULL WHERE run_id='run'")
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("DELETE FROM runs WHERE run_id='run'")


def test_hash_only_manifest_cannot_be_sealed(connection: sqlite3.Connection) -> None:
    create_run(connection)
    connection.execute(
        "INSERT INTO artifacts VALUES ('run',?,1,'application/json',NULL)", (DIGEST,)
    )
    with pytest.raises(sqlite3.IntegrityError, match="retained"):
        connection.execute(
            "UPDATE runs SET state='submitted',manifest_sha256=?", (DIGEST,)
        )


def test_observations_reject_prepared_and_uncertain_work(
    connection: sqlite3.Connection,
) -> None:
    prepare(connection)
    for _ in range(2):
        with pytest.raises(sqlite3.IntegrityError, match="accepted"):
            connection.execute(
                "INSERT INTO observations VALUES ('run','observation','submission','attempt','one','response',NULL,?)",
                (NOW,),
            )
        if (
            connection.execute("SELECT state FROM submissions").fetchone()[0]
            == "prepared"
        ):
            start_submission(connection)
            connection.execute(
                "UPDATE submissions SET state='uncertain' WHERE run_id='run'"
            )
    assert connection.execute("SELECT count(*) FROM observations").fetchone()[0] == 0


def test_acknowledged_certainty_and_remote_id_not_lost(
    connection: sqlite3.Connection,
) -> None:
    prepare(connection)
    observe(connection)
    for statement in (
        "UPDATE submissions SET state='uncertain',certainty='unknown'",
        "UPDATE submissions SET remote_id=NULL",
        "UPDATE submissions SET remote_id='different'",
    ):
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(statement)
    connection.execute("UPDATE submissions SET state='uncertain'")
    assert connection.execute(
        "SELECT certainty,remote_id FROM submissions"
    ).fetchone() == ("accepted", "remote")


@pytest.mark.parametrize(
    "kind,status,error_kind",
    [
        ("response", "complete", None),
        ("response", "invalid", "missing_output"),
        ("refusal", "refused", "provider_refusal"),
        ("provider_error", "errored", "provider_error"),
    ],
)
def test_success_and_unsuccessful_results_persist(
    connection: sqlite3.Connection, kind: str, status: str, error_kind: str | None
) -> None:
    prepare(connection)
    observe(connection, kind)
    record = result_record(status, error_kind)
    insert_result(connection, record)
    stored = connection.execute("SELECT status,record_json FROM results").fetchone()
    assert stored[0] == status
    assert RubricResult.model_validate_json(stored[1]) == record
    if status != "complete":
        assert json.loads(stored[1])["primary_score"] is None


def test_transport_failure_does_not_invent_provider_observation(
    connection: sqlite3.Connection,
) -> None:
    prepare(connection)
    failure(connection)
    insert_result(
        connection,
        result_record("errored", "transport_not_submitted"),
        None,
        "submission",
    )
    assert connection.execute("SELECT count(*) FROM observations").fetchone()[0] == 0
    assert connection.execute(
        "SELECT observation_id,failure_submission_id FROM results"
    ).fetchone() == (None, "submission")


def test_uncertain_work_has_no_normalized_outcome(
    connection: sqlite3.Connection,
) -> None:
    prepare(connection)
    start_submission(connection)
    connection.execute("UPDATE submissions SET state='uncertain'")
    with pytest.raises(sqlite3.IntegrityError):
        insert_result(
            connection,
            result_record("errored", "transport_not_submitted"),
            None,
            "submission",
        )
    assert connection.execute("SELECT count(*) FROM results").fetchone()[0] == 0


def test_result_identity_matches_its_actual_task(
    connection: sqlite3.Connection,
) -> None:
    prepare(connection)
    observe(connection)
    record = result_record().model_copy(update={"item_id": "002"})
    with pytest.raises(sqlite3.IntegrityError, match="identity"):
        insert_result(connection, record)


def test_result_status_matches_observation_kind(connection: sqlite3.Connection) -> None:
    prepare(connection)
    observe(connection, "refusal")
    with pytest.raises(sqlite3.IntegrityError, match="outcome evidence"):
        insert_result(connection, result_record())


@pytest.mark.parametrize(
    "table",
    [
        "schema_migrations",
        "artifacts",
        "request_tasks",
        "attempt_tasks",
        "observations",
        "results",
        "events",
    ],
)
@pytest.mark.parametrize("operation", ["UPDATE", "DELETE"])
def test_append_only_guards(
    connection: sqlite3.Connection, table: str, operation: str
) -> None:
    prepare(connection)
    observe(connection)
    insert_result(connection, result_record())
    connection.execute(
        "INSERT INTO events(run_id,kind,entity_kind,entity_id,occurred_at) VALUES ('run','result_recorded','result','task',?)",
        (NOW,),
    )
    column = "version" if table == "schema_migrations" else "run_id"
    statement = (
        f"UPDATE {table} SET {column}={column}"
        if operation == "UPDATE"
        else f"DELETE FROM {table}"
    )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        connection.execute(statement)


def test_result_uniqueness_prevents_replayed_completion(
    connection: sqlite3.Connection,
) -> None:
    prepare(connection)
    observe(connection)
    insert_result(connection, result_record())
    with pytest.raises(sqlite3.IntegrityError):
        insert_result(connection, result_record())
    assert connection.execute("SELECT count(*) FROM results").fetchone()[0] == 1


@pytest.mark.parametrize("table", ["artifacts", "runs", "results"])
def test_replace_cannot_bypass_immutability(
    connection: sqlite3.Connection, table: str
) -> None:
    prepare(connection)
    observe(connection)
    insert_result(connection, result_record())
    with pytest.raises(sqlite3.IntegrityError, match="immutable|created"):
        connection.execute(f"INSERT OR REPLACE INTO {table} SELECT * FROM {table}")


def test_initial_state_cannot_skip_execution_history(
    connection: sqlite3.Connection,
) -> None:
    prepare(connection)
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO tasks(run_id,task_id,trait_id,item_id,scheduling_ordinal,state) "
            "VALUES ('run','second','other','001',1,'complete')"
        )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO requests(run_id,request_uid,scheduling_ordinal,dependency_sha256,membership_sealed) "
            "VALUES ('run','second',1,?,1)",
            (DIGEST,),
        )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO attempts(run_id,attempt_id,request_uid,attempt_number,"
            "prompt_sha256,payload_sha256,membership_sha256,state) "
            "VALUES ('run','second','request',2,?,?,?,'normalized')",
            (DIGEST, DIGEST, DIGEST),
        )


def test_task_target_must_match_run_kind(connection: sqlite3.Connection) -> None:
    create_run(connection)
    with pytest.raises(sqlite3.IntegrityError, match="run kind"):
        connection.execute(
            "INSERT INTO tasks(run_id,task_id,trait_id,pair_uid,pair_id,scheduling_ordinal) "
            "VALUES ('run','pair','quality','occurrence','pair',0)"
        )


@pytest.mark.parametrize("field", ["manifest_sha256", "provider", "endpoint", "model"])
def test_result_provenance_must_match_run(
    connection: sqlite3.Connection, field: str
) -> None:
    prepare(connection)
    observe(connection)
    replacement = "b" * 64 if field == "manifest_sha256" else "different"
    with pytest.raises(sqlite3.IntegrityError, match="identity"):
        insert_result(
            connection, result_record().model_copy(update={field: replacement})
        )


def test_inflight_cancellation_needs_resolved_submission(
    connection: sqlite3.Connection,
) -> None:
    prepare(connection)
    start_submission(connection)
    with pytest.raises(sqlite3.IntegrityError, match="conclusive"):
        connection.execute("UPDATE attempts SET state='cancelled'")
    connection.execute("UPDATE submissions SET state='resolved',certainty='accepted'")
    connection.execute("UPDATE attempts SET state='cancelled'")


@pytest.mark.parametrize("kind", ["response", "provider_error"])
def test_attempt_error_needs_provider_error_observation(
    connection: sqlite3.Connection, kind: str
) -> None:
    prepare(connection)
    observe(connection, kind)
    if kind == "provider_error":
        connection.execute("UPDATE attempts SET state='errored'")
    else:
        with pytest.raises(sqlite3.IntegrityError, match="provider observation"):
            connection.execute("UPDATE attempts SET state='errored'")
