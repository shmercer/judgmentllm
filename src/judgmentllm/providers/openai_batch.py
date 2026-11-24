"""Official SDK Files/Batch transport with one guarded forward per intent.

Only typed approved projections leave this module. SDK retries, ambient routing,
redirects and automatic successors are disabled.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from contextvars import ContextVar
from email.parser import BytesParser
from email.policy import default
from typing import Any, cast

from judgmentllm._auth import _resolve_credentials
from judgmentllm.batch_models import (
    BatchOperationReceiptV1,
    OpenAIBatchContextV1,
    OpenAIBatchStatusV1,
)
from judgmentllm.config import RunSpecification
from judgmentllm.openai_batch_transport import (
    ROUTES,
    batch_context,
    compile_request,
    require_openai_batch,
    require_sdk,
    scope_for,
)
from judgmentllm.output_commitments import ordered_json_bytes
from judgmentllm.output_contracts import decode_assessment_output
from judgmentllm.persistence.hashing import (
    bytes_digest,
    canonical_bytes,
    semantic_digest,
)
from judgmentllm.planning import CapabilityProfile
from judgmentllm.providers.batch_protocol import (
    BatchEvidenceWithheld,
    BatchSourceOverrun,
)
from judgmentllm.providers.openai import _SDK_LOGGING, _protected_client, _sdk
from judgmentllm.providers.openai_batch_response import parse, safe_id, screen_result
from judgmentllm.providers.openai_response import _mapping
from judgmentllm.providers.protocol import ProviderIntegrityError

# pyright: reportPrivateUsage=false

_EXPIRY = {"anchor": "created_at", "seconds": 2_592_000}
CONTROL_MAX_BYTES = 1_048_576
_CALL: ContextVar[
    tuple[str, str, bytes, Any, Callable[[], None], int, dict[str, bool]] | None
] = ContextVar("openai_batch_authorized_call", default=None)


class OpenAIBatchAdapter:
    identity = "openai-v1"

    def __init__(
        self,
        context: OpenAIBatchContextV1,
        *,
        http_transport: Any = None,
        budget: Any = None,
    ) -> None:
        self._context = context
        self.endpoint, self.model = context.endpoint, context.model
        self._http_transport = http_transport
        self.budget = budget
        self._client: Any = None
        self._key: str | None = None
        self._uploads: dict[str, bytes] = {}
        self.upload_resolver: Callable[[Any], bytes | None] | None = None
        self.reconcile_uploads = True

    def context(self) -> OpenAIBatchContextV1:
        return self._context

    @staticmethod
    def capabilities_for(provider: str, endpoint: str, model: str) -> CapabilityProfile:
        if provider != "openai" or endpoint not in ROUTES:
            raise ValueError("unsupported OpenAI Batch route")
        from judgmentllm.production_registry import production_generation_capabilities

        return CapabilityProfile(
            modes=("batch_native",),
            generation=production_generation_capabilities(scope_for(endpoint, model)),
        )

    def capabilities(self, spec: RunSpecification) -> CapabilityProfile:
        from judgmentllm.providers.reasoning import generation_capabilities

        profile = self.capabilities_for(spec.provider, spec.endpoint, spec.model)
        return profile.model_copy(
            update={
                "generation": generation_capabilities(
                    spec, scope_for(spec.endpoint, spec.model), profile.generation
                )
            }
        )

    def validate(self, spec: Any) -> Any:
        require_openai_batch(spec)
        if batch_context(spec) != self.context():
            raise ValueError("OpenAI Batch scope differs")
        require_sdk()

    def prepare(self, spec: Any, dependency: Any, rendered: Any) -> Any:
        self.validate(spec)
        return compile_request(spec, dependency, rendered)

    parse = staticmethod(parse)

    def screen_result(self, raw: Any, expected_keys: Any) -> Any:
        from judgmentllm.providers.google_response import contains_credential

        screened = screen_result(raw, expected_keys, self.endpoint)
        if screened is not None and self._key is not None:
            if contains_credential(screened[3], self._key):
                return None
        return screened

    def screen_replay(self, raw: Any, expected_keys: Any) -> Any:
        return screen_result(raw, expected_keys, self.endpoint, allow_replay=True)

    def control_projection(self, value: Any) -> Any:
        from judgmentllm.providers.google_response import contains_credential

        if self._key is not None and contains_credential(
            canonical_bytes(value), self._key
        ):
            raise BatchEvidenceWithheld("credential-bearing control evidence")
        return value

    def provider_id(self, value: Any, prefix: str) -> str:
        checked = safe_id(value, prefix)
        self.control_projection(checked)
        return checked

    @staticmethod
    def filename(intent: Any) -> Any:
        return "judgmentllm-" + intent.instance_id + ".jsonl"

    def create_payload(self, manifest_sha256: Any, shard: Any, uploaded: Any) -> Any:
        logical = semantic_digest(
            "batch-logical-operation-v1",
            [shard.run_id, shard.shard_id, "create", uploaded.remote_id],
        )
        operation = semantic_digest("batch-operation-instance-v1", [logical, 1])
        return ordered_json_bytes(
            {
                "completion_window": "24h",
                "endpoint": ROUTES[self.endpoint],
                "input_file_id": uploaded.remote_id,
                "metadata": {
                    "judgmentllm_manifest": manifest_sha256,
                    "judgmentllm_shard": shard.shard_id,
                    "judgmentllm_operation": operation,
                },
                "output_expires_after": _EXPIRY,
            }
        )

    @staticmethod
    def cancel_payload(
        manifest_sha256: str, submission_id: str, remote_id: str | None
    ) -> bytes:
        return b""

    async def client(self) -> Any:
        require_sdk()
        if self.budget is None:
            raise ProviderIntegrityError("OpenAI Batch requires a bounded invocation")
        if self._client is not None:
            return self._client
        sdk, http = _sdk()
        # This is deliberately after the actual-runtime guard and local validation.
        key = _resolve_credentials("openai")._api_key()
        self._key = key
        adapter = self
        delegate = self._http_transport or http.AsyncHTTPTransport(retries=0)

        class BoundedStream(http.AsyncByteStream):
            def __init__(self, stream: Any, limit: int) -> None:
                self.stream, self.limit = stream, limit
                self.total = 0

            def receive(self, chunk: bytes) -> None:
                self.total += len(chunk)
                if self.total > self.limit:
                    adapter.budget.bytes += len(chunk)
                    if adapter.budget.bytes > adapter.budget.byte_limit:
                        adapter.budget.closed = True
                    raise BatchSourceOverrun(
                        "OpenAI Batch response exceeds its authorized byte size"
                    )
                adapter.budget.receive_bytes(len(chunk))
                adapter.budget.remaining()

            async def __aiter__(self) -> AsyncIterator[bytes]:
                stream = self.stream.__aiter__()
                while True:
                    try:
                        chunk = await adapter.budget.call(lambda: anext(stream))
                    except StopAsyncIteration:
                        break
                    self.receive(chunk)
                    for offset in range(0, len(chunk), 65_536):
                        yield chunk[offset : offset + 65_536]

            async def aclose(self) -> None:
                await self.stream.aclose()

        class GuardedTransport(http.AsyncBaseTransport):
            async def handle_async_request(self, request: Any) -> Any:
                authorized = _CALL.get()
                if authorized is None:
                    raise ProviderIntegrityError(
                        "OpenAI Batch request has no operation authorization"
                    )
                method, path, query, expected, guard, limit, forwarded = authorized
                if (
                    request.method,
                    request.url.scheme,
                    request.url.host,
                    request.url.port,
                    request.url.path,
                    request.url.query,
                ) != (method, "https", "api.openai.com", None, path, query):
                    raise ProviderIntegrityError("OpenAI Batch routing differs")
                if request.headers.get("authorization") != "Bearer " + key:
                    raise ProviderIntegrityError("OpenAI Batch authorization differs")
                from judgmentllm.providers.google_response import contains_credential

                if contains_credential(request.url.path.encode("utf-8"), key) or any(
                    name in request.headers
                    for name in (
                        "range",
                        "cookie",
                        "proxy-authorization",
                        "x-api-key",
                        "openai-organization",
                        "openai-project",
                    )
                ):
                    raise ProviderIntegrityError("OpenAI Batch headers or path differ")
                body = await request.aread()
                if method == "POST" and path == "/v1/files":
                    content_type = request.headers.get("content-type", "")
                    if len(body) > len(expected[1]) + 16_384:
                        raise ProviderIntegrityError(
                            "OpenAI Batch multipart bound differs"
                        )
                    message = BytesParser(policy=default).parsebytes(
                        b"Content-Type: "
                        + content_type.encode("ascii")
                        + b"\r\n\r\n"
                        + body
                    )
                    fields = {}
                    for part in message.iter_parts():
                        name = part.get_param("name", header="content-disposition")
                        if name in fields:
                            raise ProviderIntegrityError(
                                "duplicate OpenAI Batch multipart field"
                            )
                        fields[name] = (
                            part.get_filename(),
                            part.get_payload(decode=True),
                        )
                    if fields != {
                        "file": (expected[0], expected[1]),
                        "purpose": (None, b"batch"),
                        "expires_after[anchor]": (None, b"created_at"),
                        "expires_after[seconds]": (None, b"2592000"),
                    }:
                        raise ProviderIntegrityError(
                            "OpenAI Batch multipart differs from seal"
                        )
                elif body != expected:
                    raise ProviderIntegrityError(
                        "OpenAI Batch body differs from authorized bytes"
                    )
                if forwarded["sent"]:
                    raise ProviderIntegrityError(
                        "OpenAI Batch forwarding was already consumed"
                    )
                adapter.budget.remaining()
                guard()
                forwarded["sent"] = True
                response = await delegate.handle_async_request(request)
                if not response.is_success:
                    from judgmentllm.lifecycle import _close_adapter  # pyright: ignore[reportPrivateUsage]

                    # Reject status before the SDK eagerly reads error bodies.
                    # Unread bodies are truncated; cached bytes already arrived.
                    try:
                        try:
                            if response.is_stream_consumed:
                                received = len(response.content)
                                adapter.budget.receive_bytes(received)
                                if received > CONTROL_MAX_BYTES:
                                    raise OSError(
                                        "OpenAI Batch HTTP error exceeds control bound"
                                    )
                            adapter.budget.remaining()
                        except BaseException:
                            adapter.budget.closed = True
                            raise
                    finally:
                        try:
                            await _close_adapter(cast(Any, response))
                        except BaseException:
                            adapter.budget.closed = True
                            raise
                    raise OSError("OpenAI Batch HTTP response is unavailable")
                if response.headers.get("content-encoding", "identity") != "identity":
                    await response.aclose()
                    raise OSError("encoded OpenAI Batch response is unsupported")
                stream = BoundedStream(response.stream, limit)
                if response.is_stream_consumed:
                    stream.receive(response.content)
                response.stream = stream
                return response

            async def aclose(self) -> Any:
                await delegate.aclose()

        self._client = _protected_client(
            sdk,
            api_key=key,
            base_url="https://api.openai.com/v1",
            max_retries=0,
            http_client=http.AsyncClient(
                transport=GuardedTransport(),
                trust_env=False,
                follow_redirects=False,
                timeout=30,
                headers={"Accept-Encoding": "identity"},
            ),
        )
        return self._client

    async def _json(
        self,
        method: Any,
        path: Any,
        function: Callable[[Any], Awaitable[Any]],
        *,
        query: Any = b"",
        body: Any = b"",
        guard: Any = lambda: None,
    ) -> dict[str, Any]:
        require_sdk()
        token = _CALL.set(
            (method, path, query, body, guard, CONTROL_MAX_BYTES, {"sent": False})
        )
        logging = _SDK_LOGGING.set(True)
        try:
            response = await function(await self.client())
            self.budget.remaining()
            raw = response.http_response.content
            if len(raw) > CONTROL_MAX_BYTES:
                raise BatchEvidenceWithheld("oversized control projection")
            try:
                return _mapping(decode_assessment_output(raw))
            except (ValueError, TypeError):
                raise BatchEvidenceWithheld("unsafe control representation") from None
        except BatchSourceOverrun:
            raise BatchEvidenceWithheld("oversized control representation") from None
        except (ProviderIntegrityError, BatchEvidenceWithheld):
            raise
        except Exception as error:
            cause = error.__cause__
            if isinstance(cause, BatchSourceOverrun):
                raise BatchEvidenceWithheld(
                    "oversized control representation"
                ) from None
            if isinstance(cause, (ProviderIntegrityError, BatchEvidenceWithheld)):
                raise cause from None
            raise OSError("OpenAI Batch control evidence is unavailable") from None
        finally:
            _SDK_LOGGING.reset(logging)
            _CALL.reset(token)

    def _receipt(self, intent: Any, remote_id: Any) -> Any:
        remote_id = self.provider_id(
            remote_id, "file-" if intent.kind == "upload" else "batch_"
        )
        return canonical_bytes(
            self.control_projection(
                BatchOperationReceiptV1(
                    instance_id=intent.instance_id,
                    intent_sha256=bytes_digest(canonical_bytes(intent)),
                    outcome="accepted",
                    remote_id=remote_id,
                )
            )
        )

    def decode_receipt(self, raw: Any, intent: Any) -> Any:
        receipt = BatchOperationReceiptV1.model_validate_json(raw)
        if (
            (receipt.instance_id, receipt.intent_sha256)
            != (intent.instance_id, bytes_digest(canonical_bytes(intent)))
            or receipt.outcome != "accepted"
            or receipt.fence is not None
        ):
            raise ProviderIntegrityError(
                "OpenAI Batch receipt differs from original intent"
            )
        self.provider_id(
            receipt.remote_id, "file-" if intent.kind == "upload" else "batch_"
        )
        return receipt

    async def invoke(self, intent: Any, payload: Any, guard: Any) -> Any:
        require_sdk()
        data = bytearray()
        async for chunk in payload:
            data.extend(chunk)
        data = bytes(data)
        if bytes_digest(data) != intent.payload_sha256 or intent.sequence != 1:
            raise ProviderIntegrityError("OpenAI Batch operation payload differs")
        if intent.kind == "upload":
            filename = self.filename(intent)
            self.authorize_upload(data)
            body = await self._json(
                "POST",
                "/v1/files",
                lambda c: c.files.with_raw_response.create(
                    file=(filename, data, "application/jsonl"),
                    purpose="batch",
                    expires_after=_EXPIRY,
                ),
                body=(filename, data),
                guard=guard,
            )
            if (
                body.get("filename") != filename
                or body.get("purpose") != "batch"
                or type(body.get("bytes")) is not int
                or body["bytes"] != len(data)
            ):
                raise BatchEvidenceWithheld("unsafe upload acknowledgment")
        elif intent.kind == "create":
            values = _mapping(decode_assessment_output(data))
            expected = {
                "completion_window": "24h",
                "endpoint": ROUTES[self.endpoint],
                "input_file_id": intent.remote_id,
                "metadata": {
                    "judgmentllm_manifest": intent.manifest_sha256,
                    "judgmentllm_shard": intent.shard_id,
                    "judgmentllm_operation": intent.instance_id,
                },
                "output_expires_after": _EXPIRY,
            }
            if values != expected:
                raise ProviderIntegrityError("OpenAI Batch create binding differs")
            # Operation identity is sent as opaque metadata; no assessment text.

            body = await self._json(
                "POST",
                "/v1/batches",
                lambda c: c.batches.with_raw_response.create(**values),
                body=data,
                guard=guard,
            )
            self._screen_job(body, intent.instance_id, intent=intent)
        else:
            job_id = self.provider_id(intent.remote_id, "batch_")
            if data:
                raise ProviderIntegrityError("OpenAI Batch cancel body differs")
            body = await self._json(
                "POST",
                "/v1/batches/" + job_id + "/cancel",
                lambda c: c.batches.with_raw_response.cancel(job_id),
                guard=guard,
            )
            if body.get("id") != job_id:
                raise BatchEvidenceWithheld("unsafe cancel acknowledgment")
        return self._receipt(intent, body.get("id"))

    def _screen_job(
        self, body: dict[str, Any], submission_id: Any, *, intent: Any = None
    ) -> Any:
        try:
            remote = self.provider_id(body.get("id"), "batch_")
            input_file = self.provider_id(body.get("input_file_id"), "file-")
            if body.get("endpoint") != ROUTES[self.endpoint]:
                raise ValueError
            if intent is not None and (
                input_file != intent.remote_id
                or body.get("metadata")
                != {
                    "judgmentllm_manifest": intent.manifest_sha256,
                    "judgmentllm_shard": intent.shard_id,
                    "judgmentllm_operation": intent.instance_id,
                }
            ):
                raise ValueError
            state = body.get("status")
            generic = (
                "completed"
                if state == "completed"
                else "failed"
                if state in ("failed", "expired")
                else "cancelled"
                if state == "cancelled"
                else "running"
            )
            return self.control_projection(
                OpenAIBatchStatusV1.model_validate(
                    dict(
                        submission_id=submission_id,
                        remote_id=remote,
                        input_file_id=input_file,
                        endpoint=body["endpoint"],
                        state=generic,
                        provider_state=state,
                        output_file_id=None
                        if body.get("output_file_id") is None
                        else self.provider_id(body["output_file_id"], "file-"),
                        error_file_id=None
                        if body.get("error_file_id") is None
                        else self.provider_id(body["error_file_id"], "file-"),
                    )
                )
            )
        except (ValueError, TypeError, KeyError):
            raise BatchEvidenceWithheld("unsafe job evidence") from None

    async def status(self, submission_id: Any, remote_id: Any) -> Any:
        remote_id = self.provider_id(remote_id, "batch_")
        body = await self._json(
            "GET",
            "/v1/batches/" + remote_id,
            lambda c: c.batches.with_raw_response.retrieve(remote_id),
        )
        status = self._screen_job(body, submission_id)
        if status.remote_id != remote_id:
            raise BatchEvidenceWithheld("job identity differs")
        return status

    async def file_metadata(
        self, file_id: Any, *, purpose: Any = "batch_output"
    ) -> Any:
        file_id = self.provider_id(file_id, "file-")
        body = await self._json(
            "GET",
            "/v1/files/" + file_id,
            lambda c: c.files.with_raw_response.retrieve(file_id),
        )
        if (
            body.get("id") != file_id
            or body.get("purpose") != purpose
            or type(body.get("bytes")) is not int
            or body["bytes"] < 0
        ):
            raise BatchEvidenceWithheld("unsafe Files metadata")
        self.control_projection(
            {"id": file_id, "purpose": purpose, "bytes": body["bytes"]}
        )
        return file_id, purpose, body["bytes"]

    async def content(
        self, file_id: str, byte_size: int
    ) -> AsyncGenerator[bytes, None]:
        file_id = self.provider_id(file_id, "file-")
        require_sdk()
        token = _CALL.set(
            (
                "GET",
                "/v1/files/" + file_id + "/content",
                b"",
                b"",
                lambda: None,
                byte_size,
                {"sent": False},
            )
        )
        logging = _SDK_LOGGING.set(True)
        context = None
        try:
            client = await self.client()
            context = client.files.with_streaming_response.content(file_id)
            response = await context.__aenter__()
        except (ProviderIntegrityError, BatchEvidenceWithheld, BatchSourceOverrun):
            raise
        except Exception as error:
            cause = error.__cause__
            if isinstance(
                cause,
                (ProviderIntegrityError, BatchEvidenceWithheld, BatchSourceOverrun),
            ):
                raise cause from None
            raise OSError("OpenAI Batch file content is unavailable") from None
        finally:
            # A generator's next advance may run in another owned budget task.
            _SDK_LOGGING.reset(logging)
            _CALL.reset(token)
        try:
            async for chunk in response.iter_bytes():
                # Do not buffer the last short chunk waiting for source EOF.
                for offset in range(0, len(chunk), 65_536):
                    yield chunk[offset : offset + 65_536]
        except (ProviderIntegrityError, BatchEvidenceWithheld, BatchSourceOverrun):
            raise
        except Exception:
            raise OSError("OpenAI Batch file content is unavailable") from None
        finally:
            await context.__aexit__(None, None, None)

    async def reconcile_operation(self, intent: Any) -> Any:
        require_sdk()
        if intent.kind == "upload" and not self.reconcile_uploads:
            return None
        if intent.kind == "cancel":
            status = await self.status(intent.instance_id, intent.remote_id)
            return (
                self._receipt(intent, status.remote_id)
                if status.provider_state in ("cancelling", "cancelled")
                else None
            )
        resource = "files" if intent.kind == "upload" else "batches"
        query = b"limit=100&purpose=batch" if resource == "files" else b"limit=100"
        body = await self._json(
            "GET",
            "/v1/" + resource,
            (lambda c: c.files.with_raw_response.list(limit=100, purpose="batch"))
            if resource == "files"
            else (lambda c: c.batches.with_raw_response.list(limit=100)),
            query=query,
        )
        if type(body.get("has_more")) is not bool or body["has_more"]:
            return None
        candidates: list[dict[str, Any]] = []
        items: Any = body.get("data")
        if not isinstance(items, list) or len(cast(list[Any], items)) > 100:
            return None
        for value in cast(list[Any], items):
            item: dict[str, Any] = value
            if not isinstance(value, dict):
                return None
            if resource == "files":
                if (
                    item.get("filename") == self.filename(intent)
                    and item.get("purpose") == "batch"
                ):
                    candidates.append(item)
            else:
                metadata = item.get("metadata")
                if (
                    isinstance(metadata, dict)
                    and cast(dict[str, Any], metadata).get("judgmentllm_operation")
                    == intent.instance_id
                ):
                    candidates.append(item)
        if len(candidates) != 1:
            return None
        found = candidates[0]
        if resource == "files":
            expected = self._uploads.get(intent.payload_sha256)
            if expected is None and self.upload_resolver is not None:
                expected = self.upload_resolver(intent)
            if expected is None:
                return None
            file_id = self.provider_id(found.get("id"), "file-")
            if await self.file_metadata(file_id, purpose="batch") != (
                file_id,
                "batch",
                len(expected),
            ):
                return None
            received = bytearray()
            async for chunk in self.content(file_id, len(expected)):
                if len(received) + len(chunk) > len(expected):
                    return None
                # Compare authorized input bytes before any returned-data digest.
                if chunk != expected[len(received) : len(received) + len(chunk)]:
                    return None
                received.extend(chunk)
            if len(received) != len(expected):
                return None
        else:
            status = self._screen_job(found, intent.instance_id, intent=intent)
            # A positive match still requires the exact identified job retrieval.
            if await self.status(intent.instance_id, status.remote_id) != status:
                return None
        return self._receipt(intent, found.get("id"))

    def authorize_upload(self, raw: bytes) -> None:
        self._uploads = {bytes_digest(raw): raw}

    async def result_source(self, *args: Any, **kwargs: Any) -> Any:
        raise ProviderIntegrityError("OpenAI uses pinned Files sources")

    async def result_index(self, *args: Any, **kwargs: Any) -> Any:
        raise ProviderIntegrityError("OpenAI has no provider prefix index")

    def results(self, *args: Any, **kwargs: Any) -> Any:
        raise ProviderIntegrityError("OpenAI requires screened full-file replay")

    async def aclose(self) -> Any:
        self._uploads.clear()
        self._key = None
        if self._client is not None:
            await self._client.close()
            self._client = None
