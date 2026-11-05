"""Inspection verifies existing registries without initialization or repair."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from judgmentllm.persistence.models import NotFoundError, SchemaVersionError
from judgmentllm.persistence.repository import ReadOnlyRepository
from judgmentllm.persistence.schema import (
    SCHEMA_CHECKSUM,
    SCHEMA_VERSION,
    initialize,
    verify,
)


def snapshot(root: Path, *, excluded: frozenset[str] = frozenset()) -> dict[str, bytes]:
    return (
        {
            str(path.relative_to(root)): path.read_bytes()
            for path in root.rglob("*")
            if path.is_file() and str(path.relative_to(root)) not in excluded
        }
        if root.exists()
        else {}
    )


@pytest.mark.parametrize(
    "kind", ["zero", "malformed", "uninitialized", "unversioned", "incompatible"]
)
def test_invalid_registry_remains_byte_identical_and_uninitialized(
    tmp_path: Path, kind: str
) -> None:
    store = tmp_path / "store"
    store.mkdir()
    database = store / "registry.sqlite3"
    if kind == "zero":
        database.write_bytes(b"")
    elif kind == "malformed":
        database.write_bytes(b"synthetic invalid SQLite registry")
    else:
        with closing(sqlite3.connect(database, isolation_level=None)) as connection:
            if kind == "uninitialized":
                connection.execute("PRAGMA user_version=0")
            elif kind == "unversioned":
                connection.execute("CREATE TABLE arbitrary(value TEXT)")
            else:
                connection.execute("PRAGMA user_version=2")
    before = snapshot(store)
    with pytest.raises((SchemaVersionError, sqlite3.DatabaseError)):
        ReadOnlyRepository(store)
    assert snapshot(store) == before
    assert not (store / "registry.sqlite3-wal").exists()
    assert not (store / "registry.sqlite3-shm").exists()
    if kind == "zero":
        assert database.read_bytes() == b""


def test_missing_readonly_registry_never_creates_paths(tmp_path: Path) -> None:
    store = tmp_path / "missing" / "nested"
    before = snapshot(tmp_path)
    with pytest.raises(NotFoundError):
        ReadOnlyRepository(store)
    assert snapshot(tmp_path) == before and not store.exists()


@pytest.mark.parametrize("damage", ["checksum", "structure", "migration"])
def test_shared_verifier_rejects_damaged_registry_without_repair(
    tmp_path: Path, damage: str
) -> None:
    store = tmp_path / "store"
    store.mkdir()
    database = store / "registry.sqlite3"
    # A rollback-mode synthetic database isolates schema verification from WAL
    # coordination. Production write Repository continues selecting WAL itself.
    with closing(sqlite3.connect(database, isolation_level=None)) as connection:
        initialize(connection)
        if damage == "checksum":
            connection.execute("DROP TRIGGER schema_migrations_no_update")
            connection.execute("UPDATE schema_migrations SET checksum='changed'")
        elif damage == "structure":
            connection.execute("DROP TRIGGER results_no_delete")
        else:
            connection.execute("DROP TABLE schema_migrations")
    before = snapshot(store)
    with pytest.raises((SchemaVersionError, sqlite3.DatabaseError)):
        ReadOnlyRepository(store)
    assert snapshot(store) == before


def test_shared_verifier_is_readonly_and_write_initialization_is_unchanged(
    tmp_path: Path,
) -> None:
    store = tmp_path / "store"
    store.mkdir()
    database = store / "registry.sqlite3"
    with closing(sqlite3.connect(database, isolation_level=None)) as connection:
        initialize(connection)
    before = snapshot(store)
    with ReadOnlyRepository(store) as repository:
        verify(repository.connection)
        assert repository.connection.execute("PRAGMA query_only").fetchone()[0] == 1
        assert (
            repository.connection.execute("PRAGMA user_version").fetchone()[0]
            == SCHEMA_VERSION
        )
        assert (
            repository.connection.execute(
                "SELECT checksum FROM schema_migrations"
            ).fetchone()[0]
            == SCHEMA_CHECKSUM
        )
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            repository.connection.execute("CREATE TABLE must_not_exist(value TEXT)")
        # query_only is defense in depth: disabling it cannot grant this
        # connection write permission to the underlying database file.
        repository.connection.execute("PRAGMA query_only=OFF")
        assert repository.connection.execute("PRAGMA query_only").fetchone()[0] == 0
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            repository.connection.execute(
                "CREATE TABLE still_must_not_exist(value TEXT)"
            )
    assert snapshot(store) == before


@pytest.mark.parametrize("missing", ["directory", "objects"])
def test_sealed_inspection_does_not_recreate_missing_artifact_paths(
    tmp_path: Path, missing: str
) -> None:
    import shutil

    from test_preparation_rendering import specification

    from judgmentllm._lifecycle_store import inspect_run
    from judgmentllm.persistence.models import PersistenceError
    from judgmentllm.preparation import PreparationError, prepare_run

    spec = specification(tmp_path / "input", responses=1, traits=1)
    store = tmp_path / "store"
    with prepare_run(spec, store=store, run_id="001") as prepared:
        directory = prepared.repository.run_directory("001")
    database = store / "registry.sqlite3"
    # Isolate artifact inspection from the separately reviewed SQLite WAL/SHM
    # coordination policy; production initialization still selects WAL itself.
    with closing(sqlite3.connect(database, isolation_level=None)) as connection:
        connection.execute("PRAGMA journal_mode=DELETE")
    selected = directory if missing == "directory" else directory / "objects"
    shutil.rmtree(selected)
    before = snapshot(store)
    with ReadOnlyRepository(store) as repository:
        with pytest.raises((PersistenceError, PreparationError)):
            inspect_run(repository, "001")
    assert not selected.exists() and snapshot(store) == before


def test_hash_only_artifact_lookup_never_initializes_run_directory(
    tmp_path: Path,
) -> None:
    from judgmentllm.persistence.models import RunRecord
    from judgmentllm.persistence.repository import Repository

    store = tmp_path / "store"
    with Repository(store) as repository:
        repository.create_run(
            RunRecord(
                run_id="001",
                task_kind="rubric",
                provider="mock",
                endpoint="synthetic",
                model="fixture",
            )
        )
        repository.connection.execute(
            "INSERT INTO artifacts VALUES(?,?,?,?,?)",
            ("001", "a" * 64, 0, "application/octet-stream", None),
        )
        directory = repository.run_directory("001")
        assert not directory.exists()
        identity = repository.get_artifact("001", "a" * 64)
        assert identity.relative_path is None and not directory.exists()


def application_snapshot(
    root: Path, *, locked_file: Path | None = None
) -> dict[str, bytes]:
    """Exclude SQLite coordination; an explicitly locked file is checked by stat."""
    excluded = {"registry.sqlite3-wal", "registry.sqlite3-shm"}
    if locked_file is not None:
        excluded.add(str(locked_file.relative_to(root)))
    return snapshot(root, excluded=frozenset(excluded))


@pytest.mark.parametrize(
    "kind", ["zero", "malformed", "uninitialized", "unversioned", "incompatible"]
)
@pytest.mark.parametrize(
    "operation",
    [
        "get_explicit",
        "get_default",
        "get_directory",
        "list_explicit",
        "list_default",
        "export_default",
        "resume_default",
    ],
)
def test_public_inspection_rejects_invalid_existing_registry_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, operation: str
) -> None:
    from judgmentllm import export_run_results, get_run, list_runs, resume_run
    from judgmentllm.lifecycle_models import LifecycleError
    from judgmentllm.persistence.hashing import bytes_digest

    store = tmp_path / "store"
    store.mkdir()
    database = store / "registry.sqlite3"
    if kind == "zero":
        database.write_bytes(b"")
    elif kind == "malformed":
        database.write_bytes(b"synthetic invalid registry")
    else:
        with closing(sqlite3.connect(database, isolation_level=None)) as connection:
            if kind == "unversioned":
                connection.execute("CREATE TABLE arbitrary(value TEXT)")
            else:
                connection.execute(
                    f"PRAGMA user_version={2 if kind == 'incompatible' else 0}"
                )
    monkeypatch.setenv("JUDGMENTLLM_RUN_HOME", str(store))
    before = snapshot(store)
    with pytest.raises(LifecycleError) as error:
        if operation == "get_explicit":
            get_run("001", store=store)
        elif operation == "get_default":
            get_run("001")
        elif operation == "get_directory":
            get_run(store / "runs" / bytes_digest(b"001"))
        elif operation == "list_explicit":
            list_runs(store=store)
        elif operation == "list_default":
            list_runs()
        elif operation == "export_default":
            export_run_results("001")
        else:
            resume_run("001")
    assert error.value.category == "execution"
    assert snapshot(store) == before and not (store / "runs").exists()
    if kind == "zero":
        assert database.read_bytes() == b""


def test_public_missing_store_and_run_never_create_application_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from judgmentllm import get_run, list_runs
    from judgmentllm.lifecycle_models import LifecycleError
    from judgmentllm.persistence.repository import Repository

    store = tmp_path / "missing" / "nested"
    monkeypatch.setenv("JUDGMENTLLM_RUN_HOME", str(store))
    assert list_runs().runs == ()
    assert not store.exists()
    with pytest.raises(LifecycleError) as error:
        get_run("absent")
    assert error.value.category == "not_found" and not store.exists()
    with Repository(store):
        pass
    before = application_snapshot(store)
    assert list_runs(store=store).runs == ()
    with pytest.raises(LifecycleError) as error:
        get_run("absent", store=store)
    assert error.value.category == "not_found"
    assert error.value.run_id == "absent" and error.value.store == str(store.resolve())
    assert application_snapshot(store) == before and not (store / "runs").exists()


def test_closed_wal_public_inspection_only_allows_sqlite_coordination_files(
    tmp_path: Path,
) -> None:
    from judgmentllm import get_run, list_runs
    from judgmentllm.persistence.models import RunRecord
    from judgmentllm.persistence.repository import Repository

    store = tmp_path / "closed WAL with # and % in path"
    with Repository(store) as repository:
        repository.create_run(
            RunRecord(
                run_id="001",
                task_kind="rubric",
                provider="mock",
                endpoint="synthetic",
                model="fixture",
            )
        )
    assert not (store / "registry.sqlite3-wal").exists()
    assert not (store / "registry.sqlite3-shm").exists()
    before = application_snapshot(store)
    assert get_run("001", store=store).run_id == "001"
    assert [run.run_id for run in list_runs(store=store).runs] == ["001"]
    assert application_snapshot(store) == before and not (store / "runs").exists()


_INDEPENDENT_READER = r"""
import json,sys
from judgmentllm import get_run,list_runs
status=get_run("001",store=sys.argv[1])
print(json.dumps({"run_id":status.run_id,"executor_active":status.executor_active,"runs":[run.run_id for run in list_runs(store=sys.argv[1]).runs]}))
"""


def test_independent_reader_sees_committed_wal_with_active_executor_without_state_changes(
    tmp_path: Path,
) -> None:
    import json
    import subprocess
    import sys

    from judgmentllm.persistence.hashing import bytes_digest
    from judgmentllm.persistence.models import RunRecord
    from judgmentllm.persistence.ownership import ExecutorOwnership
    from judgmentllm.persistence.repository import Repository

    store = tmp_path / "store"
    # Keep ownership in the parent. The independent reader never owns execution,
    # so timeout cleanup cannot orphan a Windows venv writer/lock-holder process.
    with Repository(store) as repository:
        repository.connection.execute("PRAGMA wal_autocheckpoint=0")
        repository.create_run(
            RunRecord(
                run_id="001",
                task_kind="rubric",
                provider="mock",
                endpoint="synthetic",
                model="fixture",
            )
        )
        with ExecutorOwnership(repository, "001"):
            owner_before = repository.get_owner("001")
            events_before = tuple(repository.events("001"))
            database = store / "registry.sqlite3"
            # Schema, latest run and ownership exist only in committed WAL. An
            # immutable/no-WAL reader cannot pass the schema verification gate.
            assert int.from_bytes(database.read_bytes()[60:64], "big") == 0
            lock_file = store / "runs" / bytes_digest(b"001") / "executor.lock"
            lock_before = lock_file.stat()
            before = application_snapshot(store, locked_file=lock_file)
            reader = subprocess.run(
                [sys.executable, "-c", _INDEPENDENT_READER, str(store)],
                capture_output=True,
                text=True,
                timeout=15,
            )
            assert reader.returncode == 0 and reader.stderr == ""
            assert json.loads(reader.stdout) == {
                "run_id": "001",
                "executor_active": True,
                "runs": ["001"],
            }
            assert application_snapshot(store, locked_file=lock_file) == before
            assert repository.get_owner("001") == owner_before
            assert tuple(repository.events("001")) == events_before
            lock_after = lock_file.stat()
            assert (
                lock_after.st_dev,
                lock_after.st_ino,
                lock_after.st_size,
                lock_after.st_mtime_ns,
            ) == (
                lock_before.st_dev,
                lock_before.st_ino,
                lock_before.st_size,
                lock_before.st_mtime_ns,
            )


def test_missing_run_control_retains_requested_id_without_creating_state(
    tmp_path: Path,
) -> None:
    import json

    from typer.testing import CliRunner

    from judgmentllm.cli import app
    from judgmentllm.persistence.repository import Repository

    store = tmp_path / "store"
    with Repository(store):
        pass
    before = application_snapshot(store)
    result = CliRunner().invoke(
        app, ["runs", "status", "--run", "absent", "--store", str(store), "--json"]
    )
    assert result.exit_code == 2
    control = json.loads(result.stdout)
    assert control["run_id"] == "absent" and control["error"]["category"] == "not_found"
    assert application_snapshot(store) == before and not (store / "runs").exists()


@pytest.mark.parametrize("damage", ["checksum", "structure", "migration"])
@pytest.mark.parametrize("operation", ["get", "list", "directory", "cli"])
def test_public_inspection_rejects_damaged_schema_without_repair(
    tmp_path: Path, damage: str, operation: str
) -> None:
    import json

    from typer.testing import CliRunner

    from judgmentllm import get_run, list_runs
    from judgmentllm.cli import app
    from judgmentllm.lifecycle_models import LifecycleError
    from judgmentllm.persistence.hashing import bytes_digest

    store = tmp_path / "store"
    store.mkdir()
    with closing(
        sqlite3.connect(store / "registry.sqlite3", isolation_level=None)
    ) as connection:
        initialize(connection)
        if damage == "checksum":
            connection.execute("DROP TRIGGER schema_migrations_no_update")
            connection.execute("UPDATE schema_migrations SET checksum='changed'")
        elif damage == "structure":
            connection.execute("DROP TRIGGER results_no_delete")
        else:
            connection.execute("DROP TABLE schema_migrations")
    before = snapshot(store)
    if operation == "cli":
        result = CliRunner().invoke(
            app, ["runs", "list", "--store", str(store), "--json"]
        )
        assert result.exit_code == 1
        control = json.loads(result.stdout)
        assert (
            control["status"] == "error" and control["error"]["category"] == "execution"
        )
    else:
        with pytest.raises(LifecycleError) as error:
            if operation == "get":
                get_run("001", store=store)
            elif operation == "list":
                list_runs(store=store)
            else:
                get_run(store / "runs" / bytes_digest(b"001"))
        assert error.value.category == "execution"
    assert snapshot(store) == before and not (store / "runs").exists()
