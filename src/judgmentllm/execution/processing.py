"""Single-member interpretation using the established validators and normalizers."""

from __future__ import annotations

from judgmentllm.config import RunSpec
from judgmentllm.preparation import RequestDependency
from judgmentllm.providers import ParsedResponse
from judgmentllm.results import (
    PairwiseResult,
    RubricResult,
    normalize_pairwise_result,
    normalize_rubric_result,
    validate_categorical_output,
    validate_pairwise_output,
)

Result = RubricResult | PairwiseResult


def unsuccessful(
    spec: RunSpec,
    dependency: RequestDependency,
    run_id: str,
    attempt_id: str,
    status: str,
    error_kind: str,
) -> Result:
    """Represent established unsuccessful evidence, never a preparation failure."""
    request = dependency.request
    task = request.tasks[0]
    common = dict(
        run_id=run_id,
        task_id=task.task_id,
        request_uid=request.request_uid,
        attempt_id=attempt_id,
        status=status,
        error_kind=error_kind,
        provider=spec.provider,
        endpoint=spec.endpoint,
        model=spec.model,
    )
    if request.pair is not None:
        pair = request.pair
        return PairwiseResult.model_validate(
            dict(
                common,
                pair_id=pair.pair_id,
                pair_uid=pair.pair_uid,
                trait_id=pair.trait_id,
                left_item_id=pair.left_item_id,
                right_item_id=pair.right_item_id,
                original_order=pair.original_order,
                presented_order=pair.presented_order,
                source_pair_uid=pair.source_pair_uid,
                repetition_index=pair.repetition_index,
                is_reversal=pair.is_reversal,
                reversal_group=pair.reversal_group,
                allow_ties=spec.allow_ties,
            )
        )
    assert spec.rubric is not None and task.item_id is not None
    trait = next(t for t in spec.rubric.traits if t.trait_id == task.trait_id)
    return RubricResult.model_validate(
        dict(
            common,
            item_id=task.item_id,
            trait_id=trait.trait_id,
            resolved_levels=trait.resolved_levels,
        )
    )


def normalize(
    parsed: ParsedResponse,
    spec: RunSpec,
    dependency: RequestDependency,
    run_id: str,
    attempt_id: str,
) -> Result:
    """Only received provider evidence reaches this function."""
    if parsed.kind == "refusal":
        return unsuccessful(
            spec, dependency, run_id, attempt_id, "refused", "provider_refusal"
        )
    if parsed.kind == "provider_error":
        return unsuccessful(
            spec, dependency, run_id, attempt_id, "errored", "provider_error"
        )
    task = dependency.request.tasks[0]
    pair = dependency.request.pair
    if pair is not None:
        output = validate_pairwise_output(
            parsed.output,
            pair,
            allow_ties=spec.allow_ties,
            explanations=spec.execution.explanations,
        )
        return normalize_pairwise_result(
            pair,
            run_id=run_id,
            task_id=task.task_id,
            request_uid=dependency.request.request_uid,
            attempt_id=attempt_id,
            winner_id=output.winner_id,
            tie=output.tie,
            p_left=parsed.p_left,
            p_right=parsed.p_right,
            p_tie=parsed.p_tie,
            allow_ties=spec.allow_ties,
            explanation=output.explanation,
            options=spec.normalization,
            provider=spec.provider,
            endpoint=spec.endpoint,
            model=spec.model,
        )
    assert spec.rubric is not None and task.item_id is not None
    trait = next(t for t in spec.rubric.traits if t.trait_id == task.trait_id)
    output = validate_categorical_output(
        parsed.output, [trait], explanations=spec.execution.explanations
    )
    return normalize_rubric_result(
        trait,
        run_id=run_id,
        task_id=task.task_id,
        request_uid=dependency.request.request_uid,
        attempt_id=attempt_id,
        item_id=task.item_id,
        category_label=output.scores[0].category_label,
        probabilities=parsed.rubric_probabilities,
        provider_expected_index=parsed.provider_expected_index,
        explanation=output.scores[0].explanation,
        options=spec.normalization,
        provider=spec.provider,
        endpoint=spec.endpoint,
        model=spec.model,
    )
