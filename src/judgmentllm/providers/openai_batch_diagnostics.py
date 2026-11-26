"""Bounded diagnostic fields for this OpenAI Batch route only."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Annotated, Literal, Self, cast
from uuid import uuid4

from pydantic import Field, model_validator

from judgmentllm.evidence._base import EvidenceModel
from judgmentllm.persistence.hashing import canonical_bytes
from judgmentllm.persistence.models import SHA256
from judgmentllm.providers.google_response import contains_credential
from judgmentllm.providers.openai_batch_response import safe_id, unsafe

Operation = Literal[
    "upload_post",
    "upload_list",
    "upload_metadata",
    "upload_content",
    "create_post",
    "create_list",
    "create_get",
    "job_get",
    "file_metadata_initial",
    "file_content",
    "file_metadata_final",
    "result_row",
]
Phase = Literal["upload", "create", "result"]
Outcome = Literal[
    "observed",
    "local_failure",
    "transport_failure",
    "http_failure",
    "withheld",
    "indeterminate",
    "provider_rejected",
]
Reason = Literal[
    "headers",
    "pre_forward",
    "delegate",
    "body_read",
    "http_status",
    "body_decode",
    "bounds",
    "encoding",
    "binding",
    "sdk_wrapper",
    "unknown_stage",
    "no_match",
    "inconclusive_lookup",
    "job_failed",
    "job_expired",
    "member_response",
    "member_error",
    "member_cancelled",
    "row_screen",
]
Code = Literal["batch_cancelled", "batch_expired", "invalid_json_line"]
Status = Annotated[int, Field(strict=True, ge=100, le=599)]
RequestID = Annotated[str, Field(pattern=r"^req_[A-Za-z0-9_-]{1,160}$")]
CallID = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
RowPosition = Annotated[int, Field(strict=True, ge=0, lt=8_388_608)]
PREFIX = "openai-diagnostic:"
RESULT_SCREEN_PREFIX = "openai-result-screen:"

_OUTCOMES: dict[Reason, Outcome] = {
    "headers": "observed",
    "pre_forward": "local_failure",
    "delegate": "transport_failure",
    "body_read": "transport_failure",
    "http_status": "http_failure",
    "body_decode": "withheld",
    "bounds": "withheld",
    "encoding": "withheld",
    "binding": "withheld",
    "sdk_wrapper": "indeterminate",
    "unknown_stage": "indeterminate",
    "no_match": "indeterminate",
    "inconclusive_lookup": "indeterminate",
    "job_failed": "provider_rejected",
    "job_expired": "observed",
    "member_response": "observed",
    "member_error": "provider_rejected",
    "member_cancelled": "observed",
    "row_screen": "withheld",
}
_ROW_REASONS = {"member_response", "member_error", "member_cancelled", "row_screen"}


def phase_for(operation: Operation) -> Phase:
    if operation.startswith("upload_"):
        return "upload"
    if operation.startswith("create_"):
        return "create"
    return "result"


class OpenAIBatchDiagnosticV1(EvidenceModel):
    record_type: Literal["openai_batch_diagnostic_v1"] = "openai_batch_diagnostic_v1"
    phase: Phase
    operation: Operation
    binding_id: SHA256
    call_id: CallID | None
    row_position: RowPosition | None
    member_key: SHA256 | None
    outcome: Outcome
    reason: Reason
    http_status: Status | None
    http_request_id: RequestID | None
    member_status: Status | None
    member_request_id: RequestID | None
    provider_code: Code | None

    @model_validator(mode="after")
    def check_scope(self) -> Self:
        if (
            self.phase != phase_for(self.operation)
            or self.outcome != _OUTCOMES[self.reason]
        ):
            raise ValueError("OpenAI Batch diagnostic classification differs")
        if self.operation == "result_row":
            if (
                self.call_id is not None
                or self.row_position is None
                or self.http_status is not None
                or self.http_request_id is not None
                or self.reason not in _ROW_REASONS
                or (self.reason == "row_screen") != (self.member_key is None)
                or (
                    self.member_key is None
                    and any(
                        value is not None
                        for value in (
                            self.member_status,
                            self.member_request_id,
                            self.provider_code,
                        )
                    )
                )
            ):
                raise ValueError("OpenAI Batch diagnostic row binding differs")
        elif (
            self.call_id is None
            or self.row_position is not None
            or self.member_key is not None
            or self.member_status is not None
            or self.member_request_id is not None
            or self.reason in _ROW_REASONS
        ):
            raise ValueError("OpenAI Batch diagnostic call binding differs")
        for value in (self.http_request_id, self.member_request_id):
            if value is not None:
                safe_id(value, "req_")
        if self.provider_code is not None and self.reason not in {
            "http_status",
            "job_failed",
            "job_expired",
            "member_error",
            "member_cancelled",
        }:
            raise ValueError("OpenAI Batch diagnostic code lacks attributed evidence")
        if (
            (
                self.reason in ("job_failed", "job_expired")
                and self.operation not in ("create_post", "create_get", "job_get")
            )
            or (
                self.reason in ("job_failed", "job_expired", "sdk_wrapper", "no_match")
                and (self.http_status is None or not 200 <= self.http_status < 300)
            )
            or (
                self.reason == "no_match"
                and self.operation not in ("upload_list", "create_list")
            )
            or (
                self.reason == "pre_forward"
                and (self.http_status is not None or self.http_request_id is not None)
            )
            or (
                self.reason == "http_status"
                and self.http_status is not None
                and 200 <= self.http_status < 300
            )
        ):
            raise ValueError("OpenAI Batch diagnostic reason differs from its call")
        return self

    @property
    def entity(self) -> str:
        if self.call_id is not None:
            return f"{PREFIX}call:{self.call_id}"
        return f"{PREFIX}row:{self.binding_id}:{self.row_position}"


class OpenAIBatchResultScreenV1(EvidenceModel):
    record_type: Literal["openai_batch_result_screen_v1"] = (
        "openai_batch_result_screen_v1"
    )
    source_id: SHA256
    legacy_positions: Annotated[int, Field(strict=True, ge=0, le=8_388_608)]

    @property
    def entity(self) -> str:
        return RESULT_SCREEN_PREFIX + self.source_id


def clean(raw: bytes, key: str | None) -> bool:
    return not unsafe(raw) and (key is None or not contains_credential(raw, key))


def request_id(value: object, key: str | None) -> str | None:
    try:
        result = safe_id(value, "req_")
    except ValueError:
        return None
    return result if clean(result.encode(), key) else None


def status_code(value: object) -> int | None:
    return value if type(value) is int and 100 <= value <= 599 else None


def _object(value: object) -> dict[str, object]:
    return cast(dict[str, object], value) if isinstance(value, dict) else {}


def error_code(body: object, key: str | None) -> Code | None:
    return _code(_object(_object(body).get("error")).get("code"), key)


def _code(value: object, key: str | None) -> Code | None:
    if (
        type(value) is str
        and value
        in (
            "batch_cancelled",
            "batch_expired",
            "invalid_json_line",
        )
        and clean(value.encode(), key)
    ):
        return value
    return None


def job_code(body: object, key: str | None) -> Code | None:
    items = _object(_object(body).get("errors")).get("data")
    if not isinstance(items, list) or len(cast(list[object], items)) > 32:
        return None
    found: Code | None = None
    for item in cast(list[object], items):
        if not isinstance(item, dict):
            return None
        value = cast(dict[str, object], item).get("code")
        if value is not None:
            screened = _code(value, key)
            if screened is None or (found is not None and screened != found):
                return None
            found = screened
    return found


@dataclass
class Call:
    operation: Operation
    binding_id: str
    call_id: str = field(default_factory=lambda: uuid4().hex)
    http_status: int | None = None
    http_request_id: str | None = None
    reason: Reason | None = None
    provider_code: Code | None = None
    forwarded: bool = False
    response: object | None = None
    deferred_headers: bool = False
    bound_job_code: Code | None = None
    pending_ack: bytes | None = None

    def record(self, reason: Reason) -> OpenAIBatchDiagnosticV1:
        return OpenAIBatchDiagnosticV1(
            phase=phase_for(self.operation),
            operation=self.operation,
            binding_id=self.binding_id,
            call_id=self.call_id,
            row_position=None,
            member_key=None,
            outcome=_OUTCOMES[reason],
            reason=reason,
            http_status=self.http_status,
            http_request_id=self.http_request_id,
            member_status=None,
            member_request_id=None,
            provider_code=None if reason == "headers" else self.provider_code,
        )


def row_record(
    body: object,
    source: str,
    position: int,
    member_key: str | None,
    kind: str | None,
    key: str | None,
) -> OpenAIBatchDiagnosticV1:
    row = _object(body)
    response = _object(row.get("response"))
    reason: Reason = "row_screen"
    code = None
    if member_key is not None:
        reason = "member_response"
        if kind in ("provider_error", "confirmed_cancellation"):
            reason = (
                "member_cancelled"
                if kind == "confirmed_cancellation"
                else "member_error"
            )
            code = error_code(
                row if row.get("response") is None else response.get("body"), key
            )
    return OpenAIBatchDiagnosticV1(
        phase="result",
        operation="result_row",
        binding_id=source,
        call_id=None,
        row_position=position,
        member_key=member_key,
        outcome=_OUTCOMES[reason],
        reason=reason,
        http_status=None,
        http_request_id=None,
        member_status=status_code(response.get("status_code")) if member_key else None,
        member_request_id=request_id(response.get("request_id"), key)
        if member_key
        else None,
        provider_code=code,
    )


def _nonnegative(value: object) -> bool:
    return type(value) is int and value >= 0


def _file_id(value: object) -> bool:
    try:
        safe_id(value, "file-")
    except ValueError:
        return False
    return True


def _counts(value: object) -> bool:
    obj = _object(value)
    return set(obj) == {"total", "completed", "failed"} and all(
        _nonnegative(item) for item in obj.values()
    )


def approved_ack(
    raw: bytes, body: dict[str, object], kind: str, model: str, key: str | None
) -> bool:
    # Only called after existing route and receipt binding validation.
    optional: dict[str, Callable[[object], bool]]
    if kind == "upload":
        required = {"id", "filename", "bytes", "purpose"}
        optional = {
            "object": lambda v: v == "file",
            "status": lambda v: v in ("uploaded", "processed", "error"),
            "status_details": lambda v: v is None,
        }
        timestamps = ("created_at", "expires_at")
    else:
        required = {"id", "input_file_id", "endpoint", "status", "metadata"}
        optional = {
            "object": lambda v: v == "batch",
            "completion_window": lambda v: v == "24h",
            "output_file_id": lambda v: v is None or _file_id(v),
            "error_file_id": lambda v: v is None or _file_id(v),
            "model": lambda v: v is None or v == model,
            "errors": lambda v: v is None,
            "usage": lambda v: v is None,
            "request_counts": lambda v: v is None or _counts(v),
        }
        timestamps = (
            "created_at",
            "expires_at",
            "in_progress_at",
            "finalizing_at",
            "completed_at",
            "failed_at",
            "expired_at",
            "cancelling_at",
            "cancelled_at",
        )
    eligible = (
        required <= body.keys()
        and not body.keys() - required - optional.keys() - set(timestamps)
        and all(check(body[name]) for name, check in optional.items() if name in body)
        and all(
            body.get(name) is None or _nonnegative(body[name]) for name in timestamps
        )
    )
    if not eligible or not clean(raw, key):
        return False
    try:
        return clean(canonical_bytes(body), key)
    except ValueError:
        return False
