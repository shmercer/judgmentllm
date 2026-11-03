"""Narrow provider boundary; wire bytes and evidence, never persistence/scoring."""

from __future__ import annotations

from typing import Literal, Protocol

from pydantic import JsonValue, model_validator

from judgmentllm.config import GenerationSpec, RunSpec
from judgmentllm.models import ID
from judgmentllm.persistence.hashing import bytes_digest
from judgmentllm.persistence.models import SHA256, ExecutionModel
from judgmentllm.planning import CapabilityProfile
from judgmentllm.preparation.session import RequestDependency
from judgmentllm.prompts import RenderedPrompt
from judgmentllm.results import CategoryProbability


class ProviderError(RuntimeError):
    """Content-free adapter diagnostic."""


class TransportNotSubmitted(ProviderError):
    """An invoked call conclusively did not reach acceptance."""


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
    kind: Literal["response", "refusal", "provider_error"]
    raw: bytes


class ParsedResponse(ExecutionModel):
    """Parsed transport envelope; existing output validators interpret output."""

    kind: Literal["response", "refusal", "provider_error"]
    output: dict[str, JsonValue] | None = None
    rubric_probabilities: tuple[CategoryProbability, ...] | None = None
    provider_expected_index: float | None = None
    p_left: float | None = None
    p_right: float | None = None
    p_tie: float | None = None


class ProviderAdapter(Protocol):
    def capabilities_for(
        self, provider: str, endpoint: str, model: str
    ) -> CapabilityProfile: ...

    def capabilities(self, spec: RunSpec) -> CapabilityProfile: ...

    def validate(self, spec: RunSpec) -> None: ...

    def prepare(
        self, spec: RunSpec, dependency: RequestDependency, rendered: RenderedPrompt
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

    def parse(self, response: ProviderResponse) -> ParsedResponse: ...
