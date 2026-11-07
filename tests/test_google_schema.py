"""Exact-byte Google route fixtures, independent witnesses and converter probes."""

from __future__ import annotations

import copy
import importlib.metadata
import json
import os
import subprocess
import sys
from functools import partial
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

import pytest
from native_schema_test_validator import accepts
from native_v2_cases import CANARY, cases

from judgmentllm.native_schema import NativeProjectionScopeV1
from judgmentllm.native_schema_v2 import (
    NativeMechanismV2,
    NativeProjectionErrorV2,
    NativeSchemaProjectionV2,
    project_native_schema_v2,
)
from judgmentllm.native_selection_v2 import resolve_native_transport_v2
from judgmentllm.output_contracts import (
    RubricOutputContract,
    generate_output_schema,
    serialize_output_schema,
    validate_pairwise_contract_output,
    validate_rubric_contract_output,
)
from judgmentllm.providers import google_schema

FIXTURES = Path(__file__).parent / "fixtures" / "native_google"
PROVIDERS = ("gemini", "vertex")
ROUTES = ("response_schema", "response_json_schema")


def scope(provider: str, /, **changes: Any) -> NativeProjectionScopeV1:
    return NativeProjectionScopeV1.model_validate(
        {
            "provider": provider,
            "endpoint": "models.generate_content",
            "model": "judgmentllm-synthetic-native-v2",
            "model_scope": "exact",
            "sdk_package": "google-genai",
            "sdk_version": "2.34.0",
            "api_version": "v1beta" if provider == "gemini" else "v1beta1",
            **changes,
        }
    )


def mechanism(provider: str, route: str) -> NativeMechanismV2:
    return cast(NativeMechanismV2, provider + ".generate_content." + route)


def projection(
    provider: str, route: str, name: str = "grouped_scores"
) -> NativeSchemaProjectionV2:
    return project_native_schema_v2(
        cases()[name], scope=scope(provider), mechanism=mechanism(provider, route)
    )


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("route", ROUTES)
def test_exact_google_projection_and_decision_fixtures(
    provider: str, route: str
) -> None:
    projections: list[bytes] = []
    decisions: list[bytes] = []
    for name, contract in cases().items():
        candidate = projection(provider, route, name)
        projections.append(candidate.model_dump_json().encode() + b"\n")
        decisions.append(
            resolve_native_transport_v2(
                contract,
                scope=scope(provider),
                mechanism=mechanism(provider, route),
                policy="auto",
            )
            .model_dump_json()
            .encode()
            + b"\n"
        )
        assert (
            NativeSchemaProjectionV2.model_validate_json(candidate.model_dump_json())
            == candidate
        )
        assert candidate.neutral_schema_text.encode() == serialize_output_schema(
            contract
        )
        for stem in (
            "neutral_schema",
            "native_schema",
            "sdk_config_fragment",
            "format_fragment",
        ):
            assert sha256(
                getattr(candidate, stem + "_text").encode()
            ).hexdigest() == getattr(candidate, stem + "_sha256")
        assert candidate.root_properties == tuple(
            cast(dict[str, Any], generate_output_schema(contract)["properties"])
        )
        assert all(item.represented for item in candidate.native_constraints)
        assert all(not item.represented for item in candidate.local_constraints)
        assert all(
            item.enforcement_verified is None
            for item in (*candidate.native_constraints, *candidate.local_constraints)
        )
        assert CANARY not in candidate.model_dump_json()
        config = json.loads(candidate.sdk_config_fragment_text)
        assert config["response_mime_type"] == "application/json"
        assert set(config) == {"response_mime_type", route}
        assert candidate.prompt_schema_policy == (
            "schema_in_prompt_discouraged"
            if provider == "vertex" and route == "response_schema"
            else "none"
        )
        native = json.loads(candidate.native_schema_text)
        order_key = (
            "property_ordering" if route == "response_schema" else "propertyOrdering"
        )
        assert native[order_key] == list(candidate.root_properties)
        if isinstance(contract, RubricOutputContract):
            items = native["properties"]["scores"]["items"]
            alternatives = items["any_of" if route == "response_schema" else "anyOf"]
            for trait, branch in zip(contract.traits, alternatives, strict=True):
                fields = branch["properties"]
                assert fields["trait_id"]["enum"] == [trait.trait_id]
                assert fields["category_label"]["enum"] == [
                    level.label for level in trait.levels
                ]
                assert branch[order_key] == list(fields)
        else:
            winner = native["properties"]["winner_id"]
            if route == "response_json_schema" and contract.allow_ties:
                winner = winner["anyOf"][0]
            assert winner["enum"] == contract.pair.presented_order
    folder = FIXTURES / (provider + "_" + route)
    assert (folder / "projections.jsonl").read_bytes() == b"".join(projections)
    assert (folder / "decisions.jsonl").read_bytes() == b"".join(decisions)


def test_google_provenance_hashes_actual_checked_in_manifests() -> None:
    assert (
        sha256((FIXTURES / "documentation.json").read_bytes()).hexdigest()
        == google_schema.GOOGLE_DOCUMENTATION_SHA256
    )
    assert (
        sha256((FIXTURES / "profile.json").read_bytes()).hexdigest()
        == google_schema.GOOGLE_FIXTURE_PROFILE_SHA256
    )


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("route", ROUTES)
def test_constraints_remain_independent(provider: str, route: str) -> None:
    candidate = projection(provider, route)
    closure = [
        item
        for item in candidate.native_constraints
        if item.constraint == "closed_object"
    ]
    assert closure and all(item.sdk_retained is True for item in closure)
    assert all(
        item.documented_support is (None if route == "response_schema" else True)
        for item in closure
    )
    assert all(
        item.sdk_retained is None and item.documented_support is None
        for item in candidate.local_constraints
    )
    bounds = [
        item
        for item in candidate.native_constraints
        if item.constraint == "array_cardinality"
    ]
    assert bounds and all(item.operation == "preserve_bounds" for item in bounds)
    native = json.loads(candidate.native_schema_text)
    assert not accepts(native, {"scores": []})
    for output in (
        {"scores": [{"trait_id": "unknown", "category_label": "low"}] * 2},
        {"scores": [{"trait_id": "001", "category_label": "invalid"}] * 2},
    ):
        assert not accepts(native, output)


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("route", ROUTES)
def test_each_local_constraint_has_a_native_accepted_invalid_witness(
    provider: str, route: str
) -> None:
    covered: set[str] = set()
    for name, contract in cases().items():
        candidate = projection(provider, route, name)
        native = json.loads(candidate.native_schema_text)
        witnesses: list[tuple[str, Any]] = []
        output: dict[str, Any]
        if isinstance(contract, RubricOutputContract):
            scores = [
                {
                    "trait_id": trait.trait_id,
                    "category_label": trait.levels[0].label,
                    **(
                        {"explanation": "valid"}
                        if contract.explanations and contract.output_order == "together"
                        else {}
                    ),
                }
                for trait in contract.traits
            ]
            output = {"scores": scores}
            if contract.explanations and contract.output_order != "together":
                output["explanations"] = [
                    {"trait_id": trait.trait_id, "text": "valid"}
                    for trait in contract.traits
                ]
            assert accepts(native, output)
            if len(scores) > 1:
                duplicate = copy.deepcopy(output)
                duplicate["scores"][1] = duplicate["scores"][0]
                witnesses.append(("trait_uniqueness_completeness", duplicate))
                if "explanations" in output:
                    bad_join = copy.deepcopy(output)
                    bad_join["explanations"][1] = bad_join["explanations"][0]
                    witnesses.append(("explanation_join", bad_join))
            if contract.explanations:
                blank = copy.deepcopy(output)
                blank["scores"][0]["explanation"] = (
                    "" if contract.output_order == "together" else "valid"
                )
                if contract.output_order != "together":
                    del blank["scores"][0]["explanation"]
                    blank["explanations"][0]["text"] = ""
                witnesses.append(("nonblank_explanation", blank))
            validate = partial(validate_rubric_contract_output, contract=contract)
        else:
            output = {
                "winner_id": contract.pair.presented_order[0],
                **({"tie": False} if contract.allow_ties else {}),
                **({"explanation": "valid"} if contract.explanations else {}),
            }
            assert accepts(native, output)
            if contract.allow_ties:
                mismatch = copy.deepcopy(output)
                mismatch["winner_id"] = None
                witnesses.append(("winner_tie_relation", mismatch))
            if contract.explanations:
                blank = copy.deepcopy(output)
                blank["explanation"] = ""
                witnesses.append(("nonblank_explanation", blank))
            validate = partial(validate_pairwise_contract_output, contract=contract)
        raw = json.dumps(output)
        key = next(iter(output))
        duplicate_raw = "{" + json.dumps(key) + ":null," + raw[1:]
        assert accepts(native, json.loads(duplicate_raw))
        with pytest.raises(ValueError):
            validate(duplicate_raw)
        covered.add("unique_json_keys")
        for constraint, witness in witnesses:
            assert accepts(native, witness), constraint
            with pytest.raises(ValueError):
                validate(witness)
            covered.add(constraint)
    assert covered == {
        "unique_json_keys",
        "trait_uniqueness_completeness",
        "explanation_join",
        "nonblank_explanation",
        "winner_tie_relation",
    }


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize(
    "changes",
    [
        {"provider": "anthropic"},
        {"endpoint": "chat"},
        {"sdk_package": "openai"},
        {"sdk_version": "2.34.1"},
        {"api_version": "v1"},
    ],
)
def test_google_scope_mismatch_fails(
    provider: str, route: str, changes: dict[str, Any]
) -> None:
    with pytest.raises(NativeProjectionErrorV2, match="unsupported_scope"):
        project_native_schema_v2(
            cases()["single_scores"],
            scope=scope(provider, **changes),
            mechanism=mechanism(provider, route),
        )


def test_invalid_empty_enum_is_rejected_by_canonical_validation() -> None:
    contract = cast(RubricOutputContract, cases()["single_scores"])
    contract.traits[0].levels[0] = (
        contract.traits[0].levels[0].model_copy(update={"label": ""})
    )
    for provider in PROVIDERS:
        for route in ROUTES:
            with pytest.raises(NativeProjectionErrorV2, match="invalid_contract"):
                project_native_schema_v2(
                    contract,
                    scope=scope(provider),
                    mechanism=mechanism(provider, route),
                )
    # Defensive scope-specific Cloud subset restriction never deletes a value.
    cloud = cast(Any, google_schema)._cloud_schema
    with pytest.raises(NativeProjectionErrorV2, match="unsupported_schema"):
        cloud({"type": "string", "enum": ["", "other"]}, reject_empty=True)
    assert cloud({"type": "string", "enum": ["", "other"]}, reject_empty=False)[
        "enum"
    ] == ["", "other"]


@pytest.mark.parametrize(
    "mutation",
    [
        "keyword",
        "type",
        "closure",
        "required",
        "nested_alternatives",
        "categories",
        "root_alternatives",
    ],
)
def test_unreviewed_canonical_changes_fail_without_repair(
    monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    original = google_schema.generate_output_schema

    def changed(contract: Any) -> Any:
        schema = cast(dict[str, Any], original(contract))
        branch = schema["properties"]["scores"]["items"]["oneOf"][0]
        if mutation == "keyword":
            branch["unreviewed"] = True
        elif mutation == "type":
            del branch["type"]
        elif mutation == "closure":
            branch["additionalProperties"] = True
        elif mutation == "required":
            branch["required"] = []
        elif mutation == "nested_alternatives":
            branch["properties"]["category_label"] = {"oneOf": []}
        elif mutation == "categories":
            branch["properties"]["category_label"]["enum"] = ["changed"]
        else:
            schema["oneOf"] = []
        return schema

    monkeypatch.setattr(google_schema, "generate_output_schema", changed)
    with pytest.raises(NativeProjectionErrorV2):
        projection("vertex", "response_schema")


def test_pinned_google_converters_under_self_tested_offline_guards() -> None:
    try:
        installed = importlib.metadata.version("google-genai")
    except importlib.metadata.PackageNotFoundError:
        if os.environ.get("JUDGMENTLLM_REQUIRE_NATIVE_SDK_PROBES") == "1":
            pytest.fail("required pinned Google SDK probe dependency missing")
        pytest.skip(
            "optional google-genai SDK absent; qualification installs locked all-extras"
        )
    assert installed == "2.34.0"
    records: list[dict[str, Any]] = []
    for provider in PROVIDERS:
        for route in ROUTES:
            for name in cases():
                candidate = projection(provider, route, name)
                records.append(
                    {
                        "provider": provider,
                        "config": json.loads(candidate.sdk_config_fragment_text),
                        "expected": candidate.format_fragment_text,
                    }
                )
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    run = subprocess.run(
        [sys.executable, str(Path(__file__).with_name("google_converter_probe.py"))],
        input=json.dumps(records),
        text=True,
        capture_output=True,
        env=env,
        timeout=30,
        check=False,
    )
    assert run.returncode == 0, run.stderr
    measured = json.loads(run.stdout)
    assert measured["full_schema_cases"] == 64
    assert measured["guard_checks"] >= 20
    assert measured["guarded_operations"] == 0
    assert measured["tiny"] == json.loads(
        (FIXTURES / "converter-baseline.json").read_text()
    )
    doc = json.loads((FIXTURES / "documentation.json").read_text())
    dist = importlib.metadata.distribution("google-genai")
    for name, source in doc["source_files"].items():
        assert (
            sha256(
                Path(str(dist.locate_file("google/genai/" + name))).read_bytes()
            ).hexdigest()
            == source["sha256"]
        )
