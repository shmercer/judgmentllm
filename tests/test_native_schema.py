"""Synthetic native-schema foundations; no SDK, credentials or transport required."""

from __future__ import annotations

import copy
import json
import re
import warnings
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import JsonValue, ValidationError

from judgmentllm.models import Level, PairTask, Trait
from judgmentllm.native_schema import (
    NativePairwiseSourceV1,
    NativeProjectionDiagnosticV1,
    NativeProjectionError,
    NativeProjectionScopeV1,
    NativeRubricSourceV1,
    NativeSchemaProjectionV1,
    NativeTraitMembershipV1,
    projection_digest,
    source_contract,
)
from judgmentllm.output_contracts import (
    AssessmentOutputContract,
    OutputOrder,
    PairwiseOutputContract,
    RubricOutputContract,
    generate_output_schema,
    serialize_output_schema,
    validate_pairwise_contract_output,
    validate_rubric_contract_output,
)
from judgmentllm.providers import openai_schema
from judgmentllm.providers.openai_schema import project_openai_schema

FIXTURES = Path(__file__).parent / "fixtures" / "native_openai"


def scope(endpoint: object = "responses", **changes: object) -> NativeProjectionScopeV1:
    return NativeProjectionScopeV1.model_validate(
        {
            "provider": "openai",
            "endpoint": endpoint,
            "model": "synthetic-model",
            "model_scope": "exact",
            "sdk_package": "openai",
            "sdk_version": "3.42.1",
            **changes,
        }
    )


def traits() -> tuple[Trait, ...]:
    return (
        Trait(
            trait_id="001",
            name="Private name",
            description="Private description",
            levels=[Level(label="low"), Level(label="high")],
        ),
        Trait(
            trait_id='é"\\',
            name="Other private name",
            description="Other private description",
            levels=[
                Level(label='quote "'),
                Level(label="slash \\"),
                Level(label="é\n雪"),
            ],
        ),
    )


def rubric(
    order: OutputOrder = "together", *, explanations: bool = False, single: bool = False
) -> RubricOutputContract:
    return RubricOutputContract(
        traits=traits()[:1] if single else traits(),
        explanations=explanations,
        output_order=order,
    )


def pairwise(
    *, allow_ties: bool = False, explanations: bool = False
) -> PairwiseOutputContract:
    return PairwiseOutputContract(
        pair=PairTask(
            pair_id="pair-01",
            pair_uid="occurrence-01",
            trait_id="preference",
            left_item_id="001",
            right_item_id="002",
            presented_order=["002", "001"],
            metadata={"private": "not schema content"},
        ),
        allow_ties=allow_ties,
        explanations=explanations,
    )


def cases() -> dict[str, AssessmentOutputContract]:
    output: dict[str, AssessmentOutputContract] = {}
    for single in (False, True):
        label = "single" if single else "grouped"
        output[f"{label}_scores"] = rubric(single=single)
        for order in ("together", "scores_first", "explanations_first"):
            output[f"{label}_{order}"] = rubric(order, explanations=True, single=single)
    for ties in (False, True):
        for explain in (False, True):
            output[f"pairwise_ties_{ties}_explanations_{explain}"] = pairwise(
                allow_ties=ties, explanations=explain
            )
    return output


def assert_schema_value(value: Any, schema: dict[str, Any]) -> None:
    """Check only the emitted core JSON-schema vocabulary in synthetic fixtures."""
    if "anyOf" in schema:
        accepted = 0
        for alternative in schema["anyOf"]:
            try:
                assert_schema_value(value, alternative)
            except AssertionError:
                continue
            accepted += 1
        assert accepted > 0
    if "enum" in schema:
        assert value in schema["enum"]
    types = schema.get("type")
    if types is not None:
        if isinstance(types, str):
            types = [types]
        actual = (
            "null"
            if value is None
            else "boolean"
            if type(value) is bool
            else "string"
            if type(value) is str
            else "array"
            if type(value) is list
            else "object"
            if type(value) is dict
            else "number"
        )
        assert actual in types
    if isinstance(value, dict) and "properties" in schema:
        value = cast(dict[str, Any], value)
        assert schema["additionalProperties"] is False
        assert set(value) == set(schema["required"]) == set(schema["properties"])
        for key, item in value.items():
            assert_schema_value(item, schema["properties"][key])
    if isinstance(value, list) and "items" in schema:
        for item in cast(list[Any], value):
            assert_schema_value(item, schema["items"])


def walk_schema(schema: dict[str, Any]) -> None:
    assert set(schema) <= {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "anyOf",
        "enum",
    }
    if schema.get("type") == "object":
        assert schema["additionalProperties"] is False
        assert schema["required"] == list(schema["properties"])
        for child in schema["properties"].values():
            walk_schema(child)
    if "items" in schema:
        walk_schema(schema["items"])
    for child in schema.get("anyOf", []):
        walk_schema(child)


@pytest.mark.parametrize("endpoint", ["responses", "chat.completions"])
def test_exact_fixtures_all_layouts_pairwise_wrappers_and_order(endpoint: str) -> None:
    fixture = json.loads((FIXTURES / f"{endpoint}.json").read_text(encoding="utf-8"))
    for name, contract in cases().items():
        before = serialize_output_schema(contract)
        projection = project_openai_schema(contract, scope=scope(endpoint))
        assert (
            projection.neutral_schema_text.encode()
            == before
            == serialize_output_schema(contract)
        )
        assert projection.native_schema_text == fixture[name]["native_schema_text"]
        assert projection.native_schema_sha256 == fixture[name]["native_schema_sha256"]
        assert projection.format_fragment_text == fixture[name]["format_fragment_text"]
        assert (
            projection.format_fragment_sha256 == fixture[name]["format_fragment_sha256"]
        )
        assert projection.projection_sha256 == fixture[name]["projection_sha256"]
        assert projection == project_openai_schema(contract, scope=scope(endpoint))
        assert projection == NativeSchemaProjectionV1.model_validate_json(
            projection.model_dump_json()
        )
        assert re.fullmatch(r"[A-Za-z0-9_-]{1,64}", projection.schema_name)
        schema = json.loads(projection.native_schema_text)
        assert schema["type"] == "object"
        assert "oneOf" not in schema and "anyOf" not in schema
        assert tuple(schema["properties"]) == projection.root_properties
        walk_schema(schema)
        fragment = json.loads(projection.format_fragment_text)
        fmt = (
            fragment["text"]["format"]
            if endpoint == "responses"
            else fragment["response_format"]["json_schema"]
        )
        assert fmt["strict"] is True and fmt["schema"] == schema
        if isinstance(contract, RubricOutputContract):
            scores = schema["properties"]["scores"]
            assert "minItems" not in scores and "maxItems" not in scores
            for trait, branch in zip(
                contract.traits, scores["items"]["anyOf"], strict=True
            ):
                assert branch["properties"]["trait_id"] == {
                    "type": "string",
                    "enum": [trait.trait_id],
                }
                assert branch["properties"]["category_label"] == {
                    "type": "string",
                    "enum": [level.label for level in trait.levels],
                }
            assert [
                member.trait_id
                for member in projection.member_properties
                if member.array == "scores"
            ] == [trait.trait_id for trait in contract.traits]
            assert all(
                tuple(branch["properties"]) == member.properties
                for branch, member in zip(
                    scores["items"]["anyOf"], projection.member_properties, strict=False
                )
            )
        else:
            assert schema["properties"]["winner_id"]["enum"] == [
                *contract.pair.presented_order,
                *([None] if contract.allow_ties else []),
            ]
            assert list(schema["properties"]) == ["winner_id"] + (
                ["tie"] if contract.allow_ties else []
            ) + (["explanation"] if contract.explanations else [])


def test_fixture_profile_and_documentation_hashes_are_exact() -> None:
    assert (
        sha256((FIXTURES / "profile.json").read_bytes()).hexdigest()
        == openai_schema.OPENAI_FIXTURE_PROFILE_SHA256
    )
    assert (
        sha256((FIXTURES / "documentation.json").read_bytes()).hexdigest()
        == openai_schema.OPENAI_DOCUMENTATION_SHA256
    )
    profile = json.loads((FIXTURES / "profile.json").read_text())
    assert profile["sdk_version"] == openai_schema.OPENAI_SDK_VERSION
    assert (
        profile["model_acceptance"] == "unknown" and profile["live_qualified"] is False
    )


@pytest.mark.parametrize("order", ["together", "scores_first", "explanations_first"])
def test_weaker_native_rubric_keeps_all_local_constraints(order: OutputOrder) -> None:
    contract = rubric(order, explanations=True)
    projection = project_openai_schema(contract, scope=scope())
    schema = json.loads(projection.native_schema_text)
    inline = order == "together"
    scores = [
        {
            "trait_id": trait.trait_id,
            "category_label": trait.levels[0].label,
            **({"explanation": "Synthetic."} if inline else {}),
        }
        for trait in contract.traits
    ]
    valid = {"scores": scores}
    if not inline:
        valid["explanations"] = [
            {"trait_id": trait.trait_id, "text": "Synthetic."}
            for trait in reversed(contract.traits)
        ]
    assert_schema_value(valid, schema)
    validate_rubric_contract_output(valid, contract)
    wrong: list[dict[str, Any]] = []
    duplicate = copy.deepcopy(valid)
    duplicate["scores"][1] = copy.deepcopy(duplicate["scores"][0])
    wrong.append(duplicate)
    short = copy.deepcopy(valid)
    short["scores"].pop()
    wrong.append(short)
    empty = copy.deepcopy(valid)
    empty["scores"] = []
    wrong.append(empty)
    blank = copy.deepcopy(valid)
    blank["scores" if inline else "explanations"][0][
        "explanation" if inline else "text"
    ] = " \t\n"
    wrong.append(blank)
    if not inline:
        missing = copy.deepcopy(valid)
        missing["explanations"].pop()
        wrong.append(missing)
        repeated = copy.deepcopy(valid)
        repeated["explanations"][1] = copy.deepcopy(repeated["explanations"][0])
        wrong.append(repeated)
    for output in wrong:
        assert_schema_value(output, schema)
        with pytest.raises(ValueError):
            validate_rubric_contract_output(output, contract)
    residual = {entry.constraint for entry in projection.local_constraints}
    assert {
        "array_cardinality",
        "trait_uniqueness_completeness",
        "nonblank_explanation",
        "unique_json_keys",
    } <= residual
    if not inline:
        assert "explanation_join" in residual


@pytest.mark.parametrize("explanations", [False, True])
def test_root_pairwise_composition_is_removed_only_with_local_relation(
    explanations: bool,
) -> None:
    contract = pairwise(allow_ties=True, explanations=explanations)
    assert "oneOf" in generate_output_schema(contract)
    projection = project_openai_schema(contract, scope=scope())
    schema = json.loads(projection.native_schema_text)
    invalid_outputs: list[dict[str, Any]] = [
        {"winner_id": None, "tie": False},
        {"winner_id": "001", "tie": True},
    ]
    for output in invalid_outputs:
        if explanations:
            output["explanation"] = "Synthetic."
        assert_schema_value(output, schema)
        with pytest.raises(ValueError):
            validate_pairwise_contract_output(output, contract)
    valid_outputs: list[dict[str, Any]] = [
        {"winner_id": None, "tie": True},
        {"winner_id": "002", "tie": False},
    ]
    for output in valid_outputs:
        if explanations:
            output["explanation"] = "Synthetic."
        assert_schema_value(output, schema)
        validate_pairwise_contract_output(output, contract)
    assert any(
        entry.constraint == "winner_tie_relation"
        and entry.operation == "omit_root_relation"
        for entry in projection.local_constraints
    )


def test_forced_choice_rejects_wrong_category_and_winner_without_dropping_ids() -> None:
    configured = pairwise()
    projection = project_openai_schema(configured, scope=scope())
    schema = json.loads(projection.native_schema_text)
    for output in (
        {"winner_id": "unknown"},
        {"winner_id": None},
        {"winner_id": "001", "tie": False},
    ):
        with pytest.raises(AssertionError):
            assert_schema_value(output, schema)
    contract = rubric()
    schema = json.loads(
        project_openai_schema(contract, scope=scope()).native_schema_text
    )
    with pytest.raises(AssertionError):
        assert_schema_value(
            {"scores": [{"trait_id": "001", "category_label": 'quote "'}]}, schema
        )


def test_source_is_detached_does_not_capture_names_descriptions_metadata_or_codes() -> (
    None
):
    contract = rubric()
    projection = project_openai_schema(contract, scope=scope())
    original_text = projection.model_dump_json()
    assert "Private" not in original_text
    contract.traits[0].levels.clear()
    contract.traits[0].name = "changed"
    assert projection.model_dump_json() == original_text
    assert NativeSchemaProjectionV1.model_validate_json(original_text) == projection
    pair = pairwise()
    projected = project_openai_schema(pair, scope=scope())
    assert "not schema content" not in projected.model_dump_json()
    pair.pair.presented_order.reverse()
    assert (
        projected.source.presented_order == ("002", "001")
        if isinstance(projected.source, NativePairwiseSourceV1)
        else False
    )


def test_numeric_looking_categories_keep_exact_order_and_never_emit_warnings() -> None:
    selected = Trait(
        trait_id="001",
        name="",
        description="",
        levels=[Level(label="2", numeric_value=0), Level(label="1", numeric_value=1)],
    )
    configured = RubricOutputContract(traits=(selected,))
    projection = project_openai_schema(configured, scope=scope())
    assert '"enum":["2","1"]' in projection.native_schema_text
    mixed = Trait(
        trait_id="private-canary",
        name="",
        description="",
        levels=[Level(label="low", numeric_value=0), Level(label="2", numeric_value=1)],
    )
    mixed_contract = RubricOutputContract(traits=(mixed,))
    mixed.levels[0].numeric_value = mixed.levels[1].numeric_value = None
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        project_openai_schema(mixed_contract, scope=scope())
    assert not caught


def test_projection_identity_includes_exact_bytes_partition_order_and_scope() -> None:
    contract = rubric("scores_first", explanations=True)
    first = project_openai_schema(contract, scope=scope())
    alternatives: list[AssessmentOutputContract] = [
        rubric("explanations_first", explanations=True),
        rubric(),
        rubric(single=True),
        RubricOutputContract(
            traits=tuple(reversed(contract.traits)),
            output_order="scores_first",
            explanations=True,
        ),
    ]
    changed = rubric("scores_first", explanations=True)
    changed.traits[0].levels[0].label = "other"
    changed.traits[0].levels[0].source_label = "other"
    alternatives.append(changed)
    assert all(
        project_openai_schema(value, scope=scope()).projection_sha256
        != first.projection_sha256
        for value in alternatives
    )
    assert (
        project_openai_schema(
            contract, scope=scope(model="other-synthetic")
        ).projection_sha256
        != first.projection_sha256
    )
    assert (
        project_openai_schema(
            contract, scope=scope("chat.completions")
        ).projection_sha256
        != first.projection_sha256
    )
    unresolved = project_openai_schema(
        contract, scope=scope(model=None, model_scope="unresolved")
    )
    assert unresolved.scope.model_scope == "unresolved"
    assert unresolved.projection_sha256 != first.projection_sha256
    # Original orientation is committed even when presented winners/schema are unchanged.
    left = pairwise()
    right = pairwise()
    right.pair.left_item_id, right.pair.right_item_id = "002", "001"
    right.pair.original_order = ["002", "001"]
    pleft = project_openai_schema(left, scope=scope())
    pright = project_openai_schema(right, scope=scope())
    assert pleft.native_schema_sha256 == pright.native_schema_sha256
    assert pleft.source_sha256 != pright.source_sha256
    assert pleft.projection_sha256 != pright.projection_sha256


@pytest.mark.parametrize(
    "field",
    [
        "neutral_schema_text",
        "neutral_schema_sha256",
        "native_schema_text",
        "native_schema_sha256",
        "format_fragment_text",
        "format_fragment_sha256",
        "projection_sha256",
        "schema_name",
        "mechanism",
        "schema_dialect",
        "root_properties",
        "member_properties",
        "native_constraints",
        "local_constraints",
        "provenance",
        "source_sha256",
    ],
)
def test_tamper_detected_even_if_identity_is_recomputed(field: str) -> None:
    projection = project_openai_schema(
        rubric("scores_first", explanations=True), scope=scope()
    )
    data = projection.model_dump()
    if field.endswith("sha256"):
        data[field] = "0" * 64
    elif field in {
        "root_properties",
        "member_properties",
        "native_constraints",
        "local_constraints",
        "provenance",
    }:
        data[field] = tuple(reversed(data[field])) if len(data[field]) > 1 else ()
    else:
        data[field] = "sensitive-canary"
    with pytest.raises(ValueError) as error:
        NativeSchemaProjectionV1.model_validate(data)
    assert "sensitive-canary" not in str(error.value)
    if field != "projection_sha256":
        # Rehashing arbitrary changes cannot manufacture a projection commitment.
        from judgmentllm.persistence.hashing import canonical_bytes

        data_json = json.loads(json.dumps(data, default=str))
        data_json["projection_sha256"] = sha256(
            canonical_bytes(
                {
                    key: value
                    for key, value in data_json.items()
                    if key != "projection_sha256"
                }
            )
        ).hexdigest()
        with pytest.raises(ValueError):
            NativeSchemaProjectionV1.model_validate_json(json.dumps(data_json))


@pytest.mark.parametrize(
    "field",
    [
        "schema_version",
        "projection_version",
        "implementation_version",
        "source_contract_version",
    ],
)
@pytest.mark.parametrize("value", [True, 1.0, "1", 2.0, "2", 0, 3])
def test_version_fields_are_strict_including_bool_float(
    field: str, value: object
) -> None:
    projected = project_openai_schema(rubric(), scope=scope())
    with pytest.raises(ValueError):
        projected.model_copy(update={field: value})


@pytest.mark.parametrize("value", [True, 2.0, "2", 1, 3])
def test_source_versions_and_unknown_fields_are_strict(value: object) -> None:
    for source in (
        project_openai_schema(rubric(), scope=scope()).source,
        project_openai_schema(pairwise(), scope=scope()).source,
    ):
        with pytest.raises(ValueError):
            source.model_copy(update={"contract_version": value})
        with pytest.raises(ValueError):
            source.model_copy(update={"private-canary": "secret"})


def test_model_construction_copy_and_mutation_do_not_bypass_commitment() -> None:
    projection = project_openai_schema(rubric(), scope=scope())
    assert projection.model_copy(deep=True) == projection
    with pytest.raises(ValueError):
        projection.scope.endpoint = "changed"
    with pytest.raises(ValueError):
        NativeSchemaProjectionV1.model_construct(
            _fields_set=None,
            **cast(
                dict[str, Any],
                {**projection.model_dump(), "native_schema_text": "changed"},
            ),
        )
    with pytest.raises(ValueError):
        NativeSchemaProjectionV1.model_validate(
            {**projection.model_dump(), "private-canary": "secret"}
        )
    assert isinstance(projection.source, NativeRubricSourceV1)
    with pytest.raises(ValueError):
        projection.source.traits[0].category_labels = ("changed", "other")
    assert projection_digest(projection) == projection.projection_sha256


@pytest.mark.parametrize(
    "changes",
    [
        {"provider": "other"},
        {"endpoint": "chat"},
        {"sdk_version": "3.42.2"},
        {"sdk_package": "other"},
        {"sdk_package": None, "sdk_version": None},
        {"api_version": "v2"},
    ],
)
def test_unsupported_scope_is_a_safe_structured_incompatibility(
    changes: dict[str, object],
) -> None:
    with pytest.raises(NativeProjectionError) as error:
        project_openai_schema(rubric(), scope=scope(**changes))
    assert error.value.reason == "unsupported_scope"
    assert error.value.diagnostic.reason == "unsupported_scope"


@pytest.mark.parametrize(
    "changes",
    [
        {"model": None},
        {"model_scope": "unresolved"},
        {"sdk_version": None},
        {"sdk_package": None},
        {"schema": {}},
        {"sdk_version": "private secret"},
    ],
)
def test_invalid_scope_cannot_claim_consistent_identity(
    changes: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        scope(**changes)


def test_safe_diagnostic_contract_rejects_unknown_or_inconsistent_fields() -> None:
    with pytest.raises(ValueError):
        NativeProjectionDiagnosticV1(reason="invalid_contract", limit="properties")
    with pytest.raises(ValueError):
        NativeProjectionDiagnosticV1(reason="schema_limit_exceeded")
    assert (
        NativeProjectionDiagnosticV1(reason="unsupported_schema").model_dump()["limit"]
        is None
    )


@pytest.mark.parametrize(
    "mutate",
    [
        "extra",
        "root_composition",
        "required",
        "closure",
        "trait_const",
        "category",
        "nested_keyword",
        "array_bound",
        "scores_type",
        "choices",
        "explanation",
        "separated",
        "pairwise_relation",
        "pairwise_winner",
        "pairwise_explanation",
    ],
)
def test_unreviewed_schema_constructs_are_rejected(
    monkeypatch: pytest.MonkeyPatch, mutate: str
) -> None:
    configured: AssessmentOutputContract = (
        pairwise(allow_ties=True, explanations=True)
        if mutate.startswith("pairwise")
        else rubric(
            "scores_first" if mutate == "separated" else "together", explanations=True
        )
    )
    original = openai_schema.generate_output_schema

    def changed(contract: AssessmentOutputContract) -> dict[str, JsonValue]:
        schema: dict[str, Any] = original(contract)
        props = schema["properties"]
        if mutate == "extra":
            schema["private-canary"] = "sensitive"
        elif mutate == "root_composition":
            schema["anyOf"] = [{"type": "object"}]
        elif mutate == "required":
            schema["required"] = []
        elif mutate == "closure":
            schema["additionalProperties"] = True
        elif mutate == "trait_const":
            props["scores"]["items"]["oneOf"][0]["properties"]["trait_id"] = {
                "type": "string"
            }
        elif mutate == "category":
            props["scores"]["items"]["oneOf"][0]["properties"]["category_label"][
                "enum"
            ].pop()
        elif mutate == "nested_keyword":
            props["scores"]["items"]["oneOf"][0]["properties"]["category_label"][
                "pattern"
            ] = ".*"
        elif mutate == "array_bound":
            props["scores"]["maxItems"] = 1
        elif mutate == "scores_type":
            props["scores"] = {"type": "string"}
        elif mutate == "choices":
            props["scores"]["items"]["oneOf"] = []
        elif mutate == "explanation":
            props["scores"]["items"]["oneOf"][0]["properties"]["explanation"] = {
                "type": "number"
            }
        elif mutate == "separated":
            props["explanations"]["items"]["properties"]["text"] = {"type": "number"}
        elif mutate == "pairwise_relation":
            schema["oneOf"][0]["properties"]["tie"] = {"const": False}
        elif mutate == "pairwise_winner":
            props["winner_id"]["enum"] = ["other"]
        else:
            props["explanation"] = {"type": "number"}
        return schema

    monkeypatch.setattr(openai_schema, "generate_output_schema", changed)

    def changed_bytes(contract: AssessmentOutputContract) -> bytes:
        return openai_schema.schema_text(changed(contract)).encode("utf-8")

    monkeypatch.setattr(openai_schema, "serialize_output_schema", changed_bytes)
    with pytest.raises(NativeProjectionError) as error:
        project_openai_schema(configured, scope=scope())
    assert error.value.reason == "unsupported_schema"
    assert "private-canary" not in str(error.value)


def test_invalid_contract_models_and_source_shape_reject_cleanly() -> None:
    for configured in (
        rubric().model_copy(update={"traits": []}),
        rubric().model_copy(update={"arbitrary": "sensitive"}),
        pairwise().model_copy(update={"output_order": "scores_first"}),
        cast(AssessmentOutputContract, object()),
    ):
        with pytest.raises(NativeProjectionError) as error:
            project_openai_schema(configured, scope=scope())
        assert error.value.reason == "invalid_contract"
    with pytest.raises(ValueError):
        NativeRubricSourceV1(
            traits=(
                NativeTraitMembershipV1(
                    trait_id="001", category_labels=("same", "same")
                ),
            )
        )
    projected = project_openai_schema(pairwise(), scope=scope())
    assert isinstance(projected.source, NativePairwiseSourceV1)
    with pytest.raises(ValueError):
        projected.source.model_copy(update={"presented_order": ("001", "unknown")})
    assert serialize_output_schema(
        source_contract(projected.source)
    ) == serialize_output_schema(pairwise())


@pytest.mark.parametrize(
    "limit",
    ["properties", "nesting", "string_characters", "enum_values", "enum_characters"],
)
def test_documented_general_limits_with_exact_boundaries(limit: str) -> None:
    # Private scanner fixtures test counting; these are not model acceptance claims.
    valid: dict[str, JsonValue]
    invalid: dict[str, JsonValue]
    if limit == "properties":
        valid = {"properties": {f"p{x}": {"type": "string"} for x in range(5000)}}
        invalid = {"properties": {f"p{x}": {"type": "string"} for x in range(5001)}}
    elif limit == "nesting":

        def depth(count: int) -> dict[str, JsonValue]:
            node: dict[str, JsonValue] = {"type": "string"}
            for _ in range(count):
                node = {"type": "array", "items": node}
            return node

        valid, invalid = depth(10), depth(11)
    elif limit == "string_characters":
        # Characters, not UTF-8 bytes: 120000 snow characters occupy 360000 bytes.
        valid, invalid = {"enum": ["雪" * 120000]}, {"enum": ["雪" * 120001]}
    elif limit == "enum_values":
        valid, invalid = (
            {"enum": [str(x) for x in range(1000)]},
            {"enum": [str(x) for x in range(1001)]},
        )
    else:
        valid, invalid = (
            {"enum": [f"{index:03d}" + "x" * 47 for index in range(300)]},
            {"enum": [f"{index:03d}" + "x" * 47 for index in range(301)]},
        )
    openai_schema._limits(valid)  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(NativeProjectionError) as error:
        openai_schema._limits(invalid)  # pyright: ignore[reportPrivateUsage]
    assert error.value.diagnostic.limit == limit


def test_projection_rejects_large_membership_and_categories_without_truncation() -> (
    None
):
    configured = rubric(single=True)
    expanded = tuple(
        Trait(
            trait_id=f"trait-{x}",
            name="",
            description="",
            levels=[Level(label="low"), Level(label="high")],
        )
        for x in range(334)
    )
    with pytest.raises(NativeProjectionError) as error:
        project_openai_schema(RubricOutputContract(traits=expanded), scope=scope())
    assert error.value.diagnostic.limit == "enum_values"
    large = configured.model_copy(update={"traits": configured.traits * 5001})
    with pytest.raises(NativeProjectionError) as error:
        project_openai_schema(large, scope=scope())
    assert error.value.diagnostic.limit == "properties"
    long_label = rubric(single=True)
    long_label.traits[0].levels[0] = Level(label="x" * 120001)
    with pytest.raises(NativeProjectionError) as error:
        project_openai_schema(long_label, scope=scope())
    assert error.value.diagnostic.limit == "string_characters"
    assert len(expanded) == 334
    assert len(long_label.traits[0].levels[0].label) == 120001


@pytest.mark.parametrize(
    "field",
    [
        "schema_version",
        "projection_version",
        "implementation_version",
        "source_contract_version",
    ],
)
@pytest.mark.parametrize("value", [True, 1.0, 2.0, "1", "2"])
def test_json_projection_versions_reject_original_noninteger_tokens(
    field: str, value: object
) -> None:
    projection = project_openai_schema(rubric(), scope=scope())
    data = projection.model_dump(mode="json")
    data[field] = value
    with pytest.raises(ValueError):
        NativeSchemaProjectionV1.model_validate_json(json.dumps(data))


@pytest.mark.parametrize("value", [True, 2.0, "2"])
def test_json_source_versions_reject_original_noninteger_tokens(value: object) -> None:
    for source in (
        project_openai_schema(rubric(), scope=scope()).source,
        project_openai_schema(pairwise(), scope=scope()).source,
    ):
        data = source.model_dump(mode="json")
        data["contract_version"] = value
        with pytest.raises(ValueError):
            type(source).model_validate_json(json.dumps(data))


def test_large_enum_conditional_250_251_and_aggregate_character_limit() -> None:
    # An enum with <=250 values may exceed 15000 characters within total ceiling.
    two_fifty: list[JsonValue] = [f"{index:03d}" + "x" * 60 for index in range(250)]
    openai_schema._limits({"enum": two_fifty})  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(NativeProjectionError) as error:
        openai_schema._limits({"enum": [*two_fifty, "another-value"]})  # pyright: ignore[reportPrivateUsage]
    assert error.value.diagnostic.limit == "enum_characters"
    exact: dict[str, JsonValue] = {
        "type": "object",
        "properties": {"p" * 20: {"type": "string", "enum": ["雪" * 119980]}},
    }
    openai_schema._limits(exact)  # pyright: ignore[reportPrivateUsage]
    over: dict[str, JsonValue] = {
        "type": "object",
        "properties": {"p" * 21: {"type": "string", "enum": ["雪" * 119980]}},
    }
    with pytest.raises(NativeProjectionError) as error:
        openai_schema._limits(over)  # pyright: ignore[reportPrivateUsage]
    assert error.value.diagnostic.limit == "string_characters"


def test_pairwise_large_winner_ids_are_rejected_before_canonical_expansion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    contract = PairwiseOutputContract(
        pair=PairTask(
            pair_id="pair",
            pair_uid="pair",
            trait_id="trait",
            left_item_id="x" * 120001,
            right_item_id="002",
        )
    )

    def forbidden(contract: AssessmentOutputContract) -> bytes:
        pytest.fail("canonical expansion must not run after budget rejection")

    monkeypatch.setattr(openai_schema, "serialize_output_schema", forbidden)
    with pytest.raises(NativeProjectionError) as error:
        project_openai_schema(contract, scope=scope())
    assert error.value.diagnostic.limit == "string_characters"


def duplicate_json_member(
    record: dict[str, Any], path: tuple[str | int, ...], key: str, first: object
) -> str:
    """Insert a duplicate while preserving the original member as the last value."""
    target: Any = record
    for component in path:
        target = target[component]
    target_text = json.dumps(target, ensure_ascii=False, separators=(",", ":"))
    duplicate = (
        "{"
        + json.dumps(key, ensure_ascii=False)
        + ":"
        + json.dumps(first, ensure_ascii=False, separators=(",", ":"))
        + ","
        + target_text[1:]
    )
    document = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    assert target_text in document
    raw = document.replace(target_text, duplicate, 1)
    # Last-key-wins decoding would reproduce every original value and hash.
    assert json.loads(raw) == record
    return raw


@pytest.mark.parametrize(
    ("path", "key"),
    [
        ((), "projection_version"),
        ((), "native_schema_sha256"),
        ((), "projection_sha256"),
        (("scope",), "provider"),
        (("scope",), "endpoint"),
        (("scope",), "model"),
        (("scope",), "model_scope"),
        (("scope",), "sdk_package"),
        (("scope",), "sdk_version"),
        (("scope",), "api_version"),
        (("source",), "contract_version"),
        (("source", "traits", 0), "trait_id"),
        (("source", "traits", 0), "category_labels"),
        (("member_properties", 0), "trait_id"),
        (("native_constraints", 0), "constraint"),
        (("provenance", 0), "source_sha256"),
    ],
)
@pytest.mark.parametrize("identical", [False, True])
@pytest.mark.parametrize("input_type", [str, bytes, bytearray])
def test_projection_json_rejects_duplicate_members_before_pair_evidence_is_lost(
    path: tuple[str | int, ...],
    key: str,
    identical: bool,
    input_type: type[str] | type[bytes] | type[bytearray],
) -> None:
    projection = project_openai_schema(
        rubric("scores_first", explanations=True), scope=scope()
    )
    record = projection.model_dump(mode="json")
    target: Any = record
    for component in path:
        target = target[component]
    first = target[key] if identical else "private-duplicate-key-canary"
    raw = duplicate_json_member(record, path, key, first)
    data = raw if input_type is str else input_type(raw.encode("utf-8"))
    with pytest.raises(ValidationError) as error:
        NativeSchemaProjectionV1.model_validate_json(data)
    diagnostic = str(error.value) + repr(error.value.errors()) + error.value.json()
    assert "private-duplicate-key-canary" not in diagnostic
    assert key not in diagnostic
    assert error.value.errors()[0]["type"] == "evidence_invalid"


@pytest.mark.parametrize(
    "key",
    [
        "provider",
        "endpoint",
        "model",
        "model_scope",
        "sdk_package",
        "sdk_version",
        "api_version",
    ],
)
@pytest.mark.parametrize("identical", [False, True])
def test_native_scope_json_rejects_direct_duplicate_identity_fields(
    key: str, identical: bool
) -> None:
    record = scope().model_dump(mode="json")
    raw = duplicate_json_member(
        record, (), key, record[key] if identical else "private-scope-canary"
    )
    with pytest.raises(ValidationError) as error:
        NativeProjectionScopeV1.model_validate_json(raw)
    assert "private-scope-canary" not in str(error.value)
    assert key not in error.value.json()


@pytest.mark.parametrize("input_type", [str, bytes, bytearray])
def test_duplicate_safe_json_roundtrips_preserve_unicode_dates_tuples_and_identity(
    input_type: type[str] | type[bytes] | type[bytearray],
) -> None:
    projection = project_openai_schema(
        rubric("explanations_first", explanations=True), scope=scope()
    )
    raw = projection.model_dump_json()
    data = raw if input_type is str else input_type(raw.encode("utf-8"))
    restored = NativeSchemaProjectionV1.model_validate_json(data)
    assert restored == projection
    assert restored.projection_sha256 == projection.projection_sha256
    assert restored.provenance[0].recorded_on.isoformat() == "2026-11-07"
    assert isinstance(restored.root_properties, tuple)
    assert restored.native_schema_text == projection.native_schema_text
    assert "é" in restored.native_schema_text
    assert isinstance(restored.source, NativeRubricSourceV1)
    assert restored.source.traits[1].category_labels == tuple(
        level.label for level in traits()[1].levels
    )


def test_duplicate_preflight_precedes_pydantic_and_matches_escaped_key_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from judgmentllm.evidence._base import EvidenceModel

    called = False

    def forbidden(
        cls: type[EvidenceModel], raw: str | bytes | bytearray, **kwargs: Any
    ) -> EvidenceModel:
        nonlocal called
        called = True
        raise AssertionError("Pydantic must not see a duplicate-key document")

    monkeypatch.setattr(EvidenceModel, "model_validate_json", classmethod(forbidden))
    # The decoded keys are identical despite distinct spellings in the source.
    raw = r'{"\u0070rovider":"private-escaped-canary","provider":"openai","endpoint":"responses"}'
    with pytest.raises(ValidationError) as error:
        NativeProjectionScopeV1.model_validate_json(raw)
    assert not called
    assert "private-escaped-canary" not in str(error.value)
    assert "provider" not in error.value.json()


@pytest.mark.parametrize(
    "raw",
    [
        '{"private-canary":NaN}',
        '{"private-canary":1e9999}',
        '{"private-canary":',
        b"\xff",
        bytearray(b'{"private-canary":Infinity}'),
    ],
)
def test_strict_json_preflight_failures_use_safe_native_validation_errors(
    raw: str | bytes | bytearray,
) -> None:
    with pytest.raises(ValidationError) as error:
        NativeProjectionScopeV1.model_validate_json(raw)
    diagnostic = str(error.value) + repr(error.value.errors()) + error.value.json()
    assert "private-canary" not in diagnostic
    assert error.value.errors()[0]["type"] == "evidence_invalid"
