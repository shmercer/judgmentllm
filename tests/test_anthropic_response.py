"""Messages envelope failures, local assessment validity, and cache policies."""

# pyright: reportPrivateUsage=false
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal, cast

import httpx2
import pytest
from anthropic_fixture import case, wire_for
from test_anthropic_lifecycle import envelope

from judgmentllm.anthropic_transport import (
    prepare_production_request,
    production_context,
)
from judgmentllm.assessment_diagnostics import diagnose_assessment_output
from judgmentllm.providers.anthropic import AnthropicAdapter
from judgmentllm.providers.anthropic_response import (
    _extract_assessment_component,
    _messages_output,
    _safe_original_envelope,
    _usage,
)
from judgmentllm.providers.protocol import SubmissionUncertain
from judgmentllm.transport import physical_identity


@pytest.mark.parametrize(
    "reason,outcome",
    [
        ("end_turn", "completed"),
        ("max_tokens", "output_limit"),
        ("pause_turn", "incomplete"),
        ("model_context_window_exceeded", "incomplete"),
        ("tool_use", "tool_call"),
        ("refusal", "refusal"),
        ("stop_sequence", "unsupported_output"),
        ("future", "unsupported_output"),
        (None, "incomplete"),
        ({}, "unsupported_output"),
    ],
)
def test_stop_reasons_never_turn_partial_work_into_success(
    reason: Any, outcome: str
) -> None:
    body = envelope("messages", '{"winner_id":"001"}')
    body["stop_reason"] = reason
    kind, _, observed = _messages_output(body)
    assert observed == outcome
    assert (kind == "response") == (reason == "end_turn")
    component = _extract_assessment_component(body, "messages")
    assert component.stop_reason == (
        reason
        if isinstance(reason, str) and reason != "future"
        else None
        if reason is None
        else "unknown"
    )
    if reason == "refusal":
        assert component.output_text is None


@pytest.mark.parametrize(
    "content,status",
    [
        ([], "missing_output"),
        ([{"type": "text", "text": ""}], "missing_output"),
        (
            [{"type": "text", "text": "{}"}, {"type": "text", "text": "{}"}],
            "multiple_outputs",
        ),
        ([{"type": "tool_use", "input": {"secret": "PRIVATE_TOOL"}}], "tool_call"),
        ([{"type": "server_tool_use"}], "tool_call"),
        ([{"type": "future", "secret": "PRIVATE_EXTENSION"}], "unsupported_output"),
        ([{"type": cast(list[str], [])}], "unsupported_output"),
        (None, "missing_output"),
    ],
)
def test_components_are_not_concatenated_or_repaired(content: Any, status: str) -> None:
    body = envelope("messages", "{}")
    body["content"] = content
    assert _messages_output(body)[2] == status
    component = _extract_assessment_component(body, "messages")
    if status == "multiple_outputs":
        assert component.output_text is None
    assert "PRIVATE" not in component.model_dump_json()


@pytest.mark.parametrize("thinking", ["thinking", "redacted_thinking"])
def test_thinking_is_separate_and_never_retained_as_explanation(thinking: str) -> None:
    body = envelope("messages", '{"winner_id":"001"}')
    body["content"].insert(
        0,
        {
            "type": thinking,
            "thinking": "PRIVATE_REASONING",
            "signature": "PRIVATE_SIGNATURE",
            "data": "PRIVATE_REDACTED",
        },
    )
    adapter = AnthropicAdapter("messages", "offline-model")
    response = adapter._response(json.dumps(body).encode(), body, "msg-offline")
    assert response.kind == "response"
    evidence = response.evidence
    assert evidence is not None and evidence.assessment_component is not None
    assert evidence.assessment_component.reasoning_present
    assert not evidence.original_body_safe
    assert (
        evidence.safe_response is not None and b"PRIVATE" not in evidence.safe_response
    )
    assert len(evidence.reasoning) == 1 and evidence.reasoning[0].text is None
    assert adapter.parse(response).output == {"winner_id": "001"}


@pytest.mark.parametrize(
    "usage",
    [
        None,
        {},
        {"input_tokens": 0},
        {"output_tokens": 4},
        {"cache_read_input_tokens": 9},
        {
            "cache_creation_input_tokens": 5,
            "cache_creation": {"ephemeral_5m_input_tokens": 3},
        },
        {
            "input_tokens": 2,
            "output_tokens": 3,
            "cache_creation_input_tokens": 4,
            "cache_read_input_tokens": 8,
            "cache_creation": {
                "ephemeral_5m_input_tokens": 3,
                "ephemeral_1h_input_tokens": 1,
            },
        },
    ],
)
def test_partial_usage_does_not_fabricate_totals(usage: Any) -> None:
    reported = _usage("messages", usage)
    if usage is None:
        assert reported is None
    else:
        assert (
            reported is not None
            and reported.total_tokens is None
            and reported.reasoning_tokens is None
        )
        assert reported.input_tokens == usage.get("input_tokens")
        assert reported.output_tokens == usage.get("output_tokens")


@pytest.mark.parametrize(
    "usage",
    [
        {"input_tokens": -1},
        {"output_tokens": True},
        {
            "cache_creation_input_tokens": 2,
            "cache_creation": {
                "ephemeral_5m_input_tokens": 2,
                "ephemeral_1h_input_tokens": 1,
            },
        },
    ],
)
def test_invalid_usage_does_not_validate_counters(usage: Any) -> None:
    with pytest.raises(ValueError):
        _usage("messages", usage)
    body = envelope("messages", "{}")
    body["usage"] = usage
    response = AnthropicAdapter("messages", "offline-model")._response(
        json.dumps(body).encode(), body, "msg-offline"
    )
    assert response.evidence is not None and response.evidence.usage is None


def test_unknown_envelope_and_cache_fields_cannot_be_retained() -> None:
    body = envelope("messages", "{}")
    body["usage"]["cache_creation"] = {
        "ephemeral_5m_input_tokens": 1,
        "secret": "PRIVATE_CACHE",
    }
    assert not _safe_original_envelope(body, "messages")
    body["private"] = "PRIVATE_ENVELOPE"
    response = AnthropicAdapter("messages", "offline-model")._response(
        json.dumps(body).encode(), body, "msg-offline"
    )
    assert response.evidence is not None and response.evidence.safe_response is not None
    assert b"PRIVATE" not in response.evidence.safe_response


@pytest.mark.parametrize(
    "text,code",
    [
        ("not-json", "json_syntax"),
        ('{"scores":[],"scores":[]}', "duplicate_keys"),
        ("[]", "root_type"),
        ('{"scores":[{"trait_id":"unknown","category_label":"low"}]}', "trait_error"),
    ],
)
def test_diagnostics_do_not_repair_invalid_assessments(
    tmp_path: Path, text: str, code: str
) -> None:
    wire, *_ = wire_for(tmp_path)
    component = _extract_assessment_component(envelope("messages", text), "messages")
    result = diagnose_assessment_output(component, wire.output_commitment.contract)
    assert not result.canonical_valid
    # Codes are closed and content-free; exact code vocabulary is shared with Phase 5A.
    assert result.code == code
    assert component.output_text == text.encode()


@pytest.mark.parametrize("cache", ["off", "auto", "explicit"])
@pytest.mark.parametrize("tier", [None, "auto", "standard_only"])
@pytest.mark.asyncio
async def test_explicit_cache_tier_bytes_and_generation_omission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cache: Literal["off", "auto", "explicit"],
    tier: str | None,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "SYNTHETIC_POLICY_KEY")
    spec, _, records = case(tmp_path)
    spec.model = "claude-sonnet-4-6"
    spec.execution.cache_policy = cache
    spec.execution.tier = tier
    from judgmentllm.planning import plan_record
    from judgmentllm.preparation.session import RequestDependency

    request = next(plan_record(spec, records["001"]))
    dep = RequestDependency(request=request, dependency_sha256="0" * 64)
    wire = prepare_production_request(
        spec,
        dep,
        physical_identity("001", request.request_uid, "attempt"),
        production_context(spec),
    )
    body = json.loads(wire.payload)
    assert wire.generation is None and body["max_tokens"] == 4096
    if tier is None:
        assert "service_tier" not in body
    else:
        assert body["service_tier"] == tier
    content = body["messages"][0]["content"]
    if cache == "explicit":
        assert content == [
            {
                "type": "text",
                "text": wire.rendered_text,
                "cache_control": {"type": "ephemeral", "ttl": "5m"},
            }
        ]
    else:
        assert content == wire.rendered_text
    seen: list[bytes] = []

    def response(r: httpx2.Request) -> httpx2.Response:
        seen.append(r.content)
        return httpx2.Response(200, json=envelope("messages", "{}"))

    adapter = AnthropicAdapter(
        "messages", spec.model, http_transport=httpx2.MockTransport(response)
    )
    try:
        identity = wire.transport_plan.identity
        await adapter.submit(
            wire,
            submission_id=identity.submission_id,
            correlation_id=identity.correlation_id,
        )
        assert seen == [wire.payload]
    finally:
        await adapter.aclose()


@pytest.mark.parametrize("encoded", [False, True])
@pytest.mark.asyncio
async def test_credential_echo_is_never_retained_or_hashed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, encoded: bool
) -> None:
    key = "SYNTHETIC_ECHO_CREDENTIAL"
    monkeypatch.setenv("ANTHROPIC_API_KEY", key)
    wire, spec, _, _ = wire_for(tmp_path)
    body = json.dumps(envelope("messages", key))
    if encoded:
        body = body.replace(key, "".join(f"\\u{ord(c):04x}" for c in key))
    adapter = AnthropicAdapter(
        "messages",
        spec.model,
        http_transport=httpx2.MockTransport(
            lambda request: httpx2.Response(200, content=body.encode())
        ),
    )
    try:
        with pytest.raises(SubmissionUncertain) as info:
            identity = wire.transport_plan.identity
            await adapter.submit(
                wire,
                submission_id=identity.submission_id,
                correlation_id=identity.correlation_id,
            )
        assert key not in str(info.value) and adapter.actual_call_count == 1
        assert not adapter._responses
    finally:
        await adapter.aclose()


def test_oversized_valid_json_is_not_a_successful_assessment() -> None:
    from judgmentllm.anthropic_assessment import MAX_ASSESSMENT_COMPONENT_BYTES

    text = '{"winner_id":"001"}' + " " * MAX_ASSESSMENT_COMPONENT_BYTES
    adapter = AnthropicAdapter("messages", "offline-model")
    body = envelope("messages", text)
    response = adapter._response(json.dumps(body).encode(), body, body["id"])
    assert response.kind == "provider_error"
    assert response.evidence is not None
    assert response.evidence.assessment_component is not None
    assert response.evidence.assessment_component.extraction_status == "oversized"
    assert adapter.parse(response).output is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("type", "future"),
        ("role", "user"),
        ("content", [42]),
        ("content", None),
        ("usage", []),
        ("usage", {"input_tokens": True}),
        ("usage", {"cache_creation": []}),
    ],
)
def test_malformed_envelope_components_never_create_a_success_or_fake_usage(
    field: str, value: Any
) -> None:
    body = envelope("messages", '{"winner_id":"001"}')
    body[field] = value
    adapter = AnthropicAdapter("messages", "offline-model")
    response = adapter._response(json.dumps(body).encode(), body, body["id"])
    assert response.evidence is not None
    if field == "usage":
        assert response.evidence.usage is None
        assert adapter.parse(response).output == {"winner_id": "001"}
    else:
        assert response.kind == "provider_error"
        assert adapter.parse(response).output is None


@pytest.mark.parametrize("operation", ["usage", "component"])
def test_provider_specific_extractors_reject_foreign_endpoints(operation: str) -> None:
    with pytest.raises(ValueError):
        if operation == "usage":
            _usage("responses", {})
        else:
            _extract_assessment_component(envelope("messages", "{}"), "responses")


def test_forged_component_cannot_smuggle_withheld_or_mismatched_text() -> None:
    from judgmentllm.anthropic_assessment import AnthropicAssessmentComponentV2

    component = _extract_assessment_component(envelope("messages", "{}"), "messages")
    with pytest.raises(ValueError):
        AnthropicAssessmentComponentV2.model_validate(
            dict(component) | {"output_text": b"PRIVATE_MISMATCH"}
        )
    body = envelope("messages", "{}")
    body["content"] = []
    withheld = _extract_assessment_component(body, "messages")
    with pytest.raises(ValueError) as error:
        AnthropicAssessmentComponentV2.model_validate(
            dict(withheld) | {"output_text": b"PRIVATE_WITHHELD"}
        )
    assert "PRIVATE" not in str(error.value)


@pytest.mark.parametrize(
    "reason,details,outcome",
    [
        ("end_turn", None, "completed"),
        ("refusal", None, "refusal"),
        ("refusal", {"type": "refusal", "explanation": "PRIVATE_REFUSAL"}, "refusal"),
        ("end_turn", {"type": "refusal", "explanation": "PRIVATE_REFUSAL"}, "refusal"),
        ("max_tokens", {"type": "refusal"}, "refusal"),
        ("end_turn", {}, "unsupported_output"),
        (
            "end_turn",
            {"type": "future", "secret": "PRIVATE_DETAILS"},
            "unsupported_output",
        ),
        ("end_turn", {"type": []}, "unsupported_output"),
        ("end_turn", ["PRIVATE_DETAILS"], "unsupported_output"),
        ("end_turn", "PRIVATE_DETAILS", "unsupported_output"),
        ("end_turn", False, "unsupported_output"),
        ("refusal", {"type": "future"}, "refusal"),
    ],
)
def test_stop_details_override_json_and_never_enter_diagnostics(
    tmp_path: Path,
    reason: str,
    details: object,
    outcome: str,
) -> None:
    wire, *_ = wire_for(tmp_path, pairwise=True)
    body = envelope("messages", '{"winner_id":"001"}')
    body.update(stop_reason=reason, stop_details=details)
    kind, text, observed = _messages_output(body)
    assert observed == outcome
    assert kind == (
        "response"
        if outcome == "completed"
        else "refusal"
        if outcome == "refusal"
        else "provider_error"
    )
    assert (text is not None) is (outcome == "completed")
    component = _extract_assessment_component(body, "messages")
    diagnostic = diagnose_assessment_output(component, wire.output_commitment.contract)
    assert diagnostic.canonical_valid is (outcome == "completed")
    assert component.refusal_present is (outcome == "refusal")
    assert component.unsupported_present is (
        details is not None
        and not (
            isinstance(details, dict)
            and cast(dict[str, object], details).get("type") == "refusal"
        )
    )
    assert "PRIVATE" not in component.model_dump_json()
    adapter = AnthropicAdapter("messages", "ordinary-model-alias")
    response = adapter._response(json.dumps(body).encode(), body, "msg-offline")
    parsed = adapter.parse(response)
    assert parsed.kind == kind
    assert (parsed.output is not None) is (outcome == "completed")
    assert response.evidence is not None
    safe = response.evidence.safe_response
    assert safe is not None and b"PRIVATE" not in safe
    if details is not None:
        assert not response.evidence.original_body_safe
