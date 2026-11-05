"""Streaming independent-process checks of completed scale exports and recovery."""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import time
from itertools import zip_longest
from pathlib import Path
from typing import Any

from judgmentllm.config import load_run_spec
from judgmentllm.execution import execute_run
from judgmentllm.execution.exports import export_run
from judgmentllm.persistence.repository import ReadOnlyRepository
from judgmentllm.providers.mock import MockAdapter, MockScenario
from judgmentllm.results import RubricResult


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            value.update(chunk)
    return value.hexdigest()


def verify(report: dict[str, Any]) -> dict[str, Any]:
    from execution_worker import native_peak_bytes

    process_started = time.process_time()
    directory = Path(report["working_directory"])
    store = directory / "store"
    adapter = MockAdapter(
        directory / "service.sqlite3",
        scenario=MockScenario(category_index=1, probabilities=True),
    )
    before = adapter.ledger.acceptance_count()
    with ReadOnlyRepository(store) as repository:
        summary = repository.summary("001")
        attempts_before = summary.attempts
        assert summary.results == report["result_rows"]
        assert (
            repository.connection.execute("PRAGMA integrity_check").fetchone()[0]
            == "ok"
        )
        assert not repository.connection.execute("PRAGMA foreign_key_check").fetchall()
        counts = {
            table: repository.connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE run_id='001'"
            ).fetchone()[0]
            for table in (
                "tasks",
                "requests",
                "attempts",
                "submissions",
                "observations",
                "request_tasks",
                "attempt_tasks",
                "results",
            )
        }
        assert (
            counts["attempts"] == counts["submissions"] == report["prepared_attempts"]
        )
        assert counts["requests"] == report["prepared_requests"]
        assert counts["observations"] == report["completed_physical_requests"]
        assert counts["tasks"] == counts["request_tasks"] == counts["attempt_tasks"]
        if report["extent"] == "complete":
            assert counts["tasks"] == counts["results"] == report["logical_task_count"]
        repository.connection.execute(
            "ATTACH DATABASE ? AS ledger",
            ((directory / "service.sqlite3").as_uri() + "?mode=ro",),
        )
        assert (
            repository.connection.execute(
                "SELECT COUNT(*) FROM ledger.acceptances WHERE accepted=1"
            ).fetchone()[0]
            == report["accepted_physical_requests"]
        )
        assert (
            repository.connection.execute(
                "SELECT COUNT(*) FROM submissions s JOIN ledger.acceptances a ON a.submission_id=s.submission_id WHERE s.payload_sha256<>a.payload_sha256 OR s.correlation_id<>a.correlation_id OR a.acceptance_count<>1"
            ).fetchone()[0]
            == 0
        )
        assert (
            repository.connection.execute(
                "SELECT COUNT(*) FROM observations o LEFT JOIN ledger.acceptances a ON o.submission_id=a.submission_id AND o.observation_key=a.observation_key WHERE a.submission_id IS NULL"
            ).fetchone()[0]
            == 0
        )
    recovery_wall, recovery_cpu = time.perf_counter(), time.process_time()
    if report["extent"] == "complete":
        asyncio.run(
            execute_run(
                "001",
                store=store,
                adapter=adapter,
                spec=load_run_spec(directory / "inputs" / "run.json"),
            )
        )
        assert adapter.ledger.acceptance_count() == before
    recovery = {
        "wall_seconds": time.perf_counter() - recovery_wall,
        "cpu_seconds": time.process_time() - recovery_cpu,
    }
    exports = report["export_files"]
    canonical = next(item for item in exports if item["format"] == "jsonl")
    r_csv = next(item for item in exports if item["view"] == "r")
    count = 0
    identity = hashlib.sha256()
    with (
        Path(canonical["path"]).open(encoding="utf-8") as jsonl,
        Path(r_csv["path"]).open(encoding="utf-8", newline="") as table,
    ):
        for line, csv_row in zip_longest(jsonl, csv.DictReader(table)):
            assert line is not None and csv_row is not None
            result = RubricResult.model_validate_json(line)
            assert (
                result.status == "complete" and result.primary_score_kind == "expected"
            )
            assert result.item_id.isdigit() and len(result.item_id) >= 6
            assert len(result.resolved_levels) == len(result.probabilities or ()) == 4
            assert [level.numeric_value for level in result.resolved_levels] == [
                0,
                1,
                3,
                7,
            ]
            assert result.provider == "mock" and result.manifest_sha256 is not None
            for field in (
                "run_id",
                "task_id",
                "request_uid",
                "attempt_id",
                "item_id",
                "trait_id",
                "status",
                "manifest_sha256",
                "primary_score_kind",
            ):
                assert csv_row[field] == getattr(result, field)
            assert json.loads(csv_row["probabilities_json"]) == [
                item.model_dump() for item in result.probabilities or ()
            ]
            assert json.loads(csv_row["resolved_levels_json"]) == [
                item.model_dump() for item in result.resolved_levels
            ]
            identity.update((result.task_id + "\0" + result.attempt_id + "\n").encode())
            count += 1
    assert count == report["result_rows"]
    for item in exports:
        path = Path(item["path"])
        assert path.stat().st_size == item["bytes"] and digest(path) == item["sha256"]
        if item["format"] == "csv":
            with path.open(encoding="utf-8", newline="") as stream:
                assert sum(1 for _ in csv.DictReader(stream)) == count
        # Reopening the same immutable snapshot must not alter its identity.
        reopened = export_run(
            "001",
            store=store,
            format=item["format"],
            view=item["view"],
            high_water_mark=item["result_high_water_mark"],
        )
        assert (
            reopened.artifact.sha256 == item["sha256"] and reopened.row_count == count
        )
    with ReadOnlyRepository(store) as repository:
        assert repository.summary("001").attempts == attempts_before
    peak = native_peak_bytes()
    assert peak < 256 * 1024**2, "independent verification process exceeds 256 MiB"
    return {
        "verified_rows": count,
        "ordered_task_attempt_sha256": identity.hexdigest(),
        "no_op_resume_acceptance_delta": adapter.ledger.acceptance_count() - before,
        "no_op_resume_checked": report["extent"] == "complete",
        "integrity_check": "ok",
        "counts": counts,
        "recovery": recovery,
        "native_peak_bytes": peak,
        "total_cpu_seconds": time.process_time() - process_started,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    options = parser.parse_args()
    print(json.dumps(verify(json.loads(options.report.read_text(encoding="utf-8")))))


if __name__ == "__main__":
    main()
