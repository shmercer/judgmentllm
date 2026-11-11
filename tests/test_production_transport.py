"""Offline production byte commitments without synthetic execution authority."""

# pyright: reportPrivateUsage=false
from __future__ import annotations

import json
import os
import socket
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any, Literal, cast

import pytest

from judgmentllm.config import (
    ExecutionSpec,
    GenerationSpec,
    InputSpec,
    PromptSpecV3,
    RunSpecV3,
)
from judgmentllm.evidence.scope import EvidenceProvenance
from judgmentllm.execution.retry import subset_dependency
from judgmentllm.models import (
    Level,
    PairTask,
    PairwiseCriterion,
    ResponseItem,
    Rubric,
    Trait,
)
from judgmentllm.persistence.hashing import (
    bytes_digest,
    canonical_bytes,
    semantic_digest,
)
from judgmentllm.planning import plan_record
from judgmentllm.preparation.session import RequestDependency
from judgmentllm.production_registry import _QualifiedProfile
from judgmentllm.production_transport import (
    OutputCommitmentV4,
    PreparedRequestV4,
    ProductionContext,
    TransportCommitmentV2,
    _qualification_context,
    build_output_commitment_v4,
    prepare_production_request,
    production_context,
    production_context_digest,
    production_context_for_identity,
)
from judgmentllm.transport import TransportIntegrityError, physical_identity


@pytest.fixture(autouse=True)
def external_network_forbidden(monkeypatch: pytest.MonkeyPatch) -> None:
    original = socket.socket.connect
    original_dns = socket.getaddrinfo

    def connect(current: socket.socket, address: Any) -> Any:
        if current.family in {socket.AF_INET, socket.AF_INET6} and address[0] not in {
            "127.0.0.1",
            "::1",
        }:
            raise AssertionError("External network blocked offline")
        return original(current, address)

    def dns(host: Any, *args: Any, **kwargs: Any) -> Any:
        if host not in {"127.0.0.1", "::1", "localhost"}:
            raise AssertionError("External DNS blocked offline")
        return original_dns(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "getaddrinfo", dns)


def case(
    tmp_path: Path,
    *,
    endpoint: str = "responses",
    policy: Literal["auto", "native", "text"] = "text",
    layout: Literal["together", "scores_first", "explanations_first"] = "together",
    explanations: bool = False,
    grouped: bool = True,
    pairwise: bool = False,
    ties: bool = False,
    generation: GenerationSpec | None = None,
    strict: bool = False,
) -> tuple[RunSpecV3, RequestDependency, dict[str, ResponseItem]]:
    traits = [
        Trait(
            trait_id="01",
            name="Clarity",
            description="Synthetic clarity.",
            levels=[Level(label="low 雪"), Level(label="high é")],
        ),
        Trait(
            trait_id="02",
            name="Detail",
            description="Synthetic detail.",
            levels=[Level(label="limited"), Level(label="complete")],
        ),
    ]
    if not grouped:
        traits = traits[:1]
    mechanism = (
        "openai.responses.text.format"
        if endpoint == "responses"
        else "openai.chat.response_format"
    )
    responses = {
        ident: ResponseItem(
            item_id=ident, text=f"Synthetic response {ident}: a seed grows. 雪"
        )
        for ident in ("001", "002")
    }
    spec = RunSpecV3(
        task_kind="pairwise" if pairwise else "rubric",
        input=InputSpec(path=tmp_path / "responses.jsonl"),
        rubric=None
        if pairwise
        else Rubric(rubric_id="rubric-001", name="Synthetic", traits=traits),
        criteria=[
            PairwiseCriterion(
                trait_id="01", name="Clarity", description="Compare clarity."
            )
        ]
        if pairwise
        else [],
        pairs=InputSpec(path=tmp_path / "pairs.jsonl") if pairwise else None,
        provider="openai",
        endpoint=endpoint,
        model="offline-qualified-model",
        generation=generation,
        execution=ExecutionSpec(
            grouping="grouped" if grouped and not pairwise else "independent",
            explanations=explanations,
        ),
        prompt=PromptSpecV3(
            structured_output=policy,
            native_mechanism=cast(Any, mechanism),
            output_order=layout,
            file=tmp_path / "tie.j2" if ties else None,
        ),
        allow_ties=ties,
        strict_capabilities=strict,
    )
    record = (
        PairTask(
            pair_id="pair-001",
            pair_uid="pair-001-occurrence",
            left_item_id="001",
            right_item_id="002",
            trait_id="01",
            presented_order=["002", "001"],
        )
        if pairwise
        else responses["001"]
    )
    request = next(plan_record(spec, record))
    return (
        spec,
        RequestDependency(request=request, dependency_sha256="0" * 64),
        responses,
    )


def wire_for(
    tmp_path: Path, *, probe: bool = False, **kwargs: Any
) -> tuple[PreparedRequestV4, RunSpecV3, RequestDependency, ProductionContext]:
    spec, dep, records = case(tmp_path, **kwargs)
    context = (
        _qualification_context(spec, source_commit="a" * 40)
        if probe
        else production_context(spec)
    )

    def lookup(kind: str, ident: str) -> ResponseItem | PairTask:
        if kind == "pair":
            assert dep.request.pair is not None
            return dep.request.pair
        return records[ident]

    template = (
        "Compare.\n{{ criterion }}\n{{ first_response }}\n{{ second_response }}\n{{ output_instructions }}"
        if spec.allow_ties
        else None
    )
    wire = prepare_production_request(
        spec,
        dep,
        physical_identity("run-001", dep.request.request_uid, "attempt-001"),
        context,
        lookup=cast(Any, lookup),
        template=template,
    )
    return wire, spec, dep, context


@pytest.mark.parametrize("endpoint", ["responses", "chat.completions"])
@pytest.mark.parametrize("policy", ["text", "native"])
@pytest.mark.parametrize(
    "scenario",
    [
        "scores",
        "single",
        "together",
        "scores_first",
        "explanations_first",
        "pairwise",
        "ties",
    ],
)
def test_exact_complete_production_body(
    tmp_path: Path, endpoint: str, policy: Literal["text", "native"], scenario: str
) -> None:
    wire, _, _, _ = wire_for(
        tmp_path,
        endpoint=endpoint,
        policy=policy,
        probe=policy == "native",
        grouped=scenario != "single",
        pairwise=scenario in {"pairwise", "ties"},
        ties=scenario == "ties",
        explanations=scenario not in {"scores", "single", "pairwise"},
        layout=scenario
        if scenario in {"scores_first", "explanations_first"}
        else "together",
        generation=GenerationSpec(temperature=0.0, top_p=1, max_output_tokens=64),
    )
    body = json.loads(wire.payload)
    expected_keys = [
        "model",
        "stream",
        "input" if endpoint == "responses" else "messages",
        "max_output_tokens" if endpoint == "responses" else "max_completion_tokens",
    ]
    if endpoint == "chat.completions":
        expected_keys += ["n"] + (["response_format"] if policy == "native" else [])
    expected_keys += ["store", "temperature"]
    if endpoint == "responses" and policy == "native":
        expected_keys.append("text")
    expected_keys += ["top_p"] + (["truncation"] if endpoint == "responses" else [])
    assert list(body) == expected_keys
    assert body["stream"] is False and body["store"] is False
    assert type(body["temperature"]) is float and type(body["top_p"]) is int
    assert "雪" in wire.payload.decode()
    assert (
        wire.payload
        == json.dumps(
            body, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode()
    )
    assert wire.payload_sha256 == bytes_digest(wire.payload)
    assert b"judgmentllm-native-offline-v1" not in wire.payload
    assert "transport_plan" not in body and "request_uid" not in body
    assert wire.transport_commitment.identity.request_uid == wire.request_uid
    assert wire.transport_commitment.payload_bytes == len(wire.payload)
    assert wire.output_commitment.commitment_version == 4
    assert wire.transport_commitment.transport_commitment_version == 2
    if policy == "native":
        projection = wire.output_commitment.projection
        assert projection is not None and projection.projection_version == 1
        fragment = json.loads(projection.format_fragment_text)
        for key, value in fragment.items():
            assert body[key] == value
        assert wire.output_commitment.repetition == "omit"
    else:
        assert wire.output_commitment.schema_text in wire.rendered_text
        assert "response_format" not in body and "text" not in body


@pytest.mark.parametrize("endpoint", ["responses", "chat.completions"])
@pytest.mark.parametrize("policy", ["text", "native"])
@pytest.mark.parametrize(
    "scenario",
    [
        "scores",
        "single",
        "together",
        "scores_first",
        "explanations_first",
        "pairwise",
        "ties",
    ],
)
@pytest.mark.parametrize("explicit_generation", [False, True])
@pytest.mark.asyncio
async def test_actual_pinned_sdk_serializes_committed_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
    policy: Literal["text", "native"],
    scenario: str,
    explicit_generation: bool,
) -> None:
    try:
        import httpx2
        import openai
    except ImportError:
        if os.environ.get("JUDGMENTLLM_REQUIRE_NATIVE_SDK_PROBES") == "1":
            pytest.fail(
                "Required OpenAI SDK request-byte probe dependencies unavailable"
            )
        pytest.skip("OpenAI extra unavailable")
    wire, _, _, _ = wire_for(
        tmp_path,
        endpoint=endpoint,
        policy=policy,
        probe=policy == "native",
        grouped=scenario != "single",
        pairwise=scenario in {"pairwise", "ties"},
        ties=scenario == "ties",
        explanations=scenario not in {"scores", "single", "pairwise"},
        layout=scenario
        if scenario in {"scores_first", "explanations_first"}
        else "together",
        generation=GenerationSpec(temperature=0.0, top_p=1, max_output_tokens=64)
        if explicit_generation
        else None,
    )

    async def denied_transport(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("External provider network transport blocked offline")

    monkeypatch.setattr(
        httpx2.AsyncHTTPTransport, "handle_async_request", denied_transport
    )
    for key in (
        "OPENAI_ORG_ID",
        "OPENAI_PROJECT_ID",
        "OPENAI_ADMIN_KEY",
        "OPENAI_WEBHOOK_SECRET",
        "OPENAI_CUSTOM_HEADERS",
        "OPENAI_BASE_URL",
    ):
        monkeypatch.delenv(key, raising=False)
    outgoing: list[bytes] = []

    async def handler(request: httpx2.Request) -> httpx2.Response:
        outgoing.append(request.content)
        assert request.headers["Authorization"] == "Bearer synthetic-offline-key"
        return httpx2.Response(
            400,
            json={
                "error": {
                    "message": "offline fixture",
                    "type": "invalid_request_error",
                    "code": "fixture",
                }
            },
        )

    async with httpx2.AsyncClient(
        transport=httpx2.MockTransport(handler), trust_env=False
    ) as http:
        async with openai.AsyncOpenAI(
            api_key="synthetic-offline-key",
            http_client=http,
            max_retries=0,
            timeout=5,
            base_url="https://api.openai.com/v1",
        ) as client:
            # Avoid the sandbox's unrelated async platform-header thread probe.
            client._platform = "Linux"
            with pytest.raises(openai.BadRequestError):
                if endpoint == "responses":
                    await client.responses.create(**json.loads(wire.payload))
                else:
                    await client.chat.completions.create(**json.loads(wire.payload))
    assert openai.__version__ == "3.42.1"
    assert outgoing == [wire.payload]


def test_omitted_settings_and_candidate_final_binding(tmp_path: Path) -> None:
    wire, spec, dep, candidate = wire_for(tmp_path, policy="native", probe=True)
    final = _qualification_context(spec, source_commit="a" * 40, plan_sha256="b" * 64)
    final_wire = prepare_production_request(
        spec, dep, wire.transport_plan.identity, final
    )
    assert final_wire.payload == wire.payload
    assert final_wire.transport_commitment != wire.transport_commitment
    assert production_context_digest(final) != production_context_digest(candidate)
    assert (
        b"temperature" not in wire.payload and b"max_output_tokens" not in wire.payload
    )
    assert b"top_p" not in wire.payload
    assert repr(candidate) == "ProductionContext(<scoped production authority>)"
    assert not candidate.trusted_synthetic


def test_unqualified_native_and_strict_auto_cannot_execute(tmp_path: Path) -> None:
    for policy, strict in (("native", False), ("auto", True)):
        with pytest.raises((TransportIntegrityError, ValueError)):
            wire_for(tmp_path, policy=policy, strict=strict)
    wire, _, _, _ = wire_for(tmp_path, policy="auto")
    assert wire.output_commitment.selection.mechanism == "text"
    assert wire.output_commitment.selection.native_support == "unknown"


def test_frozen_reconstruction_and_subset_lineage(tmp_path: Path) -> None:
    wire, spec, dep, context = wire_for(tmp_path, policy="native", probe=True)
    frozen = prepare_production_request(
        spec,
        dep,
        wire.transport_plan.identity,
        context,
        frozen=wire.transport_commitment,
    )
    assert frozen == wire
    subset = subset_dependency(dep, wire.task_ids[:1])
    retry = prepare_production_request(
        spec,
        subset,
        physical_identity("run-001", dep.request.request_uid, "attempt-002"),
        context,
        parent=wire.transport_commitment,
    )
    assert (
        retry.transport_commitment.parent_commitment_sha256
        == wire.transport_commitment.commitment_sha256
    )
    assert retry.transport_commitment.mechanism == wire.transport_commitment.mechanism
    assert retry.task_ids == wire.task_ids[:1]
    assert retry.payload != wire.payload
    assert (
        prepare_production_request(
            spec,
            subset,
            retry.transport_plan.identity,
            context,
            frozen=retry.transport_commitment,
            parent=wire.transport_commitment,
        )
        == retry
    )
    with pytest.raises(TransportIntegrityError):
        prepare_production_request(
            spec,
            subset,
            retry.transport_plan.identity,
            context,
            frozen=retry.transport_commitment,
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("payload", b"{}"),
        ("rendered_text", "changed"),
        ("model", "changed"),
        ("task_ids", ("changed",)),
        ("dependency_sha256", "f" * 64),
    ],
)
def test_current_prepared_objects_revalidate_without_content_leaks(
    tmp_path: Path, field: str, value: Any
) -> None:
    wire, _, _, _ = wire_for(tmp_path)
    with pytest.raises(ValueError) as exc:
        PreparedRequestV4.model_validate({**dict(wire), field: value})
    assert "seed grows" not in str(exc.value)
    assert "low 雪" not in str(exc.value)


def test_receipt_round_trip_duplicate_ingress_and_versions(tmp_path: Path) -> None:
    wire, _, _, _ = wire_for(tmp_path)
    receipt = wire.transport_commitment
    assert (
        TransportCommitmentV2.model_validate_json(canonical_bytes(receipt)) == receipt
    )
    for changed in (
        {"transport_commitment_version": 2.0},
        {"store": 0},
        {"commitment_sha256": "a" * 64},
        {"payload_sha256": "b" * 64},
    ):
        with pytest.raises(ValueError):
            TransportCommitmentV2.model_validate({**dict(receipt), **changed})
    raw = canonical_bytes(receipt)
    with pytest.raises(ValueError):
        TransportCommitmentV2.model_validate_json(b'{"store":false,' + raw[1:])
    changed = receipt.model_dump(mode="json", exclude={"commitment_sha256"})
    changed["payload_sha256"] = "b" * 64
    changed["commitment_sha256"] = semantic_digest("transport-commitment-v2", changed)
    with pytest.raises(ValueError):
        PreparedRequestV4.model_validate(
            {
                **dict(wire),
                "transport_commitment": TransportCommitmentV2.model_validate_json(
                    canonical_bytes(changed)
                ),
            }
        )


def test_credential_presence_does_not_authorize_native(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-present-key")
    with pytest.raises((ValueError, TransportIntegrityError)):
        wire_for(tmp_path, policy="native")
    context = production_context_for_identity(
        "openai", "responses", "offline-qualified-model"
    )
    assert context == production_context(case(tmp_path)[0])
    with pytest.raises(TransportIntegrityError):
        production_context_for_identity(
            "openai", "responses", "judgmentllm-synthetic-native-v1"
        )
    with pytest.raises(TransportIntegrityError):
        production_context_digest(replace(context, kind=cast(Any, "synthetic")))
    with pytest.raises(TransportIntegrityError):
        production_context_digest(replace(context, qualification_plan_sha256="f" * 64))


def test_controlled_registry_exact_features_and_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from judgmentllm import production_registry

    spec, dep, _ = case(tmp_path, policy="native")
    context = production_context(spec)
    profile = _QualifiedProfile(
        qualification_date=date(2026, 11, 7),
        source_commit="a" * 40,
        support_status="supported",
        request_acceptance="accepted",
        assessment_behavior="verified",
        native_schema_behavior="verified",
        provenance=tuple(
            EvidenceProvenance(
                kind=cast(Any, kind),
                source_sha256=digest * 64,
                recorded_on=date(2026, 11, 7),
            )
            for kind, digest in (
                ("documentation", "a"),
                ("offline_fixture", "b"),
                ("independent_live", "c"),
            )
        ),
        scope=context.scope,
        supported_features=(
            "rubric_scores",
            "rubric_grouped",
            "assessment_presentation_v2",
        ),
        documentation_sha256="a" * 64,
        offline_fixture_sha256="b" * 64,
        independent_live_sha256="c" * 64,
    )
    monkeypatch.setattr(production_registry, "_QUALIFIED_PROFILES", (profile,))
    output = build_output_commitment_v4(spec, dep.request, context)
    assert output.selection.kind == "native"
    assert output.selection.native_support == "supported"
    assert OutputCommitmentV4.model_validate_json(canonical_bytes(output)) == output
    changed = spec.model_copy(update={"model": "another-exact-model"})
    with pytest.raises(ValueError):
        build_output_commitment_v4(changed, dep.request, production_context(changed))
    monkeypatch.setattr(
        production_registry,
        "_QUALIFIED_PROFILES",
        (
            profile.model_copy(
                update={
                    "supported_features": (),
                    "unsupported_features": ("rubric_scores",),
                }
            ),
        ),
    )
    with pytest.raises(ValueError):
        build_output_commitment_v4(spec, dep.request, context)


def test_protected_templates_limits_and_mutation_guard(tmp_path: Path) -> None:
    from judgmentllm.prompts import RenderLimits

    wire, spec, dep, context = wire_for(tmp_path)
    for template in (
        "{{ response }}",
        "{{ rubric }} {{ response }}",
        "{{ output_instructions }}",
    ):
        with pytest.raises(TransportIntegrityError):
            prepare_production_request(
                spec, dep, wire.transport_plan.identity, context, template=template
            )
    small = spec.model_copy(
        update={
            "prompt": spec.prompt.model_copy(
                update={"limits": RenderLimits(max_output_bytes=30)}
            )
        }
    )
    with pytest.raises(TransportIntegrityError):
        prepare_production_request(small, dep, wire.transport_plan.identity, context)

    def lookup(
        kind: Literal["response", "pair"], identifier: str
    ) -> ResponseItem | PairTask:
        assert dep.request.response is not None
        spec.model = "mutated-private-canary"
        return dep.request.response

    with pytest.raises(TransportIntegrityError):
        prepare_production_request(
            spec, dep, wire.transport_plan.identity, context, lookup=lookup
        )


def test_scope_generation_and_parent_changes_fail_closed(tmp_path: Path) -> None:
    wire, spec, dep, context = wire_for(tmp_path)
    changed_context = replace(
        context, scope=context.scope.model_copy(update={"sdk_version": "3.42.2"})
    )
    with pytest.raises(TransportIntegrityError):
        production_context_digest(changed_context)
    changed = spec.model_copy(
        update={"generation": GenerationSpec(max_output_tokens=10)}
    )
    with pytest.raises(TransportIntegrityError):
        prepare_production_request(
            changed,
            dep,
            wire.transport_plan.identity,
            context,
            frozen=wire.transport_commitment,
        )
    with pytest.raises(TransportIntegrityError):
        prepare_production_request(
            spec,
            dep,
            physical_identity("another-run", dep.request.request_uid, "attempt-002"),
            context,
            parent=wire.transport_commitment,
        )
    with pytest.raises(TransportIntegrityError):
        prepare_production_request(
            spec,
            dep,
            wire.transport_plan.identity,
            context,
            parent=wire.transport_commitment,
        )
    with pytest.raises(TransportIntegrityError):
        prepare_production_request(
            spec,
            dep,
            physical_identity("run-001", "another-request", "attempt-002"),
            context,
        )


def test_pure_preparation_never_discovers_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec, dep, _ = case(tmp_path)
    context = production_context(spec)
    original_getitem = type(os.environ).__getitem__

    def environment_getitem(environment: Any, key: Any) -> Any:
        if os.fsdecode(key).startswith(("OPENAI_", "AWS_", "GOOGLE_", "GEMINI_")):
            raise AssertionError("Credential discovery forbidden during preparation")
        return original_getitem(environment, key)

    with monkeypatch.context() as restricted:
        restricted.setattr(type(os.environ), "__getitem__", environment_getitem)
        wire = prepare_production_request(
            spec,
            dep,
            physical_identity("run-001", dep.request.request_uid, "attempt-001"),
            context,
        )
    receipt = canonical_bytes(wire.transport_commitment)
    assert b"seed grows" not in receipt and "low 雪" not in receipt.decode()
    assert "seed grows" not in repr(wire)
    assert "low 雪" not in repr(wire.output_commitment)


def test_output_ingress_sanitizes_invalid_private_configuration(tmp_path: Path) -> None:
    spec, dep, _ = case(tmp_path)
    context = production_context(spec)
    changed = spec.model_copy(
        update={"private-canary-config-field": "private-canary-value"}
    )
    with pytest.raises(ValueError) as exc:
        build_output_commitment_v4(changed, dep.request, context)
    assert "private-canary" not in str(exc.value)


def _reviewed_profile(context: ProductionContext, **changes: Any) -> _QualifiedProfile:
    fields: dict[str, Any] = {
        "scope": context.scope,
        "qualification_date": date(2026, 11, 7),
        "source_commit": "a" * 40,
        "support_status": "supported",
        "request_acceptance": "accepted",
        "assessment_behavior": "verified",
        "native_schema_behavior": "verified",
        "supported_features": (
            "rubric_scores",
            "rubric_grouped",
            "assessment_presentation_v2",
        ),
        "documentation_sha256": "a" * 64,
        "offline_fixture_sha256": "b" * 64,
        "independent_live_sha256": "c" * 64,
        "provenance": tuple(
            EvidenceProvenance(
                kind=cast(Any, kind),
                source_sha256=digest * 64,
                recorded_on=date(2026, 11, 7),
            )
            for kind, digest in (
                ("documentation", "a"),
                ("offline_fixture", "b"),
                ("independent_live", "c"),
            )
        ),
    }
    return _QualifiedProfile.model_validate(fields | changes)


@pytest.mark.parametrize(
    "change",
    [
        {"translator_version": True},
        {"source_commit": "private-config-canary"},
        {"request_acceptance": "unknown"},
        {"assessment_behavior": "unknown"},
        {"native_schema_behavior": "unknown"},
        {"support_status": "unknown"},
        {"provenance": ()},
        {"unsupported_features": ("rubric_scores",)},
        {"supported_features": ("rubric_scores", "rubric_scores")},
        {"api_scope": "private-api-scope-canary"},
    ],
)
def test_reviewed_profiles_reject_incomplete_or_conflicting_qualification(
    tmp_path: Path, change: dict[str, Any]
) -> None:
    context = production_context(case(tmp_path)[0])
    with pytest.raises(ValueError) as exc:
        _reviewed_profile(context, **change)
    assert "private-" not in str(exc.value)


def test_known_unsupported_auto_and_changed_registry_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from judgmentllm import production_registry

    spec, dep, _ = case(tmp_path, policy="auto")
    spec.strict_capabilities = True
    context = production_context(spec)
    unsupported = _reviewed_profile(
        context,
        support_status="unsupported",
        supported_features=(),
        unsupported_features=("rubric_scores",),
        native_schema_behavior="unsupported",
    )
    monkeypatch.setattr(production_registry, "_QUALIFIED_PROFILES", (unsupported,))
    output = build_output_commitment_v4(spec, dep.request, context)
    assert output.selection.mechanism == "text"
    assert output.selection.native_support == "unsupported"
    supported = _reviewed_profile(context)
    monkeypatch.setattr(production_registry, "_QUALIFIED_PROFILES", (supported,))
    output = build_output_commitment_v4(spec, dep.request, context)
    assert output.selection.mechanism != "text"
    wire = prepare_production_request(
        spec,
        dep,
        physical_identity("run-001", dep.request.request_uid, "attempt-001"),
        context,
    )
    monkeypatch.setattr(production_registry, "_QUALIFIED_PROFILES", ())
    with pytest.raises(TransportIntegrityError):
        prepare_production_request(
            spec,
            dep,
            wire.transport_plan.identity,
            context,
            frozen=wire.transport_commitment,
        )
    monkeypatch.setattr(
        production_registry, "_QUALIFIED_PROFILES", (supported, supported)
    )
    with pytest.raises(TransportIntegrityError):
        production_context_digest(context)


def test_generation_uses_only_reviewed_exact_scope_rules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from judgmentllm import production_registry
    from judgmentllm.generation import (
        GenerationCapabilities,
        GenerationParameterCapability,
    )

    spec, dep, _ = case(
        tmp_path,
        generation=GenerationSpec(temperature=0, max_output_tokens=32),
    )
    spec.strict_capabilities = True
    context = production_context(spec)
    with pytest.raises(TransportIntegrityError):
        build_output_commitment_v4(spec, dep.request, context)
    facts = GenerationCapabilities(
        provider="openai",
        endpoint="responses",
        model=spec.model,
        temperature=GenerationParameterCapability(supported=True, minimum=0, maximum=2),
        max_output_tokens=GenerationParameterCapability(supported=True, maximum=64),
        fixture_tested=True,
        live_qualified=True,
    )
    profile = _reviewed_profile(context, generation=facts)
    monkeypatch.setattr(production_registry, "_QUALIFIED_PROFILES", (profile,))
    wire = prepare_production_request(
        spec,
        dep,
        physical_identity("run-001", dep.request.request_uid, "attempt-001"),
        context,
    )
    assert json.loads(wire.payload)["temperature"] == 0
    unsupported = facts.model_copy(
        update={"temperature": GenerationParameterCapability(supported=False)}
    )
    monkeypatch.setattr(
        production_registry,
        "_QUALIFIED_PROFILES",
        (_reviewed_profile(context, generation=unsupported),),
    )
    with pytest.raises(TransportIntegrityError):
        prepare_production_request(spec, dep, wire.transport_plan.identity, context)
    with pytest.raises(ValueError):
        _reviewed_profile(
            context, generation=facts.model_copy(update={"model": "unrelated-model"})
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"qualification_plan_sha256": "a" * 64},
        {"source_commit": "a" * 40},
        {"kind": "openai-qualification-candidate-v1"},
        {"kind": "openai-qualification-candidate-v1", "source_commit": "bad"},
        {
            "kind": "openai-qualification-v1",
            "source_commit": "a" * 40,
            "qualification_plan_sha256": "bad",
        },
    ],
)
def test_qualification_context_metadata_cannot_masquerade_as_production(
    tmp_path: Path, changes: dict[str, Any]
) -> None:
    context = production_context(case(tmp_path)[0])
    with pytest.raises(TransportIntegrityError):
        production_context_digest(replace(context, **changes))


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_text", "private-schema-canary"),
        ("schema_sha256", "a" * 64),
        ("projection", None),
        ("sdk_config_fragment_text", "private-config-canary"),
        ("repetition", "canonical_neutral"),
    ],
)
def test_production_output_current_values_and_duplicate_keys_are_strict(
    tmp_path: Path, field: str, value: Any
) -> None:
    wire, _, _, _ = wire_for(tmp_path, policy="native", probe=True)
    output = wire.output_commitment
    with pytest.raises(ValueError) as exc:
        OutputCommitmentV4.model_validate({**dict(output), field: value})
    assert "private-" not in str(exc.value)
    with pytest.raises(ValueError):
        OutputCommitmentV4.model_validate_json(
            b'{"commitment_version":4,' + canonical_bytes(output)[1:]
        )


def test_probe_selection_does_not_claim_serving_model_support(tmp_path: Path) -> None:
    wire, spec, dep, context = wire_for(tmp_path, policy="native", probe=True)
    selection = wire.output_commitment.selection
    assert selection.kind == "qualification_probe"
    assert selection.authority_kind == "qualification_probe"
    assert selection.native_support == "unknown"
    assert wire.transport_commitment.selection.native_support == "unknown"
    subset = subset_dependency(dep, wire.task_ids[:1])
    retry = prepare_production_request(
        spec,
        subset,
        physical_identity("run-001", dep.request.request_uid, "attempt-002"),
        context,
        parent=wire.transport_commitment,
    )
    assert retry.output_commitment.selection.kind == "inherited"
    assert retry.output_commitment.selection.authority_kind == "qualification_probe"
    assert retry.output_commitment.selection.native_support == "unknown"
    from pydantic import BaseModel

    forged = BaseModel.model_copy(selection, update={"authority_kind": "reviewed"})
    with pytest.raises(ValueError):
        type(selection).model_validate(dict(forged))
