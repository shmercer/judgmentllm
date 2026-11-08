"""Narrow provider boundary; wire bytes and evidence, never persistence/scoring."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any, Literal, Protocol, Self

from pydantic import Field, JsonValue, field_validator, model_validator

from judgmentllm.config import GenerationSpec, RunSpecification
from judgmentllm.evidence._base import EvidenceModel
from judgmentllm.evidence.usage import InvocationFailureCode, ReportedUsageInput
from judgmentllm.models import ID
from judgmentllm.output_commitments import OutputCommitmentV2
from judgmentllm.persistence.hashing import bytes_digest
from judgmentllm.persistence.models import SHA256, ExecutionModel
from judgmentllm.planning import CapabilityProfile
from judgmentllm.preparation.session import RequestDependency
from judgmentllm.prompts import RenderedAssessmentPrompt
from judgmentllm.providers.evidence import ProviderEvidence
from judgmentllm.results import CategoryProbability


class ProviderError(RuntimeError):
    """Content-free adapter diagnostic."""


class TransportNotSubmitted(ProviderError):
    """An invoked call conclusively did not reach acceptance.

    Optional usage is an explicit reported invocation fact, not proof of billing
    or acceptance. It never creates an observation or authorizes another call.
    """

    def __init__(
        self,
        message: str = "transport conclusively did not submit",
        *,
        usage: ReportedUsageInput | None = None,
        diagnostic_code: InvocationFailureCode | None = None,
    ):
        super().__init__(message)
        if diagnostic_code not in (
            None,
            "request_rejected",
            "credentials_rejected",
            "permission_denied",
            "model_or_endpoint_unavailable",
            "rate_limited",
        ):
            raise ValueError("invalid invocation failure code")
        self.diagnostic_code: InvocationFailureCode | None = diagnostic_code
        self.usage = (
            ReportedUsageInput.model_validate(usage) if usage is not None else None
        )


class TransientNotSubmitted(TransportNotSubmitted):
    """Authoritatively unaccepted transient failure, optionally with retry-after."""

    def __init__(
        self,
        message: str = "transient authoritative nonacceptance",
        *,
        retry_after: float | None = None,
        usage: ReportedUsageInput | None = None,
    ):
        super().__init__(message, usage=usage)
        if retry_after is not None and (
            type(retry_after) not in (int, float) or not 0 <= retry_after < float("inf")
        ):
            raise ValueError("retry-after must be a finite nonnegative duration")
        self.retry_after = retry_after


class SubmissionUncertain(ProviderError):
    """Acceptance is unresolved; optional facts establish an invoked SDK call only."""

    def __init__(
        self,
        message: str = "submission acceptance is unresolved",
        *,
        invoked: bool = False,
        usage: ReportedUsageInput | None = None,
    ):
        super().__init__(message)
        if type(invoked) is not bool:
            raise ValueError("invalid invocation certainty")
        self.invoked = invoked
        self.usage = (
            ReportedUsageInput.model_validate(usage) if usage is not None else None
        )


class ProviderInvocationCancelled(asyncio.CancelledError):
    """Cancellation with a known forwarding fact, never acceptance authority."""

    def __init__(self, *, invoked: bool) -> None:
        super().__init__("provider invocation was cancelled")
        if type(invoked) is not bool:
            raise ValueError("invalid invocation certainty")
        self.invoked = invoked


class RetrievalUnavailable(ProviderError):
    """An original accepted observation cannot currently be retrieved."""


class ProviderIntegrityError(ProviderError):
    """Identity, payload, or observation evidence conflicts."""


class PreparedRequest(ExecutionModel):
    """In-memory actual wire bytes, distinct from the logical request identity.

    Never serialize this object to executor persistence in minimal capture.
    The executor journals permitted fields and hashes through its own models.
    """

    request_uid: ID
    task_ids: tuple[ID, ...]
    dependency_sha256: SHA256
    prompt_sha256: SHA256
    payload_sha256: SHA256
    provider: ID
    endpoint: ID
    model: ID
    generation: GenerationSpec | None = None
    payload: bytes

    @model_validator(mode="after")
    def exact_bytes(self) -> PreparedRequest:
        if not self.task_ids or len(set(self.task_ids)) != len(self.task_ids):
            raise ValueError("prepared request needs unique original membership")
        if bytes_digest(self.payload) != self.payload_sha256:
            raise ValueError("prepared request payload identity does not match bytes")
        return self


class PreparedRequestV2(PreparedRequest):
    """In-memory v2 representation; its commitment is bound in adapter bytes."""

    preparation_version: Literal[2] = 2
    output_commitment: OutputCommitmentV2

    @field_validator("preparation_version", mode="before")
    @classmethod
    def strict_preparation_version(cls, value: object) -> object:
        if type(value) is not int or value != 2:
            raise ValueError("preparation_version must be integer 2")
        return value


class ProviderAcknowledgment(ExecutionModel):
    remote_id: ID
    observation_key: ID


class ReconciliationEvidence(ExecutionModel):
    """An absent acknowledgment never supplies one of these positive facts."""

    outcome: Literal["not_submitted", "accepted", "observation_available", "unresolved"]
    remote_id: ID | None = None
    observation_key: ID | None = None


class ProviderResponse(EvidenceModel):
    """Exact received bytes; only full capture may retain them."""

    observation_key: ID
    kind: Literal["response", "refusal", "provider_error", "confirmed_cancellation"]
    raw: bytes = Field(exclude=True, repr=False)
    evidence: ProviderEvidence | None = Field(default=None, exclude=True, repr=False)

    def model_copy(
        self, *, update: Mapping[str, Any] | None = None, deep: bool = False
    ) -> Self:
        # Preserve excluded transient bytes while validating the complete copy.
        values = dict(self)
        if update is not None:
            values.update(update)
        return type(self).model_validate(values)


class ParsedResponse(ExecutionModel):
    """Parsed transport envelope; existing output validators interpret output."""

    kind: Literal["response", "refusal", "provider_error", "confirmed_cancellation"]
    output: dict[str, JsonValue] | None = None
    rubric_probabilities: tuple[CategoryProbability, ...] | None = None
    provider_expected_index: float | None = None
    member_probabilities: dict[str, tuple[CategoryProbability, ...]] | None = None
    member_expected_indices: dict[str, float] | None = None
    p_left: float | None = None
    p_right: float | None = None
    p_tie: float | None = None


class ProviderAdapter(Protocol):
    def capabilities_for(
        self, provider: str, endpoint: str, model: str
    ) -> CapabilityProfile: ...

    def capabilities(self, spec: RunSpecification) -> CapabilityProfile: ...

    def validate(self, spec: RunSpecification) -> None: ...

    def prepare(
        self,
        spec: RunSpecification,
        dependency: RequestDependency,
        rendered: RenderedAssessmentPrompt,
    ) -> PreparedRequest: ...

    async def submit(
        self, prepared: PreparedRequest, *, submission_id: str, correlation_id: str
    ) -> ProviderAcknowledgment: ...

    async def reconcile(
        self, prepared: PreparedRequest, *, submission_id: str, correlation_id: str
    ) -> ReconciliationEvidence: ...

    async def retrieve(
        self, prepared: PreparedRequest, *, submission_id: str, correlation_id: str
    ) -> ProviderResponse: ...

    def parse(
        self, response: ProviderResponse, *, strict_output: bool = False
    ) -> ParsedResponse: ...
