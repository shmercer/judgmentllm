"""Validated synthetic-output contracts and pure, provider-neutral normalization."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Literal

from pydantic import Field, model_validator

from judgmentllm.models import (
    ID,
    Label,
    PairTask,
    ResolvedLevel,
    StrictModel,
    Trait,
    VersionedModel,
)


class NormalizationOptions(StrictModel):
    """Absolute sum tolerance; probabilities are retained without renormalization."""

    probability_sum_tolerance: float = Field(default=1e-8, ge=0, lt=1)


class CategoryProbability(StrictModel):
    """One probability in rubric category order."""

    level_label: Label
    probability: float = Field(ge=0, le=1)


class CategorySelection(StrictModel):
    """One validated categorical model output before result normalization."""

    trait_id: ID
    category_label: str
    explanation: str | None = None


class CategoricalOutput(StrictModel):
    """Expected output shape for independent or grouped generative scoring."""

    scores: list[CategorySelection] = Field(min_length=1)


def validate_categorical_output(
    data: object, traits: Sequence[Trait], *, explanations: bool = False
) -> CategoricalOutput:
    """Validate exact requested trait coverage separately from prompt wording."""
    output = CategoricalOutput.model_validate(data)
    expected = {trait.trait_id: trait for trait in traits}
    if len(expected) != len(traits):
        raise ValueError("requested traits must have unique IDs")
    supplied = [score.trait_id for score in output.scores]
    if len(set(supplied)) != len(supplied):
        raise ValueError("categorical output has duplicate trait IDs")
    if set(supplied) != set(expected):
        raise ValueError(
            f"categorical output trait mismatch: missing {sorted(set(expected) - set(supplied))}; "
            f"unrequested {sorted(set(supplied) - set(expected))}"
        )
    for score in output.scores:
        if score.category_label not in {
            level.label for level in expected[score.trait_id].levels
        }:
            raise ValueError(
                f"unknown category {score.category_label!r} for trait {score.trait_id!r}"
            )
        if not explanations and "explanation" in score.model_fields_set:
            raise ValueError(
                "explanation field is not permitted when explanations=false"
            )
        if explanations and (
            score.explanation is None or not score.explanation.strip()
        ):
            raise ValueError(f"explanation required for trait {score.trait_id!r}")
    return output


class PairwiseOutput(StrictModel):
    """Observed categorical pair choice before normalization."""

    winner_id: ID | None
    tie: bool
    explanation: str | None = None

    @model_validator(mode="after")
    def _outcome(self) -> PairwiseOutput:
        if self.tie and self.winner_id is not None:
            raise ValueError("a tie must not include winner_id")
        if not self.tie and self.winner_id is None:
            raise ValueError("pairwise output requires winner_id or tie=true")
        return self


def validate_pairwise_output(
    data: object,
    pair: PairTask,
    *,
    allow_ties: bool = False,
    explanations: bool = False,
) -> PairwiseOutput:
    """Validate exact pair choice and explanation independently of prompt text."""
    output = PairwiseOutput.model_validate(data)
    if output.tie and not allow_ties:
        raise ValueError("tie output requires allow_ties=true")
    if output.winner_id is not None and output.winner_id not in pair.original_order:
        raise ValueError("winner_id must be one of the original pair item IDs")
    if not explanations and "explanation" in output.model_fields_set:
        raise ValueError("explanation field is not permitted when explanations=false")
    if explanations and (output.explanation is None or not output.explanation.strip()):
        raise ValueError("explanation required for pairwise output")
    return output


class ResultBase(VersionedModel):
    """Separate logical identities, execution provenance, and optional audit data."""

    run_id: ID
    task_id: ID
    request_uid: ID
    attempt_id: ID
    status: Literal["complete", "invalid", "refused", "errored", "cancelled"] = (
        "complete"
    )
    error_kind: str | None = None
    error_message: str | None = None
    provider: str | None = None
    endpoint: str | None = None
    model: str | None = None
    tier: str | None = None
    mode: Literal["live", "bulk_live", "batch_native"] | None = None
    explanation: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cache_read_tokens: int | None = Field(default=None, ge=0)
    cache_write_tokens: int | None = Field(default=None, ge=0)
    raw_response_path: str | None = None
    prompt_path: str | None = None
    manifest_sha256: str | None = None
    probability_sum_tolerance: float | None = Field(default=None, ge=0, lt=1)


def _sum_is_valid(values: Sequence[float], tolerance: float) -> None:
    if abs(math.fsum(values) - 1.0) > tolerance:
        raise ValueError(
            f"probabilities must sum to 1 within absolute tolerance {tolerance}"
        )


def _distribution(
    levels: Sequence[ResolvedLevel],
    probabilities: Sequence[CategoryProbability],
    tolerance: float,
) -> tuple[float, str]:
    if [entry.level_label for entry in probabilities] != [
        level.label for level in levels
    ]:
        raise ValueError(
            "probabilities must contain every category exactly once in rubric order"
        )
    _sum_is_valid([entry.probability for entry in probabilities], tolerance)
    try:
        expected = math.fsum(
            entry.probability * level.numeric_value
            for entry, level in zip(probabilities, levels, strict=True)
        )
    except OverflowError as exc:
        raise ValueError("mapped expected value exceeds finite numeric range") from exc
    mode = max(probabilities, key=lambda entry: entry.probability).level_label
    return expected, mode


class RubricResult(ResultBase):
    """One trait result; selected and modal categories remain distinct."""

    task_kind: Literal["rubric"] = "rubric"
    item_id: ID
    trait_id: ID
    resolved_levels: list[ResolvedLevel] = Field(min_length=2)
    category_label: Label | None = None
    numeric_category_value: float | None = None
    modal_category_label: Label | None = None
    modal_numeric_category_value: float | None = None
    probabilities: list[CategoryProbability] | None = None
    provider_expected_index: float | None = None
    expected_value: float | None = None
    primary_score: float | None = None
    primary_score_kind: Literal["expected", "category_code"] | None = None

    @model_validator(mode="after")
    def _scores(self) -> RubricResult:
        labels = [level.label for level in self.resolved_levels]
        values = [level.numeric_value for level in self.resolved_levels]
        if len(set(labels)) != len(labels) or any(
            right <= left for left, right in zip(values, values[1:], strict=False)
        ):
            raise ValueError(
                "resolved_levels must have unique labels and increasing numeric values"
            )
        coding = dict(zip(labels, values, strict=True))
        if self.category_label is not None:
            if self.category_label not in coding:
                raise ValueError("selected category is not in resolved_levels")
            if self.numeric_category_value != coding[self.category_label]:
                raise ValueError(
                    "numeric_category_value does not match selected category coding"
                )
        elif self.numeric_category_value is not None:
            raise ValueError("numeric_category_value requires a selected category")
        if self.provider_expected_index is not None and not (
            0 <= self.provider_expected_index <= len(labels) - 1
        ):
            raise ValueError(
                "provider_expected_index must be within the category index range"
            )
        if self.probabilities is not None:
            if self.probability_sum_tolerance is None:
                raise ValueError(
                    "probability distributions require recorded probability_sum_tolerance"
                )
            expected, mode = _distribution(
                self.resolved_levels, self.probabilities, self.probability_sum_tolerance
            )
            if (
                self.expected_value != expected
                or self.modal_category_label != mode
                or self.modal_numeric_category_value != coding[mode]
            ):
                raise ValueError(
                    "expected_value and modal_category_label must match the distribution"
                )
            score, kind = expected, "expected"
        else:
            if (
                self.expected_value is not None
                or self.modal_category_label is not None
                or self.modal_numeric_category_value is not None
            ):
                raise ValueError(
                    "expected or modal category values require probabilities"
                )
            score, kind = self.numeric_category_value, "category_code"
        if self.status == "complete":
            if score is None:
                raise ValueError(
                    "complete rubric result requires a category or full probabilities"
                )
            if self.primary_score != score or self.primary_score_kind != kind:
                raise ValueError(
                    "primary_score and primary_score_kind do not match scoring semantics"
                )
        elif self.primary_score is not None or self.primary_score_kind is not None:
            raise ValueError("unsuccessful result cannot have a primary score")
        return self


def normalize_rubric_result(
    trait: Trait,
    *,
    run_id: str,
    task_id: str,
    request_uid: str,
    attempt_id: str,
    item_id: str,
    category_label: str | int | float | None = None,
    probabilities: Sequence[CategoryProbability | Mapping[str, object]] | None = None,
    provider_expected_index: float | None = None,
    explanation: str | None = None,
    options: NormalizationOptions | None = None,
    provider: str | None = None,
    endpoint: str | None = None,
    model: str | None = None,
) -> RubricResult:
    """Derive scores from a selected category and/or complete ordered distribution.

    No selected category is inferred from probabilities. Modal ties select the
    first category in rubric order. Provider-reported expected indices are kept
    unchanged and never substituted for mapped numeric expectations.
    """
    from judgmentllm.models import canonical_label

    selected = canonical_label(category_label) if category_label is not None else None
    coding = {level.label: level.numeric_value for level in trait.resolved_levels}
    if selected is not None and selected not in coding:
        raise ValueError(f"unknown category {selected!r} for trait {trait.trait_id!r}")
    numeric = coding[selected] if selected is not None else None
    parsed = (
        [CategoryProbability.model_validate(entry) for entry in probabilities]
        if probabilities is not None
        else None
    )
    tolerance = (options or NormalizationOptions()).probability_sum_tolerance
    expected, modal = (
        _distribution(trait.resolved_levels, parsed, tolerance)
        if parsed is not None
        else (None, None)
    )
    return RubricResult(
        run_id=run_id,
        task_id=task_id,
        request_uid=request_uid,
        attempt_id=attempt_id,
        item_id=item_id,
        trait_id=trait.trait_id,
        resolved_levels=trait.resolved_levels,
        category_label=selected,
        numeric_category_value=numeric,
        modal_category_label=modal,
        modal_numeric_category_value=coding[modal] if modal is not None else None,
        probabilities=parsed,
        provider_expected_index=provider_expected_index,
        expected_value=expected,
        primary_score=expected if parsed is not None else numeric,
        primary_score_kind="expected" if parsed is not None else "category_code",
        probability_sum_tolerance=tolerance if parsed is not None else None,
        explanation=explanation,
        provider=provider,
        endpoint=endpoint,
        model=model,
    )


class PairwiseResult(ResultBase):
    """An attempted pair occurrence; probabilities use original left/right IDs."""

    task_kind: Literal["pairwise"] = "pairwise"
    pair_id: ID
    pair_uid: ID
    trait_id: ID
    left_item_id: ID
    right_item_id: ID
    original_order: list[ID]
    presented_order: list[ID]
    source_pair_uid: ID | None = None
    repetition_index: int = Field(default=0, ge=0)
    is_reversal: bool = False
    reversal_group: ID | None = None
    winner_id: ID | None = None
    tie: bool = False
    allow_ties: bool = False
    p_left: float | None = Field(default=None, ge=0, le=1)
    p_right: float | None = Field(default=None, ge=0, le=1)
    p_tie: float | None = Field(default=None, ge=0, le=1)

    @property
    def presented_first(self) -> str:
        return self.presented_order[0]

    @property
    def presented_second(self) -> str:
        return self.presented_order[1]

    @model_validator(mode="after")
    def _choice(self) -> PairwiseResult:
        expected = [self.left_item_id, self.right_item_id]
        if len(set(expected)) != 2 or self.original_order != expected:
            raise ValueError("original pair order must contain distinct left/right IDs")
        if len(self.presented_order) != 2 or set(self.presented_order) != set(expected):
            raise ValueError(
                "presented_order must be a permutation of original pair order"
            )
        if self.tie and (not self.allow_ties or self.winner_id is not None):
            raise ValueError("a tie requires allow_ties=true and no winner_id")
        if self.winner_id is not None and self.winner_id not in expected:
            raise ValueError("winner_id must be one of the pair item IDs")
        if self.status == "complete" and not self.tie and self.winner_id is None:
            raise ValueError(
                "complete pairwise result requires a winner or permitted tie"
            )
        probabilities = [self.p_left, self.p_right]
        if self.allow_ties:
            probabilities.append(self.p_tie)
        elif self.p_tie is not None:
            raise ValueError("p_tie requires allow_ties=true")
        if any(value is not None for value in probabilities):
            if any(value is None for value in probabilities):
                raise ValueError(
                    "supply a complete choice probability distribution or none"
                )
            if self.probability_sum_tolerance is None:
                raise ValueError(
                    "choice probabilities require recorded probability_sum_tolerance"
                )
            _sum_is_valid(
                [value for value in probabilities if value is not None],
                self.probability_sum_tolerance,
            )
        return self


def normalize_pairwise_result(
    pair: PairTask,
    *,
    run_id: str,
    task_id: str,
    request_uid: str,
    attempt_id: str,
    winner_id: str | None = None,
    tie: bool = False,
    p_left: float | None = None,
    p_right: float | None = None,
    p_tie: float | None = None,
    allow_ties: bool = False,
    explanation: str | None = None,
    options: NormalizationOptions | None = None,
    provider: str | None = None,
    endpoint: str | None = None,
    model: str | None = None,
) -> PairwiseResult:
    """Validate an observed winner/tie without inferring one from probabilities."""
    probability_present = any(value is not None for value in (p_left, p_right, p_tie))
    return PairwiseResult(
        run_id=run_id,
        task_id=task_id,
        request_uid=request_uid,
        attempt_id=attempt_id,
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
        winner_id=winner_id,
        tie=tie,
        allow_ties=allow_ties,
        p_left=p_left,
        p_right=p_right,
        p_tie=p_tie,
        probability_sum_tolerance=(
            (options or NormalizationOptions()).probability_sum_tolerance
            if probability_present
            else None
        ),
        explanation=explanation,
        provider=provider,
        endpoint=endpoint,
        model=model,
    )
