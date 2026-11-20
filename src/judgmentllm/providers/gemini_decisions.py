"""Pinned Gemini Developer Decisions forwarding over one guarded SDK request."""

from __future__ import annotations

import asyncio
import json
import re
from collections import deque
from collections.abc import Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, cast

from judgmentllm._auth import _resolve_credentials  # pyright: ignore[reportPrivateUsage]
from judgmentllm._bulk_dispatch import scoring_forward
from judgmentllm.config import RunSpecification, RunSpecV4, validate_run_spec
from judgmentllm.decisions.records import (
    GEMINI_DECISIONS_BODY_CAP,
    GEMINI_DECISIONS_CONTEXT_WARNING,
    GEMINI_DECISIONS_MODEL,
    GEMINI_DECISIONS_PRECISION,
    GEMINI_DECISIONS_ROUTE,
    DecisionQuestionV1,
    GeminiDecisionsContextV1,
)
from judgmentllm.decisions.replay import GeminiDecisionsReplayV1
from judgmentllm.decisions.responses import ParsedDecisionsResponseV2
from judgmentllm.decisions.transport import PreparedDecisionsRequestV4
from judgmentllm.output_contracts import decode_assessment_output
from judgmentllm.persistence.hashing import canonical_bytes, semantic_digest
from judgmentllm.persistence.receipts import CompleteResponseEvidenceV4
from judgmentllm.planning import CapabilityProfile, bulk_capabilities
from judgmentllm.presentation import ESSAY_ALIASES
from judgmentllm.providers.evidence import ProviderEvidence, ProviderObservationContext
from judgmentllm.providers.gemini import (  # pyright: ignore[reportPrivateUsage]
    _SDK_LOGGING,  # pyright: ignore[reportPrivateUsage]
    GeminiReadinessError,
    _close_client,  # pyright: ignore[reportPrivateUsage]
    _contains_credential,  # pyright: ignore[reportPrivateUsage]
    _protected_client,  # pyright: ignore[reportPrivateUsage]
    _sdk,  # pyright: ignore[reportPrivateUsage]
    gemini_sdk_version,
)
from judgmentllm.providers.gemini_decisions_response import (
    analyze,
    observation_key,
    rejected,
    verify_replay,
)
from judgmentllm.providers.protocol import (
    DecisionsReconciliationEvidenceV1,
    GeminiDecisionsRejectionCancelled,
    PreparedRequest,
    ProviderAcknowledgmentV5,
    ProviderError,
    ProviderIntegrityError,
    ProviderInvocationCancelled,
    ProviderResponse,
    RetrievalUnavailable,
    SubmissionUncertain,
)

_RESPONSE_LIMIT = 8_388_608
_QUESTION_KEY = re.compile(r"[A-Za-z0-9_-]{1,64}")
_CHOICES = dict(
    zip(
        ESSAY_ALIASES,
        ("The first presented essay.", "The second presented essay."),
        strict=True,
    )
)


class GeminiDecisionsConfigurationError(ValueError):
    """Fixed diagnostics for offline Decisions admission."""


def decisions_context(spec: RunSpecification) -> GeminiDecisionsContextV1:
    checked = validate_run_spec(spec)
    if (
        not isinstance(checked, RunSpecV4)
        or (checked.provider, checked.endpoint) != GEMINI_DECISIONS_ROUTE
        or checked.model != GEMINI_DECISIONS_MODEL
        or checked.execution.tier is not None
    ):
        raise GeminiDecisionsConfigurationError(
            "Gemini Decisions requires RunSpec V4, exact model gemini-decision-1 and the default service."
        )
    if checked.strict_capabilities:
        raise GeminiDecisionsConfigurationError(GEMINI_DECISIONS_CONTEXT_WARNING)
    if checked.rubric is not None and any(
        len(trait.levels) > 255 for trait in checked.rubric.traits
    ):
        raise GeminiDecisionsConfigurationError(
            "Gemini Decisions score requires 2–255 ordered levels."
        )
    normalization = checked.normalization
    tolerance = (
        normalization.probability_sum_tolerance
        if "probability_sum_tolerance" in normalization.model_fields_set
        else GEMINI_DECISIONS_PRECISION
    )
    if tolerance > GEMINI_DECISIONS_PRECISION:
        raise GeminiDecisionsConfigurationError(
            "Gemini Decisions probability sum tolerance cannot exceed 1e-6."
        )
    return GeminiDecisionsContextV1(
        account_scope=checked.account_scope, probability_sum_tolerance=tolerance
    )


def serialize_gemini_decisions(
    shared: str, questions: Sequence[DecisionQuestionV1], spec: RunSpecV4
) -> bytes:
    # Locked SDK field order and ASCII-escaped compact JSON are verified at forwarding.
    return json.dumps(
        dict(
            input=shared,
            model=spec.model,
            questions={
                question.key: dict(
                    criteria=[
                        f"{level.label}: {level.description}"
                        if level.description
                        else level.label
                        for level in question.trait.levels
                    ]
                    if question.trait is not None
                    else _CHOICES,
                    instructions=question.instructions,
                    type=question.kind,
                )
                for question in questions
            },
        ),
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


@dataclass
class _Invocation:
    wire: PreparedDecisionsRequestV4
    forwarded: bool = False
    received: tuple[int, bytes] | None = None


_ACTIVE: ContextVar[_Invocation | None] = ContextVar(
    "gemini_decisions_invocation", default=None
)


class GeminiDecisionsAdapter:
    def __init__(
        self,
        model: str,
        *,
        concurrency: int = 4,
        context: GeminiDecisionsContextV1 | None = None,
        http_transport: object | None = None,
    ) -> None:
        if concurrency < 1:
            raise ValueError("Gemini Decisions concurrency must be positive")
        self.model = model
        self.concurrency = concurrency
        self.transport_context = context or GeminiDecisionsContextV1()
        self.http_transport = http_transport
        self._key: str | None = None
        self._client: Any = None
        self._invoked: deque[str] = deque(maxlen=2 * concurrency)
        self._responses: dict[
            str, tuple[ProviderAcknowledgmentV5, ProviderResponse]
        ] = {}
        self.actual_call_count = 0

    @staticmethod
    def capabilities_for(provider: str, endpoint: str, model: str) -> CapabilityProfile:
        if (
            provider,
            endpoint,
        ) != GEMINI_DECISIONS_ROUTE or model != GEMINI_DECISIONS_MODEL:
            raise ProviderError("Unsupported Gemini Decisions route or model")
        return CapabilityProfile(
            grouped=True,
            max_levels=255,
            explanations=False,
            ties=False,
            modes=("live",),
        )

    def capabilities(self, spec: RunSpecification) -> CapabilityProfile:
        return bulk_capabilities(
            spec, self.capabilities_for(spec.provider, spec.endpoint, spec.model)
        )

    def validate(self, spec: RunSpecification) -> None:
        expected = decisions_context(spec)
        # The sealed context records whether a now-materialized default was omitted.
        if (
            expected.account_scope != self.transport_context.account_scope
            or spec.model != self.model
        ):
            raise ProviderIntegrityError("Gemini Decisions runtime scope differs")

    def validate_credentials(self) -> None:
        if self._key is not None:
            return
        try:
            installed = gemini_sdk_version()
        except ProviderError:
            raise GeminiReadinessError("missing_extra") from None
        if installed != self.transport_context.sdk_version:
            raise GeminiReadinessError("sdk_scope_mismatch")
        try:
            self._key = _resolve_credentials("gemini")._api_key()  # pyright: ignore[reportPrivateUsage]
        except ValueError:
            raise GeminiReadinessError("missing_credentials") from None

    def prepare(self, *_args: Any, **_kwargs: Any) -> PreparedRequest:
        raise ProviderError("Gemini Decisions requires sealed preparation")

    async def _execution_client(self) -> Any:
        if self._client is not None:
            return self._client
        self.validate_credentials()
        key = self._key
        assert key is not None
        sdk, http = _sdk()
        adapter = self
        underlying = self.http_transport or http.AsyncHTTPTransport(
            retries=0,
            trust_env=False,
            limits=http.Limits(
                max_connections=self.concurrency,
                max_keepalive_connections=self.concurrency,
            ),
        )

        class Guarded(http.AsyncBaseTransport):
            async def handle_async_request(self, request: Any) -> Any:
                active = _ACTIVE.get()
                if (
                    active is None
                    or active.forwarded
                    or request.method != "POST"
                    or str(request.url) != adapter.transport_context.request_url
                    or request.headers.get_list("x-goog-api-key") != [key]
                    or request.headers.get_list("accept-encoding") != ["identity"]
                    or set(request.headers)
                    - {
                        "host",
                        "accept",
                        "accept-encoding",
                        "connection",
                        "content-type",
                        "user-agent",
                        "x-goog-api-key",
                        "x-goog-api-client",
                        "content-length",
                    }
                    or request.content != active.wire.payload
                    or len(request.content) > GEMINI_DECISIONS_BODY_CAP
                ):
                    raise ProviderIntegrityError(
                        "Gemini Decisions SDK forwarding differs from sealed request"
                    )
                request.extensions["timeout"] = {
                    "connect": 10.0,
                    "write": 10.0,
                    "read": 60.0,
                    "pool": 10.0,
                }
                with scoring_forward(
                    active.wire, active.wire.transport_commitment.identity.submission_id
                ):
                    active.forwarded = True
                    adapter.actual_call_count += 1
                    response = await cast(Any, underlying).handle_async_request(request)
                    body = bytearray()
                    try:
                        stream = response.stream
                        if (
                            not isinstance(stream, http.AsyncByteStream)
                            or response.headers.get(
                                "content-encoding", "identity"
                            ).lower()
                            != "identity"
                        ):
                            raise SubmissionUncertain(invoked=True)
                        # Capture EOF before closing so cancellation cannot discard a complete response.
                        async for chunk in stream:
                            if len(body) + len(chunk) > _RESPONSE_LIMIT:
                                raise SubmissionUncertain(invoked=True)
                            body.extend(chunk)
                        lengths = response.headers.get_list("content-length")
                        if lengths and (
                            len(lengths) != 1
                            or not re.fullmatch(r"[0-9]{1,10}", lengths[0])
                            or int(lengths[0]) != len(body)
                        ):
                            raise SubmissionUncertain(invoked=True)
                        raw = bytes(body)
                        if _contains_credential(raw, key):
                            raise SubmissionUncertain(invoked=True)
                        active.received = response.status_code, raw
                    finally:
                        await response.aclose()
                    return http.Response(
                        response.status_code,
                        content=raw,
                        request=request,
                        headers={
                            "content-type": response.headers.get(
                                "content-type", "application/json"
                            )
                        },
                    )

            async def aclose(self) -> None:
                await cast(Any, underlying).aclose()

        http_client = http.AsyncClient(
            transport=Guarded(),
            trust_env=False,
            follow_redirects=False,
            headers={"accept-encoding": "identity"},
        )
        try:
            self._client = _protected_client(
                sdk, http, key=key, http_client=http_client
            )
        except BaseException:
            await http_client.aclose()
            raise
        return self._client

    async def submit(
        self, prepared: PreparedRequest, *, submission_id: str, correlation_id: str
    ) -> ProviderAcknowledgmentV5:
        if type(prepared) is not PreparedDecisionsRequestV4:
            raise ProviderIntegrityError(
                "Gemini Decisions requires production preparation 11"
            )
        wire = PreparedDecisionsRequestV4.model_validate(dict(prepared))
        commitment = wire.transport_commitment
        identity = commitment.identity
        request = cast(dict[str, Any], decode_assessment_output(wire.payload))
        if (
            (wire.provider, wire.endpoint) != GEMINI_DECISIONS_ROUTE
            or wire.model != self.model
            or (identity.submission_id, identity.correlation_id)
            != (submission_id, correlation_id)
            or commitment.context_sha256
            != semantic_digest("decisions-context-v1", self.transport_context)
            or commitment.limits != self.transport_context.limits
            or commitment.probability_sum_tolerance
            != self.transport_context.probability_sum_tolerance
            or len(wire.payload) > GEMINI_DECISIONS_BODY_CAP
            or not 1 <= len(commitment.questions) <= commitment.limits.max_questions
            or set(request) != {"model", "input", "questions"}
            or request["model"] != wire.model
            or not isinstance(request["input"], str)
            or not isinstance(request["questions"], dict)
            or tuple(cast(dict[str, object], request["questions"]))
            != tuple(q.key for q in commitment.questions)
            or any(_QUESTION_KEY.fullmatch(q.key) is None for q in commitment.questions)
            or submission_id in self._invoked
            or len(self._responses) >= self.concurrency
        ):
            raise ProviderIntegrityError(
                "Gemini Decisions invocation differs or handoff is full"
            )
        self._invoked.append(submission_id)
        state = _Invocation(wire)
        active = _ACTIVE.set(state)
        logging = _SDK_LOGGING.set(True)
        cancelled = False
        try:
            client = await self._execution_client()
            async with asyncio.timeout(90):
                await client.aio.decisions.with_raw_response.create(**request)
        except asyncio.CancelledError:
            cancelled = True
        except ProviderIntegrityError:
            if not state.forwarded:
                raise
        except GeminiReadinessError:
            raise
        except Exception:
            # The guarded HTTP response is authoritative independently of the SDK parser.
            pass
        finally:
            _ACTIVE.reset(active)
            _SDK_LOGGING.reset(logging)

        if state.received is not None:
            status, raw = state.received
            rejection = rejected(status, raw, wire)
            if rejection is not None:
                if cancelled:
                    raise GeminiDecisionsRejectionCancelled(rejection)
                raise rejection
            if status == 200:
                replay, safe, usage, kind = analyze(raw, wire)
                permitted = raw if safe else canonical_bytes(replay)
                if len(permitted) <= _RESPONSE_LIMIT:
                    response = ProviderResponse(
                        observation_key=observation_key(wire),
                        kind=kind,
                        raw=raw,
                        evidence=ProviderEvidence(
                            provenance="provider_documented",
                            usage=usage,
                            observation_context=ProviderObservationContext(
                                sdk_version=self.transport_context.sdk_version,
                                invocation_kind="live",
                            ),
                            safe_response=permitted,
                            original_body_safe=safe,
                            decisions_validation=replay.metadata,
                            decisions_retained_kind="original"
                            if safe
                            else "filtered_replay",
                        ),
                    )
                    ack = ProviderAcknowledgmentV5(
                        remote_id=None,
                        observation_key=response.observation_key,
                        response_evidence=CompleteResponseEvidenceV4(
                            **identity.model_dump(exclude={"identity_version"}),
                            payload_sha256=wire.payload_sha256,
                            response_sha256=replay.metadata.original_sha256,
                            response_byte_length=len(raw),
                        ),
                    )
                    self._responses[submission_id] = ack, response
                    if cancelled:
                        raise ProviderInvocationCancelled(invoked=True)
                    return ack
        if cancelled:
            raise ProviderInvocationCancelled(invoked=state.forwarded)
        raise SubmissionUncertain(invoked=state.forwarded)

    def _received_response_for_cancellation(
        self, prepared: PreparedRequest, *, submission_id: str, correlation_id: str
    ) -> ProviderResponse | None:
        found = self._responses.get(submission_id)
        return found[1] if found is not None else None

    def _received_acknowledgment_for_cancellation(
        self, prepared: PreparedRequest, *, submission_id: str, correlation_id: str
    ) -> ProviderAcknowledgmentV5 | None:
        found = self._responses.get(submission_id)
        return found[0] if found is not None else None

    async def retrieve(
        self, prepared: PreparedRequest, *, submission_id: str, correlation_id: str
    ) -> ProviderResponse:
        found = self._responses.pop(submission_id, None)
        if found is None:
            raise RetrievalUnavailable()
        if (
            found[0].response_evidence.correlation_id != correlation_id
            or found[0].response_evidence.payload_sha256 != prepared.payload_sha256
        ):
            raise ProviderIntegrityError("Gemini Decisions retrieval identity differs")
        return found[1]

    async def reconcile(
        self, *_args: Any, **_kwargs: Any
    ) -> DecisionsReconciliationEvidenceV1:
        return DecisionsReconciliationEvidenceV1(outcome="unresolved")

    def parse_decisions(
        self, response: ProviderResponse, prepared: PreparedDecisionsRequestV4
    ) -> ParsedDecisionsResponseV2:
        if response.decisions_replay is None:
            replay, _, _, kind = analyze(response.raw, prepared)
            if response.kind != kind:
                raise ProviderIntegrityError(
                    "Gemini Decisions outcome differs from the raw response"
                )
        else:
            replay = response.decisions_replay
        if not isinstance(replay, GeminiDecisionsReplayV1):
            raise ProviderIntegrityError("Gemini Decisions replay protocol differs")
        verify_replay(replay, prepared)
        return ParsedDecisionsResponseV2(
            identity=prepared.transport_commitment.identity,
            payload_sha256=prepared.payload_sha256,
            response_sha256=replay.metadata.original_sha256,
            kind=response.kind,
            recognizable=replay.metadata.recognizable,
            reported_model=None,
            answer_order="map",
            answers=tuple(answer for answer in replay.answers if answer is not None),
            metadata=replay.metadata,
        )

    def parse(self, *_args: Any, **_kwargs: Any) -> Any:
        raise ProviderError("Gemini Decisions requires named-member interpretation")

    async def aclose(self) -> None:
        if self._client is not None:
            token = _SDK_LOGGING.set(True)
            try:
                await _close_client(self._client)
            finally:
                _SDK_LOGGING.reset(token)
                self._client = None
                self._key = None
                self._responses.clear()
