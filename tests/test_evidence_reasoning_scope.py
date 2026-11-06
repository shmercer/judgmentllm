"""Detached reasoning/scope contracts never accept provider text or infer access."""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import pytest
from pydantic import ValidationError
from test_auth_cli import private_failure

from judgmentllm.evidence._base import EvidenceIdentity, EvidenceModel
from judgmentllm.evidence.reasoning import (
    ReasoningArtifactReference,
    ReasoningAuthorization,
    ReasoningBlockMetadata,
    ReasoningEvidence,
    ReasoningRetentionDecision,
    authorize_reasoning,
    extract_reasoning_metadata,
)
from judgmentllm.evidence.scope import EvidenceProvenance, ScopedProviderEvidence
from judgmentllm.generation import GenerationCapabilities


def identity() -> EvidenceIdentity:
    return EvidenceIdentity(
        run_id="001",
        request_uid="002",
        attempt_id="003",
        submission_id="004",
        observation_id="005",
    )


def provenance(kind: str = "documentation") -> EvidenceProvenance:
    return EvidenceProvenance.model_validate(
        dict(kind=kind, source_sha256="a" * 64, recorded_on=date(2026, 11, 6))
    )


def scope(**updates: Any) -> ScopedProviderEvidence:
    data: dict[str, Any] = dict(
        provider="openai",
        endpoint="responses",
        feature="reasoning",
        provenance=(provenance(),),
    )
    data.update(updates)
    return ScopedProviderEvidence.model_validate(data)


@pytest.mark.parametrize("version", [True, False, 1.0, 2, "1", None])
def test_strict_version(version: object) -> None:
    with pytest.raises(ValidationError):
        EvidenceIdentity.model_validate(
            {**identity().model_dump(), "schema_version": version}
        )


@pytest.mark.parametrize(
    "field", ["run_id", "request_uid", "attempt_id", "submission_id"]
)
@pytest.mark.parametrize("value", ["", " leading", "tail ", "bad\n", 1, True])
def test_identity_constraints(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        identity().model_copy(update={field: value})


@private_failure
def test_safe_all_entrypoints(capsys: pytest.CaptureFixture[str]) -> None:
    secret = "SYNTHETIC-PRIVATE-INPUT"
    operations = [
        lambda: EvidenceIdentity.model_validate({secret: secret}),
        lambda: EvidenceIdentity.model_validate_json('{"' + secret),
        lambda: EvidenceIdentity.model_validate_json(json.dumps({secret: secret})),
        lambda: EvidenceIdentity.model_validate_strings({secret: secret}),
        lambda: EvidenceIdentity.model_construct(**{secret: secret}),  # type: ignore[arg-type]
        lambda: identity().model_copy(update={secret: secret}),
        lambda: setattr(identity(), secret, secret),
    ]
    for operation in operations:
        with pytest.raises(ValidationError) as caught:
            operation()
        error = caught.value
        assert (
            secret not in str(error) + repr(error) + repr(error.errors()) + error.json()
        )
        assert error.errors()[0]["input"] is None
    for method in (
        EvidenceIdentity.parse_raw,
        EvidenceIdentity.parse_file,
        identity().copy,
    ):
        with pytest.raises(ValueError) as caught:
            method(secret)
        assert secret not in str(caught.value)
    assert capsys.readouterr() == ("", "")
    assert identity().model_copy(deep=True) == identity()
    assert (
        EvidenceIdentity.model_validate_json(identity().model_dump_json()) == identity()
    )


@pytest.mark.parametrize("capture", ["minimal", "full"])
@pytest.mark.parametrize("effort", [False, True])
@pytest.mark.parametrize("summary", [False, True])
def test_exposure_effort_never_authorize(
    capture: str, effort: bool, summary: bool
) -> None:
    policy = ReasoningAuthorization.model_validate(
        dict(capture=capture, effort_requested=effort, summary_requested=summary)
    )
    for kind in ("summary", "thinking_block", "other_documented"):
        decision = authorize_reasoning(policy, kind)  # type: ignore[arg-type]
        assert not decision.retain_text
        assert decision.request_summary is summary


@pytest.mark.parametrize(
    "retention,kind,expected",
    [
        ("summary", "summary", True),
        ("summary", "thinking_block", False),
        ("provider_text", "summary", False),
        ("provider_text", "thinking_block", True),
        ("provider_text", "other_documented", False),
    ],
)
def test_explicit_type_authorization(retention: str, kind: str, expected: bool) -> None:
    policy = ReasoningAuthorization.model_validate(
        dict(capture="full", retention=retention)
    )
    assert authorize_reasoning(policy, kind).retain_text is expected  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        policy.model_copy(update={"capture": "minimal"})


@private_failure
def test_ordered_blocks_and_no_retention() -> None:
    policy = ReasoningAuthorization(capture="full", retention="provider_text")
    blocks = tuple(
        ReasoningBlockMetadata(
            kind="thinking_block",
            provider_field="anthropic.messages.thinking",
            block_ordinal=i,
            redacted=i == 1,
        )
        for i in range(3)
    )
    records = list(
        extract_reasoning_metadata(identity(), iter(blocks), authorization=policy)
    )
    assert [r.block_ordinal for r in records] == [0, 1, 2]
    assert [r.availability for r in records] == [
        "not_retained",
        "redacted",
        "not_retained",
    ]
    assert all(r.artifact is None for r in records)
    assert list(extract_reasoning_metadata(identity(), (), authorization=policy)) == []
    with pytest.raises(ValueError, match="ordinals must increase"):
        list(
            extract_reasoning_metadata(
                identity(), (blocks[1], blocks[0]), authorization=policy
            )
        )
    with pytest.raises(ValueError, match="requires an observation"):
        list(
            extract_reasoning_metadata(
                identity().model_copy(update={"observation_id": None}),
                (),
                authorization=policy,
            )
        )
    with pytest.raises(ValueError, match="unsupported reasoning kind"):
        authorize_reasoning(policy, "SYNTHETIC-PRIVATE-INPUT")  # type: ignore[arg-type]


@private_failure
def test_availability_artifacts_and_safe_fields() -> None:
    data = dict(
        **identity().model_dump(),
        kind="summary",
        provider_field="openai.responses.reasoning.summary",
        block_ordinal=0,
    )
    for status in ("not_exposed", "not_returned", "not_retained", "redacted"):
        record = ReasoningEvidence.model_validate({**data, "availability": status})
        assert record.artifact is None
        with pytest.raises(ValidationError):
            record.model_copy(
                update={
                    "artifact": ReasoningArtifactReference(
                        sha256="b" * 64, byte_length=1
                    )
                }
            )
    record = ReasoningEvidence.model_validate(
        {
            **data,
            "availability": "retained",
            "artifact": dict(sha256="b" * 64, byte_length=10),
            "source_bytes_sha256": "c" * 64,
        }
    )
    assert record.artifact is not None
    assert record.source_bytes_sha256 == "c" * 64
    for changes in (
        {"availability": "retained", "artifact": None},
        {"provider_field": "SYNTHETIC-PRIVATE-INPUT"},
        {"kind": "thinking_block"},
        {"observation_id": None},
        {"block_ordinal": True},
        {"block_ordinal": -1},
        {"text": "SYNTHETIC-PRIVATE-INPUT"},
        {"trait_id": "SYNTHETIC-PRIVATE-INPUT"},
    ):
        with pytest.raises(ValidationError) as caught:
            record.model_copy(update=changes)
        assert "SYNTHETIC-PRIVATE-INPUT" not in caught.value.json()
    with pytest.raises(ValidationError):
        ReasoningBlockMetadata(
            kind="summary",
            provider_field="anthropic.messages.thinking",
            block_ordinal=0,
        )


def test_unknown_scope_doc_and_unsupported() -> None:
    record = scope()
    assert record.model is None and record.model_scope == "unresolved"
    assert (
        record.supported
        is record.fixture_tested
        is record.live_qualified
        is record.implemented
        is None
    )
    assert scope(supported=False).supported is False
    assert (
        ScopedProviderEvidence.model_validate_json(record.model_dump_json()) == record
    )
    with pytest.raises(ValidationError):
        scope(live_qualified=True, stage="live_qualified")


def test_fixture_does_not_imply_live() -> None:
    record = scope(
        model="synthetic-model",
        model_scope="exact",
        sdk_package="openai",
        sdk_version="3.42.1",
        implemented=True,
        fixture_tested=True,
        stage="fixture_tested",
        provenance=(provenance("offline_fixture"),),
    )
    assert record.live_qualified is None
    for changes in (
        {"live_qualified": True},
        {"stage": "live_qualified"},
        {"provenance": (provenance(),)},
        {"implemented": False},
        {"sdk_version": None},
        {"model_scope": "unresolved"},
    ):
        with pytest.raises(ValidationError):
            record.model_copy(update=changes)


def test_explicit_live_requires_independent_provenance() -> None:
    data: dict[str, Any] = dict(
        model="synthetic-model",
        model_scope="exact",
        sdk_package="openai",
        sdk_version="3.42.1",
        implemented=True,
        fixture_tested=True,
        live_qualified=True,
        supported=True,
        stage="live_qualified",
        provenance=(provenance("offline_fixture"), provenance("independent_live")),
    )
    assert scope(**data).live_qualified is True
    for changes in (
        {"provenance": (provenance("offline_fixture"),)},
        {"model": None, "model_scope": "unresolved"},
        {"sdk_package": None, "sdk_version": None},
        {"supported": None},
    ):
        with pytest.raises(ValidationError):
            scope(**{**data, **changes})


def test_compose_existing_generation_facts() -> None:
    generation = GenerationCapabilities(
        provider="openai", endpoint="responses", model="synthetic-model"
    )
    record = scope(
        feature="generation",
        model="synthetic-model",
        model_scope="exact",
        generation=generation,
    )
    assert record.generation == generation
    for changes in (
        {"model": "other"},
        {"feature": "usage"},
        {
            "fixture_tested": True,
            "implemented": True,
            "stage": "fixture_tested",
            "provenance": (provenance("offline_fixture"),),
        },
    ):
        with pytest.raises(ValidationError):
            record.model_copy(update=changes)


@pytest.mark.parametrize(
    "model",
    [
        ReasoningAuthorization,
        ReasoningBlockMetadata,
        ReasoningEvidence,
        ReasoningRetentionDecision,
        ScopedProviderEvidence,
        EvidenceProvenance,
        ReasoningArtifactReference,
    ],
)
def test_models_have_no_text_or_metadata_field(model: type[EvidenceModel]) -> None:
    assert not (
        {"text", "metadata", "headers", "api_key", "account", "path"}
        & model.model_fields.keys()
    )


@pytest.mark.parametrize("version", [True, False, 1.0, 2, "1", None])
def test_strict_json_version(version: object) -> None:
    with pytest.raises(ValidationError):
        EvidenceIdentity.model_validate_json(
            json.dumps({**identity().model_dump(), "schema_version": version})
        )


def test_retention_decision_consistency() -> None:
    with pytest.raises(ValidationError):
        ReasoningRetentionDecision(
            retain_text=True, request_summary=False, diagnostic="not_authorized"
        )


def test_generation_composition_revalidates_existing_models() -> None:
    generation = GenerationCapabilities(
        provider="openai", endpoint="responses", model="synthetic"
    )
    invalid = generation.model_copy(
        update={"incompatible_combinations": (("temperature", "temperature"),)}
    )
    with pytest.raises(ValidationError):
        scope(
            feature="generation",
            model="synthetic",
            model_scope="exact",
            generation=invalid,
        )
