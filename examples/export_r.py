"""Export synthetic all-attempts audit data for examples/r_interoperability.R."""

from __future__ import annotations

import argparse
from pathlib import Path

from judgmentllm import (
    PairTask,
    PairwiseResult,
    RubricResult,
    Trait,
    normalize_pairwise_result,
    normalize_rubric_result,
)
from judgmentllm.io import write_results_jsonl
from judgmentllm.r_exports import export_pairwise_r_csv, export_rubric_r_csv
from judgmentllm.results import CategoryProbability


def synthetic_pairwise_results() -> list[PairwiseResult]:
    """Six audit rows: retry, reversal, tie, failure, and missing probabilities."""
    source = PairTask(
        pair_id="comparison-001",
        pair_uid="pair-001",
        left_item_id="001",
        right_item_id="002",
        trait_id="quality",
        reversal_group="audit-001",
    )

    def win(
        pair: PairTask,
        task: str,
        winner: str,
        *,
        attempt: str = "attempt-1",
        p_left: float | None = None,
        p_right: float | None = None,
    ) -> PairwiseResult:
        return normalize_pairwise_result(
            pair,
            run_id="synthetic-run",
            task_id=task,
            request_uid=f"request-{pair.pair_uid}",
            attempt_id=attempt,
            winner_id=winner,
            p_left=p_left,
            p_right=p_right,
            provider="synthetic",
            endpoint="offline",
            model="fixture",
        )

    forward = win(source, "pair-task-001", "001", p_left=0.8, p_right=0.2)
    retry = win(
        source, "pair-task-001", "002", attempt="attempt-2", p_left=0.4, p_right=0.6
    )
    reverse_pair = PairTask.model_validate(
        {
            **source.model_dump(),
            "pair_uid": "pair-002",
            "source_pair_uid": "pair-001",
            "is_reversal": True,
            "presented_order": ["002", "001"],
        }
    )
    reverse = win(reverse_pair, "pair-task-002", "001", p_left=0.7, p_right=0.3)
    tie_pair = PairTask.model_validate({**source.model_dump(), "pair_uid": "pair-003"})
    tie = normalize_pairwise_result(
        tie_pair,
        run_id="synthetic-run",
        task_id="pair-task-003",
        request_uid="request-pair-003",
        attempt_id="attempt-1",
        tie=True,
        allow_ties=True,
        p_left=0.25,
        p_right=0.25,
        p_tie=0.5,
        provider="synthetic",
        endpoint="offline",
        model="fixture",
    )
    failed_pair = PairTask.model_validate(
        {**source.model_dump(), "pair_uid": "pair-004"}
    )
    failed = PairwiseResult.model_validate(
        {
            **failed_pair.model_dump(exclude={"metadata"}),
            "run_id": "synthetic-run",
            "task_id": "pair-task-004",
            "request_uid": "request-pair-004",
            "attempt_id": "attempt-1",
            "status": "errored",
            "error_kind": "synthetic_failure",
            "error_message": "Offline synthetic failure.",
            "p_left": 0.9,
            "p_right": 0.1,
            "probability_sum_tolerance": 1e-8,
            "provider": "synthetic",
            "endpoint": "offline",
            "model": "fixture",
        }
    )
    no_probs_pair = PairTask.model_validate(
        {**source.model_dump(), "pair_uid": "pair-005"}
    )
    no_probs = win(no_probs_pair, "pair-task-005", "002")
    return [forward, retry, reverse, tie, failed, no_probs]


def synthetic_rubric_results() -> list[RubricResult]:
    """Different scales and repeated successful attempts, requiring selection."""
    organization = Trait.model_validate(
        {
            "trait_id": "organization",
            "name": "Organization",
            "description": "Synthetic organization criterion",
            "levels": [
                {"label": "Beginning"},
                {"label": "Developing"},
                {"label": "Proficient"},
                {"label": "Advanced"},
            ],
        }
    )
    evidence = Trait.model_validate(
        {
            "trait_id": "evidence",
            "name": "Evidence",
            "description": "Synthetic evidence criterion",
            "levels": [
                {"label": 1, "numeric_value": 1},
                {"label": 2, "numeric_value": 3},
                {"label": 3, "numeric_value": 8},
            ],
        }
    )
    categorical = normalize_rubric_result(
        organization,
        run_id="synthetic-run",
        task_id="rubric-task-001",
        request_uid="request-rubric-001",
        attempt_id="attempt-1",
        item_id="001",
        category_label="Proficient",
        provider="synthetic",
        model="fixture",
    )
    probabilities = [
        CategoryProbability(level_label=level.label, probability=value)
        for level, value in zip(evidence.levels, (0.1, 0.25, 0.65), strict=True)
    ]
    expected = normalize_rubric_result(
        evidence,
        run_id="synthetic-run",
        task_id="rubric-task-002",
        request_uid="request-rubric-002",
        attempt_id="attempt-1",
        item_id="001",
        category_label="2",
        probabilities=probabilities,
        provider_expected_index=1.55,
        provider="synthetic",
        model="fixture",
    )
    retry = normalize_rubric_result(
        evidence,
        run_id="synthetic-run",
        task_id="rubric-task-002",
        request_uid="request-rubric-002",
        attempt_id="attempt-2",
        item_id="001",
        category_label="3",
        provider="synthetic",
        model="fixture",
    )
    return [categorical, expected, retry]


def main() -> None:
    """Write derived R views and retain canonical normalized audit records."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Destination directory for synthetic files.",
    )
    directory: Path = parser.parse_args().output
    directory.mkdir(parents=True, exist_ok=True)
    pairwise = synthetic_pairwise_results()
    rubric = synthetic_rubric_results()
    export_pairwise_r_csv(directory / "pairwise_r.csv", iter(pairwise))
    export_rubric_r_csv(directory / "rubric_r.csv", iter(rubric))
    write_results_jsonl(
        directory / "normalized_results.jsonl", iter([*pairwise, *rubric])
    )
    print(
        f"Wrote {len(pairwise)} pairwise attempts and {len(rubric)} rubric attempts to {directory}"
    )


if __name__ == "__main__":
    main()
