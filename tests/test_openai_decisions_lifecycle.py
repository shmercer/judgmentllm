"""Failure and privacy regressions assert outcomes independently of the parser."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter
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
from judgmentllm.providers.protocol import PreparedRequest, ProviderError


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
