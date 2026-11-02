"""Synthetic assessment preparation; never submits requests or requires keys."""

from pathlib import Path

from judgmentllm import (
    iter_requests,
    load_run_spec,
    normalize_pairwise_result,
    normalize_rubric_result,
    prepare_pairs,
    preview_prompt,
)
from judgmentllm.io import read_pairs
from judgmentllm.results import CategoryProbability


def main() -> None:
    """Demonstrate rubric preparation, pair schedules, and pure normalization."""
    examples = Path(__file__).resolve().parent
    rubric = load_run_spec(examples / "run.yaml")
    request = next(iter_requests(rubric))
    assert request.response is not None and rubric.rubric is not None
    trait = rubric.rubric.traits[1]
    distribution = [
        CategoryProbability(level_label=level.label, probability=probability)
        for level, probability in zip(trait.levels, (0.1, 0.25, 0.65), strict=True)
    ]
    result = normalize_rubric_result(
        trait,
        run_id="offline",
        task_id="evidence-demo",
        request_uid=request.request_uid,
        attempt_id="attempt-1",
        item_id=request.response.item_id,
        probabilities=distribution,
        options=rubric.normalization,
    )
    print(
        f"Mapped expected score: {result.expected_value}; provider selection: {result.category_label}; mode: {result.modal_category_label}"
    )
    pairwise = load_run_spec(examples / "pairwise.yaml")
    assert pairwise.pairs is not None
    source = next(read_pairs(pairwise.pairs))
    scheduled = list(
        prepare_pairs(
            [source], repetitions=2, include_reversals=True, seed=pairwise.seed
        )
    )
    print(
        f"Pairwise occurrences: {len(scheduled)}; unique IDs: {len({pair.pair_uid for pair in scheduled})}"
    )
    pair_result = normalize_pairwise_result(
        scheduled[0],
        run_id="offline",
        task_id="pair-demo",
        request_uid="request-demo",
        attempt_id="attempt-1",
        winner_id=source.left_item_id,
    )
    print(f"Synthetic pair winner: {pair_result.winner_id}")
    print(preview_prompt(rubric, item_id="essay_001", trait_id="organization").text)
    print(preview_prompt(pairwise, pair_uid="comparison_001_original").text)


if __name__ == "__main__":
    main()
