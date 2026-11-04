"""The offline service commits acceptance independently of executor state."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import sys
import threading
from contextlib import closing
from pathlib import Path

import pytest

from judgmentllm.config import RunSpec
from judgmentllm.generation import GenerationCapabilities, GenerationParameterCapability
from judgmentllm.models import PairTask, ResponseItem
from judgmentllm.persistence.hashing import (
    bytes_digest,
    canonical_bytes,
    semantic_digest,
)
from judgmentllm.planning import CapabilityProfile, plan_record
from judgmentllm.preparation.session import RequestDependency
from judgmentllm.prompts import render_prompt
from judgmentllm.providers import (
    AcceptanceRecord,
    MockAcceptanceLedger,
    MockAdapter,
    MockPayload,
    MockScenario,
    PreparedRequest,
    ProviderError,
    ProviderIntegrityError,
    ProviderResponse,
    RetrievalUnavailable,
    SubmissionUncertain,
    TransportNotSubmitted,
    mock,
)
from judgmentllm.results import (
    normalize_pairwise_result,
    normalize_rubric_result,
    validate_categorical_output,
    validate_pairwise_output,
)


def request(
    adapter: MockAdapter,
    *,
    pairwise: bool = False,
    ties: bool = False,
    explanations: bool = False,
    generation: dict[str, int | float] | None = None,
    grouping: bool = False,
) -> tuple[RunSpec, RequestDependency, PreparedRequest]:
    data: dict[str, object] = {
        "task_kind": "pairwise" if pairwise else "rubric",
        "input": "unused.jsonl",
        "provider": "mock",
        "endpoint": "synthetic",
        "model": "unknown-model",
        "allow_ties": ties,
        "execution": {
            "explanations": explanations,
            "grouping": "grouped" if grouping else "independent",
        },
    }
    if generation is not None:
        data["generation"] = generation
    if ties:
        data["prompt"] = {"file": Path("unused-template.txt")}
    if pairwise:
        data["criteria"] = [
            {"trait_id": "01", "name": "Synthetic", "description": "Compare."}
        ]
        data["pairs"] = "unused-pairs.jsonl"
    else:
        data["rubric"] = {
            "rubric_id": "01",
            "name": "Synthetic",
            "traits": [
                {
                    "trait_id": trait_id,
                    "name": "Quality",
                    "description": "Assess.",
                    "levels": [
                        {"label": "low", "numeric_value": 1},
                        {"label": "medium", "numeric_value": 3},
                        {"label": "high", "numeric_value": 8},
                    ],
                }
                for trait_id in (("01", "02") if grouping else ("01",))
            ],
        }
    spec = RunSpec.model_validate(data)
    response = ResponseItem(item_id="001", text="SOURCE_TEXT_SENTINEL")
    if pairwise:
        pair = PairTask(
            pair_uid="0001",
            pair_id="01",
            trait_id="01",
            left_item_id="001",
            right_item_id="002",
            presented_order=["002", "001"],
        )
        planned = next(plan_record(spec, pair, capabilities=adapter.capabilities(spec)))
        rendered = render_prompt(
            pair=pair,
            left=response,
            right=ResponseItem(item_id="002", text="Other synthetic text."),
            criterion=spec.criteria[0],
            explanations=explanations,
            allow_ties=ties,
            template="{{ criterion }}\n{{ first_response }}\n{{ second_response }}\n{{ exemplars }}\n{{ output_instructions }}"
            if ties
            else None,
        )
    else:
        planned = next(
            plan_record(spec, response, capabilities=adapter.capabilities(spec))
        )
        assert spec.rubric is not None
        rendered = render_prompt(
            response=response,
            rubric=spec.rubric,
            traits=spec.rubric.traits,
            explanations=explanations,
        )
    dependency = RequestDependency(
        request=planned,
        dependency_sha256=semantic_digest("unit-dependency", "synthetic"),
    )
    return spec, dependency, adapter.prepare(spec, dependency, rendered)


@pytest.mark.asyncio
async def test_rubric_exact_bytes_acceptance_retrieval_and_normalization(
    tmp_path: Path,
) -> None:
    adapter = MockAdapter(
        tmp_path / "service.sqlite3",
        scenario=MockScenario(
            category_index=0, probabilities=True, explanation="AUTHORIZED_EXPLANATION"
        ),
    )
    spec, dependency, prepared = request(
        adapter,
        explanations=True,
        generation={"temperature": 0, "top_p": 0.7, "max_output_tokens": 23},
    )
    wire = MockPayload.model_validate_json(prepared.payload)
    assert canonical_bytes(wire) == prepared.payload
    assert prepared.payload_sha256 == bytes_digest(prepared.payload)
    assert prepared.prompt_sha256 == bytes_digest(wire.prompt.encode())
    assert wire.generation == spec.generation
    assert json.loads(prepared.payload)["generation"] == {
        "temperature": 0,
        "top_p": 0.7,
        "max_output_tokens": 23,
    }
    assert (
        await adapter.reconcile(prepared, submission_id="s", correlation_id="c")
    ).outcome == "not_submitted"
    with pytest.raises(RetrievalUnavailable):
        await adapter.retrieve(prepared, submission_id="s", correlation_id="c")
    acknowledgment = await adapter.submit(
        prepared, submission_id="s", correlation_id="c"
    )
    first = await adapter.retrieve(prepared, submission_id="s", correlation_id="c")
    assert first == await adapter.retrieve(
        prepared, submission_id="s", correlation_id="c"
    )
    assert first.observation_key == acknowledgment.observation_key
    assert adapter.ledger.acceptance_count() == 1
    parsed = adapter.parse(first)
    assert spec.rubric is not None
    selected = validate_categorical_output(
        parsed.output, spec.rubric.traits, explanations=True
    ).scores[0]
    result = normalize_rubric_result(
        spec.rubric.traits[0],
        run_id="r",
        task_id=prepared.task_ids[0],
        request_uid=dependency.request.request_uid,
        attempt_id="a",
        item_id="001",
        category_label=selected.category_label,
        explanation=selected.explanation,
        probabilities=parsed.rubric_probabilities,
        provider_expected_index=parsed.provider_expected_index,
    )
    assert result.category_label == "low"
    assert result.modal_category_label == "medium"
    assert result.primary_score_kind == "expected"
    assert result.explanation == "AUTHORIZED_EXPLANATION"
    assert result.provider_expected_index is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "winner,ties,expected",
    [("first", False, "002"), ("second", False, "001"), ("tie", True, None)],
)
async def test_pairwise_reversed_choice_ties_and_probabilities(
    tmp_path: Path, winner: str, ties: bool, expected: str | None
) -> None:
    adapter = MockAdapter(
        tmp_path / "service.sqlite3",
        scenario=MockScenario.model_validate({"winner": winner, "probabilities": True}),
    )
    _, dependency, prepared = request(
        adapter, pairwise=True, ties=ties, explanations=True
    )
    await adapter.submit(prepared, submission_id="s", correlation_id="c")
    parsed = adapter.parse(
        await adapter.retrieve(prepared, submission_id="s", correlation_id="c")
    )
    pair = dependency.request.pair
    assert pair is not None
    selected = validate_pairwise_output(
        parsed.output, pair, explanations=True, allow_ties=ties
    )
    result = normalize_pairwise_result(
        pair,
        run_id="r",
        task_id=prepared.task_ids[0],
        request_uid=prepared.request_uid,
        attempt_id="a",
        winner_id=selected.winner_id,
        tie=selected.tie,
        allow_ties=ties,
        explanation=selected.explanation,
        p_left=parsed.p_left,
        p_right=parsed.p_right,
        p_tie=parsed.p_tie,
    )
    assert result.winner_id == expected
    assert result.presented_order == ["002", "001"]
    assert result.original_order == ["001", "002"]
    if not ties:
        assert result.p_left == (0.75 if expected == "001" else 0.25)
    else:
        assert result.tie and result.p_tie == 0.5


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome,kind",
    [
        ("invalid", "response"),
        ("refusal", "refusal"),
        ("provider_error", "provider_error"),
    ],
)
async def test_received_failure_outcomes_are_distinct(
    tmp_path: Path, outcome: str, kind: str
) -> None:
    adapter = MockAdapter(
        tmp_path / "service.sqlite3",
        scenario=MockScenario.model_validate({"outcome": outcome}),
    )
    spec, _, prepared = request(adapter)
    await adapter.submit(prepared, submission_id="s", correlation_id="c")
    response = await adapter.retrieve(prepared, submission_id="s", correlation_id="c")
    parsed = adapter.parse(response)
    assert parsed.kind == kind and adapter.ledger.acceptance_count() == 1
    if outcome == "invalid":
        assert spec.rubric is not None
        with pytest.raises(ValueError):
            validate_categorical_output(parsed.output, spec.rubric.traits)
    else:
        assert parsed.output is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "transport,accepted",
    [
        ("timeout_before_acceptance", False),
        ("timeout_after_acceptance", True),
        ("not_submitted", False),
    ],
)
async def test_transport_uncertainty_and_authoritative_evidence(
    tmp_path: Path, transport: str, accepted: bool
) -> None:
    adapter = MockAdapter(
        tmp_path / "service.sqlite3",
        scenario=MockScenario.model_validate({"transport": transport}),
    )
    _, _, prepared = request(adapter)
    with pytest.raises(
        TransportNotSubmitted if transport == "not_submitted" else SubmissionUncertain
    ):
        await adapter.submit(prepared, submission_id="s", correlation_id="c")
    evidence = await adapter.reconcile(prepared, submission_id="s", correlation_id="c")
    assert evidence.outcome == (
        "observation_available" if accepted else "not_submitted"
    )
    assert adapter.ledger.acceptance_count() == int(accepted)
    if accepted:
        reopened = MockAdapter(adapter.ledger.path)
        assert await reopened.retrieve(prepared, submission_id="s", correlation_id="c")
        assert reopened.ledger.acceptance_count() == 1
        assert reopened.submit_calls == 0
    else:
        with pytest.raises(RetrievalUnavailable):
            await adapter.retrieve(prepared, submission_id="s", correlation_id="c")


@pytest.mark.asyncio
async def test_acceptance_precedes_ack_and_survives_caller_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = MockAdapter(tmp_path / "service.sqlite3")
    _, _, prepared = request(adapter)

    def fault(stage: str, submission_id: str) -> None:
        assert submission_id == "s"
        if stage == "after_acceptance":
            raise RuntimeError("synthetic acknowledgment loss")

    monkeypatch.setattr(mock, "_checkpoint", fault)
    with pytest.raises(RuntimeError, match="acknowledgment loss"):
        await adapter.submit(prepared, submission_id="s", correlation_id="c")
    monkeypatch.undo()
    reopened = MockAdapter(adapter.ledger.path)
    assert (
        await reopened.reconcile(prepared, submission_id="s", correlation_id="c")
    ).outcome == "observation_available"
    await reopened.retrieve(prepared, submission_id="s", correlation_id="c")
    # Service-level idempotent replay is tested independently of executor policy.
    await reopened.submit(prepared, submission_id="s", correlation_id="c")
    assert reopened.ledger.acceptance_count() == 1


@pytest.mark.asyncio
async def test_unavailable_retrieval_and_reconciliation_are_separate(
    tmp_path: Path,
) -> None:
    adapter = MockAdapter(tmp_path / "service.sqlite3")
    _, _, prepared = request(adapter)
    await adapter.submit(prepared, submission_id="s", correlation_id="c")
    adapter.retrieval_available = False
    assert (
        await adapter.reconcile(prepared, submission_id="s", correlation_id="c")
    ).outcome == "accepted"
    with pytest.raises(RetrievalUnavailable):
        await adapter.retrieve(prepared, submission_id="s", correlation_id="c")
    adapter.reconciliation_available = False
    assert (
        await adapter.reconcile(prepared, submission_id="s", correlation_id="c")
    ).outcome == "unresolved"
    assert adapter.ledger.acceptance_count() == 1


@pytest.mark.asyncio
async def test_conflicting_submission_and_observation_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = MockAdapter(tmp_path / "service.sqlite3")
    _, _, prepared = request(adapter)
    await adapter.submit(prepared, submission_id="s", correlation_id="c")
    with pytest.raises(ProviderIntegrityError):
        await adapter.retrieve(prepared, submission_id="s", correlation_id="wrong")
    with pytest.raises(ProviderIntegrityError):
        await adapter.submit(prepared, submission_id="other", correlation_id="c")
    original = adapter._response  # pyright: ignore[reportPrivateUsage]

    def changed(
        payload: mock.MockPayload, scenario: MockScenario, key: str, index: int
    ) -> ProviderResponse:
        result = original(payload, scenario, key, index)
        return ProviderResponse(
            observation_key=result.observation_key,
            kind=result.kind,
            raw=result.raw + b" ",
        )

    monkeypatch.setattr(adapter, "_response", changed)
    with pytest.raises(ProviderIntegrityError, match="conflicts"):
        await adapter.retrieve(prepared, submission_id="s", correlation_id="c")
    assert adapter.ledger.acceptance_count() == 1


def test_sparse_generation_group_representation_and_rejections(tmp_path: Path) -> None:
    adapter = MockAdapter(tmp_path / "service.sqlite3")
    spec, dependency, prepared = request(adapter)
    assert "generation" not in json.loads(prepared.payload)
    assert "payload_marker" not in json.loads(prepared.payload)
    _, _, grouped = request(adapter, grouping=True)
    assert len(MockPayload.model_validate_json(grouped.payload).members) == 2
    adapter.scenario = MockScenario(probabilities=True)
    assert len(request(adapter, grouping=True)[2].task_ids) == 2
    adapter.scenario = MockScenario(winner="tie")
    with pytest.raises(ProviderError):
        request(adapter, pairwise=True)
    adapter.scenario = MockScenario(category_index=100)
    with pytest.raises(ProviderError):
        request(adapter)
    for fields in (
        {"provider": "openai"},
        {"endpoint": "responses"},
        {"execution": {"mode": "bulk_live"}},
        {"execution": {"cache_policy": "explicit"}},
        {"execution": {"tier": "fast"}},
    ):
        modified = RunSpec.model_validate(spec.model_dump() | fields)
        with pytest.raises(ProviderError):
            adapter.validate(modified)
    assert adapter.ledger.acceptance_count() == 0
    assert dependency.request.tasks[0].task_id == prepared.task_ids[0]


def test_unknown_generation_support_cannot_silently_discard_controls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = MockAdapter(tmp_path / "service.sqlite3")
    spec, _, _ = request(adapter, generation={"temperature": 0.5})
    profile = CapabilityProfile(
        modes=("live",),
        generation=GenerationCapabilities(
            provider="mock",
            endpoint="synthetic",
            model=spec.model,
            temperature=GenerationParameterCapability(supported=None),
        ),
    )

    def supplied(spec: RunSpec) -> CapabilityProfile:
        return profile

    monkeypatch.setattr(adapter, "capabilities", supplied)
    with pytest.raises(ProviderError):
        adapter.validate(spec)


@pytest.mark.asyncio
async def test_payload_and_ledger_replacement_fail_closed(tmp_path: Path) -> None:
    adapter = MockAdapter(tmp_path / "service.sqlite3")
    _, _, prepared = request(adapter)
    with pytest.raises(ValueError):
        PreparedRequest.model_validate(
            dict(prepared) | {"payload": prepared.payload + b"changed"}
        )
    with pytest.raises(ProviderIntegrityError):
        await adapter.submit(
            prepared.model_copy(update={"prompt_sha256": "0" * 64}),
            submission_id="s",
            correlation_id="c",
        )
    replacement = MockAdapter(tmp_path / "different-service.sqlite3")
    with pytest.raises(ProviderIntegrityError):
        await replacement.reconcile(prepared, submission_id="s", correlation_id="c")
    adapter.scenario = MockScenario(raw_marker="changed-fixture")
    with pytest.raises(ProviderIntegrityError):
        await adapter.retrieve(prepared, submission_id="s", correlation_id="c")


@pytest.mark.asyncio
async def test_minimal_ledger_contains_no_assessment_or_wire_material(
    tmp_path: Path,
) -> None:
    adapter = MockAdapter(
        tmp_path / "service.sqlite3",
        scenario=MockScenario(
            explanation="AUTHORIZED_EXPLANATION_SENTINEL",
            raw_marker="RAW_ONLY_SENTINEL",
            payload_marker="PAYLOAD_ONLY_SENTINEL",
        ),
    )
    _, _, prepared = request(adapter, explanations=True)
    assert b"PAYLOAD_ONLY_SENTINEL" in prepared.payload
    await adapter.submit(prepared, submission_id="s", correlation_id="c")
    response = await adapter.retrieve(prepared, submission_id="s", correlation_id="c")
    assert b"RAW_ONLY_SENTINEL" in response.raw
    assert b"AUTHORIZED_EXPLANATION_SENTINEL" in response.raw
    for path in tmp_path.rglob("*"):
        if path.is_file():
            contents = path.read_bytes()
            for sentinel in (
                b"SOURCE_TEXT_SENTINEL",
                b"RAW_ONLY_SENTINEL",
                b"PAYLOAD_ONLY_SENTINEL",
                b"AUTHORIZED_EXPLANATION_SENTINEL",
            ):
                assert sentinel not in contents
    with closing(sqlite3.connect(adapter.ledger.path)) as connection:
        for row in connection.execute("SELECT * FROM acceptances"):
            assert "SENTINEL" not in repr(tuple(row))


@pytest.mark.asyncio
async def test_delays_reorder_completion_without_changing_membership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = MockAdapter(tmp_path / "service.sqlite3")
    _, _, first = request(adapter)
    second_spec, second_dep, _ = request(adapter)
    # Add a distinct item rather than altering the immutable original request.
    response = ResponseItem(item_id="003", text="A second synthetic response.")
    planned = next(
        plan_record(
            second_spec, response, capabilities=adapter.capabilities(second_spec)
        )
    )
    assert second_spec.rubric is not None
    second_dep = RequestDependency(
        request=planned, dependency_sha256=second_dep.dependency_sha256
    )
    second = adapter.prepare(
        second_spec,
        second_dep,
        render_prompt(
            response=response,
            rubric=second_spec.rubric,
            traits=second_spec.rubric.traits,
        ),
    )
    adapter.scenarios[first.request_uid] = MockScenario(delay_seconds=0.03)
    completed: list[str] = []
    requested_delays: list[float] = []
    delay_started, second_completed = asyncio.Event(), asyncio.Event()
    original_sleep = asyncio.sleep
    first_task: asyncio.Task[None] | None = None
    memberships = (first.task_ids, second.task_ids)

    async def controlled_sleep(delay: float, result: object = None) -> object:
        if delay != 0.03 or asyncio.current_task() is not first_task:
            return await original_sleep(delay, result)
        requested_delays.append(delay)
        delay_started.set()
        # The configured delay must hold the first real submission until the
        # second finishes, regardless of payload-thread or ledger timing.
        await second_completed.wait()
        return result

    monkeypatch.setattr(mock.asyncio, "sleep", controlled_sleep)

    async def send(prepared: PreparedRequest, identity: str) -> None:
        if identity == "second":
            await delay_started.wait()
            assert first_task is not None and not first_task.done()
        await adapter.submit(prepared, submission_id=identity, correlation_id=identity)
        completed.append(identity)
        if identity == "second":
            second_completed.set()

    first_task = asyncio.create_task(send(first, "first"))
    await asyncio.wait_for(
        asyncio.gather(first_task, send(second, "second")), timeout=5
    )
    assert requested_delays == [0.03]
    assert completed == ["second", "first"]
    assert (first.task_ids, second.task_ids) == memberships
    assert adapter.ledger.acceptance_count() == 2


def test_parsing_malformed_and_conflicting_envelopes_is_safe(tmp_path: Path) -> None:
    adapter = MockAdapter(tmp_path / "service.sqlite3")
    for raw in (b"PRIVATE_INVALID_JSON", b'{"synthetic":false}', b"[]"):
        with pytest.raises(ValueError, match="envelope is invalid") as error:
            adapter.parse(
                ProviderResponse(observation_key="o", kind="response", raw=raw)
            )
        assert "PRIVATE" not in str(error.value)
    envelope = mock.MockResponseEnvelope(observation_key="different", kind="response")
    with pytest.raises(ProviderIntegrityError):
        adapter.parse(
            ProviderResponse(
                observation_key="o", kind="response", raw=canonical_bytes(envelope)
            )
        )


@pytest.mark.asyncio
async def test_cancellation_waits_for_inflight_service_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = MockAdapter(tmp_path / "service.sqlite3")
    _, _, prepared = request(adapter)
    original = adapter.ledger.establish
    started, release = threading.Event(), threading.Event()

    def held(
        record: AcceptanceRecord, *, service_identity: str | None = None
    ) -> AcceptanceRecord:
        started.set()
        assert release.wait(5)
        return original(record, service_identity=service_identity)

    monkeypatch.setattr(adapter.ledger, "establish", held)
    pending = asyncio.create_task(
        adapter.submit(prepared, submission_id="s", correlation_id="c")
    )
    assert await asyncio.to_thread(started.wait, 3)
    pending.cancel()
    await asyncio.sleep(0.01)
    assert not pending.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert adapter.ledger.acceptance_count() == 1
    assert (
        await adapter.reconcile(prepared, submission_id="s", correlation_id="c")
    ).outcome == "observation_available"


@pytest.mark.asyncio
async def test_judgment_is_deterministic_across_service_namespaces(
    tmp_path: Path,
) -> None:
    first, second = (
        MockAdapter(tmp_path / "one.sqlite3"),
        MockAdapter(tmp_path / "two.sqlite3"),
    )
    _, _, prepared_first = request(first, generation={"temperature": 0})
    _, _, prepared_second = request(second, generation={"temperature": 0})
    assert prepared_first.payload_sha256 != prepared_second.payload_sha256
    await first.submit(prepared_first, submission_id="s", correlation_id="c")
    await second.submit(prepared_second, submission_id="s", correlation_id="c")
    observed_first = first.parse(
        await first.retrieve(prepared_first, submission_id="s", correlation_id="c")
    )
    observed_second = second.parse(
        await second.retrieve(prepared_second, submission_id="s", correlation_id="c")
    )
    assert observed_first == observed_second


@pytest.mark.asyncio
async def test_ledger_independent_process_read_and_immutable_rows(
    tmp_path: Path,
) -> None:
    adapter = MockAdapter(tmp_path / "service.sqlite3")
    _, _, prepared = request(adapter)
    await adapter.submit(prepared, submission_id="s", correlation_id="c")
    script = "from judgmentllm.providers import MockAcceptanceLedger; import sys; ledger=MockAcceptanceLedger(sys.argv[1]); record=ledger.get('s'); assert record and record.accepted and record.acceptance_count==1; assert ledger.acceptance_count()==1; print(record.payload_sha256)"
    child = subprocess.run(
        [sys.executable, "-c", script, str(adapter.ledger.path)],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert child.returncode == 0, child.stderr
    assert child.stdout.strip() == prepared.payload_sha256
    independent = MockAcceptanceLedger(adapter.ledger.path)
    assert independent.get("s") == adapter.ledger.get("s")
    with closing(sqlite3.connect(adapter.ledger.path)) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE acceptances SET acceptance_count=0")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM acceptances")


@pytest.mark.asyncio
async def test_corrupted_accepted_recipe_never_becomes_an_observation(
    tmp_path: Path,
) -> None:
    adapter = MockAdapter(tmp_path / "service.sqlite3")
    _, _, prepared = request(adapter)
    await adapter.submit(prepared, submission_id="s", correlation_id="c")
    record = adapter.ledger.get("s")
    assert record is not None and record.recipe is not None
    with closing(sqlite3.connect(adapter.ledger.path)) as connection, connection:
        connection.execute("DROP TRIGGER immutable_acceptance")
        corrupted = record.recipe.model_dump() | {"winner": "second"}
        connection.execute("UPDATE acceptances SET recipe=?", (json.dumps(corrupted),))
    with pytest.raises(ProviderIntegrityError, match="recipe conflicts"):
        await adapter.reconcile(prepared, submission_id="s", correlation_id="c")
    with closing(sqlite3.connect(adapter.ledger.path)) as connection, connection:
        connection.execute(
            "UPDATE acceptances SET recipe=?", ('{"unexpected":"PRIVATE_CONTENT"}',)
        )
    with pytest.raises(ProviderIntegrityError, match="evidence is invalid") as error:
        await adapter.retrieve(prepared, submission_id="s", correlation_id="c")
    assert "PRIVATE_CONTENT" not in str(error.value)


def test_ledger_rejects_inconsistent_evidence_and_unreadable_database(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="nonacceptance"):
        AcceptanceRecord(
            submission_id="s",
            correlation_id="c",
            payload_sha256="0" * 64,
            accepted=False,
            observation_key="fabricated",
            recipe=None,
            acceptance_count=0,
            retrieval_status="not_accepted",
        )
    with pytest.raises(ValueError, match="requires"):
        AcceptanceRecord(
            submission_id="s",
            correlation_id="c",
            payload_sha256="0" * 64,
            accepted=True,
            observation_key="fabricated",
            recipe=None,
            acceptance_count=1,
            retrieval_status="available",
        )
    broken = tmp_path / "broken.sqlite3"
    broken.write_bytes(b"invalid synthetic database fixture")
    with pytest.raises(ProviderIntegrityError, match="could not be opened"):
        MockAcceptanceLedger(broken)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["submit", "reconcile"])
async def test_namespace_replacement_between_payload_check_and_atomic_service_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    adapter = MockAdapter(tmp_path / "service.sqlite3")
    _, _, prepared = request(adapter)
    if operation == "reconcile":
        await adapter.submit(prepared, submission_id="s", correlation_id="c")
    original = adapter._payload  # pyright: ignore[reportPrivateUsage]

    def replace_after_validation(
        current: PreparedRequest,
    ) -> tuple[MockPayload, MockScenario]:
        payload, scenario = original(current)
        for suffix in ("", "-wal", "-shm"):
            Path(str(adapter.ledger.path) + suffix).unlink(missing_ok=True)
        MockAcceptanceLedger(adapter.ledger.path)
        return payload, scenario

    monkeypatch.setattr(adapter, "_payload", replace_after_validation)
    with pytest.raises(ProviderIntegrityError, match="namespace"):
        if operation == "submit":
            await adapter.submit(prepared, submission_id="s", correlation_id="c")
        else:
            await adapter.reconcile(prepared, submission_id="s", correlation_id="c")
    assert adapter.ledger.acceptance_count() == 0
