"""Official, single-forwarding OpenAI Decisions adapter over sealed grouped work."""

from __future__ import annotations

# pyright: reportPrivateUsage=false
import asyncio
from collections import deque
from collections.abc import Sequence
from contextvars import ContextVar
from typing import Any, cast

from judgmentllm._auth import _resolve_credentials
from judgmentllm.config import RunSpecification, RunSpecV4, validate_run_spec
from judgmentllm.decisions.records import DecisionQuestionV1, OpenAIDecisionsContextV1
from judgmentllm.decisions.responses import ParsedDecisionsResponseV2
from judgmentllm.decisions.transport import PreparedDecisionsRequestV2
from judgmentllm.evidence.usage import (
    OpenAIResponsesUsageInput,
    ReportedUsageInput,
    interpret_openai_responses_usage,
)
from judgmentllm.output_commitments import ordered_json_bytes
from judgmentllm.output_contracts import decode_assessment_output
from judgmentllm.persistence.hashing import (
    bytes_digest,
    canonical_bytes,
    semantic_digest,
)
from judgmentllm.persistence.receipts import CompleteResponseEvidenceV2
from judgmentllm.planning import CapabilityProfile
from judgmentllm.providers.evidence import ProviderEvidence, ProviderObservationContext
from judgmentllm.providers.openai import (
    _SDK_LOGGING,
    OpenAIConfigurationError,
    OpenAIReadinessError,
    OpenAIRequestRejected,
    _protected_client,
    _sdk,
    openai_sdk_version,
)
from judgmentllm.providers.openai_decisions_response import (
    analyze,
    observation_key,
    verify_replay,
)
from judgmentllm.providers.protocol import (
    DecisionsReconciliationEvidenceV1,
    PreparedRequest,
    ProviderAcknowledgmentV3,
    ProviderError,
    ProviderIntegrityError,
    ProviderInvocationCancelled,
    ProviderResponse,
    RetrievalUnavailable,
    SubmissionUncertain,
)

_ACTIVE: ContextVar[dict[str, Any] | None] = ContextVar(
    "openai_decisions_invocation", default=None
)


def decisions_context(spec: RunSpecification) -> OpenAIDecisionsContextV1:
    checked = validate_run_spec(spec)
    if not isinstance(checked, RunSpecV4) or (checked.provider, checked.endpoint) != (
        "openai",
        "decisions",
    ):
        raise OpenAIConfigurationError(("schema_version",))
    if checked.execution.tier is not None:
        raise OpenAIConfigurationError(("execution.tier",))
    if checked.rubric is not None and any(
        not 2 <= len(t.levels) <= 10 for t in checked.rubric.traits
    ):
        raise ProviderError("OpenAI Decisions requires 2–10 ordered levels per trait")
    if checked.strict_capabilities:
        raise ProviderError(
            "Decisions context ceiling is unknown under strict capabilities"
        )
    return OpenAIDecisionsContextV1(account_scope=checked.account_scope)


def serialize_openai_decisions(
    shared: str, questions: Sequence[DecisionQuestionV1], spec: RunSpecV4
) -> bytes:
    decisions_context(spec)
    return ordered_json_bytes(
        dict(
            model=spec.model,
            input=shared,
            questions=[
                dict(
                    type=q.kind,
                    name=q.key,
                    instructions=q.instructions,
                    **(
                        {
                            "levels": [
                                dict(label=level.label, description=level.description)
                                for level in q.trait.levels
                            ]
                        }
                        if q.trait
                        else {
                            "choices": [
                                dict(
                                    value="essay_01",
                                    description="The first presented essay.",
                                ),
                                dict(
                                    value="essay_02",
                                    description="The second presented essay.",
                                ),
                            ]
                        }
                    ),
                )
                for q in questions
            ],
            prompt_cache_options={"mode": "explicit"},
        )
    )


def usage_from(raw: bytes) -> ReportedUsageInput | None:
    try:
        doc = cast(dict[str, Any], decode_assessment_output(raw))
        parsed = OpenAIResponsesUsageInput.model_validate(doc.get("usage") or {})
        values = interpret_openai_responses_usage(parsed).model_dump()
        values["usage_source"] = "openai.decisions.usage"
        for detail in values["breakdown"]:
            detail["usage_source"] = "openai.decisions.usage"
        return ReportedUsageInput.model_validate(values)
    except (ValueError, TypeError, AttributeError):
        return None


class OpenAIDecisionsAdapter:
    def __init__(
        self,
        model: str,
        *,
        context: OpenAIDecisionsContextV1 | None = None,
        concurrency: int = 4,
        http_transport: object | None = None,
    ):
        self.model = model
        self.transport_context = context or OpenAIDecisionsContextV1()
        self.concurrency = concurrency
        self.http_transport = http_transport
        self._client: Any = None
        self._responses: dict[
            str, tuple[ProviderAcknowledgmentV3, ProviderResponse]
        ] = {}
        self.actual_call_count = 0
        self._invoked: deque[str] = deque(maxlen=2 * concurrency)

    @staticmethod
    def capabilities_for(provider: str, endpoint: str, model: str) -> CapabilityProfile:
        if (provider, endpoint) != ("openai", "decisions"):
            raise ProviderError("Unsupported OpenAI Decisions route")
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
            raise ProviderIntegrityError("Decisions runtime scope differs")

    def validate_credentials(self) -> None:
        if openai_sdk_version() != self.transport_context.sdk_version:
            raise OpenAIReadinessError("sdk_scope_mismatch")
        try:
            _resolve_credentials("openai")
        except ValueError:
            raise OpenAIReadinessError("missing_credentials") from None

    def prepare(self, *_args: Any, **_kwargs: Any) -> PreparedRequest:
        raise ProviderError("Decisions requires sealed preparation")

    async def _execution_client(self) -> Any:
        if self._client is not None:
            return self._client
        self.validate_credentials()
        sdk, http = _sdk()
        key = _resolve_credentials("openai")._api_key()
        underlying: Any = self.http_transport or http.AsyncHTTPTransport(
            retries=0,
            limits=http.Limits(
                max_connections=self.concurrency,
                max_keepalive_connections=self.concurrency,
            ),
        )
        adapter = self

        class Guarded(http.AsyncBaseTransport):
            async def handle_async_request(self, request: Any) -> Any:
                state = _ACTIVE.get()
                if (
                    state is None
                    or state["invoked"]
                    or request.method != "POST"
                    or str(request.url) != "https://api.openai.com/v1/decisions"
                    or request.content != state["wire"].payload
                    or request.headers.get("authorization") != f"Bearer {key}"
                ):
                    raise ProviderIntegrityError(
                        "Decisions SDK forwarding differs from sealed request"
                    )
                state["invoked"] = True
                adapter.actual_call_count += 1
                response = await underlying.handle_async_request(request)
                body = bytearray()
                try:
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > 8_388_608:
                            raise SubmissionUncertain(invoked=True)
                        body.extend(chunk)
                finally:
                    await response.aclose()
                state["received"] = (response.status_code, bytes(body))
                return http.Response(
                    response.status_code, content=bytes(body), request=request
                )

            async def aclose(self) -> None:
                await underlying.aclose()

        client = http.AsyncClient(
            transport=Guarded(),
            timeout=http.Timeout(60, connect=10, write=10, pool=10),
            follow_redirects=False,
            trust_env=False,
        )
        self._client = _protected_client(
            sdk,
            api_key=key,
            base_url="https://api.openai.com/v1",
            max_retries=0,
            http_client=client,
        )
        return self._client

    async def submit(
        self, prepared: PreparedRequest, *, submission_id: str, correlation_id: str
    ) -> ProviderAcknowledgmentV3:
        if not isinstance(prepared, PreparedDecisionsRequestV2):
            raise ProviderIntegrityError("Decisions requires production preparation 9")
        wire = PreparedDecisionsRequestV2.model_validate(dict(prepared))
        identity = wire.transport_commitment.identity
        if (
            (
                identity.submission_id,
                identity.correlation_id,
                wire.provider,
                wire.endpoint,
                wire.model,
                wire.transport_commitment.context_sha256,
            )
            != (
                submission_id,
                correlation_id,
                "openai",
                "decisions",
                self.model,
                semantic_digest("decisions-context-v1", self.transport_context),
            )
            or submission_id in self._invoked
            or len(self._responses) >= self.concurrency
        ):
            raise ProviderIntegrityError(
                "Decisions invocation identity differs or handoff is full"
            )
        self.validate_credentials()
        self._invoked.append(submission_id)
        client = await self._execution_client()
        state: dict[str, Any] = {"wire": wire, "invoked": False, "received": None}
        active = _ACTIVE.set(state)
        logging = _SDK_LOGGING.set(True)
        cancelled = False
        try:
            await asyncio.wait_for(
                client.decisions.with_raw_response.create(
                    **cast(dict[str, Any], decode_assessment_output(wire.payload))
                ),
                timeout=90,
            )
        except BaseException as error:
            if state["received"] is None or not 200 <= state["received"][0] < 300:
                if isinstance(error, asyncio.CancelledError):
                    raise ProviderInvocationCancelled(
                        invoked=state["invoked"]
                    ) from None
                status = state["received"][0] if state["received"] else None
                if status in {400, 401, 403, 404, 422, 429}:
                    codes = {
                        401: "credentials_rejected",
                        403: "permission_denied",
                        404: "model_or_endpoint_unavailable",
                        429: "rate_limited",
                    }
                    raise OpenAIRequestRejected(
                        cast(Any, codes.get(status, "request_rejected"))
                    ) from None
                if isinstance(error, ProviderIntegrityError) and not state["invoked"]:
                    raise
                raise SubmissionUncertain(invoked=state["invoked"]) from None
            if isinstance(error, asyncio.CancelledError):
                cancelled = True
            elif not isinstance(error, Exception):
                raise
            # A complete first response remains authoritative after a later guard defect.
        finally:
            _ACTIVE.reset(active)
            _SDK_LOGGING.reset(logging)
        status, raw = state["received"]
        replay, safe = analyze(raw, wire)
        permitted = raw if safe else canonical_bytes(replay)
        if len(permitted) > 8_388_608:
            raise SubmissionUncertain(invoked=True)
        response = ProviderResponse(
            observation_key=observation_key(wire),
            kind="response",
            raw=raw,
            evidence=ProviderEvidence(
                provenance="provider_documented",
                usage=usage_from(raw),
                observation_context=ProviderObservationContext(
                    reported_model=replay.metadata.reported_model,
                    sdk_version="3.42.1",
                    invocation_kind="live",
                ),
                safe_response=permitted,
                original_body_safe=safe,
                decisions_validation=replay.metadata,
                decisions_retained_kind="original" if safe else "filtered_replay",
            ),
        )
        ack = ProviderAcknowledgmentV3(
            remote_id=None,
            observation_key=response.observation_key,
            response_evidence=CompleteResponseEvidenceV2(
                **identity.model_dump(exclude={"identity_version"}),
                payload_sha256=wire.payload_sha256,
                response_sha256=bytes_digest(raw),
                response_byte_length=len(raw),
                http_status=status,
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
    ) -> ProviderAcknowledgmentV3 | None:
        received = self._responses.get(submission_id)
        return received[0] if received else None

    async def retrieve(
        self, prepared: PreparedRequest, *, submission_id: str, correlation_id: str
    ) -> ProviderResponse:
        received = self._responses.pop(submission_id, None)
        if received is None:
            raise RetrievalUnavailable()
        if received[0].response_evidence.correlation_id != correlation_id:
            raise ProviderIntegrityError("Decisions retrieval identity differs")
        return received[1]

    async def reconcile(
        self, *_args: Any, **_kwargs: Any
    ) -> DecisionsReconciliationEvidenceV1:
        return DecisionsReconciliationEvidenceV1(outcome="unresolved")

    def parse_decisions(
        self, response: ProviderResponse, prepared: PreparedDecisionsRequestV2
    ) -> ParsedDecisionsResponseV2:
        replay = response.decisions_replay or analyze(response.raw, prepared)[0]
        verify_replay(replay, prepared)
        return ParsedDecisionsResponseV2(
            identity=prepared.transport_commitment.identity,
            payload_sha256=prepared.payload_sha256,
            response_sha256=replay.metadata.original_sha256,
            kind=response.kind,
            recognizable=replay.metadata.recognizable,
            reported_model=replay.metadata.reported_model,
            answer_order="ordered_array",
            answers=tuple(a for a in replay.answers if a is not None),
            metadata=replay.metadata,
        )

    def parse(self, *_args: Any, **_kwargs: Any) -> Any:
        raise ProviderError("Decisions requires named-member interpretation")

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None
