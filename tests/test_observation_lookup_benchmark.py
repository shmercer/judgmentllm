"""Query equivalence and independent SQLite VM evidence on small synthetic data."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from test_scale_benchmarks import benchmark_module


def test_observation_lookup_candidate_preserves_identity_and_conflicts(
    tmp_path: Path,
) -> None:
    tool = benchmark_module("observation_lookup")
    database = tmp_path / "observations.sqlite3"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(
            "CREATE TABLE observations(run_id TEXT, observation_id TEXT, submission_id TEXT, observation_key TEXT, PRIMARY KEY(run_id,observation_id), UNIQUE(run_id,submission_id,observation_key))"
        )
        connection.executemany(
            "INSERT INTO observations VALUES(?,?,?,?)",
            [
                (
                    "001",
                    f"observation-{index:04}",
                    f"submission-{index:04}",
                    f"key-{index:04}",
                )
                for index in range(500)
            ],
        )
        for submission, expected in (("missing", 0), ("submission-0000", 1)):
            baseline, old_steps = tool.count_steps(
                connection, tool.QUERIES["baseline"], ("001", submission)
            )
            candidate, new_steps = tool.count_steps(
                connection, tool.QUERIES["candidate"], ("001", submission)
            )
            assert baseline == candidate and len(candidate) == expected
            assert new_steps < old_steps
        connection.execute(
            "INSERT INTO observations VALUES('001','a-second','submission-0000','key-second')"
        )
        assert (
            len(
                connection.execute(
                    tool.QUERIES["baseline"], ("001", "submission-0000")
                ).fetchall()
            )
            == 2
        )
        assert (
            len(
                connection.execute(
                    tool.QUERIES["candidate"], ("001", "submission-0000")
                ).fetchall()
            )
            == 2
        )


@pytest.mark.parametrize("counter_failure", [False, True])
def test_observation_lookup_experiment_closes_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, counter_failure: bool
) -> None:
    tool = benchmark_module("observation_lookup")
    database = tmp_path / "observations.sqlite3"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(
            "CREATE TABLE observations(run_id TEXT, observation_id TEXT, submission_id TEXT, observation_key TEXT)"
        )
        connection.execute("INSERT INTO observations VALUES('001','o','s','k')")

    original_connect = sqlite3.connect
    opened: list[sqlite3.Connection] = []

    def connect(database: str, *, uri: bool = False) -> sqlite3.Connection:
        connection = original_connect(database, uri=uri)
        opened.append(connection)
        return connection

    def fail_counter(
        connection: sqlite3.Connection, query: str, parameters: tuple[str, str]
    ) -> tuple[list[tuple[str]], int]:
        raise LookupError("synthetic counter failure")

    monkeypatch.setattr(tool.sqlite3, "connect", connect)
    if counter_failure:
        monkeypatch.setattr(tool, "count_steps", fail_counter)
    try:
        if counter_failure:
            with pytest.raises(LookupError, match="synthetic counter failure"):
                tool.run(
                    database,
                    Path(__file__).resolve().parents[1],
                    samples=1,
                    selections=1,
                    warmups=0,
                )
        else:
            result = tool.run(
                database,
                Path(__file__).resolve().parents[1],
                samples=1,
                selections=1,
                warmups=0,
            )
            assert result["result_identity_parity"] is True
            assert result["observation_rows"] == 1
        assert len(opened) == 1
        # Keep a strong reference: closure must not depend on garbage collection.
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            opened[0].execute("SELECT 1")
    finally:
        for connection in opened:
            connection.close()
