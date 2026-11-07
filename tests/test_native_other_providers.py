"""Exact detached Anthropic, Together and local Ollama representations."""

from __future__ import annotations

import copy
import json
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

import pytest
from native_schema_test_validator import accepts
from native_v2_cases import CANARY, cases
from pydantic import JsonValue

from judgmentllm.native_schema import NativeProjectionScopeV1, snapshot_source
from judgmentllm.native_schema_v2 import (
    NativeMechanismV2,
    NativeProjectionErrorV2,
    NativeSchemaProjectionV2,
    project_native_schema_v2,
)
from judgmentllm.output_contracts import (
    AssessmentOutputContract,
    PairwiseOutputContract,
    RubricOutputContract,
    generate_output_schema,
    serialize_output_schema,
    validate_pairwise_contract_output,
    validate_rubric_contract_output,
)
from judgmentllm.providers import (
    _assessment_schema,
    anthropic_schema,
    ollama_schema,
    together_schema,
)

FIXTURES = Path(__file__).parent / "fixtures"
ROUTES: tuple[tuple[str, str, str, str | None, NativeMechanismV2], ...] = (
    (
        "anthropic",
        "messages",
        "2.3.0",
        "2026-10-21",
        "anthropic.messages.output_config.format",
    ),
    ("together", "chat.completions", "3.0.0", None, "together.chat.response_format"),
    ("ollama", "chat", "0.6.3", None, "ollama.chat.format"),
)


def scope(provider: str) -> NativeProjectionScopeV1:
    route = next(item for item in ROUTES if item[0] == provider)
    return NativeProjectionScopeV1(
        provider=route[0],
        endpoint=route[1],
        model="synthetic-model",
        model_scope="exact",
        sdk_package=provider,
        sdk_version=route[2],
        api_version=route[3],
    )


@pytest.mark.parametrize("route", ROUTES)
def test_exact_sixteen_projection_records(
    route: tuple[str, str, str, str | None, NativeMechanismV2],
) -> None:
    provider, _, _, _, mechanism = route
    originals = cases()
    assert len(originals) == 16
    expected = (FIXTURES / ("native_" + provider) / "projections.jsonl").read_bytes()
    actual: list[bytes] = []
    for contract in originals.values():
        before = serialize_output_schema(contract)
        projection = project_native_schema_v2(
            contract, scope=scope(provider), mechanism=mechanism
        )
        raw = projection.model_dump_json().encode()
        actual.append(raw)
        assert (
            projection.neutral_schema_text.encode()
            == before
            == serialize_output_schema(contract)
        )
        assert projection == NativeSchemaProjectionV2.model_validate_json(raw)
        assert projection == project_native_schema_v2(
            contract, scope=scope(provider), mechanism=mechanism
        )
        assert CANARY not in raw.decode() + repr(projection)
        for text_name, hash_name in (
            ("neutral_schema_text", "neutral_schema_sha256"),
            ("native_schema_text", "native_schema_sha256"),
            ("sdk_config_fragment_text", "sdk_config_fragment_sha256"),
            ("format_fragment_text", "format_fragment_sha256"),
        ):
            assert sha256(
                getattr(projection, text_name).encode()
            ).hexdigest() == getattr(projection, hash_name)
        native = json.loads(projection.native_schema_text)
        assert tuple(native["properties"]) == projection.root_properties
        assert all(
            item.represented
            and item.sdk_retained is True
            and item.enforcement_verified is None
            for item in projection.native_constraints
        )
        assert all(
            not item.represented and item.enforcement_verified is None
            for item in projection.local_constraints
        )
        if isinstance(contract, RubricOutputContract):
            keyword = "anyOf" if provider == "anthropic" else "oneOf"
            branches = native["properties"]["scores"]["items"][keyword]
            for trait, branch in zip(contract.traits, branches, strict=True):
                fields = branch["properties"]
                assert fields["trait_id"] == (
                    {"type": "string", "enum": [trait.trait_id]}
                    if provider == "anthropic"
                    else {"type": "string", "const": trait.trait_id}
                )
                assert fields["category_label"] == {
                    "type": "string",
                    "enum": [level.label for level in trait.levels],
                }
            assert [
                member.trait_id
                for member in projection.member_properties
                if member.array == "scores"
            ] == [trait.trait_id for trait in contract.traits]
        else:
            assert native["properties"]["winner_id"]["enum"] == [
                *contract.pair.presented_order,
                *([None] if contract.allow_ties else []),
            ]
            assert ("oneOf" in native) == (
                contract.allow_ties and provider != "anthropic"
            )
        fragment = json.loads(projection.format_fragment_text)
        if provider == "anthropic":
            assert fragment == {
                "output_config": {"format": {"type": "json_schema", "schema": native}}
            }
            assert projection.prompt_schema_policy == "none"
        elif provider == "together":
            assert fragment == {
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "judgmentllm_assessment_v2",
                        "schema": native,
                    },
                }
            }
            assert projection.prompt_schema_policy == "schema_in_prompt_recommended"
        else:
            assert fragment == {"format": native}
            assert projection.prompt_schema_policy == "schema_in_prompt_recommended"
        assert projection.sdk_config_fragment_text == projection.format_fragment_text
        for provenance in projection.provenance:
            filename = (
                "documentation.json"
                if provenance.kind == "documentation"
                else "profile.json"
            )
            assert (
                provenance.source_sha256
                == sha256(
                    (FIXTURES / ("native_" + provider) / filename).read_bytes()
                ).hexdigest()
            )
    assert b"\n".join(actual) + b"\n" == expected


@pytest.mark.parametrize("provider", ["anthropic", "together", "ollama"])
@pytest.mark.parametrize(
    "mutation",
    [
        "root_type",
        "extra_keyword",
        "closure",
        "required",
        "field_order",
        "array_type",
        "array_bounds",
        "alternative_type",
        "alternative_count",
        "member_type",
        "member_id",
        "category_enum",
        "explanation_type",
        "explanation_membership",
        "winner_enum",
        "tie_type",
        "tie_relation",
    ],
)
def test_unreviewed_canonical_shapes_fail_without_repair(
    monkeypatch: pytest.MonkeyPatch, provider: str, mutation: str
) -> None:
    contract: AssessmentOutputContract = cases()["grouped_scores_first"]
    if mutation in {"winner_enum", "tie_type", "tie_relation"}:
        contract = cases()["pairwise_ties_True_explanations_True"]
    schema: dict[str, Any] = generate_output_schema(contract)
    fields = schema["properties"]
    if mutation == "root_type":
        schema["type"] = "array"
    elif mutation == "extra_keyword":
        schema["private_keyword"] = CANARY
    elif mutation == "closure":
        schema["additionalProperties"] = True
    elif mutation == "required":
        schema["required"] = []
    elif mutation == "field_order":
        schema["properties"] = dict(reversed(list(fields.items())))
    elif mutation == "array_type":
        fields["scores"]["type"] = "object"
    elif mutation == "array_bounds":
        fields["scores"]["maxItems"] = 1
    elif mutation == "alternative_type":
        fields["scores"]["items"] = {"oneOf": None}
    elif mutation == "alternative_count":
        fields["scores"]["items"]["oneOf"].pop()
    elif mutation == "member_type":
        fields["scores"]["items"]["oneOf"][0] = []
    elif mutation == "member_id":
        fields["scores"]["items"]["oneOf"][0]["properties"]["trait_id"] = {
            "const": "wrong"
        }
    elif mutation == "category_enum":
        fields["scores"]["items"]["oneOf"][0]["properties"]["category_label"][
            "enum"
        ] = [""]
    elif mutation == "explanation_type":
        fields["explanations"]["items"]["properties"]["text"] = {"type": "number"}
    elif mutation == "explanation_membership":
        fields["explanations"]["items"]["properties"]["trait_id"]["enum"] = ["wrong"]
    elif mutation == "winner_enum":
        fields["winner_id"] = {"enum": ["wrong", None]}
    elif mutation == "tie_type":
        fields["tie"] = {"type": "string"}
    else:
        schema["oneOf"] = []
    raw = json.dumps(schema, ensure_ascii=False, separators=(",", ":")).encode()

    def malformed(_: object) -> dict[str, Any]:
        return copy.deepcopy(schema)

    def malformed_raw(_: object) -> bytes:
        return raw

    monkeypatch.setattr(_assessment_schema, "generate_output_schema", malformed)
    monkeypatch.setattr(_assessment_schema, "serialize_output_schema", malformed_raw)
    mechanism = cast(
        NativeMechanismV2, next(route[4] for route in ROUTES if route[0] == provider)
    )
    source = snapshot_source(contract)
    for module in (anthropic_schema, together_schema, ollama_schema):
        if module.__name__.endswith(provider + "_schema"):
            with pytest.raises(
                NativeProjectionErrorV2, match="unsupported_schema"
            ) as error:
                module._projection_parts(source, scope(provider), mechanism)  # pyright: ignore[reportPrivateUsage]
            assert CANARY not in str(error.value)
    assert serialize_output_schema(contract) != raw


def test_anthropic_documented_union_limit_and_no_casing_normalization() -> None:
    for count in (16, 17):
        schema: dict[str, JsonValue] = {
            "type": "object",
            "properties": {
                str(i): {"anyOf": [{"type": "string"}, {"type": "null"}]}
                for i in range(count)
            },
        }
        if count == 16:
            anthropic_schema._limits(schema)  # pyright: ignore[reportPrivateUsage]
        else:
            with pytest.raises(NativeProjectionErrorV2) as error:
                anthropic_schema._limits(schema)  # pyright: ignore[reportPrivateUsage]
            assert error.value.diagnostic.limit == "alternatives"
    contract = cast(RubricOutputContract, cases()["single_scores"])
    projection = project_native_schema_v2(
        contract, scope=scope("anthropic"), mechanism=ROUTES[0][4]
    )
    assert any(
        item.reason == "enum_casing_unqualified"
        for item in projection.native_constraints
    )
    with pytest.raises(ValueError):
        validate_rubric_contract_output(
            {"scores": [{"trait_id": "001", "category_label": "HIGH"}]}, contract
        )


@pytest.mark.parametrize("provider", ["anthropic", "together", "ollama"])
def test_local_omission_witnesses_and_lossless_semantics(provider: str) -> None:
    mechanism = cast(
        NativeMechanismV2, next(route[4] for route in ROUTES if route[0] == provider)
    )
    contract = cast(RubricOutputContract, cases()["grouped_scores_first"])
    projection = project_native_schema_v2(
        contract, scope=scope(provider), mechanism=mechanism
    )
    native = json.loads(projection.native_schema_text)
    # Full length and correct enums cannot establish complete unique trait coverage.
    repeated = {
        "scores": [{"trait_id": "001", "category_label": "low"}] * 2,
        "explanations": [{"trait_id": "001", "text": "valid"}] * 2,
    }
    assert accepts(native, repeated)
    with pytest.raises(ValueError):
        validate_rubric_contract_output(repeated, contract)
    valid = {
        "scores": [
            {"trait_id": trait.trait_id, "category_label": trait.levels[0].label}
            for trait in contract.traits
        ],
        "explanations": [
            {"trait_id": trait.trait_id, "text": "valid"} for trait in contract.traits
        ],
    }
    assert accepts(native, valid)
    validate_rubric_contract_output(valid, contract)
    # Whitespace satisfies JSON Schema minLength but violates the complete contract.
    blank = copy.deepcopy(valid)
    blank["explanations"][0]["text"] = " "
    assert accepts(native, blank)
    with pytest.raises(ValueError):
        validate_rubric_contract_output(blank, contract)
    mismatch = copy.deepcopy(valid)
    mismatch["explanations"][1]["trait_id"] = "001"
    assert accepts(native, mismatch)
    with pytest.raises(ValueError):
        validate_rubric_contract_output(mismatch, contract)
    pair = cast(PairwiseOutputContract, cases()["pairwise_ties_True_explanations_True"])
    pair_projection = project_native_schema_v2(
        pair, scope=scope(provider), mechanism=mechanism
    )
    pair_schema = json.loads(pair_projection.native_schema_text)
    inconsistent = {"winner_id": "001", "tie": True, "explanation": "valid"}
    assert accepts(pair_schema, inconsistent) == (provider == "anthropic")
    with pytest.raises(ValueError):
        validate_pairwise_contract_output(inconsistent, pair)
    if provider == "anthropic":
        bad_values: list[dict[str, Any]] = [
            {"scores": [], "explanations": []},
            {
                **valid,
                "explanations": [
                    {"trait_id": trait.trait_id, "text": ""}
                    for trait in contract.traits
                ],
            },
        ]
        for bad in bad_values:
            assert accepts(native, bad)
            with pytest.raises(ValueError):
                validate_rubric_contract_output(bad, contract)
    else:
        # Lossless routes retain bounds and minLength; no manufactured omission.
        assert not accepts(native, {"scores": [], "explanations": []})
        assert all(
            item.documented_support is None for item in projection.native_constraints
        )


@pytest.mark.parametrize(
    "name",
    [
        "grouped_together",
        "grouped_scores_first",
        "grouped_explanations_first",
        "pairwise_ties_True_explanations_True",
        "pairwise_ties_False_explanations_True",
    ],
)
def test_each_anthropic_explanation_omission_has_a_local_failure_witness(
    name: str,
) -> None:
    contract = cases()[name]
    projection = project_native_schema_v2(
        contract, scope=scope("anthropic"), mechanism=ROUTES[0][4]
    )
    schema = json.loads(projection.native_schema_text)
    if isinstance(contract, RubricOutputContract):
        value: dict[str, Any] = {
            "scores": [
                {"trait_id": trait.trait_id, "category_label": trait.levels[0].label}
                for trait in contract.traits
            ]
        }
        if contract.output_order == "together":
            for score in value["scores"]:
                score["explanation"] = ""
        else:
            value["explanations"] = [
                {"trait_id": trait.trait_id, "text": ""} for trait in contract.traits
            ]
        assert accepts(schema, value)
        with pytest.raises(ValueError):
            validate_rubric_contract_output(value, contract)
    else:
        value = {"winner_id": contract.pair.presented_order[0], "explanation": ""}
        if contract.allow_ties:
            value["tie"] = False
        assert accepts(schema, value)
        with pytest.raises(ValueError):
            validate_pairwise_contract_output(value, contract)


def test_schema_snapshot_mismatch_is_controlled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def malformed(_: object) -> dict[str, Any]:
        return {"type": "object", "private_keyword": CANARY}

    monkeypatch.setattr(_assessment_schema, "generate_output_schema", malformed)
    with pytest.raises(NativeProjectionErrorV2, match="unsupported_schema"):
        _assessment_schema.assessment_parts(
            snapshot_source(cases()["single_scores"]), conservative=True
        )
