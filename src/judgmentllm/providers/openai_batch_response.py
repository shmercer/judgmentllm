"""Screen Batch members before hashes, artifacts, SQLite or normalization."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Literal

from judgmentllm.evidence._base import EvidenceModel
from judgmentllm.evidence.usage import ReportedUsageInput
from judgmentllm.openai_batch_transport import PreparedOpenAIBatchRequest
from judgmentllm.output_contracts import decode_assessment_output
from judgmentllm.persistence.hashing import canonical_bytes, semantic_digest
from judgmentllm.providers.evidence import ProviderEvidence, ProviderObservationContext
from judgmentllm.providers.openai_response import (
    _chat_output,
    _extract_assessment_component,
    _mapping,
    _responses_output,
    _safe_original_envelope,
    _usage,
)
from judgmentllm.providers.protocol import ParsedResponse, ProviderResponse

# pyright: reportPrivateUsage=false


def unsafe(raw: bytes) -> bool:
    return (
        re.search(
            rb"(?:sk-[A-Za-z0-9_-]{8,}|PROHIBITED_[A-Z_]+|Bearer\s+[^\s\"]+)", raw
        )
        is not None
    )


class ScreenedOpenAIBatchRowV1(EvidenceModel):
    record_type: Literal["screened_openai_batch_row_v1"] = (
        "screened_openai_batch_row_v1"
    )
    custom_id: str
    kind: Literal["response", "refusal", "provider_error", "confirmed_cancellation"]
    assessment_text: str | None
    usage: ReportedUsageInput
    reported_model: str | None
    remote_response_id: str | None
    provider_outcome: Literal[
        "completed",
        "refusal",
        "content_filter",
        "output_limit",
        "incomplete",
        "provider_failure",
        "missing_output",
        "multiple_outputs",
        "unsupported_output",
        "tool_call",
    ]


def safe_id(value: object, prefix: str) -> str:
    if (
        type(value) is not str
        or not re.fullmatch(re.escape(prefix) + r"[A-Za-z0-9_-]{1,160}", value)
        or unsafe(value.encode())
    ):
        raise ValueError("unsafe provider identity")
    return value


def screen_result(
    raw: bytes,
    expected_keys: Mapping[str, PreparedOpenAIBatchRequest],
    endpoint: str,
    *,
    allow_replay: bool = False,
) -> tuple[str, ProviderResponse, ReportedUsageInput, bytes, bool] | None:
    try:
        row = _mapping(decode_assessment_output(raw))
        if row.get("record_type") == "screened_openai_batch_row_v1":
            if not allow_replay:
                return None
            saved = ScreenedOpenAIBatchRowV1.model_validate_json(raw)
            if saved.custom_id not in expected_keys or unsafe(canonical_bytes(saved)):
                return None
            return observation(saved, canonical_bytes(saved), False)
        if set(row) - {
            "id",
            "custom_id",
            "response",
            "error",
            "api_key",
        }:
            return None
        key = row.get("custom_id")
        if type(key) is not str or key not in expected_keys:
            return None
        wire = expected_keys[key]
        body = None
        reported = ReportedUsageInput()
        kind, text, outcome = "provider_error", None, "provider_failure"
        exact = False
        if row.get("response") is not None:
            response = _mapping(row["response"])
            if set(response) - {
                "status_code",
                "request_id",
                "body",
            }:
                return None
            status = response.get("status_code")
            if type(status) is not int or not 100 <= status <= 599:
                return None
            if row.get("error") is not None:
                return None
            if status == 200:
                body = _mapping(response.get("body"))
                kind, text, outcome = (
                    _responses_output(body)
                    if endpoint == "responses"
                    else _chat_output(body)
                )
                try:
                    reported = (
                        _usage(endpoint, body.get("usage")) or ReportedUsageInput()
                    )
                except ValueError:
                    pass
                exact = _safe_original_envelope(
                    body, endpoint, _mapping(decode_assessment_output(wire.payload))
                )
                if response.get("request_id") is not None:
                    safe_id(response["request_id"], "req_")
        else:
            error = _mapping(row.get("error"))
            if set(error) - {"code", "message"}:
                return None
            # Error messages and arbitrary codes never enter durable evidence.
            if error.get("code") == "batch_cancelled":
                kind = "confirmed_cancellation"
        remote_id = reported_model = None
        if body is not None:
            remote_id = safe_id(
                body.get("id"), "resp" if endpoint == "responses" else "chatcmpl"
            )
            model = body.get("model")
            if type(model) is str and re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}", model
            ):
                reported_model = model
            component = _extract_assessment_component(body, endpoint)
            if (
                text is None
                and component.extraction_status == "extracted"
                and component.output_text is not None
            ):
                text = component.output_text.decode("utf-8")
            if text is not None and component.extraction_status != "extracted":
                return None
        if row.get("id") is not None:
            safe_id(row["id"], "batch_req_")
        saved = ScreenedOpenAIBatchRowV1.model_validate(
            dict(
                custom_id=key,
                kind=kind,
                assessment_text=text,
                usage=reported,
                reported_model=reported_model,
                remote_response_id=remote_id,
                provider_outcome=outcome,
            )
        )
        safe = canonical_bytes(saved)
        if unsafe(safe):
            return None
        exact = exact and "api_key" not in row and not unsafe(raw)
        return observation(saved, canonical_bytes(row) if exact else safe, exact)
    except (ValueError, TypeError, KeyError, UnicodeError):
        return None


def observation(
    saved: ScreenedOpenAIBatchRowV1, safe: bytes, exact: bool
) -> tuple[str, ProviderResponse, ReportedUsageInput, bytes, bool]:
    key = semantic_digest(
        "batch-observation-v1",
        [saved.custom_id, "output" if saved.kind == "response" else "error"],
    )
    response = ProviderResponse(
        observation_key=key,
        kind=saved.kind,
        raw=canonical_bytes(saved),
        evidence=ProviderEvidence(
            provenance="provider_documented",
            safe_response=safe,
            original_body_safe=exact,
            usage=saved.usage,
            observation_context=ProviderObservationContext(
                sdk_version="3.42.1",
                invocation_kind="native_batch",
                reported_model=saved.reported_model,
                remote_response_id=saved.remote_response_id,
                provider_outcome=saved.provider_outcome,
            ),
        ),
    )
    return saved.custom_id, response, saved.usage, safe, exact


def parse(response: ProviderResponse, *, strict_output: bool = False) -> ParsedResponse:
    saved = ScreenedOpenAIBatchRowV1.model_validate_json(response.raw)
    if saved.kind != response.kind:
        raise ValueError("OpenAI Batch assessment identity differs")
    try:
        output = (
            decode_assessment_output(saved.assessment_text)
            if saved.kind == "response" and isinstance(saved.assessment_text, str)
            else None
        )
        return ParsedResponse.model_validate(dict(kind=saved.kind, output=output))
    except (ValueError, TypeError):
        return ParsedResponse(kind=saved.kind, output=None)
