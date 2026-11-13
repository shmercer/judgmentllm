"""OpenAI nonstreaming adapters over committed production request bytes.

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
from judgmentllm.evidence.usage import (
    InvocationFailureCode,
)
from judgmentllm.output_contracts import decode_assessment_output
from judgmentllm.planning import CapabilityProfile, validate_run
from judgmentllm.preparation.session import RequestDependency
from judgmentllm.prompts import RenderedAssessmentPrompt
from judgmentllm.providers.evidence import ProviderEvidence, ProviderObservationContext
from judgmentllm.providers.openai_response import (
    _chat_output,
    _encoded,
    _extract_assessment_component,
    _mapping,
    _responses_output,
    _safe_original_envelope,
    _usage,
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
from judgmentllm.service_tiers import (
    observe_service_tier,
    service_policy,
    terminal_rate_rejection,
    validate_service_support,
)

if TYPE_CHECKING:
    from judgmentllm.production_transport import ProductionContext

_SDK_LOGGING: ContextVar[bool] = ContextVar(
    "judgmentllm_openai_sensitive_call", default=False
)
_INVOKING: ContextVar[PreparedRequest | None] = ContextVar(
    "judgmentllm_openai_request", default=None
)
_SERIALIZATION_STARTED: ContextVar[float] = ContextVar(
    "judgmentllm_openai_serialization", default=0.0
)
_INVOCATION_STATE: ContextVar[dict[str, Any] | None] = ContextVar(
    "judgmentllm_openai_invocation", default=None
)
_PROTOCOL = "judgmentllm-openai-observation-v1"
_MAX_RESPONSE_BYTES = 8_388_608


class OpenAIConfigurationError(ProviderError):
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
            "generation.reasoning",
            "generation.reasoning.effort",
            "generation.reasoning.mode",
            "generation.temperature/top_p",
            "prompt.structured_output",
            "prompt.output_order",
        }
        selected = sorted(
            {field if field in allowed else "configuration" for field in fields}
        )
        super().__init__(
            "Unsupported or unverified OpenAI settings: "
            + ", ".join(selected)
            + "; revise these settings or obtain reviewed exact-scope evidence"
        )


class OpenAIReadinessError(ProviderError):
    """Fixed actionable errors before creating a run or submitting a request."""

    def __init__(
        self,
        code: Literal["missing_credentials", "missing_extra", "sdk_scope_mismatch"],
    ):
        messages = {
            "missing_credentials": "Set a valid OPENAI_API_KEY process environment value before execution",
            "missing_extra": "Install judgmentllm[openai] before OpenAI execution",
            "sdk_scope_mismatch": "The installed OpenAI SDK differs from the qualified transport version; install openai==3.42.1",
        }
        super().__init__(messages[code])
        self.diagnostic_code = code


class OpenAIRequestRejected(TransportNotSubmitted):
    """Authoritative HTTP rejection with only a fixed nonsensitive classification."""

    def __init__(self, code: InvocationFailureCode) -> None:
        super().__init__(
            "OpenAI rejected the request before assessment acceptance",
            diagnostic_code=code,
        )


class _ExecutionLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return _INVOKING.get() is None and not _SDK_LOGGING.get()


def _sdk() -> tuple[Any, Any]:
    try:
        sdk = importlib.import_module("openai")
        http = importlib.import_module("httpx2")
    except ImportError:
        raise ProviderError(
            "Install judgmentllm[openai] to execute OpenAI requests"
        ) from None
    # The SDK can log request options at DEBUG. Suppress them within our calls,
    # without changing logging for unrelated SDK users in this process.
    logger = logging.getLogger("openai._base_client")
    if not any(isinstance(item, _ExecutionLogFilter) for item in logger.filters):
        logger.addFilter(_ExecutionLogFilter())
    return sdk, http


def openai_sdk_version() -> str:
    """Read distribution metadata without constructing an SDK client."""
    try:
        return version("openai")
    except PackageNotFoundError:
        raise ProviderError(
            "Install judgmentllm[openai] to execute OpenAI requests"
        ) from None


def _protected_client(sdk: Any, **options: Any) -> Any:
    """Ignore SDK ambient custom headers; pass only the resolved API key.

    SDK 3.42.1 inspects OPENAI_CUSTOM_HEADERS even with explicit options. Its
    contents never enter our requests: override default_headers without calling
    the ambient implementation, clear the transient SDK copy immediately, and
    verify the bearer header at the forwarding boundary.
    """

    class ExplicitCredentialsClient(sdk.AsyncOpenAI):
        @property
        def default_headers(self) -> dict[str, str]:
            return {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "JudgmentLLM/OpenAI",
            }

    client = ExplicitCredentialsClient(
        admin_api_key="", organization="", project="", webhook_secret="", **options
    )
    # These headers are deliberately omitted by the protected subclass. Avoid
    # the pinned SDK's otherwise unconditional asynchronous OS metadata probe.
    client._platform = "Unknown"
    client._custom_headers = {}
    client._ambient_authorizations = frozenset()
    return client


class OpenAIAdapter:
    """One endpoint/model scoped async client; storage and SDK retries disabled.

    ``http_transport`` is an injected HTTP transport for deterministic SDK tests,
    never a configurable base URL or arbitrary request-body escape hatch.
    """

    def __init__(
        self,
        endpoint: str,
        model: str,
        *,
        context: ProductionContext | None = None,
        concurrency: int = 4,
        http_transport: object | None = None,
        submission_guard: Callable[[PreparedRequest], None] | None = None,
    ) -> None:
        if endpoint not in {"responses", "chat.completions"} or concurrency < 1:
            raise ProviderError("Unsupported OpenAI endpoint or execution concurrency")
        if context is None:
            from judgmentllm.production_transport import production_context_for_identity

            context = production_context_for_identity("openai", endpoint, model)
        self.endpoint = endpoint
        self.model = model
        self.transport_context = context
        if (context.scope.provider, context.scope.endpoint, context.scope.model) != (
            "openai",
            endpoint,
            model,
        ):
            raise ProviderIntegrityError(
                "OpenAI adapter context differs from requested scope"
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
        if (provider, endpoint) not in {
            ("openai", "responses"),
            ("openai", "chat.completions"),
        }:
            raise ProviderError("Unsupported OpenAI provider endpoint")
        # Live describes implemented local orchestration. Model assessment
        # behavior remains unknown without reviewed exact-scope evidence.
        from judgmentllm.native_selection import NativeFeature
        from judgmentllm.production_registry import (
            _profile_for,
            production_generation_capabilities,
        )
        from judgmentllm.production_transport import production_context_for_identity

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
        if self.transport_context.kind == "openai-study-candidate-v1":
            from judgmentllm.study.admission import guarded_capabilities

            return guarded_capabilities(
                self._submission_guard, self.transport_context, spec
            )

        from judgmentllm.providers.reasoning import generation_capabilities

        profile = self.capabilities_for(spec.provider, spec.endpoint, spec.model)
        return profile.model_copy(
            update={
                "generation": generation_capabilities(
                    spec, self.transport_context.scope, profile.generation
                )
            }
        )

    def validate(self, spec: RunSpecification) -> None:
        if (spec.provider, spec.endpoint, spec.model) != (
            "openai",
            self.endpoint,
            self.model,
        ):
            raise ProviderIntegrityError("OpenAI adapter configuration scope differs")
        if spec.schema_version != 3 or spec.execution.mode != "live":
            raise OpenAIConfigurationError(("schema_version", "execution.mode"))
        if spec.execution.cache_policy not in (None, "auto", "off"):
            raise OpenAIConfigurationError(
                tuple(
                    field
                    for field, invalid in (
                        (
                            "execution.cache_policy",
                            spec.execution.cache_policy == "explicit",
                        ),
                        ("execution.tier", spec.execution.tier is not None),
                    )
                    if invalid
                )
            )
        try:
            service_policy(spec)
        except ValueError:
            raise OpenAIConfigurationError(("execution.tier",)) from None
        report = validate_run(spec, capabilities=self.capabilities(spec))
        if not report.valid:
            raise OpenAIConfigurationError(
                tuple(
                    issue.field for issue in report.issues if issue.severity == "error"
                )
            )
        concurrency = spec.execution.concurrency
        if self._client is not None and concurrency != self._concurrency:
            raise ProviderIntegrityError(
                "OpenAI client concurrency differs from the sealed run"
            )
        if self._client is None and concurrency != self._concurrency:
            self._concurrency = concurrency
            self._invoked = deque(self._invoked, maxlen=2 * concurrency)

    def validate_new_run(self, spec: RunSpecification) -> None:
        """Check new tier entitlement without changing sealed historical validation."""
        self.validate(spec)
        assert spec.schema_version == 3
        from judgmentllm.production_registry import _profile_for

        reviewed = _profile_for(self.transport_context.scope)
        try:
            validate_service_support(
                service_policy(spec),
                () if reviewed is None else reviewed.service_scopes,
                strict=spec.strict_capabilities,
                qualification="qualification" in self.transport_context.kind
                or self.transport_context.kind == "openai-study-candidate-v1",
            )
        except ValueError:
            raise OpenAIConfigurationError(("execution.tier",)) from None

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
        if self.transport_context.kind == "openai-study-candidate-v1":
            self._check_authority()
        try:
            _resolve_credentials("openai")
        except ValueError:
            raise OpenAIReadinessError("missing_credentials") from None
        try:
            installed = openai_sdk_version()
        except ProviderError:
            raise OpenAIReadinessError("missing_extra") from None
        if installed != self.transport_context.scope.sdk_version:
            raise OpenAIReadinessError("sdk_scope_mismatch")

    def _check_authority(self) -> None:
        if self.transport_context.kind == "openai-study-candidate-v1":
            from judgmentllm.study.admission import guarded_authority

            guarded_authority(self._submission_guard, self.transport_context)
            return

        context = self.transport_context
        if context.kind == "openai-qualification-candidate-v1":
            raise ProviderIntegrityError(
                "Offline qualification candidates cannot execute"
            )
        if context.kind == "openai-qualification-v1":
            from judgmentllm.qualification import _QualificationSubmissionGuard

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
            key = _resolve_credentials("openai")._api_key()
        except ValueError:
            raise TransportNotSubmitted(
                "Configure a valid OPENAI_API_KEY environment source"
            ) from None
        adapter = self
        from judgmentllm.production_transport import PreparedRequestV4

        underlying: Any = self._http_transport or http.AsyncHTTPTransport(
            retries=0,
            limits=http.Limits(
                max_connections=self._concurrency,
                max_keepalive_connections=self._concurrency,
            ),
        )

        class CommittedHTTPTransport(http.AsyncBaseTransport):
            async def handle_async_request(self, request: Any) -> Any:
                prepared = _INVOKING.get()
                expected_path = (
                    "/v1/responses"
                    if adapter.endpoint == "responses"
                    else "/v1/chat/completions"
                )
                started = _SERIALIZATION_STARTED.get()
                if (
                    request.headers.get("authorization") != f"Bearer {key}"
                    or not isinstance(prepared, PreparedRequestV4)
                    or request.method != "POST"
                    or request.url.scheme != "https"
                    or request.url.host != "api.openai.com"
                    or request.url.path != expected_path
                    or request.url.query
                    or request.content != prepared.payload
                ):
                    raise ProviderIntegrityError(
                        "SDK request bytes or endpoint differ from the sealed commitment"
                    )
                policy = prepared.output_commitment.service_policy
                request.extensions["timeout"] = {
                    "connect": 10.0,
                    "write": 10.0,
                    "pool": 10.0,
                    "read": 60.0
                    if policy is None
                    else float(policy.read_timeout_seconds),
                }
                adapter.serialization_seconds += time.perf_counter() - started
                adapter._check_authority()
                if adapter._submission_guard is not None:
                    adapter._submission_guard(prepared)
                state = _INVOCATION_STATE.get()
                if state is None:
                    raise ProviderIntegrityError(
                        "OpenAI invocation boundary state is unavailable"
                    )
                state["invoked"] = True
                adapter.actual_call_count += 1
                response = await underlying.handle_async_request(request)
                body = bytearray()
                try:
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > _MAX_RESPONSE_BYTES:
                            raise SubmissionUncertain(
                                "OpenAI response exceeds the bounded retention limit",
                                invoked=True,
                            )
                        body.extend(chunk)
                finally:
                    await response.aclose()
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
        self._client = _protected_client(
            sdk,
            api_key=key,
            base_url="https://api.openai.com/v1",
            max_retries=0,
            timeout=http.Timeout(60.0, connect=10.0, write=10.0, pool=10.0),
            http_client=client,
        )
        return self._client

    async def submit(
        self, prepared: PreparedRequest, *, submission_id: str, correlation_id: str
    ) -> ProviderAcknowledgment:
        from judgmentllm.production_transport import PreparedRequestV4

        if not isinstance(prepared, PreparedRequestV4) or (
            prepared.provider,
            prepared.endpoint,
            prepared.model,
        ) != ("openai", self.endpoint, self.model):
            raise ProviderIntegrityError(
                "OpenAI submission requires its sealed production request"
            )
        prepared = PreparedRequestV4.model_validate(dict(prepared))
        from judgmentllm.production_transport import production_context_digest

        if (
            prepared.transport_commitment.context_kind != self.transport_context.kind
            or prepared.transport_commitment.context_sha256
            != production_context_digest(self.transport_context)
            or prepared.transport_plan.identity.submission_id != submission_id
            or prepared.transport_plan.identity.correlation_id != correlation_id
            or openai_sdk_version() != self.transport_context.scope.sdk_version
        ):
            raise ProviderIntegrityError(
                "OpenAI sealed transport scope or invocation identity differs"
            )
        self._check_authority()
        if submission_id in self._invoked or len(self._responses) >= self._concurrency:
            raise ProviderIntegrityError(
                "OpenAI invocation identity was reused or handoff capacity exceeded"
            )
        self._invoked.append(submission_id)
        body = _mapping(decode_assessment_output(prepared.payload))
        client = await self._execution_client()
        operation_started = time.perf_counter()
        invocation_state: dict[str, Any] = {"invoked": False}
        invocation_token = _INVOCATION_STATE.set(invocation_state)
        token = _INVOKING.set(prepared)
        serialization_token = _SERIALIZATION_STARTED.set(time.perf_counter())
        try:
            operation = (
                client.responses.with_raw_response.create
                if self.endpoint == "responses"
                else client.chat.completions.with_raw_response.create
            )
            policy = prepared.output_commitment.service_policy
            if policy is not None:
                _, http = _sdk()
                body["timeout"] = http.Timeout(
                    float(policy.read_timeout_seconds),
                    connect=10.0,
                    write=10.0,
                    pool=10.0,
                )
            raw_response = await asyncio.wait_for(
                operation(**body),
                timeout=90.0 if policy is None else policy.operation_deadline_seconds,
            )
            raw = bytes(raw_response.content)
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
            if status in {400, 401, 403, 404, 422, 429} and (
                status != 429
                or prepared.output_commitment.service_policy is None
                or terminal_rate_rejection("openai", invocation_state.get("raw"))
            ):
                code = {
                    401: "credentials_rejected",
                    403: "permission_denied",
                    404: "model_or_endpoint_unavailable",
                    429: "rate_limited",
                }.get(status, "request_rejected")
                raise OpenAIRequestRejected(cast(InvocationFailureCode, code)) from None
            # No network exception establishes whether a provider accepted work.
            raise SubmissionUncertain(
                "OpenAI acceptance is unresolved; no request was repeated",
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
            response = self._response(
                raw,
                document,
                remote_id,
                prepared=prepared,
                latency_seconds=time.perf_counter() - operation_started,
            )
        except (ValueError, TypeError):
            raise SubmissionUncertain(
                "OpenAI response identity is unavailable", invoked=True
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
        latency_seconds: float | None = None,
    ) -> ProviderResponse:
        try:
            kind, text, completion_status = (
                _responses_output(body)
                if self.endpoint == "responses"
                else _chat_output(body)
            )
        except (ValueError, TypeError):
            kind, text, completion_status = "provider_error", None, "unsupported_output"
        try:
            usage = _usage(self.endpoint, body.get("usage"))
        except ValueError:
            # Invalid provider counters cannot invalidate otherwise useful output.
            usage = None
        component = _extract_assessment_component(body, self.endpoint)
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
        exact_safe = exact_safe and encoding_safe
        safe = raw if exact_safe else _encoded(envelope)
        reported = body.get("model") if isinstance(body.get("model"), str) else None
        policy = getattr(
            getattr(prepared, "output_commitment", None), "service_policy", None
        )
        return ProviderResponse.model_validate(
            {
                "observation_key": remote_id,
                "kind": kind,
                "raw": raw,
                "evidence": ProviderEvidence(
                    provenance="provider_documented",
                    service_tier=observe_service_tier(
                        policy,
                        body.get("service_tier"),
                        latency_seconds=latency_seconds,
                    )
                    if policy is not None
                    else None,
                    assessment_component=component,
                    usage=usage,
                    output_text=text_bytes,
                    output_source="raw_output_text"
                    if text_bytes is not None
                    else "unavailable",
                    safe_response=safe,
                    original_body_safe=exact_safe,
                    observation_context=ProviderObservationContext(
                        reported_model=reported,
                        sdk_version=openai_sdk_version(),
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
        saved = self._responses.pop(submission_id, None)
        if saved is None:
            return None
        if (saved[0], saved[1]) != (correlation_id, prepared.payload):
            raise ProviderIntegrityError("OpenAI retained observation identity differs")
        return saved[2]

    async def retrieve(
        self, prepared: PreparedRequest, *, submission_id: str, correlation_id: str
    ) -> ProviderResponse:
        saved = self._received_response_for_cancellation(
            prepared, submission_id=submission_id, correlation_id=correlation_id
        )
        if saved is None:
            raise RetrievalUnavailable(
                "OpenAI store=false response is unavailable; resubmission is prohibited"
            )
        return saved

    async def reconcile(
        self, prepared: PreparedRequest, *, submission_id: str, correlation_id: str
    ) -> ReconciliationEvidence:
        saved = self._responses.get(submission_id)
        if saved is None:
            return ReconciliationEvidence(outcome="unresolved")
        if (saved[0], saved[1]) != (correlation_id, prepared.payload):
            raise ProviderIntegrityError("OpenAI reconciliation identity differs")
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
                kind, text, _ = (
                    _responses_output(body)
                    if self.endpoint == "responses"
                    else _chat_output(body)
                )
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
            raise ValueError("OpenAI assessment output is invalid") from None

    async def aclose(self) -> None:
        self._responses.clear()
        if self._client is not None:
            await self._client.close()
            self._client = None
