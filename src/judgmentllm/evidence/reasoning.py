"""Observation-level reasoning metadata and pure retention authorization.

No text is accepted or retained here. A later adapter supplies documented block
headers; Phase 4B will apply these decisions before retaining response bytes.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Annotated, Literal

from pydantic import Field, model_validator

from judgmentllm.persistence.models import SHA256

from ._base import EvidenceIdentity, EvidenceModel

ReasoningKind = Literal["summary", "thinking_block", "other_documented"]
ReasoningAvailability = Literal[
    "retained", "not_exposed", "not_returned", "not_retained", "redacted"
]
ReasoningField = Literal[
    "openai.responses.reasoning.summary",
    "anthropic.messages.thinking",
    "together.chat.reasoning",
    "together.chat.reasoning_content",
    "ollama.chat.message.thinking",
    "gemini.generate_content.thought",
    "vertex.generate_content.thought",
]
_FIELD_KIND: dict[ReasoningField, ReasoningKind] = {
    "openai.responses.reasoning.summary": "summary",
    "anthropic.messages.thinking": "thinking_block",
    "together.chat.reasoning": "thinking_block",
    "together.chat.reasoning_content": "thinking_block",
    "ollama.chat.message.thinking": "thinking_block",
    "gemini.generate_content.thought": "other_documented",
    "vertex.generate_content.thought": "other_documented",
}
Ordinal = Annotated[int, Field(ge=0)]


class ReasoningArtifactReference(EvidenceModel):
    """Content-addressed reference; no filesystem or credential path."""

    sha256: SHA256
    byte_length: Annotated[int, Field(ge=1)]


class ReasoningEvidence(EvidenceIdentity):
    """Safe block metadata; assessment explanations remain in task results.

    `retained` describes an actual artifact supplied by a future retention layer,
    never a promise made by the authorization decision below.
    """

    kind: ReasoningKind
    provider_field: ReasoningField
    block_ordinal: Ordinal
    artifact: ReasoningArtifactReference | None = None
    source_bytes_sha256: SHA256 | None = None
    availability: ReasoningAvailability

    @model_validator(mode="after")
    def consistent(self) -> ReasoningEvidence:
        if self.observation_id is None:
            raise ValueError("reasoning requires an observation")
        if _FIELD_KIND[self.provider_field] != self.kind:
            raise ValueError("unsupported reasoning kind")
        if (self.availability == "retained") != (self.artifact is not None):
            raise ValueError("reasoning artifact availability differs")
        return self


class ReasoningAuthorization(EvidenceModel):
    """Explicit independent request and retention policy; not RunSpec configuration.

    `effort_requested` records a request for computation, with no retention grant.
    `summary_requested` records an exposure request, with no retention grant.
    """

    capture: Literal["full", "minimal"]
    effort_requested: bool = False
    summary_requested: bool = False
    retention: Literal["none", "summary", "provider_text"] = "none"

    @model_validator(mode="after")
    def compatible(self) -> ReasoningAuthorization:
        if self.capture == "minimal" and self.retention != "none":
            raise ValueError("minimal capture cannot authorize reasoning retention")
        return self


class ReasoningRetentionDecision(EvidenceModel):
    retain_text: bool
    request_summary: bool
    diagnostic: Literal[
        "authorized", "not_authorized", "kind_not_authorized", "minimal_capture"
    ]

    @model_validator(mode="after")
    def consistent(self) -> ReasoningRetentionDecision:
        if self.retain_text != (self.diagnostic == "authorized"):
            raise ValueError("reasoning decision differs from diagnostic")
        return self


def authorize_reasoning(
    policy: ReasoningAuthorization, kind: ReasoningKind
) -> ReasoningRetentionDecision:
    """Decide permission only; no artifact, generation control or success outcome."""
    policy = ReasoningAuthorization.model_validate(policy)
    if kind not in ("summary", "thinking_block", "other_documented"):
        raise ValueError("unsupported reasoning kind")
    retain = policy.capture == "full" and (
        (policy.retention == "summary" and kind == "summary")
        or (policy.retention == "provider_text" and kind == "thinking_block")
    )
    diagnostic = (
        "authorized"
        if retain
        else "minimal_capture"
        if policy.capture == "minimal"
        else "not_authorized"
        if policy.retention == "none"
        else "kind_not_authorized"
    )
    return ReasoningRetentionDecision(
        retain_text=retain,
        request_summary=policy.summary_requested,
        diagnostic=diagnostic,
    )


class ReasoningBlockMetadata(EvidenceModel):
    """Adapter-supplied documented block header; deliberately no text field.

    Only original source-byte hashes established by the caller may be supplied.
    Opaque signatures are outside this field allowlist.
    """

    kind: ReasoningKind
    provider_field: ReasoningField
    block_ordinal: Ordinal
    source_bytes_sha256: SHA256 | None = None
    redacted: bool = False

    @model_validator(mode="after")
    def documented(self) -> ReasoningBlockMetadata:
        if _FIELD_KIND[self.provider_field] != self.kind:
            raise ValueError("unsupported reasoning kind")
        return self


def extract_reasoning_metadata(
    identity: EvidenceIdentity,
    blocks: Iterable[ReasoningBlockMetadata],
    *,
    authorization: ReasoningAuthorization,
) -> Iterator[ReasoningEvidence]:
    """Stream safe metadata in strictly increasing ordinal order, without I/O.

    Even authorized blocks are `not_retained` here: Phase 4A creates no artifacts.
    Missing blocks produce no fabricated empty block. Callers may explicitly
    describe `not_exposed` / `not_returned` using ReasoningEvidence if established.
    """
    identity = EvidenceIdentity.model_validate(identity)
    authorization = ReasoningAuthorization.model_validate(authorization)
    if identity.observation_id is None:
        raise ValueError("reasoning requires an observation")
    previous = -1
    for block in blocks:
        block = ReasoningBlockMetadata.model_validate(block)
        if block.block_ordinal <= previous:
            raise ValueError("reasoning block ordinals must increase")
        previous = block.block_ordinal
        authorize_reasoning(authorization, block.kind)
        yield ReasoningEvidence(
            **identity.model_dump(exclude={"schema_version"}),
            kind=block.kind,
            provider_field=block.provider_field,
            block_ordinal=block.block_ordinal,
            source_bytes_sha256=block.source_bytes_sha256,
            availability="redacted" if block.redacted else "not_retained",
        )
