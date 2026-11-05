"""Deterministic synthetic service with independently committed acceptance.

This is its own canonical JSON protocol, not an imitation cloud request. Fixture
text exists only in memory and permitted wire bytes. The service ledger retains
hash-bound numeric recipes; it never retains those bytes or fixture text.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import Literal, cast

from pydantic import Field, JsonValue

from judgmentllm.config import GenerationSpec, RunSpec
from judgmentllm.generation import GenerationCapabilities, GenerationParameterCapability
from judgmentllm.io import _revalidate_record  # pyright: ignore[reportPrivateUsage]
from judgmentllm.models import ID
from judgmentllm.persistence.hashing import (
    bytes_digest,
    canonical_bytes,
    semantic_digest,
)
from judgmentllm.persistence.models import SHA256, ExecutionModel
from judgmentllm.planning import CapabilityProfile, stable_id, validate_run
from judgmentllm.preparation.session import RequestDependency
from judgmentllm.prompts import RenderedPrompt
from judgmentllm.providers.ledger import (
    AcceptanceRecord,
    MockAcceptanceLedger,
    ObservationRecipe,
)
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
from judgmentllm.results import CategoryProbability


class MockMemberScenario(ExecutionModel):
    """One synthetic rubric member; text is excluded from acceptance recipes."""

    outcome: Literal[
        "valid",
        "invalid_category",
        "missing",
        "duplicate",
        "malformed",
        "missing_explanation",
        "invalid_explanation",
    ] = "valid"
    category_index: int | None = Field(default=None, ge=0)
    probabilities: bool | None = None
    explanation: str | None = None


class MockScenario(ExecutionModel):
    """Explicit synthetic outcomes; text-bearing values are never ledger fields."""

    outcome: Literal[
        "valid", "invalid", "refusal", "provider_error", "confirmed_cancellation"
    ] = "valid"
    transport: Literal[
        "success",
        "not_submitted",
        "transient_not_submitted",
        "timeout_before_acceptance",
        "timeout_after_acceptance",
    ] = "success"
    category_index: int | None = Field(default=None, ge=0)
    winner: Literal["first", "second", "tie"] = "first"
    probabilities: bool = False
    explanation: str = "Synthetic mock assessment."
    raw_marker: str | None = None
    payload_marker: str | None = None
    delay_seconds: float = Field(default=0, ge=0)
    transient_failures: int = Field(default=0, ge=0, exclude_if=lambda value: not value)
    retry_after: float | None = Field(
        default=None, ge=0, exclude_if=lambda value: value is None
    )
    members: dict[str, MockMemberScenario] = Field(
        default_factory=dict, exclude_if=lambda value: not value
    )
    reverse_members: bool = Field(default=False, exclude_if=lambda value: not value)
    unexpected_members: int = Field(default=0, ge=0, exclude_if=lambda value: not value)
    unattributable_members: int = Field(
        default=0, ge=0, exclude_if=lambda value: not value
    )


class MockMember(ExecutionModel):
    task_id: ID
    trait_id: ID
    category_labels: tuple[str, ...] = ()
    original_order: tuple[ID, ...] = ()
    presented_order: tuple[ID, ...] = ()


class MockPayload(ExecutionModel):
    """Exact mock wire contract, including every requested generation control."""

    protocol: Literal["judgmentllm-mock-v1"] = "judgmentllm-mock-v1"
    service_identity: ID
    provider: Literal["mock"] = "mock"
    endpoint: Literal["synthetic"] = "synthetic"
    model: ID
    request_uid: ID
    dependency_sha256: SHA256
    prompt: str
    output_schema: dict[str, JsonValue]
    members: tuple[MockMember, ...]
    task_kind: Literal["rubric", "pairwise"]
    explanations: bool
    allow_ties: bool
    cache_policy: Literal["off", "auto"]
    fixture_sha256: SHA256
    generation: GenerationSpec | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    payload_marker: str | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class MockResponseEnvelope(ExecutionModel):
    synthetic: Literal[True] = True
    observation_key: ID
    kind: Literal["response", "refusal", "provider_error", "confirmed_cancellation"]
    output: dict[str, JsonValue] | None = None
    rubric_probabilities: tuple[CategoryProbability, ...] | None = None
    provider_expected_index: float | None = None
    member_probabilities: dict[str, tuple[CategoryProbability, ...]] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    member_expected_indices: dict[str, float] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    p_left: float | None = None
    p_right: float | None = None
    p_tie: float | None = None
    raw_marker: str | None = None


def _fixture_digest(scenario: MockScenario) -> str:
    # Availability, delay and transport faults do not change an accepted answer.
    return semantic_digest(
        "mock-observation-fixture",
        scenario.model_dump(
            exclude={"transport", "delay_seconds", "transient_failures", "retry_after"}
        ),
    )


def _checkpoint(stage: str, submission_id: str) -> None:
    """Content-free deterministic service-boundary fault-injection seam."""


def _selection_index(payload: MockPayload, scenario: MockScenario) -> int:
    if scenario.category_index is not None:
        return scenario.category_index
    return int(
        semantic_digest(
            "mock-selection",
            {
                "request_uid": payload.request_uid,
                "dependency_sha256": payload.dependency_sha256,
                "fixture_sha256": payload.fixture_sha256,
            },
        )[:16],
        16,
    )


class MockAdapter:
    """Credential-free service boundary with exact, hash-bound retrieval.

    Reopening requires the same ledger and the same text-bearing fixture recipe
    supplied by the caller. An unavailable recipe does not authorize resubmission.
    """

    def __init__(
        self,
        ledger_path: str | Path,
        *,
        scenario: MockScenario | None = None,
        scenarios: Mapping[str, MockScenario] | None = None,
        retrieval_available: bool = True,
        reconciliation_available: bool = True,
    ):
        self.ledger = MockAcceptanceLedger(ledger_path)
        self.scenario = _revalidate_record(scenario or MockScenario())
        self.scenarios = {
            key: _revalidate_record(value) for key, value in (scenarios or {}).items()
        }
        self.retrieval_available = retrieval_available
        self.reconciliation_available = reconciliation_available
        self.submit_calls = 0

    @staticmethod
    def capabilities_for(provider: str, endpoint: str, model: str) -> CapabilityProfile:
        if (provider, endpoint) != ("mock", "synthetic"):
            raise ProviderError(
                "mock adapter requires provider mock and endpoint synthetic"
            )
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
                source="mock-v1-fixture",
                fixture_tested=True,
                live_qualified=False,
            ),
        )

    def capabilities(self, spec: RunSpec) -> CapabilityProfile:
        return self.capabilities_for(spec.provider, spec.endpoint, spec.model)

    def validate(self, spec: RunSpec) -> None:
        self.validate_configuration(spec, capabilities=self.capabilities(spec))

    @staticmethod
    def validate_configuration(
        spec: RunSpec, *, capabilities: CapabilityProfile | None = None
    ) -> None:
        """Pure mock preflight, shared with provider-free public validation."""
        try:
            spec = _revalidate_record(spec)
            profile = capabilities or MockAdapter.capabilities_for(
                spec.provider, spec.endpoint, spec.model
            )
            report = validate_run(spec, capabilities=profile)
            if not report.valid:
                raise ValueError("incompatible capabilities")
            if (
                spec.execution.cache_policy not in ("off", "auto")
                or spec.execution.tier is not None
            ):
                raise ValueError("unsupported cache or tier")
            # Unknown support is never permission to discard an explicit value.
            if spec.generation is not None:
                generation_profile = profile.generation
                if generation_profile is None or any(
                    getattr(generation_profile, name).supported is not True
                    for name in spec.generation.model_dump()
                ):
                    raise ValueError("generation support is unconfirmed")
        except ValueError:
            raise ProviderError("mock request configuration is unsupported") from None

    def _scenario(self, request_uid: str) -> MockScenario:
        return _revalidate_record(self.scenarios.get(request_uid, self.scenario))

    def prepare(
        self, spec: RunSpec, dependency: RequestDependency, rendered: RenderedPrompt
    ) -> PreparedRequest:
        self.validate(spec)
        try:
            spec, dependency, rendered = (
                _revalidate_record(spec),
                _revalidate_record(dependency),
                _revalidate_record(rendered),
            )
            request = dependency.request
            scenario = self._scenario(request.request_uid)
            if (
                scenario.winner == "tie"
                and spec.task_kind == "pairwise"
                and not spec.allow_ties
            ):
                raise ValueError("fixture tie is not permitted")
            members: list[MockMember] = []
            for task in request.tasks:
                if spec.task_kind == "rubric":
                    assert spec.rubric is not None
                    trait = next(
                        trait
                        for trait in spec.rubric.traits
                        if trait.trait_id == task.trait_id
                    )
                    recipe = scenario.members.get(task.trait_id)
                    member_index = (
                        recipe.category_index
                        if recipe is not None and recipe.category_index is not None
                        else scenario.category_index
                    )
                    if member_index is not None and member_index >= len(trait.levels):
                        raise ValueError("fixture category index is outside the rubric")
                    members.append(
                        MockMember(
                            task_id=task.task_id,
                            trait_id=task.trait_id,
                            category_labels=tuple(
                                level.label for level in trait.levels
                            ),
                        )
                    )
                else:
                    assert request.pair is not None
                    members.append(
                        MockMember(
                            task_id=task.task_id,
                            trait_id=task.trait_id,
                            original_order=tuple(request.pair.original_order),
                            presented_order=tuple(request.pair.presented_order),
                        )
                    )
            payload = MockPayload(
                service_identity=self.ledger.identity,
                model=spec.model,
                request_uid=request.request_uid,
                dependency_sha256=dependency.dependency_sha256,
                prompt=rendered.text,
                output_schema=rendered.output_schema,
                members=tuple(members),
                task_kind=spec.task_kind,
                explanations=spec.execution.explanations,
                allow_ties=spec.allow_ties,
                cache_policy=cast(Literal["off", "auto"], spec.execution.cache_policy),
                fixture_sha256=_fixture_digest(scenario),
                generation=spec.generation,
                payload_marker=scenario.payload_marker,
            )
            wire = canonical_bytes(payload)
            return PreparedRequest(
                request_uid=request.request_uid,
                task_ids=tuple(task.task_id for task in request.tasks),
                dependency_sha256=dependency.dependency_sha256,
                prompt_sha256=bytes_digest(rendered.text.encode("utf-8")),
                payload_sha256=bytes_digest(wire),
                provider=spec.provider,
                endpoint=spec.endpoint,
                model=spec.model,
                generation=spec.generation,
                payload=wire,
            )
        except (ValueError, StopIteration):
            raise ProviderError("mock request could not be prepared") from None

    def _payload(self, prepared: PreparedRequest) -> tuple[MockPayload, MockScenario]:
        try:
            prepared = _revalidate_record(prepared)
            payload = MockPayload.model_validate_json(prepared.payload)
            scenario = self._scenario(prepared.request_uid)
            if (
                canonical_bytes(payload) != prepared.payload
                or payload.service_identity != self.ledger.identity
                or payload.request_uid != prepared.request_uid
                or tuple(member.task_id for member in payload.members)
                != prepared.task_ids
                or payload.dependency_sha256 != prepared.dependency_sha256
                or bytes_digest(payload.prompt.encode("utf-8"))
                != prepared.prompt_sha256
                or (payload.provider, payload.endpoint, payload.model)
                != (prepared.provider, prepared.endpoint, prepared.model)
                or payload.generation != prepared.generation
                or payload.fixture_sha256 != _fixture_digest(scenario)
            ):
                raise ValueError("mock identity conflict")
            return payload, scenario
        except ValueError:
            raise ProviderIntegrityError(
                "mock prepared request or fixture identity conflicts"
            ) from None

    def _response(
        self,
        payload: MockPayload,
        scenario: MockScenario,
        observation_key: str,
        selection_index: int,
    ) -> ProviderResponse:
        fields: dict[str, object] = {
            "observation_key": observation_key,
            "kind": "response",
            "raw_marker": scenario.raw_marker,
        }
        if scenario.outcome in ("refusal", "provider_error", "confirmed_cancellation"):
            fields["kind"] = scenario.outcome
        elif scenario.outcome == "invalid":
            fields["output"] = {"synthetic_invalid": True}
        elif payload.task_kind == "rubric":
            scores: list[JsonValue] = []
            distributions: dict[str, tuple[CategoryProbability, ...]] = {}
            expected_indices: dict[str, float] = {}
            for member in payload.members:
                recipe = scenario.members.get(member.trait_id, MockMemberScenario())
                if recipe.outcome == "missing":
                    continue
                selected = (
                    recipe.category_index
                    if recipe.category_index is not None
                    else selection_index
                )
                index = selected % len(member.category_labels)
                score: dict[str, JsonValue] = {
                    "trait_id": member.trait_id,
                    "category_label": member.category_labels[index],
                }
                if payload.explanations:
                    score["explanation"] = (
                        recipe.explanation
                        if recipe.explanation is not None
                        else scenario.explanation
                    )
                if recipe.outcome == "invalid_category":
                    invalid = "synthetic-invalid-category"
                    while invalid in member.category_labels:
                        invalid += "-invalid"
                    score["category_label"] = invalid
                elif recipe.outcome == "malformed":
                    score = {"trait_id": member.trait_id, "synthetic_malformed": True}
                elif recipe.outcome == "missing_explanation":
                    score.pop("explanation", None)
                elif recipe.outcome == "invalid_explanation":
                    score["explanation"] = 17
                scores.append(score)
                if recipe.outcome == "duplicate":
                    scores.append(dict(score))
                if (
                    recipe.probabilities
                    if recipe.probabilities is not None
                    else scenario.probabilities
                ):
                    labels = member.category_labels
                    modal = (selected + 1) % len(labels)
                    distribution = tuple(
                        CategoryProbability(
                            level_label=label,
                            probability=0.6
                            if position == modal
                            else 0.4 / (len(labels) - 1),
                        )
                        for position, label in enumerate(labels)
                    )
                    distributions[member.trait_id] = distribution
                    expected_indices[member.trait_id] = sum(
                        position * entry.probability
                        for position, entry in enumerate(distribution)
                    )
            known = {member.trait_id for member in payload.members}
            for index in range(scenario.unexpected_members):
                trait_id = f"synthetic-unrequested-{index}"
                while trait_id in known:
                    trait_id += "-unknown"
                scores.append({"trait_id": trait_id, "category_label": "synthetic"})
            scores.extend(None for _ in range(scenario.unattributable_members))
            if scenario.reverse_members:
                scores.reverse()
            fields["output"] = {"scores": scores}
            if distributions:
                if len(payload.members) == 1:
                    trait_id = payload.members[0].trait_id
                    fields["rubric_probabilities"] = distributions.get(trait_id)
                    fields["provider_expected_index"] = expected_indices.get(trait_id)
                else:
                    fields["member_probabilities"] = distributions
                    fields["member_expected_indices"] = expected_indices
        else:
            member = payload.members[0]
            tie = scenario.winner == "tie"
            winner = (
                None
                if tie
                else member.presented_order[0 if scenario.winner == "first" else 1]
            )
            output: dict[str, JsonValue] = {"winner_id": winner, "tie": tie}
            if payload.explanations:
                output["explanation"] = scenario.explanation
            fields["output"] = output
            if scenario.probabilities:
                if payload.allow_ties:
                    fields.update(p_left=0.2, p_right=0.3, p_tie=0.5)
                else:
                    left = winner == member.original_order[0]
                    fields.update(
                        p_left=0.75 if left else 0.25, p_right=0.25 if left else 0.75
                    )
        envelope = MockResponseEnvelope.model_validate(fields)
        return ProviderResponse(
            observation_key=observation_key,
            kind=envelope.kind,
            raw=canonical_bytes(envelope),
        )

    async def _establish(
        self, record: AcceptanceRecord, service_identity: str
    ) -> AcceptanceRecord:
        # Cancellation must not release the executor's lifetime ownership while
        # a service transaction can still establish acceptance in an I/O thread.
        # At most one such task exists per active bounded provider worker.
        pending = asyncio.create_task(
            asyncio.to_thread(
                self.ledger.establish, record, service_identity=service_identity
            )
        )
        try:
            return await asyncio.shield(pending)
        except asyncio.CancelledError:
            while not pending.done():
                try:
                    await asyncio.shield(pending)
                except asyncio.CancelledError:
                    continue
            pending.result()
            raise

    async def submit(
        self, prepared: PreparedRequest, *, submission_id: str, correlation_id: str
    ) -> ProviderAcknowledgment:
        payload, scenario = await asyncio.to_thread(self._payload, prepared)
        self.submit_calls += 1
        if scenario.delay_seconds:
            await asyncio.sleep(scenario.delay_seconds)
        if scenario.transport == "timeout_before_acceptance":
            raise SubmissionUncertain(
                "synthetic transport timeout with unknown acceptance"
            )
        transient = scenario.transport == "transient_not_submitted"
        if scenario.transient_failures:
            previous = await asyncio.to_thread(
                self.ledger.nonacceptance_count, prepared.payload_sha256
            )
            recorded = await asyncio.to_thread(
                self.ledger.get,
                submission_id,
                service_identity=payload.service_identity,
            )
            transient = previous < scenario.transient_failures or (
                recorded is not None and not recorded.accepted
            )
        if scenario.transport == "not_submitted" or transient:
            await self._establish(
                AcceptanceRecord(
                    submission_id=submission_id,
                    correlation_id=correlation_id,
                    payload_sha256=prepared.payload_sha256,
                    accepted=False,
                    observation_key=None,
                    recipe=None,
                    acceptance_count=0,
                    retrieval_status="not_accepted",
                ),
                payload.service_identity,
            )
            if transient:
                raise TransientNotSubmitted(retry_after=scenario.retry_after)
            raise TransportNotSubmitted(
                "synthetic transport conclusively did not submit"
            )
        observation_key = stable_id(
            "mock-observation",
            payload.service_identity,
            submission_id,
            correlation_id,
            prepared.payload_sha256,
        )
        selection_index = _selection_index(payload, scenario)
        response = self._response(payload, scenario, observation_key, selection_index)
        recipe = ObservationRecipe(
            fixture_sha256=payload.fixture_sha256,
            outcome=scenario.outcome,
            selection_index=selection_index,
            winner=scenario.winner,
            probabilities=scenario.probabilities,
            response_sha256=bytes_digest(response.raw),
        )
        _checkpoint("before_acceptance", submission_id)
        await self._establish(
            AcceptanceRecord(
                submission_id=submission_id,
                correlation_id=correlation_id,
                payload_sha256=prepared.payload_sha256,
                accepted=True,
                observation_key=observation_key,
                recipe=recipe,
                acceptance_count=1,
                retrieval_status="available",
            ),
            payload.service_identity,
        )
        _checkpoint("after_acceptance", submission_id)
        if scenario.transport == "timeout_after_acceptance":
            raise SubmissionUncertain(
                "synthetic transport timeout with unknown acceptance"
            )
        return ProviderAcknowledgment(
            remote_id=stable_id("mock-submission", submission_id),
            observation_key=observation_key,
        )

    async def _accepted(
        self, prepared: PreparedRequest, submission_id: str, correlation_id: str
    ) -> tuple[MockPayload, MockScenario, AcceptanceRecord | None]:
        payload, scenario = await asyncio.to_thread(self._payload, prepared)
        record = await asyncio.to_thread(
            self.ledger.get, submission_id, service_identity=payload.service_identity
        )
        if record and (
            record.correlation_id != correlation_id
            or record.payload_sha256 != prepared.payload_sha256
        ):
            raise ProviderIntegrityError("mock acceptance identity conflicts")
        if record is not None and record.accepted:
            recipe = record.recipe
            if recipe is None or (
                recipe.fixture_sha256 != payload.fixture_sha256
                or recipe.outcome != scenario.outcome
                or recipe.selection_index != _selection_index(payload, scenario)
                or recipe.winner != scenario.winner
                or recipe.probabilities != scenario.probabilities
            ):
                raise ProviderIntegrityError("mock accepted recipe conflicts")
        return payload, scenario, record

    async def reconcile(
        self, prepared: PreparedRequest, *, submission_id: str, correlation_id: str
    ) -> ReconciliationEvidence:
        if not self.reconciliation_available:
            return ReconciliationEvidence(outcome="unresolved")
        _, _, record = await self._accepted(prepared, submission_id, correlation_id)
        if record is None or not record.accepted:
            # This complete authoritative local-service lookup establishes absence;
            # an absent executor acknowledgment does not.
            return ReconciliationEvidence(outcome="not_submitted")
        return ReconciliationEvidence(
            outcome="observation_available"
            if record.retrieval_status == "available" and self.retrieval_available
            else "accepted",
            remote_id=stable_id("mock-submission", submission_id),
            observation_key=record.observation_key,
        )

    async def retrieve(
        self, prepared: PreparedRequest, *, submission_id: str, correlation_id: str
    ) -> ProviderResponse:
        if not self.retrieval_available:
            raise RetrievalUnavailable("mock authoritative retrieval is unavailable")
        _checkpoint("before_retrieval", submission_id)
        payload, scenario, record = await self._accepted(
            prepared, submission_id, correlation_id
        )
        if (
            record is None
            or not record.accepted
            or record.recipe is None
            or record.observation_key is None
        ):
            raise RetrievalUnavailable("mock submission has no accepted observation")
        if record.retrieval_status != "available":
            raise RetrievalUnavailable("mock accepted observation is unavailable")
        response = self._response(
            payload, scenario, record.observation_key, record.recipe.selection_index
        )
        if (
            record.recipe.fixture_sha256 != payload.fixture_sha256
            or bytes_digest(response.raw) != record.recipe.response_sha256
        ):
            raise ProviderIntegrityError(
                "mock accepted observation conflicts with retrieval"
            )
        _checkpoint("after_retrieval", submission_id)
        return response

    def parse(self, response: ProviderResponse) -> ParsedResponse:
        try:
            response = _revalidate_record(response)
            envelope = MockResponseEnvelope.model_validate_json(response.raw)
        except ValueError:
            raise ValueError("mock response envelope is invalid") from None
        if (envelope.observation_key, envelope.kind) != (
            response.observation_key,
            response.kind,
        ):
            raise ProviderIntegrityError(
                "mock response evidence conflicts with its envelope"
            )
        return ParsedResponse.model_validate(
            envelope.model_dump(
                exclude={"schema_version", "synthetic", "observation_key", "raw_marker"}
            )
        )
