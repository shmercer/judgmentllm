"""Reviewed real-model admission, exercised only with synthetic offline inputs."""

from __future__ import annotations

# pyright: reportPrivateUsage=false
import json
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any, Literal, cast

import httpx2
import pytest
from gemini_fixture import case
from gemini_offline import envelope, isolate
from pydantic import BaseModel
from test_gemini_lifecycle import production_spec

from judgmentllm import gemini_registry as registry
from judgmentllm.config import GenerationSpec, RunSpecV3
from judgmentllm.execution import execute_run, recover_run
from judgmentllm.gemini_transport import (
    GeminiProductionContext,
    PreparedRequestV6,
    TransportCommitmentV4,
    _support,
    build_output_commitment_v6,
    prepare_production_request,
    production_context,
    production_context_digest,
)
from judgmentllm.models import PairTask, ResponseItem
from judgmentllm.native_selection import NativeFeature
from judgmentllm.persistence.hashing import bytes_digest, canonical_bytes
from judgmentllm.persistence.repository import ReadOnlyRepository
from judgmentllm.planning import plan_record
from judgmentllm.preparation import open_prepared_run, prepare_run
from judgmentllm.preparation.session import RequestDependency
from judgmentllm.providers.gemini import GeminiAdapter
from judgmentllm.transport import TransportIntegrityError, physical_identity

MODEL = "gemini-3.1-flash-lite"
MECHANISM = "gemini.generate_content.response_json_schema"


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    isolate(monkeypatch)


def prepare(
    tmp_path: Path,
    *,
    model: str = MODEL,
    frozen: TransportCommitmentV4 | None = None,
    **settings: Any,
) -> tuple[PreparedRequestV6, RunSpecV3, RequestDependency, GeminiProductionContext]:
    strict = settings.pop("strict", False)
    spec, dependency, records = case(tmp_path, grouped=False, **settings)
    spec.model = model
    spec.strict_capabilities = strict
    record = dependency.request.pair or dependency.request.response
    assert record is not None
    request = next(
        plan_record(
            spec,
            record,
            capabilities=GeminiAdapter.capabilities_for(
                spec.provider,
                spec.endpoint,
                spec.model,
                mechanism=spec.prompt.native_mechanism,
            ),
        )
    )
    dependency = RequestDependency(request=request, dependency_sha256="0" * 64)
    context = production_context(spec)

    def lookup(
        kind: Literal["response", "pair"], ident: str
    ) -> ResponseItem | PairTask:
        if kind == "pair":
            assert request.pair is not None
            return request.pair
        return records[ident]

    wire = prepare_production_request(
        spec,
        dependency,
        physical_identity("run-001", request.request_uid, "attempt-001"),
        context,
        lookup=lookup,
        frozen=frozen,
        template=(
            "Compare.\n{{ criterion }}\n{{ first_response }}\n{{ second_response }}\n{{ output_instructions }}"
            if spec.allow_ties
            else None
        ),
    )
    return wire, spec, dependency, context


def test_exact_profile_and_provenance() -> None:
    assert len(registry._QUALIFIED_PROFILES) == 1
    profile = registry._QUALIFIED_PROFILES[0]
    assert profile.scope.model == MODEL and profile.mechanism == MECHANISM
    assert (
        profile.scope.provider,
        profile.scope.endpoint,
        profile.scope.model_scope,
        profile.scope.sdk_package,
        profile.scope.sdk_version,
        profile.scope.api_version,
    ) == (
        "gemini",
        "models.generate_content",
        "exact",
        "google-genai",
        "2.34.0",
        "v1beta",
    )
    assert profile.qualification_date == date(2026, 11, 10)
    assert profile.source_commit == "a77e700f550e68b382c1734d270b8bfa9eb8b181"
    assert profile.translator_version == 2
    assert profile.schema_dialect == "gemini.json-schema.v1"
    assert profile.support_status == "supported"
    assert profile.request_acceptance == "accepted"
    assert profile.assessment_behavior == profile.native_schema_behavior == "verified"
    assert profile.supported_features == ("rubric_scores", "pairwise_forced_choice")
    assert profile.unsupported_features == () and profile.generation is None
    assert profile.documentation_sha256 == (
        "67b3050f1727a23153a4f9eb5f8b27a85768e407f8f10c96e990db4eef7a1576"
    )
    # Provenance identifies the LF Git blob, including on CRLF Windows checkouts.
    assert profile.offline_fixture_sha256 == bytes_digest(
        (Path(__file__).parent / "gemini_fixture.py")
        .read_text(encoding="utf-8")
        .encode()
    )
    assert profile.independent_live_sha256 == (
        "ddd2a6e2f206d7b0f6f99db1a9328f65926abe8d8c660309fddad285afa36e31"
    )
    assert {p.kind: p.source_sha256 for p in profile.provenance} == {
        "documentation": profile.documentation_sha256,
        "offline_fixture": profile.offline_fixture_sha256,
        "independent_live": profile.independent_live_sha256,
    }
    assert all(p.recorded_on == profile.qualification_date for p in profile.provenance)
    record = (
        Path(__file__).parents[1] / "docs/gemini-live-qualification-acceptance.md"
    ).read_text()
    assert profile.authority_sha256 in record


@pytest.mark.parametrize("pairwise", [False, True])
@pytest.mark.parametrize("policy", ["native", "auto", "text"])
@pytest.mark.parametrize("strict", [False, True])
def test_exact_admission_and_complete_frozen_bytes(
    tmp_path: Path, pairwise: bool, policy: str, strict: bool
) -> None:
    wire, _, _, context = prepare(
        tmp_path, pairwise=pairwise, policy=policy, strict=strict
    )
    selection = wire.output_commitment.selection
    assert selection.mechanism == ("text" if policy == "text" else MECHANISM)
    assert selection.authority_kind == "reviewed"
    assert selection.native_support == "supported"
    assert selection.features == (
        "pairwise_forced_choice" if pairwise else "rubric_scores",
    )
    config: dict[str, Any] = {"candidateCount": 1}
    if policy != "text":
        member = {
            "type": "object",
            "properties": {
                "trait_id": {"type": "string", "enum": ["01"]},
                "category_label": {"type": "string", "enum": ["low 雪", "high é"]},
            },
            "required": ["trait_id", "category_label"],
            "additionalProperties": False,
            "propertyOrdering": ["trait_id", "category_label"],
        }
        properties = (
            {"winner_id": {"type": "string", "enum": ["002", "001"]}}
            if pairwise
            else {
                "scores": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 1,
                    "items": {"anyOf": [member]},
                }
            }
        )
        config.update(
            responseMimeType="application/json",
            responseJsonSchema={
                "type": "object",
                "properties": properties,
                "required": list(properties),
                "additionalProperties": False,
                "propertyOrdering": list(properties),
            },
        )
    expected = json.dumps(
        {
            "contents": [{"parts": [{"text": wire.rendered_text}], "role": "user"}],
            "generationConfig": config,
        },
        ensure_ascii=True,
        allow_nan=False,
    ).encode()
    # Compare complete serialization, including order, whitespace and escaping.
    assert wire.payload == expected
    assert wire.transport_commitment.payload_sha256 == bytes_digest(expected)
    assert wire.transport_commitment.payload_bytes == len(expected)
    assert wire.transport_commitment.context_sha256 == production_context_digest(
        context
    )
    frozen, _, _, _ = prepare(
        tmp_path,
        pairwise=pairwise,
        policy=policy,
        strict=strict,
        frozen=wire.transport_commitment,
    )
    assert frozen == wire


@pytest.mark.parametrize(
    "settings",
    [
        {"explanations": True},
        {"explanations": True, "layout": "scores_first"},
        {"explanations": True, "layout": "explanations_first"},
        {"pairwise": True, "explanations": True},
        {"pairwise": True, "ties": True},
        {"mechanism": "gemini.generate_content.response_schema"},
        {"model": "gemini-another-model"},
        {"model": "models/gemini-3.1-flash-lite"},
    ],
)
def test_untested_native_fails_and_auto_is_explicitly_unknown(
    tmp_path: Path, settings: dict[str, Any]
) -> None:
    for policy, strict in (("native", False), ("auto", True)):
        with pytest.raises(ValueError):
            prepare(tmp_path, policy=policy, strict=strict, **settings)
    wire, _, _, _ = prepare(tmp_path, policy="auto", **settings)
    assert wire.output_commitment.selection.mechanism == "text"
    assert wire.output_commitment.selection.native_support == "unknown"


def test_grouped_and_all_unqualified_features_stay_unknown(tmp_path: Path) -> None:
    spec, dep, _ = case(tmp_path, policy="native", grouped=True)
    spec.model = MODEL
    context = production_context(spec)
    with pytest.raises(TransportIntegrityError):
        build_output_commitment_v6(spec, dep.request, context)
    for feature in (
        "rubric_grouped",
        "rubric_explanations_together",
        "rubric_scores_first",
        "rubric_explanations_first",
        "pairwise_ties",
        "pairwise_explanations",
    ):
        assert (
            _support(context, (cast(NativeFeature, feature),), MECHANISM) == "unknown"
        )
    spec.prompt.structured_output = "auto"
    output = build_output_commitment_v6(spec, dep.request, context)
    assert output.selection.mechanism == "text"
    spec.strict_capabilities = True
    with pytest.raises(TransportIntegrityError):
        build_output_commitment_v6(spec, dep.request, context)


@pytest.mark.parametrize(
    "changes",
    [
        {"provider": "vertex", "api_version": "v1beta1"},
        {"sdk_version": "2.35.0"},
        {"api_version": "v1"},
        {"endpoint": "interactions.create"},
        {"sdk_package": "openai"},
    ],
)
def test_no_foreign_scope_authority(tmp_path: Path, changes: dict[str, str]) -> None:
    _, _, _, context = prepare(tmp_path)
    scope = context.scope.model_copy(update=changes)
    assert registry._profile_for(scope, MECHANISM) is None
    with pytest.raises(TransportIntegrityError):
        production_context_digest(replace(context, scope=scope))


def test_generation_qualification_does_not_expand(tmp_path: Path) -> None:
    for generation in (
        GenerationSpec(max_output_tokens=512),
        GenerationSpec(temperature=0, top_p=1, max_output_tokens=512),
    ):
        wire, _, _, context = prepare(tmp_path, policy="native", generation=generation)
        facts = registry.production_generation_capabilities(context.scope)
        assert facts is not None and facts.fixture_tested and not facts.live_qualified
        assert wire.generation == generation
    capabilities = GeminiAdapter.capabilities_for(
        "gemini", "models.generate_content", MODEL, mechanism=MECHANISM
    )
    assert (
        capabilities.explanations is capabilities.grouped is capabilities.ties is None
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"supported_features": ("rubric_scores", "rubric_scores")},
        {"unsupported_features": ("rubric_scores",)},
        {"provenance": ()},
        {"native_schema_behavior": "unknown"},
        {"request_acceptance": "unknown"},
        {"documentation_sha256": "0" * 64},
        {"translator_version": True},
        {"generation": {"live_qualified": True}},
        {"unreviewed_feature": True},
    ],
)
def test_forged_profile_values_revalidate_before_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changes: dict[str, Any]
) -> None:
    # Deliberately bypass the normal safe model_copy to simulate forged state.
    profile = BaseModel.model_copy(registry._QUALIFIED_PROFILES[0], update=changes)
    monkeypatch.setattr(registry, "_QUALIFIED_PROFILES", (profile,))
    with pytest.raises(ValueError):
        prepare(tmp_path, policy="native")


@pytest.mark.parametrize("pairwise", [False, True])
@pytest.mark.asyncio
async def test_actual_sdk_bytes_native_failure_and_frozen_recovery(
    tmp_path: Path, pairwise: bool
) -> None:
    spec = production_spec(
        tmp_path / "inputs",
        pairwise=pairwise,
        traits=1,
        grouping="independent",
        explanations=False,
    )
    spec.model = MODEL
    spec.prompt.structured_output = "native"
    spec.prompt.native_mechanism = MECHANISM
    store = tmp_path / "store"
    with prepare_run(
        spec, run_id="001", store=store, transport_context=production_context(spec)
    ) as run:
        dep = next(run.iter_request_dependencies())
        from judgmentllm.planning import stable_id

        ident = physical_identity(
            "001",
            dep.request.request_uid,
            stable_id("attempt", "001", dep.request.request_uid, 1),
        )
        wire = run.prepare_transport_attempt(dep, ident)
    sent: list[bytes] = []

    def respond(request: Any) -> Any:
        sent.append(request.content)
        assert request.content == wire.payload
        return httpx2.Response(200, json=envelope("{invalid", modelVersion=MODEL))

    adapter = GeminiAdapter(
        spec.endpoint, spec.model, http_transport=httpx2.MockTransport(respond)
    )
    try:
        result = await execute_run("001", store=store, spec=spec, adapter=adapter)
        assert not result.recovery_required
        await recover_run("001", store=store, spec=spec, adapter=adapter)
        with open_prepared_run(
            "001", spec=spec, store=store, transport_context=production_context(spec)
        ) as run:
            assert run.prepare_transport_attempt(dep, ident).payload == wire.payload
        with ReadOnlyRepository(store) as repository:
            summary = repository.summary("001")
            assert summary.attempts == 1 and summary.retry_operations == 0
            assert all(
                row.status == "invalid" for row in repository.iter_results("001")
            )
        assert sent == [wire.payload] and adapter.actual_call_count == 1
        assert b"responseJsonSchema" in sent[0]
        assert canonical_bytes(wire.transport_commitment)
    finally:
        await adapter.aclose()
