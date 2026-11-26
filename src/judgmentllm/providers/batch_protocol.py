"""Narrow batch boundary. Encoding and authoritative evidence belong to adapters."""

from collections.abc import AsyncGenerator, AsyncIterator, Callable, Mapping
from typing import TYPE_CHECKING, Any, Protocol

from judgmentllm.batch_models import (
    BatchContextV1,
    BatchCursorV1,
    BatchOperationIntentV1,
    BatchOperationReceiptV1,
    BatchSourceCommitmentV1,
    BatchStatusEvidenceV1,
    OpenAIBatchContextV1,
)
from judgmentllm.config import RunSpecification
from judgmentllm.evidence.usage import ReportedUsageInput
from judgmentllm.persistence.hashing import semantic_digest
from judgmentllm.planning import CapabilityProfile
from judgmentllm.preparation.session import RequestDependency
from judgmentllm.providers.protocol import (
    ParsedResponse,
    PreparedRequestV2,
    ProviderResponse,
)

if TYPE_CHECKING:
    from judgmentllm.openai_batch_transport import PreparedOpenAIBatchRequest

if TYPE_CHECKING:
    BatchPreparedRequest = PreparedRequestV2 | PreparedOpenAIBatchRequest
else:
    BatchPreparedRequest = PreparedRequestV2


class BatchEvidenceWithheld(Exception):
    """An untrusted identity or representation cannot cross the durable boundary."""


def result_index_scope(
    service_id: str,
    submission_id: str,
    remote_id: str,
    source_id: str,
    members: list[tuple[str, int, Any]],
) -> str:
    return semantic_digest(
        "batch-result-index-scope-v1",
        [
            service_id,
            submission_id,
            remote_id,
            source_id,
            [[key, position, wire.payload_sha256] for key, position, wire in members],
        ],
    )


class NativeBatchAdapter(Protocol):
    identity: str

    def context(self) -> BatchContextV1 | OpenAIBatchContextV1: ...
    def capabilities(self, spec: RunSpecification) -> CapabilityProfile: ...
    def validate(self, spec: RunSpecification) -> None: ...
    def capabilities_for(
        self, provider: str, endpoint: str, model: str
    ) -> CapabilityProfile: ...
    def prepare(
        self,
        spec: RunSpecification,
        dependency: RequestDependency,
        rendered: Any,
    ) -> BatchPreparedRequest: ...
    def parse(
        self, response: ProviderResponse, *, strict_output: bool = False
    ) -> ParsedResponse: ...
    def create_payload(
        self, manifest_sha256: str, shard: Any, uploaded: BatchOperationReceiptV1
    ) -> bytes: ...
    def cancel_payload(
        self, manifest_sha256: str, submission_id: str, remote_id: str | None
    ) -> bytes: ...
    def screen_replay(
        self, raw: bytes, expected_keys: Mapping[str, Any]
    ) -> tuple[str, ProviderResponse, ReportedUsageInput, bytes, bool] | None: ...
    async def invoke(
        self,
        intent: BatchOperationIntentV1,
        payload: AsyncIterator[bytes],
        guard: Callable[[], None],
    ) -> bytes: ...
    def decode_receipt(
        self, raw: bytes, intent: BatchOperationIntentV1
    ) -> BatchOperationReceiptV1: ...
    async def reconcile_operation(
        self, intent: BatchOperationIntentV1
    ) -> bytes | None: ...
    async def status(
        self, submission_id: str, remote_id: str
    ) -> BatchStatusEvidenceV1: ...
    async def result_source(self, submission_id: str, source: str) -> str: ...
    async def result_index(
        self,
        submission_id: str,
        remote_id: str,
        source: str,
        members: list[tuple[str, int, Any]],
    ) -> tuple[BatchSourceCommitmentV1, bytes]:
        """Read bounded typed metadata from the original source authority."""
        ...

    def results(
        self,
        submission_id: str,
        remote_id: str,
        source: str,
        members: list[tuple[str, int, Any]],
        cursor: BatchCursorV1 | None = None,
    ) -> AsyncGenerator[bytes, None]:
        """Stream a range only after its cursor matches the authoritative index."""
        ...

    def screen_result(
        self, raw: bytes, expected_keys: Mapping[str, Any]
    ) -> tuple[str, ProviderResponse, ReportedUsageInput, bytes, bool] | None: ...


class BatchSourceOverrun(OSError):
    """An actual content chunk exceeds the pinned file size before admission."""
