"""Screen Anthropic Batch wire evidence before durable writes or hashes."""

import re
from collections.abc import Mapping
from typing import Any, Literal, cast, get_args

from pydantic import Field

from judgmentllm.anthropic_batch_transport import BATCH_ORIGIN, BATCH_PATH
from judgmentllm.batch_models import (
    AnthropicBatchCountsV1,
    AnthropicBatchError,
    AnthropicBatchResult,
    AnthropicBatchStatusV1,
)
from judgmentllm.evidence._base import EvidenceModel
from judgmentllm.evidence.usage import ReportedUsageInput
from judgmentllm.output_contracts import decode_assessment_output
from judgmentllm.persistence.hashing import canonical_bytes, semantic_digest
from judgmentllm.providers.anthropic import _contains_credential
from judgmentllm.providers.anthropic_response import (
    _extract_assessment_component,
    _mapping,
    _messages_output,
    _safe_original_envelope,
    _usage,
)
from judgmentllm.providers.batch_protocol import BatchEvidenceWithheld
from judgmentllm.providers.evidence import ProviderEvidence, ProviderObservationContext
from judgmentllm.providers.protocol import ParsedResponse, ProviderResponse

# pyright: reportPrivateUsage=false

CONTROL_FIELDS = (
    "id",
    "type",
    "processing_status",
    "created_at",
    "expires_at",
    "ended_at",
    "archived_at",
    "cancel_initiated_at",
    "results_url",
    "request_counts",
)
_ERROR_TYPES = frozenset(get_args(AnthropicBatchError))


def unsafe(raw: bytes, key: str) -> bool:
    return _contains_credential(raw, key) or _contains_credential(raw, "sk-ant-")


def safe_id(value: object, prefix: str, key: str) -> str:
    if (
        type(value) is not str
        or not re.fullmatch(re.escape(prefix) + r"[A-Za-z0-9_-]{1,160}", value)
        or unsafe(value.encode(), key)
    ):
        raise BatchEvidenceWithheld("unsafe Anthropic Batch identifier")
    return value


def screen_job(
    raw: bytes,
    *,
    key: str,
    submission_id: str,
    expected_job: str | None,
    expected_count: int,
) -> tuple[AnthropicBatchStatusV1, bytes, bool]:
    try:
        if unsafe(raw, key):
            raise ValueError("unsafe job response")
        value = _mapping(decode_assessment_output(raw.decode("utf-8")))
        job = safe_id(value.get("id"), "msgbatch_", key)
        state = value.get("processing_status")
        if (
            value.get("type") != "message_batch"
            or state not in ("in_progress", "canceling", "ended")
            or expected_job is not None
            and expected_job != job
        ):
            raise ValueError("job does not match the admitted call")
        counts = AnthropicBatchCountsV1.model_validate(
            {"schema_version": 1, **_mapping(value.get("request_counts"))}
        )
        if (
            counts.total != expected_count
            or "schema_version" in value["request_counts"]
        ):
            raise ValueError("job count does not match its sealed members")
        times = {
            name: value.get(name)
            for name in (
                "created_at",
                "expires_at",
                "ended_at",
                "archived_at",
                "cancel_initiated_at",
            )
        }
        url = value.get("results_url")
        if (
            (state == "ended") != (times["ended_at"] is not None)
            or state == "ended"
            and counts.processing != 0
            or state != "ended"
            and (url is not None or times["archived_at"] is not None)
            or url is not None
            and url != f"{BATCH_ORIGIN}{BATCH_PATH}/{job}/results"
        ):
            raise ValueError("result source is not bound to the accepted job")
        status = AnthropicBatchStatusV1.model_validate(
            {
                "submission_id": submission_id,
                "remote_id": job,
                "state": "completed" if state == "ended" else "running",
                "provider_state": state,
                "results_url": url,
                "counts": counts,
                **times,
            }
        )
        exact = not (set(value) - set(CONTROL_FIELDS))
        safe = canonical_bytes(
            {field: value[field] for field in CONTROL_FIELDS if field in value}
        )
        return status, safe, exact
    except (ValueError, TypeError, RecursionError, UnicodeError, BatchEvidenceWithheld):
        raise BatchEvidenceWithheld("unsafe Anthropic Batch job evidence") from None


def screen_error(
    raw: bytes, request_id: str | None
) -> tuple[AnthropicBatchError | None, bytes | None]:
    try:
        document = _mapping(decode_assessment_output(raw.decode("utf-8")))
        code = _mapping(document.get("error")).get("type")
        if (
            document.get("type") == "error"
            and type(code) is str
            and code in _ERROR_TYPES
        ):
            safe: dict[str, object] = {"type": "error", "error": {"type": code}}
            if request_id is not None and document.get("request_id") == request_id:
                safe["request_id"] = request_id
            return cast(AnthropicBatchError, code), canonical_bytes(safe)
    except (ValueError, TypeError, RecursionError):
        pass
    return None, None


class ScreenedAnthropicBatchRowV1(EvidenceModel):
    record_type: Literal["screened_anthropic_batch_row_v1"] = (
        "screened_anthropic_batch_row_v1"
    )
    custom_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    provider_result: AnthropicBatchResult
    kind: Literal["response", "refusal", "provider_error", "confirmed_cancellation"]
    assessment_text: str | None = Field(repr=False)
    usage: ReportedUsageInput
    remote_response_id: str | None
    reported_model: str | None
    provider_outcome: str


def screen_row(
    raw: bytes,
    expected_keys: Mapping[str, Any],
    key: str,
    *,
    replay: bool = False,
) -> tuple[str, ProviderResponse, ReportedUsageInput, bytes, bool] | None:
    try:
        decoded = decode_assessment_output(raw)
    except ValueError:
        if replay:
            return None
        raise
    try:
        if unsafe(raw, key):
            return None
        row = _mapping(decoded)
        member = row.get("custom_id")
        if type(member) is not str or member not in expected_keys:
            return None
        exact = False
        if replay and row.get("record_type") == "screened_anthropic_batch_row_v1":
            saved = ScreenedAnthropicBatchRowV1.model_validate_json(raw)
            if canonical_bytes(saved) != raw:
                return None
        else:
            if set(row) != {"custom_id", "result"}:
                return None
            result = _mapping(row.get("result"))
            outer = result.get("type")
            text = remote_id = model = None
            usage = ReportedUsageInput()
            kind, outcome = "provider_error", "provider_failure"
            if outer == "succeeded" and set(result) == {"type", "message"}:
                body = _mapping(result.get("message"))
                remote_id = safe_id(body.get("id"), "msg_", key)
                model = safe_id(body.get("model"), "", key)
                kind, text, outcome = _messages_output(body)
                try:
                    usage = _usage("messages", body.get("usage")) or usage
                    exact = kind == "response" and _safe_original_envelope(
                        body, "messages"
                    )
                except (ValueError, TypeError):
                    exact = False
                component = _extract_assessment_component(body, "messages")
                if (
                    text is None
                    and component.extraction_status == "extracted"
                    and component.output_text is not None
                ):
                    text = component.output_text.decode("utf-8")
                if text is not None and component.extraction_status != "extracted":
                    return None
            elif outer == "errored" and set(result) == {"type", "error"}:
                _mapping(result.get("error"))
            elif outer in ("canceled", "expired") and set(result) == {"type"}:
                if outer == "canceled":
                    kind = "confirmed_cancellation"
            else:
                return None
            saved = ScreenedAnthropicBatchRowV1.model_validate(
                dict(
                    custom_id=member,
                    provider_result=outer,
                    kind=kind,
                    assessment_text=text,
                    usage=usage,
                    remote_response_id=remote_id,
                    reported_model=model,
                    provider_outcome=outcome,
                )
            )
        normalized = canonical_bytes(saved)
        safe = canonical_bytes(row) if exact else normalized
        response = ProviderResponse(
            observation_key=semantic_digest(
                "batch-observation-v1",
                [member, "output" if saved.kind == "response" else "error"],
            ),
            kind=saved.kind,
            raw=normalized,
            evidence=ProviderEvidence(
                provenance="provider_documented",
                safe_response=safe,
                original_body_safe=exact,
                usage=saved.usage,
                observation_context=ProviderObservationContext.model_validate(
                    dict(
                        sdk_version="2.3.0",
                        invocation_kind="native_batch",
                        reported_model=saved.reported_model,
                        remote_response_id=saved.remote_response_id,
                        provider_outcome=saved.provider_outcome,
                    )
                ),
            ),
        )
        return member, response, saved.usage, safe, exact
    except (ValueError, TypeError, KeyError, RecursionError, BatchEvidenceWithheld):
        return None


def parse(response: ProviderResponse, *, strict_output: bool = False) -> ParsedResponse:
    saved = ScreenedAnthropicBatchRowV1.model_validate_json(response.raw)
    if saved.kind != response.kind:
        raise ValueError("Anthropic Batch assessment identity differs")
    try:
        output = (
            decode_assessment_output(saved.assessment_text)
            if saved.kind == "response" and saved.assessment_text is not None
            else None
        )
        return ParsedResponse.model_validate(dict(kind=saved.kind, output=output))
    except (ValueError, TypeError, RecursionError):
        return ParsedResponse(kind=saved.kind)
