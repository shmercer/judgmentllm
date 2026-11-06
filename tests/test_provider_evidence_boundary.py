"""Offline tests for safe evidence preparation before artifact publication."""

from __future__ import annotations

import json
from typing import Literal
from uuid import uuid4

import pytest
from pydantic import ValidationError
from test_evidence_order import commitment, identity, payload

from judgmentllm.evidence import ReasoningAuthorization, ReasoningBlockMetadata
from judgmentllm.evidence.usage import UsageContext
from judgmentllm.providers.evidence import (
    ProviderEvidence,
    ProviderReasoningBlock,
    exact_output_substring,
    prepare_evidence,
)
from judgmentllm.providers.protocol import ProviderResponse, TransportNotSubmitted


def context() -> UsageContext:
    return UsageContext(
        provider="mock",
        endpoint="synthetic",
        requested_model="synthetic-model",
        observation_kind="response",
    )


def block(text: bytes) -> ProviderReasoningBlock:
    return ProviderReasoningBlock(
        metadata=ReasoningBlockMetadata(
            kind="summary",
            provider_field="openai.responses.reasoning.summary",
            block_ordinal=0,
        ),
        text=text,
    )


@pytest.mark.parametrize("capture", ["full", "minimal"])
def test_no_evidence_cannot_authorize_body_capture(
    capture: Literal["full", "minimal"],
) -> None:
    response = ProviderResponse(
        observation_key="observation-001", kind="response", raw=b"untrusted"
    )
    prepared = prepare_evidence(
        identity=identity(),
        context=context(),
        commitment=commitment(),
        response=response,
        capture=capture,
    )
    assert prepared.retained_response is None
    assert prepared.original_sha256 is None
    assert prepared.disposition == "withheld"
    assert prepared.usage.input_tokens is None
    assert prepared.order is not None and prepared.order.order_adherence == "unknown"


@pytest.mark.parametrize("capture", ["full", "minimal"])
def test_filter_unauthorized_reasoning_before_capture(
    capture: Literal["full", "minimal"],
) -> None:
    text = f"synthetic-private-{uuid4().hex}".encode()
    output = json.dumps(payload()).encode()
    evidence = ProviderEvidence(
        provenance="synthetic_fixture",
        output_text=output,
        output_source="raw_output_text",
        reasoning=(block(text),),
        safe_response=output,
    )
    response = ProviderResponse(
        observation_key="observation-001",
        kind="response",
        raw=text + output,
        evidence=evidence,
    )
    prepared = prepare_evidence(
        identity=identity(),
        context=context(),
        commitment=commitment(),
        response=response,
        capture=capture,
    )
    assert prepared.retained_response == output
    assert prepared.disposition == "redacted"
    assert prepared.original_sha256 is None
    assert prepared.reasoning_artifacts == ()
    assert prepared.reasoning[0].artifact is None
    assert prepared.reasoning[0].source_bytes_sha256 is None
    assert text.decode() not in evidence.model_dump_json()
    assert text.decode() not in repr(evidence)
    assert text.decode() not in repr(prepared)
    assert prepared.order is not None and prepared.order.order_adherence == "adherent"


def test_explicit_synthetic_summary_authorization_creates_reference() -> None:
    output = json.dumps(payload()).encode()
    response = ProviderResponse(
        observation_key="observation-001",
        kind="response",
        raw=b"synthetic-envelope",
        evidence=ProviderEvidence(
            provenance="synthetic_fixture",
            reasoning=(block(b"synthetic summary"),),
            safe_response=output,
        ),
    )
    authorization = ReasoningAuthorization(capture="full", retention="summary")
    prepared = prepare_evidence(
        identity=identity(),
        context=context(),
        commitment=None,
        response=response,
        capture="full",
        authorization=authorization,
    )
    assert len(prepared.reasoning_artifacts) == 1
    assert prepared.reasoning[0].availability == "retained"
    assert prepared.reasoning[0].artifact == prepared.reasoning_artifacts[0][0]
    assert (
        prepared.order is not None and prepared.order.unknown_reason == "not_applicable"
    )
    assert prepared.authorization == authorization


def test_raw_reasoning_never_retained_as_unfiltered_envelope() -> None:
    raw = b"synthetic reasoning"
    response = ProviderResponse(
        observation_key="observation-001",
        kind="response",
        raw=raw,
        evidence=ProviderEvidence(
            provenance="synthetic_fixture", safe_response=raw, reasoning=(block(raw),)
        ),
    )
    prepared = prepare_evidence(
        identity=identity(),
        context=context(),
        commitment=None,
        response=response,
        capture="full",
    )
    assert prepared.retained_response is None
    assert prepared.disposition == "withheld"


def test_capture_and_provenance_conflicts_are_safe() -> None:
    response = ProviderResponse(
        observation_key="observation-001",
        kind="response",
        raw=b"fixture",
        evidence=ProviderEvidence(provenance="synthetic_fixture"),
    )
    with pytest.raises(ValueError, match="capture authorization conflicts"):
        prepare_evidence(
            identity=identity(),
            context=context(),
            commitment=None,
            response=response,
            capture="full",
            authorization=ReasoningAuthorization(capture="minimal"),
        )
    with pytest.raises(ValueError, match="synthetic evidence requires"):
        prepare_evidence(
            identity=identity(),
            context=context().model_copy(update={"provider": "openai"}),
            commitment=None,
            response=response,
            capture="full",
        )
    live = response.model_copy(
        update={"evidence": ProviderEvidence(provenance="provider_documented")}
    )
    with pytest.raises(ValueError, match="authorization requires"):
        prepare_evidence(
            identity=identity(),
            context=context(),
            commitment=None,
            response=live,
            capture="full",
            authorization=ReasoningAuthorization(capture="full", retention="summary"),
        )


@pytest.mark.parametrize(
    "raw,expected",
    [
        (b'{"output":{"b":1,"a":2},"x":3}', b'{"b":1,"a":2}'),
        (b' { "output" : { "b":1, "a":2 } } ', b'{ "b":1, "a":2 }'),
        (b'{"output":null}', None),
        (b'{"output":{},"output":{}}', None),
        (b'{"x":1,"x":2,"output":{}}', None),
        (b'{"output":{}} trailing', None),
        (b'{"output":', None),
        (b"[]", None),
        (b"", None),
        (b"\xff", None),
    ],
)
def test_exact_substring_preserves_original_bytes(
    raw: bytes, expected: bytes | None
) -> None:
    assert exact_output_substring(raw) == expected


def test_boundary_errors_never_echo_transient_bytes() -> None:
    secret = f"synthetic-private-{uuid4().hex}"
    try:
        ProviderReasoningBlock.model_validate(
            {"metadata": {secret: secret}, "text": secret.encode()}
        )
    except ValidationError as error:
        assert secret not in str(error)
        assert secret not in str(error.errors())
        assert secret not in error.json()
    else:
        pytest.fail("invalid block accepted")
    assert TransportNotSubmitted().usage is None
    assert (
        json.loads(
            ProviderEvidence(
                provenance="synthetic_fixture", safe_response=secret.encode()
            ).model_dump_json()
        ).get("safe_response")
        is None
    )


@pytest.mark.parametrize("case", ["key", "trait", "malformed"])
def test_untrusted_output_cannot_enter_body_artifacts(case: str) -> None:
    secret = f"synthetic-private-{uuid4().hex}"
    output = payload()
    if case == "key":
        output[secret] = secret
    elif case == "trait":
        output["scores"][0]["trait_id"] = secret
    raw = json.dumps(output).encode() if case != "malformed" else secret.encode()
    response = ProviderResponse(
        observation_key="observation-001",
        kind="response",
        raw=raw,
        evidence=ProviderEvidence(
            provenance="synthetic_fixture",
            output_text=raw,
            output_source="raw_output_text",
            safe_response=raw,
            original_body_safe=True,
        ),
    )
    prepared = prepare_evidence(
        identity=identity(),
        context=context(),
        commitment=commitment(),
        response=response,
        capture="full",
    )
    assert prepared.retained_response is None
    assert prepared.original_sha256 is None
    assert secret not in prepared.order.model_dump_json()
    assert secret not in response.model_dump_json()
    assert secret not in repr(response)


def test_response_validation_errors_and_copies_protect_transient_bytes() -> None:
    secret = f"synthetic-private-{uuid4().hex}"
    with pytest.raises(ValidationError) as failure:
        ProviderResponse.model_validate(
            {"observation_key": "safe", "kind": "response", "raw": {secret: secret}}
        )
    assert secret not in str(failure.value)
    assert secret not in str(failure.value.errors())
    assert secret not in failure.value.json()
    response = ProviderResponse(
        observation_key="safe", kind="response", raw=secret.encode()
    )
    assert response.model_copy().raw == secret.encode()
    assert response.model_copy(update={"kind": "refusal"}).raw == secret.encode()


@pytest.mark.parametrize(
    "original_safe,expected", [(False, "redacted"), (True, "exact")]
)
def test_exact_disposition_requires_authorized_original_commitment(
    original_safe: bool, expected: str
) -> None:
    raw = json.dumps(payload()).encode()
    response = ProviderResponse(
        observation_key="observation-001",
        kind="response",
        raw=raw,
        evidence=ProviderEvidence(
            provenance="synthetic_fixture",
            output_text=raw,
            output_source="raw_output_text",
            safe_response=raw,
            original_body_safe=original_safe,
        ),
    )
    prepared = prepare_evidence(
        identity=identity(),
        context=context(),
        commitment=commitment(),
        response=response,
        capture="full",
    )
    assert prepared.retained_response == raw
    assert prepared.disposition == expected
    assert (prepared.original_sha256 is not None) == original_safe
