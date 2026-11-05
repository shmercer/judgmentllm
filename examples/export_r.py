"""Export synthetic all-attempts audit data for examples/r_interoperability.R."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Literal

from pydantic import JsonValue

from judgmentllm import (
    PairTask,
    PairwiseResult,
    RubricResult,
    Trait,
    normalize_pairwise_result,
    normalize_rubric_result,
)
from judgmentllm.config import RunSpec
from judgmentllm.execution.processing import interpret
from judgmentllm.io import write_results_jsonl
from judgmentllm.models import ResponseItem, Rubric
from judgmentllm.output_contracts import (
    PairwiseOutputContract,
    RubricOutputContract,
    validate_pairwise_contract_output,
    validate_rubric_contract_output,
)
from judgmentllm.persistence.hashing import semantic_digest
from judgmentllm.planning import CapabilityProfile, plan_record
from judgmentllm.preparation import RequestDependency
from judgmentllm.providers import ParsedResponse
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


def assessment_contract_results() -> tuple[list[RubricResult], list[PairwiseResult]]:
    """Exercise standalone Phase 1 interpretation without submission or storage.

    The version-1 specification supplies established identities and normalization
    settings only. The explicit output contract is not a prepared v2 request.
    """
    traits = tuple(
        Trait.model_validate(
            {
                "trait_id": identifier,
                "name": "Synthetic criterion",
                "description": "Assess the synthetic response.",
                "levels": levels,
            }
        )
        for identifier, levels in (
            (
                "001",
                [
                    {"label": "low", "numeric_value": 1},
                    {"label": "middle", "numeric_value": 3},
                    {"label": "high", "numeric_value": 8},
                ],
            ),
            (
                "002",
                [
                    {"label": "1", "numeric_value": 10},
                    {"label": "2", "numeric_value": 40},
                ],
            ),
        )
    )
    rubric = Rubric(rubric_id="001", name="Contract fixture", traits=list(traits))
    spec = RunSpec.model_validate(
        {
            "task_kind": "rubric",
            "input": "unused-offline-fixture.jsonl",
            "rubric": rubric.model_dump(),
            "provider": "synthetic",
            "endpoint": "offline",
            "model": "contract-fixture",
            "execution": {"grouping": "grouped", "explanations": True},
        }
    )
    request = next(
        plan_record(
            spec,
            ResponseItem(item_id="001", text="Synthetic response."),
            capabilities=CapabilityProfile(grouped=True, explanations=True),
        )
    )
    dependency = RequestDependency(
        request=request,
        dependency_sha256=semantic_digest("offline-contract-fixture", request),
    )
    layouts: tuple[Literal["together", "scores_first", "explanations_first"], ...] = (
        "together",
        "scores_first",
        "explanations_first",
    )
    rubric_results: list[RubricResult] = []
    for layout in layouts:
        contract = RubricOutputContract(
            traits=traits, output_order=layout, explanations=True
        )
        scores: list[JsonValue] = [
            {"trait_id": "001", "category_label": "middle"},
            {"trait_id": "002", "category_label": "2"},
        ]
        explanations: list[JsonValue] = [
            {"trait_id": "002", "text": "Assessment for 002."},
            {"trait_id": "001", "text": "Assessment for 001."},
        ]
        if layout == "together":
            for score in scores:
                assert isinstance(score, dict)
                score["explanation"] = f"Assessment for {score['trait_id']}."
            output: dict[str, JsonValue] = {"scores": scores}
        elif layout == "scores_first":
            output = {"scores": scores, "explanations": explanations}
        else:
            output = {"explanations": explanations, "scores": scores}
        # Strict whole-output validation and member interpretation are distinct.
        validate_rubric_contract_output(json.dumps(output), contract)
        interpreted = interpret(
            ParsedResponse(kind="response", output=output),
            spec,
            dependency,
            f"contract-{layout}",
            "attempt-001",
            contract=contract,
        )
        assert all(result.status == "complete" for result in interpreted.results)
        for result in interpreted.results:
            assert isinstance(result, RubricResult)
            rubric_results.append(result)

    partial_contract = RubricOutputContract(
        traits=traits, output_order="scores_first", explanations=True
    )
    partial_output: dict[str, JsonValue] = {
        "scores": [
            {"trait_id": "001", "category_label": "middle"},
            {"trait_id": "002", "category_label": "2"},
        ],
        "explanations": [{"trait_id": "001", "text": "Assessment for 001."}],
    }
    try:
        validate_rubric_contract_output(json.dumps(partial_output), partial_contract)
    except ValueError:
        pass
    else:
        raise AssertionError("Incomplete grouped output passed whole validation")
    partial = interpret(
        ParsedResponse(kind="response", output=partial_output),
        spec,
        dependency,
        "contract-partial",
        "attempt-001",
        contract=partial_contract,
    )
    assert [result.status for result in partial.results] == ["complete", "invalid"]
    for result in partial.results:
        assert isinstance(result, RubricResult)
        rubric_results.append(result)

    pairwise_results: list[PairwiseResult] = []
    for case, reversed_order, allow_ties, data in (
        ("winner-only", False, False, {"winner_id": "002"}),
        ("historical-false", True, False, {"winner_id": "001", "tie": False}),
        ("tie", True, True, {"winner_id": None, "tie": True}),
        ("allowed-win", False, True, {"winner_id": "002", "tie": False}),
    ):
        pair = PairTask(
            pair_id="001",
            pair_uid=f"contract-{case}",
            source_pair_uid="001",
            left_item_id="001",
            right_item_id="002",
            trait_id="001",
            presented_order=["002", "001"] if reversed_order else ["001", "002"],
            is_reversal=reversed_order,
            repetition_index=1,
        )
        pair_contract = PairwiseOutputContract(pair=pair, allow_ties=allow_ties)
        validated = validate_pairwise_contract_output(json.dumps(data), pair_contract)
        pairwise_results.append(
            normalize_pairwise_result(
                pair,
                run_id="contract-pairwise",
                task_id=f"contract-task-{case}",
                request_uid=f"contract-request-{case}",
                attempt_id="attempt-001",
                winner_id=validated.winner_id,
                tie=validated.tie,
                allow_ties=allow_ties,
                provider="synthetic",
                endpoint="offline",
                model="contract-fixture",
            )
        )
    return rubric_results, pairwise_results


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
    contract_rubric, contract_pairwise = assessment_contract_results()
    export_rubric_r_csv(directory / "assessment_contract_rubric_r.csv", contract_rubric)
    export_pairwise_r_csv(
        directory / "assessment_contract_pairwise_r.csv", contract_pairwise
    )
    write_results_jsonl(
        directory / "assessment_contract_results.jsonl",
        iter([*contract_rubric, *contract_pairwise]),
    )
    print(
        f"Wrote {len(pairwise)} pairwise attempts and {len(rubric)} rubric attempts to {directory}"
    )


if __name__ == "__main__":
    main()
