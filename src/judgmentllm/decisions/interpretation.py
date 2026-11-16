"""Strict named-answer validation and normalization for the synthetic boundary."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, cast

from pydantic import Field

from judgmentllm.config import RunSpecV4
from judgmentllm.decisions.outcomes import AnswerAttributionV1
from judgmentllm.decisions.records import DecisionQuestionV1
from judgmentllm.decisions.responses import ParsedDecisionsResponseV1
from judgmentllm.decisions.transport import (
    DecisionsDependencyV1,
    PreparedDecisionsRequestV1,
)
from judgmentllm.execution.processing import InterpretedResponse, Result, unsuccessful
from judgmentllm.execution.retry import subset_dependency
from judgmentllm.io import _revalidate_record  # pyright: ignore[reportPrivateUsage]
from judgmentllm.models import StrictModel
from judgmentllm.persistence.models import IntegrityError
from judgmentllm.results import normalize_pairwise_result, normalize_rubric_result


@dataclass(frozen=True)
class InterpretedDecisionsResponse(InterpretedResponse):
    answers: tuple[AnswerAttributionV1, ...] = ()


class IndexedProbabilityV1(StrictModel):
    index: int = Field(ge=0)
    probability: float = Field(ge=0, le=1, allow_inf_nan=False)


class LegendEntryV1(StrictModel):
    index: int = Field(ge=0)
    label: str
    description: str


class ScoreAnswerV1(StrictModel):
    name: str
    type: Literal["score"]
    probabilities: list[IndexedProbabilityV1]
    legend: list[LegendEntryV1]
    expected_index: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    selected_index: int | None = Field(default=None, ge=0)
    confidence: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)


class ChoiceProbabilityV1(StrictModel):
    option: Literal["essay_01", "essay_02"]
    probability: float = Field(ge=0, le=1, allow_inf_nan=False)


class ChoiceAnswerV1(StrictModel):
    name: str
    type: Literal["choice"]
    probabilities: list[ChoiceProbabilityV1]
    selection: Literal["essay_01", "essay_02"]
    confidence: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)


def normalize_answer(
    answer: object,
    question: DecisionQuestionV1,
    spec: RunSpecV4,
    wire: PreparedDecisionsRequestV1,
    item_id: str | None,
) -> Result:
    identity = wire.transport_commitment.identity
    if question.trait is not None:
        checked = ScoreAnswerV1.model_validate(answer)
        levels = question.trait.levels
        if [p.index for p in checked.probabilities] != list(range(len(levels))) or [
            e.model_dump() for e in checked.legend
        ] != [
            dict(index=i, label=level.label, description=level.description)
            for i, level in enumerate(levels)
        ]:
            raise ValueError("score legend or full ordered distribution differs")
        expected = math.fsum(p.index * p.probability for p in checked.probabilities)
        if (
            checked.expected_index is not None
            and abs(expected - checked.expected_index)
            > spec.normalization.probability_sum_tolerance
        ):
            raise ValueError("native expected index conflicts with distribution")
        if checked.selected_index is not None and checked.selected_index >= len(levels):
            raise ValueError("selected native index outside scale")
        confidence = checked.confidence
        result = normalize_rubric_result(
            question.trait,
            item_id=cast(str, item_id),
            category_label=levels[checked.selected_index].label
            if checked.selected_index is not None
            else None,
            probabilities=[
                dict(level_label=level.label, probability=p.probability)
                for level, p in zip(levels, checked.probabilities, strict=True)
            ],
            provider_expected_index=checked.expected_index,
            run_id=identity.run_id,
            task_id=question.task_id,
            request_uid=identity.request_uid,
            attempt_id=identity.attempt_id,
            options=spec.normalization,
            provider=spec.provider,
            endpoint=spec.endpoint,
            model=spec.model,
        )
    else:
        assert question.pair is not None
        checked_choice = ChoiceAnswerV1.model_validate(answer)
        if [p.option for p in checked_choice.probabilities] != ["essay_01", "essay_02"]:
            raise ValueError("choice distribution must cover both aliases in order")
        confidence = checked_choice.confidence
        pair = question.pair
        presented = dict(
            zip(
                pair.presented_order,
                (p.probability for p in checked_choice.probabilities),
                strict=True,
            )
        )
        result = normalize_pairwise_result(
            pair,
            winner_id=pair.presented_order[
                0 if checked_choice.selection == "essay_01" else 1
            ],
            p_left=presented[pair.left_item_id],
            p_right=presented[pair.right_item_id],
            run_id=identity.run_id,
            task_id=question.task_id,
            request_uid=identity.request_uid,
            attempt_id=identity.attempt_id,
            options=spec.normalization,
            provider=spec.provider,
            endpoint=spec.endpoint,
            model=spec.model,
        )
    return type(result).model_validate(
        {**result.model_dump(), "confidence": confidence}
    )


def interpret_decisions(
    response: ParsedDecisionsResponseV1,
    spec: RunSpecV4,
    dependency: DecisionsDependencyV1,
    wire: PreparedDecisionsRequestV1,
    *,
    member_checkpoint: Callable[[], None] | None = None,
) -> InterpretedDecisionsResponse:
    """Only a complete, bound envelope permits independently validated salvage."""
    response = _revalidate_record(response)
    identity = wire.transport_commitment.identity
    if (
        response.identity != identity
        or response.payload_sha256 != wire.payload_sha256
        or (
            response.reported_model is not None
            and response.reported_model != wire.model
        )
    ):
        raise IntegrityError("Decisions parsed response differs from invocation")
    recognizable = response.recognizable
    members: dict[str, list[object]] = {}
    extras = unattributed = 0
    expected = {q.key: i for i, q in enumerate(dependency.questions)}
    order: list[str] = []
    ordinals: dict[str, list[int]] = {}
    if recognizable:
        for ordinal, entry in enumerate(response.answers):
            if not isinstance(entry, dict):
                unattributed += 1
                continue
            entry = cast(dict[str, object], entry)
            if not isinstance(entry.get("name"), str):
                unattributed += 1
                continue
            name = cast(str, entry["name"])
            if name not in expected:
                extras += 1
                continue
            order.append(name)
            positions = ordinals.setdefault(name, [])
            if len(positions) < 2:
                positions.append(ordinal)
            bucket = members.setdefault(name, [])
            if len(bucket) < 2:
                bucket.append(entry)
        # Missing/duplicated/unexpected names do not enter relative-order checks.
        unique_order = [expected[n] for n in order if len(members[n]) == 1]
        if response.answer_order == "ordered_array" and unique_order != sorted(
            unique_order
        ):
            recognizable = False
    results: list[Result] = []
    answers: list[AnswerAttributionV1] = []
    for question in dependency.questions:
        selected = subset_dependency(dependency, (question.task_id,))
        attributed = members.get(question.key, [])
        answer_type = (
            cast(dict[str, object], attributed[0]).get("type")
            if len(attributed) == 1
            else None
        )
        answers.append(
            AnswerAttributionV1.model_validate(
                {
                    "key": question.key,
                    "answer_ordinals": tuple(ordinals.get(question.key, []))
                    if recognizable
                    else (),
                    "answer_type": answer_type
                    if recognizable
                    and isinstance(answer_type, str)
                    and answer_type in {"score", "choice", "refusal"}
                    else "invalid"
                    if recognizable
                    else "unavailable",
                }
            )
        )
        result: Result | None = None
        status, error = "invalid", "invalid_output"
        entries = members.get(question.key, [])
        if response.kind == "refusal":
            status, error = "refused", "provider_refusal"
        elif response.kind == "provider_error":
            status, error = "errored", "provider_error"
        elif recognizable:
            if len(entries) > 1:
                error = "duplicate_output"
            elif not entries:
                error = "missing_output"
            elif entries[0] == {"name": question.key, "type": "refusal"}:
                status, error = "refused", "provider_refusal"
            else:
                try:
                    result = normalize_answer(
                        entries[0],
                        question,
                        spec,
                        wire,
                        selected.request.tasks[0].item_id,
                    )
                except ValueError:
                    pass
        results.append(
            result
            or unsuccessful(
                spec, selected, identity.run_id, identity.attempt_id, status, error
            )
        )
        if member_checkpoint is not None:
            member_checkpoint()
    return InterpretedDecisionsResponse(
        tuple(results),
        recognizable=recognizable,
        unexpected_members=extras,
        unattributable_members=unattributed,
        answers=tuple(answers),
    )
