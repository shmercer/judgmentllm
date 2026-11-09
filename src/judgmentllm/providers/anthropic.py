"""Anthropic nonstreaming adapters over committed production request bytes.

The SDK is loaded only at an explicitly invoked execution boundary. Authentication
headers never enter preparation, response derivatives, or error messages.
"""

from __future__ import annotations

# pyright: reportPrivateUsage=false
import asyncio
import importlib
import logging
import time
from collections import deque
from collections.abc import Callable
from contextvars import ContextVar
from importlib.metadata import PackageNotFoundError, version
from typing import TYPE_CHECKING, Any, Literal, cast

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
from judgmentllm.providers.anthropic_response import (
    _encoded,
    _extract_assessment_component,
    _mapping,
    _messages_output,
    _safe_original_envelope,
    _usage,
)
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

if TYPE_CHECKING:
    from judgmentllm.anthropic_assessment import AnthropicAssessmentComponentV2
    from judgmentllm.anthropic_transport import AnthropicProductionContext

_SDK_LOGGING: ContextVar[bool] = ContextVar(
    "judgmentllm_anthropic_sensitive_call", default=False
)
_INVOKING: ContextVar[PreparedRequest | None] = ContextVar(
    "judgmentllm_anthropic_request", default=None
)
_SERIALIZATION_STARTED: ContextVar[float] = ContextVar(
    "judgmentllm_anthropic_serialization", default=0.0
)
_INVOCATION_STATE: ContextVar[dict[str, bool] | None] = ContextVar(
    "judgmentllm_anthropic_invocation", default=None
)
_PROTOCOL = "judgmentllm-anthropic-observation-v1"
_MAX_RESPONSE_BYTES = 8_388_608


class AnthropicConfigurationError(ProviderError):
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
            "Unsupported or unverified Anthropic settings: "
            + ", ".join(selected)
            + "; revise these settings or obtain reviewed exact-scope evidence"
        )


class AnthropicReadinessError(ProviderError):
    """Fixed actionable errors before creating a run or submitting a request."""

    def __init__(
        self,
        code: Literal["missing_credentials", "missing_extra", "sdk_scope_mismatch"],
    ):
        messages = {
            "missing_credentials": "Set a valid ANTHROPIC_API_KEY process environment value before execution",
            "missing_extra": "Install judgmentllm[anthropic] before Anthropic execution",
            "sdk_scope_mismatch": "The installed Anthropic SDK differs from the qualified transport version; install anthropic==2.3.0",
        }
        super().__init__(messages[code])
        self.diagnostic_code = code


class AnthropicRequestRejected(TransportNotSubmitted):
    """Authoritative HTTP rejection with only a fixed nonsensitive classification."""

    def __init__(self, code: InvocationFailureCode) -> None:
        super().__init__(
            "Anthropic rejected the request before assessment acceptance",
            diagnostic_code=code,
        )


class _ExecutionLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return _INVOKING.get() is None and not _SDK_LOGGING.get()


def _sdk() -> tuple[Any, Any]:
    try:
        sdk = importlib.import_module("anthropic")
        http = importlib.import_module("httpx2")
    except ImportError:
        raise ProviderError(
            "Install judgmentllm[anthropic] to execute Anthropic requests"
        ) from None
    # The SDK can log request options at DEBUG. Suppress them within our calls,
    # without changing logging for unrelated SDK users in this process.
    for name in (
        "anthropic",
        "anthropic._base_client",
        "anthropic._response",
        "anthropic._tracing",
        "anthropic.lib.credentials._auth",
        "anthropic.lib.credentials._providers",
        "anthropic.lib.credentials._cache",
        "anthropic.lib.credentials._workload",
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


def anthropic_sdk_version() -> str:
    """Read distribution metadata without constructing an SDK client."""
    try:
        return version("anthropic")
    except PackageNotFoundError:
        raise ProviderError(
            "Install judgmentllm[anthropic] to execute Anthropic requests"
        ) from None


def _protected_client(sdk: Any, **options: Any) -> Any:
    """Explicit API-key-only client, with ambient routing/telemetry disabled."""

    class ExplicitCredentialsClient(sdk.AsyncAnthropic):
        @property
        def default_headers(self) -> dict[str, str]:
            return {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "JudgmentLLM/Anthropic",
                "anthropic-version": "2026-10-21",
                **self.auth_headers,
            }

    client = ExplicitCredentialsClient(webhook_key="", open_telemetry=False, **options)
    client._platform = "Unknown"
    client._custom_headers = {}
    client._credential_headers = {}
    if (
        client.auth_token is not None
        or client.credentials is not None
        or client._token_cache is not None
    ):
        raise ProviderIntegrityError("Anthropic authentication source differs")
    return client


def _contains_credential(raw: bytes, key: str) -> bool:
    """Do not retain or hash a credential echoed in a provider response."""
    if key.encode("utf-8") in raw:
        return True
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


class AnthropicAdapter:
    """One endpoint/model scoped async client; storage and SDK retries disabled.

    ``http_transport`` is an injected HTTP transport for deterministic SDK tests,
    never a configurable base URL or arbitrary request-body escape hatch.
    """

    def __init__(
        self,
        endpoint: str,
        model: str,
        *,
        context: AnthropicProductionContext | None = None,
        concurrency: int = 4,
        http_transport: object | None = None,
        submission_guard: Callable[[PreparedRequest], None] | None = None,
    ) -> None:
        if endpoint != "messages" or concurrency < 1:
            raise ProviderError(
                "Unsupported Anthropic endpoint or execution concurrency"
            )
        if context is None:
            from judgmentllm.anthropic_transport import production_context_for_identity

            context = production_context_for_identity("anthropic", endpoint, model)
        self.endpoint = endpoint
        self.model = model
        self.transport_context = context
        if (context.scope.provider, context.scope.endpoint, context.scope.model) != (
            "anthropic",
            endpoint,
            model,
        ):
            raise ProviderIntegrityError(
                "Anthropic adapter context differs from requested scope"
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
    def capabilities_for(provider: str, endpoint: str, model: str) -> CapabilityProfile:
        if (provider, endpoint) != ("anthropic", "messages"):
            raise ProviderError("Unsupported Anthropic provider endpoint")
        # Live describes implemented local orchestration. Model assessment
        # behavior remains unknown without reviewed exact-scope evidence.
        from judgmentllm.anthropic_registry import (
            _profile_for,
            production_generation_capabilities,
        )
        from judgmentllm.anthropic_transport import production_context_for_identity
        from judgmentllm.native_selection import NativeFeature

        scope = production_context_for_identity(provider, endpoint, model).scope
        reviewed = _profile_for(scope)

        def support(features: tuple[NativeFeature, ...]) -> bool | None:
            if reviewed is None:
                return None
            if all(feature in reviewed.supported_features for feature in features):
                return True
            if all(feature in reviewed.unsupported_features for feature in features):
                return False
            return None

        return CapabilityProfile(
            # The generic explanations flag covers every layout and task kind;
            # one qualified example cannot authorize all of them.
            explanations=support(
                (
                    "rubric_explanations_together",
                    "rubric_scores_first",
                    "rubric_explanations_first",
                    "pairwise_explanations",
                )
            ),
            grouped=support(("rubric_grouped",)),
            ties=support(("pairwise_ties",)),
            modes=("live",),
            generation=production_generation_capabilities(scope),
        )

    def capabilities(self, spec: RunSpecification) -> CapabilityProfile:
        return self.capabilities_for(spec.provider, spec.endpoint, spec.model)

    def validate(self, spec: RunSpecification) -> None:
        if (spec.provider, spec.endpoint, spec.model) != (
            "anthropic",
            self.endpoint,
            self.model,
        ):
            raise ProviderIntegrityError(
                "Anthropic adapter configuration scope differs"
            )
        if spec.schema_version != 3 or spec.execution.mode != "live":
            raise AnthropicConfigurationError(("schema_version", "execution.mode"))
        from judgmentllm.anthropic_transport import _request_policy

        try:
            _request_policy(spec)
        except ValueError:
            raise AnthropicConfigurationError(
                ("execution.cache_policy", "execution.tier")
            ) from None
        report = validate_run(spec, capabilities=self.capabilities(spec))
        if not report.valid:
            raise AnthropicConfigurationError(
                tuple(
                    issue.field for issue in report.issues if issue.severity == "error"
                )
            )
        concurrency = spec.execution.concurrency
        if self._client is not None and concurrency != self._concurrency:
            raise ProviderIntegrityError(
                "Anthropic client concurrency differs from the sealed run"
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
        try:
            _resolve_credentials("anthropic")
        except ValueError:
            raise AnthropicReadinessError("missing_credentials") from None
        try:
            installed = anthropic_sdk_version()
        except ProviderError:
            raise AnthropicReadinessError("missing_extra") from None
        if installed != self.transport_context.scope.sdk_version:
            raise AnthropicReadinessError("sdk_scope_mismatch")

    def _check_authority(self) -> None:
        context = self.transport_context
        if context.kind == "anthropic-qualification-candidate-v1":
            raise ProviderIntegrityError(
                "Offline qualification candidates cannot execute"
            )
        if context.kind == "anthropic-qualification-v1":
            from judgmentllm.anthropic_qualification import (
                _QualificationSubmissionGuard,
            )

            if type(self._submission_guard) is not _QualificationSubmissionGuard:
                raise ProviderIntegrityError(
                    "Qualification execution requires the dedicated harness grant"
                )
            cast(Any, self._submission_guard).verify_context(context)
        elif self._submission_guard is not None:
            raise ProviderIntegrityError(
                "Production execution cannot use qualification authority"
            )

    async def _execution_client(self) -> Any:
        if self._client is not None:
            return self._client
        sdk, http = _sdk()
        try:
            key = _resolve_credentials("anthropic")._api_key()
        except ValueError:
            raise TransportNotSubmitted(
                "Configure a valid ANTHROPIC_API_KEY environment source"
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
                checks = {
                    "credential": request.headers.get("authorization")
                    == f"Bearer {key}",
                    "prepared_request": prepared is not None,
                    "method": request.method == "POST",
                    "endpoint": request.url
                    == http.URL("https://api.anthropic.com/v1/messages"),
                    "version": request.headers.get("anthropic-version") == "2026-10-21",
                    "raw_response": request.headers.get("x-stainless-raw-response")
                    == "raw",
                    "headers": not (
                        set(request.headers)
                        - {
                            "host",
                            "accept",
                            "accept-encoding",
                            "connection",
                            "content-type",
                            "user-agent",
                            "authorization",
                            "anthropic-version",
                            "content-length",
                            "x-stainless-timeout",
                            "x-stainless-retry-count",
                            "x-stainless-read-timeout",
                            "x-stainless-raw-response",
                        }
                    ),
                    "body": prepared is not None
                    and request.content == prepared.payload,
                }
                if not all(checks.values()):
                    raise ProviderIntegrityError(
                        "Anthropic SDK request violates sealed transport: "
                        + ", ".join(name for name, valid in checks.items() if not valid)
                    )
                assert prepared is not None
                adapter.serialization_seconds += time.perf_counter() - started
                adapter._check_authority()
                if adapter._submission_guard is not None:
                    adapter._submission_guard(prepared)
                state = _INVOCATION_STATE.get()
                if state is None:
                    raise ProviderIntegrityError(
                        "Anthropic invocation boundary state is unavailable"
                    )
                state["invoked"] = True
                adapter.actual_call_count += 1
                response = await underlying.handle_async_request(request)
                body = bytearray()
                try:
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > _MAX_RESPONSE_BYTES:
                            raise SubmissionUncertain(
                                "Anthropic response exceeds the bounded retention limit",
                                invoked=True,
                            )
                        body.extend(chunk)
                finally:
                    await response.aclose()
                if _contains_credential(bytes(body), key):
                    raise SubmissionUncertain(
                        "Anthropic response cannot be retained safely", invoked=True
                    )
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
                sdk,
                api_key=key,
                base_url="https://api.anthropic.com",
                max_retries=0,
                timeout=http.Timeout(60.0, connect=10.0, write=10.0, pool=10.0),
                http_client=client,
            )
        except Exception:
            await client.aclose()
            raise ProviderIntegrityError(
                "Anthropic client initialization failed"
            ) from None
        return self._client

    async def submit(
        self, prepared: PreparedRequest, *, submission_id: str, correlation_id: str
    ) -> ProviderAcknowledgment:
        from judgmentllm.anthropic_transport import PreparedRequestV5

        if not isinstance(prepared, PreparedRequestV5) or (
            prepared.provider,
            prepared.endpoint,
            prepared.model,
        ) != ("anthropic", self.endpoint, self.model):
            raise ProviderIntegrityError(
                "Anthropic submission requires its sealed production request"
            )
        prepared = PreparedRequestV5.model_validate(dict(prepared))
        from judgmentllm.anthropic_transport import production_context_digest

        if (
            prepared.transport_commitment.context_kind != self.transport_context.kind
            or prepared.transport_commitment.context_sha256
            != production_context_digest(self.transport_context)
            or prepared.transport_plan.identity.submission_id != submission_id
            or prepared.transport_plan.identity.correlation_id != correlation_id
            or anthropic_sdk_version() != self.transport_context.scope.sdk_version
        ):
            raise ProviderIntegrityError(
                "Anthropic sealed transport scope or invocation identity differs"
            )
        self._check_authority()
        if submission_id in self._invoked or len(self._responses) >= self._concurrency:
            raise ProviderIntegrityError(
                "Anthropic invocation identity was reused or handoff capacity exceeded"
            )
        self._invoked.append(submission_id)
        body = _mapping(decode_assessment_output(prepared.payload))
        logging_token = _SDK_LOGGING.set(True)
        try:
            client = await self._execution_client()
        finally:
            _SDK_LOGGING.reset(logging_token)
        invocation_state = {"invoked": False}
        invocation_token = _INVOCATION_STATE.set(invocation_state)
        token = _INVOKING.set(prepared)
        serialization_token = _SERIALIZATION_STARTED.set(time.perf_counter())
        try:
            operation = client.messages.with_raw_response.create
            raw_response = await asyncio.wait_for(operation(**body), timeout=90.0)
            raw = bytes(await raw_response.read())
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
            status = getattr(error, "status_code", None)
            if status in {400, 401, 403, 404, 422, 429}:
                code = {
                    401: "credentials_rejected",
                    403: "permission_denied",
                    404: "model_or_endpoint_unavailable",
                    429: "rate_limited",
                }.get(status, "request_rejected")
                raise AnthropicRequestRejected(
                    cast(InvocationFailureCode, code)
                ) from None
            # No network exception establishes whether a provider accepted work.
            raise SubmissionUncertain(
                "Anthropic acceptance is unresolved; no request was repeated",
                invoked=invocation_state["invoked"],
            ) from None
        finally:
            _INVOKING.reset(token)
            _INVOCATION_STATE.reset(invocation_token)
            _SERIALIZATION_STARTED.reset(serialization_token)
        try:
            document = _mapping(decode_assessment_output(raw))
            remote_id = document.get("id")
            if not isinstance(remote_id, str) or not remote_id.strip():
                raise ValueError
            response = self._response(raw, document, remote_id, prepared=prepared)
        except (ValueError, TypeError):
            raise SubmissionUncertain(
                "Anthropic response identity is unavailable", invoked=True
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
            kind, text, completion_status = _messages_output(body)
        except (ValueError, TypeError):
            kind, text, completion_status = "provider_error", None, "unsupported_output"
        try:
            usage = _usage(self.endpoint, body.get("usage"))
        except ValueError:
            # Invalid provider counters cannot invalidate otherwise useful output.
            usage = None
        # Companion evidence is optional for replay of diagnostic-free observations.
        component = cast(
            "AnthropicAssessmentComponentV2 | None",
            _extract_assessment_component(body, self.endpoint),
        )
        if component is not None and component.extraction_status == "oversized":
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
            and (
                component is None
                or component.extraction_status not in {"oversized", "invalid_utf8"}
            )
        )
        safe = raw if exact_safe else _encoded(envelope)
        reported = body.get("model") if isinstance(body.get("model"), str) else None
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
                                kind="thinking_block",
                                provider_field="anthropic.messages.thinking",
                                block_ordinal=index,
                                redacted=cast(dict[str, Any], part).get("type")
                                == "redacted_thinking",
                            )
                        )
                        for index, part in enumerate(
                            cast(
                                list[Any],
                                body.get("content")
                                if isinstance(body.get("content"), list)
                                else [],
                            )
                        )
                        if isinstance(part, dict)
                        and cast(dict[str, Any], part).get("type")
                        in ("thinking", "redacted_thinking")
                    ),
                    output_text=text_bytes,
                    output_source="raw_output_text"
                    if text_bytes is not None
                    else "unavailable",
                    safe_response=safe,
                    original_body_safe=exact_safe,
                    observation_context=ProviderObservationContext(
                        reported_model=reported,
                        sdk_version=anthropic_sdk_version(),
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
            raise ProviderIntegrityError(
                "Anthropic retained observation identity differs"
            )
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
                "Anthropic non-retrievable response is unavailable; resubmission is prohibited"
            )
        return saved

    async def reconcile(
        self, prepared: PreparedRequest, *, submission_id: str, correlation_id: str
    ) -> ReconciliationEvidence:
        saved = self._responses.get(submission_id)
        if saved is None:
            return ReconciliationEvidence(outcome="unresolved")
        if (saved[0], saved[1]) != (correlation_id, prepared.payload):
            raise ProviderIntegrityError("Anthropic reconciliation identity differs")
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
                kind, text, _ = _messages_output(body)
                if (body.get("id"), kind) != (response.observation_key, response.kind):
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
            raise ValueError("Anthropic assessment output is invalid") from None

    async def aclose(self) -> None:
        self._responses.clear()
        if self._client is not None:
            await self._client.close()
            self._client = None
