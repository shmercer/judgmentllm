"""Independent-process grouped outcomes, explicit subset retries and exports for R."""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
from pathlib import Path
from typing import Literal

from judgmentllm.config import load_run_spec
from judgmentllm.execution import execute_run, recover_run
from judgmentllm.execution.exports import export_run
from judgmentllm.execution.retry import prepare_subset_retry
from judgmentllm.persistence.hashing import semantic_digest
from judgmentllm.persistence.ownership import ExecutorOwnership
from judgmentllm.persistence.repository import Repository
from judgmentllm.preparation import open_prepared_run, prepare_run
from judgmentllm.providers import MockAdapter, MockMemberScenario, MockScenario

RUNS: tuple[tuple[str, Literal["full", "minimal"]], ...] = (
    ("001", "full"),
    ("002", "minimal"),
)


def document(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def sources(root: Path, capture: Literal["full", "minimal"]) -> Path:
    root.mkdir(parents=True)
    (root / "responses.jsonl").write_text(
        '{"item_id":"001","text":"Synthetic grouped résumé."}\n', encoding="utf-8"
    )
    path = root / "run.json"
    document(
        path,
        {
            "task_kind": "rubric",
            "input": "responses.jsonl",
            "provider": "mock",
            "endpoint": "synthetic",
            "model": "synthetic-v1",
            "capture": capture,
            "execution": {
                "grouping": "grouped",
                "concurrency": 1,
                "explanations": True,
            },
            "rubric": {
                "rubric_id": "001",
                "name": "Synthetic grouped rubric",
                "traits": [
                    {
                        "trait_id": identifier,
                        "name": "Quality",
                        "description": "Assess.",
                        "levels": [
                            {"label": "low", "numeric_value": 1},
                            {"label": "middle", "numeric_value": 3},
                            {"label": "high", "numeric_value": 8},
                        ],
                    }
                    for identifier in ("001", "002", "003")
                ],
            },
        },
    )
    return path


def adapter(root: Path, *, partial: bool = False) -> MockAdapter:
    return MockAdapter(
        root / "acceptances.sqlite3",
        scenario=MockScenario(
            members={"003": MockMemberScenario(outcome="invalid_category")}
            if partial
            else {},
            probabilities=True,
            category_index=0,
            reverse_members=True,
            explanation="Authorized synthetic explanation.",
        ),
    )


def digests(repository: Repository, run_id: str) -> list[str]:
    return [
        semantic_digest("grouped-r-result", record)
        for record in repository.iter_results(run_id)
    ]


async def persist(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    store = root / "store"
    initial, retry = adapter(root, partial=True), adapter(root)
    for run_id, capture in RUNS:
        spec_path = sources(root / "inputs" / run_id, capture)
        spec = load_run_spec(spec_path)
        with prepare_run(
            spec_path,
            store=store,
            run_id=run_id,
            capabilities=initial.capabilities(spec),
        ):
            pass
        report = await execute_run(run_id, store=store, adapter=initial)
        assert report.summary.results == 3 and report.summary.successful_results == 2
        before = export_run(run_id, store=store)
        assert before.row_count == 3 and not before.run_complete
        document(root / f"{run_id}-before.json", before.model_dump(mode="json"))
        with (
            Repository(store) as repository,
            ExecutorOwnership(repository, run_id) as ownership,
        ):
            assert ownership.owner is not None
            token = ownership.owner.token
            with open_prepared_run(
                run_id,
                store=store,
                capabilities=retry.capabilities(spec),
                owner_token=token,
            ) as prepared:
                parent = next(prepared.iter_request_dependencies())
                unsuccessful = tuple(
                    record.task_id
                    for record in prepared.repository.iter_results(run_id)
                    if record.status != "complete"
                )
                assert len(unsuccessful) == 1
                attempt = prepare_subset_retry(
                    prepared,
                    retry,
                    dependency=parent,
                    task_ids=unsuccessful,
                    operation_id="retry-001",
                    owner_token=token,
                )
                repeated = prepare_subset_retry(
                    prepared,
                    retry,
                    dependency=parent,
                    task_ids=unsuccessful,
                    operation_id="retry-001",
                    owner_token=token,
                )
                assert repeated == attempt
        report = await recover_run(run_id, store=store, adapter=retry)
        assert report.summary.results == 4 and not report.recovery_required
        assert report.summary.state == "completed"
        with Repository(store) as repository:
            document(root / f"{run_id}-digests.json", digests(repository, run_id))
    assert initial.ledger.acceptance_count() == 4


async def export(root: Path) -> None:
    store = root / "store"
    provider = adapter(root)
    evidence: list[dict[str, object]] = []
    for run_id, capture in RUNS:
        await recover_run(run_id, store=store, adapter=provider)
        with Repository(store) as repository:
            assert digests(repository, run_id) == json.loads(
                (root / f"{run_id}-digests.json").read_text(encoding="utf-8")
            )
            requests = list(repository.iter_requests(run_id))
            attempts = list(repository.iter_attempts(run_id))
            submissions = list(repository.iter_submissions(run_id))
            assert len(requests) == 1 and len(attempts) == len(submissions) == 2
            assert (
                len(repository.request_task_ids(run_id, requests[0].request_uid)) == 3
            )
            assert [
                len(repository.attempt_task_ids(run_id, attempt.attempt_id))
                for attempt in attempts
            ] == [3, 1]
            row: dict[str, object] = {
                "run_id": run_id,
                "capture": capture,
                "requests": [
                    {
                        **request.model_dump(mode="json"),
                        "task_ids": repository.request_task_ids(
                            run_id, request.request_uid
                        ),
                    }
                    for request in requests
                ],
                "attempts": [
                    {
                        **attempt.model_dump(mode="json"),
                        "task_ids": repository.attempt_task_ids(
                            run_id, attempt.attempt_id
                        ),
                    }
                    for attempt in attempts
                ],
                "submissions": [
                    submission.model_dump(mode="json") for submission in submissions
                ],
                "observations": [
                    observation.model_dump(mode="json")
                    for submission in submissions
                    if (
                        observation := repository.get_observation_for_submission(
                            run_id, submission.submission_id
                        )
                    )
                    is not None
                ],
                "acceptances": [
                    acceptance.model_dump(mode="json")
                    for submission in submissions
                    if (acceptance := provider.ledger.get(submission.submission_id))
                    is not None
                ],
                "result_links": [
                    dict(record)
                    for record in repository.connection.execute(
                        "SELECT result_seq,task_id,request_uid,attempt_id,observation_id,status FROM results WHERE run_id=? ORDER BY result_seq",
                        (run_id,),
                    )
                ],
                "retries": [
                    dict(record)
                    for record in repository.connection.execute(
                        "SELECT * FROM retry_operations WHERE run_id=?", (run_id,)
                    )
                ],
            }
        reports: list[dict[str, object]] = []
        combinations: tuple[
            tuple[Literal["jsonl", "csv"], Literal["canonical", "r"], str], ...
        ] = (
            ("jsonl", "canonical", ".jsonl"),
            ("csv", "canonical", ".csv"),
            ("csv", "r", "_r.csv"),
        )
        for format, view, suffix in combinations:
            report = export_run(
                run_id, store=store, format=format, view=view, page_size=1
            )
            assert report.row_count == 4
            assert report.run_complete and report.run_state == "completed"
            shutil.copyfile(report.artifact_path, root / f"{run_id}{suffix}")
            reports.append(report.model_dump(mode="json"))
        before = json.loads(
            (root / f"{run_id}-before.json").read_text(encoding="utf-8")
        )
        original = export_run(
            run_id, store=store, high_water_mark=before["result_high_water_mark"]
        )
        assert original.export_id == before["export_id"] and original.row_count == 3
        row.update(exports=reports, prior_export=original.model_dump(mode="json"))
        evidence.append(row)
    assert provider.submit_calls == 0 and provider.ledger.acceptance_count() == 4
    (root / "grouped-lifecycle.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in evidence), encoding="utf-8"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("persist", "export"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    options = parser.parse_args()
    asyncio.run(
        persist(options.output)
        if options.phase == "persist"
        else export(options.output)
    )


if __name__ == "__main__":
    main()
