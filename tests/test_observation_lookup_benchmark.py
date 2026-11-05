"""Query equivalence and independent SQLite VM evidence on small synthetic data."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from test_scale_benchmarks import benchmark_module


def test_observation_lookup_candidate_preserves_identity_and_conflicts(
    tmp_path: Path,
) -> None:
    tool = benchmark_module("observation_lookup")
    database = tmp_path / "observations.sqlite3"
    with sqlite3.connect(database) as connection:
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
