"""Guarded SDK transport for the offline Anthropic Batch route."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Literal, TypeVar, cast

from judgmentllm.anthropic_batch_transport import (
    BATCH_ORIGIN,
    BATCH_PATH,
    PreparedAnthropicBatchRequest,
    batch_context,
    capabilities_for,
    compile_request,
    require_sdk,
)
from judgmentllm.anthropic_prompts import RenderedAnthropicPromptV1
from judgmentllm.batch_models import (
    AnthropicBatchContextV1,
    AnthropicBatchCreateReceiptV1,
    AnthropicBatchError,
    AnthropicBatchHTTPStage,
    AnthropicBatchInterruption,
    AnthropicBatchStatusV1,
    BatchOperationIntentV1,
    BatchOperationReceiptV1,
    operation_receipt_model,
)
from judgmentllm.config import RunSpecification
from judgmentllm.execution.native_batch import BudgetExhausted, InvocationBudget
from judgmentllm.output_contracts import decode_assessment_output
from judgmentllm.persistence.hashing import bytes_digest, canonical_bytes
from judgmentllm.persistence.models import IntegrityError, PersistenceError
from judgmentllm.preparation.session import RequestDependency
from judgmentllm.providers.anthropic import _SDK_LOGGING, _protected_client, _sdk
from judgmentllm.providers.anthropic_batch_response import (
    parse,
    safe_id,
    screen_error,
    screen_job,
    screen_row,
)
from judgmentllm.providers.anthropic_response import _mapping
from judgmentllm.providers.batch_protocol import BatchEvidenceWithheld

# pyright: reportPrivateUsage=false

CONTROL_BYTES = 1_048_576
CHUNK_BYTES = 65_536
_FIXTURE_KEY = "synthetic-anthropic-batch-key"
_HEADERS = {
    "accept",
    "accept-encoding",
    "anthropic-version",
    "authorization",
    "connection",
    "content-length",
    "content-type",
    "host",
    "user-agent",
    "x-stainless-raw-response",
    "x-stainless-read-timeout",
    "x-stainless-retry-count",
    "x-stainless-timeout",
}
T = TypeVar("T")


class SourceError(OSError):
    def __init__(
        self,
        code: Literal[
            "source_limit", "source_framing", "source_conflict", "source_unavailable"
        ],
    ):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class AcceptedJob:
    submission_id: str
    job_id: str
    member_count: int
    timestamps: tuple[str, str] | None = None


@dataclass
class _Call:
    stage: AnthropicBatchHTTPStage
    method: Literal["GET", "POST"]
    path: str
    job: AcceptedJob
    intent_id: str | None = None
    source_id: str | None = None
    body: bytes = field(default=b"", repr=False)
    before_send: Callable[[], None] | None = None
    check_status: Callable[[AnthropicBatchStatusV1], None] | None = None
    call_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    status: AnthropicBatchStatusV1 | None = None
    failure: Exception | None = field(default=None, repr=False)
    http_status: int | None = None
    request_id: str | None = None
    headers_sha256: str | None = None
    interruption: AnthropicBatchInterruption | None = None


class AnthropicBatchAdapter:
    identity = "anthropic-v1"
    capabilities_for = staticmethod(capabilities_for)
    parse = staticmethod(parse)

    def __init__(
        self,
        context: AnthropicBatchContextV1,
        *,
        fixture_transport: object | None = None,
    ) -> None:
        self._context = context
        self._fixture_transport = fixture_transport
        self._client: Any = None
        self._calls: ContextVar[list[_Call] | None] = ContextVar(
            "anthropic-batch-calls", default=None
        )
        self.budget: InvocationBudget
        self.publish_diagnostic: Callable[[dict[str, object], bytes | None], str]
        self.cancel_job: Callable[[BatchOperationIntentV1], AcceptedJob]

    def context(self) -> AnthropicBatchContextV1:
        return self._context

    def validate(self, spec: RunSpecification) -> None:
        if batch_context(spec) != self._context:
            raise ValueError("Anthropic Batch scope differs")
        require_sdk()

    def capabilities(self, spec: RunSpecification):
        self.validate(spec)
        return capabilities_for(spec.provider, spec.endpoint, spec.model)

    def prepare(
        self,
        spec: RunSpecification,
        dependency: RequestDependency,
        rendered: RenderedAnthropicPromptV1,
    ) -> PreparedAnthropicBatchRequest:
        self.validate(spec)
        return compile_request(spec, dependency, rendered)

    def screen_result(self, raw: bytes, expected_keys: Mapping[str, Any]):
        try:
            return screen_row(raw, expected_keys, _FIXTURE_KEY)
        except ValueError:
            raise SourceError("source_framing") from None

    def screen_replay(self, raw: bytes, expected_keys: Mapping[str, Any]):
        return screen_row(raw, expected_keys, _FIXTURE_KEY, replay=True)

    @staticmethod
    def cancel_payload(
        manifest_sha256: str, submission_id: str, remote_id: str | None
    ) -> bytes:
        return b""

    def decode_receipt(
        self, raw: bytes, intent: BatchOperationIntentV1
    ) -> BatchOperationReceiptV1:
        model = operation_receipt_model(self._context, intent.kind)
        receipt = model.model_validate_json(raw)
        if (
            receipt.instance_id != intent.instance_id
            or receipt.intent_sha256 != bytes_digest(canonical_bytes(intent))
            or receipt.outcome != "accepted"
            or intent.kind not in ("create", "cancel")
            or intent.sequence != 1
            or (intent.kind == "cancel" and receipt.remote_id != intent.remote_id)
            or (intent.kind == "create" and intent.remote_id is not None)
        ):
            raise IntegrityError(
                "Anthropic Batch receipt differs from the original intent"
            )
        safe_id(receipt.remote_id, "msgbatch_", _FIXTURE_KEY)
        return receipt

    async def invoke(
        self,
        intent: BatchOperationIntentV1,
        payload: AsyncIterator[bytes],
        guard: Callable[[], None],
    ) -> bytes:
        data = b"".join([chunk async for chunk in payload])
        if intent.sequence != 1 or bytes_digest(data) != intent.payload_sha256:
            raise IntegrityError("Anthropic Batch intent differs from its body")
        if intent.kind == "create" and intent.remote_id is None:
            envelope = _mapping(decode_assessment_output(data))
            if set(envelope) != {"requests"} or not isinstance(
                envelope["requests"], list
            ):
                raise IntegrityError("invalid Anthropic Batch create body")
            requests = cast(list[dict[str, Any]], envelope["requests"])
            call = _Call(
                "create",
                "POST",
                BATCH_PATH,
                AcceptedJob(intent.instance_id, "", len(requests)),
                intent_id=intent.instance_id,
                body=data,
                before_send=guard,
            )
            client = await self._execution_client()
            await self._control(
                call, lambda: client.messages.batches.create(requests=requests)
            )
        elif intent.kind == "cancel" and not data:
            job = self.cancel_job(intent)
            call = _Call(
                "cancel",
                "POST",
                f"{BATCH_PATH}/{job.job_id}/cancel",
                job,
                intent_id=intent.instance_id,
                before_send=guard,
            )
            client = await self._execution_client()
            await self._control(
                call, lambda: client.messages.batches.cancel(job.job_id)
            )
            if call.status is None or not (
                call.status.provider_state == "canceling"
                or call.status.cancel_initiated_at is not None
            ):
                raise OSError("Anthropic Batch cancel acknowledgment is unavailable")
        else:
            raise IntegrityError("invalid Anthropic Batch mutation")
        assert call.status is not None
        fields = dict(
            instance_id=intent.instance_id,
            intent_sha256=bytes_digest(canonical_bytes(intent)),
            outcome="accepted",
            remote_id=call.status.remote_id,
        )
        if intent.kind == "create":
            return canonical_bytes(
                AnthropicBatchCreateReceiptV1.model_validate(
                    {
                        **fields,
                        "created_at": call.status.created_at,
                        "expires_at": call.status.expires_at,
                    }
                )
            )
        return canonical_bytes(BatchOperationReceiptV1.model_validate(fields))

    async def retrieve(
        self,
        job: AcceptedJob,
        *,
        stage: AnthropicBatchHTTPStage = "poll",
        source_id: str | None = None,
        intent_id: str | None = None,
        check: Callable[[AnthropicBatchStatusV1], None] | None = None,
    ) -> AnthropicBatchStatusV1:
        call = _Call(
            stage,
            "GET",
            f"{BATCH_PATH}/{job.job_id}",
            job,
            intent_id=intent_id,
            source_id=source_id,
            check_status=check,
        )
        client = await self._execution_client()
        await self._control(call, lambda: client.messages.batches.retrieve(job.job_id))
        assert call.status is not None
        return call.status

    async def _quiet(self, action: Callable[[], Awaitable[T]]) -> T:
        token = _SDK_LOGGING.set(True)
        try:
            return await action()
        finally:
            _SDK_LOGGING.reset(token)

    async def _execute(
        self, calls: list[_Call], action: Callable[[], Awaitable[T]]
    ) -> T:
        queue = self._calls.set(calls.copy())
        try:
            return await self._quiet(action)
        except Exception:
            for call in reversed(calls):
                if call.failure is not None:
                    raise call.failure from None
            raise OSError("Anthropic Batch HTTP call is unavailable") from None
        finally:
            self._calls.reset(queue)

    async def _control(self, call: _Call, action: Callable[[], Awaitable[Any]]) -> None:
        await self._execute([call], action)

    def _diagnostic(
        self,
        call: _Call,
        *,
        headers: bool = False,
        retained: bytes | None = None,
        disposition: Literal["exact", "redacted", "withheld"] | None = None,
        error_type: AnthropicBatchError | None = None,
    ) -> str:
        return self.publish_diagnostic(
            dict(
                call_id=call.call_id,
                stage=call.stage,
                phase="headers" if headers else "completion",
                intent_id=call.intent_id,
                submission_id=None
                if call.stage == "create"
                else call.job.submission_id,
                job_id=call.job.job_id or None,
                source_id=call.source_id,
                http_status=call.http_status,
                request_id=call.request_id,
                headers_sha256=call.headers_sha256,
                interruption=call.interruption,
                error_type=error_type,
                disposition=disposition,
            ),
            retained,
        )

    def _interruption(self, error: BaseException) -> AnthropicBatchInterruption:
        if isinstance(error, asyncio.CancelledError):
            return (
                "deadline"
                if asyncio.get_running_loop().time() >= self.budget.deadline
                else "cancelled"
            )
        if isinstance(error, TimeoutError):
            return "deadline"
        if isinstance(error, BudgetExhausted):
            return "budget"
        if isinstance(error, SourceError) and error.code == "source_limit":
            return "body_limit"
        if isinstance(error, (SourceError, BatchEvidenceWithheld)):
            return "unsafe_response"
        return "transport_error"

    async def _close(self, value: Any) -> None:
        from judgmentllm.lifecycle import _close_adapter

        await self._quiet(lambda: _close_adapter(value))

    async def _execution_client(self) -> Any:
        if self._client is not None:
            return self._client
        require_sdk()
        token = _SDK_LOGGING.set(True)
        try:
            sdk, http = _sdk()
            if type(self._fixture_transport) is not http.MockTransport:
                raise IntegrityError(
                    "Anthropic Batch requires the private offline transport"
                )
            adapter = self
            delegate: Any = self._fixture_transport

            class GuardedTransport(http.AsyncBaseTransport):
                async def handle_async_request(self, request: Any) -> Any:
                    return await adapter._forward(http, delegate, request)

                async def aclose(self) -> None:
                    await delegate.aclose()

            client = http.AsyncClient(
                transport=GuardedTransport(),
                trust_env=False,
                follow_redirects=False,
                timeout=http.Timeout(30),
                headers={"Accept-Encoding": "identity"},
            )
            try:
                self._client = _protected_client(
                    sdk,
                    api_key=_FIXTURE_KEY,
                    base_url=BATCH_ORIGIN,
                    max_retries=0,
                    timeout=http.Timeout(30),
                    http_client=client,
                )
            except BaseException:
                await self._close(client)
                raise
        finally:
            _SDK_LOGGING.reset(token)
        return self._client

    async def _forward(self, http: Any, delegate: Any, request: Any) -> Any:
        sequence = self._calls.get()
        if not sequence:
            raise IntegrityError("Anthropic Batch forward lacks an admitted call")
        call = sequence.pop(0)
        response: Any = None
        streaming = False
        retained = None
        disposition = None
        error_type = None
        forwarded = False
        try:
            if (
                request.method != call.method
                or request.url != http.URL(BATCH_ORIGIN + call.path)
                or request.headers.get("authorization") != "Bearer " + _FIXTURE_KEY
                or request.headers.get("anthropic-version") != self._context.api_version
                or request.headers.get("accept-encoding") != "identity"
                or set(request.headers) - _HEADERS
                or await request.aread() != call.body
            ):
                raise IntegrityError(
                    "Anthropic Batch SDK request differs from the admitted wire"
                )
            self.budget.remaining()
            if call.before_send is not None:
                call.before_send()
            self.budget.reconcile_step()
            forwarded = True
            response = await delegate.handle_async_request(request)
            call.http_status = response.status_code
            ids = response.headers.get_list("request-id")
            if len(ids) == 1:
                try:
                    call.request_id = safe_id(ids[0], "req_", _FIXTURE_KEY)
                except BatchEvidenceWithheld:
                    pass
            call.headers_sha256 = self._diagnostic(call, headers=True)
            buffered = len(response.content) if response.is_stream_consumed else None
            if buffered is not None:
                self.budget.receive_bytes(buffered)
            identity = (
                response.headers.get("content-encoding", "identity") == "identity"
            )
            media = (
                response.headers.get("content-type", "")
                .partition(";")[0]
                .strip()
                .lower()
            )
            if (
                not identity
                or "content-range" in response.headers
                or media.startswith("multipart/")
                or len(response.headers.get_list("content-type")) > 1
                or response.status_code != 200
                and not (
                    400 <= response.status_code < 600 and media == "application/json"
                )
            ):
                disposition = "withheld"
                if call.stage == "results_content":
                    raise SourceError("source_framing")
                raise BatchEvidenceWithheld(
                    "unsupported Anthropic Batch response framing"
                )
            if call.stage == "results_content" and response.status_code == 200:
                self._bound_stream(
                    http, response, self._context.max_source_bytes, buffered
                )
                streaming = True
                return response
            disposition = "withheld"
            self._bound_stream(http, response, CONTROL_BYTES, buffered)
            raw = await response.aread()
            if response.status_code != 200:
                error_type, retained = screen_error(raw, call.request_id)
                if retained is not None:
                    disposition = "redacted"
                else:
                    raise BatchEvidenceWithheld("unsafe Anthropic Batch rejection")
                raise OSError("Anthropic Batch HTTP response was rejected")
            call.status, safe, exact = screen_job(
                raw,
                key=_FIXTURE_KEY,
                submission_id=call.job.submission_id,
                expected_job=call.job.job_id or None,
                expected_count=call.job.member_count,
            )
            if (
                call.job.timestamps is not None
                and (call.status.created_at, call.status.expires_at)
                != call.job.timestamps
            ):
                raise SourceError("source_conflict")
            if call.check_status is not None:
                call.check_status(call.status)
            retained, disposition = (raw, "exact") if exact else (safe, "redacted")
            return http.Response(
                200,
                content=safe,
                headers={"content-type": "application/json"},
                request=request,
            )
        except Exception as error:
            if isinstance(
                error,
                (
                    BatchEvidenceWithheld,
                    BudgetExhausted,
                    IntegrityError,
                    PersistenceError,
                    SourceError,
                    TimeoutError,
                ),
            ):
                call.failure = error
            else:
                call.failure = OSError("Anthropic Batch HTTP call is unavailable")
            if error_type is None:
                call.interruption = self._interruption(error)
            raise call.failure from None
        except asyncio.CancelledError as error:
            call.interruption = self._interruption(error)
            raise
        finally:
            if not streaming and forwarded:
                try:
                    if response is not None:
                        await self._close(response)
                except Exception:
                    call.interruption = "cleanup_failed"
                    call.failure = PersistenceError(
                        "Anthropic Batch HTTP cleanup failed"
                    )
                    raise call.failure from None
                finally:
                    if call.http_status is None or call.headers_sha256 is not None:
                        try:
                            self._diagnostic(
                                call,
                                retained=retained,
                                disposition=disposition,
                                error_type=error_type,
                            )
                        except PersistenceError as error:
                            call.failure = error
                            raise

    def _bound_stream(
        self, http: Any, response: Any, limit: int, buffered: int | None
    ) -> None:
        length = response.headers.get_list("content-length")
        transfer = [
            value.strip().lower()
            for value in response.headers.get_list("transfer-encoding")
        ]
        if transfer and (transfer != ["chunked"] or length):
            raise SourceError("source_framing")
        expected = None
        if length:
            if (
                len(length) != 1
                or len(length[0]) > 12
                or not (length[0].isascii() and length[0].isdigit())
            ):
                raise SourceError("source_framing")
            expected = int(length[0])
        if buffered is not None:
            if buffered > limit:
                raise SourceError("source_limit")
            if expected is not None and expected != buffered:
                raise SourceError("source_framing")
            if limit != CONTROL_BYTES:
                raise SourceError("source_framing")
            return
        if expected is not None and expected > limit:
            raise SourceError("source_limit")
        stream = response.stream
        budget = self.budget

        class BoundedStream(http.AsyncByteStream):
            async def __aiter__(self) -> AsyncIterator[bytes]:
                received = 0
                source = stream.__aiter__()
                while True:
                    try:
                        raw = await budget.call(lambda: anext(source))
                    except StopAsyncIteration:
                        break
                    budget.receive_bytes(len(raw))
                    received += len(raw)
                    if received > limit:
                        raise SourceError("source_limit")
                    if expected is not None and received > expected:
                        raise SourceError("source_framing")
                    for offset in range(0, len(raw), CHUNK_BYTES):
                        yield raw[offset : offset + CHUNK_BYTES]
                if expected is not None and received != expected:
                    raise SourceError("source_framing")

            async def aclose(self) -> None:
                await stream.aclose()

        response.stream = BoundedStream()

    async def content(
        self,
        job: AcceptedJob,
        source_id: str,
        check: Callable[[AnthropicBatchStatusV1], None],
        admit: Callable[[], None],
        on_close: Callable[[], AnthropicBatchInterruption | None],
    ) -> AsyncGenerator[bytes, None]:
        path = f"{BATCH_PATH}/{job.job_id}"
        prelude = _Call(
            "results_prelude", "GET", path, job, source_id=source_id, check_status=check
        )
        content = _Call(
            "results_content",
            "GET",
            path + "/results",
            job,
            source_id=source_id,
            before_send=admit,
        )
        client = await self._execution_client()
        context = client.messages.batches.with_streaming_response.results(job.job_id)
        response = await self._execute([prelude, content], context.__aenter__)
        stream = response.http_response.aiter_raw()
        try:
            if prelude.status is None or content.headers_sha256 is None:
                raise IntegrityError(
                    "Anthropic Batch SDK did not follow its guarded results sequence"
                )
            while True:
                try:
                    chunk = await self._quiet(lambda: anext(stream))
                except StopAsyncIteration:
                    break
                yield chunk
        except GeneratorExit:
            content.interruption = on_close()
            raise
        except (
            SourceError,
            BudgetExhausted,
            TimeoutError,
            asyncio.CancelledError,
        ) as error:
            content.interruption = self._interruption(error)
            raise
        except IntegrityError:
            content.interruption = "unsafe_response"
            raise
        except Exception:
            content.interruption = "transport_error"
            raise OSError("Anthropic Batch result stream interrupted") from None
        finally:
            try:
                try:
                    await self._close(stream)
                finally:
                    await self._quiet(lambda: context.__aexit__(None, None, None))
            except Exception:
                content.interruption = "cleanup_failed"
                raise PersistenceError(
                    "Anthropic Batch stream cleanup failed"
                ) from None
            finally:
                if content.headers_sha256 is not None:
                    self._diagnostic(content)

    async def aclose(self) -> None:
        if self._client is not None:
            client, self._client = self._client, None
            await self._quiet(client.close)
