"""Vertex nonstreaming adapters over committed production request bytes.

The SDK is loaded only at an explicitly invoked execution boundary. Authentication
headers never enter preparation, response derivatives, or error messages.
"""

from __future__ import annotations

# pyright: reportPrivateUsage=false
import asyncio
import importlib
import logging
import re
import ssl
import time
from collections import deque
from collections.abc import Callable
from contextvars import ContextVar
from importlib.metadata import PackageNotFoundError, version
from typing import Any, Literal, cast

from judgmentllm import vertex_types
from judgmentllm._auth import _resolve_credentials
from judgmentllm.config import RunSpecification
from judgmentllm.evidence.reasoning import ReasoningBlockMetadata
from judgmentllm.evidence.usage import (
    InvocationFailureCode,
)
from judgmentllm.output_contracts import decode_assessment_output
from judgmentllm.planning import CapabilityProfile, validate_run
from judgmentllm.preparation.session import RequestDependency
from judgmentllm.prompts import RenderedAssessmentPrompt
from judgmentllm.providers.evidence import (
    ProviderEvidence,
    ProviderObservationContext,
    ProviderReasoningBlock,
)
from judgmentllm.providers.protocol import (
    ParsedResponse,
    PreparedRequest,
    ProviderAcknowledgment,
    ProviderError,
    ProviderIntegrityError,
    ProviderInvocationCancelled,
    ProviderResponse,
    ReconciliationEvidence,
    RetrievalUnavailable,
    SubmissionUncertain,
    TransportNotSubmitted,
)
from judgmentllm.providers.vertex_response import (
    _encoded,
    _extract_assessment_component,
    _generate_content_output,
    _mapping,
    _parts,
    _safe_original_envelope,
    _usage,
)

_SDK_LOGGING: ContextVar[bool] = ContextVar(
    "judgmentllm_vertex_sensitive_call", default=False
)
_INVOKING: ContextVar[PreparedRequest | None] = ContextVar(
    "judgmentllm_vertex_request", default=None
)
_SERIALIZATION_STARTED: ContextVar[float] = ContextVar(
    "judgmentllm_vertex_serialization", default=0.0
)
_INVOCATION_STATE: ContextVar[dict[str, Any] | None] = ContextVar(
    "judgmentllm_vertex_invocation", default=None
)
_PROTOCOL = "judgmentllm-vertex-observation-v1"
_MAX_RESPONSE_BYTES = 8_388_608


class VertexConfigurationError(ProviderError):
    """Actionable fixed field names without provider or assessment text."""

    def __init__(self, fields: tuple[str, ...]) -> None:
        allowed = {
            "schema_version",
            "execution.mode",
            "execution.grouping",
            "execution.explanations",
            "execution.cache_policy",
            "execution.tier",
            "allow_ties",
            "generation",
            "generation.temperature",
            "generation.top_p",
            "generation.max_output_tokens",
            "prompt.structured_output",
            "prompt.output_order",
        }
        selected = sorted(
            {field if field in allowed else "configuration" for field in fields}
        )
        super().__init__(
            "Unsupported or unverified Vertex settings: "
            + ", ".join(selected)
            + "; revise these settings or obtain reviewed exact-scope evidence"
        )


class VertexReadinessError(ProviderError):
    """Fixed actionable errors before creating a run or submitting a request."""

    def __init__(
        self,
        code: Literal["missing_credentials", "missing_extra", "sdk_scope_mismatch"],
    ):
        messages = {
            "missing_credentials": "Set JUDGMENTLLM_VERTEX_API_KEY for explicitly selected Vertex api_key mode; ADC never falls back to a key",
            "missing_extra": "Install judgmentllm[google] before Vertex execution",
            "sdk_scope_mismatch": "The installed Vertex SDK differs from the qualified transport version; install google-genai==2.34.0",
        }
        super().__init__(messages[code])
        self.diagnostic_code = code


class VertexRequestRejected(TransportNotSubmitted):
    """Authoritative HTTP rejection with only a fixed nonsensitive classification."""

    def __init__(self, code: InvocationFailureCode) -> None:
        super().__init__(
            "Vertex rejected the request before assessment acceptance",
            diagnostic_code=code,
        )


class _ExecutionLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return _INVOKING.get() is None and not _SDK_LOGGING.get()


def _sdk() -> tuple[Any, Any]:
    try:
        sdk = importlib.import_module("google.genai")
        http = importlib.import_module("httpx2")
    except ImportError:
        raise ProviderError(
            "Install judgmentllm[google] to execute Vertex requests"
        ) from None
    # The SDK can log request options at DEBUG. Suppress them within our calls,
    # without changing logging for unrelated SDK users in this process.
    for name in (
        "google_genai",
        "google_genai._api_client",
        "google_genai.models",
        "google_genai.types",
        "google_genai._transformers",
        "google_genai._common",
        "google_genai._extra_utils",
        "google.auth",
        "google.auth._default",
        "google.auth._cloud_sdk",
        "google.auth.external_account",
        "google.auth.identity_pool",
        "google.auth.impersonated_credentials",
        "google.auth.compute_engine.credentials",
        "google.auth.compute_engine._metadata",
        "google.auth.transport.requests",
        "urllib3.connectionpool",
        "httpx2",
        "httpcore2",
        "httpcore2.connection",
        "httpcore2.http11",
        "httpcore2.http2",
        "httpcore2.proxy",
        "httpcore2.socks",
    ):
        logger = logging.getLogger(name)
        if not any(isinstance(item, _ExecutionLogFilter) for item in logger.filters):
            logger.addFilter(_ExecutionLogFilter())
    return sdk, http


def vertex_sdk_version() -> str:
    """Read distribution metadata without constructing an SDK client."""
    try:
        return version("google-genai")
    except PackageNotFoundError:
        raise ProviderError(
            "Install judgmentllm[google] to execute Vertex requests"
        ) from None


def _protected_client(
    sdk: Any,
    http: Any,
    *,
    runtime: Any,
    key: str | None,
    http_client: Any,
) -> Any:
    """Explicit enterprise mode and reviewed host; SDK ADC stays lazy.

    An explicit project/location suppresses Developer key fallthrough in pinned
    2.34.0. ADC is resolved by the official chain only inside generate_content.
    Key mode passes only the independently resolved Vertex key, with no ADC.
    """
    import certifi
    from google.genai.client import DebugConfig

    def deny_sync(request: Any) -> Any:
        raise ProviderIntegrityError("Vertex synchronous I/O is unavailable")

    sync_client = http.Client(
        transport=http.MockTransport(deny_sync), trust_env=False, follow_redirects=False
    )
    tls = ssl.create_default_context(cafile=certifi.where())
    try:
        client = sdk.Client(
            enterprise=True,
            vertexai=True,
            project=runtime.project,
            location=runtime.location,
            api_key=key,
            debug_config=DebugConfig(
                client_mode=None, replays_directory=None, replay_id=None
            ),
            http_options=sdk.types.HttpOptions(
                base_url=runtime.base_url,
                api_version="v1beta1",
                headers={"User-Agent": "JudgmentLLM/Vertex"},
                timeout=60_000,
                httpx_client=sync_client,
                httpx_async_client=http_client,
                client_args={"verify": tls, "trust_env": False},
                async_client_args={"verify": tls, "ssl": tls, "trust_env": False},
                retry_options=sdk.types.HttpRetryOptions(attempts=1),
            ),
        )
        if (
            not client.vertexai
            or client._api_client.project != runtime.project
            or client._api_client.location != runtime.location
            or client._api_client.api_key != key
            or client._api_client._credentials is not None
            or client._api_client._use_aiohttp()
        ):
            raise ProviderIntegrityError("Vertex authentication source differs")
        client._judgmentllm_sync_client = sync_client
        client._judgmentllm_http_client = http_client
        return client
    except Exception:
        sync_client.close()
        raise ProviderIntegrityError("Vertex client initialization failed") from None


async def _close_client(client: Any) -> None:
    try:
        await client.aio.aclose()
    finally:
        try:
            await client._judgmentllm_http_client.aclose()
        finally:
            client._judgmentllm_sync_client.close()
            client.close()


def _sdk_arguments(sdk: Any, prepared: Any) -> dict[str, Any]:
    """Typed SDK inputs regenerated solely from endpoint-owned sealed fields."""
    values: dict[str, Any] = {
        "candidate_count": 1,
        "automatic_continuation": False,
        "automatic_function_calling": sdk.types.AutomaticFunctionCallingConfig(
            disable=True
        ),
        "should_return_http_response": True,
    }
    if prepared.generation is not None:
        values.update(prepared.generation.model_dump(mode="json", exclude_none=True))
    fragment = prepared.output_commitment.sdk_config_fragment_text
    if fragment is not None:
        config = _mapping(decode_assessment_output(fragment))
        if "response_schema" in config:
            config["response_schema"] = sdk.types.Schema.model_validate(
                config["response_schema"]
            )
        values.update(config)
    return {
        "model": prepared.model,
        "contents": [
            sdk.types.Content(
                role="user", parts=[sdk.types.Part(text=prepared.rendered_text)]
            )
        ],
        "config": sdk.types.GenerateContentConfig(**values),
    }


def _contains_credential(raw: bytes, key: str) -> bool:
    """Do not retain or hash a credential echoed in a provider response."""
    if key.encode("utf-8") in raw:
        return True
    # Assessment JSON is itself carried in a JSON string. Inspect escaped key
    # characters before duplicate-safe parsing, including malformed envelopes
    # that must never retain an encoded credential in a diagnostic component.
    try:
        text = raw.decode("utf-8")
    except UnicodeError:
        return False
    for _ in range(4):
        decoded = re.sub(
            r"\\+u([0-9a-fA-F]{4})", lambda match: chr(int(match[1], 16)), text
        )
        if key in decoded:
            return True
        if decoded == text:
            break
        text = decoded
    else:
        return True  # A repeatedly escaped envelope cannot be retained safely.
    try:
        pending = [decode_assessment_output(raw)]
    except (ValueError, TypeError, RecursionError):
        return False
    while pending:
        value = pending.pop()
        if isinstance(value, str) and key in value:
            return True
        if isinstance(value, dict):
            mapping = cast(dict[str, object], value)
            pending.extend(mapping)
            pending.extend(mapping.values())
        elif isinstance(value, list):
            pending.extend(cast(list[object], value))
    return False


def _response_parts(body: dict[str, Any]) -> list[object]:
    return _parts(body)[1]


def _invalid_api_key(raw: bytes | None) -> bool:
    """Recognize Google's documented HTTP-400 credential code, never its prose."""
    if raw is None:
        return False
    try:
        error = _mapping(_mapping(decode_assessment_output(raw)).get("error"))
    except (ValueError, TypeError, RecursionError):
        return False
    details = error.get("details")
    if (
        type(error.get("code")) is not int
        or error["code"] != 400
        or error.get("status") != "INVALID_ARGUMENT"
        or not isinstance(details, list)
    ):
        return False
    for value in cast(list[object], details):
        if not isinstance(value, dict):
            continue
        detail = _mapping(cast(object, value))
        if (
            detail.get("@type") == "type.googleapis.com/google.rpc.ErrorInfo"
            and detail.get("reason") == "API_KEY_INVALID"
            and detail.get("domain") == "googleapis.com"
        ):
            return True
    return False


class VertexAdapter:
    """One endpoint/model scoped async client; storage and SDK retries disabled.

    ``http_transport`` is an injected HTTP transport for deterministic SDK tests,
    never a configurable base URL or arbitrary request-body escape hatch.
    """

    def __init__(
        self,
        endpoint: str,
        model: str,
        *,
        context: vertex_types.VertexProductionContext | None = None,
        concurrency: int = 4,
        http_transport: object | None = None,
        submission_guard: Callable[[PreparedRequest], None] | None = None,
    ) -> None:
        if (
            endpoint != "models.generate_content"
            or type(concurrency) is not int
            or concurrency < 1
        ):
            raise ProviderError("Unsupported Vertex endpoint or execution concurrency")
        if context is None:
            from judgmentllm.vertex_transport import production_context_for_identity

            context = production_context_for_identity("vertex", endpoint, model)
        self.endpoint = endpoint
        self.model = model
        self.transport_context = context
        if (context.scope.provider, context.scope.endpoint, context.scope.model) != (
            "vertex",
            endpoint,
            model,
        ):
            raise ProviderIntegrityError(
                "Vertex adapter context differs from requested scope"
            )
        self._concurrency = concurrency
        self._http_transport = http_transport
        self._submission_guard = submission_guard
        self._client: Any = None
        self._responses: dict[str, tuple[str, bytes, ProviderResponse]] = {}
        self._invoked: deque[str] = deque(maxlen=2 * concurrency)
        self.actual_call_count = 0
        self.serialization_seconds = 0.0

    @staticmethod
    def capabilities_for(
        provider: str,
        endpoint: str,
        model: str,
        *,
        mechanism: str | None = None,
    ) -> CapabilityProfile:
        if (provider, endpoint) != ("vertex", "models.generate_content"):
            raise ProviderError("Unsupported Vertex provider endpoint")
        # Live describes implemented local orchestration. Model assessment
        # behavior remains unknown without reviewed exact-scope evidence.
        from judgmentllm.native_schema import NativeProjectionScopeV1
        from judgmentllm.vertex_registry import production_generation_capabilities

        return CapabilityProfile(
            explanations=None,
            grouped=None,
            ties=None,
            modes=("live",),
            generation=production_generation_capabilities(
                NativeProjectionScopeV1(
                    provider="vertex",
                    endpoint=endpoint,
                    model=model,
                    model_scope="exact",
                    sdk_package="google-genai",
                    sdk_version="2.34.0",
                    api_version="v1beta1",
                )
            ),
        )

    def capabilities(self, spec: RunSpecification) -> CapabilityProfile:
        return self.capabilities_for(
            spec.provider,
            spec.endpoint,
            spec.model,
            mechanism=getattr(spec.prompt, "native_mechanism", None),
        )

    def validate(self, spec: RunSpecification) -> None:
        if (spec.provider, spec.endpoint, spec.model) != (
            "vertex",
            self.endpoint,
            self.model,
        ):
            raise ProviderIntegrityError("Vertex adapter configuration scope differs")
        if spec.schema_version != 3 or spec.execution.mode != "live":
            raise VertexConfigurationError(("schema_version", "execution.mode"))
        from judgmentllm.vertex_transport import _request_policy

        try:
            _request_policy(spec)
        except ValueError:
            raise VertexConfigurationError(
                ("execution.cache_policy", "execution.tier")
            ) from None
        report = validate_run(spec, capabilities=self.capabilities(spec))
        if not report.valid:
            raise VertexConfigurationError(
                tuple(
                    issue.field for issue in report.issues if issue.severity == "error"
                )
            )
        concurrency = spec.execution.concurrency
        if self._client is not None and concurrency != self._concurrency:
            raise ProviderIntegrityError(
                "Vertex client concurrency differs from the sealed run"
            )
        if self._client is None and concurrency != self._concurrency:
            self._concurrency = concurrency
            self._invoked = deque(self._invoked, maxlen=2 * concurrency)

    def prepare(
        self,
        spec: RunSpecification,
        dependency: RequestDependency,
        rendered: RenderedAssessmentPrompt,
    ) -> PreparedRequest:
        raise ProviderIntegrityError(
            "Production preparation requires its durable physical attempt identity"
        )

    def validate_credentials(self) -> None:
        """Explicit execution preflight; never used by offline validation/preview."""
        self._check_authority()
        try:
            _resolve_credentials(
                "vertex", vertex_mode=self.transport_context.runtime.auth_mode
            )
        except ValueError:
            raise VertexReadinessError("missing_credentials") from None
        try:
            installed = vertex_sdk_version()
        except ProviderError:
            raise VertexReadinessError("missing_extra") from None
        if installed != self.transport_context.scope.sdk_version:
            raise VertexReadinessError("sdk_scope_mismatch")

    def _check_authority(self) -> None:
        if self.transport_context.kind == "vertex-qualification-candidate-v1":
            from judgmentllm.vertex_qualification_execution import (
                _QualificationSubmissionGuard,
            )

            if type(self._submission_guard) is not _QualificationSubmissionGuard:
                raise ProviderIntegrityError(
                    "Offline Vertex qualification candidates cannot execute without the dedicated Vertex executor authority"
                )
            cast(Any, self._submission_guard).verify_context(self.transport_context)
        elif (
            self.transport_context.kind != "vertex-production-v1"
            or self._submission_guard is not None
        ):
            raise ProviderIntegrityError(
                "Vertex production execution cannot use qualification authority"
            )

    async def _execution_client(self) -> Any:
        self._check_authority()
        if self._client is not None:
            return self._client
        sdk, http = _sdk()
        runtime = self.transport_context.runtime
        try:
            resolved = _resolve_credentials("vertex", vertex_mode=runtime.auth_mode)
            key = resolved._api_key() if runtime.auth_mode == "api_key" else None
        except ValueError:
            raise TransportNotSubmitted(
                "Configure JUDGMENTLLM_VERTEX_API_KEY for explicit Vertex api_key mode; no ADC fallback is available"
            ) from None
        adapter = self
        underlying: Any = self._http_transport or http.AsyncHTTPTransport(
            retries=0,
            trust_env=False,
            limits=http.Limits(
                max_connections=self._concurrency,
                max_keepalive_connections=self._concurrency,
            ),
        )

        class CommittedHTTPTransport(http.AsyncBaseTransport):
            async def handle_async_request(self, request: Any) -> Any:
                prepared = _INVOKING.get()
                started = _SERIALIZATION_STARTED.get()
                state = _INVOCATION_STATE.get()
                checks = {
                    "credential": (
                        request.headers.get("x-goog-api-key") == key
                        and "authorization" not in request.headers
                        if runtime.auth_mode == "api_key"
                        else "x-goog-api-key" not in request.headers
                        and adapter._client._api_client._credentials is not None
                        and bool(adapter._client._api_client._credentials.token)
                        and request.headers.get("authorization")
                        == "Bearer " + adapter._client._api_client._credentials.token
                    ),
                    "quota_project": (
                        "x-goog-user-project" not in request.headers
                        if runtime.auth_mode == "api_key"
                        else request.headers.get("x-goog-user-project")
                        == getattr(
                            adapter._client._api_client._credentials,
                            "quota_project_id",
                            None,
                        )
                    ),
                    "prepared_request": prepared is not None,
                    "method": request.method == "POST",
                    "endpoint": request.url
                    == http.URL(
                        runtime.base_url
                        + "v1beta1/"
                        + runtime.resource_path(adapter.model)
                        + ":generateContent"
                    ),
                    "headers": not (
                        set(request.headers)
                        - {
                            "host",
                            "accept",
                            "accept-encoding",
                            "connection",
                            "content-type",
                            "user-agent",
                            "x-goog-api-key",
                            "authorization",
                            "x-goog-user-project",
                            "x-goog-api-client",
                            "content-length",
                            "x-server-timeout",
                        }
                    ),
                    "single_attempt": state is not None and not state.get("invoked"),
                    "body": prepared is not None
                    and request.content == prepared.payload,
                }
                if not all(checks.values()):
                    raise ProviderIntegrityError(
                        "Vertex SDK request violates sealed transport: "
                        + ", ".join(name for name, valid in checks.items() if not valid)
                    )
                assert prepared is not None and state is not None
                # The SDK supplies a scalar timeout per request, overriding the
                # client's finer defaults. Enforce the sealed adapter policy at
                # the final transport boundary; these are local I/O settings.
                request.extensions["timeout"] = {
                    "connect": 10.0,
                    "write": 10.0,
                    "pool": 10.0,
                    "read": 60.0,
                }
                adapter.serialization_seconds += time.perf_counter() - started
                adapter._check_authority()
                if adapter._submission_guard is not None:
                    from judgmentllm.vertex_qualification_execution import (
                        _verify_quota_project,
                    )

                    _verify_quota_project(
                        adapter.transport_context,
                        request.headers.get("x-goog-user-project"),
                    )
                    adapter._submission_guard(prepared)
                # Screen against the exact secrets forwarded, even if another
                # ADC refresh rotates/clears the credentials before response.
                secrets = [runtime.project]
                if key is not None:
                    secrets.append(key)
                authorization = request.headers.get("authorization")
                if authorization is not None:
                    secrets.append(authorization.removeprefix("Bearer "))
                quota_project = request.headers.get("x-goog-user-project")
                if quota_project is not None:
                    secrets.append(quota_project)
                state["invoked"] = True
                adapter.actual_call_count += 1
                response = await underlying.handle_async_request(request)
                if isinstance(response, http.Response) and response.status_code == 403:
                    # The sealed HTTP status establishes rejection independently
                    # of its body. Discard it before reading, parsing or hashing.
                    try:
                        await response.aclose()
                    finally:
                        raise VertexRequestRejected("permission_denied") from None
                body = bytearray()
                try:
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > _MAX_RESPONSE_BYTES:
                            raise SubmissionUncertain(
                                "Vertex response exceeds the bounded retention limit",
                                invoked=True,
                            )
                        body.extend(chunk)
                finally:
                    await response.aclose()
                if any(_contains_credential(bytes(body), secret) for secret in secrets):
                    raise SubmissionUncertain(
                        "Vertex response cannot be retained safely", invoked=True
                    )
                state["raw"] = bytes(body)
                headers = {
                    key: value
                    for key, value in response.headers.items()
                    if key.lower() not in {"content-encoding", "content-length"}
                }
                return http.Response(
                    response.status_code,
                    content=bytes(body),
                    headers=headers,
                    request=request,
                )

            async def aclose(self) -> None:
                await underlying.aclose()

        client = http.AsyncClient(
            transport=CommittedHTTPTransport(),
            timeout=http.Timeout(60.0, connect=10.0, write=10.0, pool=10.0),
            follow_redirects=False,
            trust_env=False,
        )
        try:
            self._client = _protected_client(
                sdk, http, runtime=runtime, key=key, http_client=client
            )
        except Exception:
            await client.aclose()
            raise ProviderIntegrityError(
                "Vertex client initialization failed"
            ) from None
        return self._client

    async def submit(
        self, prepared: PreparedRequest, *, submission_id: str, correlation_id: str
    ) -> ProviderAcknowledgment:

        if not isinstance(prepared, vertex_types.PreparedRequestV7) or (
            prepared.provider,
            prepared.endpoint,
            prepared.model,
        ) != ("vertex", self.endpoint, self.model):
            raise ProviderIntegrityError(
                "Vertex submission requires its sealed production request"
            )
        prepared = vertex_types.PreparedRequestV7.model_validate(dict(prepared))
        from judgmentllm.vertex_transport import production_context_digest

        if (
            prepared.transport_commitment.context_kind != self.transport_context.kind
            or prepared.transport_commitment.context_sha256
            != production_context_digest(self.transport_context)
            or prepared.transport_plan.identity.submission_id != submission_id
            or prepared.transport_plan.identity.correlation_id != correlation_id
            or vertex_sdk_version() != self.transport_context.scope.sdk_version
        ):
            raise ProviderIntegrityError(
                "Vertex sealed transport scope or invocation identity differs"
            )
        self._check_authority()
        # Production native remains unavailable. Only the dedicated exact-plan
        # executor can probe native candidate requests, consumed at HTTP forwarding.
        if (
            prepared.output_commitment.selection.mechanism != "text"
            and self.transport_context.kind != "vertex-qualification-candidate-v1"
        ):
            raise ProviderIntegrityError(
                "Vertex native execution requires independent reviewed qualification"
            )
        if submission_id in self._invoked or len(self._responses) >= self._concurrency:
            raise ProviderIntegrityError(
                "Vertex invocation identity was reused or handoff capacity exceeded"
            )
        self._invoked.append(submission_id)
        logging_token = _SDK_LOGGING.set(True)
        try:
            client = await self._execution_client()
        finally:
            _SDK_LOGGING.reset(logging_token)
        invocation_state: dict[str, Any] = {"invoked": False}
        invocation_token = _INVOCATION_STATE.set(invocation_state)
        token = _INVOKING.set(prepared)
        serialization_token = _SERIALIZATION_STARTED.set(time.perf_counter())
        try:
            sdk, _ = _sdk()
            await asyncio.wait_for(
                client.aio.models.generate_content(**_sdk_arguments(sdk, prepared)),
                timeout=90.0,
            )
            raw = cast(bytes, invocation_state["raw"])
        except asyncio.CancelledError:
            raise ProviderInvocationCancelled(
                invoked=invocation_state["invoked"]
            ) from None
        except Exception as error:
            if isinstance(
                error,
                (ProviderIntegrityError, SubmissionUncertain, TransportNotSubmitted),
            ):
                raise
            cause = error.__cause__
            if isinstance(
                cause,
                (ProviderIntegrityError, SubmissionUncertain, TransportNotSubmitted),
            ):
                raise cause from None
            if not invocation_state["invoked"]:
                raise TransportNotSubmitted(
                    "Vertex credentials or request preparation are unavailable before forwarding; check the selected authentication mode and ADC setup"
                ) from None
            status = getattr(error, "code", None)
            if status in {400, 401, 403, 404, 422, 429}:
                code = {
                    401: "credentials_rejected",
                    403: "permission_denied",
                    404: "model_or_endpoint_unavailable",
                    429: "rate_limited",
                }.get(status, "request_rejected")
                if status == 400 and _invalid_api_key(invocation_state.get("raw")):
                    code = "credentials_rejected"
                raise VertexRequestRejected(cast(InvocationFailureCode, code)) from None
            # No network exception establishes whether a provider accepted work.
            raise SubmissionUncertain(
                "Vertex acceptance is unresolved; no request was repeated",
                invoked=invocation_state["invoked"],
            ) from None
        finally:
            _INVOKING.reset(token)
            _INVOCATION_STATE.reset(invocation_token)
            _SERIALIZATION_STARTED.reset(serialization_token)
        try:
            document = _mapping(decode_assessment_output(raw))
            remote_id = document.get("responseId")
            if not isinstance(remote_id, str) or not remote_id.strip():
                raise ValueError
            response = self._response(raw, document, remote_id, prepared=prepared)
        except (ValueError, TypeError):
            raise SubmissionUncertain(
                "Vertex response identity is unavailable", invoked=True
            ) from None
        self._responses[submission_id] = (correlation_id, prepared.payload, response)
        return ProviderAcknowledgment(remote_id=remote_id, observation_key=remote_id)

    def _response(
        self,
        raw: bytes,
        body: dict[str, Any],
        remote_id: str,
        *,
        prepared: PreparedRequest | None = None,
    ) -> ProviderResponse:
        try:
            kind, text, completion_status = _generate_content_output(body)
        except (ValueError, TypeError):
            kind, text, completion_status = "provider_error", None, "unsupported_output"
        try:
            usage = _usage(self.endpoint, body.get("usageMetadata"))
        except ValueError:
            # Invalid provider counters cannot invalidate otherwise useful output.
            usage = None
        # Companion evidence is optional for replay of diagnostic-free observations.
        try:
            component = _extract_assessment_component(body, self.endpoint)
        except (ValueError, TypeError):
            component = vertex_types.VertexAssessmentComponentV4(
                endpoint="models.generate_content",
                extraction_status="unsupported_components",
                unsupported_present=True,
            )
        if component.extraction_status == "oversized":
            kind, text, completion_status = "provider_error", None, "unsupported_output"
        text_bytes = None
        encoding_safe = True
        if text is not None:
            try:
                text_bytes = text.encode("utf-8")
            except UnicodeError:
                # A valid JSON envelope can contain an escaped unpaired
                # surrogate. It is no UTF-8 assessment component: preserve the
                # observation and failure metadata without repairing its text.
                text = None
                encoding_safe = False
        envelope = {
            "protocol": _PROTOCOL,
            "observation_key": remote_id,
            "kind": kind,
            "assessment_text": text,
        }
        try:
            exact_safe = _safe_original_envelope(
                body,
                self.endpoint,
                _mapping(decode_assessment_output(prepared.payload))
                if prepared is not None
                else None,
            )
        except (ValueError, TypeError):
            exact_safe = False
        exact_safe = (
            exact_safe
            and encoding_safe
            and component.extraction_status not in {"oversized", "invalid_utf8"}
        )
        safe = raw if exact_safe else _encoded(envelope)
        reported = (
            body.get("modelVersion")
            if isinstance(body.get("modelVersion"), str)
            else None
        )
        return ProviderResponse.model_validate(
            {
                "observation_key": remote_id,
                "kind": kind,
                "raw": raw,
                "evidence": ProviderEvidence(
                    provenance="provider_documented",
                    assessment_component=component,
                    usage=usage,
                    reasoning=tuple(
                        ProviderReasoningBlock(
                            metadata=ReasoningBlockMetadata(
                                kind="other_documented",
                                provider_field="vertex.generate_content.thought",
                                block_ordinal=index,
                            )
                        )
                        for index, part in enumerate(_response_parts(body))
                        if isinstance(part, dict)
                        and cast(dict[str, Any], part).get("thought") is True
                    ),
                    output_text=text_bytes,
                    output_source="raw_output_text"
                    if text_bytes is not None
                    else "unavailable",
                    safe_response=safe,
                    original_body_safe=exact_safe,
                    observation_context=ProviderObservationContext(
                        reported_model=reported,
                        sdk_version=vertex_sdk_version(),
                        remote_response_id=remote_id,
                        invocation_kind="live",
                        provider_outcome=cast(Any, completion_status),
                    ),
                ),
            }
        )

    def _received_response_for_cancellation(
        self, prepared: PreparedRequest, *, submission_id: str, correlation_id: str
    ) -> ProviderResponse | None:
        """Take only an already-received bounded handoff; never perform I/O."""
        saved = self._responses.get(submission_id)
        if saved is None:
            return None
        if (saved[0], saved[1]) != (correlation_id, prepared.payload):
            raise ProviderIntegrityError("Vertex retained observation identity differs")
        self._responses.pop(submission_id)
        return saved[2]

    async def retrieve(
        self, prepared: PreparedRequest, *, submission_id: str, correlation_id: str
    ) -> ProviderResponse:
        saved = self._received_response_for_cancellation(
            prepared, submission_id=submission_id, correlation_id=correlation_id
        )
        if saved is None:
            raise RetrievalUnavailable(
                "Vertex non-retrievable response is unavailable; resubmission is prohibited"
            )
        return saved

    async def reconcile(
        self, prepared: PreparedRequest, *, submission_id: str, correlation_id: str
    ) -> ReconciliationEvidence:
        saved = self._responses.get(submission_id)
        if saved is None:
            return ReconciliationEvidence(outcome="unresolved")
        if (saved[0], saved[1]) != (correlation_id, prepared.payload):
            raise ProviderIntegrityError("Vertex reconciliation identity differs")
        return ReconciliationEvidence(
            outcome="observation_available",
            remote_id=saved[2].observation_key,
            observation_key=saved[2].observation_key,
        )

    def parse(
        self, response: ProviderResponse, *, strict_output: bool = False
    ) -> ParsedResponse:
        try:
            if response.evidence is not None:
                safe = response.evidence.safe_response
                if safe is None:
                    raise ValueError
            else:
                safe = response.raw
            body = _mapping(decode_assessment_output(safe))
            if body.get("protocol") == _PROTOCOL:
                if (body.get("observation_key"), body.get("kind")) != (
                    response.observation_key,
                    response.kind,
                ):
                    raise ValueError
                text = body.get("assessment_text")
            else:
                kind, text, _ = _generate_content_output(body)
                if (body.get("responseId"), kind) != (
                    response.observation_key,
                    response.kind,
                ):
                    raise ValueError
            output = (
                decode_assessment_output(text)
                if response.kind == "response" and isinstance(text, (str, bytes))
                else None
            )
            return ParsedResponse.model_validate(
                {"kind": response.kind, "output": output}
            )
        except (ValueError, TypeError, KeyError):
            raise ValueError("Vertex assessment output is invalid") from None

    async def aclose(self) -> None:
        self._responses.clear()
        if self._client is not None:
            await _close_client(self._client)
            self._client = None
