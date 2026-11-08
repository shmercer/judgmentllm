"""Documented endpoint envelopes and exact assessment text interpretation."""

# pyright: reportPrivateUsage=false
from __future__ import annotations

import json
from copy import deepcopy
from typing import Any, cast

import pytest
from test_openai_lifecycle import envelope

from judgmentllm.evidence import (
    EvidenceIdentity,
    UsageContext,
    extract_openai_responses_usage,
)
from judgmentllm.evidence.usage import OpenAIResponsesUsageInput
from judgmentllm.providers.openai import OpenAIAdapter


@pytest.mark.parametrize("endpoint", ["responses", "chat.completions"])
@pytest.mark.parametrize(
    "scenario,expected",
    [
        ("valid", "completed"),
        ("refusal", "refusal"),
        ("length", "output_limit"),
        ("filter", "content_filter"),
        ("missing", "missing_output"),
        ("multiple", "multiple_outputs"),
        ("tool", "tool_call"),
        ("no_text", "missing_output"),
        ("bad_role", "unsupported_output"),
    ],
)
def test_explicit_non_success_outcomes(
    endpoint: str, scenario: str, expected: str
) -> None:
    body = envelope(endpoint, '{"scores":[]}')
    if endpoint == "responses":
        if scenario == "refusal":
            body["output"][0]["content"] = [
                {"type": "refusal", "refusal": "private-refusal-text"}
            ]
        elif scenario in {"length", "filter"}:
            body.update(
                status="incomplete",
                incomplete_details={
                    "reason": "max_output_tokens"
                    if scenario == "length"
                    else "content_filter"
                },
            )
        elif scenario == "missing":
            body["output"] = []
        elif scenario == "multiple":
            body["output"].append(deepcopy(body["output"][0]))
        elif scenario == "tool":
            body["output"] = [
                {"type": "function_call", "arguments": "private-tool-text"}
            ]
            expected = "unsupported_output"
        elif scenario == "no_text":
            body["output"][0]["content"][0]["text"] = None
        elif scenario == "bad_role":
            body["output"][0]["role"] = "user"
            expected = "incomplete"
    else:
        if scenario == "refusal":
            body["choices"][0]["message"]["refusal"] = "private-refusal-text"
        elif scenario in {"length", "filter"}:
            body["choices"][0]["finish_reason"] = (
                "length" if scenario == "length" else "content_filter"
            )
        elif scenario == "missing":
            body["choices"] = []
        elif scenario == "multiple":
            body["choices"].append(deepcopy(body["choices"][0]))
        elif scenario == "tool":
            body["choices"][0]["message"]["tool_calls"] = [{"type": "function"}]
        elif scenario == "no_text":
            body["choices"][0]["message"]["content"] = None
        elif scenario == "bad_role":
            body["choices"][0]["message"]["role"] = "user"
    adapter = OpenAIAdapter(endpoint, "offline-model")
    response = adapter._response(json.dumps(body).encode(), body, body["id"])
    assert (
        response.evidence is not None
        and response.evidence.observation_context is not None
    )
    assert response.evidence.observation_context.provider_outcome == expected
    assert adapter.parse(response).kind == (
        "response"
        if scenario == "valid"
        else "refusal"
        if scenario == "refusal" or (scenario == "filter" and endpoint != "responses")
        else "provider_error"
    )
    assert b"private-refusal-text" not in (response.evidence.safe_response or b"")
    assert response.evidence.usage is not None


@pytest.mark.parametrize("endpoint", ["responses", "chat.completions"])
@pytest.mark.parametrize(
    "text",
    [
        "not JSON",
        '```json\n{"scores":[]}\n```',
        '{"scores":[],"scores":[]}',
        '{"scores":[{"trait_id":"01","trait_id":"02"}]}',
        '{"winner_id":"001","winner_id":"002"}',
    ],
)
def test_original_text_is_never_repaired(endpoint: str, text: str) -> None:
    adapter = OpenAIAdapter(endpoint, "offline-model")
    body = envelope(endpoint, text)
    response = adapter._response(json.dumps(body).encode(), body, body["id"])
    assert response.evidence is not None
    assert response.evidence.output_text == text.encode()
    with pytest.raises(ValueError, match="OpenAI assessment output is invalid"):
        adapter.parse(response, strict_output=True)


def test_responses_reasoning_is_not_assessment_or_retained() -> None:
    body = envelope("responses", '{"scores":[]}')
    body["output"].insert(
        0,
        {
            "type": "reasoning",
            "summary": [
                {"type": "summary_text", "text": "private-unauthorized-reasoning"}
            ],
            "encrypted_content": "private-encrypted-reasoning",
        },
    )
    response = OpenAIAdapter("responses", "offline-model")._response(
        json.dumps(body).encode(), body, body["id"]
    )
    assert response.kind == "response" and response.evidence is not None
    assert b"private-unauthorized-reasoning" not in (
        response.evidence.safe_response or b""
    )
    assert b"private-encrypted-reasoning" not in (
        response.evidence.safe_response or b""
    )
    assert not response.evidence.reasoning
    assert response.evidence.original_body_safe is False


@pytest.mark.parametrize("endpoint", ["responses", "chat.completions"])
@pytest.mark.parametrize(
    "usage",
    [None, {}, {"total_tokens": 7}, {"total_tokens": -1}, {"total_tokens": True}],
)
def test_usage_missing_partial_invalid_remain_unknown(
    endpoint: str, usage: Any
) -> None:
    body = envelope(endpoint, '{"scores":[]}')
    body["usage"] = usage
    response = OpenAIAdapter(endpoint, "offline-model")._response(
        json.dumps(body).encode(), body, body["id"]
    )
    assert response.evidence is not None
    found = response.evidence.usage
    assert response.kind == "response"
    if usage is None or (
        isinstance(usage, dict)
        and (
            cast(dict[str, Any], usage).get("total_tokens") == -1
            or cast(dict[str, Any], usage).get("total_tokens") is True
        )
    ):
        assert found is None
    else:
        assert (
            found is not None
            and found.input_tokens is None
            and found.output_tokens is None
        )
        assert found.total_tokens == (7 if usage else None)


def test_responses_usage_source_and_overlap() -> None:
    identity = EvidenceIdentity(
        run_id="run",
        request_uid="req",
        attempt_id="attempt",
        submission_id="submission",
        observation_id="observation",
    )
    context = UsageContext(
        provider="openai",
        endpoint="responses",
        requested_model="requested",
        reported_model="reported",
        observation_kind="response",
    )
    usage = OpenAIResponsesUsageInput.model_validate(
        {
            "input_tokens": 12,
            "output_tokens": 8,
            "total_tokens": 20,
            "input_tokens_details": {"cached_tokens": 4},
            "output_tokens_details": {"reasoning_tokens": 3},
        }
    )
    result = extract_openai_responses_usage(identity, context, usage)
    assert result.usage_source == "openai.responses.usage"
    assert (
        result.input_tokens,
        result.output_tokens,
        result.total_tokens,
        result.cache_read_tokens,
        result.reasoning_tokens,
    ) == (12, 8, 20, 4, 3)
    assert (
        result.breakdown[-1].relationship
        == result.breakdown[-2].relationship
        == "included_in"
    )
    assert result.reported_model == "reported"
    with pytest.raises(ValueError):
        extract_openai_responses_usage(
            identity, context.model_copy(update={"endpoint": "chat.completions"}), usage
        )
    assert extract_openai_responses_usage(identity, context, None).input_tokens is None


@pytest.mark.parametrize("endpoint", ["responses", "chat.completions"])
def test_exact_safe_original_and_recovery_parser(endpoint: str) -> None:
    from judgmentllm.providers.protocol import ProviderResponse

    body = envelope(endpoint, '{"scores":[]}')
    raw = json.dumps(body, ensure_ascii=False, indent=2).encode()
    adapter = OpenAIAdapter(endpoint, "offline-model")
    response = adapter._response(raw, body, body["id"])
    assert response.evidence is not None
    assert response.evidence.original_body_safe is True
    assert response.evidence.safe_response == raw
    restored = ProviderResponse(
        observation_key=response.observation_key, kind=response.kind, raw=raw
    )
    assert adapter.parse(restored).output == {"scores": []}


@pytest.mark.parametrize(
    "extension",
    [
        {"unknown_extension": {"private": "private-envelope-canary"}},
        {"metadata": {"account": "private-envelope-canary"}},
        {"text": {"unknown": "private-envelope-canary"}},
        {"reasoning": {"effort": {"account": "private-envelope-canary"}}},
    ],
)
def test_unsafe_envelope_extensions_force_derivative(extension: Any) -> None:
    body = envelope("responses", '{"scores":[]}')
    body.update(extension)
    adapter = OpenAIAdapter("responses", "offline-model")
    response = adapter._response(json.dumps(body).encode(), body, body["id"])
    assert response.evidence is not None
    assert response.evidence.original_body_safe is False
    assert b"private-envelope-canary" not in (response.evidence.safe_response or b"")
    assert adapter.parse(response).output == {"scores": []}
