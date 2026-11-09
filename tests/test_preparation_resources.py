"""Fresh processes bound Python/native memory as sources and task products grow."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any, BinaryIO, cast

import pytest

from judgmentllm import preparation
from judgmentllm.persistence import repository as repository_module
from judgmentllm.persistence.repository import Repository
from judgmentllm.preparation.session import PreparedRun

FIXTURE = (
    Path(__file__).resolve().parents[1] / "scripts" / "preparation_memory_fixture.py"
)
MIB = 1024 * 1024


def measurement(directory: Path, responses: int, traits: int) -> dict[str, int | float]:
    completed = subprocess.run(
        [
            sys.executable,
            str(FIXTURE),
            "--directory",
            str(directory),
            "--responses",
            str(responses),
            "--traits",
            str(traits),
        ],
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert completed.returncode == 0, completed.stderr
    result = cast(dict[str, int | float], json.loads(completed.stdout))
    assert result["native_peak_bytes"] > 0, result
    assert result["native_peak_bytes"] < 256 * MIB, result
    assert result["python_peak_bytes"] < 32 * MIB, result
    assert result["python_after_gc_bytes"] < result["python_peak_bytes"], result
    assert result["sample_count"] == 64, result
    return result


@pytest.mark.resource
def test_large_sources_use_bounded_indexing_and_lookup_memory(tmp_path: Path) -> None:
    small = measurement(tmp_path / "small", 200, 1)
    large = measurement(tmp_path / "large", 2_000, 1)
    assert large["source_byte_length"] > 9 * small["source_byte_length"]
    assert large["python_peak_bytes"] < small["python_peak_bytes"] + 3 * MIB, (
        small,
        large,
    )
    assert large["native_peak_bytes"] < small["native_peak_bytes"] + 48 * MIB, (
        small,
        large,
    )
    assert large["python_after_gc_bytes"] < small["python_after_gc_bytes"] + MIB, (
        small,
        large,
    )


@pytest.mark.resource
def test_trait_product_remains_incremental_without_execution_rows(
    tmp_path: Path,
) -> None:
    small = measurement(tmp_path / "few-traits", 40, 20)
    large = measurement(tmp_path / "many-traits", 40, 200)
    assert large["logical_task_count"] == 10 * small["logical_task_count"]
    # Rubric/context memory may grow with trait count; full response/trait product
    # storage and execution rows are never needed for the bounded request sample.
    assert large["python_peak_bytes"] < small["python_peak_bytes"] + 8 * MIB, (
        small,
        large,
    )
    assert large["native_peak_bytes"] < small["native_peak_bytes"] + 48 * MIB, (
        small,
        large,
    )


def test_failed_recovery_closes_cached_source_descriptors_and_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_preparation_integrity import _sources  # pyright: ignore[reportPrivateUsage]

    configuration = _sources(tmp_path / "inputs", "full")
    store = tmp_path / "runtime"

    def interrupt(stage: str, source_id: str | None = None) -> None:
        if stage == "manifest_published":
            raise RuntimeError("interrupted before sealing")

    monkeypatch.setattr(preparation, "_checkpoint", interrupt)
    with pytest.raises(RuntimeError):
        preparation.prepare_run(configuration, store=store, run_id="001")

    sessions: list[PreparedRun] = []
    handles: list[BinaryIO] = []

    def tracked_session(*args: Any, **kwargs: Any) -> PreparedRun:
        prepared = PreparedRun(*args, **kwargs)
        sessions.append(prepared)
        return prepared

    def failed_seal(self: Repository, *args: object, **kwargs: object) -> None:
        assert sessions
        handles.extend(sessions[0]._handles.values())  # pyright: ignore[reportPrivateUsage]
        assert handles
        raise RuntimeError("synthetic seal failure after indexed reads")

    monkeypatch.setattr(preparation, "PreparedRun", tracked_session)
    monkeypatch.setattr(Repository, "seal_run", failed_seal)
    with pytest.raises(RuntimeError):
        preparation.open_prepared_run("001", store=store)
    assert all(handle.closed for handle in handles)
    assert not sessions[0]._handles  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        sessions[0].repository.connection.execute("SELECT 1")


def test_session_close_closes_every_resource_when_a_descriptor_close_fails(
    tmp_path: Path,
) -> None:
    from test_preparation_integrity import _sources  # pyright: ignore[reportPrivateUsage]

    configuration = _sources(tmp_path / "inputs", "minimal")
    prepared = preparation.prepare_run(
        configuration, store=tmp_path / "runtime", run_id="001"
    )
    prepared.get_response("001")
    handles = list(prepared._handles.values())  # pyright: ignore[reportPrivateUsage]

    class FailedClose:
        closed = False

        def close(self) -> None:
            self.closed = True
            raise OSError("synthetic descriptor close failure")

    failing = FailedClose()
    prepared._handles["failed"] = cast(BinaryIO, failing)  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(OSError, match="synthetic descriptor"):
        prepared.close()
    assert failing.closed and all(handle.closed for handle in handles)
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        prepared.repository.connection.execute("SELECT 1")
    prepared.close()


def test_repository_closes_connection_when_initial_setup_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connections: list[sqlite3.Connection] = []
    original_connect = sqlite3.connect

    class RejectedSetup(sqlite3.Connection):
        def __setattr__(self, name: str, value: object) -> None:
            if name == "row_factory":
                raise RuntimeError("synthetic connection setup failure")
            super().__setattr__(name, value)

    def connect(
        database: str | Path, *, isolation_level: str | None, timeout: float
    ) -> sqlite3.Connection:
        connection = original_connect(
            database,
            isolation_level=None,
            timeout=timeout,
            factory=RejectedSetup,
        )
        connections.append(connection)
        return connection

    monkeypatch.setattr(repository_module.sqlite3, "connect", connect)
    with pytest.raises(RuntimeError, match="synthetic connection"):
        Repository(tmp_path)
    assert len(connections) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connections[0].execute("SELECT 1")
