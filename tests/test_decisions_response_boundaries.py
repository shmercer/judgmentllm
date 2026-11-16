"""One synthetic invocation cannot fabricate acceptance or repair named answers."""

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
from test_decisions_contracts import pair_spec
from test_decisions_lifecycle import native_spec
from test_decisions_validation import native_request
from test_native_transport_integration import prepared_case
from test_preparation_rendering import specification
from test_v2_public_lifecycle import configuration

from judgmentllm.config import RunSpecV4, load_run_spec
from judgmentllm.decisions.interpretation import interpret_decisions
from judgmentllm.decisions.transport import (
    DecisionsContextV1,
    PreparedDecisionsRequestV1,
    plan_source,
    prepare_request,
)
from judgmentllm.execution import execute_run, recover_run
from judgmentllm.execution.session import ExecutionSession
from judgmentllm.models import ResponseItem
from judgmentllm.persistence.decisions_invocations import publish_invocation
from judgmentllm.persistence.hashing import canonical_bytes
from judgmentllm.persistence.models import IntegrityError
from judgmentllm.persistence.receipts import CompleteResponseEvidenceV1
from judgmentllm.persistence.repository import Repository
from judgmentllm.preparation import prepare_run
from judgmentllm.providers.decisions_mock import (
    DecisionsScenarioV1,
    MockDecisionsAdapter,
)
from judgmentllm.providers.decisions_wire import parse_synthetic_decisions
from judgmentllm.providers.mock import MockAdapter, MockMemberScenario, MockScenario
from judgmentllm.providers.protocol import (
    DecisionsReconciliationEvidenceV1,
    PreparedRequest,
    ProviderAcknowledgment,
    ProviderAcknowledgmentV2,
    ProviderError,
    ProviderIntegrityError,
    ProviderResponse,
    RetrievalUnavailable,
)
from judgmentllm.transport import physical_identity


class AnswerBoundaryAdapter(MockDecisionsAdapter):
    fault: str

    def _response(
        self, wire: PreparedDecisionsRequestV1, observation_key: str
    ) -> ProviderResponse:
        response = super()._response(wire, observation_key)
        data = json.loads(response.raw)
        first = data["answers"][0]
        if self.fault == "nonobject":
            data["answers"].append(None)
        elif self.fault == "nameless":
            data["answers"].append({"type": "refusal"})
        elif self.fault == "triplicate":
            data["answers"].extend([first.copy(), first.copy()])
        elif self.fault == "expected":
            first["expected_index"] = 0
        elif self.fault == "selected":
            first["selected_index"] = 7
        elif self.fault == "explicit_selected":
            first["selected_index"] = 0
        elif self.fault == "choice_order":
            first["probabilities"].reverse()
        elif self.fault == "refusal_extra":
            data["answers"][0] = dict(
                name=first["name"], type="refusal", explanation="Synthetic extra field"
            )
        else:
            first["type"] = []
        raw = canonical_bytes(data)
        assert response.evidence is not None
        return response.model_copy(
            update={
                "raw": raw,
                "evidence": response.evidence.model_copy(update={"safe_response": raw}),
            }
        )


@pytest.mark.parametrize(
    "fault",
    [
        "nonobject",
        "nameless",
        "triplicate",
        "expected",
        "selected",
        "explicit_selected",
        "choice_order",
        "refusal_extra",
        "type_object",
    ],
)
@pytest.mark.asyncio
async def test_complete_bound_envelope_only_salvages_valid_named_members(
    tmp_path: Path, fault: str
) -> None:
    spec = pair_spec(tmp_path) if fault == "choice_order" else native_spec(tmp_path)
    store = tmp_path / "store"
    with prepare_run(spec, store=store, run_id="001"):
        pass
    adapter = AnswerBoundaryAdapter(store / "mock-service.sqlite3")
    adapter.fault = fault
    report = await execute_run("001", store=store, adapter=adapter)
    with Repository(store) as repository:
        rows = list(repository.iter_results("001"))
        if fault in {"nonobject", "nameless", "explicit_selected"}:
            assert all(r.status == "complete" for r in rows)
            if fault == "explicit_selected":
                first = next(r for r in rows if r.trait_id == "00")
                assert first.task_kind == "rubric"
                assert (
                    first.category_label == "00" and first.modal_category_label == "02"
                )
                assert first.expected_value == pytest.approx(5.15)
        else:
            assert sum(r.status == "invalid" for r in rows) == (
                4 if fault == "choice_order" else 1
            )
            assert not any(r.status == "refused" for r in rows)
            assert sum(r.status == "complete" for r in rows) == (
                8 if fault == "choice_order" else 2
            )
    assert report.summary.results == len(rows)
    assert adapter.forward_entries == (4 if fault == "choice_order" else 1)


def test_neutral_interpreter_checks_binding_without_an_executor_checkpoint(
    tmp_path: Path,
) -> None:
    spec, dependency, wire = native_request(tmp_path)
    adapter = MockDecisionsAdapter(tmp_path / "ledger.sqlite3")
    response = adapter._response(wire, "local")  # pyright: ignore[reportPrivateUsage]
    parsed = parse_synthetic_decisions(response, wire)
    outcome = interpret_decisions(parsed, spec, dependency, wire)
    assert len(outcome.results) == 3 and all(
        r.status == "complete" for r in outcome.results
    )
    foreign = parsed.model_copy(
        update={"identity": physical_identity("other", wire.request_uid, "other")}
    )
    with pytest.raises(IntegrityError):
        interpret_decisions(foreign, spec, dependency, wire)
    with pytest.raises(ProviderError):
        adapter.parse(response)


def test_synthetic_boundary_refuses_unimplemented_scopes_and_foreign_wires(
    tmp_path: Path,
) -> None:
    spec, dependency, wire = native_request(tmp_path)
    with pytest.raises(ProviderIntegrityError):
        MockDecisionsAdapter(
            tmp_path / "ledger.sqlite3",
            context=DecisionsContextV1(service_identity="foreign"),
        )
    adapter = MockDecisionsAdapter(tmp_path / "ledger.sqlite3")
    with pytest.raises(ProviderError):
        adapter.capabilities_for("openai", "decisions", "unknown-model")
    tier = RunSpecV4.model_validate(
        {**spec.model_dump(), "execution": {"tier": "priority"}}
    )
    with pytest.raises(ProviderError):
        adapter.validate(tier)
    with pytest.raises(ProviderError):
        adapter.prepare(spec, dependency, None)
    old = PreparedRequest.model_validate(
        {
            k: (1 if k == "preparation_version" else v)
            for k, v in wire.model_dump().items()
            if k in PreparedRequest.model_fields
        }
    )
    with pytest.raises(ProviderIntegrityError):
        adapter._wire(old, "s", "c")  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(ProviderIntegrityError):
        adapter._wire(wire, "foreign", "foreign")  # pyright: ignore[reportPrivateUsage]
    assert adapter.forward_entries == adapter.submit_calls == 0


@pytest.mark.parametrize("transport", ["not_submitted", "transient_not_submitted"])
@pytest.mark.asyncio
async def test_explicit_receiver_rejection_is_counted_once(
    tmp_path: Path, transport: str
) -> None:
    spec = native_spec(tmp_path)
    store = tmp_path / "store"
    with prepare_run(spec, store=store, run_id="001"):
        pass
    adapter = MockDecisionsAdapter(
        store / "mock-service.sqlite3",
        scenario=DecisionsScenarioV1(transport=cast(Any, transport)),
    )
    report = await execute_run("001", store=store, adapter=adapter)
    calls = 3 if transport == "transient_not_submitted" else 1
    assert (
        report.summary.results == 3 * calls and report.summary.successful_results == 0
    )
    assert adapter.forward_entries == adapter.submit_calls == calls
    assert adapter.ledger.acceptance_count() == 0
    with Repository(store) as repository:
        submissions = list(repository.iter_submissions("001"))
        assert len({s.attempt_id for s in submissions}) == len(submissions) == calls
        assert {s.certainty for s in submissions} == {"not_submitted"}


@pytest.mark.asyncio
async def test_cancellation_drains_the_real_receiver_acceptance_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = native_spec(tmp_path)
    adapter = MockDecisionsAdapter(tmp_path / "ledger.sqlite3")
    record = ResponseItem(item_id="000", text="Synthetic source 0.")
    dependency = next(
        plan_source(spec, record, lambda _: record, adapter.transport_context, None)
    )
    wire = prepare_request(
        spec,
        dependency,
        physical_identity("001", dependency.request.request_uid, "attempt"),
        adapter.transport_context,
    )
    entered, release = threading.Event(), threading.Event()
    establish = adapter.ledger.establish

    def delayed(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        assert release.wait(5)
        return establish(*args, **kwargs)

    monkeypatch.setattr(adapter.ledger, "establish", delayed)
    identity = wire.transport_commitment.identity
    pending = asyncio.create_task(
        adapter.submit(
            wire,
            submission_id=identity.submission_id,
            correlation_id=identity.correlation_id,
        )
    )
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        pending.cancel()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        pending.cancel()
        await asyncio.sleep(0)
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert adapter.forward_entries == adapter.ledger.acceptance_count() == 1
    original = await adapter.retrieve(
        wire,
        submission_id=identity.submission_id,
        correlation_id=identity.correlation_id,
    )
    assert original.kind == "response" and adapter.forward_entries == 1


@pytest.mark.asyncio
async def test_absent_or_changed_observation_cannot_be_retrieved_as_success(
    tmp_path: Path,
) -> None:
    spec = native_spec(tmp_path)
    store = tmp_path / "store"
    with prepare_run(spec, store=store, run_id="001"):
        pass
    adapter = MockDecisionsAdapter(
        store / "mock-service.sqlite3",
        scenario=DecisionsScenarioV1(transport="timeout_after"),
        reconciliation_available=False,
    )
    await execute_run("001", store=store, adapter=adapter)
    altered = MockDecisionsAdapter(
        store / "mock-service.sqlite3",
        scenario=DecisionsScenarioV1.model_validate(
            {"members": {"00": {"outcome": "refusal"}}}
        ),
    )
    with pytest.raises(ExceptionGroup):
        await recover_run("001", store=store, adapter=altered)
    assert altered.forward_entries == 0
    unavailable = MockDecisionsAdapter(
        store / "mock-service.sqlite3", retrieval_available=False
    )
    held = await recover_run("001", store=store, adapter=unavailable)
    assert (
        held.recovery_required
        and held.summary.results == unavailable.forward_entries == 0
    )
    spec2, dependency, _ = native_request(tmp_path / "unreceived")
    empty = MockDecisionsAdapter(tmp_path / "empty.sqlite3")
    wire = prepare_request(
        spec2,
        dependency,
        physical_identity("001", dependency.request.request_uid, "attempt"),
        empty.transport_context,
    )
    args = dict(
        submission_id=wire.transport_commitment.identity.submission_id,
        correlation_id=wire.transport_commitment.identity.correlation_id,
    )
    with pytest.raises(RetrievalUnavailable):
        await empty.retrieve(wire, **args)
    empty.retrieval_available = False
    with pytest.raises(RetrievalUnavailable):
        await empty.retrieve(wire, **args)


@pytest.mark.parametrize(
    "fault", ["raw", "parser_missing", "parser_type", "parser_hash", "current_wire"]
)
@pytest.mark.asyncio
async def test_received_native_response_keeps_acceptance_but_cannot_fabricate_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    spec = native_spec(tmp_path)
    store = tmp_path / "store"
    with prepare_run(spec, store=store, run_id="001"):
        pass
    adapter = MockDecisionsAdapter(store / "mock-service.sqlite3")
    session = ExecutionSession("001", store, adapter, spec, False, None)
    try:
        work = session.next_work()
        assert work is not None and isinstance(
            work.prepared, PreparedDecisionsRequestV1
        )
        assert session.boundary(work)
        ack = await adapter.submit(
            work.prepared,
            submission_id=work.submission_id,
            correlation_id=work.correlation_id,
        )
        session.acknowledge(work, ack)
        response = await adapter.retrieve(
            work.prepared,
            submission_id=work.submission_id,
            correlation_id=work.correlation_id,
        )
        if fault == "raw":
            response = response.model_copy(update={"raw": response.raw + b" "})
        elif fault == "parser_missing":
            monkeypatch.setattr(adapter, "parse_decisions", None)
        elif fault == "parser_type":

            def wrong_type(*_args: Any) -> None:
                return None

            monkeypatch.setattr(adapter, "parse_decisions", wrong_type)
        elif fault == "parser_hash":
            parsed = parse_synthetic_decisions(response, work.prepared)

            def wrong_hash(*_args: Any) -> Any:
                return parsed.model_copy(update={"response_sha256": "f" * 64})

            monkeypatch.setattr(adapter, "parse_decisions", wrong_hash)
        else:
            wire = work.prepared

            def changed_wire(*_args: Any, **_kwargs: Any) -> PreparedDecisionsRequestV1:
                return wire.model_copy(update={"model": "foreign"})

            monkeypatch.setattr(session, "prepare_attempt", changed_wire)
        with pytest.raises(IntegrityError):
            session.process(work, response)
        assert session.repository.summary("001").results == 0
        assert (
            session.repository.get_submission("001", work.submission_id).certainty
            == "accepted"
        )
        assert adapter.forward_entries == 1
    finally:
        session.close()


def test_native_and_historical_acceptance_authorities_are_not_interchangeable(
    tmp_path: Path,
) -> None:
    spec = native_spec(tmp_path / "new")
    store = tmp_path / "new-store"
    with prepare_run(spec, store=store, run_id="001"):
        pass
    adapter = MockDecisionsAdapter(store / "mock-service.sqlite3")
    session = ExecutionSession("001", store, adapter, spec, False, None)
    try:
        work = session.next_work()
        assert work is not None
        with pytest.raises(IntegrityError):
            session.acknowledge(
                work, ProviderAcknowledgment(remote_id="real", observation_key="local")
            )
        adapter.transport_context = adapter.transport_context.model_copy(
            update={"service_identity": "foreign"}
        )
        with pytest.raises(IntegrityError):
            session.prepare_attempt(work.dependency, work.attempt_id, initial=True)
    finally:
        session.close()
    old = specification(tmp_path / "old", responses=1, traits=1)
    old.provider, old.endpoint = "mock", "synthetic"
    old_store = tmp_path / "old-store"
    with prepare_run(old, store=old_store, run_id="001"):
        pass
    previous = ExecutionSession(
        "001", old_store, MockAdapter(tmp_path / "old-ledger.sqlite3"), old, False, None
    )
    try:
        work = previous.next_work()
        assert work is not None
        evidence = CompleteResponseEvidenceV1(
            run_id="001",
            request_uid=work.prepared.request_uid,
            attempt_id=work.attempt_id,
            submission_id=work.submission_id,
            correlation_id=work.correlation_id,
            payload_sha256=work.prepared.payload_sha256,
            response_sha256="f" * 64,
            response_byte_length=1,
            http_status=200,
            protocol="judgmentllm-decisions-offline-v1",
        )
        with pytest.raises(IntegrityError):
            previous.acknowledge(
                work,
                ProviderAcknowledgmentV2(
                    remote_id=None, observation_key="local", response_evidence=evidence
                ),
            )
        assert previous.repository.summary("001").results == 0
    finally:
        previous.close()


def test_uncertain_evidence_cannot_replace_a_previous_forwarding_binding(
    tmp_path: Path,
) -> None:
    from judgmentllm.evidence.usage import ProviderUsage
    from judgmentllm.persistence.decisions_invocations import DecisionsInvocationV1

    spec = native_spec(tmp_path)
    store = tmp_path / "store"
    with prepare_run(spec, store=store, run_id="001"):
        pass
    adapter = MockDecisionsAdapter(store / "mock-service.sqlite3")
    session = ExecutionSession("001", store, adapter, spec, False, None)
    try:
        work = session.next_work()
        assert work is not None and isinstance(
            work.prepared, PreparedDecisionsRequestV1
        )
        assert session.prepared is not None
        identity = work.prepared.transport_commitment.identity
        fact = DecisionsInvocationV1(
            identity=identity,
            payload_sha256="f" * 64,
            usage=ProviderUsage(
                run_id="001",
                request_uid=identity.request_uid,
                attempt_id=identity.attempt_id,
                submission_id=identity.submission_id,
                observation_id=None,
                provider="mock",
                endpoint="synthetic",
                requested_model=spec.model,
                observation_kind="invocation",
            ),
        )
        publish_invocation(session.prepared.store, fact)
        with pytest.raises(IntegrityError, match="forwarding identity"):
            session.evidence.uncertain(work, known_invoked=True)
        assert session.repository.summary("001").results == adapter.forward_entries == 0
    finally:
        session.close()


@pytest.mark.parametrize(
    "fault", ["legacy_parse", "native_type", "native_current", "v2_type", "v2_current"]
)
@pytest.mark.asyncio
async def test_historical_response_guards_remain_active_after_native_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    if fault == "native_current":
        adapter, store, spec = prepared_case(tmp_path)
    else:
        spec = (
            load_run_spec(configuration(tmp_path / "inputs", capture="full"))
            if fault.startswith("v2")
            else specification(tmp_path / "inputs", responses=1, traits=1)
        )
        if not fault.startswith("v2"):
            spec.provider, spec.endpoint = "mock", "synthetic"
        store = tmp_path / "store"
        adapter = MockAdapter(
            store / "mock-service.sqlite3",
            scenario=MockScenario(
                outcome="refusal" if fault == "legacy_parse" else "valid"
            ),
        )
        with prepare_run(spec, store=store, run_id="001"):
            pass
    session = ExecutionSession("001", store, adapter, spec, False, None)
    try:
        work = session.next_work()
        assert work is not None and session.prepared is not None
        assert session.boundary(work)
        ack = await adapter.submit(
            work.prepared,
            submission_id=work.submission_id,
            correlation_id=work.correlation_id,
        )
        session.acknowledge(work, ack)
        response = await adapter.retrieve(
            work.prepared,
            submission_id=work.submission_id,
            correlation_id=work.correlation_id,
        )
        if fault == "legacy_parse":

            def cannot_parse(*_args: Any, **_kwargs: Any) -> Any:
                raise ValueError("synthetic parser defect")

            monkeypatch.setattr(adapter, "parse", cannot_parse)
        elif fault == "native_type":

            def changed_manifest(stage: str, _request: str) -> None:
                if stage == "before_parsing":
                    assert session.prepared is not None
                    session.prepared.manifest = session.prepared.manifest.model_copy(
                        update={"schema_version": 3}
                    )

            session.fault = changed_manifest
        elif fault == "v2_type":
            data = work.prepared.model_dump(include=set(PreparedRequest.model_fields))
            work = replace(work, prepared=PreparedRequest.model_validate(data))
        else:
            wire = work.prepared

            def different(*_args: Any, **_kwargs: Any) -> PreparedRequest:
                return PreparedRequest.model_validate(
                    wire.model_dump(include=set(PreparedRequest.model_fields))
                )

            monkeypatch.setattr(session, "prepare_attempt", different)
        with pytest.raises(
            IntegrityError,
            match="could not be parsed|committed output|commitment changed",
        ):
            session.process(work, response)
        assert session.repository.summary("001").results == 0
        assert adapter.ledger.acceptance_count() == 1
    finally:
        session.close()


@pytest.mark.asyncio
async def test_historical_subset_retry_cannot_gain_native_decisions_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from judgmentllm.execution.retry import authorize_subset_retry
    from judgmentllm.preparation import PreparedRun

    adapter, store, spec = prepared_case(
        tmp_path / "old",
        scenario=MockScenario(
            members={"02": MockMemberScenario(outcome="invalid_category")}
        ),
    )
    await execute_run("001", store=store, adapter=adapter)
    with Repository(store) as repository:
        failed = next(
            r for r in repository.iter_results("001") if r.status == "invalid"
        )
    _, _, foreign = native_request(tmp_path / "new")

    original = PreparedRun.prepare_transport_attempt

    def substitute(self: PreparedRun, *args: Any, **kwargs: Any) -> Any:
        if kwargs.get("parent") is not None:
            return foreign
        return original(self, *args, **kwargs)

    monkeypatch.setattr(PreparedRun, "prepare_transport_attempt", substitute)
    with pytest.raises(IntegrityError, match="historical retry cannot acquire"):
        authorize_subset_retry(
            "001",
            store=store,
            adapter=adapter,
            spec=spec,
            task_ids=(failed.task_id,),
            operation_id="cross-version",
        )
    assert adapter.submit_calls == adapter.ledger.acceptance_count() == 1


@pytest.mark.parametrize("remote_id", [None, "real"])
@pytest.mark.asyncio
async def test_reconciliation_cannot_substitute_a_remote_id_for_complete_evidence(
    tmp_path: Path, remote_id: str | None
) -> None:
    spec = native_spec(tmp_path)
    store = tmp_path / "store"
    with prepare_run(spec, store=store, run_id="001"):
        pass
    original = MockDecisionsAdapter(
        store / "mock-service.sqlite3",
        scenario=DecisionsScenarioV1(transport="timeout_after"),
        reconciliation_available=False,
    )
    await execute_run("001", store=store, adapter=original)

    class MissingEvidence(MockDecisionsAdapter):
        async def reconcile(
            self, prepared: PreparedRequest, *, submission_id: str, correlation_id: str
        ) -> DecisionsReconciliationEvidenceV1:
            return DecisionsReconciliationEvidenceV1(
                outcome="accepted", remote_id=remote_id, observation_key="local"
            )

    replacement = MissingEvidence(store / "mock-service.sqlite3")
    with pytest.raises(ExceptionGroup):
        await recover_run("001", store=store, adapter=replacement)
    assert replacement.forward_entries == replacement.submit_calls == 0
