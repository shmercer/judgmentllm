"""Detached evidence v1 contracts and deterministic offline extraction.

No runtime capture, persistence, provider client or capability registration is
activated by this package. IDs and invocation context come from trusted callers;
provider output enters only through the documented allowlisted extraction APIs.
"""

from ._base import EvidenceIdentity
from .order import (
    ObservedMemberOrder,
    OrderAdherence,
    OrderEvidence,
    OrderUnknownReason,
    ProviderObservationMetadata,
    extract_observation_metadata,
)
from .reasoning import (
    ReasoningArtifactReference,
    ReasoningAuthorization,
    ReasoningAvailability,
    ReasoningBlockMetadata,
    ReasoningEvidence,
    ReasoningField,
    ReasoningKind,
    ReasoningRetentionDecision,
    authorize_reasoning,
    extract_reasoning_metadata,
)
from .scope import (
    EvidenceFeature,
    EvidenceProvenance,
    EvidenceStage,
    ScopedProviderEvidence,
)
from .usage import (
    AnthropicCacheCreationInput,
    AnthropicMessagesUsageInput,
    EstimatedCost,
    OpenAIChatUsageInput,
    OpenAIInputTokenDetails,
    OpenAIOutputTokenDetails,
    ProviderUsage,
    ReportedCost,
    ReportedUsageInput,
    UsageBreakdown,
    UsageContext,
    extract_anthropic_messages_usage,
    extract_openai_chat_usage,
    extract_provider_usage,
)

__all__ = [
    "EvidenceIdentity",
    "ObservedMemberOrder",
    "OrderAdherence",
    "OrderEvidence",
    "OrderUnknownReason",
    "ProviderObservationMetadata",
    "extract_observation_metadata",
    "ReasoningArtifactReference",
    "ReasoningAuthorization",
    "ReasoningAvailability",
    "ReasoningBlockMetadata",
    "ReasoningEvidence",
    "ReasoningField",
    "ReasoningKind",
    "ReasoningRetentionDecision",
    "authorize_reasoning",
    "extract_reasoning_metadata",
    "EvidenceFeature",
    "EvidenceProvenance",
    "EvidenceStage",
    "ScopedProviderEvidence",
    "AnthropicCacheCreationInput",
    "AnthropicMessagesUsageInput",
    "EstimatedCost",
    "OpenAIChatUsageInput",
    "OpenAIInputTokenDetails",
    "OpenAIOutputTokenDetails",
    "ProviderUsage",
    "ReportedCost",
    "ReportedUsageInput",
    "UsageBreakdown",
    "UsageContext",
    "extract_anthropic_messages_usage",
    "extract_openai_chat_usage",
    "extract_provider_usage",
]
