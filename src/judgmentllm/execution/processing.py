"""Partial rubric interpretation using the unchanged strict trait validator."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import cast

from judgmentllm.config import RunSpec
from judgmentllm.output_contracts import (
    AssessmentOutputContract,
    PairwiseOutputContract,
    RubricOutputContract,
    _checked_contract,  # pyright: ignore[reportPrivateUsage]
    _checked_output,  # pyright: ignore[reportPrivateUsage]
    validate_pairwise_contract_output,
    validate_rubric_contract_output,
)
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


def _request_contract(
    contract: AssessmentOutputContract,
    spec: RunSpec,
    dependency: RequestDependency,
) -> AssessmentOutputContract:
    """Check explicit interpretation settings against the original request."""
    contract = _checked_contract(contract)
    if contract.task_kind != spec.task_kind:
        raise ValueError("output contract assessment kind must match the run")
    if contract.explanations != spec.execution.explanations:
        raise ValueError("output contract explanations must match the run")
    request = dependency.request
    if isinstance(contract, PairwiseOutputContract):
        if contract.allow_ties != spec.allow_ties:
            raise ValueError("output contract tie policy must match the run")
        if request.pair != contract.pair:
            raise ValueError("output contract pair must match the presented request")
        if (
            len(request.tasks) != 1
            or request.trait_ids != (contract.pair.trait_id,)
            or request.tasks[0].trait_id != contract.pair.trait_id
            or request.tasks[0].pair_uid != contract.pair.pair_uid
            or request.tasks[0].pair_id != contract.pair.pair_id
        ):
            raise ValueError(
                "pairwise request membership must match the output contract"
            )
    else:
        if request.pair is not None or spec.rubric is None:
            raise ValueError("rubric output contract requires a rubric request")
        if spec.allow_ties:
            raise ValueError("tie policy is only valid for pairwise assessment")
        trait_ids = tuple(trait.trait_id for trait in contract.traits)
        if (
            request.trait_ids != trait_ids
            or tuple(task.trait_id for task in request.tasks) != trait_ids
        ):
            raise ValueError(
                "output contract traits must match ordered request membership"
            )
        traits = {trait.trait_id: trait for trait in spec.rubric.traits}
        if any(traits.get(trait.trait_id) != trait for trait in contract.traits):
            raise ValueError("output contract traits must match the run rubric")
    return contract


def _index_members(
    entries: Sequence[object], expected: set[str]
) -> tuple[dict[str, list[object]], int, int]:
    """Keep only bounded attributable evidence and content-free extra counts."""
    members: dict[str, list[object]] = {}
    unexpected = unattributable = 0
    for entry in entries:
        if not isinstance(entry, dict):
            unattributable += 1
            continue
        fields = cast(dict[str, object], entry)
        trait_id = fields.get("trait_id")
        if not isinstance(trait_id, str):
            unattributable += 1
            continue
        if trait_id not in expected:
            unexpected += 1
            continue
        attributable = members.setdefault(trait_id, [])
        if len(attributable) < 2:
            attributable.append(fields)
    return members, unexpected, unattributable


def interpret(
    parsed: ParsedResponse | None,
    spec: RunSpec,
    dependency: RequestDependency,
    run_id: str,
    attempt_id: str,
    *,
    member_checkpoint: Callable[[], None] | None = None,
    contract: AssessmentOutputContract | None = None,
) -> InterpretedResponse:
    """Match exact trait IDs and salvage independently valid rubric members.

    This intentionally does not relax whole-group validation. Every attributable
    member passes that validator with exactly its own requested trait. Unknown
    IDs and unattributable entries yield counts, never persisted provider text.

    An explicit version-2 output contract enables detached interpretation only;
    the executor continues using its committed version-1 contract. Parsed SDK
    objects cannot establish whether the original JSON contained duplicate keys.
    """
    if contract is not None:
        contract = _request_contract(contract, spec, dependency)
        if parsed is not None and parsed.kind == "response":
            try:
                _checked_output(parsed.output)
            except ValueError:
                parsed = None
    tasks = dependency.request.tasks
    if dependency.request.pair is not None:
        try:
            result = (
                normalize(
                    parsed, spec, dependency, run_id, attempt_id, contract=contract
                )
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
    separated = (
        isinstance(contract, RubricOutputContract)
        and contract.output_order != "together"
    )
    required_fields = {"scores", "explanations"} if separated else {"scores"}
    recognizable = (
        isinstance(output, dict)
        and set(output) == required_fields
        and all(isinstance(output[field], list) for field in required_fields)
    )
    expected = {task.trait_id for task in tasks}
    members: dict[str, list[object]] = {}
    explanation_members: dict[str, list[object]] = {}
    unexpected = unattributable = 0
    if recognizable:
        assert output is not None
        scores = output["scores"]
        assert isinstance(scores, list)
        members, unexpected, unattributable = _index_members(scores, expected)
        if separated:
            explanations = output["explanations"]
            assert isinstance(explanations, list)
            explanation_members, extra_unexpected, extra_unattributable = (
                _index_members(explanations, expected)
            )
            unexpected += extra_unexpected
            unattributable += extra_unattributable
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
            explanation_entries = explanation_members.get(task.trait_id, [])
            if len(entries) > 1 or len(explanation_entries) > 1:
                error = "duplicate_output"
            elif not entries or (separated and not explanation_entries):
                error = "missing_output"
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
                member_output: dict[str, object] = {"scores": entries}
                if separated:
                    member_output["explanations"] = explanation_entries
                member_contract = (
                    contract.model_copy(update={"traits": (contract.traits[index],)})
                    if isinstance(contract, RubricOutputContract)
                    else None
                )
                try:
                    member = ParsedResponse.model_validate(
                        {
                            "kind": "response",
                            "output": member_output,
                            "rubric_probabilities": probabilities,
                            "provider_expected_index": expected_index,
                        }
                    )
                    result = normalize(
                        member,
                        spec,
                        selected,
                        run_id,
                        attempt_id,
                        contract=member_contract,
                    )
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
    *,
    contract: AssessmentOutputContract | None = None,
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
        output = (
            validate_pairwise_contract_output(parsed.output, contract)
            if isinstance(contract, PairwiseOutputContract)
            else validate_pairwise_output(
                parsed.output,
                pair,
                allow_ties=spec.allow_ties,
                explanations=spec.execution.explanations,
            )
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
    output = (
        validate_rubric_contract_output(parsed.output, contract)
        if isinstance(contract, RubricOutputContract)
        else validate_categorical_output(
            parsed.output, [trait], explanations=spec.execution.explanations
        )
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
