"""Strict response completion, assessment, usage and privacy interpretation."""

from __future__ import annotations

# pyright: reportPrivateUsage=false, reportUnknownArgumentType=false
import json
from pathlib import Path
from typing import Any

import pytest
from gemini_fixture import wire_for
from gemini_offline import envelope

from judgmentllm.assessment_diagnostics import diagnose_assessment_output
from judgmentllm.evidence._base import EvidenceIdentity
from judgmentllm.evidence.usage import (
    ProviderUsage,
    UsageContext,
    extract_provider_usage,
)
from judgmentllm.gemini_assessment import GeminiAssessmentComponentV3
from judgmentllm.gemini_usage import GeminiUsageInput, interpret_gemini_usage
from judgmentllm.providers.gemini import GeminiAdapter
from judgmentllm.providers.gemini_response import (
    _extract_assessment_component,
    _generate_content_output,
    _safe_original_envelope,
    _usage,
)


def test_nonblocking_feedback_and_absent_usage_are_safe_but_not_invented() -> None:
    body = envelope("{}")
    body.pop("usageMetadata")
    assert _safe_original_envelope(body, "models.generate_content")
    body["promptFeedback"] = {"blockReason": "BLOCKED_REASON_UNSPECIFIED"}
    assert _generate_content_output(body) == ("response", "{}", "completed")
    body["promptFeedback"] = {"safetyRatings": "INVALID"}
    with pytest.raises(ValueError):
        _generate_content_output(body)
    with pytest.raises(ValueError):
        _extract_assessment_component(body, "vertex")


@pytest.mark.parametrize(
    "reason,kind,outcome",
    [
        ("STOP", "response", "completed"),
        ("MAX_TOKENS", "provider_error", "output_limit"),
        (None, "provider_error", "incomplete"),
        ("FINISH_REASON_UNSPECIFIED", "provider_error", "incomplete"),
        ("CONTINUATION", "provider_error", "incomplete"),
        ("OTHER", "provider_error", "unsupported_output"),
        ("LANGUAGE", "provider_error", "unsupported_output"),
        ("UNKNOWN_PRIVATE_DETAIL", "provider_error", "unsupported_output"),
        ("SAFETY", "refusal", "content_filter"),
        ("RECITATION", "refusal", "content_filter"),
        ("BLOCKLIST", "refusal", "content_filter"),
        ("PROHIBITED_CONTENT", "refusal", "content_filter"),
        ("SPII", "refusal", "content_filter"),
        ("IMAGE_SAFETY", "refusal", "content_filter"),
        ("IMAGE_PROHIBITED_CONTENT", "refusal", "content_filter"),
        ("IMAGE_RECITATION", "refusal", "content_filter"),
        ("MALFORMED_FUNCTION_CALL", "provider_error", "tool_call"),
        ("UNEXPECTED_TOOL_CALL", "provider_error", "tool_call"),
        ("TOO_MANY_TOOL_CALLS", "provider_error", "tool_call"),
        ("NO_IMAGE", "provider_error", "unsupported_output"),
        ("IMAGE_OTHER", "provider_error", "unsupported_output"),
    ],
)
def test_finish_reason_never_overridden_by_valid_json(
    reason: str | None, kind: str, outcome: str
) -> None:
    body = envelope('{"winner_id":"001"}')
    body["candidates"][0]["finishReason"] = reason
    selected, text, status = _generate_content_output(body)
    assert (selected, status) == (kind, outcome)
    assert (text is not None) is (reason == "STOP")
    component = _extract_assessment_component(body, "models.generate_content")
    assert component.finish_reason == (
        "unknown" if reason == "UNKNOWN_PRIVATE_DETAIL" else reason
    )
    assert "UNKNOWN_PRIVATE_DETAIL" not in component.model_dump_json()


@pytest.mark.parametrize(
    "shape,expected",
    [
        (None, "missing_output"),
        ([], "missing_output"),
        ([{}, {}], "multiple_outputs"),
        ([{}], "incomplete"),
        ([{"index": 1, "finishReason": "STOP"}], "unsupported_output"),
        ([{"index": True, "finishReason": "STOP"}], "unsupported_output"),
        ([None], "unsupported_output"),
    ],
)
def test_missing_multiple_and_wrong_candidate_identity(
    shape: Any, expected: str
) -> None:
    assert _generate_content_output(envelope("{}", candidates=shape))[2] == expected
    component = _extract_assessment_component(
        envelope("{}", candidates=shape), "models.generate_content"
    )
    assert component.output_text is None


@pytest.mark.parametrize(
    "parts,expected",
    [
        ([], "missing_output"),
        ([{"text": ""}], "missing_output"),
        ([{"text": " \n"}], "missing_output"),
        ([{"text": "{"}, {"text": "}"}], "multiple_outputs"),
        ([{"text": "{}", "thought": True}], "missing_output"),
        ([{"text": "PRIVATE_THOUGHT", "thought": True}, {"text": "{}"}], "completed"),
        ([{"text": "{}", "thoughtSignature": "PRIVATE_SIGNATURE"}], "completed"),
        ([{"text": "{}", "thought": False}], "completed"),
        ([{"text": "{}"}, {"functionCall": {"name": "tool"}}], "tool_call"),
        ([{"text": "{}"}, {"functionResponse": {}}], "tool_call"),
        ([{"executableCode": {}}], "tool_call"),
        ([{"codeExecutionResult": {}}], "tool_call"),
        ([{"inlineData": {}}], "unsupported_output"),
        ([{"fileData": {}}], "unsupported_output"),
        ([{"text": "{}", "unknown": "PRIVATE"}], "unsupported_output"),
        ([{"text": 1}], "unsupported_output"),
        ([{"text": "{}", "thought": "false"}], "unsupported_output"),
        ([{"text": "{}", "thoughtSignature": 1}], "unsupported_output"),
    ],
)
def test_parts_are_never_joined_or_reasoning_reinterpreted(
    parts: list[Any], expected: str
) -> None:
    body = envelope("{}")
    body["candidates"][0]["content"]["parts"] = parts
    kind, text, outcome = _generate_content_output(body)
    if any(set(part) != {"text"} for part in parts):
        assert not _safe_original_envelope(body, "models.generate_content")
    assert outcome == expected
    if outcome == "completed":
        assert kind == "response" and text == "{}"
    else:
        assert text is None


@pytest.mark.parametrize("where", ["prompt", "output", "rating", "prompt_rating"])
def test_safety_blocking_and_refusal_details_are_not_assessment(where: str) -> None:
    body = envelope('{"winner_id":"001"}')
    if where == "prompt":
        body["promptFeedback"] = {
            "blockReason": "SAFETY",
            "blockReasonMessage": "PRIVATE_REFUSAL",
        }
    elif where == "output":
        body["candidates"][0]["finishReason"] = "PROHIBITED_CONTENT"
    elif where == "rating":
        body["candidates"][0]["safetyRatings"] = [{"blocked": True}]
    else:
        body["promptFeedback"] = {"safetyRatings": [{"blocked": True}]}
    assert _generate_content_output(body) == ("refusal", None, "content_filter")
    component = _extract_assessment_component(body, "models.generate_content")
    assert component.refusal_present and component.output_text is None
    assert "PRIVATE_REFUSAL" not in component.model_dump_json()


@pytest.mark.parametrize(
    "text,code",
    [
        (
            '{"scores":[{"trait_id":"01","category_label":"low 雪"},{"trait_id":"02","category_label":"limited"}]}',
            "valid",
        ),
        ('{"scores":[],"scores":[]}', "duplicate_keys"),
        ("```json\n{}\n```", "json_syntax"),
        ("{bad", "json_syntax"),
        ("[]", "root_type"),
        (
            '{"scores":[{"trait_id":"01","category_label":"BAD"},{"trait_id":"02","category_label":"limited"}]}',
            "category_error",
        ),
        ('{"scores":[{"trait_id":"03","category_label":"low 雪"}]}', "trait_error"),
        (
            '{"scores":[{"trait_id":"01","category_label":"low 雪","explanation":"forbidden"},{"trait_id":"02","category_label":"limited"}]}',
            "extra_fields",
        ),
        ('{"scores":[],"private":"value"}', "extra_fields"),
        ('{"scores":[]}', "trait_error"),
    ],
)
def test_canonical_diagnostics_preserve_invalid_original_component(
    tmp_path: Path, text: str, code: str
) -> None:
    wire, _, _, _ = wire_for(tmp_path)
    body = envelope(text)
    component = _extract_assessment_component(body, "models.generate_content")
    diagnostic = diagnose_assessment_output(component, wire.output_commitment.contract)
    assert diagnostic.code == code
    assert diagnostic.canonical_valid is (code == "valid")
    assert component.output_text == text.encode()
    assert "output_text" not in component.model_dump()


def test_usage_preserves_missing_and_separate_thinking_counts() -> None:
    usage = _usage(
        "models.generate_content",
        {
            "promptTokenCount": 20,
            "candidatesTokenCount": 3,
            "thoughtsTokenCount": 40,
            "cachedContentTokenCount": 5,
            "totalTokenCount": 63,
            "unknownCounter": 9,
        },
    )
    assert usage is not None
    assert (
        usage.input_tokens,
        usage.output_tokens,
        usage.reasoning_tokens,
        usage.cache_read_tokens,
        usage.total_tokens,
    ) == (20, 3, 40, 5, 63)
    assert usage.cache_write_tokens is None and usage.reported_cost is None
    thought = next(row for row in usage.breakdown if row.field == "thoughtsTokenCount")
    assert (thought.relationship, thought.related_counter) == (
        "excluded_from",
        "output_tokens",
    )
    missing = interpret_gemini_usage(GeminiUsageInput(candidatesTokenCount=0))
    assert (
        missing.output_tokens == 0
        and missing.input_tokens is None
        and missing.total_tokens is None
    )
    assert _usage("models.generate_content", None) is None
    assert _usage("models.generate_content", {}) is not None
    with pytest.raises(ValueError):
        _usage("vertex", {})
    for counters in (
        {"promptTokenCount": True},
        {"candidatesTokenCount": -1},
        {"thoughtsTokenCount": "3"},
        {"cachedContentTokenCount": 21, "promptTokenCount": 20},
    ):
        with pytest.raises(ValueError):
            _usage("models.generate_content", counters)
    identity = EvidenceIdentity(
        run_id="001",
        request_uid="002",
        attempt_id="003",
        submission_id="004",
        observation_id="005",
    )
    record = extract_provider_usage(
        identity,
        UsageContext(
            provider="gemini",
            endpoint="models.generate_content",
            requested_model="unknown",
            observation_kind="response",
        ),
        usage,
    )
    assert ProviderUsage.model_validate_json(record.model_dump_json()) == record
    with pytest.raises(ValueError):
        ProviderUsage.model_validate(dict(record) | {"provider": "vertex"})


def test_adapter_drops_unsafe_envelope_and_thoughts_without_dropping_assessment(
    tmp_path: Path,
) -> None:
    wire, _, _, _ = wire_for(tmp_path)
    text = '{"scores":[]}'
    body = envelope(text)
    body["privateMetadata"] = "PRIVATE_METADATA"
    body["candidates"][0]["content"]["parts"].insert(
        0,
        {
            "text": "PRIVATE_THOUGHT",
            "thought": True,
            "thoughtSignature": "PRIVATE_SIGNATURE",
        },
    )
    adapter = GeminiAdapter(wire.endpoint, wire.model)
    response = adapter._response(
        json.dumps(body).encode(), body, body["responseId"], prepared=wire
    )
    evidence = response.evidence
    assert evidence is not None and evidence.safe_response is not None
    assert all(
        private not in evidence.safe_response
        for private in (b"PRIVATE_METADATA", b"PRIVATE_THOUGHT", b"PRIVATE_SIGNATURE")
    )
    assert (
        evidence.reasoning[0].metadata.provider_field
        == "gemini.generate_content.thought"
    )
    assert evidence.reasoning[0].text is None
    assert adapter.parse(response).output == {"scores": []}
    assert not evidence.original_body_safe
    assert _safe_original_envelope(envelope(text), wire.endpoint)


@pytest.mark.parametrize(
    "text,status", [("x" * 1_048_577, "oversized"), ("\ud800", "invalid_utf8")]
)
def test_component_boundaries(tmp_path: Path, text: str, status: str) -> None:
    wire, _, _, _ = wire_for(tmp_path)
    body = envelope(text)
    component = _extract_assessment_component(body, wire.endpoint)
    assert component.extraction_status == status and component.output_text is None
    adapter = GeminiAdapter(wire.endpoint, wire.model)
    response = adapter._response(
        json.dumps(body).encode(), body, body["responseId"], prepared=wire
    )
    assert response.evidence is not None and response.evidence.output_text is None
    assert not response.evidence.original_body_safe
    with pytest.raises(ValueError):
        GeminiAssessmentComponentV3.model_validate(
            dict(component) | {"output_text": b"forbidden"}
        )
