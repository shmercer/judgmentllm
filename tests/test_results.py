"""Pure normalization and categorical-output validation using synthetic inputs."""

from typing import Any

import pytest
from pydantic import ValidationError

from judgmentllm.models import Level, PairTask, Trait
from judgmentllm.results import (
    CategoryProbability,
    NormalizationOptions,
    PairwiseResult,
    RubricResult,
    normalize_pairwise_result,
    normalize_rubric_result,
    validate_categorical_output,
    validate_pairwise_output,
)

pytestmark = pytest.mark.smoke


def trait() -> Trait:
    return Trait(
        trait_id="q",
        name="Quality",
        description="Synthetic",
        levels=[
            Level(label="low", numeric_value=1),
            Level(label="mid", numeric_value=2),
            Level(label="high", numeric_value=7),
        ],
    )


def probabilities(values: list[float] | None = None) -> list[dict[str, Any]]:
    return [
        {"level_label": label, "probability": probability}
        for label, probability in zip(
            ["low", "mid", "high"], values or [0.1, 0.25, 0.65], strict=True
        )
    ]


def result(**changes: Any) -> RubricResult:
    kwargs: dict[str, Any] = dict(
        run_id="run",
        task_id="task",
        request_uid="request",
        attempt_id="attempt",
        item_id="item",
        category_label="high",
    )
    return normalize_rubric_result(trait(), **(kwargs | changes))


def pair_result(**changes: Any) -> PairwiseResult:
    kwargs: dict[str, Any] = dict(
        run_id="run",
        task_id="task",
        request_uid="request",
        attempt_id="attempt",
        winner_id="A",
    )
    pair = PairTask(
        pair_id="AB",
        pair_uid="AB-0",
        left_item_id="A",
        right_item_id="B",
        trait_id="q",
        presented_order=["B", "A"],
        is_reversal=True,
        reversal_group="audit",
        source_pair_uid="AB-base",
        repetition_index=2,
    )
    return normalize_pairwise_result(pair, **(kwargs | changes))


def test_nonuniform_expected_score_and_raw_index() -> None:
    scored = result(
        probabilities=probabilities(),
        provider_expected_index=1.55,
        explanation="Synthetic rationale",
        provider="synthetic",
        endpoint="score",
        model="unknown-model",
    )
    assert scored.expected_value == pytest.approx(5.15)
    assert scored.primary_score == scored.expected_value
    assert scored.primary_score_kind == "expected"
    assert scored.provider_expected_index == 1.55
    assert scored.category_label == scored.modal_category_label == "high"
    assert scored.numeric_category_value == 7
    assert scored.probabilities == [
        CategoryProbability.model_validate(value) for value in probabilities()
    ]
    assert scored.probability_sum_tolerance == 1e-8
    assert RubricResult.model_validate_json(scored.model_dump_json()) == scored


def test_category_output_does_not_invent_probabilities() -> None:
    scored = result()
    assert scored.numeric_category_value == scored.primary_score == 7
    assert scored.primary_score_kind == "category_code"
    assert (
        scored.probabilities
        is scored.expected_value
        is scored.modal_category_label
        is None
    )
    assert scored.probability_sum_tolerance is None


def test_selected_and_modal_category_distinct_without_invention() -> None:
    selected = result(category_label="low", probabilities=probabilities())
    assert selected.category_label == "low"
    assert selected.modal_category_label == "high"
    assert selected.numeric_category_value == 1
    no_selected = result(
        category_label=None, probabilities=probabilities([0.5, 0.5, 0])
    )
    assert no_selected.category_label is no_selected.numeric_category_value is None
    assert no_selected.modal_category_label == "low"
    assert no_selected.primary_score == 1.5


def test_numeric_labels_and_zero_based_text_mapping() -> None:
    for labels, expected in [(["1", "2", "3"], 2.55), (["a", "b", "c"], 1.55)]:
        selected = Trait(
            trait_id="q",
            name="Q",
            description="",
            levels=[Level(label=label) for label in labels],
        )
        distribution = [
            CategoryProbability(level_label=label, probability=value)
            for label, value in zip(labels, [0.1, 0.25, 0.65], strict=True)
        ]
        scored = normalize_rubric_result(
            selected,
            run_id="r",
            task_id="t",
            request_uid="req",
            attempt_id="a",
            item_id="i",
            probabilities=distribution,
        )
        assert scored.expected_value == pytest.approx(expected)
        assert [
            entry.numeric_value for entry in scored.resolved_levels
        ] == selected.numeric_values


def test_numeric_selected_label_and_unknown_category() -> None:
    selected = Trait(
        trait_id="q",
        name="Q",
        description="",
        levels=[Level.model_validate({"label": 1}), Level.model_validate({"label": 2})],
    )
    scored = normalize_rubric_result(
        selected,
        run_id="r",
        task_id="t",
        request_uid="req",
        attempt_id="a",
        item_id="i",
        category_label=2,
    )
    assert scored.category_label == "2"
    with pytest.raises(ValueError, match="unknown category"):
        result(category_label="missing")
    with pytest.raises(ValueError, match="requires a category"):
        result(category_label=None)


@pytest.mark.parametrize(
    "values",
    [
        [-0.1, 0.4, 0.7],
        [1.1, 0, 0],
        [float("nan"), 0, 1],
        [float("inf"), 0, 0],
        [True, 0, 0],
        [0.1, 0.2, 0.3],
    ],
)
def test_invalid_probabilities(values: list[Any]) -> None:
    with pytest.raises(ValueError):
        result(probabilities=probabilities(values))


@pytest.mark.parametrize(
    "distribution",
    [
        [],
        [{"level_label": "low", "probability": 1}],
        probabilities()[::-1],
        [probabilities()[0]] * 3,
        [{"level_label": "missing", "probability": 1}, *probabilities()[1:]],
    ],
)
def test_incomplete_duplicate_or_unordered_distributions(
    distribution: list[dict[str, Any]],
) -> None:
    with pytest.raises(ValueError, match="every category"):
        result(probabilities=distribution)


def test_tolerance_configurable_recorded_and_never_renormalizes() -> None:
    distribution = probabilities([0.1, 0.25, 0.6500001])
    with pytest.raises(ValueError, match="sum to 1"):
        result(probabilities=distribution)
    scored = result(
        probabilities=distribution,
        options=NormalizationOptions(probability_sum_tolerance=1e-5),
    )
    assert scored.probabilities is not None
    assert scored.probabilities[2].probability == 0.6500001
    assert scored.probability_sum_tolerance == 1e-5
    assert (
        result(
            probabilities=probabilities([0.25, 0.25, 0.5]),
            options=NormalizationOptions(probability_sum_tolerance=0),
        ).primary_score
        == 4.25
    )


@pytest.mark.parametrize("tolerance", [-1, 1, float("nan"), True, "0.1"])
def test_invalid_tolerance(tolerance: object) -> None:
    with pytest.raises(ValidationError):
        NormalizationOptions.model_validate({"probability_sum_tolerance": tolerance})


@pytest.mark.parametrize(
    "changes,match",
    [
        ({"numeric_category_value": 1}, "numeric_category_value"),
        ({"category_label": "unknown"}, "selected category"),
        ({"category_label": None}, "requires a selected"),
        ({"primary_score": 0}, "primary_score"),
        ({"primary_score_kind": "expected"}, "primary_score"),
        ({"modal_category_label": "high"}, "require probabilities"),
        ({"expected_value": 7}, "require probabilities"),
        ({"provider_expected_index": -1}, "index range"),
        ({"provider_expected_index": 3}, "index range"),
        ({"status": "errored"}, "unsuccessful"),
        (
            {
                "resolved_levels": [
                    {"label": "a", "numeric_value": 0},
                    {"label": "a", "numeric_value": 1},
                ]
            },
            "unique labels",
        ),
        (
            {
                "resolved_levels": [
                    {"label": "a", "numeric_value": 1},
                    {"label": "b", "numeric_value": 0},
                ]
            },
            "unique labels",
        ),
    ],
)
def test_result_rejects_inconsistent_serialized_fields(
    changes: dict[str, Any], match: str
) -> None:
    with pytest.raises(ValidationError, match=match):
        RubricResult.model_validate(result().model_dump() | changes)


@pytest.mark.parametrize(
    "changes,match",
    [
        ({"probability_sum_tolerance": None}, "recorded"),
        ({"expected_value": 1}, "match the distribution"),
        ({"modal_category_label": "low"}, "match the distribution"),
    ],
)
def test_probability_result_rejects_tampered_scores(
    changes: dict[str, Any], match: str
) -> None:
    with pytest.raises(ValidationError, match=match):
        RubricResult.model_validate(
            result(probabilities=probabilities()).model_dump() | changes
        )


def test_failed_result_has_no_invented_primary_score() -> None:
    payload = result().model_dump() | dict(
        status="invalid",
        category_label=None,
        numeric_category_value=None,
        primary_score=None,
        primary_score_kind=None,
        error_kind="schema",
        error_message="Invalid synthetic reply",
    )
    failed = RubricResult.model_validate(payload)
    assert failed.primary_score is None


def test_pair_result_preserves_occurrence_orientation_and_winner() -> None:
    scored = pair_result(p_left=0.8, p_right=0.2, explanation="Synthetic choice")
    assert scored.winner_id == "A"
    assert scored.presented_first == "B"
    assert scored.presented_second == "A"
    assert scored.original_order == ["A", "B"]
    assert scored.p_left == 0.8
    assert scored.repetition_index == 2
    assert scored.source_pair_uid == "AB-base"
    assert scored.is_reversal
    assert PairwiseResult.model_validate_json(scored.model_dump_json()) == scored
    assert pair_result().probability_sum_tolerance is None


def test_pair_tie_representation_and_distribution() -> None:
    scored = pair_result(
        winner_id=None, tie=True, allow_ties=True, p_left=0.25, p_right=0.25, p_tie=0.5
    )
    assert scored.winner_id is None
    assert scored.tie
    assert scored.p_tie == 0.5
    assert pair_result(winner_id=None, tie=True, allow_ties=True).tie


@pytest.mark.parametrize(
    "changes,match",
    [
        ({"winner_id": None}, "requires a winner"),
        ({"winner_id": "C"}, "one of the pair"),
        ({"tie": True}, "tie requires"),
        ({"tie": True, "allow_ties": True}, "tie requires"),
        ({"p_left": 0.7}, "complete choice"),
        ({"p_right": 0.3}, "complete choice"),
        ({"p_left": 0.2, "p_right": 0.2}, "sum to 1"),
        ({"p_tie": 0}, "p_tie requires"),
        ({"allow_ties": True, "p_left": 0.5, "p_right": 0.5}, "complete choice"),
        ({"p_left": float("nan"), "p_right": 1}, "finite"),
    ],
)
def test_invalid_pair_results(changes: dict[str, Any], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        pair_result(**changes)


@pytest.mark.parametrize(
    "changes,match",
    [
        ({"right_item_id": "A"}, "distinct"),
        ({"original_order": ["B", "A"]}, "original pair"),
        ({"presented_order": ["A", "C"]}, "permutation"),
        ({"presented_order": ["A"]}, "permutation"),
        ({"probability_sum_tolerance": None}, "recorded"),
    ],
)
def test_serialized_pair_validation(changes: dict[str, Any], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        PairwiseResult.model_validate(
            pair_result(p_left=0.5, p_right=0.5).model_dump() | changes
        )


def test_pair_probability_tolerance_override() -> None:
    assert (
        pair_result(
            p_left=0.5,
            p_right=0.500001,
            options=NormalizationOptions(probability_sum_tolerance=0.001),
        ).p_right
        == 0.500001
    )
    failed = pair_result().model_dump() | {"status": "errored", "winner_id": None}
    assert PairwiseResult.model_validate(failed).winner_id is None


def test_output_validator_accepts_independent_and_grouped() -> None:
    first = trait()
    second = Trait(
        trait_id="other",
        name="Other",
        description="",
        levels=[Level(label="yes"), Level(label="no")],
    )
    data = {
        "scores": [
            {"trait_id": "q", "category_label": "mid", "explanation": "Synthetic"}
        ]
    }
    assert (
        validate_categorical_output(data, [first], explanations=True)
        .scores[0]
        .category_label
        == "mid"
    )
    grouped = {
        "scores": [
            {"trait_id": "q", "category_label": "mid"},
            {"trait_id": "other", "category_label": "yes"},
        ]
    }
    assert len(validate_categorical_output(grouped, [first, second]).scores) == 2


@pytest.mark.parametrize(
    "data,explain,match",
    [
        ({"scores": []}, False, "at least"),
        (
            {"scores": [{"trait_id": "q", "category_label": "mid"}] * 2},
            False,
            "duplicate",
        ),
        (
            {"scores": [{"trait_id": "other", "category_label": "mid"}]},
            False,
            "missing.*unrequested",
        ),
        (
            {"scores": [{"trait_id": "q", "category_label": "missing"}]},
            False,
            "unknown category",
        ),
        (
            {"scores": [{"trait_id": "q", "category_label": "mid"}]},
            True,
            "explanation required",
        ),
        (
            {
                "scores": [
                    {"trait_id": "q", "category_label": "mid", "explanation": " "}
                ]
            },
            True,
            "explanation required",
        ),
        (
            {"scores": [{"trait_id": "q", "category_label": "mid", "extra": 1}]},
            False,
            "Extra inputs",
        ),
    ],
)
def test_invalid_categorical_output(data: object, explain: bool, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        validate_categorical_output(data, [trait()], explanations=explain)


def test_duplicate_requested_traits_rejected() -> None:
    with pytest.raises(ValueError, match="unique IDs"):
        validate_categorical_output(
            {"scores": [{"trait_id": "q", "category_label": "mid"}]}, [trait(), trait()]
        )


def test_modal_numeric_code_and_tampering() -> None:
    scored = result(probabilities=probabilities(), category_label="low")
    assert scored.modal_numeric_category_value == 7
    assert scored.numeric_category_value == 1
    assert result().modal_numeric_category_value is None
    with pytest.raises(ValidationError, match="match the distribution"):
        RubricResult.model_validate(
            scored.model_dump() | {"modal_numeric_category_value": 2}
        )
    with pytest.raises(ValidationError, match="require probabilities"):
        RubricResult.model_validate(
            result().model_dump() | {"modal_numeric_category_value": 2}
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"primary_score": True},
        {"numeric_category_value": True},
        {"provider_expected_index": False},
        {"confidence": True},
        {"input_tokens": True},
        {"probability_sum_tolerance": False},
    ],
)
def test_result_numeric_fields_reject_booleans(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        RubricResult.model_validate(result().model_dump() | changes)


@pytest.mark.parametrize(
    "payload,allow_ties,explanations,match",
    [
        ({}, False, False, "Field required"),
        ({"winner_id": None, "tie": False}, False, False, "requires winner_id"),
        ({"winner_id": "C", "tie": False}, False, False, "original pair item"),
        ({"winner_id": "A", "tie": True}, True, False, "must not include"),
        ({"winner_id": None, "tie": True}, False, False, "allow_ties"),
        ({"winner_id": "A", "tie": False}, False, True, "explanation required"),
        (
            {"winner_id": "A", "tie": False, "explanation": " "},
            False,
            True,
            "explanation required",
        ),
        ({"winner_id": "A", "tie": False, "extra": True}, False, False, "Extra inputs"),
        ({"winner_id": "A", "tie": 1}, False, False, "valid boolean"),
    ],
)
def test_pairwise_output_validation_errors(
    payload: object, allow_ties: bool, explanations: bool, match: str
) -> None:
    pair = PairTask(
        pair_id="AB", pair_uid="AB-0", left_item_id="A", right_item_id="B", trait_id="q"
    )
    with pytest.raises(ValueError, match=match):
        validate_pairwise_output(
            payload, pair, allow_ties=allow_ties, explanations=explanations
        )


def test_pairwise_output_choice_and_tie() -> None:
    pair = PairTask(
        pair_id="AB",
        pair_uid="AB-0",
        left_item_id="A",
        right_item_id="B",
        trait_id="q",
        presented_order=["B", "A"],
    )
    output = validate_pairwise_output(
        {"winner_id": "B", "tie": False, "explanation": "Synthetic"},
        pair,
        explanations=True,
    )
    assert output.winner_id == "B"
    tied = validate_pairwise_output(
        {"tie": True, "winner_id": None}, pair, allow_ties=True
    )
    assert tied.tie


def test_expected_score_overflow_reports_validation_error() -> None:
    large = Trait(
        trait_id="large",
        name="Large",
        description="Synthetic",
        levels=[
            Level(label="a", numeric_value=1.6e308),
            Level(label="b", numeric_value=1.7e308),
        ],
    )
    with pytest.raises(ValueError, match="finite numeric range"):
        normalize_rubric_result(
            large,
            run_id="r",
            task_id="t",
            request_uid="q",
            attempt_id="a",
            item_id="i",
            probabilities=[
                CategoryProbability(level_label="a", probability=0.8),
                CategoryProbability(level_label="b", probability=0.8),
            ],
            options=NormalizationOptions(probability_sum_tolerance=0.7),
        )


@pytest.mark.parametrize("value", [None, "Synthetic rationale"])
def test_unrequested_explanation_field_rejected_in_both_output_validators(
    value: str | None,
) -> None:
    pair = PairTask(
        pair_id="AB", pair_uid="AB-0", left_item_id="A", right_item_id="B", trait_id="q"
    )
    with pytest.raises(ValueError, match="not permitted"):
        validate_pairwise_output(
            {"winner_id": "A", "tie": False, "explanation": value}, pair
        )
    with pytest.raises(ValueError, match="not permitted"):
        validate_categorical_output(
            {
                "scores": [
                    {"trait_id": "q", "category_label": "high", "explanation": value}
                ]
            },
            [trait()],
        )


@pytest.mark.parametrize("payload", [{"winner_id": "A"}, {"tie": True}])
def test_pairwise_output_requires_explicit_winner_and_tie_fields(
    payload: object,
) -> None:
    pair = PairTask(
        pair_id="AB", pair_uid="AB-0", left_item_id="A", right_item_id="B", trait_id="q"
    )
    with pytest.raises(ValidationError, match="Field required"):
        validate_pairwise_output(payload, pair, allow_ties=True)


def test_generative_category_label_requires_string() -> None:
    selected = Trait(
        trait_id="q",
        name="Quality",
        description="Synthetic",
        levels=[Level(label="1"), Level(label="2")],
    )
    with pytest.raises(ValidationError, match="valid string"):
        validate_categorical_output(
            {"scores": [{"trait_id": "q", "category_label": 1}]}, [selected]
        )
    assert (
        validate_categorical_output(
            {"scores": [{"trait_id": "q", "category_label": "1"}]}, [selected]
        )
        .scores[0]
        .category_label
        == "1"
    )
