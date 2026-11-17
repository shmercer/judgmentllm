"""Failure and privacy regressions assert outcomes independently of the parser."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx2
import pytest
from test_decisions_contracts import pair_spec
from test_openai_decisions_sdk import envelope, spec_for
from test_openai_decisions_sdk import offline as offline

from judgmentllm.config import RunSpecV4
from judgmentllm.decisions.provenance import (
    PhysicalProvenanceV1,
    export_decisions_provenance,
)
from judgmentllm.decisions.transport import DecisionsDependencyV1
from judgmentllm.execution import execute_run, recover_run
from judgmentllm.execution.retry import authorize_subset_retry
from judgmentllm.execution.session import ExecutionSession, ExecutionWork
from judgmentllm.lifecycle import aresume_run
from judgmentllm.lifecycle_models import RetrySelection
from judgmentllm.persistence.artifacts import ArtifactStore
from judgmentllm.persistence.evidence import (
    DecisionsEvidenceReceiptV2,
    read_evidence_receipt,
)
from judgmentllm.persistence.receipts import (
    AcknowledgmentReceiptV2,
    read_acknowledgment,
)
from judgmentllm.persistence.repository import Repository
from judgmentllm.preparation import prepare_run
from judgmentllm.providers.openai_decisions import (
    OpenAIDecisionsAdapter,
    decisions_context,
)
from judgmentllm.providers.protocol import (
    PreparedRequest,
    ProviderError,
    ProviderResponse,
)


@pytest.mark.parametrize(
    "case,expected",
    [
        ("missing", ["complete", "complete", "invalid"]),
        ("duplicate", ["invalid", "complete", "complete"]),
        ("triple_duplicate", ["invalid", "complete", "complete"]),
        ("unexpected", ["complete"] * 3),
        ("extra_field", ["complete", "invalid", "complete"]),
        ("bad_sum", ["invalid", "complete", "complete"]),
        ("bad_label", ["invalid", "complete", "complete"]),
        ("bad_index", ["invalid", "complete", "complete"]),
        ("bad_type", ["invalid", "complete", "complete"]),
        ("refusal", ["refused", "complete", "complete"]),
        ("order", ["invalid"] * 3),
        ("duplicate_json", ["invalid"] * 3),
        ("utf8", ["invalid"] * 3),
        ("unknown_root", ["complete"] * 3),
        ("bad_usage", ["complete"] * 3),
    ],
)
@pytest.mark.asyncio
async def test_received_failures_are_not_repaired_or_hidden(
    tmp_path: Path, case: str, expected: list[str]
) -> None:
    spec = spec_for(tmp_path)
    calls = 0

    def respond(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        doc = envelope(json.loads(request.content))
        answers = doc["answers"]
        if case == "missing":
            answers.pop()
        elif case == "duplicate":
            answers.insert(1, answers[0])
        elif case == "triple_duplicate":
            answers.insert(1, answers[0])
            answers.insert(2, {**answers[0], "private": "UNSAFE_WIRE_CANARY"})
        elif case == "unexpected":
            answers.insert(1, dict(name="UNSAFE_WIRE_CANARY", type="refusal"))
        elif case == "extra_field":
            answers[1]["credential"] = "UNSAFE_WIRE_CANARY"
        elif case == "bad_sum":
            answers[0]["probabilities"][0]["probability"] = 0.7
        elif case == "bad_label":
            answers[0]["probabilities"][0]["label"] = "UNSAFE_WIRE_CANARY"
        elif case == "bad_index":
            answers[0]["probabilities"][0]["value"] = True
        elif case == "bad_type":
            answers[0]["type"] = {"private": "UNSAFE_WIRE_CANARY"}
        elif case == "refusal":
            answers[0] = dict(name=answers[0]["name"], type="refusal")
        elif case == "order":
            answers.reverse()
        elif case == "unknown_root":
            doc["private"] = "UNSAFE_WIRE_CANARY"
        elif case == "bad_usage":
            doc["usage"] = []
        elif case == "duplicate_json":
            return httpx2.Response(
                200,
                content=b'{"answers":[],"answers":[],"secret":"UNSAFE_WIRE_CANARY"}',
            )
        elif case == "utf8":
            return httpx2.Response(200, content=b"\xffUNSAFE_WIRE_CANARY")
        return httpx2.Response(200, json=doc)

    store = tmp_path / "store"
    with prepare_run(spec, store=store, run_id="001"):
        pass
    adapter = OpenAIDecisionsAdapter(
        spec.model, http_transport=httpx2.MockTransport(respond)
    )
    try:
        await asyncio.wait_for(execute_run("001", store=store, adapter=adapter), 15)
        await recover_run("001", store=store, adapter=adapter)
        with Repository(store) as repo:
            rows = list(repo.iter_results("001"))
            assert [r.status for r in rows] == expected
            attempt = next(repo.iter_attempts("001"))
            submission = repo.get_submission_for_attempt("001", attempt.attempt_id)
            assert submission is not None
            artifacts = ArtifactStore(repo.run_directory("001"), create=False)
            ack = read_acknowledgment(artifacts, submission.submission_id)
            evidence = read_evidence_receipt(artifacts, submission.submission_id)
            assert ack is not None and evidence is not None
            with pytest.raises(ValueError):
                AcknowledgmentReceiptV2.model_validate(ack.model_dump())
            with pytest.raises(ValueError):
                DecisionsEvidenceReceiptV2.model_validate(evidence.model_dump())
        bundle = export_decisions_provenance("001", store=store)
        physical = next(f for f in bundle.manifest.files if f.name == "physical.jsonl")
        assert physical.artifact.relative_path is not None
        root = next((store / "runs").iterdir())
        row = json.loads((root / physical.artifact.relative_path).read_text())
        if case == "triple_duplicate":
            assert row["validation"]["members"][0]["answer_ordinals"] == [0, 1, 2]
        if case == "order":
            assert [m["answer_ordinals"] for m in row["validation"]["members"]] == [
                [2],
                [1],
                [0],
            ]
        if case == "bad_usage":
            assert "unknown_field" in row["validation"]["findings"]
            assert row["retained_kind"] == "filtered_replay"
        with pytest.raises(ValueError):
            PhysicalProvenanceV1.model_validate(row)
        question_file = next(
            f for f in bundle.manifest.files if f.name == "questions.jsonl"
        )
        assert question_file.artifact.relative_path is not None
        questions = [
            json.loads(line)
            for line in (root / question_file.artifact.relative_path)
            .read_text()
            .splitlines()
        ]
        assert all(q["whole_response_valid"] is False for q in questions)
        assert calls == 1
        for path in root.rglob("*"):
            if path.is_file():
                assert b"UNSAFE_WIRE_CANARY" not in path.read_bytes()
    finally:
        await adapter.aclose()


@pytest.mark.parametrize(
    "count,maximum,groups",
    [
        (1, 32, [1]),
        (2, 32, [2]),
        (12, 32, [12]),
        (13, 1, [1] * 13),
        (400, 500, [400]),
        (401, 500, [400, 1]),
        (4, 3, [3, 1]),
    ],
)
def test_exact_endpoint_and_user_splitting(
    tmp_path: Path, count: int, maximum: int, groups: list[int]
) -> None:
    spec = spec_for(tmp_path, traits=count, maximum=maximum)
    with prepare_run(spec, store=tmp_path / "store", run_id="001") as prepared:
        dependencies = list(prepared.iter_request_dependencies())
        assert [len(d.request.tasks) for d in dependencies] == groups
        assert len({t.task_id for d in dependencies for t in d.request.tasks}) == count


@pytest.mark.asyncio
async def test_multi_criterion_orientation_repetition_and_subset_retry(
    tmp_path: Path,
) -> None:
    base = pair_spec(tmp_path, criteria=13).model_dump()
    spec = RunSpecV4.model_validate(
        {**base, "provider": "openai", "endpoint": "decisions", "model": "gpt-6-luna"}
    )
    calls = 0

    def respond(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        doc = envelope(json.loads(request.content))
        if calls == 1:
            doc["answers"][0].pop("choice")
        return httpx2.Response(200, json=doc)

    store = tmp_path / "store"
    with prepare_run(spec, store=store, run_id="001"):
        pass
    adapter = OpenAIDecisionsAdapter(
        spec.model, http_transport=httpx2.MockTransport(respond)
    )
    try:
        await execute_run("001", store=store, adapter=adapter)
        with Repository(store) as repo:
            rows = list(repo.iter_results("001"))
            assert len(rows) == 52 and sum(r.status == "invalid" for r in rows) == 1
            for r in rows:
                assert r.task_kind == "pairwise"
                if r.status == "complete":
                    assert r.winner_id == r.presented_order[1]
                    assert (r.p_left, r.p_right) == (
                        (0.2, 0.8)
                        if r.presented_order == r.original_order
                        else (0.8, 0.2)
                    )
            failed = next(r for r in rows if r.status == "invalid")
        authorization = authorize_subset_retry(
            "001",
            store=store,
            adapter=adapter,
            task_ids=(failed.task_id,),
            operation_id="retry-001",
        )
        await recover_run("001", store=store, adapter=adapter)
        authorize_subset_retry(
            "001",
            store=store,
            adapter=adapter,
            task_ids=(failed.task_id,),
            operation_id="retry-001",
        )
        await recover_run("001", store=store, adapter=adapter)
        with Repository(store) as repo:
            assert len(list(repo.iter_results("001"))) == 53
        assert authorization is not None and calls == 5
    finally:
        await adapter.aclose()


def test_invalid_controls_and_sdk_version_precede_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from judgmentllm.providers import openai_decisions as module

    spec = spec_for(tmp_path)

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("credential resolution before preflight")

    monkeypatch.setattr(module, "_resolve_credentials", forbidden)
    for patch in (
        {"execution": {"tier": "standard"}},
        {"generation": {"temperature": 0}},
        {"strict_capabilities": True},
    ):
        with pytest.raises((ValueError, module.ProviderError)):
            decisions_context(RunSpecV4.model_validate({**spec.model_dump(), **patch}))
    monkeypatch.setattr(module, "openai_sdk_version", lambda: "3.42.2")
    with pytest.raises(module.OpenAIReadinessError, match="3.42.1"):
        OpenAIDecisionsAdapter(spec.model).validate_credentials()


@pytest.mark.asyncio
async def test_ambient_headers_and_debug_logs_cannot_leak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    spec = spec_for(tmp_path)
    monkeypatch.setenv(
        "OPENAI_CUSTOM_HEADERS",
        "Authorization: AMBIENT_PRIVATE_CANARY\nX-Private: AMBIENT_PRIVATE_CANARY",
    )
    monkeypatch.setenv("OPENAI_BASE_URL", "https://ambient.invalid")
    caplog.set_level(logging.DEBUG)

    def respond(request: httpx2.Request) -> httpx2.Response:
        assert "AMBIENT_PRIVATE_CANARY" not in str(request.headers)
        return httpx2.Response(200, json=envelope(json.loads(request.content)))

    with prepare_run(spec, store=tmp_path / "store", run_id="001"):
        pass
    adapter = OpenAIDecisionsAdapter(
        spec.model, http_transport=httpx2.MockTransport(respond)
    )
    try:
        await execute_run("001", store=tmp_path / "store", adapter=adapter)
    finally:
        await adapter.aclose()
    assert "synthetic-decisions-key" not in caplog.text
    assert "AMBIENT_PRIVATE_CANARY" not in caplog.text


@pytest.mark.parametrize("levels", [2, 10, 11])
def test_score_limits_cannot_be_overridden_or_become_neutral_limits(
    tmp_path: Path, levels: int
) -> None:
    spec = spec_for(tmp_path)
    data = spec.model_dump()
    data["rubric"]["traits"][0]["levels"] = [
        dict(label=f"{i:02d}", description=f"Level {i}", numeric_value=i)
        for i in range(levels)
    ]
    checked = RunSpecV4.model_validate(data)
    if levels == 11:
        with pytest.raises(ProviderError, match="2–10"):
            prepare_run(checked, store=tmp_path / "openai-store", run_id="001")
        neutral = RunSpecV4.model_validate(
            {**data, "provider": "mock", "endpoint": "synthetic"}
        )
        with prepare_run(
            neutral, store=tmp_path / "neutral-store", run_id="001"
        ) as prepared:
            assert prepared.spec.rubric is not None
            assert len(prepared.spec.rubric.traits[0].levels) == 11
    else:
        with prepare_run(checked, store=tmp_path / "store", run_id="001") as prepared:
            dependency = next(prepared.iter_request_dependencies())
            assert len(dependency.request.tasks) == 3


def test_complete_trait_instructions_and_examples_are_question_local(
    tmp_path: Path,
) -> None:
    spec = spec_for(tmp_path, traits=2)
    data = spec.model_dump()
    data["exemplars"] = [
        dict(exemplar_id="example-a", text="EXAMPLE_A_COMPLETE", scores={"00": "00"}),
        dict(exemplar_id="example-b", text="EXAMPLE_B_COMPLETE", scores={"01": "02"}),
    ]
    data["prompt"]["exemplar_ids_by_trait"] = {"00": ["example-a"], "01": ["example-b"]}
    checked = RunSpecV4.model_validate(data)
    with prepare_run(checked, store=tmp_path / "store", run_id="001") as prepared:
        dependency = next(prepared.iter_request_dependencies())
        assert isinstance(dependency, DecisionsDependencyV1)
        first, second = dependency.questions
        assert (
            "EXAMPLE_A_COMPLETE" in first.instructions
            and "EXAMPLE_B_COMPLETE" not in first.instructions
        )
        assert (
            "EXAMPLE_B_COMPLETE" in second.instructions
            and "EXAMPLE_A_COMPLETE" not in second.instructions
        )
        assert (
            "Assess criterion 0." in first.instructions
            and "Assess criterion 1." in second.instructions
        )
        assert (
            "EXAMPLE_A_COMPLETE" not in dependency.shared_input
            and "EXAMPLE_B_COMPLETE" not in dependency.shared_input
        )


@pytest.mark.asyncio
async def test_public_resume_preserves_eight_worker_capacity_across_burst_and_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = spec_for(tmp_path, traits=17, maximum=1).model_dump()
    data["execution"]["concurrency"] = 8
    spec = RunSpecV4.model_validate(data)
    store = tmp_path / "store"
    with prepare_run(spec, store=store, run_id="001") as prepared:
        keys: list[str] = []
        for dependency in prepared.iter_request_dependencies():
            assert isinstance(dependency, DecisionsDependencyV1)
            keys.extend(q.key for q in dependency.questions)
    assert len(keys) == 17

    burst = asyncio.Event()
    handoff_full = asyncio.Event()
    ninth_forwarded = asyncio.Event()
    forwarded: list[str] = []
    submitted: set[str] = set()
    capacities: list[int] = []
    buffered: list[int] = []
    first_drained = False
    retrying = False

    async def respond(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        name = body["questions"][0]["name"]
        forwarded.append(name)
        if not retrying:
            if len(forwarded) == 8:
                burst.set()
            if len(forwarded) <= 8:
                await burst.wait()
            else:
                ninth_forwarded.set()
        doc = envelope(body)
        if not retrying and name == keys[0]:
            doc["answers"][0].pop("score")
        return httpx2.Response(200, json=doc)

    original_init = OpenAIDecisionsAdapter.__init__
    original_submit = OpenAIDecisionsAdapter.submit
    original_retrieve = OpenAIDecisionsAdapter.retrieve

    def initialize(self: OpenAIDecisionsAdapter, *args: Any, **kwargs: Any) -> None:
        original_init(
            self, *args, **{**kwargs, "http_transport": httpx2.MockTransport(respond)}
        )
        capacities.append(self.concurrency)

    async def submit(
        self: OpenAIDecisionsAdapter,
        prepared: PreparedRequest,
        *,
        submission_id: str,
        correlation_id: str,
    ):
        ack = await original_submit(
            self, prepared, submission_id=submission_id, correlation_id=correlation_id
        )
        assert submission_id not in submitted
        submitted.add(submission_id)
        buffered.append(len(self._responses))  # pyright: ignore[reportPrivateUsage]
        if len(submitted) == 8:
            handoff_full.set()
        return ack

    async def retrieve(
        self: OpenAIDecisionsAdapter,
        prepared: PreparedRequest,
        *,
        submission_id: str,
        correlation_id: str,
    ):
        nonlocal first_drained
        if not retrying:
            await handoff_full.wait()
            if not first_drained:
                first_drained = True
                response = await original_retrieve(
                    self,
                    prepared,
                    submission_id=submission_id,
                    correlation_id=correlation_id,
                )
                assert len(self._responses) == 7  # pyright: ignore[reportPrivateUsage]
                return response
            await ninth_forwarded.wait()
        return await original_retrieve(
            self, prepared, submission_id=submission_id, correlation_id=correlation_id
        )

    monkeypatch.setattr(OpenAIDecisionsAdapter, "__init__", initialize)
    monkeypatch.setattr(OpenAIDecisionsAdapter, "submit", submit)
    monkeypatch.setattr(OpenAIDecisionsAdapter, "retrieve", retrieve)
    initial = await asyncio.wait_for(aresume_run("001", store=store), 20)
    assert initial.results == 17 and initial.successful_results == 16
    assert Counter(forwarded) == Counter(keys)
    assert max(buffered) == 8 and ninth_forwarded.is_set()
    with Repository(store) as repo:
        failed = next(r for r in repo.iter_results("001") if r.status == "invalid")
    retrying = True
    selection = RetrySelection(task_ids=(failed.task_id,), operation_id="retry-001")
    retried = await asyncio.wait_for(
        aresume_run("001", store=store, retry=selection), 20
    )
    assert retried.results == 18 and retried.successful_results == 17
    recovered = await asyncio.wait_for(
        aresume_run("001", store=store, retry=selection), 20
    )
    assert recovered.results == 18 and not recovered.recovery_required
    assert Counter(forwarded) == Counter([*keys, keys[0]])
    assert len(submitted) == 18 and capacities == [8, 8, 8]
    with Repository(store) as repo:
        assert len(list(repo.iter_submissions("001"))) == 18
        assert len(list(repo.iter_evidence("001"))) == 18
        assert (
            repo.connection.execute("SELECT count(*) FROM usage_revisions").fetchone()[
                0
            ]
            == 18
        )


@asynccontextmanager
async def production_boundary(
    root: Path, *, filtered: bool = False
) -> AsyncGenerator[tuple[ExecutionSession, ExecutionWork, ProviderResponse], None]:
    """One real SDK response, acknowledged but not yet processed by the session."""
    spec = spec_for(root / "inputs")
    store = root / "store"
    with prepare_run(spec, store=store, run_id="001"):
        pass

    def respond(request: httpx2.Request) -> httpx2.Response:
        doc = envelope(json.loads(request.content))
        if filtered:
            doc["answers"][1]["private"] = "UNSAFE_REPLAY_CANARY"
            doc["answers"].insert(1, dict(name="UNSAFE_REPLAY_CANARY", type="refusal"))
        return httpx2.Response(200, json=doc)

    adapter = OpenAIDecisionsAdapter(
        spec.model, http_transport=httpx2.MockTransport(respond)
    )
    session = ExecutionSession("001", store, adapter, spec, False, None)
    try:
        work = session.next_work()
        assert work is not None and session.boundary(work)
        acknowledgment = await asyncio.wait_for(
            adapter.submit(
                work.prepared,
                submission_id=work.submission_id,
                correlation_id=work.correlation_id,
            ),
            15,
        )
        session.acknowledge(work, acknowledgment)
        response = await adapter.retrieve(
            work.prepared,
            submission_id=work.submission_id,
            correlation_id=work.correlation_id,
        )
        yield session, work, response
        assert adapter.actual_call_count == 1
    finally:
        session.close()
        await adapter.aclose()


async def production_records(root: Path) -> dict[str, dict[str, Any]]:
    from judgmentllm.decisions.transport import PreparedDecisionsRequestV2
    from judgmentllm.providers.openai_decisions_response import analyze

    async with production_boundary(root) as (session, work, response):
        assert session.prepared is not None and isinstance(
            work.prepared, PreparedDecisionsRequestV2
        )
        session.process(work, response)
        ack = read_acknowledgment(session.prepared.store, work.submission_id)
        evidence = read_evidence_receipt(session.prepared.store, work.submission_id)
        assert ack is not None and evidence is not None
        replay, safe = analyze(response.raw, work.prepared)
        assert safe
        records = {
            "manifest": session.prepared.manifest.model_dump(mode="json"),
            "wire": work.prepared.model_dump(mode="json"),
            "ack": ack.model_dump(mode="json"),
            "acceptance": ack.model_dump(mode="json")["response_evidence"],
            "evidence": evidence.model_dump(mode="json"),
            "replay": replay.model_dump(mode="json"),
            "replay_metadata": replay.metadata.model_dump(mode="json"),
        }
    records["bundle"] = export_decisions_provenance(
        "001", store=root / "store"
    ).manifest.model_dump(mode="json")
    return records


@pytest.mark.parametrize("invalid", [True, 0])
@pytest.mark.parametrize(
    "family,field",
    [
        ("manifest", "schema_version"),
        ("wire", "preparation_version"),
        ("ack", "acknowledgment_version"),
        ("acceptance", "acceptance_evidence_version"),
        ("evidence", "evidence_receipt_version"),
        ("replay", "replay_version"),
        ("replay_metadata", "replay_policy_version"),
        ("bundle", "physical_row_version"),
        ("bundle", "question_row_version"),
    ],
)
@pytest.mark.asyncio
async def test_production_versions_reject_coercion_without_relabeling_history(
    tmp_path: Path, family: str, field: str, invalid: bool | int
) -> None:
    from pydantic import BaseModel

    from judgmentllm.decisions.provenance import DecisionsProvenanceManifestV2
    from judgmentllm.decisions.replay import (
        DecisionsReplayMetadataV1,
        OpenAIDecisionsReplayV1,
    )
    from judgmentllm.decisions.transport import PreparedDecisionsRequestV2
    from judgmentllm.persistence.evidence import DecisionsEvidenceReceiptV3
    from judgmentllm.persistence.receipts import (
        AcknowledgmentReceiptV3,
        CompleteResponseEvidenceV2,
    )
    from judgmentllm.preparation.manifest import ExecutableRunManifestV9

    models: dict[str, type[BaseModel]] = {
        "manifest": ExecutableRunManifestV9,
        "wire": PreparedDecisionsRequestV2,
        "ack": AcknowledgmentReceiptV3,
        "acceptance": CompleteResponseEvidenceV2,
        "evidence": DecisionsEvidenceReceiptV3,
        "replay": OpenAIDecisionsReplayV1,
        "replay_metadata": DecisionsReplayMetadataV1,
        "bundle": DecisionsProvenanceManifestV2,
    }
    data = (await production_records(tmp_path))[family]
    model = models[family]
    checked = model.model_validate_json(json.dumps(data))
    assert checked.model_dump(mode="json") == data
    if family == "evidence":
        # JSON field adaptation rejects bad literals before the Python boundary validator.
        python_data = checked.model_dump()
        python_data[field] = invalid
        with pytest.raises(ValueError):
            model.model_validate(python_data)
    data[field] = invalid
    with pytest.raises(ValueError):
        model.model_validate_json(json.dumps(data))


@pytest.mark.parametrize(
    "fault",
    [
        "scope",
        "missing_validation",
        "original_binding",
        "invocation_validation",
        "manifest_context",
        "replay_count",
        "replay_mask",
        "replay_failure",
    ],
)
@pytest.mark.asyncio
async def test_production_records_require_independent_scope_and_replay_bindings(
    tmp_path: Path, fault: str
) -> None:
    from pydantic import BaseModel

    from judgmentllm.decisions.replay import OpenAIDecisionsReplayV1
    from judgmentllm.persistence.evidence import DecisionsEvidenceReceiptV3
    from judgmentllm.preparation.manifest import ExecutableRunManifestV9

    records = await production_records(tmp_path)
    model: type[BaseModel] = DecisionsEvidenceReceiptV3
    data = records["evidence"]
    if fault == "scope":
        data["usage"].update(
            provider="mock",
            endpoint="synthetic",
            usage_source="unknown",
            semantics_version=1,
            breakdown=[],
            cache_write_tokens=None,
        )
    elif fault == "missing_validation":
        data["validation"] = None
    elif fault == "original_binding":
        data["validation"]["original_sha256"] = "f" * 64
    elif fault == "invocation_validation":
        data["identity"]["observation_id"] = None
        data["usage"].update(observation_id=None, observation_kind="invocation")
        data.update(
            order=None,
            response_receipt=None,
            observation_key=None,
            remote_id=None,
            retained=None,
            original=None,
            acknowledgment_sha256=None,
            response_receipt_sha256=None,
            response_retention="omitted",
            retained_kind="omitted",
        )
    elif fault == "manifest_context":
        model, data = ExecutableRunManifestV9, records["manifest"]
        data["production_context"]["account_scope"] = "foreign"
    else:
        model, data = OpenAIDecisionsReplayV1, records["replay"]
        if fault == "replay_count":
            data["answers"].pop()
        elif fault == "replay_mask":
            data["answers"][0] = None
        else:
            data["metadata"]["members"][0]["failure"] = "invalid_output"
    with pytest.raises(ValueError):
        model.model_validate_json(json.dumps(data))


@pytest.mark.asyncio
async def test_in_process_filtered_recovery_matches_real_process_contract(
    tmp_path: Path,
) -> None:
    from judgmentllm.persistence.evidence import DecisionsEvidenceReceiptV3

    async with production_boundary(tmp_path, filtered=True) as (
        session,
        work,
        response,
    ):
        spec = session.spec

        def stop(stage: str, _request: str) -> None:
            if stage == "after_receipt":
                raise RuntimeError("synthetic post-publication interruption")

        session.fault = stop
        with pytest.raises(RuntimeError, match="post-publication"):
            session.process(work, response)
        assert session.repository.summary("001").results == 0

    def forbidden(_request: httpx2.Request) -> httpx2.Response:
        raise AssertionError("Saved filtered response forwarded again")

    fresh = OpenAIDecisionsAdapter(
        spec.model,
        context=decisions_context(spec),
        http_transport=httpx2.MockTransport(forbidden),
    )
    try:
        first = await recover_run("001", store=tmp_path / "store", adapter=fresh)
        second = await recover_run("001", store=tmp_path / "store", adapter=fresh)
        assert not first.recovery_required and not second.recovery_required
        with Repository(tmp_path / "store") as repo:
            rows = list(repo.iter_results("001"))
            assert [r.status for r in rows] == ["complete", "invalid", "complete"]
            assert [r.task_id for r in rows] == list(work.prepared.task_ids)
            assert (
                repo.connection.execute(
                    "SELECT count(*) FROM usage_revisions"
                ).fetchone()[0]
                == 1
            )
            evidence = repo.get_evidence_for_submission("001", work.submission_id)
            assert (
                isinstance(evidence, DecisionsEvidenceReceiptV3)
                and evidence.validation is not None
            )
            assert evidence.usage.input_tokens == 120
            assert [m.answer_ordinals for m in evidence.validation.members] == [
                (0,),
                (2,),
                (3,),
            ]
            assert evidence.validation.unexpected_members == 1
            assert evidence.original is not None and evidence.retained is not None
            assert evidence.original.sha256 != evidence.retained.sha256
            for path in repo.run_directory("001").rglob("*"):
                if path.is_file():
                    assert b"UNSAFE_REPLAY_CANARY" not in path.read_bytes()
        assert fresh.actual_call_count == 0
    finally:
        await fresh.aclose()


@pytest.mark.parametrize("fault", ["ack_kind", "ack_hash", "ack_length", "commitment"])
@pytest.mark.asyncio
async def test_durable_evidence_checks_original_acceptance_before_replay(
    tmp_path: Path, fault: str
) -> None:
    from judgmentllm.persistence.evidence import DecisionsEvidenceReceiptV3
    from judgmentllm.persistence.hashing import canonical_bytes
    from judgmentllm.persistence.models import IntegrityError
    from judgmentllm.persistence.receipts import AcknowledgmentReceiptV3

    async with production_boundary(tmp_path, filtered=True) as (
        session,
        work,
        response,
    ):
        _, _, evidence = session.evidence.prepare(work, response)
        assert isinstance(evidence, DecisionsEvidenceReceiptV3)
        session.evidence.verify(evidence, work)
        if fault == "commitment":
            data = evidence.model_dump(mode="json")
            data["validation"]["commitment_sha256"] = "f" * 64
            evidence = DecisionsEvidenceReceiptV3.model_validate_json(json.dumps(data))
        else:
            ack = read_acknowledgment(session.evidence.store, work.submission_id)
            assert isinstance(ack, AcknowledgmentReceiptV3)
            data = ack.model_dump(mode="json")
            if fault == "ack_kind":
                data["acknowledgment_version"] = 2
                data["response_evidence"].update(
                    acceptance_evidence_version=1,
                    protocol="judgmentllm-decisions-offline-v1",
                )
                replacement = AcknowledgmentReceiptV2.model_validate_json(
                    json.dumps(data)
                )
            else:
                data["response_evidence"][
                    "response_sha256" if fault == "ack_hash" else "response_byte_length"
                ] = (
                    "f" * 64
                    if fault == "ack_hash"
                    else ack.response_evidence.response_byte_length + 1
                )
                replacement = AcknowledgmentReceiptV3.model_validate_json(
                    json.dumps(data)
                )
            raw = canonical_bytes(replacement)
            session.evidence.store.put(raw, "application/json")
            # A corrupt reference names a real, canonical object with a different binding.
            next(
                (session.evidence.store.root / "acknowledgments").glob("*.json")
            ).write_bytes(raw)
        with pytest.raises(IntegrityError, match="original/replay acceptance"):
            session.evidence.verify(evidence, work)
        assert session.repository.summary("001").results == 0
        assert (
            session.repository.connection.execute(
                "SELECT count(*) FROM usage_revisions"
            ).fetchone()[0]
            == 0
        )


@pytest.mark.parametrize("fault", ["noncanonical", "metadata"])
@pytest.mark.asyncio
async def test_rehashed_saved_replay_still_requires_canonical_bound_metadata(
    tmp_path: Path, fault: str
) -> None:
    from judgmentllm.persistence.evidence import DecisionsEvidenceReceiptV3
    from judgmentllm.persistence.hashing import bytes_digest, canonical_bytes
    from judgmentllm.persistence.models import IntegrityError

    async with production_boundary(tmp_path, filtered=True) as (
        session,
        work,
        response,
    ):
        raw_identity, receipt, evidence = session.evidence.prepare(work, response)
        assert isinstance(evidence, DecisionsEvidenceReceiptV3)
        raw = session.evidence.store.read(raw_identity)
        if fault == "noncanonical":
            raw += b"\n"
        else:
            data = json.loads(raw)
            data["metadata"]["unexpected_members"] += 1
            raw = canonical_bytes(data)
        retained = session.evidence.store.put(raw, "application/json")
        receipt = receipt.model_copy(update={"raw": retained})
        evidence = evidence.model_copy(
            update={
                "retained": retained,
                "response_receipt": receipt,
                "response_receipt_sha256": bytes_digest(canonical_bytes(receipt)),
            }
        )
        for directory, record in (("receipts", receipt), ("evidence", evidence)):
            encoded = canonical_bytes(record)
            session.evidence.store.put(encoded, "application/json")
            next((session.evidence.store.root / directory).glob("*.json")).write_bytes(
                encoded
            )
        with pytest.raises(IntegrityError, match="retained replay differs"):
            session.saved_response(work)
        assert session.repository.summary("001").results == 0
        assert (
            session.repository.connection.execute(
                "SELECT count(*) FROM usage_revisions"
            ).fetchone()[0]
            <= 1
        )


@pytest.mark.parametrize("fault", ["missing", "metadata", "bytes"])
@pytest.mark.asyncio
async def test_in_memory_replay_cannot_bypass_immutable_evidence(
    tmp_path: Path, fault: str
) -> None:
    from judgmentllm.decisions.transport import PreparedDecisionsRequestV2
    from judgmentllm.persistence.hashing import canonical_bytes
    from judgmentllm.persistence.models import IntegrityError
    from judgmentllm.providers.openai_decisions_response import analyze

    async with production_boundary(tmp_path, filtered=True) as (
        session,
        work,
        response,
    ):
        assert isinstance(work.prepared, PreparedDecisionsRequestV2)
        replay, _ = analyze(response.raw, work.prepared)
        if fault != "missing":
            session.evidence.prepare(work, response)
        if fault == "metadata":
            replay = replay.model_copy(
                update={
                    "metadata": replay.metadata.model_copy(
                        update={"unexpected_members": 2}
                    )
                }
            )
        raw = canonical_bytes(replay) + (b"\n" if fault == "bytes" else b"")
        supplied = response.model_copy(
            update={"raw": raw, "decisions_replay": replay, "evidence": None}
        )
        with pytest.raises(IntegrityError, match="matching immutable evidence"):
            session.process(work, supplied)
        assert session.repository.summary("001").results == 0
        assert (
            session.repository.connection.execute(
                "SELECT count(*) FROM usage_revisions"
            ).fetchone()[0]
            == 0
        )


@pytest.mark.parametrize("production", [False, True])
@pytest.mark.asyncio
async def test_registered_forwarding_protocol_cannot_be_relabelled(
    tmp_path: Path, production: bool
) -> None:
    from test_decisions_lifecycle import native_spec

    from judgmentllm.decisions.provenance import decisions_status, physical_rows
    from judgmentllm.decisions.transport import PreparedDecisionsRequestV1
    from judgmentllm.evidence.usage import ProviderUsage
    from judgmentllm.persistence.decisions_invocations import (
        DecisionsInvocationV1,
        DecisionsInvocationV2,
        publish_invocation,
    )
    from judgmentllm.persistence.models import IntegrityError
    from judgmentllm.preparation.manifest import ExecutableRunManifestV8
    from judgmentllm.providers.decisions_mock import MockDecisionsAdapter

    spec = (
        spec_for(tmp_path / "inputs")
        if production
        else native_spec(tmp_path / "inputs")
    )
    store = tmp_path / "store"
    with prepare_run(spec, store=store, run_id="001"):
        pass
    adapter = (
        OpenAIDecisionsAdapter(spec.model)
        if production
        else MockDecisionsAdapter(store / "mock-service.sqlite3")
    )
    session = ExecutionSession("001", store, adapter, spec, False, None)
    try:
        work = session.next_work()
        assert work is not None and session.boundary(work)
        session.uncertain(work, invoked=False)
        assert session.prepared is not None and isinstance(
            session.prepared.manifest, ExecutableRunManifestV8
        )
        assert isinstance(work.prepared, PreparedDecisionsRequestV1)
        identity = work.prepared.transport_commitment.identity
        fact_type = DecisionsInvocationV1 if production else DecisionsInvocationV2
        fact = fact_type(
            identity=identity,
            payload_sha256=work.prepared.payload_sha256,
            usage=ProviderUsage(
                run_id="001",
                request_uid=identity.request_uid,
                attempt_id=identity.attempt_id,
                submission_id=identity.submission_id,
                observation_id=None,
                provider="mock" if production else "openai",
                endpoint="synthetic" if production else "decisions",
                requested_model=spec.model,
                observation_kind="invocation",
            ),
        )
        artifact = publish_invocation(session.prepared.store, fact)
        session.repository.register_artifact("001", artifact)
        with pytest.raises(IntegrityError, match="protocol differs"):
            session.evidence.uncertain(work, known_invoked=True)
        with pytest.raises(IntegrityError, match="protocol differs"):
            decisions_status(session.repository, session.prepared.manifest)
        with pytest.raises(IntegrityError, match="protocol differs"):
            list(physical_rows(session.repository, session.prepared.manifest, 0))
        assert session.repository.summary("001").results == 0
        assert (
            session.repository.connection.execute(
                "SELECT count(*) FROM usage_revisions"
            ).fetchone()[0]
            == 0
        )
    finally:
        session.close()
        if isinstance(adapter, OpenAIDecisionsAdapter):
            await adapter.aclose()


@pytest.mark.asyncio
async def test_public_production_preflight_execution_and_mock_rejection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import judgmentllm.providers.openai_decisions as module
    from judgmentllm.lifecycle import LifecycleError, arun_live, validate_inputs
    from judgmentllm.lifecycle_models import MockExecutionOptions
    from judgmentllm.planning import validate_run

    spec = spec_for(tmp_path / "inputs")
    validation = validate_inputs(spec)
    assert validation.valid
    assert any(
        i.code == "unknown_context_limit" and i.severity == "warning"
        for i in validation.issues
    )
    strict = validate_run(spec.model_copy(update={"strict_capabilities": True}))
    assert any(
        i.code == "unknown_context_limit" and i.severity == "error"
        for i in strict.issues
    )
    forwarded: list[bytes] = []
    constructed: list[int] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        forwarded.append(request.content)
        return httpx2.Response(200, json=envelope(json.loads(request.content)))

    original = OpenAIDecisionsAdapter.__init__

    def offline_init(self: OpenAIDecisionsAdapter, model: str, **options: Any) -> None:
        constructed.append(1)
        original(self, model, http_transport=httpx2.MockTransport(respond), **options)

    monkeypatch.setattr(OpenAIDecisionsAdapter, "__init__", offline_init)
    result = await arun_live(spec, store=tmp_path / "public")
    assert result.successful_results == 3 and len(forwarded) == len(constructed) == 1

    def forbidden(*_args: Any) -> Any:
        raise AssertionError("Mock controls reached credentials")

    monkeypatch.setattr(module, "_resolve_credentials", forbidden)
    with pytest.raises(LifecycleError, match="mock controls"):
        await arun_live(spec, store=tmp_path / "rejected", mock=MockExecutionOptions())
    assert len(forwarded) == len(constructed) == 1
    assert not (tmp_path / "rejected").exists()


def test_explicit_production_context_must_match_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import judgmentllm.providers.openai_decisions as module
    from judgmentllm.decisions.records import OpenAIDecisionsContextV1
    from judgmentllm.preparation import PreparationError
    from judgmentllm.preparation.decisions import prepare_decisions_run

    def forbidden(*_args: Any) -> Any:
        raise AssertionError("Context mismatch reached credentials")

    monkeypatch.setattr(module, "_resolve_credentials", forbidden)
    spec = spec_for(tmp_path / "inputs")
    with pytest.raises(PreparationError, match="context differs"):
        prepare_decisions_run(
            spec,
            store=tmp_path / "store",
            run_id="001",
            context=OpenAIDecisionsContextV1(account_scope="foreign"),
        )
    with Repository(tmp_path / "store") as repo:
        assert repo.connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_public_resume_rejects_registry_manifest_family_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_decisions_lifecycle import native_spec

    import judgmentllm.lifecycle as lifecycle
    from judgmentllm.lifecycle import LifecycleError
    from judgmentllm.persistence.generation import build_generation_commitment
    from judgmentllm.persistence.hashing import canonical_bytes
    from judgmentllm.persistence.models import RunRecord
    from judgmentllm.preparation.manifest import ExecutableRunManifestV8

    spec = native_spec(tmp_path / "inputs")
    with prepare_run(spec, store=tmp_path / "synthetic", run_id="001") as prepared:
        data = prepared.manifest.model_dump(mode="json")
    production = spec_for(tmp_path / "production-inputs")
    data["generation"] = build_generation_commitment(production).model_dump(mode="json")
    wrong_family = ExecutableRunManifestV8.model_validate_json(json.dumps(data))
    raw = canonical_bytes(wrong_family)
    store = tmp_path / "store"
    with Repository(store) as repo:
        repo.create_run(
            RunRecord(
                run_id="001",
                task_kind="rubric",
                provider="openai",
                endpoint="decisions",
                model=production.model,
                capture="full",
            ),
            decisions_version=4,
        )
        artifacts = ArtifactStore(repo.run_directory("001"))
        identity = artifacts.put(raw, "application/json")
        repo.register_artifact("001", identity)
        artifacts.publish_manifest(identity)

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("Wrong manifest family reached adapter selection")

    monkeypatch.setattr(lifecycle, "_execution_adapter", forbidden)
    with pytest.raises(LifecycleError, match="manifest differs"):
        await aresume_run("001", store=store)
    with Repository(store) as repo:
        assert repo.get_run("001").provider == "openai"
        assert repo.get_run("001").manifest_sha256 is None
        assert repo.get_run("001").state == "created"
        assert (repo.run_directory("001") / "manifest.json").read_bytes() == raw
        assert not list(repo.iter_submissions("001"))


@pytest.mark.parametrize("production", [False, True])
def test_seal_fails_closed_if_parser_violates_its_typed_dispatch_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, production: bool
) -> None:
    from test_decisions_lifecycle import native_spec
    from test_decisions_preparation_boundaries import published_native
    from test_preparation_rendering import specification

    import judgmentllm.preparation.manifest as manifests
    from judgmentllm.persistence.hashing import bytes_digest, canonical_bytes
    from judgmentllm.persistence.models import IntegrityError, ManifestReference
    from judgmentllm.preparation.manifest import ExecutableRunManifestV8

    old = specification(tmp_path / "old-input", responses=1, traits=1)
    with prepare_run(old, store=tmp_path / "old-store", run_id="old") as historical:
        previous = historical.manifest
    assert manifests.parse_executable_manifest(canonical_bytes(previous)) == previous
    spec = (
        spec_for(tmp_path / "inputs")
        if production
        else native_spec(tmp_path / "inputs")
    )
    store = published_native(tmp_path, spec, monkeypatch)
    with Repository(store) as repo:
        raw = (repo.run_directory("001") / "manifest.json").read_bytes()
        checked = manifests.parse_executable_manifest(raw)
        assert isinstance(checked, ExecutableRunManifestV8)
        assert checked.schema_version == (9 if production else 8)
        reference = ManifestReference(
            artifact=repo.get_artifact("001", bytes_digest(raw))
        )

        def broken_parser(_raw: bytes) -> Any:
            # Parser-contract fault injection, not a claim that these bytes dispatch this way.
            return previous

        monkeypatch.setattr(manifests, "parse_executable_manifest", broken_parser)
        with pytest.raises(IntegrityError, match="protocol differs"):
            repo.seal_run("001", reference, decisions_spec=spec)
        assert repo.get_run("001").manifest_sha256 is None
        assert (
            repo.get_run("001").state == "created"
            and repo.decisions_version("001") == 4
        )
        assert not list(repo.iter_submissions("001"))
        assert (repo.run_directory("001") / "manifest.json").read_bytes() == raw
