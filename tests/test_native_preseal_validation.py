"""Native preparation snapshots retain independent nested mutation validation."""

# Private hooks are intentionally exercised by these adversarial tests.
# pyright: reportPrivateUsage=false
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from enum import StrEnum
from pathlib import Path
from types import FunctionType
from typing import Any, cast

import pytest
from native_transport_fixture import MECHANISMS, context_for
from pydantic import BaseModel
from test_native_transport_integration import offline as offline
from test_native_transport_rendering import rendered_case

from judgmentllm import transport
from judgmentllm.config import RunSpecV3, load_run_spec
from judgmentllm.models import Level, Trait
from judgmentllm.native_schema import (
    NativeProjectionScopeV1,
    NativeSchemaProjectionV1,
)
from judgmentllm.native_selection import NativeSelectionError, TransportDecisionV1
from judgmentllm.output_contracts import AssessmentOutputContract, RubricOutputContract
from judgmentllm.persistence.hashing import (
    bytes_digest,
    canonical_bytes,
    semantic_digest,
)
from judgmentllm.preparation.session import RequestDependency
from judgmentllm.production_transport import TransportCommitmentV2
from judgmentllm.providers import openai_schema
from judgmentllm.transport import (
    OutputCommitmentV3,
    PreparedRequestV3,
    TransportCommitmentV1,
)

_TRANSLATOR_CALLS: list[str] = []
_TRANSLATOR_THROW_ON: int | None = None
_TRANSLATOR_DELEGATE: Callable[..., NativeSchemaProjectionV1] = (
    openai_schema.project_openai_schema
)


def _module_global_projector(
    contract: AssessmentOutputContract, *, scope: NativeProjectionScopeV1
) -> NativeSchemaProjectionV1:
    # Deliberately closureless: exact metadata tokens cannot prove this callable
    # has stock behavior because its observable state lives in module globals.
    _TRANSLATOR_CALLS.append("project")
    if len(_TRANSLATOR_CALLS) == _TRANSLATOR_THROW_ON:
        raise TypeError("private-translator-error")
    return _TRANSLATOR_DELEGATE(contract, scope=scope)


def corrupt(model: BaseModel, **values: object) -> BaseModel:
    # Deliberately bypass EvidenceModel's validating model_copy. The receiving
    # constructor must independently reject even this manufactured corruption.
    return BaseModel.model_copy(model, update=values)


@pytest.mark.parametrize(
    "level",
    ["output", "selection", "projection", "scope", "contract", "trait", "category"],
)
def test_primitive_snapshot_hash_never_bypasses_nested_validation(
    tmp_path: Path, level: str
) -> None:
    wire = rendered_case(tmp_path, "openai.responses.text.format", "together")
    output = wire.output_commitment
    contract = output.contract
    assert isinstance(contract, RubricOutputContract)
    marker = {"private-injected-key": "private-injected-value"}
    if level == "output":
        changed = corrupt(output, **marker)
    elif level == "selection":
        changed = corrupt(output, selection=corrupt(output.selection, **marker))
    elif level == "projection":
        assert output.projection is not None
        changed = corrupt(output, projection=corrupt(output.projection, **marker))
    elif level == "scope":
        changed = corrupt(output, scope=corrupt(output.scope, **marker))
    elif level == "contract":
        changed = corrupt(output, contract=corrupt(contract, **marker))
    else:
        traits = list(contract.traits)
        trait = traits[0]
        if level == "trait":
            traits[0] = cast(Trait, corrupt(trait, **marker))
        else:
            levels = list(trait.levels)
            levels[0] = cast(Level, corrupt(levels[0], **marker))
            traits[0] = cast(Trait, corrupt(trait, levels=levels))
        changed = corrupt(output, contract=corrupt(contract, traits=tuple(traits)))
    with pytest.raises(ValueError) as error:
        PreparedRequestV3.model_validate({**dict(wire), "output_commitment": changed})
    assert "private-injected" not in str(error.value)
    # The separate public output boundary remains independently strict too.
    with pytest.raises(ValueError):
        OutputCommitmentV3.model_validate(dict(changed))


def test_primitive_snapshot_hash_still_binds_current_category_bytes(
    tmp_path: Path,
) -> None:
    wire = rendered_case(tmp_path, "openai.responses.text.format", "together")
    output = wire.output_commitment
    assert isinstance(output.contract, RubricOutputContract)
    traits = list(output.contract.traits)
    levels = list(traits[0].levels)
    levels[0] = levels[0].model_copy(update={"description": "changed harmless prose"})
    traits[0] = traits[0].model_copy(update={"levels": levels})
    changed = corrupt(output, contract=corrupt(output.contract, traits=tuple(traits)))
    # Even a category-description change that leaves the generated schema valid
    # changes the committed canonical assessment source and cannot reuse a seal.
    with pytest.raises(ValueError):
        PreparedRequestV3.model_validate({**dict(wire), "output_commitment": changed})


@pytest.mark.parametrize("construction", ["copy", "construct", "subclass"])
@pytest.mark.parametrize("rehash", [False, True])
def test_projection_field_independently_rebuilds_untrusted_model_instances(
    tmp_path: Path, construction: str, rehash: bool
) -> None:
    wire = rendered_case(tmp_path, "openai.responses.text.format", "together")
    output = wire.output_commitment
    projection = output.projection
    assert projection is not None
    values = dict(projection)
    changed_text = projection.native_schema_text + " "
    values.update(
        native_schema_text=changed_text,
        native_schema_sha256=bytes_digest(changed_text.encode("utf-8")),
    )
    if construction == "copy":
        changed = corrupt(projection, **values)
    else:
        model_type = (
            type(projection)
            if construction == "construct"
            else type("UntrustedProjection", (type(projection),), {})
        )
        # Bypass the project's validating model_construct override deliberately.
        changed = BaseModel.model_construct.__func__(model_type, **values)
    selection = output.selection
    if rehash:
        digest = bytes_digest(
            canonical_bytes(
                changed.model_dump(mode="json", exclude={"projection_sha256"})
            )
        )
        changed = corrupt(changed, projection_sha256=digest)
        decision = selection.openai_decision
        assert decision is not None
        decision_values = decision.model_dump(mode="json", exclude={"decision_sha256"})
        decision_values["projection_sha256"] = digest
        decision_values["decision_sha256"] = semantic_digest(
            "native-transport-decision-v1", decision_values
        )
        changed_decision = type(decision).model_validate_json(
            json.dumps(decision_values)
        )
        selection = selection.model_copy(update={"openai_decision": changed_decision})
    corrupted = corrupt(output, projection=changed, selection=selection)
    # The output after-validator does not reproduce native schema text itself:
    # its always-revalidated projection field must reject this even when every
    # byte digest and frozen decision reference is internally rehashed.
    with pytest.raises(ValueError):
        OutputCommitmentV3.model_validate(dict(corrupted))
    with pytest.raises(ValueError):
        PreparedRequestV3.model_validate({**dict(wire), "output_commitment": corrupted})


@pytest.mark.parametrize("mechanism", [MECHANISMS[0], MECHANISMS[2]])
@pytest.mark.parametrize(
    "field",
    ["scope", "source", "provenance", "native_constraints", "root_properties"],
)
def test_projection_field_rejects_nested_unknown_fields_and_container_coercion(
    tmp_path: Path, field: str, mechanism: str
) -> None:
    wire = rendered_case(tmp_path, mechanism, "together")
    output = wire.output_commitment
    projection = output.projection
    assert projection is not None
    marker = {"private-injected-key": "private-injected-value"}
    if field in {"scope", "source"}:
        value = corrupt(getattr(projection, field), **marker)
    elif field == "root_properties":
        value = list(projection.root_properties)
    else:
        records = list(getattr(projection, field))
        records[0] = corrupt(records[0], **marker)
        value = tuple(records)
    changed = corrupt(projection, **{field: value})
    with pytest.raises(ValueError) as error:
        OutputCommitmentV3.model_validate({**dict(output), "projection": changed})
    assert "private-injected" not in str(error.value)


@pytest.mark.parametrize(
    "field",
    [
        "prompt_bytes",
        "payload_bytes",
        "native_schema_bytes",
        "sdk_config_bytes",
        "format_bytes",
    ],
)
def test_prepared_receipt_binding_rejects_all_rehashed_length_changes(
    tmp_path: Path, field: str
) -> None:
    wire = rendered_case(tmp_path, "openai.responses.text.format", "together")
    values = wire.transport_commitment.model_dump(
        mode="json", exclude={"commitment_sha256"}
    )
    assert type(values[field]) is int
    values[field] += 1
    values["commitment_sha256"] = semantic_digest("transport-commitment-v1", values)
    # The receipt is independently valid as metadata. Physical request admission
    # must still compare every current field with the actual committed bytes.
    receipt = TransportCommitmentV1.model_validate_json(json.dumps(values))
    with pytest.raises(ValueError):
        PreparedRequestV3.model_validate(
            {**dict(wire), "transport_commitment": receipt}
        )


@pytest.mark.parametrize("behavior", ["decline", "throw", "stateful"])
def test_substituted_resolver_keeps_owed_call_and_legacy_signature(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, behavior: str
) -> None:
    wire = rendered_case(tmp_path, MECHANISMS[0], "single_scores")
    spec = load_run_spec(tmp_path / "inputs" / "run.json")
    assert isinstance(spec, RunSpecV3)
    dependency = RequestDependency(
        request=wire.logical_request, dependency_sha256=wire.dependency_sha256
    )
    context = context_for(MECHANISMS[0])
    original = transport._resolve_native_transport
    calls: list[str] = []

    def substituted(*args: Any, **kwargs: Any) -> TransportDecisionV1:
        calls.append(behavior)
        assert "_projection_sink" not in kwargs
        if behavior == "decline":
            raise NativeSelectionError("native_support_unknown", "unknown")
        if behavior == "throw":
            raise TypeError("private-resolver-error")
        return original(*args, **kwargs)

    monkeypatch.setattr(transport, "_resolve_native_transport", substituted)
    if behavior == "stateful":
        current = transport.prepare_transport_request(
            spec, dependency, wire.transport_plan.identity, context
        )
        assert current.output_commitment.selection.mechanism == MECHANISMS[0]
    else:
        with pytest.raises(transport.TransportIntegrityError) as error:
            transport.prepare_transport_request(
                spec, dependency, wire.transport_plan.identity, context
            )
        assert "private-resolver-error" not in str(error.value)
    assert calls == [behavior]


def test_private_projection_sink_rejects_caller_prepopulated_candidates(
    tmp_path: Path,
) -> None:
    from judgmentllm.native_selection import _resolve_native_transport

    wire = rendered_case(tmp_path, MECHANISMS[0], "single_scores")
    context = context_for(MECHANISMS[0])
    projection = wire.output_commitment.projection
    assert isinstance(projection, NativeSchemaProjectionV1)
    assert context.openai_catalog is not None
    with pytest.raises(NativeSelectionError):
        _resolve_native_transport(
            wire.output_commitment.contract,
            scope=context.scope,
            policy="native",
            evidence=context.evidence,
            catalog=context.openai_catalog,
            _projection_sink=[projection],
        )


@pytest.mark.parametrize("family", ["synthetic-v1", "production-v2"])
@pytest.mark.parametrize("endpoint", ["responses", "chat.completions"])
@pytest.mark.parametrize("policy", ["text", "native"])
def test_receipt_json_snapshot_preserves_exact_publication_and_recovery(
    tmp_path: Path, family: str, endpoint: str, policy: str
) -> None:
    from test_production_transport import wire_for

    from judgmentllm.persistence.artifacts import ArtifactStore
    from judgmentllm.persistence.models import ArtifactIntegrityError
    from judgmentllm.persistence.transport_receipts import (
        _parse_transport_commitment,
        publish_transport_commitment,
        read_transport_commitment,
        transport_reference,
    )

    if family == "synthetic-v1":
        mechanism = (
            "openai.responses.text.format"
            if endpoint == "responses"
            else "openai.chat.response_format"
        )
        wire = rendered_case(tmp_path, mechanism, "single_scores", policy=policy)
    else:
        wire, _, _, _ = wire_for(
            tmp_path, endpoint=endpoint, policy=policy, probe=policy == "native"
        )
    receipt = wire.transport_commitment
    expected = canonical_bytes(receipt)
    assert canonical_bytes(receipt.model_dump(mode="json")) == expected
    store = ArtifactStore(tmp_path / "receipt-copy")
    identity = publish_transport_commitment(store, receipt)
    assert store.read(identity) == expected
    recovered = read_transport_commitment(store, receipt.identity.attempt_id)
    assert recovered is not None
    assert canonical_bytes(recovered) == expected
    assert canonical_bytes(recovered.model_dump(mode="json")) == expected

    # The first public model boundary remains strict for both record versions,
    # including preconstructed literals and nested extras serializers can omit.
    for bad in (
        corrupt(receipt, **{"private-injected-key": "private-injected-value"}),
        BaseModel.model_construct.__func__(
            type(receipt), **{**dict(receipt), "local_validation_required": False}
        ),
        corrupt(
            receipt,
            selection=corrupt(
                receipt.selection, **{"private-injected-key": "private-injected-value"}
            ),
        ),
    ):
        with pytest.raises(ArtifactIntegrityError):
            publish_transport_commitment(
                store, cast(TransportCommitmentV1 | TransportCommitmentV2, bad)
            )

    # A fully parseable, self-consistent receipt with matching content-addressed
    # bytes still fails if the named reference is not the exact canonical form.
    noncanonical = json.dumps(
        json.loads(expected), ensure_ascii=False, indent=2
    ).encode()
    assert (
        _parse_transport_commitment(noncanonical).commitment_sha256
        == receipt.commitment_sha256
    )
    store.put(noncanonical, "application/json")
    reference = store.reference_path(transport_reference(receipt.identity.attempt_id))
    reference.write_bytes(noncanonical)
    with pytest.raises(ArtifactIntegrityError):
        read_transport_commitment(store, receipt.identity.attempt_id)
    assert reference.read_bytes() == noncanonical


def test_changed_stock_resolver_code_keeps_owed_legacy_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wire = rendered_case(tmp_path, MECHANISMS[0], "single_scores")
    spec = load_run_spec(tmp_path / "inputs" / "run.json")
    assert isinstance(spec, RunSpecV3)
    dependency = RequestDependency(
        request=wire.logical_request, dependency_sha256=wire.dependency_sha256
    )

    def changed_code(*args: object, **kwargs: object) -> None:
        if "_projection_sink" in kwargs:
            raise AssertionError("a changed resolver received compiler handoff")
        raise TypeError("private-resolver-error")

    original = transport._resolve_native_transport
    monkeypatch.setattr(original, "__code__", changed_code.__code__)
    with pytest.raises(transport.TransportIntegrityError) as error:
        transport.prepare_transport_request(
            spec, dependency, wire.transport_plan.identity, context_for(MECHANISMS[0])
        )
    assert "private-resolver-error" not in str(error.value)


@pytest.mark.parametrize("custom", [False, True])
@pytest.mark.parametrize("with_evidence", [False, True])
def test_owned_context_snapshots_keep_scope_enum_date_and_null_bytes(
    custom: bool, with_evidence: bool
) -> None:
    class ProviderName(StrEnum):
        OPENAI = "openai"

    class SDKVersion(StrEnum):
        PINNED = "3.42.1"

    context = context_for(MECHANISMS[0])
    scope = context.scope.model_copy(
        update={
            "provider": ProviderName.OPENAI,
            "model": "custom-model" if custom else context.scope.model,
            "sdk_package": None if custom else "openai",
            "sdk_version": None if custom else SDKVersion.PINNED,
            "api_version": "v1" if custom else None,
        }
    )
    current = context.evidence
    assert current is not None
    evidence = (
        current.model_copy(
            update={
                "provider": scope.provider,
                "model": scope.model,
                "sdk_package": scope.sdk_package,
                "sdk_version": scope.sdk_version,
                "api_version": scope.api_version,
            }
        )
        if with_evidence
        else None
    )
    if evidence is not None:
        assert evidence.provenance
        assert evidence.provenance[0].recorded_on is not None
    # The old public canonical boundary and the newly admitted owned JSON view
    # encode exactly the same bytes, including enum normalization, dates/None.
    for record in (scope, evidence):
        if record is not None:
            admitted = type(record).model_validate(dict(record))
            assert canonical_bytes(admitted) == canonical_bytes(
                admitted.model_dump(mode="json")
            )
    current_context = replace(context, scope=scope, evidence=evidence)
    digest = transport.transport_context_digest(current_context)
    assert len(digest) == 64
    assert digest == transport.transport_context_digest(
        replace(
            current_context,
            scope=scope.model_copy(),
            evidence=evidence.model_copy() if evidence else None,
        )
    )


@pytest.mark.parametrize("stage", ["fresh", "frozen"])
@pytest.mark.parametrize("mutation", ["replacement", "code"])
@pytest.mark.parametrize("behavior", ["stateful", "throw"])
def test_closureless_and_changed_code_projectors_keep_owed_legacy_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    mutation: str,
    behavior: str,
) -> None:
    wire = rendered_case(tmp_path, MECHANISMS[0], "single_scores")
    spec = load_run_spec(tmp_path / "inputs" / "run.json")
    assert isinstance(spec, RunSpecV3)
    dependency = RequestDependency(
        request=wire.logical_request, dependency_sha256=wire.dependency_sha256
    )
    payload = json.loads(wire.payload)
    context = replace(
        context_for(MECHANISMS[0]),
        service_identity=payload["service_identity"],
        fixture_sha256=payload["fixture_sha256"],
    )
    expected_calls = 2 if stage == "frozen" or mutation == "code" else 1
    original = transport.project_openai_schema
    _TRANSLATOR_CALLS.clear()
    throw_on = expected_calls if behavior == "throw" else None
    monkeypatch.setitem(globals(), "_TRANSLATOR_THROW_ON", throw_on)
    assert _module_global_projector.__closure__ is None
    if mutation == "replacement":
        monkeypatch.setitem(globals(), "_TRANSLATOR_DELEGATE", original)
        monkeypatch.setattr(
            transport, "project_openai_schema", _module_global_projector
        )
    else:
        # Keep the translator's identity and keyword/default metadata unchanged,
        # but replace its code with a closureless module-global-state wrapper.
        # The old code remains available only as the wrapper's pure delegate.
        delegate = FunctionType(
            original.__code__,
            original.__globals__,
            original.__name__,
            original.__defaults__,
            original.__closure__,
        )
        delegate.__kwdefaults__ = original.__kwdefaults__
        monkeypatch.setattr(
            openai_schema, "_TRANSLATOR_CALLS", _TRANSLATOR_CALLS, raising=False
        )
        monkeypatch.setattr(
            openai_schema, "_TRANSLATOR_THROW_ON", throw_on, raising=False
        )
        monkeypatch.setattr(
            openai_schema, "_TRANSLATOR_DELEGATE", delegate, raising=False
        )
        monkeypatch.setattr(original, "__code__", _module_global_projector.__code__)
    assert (
        transport._preparation_token(
            (spec, dependency, wire.transport_plan.identity, context)
        )
        is not None
    )
    frozen = wire.transport_commitment if stage == "frozen" else None
    if behavior == "throw":
        with pytest.raises(transport.TransportIntegrityError) as error:
            transport.prepare_transport_request(
                spec, dependency, wire.transport_plan.identity, context, frozen=frozen
            )
        assert "private-translator-error" not in str(error.value)
    else:
        current = transport.prepare_transport_request(
            spec, dependency, wire.transport_plan.identity, context, frozen=frozen
        )
        assert current.payload == wire.payload
    assert _TRANSLATOR_CALLS == ["project"] * expected_calls


def test_private_candidate_sink_does_not_capture_changed_translator_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from judgmentllm.native_selection import _resolve_native_transport

    wire = rendered_case(tmp_path, MECHANISMS[0], "single_scores")
    context = context_for(MECHANISMS[0])
    assert context.openai_catalog is not None
    original = transport.project_openai_schema
    delegate = FunctionType(
        original.__code__,
        original.__globals__,
        original.__name__,
        original.__defaults__,
        original.__closure__,
    )
    delegate.__kwdefaults__ = original.__kwdefaults__
    _TRANSLATOR_CALLS.clear()
    monkeypatch.setattr(
        openai_schema, "_TRANSLATOR_CALLS", _TRANSLATOR_CALLS, raising=False
    )
    monkeypatch.setattr(openai_schema, "_TRANSLATOR_THROW_ON", None, raising=False)
    monkeypatch.setattr(openai_schema, "_TRANSLATOR_DELEGATE", delegate, raising=False)
    monkeypatch.setattr(original, "__code__", _module_global_projector.__code__)
    candidates: list[NativeSchemaProjectionV1] = []
    decision = _resolve_native_transport(
        wire.output_commitment.contract,
        scope=context.scope,
        policy="native",
        evidence=context.evidence,
        catalog=context.openai_catalog,
        _projection_sink=candidates,
    )
    assert decision.mechanism == MECHANISMS[0]
    assert _TRANSLATOR_CALLS == ["project"]
    assert candidates == []
