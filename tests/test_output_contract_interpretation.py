"""Detached output-contract interpretation keeps original task/result semantics."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Literal, cast

import pytest
from pydantic import JsonValue
from test_preparation_rendering import specification

from judgmentllm.config import RunSpec
from judgmentllm.execution.processing import (
    _index_members,  # pyright: ignore[reportPrivateUsage]
    interpret,
)
from judgmentllm.models import PairTask
from judgmentllm.output_contracts import (
    PairwiseOutputContract,
    RubricOutputContract,
    validate_rubric_contract_output,
)
from judgmentllm.preparation import RequestDependency, prepare_run
from judgmentllm.prompts import get_template
from judgmentllm.providers import ParsedResponse
from judgmentllm.results import PairwiseResult, RubricResult

Layout = Literal["together", "scores_first", "explanations_first"]
LAYOUTS: tuple[Layout, ...] = ("together", "scores_first", "explanations_first")


def rubric_case(
    directory: Path,
    layout: Layout = "scores_first",
    *,
    explanations: bool = True,
    traits: int = 3,
) -> tuple[RunSpec, RequestDependency, RubricOutputContract]:
    spec = specification(directory / "inputs", responses=1, traits=traits)
    spec.execution.grouping = "grouped"
    spec.execution.explanations = explanations
    spec.prompt.preset = None
    assert spec.rubric is not None
    for trait in spec.rubric.traits:
        trait.levels[0].numeric_value = 2.0
        trait.levels[1].numeric_value = 9.0
    spec = RunSpec.model_validate(spec.model_dump())
    with prepare_run(spec, store=directory / "store", run_id="001") as prepared:
        dependency = next(prepared.iter_request_dependencies())
    assert spec.rubric is not None
    return (
        spec,
        dependency,
        RubricOutputContract(
            traits=tuple(spec.rubric.traits),
            output_order=layout,
            explanations=explanations,
        ),
    )


def rubric_output(contract: RubricOutputContract) -> dict[str, JsonValue]:
    scores: list[JsonValue] = []
    explanations: list[JsonValue] = []
    for trait in contract.traits:
        entry: dict[str, JsonValue] = {
            "trait_id": trait.trait_id,
            "category_label": "high",
        }
        if contract.explanations:
            text = f"Synthetic explanation for {trait.trait_id}."
            if contract.output_order == "together":
                entry["explanation"] = text
            else:
                explanations.append({"trait_id": trait.trait_id, "text": text})
        scores.append(entry)
    # Deliberately use differing array orders and the opposite root key order.
    scores.reverse()
    if contract.output_order == "together":
        return {"scores": scores}
    if contract.output_order == "scores_first":
        return {"explanations": explanations, "scores": scores}
    return {"scores": scores, "explanations": explanations}


def parsed_output(output: dict[str, JsonValue]) -> ParsedResponse:
    return ParsedResponse.model_validate({"kind": "response", "output": output})


@pytest.mark.parametrize("layout", LAYOUTS)
@pytest.mark.parametrize("traits", [1, 3])
def test_all_layouts_keep_identities_and_join_by_trait(
    tmp_path: Path, layout: Layout, traits: int
) -> None:
    spec, dependency, contract = rubric_case(tmp_path, layout, traits=traits)
    output = rubric_output(contract)
    assert len(validate_rubric_contract_output(output, contract).scores) == traits
    checkpoints: list[None] = []
    interpreted = interpret(
        parsed_output(output),
        spec,
        dependency,
        "001",
        "attempt-001",
        contract=contract,
        member_checkpoint=lambda: checkpoints.append(None),
    )
    assert interpreted.recognizable
    assert interpreted.unexpected_members == interpreted.unattributable_members == 0
    assert len(checkpoints) == traits
    for result, task in zip(interpreted.results, dependency.request.tasks, strict=True):
        assert isinstance(result, RubricResult)
        assert (
            result.run_id,
            result.task_id,
            result.request_uid,
            result.attempt_id,
        ) == ("001", task.task_id, dependency.request.request_uid, "attempt-001")
        assert result.trait_id == task.trait_id
        assert result.status == "complete" and result.error_kind is None
        assert result.explanation == f"Synthetic explanation for {task.trait_id}."
        assert result.category_label == "high"
        assert result.numeric_category_value == result.primary_score == 9.0
        assert result.primary_score_kind == "category_code"
        assert (
            result.expected_value
            is result.probabilities
            is result.modal_category_label
            is None
        )
        assert (result.provider, result.endpoint, result.model) == (
            spec.provider,
            spec.endpoint,
            spec.model,
        )
        assert result.schema_version == 1
    assert len({result.task_id for result in interpreted.results}) == traits


def test_scores_only_contract_and_legacy_path_are_identical(tmp_path: Path) -> None:
    spec, dependency, contract = rubric_case(tmp_path, "together", explanations=False)
    parsed = parsed_output(rubric_output(contract))
    assert interpret(
        parsed, spec, dependency, "001", "attempt", contract=contract
    ) == interpret(parsed, spec, dependency, "001", "attempt")


@pytest.mark.parametrize("layout", LAYOUTS)
@pytest.mark.parametrize(
    "failure",
    [
        "invalid_category",
        "missing_score",
        "duplicate_score",
        "blank_explanation",
        "missing_explanation",
        "wrong_explanation",
        "extra_score_field",
        "numeric_category",
    ],
)
def test_invalid_members_preserve_valid_siblings(
    tmp_path: Path, layout: Layout, failure: str
) -> None:
    spec, dependency, contract = rubric_case(tmp_path, layout)
    output = rubric_output(contract)
    scores = cast(list[dict[str, JsonValue]], output["scores"])
    score = next(entry for entry in scores if entry["trait_id"] == "001")
    explanations = (
        scores
        if layout == "together"
        else cast(list[dict[str, JsonValue]], output["explanations"])
    )
    explanation = next(entry for entry in explanations if entry["trait_id"] == "001")
    field = "explanation" if layout == "together" else "text"
    error = "invalid_output"
    if failure == "invalid_category":
        score["category_label"] = "unknown"
    elif failure == "numeric_category":
        score["category_label"] = 1
    elif failure == "missing_score":
        scores.remove(score)
        error = "missing_output"
    elif failure == "duplicate_score":
        scores.append(deepcopy(score))
        error = "duplicate_output"
    elif failure == "blank_explanation":
        explanation[field] = " \t\n"
    elif failure == "missing_explanation":
        if layout == "together":
            del explanation[field]
        else:
            explanations.remove(explanation)
            error = "missing_output"
    elif failure == "wrong_explanation":
        explanation[field] = None
    else:
        score["unexpected"] = "Synthetic rejected field."
    with pytest.raises(ValueError):
        validate_rubric_contract_output(output, contract)
    interpreted = interpret(
        parsed_output(output), spec, dependency, "001", "attempt", contract=contract
    )
    assert interpreted.recognizable
    assert [
        (result.trait_id, result.status, result.error_kind)
        for result in interpreted.results
    ] == [
        ("000", "complete", None),
        ("001", "invalid", error),
        ("002", "complete", None),
    ]
    invalid = interpreted.results[1]
    assert isinstance(invalid, RubricResult)
    assert (
        invalid.category_label
        is invalid.primary_score
        is invalid.explanation
        is invalid.probabilities
        is None
    )


@pytest.mark.parametrize("layout", ["scores_first", "explanations_first"])
@pytest.mark.parametrize("missing_score", [False, True])
def test_duplicate_explanation_is_invalid_even_with_missing_score(
    tmp_path: Path, layout: Layout, missing_score: bool
) -> None:
    spec, dependency, contract = rubric_case(tmp_path, layout)
    output = rubric_output(contract)
    explanations = cast(list[dict[str, JsonValue]], output["explanations"])
    explanation = next(entry for entry in explanations if entry["trait_id"] == "001")
    explanations.extend([deepcopy(explanation), {"trait_id": "001", "text": None}])
    if missing_score:
        scores = cast(list[dict[str, JsonValue]], output["scores"])
        scores[:] = [entry for entry in scores if entry["trait_id"] != "001"]
    interpreted = interpret(
        parsed_output(output), spec, dependency, "001", "attempt", contract=contract
    )
    assert interpreted.results[1].error_kind == "duplicate_output"
    assert interpreted.results[0].status == interpreted.results[2].status == "complete"


@pytest.mark.parametrize("layout", LAYOUTS)
def test_unexpected_and_unattributable_entries_only_add_counts(
    tmp_path: Path, layout: Layout
) -> None:
    spec, dependency, contract = rubric_case(tmp_path, layout)
    output = rubric_output(contract)
    arrays = ("scores",) if layout == "together" else ("scores", "explanations")
    for array in arrays:
        cast(list[JsonValue], output[array]).extend(
            [
                {
                    "trait_id": "Unrequested sensitive generated text.",
                    "text": "Do not log.",
                },
                {"trait_id": "unknown"},
                {"trait_id": 1},
                None,
                "Do not log.",
            ]
        )
    interpreted = interpret(
        parsed_output(output), spec, dependency, "001", "attempt", contract=contract
    )
    assert interpreted.unexpected_members == 2 * len(arrays)
    assert interpreted.unattributable_members == 3 * len(arrays)
    assert len(interpreted.results) == 3
    assert all(result.status == "complete" for result in interpreted.results)
    assert "Do not log" not in repr(interpreted)
    assert "Unrequested sensitive" not in repr(interpreted)


@pytest.mark.parametrize("layout", LAYOUTS)
@pytest.mark.parametrize("envelope", ["missing", "null", "wrong_type", "extra"])
def test_unrecognizable_envelope_has_no_successful_members(
    tmp_path: Path, layout: Layout, envelope: str
) -> None:
    spec, dependency, contract = rubric_case(tmp_path, layout)
    output = rubric_output(contract)
    field = "scores" if layout == "together" else "explanations"
    if envelope == "missing":
        del output[field]
    elif envelope == "null":
        output[field] = None
    elif envelope == "wrong_type":
        output[field] = {}
    else:
        output["unexpected"] = []
    interpreted = interpret(
        parsed_output(output), spec, dependency, "001", "attempt", contract=contract
    )
    assert not interpreted.recognizable
    assert all(
        result.status == "invalid" and result.error_kind == "invalid_output"
        for result in interpreted.results
    )
    assert interpreted.unexpected_members == interpreted.unattributable_members == 0


@pytest.mark.parametrize(
    "kind,status,error",
    [
        ("refusal", "refused", "provider_refusal"),
        ("provider_error", "errored", "provider_error"),
        ("confirmed_cancellation", "cancelled", "confirmed_cancellation"),
        (None, "invalid", "invalid_output"),
    ],
)
@pytest.mark.parametrize("layout", LAYOUTS)
def test_terminal_provider_evidence_keeps_existing_outcomes(
    tmp_path: Path, layout: Layout, kind: str | None, status: str, error: str
) -> None:
    spec, dependency, contract = rubric_case(tmp_path, layout)
    parsed = ParsedResponse.model_validate({"kind": kind}) if kind else None
    interpreted = interpret(
        parsed, spec, dependency, "001", "attempt", contract=contract
    )
    assert len(interpreted.results) == 3
    assert all(
        result.status == status and result.error_kind == error
        for result in interpreted.results
    )


@pytest.mark.parametrize("layout", LAYOUTS)
def test_probabilities_and_provider_indices_are_preserved(
    tmp_path: Path, layout: Layout
) -> None:
    spec, dependency, contract = rubric_case(tmp_path, layout)
    parsed = ParsedResponse.model_validate(
        {
            "kind": "response",
            "output": rubric_output(contract),
            "member_probabilities": {
                trait.trait_id: (
                    {"level_label": "low", "probability": 0.8},
                    {"level_label": "high", "probability": 0.2},
                )
                for trait in contract.traits
            },
            "member_expected_indices": {
                trait.trait_id: 0.2 for trait in contract.traits
            },
        }
    )
    interpreted = interpret(
        parsed, spec, dependency, "001", "attempt", contract=contract
    )
    for result in interpreted.results:
        assert isinstance(result, RubricResult)
        assert result.category_label == "high" and result.numeric_category_value == 9.0
        assert (
            result.modal_category_label == "low"
            and result.modal_numeric_category_value == 2.0
        )
        assert result.primary_score_kind == "expected"
        assert result.expected_value == result.primary_score == pytest.approx(3.4)
        assert result.provider_expected_index == 0.2
        assert result.probabilities is not None
        assert [entry.probability for entry in result.probabilities] == [0.8, 0.2]


def pairwise_case(
    directory: Path, *, allow_ties: bool = False, explanations: bool = False
) -> tuple[RunSpec, RequestDependency, PairwiseOutputContract]:
    spec = specification(directory / "inputs", responses=2, traits=1, pairwise=True)
    spec.allow_ties = allow_ties
    spec.execution.explanations = explanations
    template = directory / "template.j2"
    template.write_text(get_template("pairwise_forced_choice"), encoding="utf-8")
    spec.prompt.preset, spec.prompt.file = None, template
    pair = PairTask(
        pair_id="000",
        pair_uid="pair-reversed-001",
        left_item_id="000",
        right_item_id="001",
        trait_id="000",
        presented_order=["001", "000"],
        source_pair_uid="source-001",
        repetition_index=2,
        is_reversal=True,
        reversal_group="reversal-001",
    )
    assert spec.pairs is not None
    spec.pairs.path.write_text(pair.model_dump_json() + "\n", encoding="utf-8")
    spec = RunSpec.model_validate(spec.model_dump())
    with prepare_run(spec, store=directory / "store", run_id="001") as prepared:
        dependency = next(prepared.iter_request_dependencies())
    assert dependency.request.pair is not None
    return (
        spec,
        dependency,
        PairwiseOutputContract(
            pair=dependency.request.pair,
            allow_ties=allow_ties,
            explanations=explanations,
        ),
    )


@pytest.mark.parametrize("legacy_tie", [False, True])
@pytest.mark.parametrize("explanations", [False, True])
def test_pairwise_winner_contract_keeps_provenance_and_original_probabilities(
    tmp_path: Path, legacy_tie: bool, explanations: bool
) -> None:
    spec, dependency, contract = pairwise_case(tmp_path, explanations=explanations)
    output: dict[str, JsonValue] = {"winner_id": "001"}
    if legacy_tie:
        output["tie"] = False
    if explanations:
        output["explanation"] = "Synthetic winning response."
    parsed = ParsedResponse.model_validate(
        {"kind": "response", "output": output, "p_left": 0.7, "p_right": 0.3}
    )
    interpreted = interpret(
        parsed, spec, dependency, "001", "attempt", contract=contract
    )
    result = interpreted.results[0]
    assert isinstance(result, PairwiseResult)
    assert (
        result.status == "complete"
        and result.winner_id == "001"
        and result.tie is False
    )
    assert result.original_order == ["000", "001"] and result.presented_order == [
        "001",
        "000",
    ]
    assert result.p_left == 0.7 and result.p_right == 0.3
    assert result.is_reversal and result.repetition_index == 2
    assert (
        result.source_pair_uid == "source-001"
        and result.reversal_group == "reversal-001"
    )
    assert result.task_id == dependency.request.tasks[0].task_id
    assert (
        result.request_uid == dependency.request.request_uid
        and result.attempt_id == "attempt"
    )
    legacy = interpret(parsed, spec, dependency, "001", "attempt")
    assert legacy.results[0].status == ("complete" if legacy_tie else "invalid")


@pytest.mark.parametrize("tie", [False, True])
def test_pairwise_tie_policy(tmp_path: Path, tie: bool) -> None:
    spec, dependency, contract = pairwise_case(tmp_path, allow_ties=True)
    result = interpret(
        parsed_output({"winner_id": None if tie else "000", "tie": tie}),
        spec,
        dependency,
        "001",
        "attempt",
        contract=contract,
    ).results[0]
    assert isinstance(result, PairwiseResult) and result.status == "complete"
    assert result.tie is tie and result.allow_ties
    assert result.winner_id == (None if tie else "000")


@pytest.mark.parametrize(
    "case", ["trait_order", "trait_definition", "explanations", "kind"]
)
def test_incompatible_contract_fails_before_interpreting(
    tmp_path: Path, case: str
) -> None:
    spec, dependency, contract = rubric_case(tmp_path)
    if case == "trait_order":
        incompatible = contract.model_copy(
            update={"traits": tuple(reversed(contract.traits))}
        )
    elif case == "trait_definition":
        altered = deepcopy(contract.traits[0])
        altered.name = "Different requested trait."
        incompatible = contract.model_copy(
            update={"traits": (altered, *contract.traits[1:])}
        )
    elif case == "explanations":
        incompatible = RubricOutputContract(traits=contract.traits)
    else:
        incompatible = PairwiseOutputContract(
            pair=PairTask(
                pair_id="pair",
                pair_uid="pair",
                left_item_id="left",
                right_item_id="right",
                trait_id="000",
            )
        )
    with pytest.raises(ValueError, match="must match"):
        interpret(
            parsed_output(rubric_output(contract)),
            spec,
            dependency,
            "001",
            "attempt",
            contract=incompatible,
        )


@pytest.mark.parametrize("case", ["tie_policy", "pair_identity", "task_membership"])
def test_pairwise_contract_mismatch_fails_before_results(
    tmp_path: Path, case: str
) -> None:
    spec, dependency, contract = pairwise_case(tmp_path)
    if case == "tie_policy":
        contract = contract.model_copy(update={"allow_ties": True})
    elif case == "pair_identity":
        contract = contract.model_copy(
            update={"pair": contract.pair.model_copy(update={"pair_uid": "other"})}
        )
    else:
        request = dependency.request.model_copy(update={"trait_ids": ("wrong",)})
        dependency = dependency.model_copy(update={"request": request})
    with pytest.raises(ValueError, match="must match"):
        interpret(
            parsed_output({"winner_id": "001"}),
            spec,
            dependency,
            "001",
            "attempt",
            contract=contract,
        )


def test_large_indexes_are_bounded_and_unknown_text_is_not_retained(
    tmp_path: Path,
) -> None:
    spec, dependency, contract = rubric_case(tmp_path)
    output = rubric_output(contract)
    scores = cast(list[JsonValue], output["scores"])
    explanations = cast(list[JsonValue], output["explanations"])
    for _ in range(1000):
        scores.extend(
            [
                {"trait_id": "001", "category_label": "high"},
                {"trait_id": "private unknown", "text": "private generated text"},
            ]
        )
        explanations.extend(
            [{"trait_id": "001", "text": "private duplicate"}, "private unattributable"]
        )
    indexed, unexpected, unattributable = _index_members(scores, {"000", "001", "002"})
    assert set(indexed) == {"000", "001", "002"}
    assert len(indexed["001"]) == 2 and unexpected == 1000 and unattributable == 0
    interpreted = interpret(
        parsed_output(output), spec, dependency, "001", "attempt", contract=contract
    )
    assert interpreted.unexpected_members == interpreted.unattributable_members == 1000
    assert [result.status for result in interpreted.results] == [
        "complete",
        "invalid",
        "complete",
    ]
    assert "private" not in repr(interpreted)


def test_nonfinite_decoded_output_cannot_be_salvaged(tmp_path: Path) -> None:
    spec, dependency, contract = rubric_case(tmp_path)
    output = rubric_output(contract)
    cast(list[JsonValue], output["scores"]).append(
        {"trait_id": "unknown", "value": float("inf")}
    )
    # SDK/model-copy data may already have bypassed the normal transport validation.
    parsed = ParsedResponse.model_construct(kind="response", output=output)
    interpreted = interpret(
        parsed, spec, dependency, "001", "attempt", contract=contract
    )
    assert not interpreted.recognizable
    assert all(result.status == "invalid" for result in interpreted.results)


@pytest.mark.parametrize("layout", LAYOUTS)
def test_all_missing_members_remain_individually_unsuccessful(
    tmp_path: Path, layout: Layout
) -> None:
    spec, dependency, contract = rubric_case(tmp_path, layout)
    output: dict[str, JsonValue] = {"scores": []}
    if layout != "together":
        output["explanations"] = []
    interpreted = interpret(
        parsed_output(output), spec, dependency, "001", "attempt", contract=contract
    )
    assert interpreted.recognizable
    assert len(interpreted.results) == 3
    assert all(
        result.status == "invalid" and result.error_kind == "missing_output"
        for result in interpreted.results
    )


def test_rubric_contract_rejects_injected_pair_and_tie_controls(tmp_path: Path) -> None:
    spec, dependency, contract = rubric_case(tmp_path)
    pair = PairTask(
        pair_id="pair",
        pair_uid="pair",
        left_item_id="left",
        right_item_id="right",
        trait_id="000",
    )
    injected = dependency.model_copy(
        update={"request": dependency.request.model_copy(update={"pair": pair})}
    )
    with pytest.raises(ValueError, match="requires a rubric request"):
        interpret(None, spec, injected, "001", "attempt", contract=contract)
    spec.allow_ties = True
    with pytest.raises(ValueError, match="only valid for pairwise"):
        interpret(None, spec, dependency, "001", "attempt", contract=contract)


@pytest.mark.parametrize("layout", LAYOUTS)
def test_independent_contract_uses_probability_fallback_and_recorded_tolerance(
    tmp_path: Path, layout: Layout
) -> None:
    spec, dependency, contract = rubric_case(tmp_path, layout, traits=1)
    probabilities = (
        {"level_label": "low", "probability": 0.8},
        {"level_label": "high", "probability": 0.20000005},
    )
    parsed = ParsedResponse.model_validate(
        {
            "kind": "response",
            "output": rubric_output(contract),
            "rubric_probabilities": probabilities,
            "provider_expected_index": 0.2,
        }
    )
    strict = interpret(
        parsed, spec, dependency, "001", "strict", contract=contract
    ).results[0]
    assert strict.status == "invalid" and strict.error_kind == "invalid_output"
    spec.normalization.probability_sum_tolerance = 1e-7
    accepted = interpret(
        parsed, spec, dependency, "001", "tolerant", contract=contract
    ).results[0]
    assert isinstance(accepted, RubricResult) and accepted.status == "complete"
    assert accepted.primary_score_kind == "expected"
    assert accepted.expected_value == pytest.approx(3.40000045)
    assert accepted.provider_expected_index == 0.2
    assert accepted.probability_sum_tolerance == 1e-7
    assert accepted.probabilities is not None
    assert [entry.probability for entry in accepted.probabilities] == [0.8, 0.20000005]


@pytest.mark.parametrize("layout", LAYOUTS)
def test_recognized_group_with_all_invalid_explanations_has_no_success(
    tmp_path: Path, layout: Layout
) -> None:
    spec, dependency, contract = rubric_case(tmp_path, layout)
    output = rubric_output(contract)
    array, field = (
        ("scores", "explanation") if layout == "together" else ("explanations", "text")
    )
    for entry in cast(list[dict[str, JsonValue]], output[array]):
        entry[field] = " \t\n"
    interpreted = interpret(
        parsed_output(output), spec, dependency, "001", "attempt", contract=contract
    )
    assert interpreted.recognizable and len(interpreted.results) == 3
    assert all(
        result.status == "invalid" and result.error_kind == "invalid_output"
        for result in interpreted.results
    )
