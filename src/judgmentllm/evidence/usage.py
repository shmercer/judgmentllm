"""Reported usage per physical submission; pure extraction, never aggregation.

Mappings are deliberately limited to pinned, documented source semantics. The
input models represent supplied synthetic observations, not SDK envelopes. No
extractor derives totals, charges, token counts from text, or model support.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from judgmentllm.evidence._base import EvidenceIdentity, EvidenceModel
from judgmentllm.models import ID

TokenCount = Annotated[int, Field(ge=0)]
UsageSource = Literal[
    "unknown",
    "openai.chat.usage",
    "anthropic.messages.usage",
    "synthetic.reported_usage",
]
UsageCounter = Literal[
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "reasoning_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
]
UsageField = Literal[
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "prompt_tokens_details.cached_tokens",
    "completion_tokens_details.reasoning_tokens",
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "cache_creation.ephemeral_5m_input_tokens",
    "cache_creation.ephemeral_1h_input_tokens",
]
UsageRelationship = Literal["reported", "included_in", "excluded_from", "unknown"]
Currency = Literal["USD", "EUR", "GBP", "CAD", "JPY"]
Amount = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]


class _UsageSemantics(EvidenceModel):
    semantics_version: Literal[1] = 1

    @field_validator("semantics_version", mode="before")
    @classmethod
    def strict_semantics_version(cls, value: object) -> object:
        if type(value) is not int or value != 1:
            raise ValueError("unsupported usage semantics version")
        return value


class UsageBreakdown(_UsageSemantics):
    """One original reported field with its documented overlap relationship."""

    field: UsageField
    value: TokenCount
    unit: Literal["tokens"] = "tokens"
    relationship: UsageRelationship
    related_counter: UsageCounter | None = None
    usage_source: UsageSource
    semantics_version: Literal[1] = 1

    @model_validator(mode="after")
    def consistent(self) -> UsageBreakdown:
        expected = _SEMANTICS.get((self.usage_source, self.field))
        if expected is None or expected != (self.relationship, self.related_counter):
            raise ValueError("usage breakdown semantics are not established")
        return self


# Original fields and overlap relationships; not a provider capability registry.
_SEMANTICS: dict[
    tuple[UsageSource, UsageField], tuple[UsageRelationship, UsageCounter | None]
] = {
    ("openai.chat.usage", "prompt_tokens"): ("reported", None),
    ("openai.chat.usage", "completion_tokens"): ("reported", None),
    ("openai.chat.usage", "total_tokens"): ("reported", None),
    ("openai.chat.usage", "prompt_tokens_details.cached_tokens"): (
        "included_in",
        "input_tokens",
    ),
    ("openai.chat.usage", "completion_tokens_details.reasoning_tokens"): (
        "included_in",
        "output_tokens",
    ),
    ("anthropic.messages.usage", "input_tokens"): ("reported", None),
    ("anthropic.messages.usage", "output_tokens"): ("reported", None),
    ("anthropic.messages.usage", "cache_read_input_tokens"): (
        "excluded_from",
        "input_tokens",
    ),
    ("anthropic.messages.usage", "cache_creation_input_tokens"): (
        "excluded_from",
        "input_tokens",
    ),
    ("anthropic.messages.usage", "cache_creation.ephemeral_5m_input_tokens"): (
        "included_in",
        "cache_write_tokens",
    ),
    ("anthropic.messages.usage", "cache_creation.ephemeral_1h_input_tokens"): (
        "included_in",
        "cache_write_tokens",
    ),
}


class ReportedCost(_UsageSemantics):
    """Reliable amount explicitly supplied by a documented provider field.

    Only synthetic cost semantics are established in 4A. Real provider cost
    mappings require documented adapter-specific extensions in later phases.
    """

    amount: Amount
    currency: Currency
    source: Literal["synthetic.reported_cost"]
    semantics_version: Literal[1] = 1


class EstimatedCost(EvidenceModel):
    """Caller-supplied estimate, never extracted or labeled provider reported."""

    amount: Amount
    currency: Currency
    source: Literal["caller_estimate"] = "caller_estimate"
    pricing_date: date
    pricing_source: Literal["synthetic.price_fixture", "caller_supplied_rates"]
    assumptions: tuple[
        Literal[
            "caller_supplied_rates",
            "excludes_taxes",
            "excludes_discounts",
            "token_overlap_not_resolved",
            "fixed_submission_estimate",
        ],
        ...,
    ]

    @model_validator(mode="after")
    def documented(self) -> EstimatedCost:
        if not self.assumptions or len(set(self.assumptions)) != len(self.assumptions):
            raise ValueError("estimated cost requires distinct assumptions")
        return self


class UsageContext(EvidenceModel):
    """Explicit invocation metadata supplied independently of assessment traits."""

    provider: Literal[
        "mock",
        "openai",
        "anthropic",
        "gemini",
        "vertex",
        "together",
        "ollama",
        "typesafe",
    ]
    endpoint: ID
    requested_model: ID
    reported_model: ID | None = None
    sdk_version: ID | None = None
    observation_kind: Literal[
        "response", "refusal", "provider_error", "confirmed_cancellation", "invocation"
    ]
    invocation_kind: Literal["live", "managed_bulk_live", "native_batch", "unknown"] = (
        "unknown"
    )
    remote_response_id: ID | None = None
    definitive_failure_id: ID | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_seconds: Annotated[float, Field(ge=0)] | None = None
    clock_source: Literal[
        "unavailable", "provider", "client_wall", "client_monotonic"
    ] = "unavailable"

    @model_validator(mode="after")
    def timing_and_failure(self) -> UsageContext:
        timestamps = (self.started_at, self.completed_at)
        if any(value is not None and value.utcoffset() is None for value in timestamps):
            raise ValueError("usage timestamps require an explicit timezone")
        if self.clock_source == "unavailable" and any(
            value is not None for value in (*timestamps, self.duration_seconds)
        ):
            raise ValueError("usage timing requires a documented clock source")
        if self.clock_source == "client_monotonic" and any(
            value is not None for value in timestamps
        ):
            raise ValueError("monotonic clock supports duration only")
        if (
            self.started_at is not None
            and self.completed_at is not None
            and self.completed_at < self.started_at
        ):
            raise ValueError("usage timestamps are out of order")
        if self.definitive_failure_id is not None and self.observation_kind not in (
            "provider_error",
            "confirmed_cancellation",
            "invocation",
        ):
            raise ValueError(
                "definitive failure reference requires a failure invocation"
            )
        return self


class ReportedUsageInput(_UsageSemantics):
    """Explicit already-interpreted counters; unknown source means unknown semantics."""

    usage_source: UsageSource = "unknown"
    semantics_version: Literal[1] = 1
    input_tokens: TokenCount | None = None
    output_tokens: TokenCount | None = None
    total_tokens: TokenCount | None = None
    reasoning_tokens: TokenCount | None = None
    cache_read_tokens: TokenCount | None = None
    cache_write_tokens: TokenCount | None = None
    breakdown: tuple[UsageBreakdown, ...] = ()
    reported_cost: ReportedCost | None = None

    @model_validator(mode="after")
    def source_consistency(self) -> ReportedUsageInput:
        fields = tuple(row.field for row in self.breakdown)
        if len(fields) != len(set(fields)) or any(
            row.usage_source != self.usage_source for row in self.breakdown
        ):
            raise ValueError(
                "usage breakdown sources must match without duplicate fields"
            )
        if (
            self.reported_cost is not None
            and self.usage_source != "synthetic.reported_usage"
        ):
            raise ValueError(
                "reported cost semantics are not established for this source"
            )
        if self.usage_source == "openai.chat.usage":
            if self.cache_write_tokens is not None:
                raise ValueError("4A mapping does not establish cache write tokens")
            for component, parent in (
                (self.reasoning_tokens, self.output_tokens),
                (self.cache_read_tokens, self.input_tokens),
            ):
                if component is not None and parent is not None and component > parent:
                    raise ValueError("usage component exceeds its reported parent")
        if self.usage_source == "anthropic.messages.usage" and (
            self.reasoning_tokens is not None or self.total_tokens is not None
        ):
            raise ValueError("4A mapping does not establish these counters")
        targets: dict[UsageField, UsageCounter] = {
            "prompt_tokens": "input_tokens",
            "completion_tokens": "output_tokens",
            "total_tokens": "total_tokens",
            "input_tokens": "input_tokens",
            "output_tokens": "output_tokens",
            "prompt_tokens_details.cached_tokens": "cache_read_tokens",
            "completion_tokens_details.reasoning_tokens": "reasoning_tokens",
            "cache_read_input_tokens": "cache_read_tokens",
            "cache_creation_input_tokens": "cache_write_tokens",
        }
        for row in self.breakdown:
            target = targets.get(row.field)
            if target is not None and getattr(self, target) != row.value:
                raise ValueError("usage breakdown conflicts with normalized counter")
            if row.relationship == "included_in" and row.related_counter is not None:
                parent = getattr(self, row.related_counter)
                if parent is not None and row.value > parent:
                    raise ValueError("usage component exceeds its reported parent")
        if self.usage_source == "anthropic.messages.usage":
            subdivisions = {
                row.field: row.value
                for row in self.breakdown
                if row.field
                in (
                    "cache_creation.ephemeral_5m_input_tokens",
                    "cache_creation.ephemeral_1h_input_tokens",
                )
            }
            if (
                len(subdivisions) == 2
                and self.cache_write_tokens is not None
                and sum(subdivisions.values()) > self.cache_write_tokens
            ):
                raise ValueError("usage subdivisions exceed their reported parent")
        if self.usage_source == "unknown" and self.breakdown:
            raise ValueError(
                "unknown usage source cannot establish breakdown semantics"
            )
        return self


class ProviderUsage(EvidenceIdentity, UsageContext, ReportedUsageInput):
    """One usage fact for one physical submission, never one grouped trait.

    Repeated extraction of the same observation returns the same facts. Storage,
    deduplication, revision selection and aggregation are Phase 4B boundaries.
    """

    estimated_cost: EstimatedCost | None = None

    @model_validator(mode="after")
    def provider_source(self) -> ProviderUsage:
        if self.reported_cost is not None and self.provider != "mock":
            raise ValueError("synthetic reported cost requires a synthetic provider")
        if self.usage_source == "openai.chat.usage" and (
            self.provider,
            self.endpoint,
        ) != ("openai", "chat.completions"):
            raise ValueError("usage source does not match provider endpoint")
        if self.usage_source == "anthropic.messages.usage" and (
            self.provider,
            self.endpoint,
        ) != ("anthropic", "messages"):
            raise ValueError("usage source does not match provider endpoint")
        return self


class OpenAIInputTokenDetails(EvidenceModel):
    cached_tokens: TokenCount | None = None


class OpenAIOutputTokenDetails(EvidenceModel):
    reasoning_tokens: TokenCount | None = None


class OpenAIChatUsageInput(EvidenceModel):
    """Allowlisted Chat usage fields; detail counts are components, not additions."""

    prompt_tokens: TokenCount | None = None
    completion_tokens: TokenCount | None = None
    total_tokens: TokenCount | None = None
    prompt_tokens_details: OpenAIInputTokenDetails | None = None
    completion_tokens_details: OpenAIOutputTokenDetails | None = None


class AnthropicCacheCreationInput(EvidenceModel):
    ephemeral_5m_input_tokens: TokenCount | None = None
    ephemeral_1h_input_tokens: TokenCount | None = None


class AnthropicMessagesUsageInput(EvidenceModel):
    """Input excludes cache reads/writes; nested creation counts subdivide writes."""

    input_tokens: TokenCount | None = None
    output_tokens: TokenCount | None = None
    cache_read_input_tokens: TokenCount | None = None
    cache_creation_input_tokens: TokenCount | None = None
    cache_creation: AnthropicCacheCreationInput | None = None


def extract_provider_usage(
    identity: EvidenceIdentity,
    context: UsageContext,
    reported: ReportedUsageInput | None,
    *,
    estimated_cost: EstimatedCost | None = None,
) -> ProviderUsage:
    """Copy explicitly supplied facts; absence is unknown, including on failures."""
    return ProviderUsage.model_validate(
        {
            **identity.model_dump(),
            **context.model_dump(),
            **(reported or ReportedUsageInput()).model_dump(),
            "estimated_cost": estimated_cost,
        }
    )


def _breakdown(
    source: UsageSource, values: tuple[tuple[UsageField, int | None], ...]
) -> tuple[UsageBreakdown, ...]:
    return tuple(
        UsageBreakdown(
            field=field,
            value=value,
            usage_source=source,
            relationship=_SEMANTICS[(source, field)][0],
            related_counter=_SEMANTICS[(source, field)][1],
        )
        for field, value in values
        if value is not None
    )


def extract_openai_chat_usage(
    identity: EvidenceIdentity,
    context: UsageContext,
    usage: OpenAIChatUsageInput | None,
) -> ProviderUsage:
    """Interpret supplied Chat usage, retaining reasoning/cache overlap with totals."""
    if (context.provider, context.endpoint) != ("openai", "chat.completions"):
        raise ValueError("usage source does not match provider endpoint")
    if usage is None:
        return extract_provider_usage(identity, context, None)
    cached = (
        usage.prompt_tokens_details.cached_tokens
        if usage.prompt_tokens_details
        else None
    )
    reasoning = (
        usage.completion_tokens_details.reasoning_tokens
        if usage.completion_tokens_details
        else None
    )
    reported = ReportedUsageInput(
        usage_source="openai.chat.usage",
        input_tokens=usage.prompt_tokens,
        output_tokens=usage.completion_tokens,
        total_tokens=usage.total_tokens,
        reasoning_tokens=reasoning,
        cache_read_tokens=cached,
        breakdown=_breakdown(
            "openai.chat.usage",
            (
                ("prompt_tokens", usage.prompt_tokens),
                ("completion_tokens", usage.completion_tokens),
                ("total_tokens", usage.total_tokens),
                ("prompt_tokens_details.cached_tokens", cached),
                ("completion_tokens_details.reasoning_tokens", reasoning),
            ),
        ),
    )
    return extract_provider_usage(identity, context, reported)


def extract_anthropic_messages_usage(
    identity: EvidenceIdentity,
    context: UsageContext,
    usage: AnthropicMessagesUsageInput | None,
) -> ProviderUsage:
    """Interpret supplied Messages usage without adding cache classes/subdivisions."""
    if (context.provider, context.endpoint) != ("anthropic", "messages"):
        raise ValueError("usage source does not match provider endpoint")
    if usage is None:
        return extract_provider_usage(identity, context, None)
    short = (
        usage.cache_creation.ephemeral_5m_input_tokens if usage.cache_creation else None
    )
    long = (
        usage.cache_creation.ephemeral_1h_input_tokens if usage.cache_creation else None
    )
    reported = ReportedUsageInput(
        usage_source="anthropic.messages.usage",
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_input_tokens,
        cache_write_tokens=usage.cache_creation_input_tokens,
        breakdown=_breakdown(
            "anthropic.messages.usage",
            (
                ("input_tokens", usage.input_tokens),
                ("output_tokens", usage.output_tokens),
                ("cache_read_input_tokens", usage.cache_read_input_tokens),
                ("cache_creation_input_tokens", usage.cache_creation_input_tokens),
                ("cache_creation.ephemeral_5m_input_tokens", short),
                ("cache_creation.ephemeral_1h_input_tokens", long),
            ),
        ),
    )
    return extract_provider_usage(identity, context, reported)
