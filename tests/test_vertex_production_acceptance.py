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
from pydantic import BaseModel
from test_vertex_lifecycle import production_spec
from vertex_fixture import case
from vertex_offline import envelope, isolate

from judgmentllm import vertex_registry as registry
from judgmentllm.config import GenerationSpec, RunSpecV3
from judgmentllm.execution import execute_run, recover_run
from judgmentllm.models import PairTask, ResponseItem
from judgmentllm.native_selection import NativeFeature
from judgmentllm.persistence.hashing import bytes_digest, canonical_bytes
from judgmentllm.persistence.repository import ReadOnlyRepository
from judgmentllm.planning import plan_record
from judgmentllm.preparation import open_prepared_run, prepare_run
from judgmentllm.preparation.session import RequestDependency
from judgmentllm.presentation import PresentationVersion
from judgmentllm.providers.vertex import VertexAdapter
from judgmentllm.transport import TransportIntegrityError, physical_identity
from judgmentllm.vertex_transport import (
    PreparedRequestV7,
    TransportCommitmentV5,
    VertexProductionContext,
    _support,
    build_output_commitment_v7,
    prepare_production_request,
    production_context,
    production_context_digest,
)

MODEL = "gemini-3.1-flash-lite"
MECHANISM = "vertex.generate_content.response_json_schema"


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    isolate(monkeypatch)


def prepare(
    tmp_path: Path,
    *,
    model: str = MODEL,
    frozen: TransportCommitmentV5 | None = None,
    presentation_version: PresentationVersion = 1,
    **settings: Any,
) -> tuple[PreparedRequestV7, RunSpecV3, RequestDependency, VertexProductionContext]:
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
            capabilities=VertexAdapter.capabilities_for(
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
        presentation_version=presentation_version,
        template=(
            "Compare.\n{{ criterion }}\n{{ first_response }}\n{{ second_response }}\n{{ output_instructions }}"
            if spec.allow_ties
            else None
        ),
    )
    return wire, spec, dependency, context


def test_exact_profile_and_provenance() -> None:
    assert len(registry._QUALIFIED_PROFILES) == 2
    record = (
        Path(__file__).parents[1] / "docs/vertex-live-qualification-acceptance.md"
    ).read_text()
    sources = {
        "adc": (
            "59a45ec31e53de7ed9eb833f5876c0000d982bca",
            "ff2fd19488f11524ee0aed165e588a3a13581d12127cc7f25a9ca5f7836fa5aa",
            "6843a1b81b0cca2bea08bcae6a5b1e5788fde8f557768f90272a8ecdfb3837ae",
        ),
        "api_key": (
            "db98be97654d8e918516618399d57ec8c28e4ec6",
            "6707041fa6b0eb2945dc82593b7d5e975a019d0655e81aee7a25f4fba6b776c7",
            "1a523c431b795e7e1d4b4ded26688f8d4cb9ea2746ed409f64b5331e5de84fff",
        ),
    }
    for profile in registry._QUALIFIED_PROFILES:
        assert profile.scope.model == MODEL and profile.mechanism == MECHANISM
        assert (
            profile.scope.provider,
            profile.scope.endpoint,
            profile.scope.model_scope,
            profile.scope.sdk_package,
            profile.scope.sdk_version,
            profile.scope.api_version,
        ) == (
            "vertex",
            "models.generate_content",
            "exact",
            "google-genai",
            "2.34.0",
            "v1beta1",
        )
        assert profile.api_scope == "vertex-generate-content-v1beta1-v1"
        assert (
            profile.qualification_date == date(2026, 11, 11)
            and profile.location == "global"
        )
        assert (
            profile.source_commit,
            profile.plan_sha256,
            profile.independent_live_sha256,
        ) == sources[profile.authentication_mode]
        assert (
            profile.translator_version == 2
            and profile.schema_dialect == "vertex.json-schema.v1"
        )
        assert (
            profile.support_status == "supported"
            and profile.request_acceptance == "accepted"
        )
        assert (
            profile.assessment_behavior == profile.native_schema_behavior == "verified"
        )
        assert profile.supported_features == ("rubric_scores", "pairwise_forced_choice")
        assert profile.unsupported_features == () and profile.generation is None
        assert (
            profile.documentation_sha256
            == "9b423e04ed0c7d9c569c4fe73b2dd5b3bc13380856a2bc53f49760d622cec8b7"
        )
        # This digest identifies the original qualification source, before refactors.
        assert profile.offline_fixture_sha256 == (
            "918cd2571aef737985c658a7faa45d5b455d424119b2c1493711883c208f2fcb"
        )
        assert {p.kind: p.source_sha256 for p in profile.provenance} == {
            "documentation": profile.documentation_sha256,
            "offline_fixture": profile.offline_fixture_sha256,
            "independent_live": profile.independent_live_sha256,
        }
        assert all(
            p.recorded_on == profile.qualification_date for p in profile.provenance
        )
        assert profile.authority_sha256 in record
        audit = json.loads(
            (
                Path(__file__).parents[1] / "docs/vertex-evidence-verification.json"
            ).read_text()
        )
        accepted = next(
            r
            for r in audit["records"]
            if r["authentication_mode"] == profile.authentication_mode
        )
        assert accepted["profile_authority_sha256"] == profile.authority_sha256
        assert accepted["source_commit"] == profile.source_commit
        assert accepted["plan_sha256"] == profile.plan_sha256
        assert (
            accepted["canonical_report_sha256_excluding_self"]
            == profile.independent_live_sha256
        )
        assert (
            accepted["accepted_requests"]
            == accepted["valid_assessments"]
            == accepted["exact_model_matches"]
            == 4
        )
        assert len({c["submission_id"] for c in accepted["cases"]}) == 4
        assert all(
            c["assessment_valid"] and c["assessment_bytes"] > 0
            for c in accepted["cases"]
        )
        assert audit["complete_evidence_root_inventory"][
            "bytes_membership_and_all_timestamps_unchanged"
        ]
        assert (
            registry._profile_for(
                profile.scope,
                MECHANISM,
                route_sha256=profile.route_sha256,
                authentication_mode=profile.authentication_mode,
                location="global",
            )
            == profile
        )


@pytest.fixture(params=["adc", "api_key"])
def qualified(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    from vertex_production_fixture import activate

    activate(monkeypatch, request.param)
    return request.param


@pytest.mark.parametrize("pairwise", [False, True])
@pytest.mark.parametrize("policy", ["native", "auto", "text"])
@pytest.mark.parametrize("strict", [False, True])
def test_exact_admission_and_complete_frozen_bytes(
    tmp_path: Path, pairwise: bool, policy: str, strict: bool, qualified: str
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
        {"mechanism": "vertex.generate_content.response_schema"},
        {"model": "gemini-another-model"},
    ],
)
def test_untested_native_fails_and_auto_is_explicitly_unknown(
    tmp_path: Path, settings: dict[str, Any], qualified: str
) -> None:
    for policy, strict in (("native", False), ("auto", True)):
        with pytest.raises(ValueError):
            prepare(tmp_path, policy=policy, strict=strict, **settings)
    wire, _, _, _ = prepare(tmp_path, policy="auto", **settings)
    assert wire.output_commitment.selection.mechanism == "text"
    assert wire.output_commitment.selection.native_support == "unknown"


def test_grouped_and_all_unqualified_features_stay_unknown(
    tmp_path: Path, qualified: str
) -> None:
    spec, dep, _ = case(tmp_path, policy="native", grouped=True)
    spec.model = MODEL
    context = production_context(spec)
    with pytest.raises(TransportIntegrityError):
        build_output_commitment_v7(spec, dep.request, context)
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
    output = build_output_commitment_v7(spec, dep.request, context)
    assert output.selection.mechanism == "text"
    spec.strict_capabilities = True
    with pytest.raises(TransportIntegrityError):
        build_output_commitment_v7(spec, dep.request, context)


@pytest.mark.parametrize(
    "changes",
    [
        {"provider": "gemini", "api_version": "v1beta"},
        {"sdk_version": "2.35.0"},
        {"api_version": "v1"},
        {"endpoint": "interactions.create"},
        {"sdk_package": "openai"},
    ],
)
def test_no_foreign_scope_authority(
    tmp_path: Path, changes: dict[str, str], qualified: str
) -> None:
    _, _, _, context = prepare(tmp_path)
    scope = context.scope.model_copy(update=changes)
    assert (
        registry._profile_for(
            scope,
            MECHANISM,
            route_sha256=context.runtime.route_sha256,
            authentication_mode=context.runtime.auth_mode,
            location=context.runtime.location,
        )
        is None
    )
    with pytest.raises(TransportIntegrityError):
        production_context_digest(replace(context, scope=scope))


def test_generation_qualification_does_not_expand(
    tmp_path: Path, qualified: str
) -> None:
    for generation in (
        GenerationSpec(max_output_tokens=512),
        GenerationSpec(temperature=0, top_p=1, max_output_tokens=512),
    ):
        wire, _, _, context = prepare(tmp_path, policy="native", generation=generation)
        facts = registry.production_generation_capabilities(context.scope)
        assert facts is not None and facts.fixture_tested and not facts.live_qualified
        assert wire.generation == generation
    capabilities = VertexAdapter.capabilities_for(
        "vertex", "models.generate_content", MODEL, mechanism=MECHANISM
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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    changes: dict[str, Any],
    qualified: str,
) -> None:
    # Deliberately bypass the normal safe model_copy to simulate forged state.
    original = next(
        p for p in registry._QUALIFIED_PROFILES if p.authentication_mode == qualified
    )
    profile = BaseModel.model_copy(original, update=changes)
    monkeypatch.setattr(registry, "_QUALIFIED_PROFILES", (profile,))
    with pytest.raises(ValueError):
        prepare(tmp_path, policy="native")


@pytest.mark.parametrize("pairwise", [False, True])
@pytest.mark.asyncio
async def test_actual_sdk_bytes_native_failure_and_frozen_recovery(
    tmp_path: Path, pairwise: bool, qualified: str
) -> None:
    spec = production_spec(
        tmp_path / "inputs",
        pairwise=pairwise,
        traits=1,
        grouping="independent",
        explanations=False,
    )
    spec.model = MODEL
    spec.prompt.structured_output = "auto"
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

    adapter = VertexAdapter(
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
        assert b"responseJsonSchema" not in sent[0]
        assert wire.transport_commitment.mechanism == "text"
        assert canonical_bytes(wire.transport_commitment)
    finally:
        await adapter.aclose()


@pytest.mark.parametrize(
    "changed",
    [
        {"project": "other-synthetic-project"},
        {"location": "us-central1"},
        {"auth_mode": "adc"},
    ],
)
def test_route_admission_never_transfers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: dict[str, str]
) -> None:
    from vertex_production_fixture import activate

    from judgmentllm.vertex_config import VertexRuntimeConfiguration
    from judgmentllm.vertex_transport import _authority_sha256

    activate(monkeypatch, "api_key")
    wire, spec, dep, context = prepare(tmp_path, policy="native")
    # Retain only the independently qualified API-key record. An ADC record
    # cannot be inferred from it even for the identical project/global route.
    monkeypatch.setattr(
        registry,
        "_QUALIFIED_PROFILES",
        tuple(
            p
            for p in registry._QUALIFIED_PROFILES
            if p.authentication_mode == "api_key"
        ),
    )
    runtime = VertexRuntimeConfiguration(
        **cast(Any, {**vars(context.runtime), **changed})
    )
    mismatched = replace(context, runtime=runtime)
    assert _support(mismatched, ("rubric_scores",), MECHANISM) == "unknown"
    assert _authority_sha256(mismatched) != _authority_sha256(context)
    with pytest.raises(TransportIntegrityError):
        prepare_production_request(spec, dep, wire.transport_plan.identity, mismatched)
    spec.prompt.structured_output = "auto"
    fallback = prepare_production_request(
        spec, dep, wire.transport_plan.identity, mismatched
    )
    assert fallback.transport_commitment.mechanism == "text"
    spec.strict_capabilities = True
    with pytest.raises(TransportIntegrityError):
        prepare_production_request(spec, dep, wire.transport_plan.identity, mismatched)


def test_known_unsupported_and_duplicate_registry_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, qualified: str
) -> None:
    profile = next(
        p for p in registry._QUALIFIED_PROFILES if p.authentication_mode == qualified
    )
    monkeypatch.setattr(
        registry,
        "_QUALIFIED_PROFILES",
        (profile.model_copy(update={"unsupported_features": ("rubric_grouped",)}),),
    )
    _, _, _, context = prepare(tmp_path, policy="native")
    assert _support(context, ("rubric_grouped",), MECHANISM) == "unsupported"
    capabilities = VertexAdapter.capabilities_for(
        "vertex",
        "models.generate_content",
        MODEL,
        mechanism=MECHANISM,
        runtime=context.runtime,
    )
    assert capabilities.grouped is False
    broader = profile.model_copy(
        update={
            "supported_features": (
                *profile.supported_features,
                "rubric_grouped",
                "pairwise_ties",
                "rubric_explanations_together",
                "rubric_scores_first",
                "rubric_explanations_first",
                "pairwise_explanations",
            )
        }
    )
    monkeypatch.setattr(registry, "_QUALIFIED_PROFILES", (broader,))
    capabilities = VertexAdapter.capabilities_for(
        "vertex",
        "models.generate_content",
        MODEL,
        mechanism=MECHANISM,
        runtime=context.runtime,
    )
    assert (
        capabilities.grouped is capabilities.explanations is capabilities.ties is True
    )
    monkeypatch.setattr(registry, "_QUALIFIED_PROFILES", (profile, profile))
    with pytest.raises(ValueError, match="registry conflicts"):
        registry._profile_for(
            profile.scope,
            MECHANISM,
            route_sha256=profile.route_sha256,
            authentication_mode=profile.authentication_mode,
            location="global",
        )
    with pytest.raises(TransportIntegrityError):
        prepare(tmp_path, policy="native")


@pytest.mark.asyncio
async def test_harness_context_cannot_supply_production_authority(
    tmp_path: Path, qualified: str
) -> None:
    from judgmentllm.providers.protocol import ProviderIntegrityError
    from judgmentllm.vertex_transport import _qualification_context

    wire, spec, dep, context = prepare(tmp_path, policy="native")
    probe = _qualification_context(
        spec, source_commit="a" * 40, runtime=context.runtime
    )
    candidate = prepare_production_request(
        spec, dep, wire.transport_plan.identity, probe
    )
    assert candidate.output_commitment.selection.authority_kind == "qualification_probe"
    production = VertexAdapter(spec.endpoint, MODEL, context=context)
    ungranted = VertexAdapter(spec.endpoint, MODEL, context=probe)
    ident = wire.transport_plan.identity
    for adapter, request in [
        (production, candidate),
        (ungranted, candidate),
        (ungranted, wire),
    ]:
        with pytest.raises(ProviderIntegrityError):
            await adapter.submit(
                request,
                submission_id=ident.submission_id,
                correlation_id=ident.correlation_id,
            )
        assert adapter.actual_call_count == 0
        await adapter.aclose()


@pytest.mark.parametrize("pairwise", [False, True])
@pytest.mark.parametrize("policy", ["native", "auto"])
@pytest.mark.asyncio
async def test_public_production_workflow_native_and_no_call_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    qualified: str,
    pairwise: bool,
    policy: str,
) -> None:
    from judgmentllm import lifecycle
    from judgmentllm.providers.vertex import _INVOKING

    spec = production_spec(
        tmp_path / "inputs",
        pairwise=pairwise,
        traits=1,
        grouping="independent",
        explanations=False,
    )
    spec.model = MODEL
    spec.prompt.structured_output = cast(Any, policy)
    spec.prompt.native_mechanism = MECHANISM
    calls: list[bytes] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        prepared = _INVOKING.get()
        assert isinstance(prepared, PreparedRequestV7)
        assert request.content == prepared.payload
        assert prepared.transport_commitment.mechanism == "text"
        assert "projects/synthetic-accepted-project/locations/global/" in str(
            request.url
        )
        calls.append(request.content)
        assert spec.rubric is not None or pairwise
        output = (
            {"winner_id": "essay_01"}
            if pairwise
            else {
                "scores": [
                    {
                        "trait_id": "01",
                        "category_label": cast(Any, spec.rubric)
                        .traits[0]
                        .levels[0]
                        .label,
                    }
                ]
            }
        )  # pyright: ignore[reportOptionalMemberAccess]
        return httpx2.Response(
            200, json=envelope(json.dumps(output), modelVersion=MODEL)
        )

    adapter = VertexAdapter(
        spec.endpoint, MODEL, http_transport=httpx2.MockTransport(respond)
    )

    def injected(*args: Any, **kwargs: Any) -> VertexAdapter:
        return adapter

    monkeypatch.setattr(lifecycle, "_execution_adapter", injected)
    store = tmp_path / "store"
    if policy == "native":
        from judgmentllm import LifecycleError

        with pytest.raises(LifecycleError):
            await lifecycle.arun_live(spec, store=store)
        assert calls == []
        await adapter.aclose()
        return
    result = await lifecycle.arun_live(spec, store=store)
    assert (
        result.run_status == "completed" and len(calls) == 1 and adapter._client is None
    )
    await lifecycle.aresume_run(result.run_id, spec=spec, store=store)
    assert len(calls) == 1
    with ReadOnlyRepository(store) as repository:
        rows = tuple(repository.iter_results(result.run_id))
        assert len(rows) == 1 and rows[0].status == "complete"
        assert rows[0].provider == "vertex" and rows[0].model == MODEL
        assert rows[0].explanation is None
        if pairwise:
            from judgmentllm.results import PairwiseResult

            assert isinstance(rows[0], PairwiseResult)
            assert rows[0].winner_id == "002" and not rows[0].tie


@pytest.mark.parametrize(
    "changes",
    [
        {"scope": {"provider": "gemini"}},
        {"scope": {"endpoint": "interactions.create"}},
        {"scope": {"model_scope": "unresolved", "model": None}},
        {"scope": {"model": "unqualified-model"}},
        {"scope": {"sdk_package": "openai"}},
        {"scope": {"sdk_version": "2.35.0"}},
        {"scope": {"api_version": "v1"}},
        {"schema_dialect": "vertex.cloud-schema.v1"},
        {"unsupported_features": ("rubric_grouped", "rubric_grouped")},
        {"offline_fixture_sha256": "0" * 64},
        {"independent_live_sha256": "0" * 64},
        {"support_status": "unknown"},
        {"assessment_behavior": "unknown"},
        {"translator_version": 3},
        {"qualification_date": date(2026, 11, 10)},
    ],
)
def test_reviewed_profile_rejects_inconsistent_provenance_and_scope(
    changes: dict[str, Any],
) -> None:
    profile = registry._QUALIFIED_PROFILES[0]
    if "scope" in changes:
        changes = {"scope": profile.scope.model_copy(update=changes["scope"])}
    with pytest.raises(ValueError):
        registry._QualifiedProfile.model_validate({**dict(profile), **changes})


@pytest.mark.asyncio
async def test_adapter_rechecks_native_authority_before_sdk_construction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, qualified: str
) -> None:
    from judgmentllm import vertex_transport as transport
    from judgmentllm.providers.protocol import ProviderIntegrityError

    wire, spec, _, context = prepare(tmp_path, policy="native")
    adapter = VertexAdapter(spec.endpoint, MODEL, context=context)

    def unavailable(*args: Any) -> Literal["unknown"]:
        return "unknown"

    monkeypatch.setattr(transport, "_support", unavailable)
    ident = wire.transport_plan.identity
    try:
        with pytest.raises(ProviderIntegrityError, match="independent reviewed"):
            await adapter.submit(
                wire,
                submission_id=ident.submission_id,
                correlation_id=ident.correlation_id,
            )
        assert adapter._client is None and adapter.actual_call_count == 0
    finally:
        await adapter.aclose()


@pytest.mark.parametrize("forgery", ["support", "probe_kind", "auto_text"])
def test_forged_frozen_selection_cannot_reinterpret_reviewed_context(
    tmp_path: Path, qualified: str, forgery: str
) -> None:
    from judgmentllm.vertex_transport import _verify_selection

    wire, spec, _, context = prepare(
        tmp_path,
        policy="auto"
        if forgery == "auto_text"
        else "text"
        if forgery == "support"
        else "native",
    )
    selection = wire.output_commitment.selection
    changes = (
        {"native_support": "unknown"}
        if forgery == "support"
        else {"kind": "qualification_probe"}
        if forgery == "probe_kind"
        else {"kind": "text", "mechanism": "text"}
    )
    forged = BaseModel.model_copy(selection, update=changes)
    with pytest.raises(TransportIntegrityError):
        _verify_selection(
            cast(Any, forged), spec, wire.output_commitment.contract, context, None
        )


@pytest.mark.parametrize("pairwise", [False, True])
def test_new_presentation_cannot_borrow_historical_native_authority(
    tmp_path: Path, pairwise: bool, qualified: str
) -> None:
    with pytest.raises(ValueError):
        prepare(tmp_path, pairwise=pairwise, policy="native", presentation_version=2)
    with pytest.raises(ValueError):
        prepare(
            tmp_path,
            pairwise=pairwise,
            policy="auto",
            strict=True,
            presentation_version=2,
        )
    wire, *_ = prepare(
        tmp_path, pairwise=pairwise, policy="auto", presentation_version=2
    )
    assert wire.transport_commitment.presentation_version == 2
    assert wire.output_commitment.selection.mechanism == "text"
    assert wire.output_commitment.selection.native_support == "unknown"


@pytest.mark.asyncio
async def test_historical_native_wire_still_executes_with_original_authority(
    tmp_path: Path, qualified: str
) -> None:
    wire, spec, _, context = prepare(tmp_path, policy="native", pairwise=True)
    assert wire.output_commitment.contract.presentation_version == 1
    sent: list[bytes] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        sent.append(request.content)
        assert request.content == wire.payload
        return httpx2.Response(
            200, json=envelope('{"winner_id":"002"}', modelVersion=MODEL)
        )

    adapter = VertexAdapter(
        spec.endpoint,
        spec.model,
        context=context,
        http_transport=httpx2.MockTransport(respond),
    )
    identity = wire.transport_plan.identity
    arguments = {
        "submission_id": identity.submission_id,
        "correlation_id": identity.correlation_id,
    }
    try:
        await adapter.submit(wire, **arguments)
        response = await adapter.retrieve(wire, **arguments)
        assert adapter.parse(response).kind == "response"
        assert adapter.actual_call_count == len(sent) == 1
        assert b"responseJsonSchema" in sent[0]
    finally:
        await adapter.aclose()
