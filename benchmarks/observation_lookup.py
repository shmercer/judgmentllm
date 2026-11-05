"""Read-only observation lookup timing, query-plan and exact VM-step evidence.

Ordinary wall/process CPU samples run without progress handlers or profilers.
Exact SQLite VM counting is a separate pass and never supplies throughput data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import statistics
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from provenance import host_identity, runtime_identity, source_fingerprint

QUERIES = {
    "baseline": "SELECT observation_id FROM observations WHERE run_id=? AND submission_id=? ORDER BY observation_id LIMIT 2",
    "candidate": "SELECT observation_id FROM observations WHERE run_id=? AND submission_id=? ORDER BY observation_key LIMIT 2",
}


def file_identity(path: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return {
        "path": str(path.resolve()),
        "bytes": path.stat().st_size,
        "sha256": digest.hexdigest(),
    }


def count_steps(
    connection: sqlite3.Connection, query: str, parameters: tuple[str, str]
) -> tuple[list[tuple[str]], int]:
    steps = 0

    def progress() -> int:
        nonlocal steps
        steps += 1
        return 0

    connection.set_progress_handler(progress, 1)
    try:
        rows = connection.execute(query, parameters).fetchall()
    finally:
        connection.set_progress_handler(None, 0)
    return rows, steps


def run(
    database: Path,
    source_root: Path,
    *,
    run_id: str = "001",
    samples: int = 5,
    selections: int = 8,
    warmups: int = 1,
) -> dict[str, Any]:
    if samples < 1 or selections < 1 or warmups < 0:
        raise ValueError("samples/selections must be positive and warmups nonnegative")
    before = {
        path.name: file_identity(path)
        for path in (database, database.with_name(database.name + "-wal"))
        if path.exists()
    }
    with sqlite3.connect(
        database.resolve().as_uri() + "?mode=ro", uri=True
    ) as connection:
        connection.execute("PRAGMA query_only=ON")
        count = connection.execute(
            "SELECT COUNT(*) FROM observations WHERE run_id=?", (run_id,)
        ).fetchone()[0]
        identifiers = [
            row[0]
            for row in connection.execute(
                "SELECT submission_id FROM observations WHERE run_id=? ORDER BY submission_id LIMIT ?",
                (run_id, selections),
            )
        ]
        assert len(identifiers) == min(selections, count) and identifiers
        parameters = [(run_id, identifier) for identifier in identifiers]
        plans = {
            name: [
                list(row)
                for row in connection.execute(
                    "EXPLAIN QUERY PLAN " + query, parameters[0]
                )
            ]
            for name, query in QUERIES.items()
        }
        results = {
            name: [
                connection.execute(query, selected).fetchall()
                for selected in parameters
            ]
            for name, query in QUERIES.items()
        }
        assert results["baseline"] == results["candidate"]
        assert all(len(rows) == 1 for rows in results["baseline"])
        for _ in range(warmups):
            for query in QUERIES.values():
                for selected in parameters:
                    connection.execute(query, selected).fetchall()
        ordinary: dict[str, list[dict[str, float | int]]] = {
            name: [] for name in QUERIES
        }
        for repetition in range(samples):
            for name in (
                tuple(QUERIES) if repetition % 2 == 0 else tuple(reversed(QUERIES))
            ):
                started_wall, started_cpu = (
                    time.perf_counter_ns(),
                    time.process_time_ns(),
                )
                for selected in parameters:
                    connection.execute(QUERIES[name], selected).fetchall()
                ordinary[name].append(
                    {
                        "repetition": repetition,
                        "queries": len(parameters),
                        "wall_seconds": (time.perf_counter_ns() - started_wall) / 1e9,
                        "cpu_seconds": (time.process_time_ns() - started_cpu) / 1e9,
                    }
                )
        # Exact counters are deliberately collected only after ordinary timing.
        counted: dict[str, list[dict[str, Any]]] = {}
        for name, query in QUERIES.items():
            counted[name] = []
            for selected in parameters:
                rows, steps = count_steps(connection, query, selected)
                assert rows == results[name][parameters.index(selected)]
                counted[name].append(
                    {
                        "parameters": selected,
                        "returned_rows": len(rows),
                        "sqlite_vm_steps": steps,
                    }
                )
    after = {
        path.name: file_identity(path)
        for path in (database, database.with_name(database.name + "-wal"))
        if path.exists()
    }
    assert before == after, "read-only experiment changed database identity"
    return {
        "recorded_at": datetime.now(UTC).isoformat(),
        "sqlite": sqlite3.sqlite_version,
        "runtime_identity": runtime_identity(),
        "source_tree_sha256": source_fingerprint(source_root),
        "host_identity": host_identity(source_root),
        "tool_identity": file_identity(Path(__file__)),
        "database_identity": before,
        "observation_rows": count,
        "run_id": run_id,
        "selected_parameters": parameters,
        "queries": QUERIES,
        "query_plans": plans,
        "ordinary_samples": ordinary,
        "ordinary_summary": {
            name: {
                clock: {
                    "median": statistics.median(row[clock] for row in rows),
                    "minimum": min(row[clock] for row in rows),
                    "maximum": max(row[clock] for row in rows),
                }
                for clock in ("wall_seconds", "cpu_seconds")
            }
            for name, rows in ordinary.items()
        },
        "exact_vm_counts": counted,
        "result_identity_parity": True,
        "method": "read-only database; streaming identity hashing and configured warmups before interleaved ordinary samples; separate exact progress-handler VM counts; OS caches uncontrolled",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run-id", default="001")
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--selections", type=int, default=8)
    parser.add_argument("--warmups", type=int, default=1)
    options = parser.parse_args()
    if options.output.exists():
        parser.error("output already exists; choose a new evidence path")
    result = run(
        options.database,
        options.source_root,
        run_id=options.run_id,
        samples=options.samples,
        selections=options.selections,
        warmups=options.warmups,
    )
    options.output.parent.mkdir(parents=True, exist_ok=True)
    with options.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2)
    print(
        json.dumps(
            {
                "output": str(options.output),
                "ordinary_summary": result["ordinary_summary"],
                "exact_vm_counts": result["exact_vm_counts"],
            }
        )
    )


if __name__ == "__main__":
    main()
