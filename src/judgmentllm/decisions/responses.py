"""Transient provider-neutral named-answer envelope, bound to one invocation."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, JsonValue

from judgmentllm.decisions._base import DecisionsModel
from judgmentllm.decisions.replay import DecisionsReplayMetadataV1
from judgmentllm.persistence.models import SHA256
from judgmentllm.transport import PhysicalRequestIdentityV1


class ParsedDecisionsResponseV1(DecisionsModel):
    decisions_response_version: Literal[1] = 1
    identity: PhysicalRequestIdentityV1
    payload_sha256: SHA256
    response_sha256: SHA256
    kind: Literal["response", "refusal", "provider_error", "confirmed_cancellation"]
    recognizable: bool
    reported_model: str | None
    answer_order: Literal["map", "ordered_array"]
    answers: tuple[JsonValue, ...] = Field(exclude=True, repr=False)


class ParsedDecisionsResponseV2(ParsedDecisionsResponseV1):
    decisions_response_version: Literal[2] = 2  # pyright: ignore[reportIncompatibleVariableOverride]
    metadata: DecisionsReplayMetadataV1
