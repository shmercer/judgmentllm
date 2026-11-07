"""Physical-call reuse preserves exact bytes and rejects mutable aliases."""

# pyright: reportPrivateUsage=false
from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from native_transport_fixture import MECHANISMS, context_for
from native_v2_cases import cases
from test_native_transport_integration import offline as offline
from test_transport_rendering import fixture_request

from judgmentllm import native_prompts, transport
from judgmentllm.config import RunSpecV3
from judgmentllm.preparation.session import RequestDependency
from judgmentllm.transport import (
    TransportIntegrityError,
    physical_identity,
    prepare_transport_request,
)


def preparation(
    mechanism: str = MECHANISMS[0], policy: str = "native", case: str = "grouped_scores"
) -> tuple[Any, ...]:
    spec, original, responses, template = fixture_request(mechanism, cases()[case])
    spec = spec.model_copy(
        update={"prompt": spec.prompt.model_copy(update={"structured_output": policy})}
    )
    dependency = RequestDependency(request=original, dependency_sha256="a" * 64)
    identity = physical_identity("001", original.request_uid, "first")
    return spec, dependency, identity, context_for(mechanism), responses, template


def build(values: tuple[Any, ...]) -> Any:
    spec, dependency, identity, context, responses, template = values
    return prepare_transport_request(
        spec,
        dependency,
        identity,
        context,
        template=template,
        lookup=lambda kind, identifier: (
            dependency.request.pair if kind == "pair" else responses[identifier]
        ),
    )


@pytest.mark.parametrize("mechanism", MECHANISMS)
def test_transient_live_authority_change_cannot_affect_owned_selection(
    mechanism: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    values = preparation(mechanism, "auto")
    expected = build(values)
    context = values[3]
    resolver_name = (
        "_resolve_native_transport"
        if mechanism.startswith("openai.")
        else "_resolve_native_transport_v2"
    )
    original_resolver = getattr(transport, resolver_name)

    def mutate_and_restore(*args: Any, **kwargs: Any) -> Any:
        # The private decision must use detached evidence, not this temporarily
        # changed caller-owned record whose value is restored before exit.
        assert context.evidence is not None
        previous = context.evidence.supported
        context.evidence.__dict__["supported"] = False
        try:
            return original_resolver(*args, **kwargs)
        finally:
            context.evidence.__dict__["supported"] = previous

    monkeypatch.setattr(transport, resolver_name, mutate_and_restore)
    actual = build(values)
    assert actual.payload == expected.payload
    assert actual.transport_commitment == expected.transport_commitment
    assert actual.transport_commitment.mechanism == mechanism
    assert actual.transport_plan.output_commitment_sha256 == (
        actual.output_commitment.commitment_sha256
    )


@pytest.mark.parametrize(
    "change",
    [
        "neutral_schema",
        "native_schema",
        "sdk",
        "format",
        "categories",
        "trait_order",
        "selection",
        "scope",
        "version",
        "unknown",
        "provenance",
        "spec",
        "dependency",
        "identity",
        "evidence",
        "profile",
        "translator",
        "translator_module",
        "translator_qualname",
    ],
)
def test_reuse_rejects_mutations_before_returning_payload(
    change: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    values = preparation()
    spec, dependency, identity, context, _, _ = values
    render = native_prompts._render_text_v3

    def mutate(*args: Any, **kwargs: Any) -> Any:
        result = render(*args, **kwargs)
        output = args[-1]
        projection = output.projection
        assert projection is not None
        if change == "neutral_schema":
            output.__dict__["schema_text"] += " "
        elif change == "native_schema":
            projection.__dict__["native_schema_text"] += " "
        elif change == "sdk":
            output.__dict__["sdk_config_fragment_text"] += " "
        elif change == "format":
            projection.__dict__["format_fragment_text"] += " "
        elif change == "categories":
            output.contract.traits[0].levels[0].__dict__["label"] = "private-canary"
        elif change == "trait_order":
            output.contract.__dict__["traits"] = tuple(reversed(output.contract.traits))
        elif change == "selection":
            output.selection.__dict__["policy"] = "auto"
        elif change == "scope":
            output.scope.__dict__["model"] = "private-canary"
        elif change == "version":
            output.__dict__["commitment_version"] = 3.0
        elif change == "unknown":
            output.__dict__["private-canary"] = "private-canary"
        elif change == "provenance":
            projection.provenance[0].__dict__["recorded_on"] = date(2020, 1, 1)
        elif change == "spec":
            spec.__dict__["model"] = "private-canary"
        elif change == "dependency":
            dependency.request.__dict__["tasks"] = tuple(
                reversed(dependency.request.tasks)
            )
        elif change == "identity":
            identity.__dict__["attempt_id"] = "private-canary"
        elif change == "evidence":
            context.evidence.__dict__["supported"] = False
        elif change == "profile":
            context.openai_catalog.profiles[0].__dict__["fixture_sha256"] = "b" * 64
        elif change == "translator":
            original = transport.project_openai_schema

            def replacement(*args: Any, **kwargs: Any) -> Any:
                return original(*args, **kwargs)

            replacement.__module__ = original.__module__
            replacement.__qualname__ = original.__qualname__
            monkeypatch.setattr(transport, "project_openai_schema", replacement)
        elif change == "translator_module":
            monkeypatch.setattr(
                transport.project_openai_schema, "__module__", "private_canary"
            )
        else:
            monkeypatch.setattr(
                transport.project_openai_schema, "__qualname__", "private_canary"
            )
        return result

    monkeypatch.setattr(native_prompts, "_render_text_v3", mutate)
    with pytest.raises(TransportIntegrityError) as error:
        build(values)
    assert "private-canary" not in str(error.value)


def test_public_output_hash_remains_fully_validating_after_preparation() -> None:
    wire = build(preparation())
    output = wire.output_commitment
    original = output.commitment_sha256
    output.projection.__dict__["native_schema_text"] += " "
    with pytest.raises(ValueError):
        _ = output.commitment_sha256
    assert wire.transport_plan.output_commitment_sha256 == original


def test_transient_token_preserves_dates_types_order_and_unknown_fields() -> None:
    token = transport._preparation_token
    assert token(date(2026, 11, 7)) != token("2026-11-07")
    assert token(True) != token(1) != token(1.0)
    assert token({"a": 1, "b": 2}) != token({"b": 2, "a": 1})
    assert token(object()) is None
    output = build(preparation()).output_commitment
    before = token(output)
    output.__dict__["unknown"] = "private-canary"
    assert token(output) != before


@pytest.mark.parametrize(
    "field",
    [
        "provider",
        "endpoint",
        "model",
        "model_scope",
        "sdk_package",
        "sdk_version",
        "api_version",
        "supported",
        "implemented",
        "fixture_tested",
        "live_qualified",
        "stage",
        "evidence_provenance",
        "provenance_date",
        "profile_features",
        "profile_documentation",
        "profile_fixture",
        "implementation_version",
        "default_catalog",
    ],
)
def test_owned_context_guard_covers_all_relevant_authority_metadata(
    field: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    values = preparation(MECHANISMS[2])
    context = values[3]
    render = native_prompts._render_text_v3

    def mutate(*args: Any, **kwargs: Any) -> Any:
        result = render(*args, **kwargs)
        if field in {
            "provider",
            "endpoint",
            "model",
            "model_scope",
            "sdk_package",
            "sdk_version",
            "api_version",
        }:
            context.scope.__dict__[field] = "private-canary"
        elif field in {"supported", "implemented", "fixture_tested", "live_qualified"}:
            context.evidence.__dict__[field] = not getattr(context.evidence, field)
        elif field == "stage":
            context.evidence.__dict__[field] = "discovered"
        elif field == "evidence_provenance":
            context.evidence.provenance[0].__dict__["source_sha256"] = "b" * 64
        elif field == "provenance_date":
            context.evidence.provenance[0].__dict__["recorded_on"] = date(2020, 1, 1)
        elif field.startswith("profile_"):
            name = {
                "profile_features": "features",
                "profile_documentation": "documentation_sha256",
                "profile_fixture": "fixture_sha256",
            }[field]
            context.provider_catalog.profiles[0].__dict__[name] = (
                () if name == "features" else "b" * 64
            )
        elif field == "implementation_version":
            catalog = context.provider_catalog
            implementations = (
                replace(catalog.implementations[0], implementation_version=2),
                *catalog.implementations[1:],
            )
            object.__setattr__(
                context,
                "provider_catalog",
                replace(catalog, implementations=implementations),
            )
        else:
            # The absent first catalog still contributes default implementation
            # metadata to the committed digest and must be owned and guarded.
            catalog = transport._DEFAULT_CATALOG
            monkeypatch.setattr(
                transport,
                "_DEFAULT_CATALOG",
                replace(
                    catalog, implementations=tuple(reversed(catalog.implementations))
                ),
            )
        return result

    monkeypatch.setattr(native_prompts, "_render_text_v3", mutate)
    with pytest.raises(TransportIntegrityError):
        build(values)


def test_opaque_callback_keeps_the_full_public_validation_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import replace

    values = list(preparation())
    context = values[3]
    catalog = context.openai_catalog
    original = catalog.implementations[0].translate

    def opaque(*args: Any, **kwargs: Any) -> Any:
        return original(*args, **kwargs)

    values[3] = replace(
        context,
        openai_catalog=replace(
            catalog,
            implementations=(
                replace(catalog.implementations[0], translate=opaque),
                *catalog.implementations[1:],
            ),
        ),
    )
    assert transport._preparation_token(values[3]) is None
    renderer = native_prompts.render_request_v3
    calls: list[None] = []

    def full_renderer(*args: Any, **kwargs: Any) -> Any:
        calls.append(None)
        return renderer(*args, **kwargs)

    monkeypatch.setattr(native_prompts, "render_request_v3", full_renderer)
    wire = build(tuple(values))
    assert calls == [None]
    assert (
        wire.transport_plan.output_commitment_sha256
        == wire.output_commitment.commitment_sha256
    )


@pytest.mark.parametrize("mechanism", MECHANISMS)
def test_fully_rehashed_valid_output_cannot_replace_the_local_snapshot(
    mechanism: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    values = preparation(mechanism)
    render = native_prompts._render_text_v3

    def replace_output(*args: Any, **kwargs: Any) -> Any:
        result = render(*args, **kwargs)
        output = args[-1]
        fields = args[0].model_dump(mode="python")
        fields["rubric"]["traits"][0]["levels"][0].update(
            label="private-canary", source_label="private-canary"
        )
        changed_spec = RunSpecV3.model_validate(fields)
        replacement = transport.build_output_commitment_v3(
            changed_spec, args[2], values[3]
        )
        # Both the original and replacement are valid standalone records. The
        # independently bound original plan/payload still forbids substitution.
        transport.OutputCommitmentV3.model_validate_json(replacement.model_dump_json())
        assert replacement.commitment_sha256 != output.commitment_sha256
        output.__dict__.clear()
        output.__dict__.update(replacement.__dict__)
        return result

    monkeypatch.setattr(native_prompts, "_render_text_v3", replace_output)
    with pytest.raises(TransportIntegrityError):
        build(values)


def test_pair_metadata_reordering_cannot_replace_exact_payload_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = preparation(case="pairwise_ties_False_explanations_False")
    pair = values[1].request.pair
    pair.metadata.clear()
    pair.metadata.update({"first": 1, "second": 2})
    render = native_prompts._render_text_v3

    def reorder(*args: Any, **kwargs: Any) -> Any:
        result = render(*args, **kwargs)
        metadata = args[-1].contract.pair.metadata
        reordered = dict(reversed(tuple(metadata.items())))
        assert metadata == reordered
        metadata.clear()
        metadata.update(reordered)
        return result

    monkeypatch.setattr(native_prompts, "_render_text_v3", reorder)
    with pytest.raises(TransportIntegrityError):
        build(values)


@pytest.mark.parametrize("mechanism", MECHANISMS)
def test_caller_committed_output_is_detached_during_replay(
    mechanism: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    values = preparation(mechanism)
    original = build(values)
    committed = original.output_commitment
    render = native_prompts._render_text_v3

    def mutate_and_restore(*args: Any, **kwargs: Any) -> Any:
        owned = args[-1]
        assert owned.contract is not committed.contract
        assert owned.contract.traits[0] is not committed.contract.traits[0]
        assert owned.projection is not committed.projection
        level = committed.contract.traits[0].levels[0]
        previous = level.label, level.source_label
        level.__dict__.update(label="private-canary", source_label="private-canary")
        try:
            return render(*args, **kwargs)
        finally:
            level.__dict__.update(label=previous[0], source_label=previous[1])

    monkeypatch.setattr(native_prompts, "_render_text_v3", mutate_and_restore)
    spec, dependency, identity, context, responses, template = values
    recovered = prepare_transport_request(
        spec,
        dependency,
        identity,
        context,
        template=template,
        lookup=lambda kind, identifier: responses[identifier],
        frozen=original.transport_commitment,
        committed_output=committed,
    )
    assert recovered.payload == original.payload
    assert recovered.transport_commitment == original.transport_commitment
