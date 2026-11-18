"""Content-free findings and an explicitly derived, bounded replay representation."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, JsonValue, field_validator, model_validator

from judgmentllm.decisions._base import DecisionsModel
from judgmentllm.evidence._base import EvidenceIdentity
from judgmentllm.persistence.models import SHA256


class ReplayMemberV1(DecisionsModel):
    key: str
    answer_ordinals: tuple[int, ...]
    answer_type: Literal["score", "choice", "refusal", "invalid", "unavailable"]
    failure: (
        Literal[
            "missing_output", "duplicate_output", "invalid_output", "provider_refusal"
        ]
        | None
    )


class DecisionsReplayMetadataV1(DecisionsModel):
    replay_policy_version: Literal[1] = 1
    identity: EvidenceIdentity
    correlation_id: str
    payload_sha256: SHA256
    commitment_sha256: SHA256
    original_sha256: SHA256
    original_byte_length: int = Field(ge=0, le=8_388_608)
    recognizable: bool
    reported_model: str | None
    findings: tuple[
        Literal[
            "malformed_json",
            "unrecognized_envelope",
            "unknown_field",
            "model_conflict",
            "answer_order",
        ],
        ...,
    ]
    unexpected_members: int = Field(ge=0)
    unattributable_members: int = Field(ge=0)
    members: tuple[ReplayMemberV1, ...]

    @field_validator("replay_policy_version", mode="before")
    @classmethod
    def version(cls, value: object) -> object:
        if type(value) is not int or value != 1:
            raise ValueError("unsupported replay policy")
        return value


class OpenAIDecisionsReplayV1(DecisionsModel):
    replay_version: Literal[1] = 1
    metadata: DecisionsReplayMetadataV1
    answers: tuple[dict[str, JsonValue] | None, ...] = Field(repr=False)

    @field_validator("replay_version", mode="before")
    @classmethod
    def version(cls, value: object) -> object:
        if type(value) is not int or value != 1:
            raise ValueError("unsupported replay representation")
        return value

    @model_validator(mode="after")
    def membership(self) -> OpenAIDecisionsReplayV1:
        if len(self.answers) != len(self.metadata.members) or any(
            (answer is None) != (member.failure is not None)
            for member, answer in zip(self.metadata.members, self.answers, strict=True)
        ):
            raise ValueError("replay membership differs from validation findings")
        return self


class TypeSafeDecisionsReplayV1(OpenAIDecisionsReplayV1):
    protocol: Literal["typesafe-systemone-production-v1"] = (
        "typesafe-systemone-production-v1"
    )
