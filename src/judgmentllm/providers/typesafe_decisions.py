"""Locked TypeSafe SDK forwarding for the approved text-only Jev route."""

from __future__ import annotations

import asyncio
import logging
import re
from collections import deque
from collections.abc import Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from threading import Lock
from typing import TYPE_CHECKING, Any, Literal, cast

from pydantic_core import to_json

from judgmentllm._auth import _resolve_credentials  # pyright: ignore[reportPrivateUsage]
from judgmentllm.config import RunSpecification, RunSpecV4, validate_run_spec
from judgmentllm.decisions.records import (
    TYPESAFE_MODEL,
    TYPESAFE_ORIGIN,
    TYPESAFE_ROUTE,
    TYPESAFE_SDK,
    DecisionQuestionV1,
    TypeSafeDecisionsContextV1,
)
from judgmentllm.decisions.replay import TypeSafeDecisionsReplayV1
from judgmentllm.decisions.responses import ParsedDecisionsResponseV2
from judgmentllm.decisions.transport import PreparedDecisionsRequestV3
from judgmentllm.output_contracts import decode_assessment_output
from judgmentllm.persistence.hashing import (
    bytes_digest,
    canonical_bytes,
    semantic_digest,
)
from judgmentllm.persistence.receipts import CompleteResponseEvidenceV3
from judgmentllm.planning import CapabilityProfile
from judgmentllm.providers.evidence import ProviderEvidence, ProviderObservationContext
from judgmentllm.providers.protocol import (
    DecisionsReconciliationEvidenceV1,
    PreparedRequest,
    ProviderAcknowledgmentV4,
    ProviderError,
    ProviderIntegrityError,
    ProviderInvocationCancelled,
    ProviderResponse,
    RetrievalUnavailable,
    SubmissionUncertain,
    TypeSafeRejectionCancelled,
)
from judgmentllm.providers.typesafe_decisions_response import (
    CHOICES,
    analyze,
    observation_key,
    rejected,
    verify_replay,
)

if TYPE_CHECKING:
    import httpx2
    from typesafe_sdk import AsyncTypeSafeClient

_RESPONSE_LIMIT = 8_388_608
_REJECTIONS = (400, 401, 413, 415, 422, 429)


class TypeSafeConfigurationError(ValueError):
    """Fixed, safe configuration diagnostics."""


class TypeSafeReadinessError(ProviderError):
    def __init__(
        self,
        code: Literal[
            "missing_credentials", "invalid_credentials", "sdk_scope_mismatch"
        ],
    ) -> None:
        self.diagnostic_code = code
        super().__init__(
            {
                "missing_credentials": "TypeSafe Jev requires the selected TYPESAFE_API_KEY credential.",
                "invalid_credentials": "TypeSafe Jev requires a TYPESAFE_API_KEY with printable ASCII and no whitespace.",
                "sdk_scope_mismatch": "TypeSafe Jev requires typesafe-sdk==0.7.5 from the typesafe extra.",
            }[code]
        )


def decisions_context(spec: RunSpecification) -> TypeSafeDecisionsContextV1:
    checked = validate_run_spec(spec)
    if (
        not isinstance(checked, RunSpecV4)
        or (checked.provider, checked.endpoint) != TYPESAFE_ROUTE
    ):
        raise TypeSafeConfigurationError(
            "TypeSafe Jev requires RunSpec V4 and endpoint systemone."
        )
    if checked.model != TYPESAFE_MODEL:
        raise TypeSafeConfigurationError(
            "TypeSafe Jev requires exact model jev-1.14.0."
        )
    if checked.strict_capabilities:
        raise TypeSafeConfigurationError(
            "TypeSafe Jev context fit is unverified under strict capabilities."
        )
    if checked.execution.tier is not None:
        raise TypeSafeConfigurationError(
            "TypeSafe Jev requires the default service tier."
        )
    if checked.rubric is not None and any(
        len(trait.levels) > 10 for trait in checked.rubric.traits
    ):
        raise TypeSafeConfigurationError(
            "TypeSafe Jev supports at most ten score levels per trait."
        )
    return TypeSafeDecisionsContextV1(account_scope=checked.account_scope)


def serialize_typesafe_decisions(
    shared: str, questions: Sequence[DecisionQuestionV1], spec: RunSpecV4
) -> bytes:
    # Locked SDK order and Pydantic JSON encoding, verified at the HTTP boundary.
    return to_json(
        dict(
            state=shared,
            model=spec.model,
            questions={
                question.key: dict(
                    type=question.kind,
                    instructions=question.instructions,
                    criteria=[
                        dict(label=level.label, description=level.description)
                        for level in question.trait.levels
                    ]
                    if question.trait is not None
                    else CHOICES,
                )
                for question in questions
            },
        )
    )


@dataclass
class _Invocation:
    wire: PreparedDecisionsRequestV3
    forwarded: bool = False
    received: tuple[int, bytes, str | None] | None = None


_ACTIVE: ContextVar[_Invocation | None] = ContextVar(
    "typesafe_decisions_invocation", default=None
)
_CLOSING: ContextVar[bool] = ContextVar("typesafe_decisions_closing", default=False)


class _ExecutionLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return _ACTIVE.get() is None and not _CLOSING.get()


_LOG_FILTER = _ExecutionLogFilter()
_LOG_FILTER_LOCK = Lock()
_LOGGERS = ("typesafe_sdk", "httpx2", "httpcore2.connection", "httpcore2.http11")


class TypeSafeDecisionsAdapter:
    def __init__(
        self,
        model: str,
        *,
        concurrency: int = 4,
        context: TypeSafeDecisionsContextV1 | None = None,
        http_transport: httpx2.AsyncBaseTransport | None = None,
    ):
        self.model = model
        self.transport_context = context or TypeSafeDecisionsContextV1()
        self.concurrency = concurrency
        self.http_transport = http_transport
        self._validated_key: str | None = None
        self._client: AsyncTypeSafeClient | None = None
        self._responses: dict[
            str, tuple[ProviderAcknowledgmentV4, ProviderResponse]
        ] = {}
        self._invoked: deque[str] = deque(maxlen=2 * concurrency)

    @staticmethod
    def capabilities_for(provider: str, endpoint: str, model: str) -> CapabilityProfile:
        return CapabilityProfile(
            grouped=True, max_levels=10, explanations=False, ties=False, modes=("live",)
        )

    def capabilities(self, spec: RunSpecification) -> CapabilityProfile:
        return self.capabilities_for(spec.provider, spec.endpoint, spec.model)

    def validate(self, spec: RunSpecification) -> None:
        if (
            decisions_context(spec) != self.transport_context
            or spec.model != self.model
        ):
            raise ProviderIntegrityError("TypeSafe Decisions runtime scope differs")

    def validate_credentials(self) -> None:
        if self._validated_key is not None:
            return
        try:
            installed = version("typesafe-sdk")
        except PackageNotFoundError:
            installed = None
        if installed != self.transport_context.sdk_version:
            raise TypeSafeReadinessError("sdk_scope_mismatch")
        try:
            key = _resolve_credentials("typesafe")._api_key()  # pyright: ignore[reportPrivateUsage]
        except ValueError:
            raise TypeSafeReadinessError("missing_credentials") from None
        # Enforce the pinned SDK's syntax before creating submission intent.
        if not key.isascii() or not key.isprintable() or " " in key:
            raise TypeSafeReadinessError("invalid_credentials")
        self._validated_key = key

    def prepare(self, *_args: Any, **_kwargs: Any) -> PreparedRequest:
        raise ProviderError("TypeSafe Decisions requires sealed preparation")

    def _execution_client(self) -> AsyncTypeSafeClient:
        if self._client is not None:
            return self._client
        key = self._validated_key
        if key is None:
            raise ProviderIntegrityError("TypeSafe SDK credential was not validated")
        import httpx2
        from typesafe_sdk import AsyncTypeSafeClient, RetryPolicy

        with _LOG_FILTER_LOCK:
            for name in _LOGGERS:
                logging.getLogger(name).addFilter(_LOG_FILTER)
        limits = httpx2.Limits(
            max_connections=self.concurrency, max_keepalive_connections=self.concurrency
        )
        underlying = self.http_transport or httpx2.AsyncHTTPTransport(
            retries=0, limits=limits, trust_env=False
        )
        context = self.transport_context

        class Guarded(httpx2.AsyncBaseTransport):
            async def handle_async_request(
                self, request: httpx2.Request
            ) -> httpx2.Response:
                active = _ACTIVE.get()
                if (
                    active is None
                    or active.forwarded
                    or request.method != "POST"
                    or str(request.url) != context.request_url
                    or request.headers.get_list("authorization") != [f"Bearer {key}"]
                    or any(
                        name in request.headers
                        for name in (
                            "content-encoding",
                            "typesafe-danger-zone-long-context",
                            "typesafe-danger-zone-images",
                        )
                    )
                    or request.content != active.wire.payload
                    or len(request.content) > context.limits.local_body_bytes
                ):
                    raise ProviderIntegrityError(
                        "TypeSafe SDK forwarding differs from sealed request"
                    )
                active.forwarded = True
                response = await underlying.handle_async_request(request)
                body = bytearray()
                try:
                    stream = response.stream
                    if (
                        not isinstance(stream, httpx2.AsyncByteStream)
                        or response.headers.get("content-encoding", "identity").lower()
                        != "identity"
                    ):
                        raise SubmissionUncertain(invoked=True)
                    # HTTPX's response iterators await closing before reporting EOF.
                    async for chunk in stream:
                        if len(body) + len(chunk) > _RESPONSE_LIMIT:
                            raise SubmissionUncertain(invoked=True)
                        body.extend(chunk)
                    raw = bytes(body)
                    ids = response.headers.get_list("x-typesafe-request-id")
                    remote_id = (
                        ids[0]
                        if len(ids) == 1
                        and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", ids[0])
                        and key not in ids[0]
                        else None
                    )
                    active.received = response.status_code, raw, remote_id
                finally:
                    await response.aclose()
                return httpx2.Response(
                    response.status_code, content=raw, request=request
                )

            async def aclose(self) -> None:
                await underlying.aclose()

        self._client = AsyncTypeSafeClient(
            api_key=key,
            model=context.model,
            base_url=TYPESAFE_ORIGIN,
            compression=False,
            retry=RetryPolicy(max_retries=0),
            http_client=httpx2.AsyncClient(
                transport=Guarded(),
                timeout=httpx2.Timeout(60, connect=10, write=10, pool=10),
                follow_redirects=False,
                trust_env=False,
                headers={"accept-encoding": "identity"},
            ),
        )
        return self._client

    async def submit(
        self, prepared: PreparedRequest, *, submission_id: str, correlation_id: str
    ) -> ProviderAcknowledgmentV4:
        if not isinstance(prepared, PreparedDecisionsRequestV3):
            raise ProviderIntegrityError(
                "TypeSafe Decisions requires production preparation 10"
            )
        wire = PreparedDecisionsRequestV3.model_validate(dict(prepared))
        commitment = wire.transport_commitment
        identity = commitment.identity
        request = cast(dict[str, Any], decode_assessment_output(wire.payload))
        if (
            (wire.provider, wire.endpoint) != TYPESAFE_ROUTE
            or wire.model != self.transport_context.model
            or wire.model != self.model
            or request.get("model") != wire.model
            or tuple(request["questions"]) != tuple(q.key for q in commitment.questions)
            or commitment.limits != self.transport_context.limits
            or commitment.context_sha256
            != semantic_digest("decisions-context-v1", self.transport_context)
            or (identity.submission_id, identity.correlation_id)
            != (submission_id, correlation_id)
            or len(wire.payload) > commitment.limits.local_body_bytes
            or submission_id in self._invoked
            or len(self._responses) >= self.concurrency
        ):
            raise ProviderIntegrityError(
                "TypeSafe invocation identity differs or handoff is full"
            )
        self._invoked.append(submission_id)
        client = self._execution_client()
        from typesafe_sdk import Choice, Score

        questions = {
            name: (Score if q["type"] == "score" else Choice)(
                instructions=q["instructions"], criteria=q["criteria"]
            )
            for name, q in request["questions"].items()
        }
        state = _Invocation(wire)
        active = _ACTIVE.set(state)
        cancelled = False
        try:
            async with asyncio.timeout(90):
                await client.system_one(  # pyright: ignore[reportUnknownMemberType]
                    model=wire.model, state=request["state"], questions=questions
                )
        except asyncio.CancelledError:
            cancelled = True
        except ProviderIntegrityError:
            if not state.forwarded:
                raise
        except Exception:
            # A complete response at the guard is independent of the SDK decoder.
            pass
        finally:
            _ACTIVE.reset(active)

        if state.received is None:
            if cancelled:
                raise ProviderInvocationCancelled(invoked=state.forwarded)
            raise SubmissionUncertain(invoked=state.forwarded)
        status, raw, remote_id = state.received
        if status in _REJECTIONS:
            rejection = rejected(status, raw, wire)
            if rejection is not None:
                if cancelled:
                    raise TypeSafeRejectionCancelled(rejection)
                raise rejection
        if status != 200:
            if cancelled:
                raise ProviderInvocationCancelled(invoked=True)
            raise SubmissionUncertain(invoked=True)

        replay, safe, usage = analyze(raw, wire)
        permitted = raw if safe else canonical_bytes(replay)
        if len(permitted) > _RESPONSE_LIMIT:
            raise SubmissionUncertain(invoked=True)
        response = ProviderResponse(
            observation_key=observation_key(wire),
            kind="response",
            raw=raw,
            evidence=ProviderEvidence(
                provenance="provider_documented",
                usage=usage,
                observation_context=ProviderObservationContext(
                    reported_model=replay.metadata.reported_model,
                    sdk_version=TYPESAFE_SDK,
                    remote_response_id=remote_id,
                    invocation_kind="live",
                ),
                safe_response=permitted,
                original_body_safe=safe,
                decisions_validation=replay.metadata,
                decisions_retained_kind="original" if safe else "filtered_replay",
            ),
        )
        ack = ProviderAcknowledgmentV4(
            remote_id=remote_id,
            observation_key=response.observation_key,
            response_evidence=CompleteResponseEvidenceV3(
                **identity.model_dump(exclude={"identity_version"}),
                payload_sha256=wire.payload_sha256,
                response_sha256=bytes_digest(raw),
                response_byte_length=len(raw),
            ),
        )
        self._responses[submission_id] = ack, response
        if cancelled:
            raise ProviderInvocationCancelled(invoked=True)
        return ack

    def _received_response_for_cancellation(
        self, prepared: PreparedRequest, *, submission_id: str, correlation_id: str
    ) -> ProviderResponse | None:
        received = self._responses.get(submission_id)
        return received[1] if received else None

    def _received_acknowledgment_for_cancellation(
        self, prepared: PreparedRequest, *, submission_id: str, correlation_id: str
    ) -> ProviderAcknowledgmentV4 | None:
        received = self._responses.get(submission_id)
        return received[0] if received else None

    async def retrieve(
        self, prepared: PreparedRequest, *, submission_id: str, correlation_id: str
    ) -> ProviderResponse:
        received = self._responses.pop(submission_id, None)
        if received is None:
            raise RetrievalUnavailable()
        if received[0].response_evidence.correlation_id != correlation_id:
            raise ProviderIntegrityError("TypeSafe retrieval identity differs")
        return received[1]

    async def reconcile(
        self, *_args: Any, **_kwargs: Any
    ) -> DecisionsReconciliationEvidenceV1:
        return DecisionsReconciliationEvidenceV1(outcome="unresolved")

    def parse_decisions(
        self, response: ProviderResponse, prepared: PreparedDecisionsRequestV3
    ) -> ParsedDecisionsResponseV2:
        replay = response.decisions_replay or analyze(response.raw, prepared)[0]
        if not isinstance(replay, TypeSafeDecisionsReplayV1):
            raise ProviderIntegrityError("TypeSafe replay protocol differs")
        verify_replay(replay, prepared)
        return ParsedDecisionsResponseV2(
            identity=prepared.transport_commitment.identity,
            payload_sha256=prepared.payload_sha256,
            response_sha256=replay.metadata.original_sha256,
            kind=response.kind,
            recognizable=replay.metadata.recognizable,
            reported_model=replay.metadata.reported_model,
            answer_order="map",
            answers=tuple(answer for answer in replay.answers if answer is not None),
            metadata=replay.metadata,
        )

    def parse(self, *_args: Any, **_kwargs: Any) -> Any:
        raise ProviderError("TypeSafe Decisions requires named-member interpretation")

    async def aclose(self) -> None:
        closing = _CLOSING.set(True)
        try:
            if self._client is not None:
                await self._client.aclose()
        finally:
            _CLOSING.reset(closing)
            self._client = None
            self._validated_key = None
