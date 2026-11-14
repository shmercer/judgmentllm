"""Phase 1 contracts are strict, ordered, provider-neutral, and wholly offline."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Literal, cast

import pytest

from judgmentllm.models import Level, PairTask, Trait
from judgmentllm.output_contracts import (
    PairwiseOutputContract,
    RubricOutputContract,
    decode_assessment_output,
    generate_output_schema,
    serialize_output_schema,
    validate_pairwise_contract_output,
    validate_rubric_contract_output,
)
from judgmentllm.results import (
    CategoricalOutput,
    PairwiseOutput,
    normalize_rubric_result,
)

Order = Literal["together", "scores_first", "explanations_first"]
ORDERS: tuple[Order, ...] = ("together", "scores_first", "explanations_first")


def traits() -> tuple[Trait, ...]:
    return (
        Trait(
            trait_id="01",
            name="Cohesion",
            description="Synthetic coherence.",
            levels=[
                Level(label="low", numeric_value=1),
                Level(label="medium", numeric_value=3),
                Level(label="high", numeric_value=8),
            ],
        ),
        Trait(
            trait_id="02",
            name="Grammar",
            description="Synthetic grammar.",
            levels=[
                Level(label="1", numeric_value=2),
                Level(label="2", numeric_value=9),
            ],
        ),
    )


def pair() -> PairTask:
    return PairTask(
        pair_id="0001",
        pair_uid="0001-r1",
        left_item_id="001",
        right_item_id="002",
        trait_id="quality",
        presented_order=["002", "001"],
        source_pair_uid="0001-source",
        repetition_index=1,
        is_reversal=True,
        reversal_group="0001-audit",
    )


def contract(
    order: Order = "together", *, explanations: bool = True
) -> RubricOutputContract:
    return RubricOutputContract(
        traits=traits(), output_order=order, explanations=explanations
    )


def payload(order: Order = "together", *, explanations: bool = True) -> dict[str, Any]:
    scores = [
        {"trait_id": "01", "category_label": "high"},
        {"trait_id": "02", "category_label": "2"},
    ]
    if order == "together":
        if explanations:
            for score in scores:
                score["explanation"] = f"Synthetic explanation for {score['trait_id']}."
        return {"scores": scores}
    text = [
        {"trait_id": "01", "text": "Synthetic explanation for 01."},
        {"trait_id": "02", "text": "Synthetic explanation for 02."},
    ]
    return (
        {"scores": scores, "explanations": text}
        if order == "scores_first"
        else {"explanations": text, "scores": scores}
    )


def _properties(schema: dict[str, Any]) -> dict[str, Any]:
    return schema["properties"]


@pytest.mark.parametrize("order", ORDERS)
@pytest.mark.parametrize("single", [False, True])
def test_schema_exact_closed_trait_specific_members_and_order(
    order: Order, single: bool
) -> None:
    requested = traits()[:1] if single else traits()[::-1]
    configured = RubricOutputContract(
        traits=requested, output_order=order, explanations=True
    )
    schema = generate_output_schema(configured)
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    expected_root = (
        ["explanations", "scores"] if order == "explanations_first" else ["scores"]
    )
    if order == "scores_first":
        expected_root.append("explanations")
    assert list(_properties(schema)) == schema["required"] == expected_root
    scores = _properties(schema)["scores"]
    assert scores["type"] == "array"
    assert scores["minItems"] == scores["maxItems"] == len(requested)
    choices = scores["items"]["oneOf"]
    assert len(choices) == len(requested)
    for choice, trait in zip(choices, requested, strict=True):
        expected_fields = ["trait_id", "category_label"]
        if order == "together":
            expected_fields.append("explanation")
        assert choice["type"] == "object"
        assert choice["additionalProperties"] is False
        assert list(choice["properties"]) == choice["required"] == expected_fields
        assert choice["properties"]["trait_id"] == {"const": trait.trait_id}
        assert choice["properties"]["category_label"] == {
            "type": "string",
            "enum": [level.label for level in trait.levels],
        }
        if order == "together":
            assert choice["properties"]["explanation"] == {
                "type": "string",
                "minLength": 1,
            }
    if order != "together":
        explanations = _properties(schema)["explanations"]
        assert explanations["minItems"] == explanations["maxItems"] == len(requested)
        entry = explanations["items"]
        assert entry["type"] == "object"
        assert entry["additionalProperties"] is False
        assert list(entry["properties"]) == entry["required"] == ["trait_id", "text"]
        assert entry["properties"]["trait_id"] == {
            "type": "string",
            "enum": [trait.trait_id for trait in requested],
        }
        assert entry["properties"]["text"] == {"type": "string", "minLength": 1}


def test_scores_only_default_and_schema_determinism() -> None:
    configured = RubricOutputContract(traits=traits())
    assert configured.output_order == "together"
    assert configured.explanations is False
    assert configured.contract_version == 2
    assert configured.task_kind == "rubric"
    schema = generate_output_schema(configured)
    assert list(_properties(schema)) == ["scores"]
    for choice in _properties(schema)["scores"]["items"]["oneOf"]:
        assert choice["required"] == ["trait_id", "category_label"]
        assert "explanation" not in choice["properties"]
    first = serialize_output_schema(configured)
    assert isinstance(first, bytes)
    for _ in range(3):
        assert generate_output_schema(configured) == schema
        assert serialize_output_schema(configured) == first
    schema["unexpected"] = True
    assert "unexpected" not in generate_output_schema(configured)


def test_all_six_approved_example_schemas_and_ordered_serializations() -> None:
    document = (
        Path(__file__).resolve().parents[1] / "docs" / "assessment-prompt-examples.md"
    )
    examples = [
        line
        for line in document.read_text(encoding="utf-8").splitlines()
        if line.startswith('{"type":"object","properties":')
    ]
    assert len(examples) == 6
    selected = tuple(
        Trait(
            trait_id=trait_id,
            name=trait_id.title(),
            description="Synthetic.",
            levels=[Level(label=label) for label in ("low", "medium", "high")],
        )
        for trait_id in ("cohesion", "grammar")
    )
    presented = PairTask(
        pair_id="demo",
        pair_uid="demo",
        trait_id="quality",
        left_item_id="essay_002",
        right_item_id="essay_001",
        presented_order=["essay_001", "essay_002"],
    )
    configured = (
        RubricOutputContract(traits=selected),
        RubricOutputContract(traits=selected, explanations=True),
        RubricOutputContract(
            traits=selected, explanations=True, output_order="scores_first"
        ),
        RubricOutputContract(
            traits=selected, explanations=True, output_order="explanations_first"
        ),
        PairwiseOutputContract(presentation_version=2, pair=presented),
        PairwiseOutputContract(presentation_version=2, pair=presented, allow_ties=True),
    )
    for specification, example in zip(configured, examples, strict=True):
        assert generate_output_schema(specification) == json.loads(example)
        assert serialize_output_schema(specification) == example.encode("utf-8")


@pytest.mark.parametrize("order", ORDERS)
@pytest.mark.parametrize("representation", ["object", "text", "bytes"])
def test_complete_rubric_validation_returns_existing_typed_output(
    order: Order, representation: str
) -> None:
    data: object = payload(order)
    if representation != "object":
        text = json.dumps(data, ensure_ascii=False)
        data = text if representation == "text" else text.encode("utf-8")
    output = validate_rubric_contract_output(data, contract(order))
    assert isinstance(output, CategoricalOutput)
    assert [(score.trait_id, score.category_label) for score in output.scores] == [
        ("01", "high"),
        ("02", "2"),
    ]
    assert [score.explanation for score in output.scores] == [
        "Synthetic explanation for 01.",
        "Synthetic explanation for 02.",
    ]


@pytest.mark.parametrize("order", ("scores_first", "explanations_first"))
def test_separated_layouts_join_only_exact_ids_and_allow_different_root_order(
    order: Order,
) -> None:
    data = payload(order)
    data["explanations"].reverse()
    # Display order is a requested serialization contract, not a validity predicate.
    data = dict(reversed(tuple(data.items())))
    output = validate_rubric_contract_output(data, contract(order))
    assert [(score.trait_id, score.explanation) for score in output.scores] == [
        ("01", "Synthetic explanation for 01."),
        ("02", "Synthetic explanation for 02."),
    ]
    data["scores"].reverse()
    output = validate_rubric_contract_output(data, contract(order))
    assert [(score.trait_id, score.explanation) for score in output.scores] == [
        ("02", "Synthetic explanation for 02."),
        ("01", "Synthetic explanation for 01."),
    ]


@pytest.mark.parametrize("order", ORDERS)
@pytest.mark.parametrize(
    "failure",
    [
        "missing_score",
        "duplicate_score",
        "extra_score",
        "unattributable_score",
        "missing_trait_id",
        "numeric_trait_id",
        "unknown_trait_id",
        "trait_id_case",
        "missing_category",
        "numeric_category",
        "null_category",
        "boolean_category",
        "unknown_category",
        "category_case",
        "extra_score_field",
        "null_score",
        "wrong_root",
        "extra_root",
        "missing_scores",
        "null_scores",
        "mapping_scores",
        "tuple_scores",
        "empty_scores",
    ],
)
def test_whole_rubric_validation_rejects_invalid_scores(
    order: Order, failure: str
) -> None:
    data: Any = payload(order)
    score = data["scores"][1]
    if failure == "missing_score":
        data["scores"].pop()
    elif failure == "duplicate_score":
        data["scores"].append(copy.deepcopy(score))
    elif failure == "extra_score":
        data["scores"].append({"trait_id": "unexpected", "category_label": "high"})
    elif failure == "unattributable_score":
        data["scores"].append("private unexpected text")
    elif failure == "missing_trait_id":
        score.pop("trait_id")
    elif failure == "numeric_trait_id":
        score["trait_id"] = 2
    elif failure == "unknown_trait_id":
        score["trait_id"] = "03"
    elif failure == "trait_id_case":
        score["trait_id"] = "02 "
    elif failure == "missing_category":
        score.pop("category_label")
    elif failure == "numeric_category":
        score["category_label"] = 2
    elif failure == "null_category":
        score["category_label"] = None
    elif failure == "boolean_category":
        score["category_label"] = True
    elif failure == "unknown_category":
        score["category_label"] = "unknown"
    elif failure == "category_case":
        data["scores"][0]["category_label"] = "HIGH"
    elif failure == "extra_score_field":
        score["confidence"] = 1
    elif failure == "null_score":
        data["scores"][1] = None
    elif failure == "wrong_root":
        data = [data]
    elif failure == "extra_root":
        data["private unexpected key"] = "private unexpected text"
    elif failure == "missing_scores":
        data.pop("scores")
    elif failure == "null_scores":
        data["scores"] = None
    elif failure == "mapping_scores":
        data["scores"] = {"01": data["scores"][0]}
    elif failure == "tuple_scores":
        data["scores"] = tuple(data["scores"])
    else:
        data["scores"] = []
    with pytest.raises(ValueError):
        validate_rubric_contract_output(data, contract(order))


@pytest.mark.parametrize("order", ORDERS)
@pytest.mark.parametrize(
    "failure",
    ["missing", "null", "empty", "whitespace", "number", "boolean", "object", "extra"],
)
def test_valid_category_never_completes_with_invalid_required_explanation(
    order: Order, failure: str
) -> None:
    data = payload(order)
    explanation = data["scores"][0] if order == "together" else data["explanations"][0]
    field = "explanation" if order == "together" else "text"
    invalid: dict[str, object] = {
        "null": None,
        "empty": "",
        "whitespace": " \t\r\n",
        "number": 2,
        "boolean": True,
        "object": {"text": "private"},
    }
    if failure == "missing":
        explanation.pop(field)
    elif failure == "extra":
        explanation["unexpected"] = "private"
    else:
        explanation[field] = invalid[failure]
    with pytest.raises(ValueError):
        validate_rubric_contract_output(data, contract(order))


@pytest.mark.parametrize("order", ("scores_first", "explanations_first"))
@pytest.mark.parametrize(
    "failure",
    [
        "missing_array",
        "null_array",
        "mapping_array",
        "empty_array",
        "missing_member",
        "duplicate_member",
        "unknown_trait",
        "unattributable",
        "numeric_trait",
        "inline_explanation",
        "null_inline_explanation",
        "wrong_text_field",
    ],
)
def test_separated_explanation_correspondence_is_strict(
    order: Order, failure: str
) -> None:
    data = payload(order)
    if failure == "missing_array":
        data.pop("explanations")
    elif failure == "null_array":
        data["explanations"] = None
    elif failure == "mapping_array":
        data["explanations"] = {}
    elif failure == "empty_array":
        data["explanations"] = []
    elif failure == "missing_member":
        data["explanations"].pop()
    elif failure == "duplicate_member":
        # Exact array length cannot guarantee complete unique correspondence.
        data["explanations"][1] = dict(data["explanations"][0])
    elif failure == "unknown_trait":
        data["explanations"][1]["trait_id"] = "unknown"
    elif failure == "unattributable":
        data["explanations"][1] = "private text"
    elif failure == "numeric_trait":
        data["explanations"][1]["trait_id"] = 2
    elif failure == "inline_explanation":
        data["scores"][0]["explanation"] = "Not allowed inline."
    elif failure == "null_inline_explanation":
        data["scores"][0]["explanation"] = None
    else:
        data["explanations"][0]["explanation"] = data["explanations"][0].pop("text")
    with pytest.raises(ValueError):
        validate_rubric_contract_output(data, contract(order))


@pytest.mark.parametrize("value", [None, "Unexpected explanation.", "", " "])
def test_scores_only_forbids_explanation_field_even_explicit_null(
    value: object,
) -> None:
    data = payload(explanations=False)
    data["scores"][0]["explanation"] = value
    with pytest.raises(ValueError):
        validate_rubric_contract_output(data, contract(explanations=False))


def test_scores_only_validates_and_never_fabricates_explanations() -> None:
    output = validate_rubric_contract_output(
        payload(explanations=False), contract(explanations=False)
    )
    assert all(score.explanation is None for score in output.scores)


@pytest.mark.parametrize("order", ORDERS)
def test_layouts_normalize_through_existing_results_with_nonuniform_codes(
    order: Order,
) -> None:
    output = validate_rubric_contract_output(payload(order), contract(order))
    for score, trait in zip(output.scores, traits(), strict=True):
        result = normalize_rubric_result(
            trait,
            run_id="001",
            task_id=f"task-{trait.trait_id}",
            request_uid="request",
            attempt_id="attempt",
            item_id="0001",
            category_label=score.category_label,
            explanation=score.explanation,
        )
        assert result.schema_version == 1
        assert (
            result.primary_score
            == result.numeric_category_value
            == trait.numeric_values[-1]
        )
        assert result.primary_score_kind == "category_code"
        assert (
            result.probabilities
            is result.expected_value
            is result.modal_category_label
            is None
        )
        assert result.explanation == f"Synthetic explanation for {trait.trait_id}."


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "{",
        "[] trailing",
        '{"scores":[]',
        '{"scores":[],"scores":[]}',
        '{"scores":[{"trait_id":"01","trait_id":"02","category_label":"high"}]}',
        '{"winner_id":"001","winner_id":"002"}',
        '{"private provider text":NaN}',
        '{"private provider text":Infinity}',
        '{"private provider text":-Infinity}',
        '{"private provider text":1e9999}',
        '{"private provider text":-1e9999}',
        '{"scores":[{"trait_id":"01","category_label":"high","explanation":"private provider text","explanation":"replacement"}]}',
    ],
)
@pytest.mark.parametrize("as_bytes", [False, True])
def test_strict_raw_json_rejects_duplicates_malformed_and_all_nonfinite_tokens(
    raw: str, as_bytes: bool
) -> None:
    data = raw.encode("utf-8") if as_bytes else raw
    with pytest.raises(ValueError) as error:
        decode_assessment_output(data)
    assert "private provider text" not in str(error.value)


@pytest.mark.parametrize("raw", [b"\xff", b"\xfe\xff\x00{\x00}", b"\xff\xfe{\x00}\x00"])
def test_raw_bytes_must_be_utf8(raw: bytes) -> None:
    with pytest.raises(ValueError):
        decode_assessment_output(raw)


def test_raw_decoding_preserves_exact_unicode_and_object_order() -> None:
    raw = '{"scores":[{"trait_id":"é","category_label":"é","explanation":"  café  "}]}'
    text = decode_assessment_output(raw)
    assert text == decode_assessment_output(raw.encode("utf-8"))
    assert isinstance(text, dict)
    assert list(cast(dict[str, object], text)) == ["scores"]
    unicode_trait = Trait(
        trait_id="é",
        name="Unicode",
        description="",
        levels=[Level(label="é"), Level(label="é")],
    )
    configured = RubricOutputContract(traits=(unicode_trait,), explanations=True)
    output = validate_rubric_contract_output(raw, configured)
    assert output.scores[0].category_label == "é"
    assert output.scores[0].explanation == "  café  "
    assert "é".encode() in serialize_output_schema(configured)
    assert b"\\u00e9" not in serialize_output_schema(configured)


def test_raw_duplicate_protection_cannot_be_recovered_after_ordinary_json_decoding() -> (
    None
):
    raw = '{"scores":[{"trait_id":"01","category_label":"low","category_label":"high"},{"trait_id":"02","category_label":"2"}]}'
    configured = contract(explanations=False)
    with pytest.raises(ValueError):
        validate_rubric_contract_output(raw, configured)
    # Once a caller loses the original key pairs, only the surviving structure can be validated.
    decoded = json.loads(raw)
    assert (
        validate_rubric_contract_output(decoded, configured).scores[0].category_label
        == "high"
    )


@pytest.mark.parametrize(
    "data",
    [
        {"scores": [float("nan")]},
        {"scores": [{"nested": float("inf")}]},
        {"scores": [{1: "private provider text"}]},
        {"scores": [object()]},
        {"scores": [b"private provider text"]},
        {"scores": ({"trait_id": "01", "category_label": "high"},)},
    ],
)
def test_already_decoded_output_rejects_non_json_values(data: object) -> None:
    with pytest.raises(ValueError):
        validate_rubric_contract_output(data, contract(explanations=False))


def test_decoded_output_cycles_fail_cleanly() -> None:
    data: dict[str, Any] = {"scores": []}
    data["scores"].append(data)
    with pytest.raises(ValueError):
        validate_rubric_contract_output(data, contract(explanations=False))


@pytest.mark.parametrize("allow_ties", [False, True])
@pytest.mark.parametrize("explanations", [False, True])
def test_pairwise_schema_uses_exact_presented_orientation_and_closed_fields(
    allow_ties: bool, explanations: bool
) -> None:
    configured = PairwiseOutputContract(
        pair=pair(), allow_ties=allow_ties, explanations=explanations
    )
    schema = generate_output_schema(configured)
    properties = _properties(schema)
    expected_fields = (
        ["winner_id"]
        + (["tie"] if allow_ties else [])
        + (["explanation"] if explanations else [])
    )
    assert list(properties) == schema["required"] == expected_fields
    assert schema["additionalProperties"] is False
    winners = properties["winner_id"]["enum"]
    assert winners == ["002", "001", None] if allow_ties else winners == ["002", "001"]
    if allow_ties:
        assert properties["tie"] == {"type": "boolean"}
        assert len(cast(list[object], schema["oneOf"])) == 2
    else:
        assert "tie" not in properties
    if explanations:
        assert properties["explanation"] == {"type": "string", "minLength": 1}
    assert json.loads(serialize_output_schema(configured)) == schema


@pytest.mark.parametrize("explanations", [False, True])
@pytest.mark.parametrize("legacy_tie", [False, True])
@pytest.mark.parametrize("representation", ["object", "text", "bytes"])
def test_forced_choice_accepts_winner_only_and_legacy_false(
    explanations: bool, legacy_tie: bool, representation: str
) -> None:
    data: Any = {"winner_id": "002"}
    if legacy_tie:
        data["tie"] = False
    if explanations:
        data["explanation"] = "  Synthetic preference.  "
    if representation != "object":
        text = json.dumps(data)
        data = text if representation == "text" else text.encode()
    output = validate_pairwise_contract_output(
        data, PairwiseOutputContract(pair=pair(), explanations=explanations)
    )
    assert isinstance(output, PairwiseOutput)
    assert output.winner_id == "002"
    assert output.tie is False
    assert output.explanation == ("  Synthetic preference.  " if explanations else None)


@pytest.mark.parametrize(
    "data",
    [
        {"winner_id": None, "tie": True},
        {"winner_id": "001", "tie": False},
        {"winner_id": "002", "tie": False},
    ],
)
def test_tie_enabled_contract_accepts_consistent_explicit_outcomes(
    data: dict[str, object],
) -> None:
    output = validate_pairwise_contract_output(
        data, PairwiseOutputContract(pair=pair(), allow_ties=True)
    )
    assert output.winner_id == data["winner_id"]
    assert output.tie is data["tie"]


@pytest.mark.parametrize("allow_ties", [False, True])
@pytest.mark.parametrize(
    "data",
    [
        {},
        {"winner_id": "unknown"},
        {"winner_id": 1},
        {"winner_id": True},
        {"tie": True},
        {"winner_id": None, "tie": False},
        {"winner_id": "001", "tie": True},
        {"winner_id": "001", "tie": None},
        {"winner_id": "001", "tie": "false"},
        {"winner_id": "001", "tie": 0},
        {"winner_id": "001", "tie": 1},
        {"winner_id": "001", "tie": 0.0},
        {"winner_id": "001", "tie": False, "unexpected": "private"},
        {"winner_id": "001", "tie": False, "explanation": None},
        {"winner_id": "001", "tie": False, "explanation": "unrequested"},
    ],
)
def test_pairwise_rejects_invalid_outcomes_and_fields(
    allow_ties: bool, data: dict[str, object]
) -> None:
    with pytest.raises(ValueError):
        validate_pairwise_contract_output(
            data, PairwiseOutputContract(pair=pair(), allow_ties=allow_ties)
        )


@pytest.mark.parametrize(
    "data",
    [{"winner_id": "001"}, {"winner_id": None}, {"winner_id": None, "tie": True}],
)
def test_tie_policy_requires_explicit_tie_and_forced_choice_forbids_true_tie(
    data: dict[str, object],
) -> None:
    allow_ties = data.get("tie") is not True
    with pytest.raises(ValueError):
        validate_pairwise_contract_output(
            data, PairwiseOutputContract(pair=pair(), allow_ties=allow_ties)
        )


@pytest.mark.parametrize("allow_ties", [False, True])
@pytest.mark.parametrize("explanation", [None, "", " \t\n", 2, False, ["text"]])
def test_pairwise_requested_explanation_must_be_nonblank_string(
    allow_ties: bool, explanation: object
) -> None:
    with pytest.raises(ValueError):
        validate_pairwise_contract_output(
            {"winner_id": "001", "tie": False, "explanation": explanation},
            PairwiseOutputContract(
                pair=pair(), allow_ties=allow_ties, explanations=True
            ),
        )


def test_pairwise_missing_explanation_is_invalid_and_input_is_not_mutated() -> None:
    configured = PairwiseOutputContract(pair=pair(), explanations=True)
    with pytest.raises(ValueError):
        validate_pairwise_contract_output({"winner_id": "001"}, configured)
    data = {"winner_id": "001", "explanation": "Synthetic."}
    snapshot = copy.deepcopy(data)
    assert validate_pairwise_contract_output(data, configured).tie is False
    assert data == snapshot


def test_copied_contract_rejects_unsupported_nested_objects_privately() -> None:
    class PrivateObject:
        def __repr__(self) -> str:
            return "PRIVATE_CONTRACT_SENTINEL"

    for configured in (
        contract().model_copy(update={"traits": (PrivateObject(),)}),
        PairwiseOutputContract(pair=pair()).model_copy(
            update={"pair": PrivateObject()}
        ),
    ):
        with pytest.raises(
            ValueError, match="invalid assessment output contract"
        ) as error:
            serialize_output_schema(configured)
        assert "PRIVATE_CONTRACT_SENTINEL" not in str(error.value)


@pytest.mark.parametrize(
    "changes",
    [
        {"traits": ()},
        {"traits": (traits()[0], traits()[0])},
        {"output_order": "scores_first"},
        {"output_order": "explanations_first"},
        {"output_order": "unknown"},
        {"allow_ties": False},
        {"allow_ties": True},
        {"task_kind": "pairwise"},
        {"contract_version": 1},
        {"contract_version": "2"},
        {"contract_version": 2.0},
        {"contract_version": True},
        {"explanations": "true"},
        {"explanations": 1},
        {"schema": {}},
    ],
)
def test_rubric_contract_rejects_incompatible_or_untyped_controls(
    changes: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        RubricOutputContract.model_validate({"traits": traits()} | changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"output_order": "scores_first"},
        {"output_order": "explanations_first"},
        {"task_kind": "rubric"},
        {"traits": traits()},
        {"allow_ties": "true"},
        {"allow_ties": 1},
        {"contract_version": 1},
        {"contract_version": 2.0},
        {"contract_version": "2"},
        {"contract_version": True},
        {"output_schema": {}},
    ],
)
def test_pairwise_contract_rejects_incompatible_or_untyped_controls(
    changes: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        PairwiseOutputContract.model_validate({"pair": pair()} | changes)


@pytest.mark.parametrize(
    "mutation",
    [
        "unknown",
        "order",
        "version",
        "duplicate",
        "nested_trait",
        "nested_level",
        "numeric_label",
    ],
)
def test_unsafe_copied_rubric_contract_cannot_bypass_public_boundaries(
    mutation: str,
) -> None:
    configured = contract()
    if mutation == "unknown":
        configured = configured.model_copy(
            update={"arbitrary_schema": {"private": True}}
        )
    elif mutation == "order":
        configured = configured.model_copy(update={"output_order": "unknown"})
    elif mutation == "version":
        configured = configured.model_copy(update={"contract_version": 1})
    elif mutation == "duplicate":
        configured = configured.model_copy(
            update={"traits": (traits()[0], traits()[0])}
        )
    elif mutation == "nested_trait":
        configured.traits[0].levels.clear()
    elif mutation == "numeric_label":
        configured.traits[0].levels[0] = (
            configured.traits[0]
            .levels[0]
            .model_copy(update={"label": 42, "source_label": 42})
        )
    else:
        configured.traits[0].levels[0].numeric_value = float("nan")
    with pytest.raises(ValueError):
        generate_output_schema(configured)
    with pytest.raises(ValueError):
        serialize_output_schema(configured)
    with pytest.raises(ValueError):
        validate_rubric_contract_output(payload(), configured)


@pytest.mark.parametrize(
    "mutation",
    ["unknown", "order", "nested_pair", "tie_type", "cleared_order", "missing_order"],
)
def test_unsafe_copied_pairwise_contract_cannot_bypass_public_boundaries(
    mutation: str,
) -> None:
    configured = PairwiseOutputContract(pair=pair())
    if mutation == "unknown":
        configured = configured.model_copy(update={"arbitrary_schema": {}})
    elif mutation == "order":
        configured = configured.model_copy(update={"output_order": "scores_first"})
    elif mutation == "nested_pair":
        configured.pair.presented_order[:] = ["unknown", "001"]
    elif mutation == "cleared_order":
        configured.pair.presented_order.clear()
    elif mutation == "missing_order":
        cast(dict[str, object], configured.pair.__dict__).pop("presented_order")
    else:
        configured = configured.model_copy(update={"allow_ties": "false"})
    with pytest.raises(ValueError):
        generate_output_schema(configured)
    with pytest.raises(ValueError):
        serialize_output_schema(configured)
    with pytest.raises(ValueError):
        validate_pairwise_contract_output({"winner_id": "001"}, configured)


def test_schema_kind_cannot_be_swapped_between_assessments() -> None:
    with pytest.raises(ValueError):
        validate_rubric_contract_output(payload(), PairwiseOutputContract(pair=pair()))  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        validate_pairwise_contract_output({"winner_id": "001"}, contract())  # type: ignore[arg-type]


def test_large_synthetic_group_preserves_all_members_and_categories() -> None:
    selected = tuple(
        Trait(
            trait_id=f"{index:04d}",
            name=f"Trait {index}",
            description="Bounded synthetic group.",
            levels=[
                Level(label=str(value), numeric_value=value * value + value)
                for value in range(17)
            ],
        )
        for index in range(257)
    )
    configured = RubricOutputContract(
        traits=selected, output_order="scores_first", explanations=True
    )
    schema = generate_output_schema(configured)
    choices = _properties(schema)["scores"]["items"]["oneOf"]
    assert len(choices) == 257
    assert all(
        len(choice["properties"]["category_label"]["enum"]) == 17 for choice in choices
    )
    data = {
        "scores": [
            {"trait_id": trait.trait_id, "category_label": "16"} for trait in selected
        ],
        "explanations": [
            {"trait_id": trait.trait_id, "text": "Synthetic."}
            for trait in reversed(selected)
        ],
    }
    output = validate_rubric_contract_output(data, configured)
    assert [score.trait_id for score in output.scores] == [
        trait.trait_id for trait in selected
    ]
    assert len(output.scores) == 257
