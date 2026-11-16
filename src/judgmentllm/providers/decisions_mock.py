"""Offline Decisions service. No SDK, credentials, sockets or vendor wire shape."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Literal, cast

from pydantic import Field

from judgmentllm.config import (
    RunSpecification,
    RunSpecV4,
    parse_json,
    validate_run_spec,
)
from judgmentllm.decisions.transport import (
    DecisionsContextV1,
    PreparedDecisionsRequestV1,
)
from judgmentllm.evidence import ReportedUsageInput
from judgmentllm.io import _revalidate_record  # pyright: ignore[reportPrivateUsage]
from judgmentllm.persistence.hashing import (
    bytes_digest,
    canonical_bytes,
    semantic_digest,
)
from judgmentllm.persistence.models import ExecutionModel
from judgmentllm.persistence.receipts import CompleteResponseEvidenceV1
from judgmentllm.planning import CapabilityProfile, stable_id
from judgmentllm.providers.evidence import ProviderEvidence, ProviderObservationContext
from judgmentllm.providers.ledger import (
    AcceptanceRecord,
    MockAcceptanceLedger,
    ObservationRecipe,
)
from judgmentllm.providers.protocol import (
    DecisionsReconciliationEvidenceV1,
    ParsedResponse,
    PreparedRequest,
    ProviderAcknowledgmentV2,
    ProviderError,
    ProviderIntegrityError,
    ProviderResponse,
    RetrievalUnavailable,
    SubmissionUncertain,
    TransientNotSubmitted,
    TransportNotSubmitted,
)


class DecisionsMemberScenarioV1(ExecutionModel):
    outcome: Literal[
        "valid",
        "missing",
        "duplicate",
        "wrong_type",
        "invalid_distribution",
        "refusal",
        "no_selection",
        "bad_legend",
    ] = "valid"
    selected_index: int | None = Field(default=None, ge=0)
    winner: Literal["essay_01", "essay_02"] = "essay_02"
    confidence: float | None = Field(default=0.8, ge=0, le=1)


class DecisionsScenarioV1(ExecutionModel):
    fixture_version: Literal[1] = 1
    members: dict[str, DecisionsMemberScenarioV1] = Field(default_factory=dict)
    envelope: Literal[
        "valid",
        "malformed_json",
        "duplicate_json_key",
        "truncated_json",
        "wrong_model",
        "unbound",
        "unexpected",
        "reverse",
        "refusal",
        "error",
    ] = "valid"
    answer_order: Literal["map", "ordered_array"] = "map"
    transport: Literal[
        "success",
        "not_submitted",
        "transient_not_submitted",
        "timeout_before",
        "timeout_after",
        "partial_reception",
    ] = "success"
    extra_forward: Literal["retry", "redirect", "continuation"] | None = None
    remote_id: bool = False
    usage: ReportedUsageInput = Field(
        default_factory=lambda: ReportedUsageInput(usage_source="unknown")
    )


class InvocationGuard:
    """One receiver entry after the executor's durable submitting boundary."""

    def __init__(self, wire: PreparedDecisionsRequestV1):
        self.identity = wire.transport_commitment.identity
        self.payload_sha256 = wire.payload_sha256
        self.consumed = False

    def enter(self, wire: PreparedDecisionsRequestV1) -> None:
        if (
            self.consumed
            or self.identity != wire.transport_commitment.identity
            or self.payload_sha256 != wire.payload_sha256
            or bytes_digest(wire.payload) != self.payload_sha256
        ):
            raise ProviderIntegrityError(
                "unauthorized extra or mismatched Decisions forwarding"
            )
        self.consumed = True


class MockDecisionsAdapter:
    """Own synthetic named-answer protocol with an independent acceptance ledger."""

    def __init__(
        self,
        ledger_path: str | Path,
        *,
        scenario: DecisionsScenarioV1 | None = None,
        context: DecisionsContextV1 | None = None,
        retrieval_available: bool = True,
        reconciliation_available: bool = True,
        receiver_observer: Callable[[str], None] | None = None,
    ):
        self.ledger = MockAcceptanceLedger(ledger_path)
        self.transport_context = context or DecisionsContextV1(
            service_identity=self.ledger.identity
        )
        if self.transport_context.service_identity != self.ledger.identity:
            raise ProviderIntegrityError("synthetic Decisions service identity differs")
        self.scenario = _revalidate_record(scenario or DecisionsScenarioV1())
        self.retrieval_available = retrieval_available
        self.reconciliation_available = reconciliation_available
        self.receiver_observer = receiver_observer
        self.submit_calls = self.forward_entries = 0
        self._responses: dict[
            str, tuple[ProviderAcknowledgmentV2, ProviderResponse]
        ] = {}

    @staticmethod
    def capabilities_for(provider: str, endpoint: str, model: str) -> CapabilityProfile:
        if (provider, endpoint) != ("mock", "synthetic"):
            raise ProviderError("PR B implements synthetic Decisions only")
        return CapabilityProfile(
            grouped=True, explanations=False, ties=False, modes=("live",)
        )

    def capabilities(self, spec: RunSpecification) -> CapabilityProfile:
        return self.capabilities_for(spec.provider, spec.endpoint, spec.model)

    def validate(self, spec: RunSpecification) -> None:
        checked = validate_run_spec(spec)
        if not isinstance(checked, RunSpecV4) or checked.execution.tier is not None:
            raise ProviderError(
                "synthetic Decisions requires V4 without a service tier"
            )
        self.capabilities(spec)

    def prepare(
        self, spec: RunSpecification, dependency: object, rendered: object
    ) -> PreparedRequest:
        raise ProviderError("Decisions requests must be sealed before submission")

    def _fixture_digest(self) -> str:
        return semantic_digest(
            "decisions-scenario-v1",
            self.scenario.model_dump(
                mode="json", exclude={"transport", "extra_forward"}
            ),
        )

    def _wire(
        self, prepared: PreparedRequest, submission_id: str, correlation_id: str
    ) -> PreparedDecisionsRequestV1:
        if not isinstance(prepared, PreparedDecisionsRequestV1):
            raise ProviderIntegrityError(
                "Decisions requires its independently versioned request"
            )
        wire = _revalidate_record(prepared)
        identity = wire.transport_commitment.identity
        if (identity.submission_id, identity.correlation_id) != (
            submission_id,
            correlation_id,
        ) or wire.transport_commitment.context_sha256 != semantic_digest(
            "decisions-context-v1", self.transport_context
        ):
            raise ProviderIntegrityError(
                "Decisions invocation differs from sealed context"
            )
        return wire

    def _response(
        self, wire: PreparedDecisionsRequestV1, observation_key: str
    ) -> ProviderResponse:
        payload = cast(dict[str, object], parse_json(wire.payload.decode("utf-8")))
        questions = cast(list[dict[str, object]], payload["questions"])
        answers: list[dict[str, object]] = []
        for q in questions:
            commitment = next(
                c for c in wire.transport_commitment.questions if c.key == q["name"]
            )
            scenario = self.scenario.members.get(
                commitment.trait_id, DecisionsMemberScenarioV1()
            )
            if scenario.outcome == "missing":
                continue
            if q["type"] == "score":
                trait = cast(dict[str, object], q["levels"])
                levels = cast(list[dict[str, object]], trait["levels"])
                weights = (
                    [0.10, 0.25, 0.65]
                    if len(levels) == 3
                    else [1.0 / len(levels)] * len(levels)
                )
                answer: dict[str, object] = {
                    "name": q["name"],
                    "type": "score",
                    "probabilities": [
                        {"index": i, "probability": p} for i, p in enumerate(weights)
                    ],
                    "legend": [
                        {
                            "index": i,
                            "label": level["label"],
                            "description": level["description"],
                        }
                        for i, level in enumerate(levels)
                    ],
                    "expected_index": sum(i * p for i, p in enumerate(weights)),
                    "selected_index": scenario.selected_index,
                    "confidence": scenario.confidence,
                }
            else:
                answer = {
                    "name": q["name"],
                    "type": "choice",
                    "probabilities": [
                        {"option": "essay_01", "probability": 0.2},
                        {"option": "essay_02", "probability": 0.8},
                    ],
                    "selection": None
                    if scenario.outcome == "no_selection"
                    else scenario.winner,
                    "confidence": scenario.confidence,
                }
            if scenario.outcome == "wrong_type":
                answer["type"] = "choice" if q["type"] == "score" else "score"
            elif scenario.outcome == "invalid_distribution":
                cast(list[dict[str, object]], answer["probabilities"])[0][
                    "probability"
                ] = -1
            elif scenario.outcome == "bad_legend" and q["type"] == "score":
                cast(list[dict[str, object]], answer["legend"])[0]["description"] = (
                    "foreign description"
                )
            elif scenario.outcome == "refusal":
                answer = {"name": q["name"], "type": "refusal"}
            answers.append(answer)
            if scenario.outcome == "duplicate":
                answers.append(answer.copy())
        if self.scenario.envelope == "reverse":
            answers.reverse()
        if self.scenario.envelope == "unexpected":
            answers.append({"name": "unrecognized-synthetic-answer", "type": "refusal"})
        identity = wire.transport_commitment.identity
        envelope: dict[str, object] = {
            "protocol": self.transport_context.protocol,
            "model": "conflicting-model"
            if self.scenario.envelope == "wrong_model"
            else wire.model,
            "submission_id": "foreign"
            if self.scenario.envelope == "unbound"
            else identity.submission_id,
            "correlation_id": identity.correlation_id,
            "payload_sha256": wire.payload_sha256,
            "answer_order": self.scenario.answer_order,
            "answers": answers,
        }
        kind: Literal["response", "refusal", "provider_error"] = (
            "refusal"
            if self.scenario.envelope == "refusal"
            else "provider_error"
            if self.scenario.envelope == "error"
            else "response"
        )
        raw = canonical_bytes(envelope)
        if self.scenario.envelope == "malformed_json":
            raw = b"{malformed synthetic output"
        elif self.scenario.envelope == "duplicate_json_key":
            raw = raw.replace(b'"answers":', b'"answers":[],"answ\\u0065rs":', 1)
        elif self.scenario.envelope == "truncated_json":
            raw = raw[:-1]
        return ProviderResponse(
            observation_key=observation_key,
            kind=kind,
            raw=raw,
            evidence=ProviderEvidence(
                provenance="synthetic_fixture",
                usage=self.scenario.usage,
                observation_context=ProviderObservationContext(
                    reported_model=cast(str, envelope["model"]), invocation_kind="live"
                ),
                safe_response=raw,
                original_body_safe=True,
            ),
        )

    def _ack(
        self, wire: PreparedDecisionsRequestV1, response: ProviderResponse
    ) -> ProviderAcknowledgmentV2:
        identity = wire.transport_commitment.identity
        return ProviderAcknowledgmentV2(
            remote_id=stable_id("mock-decisions-remote", identity.submission_id)
            if self.scenario.remote_id
            else None,
            observation_key=response.observation_key,
            response_evidence=CompleteResponseEvidenceV1(
                **identity.model_dump(exclude={"schema_version", "identity_version"}),
                payload_sha256=wire.payload_sha256,
                response_sha256=bytes_digest(response.raw),
                response_byte_length=len(response.raw),
                http_status=200,
                protocol=self.transport_context.protocol,
            ),
        )

    async def _receive(
        self, wire: PreparedDecisionsRequestV1, guard: InvocationGuard
    ) -> tuple[ProviderAcknowledgmentV2, ProviderResponse]:
        guard.enter(wire)
        self.forward_entries += 1
        if self.receiver_observer is not None:
            self.receiver_observer(wire.transport_commitment.identity.submission_id)
        identity = wire.transport_commitment.identity
        key = stable_id("local-decisions-observation-v1", identity.submission_id)
        response = self._response(wire, key)
        accepted = self.scenario.transport not in {
            "not_submitted",
            "transient_not_submitted",
        }
        recipe = (
            ObservationRecipe(
                fixture_sha256=self._fixture_digest(),
                outcome="valid",
                selection_index=0,
                winner="second",
                probabilities=True,
                response_sha256=bytes_digest(response.raw),
            )
            if accepted
            else None
        )
        record = AcceptanceRecord(
            submission_id=identity.submission_id,
            correlation_id=identity.correlation_id,
            payload_sha256=wire.payload_sha256,
            accepted=accepted,
            observation_key=key if accepted else None,
            recipe=recipe,
            acceptance_count=int(accepted),
            retrieval_status="available" if accepted else "not_accepted",
        )
        pending = asyncio.create_task(
            asyncio.to_thread(
                self.ledger.establish,
                record,
                service_identity=self.transport_context.service_identity,
            )
        )
        try:
            await asyncio.shield(pending)
        except asyncio.CancelledError:
            while not pending.done():
                try:
                    await asyncio.shield(pending)
                except asyncio.CancelledError:
                    continue
            pending.result()
            raise
        if not accepted:
            if self.scenario.transport == "transient_not_submitted":
                raise TransientNotSubmitted()
            raise TransportNotSubmitted()
        return self._ack(wire, response), response

    async def submit(
        self, prepared: PreparedRequest, *, submission_id: str, correlation_id: str
    ) -> ProviderAcknowledgmentV2:
        wire = self._wire(prepared, submission_id, correlation_id)
        self.submit_calls += 1
        if self.scenario.transport == "timeout_before":
            raise SubmissionUncertain(invoked=False)
        guard = InvocationGuard(wire)
        ack, response = await self._receive(wire, guard)
        if self.scenario.transport in {"timeout_after", "partial_reception"}:
            raise SubmissionUncertain(invoked=True, usage=self.scenario.usage)
        self._responses[submission_id] = (ack, response)
        if self.scenario.extra_forward is not None:
            # Retry, redirect and continuation scripts all reach this same guard.
            await self._receive(wire, guard)
        return ack

    def _received_response_for_cancellation(
        self, prepared: PreparedRequest, *, submission_id: str, correlation_id: str
    ) -> ProviderResponse | None:
        self._wire(prepared, submission_id, correlation_id)
        pair = self._responses.get(submission_id)
        return pair[1] if pair else None

    def _received_acknowledgment_for_cancellation(
        self, prepared: PreparedRequest, *, submission_id: str, correlation_id: str
    ) -> ProviderAcknowledgmentV2 | None:
        self._wire(prepared, submission_id, correlation_id)
        received = self._responses.get(submission_id)
        return received[0] if received else None

    async def reconcile(
        self, prepared: PreparedRequest, *, submission_id: str, correlation_id: str
    ) -> DecisionsReconciliationEvidenceV1:
        wire = self._wire(prepared, submission_id, correlation_id)
        if not self.reconciliation_available:
            return DecisionsReconciliationEvidenceV1(outcome="unresolved")
        record = await asyncio.to_thread(
            self.ledger.get,
            submission_id,
            service_identity=self.transport_context.service_identity,
        )
        if record is None:
            return DecisionsReconciliationEvidenceV1(outcome="unresolved")
        if (record.correlation_id, record.payload_sha256) != (
            correlation_id,
            wire.payload_sha256,
        ):
            raise ProviderIntegrityError("Decisions reconciliation identity conflicts")
        if not record.accepted:
            return DecisionsReconciliationEvidenceV1(outcome="not_submitted")
        if not self.retrieval_available:
            return DecisionsReconciliationEvidenceV1(outcome="unresolved")
        response = await self.retrieve(
            wire, submission_id=submission_id, correlation_id=correlation_id
        )
        ack = self._ack(wire, response)
        return DecisionsReconciliationEvidenceV1(
            outcome="observation_available",
            remote_id=ack.remote_id,
            observation_key=ack.observation_key,
            acknowledgment=ack,
        )

    async def retrieve(
        self, prepared: PreparedRequest, *, submission_id: str, correlation_id: str
    ) -> ProviderResponse:
        wire = self._wire(prepared, submission_id, correlation_id)
        received = self._responses.pop(submission_id, None)
        if received is not None:
            return received[1]
        if not self.retrieval_available:
            raise RetrievalUnavailable()
        record = await asyncio.to_thread(
            self.ledger.get,
            submission_id,
            service_identity=self.transport_context.service_identity,
        )
        if (
            record is None
            or not record.accepted
            or record.recipe is None
            or record.observation_key is None
        ):
            raise RetrievalUnavailable()
        response = self._response(wire, record.observation_key)
        if (
            record.correlation_id,
            record.payload_sha256,
            record.recipe.fixture_sha256,
            record.recipe.response_sha256,
        ) != (
            correlation_id,
            wire.payload_sha256,
            self._fixture_digest(),
            bytes_digest(response.raw),
        ):
            raise ProviderIntegrityError(
                "original Decisions observation differs from fixture"
            )
        return response

    def parse_decisions(
        self, response: ProviderResponse, prepared: PreparedDecisionsRequestV1
    ):
        from judgmentllm.providers.decisions_wire import parse_synthetic_decisions

        return parse_synthetic_decisions(response, prepared)

    def parse(
        self, response: ProviderResponse, *, strict_output: bool = False
    ) -> ParsedResponse:
        raise ProviderError(
            "Decisions interpretation requires its committed named-question crosswalk"
        )
