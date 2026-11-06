"""Narrow provider boundary; wire bytes and evidence, never persistence/scoring."""

from __future__ import annotations

from typing import Literal, Protocol

from pydantic import JsonValue, field_validator, model_validator

from judgmentllm.config import GenerationSpec, RunSpecification
from judgmentllm.models import ID
from judgmentllm.output_commitments import OutputCommitmentV2
from judgmentllm.persistence.hashing import bytes_digest
from judgmentllm.persistence.models import SHA256, ExecutionModel
from judgmentllm.planning import CapabilityProfile
from judgmentllm.preparation.session import RequestDependency
from judgmentllm.prompts import RenderedAssessmentPrompt
from judgmentllm.results import CategoryProbability


class ProviderError(RuntimeError):
    """Content-free adapter diagnostic."""


class TransportNotSubmitted(ProviderError):
    """An invoked call conclusively did not reach acceptance."""


class TransientNotSubmitted(TransportNotSubmitted):
    """Authoritatively unaccepted transient failure, optionally with retry-after."""

    def __init__(
        self,
        message: str = "transient authoritative nonacceptance",
        *,
        retry_after: float | None = None,
    ):
        super().__init__(message)
        if retry_after is not None and (
            type(retry_after) not in (int, float) or not 0 <= retry_after < float("inf")
        ):
            raise ValueError("retry-after must be a finite nonnegative duration")
        self.retry_after = retry_after


class SubmissionUncertain(ProviderError):
    """The caller lacks acceptance evidence; authoritative lookup is required."""


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


class ProviderResponse(ExecutionModel):
    """Exact received bytes; only full capture may retain them."""

    observation_key: ID
    kind: Literal["response", "refusal", "provider_error", "confirmed_cancellation"]
    raw: bytes


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
