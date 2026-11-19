"""Offline SDK, admission, native answer, and precision contracts."""

from __future__ import annotations

import asyncio
import json
from importlib.metadata import PackageNotFoundError
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx2
import pytest
from gemini_offline import isolate
from test_decisions_contracts import pair_spec
from test_decisions_lifecycle import native_spec
from test_decisions_validation import native_request

from judgmentllm.config import RunSpecV4, validate_run_spec
from judgmentllm.decisions.provenance import decisions_status
from judgmentllm.decisions.replay import (
    GeminiDecisionsReplayV1,
    TypeSafeDecisionsReplayV1,
)
from judgmentllm.decisions.transport import (
    PreparedDecisionsRequestV4,
    fits,
    plan_source,
    prepare_request,
    serialize_request,
)
from judgmentllm.execution import execute_run, recover_run
from judgmentllm.execution.retry import authorize_subset_retry
from judgmentllm.execution.session import ExecutionSession
from judgmentllm.lifecycle import (
    LifecycleError,
    aresume_run,
    arun_live,
    estimate_run,
    run_live,
    validate_inputs,
)
from judgmentllm.lifecycle_models import MockExecutionOptions
from judgmentllm.models import ResponseItem
from judgmentllm.persistence.artifacts import ArtifactStore
from judgmentllm.persistence.evidence import (
    DecisionsEvidenceReceiptV5,
    read_evidence_receipt,
)
from judgmentllm.persistence.hashing import bytes_digest, canonical_bytes
from judgmentllm.persistence.models import IntegrityError
from judgmentllm.persistence.receipts import (
    AcknowledgmentReceiptV5,
    read_acknowledgment,
)
from judgmentllm.persistence.repository import Repository
from judgmentllm.preparation import open_prepared_run, prepare_run
from judgmentllm.preparation.manifest import ExecutableRunManifestV11
from judgmentllm.providers import gemini_decisions as adapter_module
from judgmentllm.providers.gemini import GeminiReadinessError
from judgmentllm.providers.gemini_decisions import (
    GeminiDecisionsAdapter,
    decisions_context,
)
from judgmentllm.providers.gemini_decisions_response import analyze, rejected
from judgmentllm.providers.protocol import (
    GeminiDecisionsRequestRejected,
    ProviderAdapter,
    ProviderError,
    ProviderIntegrityError,
    ProviderInvocationCancelled,
    ProviderResponse,
    RetrievalUnavailable,
    SubmissionUncertain,
)
from judgmentllm.transport import physical_identity


def gemini_spec(base: RunSpecV4, **changes: Any) -> RunSpecV4:
    data = base.model_dump(exclude={"normalization"})
    return RunSpecV4.model_validate(
        {
            **data,
            "provider": "gemini",
            "endpoint": "decisions",
            "model": "gemini-decision-1",
            **changes,
        }
    )


def wire_for(
    spec: RunSpecV4, *, text: str = "Synthetic café 📝", attempt_id: str = "attempt-91"
) -> PreparedDecisionsRequestV4:
    context = decisions_context(spec)
    item = ResponseItem(item_id="001", text=text)
    dependency = next(plan_source(spec, item, lambda _: item, context, None))
    wire = prepare_request(
        spec,
        dependency,
        physical_identity("001", dependency.request.request_uid, attempt_id),
        context,
    )
    assert isinstance(wire, PreparedDecisionsRequestV4)
    return wire


def answer(question: dict[str, Any]) -> dict[str, Any]:
    if question["type"] == "choice":
        return dict(
            type="choice",
            choice="essay_02",
            confidence=0.8,
            probabilities={"essay_02": 0.8, "essay_01": 0.2},
        )
    return dict(
        type="score",
        score=1.55,
        confidence=0.65,
        probabilities={"2": 0.65, "0": 0.1, "1": 0.25},
        legend={str(i): text for i, text in enumerate(question["criteria"])},
    )


def envelope(body: dict[str, Any]) -> dict[str, Any]:
    return dict(
        answers={
            key: answer(value)
            for key, value in reversed(tuple(body["questions"].items()))
        },
        usage=dict(
            total_input_tokens=120,
            total_output_tokens=0,
            total_tokens=120,
            input_tokens_by_modality=[dict(modality="text", tokens=120)],
        ),
    )


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    isolate(monkeypatch)
    monkeypatch.setenv("GOOGLE_GENAI_API_VERSION", "v1")
    monkeypatch.setenv("GOOGLE_GENAI_HTTP_CLIENT", "httpx")
    monkeypatch.setenv("GOOGLE_GENAI_DEBUG", "1")


@pytest.mark.asyncio
async def test_sdk_exact_bytes_and_grouped_usage(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    spec = gemini_spec(native_spec(tmp_path, traits=3, maximum=8))
    assert spec.rubric is not None
    description = "Directory C:" + "\\" * 32 + "files"
    spec.rubric.traits[0].levels[0].description = description
    forwards: list[bytes] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        assert (
            str(request.url)
            == "https://generativelanguage.googleapis.com/v1beta/decisions"
        )
        assert request.headers["x-goog-api-key"] == "SYNTHETIC_SELECTED_GEMINI_KEY"
        assert "authorization" not in request.headers
        forwards.append(request.content)
        body = envelope(json.loads(request.content))
        assert any(
            description in score["legend"]["0"] for score in body["answers"].values()
        )
        return httpx2.Response(200, json=body)

    store = tmp_path / "store"
    with prepare_run(spec, store=store, run_id="001") as prepared:
        assert prepared.manifest.schema_version == 11
    adapter = GeminiDecisionsAdapter(
        spec.model, http_transport=httpx2.MockTransport(respond)
    )
    try:
        with caplog.at_level("DEBUG"):
            result = await asyncio.wait_for(
                execute_run("001", store=store, adapter=adapter), timeout=20
            )
        assert not result.recovery_required and result.summary.results == 3
        assert len(forwards) == adapter.actual_call_count == 1
        assert "SYNTHETIC_SELECTED_GEMINI_KEY" not in caplog.text
        with Repository(store) as repository:
            rows = list(repository.iter_results("001"))
            assert all(
                row.status == "complete"
                and row.task_kind == "rubric"
                and row.primary_score == pytest.approx(5.15)
                and getattr(row, "provider_expected_index", None) == 1.55
                and row.confidence == 0.65
                and row.input_tokens is None
                for row in rows
            )
            (receipt,) = repository.iter_evidence("001")
            assert isinstance(receipt, DecisionsEvidenceReceiptV5)
            assert receipt.retained_kind == "original"
            assert (
                read_acknowledgment(
                    ArtifactStore(repository.run_directory("001")),
                    receipt.identity.submission_id,
                )
                is not None
            )
            assert (
                receipt.usage.input_tokens,
                receipt.usage.output_tokens,
                receipt.usage.reported_model,
            ) == (120, 0, None)
            assert receipt.remote_id is None
        await recover_run("001", store=store, adapter=adapter)
        assert len(forwards) == 1

        wire = wire_for(spec)
        identity = wire.transport_commitment.identity
        await adapter.submit(
            wire,
            submission_id=identity.submission_id,
            correlation_id=identity.correlation_id,
        )
        assert forwards[-1] == wire.payload and b"caf\\u00e9" in wire.payload
    finally:
        await adapter.aclose()


@pytest.mark.parametrize(
    "change",
    [
        {"strict_capabilities": True},
        {"model": "other"},
        {"normalization": {"probability_sum_tolerance": 1e-5}},
    ],
)
def test_invalid_scope_precedes_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: dict[str, Any]
) -> None:
    def deny(*_args: object, **_kwargs: object) -> None:
        pytest.fail("inadmissible scope reached credentials")

    monkeypatch.setattr(adapter_module, "_resolve_credentials", deny)
    spec = gemini_spec(native_spec(tmp_path), **change)
    with pytest.raises(LifecycleError, match="Gemini Decisions"):
        run_live(spec, store=tmp_path / "store")
    assert not (tmp_path / "store").exists()


def test_body_cap_and_multi_question_plan(tmp_path: Path) -> None:
    spec = gemini_spec(native_spec(tmp_path, traits=12, maximum=12))
    assert validate_inputs(spec).valid
    planned = estimate_run(spec).model_dump()
    assert planned["requests"] == 1 and planned["logical_tasks"] == 12
    assert planned["limits"]["context_fit"] == "unverified"
    assert planned["limits"]["max_context_tokens"] == 262_144
    assert 0 < planned["max_serialized_body_bytes"] < 262_144
    context = decisions_context(spec)
    assert fits(b"x" * 262_144, 1000, context, strict=False)
    assert not fits(b"x" * 262_145, 1000, context, strict=False)
    assert not fits(b"x", 1001, context, strict=False)
    assert not fits(b"x", 1, context, strict=True)
    with pytest.raises(ValueError, match="local cap is 262144 bytes"):
        wire_for(spec, text="x" * 262_144)
    shared = ResponseItem(item_id="001", text="x")
    (original,) = plan_source(spec, shared, lambda _: shared, context, None)
    widest_pair = max(
        len(
            serialize_request(shared.text, original.questions[i : i + 2], spec, context)
        )
        for i in range(0, len(original.questions), 2)
    )
    shared = ResponseItem(item_id="001", text="x" * (262_144 - widest_pair + 1))
    groups = list(plan_source(spec, shared, lambda _: shared, context, None))
    assert [len(g.questions) for g in groups] == [2] * 6
    assert all(g.shared_input == shared.text for g in groups)
    assert [q.key for g in groups for q in g.questions] == [
        q.key for q in original.questions
    ]
    assert (
        max(
            len(serialize_request(shared.text, group.questions, spec, context))
            for group in groups
        )
        == 262_144
    )


def test_explicit_zero_survives_typed_and_mapping_ingress(tmp_path: Path) -> None:
    spec = gemini_spec(native_spec(tmp_path, traits=1))
    normalization = {"probability_sum_tolerance": 0.0}
    assert decisions_context(validate_run_spec(spec)).probability_sum_tolerance == 1e-6
    for supplied in (
        spec.model_copy(update={"normalization": normalization}),
        spec.model_dump(exclude={"normalization"}) | {"normalization": normalization},
    ):
        checked = validate_run_spec(supplied)
        assert isinstance(checked, RunSpecV4)
        assert wire_for(checked).transport_commitment.probability_sum_tolerance == 0


@pytest.mark.parametrize(
    "raw",
    [
        b'{"answers":{},"answers":{}}',
        b'{"answers":{"known":{"score":NaN}}}',
        b'\xff{"answers":{}}',
    ],
)
def test_envelope_stays_strict_before_numeric_member_validation(
    tmp_path: Path, raw: bytes
) -> None:
    wire = wire_for(gemini_spec(native_spec(tmp_path, traits=2)))
    replay, safe, usage, _ = analyze(raw, wire)
    assert not safe and usage is None
    assert replay.metadata.findings == ("malformed_json",)
    assert replay.answers == (None, None)


@pytest.mark.parametrize(
    "count,maximum,sizes",
    [(1000, 1001, [1000]), (1001, 1001, [1000, 1]), (4, 3, [3, 1])],
)
def test_gemini_endpoint_and_user_grouping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    count: int,
    maximum: int,
    sizes: list[int],
) -> None:
    from judgmentllm.decisions import transport

    def short_question(*_args: object, **_kwargs: object) -> str:
        return "Score."

    monkeypatch.setattr(transport, "render_question", short_question)
    spec = gemini_spec(native_spec(tmp_path, traits=count, maximum=maximum))
    context = decisions_context(spec)
    item = ResponseItem(item_id="001", text="Synthetic.")
    groups = list(plan_source(spec, item, lambda _: item, context, None))
    assert [len(g.questions) for g in groups] == sizes
    assert len({q.key for g in groups for q in g.questions}) == count


def test_thirteen_criteria_preserve_presentation_groups(tmp_path: Path) -> None:
    spec = gemini_spec(pair_spec(tmp_path, criteria=13))
    with prepare_run(spec, store=tmp_path / "store", run_id="001") as prepared:
        groups = list(prepared.iter_request_dependencies())
    assert [len(g.request.tasks) for g in groups] == [13] * 4
    assert len({task.task_id for g in groups for task in g.request.tasks}) == 52


@pytest.mark.parametrize("levels", [2, 10, 11, 255, 256])
def test_score_level_bounds(tmp_path: Path, levels: int) -> None:
    base = native_spec(tmp_path, traits=1)
    rubric = base.model_dump()["rubric"]
    rubric["traits"][0]["levels"] = [dict(label=str(i)) for i in range(levels)]
    spec = gemini_spec(base, rubric=rubric)
    if levels == 256:
        with pytest.raises(ValueError, match="2–255"):
            wire_for(spec)
    else:
        wire = wire_for(spec)
        (question,) = json.loads(wire.payload)["questions"].values()
        assert question["criteria"] == [str(i) for i in range(levels)]


@pytest.mark.parametrize("kind", ["oversized", "credential_echo", "ambiguous_400"])
@pytest.mark.asyncio
async def test_guarded_unsafe_body_keeps_transport_uncertain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    spec = gemini_spec(native_spec(tmp_path, traits=1))
    wire = wire_for(spec)
    if kind == "oversized":
        monkeypatch.setattr(adapter_module, "_RESPONSE_LIMIT", 128)

    def respond(_request: httpx2.Request) -> httpx2.Response:
        if kind == "oversized":
            return httpx2.Response(400, content=b"x" * 129)
        if kind == "credential_echo":
            return httpx2.Response(
                200, json={"answers": {}, "detail": "SYNTHETIC_SELECTED_GEMINI_KEY"}
            )
        return httpx2.Response(400, json={"answers": {}, "error": {"code": "safety"}})

    adapter = GeminiDecisionsAdapter(
        spec.model, http_transport=httpx2.MockTransport(respond)
    )
    identity = wire.transport_commitment.identity
    try:
        with pytest.raises(SubmissionUncertain) as failure:
            await adapter.submit(
                wire,
                submission_id=identity.submission_id,
                correlation_id=identity.correlation_id,
            )
        assert failure.value.invoked and adapter.actual_call_count == 1
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_gemini_conflicting_or_missing_rejection_keeps_provider_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = gemini_spec(native_spec(tmp_path, traits=1))
    store = tmp_path / "store"
    with prepare_run(spec, store=store, run_id="001"):
        pass
    adapter = GeminiDecisionsAdapter(
        spec.model,
        http_transport=httpx2.MockTransport(lambda request: httpx2.Response(413)),
    )
    session = ExecutionSession("001", store, adapter, spec, False, None)
    try:
        work = session.next_work()
        assert work is not None and session.boundary(work)
        with pytest.raises(GeminiDecisionsRequestRejected) as rejected:
            await adapter.submit(
                work.prepared,
                submission_id=work.submission_id,
                correlation_id=work.correlation_id,
            )
        rejection = rejected.value.rejection
        session.not_submitted(work, invoked=True, rejection=rejection)
        different = rejection.model_copy(update={"response_sha256": "f" * 64})
        with pytest.raises(
            IntegrityError, match="Gemini Decisions rejection evidence conflicts"
        ):
            session.not_submitted(work, invoked=True, rejection=different)
        assert session.prepared is not None
        manifest = session.prepared.manifest
        assert isinstance(manifest, ExecutableRunManifestV11)
        assert adapter.actual_call_count == 1
    finally:
        session.close()
        await adapter.aclose()

    original = Repository.get_evidence_for_submission

    def missing(repository: Repository, run_id: str, submission_id: str) -> None:
        assert (run_id, submission_id) == ("001", rejection.submission_id)
        assert original(repository, run_id, submission_id) == receipt

    with Repository(store) as repository:
        (receipt,) = repository.iter_evidence("001")
        assert (
            isinstance(receipt, DecisionsEvidenceReceiptV5)
            and receipt.rejection == rejection
        )
        assert repository.usage_high_water_mark("001") == 1
        assert [row.status for row in repository.iter_results("001")] == ["errored"]
        with monkeypatch.context() as unavailable:
            unavailable.setattr(Repository, "get_evidence_for_submission", missing)
            with pytest.raises(
                IntegrityError,
                match="Gemini Decisions rejection lacks its evidence receipt",
            ):
                decisions_status(repository, manifest)


@pytest.mark.asyncio
async def test_gemini_refused_member_cannot_retry_with_unbound_adapter(
    tmp_path: Path,
) -> None:
    spec = gemini_spec(native_spec(tmp_path, traits=1))
    store = tmp_path / "store"
    with prepare_run(spec, store=store, run_id="001"):
        pass
    adapter = GeminiDecisionsAdapter(
        spec.model,
        http_transport=httpx2.MockTransport(
            lambda request: httpx2.Response(
                200, json={"answers": {}, "prompt_feedback": {"block_reason": "safety"}}
            )
        ),
    )
    try:
        await execute_run("001", store=store, adapter=adapter)
        with Repository(store) as repository:
            (row,) = repository.iter_results("001")
            assert row.status == "refused"

        def forbidden() -> None:
            pytest.fail("unbound Gemini adapter reached credential validation")

        claimed = cast(
            ProviderAdapter,
            SimpleNamespace(
                transport_context=adapter.transport_context,
                capabilities=adapter.capabilities,
                validate=adapter.validate,
                validate_credentials=forbidden,
            ),
        )
        with pytest.raises(
            IntegrityError,
            match="Gemini Decisions retry lacks its sealed request or adapter",
        ):
            authorize_subset_retry(
                "001",
                store=store,
                adapter=claimed,
                task_ids=(row.task_id,),
                operation_id="refused",
            )
        assert adapter.actual_call_count == 1
        with Repository(store) as repository:
            summary = repository.summary("001")
            assert summary.attempts == 1 and summary.retry_operations == 0
            assert repository.usage_high_water_mark("001") == 1
    finally:
        await adapter.aclose()


@pytest.mark.parametrize(
    "encoding", ["escaped", "nested", "unicode", "unrelated", "deep"]
)
@pytest.mark.asyncio
async def test_malformed_credential_echo_has_no_acknowledgment_or_original_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, encoding: str
) -> None:
    credential = 'SYNTHETIC_"\\KEY'
    monkeypatch.setenv("GOOGLE_API_KEY", credential)
    encoded = json.dumps(credential)
    if encoding == "nested":
        encoded = json.dumps(encoded)
    elif encoding == "unicode":
        encoded = r'"SYNTHETIC_\u0022\u005cKEY"'
    elif encoding == "unrelated":
        encoded = json.dumps('SYNTHETIC_"\\OTHER')
    elif encoding == "deep":
        encoded = (
            '"'
            + "".join("\\" + "u005c" * 4 + f"u{ord(char):04x}" for char in credential)
            + '"'
        )
    raw = b'\xff{"detail":' + encoded.encode() + b"}"
    echoed = encoding != "unrelated"
    forwards = 0

    def respond(request: httpx2.Request) -> httpx2.Response:
        nonlocal forwards
        assert request.headers["x-goog-api-key"] == credential
        forwards += 1
        return httpx2.Response(200, content=raw)

    spec = gemini_spec(native_spec(tmp_path, traits=1))
    store = tmp_path / "store"
    with prepare_run(spec, store=store, run_id="001"):
        pass
    adapter = GeminiDecisionsAdapter(
        spec.model, http_transport=httpx2.MockTransport(respond)
    )
    try:
        await execute_run("001", store=store, adapter=adapter)
        report = await recover_run("001", store=store, adapter=adapter)
        assert report.recovery_required is echoed
        assert forwards == adapter.actual_call_count == 1
    finally:
        await adapter.aclose()
    with Repository(store) as repository:
        (submission,) = repository.iter_submissions("001")
        ack = read_acknowledgment(
            ArtifactStore(repository.run_directory("001")), submission.submission_id
        )
        if not echoed:
            assert ack is not None
            assert [row.status for row in repository.iter_results("001")] == ["invalid"]
            return
        assert submission.state == "uncertain" and ack is None
        assert list(repository.iter_evidence("001")) == []
        assert repository.usage_high_water_mark("001") == 0
    original_hash = bytes_digest(raw).encode()
    for path in store.rglob("*"):
        if path.is_file():
            assert path.name.encode() != original_hash
            data = path.read_bytes()
            assert all(
                value not in data
                for value in (original_hash, credential.encode(), encoded.encode(), raw)
            )


@pytest.mark.parametrize(
    "number",
    ["2", "2e0", "negative-wide"],
    ids=["integer", "exponent", "oversized-negative"],
)
def test_integer_decoding_preserves_ordinary_numeric_scores(
    tmp_path: Path, number: str
) -> None:
    wire = wire_for(gemini_spec(native_spec(tmp_path, traits=1)))
    body = envelope(json.loads(wire.payload))
    item = next(iter(body["answers"].values()))
    item.update(
        score="number_marker", confidence=1, probabilities={"0": 0, "1": 0, "2": 1}
    )
    token = "-" + "9" * 4301 if number == "negative-wide" else number
    raw = json.dumps(body).replace('"number_marker"', token).encode()
    replay, safe, usage, _ = analyze(raw, wire)
    assert replay.metadata.recognizable and usage is not None
    assert (usage.input_tokens, usage.output_tokens) == (120, 0)
    (observed,) = replay.answers
    if number == "negative-wide":
        assert not safe and observed is None
    else:
        assert safe and observed is not None and observed["expected_index"] == 2


@pytest.mark.parametrize(
    "tolerance,mass,index,confidence,valid",
    [
        (None, 5e-7, 1.5500019, 0.6500009, True),
        (None, 0.01, 1.55, 0.65, False),
        (None, 0, 1.5500021, 0.65, False),
        (None, 0, 1.55, 0.6500011, False),
        (0, 5e-7, 1.55, 0.65, False),
        (0, 0, 1.5500019, 0.65, True),
    ],
)
def test_score_precision(
    tmp_path: Path,
    tolerance: float | None,
    mass: float,
    index: float,
    confidence: float,
    valid: bool,
) -> None:
    base = native_spec(tmp_path, traits=1)
    spec = (
        gemini_spec(base)
        if tolerance is None
        else gemini_spec(base, normalization={"probability_sum_tolerance": tolerance})
    )
    wire = wire_for(spec)
    response = envelope(json.loads(wire.payload))
    (entry,) = response["answers"].values()
    entry["probabilities"]["0"] += mass
    entry.update(score=index, confidence=confidence)
    replay, safe, _, _ = analyze(json.dumps(response).encode(), wire)
    (observed,) = replay.answers
    assert safe is valid and (observed is not None) is valid
    if observed is not None:
        assert observed["expected_index"] == index
        assert observed["confidence"] == confidence


@pytest.mark.parametrize("status,outcome", [(200, "refused"), (400, "errored")])
@pytest.mark.asyncio
async def test_refusal_cancellation_after_complete_bytes(
    tmp_path: Path, status: int, outcome: str
) -> None:
    spec = gemini_spec(native_spec(tmp_path))
    store = tmp_path / "store"
    with prepare_run(spec, store=store, run_id="001"):
        pass
    complete = asyncio.Event()
    body = json.dumps(
        {"answers": {}, "prompt_feedback": {"block_reason": "safety"}}
        if status == 200
        else {"error": {"code": "safety", "message": "UNSAFE_REFUSAL"}}
    ).encode()

    class PauseOnClose(httpx2.AsyncByteStream):
        async def __aiter__(self):  # type: ignore[override]
            yield body

        async def aclose(self) -> None:
            complete.set()
            await asyncio.Event().wait()

    def respond(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(status, stream=PauseOnClose())

    adapter = GeminiDecisionsAdapter(
        spec.model, http_transport=httpx2.MockTransport(respond)
    )
    try:
        task = asyncio.create_task(execute_run("001", store=store, adapter=adapter))
        await asyncio.wait_for(complete.wait(), 10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 10)
        assert adapter.actual_call_count == 1
    finally:
        await adapter.aclose()

    def forbidden(_request: httpx2.Request) -> httpx2.Response:
        pytest.fail("reopened refusal forwarded again")

    fresh = GeminiDecisionsAdapter(
        spec.model, http_transport=httpx2.MockTransport(forbidden)
    )
    try:
        await recover_run("001", store=store, adapter=fresh)
        with Repository(store) as repository:
            assert [r.status for r in repository.iter_results("001")] == [outcome] * 3
            assert repository.usage_high_water_mark("001") == 1
        assert fresh.actual_call_count == 0
    finally:
        await fresh.aclose()


@pytest.mark.parametrize(
    "chosen,p_first,p_second,confidence,expected",
    [
        ("essay_01", 0.5, 0.5, 0.5, True),
        ("essay_02", 0.5, 0.5, 0.5, True),
        ("essay_02", 0.5000004, 0.4999996, 0.5, True),
        ("essay_02", 0.5000004, 0.4999996, 0.5000013, False),
        ("essay_02", 0.500002, 0.499998, 0.5, False),
        ("essay_01", 0.2, 0.8, 0.8, False),
        (None, 0.5, 0.5, 0.5, False),
    ],
)
def test_choice_precision_and_returned_selection(
    tmp_path: Path,
    chosen: str | None,
    p_first: float,
    p_second: float,
    confidence: float,
    expected: bool,
) -> None:
    spec = gemini_spec(pair_spec(tmp_path, criteria=1))
    with prepare_run(spec, store=tmp_path / "store", run_id="001") as prepared:
        dependency = next(prepared.iter_request_dependencies())
        attempt = prepared.repository.get_attempt_for_request(
            "001", dependency.request.request_uid
        )
        assert attempt is not None
        wire = prepared.prepare_transport_attempt(
            dependency,
            physical_identity(
                "001", dependency.request.request_uid, attempt.attempt_id
            ),
        )
    assert isinstance(wire, PreparedDecisionsRequestV4)
    body = json.loads(wire.payload)
    key = next(iter(body["questions"]))
    response = envelope(body)
    entry = response["answers"][key]
    entry.update(
        probabilities={"essay_02": p_second, "essay_01": p_first}, confidence=confidence
    )
    if chosen is None:
        entry.pop("choice")
    else:
        entry["choice"] = chosen
    replay, _, _, _ = analyze(json.dumps(response).encode(), wire)
    (observed,) = replay.answers
    assert (observed is not None) is expected
    if observed is not None:
        assert observed["selection"] == chosen


def invocation(wire: PreparedDecisionsRequestV4) -> dict[str, str]:
    identity = wire.transport_commitment.identity
    return dict(
        submission_id=identity.submission_id, correlation_id=identity.correlation_id
    )


def test_gemini_adapter_rejects_unsealed_entrypoints_and_mismatched_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(_provider: str) -> Any:
        pytest.fail("adapter resolved credentials during preflight")

    monkeypatch.setattr(adapter_module, "_resolve_credentials", forbidden)
    spec = gemini_spec(native_spec(tmp_path), account_scope="sealed-scope")
    adapter = GeminiDecisionsAdapter(spec.model)
    with pytest.raises(ValueError, match="concurrency must be positive"):
        GeminiDecisionsAdapter(spec.model, concurrency=0)
    with pytest.raises(
        ProviderError, match="Unsupported Gemini Decisions route or model"
    ):
        adapter.capabilities_for("gemini", "models.generate_content", spec.model)
    with pytest.raises(ProviderIntegrityError, match="runtime scope differs"):
        adapter.validate(spec)
    with pytest.raises(ProviderError, match="sealed preparation"):
        adapter.prepare()
    with pytest.raises(ProviderError, match="named-member"):
        adapter.parse()
    assert adapter.actual_call_count == 0


@pytest.mark.parametrize(
    "problem", ["missing_extra", "sdk_scope_mismatch", "missing_credentials"]
)
@pytest.mark.asyncio
async def test_gemini_readiness_distinguishes_packaging_scope_and_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, problem: str
) -> None:
    from judgmentllm.providers import gemini

    def missing(_package: str) -> str:
        raise PackageNotFoundError("missing SDK fixture")

    def historical(_package: str) -> str:
        return "2.34.0"

    def forbidden(_provider: str) -> Any:
        pytest.fail("an incompatible SDK reached credential resolution")

    if problem == "missing_extra":
        monkeypatch.setattr(gemini, "version", missing)
    elif problem == "sdk_scope_mismatch":
        monkeypatch.setattr(gemini, "version", historical)
    if problem == "missing_credentials":
        monkeypatch.delenv("GOOGLE_API_KEY")
        monkeypatch.delenv("GEMINI_API_KEY")
    else:
        monkeypatch.setattr(adapter_module, "_resolve_credentials", forbidden)

    spec = gemini_spec(native_spec(tmp_path))
    wire = wire_for(spec)
    adapter = GeminiDecisionsAdapter(spec.model)
    with pytest.raises(GeminiReadinessError) as failure:
        await adapter.submit(wire, **invocation(wire))
    assert failure.value.diagnostic_code == problem
    assert "missing SDK fixture" not in str(failure.value)
    assert adapter.actual_call_count == 0
    with pytest.raises(RetrievalUnavailable):
        await adapter.retrieve(wire, **invocation(wire))


@pytest.mark.asyncio
async def test_gemini_handoff_is_bounded_and_consumed_by_exact_invocation(
    tmp_path: Path,
) -> None:
    spec = gemini_spec(native_spec(tmp_path / "gemini"))
    first, second = wire_for(spec), wire_for(spec, attempt_id="attempt-92")
    _, _, synthetic = native_request(tmp_path / "synthetic")
    forwarded: list[bytes] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        forwarded.append(request.content)
        return httpx2.Response(200, json=envelope(json.loads(request.content)))

    adapter = GeminiDecisionsAdapter(
        spec.model, concurrency=1, http_transport=httpx2.MockTransport(respond)
    )
    try:
        with pytest.raises(ProviderIntegrityError, match="production preparation"):
            await adapter.submit(synthetic, **invocation(first))
        with pytest.raises(ProviderIntegrityError, match="invocation differs"):
            await adapter.submit(
                first, **{**invocation(first), "correlation_id": "foreign"}
            )
        assert forwarded == []
        await adapter.submit(first, **invocation(first))
        with pytest.raises(ProviderIntegrityError, match="handoff is full"):
            await adapter.submit(second, **invocation(second))
        response = await adapter.retrieve(first, **invocation(first))
        assert len(adapter.parse_decisions(response, first).answers) == 3
        with pytest.raises(ProviderIntegrityError, match="invocation differs"):
            await adapter.submit(first, **invocation(first))
        await adapter.submit(second, **invocation(second))
        with pytest.raises(ProviderIntegrityError, match="retrieval identity differs"):
            await adapter.retrieve(
                second, **{**invocation(second), "correlation_id": "foreign"}
            )
        with pytest.raises(RetrievalUnavailable):
            await adapter.retrieve(second, **invocation(second))
        assert forwarded == [first.payload, second.payload]
        assert adapter.actual_call_count == len(forwarded)
    finally:
        await adapter.aclose()


@pytest.mark.parametrize("boundary", ["request", "response"])
@pytest.mark.asyncio
async def test_guarded_sdk_drift_preserves_only_already_captured_responses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    spec = gemini_spec(native_spec(tmp_path, traits=1))
    wire = wire_for(spec)
    forwards: list[bytes] = []
    create = adapter_module._protected_client  # pyright: ignore[reportPrivateUsage]

    async def redirect(request: httpx2.Request) -> None:
        request.url = request.url.copy_with(path="/v1beta/foreign")

    async def fail_after_capture(_response: httpx2.Response) -> None:
        raise ProviderIntegrityError("SDK response hook failed")

    def with_hook(
        sdk: Any, http: Any, *, http_client: httpx2.AsyncClient, **kwargs: Any
    ) -> Any:
        if boundary == "request":
            http_client.event_hooks["request"].append(redirect)
        else:
            http_client.event_hooks["response"].append(fail_after_capture)
        return create(sdk, http, http_client=http_client, **kwargs)

    def respond(request: httpx2.Request) -> httpx2.Response:
        forwards.append(request.content)
        return httpx2.Response(200, json=envelope(json.loads(request.content)))

    monkeypatch.setattr(adapter_module, "_protected_client", with_hook)
    adapter = GeminiDecisionsAdapter(
        spec.model, http_transport=httpx2.MockTransport(respond)
    )
    try:
        if boundary == "request":
            with pytest.raises(ProviderIntegrityError, match="forwarding differs"):
                await adapter.submit(wire, **invocation(wire))
            with pytest.raises(RetrievalUnavailable):
                await adapter.retrieve(wire, **invocation(wire))
            assert forwards == [] and adapter.actual_call_count == 0
        else:
            ack = await adapter.submit(wire, **invocation(wire))
            response = await adapter.retrieve(wire, **invocation(wire))
            assert ack.response_evidence.response_sha256 == bytes_digest(response.raw)
            assert len(adapter.parse_decisions(response, wire).answers) == 1
            assert forwards == [wire.payload] and adapter.actual_call_count == 1
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_gemini_sdk_initialization_failure_closes_owned_transport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from google import genai

    def fail_to_construct(**_kwargs: Any) -> Any:
        raise ValueError("UNSAFE_CONSTRUCTOR_DETAIL")

    def forbidden(_request: httpx2.Request) -> httpx2.Response:
        pytest.fail("invalid client forwarded an HTTP request")

    closed: list[bool] = []

    class TrackedTransport(httpx2.MockTransport):
        async def aclose(self) -> None:
            closed.append(True)
            await super().aclose()

    spec = gemini_spec(native_spec(tmp_path, traits=1))
    wire = wire_for(spec)
    monkeypatch.setattr(genai, "Client", fail_to_construct)
    adapter = GeminiDecisionsAdapter(
        spec.model, http_transport=TrackedTransport(forbidden)
    )
    try:
        with pytest.raises(
            ProviderIntegrityError, match="client initialization failed"
        ) as failure:
            await adapter.submit(wire, **invocation(wire))
        assert "UNSAFE_CONSTRUCTOR_DETAIL" not in str(failure.value)
        assert closed == [True] and adapter.actual_call_count == 0
        with pytest.raises(RetrievalUnavailable):
            await adapter.retrieve(wire, **invocation(wire))
    finally:
        await adapter.aclose()
    assert closed == [True]


@pytest.mark.parametrize("boundary", ["encoding", "length", "filtered_retention"])
@pytest.mark.asyncio
async def test_gemini_guard_does_not_acknowledge_unverifiable_or_unretained_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    spec = gemini_spec(native_spec(tmp_path, traits=1))
    wire = wire_for(spec)
    document = envelope(json.loads(wire.payload))
    headers: dict[str, str] = {}
    if boundary == "encoding":
        headers["content-encoding"] = "gzip"
    elif boundary == "filtered_retention":
        document["private"] = "UNSAFE_RETENTION_CANARY"
    raw = json.dumps(document).encode()
    if boundary == "length":
        headers["content-length"] = str(len(raw) + 1)
    elif boundary == "filtered_retention":
        replay, safe, _, _ = analyze(raw, wire)
        assert not safe and len(raw) < len(canonical_bytes(replay))
        monkeypatch.setattr(adapter_module, "_RESPONSE_LIMIT", len(raw))

    received: list[httpx2.Response] = []

    def respond(_request: httpx2.Request) -> httpx2.Response:
        response = httpx2.Response(200, stream=httpx2.ByteStream(raw), headers=headers)
        received.append(response)
        return response

    adapter = GeminiDecisionsAdapter(
        spec.model, http_transport=httpx2.MockTransport(respond)
    )
    try:
        with pytest.raises(SubmissionUncertain) as failure:
            await adapter.submit(wire, **invocation(wire))
        assert failure.value.invoked and adapter.actual_call_count == 1
        (response,) = received
        assert response.is_closed
        with pytest.raises(RetrievalUnavailable):
            await adapter.retrieve(wire, **invocation(wire))
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_gemini_cancellation_before_eof_has_no_acknowledgment(
    tmp_path: Path,
) -> None:
    spec = gemini_spec(native_spec(tmp_path, traits=1))
    wire = wire_for(spec)
    streaming, closed = asyncio.Event(), asyncio.Event()

    class Partial(httpx2.AsyncByteStream):
        async def __aiter__(self):  # type: ignore[override]
            yield b'{"answers":{'
            streaming.set()
            await asyncio.Event().wait()

        async def aclose(self) -> None:
            closed.set()

    adapter = GeminiDecisionsAdapter(
        spec.model,
        http_transport=httpx2.MockTransport(
            lambda _: httpx2.Response(200, stream=Partial())
        ),
    )
    task = asyncio.create_task(adapter.submit(wire, **invocation(wire)))
    try:
        await asyncio.wait_for(streaming.wait(), 10)
        task.cancel()
        with pytest.raises(ProviderInvocationCancelled) as failure:
            await asyncio.wait_for(task, 10)
        assert failure.value.invoked and closed.is_set()
        with pytest.raises(RetrievalUnavailable):
            await adapter.retrieve(wire, **invocation(wire))
        assert adapter.actual_call_count == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await adapter.aclose()


@pytest.mark.parametrize(
    "fault", ["scalar", "wrong_type", "extra_score_field", "legend", "missing"]
)
def test_malformed_named_score_does_not_invalidate_its_sibling(
    tmp_path: Path, fault: str
) -> None:
    wire = wire_for(gemini_spec(native_spec(tmp_path, traits=2)))
    body = json.loads(wire.payload)
    good, bad = body["questions"]
    document = envelope(body)
    answers = document["answers"]
    if fault == "scalar":
        answers[bad] = 1
    elif fault == "wrong_type":
        answers[bad]["type"] = "choice"
    elif fault == "extra_score_field":
        answers[bad]["private"] = "UNSAFE_MEMBER_CANARY"
    elif fault == "legend":
        answers[bad]["legend"]["0"] = "Foreign level"
    else:
        del answers[bad]

    replay, safe, usage, kind = analyze(json.dumps(document).encode(), wire)
    assert kind == "response" and replay.metadata.recognizable
    accepted, invalid = replay.answers
    assert accepted is not None and accepted["name"] == good
    assert accepted["expected_index"] == 1.55 and invalid is None
    assert [(m.key, m.failure) for m in replay.metadata.members] == [
        (good, None),
        (bad, "missing_output" if fault == "missing" else "invalid_output"),
    ]
    assert safe == (fault == "missing")
    assert b"UNSAFE_MEMBER_CANARY" not in canonical_bytes(replay)
    assert usage is not None and usage.input_tokens == 120


@pytest.mark.parametrize(
    "field,value,known_input,known_output,modality",
    [
        ("usage", ["UNSAFE_USAGE_CANARY"], None, None, False),
        ("total_input_tokens", "UNSAFE_USAGE_CANARY", None, 0, True),
        (
            "input_tokens_by_modality",
            [{"modality": "text", "tokens": 121}],
            120,
            0,
            False,
        ),
        (
            "input_tokens_by_modality",
            [{"modality": "text", "tokens": 120}, {}],
            120,
            0,
            False,
        ),
        ("input_tokens_by_modality", None, 120, 0, False),
    ],
)
def test_usage_filter_keeps_only_known_physical_facts(
    tmp_path: Path,
    field: str,
    value: object,
    known_input: int | None,
    known_output: int | None,
    modality: bool,
) -> None:
    wire = wire_for(gemini_spec(native_spec(tmp_path, traits=1)))
    document = envelope(json.loads(wire.payload))
    if field == "usage":
        document["usage"] = value
    else:
        document["usage"][field] = value
    replay, safe, usage, _ = analyze(json.dumps(document).encode(), wire)
    assert replay.answers[0] is not None
    if value is None:
        assert safe and replay.metadata.findings == ()
    else:
        assert not safe and replay.metadata.findings == ("unknown_field",)
    assert b"UNSAFE_USAGE_CANARY" not in canonical_bytes(replay)
    if known_output is None:
        assert usage is None
    else:
        assert usage is not None
        assert (usage.input_tokens, usage.output_tokens, usage.total_tokens) == (
            known_input,
            known_output,
            120,
        )
        assert (
            any(
                counter.field == "input_tokens_by_modality.text"
                for counter in usage.breakdown
            )
            == modality
        )


def test_recognizable_json_without_answers_cannot_claim_named_outcomes(
    tmp_path: Path,
) -> None:
    wire = wire_for(gemini_spec(native_spec(tmp_path, traits=1)))
    raw = b'{"usage":{"total_input_tokens":12}}'
    replay, safe, usage, kind = analyze(raw, wire)
    assert kind == "response" and not safe and not replay.metadata.recognizable
    assert replay.metadata.findings == ("unrecognized_envelope",)
    assert replay.answers == (None,)
    assert replay.metadata.members[0].failure == "invalid_output"
    assert usage is not None and usage.input_tokens == 12


@pytest.mark.parametrize(
    "status,error,reason",
    [
        (
            400,
            {"message": "The input exceeds the context window."},
            "max_tokens_exceeded",
        ),
        (
            400,
            {
                "message": "Input token count (262,145) exceeds the maximum number of tokens allowed (262,144)."
            },
            "max_tokens_exceeded",
        ),
        (
            400,
            {
                "code": "INVALID_ARGUMENT",
                "message": "Input token count (262,143) exceeds the maximum number of tokens allowed (262,144).",
            },
            "request_rejected",
        ),
        (
            400,
            {"code": 400, "message": "UNSAFE_DETAIL mentions context window"},
            "request_rejected",
        ),
        (400, {"status": "INVALID_ARGUMENT"}, "request_rejected"),
        (400, {"message": "UNSAFE_DETAIL mentions context window"}, None),
        (400, "UNSAFE_DETAIL", None),
        (403, {"code": 500, "status": "INTERNAL"}, None),
        (403, {"code": "unrecognized"}, None),
        (403, {"code": "PERMISSION_DENIED"}, "request_rejected"),
    ],
)
def test_rejection_requires_conclusive_status_and_context_grammar(
    tmp_path: Path, status: int, error: object, reason: str | None
) -> None:
    wire = wire_for(gemini_spec(native_spec(tmp_path, traits=1)))
    raw = json.dumps({"error": error}).encode()
    result = rejected(status, raw, wire)
    if reason is None:
        assert result is None
    else:
        assert result is not None
        assert (
            result.rejection.reason == reason and result.rejection.http_status == status
        )
        assert result.rejection.response_sha256 == bytes_digest(raw)
        assert set(result.rejection.model_dump()).isdisjoint(
            {"message", "error", "code"}
        )
        assert "UNSAFE_DETAIL" not in str(result)


def test_gemini_replay_binds_protocol_outcome_and_canonical_named_choices(
    tmp_path: Path,
) -> None:
    spec = gemini_spec(pair_spec(tmp_path, criteria=2))
    with prepare_run(spec, store=tmp_path / "store", run_id="001") as prepared:
        dependency = next(prepared.iter_request_dependencies())
        attempt = prepared.repository.get_attempt_for_request(
            "001", dependency.request.request_uid
        )
        assert attempt is not None
        wire = prepared.prepare_transport_attempt(
            dependency,
            physical_identity(
                "001", dependency.request.request_uid, attempt.attempt_id
            ),
        )
    assert isinstance(wire, PreparedDecisionsRequestV4)
    document = envelope(json.loads(wire.payload))
    raw = json.dumps(document).encode()
    replay, _, _, kind = analyze(raw, wire)
    adapter = GeminiDecisionsAdapter(spec.model)
    response = ProviderResponse(
        observation_key="synthetic-response", kind=kind, raw=raw
    )
    assert adapter.parse_decisions(response, wire).answers == replay.answers
    with pytest.raises(ProviderIntegrityError, match="outcome differs"):
        adapter.parse_decisions(response.model_copy(update={"kind": "refusal"}), wire)

    def from_replay(
        candidate: GeminiDecisionsReplayV1 | TypeSafeDecisionsReplayV1,
    ) -> ProviderResponse:
        return ProviderResponse(
            observation_key=response.observation_key,
            kind=kind,
            raw=b"",
            decisions_replay=candidate,
        )

    foreign = TypeSafeDecisionsReplayV1(
        metadata=replay.metadata, answers=replay.answers
    )
    with pytest.raises(ProviderIntegrityError, match="replay protocol differs"):
        adapter.parse_decisions(from_replay(foreign), wire)
    for fault, message in (
        ("invocation", "committed invocation"),
        ("name", "replay member differs"),
        ("order", "incomplete choice distribution"),
    ):
        data = replay.model_dump(mode="json")
        if fault == "invocation":
            data["metadata"]["correlation_id"] = "foreign"
        elif fault == "name":
            data["answers"][0]["name"] = data["answers"][1]["name"]
        else:
            data["answers"][0]["probabilities"].reverse()
        candidate = GeminiDecisionsReplayV1.model_validate_json(json.dumps(data))
        with pytest.raises(
            ValueError if fault == "order" else ProviderIntegrityError, match=message
        ):
            adapter.parse_decisions(from_replay(candidate), wire)
    assert adapter.parse_decisions(from_replay(replay), wire).answers == replay.answers


@pytest.mark.asyncio
async def test_public_gemini_run_resumes_filtered_evidence_with_sealed_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = gemini_spec(
        native_spec(tmp_path),
        account_scope="sealed-account",
        normalization={"probability_sum_tolerance": 0.0},
    )
    store = tmp_path / "store"
    forwards: list[bytes] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/v1beta/decisions"
        forwards.append(request.content)
        document = envelope(json.loads(request.content))
        _, invalid, _ = document["answers"].values()
        invalid["private"] = "UNSAFE_REPLAY_CANARY"
        return httpx2.Response(200, json=document)

    def intercept(**_options: Any) -> httpx2.MockTransport:
        return httpx2.MockTransport(respond)

    monkeypatch.setattr(httpx2, "AsyncHTTPTransport", intercept)
    with pytest.raises(LifecycleError, match="does not accept mock controls") as denied:
        await arun_live(spec, store=store, mock=MockExecutionOptions())
    assert denied.value.category == "validation" and denied.value.run_id is None
    assert forwards == []

    checkpoint = ExecutionSession.checkpoint

    def interrupt(session: ExecutionSession, stage: str, request_uid: str = "") -> None:
        if stage == "after_evidence_receipt":
            raise RuntimeError("synthetic interruption after durable evidence")
        checkpoint(session, stage, request_uid)

    with monkeypatch.context() as fault:
        fault.setattr(ExecutionSession, "checkpoint", interrupt)
        with pytest.raises(LifecycleError) as failure:
            await arun_live(spec, store=store)
    run_id = failure.value.run_id
    assert failure.value.category == "execution" and run_id is not None
    with open_prepared_run(run_id, store=store) as reopened:
        manifest = reopened.manifest
        assert isinstance(manifest, ExecutableRunManifestV11)
        assert manifest.production_context.account_scope == "sealed-account"
        assert manifest.production_context.probability_sum_tolerance == 0
        (submission,) = reopened.repository.iter_submissions(run_id)
        persisted = read_evidence_receipt(reopened.store, submission.submission_id)
        assert isinstance(persisted, DecisionsEvidenceReceiptV5)
        assert persisted.retained_kind == "filtered_replay"
        assert list(reopened.repository.iter_results(run_id)) == []

    monkeypatch.delenv("GOOGLE_API_KEY")
    monkeypatch.delenv("GEMINI_API_KEY")
    summary = await aresume_run(run_id, store=store)
    assert summary.outcome == "partial"
    assert summary.decisions is not None and summary.decisions.known_invocations == 1
    assert len(forwards) == 1
    with Repository(store) as repository:
        assert [row.status for row in repository.iter_results(run_id)] == [
            "complete",
            "invalid",
            "complete",
        ]
        assert repository.usage_high_water_mark(run_id) == 1
        assert list(repository.iter_evidence(run_id)) == [persisted]
        assert persisted.retained is not None
        artifacts = ArtifactStore(repository.run_directory(run_id), create=False)
        assert b"UNSAFE_REPLAY_CANARY" not in artifacts.read(persisted.retained)


def test_initial_gemini_intent_requires_the_concrete_adapter(tmp_path: Path) -> None:
    spec = gemini_spec(native_spec(tmp_path))
    store = tmp_path / "store"
    with prepare_run(spec, store=store, run_id="001"):
        pass
    adapter = GeminiDecisionsAdapter(spec.model)

    def forbidden() -> None:
        pytest.fail("unbound adapter reached credentials")

    claimed = cast(
        ProviderAdapter,
        SimpleNamespace(
            transport_context=adapter.transport_context,
            capabilities=adapter.capabilities,
            validate=adapter.validate,
            validate_credentials=forbidden,
        ),
    )
    session = ExecutionSession("001", store, claimed, spec, False, None)
    try:
        with pytest.raises(
            IntegrityError, match="Gemini Decisions request lacks its execution adapter"
        ):
            session.next_work()
    finally:
        session.close()
    with Repository(store) as repository:
        assert repository.summary("001").attempts == 1
        assert list(repository.iter_submissions("001")) == []
        assert list(repository.iter_results("001")) == []
        assert repository.usage_high_water_mark("001") == 0


def test_gemini_durable_records_require_exact_versions_and_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = gemini_spec(native_spec(tmp_path, traits=1))
    store = tmp_path / "store"

    def respond(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json=envelope(json.loads(request.content)))

    def intercept(**_options: Any) -> httpx2.MockTransport:
        return httpx2.MockTransport(respond)

    monkeypatch.setattr(httpx2, "AsyncHTTPTransport", intercept)
    summary = run_live(spec, store=store)
    assert summary.outcome == "ok"
    with open_prepared_run(summary.run_id, store=store) as prepared:
        manifest = prepared.manifest
        assert isinstance(manifest, ExecutableRunManifestV11)
        (dep,) = prepared.iter_request_dependencies()
        (submission,) = prepared.repository.iter_submissions(summary.run_id)
        assert submission.attempt_id is not None
        wire = prepared.prepare_transport_attempt(
            dep,
            physical_identity(
                summary.run_id, dep.request.request_uid, submission.attempt_id
            ),
        )
        receipt = read_evidence_receipt(prepared.store, submission.submission_id)
        ack = read_acknowledgment(prepared.store, submission.submission_id)
    assert isinstance(wire, PreparedDecisionsRequestV4)
    assert isinstance(receipt, DecisionsEvidenceReceiptV5)
    assert isinstance(ack, AcknowledgmentReceiptV5)
    rejection = rejected(400, b'{"error":{"code":"safety"}}', wire)
    assert rejection is not None
    for record, field, invalid, message in (
        (manifest, "schema_version", "11", "schema_version must be integer 11"),
        (manifest, "strict_capabilities", True, "manifest exceeds approved scope"),
        (wire, "preparation_version", "11", "preparation_version must be integer 11"),
        (
            ack,
            "acknowledgment_version",
            "5",
            "unsupported Gemini Decisions acknowledgment version",
        ),
        (
            ack,
            "response_evidence",
            {**ack.response_evidence.model_dump(), "acceptance_evidence_version": "4"},
            "unsupported Gemini Decisions acceptance evidence version",
        ),
        (
            receipt,
            "evidence_receipt_version",
            "5",
            r"Invalid evidence record\. \[type=evidence_invalid\]",
        ),
        (
            receipt.usage,
            "endpoint",
            "models.generate_content",
            r"Invalid evidence record\. \[type=evidence_invalid\]",
        ),
        (
            rejection.rejection,
            "http_status",
            401,
            "rejection reason conflicts with HTTP status",
        ),
    ):
        assert type(record).model_validate(record.model_dump()) == record
        with pytest.raises(ValueError, match=message):
            type(record).model_validate({**record.model_dump(), field: invalid})
