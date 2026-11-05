"""Small offline persistence experiments, separate from ordinary scale timings.

Invoke one case per fresh process. These are diagnostic workloads, not the
5,000-response qualification and not additions to the required CI critical path.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Literal

from provenance import host_identity, runtime_identity, source_fingerprint

from judgmentllm import get_run
from judgmentllm.config import load_run_spec
from judgmentllm.execution import execute_run, recover_run
from judgmentllm.execution.exports import export_run
from judgmentllm.execution.retry import prepare_subset_retry
from judgmentllm.persistence.ownership import ExecutorOwnership
from judgmentllm.persistence.repository import Repository
from judgmentllm.preparation import open_prepared_run, prepare_run
from judgmentllm.providers import MockAdapter, MockMemberScenario, MockScenario


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def document(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def fixture(root: Path, responses: int) -> tuple[Path, Path]:
    root.mkdir(parents=True, exist_ok=False)
    source = root / "responses.jsonl"
    with source.open("w", encoding="utf-8") as stream:
        for index in range(responses):
            stream.write(
                json.dumps(
                    {"item_id": f"{index:06d}", "text": "Synthetic assessment. " * 64}
                )
                + "\n"
            )
    path = root / "run.json"
    document(
        path,
        {
            "task_kind": "rubric",
            "input": source.name,
            "provider": "mock",
            "endpoint": "synthetic",
            "model": "fixture",
            "capture": "minimal",
            "seed": 42,
            "execution": {
                "concurrency": 4,
                "grouping": "auto",
                "max_traits_per_request": 5,
            },
            "rubric": {
                "rubric_id": "001",
                "name": "Persistence diagnostic",
                "traits": [
                    {
                        "trait_id": f"{index:03d}",
                        "name": "Quality",
                        "description": "Assess the synthetic evidence.",
                        "levels": [
                            {"label": label, "numeric_value": number}
                            for label, number in (
                                ("low", 1),
                                ("middle", 3),
                                ("high", 8),
                            )
                        ],
                    }
                    for index in range(20)
                ],
            },
        },
    )
    store = root / "store"
    adapter = MockAdapter(
        root / "service.sqlite3", scenario=MockScenario(probabilities=True)
    )
    spec = load_run_spec(path)
    with prepare_run(
        path, store=store, run_id="001", capabilities=adapter.capabilities(spec)
    ):
        pass
    return path, store


def exports(store: Path, *, high_water_mark: int | None = None) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    combinations: tuple[
        tuple[Literal["jsonl", "csv"], Literal["canonical", "r"]], ...
    ] = (
        ("jsonl", "canonical"),
        ("csv", "canonical"),
        ("csv", "r"),
    )
    for format, view in combinations:
        started = time.perf_counter()
        result = export_run(
            "001",
            store=store,
            format=format,
            view=view,
            high_water_mark=high_water_mark,
        )
        records.append(
            {
                "format": format,
                "view": view,
                "rows": result.row_count,
                "bytes": result.artifact.byte_length,
                "sha256": result.artifact.sha256,
                "path": str(result.artifact_path),
                "result_high_water_mark": result.result_high_water_mark,
                "export_id": result.export_id,
                "wall_seconds": time.perf_counter() - started,
            }
        )
    return records


def authorize(
    store: Path,
    configuration: Path,
    adapter: MockAdapter,
    selections: list[tuple[str, tuple[str, ...], str]],
    *,
    allow_completed: bool,
) -> None:
    spec = load_run_spec(configuration)
    with (
        Repository(store) as repository,
        ExecutorOwnership(repository, "001") as ownership,
    ):
        assert ownership.owner is not None
        token = ownership.owner.token
        with open_prepared_run(
            "001",
            store=store,
            spec=spec,
            capabilities=adapter.capabilities(spec),
            owner_token=token,
        ) as prepared:
            for parent, tasks, operation in selections:
                dependency = prepared.dependency_for_request(parent)
                first = prepare_subset_retry(
                    prepared,
                    adapter,
                    dependency=dependency,
                    task_ids=tasks,
                    operation_id=operation,
                    owner_token=token,
                    allow_completed=allow_completed,
                )
                assert first == prepare_subset_retry(
                    prepared,
                    adapter,
                    dependency=dependency,
                    task_ids=tasks,
                    operation_id=operation,
                    owner_token=token,
                    allow_completed=allow_completed,
                )


def verify_history(root: Path) -> dict[str, Any]:
    adapter = MockAdapter(
        root / "service.sqlite3", scenario=MockScenario(probabilities=True)
    )
    report = asyncio.run(
        recover_run(
            "001",
            store=root / "store",
            adapter=adapter,
            spec=load_run_spec(root / "run.json"),
        )
    )
    assert adapter.submit_calls == 0 and adapter.ledger.acceptance_count() == 181
    assert report.summary.results == 503 and report.summary.unsuccessful_results == 3
    with Repository(root / "store") as repository:
        assert repository.summary("001").logical_completed_tasks == 400
        assert len(list(repository.iter_requests("001"))) == 80
        attempts = list(repository.iter_attempts("001"))
        assert (
            len(attempts) == 181
            and max(attempt.attempt_number for attempt in attempts) == 3
        )
        submissions = list(repository.iter_submissions("001"))
        assert (
            len(submissions) == 181
            and len({s.submission_id for s in submissions}) == 181
        )
        for submission in submissions:
            acceptance = adapter.ledger.get(submission.submission_id)
            assert (
                acceptance is not None
                and acceptance.acceptance_count == 1
                and acceptance.accepted
            )
        originals = json.loads(
            (root / "original-results.json").read_text(encoding="utf-8")
        )
        assert [
            record.model_dump(mode="json")
            for record in repository.iter_results("001", high_water_mark=400)
        ] == originals
    return {"new_submissions": 0, "accepted_physical_requests": 181, "result_rows": 503}


def history(root: Path) -> dict[str, Any]:
    configuration, store = fixture(root, 20)
    spec = load_run_spec(configuration)
    with open_prepared_run("001", store=store, spec=spec) as prepared:
        chosen = next(prepared.iter_request_dependencies()).request
    partial = MockScenario(
        probabilities=True,
        reverse_members=True,
        members={
            "002": MockMemberScenario(outcome="invalid_category"),
            "003": MockMemberScenario(outcome="missing"),
            "004": MockMemberScenario(outcome="duplicate"),
        },
    )
    adapter = MockAdapter(
        root / "service.sqlite3",
        scenario=MockScenario(probabilities=True),
        scenarios={chosen.request_uid: partial},
    )
    report = asyncio.run(execute_run("001", store=store, adapter=adapter, spec=spec))
    assert report.summary.results == 400 and report.summary.unsuccessful_results == 3
    with Repository(store) as repository:
        original = [
            record.model_dump(mode="json") for record in repository.iter_results("001")
        ]
        failed = tuple(
            record.task_id
            for record in repository.iter_results("001")
            if record.status != "complete"
        )
        parents = [
            (
                request.request_uid,
                repository.request_task_ids("001", request.request_uid)[0],
            )
            for request in repository.iter_requests("001")
            if request.request_uid != chosen.request_uid
        ][:50]
    document(root / "original-results.json", original)
    snapshots: list[dict[str, Any]] = [
        {"label": "initial", "export_files": exports(store)}
    ]
    good = MockAdapter(
        root / "service.sqlite3", scenario=MockScenario(probabilities=True)
    )
    authorize(
        store,
        configuration,
        good,
        [(chosen.request_uid, failed, "retry-failed-three")],
        allow_completed=False,
    )
    report = asyncio.run(recover_run("001", store=store, adapter=good, spec=spec))
    assert (
        report.summary.results == 403 and report.summary.logical_completed_tasks == 400
    )
    snapshots.append({"label": "subset", "export_files": exports(store)})
    for wave in range(2):
        authorize(
            store,
            configuration,
            good,
            [
                (parent, (task,), f"repeat-{wave:02d}-{index:03d}")
                for index, (parent, task) in enumerate(parents)
            ],
            allow_completed=True,
        )
        asyncio.run(recover_run("001", store=store, adapter=good, spec=spec))
    final = exports(store)
    for snapshot in snapshots:
        previous = snapshot["export_files"]
        again = exports(store, high_water_mark=previous[0]["result_high_water_mark"])
        assert [(r["export_id"], r["sha256"], r["rows"]) for r in again] == [
            (r["export_id"], r["sha256"], r["rows"]) for r in previous
        ]
        assert all(digest(Path(r["path"])) == r["sha256"] for r in previous)
    child = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--case",
            "history",
            "--phase",
            "verify",
            "--output",
            str(root),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    assert not child.stderr, child.stderr
    verified = json.loads(child.stdout)
    assert all(digest(Path(row["path"])) == row["sha256"] for row in final)
    return {
        "logical_task_count": 400,
        "planned_physical_requests": 80,
        "accepted_physical_requests": 181,
        "result_rows": 503,
        "unsuccessful_rows": 3,
        "snapshots": snapshots,
        "export_files": final,
        "independent_process_verification": verified,
    }


def status(root: Path, inspect: bool) -> dict[str, Any]:
    configuration, store = fixture(root, 100)
    adapter = MockAdapter(
        root / "service.sqlite3", scenario=MockScenario(probabilities=True)
    )
    stopped = threading.Event()
    samples: list[float] = []
    failures: list[str] = []

    def reader() -> None:
        while not stopped.is_set():
            started = time.perf_counter()
            try:
                observed = get_run("001", store=store)
                assert observed.planned_tasks == 2000
                samples.append(time.perf_counter() - started)
            except BaseException as error:
                failures.append(type(error).__name__)
                return
            stopped.wait(max(0, 1 - (time.perf_counter() - started)))

    thread = threading.Thread(target=reader)
    if inspect:
        thread.start()
    started, cpu = time.perf_counter(), time.process_time()
    try:
        result = asyncio.run(
            execute_run(
                "001", store=store, adapter=adapter, spec=load_run_spec(configuration)
            )
        )
    finally:
        elapsed, cpu_seconds = time.perf_counter() - started, time.process_time() - cpu
        stopped.set()
        if inspect:
            thread.join(timeout=10)
            assert not thread.is_alive()
    assert not failures, failures
    assert result.summary.results == 2000 and adapter.ledger.acceptance_count() == 400
    return {
        "logical_task_count": 2000,
        "accepted_physical_requests": 400,
        "status_interval_seconds": 1 if inspect else None,
        "status_reads": len(samples),
        "status_latency_seconds": samples,
        "wall_seconds": elapsed,
        "cpu_seconds": cpu_seconds,
    }


def wal(root: Path) -> dict[str, Any]:
    configuration, store = fixture(root, 30)
    adapter = MockAdapter(
        root / "service.sqlite3", scenario=MockScenario(probabilities=True)
    )
    held = sqlite3.connect(
        (store / "registry.sqlite3").as_uri() + "?mode=ro",
        uri=True,
        isolation_level=None,
        check_same_thread=False,
    )
    held.execute("BEGIN")
    held.execute("SELECT COUNT(*) FROM results").fetchone()
    completed = 0
    maximum = 0
    measurements: dict[str, Any] = {}

    def fault(stage: str, _request: str) -> None:
        nonlocal completed, maximum
        if stage != "after_result":
            return
        completed += 1
        path = store / "registry.sqlite3-wal"
        maximum = max(maximum, path.stat().st_size if path.exists() else 0)
        if completed != 100:
            return
        with closing(sqlite3.connect(store / "registry.sqlite3", timeout=1)) as probe:
            before = tuple(probe.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone())
            assert before[1] > before[2], before
            held.execute("ROLLBACK")
            held.close()
            started = time.monotonic()
            while True:
                after = tuple(
                    probe.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
                )
                if after[0] == 0 and after[1] == after[2]:
                    break
                assert time.monotonic() - started < 30, after
                time.sleep(0.05)
            measurements.update(
                held_reader_checkpoint=before,
                released_reader_checkpoint=after,
                catchup_seconds=time.monotonic() - started,
                held_completed_requests=completed,
            )

    try:
        result = asyncio.run(
            execute_run(
                "001",
                store=store,
                adapter=adapter,
                spec=load_run_spec(configuration),
                fault=fault,
            )
        )
    finally:
        held.close()
    assert (
        measurements
        and result.summary.results == 600
        and adapter.ledger.acceptance_count() == 120
    )
    return {
        "logical_task_count": 600,
        "accepted_physical_requests": 120,
        "registry_wal_observed_high_water_bytes": maximum,
        "writer_paused_during_checkpoint_probe": True,
        **measurements,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=("history", "status", "wal"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--inspect", action="store_true", help="Status case: read status at 1 Hz"
    )
    parser.add_argument("--phase", choices=("run", "verify"), default="run")
    options = parser.parse_args()
    root = options.output.resolve()
    if options.phase == "verify":
        if options.case != "history":
            parser.error("verify phase applies only to history")
        print(json.dumps(verify_history(root)))
        return
    if root.exists():
        parser.error("output directory must be fresh")
    from execution_worker import native_peak_bytes

    started = time.time()
    wall_started, cpu_started = time.perf_counter(), time.process_time()
    result: dict[str, Any] = {}
    failure: BaseException | None = None
    try:
        result = (
            history(root)
            if options.case == "history"
            else wal(root)
            if options.case == "wal"
            else status(root, options.inspect)
        )
    except BaseException as error:
        failure = error
        result["failure_type"] = type(error).__name__
    total_wall = time.perf_counter() - wall_started
    process_cpu = time.process_time() - cpu_started
    native_peak = native_peak_bytes()
    source = Path(__file__).resolve().parents[1]
    result.update(
        case=options.case,
        runtime_identity=runtime_identity(),
        source_tree_sha256=source_fingerprint(source),
        tool_sha256=digest(Path(__file__)),
        r_tool_sha256=digest(Path(__file__).with_name("scale_integrity.R")),
        configuration_sha256=digest(root / "run.json")
        if (root / "run.json").exists()
        else None,
        input_sha256=digest(root / "responses.jsonl")
        if (root / "responses.jsonl").exists()
        else None,
        host_identity=host_identity(source),
        total_wall_seconds=total_wall,
        process_cpu_seconds=process_cpu,
        native_peak_bytes=native_peak,
        started_unix=started,
        finished_unix=time.time(),
        exit_status=1 if failure else 0,
    )
    if root.exists():
        document(root / "diagnostic.json", result)
    print(json.dumps(result))
    if failure is not None:
        raise failure


if __name__ == "__main__":
    main()
