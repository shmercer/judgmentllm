"""Typed in-memory evidence boundary and filtering before any body retention.

Adapters must identify documented fields and supply a credential-free derivative.
The executor never searches arbitrary transport envelopes for reasoning or secrets.
These inputs are transient; only PreparedEvidence's safe records are persisted.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from pydantic import Field, model_validator

from judgmentllm.assessment_diagnostics import AssessmentComponent
from judgmentllm.evidence._base import EvidenceIdentity, EvidenceModel
from judgmentllm.evidence.order import (
    OrderEvidence,
    ProviderObservationMetadata,
    extract_observation_metadata,
)
from judgmentllm.evidence.reasoning import (
    ReasoningArtifactReference,
    ReasoningAuthorization,
    ReasoningBlockMetadata,
    ReasoningEvidence,
    authorize_reasoning,
)
from judgmentllm.evidence.usage import (
    ProviderUsage,
    ReportedUsageInput,
    UsageContext,
    extract_provider_usage,
)
from judgmentllm.output_commitments import OutputCommitmentV2
from judgmentllm.persistence.hashing import bytes_digest

if TYPE_CHECKING:
    from judgmentllm.production_transport import OutputCommitmentV4, ProductionContext
    from judgmentllm.providers.protocol import ProviderResponse
    from judgmentllm.transport import OutputCommitmentV3, TransportContext


class ProviderReasoningBlock(EvidenceModel):
    """Documented block plus transient text, excluded from dumps and repr."""

    metadata: ReasoningBlockMetadata
    text: bytes | None = Field(default=None, exclude=True, repr=False)


class ProviderObservationContext(EvidenceModel):
    """Documented safe observation metadata, independent of request identity."""

    reported_model: str | None = None
    provider_outcome: (
        Literal[
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
        | None
    ) = Field(default=None, exclude_if=lambda value: value is None)
    sdk_version: str | None = None
    remote_response_id: str | None = None
    invocation_kind: Literal["live", "managed_bulk_live", "native_batch", "unknown"] = (
        "unknown"
    )


class ProviderEvidence(EvidenceModel):
    """Optional adapter extension; absence establishes no transport-body safety.

    ``safe_response`` must exclude authentication-bearing fields and unauthorized
    reasoning using documented structured boundaries. It must remain usable by
    the adapter's ordinary parser. Supplying None fails closed on body retention.
    ``original_body_safe`` additionally permits an original-body commitment only
    when the original envelope contains no authentication-bearing material.
    """

    provenance: Literal["synthetic_fixture", "provider_documented"]
    usage: ReportedUsageInput | None = None
    observation_context: ProviderObservationContext | None = None
    assessment_component: AssessmentComponent | None = Field(
        default=None, exclude=True, repr=False
    )
    output_text: bytes | None = Field(default=None, exclude=True, repr=False)
    output_source: OrderEvidence = "unavailable"
    reasoning: tuple[ProviderReasoningBlock, ...] = ()
    safe_response: bytes | None = Field(default=None, exclude=True, repr=False)
    original_body_safe: bool = False

    @model_validator(mode="after")
    def ordered_blocks(self) -> ProviderEvidence:
        ordinals = tuple(block.metadata.block_ordinal for block in self.reasoning)
        if any(
            left >= right for left, right in zip(ordinals, ordinals[1:], strict=False)
        ):
            raise ValueError("reasoning block ordinals must increase")
        return self


@dataclass(frozen=True)
class PreparedEvidence:
    """Safe records plus explicitly permitted transient publication bytes."""

    usage: ProviderUsage
    order: ProviderObservationMetadata
    reasoning: tuple[ReasoningEvidence, ...]
    authorization: ReasoningAuthorization
    reasoning_artifacts: tuple[tuple[ReasoningArtifactReference, bytes], ...] = field(
        repr=False
    )
    retained_response: bytes | None = field(repr=False)
    original_sha256: str | None
    disposition: Literal["exact", "redacted", "withheld"]


def prepare_evidence(
    *,
    identity: EvidenceIdentity,
    context: UsageContext,
    commitment: OutputCommitmentV2 | OutputCommitmentV3 | OutputCommitmentV4 | None,
    response: ProviderResponse,
    capture: Literal["full", "minimal"],
    authorization: ReasoningAuthorization | None = None,
    transport_context: TransportContext | ProductionContext | None = None,
) -> PreparedEvidence:
    """Prepare safe facts before publishing any response or reasoning artifact.

    Authorization must come from the executor's immutable commitment, never an
    environment value or adapter response. Positive retention is restricted to
    explicitly authorized synthetic fixtures in this phase.
    """
    policy = authorization or ReasoningAuthorization(capture=capture)
    policy = ReasoningAuthorization.model_validate(policy)
    if policy.capture != capture:
        raise ValueError("evidence capture authorization conflicts")
    evidence = response.evidence
    if evidence is not None:
        # Revalidate excluded transient bytes too, rather than trusting model_copy.
        evidence = ProviderEvidence.model_validate(dict(evidence))
        if evidence.provenance == "synthetic_fixture" and context.provider != "mock":
            # A persisted evidence record never grants fixture admission. The
            # caller must supply the same reviewed private runtime authority.
            if (
                transport_context is None
                or not getattr(transport_context, "trusted_synthetic", False)
                or (
                    transport_context.scope.provider,
                    transport_context.scope.endpoint,
                    transport_context.scope.model,
                )
                != (context.provider, context.endpoint, context.requested_model)
            ):
                raise ValueError("synthetic evidence requires a synthetic provider")
    if policy.retention != "none" and (
        context.provider != "mock"
        or evidence is None
        or evidence.provenance != "synthetic_fixture"
    ):
        raise ValueError("reasoning authorization requires a synthetic fixture")
    if evidence is not None and evidence.observation_context is not None:
        metadata = evidence.observation_context
        context = UsageContext.model_validate(
            {**context.model_dump(), **metadata.model_dump()}
        )
    usage = extract_provider_usage(
        identity, context, evidence.usage if evidence is not None else None
    )
    order = (
        extract_observation_metadata(
            commitment,
            identity=identity,
            output=evidence.output_text if evidence is not None else None,
            evidence_source=evidence.output_source
            if evidence is not None
            else "unavailable",
            unavailable_reason="sdk_object_only",
        )
        if commitment is not None
        else ProviderObservationMetadata(
            **identity.model_dump(exclude={"schema_version"}),
            requested_order=None,
            order_adherence="unknown",
            order_evidence="unavailable",
            unknown_reason="not_applicable",
        )
    )
    records: list[ReasoningEvidence] = []
    artifacts: list[tuple[ReasoningArtifactReference, bytes]] = []
    for block in evidence.reasoning if evidence is not None else ():
        header = block.metadata
        retain = authorize_reasoning(policy, header.kind).retain_text
        artifact = None
        source_digest = None
        if retain and block.text:
            source_digest = bytes_digest(block.text)
            if header.source_bytes_sha256 not in (None, source_digest):
                raise ValueError("reasoning source identity conflicts")
            artifact = ReasoningArtifactReference(
                sha256=source_digest, byte_length=len(block.text)
            )
            artifacts.append((artifact, block.text))
        # No hash of unauthorized text: its provenance/safety is not established.
        records.append(
            ReasoningEvidence(
                **identity.model_dump(exclude={"schema_version"}),
                kind=header.kind,
                provider_field=header.provider_field,
                block_ordinal=header.block_ordinal,
                artifact=artifact,
                source_bytes_sha256=source_digest,
                availability="retained"
                if artifact
                else "redacted"
                if header.redacted
                else "not_retained",
            )
        )
    # Minimal capture may commit the permitted derivative's hash but the executor
    # must not publish these transient bytes into the artifact store.
    retained = evidence.safe_response if evidence is not None else None
    original_digest = (
        bytes_digest(response.raw)
        if evidence is not None and evidence.original_body_safe
        else None
    )
    disposition: Literal["exact", "redacted", "withheld"] = (
        "withheld"
        if retained is None
        else "exact"
        if original_digest is not None and retained == response.raw
        else "redacted"
    )
    unsafe_output = order.unknown_reason in (
        "unsafe_key_names",
        "malformed_output",
    ) or any(member.trait_id is None for member in order.member_key_orders)
    if unsafe_output:
        # Unrecognized property names and trait identifiers are untrusted text;
        # they can contain credentials. Do not persist or hash that envelope.
        retained = None
        original_digest = None
        disposition = "withheld"
    # An adapter cannot authorize its raw reasoning by calling a body "safe".
    # If documented blocks exist, retain only a distinct filtered derivative;
    # separately authorized text is published in the protected artifact store.
    if evidence is not None and evidence.reasoning and retained == response.raw:
        retained = None
        disposition = "withheld"
    return PreparedEvidence(
        usage=usage,
        order=order,
        reasoning=tuple(records),
        authorization=policy,
        reasoning_artifacts=tuple(artifacts),
        retained_response=retained,
        original_sha256=original_digest,
        disposition=disposition,
    )


def exact_output_substring(raw: bytes) -> bytes | None:
    """Locate the original JSON root's output value without reserializing it.

    This helper is for the documented mock envelope, not a provider field
    discovery mechanism. Duplicate envelope properties yield unavailable evidence.
    """
    try:
        text = raw.decode("utf-8")
        decoder = json.JSONDecoder()
        index = 0
        while index < len(text) and text[index].isspace():
            index += 1
        if text[index] != "{":
            return None
        index += 1
        seen: set[str] = set()
        output = None
        while True:
            while index < len(text) and text[index].isspace():
                index += 1
            if text[index] == "}":
                return output if not text[index + 1 :].strip() else None
            key, index = decoder.raw_decode(text, index)
            if not isinstance(key, str) or key in seen:
                return None
            seen.add(key)
            while index < len(text) and text[index].isspace():
                index += 1
            if text[index] != ":":
                return None
            index += 1
            while index < len(text) and text[index].isspace():
                index += 1
            start = index
            _, index = decoder.raw_decode(text, index)
            if key == "output" and text[start:index] != "null":
                output = text[start:index].encode("utf-8")
            while index < len(text) and text[index].isspace():
                index += 1
            if text[index] == ",":
                index += 1
                continue
            if text[index] != "}":
                return None
            return output if not text[index + 1 :].strip() else None
    except (ValueError, UnicodeError, IndexError, RecursionError):
        return None
