"""Partial rubric interpretation using the unchanged strict trait validator."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

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


@dataclass(frozen=True)
class InterpretedResponse:
    """Bounded outcomes and content-free diagnostics for one physical response."""

    results: tuple[Result, ...]
    recognizable: bool = True
    unexpected_members: int = 0
    unattributable_members: int = 0


def member_dependency(dependency: RequestDependency, index: int) -> RequestDependency:
    """Select a member in memory while preserving its original logical identity."""
    request = dependency.request
    task = request.tasks[index]
    return dependency.model_copy(
        update={
            "request": request.model_copy(
                update={
                    "tasks": (task,),
                    "trait_ids": (task.trait_id,),
                }
            )
        }
    )


def interpret(
    parsed: ParsedResponse | None,
    spec: RunSpec,
    dependency: RequestDependency,
    run_id: str,
    attempt_id: str,
    *,
    member_checkpoint: Callable[[], None] | None = None,
) -> InterpretedResponse:
    """Match exact trait IDs and salvage independently valid rubric members.

    This intentionally does not relax whole-group validation. Every attributable
    member passes that validator with exactly its own requested trait. Unknown
    IDs and unattributable entries yield counts, never persisted provider text.
    """
    tasks = dependency.request.tasks
    if dependency.request.pair is not None:
        try:
            result = (
                normalize(parsed, spec, dependency, run_id, attempt_id)
                if parsed is not None
                else None
            )
        except ValueError:
            result = None
        if member_checkpoint is not None:
            member_checkpoint()
        return InterpretedResponse(
            (
                result
                or unsuccessful(
                    spec, dependency, run_id, attempt_id, "invalid", "invalid_output"
                ),
            ),
            recognizable=parsed is not None,
        )
    output = parsed.output if parsed is not None else None
    recognizable = (
        isinstance(output, dict)
        and set(output) == {"scores"}
        and isinstance(output["scores"], list)
    )
    expected = {task.trait_id for task in tasks}
    members: dict[str, list[object]] = {}
    unexpected = unattributable = 0
    if recognizable:
        assert output is not None
        scores = output["scores"]
        assert isinstance(scores, list)
        for entry in scores:
            if not isinstance(entry, dict) or not isinstance(
                entry.get("trait_id"), str
            ):
                unattributable += 1
                continue
            trait_id = entry["trait_id"]
            assert isinstance(trait_id, str)
            if trait_id not in expected:
                unexpected += 1
                continue
            attributable = members.setdefault(trait_id, [])
            if len(attributable) < 2:
                attributable.append(entry)
    results: list[Result] = []
    for index, task in enumerate(tasks):
        selected = member_dependency(dependency, index)
        result = None
        status, error = "invalid", "invalid_output"
        if parsed is not None and parsed.kind == "confirmed_cancellation":
            status, error = "cancelled", "confirmed_cancellation"
        elif parsed is not None and parsed.kind in {"refusal", "provider_error"}:
            status, error = (
                ("refused", "provider_refusal")
                if parsed.kind == "refusal"
                else ("errored", "provider_error")
            )
        elif recognizable:
            entries = members.get(task.trait_id, [])
            if not entries:
                error = "missing_output"
            elif len(entries) > 1:
                error = "duplicate_output"
            else:
                probabilities = (
                    parsed.member_probabilities.get(task.trait_id)
                    if parsed is not None and parsed.member_probabilities is not None
                    else parsed.rubric_probabilities
                    if parsed is not None and len(tasks) == 1
                    else None
                )
                expected_index = (
                    parsed.member_expected_indices.get(task.trait_id)
                    if parsed is not None and parsed.member_expected_indices is not None
                    else parsed.provider_expected_index
                    if parsed is not None and len(tasks) == 1
                    else None
                )
                try:
                    member = ParsedResponse.model_validate(
                        {
                            "kind": "response",
                            "output": {"scores": entries},
                            "rubric_probabilities": probabilities,
                            "provider_expected_index": expected_index,
                        }
                    )
                    result = normalize(member, spec, selected, run_id, attempt_id)
                except ValueError:
                    pass
        results.append(
            result or unsuccessful(spec, selected, run_id, attempt_id, status, error)
        )
        if member_checkpoint is not None:
            member_checkpoint()
    return InterpretedResponse(tuple(results), recognizable, unexpected, unattributable)


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
    if parsed.kind == "confirmed_cancellation":
        return unsuccessful(
            spec, dependency, run_id, attempt_id, "cancelled", "confirmed_cancellation"
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
