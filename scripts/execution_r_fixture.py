"""Execute synthetic assessments, then independently reopen and export for R.

Both phases are offline and credential-free. This fixture exercises internal
execution; the separate seventh gate covers the installed public lifecycle CLI.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Literal

from judgmentllm.config import load_run_spec
from judgmentllm.execution import execute_run, recover_run
from judgmentllm.io import export_results_csv, write_results_jsonl
from judgmentllm.persistence.hashing import semantic_digest
from judgmentllm.persistence.repository import Repository
from judgmentllm.preparation import prepare_run
from judgmentllm.providers.mock import MockAdapter, MockScenario
from judgmentllm.r_exports import export_pairwise_r_csv, export_rubric_r_csv
from judgmentllm.results import PairwiseResult, RubricResult

RUNS: tuple[
    tuple[str, Literal["full", "minimal"], Literal["rubric", "pairwise"]], ...
] = (
    ("001", "full", "rubric"),
    ("002", "minimal", "rubric"),
    ("003", "full", "pairwise"),
    ("004", "minimal", "pairwise"),
)


def write_document(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def sources(
    directory: Path,
    capture: Literal["full", "minimal"],
    kind: Literal["rubric", "pairwise"],
) -> Path:
    directory.mkdir(parents=True)
    (directory / "responses.jsonl").write_text(
        "\n".join(
            json.dumps({"item_id": identifier, "text": text}, ensure_ascii=False)
            for identifier, text in (
                ("001", "Synthetic first résumé.\nSecond line."),
                ("002", "Synthetic second response."),
            )
        )
        + "\n",
        encoding="utf-8",
    )
    configuration: dict[str, object] = {
        "schema_version": 1,
        "task_kind": kind,
        "input": "responses.jsonl",
        "provider": "mock",
        "endpoint": "synthetic",
        "model": "synthetic-v1",
        "generation": {"temperature": 0, "top_p": 0.9, "max_output_tokens": 127},
        "capture": capture,
        "execution": {"concurrency": 2, "explanations": True, "cache_policy": "off"},
    }
    if kind == "rubric":
        configuration["rubric"] = {
            "rubric_id": "001",
            "name": "Synthetic nonuniform rubric",
            "traits": [
                {
                    "trait_id": "001",
                    "name": "Quality",
                    "description": "Assess the synthetic response.",
                    "levels": [
                        {"label": "low", "numeric_value": 1},
                        {"label": "middle", "numeric_value": 3},
                        {"label": "high", "numeric_value": 8},
                    ],
                }
            ],
        }
    else:
        configuration.update(
            criteria=[
                {
                    "trait_id": "001",
                    "name": "Quality",
                    "description": "Compare the synthetic responses.",
                }
            ],
            pairs="pairs.jsonl",
        )
        base = {
            "pair_id": "001",
            "pair_uid": "001",
            "trait_id": "001",
            "left_item_id": "001",
            "right_item_id": "002",
        }
        (directory / "pairs.jsonl").write_text(
            "\n".join(
                json.dumps(record)
                for record in (
                    base,
                    {
                        **base,
                        "pair_uid": "002",
                        "presented_order": ["002", "001"],
                        "source_pair_uid": "001",
                        "is_reversal": True,
                        "reversal_group": "001",
                    },
                )
            )
            + "\n",
            encoding="utf-8",
        )
    path = directory / "run.json"
    write_document(path, configuration)
    return path


def adapter(output: Path) -> MockAdapter:
    return MockAdapter(
        output / "mock-ledger.sqlite3",
        scenario=MockScenario(category_index=0, probabilities=True),
    )


def result_digests(repository: Repository) -> dict[str, list[str]]:
    return {
        run_id: [
            semantic_digest("execution-r-result", row)
            for row in repository.iter_results(run_id)
        ]
        for run_id, _, _ in RUNS
    }


async def persist(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    provider = adapter(output)
    for run_id, capture, kind in RUNS:
        configuration = sources(output / "inputs" / run_id, capture, kind)
        spec = load_run_spec(configuration)
        with prepare_run(
            configuration,
            store=output / "store",
            run_id=run_id,
            capabilities=provider.capabilities(spec),
        ):
            pass
        report = await execute_run(run_id, store=output / "store", adapter=provider)
        assert not report.recovery_required
        assert report.summary.results == report.summary.successful_results == 2
    with Repository(output / "store") as repository:
        write_document(
            output / "persisted_result_digests.json", result_digests(repository)
        )
    assert provider.ledger.acceptance_count() == 8


async def export(output: Path) -> None:
    provider = adapter(output)
    assert provider.ledger.acceptance_count() == 8
    for run_id, _, _ in RUNS:
        report = await recover_run(run_id, store=output / "store", adapter=provider)
        assert not report.recovery_required
        assert report.summary.results == 2
    with Repository(output / "store") as repository:
        assert result_digests(repository) == json.loads(
            (output / "persisted_result_digests.json").read_text(encoding="utf-8")
        )
        rubric: list[RubricResult] = []
        pairs: list[PairwiseResult] = []
        rows: list[dict[str, object]] = []
        for run_id, capture, kind in RUNS:
            results = list(repository.iter_results(run_id))
            rubric.extend(row for row in results if isinstance(row, RubricResult))
            pairs.extend(row for row in results if isinstance(row, PairwiseResult))
            submissions = list(repository.iter_submissions(run_id))
            observations = [
                repository.get_observation_for_submission(
                    run_id, submission.submission_id
                )
                for submission in submissions
            ]
            assert all(observation is not None for observation in observations)
            acceptances = [
                provider.ledger.get(submission.submission_id)
                for submission in submissions
            ]
            assert all(
                acceptance is not None and acceptance.acceptance_count == 1
                for acceptance in acceptances
            )
            rows.append(
                {
                    "schema_version": 1,
                    "run_id": run_id,
                    "capture": capture,
                    "task_kind": kind,
                    "run_directory": str(repository.run_directory(run_id).resolve()),
                    "run": repository.get_run(run_id).model_dump(mode="json"),
                    "requests": [
                        row.model_dump(mode="json")
                        for row in repository.iter_requests(run_id)
                    ],
                    "attempts": [
                        row.model_dump(mode="json")
                        for row in repository.iter_attempts(run_id)
                    ],
                    "submissions": [row.model_dump(mode="json") for row in submissions],
                    "observations": [
                        row.model_dump(mode="json")
                        for row in observations
                        if row is not None
                    ],
                    "acceptances": [
                        row.model_dump(mode="json")
                        for row in acceptances
                        if row is not None
                    ],
                }
            )
        for name, records in (("rubric", rubric), ("pairwise", pairs)):
            write_results_jsonl(output / f"{name}.jsonl", records)
            export_results_csv(output / f"{name}.csv", records)
        export_rubric_r_csv(output / "rubric_r.csv", rubric)
        export_pairwise_r_csv(output / "pairwise_r.csv", pairs)
        (output / "execution.jsonl").write_text(
            "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8"
        )
    assert provider.ledger.acceptance_count() == 8


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
