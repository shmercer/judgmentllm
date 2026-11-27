"""Screen Batch members before hashes, artifacts, SQLite or normalization."""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from itertools import chain
from typing import Literal, cast

from judgmentllm.evidence._base import EvidenceModel
from judgmentllm.evidence.usage import ReportedUsageInput
from judgmentllm.openai_batch_transport import PreparedOpenAIBatchRequest
from judgmentllm.output_contracts import decode_assessment_output
from judgmentllm.persistence.hashing import canonical_bytes, semantic_digest
from judgmentllm.providers.evidence import ProviderEvidence, ProviderObservationContext
from judgmentllm.providers.google_response import contains_credential
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

_UNSAFE_TEXT = re.compile(
    r'(?:sk-[A-Za-z0-9_-]{8,}|PROHIBITED_[A-Z_]+|Bearer\s+[^\s"]+)'
)
_UNSAFE_BYTES = re.compile(_UNSAFE_TEXT.pattern.encode("ascii"))
_RESPONSE_REQUEST_UUID_V4 = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)


def unsafe(raw: bytes) -> bool:
    return _UNSAFE_BYTES.search(raw) is not None


def unsafe_assessment(text: str | None, active_key: str | None) -> bool:
    if text is None:
        return False
    try:
        decoded = decode_assessment_output(text)
    except ValueError:
        # Invalid assessment syntax remains subject to the ordinary text screen.
        return False
    return _unsafe_decoded(decoded, active_key)


def _unsafe_decoded(decoded: object, active_key: str | None) -> bool:
    # Keep one iterator per open container and screen values before JSON escaping.
    pending: list[Iterator[object]] = [iter((decoded,))]
    while pending:
        try:
            item = next(pending[-1])
        except StopIteration:
            pending.pop()
            continue
        if isinstance(item, str):
            raw = item.encode("utf-8")
            if _UNSAFE_TEXT.search(item) or (
                active_key is not None and contains_credential(raw, active_key)
            ):
                return True
        elif isinstance(item, list):
            pending.append(iter(cast(list[object], item)))
        elif isinstance(item, dict):
            fields = cast(dict[str, object], item)
            pending.append(chain(fields, fields.values()))
    return False


ScreenedResult = tuple[str, ProviderResponse, ReportedUsageInput, bytes, bool]


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


def _response_request_uuid(value: object) -> bool:
    return type(value) is str and _RESPONSE_REQUEST_UUID_V4.fullmatch(value) is not None


def uses_responses_request_uuid(raw: bytes) -> bool:
    """Identify the UUID form in an already screened Responses wire row."""
    row = _mapping(decode_assessment_output(raw))
    response = _mapping(row.get("response") or {})
    return response.get("status_code") == 200 and _response_request_uuid(
        response.get("request_id")
    )


def screen_result(
    raw: bytes,
    expected_keys: Mapping[str, PreparedOpenAIBatchRequest],
    endpoint: str,
    *,
    allow_replay: bool = False,
    verify_prefix: bool = False,
    active_key: str | None = None,
) -> ScreenedResult | None:
    try:
        row = _mapping(decode_assessment_output(raw))
        if row.get("record_type") == "screened_openai_batch_row_v1":
            if not allow_replay:
                return None
            saved = ScreenedOpenAIBatchRowV1.model_validate_json(raw)
            if (
                saved.custom_id not in expected_keys
                or unsafe(canonical_bytes(saved))
                or unsafe_assessment(saved.assessment_text, active_key)
            ):
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
                request_id = response.get("request_id")
                if request_id is not None and not (
                    endpoint == "responses" and _response_request_uuid(request_id)
                ):
                    safe_id(request_id, "req_")
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
        if not verify_prefix and unsafe_assessment(text, active_key):
            return None
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
        # Retained exact evidence is already bound by its member receipt. Its
        # canonical bytes can differ from the original wire spelling.
        exact = exact and "api_key" not in row and (allow_replay or not unsafe(raw))
        if exact:
            original = canonical_bytes(row)
            # Previously committed prefixes and receipts use the original screen.
            if (
                allow_replay
                or verify_prefix
                or (
                    not unsafe(original)
                    and not _unsafe_decoded(row, active_key)
                    and (active_key is None or not contains_credential(raw, active_key))
                )
            ):
                return observation(saved, original, True)
        return observation(saved, safe, False)
    except (ValueError, TypeError, KeyError, UnicodeError):
        return None


def observation(
    saved: ScreenedOpenAIBatchRowV1, safe: bytes, exact: bool
) -> ScreenedResult:
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
