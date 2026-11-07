"""Deterministic submission usage; all inputs synthetic and credential free."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from pydantic import TypeAdapter, ValidationError

from judgmentllm.evidence._base import EvidenceIdentity
from judgmentllm.evidence.usage import (
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


def identity(**changes: Any) -> EvidenceIdentity:
    return EvidenceIdentity.model_validate(
        {
            "run_id": "001",
            "request_uid": "002",
            "attempt_id": "003",
            "submission_id": "004",
            "observation_id": "005",
            **changes,
        }
    )


def context(**changes: Any) -> UsageContext:
    return UsageContext.model_validate(
        {
            "provider": "openai",
            "endpoint": "chat.completions",
            "requested_model": "synthetic-model",
            "observation_kind": "response",
            **changes,
        }
    )


def test_complete_reported_usage_preserves_inclusion() -> None:
    record = extract_openai_chat_usage(
        identity(),
        context(),
        OpenAIChatUsageInput(
            prompt_tokens=40,
            completion_tokens=20,
            total_tokens=60,
            prompt_tokens_details=OpenAIInputTokenDetails(cached_tokens=15),
            completion_tokens_details=OpenAIOutputTokenDetails(reasoning_tokens=10),
        ),
    )
    assert (record.input_tokens, record.output_tokens, record.total_tokens) == (
        40,
        20,
        60,
    )
    assert (
        record.reasoning_tokens,
        record.cache_read_tokens,
        record.cache_write_tokens,
    ) == (10, 15, None)
    assert record.breakdown[-1].relationship == "included_in"
    assert record.breakdown[-1].related_counter == "output_tokens"
    assert record.breakdown[-2].related_counter == "input_tokens"
    assert record.usage_source == "openai.chat.usage"
    assert record.reported_cost is None
    assert ProviderUsage.model_validate_json(record.model_dump_json()) == record


@pytest.mark.parametrize(
    "supplied,expected",
    [
        ({"prompt_tokens": 4, "completion_tokens": 2}, (4, 2, None)),
        ({"total_tokens": 7}, (None, None, 7)),
        ({"prompt_tokens": 4}, (4, None, None)),
        ({}, (None, None, None)),
        ({"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}, (0, 0, 0)),
        ({"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 10}, (4, 2, 10)),
    ],
)
def test_partial_usage_never_derives_total(
    supplied: dict[str, int], expected: tuple[int | None, ...]
) -> None:
    record = extract_openai_chat_usage(
        identity(), context(), OpenAIChatUsageInput.model_validate(supplied)
    )
    assert (record.input_tokens, record.output_tokens, record.total_tokens) == expected


@pytest.mark.parametrize(
    "extractor,ctx",
    [
        (extract_openai_chat_usage, context()),
        (
            extract_anthropic_messages_usage,
            context(provider="anthropic", endpoint="messages"),
        ),
    ],
)
def test_no_report_is_unknown(extractor: Any, ctx: UsageContext) -> None:
    record = extractor(identity(), ctx, None)
    assert record.usage_source == "unknown"
    assert record.breakdown == ()
    assert all(
        getattr(record, field) is None
        for field in (
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "reasoning_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
        )
    )


def test_nested_anthropic_cache_counts_not_added() -> None:
    record = extract_anthropic_messages_usage(
        identity(),
        context(provider="anthropic", endpoint="messages"),
        AnthropicMessagesUsageInput(
            input_tokens=10,
            output_tokens=5,
            cache_read_input_tokens=100,
            cache_creation_input_tokens=80,
            cache_creation=AnthropicCacheCreationInput(
                ephemeral_5m_input_tokens=30, ephemeral_1h_input_tokens=50
            ),
        ),
    )
    assert record.input_tokens == 10
    assert record.cache_read_tokens == 100
    assert record.cache_write_tokens == 80
    assert record.total_tokens is None
    assert record.reasoning_tokens is None
    assert record.breakdown[2].relationship == "excluded_from"
    assert record.breakdown[2].related_counter == "input_tokens"
    assert [row.value for row in record.breakdown[-2:]] == [30, 50]
    assert all(
        row.related_counter == "cache_write_tokens" for row in record.breakdown[-2:]
    )


def test_nested_counts_without_parent_do_not_invent_aggregate() -> None:
    record = extract_anthropic_messages_usage(
        identity(),
        context(provider="anthropic", endpoint="messages"),
        AnthropicMessagesUsageInput(
            cache_creation=AnthropicCacheCreationInput(ephemeral_5m_input_tokens=0),
        ),
    )
    assert record.cache_write_tokens is None
    assert record.breakdown[0].value == 0


@pytest.mark.parametrize(
    "field",
    [
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "reasoning_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
    ],
)
@pytest.mark.parametrize(
    "bad", [True, False, -1, 1.0, 1.5, float("nan"), float("inf"), "1"]
)
def test_strict_token_counts(field: str, bad: Any) -> None:
    with pytest.raises(ValidationError):
        ReportedUsageInput.model_validate({field: bad})


@pytest.mark.parametrize("version", [True, 1.0, "1", 0, 2])
@pytest.mark.parametrize("field", ["schema_version", "semantics_version"])
def test_strict_version(field: str, version: Any) -> None:
    with pytest.raises(ValidationError):
        ReportedUsageInput.model_validate({field: version})
    with pytest.raises(ValidationError):
        ReportedUsageInput.model_validate_json(json.dumps({field: version}))


def test_frozen_unknown_fields_and_unknown_semantics() -> None:
    record = extract_provider_usage(identity(), context(), None)
    with pytest.raises(ValidationError):
        record.input_tokens = 3
    for changes in ({"unexpected": 3}, {"usage_source": "unestablished.usage"}):
        with pytest.raises(ValidationError):
            ProviderUsage.model_validate({**record.model_dump(), **changes})
    with pytest.raises(ValidationError):
        UsageBreakdown.model_validate(
            {
                "field": "arbitrary",
                "value": 3,
                "relationship": "reported",
                "usage_source": "unknown",
            }
        )
    with pytest.raises(ValidationError):
        UsageBreakdown(
            field="prompt_tokens",
            value=3,
            relationship="unknown",
            usage_source="openai.chat.usage",
        )


def test_usage_source_must_match_provider_endpoint() -> None:
    for supplied_context in (
        context(provider="anthropic"),
        context(endpoint="responses"),
    ):
        with pytest.raises(ValueError, match="usage source does not match"):
            extract_openai_chat_usage(identity(), supplied_context, None)
    with pytest.raises(ValueError, match="usage source does not match"):
        extract_anthropic_messages_usage(identity(), context(), None)
    with pytest.raises(ValidationError):
        extract_provider_usage(
            identity(),
            context(),
            ReportedUsageInput(usage_source="anthropic.messages.usage"),
        )


def test_usage_breakdown_cross_field_consistency() -> None:
    row = UsageBreakdown(
        field="prompt_tokens",
        value=3,
        relationship="reported",
        usage_source="openai.chat.usage",
    )
    with pytest.raises(ValidationError):
        ReportedUsageInput(
            usage_source="openai.chat.usage", input_tokens=4, breakdown=(row,)
        )
    with pytest.raises(ValidationError):
        ReportedUsageInput(
            usage_source="openai.chat.usage", input_tokens=3, breakdown=(row, row)
        )
    with pytest.raises(ValidationError):
        ReportedUsageInput(
            usage_source="anthropic.messages.usage", input_tokens=3, breakdown=(row,)
        )
    with pytest.raises(ValidationError):
        extract_openai_chat_usage(
            identity(),
            context(),
            OpenAIChatUsageInput(
                completion_tokens=2,
                completion_tokens_details=OpenAIOutputTokenDetails(reasoning_tokens=3),
            ),
        )


def test_reported_and_estimated_cost_are_distinct() -> None:
    reported = ReportedCost(
        amount=Decimal("0.002"), currency="USD", source="synthetic.reported_cost"
    )
    estimate = EstimatedCost(
        amount=Decimal("0.003"),
        currency="CAD",
        pricing_date=date(2026, 11, 1),
        pricing_source="synthetic.price_fixture",
        assumptions=("fixed_submission_estimate",),
    )
    record = extract_provider_usage(
        identity(),
        context(provider="mock", endpoint="synthetic"),
        ReportedUsageInput(
            usage_source="synthetic.reported_usage",
            reported_cost=reported,
        ),
        estimated_cost=estimate,
    )
    assert record.reported_cost == reported
    assert record.estimated_cost == estimate
    assert record.total_tokens is None
    assert ProviderUsage.model_validate_json(record.model_dump_json()) == record
    with pytest.raises(ValidationError):
        ReportedUsageInput(usage_source="openai.chat.usage", reported_cost=reported)
    for bad in (True, -1, Decimal("NaN"), Decimal("Infinity")):
        with pytest.raises(ValidationError):
            ReportedCost.model_validate(
                {"amount": bad, "currency": "USD", "source": "synthetic.reported_cost"}
            )
    with pytest.raises(ValidationError):
        EstimatedCost(
            amount=Decimal("0"),
            currency="USD",
            pricing_date=date(2026, 11, 1),
            pricing_source="caller_supplied_rates",
            assumptions=(),
        )


def test_failure_and_repeated_observation_identity() -> None:
    ctx = context(observation_kind="provider_error", definitive_failure_id="failure-1")
    report = OpenAIChatUsageInput(prompt_tokens=3)
    failed = extract_openai_chat_usage(identity(observation_id=None), ctx, report)
    assert failed.observation_id is None
    assert failed.definitive_failure_id == "failure-1"
    assert failed.input_tokens == 3
    first = extract_openai_chat_usage(identity(), context(), report)
    repeated = extract_openai_chat_usage(identity(), context(), report)
    retry = extract_openai_chat_usage(
        identity(
            attempt_id="attempt-2",
            submission_id="submission-2",
            observation_id="observation-2",
        ),
        context(),
        report,
    )
    assert first == repeated
    assert retry != first
    assert retry.input_tokens == first.input_tokens == 3
    assert "trait_id" not in first.model_dump()
    with pytest.raises(ValidationError):
        context(definitive_failure_id="failure-1")


def test_timing_is_explicit_and_never_invented() -> None:
    start = datetime(2026, 11, 1, tzinfo=UTC)
    end = datetime(2026, 11, 2, tzinfo=UTC)
    ctx = context(started_at=start, completed_at=end, clock_source="provider")
    assert ctx.duration_seconds is None
    assert (
        context(duration_seconds=0.0, clock_source="client_monotonic").duration_seconds
        == 0.0
    )
    for changes in (
        {"started_at": start},
        {"started_at": datetime(2026, 11, 1), "clock_source": "provider"},
        {"started_at": start, "clock_source": "client_monotonic"},
        {"started_at": end, "completed_at": start, "clock_source": "provider"},
        {"duration_seconds": -1.0, "clock_source": "client_wall"},
        {"duration_seconds": float("inf"), "clock_source": "client_wall"},
    ):
        with pytest.raises(ValidationError):
            context(**changes)


def test_privacy_of_invalid_fields_and_values() -> None:
    __tracebackhide__ = True
    secret = f"synthetic-private-{uuid4().hex}"
    try:
        for supplied in (
            {secret: secret},
            {"input_tokens": secret},
            {"usage_source": secret},
            {"breakdown": ({"field": secret},)},
        ):
            try:
                ReportedUsageInput.model_validate(supplied)
            except ValidationError as error:
                surfaces = (str(error), repr(error), str(error.errors()), error.json())
                if any(secret in surface for surface in surfaces):
                    raise RuntimeError("unsafe error") from None
            else:
                raise RuntimeError("invalid record accepted")
        record = extract_provider_usage(identity(), context(), None)
        if (
            secret in json.dumps(record.model_dump(mode="json"))
            or secret in record.model_dump_json()
        ):
            raise RuntimeError("unsafe serialization")
    except Exception:
        pytest.fail("Synthetic usage privacy check failed.", pytrace=False)


@pytest.mark.parametrize(
    "report",
    [
        {"usage_source": "openai.chat.usage", "cache_write_tokens": 0},
        {
            "usage_source": "openai.chat.usage",
            "input_tokens": 2,
            "cache_read_tokens": 3,
        },
        {"usage_source": "anthropic.messages.usage", "total_tokens": 0},
        {"usage_source": "anthropic.messages.usage", "reasoning_tokens": 0},
    ],
)
def test_unestablished_source_counters_are_rejected(report: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ReportedUsageInput.model_validate(report)


def test_breakdown_component_cannot_exceed_parent() -> None:
    with pytest.raises(ValidationError):
        extract_anthropic_messages_usage(
            identity(),
            context(provider="anthropic", endpoint="messages"),
            AnthropicMessagesUsageInput(
                cache_creation_input_tokens=2,
                cache_creation=AnthropicCacheCreationInput(ephemeral_5m_input_tokens=3),
            ),
        )


def test_multiple_estimate_assumptions_must_be_distinct() -> None:
    with pytest.raises(ValidationError):
        EstimatedCost(
            amount=Decimal("0"),
            currency="USD",
            pricing_date=date(2026, 11, 1),
            pricing_source="caller_supplied_rates",
            assumptions=("excludes_taxes", "excludes_taxes"),
        )


def test_usage_timing_and_model_provenance_round_trip() -> None:
    record = extract_provider_usage(
        identity(),
        context(
            reported_model="reported-synthetic-model",
            sdk_version="synthetic-sdk/1.0",
            remote_response_id="response-001",
            clock_source="client_wall",
            started_at=datetime(2026, 11, 1, tzinfo=UTC),
            duration_seconds=0.0,
        ),
        None,
    )
    assert ProviderUsage.model_validate_json(record.model_dump_json()) == record
    assert record.reported_model == "reported-synthetic-model"
    assert record.requested_model == "synthetic-model"


def test_cross_field_validation_errors_are_content_free() -> None:
    __tracebackhide__ = True
    secret = f"synthetic-cross-field-{uuid4().hex}"
    try:
        supplied: dict[str, Any] = {
            **identity().model_dump(),
            **context().model_dump(),
            "requested_model": secret,
            "endpoint": "responses",
            "usage_source": "openai.chat.usage",
        }
        for operation in (
            lambda: ProviderUsage.model_validate(supplied),
            lambda: ProviderUsage(**supplied),
            lambda: TypeAdapter(ProviderUsage).validate_python(supplied),
            lambda: ProviderUsage.model_validate_json(json.dumps(supplied)),
        ):
            try:
                operation()
            except ValidationError as error:
                if any(
                    secret in surface
                    for surface in (
                        str(error),
                        repr(error),
                        str(error.errors()),
                        error.json(),
                    )
                ):
                    raise RuntimeError("unsafe cross-field error") from None
            else:
                raise RuntimeError("invalid cross-field record accepted")
    except Exception:
        pytest.fail("Synthetic cross-field privacy check failed.", pytrace=False)


@pytest.mark.parametrize("short,long,parent", [(60, 50, 100), (1, 1, 1)])
def test_complete_anthropic_subdivisions_cannot_exceed_parent(
    short: int, long: int, parent: int
) -> None:
    with pytest.raises(ValidationError):
        extract_anthropic_messages_usage(
            identity(),
            context(provider="anthropic", endpoint="messages"),
            AnthropicMessagesUsageInput(
                cache_creation_input_tokens=parent,
                cache_creation=AnthropicCacheCreationInput(
                    ephemeral_5m_input_tokens=short,
                    ephemeral_1h_input_tokens=long,
                ),
            ),
        )


@pytest.mark.parametrize(
    "short,long,parent", [(60, 30, 100), (60, None, 100), (60, 50, None)]
)
def test_partial_anthropic_subdivisions_preserve_missing_totals(
    short: int, long: int | None, parent: int | None
) -> None:
    record = extract_anthropic_messages_usage(
        identity(),
        context(provider="anthropic", endpoint="messages"),
        AnthropicMessagesUsageInput(
            cache_creation_input_tokens=parent,
            cache_creation=AnthropicCacheCreationInput(
                ephemeral_5m_input_tokens=short,
                ephemeral_1h_input_tokens=long,
            ),
        ),
    )
    assert record.cache_write_tokens == parent


@pytest.mark.parametrize(
    "provider",
    ["openai", "anthropic", "gemini", "vertex", "together", "ollama", "typesafe"],
)
def test_synthetic_cost_cannot_claim_real_provider_billing(provider: str) -> None:
    with pytest.raises(ValidationError):
        extract_provider_usage(
            identity(),
            context(provider=provider),
            ReportedUsageInput(
                usage_source="synthetic.reported_usage",
                reported_cost=ReportedCost(
                    amount=Decimal("0.002"),
                    currency="USD",
                    source="synthetic.reported_cost",
                ),
            ),
        )
