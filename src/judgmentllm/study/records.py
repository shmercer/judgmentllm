"""Strict immutable campaign records; prices and account proof are never guessed."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Literal, Self, cast

from pydantic import Field, ModelWrapValidatorHandler, model_validator

from judgmentllm.models import ID
from judgmentllm.native_schema import NativeModel
from judgmentllm.persistence.hashing import bytes_digest, canonical_bytes
from judgmentllm.persistence.models import SHA256
from judgmentllm.study.definitions import SCENARIOS, STUDY_VERSION, SUITES, SuiteName


class StudyRecord(NativeModel):
    @model_validator(mode="wrap")
    @classmethod
    def exact_tokens(
        cls, value: object, handler: ModelWrapValidatorHandler[Self]
    ) -> Self:
        if isinstance(value, dict):
            values = cast(dict[str, object], value)
            for key in (
                "proposal_version",
                "concurrency",
                "automatic_retries",
                "authorized_calls",
                "campaign_calls",
                "campaign_seconds",
                "reserved_input_tokens",
                "reserved_output_tokens",
                "max_calls",
                "maximum_calls",
                "elapsed_seconds",
                "max_output_tokens",
                "operation_deadline_seconds",
                "billable_output_bound",
            ):
                if key in values and type(values[key]) is not int:
                    raise ValueError("Study limits and versions require integers")
            for key in (
                "accept_billable_calls",
                "accept_synthetic_capture",
                "input_bound_verified",
                "served_outcomes_verified",
                "synthetic_only",
            ):
                if key in values and type(values[key]) is not bool:
                    raise ValueError("Study consent requires explicit booleans")
        return handler(value)


class Selection(StudyRecord):
    provider: Literal["openai", "anthropic", "gemini"]
    model: ID
    scenario: Literal[
        "grouped_scores",
        "grouped_explanations",
        "independent_scores",
        "independent_explanations",
        "cj",
    ]
    tier: ID
    trait_id: ID | None = None
    reversed: bool = False


class Eligibility(StudyRecord):
    """Independent review of one exact account/route/native/feature/tier scope."""

    selection: Selection
    account_scope: ID
    sdk_version: ID
    api_version: ID
    mechanism: ID
    features: tuple[ID, ...]
    credential_source: Literal[
        "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY"
    ]
    recorded_on: date
    reviewed_by: ID
    evidence_sha256: SHA256
    binding_sha256: SHA256
    input_bound_verified: Literal[True]
    billable_output_bound: int = Field(gt=0, le=8192)
    served_outcomes_verified: Literal[True]
    billable_served_tiers: tuple[ID, ...] = Field(min_length=1)


class OutcomePrice(StudyRecord):
    served_tier: ID
    input_per_million: Decimal = Field(ge=0, allow_inf_nan=False)
    billable_output_per_million: Decimal = Field(ge=0, allow_inf_nan=False)


class Price(StudyRecord):
    """Dated account rates for every documented possible served outcome."""

    selection: Selection
    account_scope: ID
    recorded_on: date
    currency: Literal["USD"] = "USD"
    outcomes: tuple[OutcomePrice, ...] = Field(min_length=1)
    evidence_sha256: SHA256
    reviewed_by: ID

    @model_validator(mode="after")
    def distinct_tiers(self) -> Price:
        if len({p.served_tier for p in self.outcomes}) != len(self.outcomes):
            raise ValueError("Duplicate served pricing outcome")
        return self

    @property
    def served_outcomes(self) -> tuple[str, ...]:
        return tuple(p.served_tier for p in self.outcomes)

    def reservation(self, inputs: int, outputs: int) -> Decimal:
        return max(
            inputs * p.input_per_million + outputs * p.billable_output_per_million
            for p in self.outcomes
        ) / Decimal(1_000_000)


class Case(StudyRecord):
    case_id: ID
    configuration_id: ID
    selection: Selection
    run_id: ID
    request_uid: ID
    task_ids: tuple[ID, ...]
    attempt_id: ID
    submission_id: ID
    correlation_id: ID
    specification_json: str = Field(repr=False)
    specification_sha256: SHA256
    request_body_utf8: str = Field(repr=False)
    payload_sha256: SHA256
    prompt_sha256: SHA256
    schema_sha256: SHA256
    projection_sha256: SHA256
    transport_sha256: SHA256
    dependency_sha256: SHA256
    features: tuple[ID, ...]
    sdk_version: ID
    api_version: ID
    mechanism: ID
    account_scope: ID
    max_output_tokens: int = Field(gt=0, le=8192)
    reserved_input_tokens: Literal[32768] = 32768
    reserved_output_tokens: Literal[8192] = 8192
    operation_deadline_seconds: int = Field(gt=0)
    eligibility: Eligibility | None = None
    price: Price | None = None
    reserved_usd: Decimal | None = Field(default=None, ge=0)
    blockers: tuple[ID, ...]

    @model_validator(mode="after")
    def commitments(self) -> Case:
        if self.max_output_tokens != SCENARIOS[self.selection.scenario][
            1
        ] or self.operation_deadline_seconds != (
            960 if self.selection.tier == "flex" else 90
        ):
            raise ValueError("Scenario output cap or service deadline differs")
        if (
            bytes_digest(self.request_body_utf8.encode()) != self.payload_sha256
            or bytes_digest(self.specification_json.encode())
            != self.specification_sha256
        ):
            raise ValueError("Case bytes differ")
        if (self.price is None) != (self.reserved_usd is None):
            raise ValueError("Unknown price cannot reserve money")
        if self.price is not None and (
            self.price.selection != self.selection
            or self.price.account_scope != self.account_scope
            or self.reserved_usd
            != self.price.reservation(
                self.reserved_input_tokens, self.reserved_output_tokens
            )
        ):
            raise ValueError("Case price differs")
        served = {
            "default": "standard",
            "standard_only": "standard",
            "fast": "priority",
        }.get(self.selection.tier, self.selection.tier)
        required: set[str] = {served}
        if served in {"flex", "priority"}:
            required.add("standard")
        if self.eligibility is not None:
            reviewed = set(self.eligibility.billable_served_tiers)
            allowed: set[str] = {"standard", "priority"}
            if self.selection.provider != "anthropic":
                allowed.add("flex")
            if self.selection.provider == "openai":
                allowed.update(("scale", "ultrafast"))
            if not required <= reviewed <= allowed or len(reviewed) != len(
                self.eligibility.billable_served_tiers
            ):
                raise ValueError("Reviewed billable served outcomes differ")
            required |= reviewed
        if self.price is not None and not required <= set(self.price.served_outcomes):
            raise ValueError("Pricing does not cover documented served outcomes")
        if self.eligibility is not None and (
            self.eligibility.selection != self.selection
            or self.eligibility.account_scope != self.account_scope
            or self.eligibility.sdk_version != self.sdk_version
            or self.eligibility.api_version != self.api_version
            or self.eligibility.mechanism != self.mechanism
            or self.eligibility.features != self.features
            or self.eligibility.billable_output_bound < self.max_output_tokens
        ):
            raise ValueError("Eligibility scope differs")
        expected = (
            ()
            if self.eligibility
            else ("account_native_tier_and_token_bounds_unknown",)
        ) + (() if self.price else ("account_prices_unknown",))
        if self.blockers != expected:
            raise ValueError("Unresolved gates differ")
        return self


class Proposal(StudyRecord):
    proposal_version: Literal[1] = 1
    suite: SuiteName
    source_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    study_version: ID
    max_calls: int
    ceiling_usd: Decimal
    elapsed_seconds: int
    currency: Literal["USD"] = "USD"
    concurrency: Literal[1] = 1
    automatic_retries: Literal[0] = 0
    capture: Literal["full", "minimal"] = "full"
    reasoning_capture: Literal["none"] = "none"
    synthetic_only: Literal[True] = True
    calls: tuple[Case, ...]
    status: Literal["blocked", "sealed"]
    authorized_calls: Literal[0] = 0

    @model_validator(mode="after")
    def limits(self) -> Proposal:
        count, dollars, seconds = SUITES[self.suite]
        if (
            self.study_version != STUDY_VERSION
            or (self.max_calls, self.ceiling_usd, self.elapsed_seconds)
            != (count, Decimal(dollars), seconds)
            or len(self.calls) != count
            or len({c.case_id for c in self.calls}) != count
        ):
            raise ValueError("Suite limits or cases differ")
        if self.status == "sealed" and any(c.blockers for c in self.calls):
            raise ValueError("Unresolved gates prevent sealing")
        return self

    @property
    def digest(self) -> str:
        return bytes_digest(canonical_bytes(self))


class Authorization(StudyRecord):
    """Separate explicit suite and cumulative-budget consent; never part of a proposal."""

    proposal_sha256: SHA256
    source_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    suite: SuiteName
    maximum_calls: int
    ceiling_usd: Decimal
    elapsed_seconds: int
    campaign_calls: Literal[66] = 66
    campaign_usd: Decimal = Field(default=Decimal(10), ge=10, le=10)
    campaign_seconds: Literal[39600] = 39600
    accept_billable_calls: Literal[True]
    accept_synthetic_capture: Literal[True]
    authorized_by: ID
    authorized_at: datetime

    def verify(self, proposal: Proposal) -> None:
        if (
            self.accept_billable_calls is not True
            or self.accept_synthetic_capture is not True
            or self.authorized_at.tzinfo is None
            or self.authorized_at > datetime.now(UTC)
        ):
            raise ValueError("Independent dated billable/capture consent is required")
        if proposal.status != "sealed" or (
            self.proposal_sha256,
            self.source_commit,
            self.suite,
            self.maximum_calls,
            self.ceiling_usd,
            self.elapsed_seconds,
        ) != (
            proposal.digest,
            proposal.source_commit,
            proposal.suite,
            proposal.max_calls,
            proposal.ceiling_usd,
            proposal.elapsed_seconds,
        ):
            raise ValueError("Authorization differs from the exact sealed proposal")
