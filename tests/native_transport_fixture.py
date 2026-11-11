"""Private offline transport across the actual durable execution services.

The payload is JudgmentLLM's explicitly synthetic protocol, never a real provider
request or serving qualification. Only numeric recipes and hashes enter its
independent acceptance ledger. No SDK, credential resolver or network is used.
"""

# pyright: reportPrivateUsage=false
from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal, cast

from durable_evidence_fixture import specification as historical_specification
from test_native_selection import catalog as openai_catalog
from test_native_selection import evidence as openai_evidence
from test_native_selection import scope as openai_scope
from test_native_v2 import catalog as provider_catalog
from test_native_v2 import evidence as provider_evidence
from test_native_v2 import scope as provider_scope

from judgmentllm.config import RunSpecification
from judgmentllm.generation import GenerationCapabilities, GenerationParameterCapability
from judgmentllm.native_schema_v2 import _ROUTES_V2
from judgmentllm.output_contracts import (
    RubricOutputContract,
    decode_assessment_output,
    wire_winner_ids,
)
from judgmentllm.persistence.hashing import (
    bytes_digest,
    semantic_digest,
)
from judgmentllm.planning import CapabilityProfile, stable_id, validate_run
from judgmentllm.preparation.session import RequestDependency
from judgmentllm.presentation import PresentationVersion
from judgmentllm.prompts import RenderedAssessmentPrompt
from judgmentllm.providers.evidence import ProviderEvidence
from judgmentllm.providers.ledger import (
    AcceptanceRecord,
    MockAcceptanceLedger,
    ObservationRecipe,
)
from judgmentllm.providers.mock import MockMemberScenario, MockScenario
from judgmentllm.providers.protocol import (
    ParsedResponse,
    PreparedRequest,
    ProviderAcknowledgment,
    ProviderError,
    ProviderIntegrityError,
    ProviderResponse,
    ReconciliationEvidence,
    RetrievalUnavailable,
    SubmissionUncertain,
    TransientNotSubmitted,
    TransportNotSubmitted,
)
from judgmentllm.transport import TransportContext

MECHANISMS = (
    "openai.responses.text.format",
    "openai.chat.response_format",
    *(route.mechanism for route in _ROUTES_V2),
)
PRIVATE_TEXT = "synthetic-private-native-transport-response-canary"
PRIVATE_LABEL = "synthetic-private-native-transport-category-canary"


def context_for(
    mechanism: str,
    *,
    supported: bool | None = True,
    trusted: bool = True,
    presentation_version: PresentationVersion = 2,
) -> TransportContext:
    if mechanism.startswith("openai."):
        scope = openai_scope(
            "responses" if ".responses." in mechanism else "chat.completions"
        )
        return TransportContext(
            scope=scope,
            evidence=openai_evidence(scope, supported),
            openai_catalog=replace(
                openai_catalog(scope),
                profiles=tuple(
                    profile.model_copy(
                        update={
                            "features": (
                                *profile.features,
                                "assessment_presentation_v2",
                            )
                        }
                    )
                    if presentation_version == 2
                    else profile
                    for profile in openai_catalog(scope).profiles
                ),
            )
            if trusted
            else None,
        )
    route = next(route for route in _ROUTES_V2 if route.mechanism == mechanism)
    return TransportContext(
        scope=provider_scope(route),
        evidence=provider_evidence(route, supported),
        provider_catalog=replace(
            provider_catalog(route),
            profiles=tuple(
                profile.model_copy(
                    update={
                        "features": (*profile.features, "assessment_presentation_v2")
                    }
                )
                if presentation_version == 2
                else profile
                for profile in provider_catalog(route).profiles
            ),
        )
        if trusted
        else None,
    )


def specification(
    directory: Path,
    mechanism: str = MECHANISMS[0],
    *,
    capture: Literal["full", "minimal"] = "minimal",
    policy: str = "native",
    order: str = "together",
    traits: int = 2,
    grouping: str = "grouped",
    explanations: bool = True,
    pairwise: bool = False,
    ties: bool = False,
    strict: bool = False,
) -> Path:
    path = historical_specification(
        directory,
        capture=capture,
        order=order,
        traits=traits,
        grouping=grouping,
        explanations=explanations,
        pairwise=pairwise,
    )
    document = json.loads(path.read_text(encoding="utf-8"))
    context = context_for(mechanism)
    document.update(
        schema_version=3,
        provider=context.scope.provider,
        endpoint=context.scope.endpoint,
        model=context.scope.model,
        strict_capabilities=strict,
        allow_ties=ties,
    )
    document["prompt"].update(structured_output=policy, native_mechanism=mechanism)
    responses = directory / "responses.jsonl"
    responses.write_text(
        "".join(
            json.dumps({"item_id": ident, "text": PRIVATE_TEXT}) + "\n"
            for ident in (("001", "002") if pairwise else ("001",))
        ),
        encoding="utf-8",
    )
    if not pairwise:
        for trait in document["rubric"]["traits"]:
            trait["levels"][0]["label"] = PRIVATE_LABEL
    if ties:
        template = directory / "pairwise.j2"
        template.write_text(
            "Compare the supplied responses.\n{{ criterion }}\n{{ output_instructions }}\n"
            "{{ first_response }}\n{{ second_response }}\n{{ exemplars }}\n",
            encoding="utf-8",
        )
        document["prompt"].update(file=str(template))
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


class NativeFixtureAdapter:
    """Exact synthetic acceptance/retrieval, with no production registration."""

    def __init__(
        self,
        ledger_path: Path,
        mechanism: str = MECHANISMS[0],
        *,
        scenario: MockScenario | None = None,
        context: TransportContext | None = None,
        transform: Callable[[bytes], bytes] | None = None,
        retrieval_available: bool = True,
        reconciliation_available: bool = True,
        retry_valid: bool = False,
    ) -> None:
        self.ledger = MockAcceptanceLedger(ledger_path)
        self.scenario = scenario or MockScenario()
        self.retry_valid = retry_valid
        self.transport_context = replace(
            context or context_for(mechanism),
            service_identity=self.ledger.identity,
            fixture_sha256=self._fixture_sha256(),
        )
        self.transform = transform
        self.retrieval_available = retrieval_available
        self.reconciliation_available = reconciliation_available
        self.submit_calls = 0

    def capabilities_for(
        self, provider: str, endpoint: str, model: str
    ) -> CapabilityProfile:
        scope = self.transport_context.scope
        if (provider, endpoint, model) != (scope.provider, scope.endpoint, scope.model):
            raise ProviderError("synthetic transport scope differs")
        supported = GenerationParameterCapability(supported=True)
        return CapabilityProfile(
            explanations=True,
            grouped=True,
            ties=True,
            modes=("live",),
            generation=GenerationCapabilities(
                provider=provider,
                endpoint=endpoint,
                model=model,
                temperature=supported,
                top_p=supported,
                max_output_tokens=supported,
                source="offline-native-fixture",
                fixture_tested=True,
            ),
        )

    def capabilities(self, spec: RunSpecification) -> CapabilityProfile:
        return self.capabilities_for(spec.provider, spec.endpoint, spec.model)

    def validate(self, spec: RunSpecification) -> None:
        if (
            spec.schema_version != 3
            or not validate_run(spec, capabilities=self.capabilities(spec)).valid
        ):
            raise ProviderError("synthetic transport configuration differs")

    def prepare(
        self,
        spec: RunSpecification,
        dependency: RequestDependency,
        rendered: RenderedAssessmentPrompt,
    ) -> PreparedRequest:
        raise ProviderError("native preparation requires exact physical identities")

    def _fixture_sha256(self) -> str:
        return semantic_digest(
            "native-offline-observation",
            {
                "retry_valid": self.retry_valid,
                "scenario": self.scenario.model_dump(
                    exclude={
                        "transport",
                        "delay_seconds",
                        "transient_failures",
                        "retry_after",
                    }
                ),
            },
        )

    def _response(
        self, prepared: PreparedRequest, observation_key: str
    ) -> ProviderResponse:
        from judgmentllm.transport import PreparedRequestV3

        if not isinstance(prepared, PreparedRequestV3):
            raise ProviderIntegrityError("synthetic native request required")
        contract = prepared.output_commitment.contract
        scenario = self.scenario
        if (
            self.retry_valid
            and isinstance(contract, RubricOutputContract)
            and len(contract.traits) == 1
        ):
            scenario = MockScenario()
        kind: Literal[
            "response", "refusal", "provider_error", "confirmed_cancellation"
        ] = "response"
        if scenario.outcome in ("refusal", "provider_error", "confirmed_cancellation"):
            kind = scenario.outcome
        output: dict[str, Any] = {}
        if scenario.outcome == "invalid":
            output = {"invalid": True}
        elif isinstance(contract, RubricOutputContract):
            scores: list[dict[str, Any]] = []
            explanations: list[dict[str, Any]] = []
            for trait in contract.traits:
                member = scenario.members.get(trait.trait_id, MockMemberScenario())
                if member.outcome == "missing":
                    continue
                index = (
                    member.category_index
                    if member.category_index is not None
                    else (scenario.category_index or 0)
                )
                score: dict[str, Any] = {
                    "trait_id": trait.trait_id,
                    "category_label": trait.levels[index % len(trait.levels)].label,
                }
                if member.outcome == "invalid_category":
                    score["category_label"] = "unrequested-synthetic-category"
                if member.outcome == "malformed":
                    score = {"trait_id": trait.trait_id, "extra": True}
                text: Any = (
                    member.explanation
                    if member.explanation is not None
                    else scenario.explanation
                )
                if member.outcome == "invalid_explanation":
                    text = 17
                if contract.explanations and member.outcome != "missing_explanation":
                    if contract.output_order == "together":
                        score["explanation"] = text
                    else:
                        explanations.append({"trait_id": trait.trait_id, "text": text})
                scores.append(score)
                if member.outcome == "duplicate":
                    scores.append(dict(score))
                if member.outcome == "duplicate_explanation" and explanations:
                    explanations.append(dict(explanations[-1]))
            output = {"scores": scores}
            if contract.explanations and contract.output_order != "together":
                output["explanations"] = explanations
                if contract.output_order == "explanations_first":
                    output = {"explanations": explanations, "scores": scores}
        else:
            output = {
                "winner_id": None
                if scenario.winner == "tie"
                else wire_winner_ids(contract)[0 if scenario.winner == "first" else 1]
            }
            if contract.allow_ties:
                output["tie"] = scenario.winner == "tie"
            if contract.explanations:
                output["explanation"] = scenario.explanation
        raw = json.dumps(output, ensure_ascii=False, separators=(",", ":")).encode()
        if scenario.wire_corruption == "invalid_json":
            raw = b"{"
        elif scenario.wire_corruption == "duplicate_root_key":
            raw = raw[:-1] + b',"scores":[]}'
        elif scenario.wire_corruption == "unexpected_root":
            raw = raw[:-1] + b',"extra":true}'
        if self.transform is not None:
            raw = self.transform(raw)
        return ProviderResponse(
            observation_key=observation_key,
            kind=kind,
            raw=raw,
            evidence=ProviderEvidence(
                provenance="synthetic_fixture",
                output_text=raw,
                output_source="raw_output_text",
                safe_response=raw,
                original_body_safe=True,
            ),
        )

    async def submit(
        self,
        prepared: PreparedRequest,
        *,
        submission_id: str,
        correlation_id: str,
    ) -> ProviderAcknowledgment:
        self.submit_calls += 1
        scenario = self.scenario
        if scenario.transport == "timeout_before_acceptance":
            raise SubmissionUncertain()
        with closing(
            sqlite3.connect(self.ledger.path.as_uri() + "?mode=ro", uri=True)
        ) as connection:
            failure_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM acceptances WHERE accepted=0"
                ).fetchone()[0]
            )
        transient = scenario.transport == "transient_not_submitted" or (
            bool(scenario.transient_failures)
            and failure_count < scenario.transient_failures
        )
        accepted = scenario.transport != "not_submitted" and not transient
        key = stable_id(
            "native-offline-observation",
            self.ledger.identity,
            submission_id,
            correlation_id,
            prepared.payload_sha256,
        )
        response = self._response(prepared, key)
        record = AcceptanceRecord(
            submission_id=submission_id,
            correlation_id=correlation_id,
            payload_sha256=prepared.payload_sha256,
            accepted=accepted,
            observation_key=key if accepted else None,
            recipe=ObservationRecipe(
                fixture_sha256=self._fixture_sha256(),
                outcome=scenario.outcome,
                selection_index=0,
                winner=scenario.winner,
                probabilities=False,
                response_sha256=bytes_digest(response.raw),
            )
            if accepted
            else None,
            acceptance_count=int(accepted),
            retrieval_status="available" if accepted else "not_accepted",
        )
        # Same-thread ledger mutation deliberately keeps cancellation outside the
        # independent acceptance transaction; no detached task may publish later.
        self.ledger.establish(record, service_identity=self.ledger.identity)
        if transient:
            raise TransientNotSubmitted(retry_after=scenario.retry_after)
        if not accepted:
            raise TransportNotSubmitted()
        if scenario.transport == "timeout_after_acceptance":
            raise SubmissionUncertain()
        return ProviderAcknowledgment(
            remote_id=stable_id("native-offline-remote", submission_id),
            observation_key=key,
        )

    def _accepted(
        self, prepared: PreparedRequest, submission_id: str, correlation_id: str
    ) -> AcceptanceRecord | None:
        record = self.ledger.get(submission_id, service_identity=self.ledger.identity)
        if record and (
            record.correlation_id != correlation_id
            or record.payload_sha256 != prepared.payload_sha256
        ):
            raise ProviderIntegrityError("synthetic acceptance identity differs")
        if (
            record
            and record.recipe
            and record.recipe.fixture_sha256 != self._fixture_sha256()
        ):
            raise ProviderIntegrityError("synthetic fixture recipe differs")
        return record

    async def reconcile(
        self, prepared: PreparedRequest, *, submission_id: str, correlation_id: str
    ) -> ReconciliationEvidence:
        if not self.reconciliation_available:
            return ReconciliationEvidence(outcome="unresolved")
        record = self._accepted(prepared, submission_id, correlation_id)
        if record is None or not record.accepted:
            return ReconciliationEvidence(outcome="not_submitted")
        return ReconciliationEvidence(
            outcome="observation_available" if self.retrieval_available else "accepted",
            remote_id=stable_id("native-offline-remote", submission_id),
            observation_key=record.observation_key,
        )

    async def retrieve(
        self, prepared: PreparedRequest, *, submission_id: str, correlation_id: str
    ) -> ProviderResponse:
        record = self._accepted(prepared, submission_id, correlation_id)
        if (
            not self.retrieval_available
            or record is None
            or not record.accepted
            or record.observation_key is None
            or record.recipe is None
        ):
            raise RetrievalUnavailable()
        response = self._response(prepared, record.observation_key)
        if bytes_digest(response.raw) != record.recipe.response_sha256:
            raise ProviderIntegrityError("synthetic observation identity differs")
        return response

    def parse(
        self, response: ProviderResponse, *, strict_output: bool = False
    ) -> ParsedResponse:
        if response.kind != "response":
            return ParsedResponse(kind=response.kind)
        decoded = decode_assessment_output(response.raw)
        if not isinstance(decoded, dict):
            raise ValueError("synthetic response must be an object")
        return ParsedResponse(kind="response", output=cast(Any, decoded))
