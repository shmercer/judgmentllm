"""Offline SDK, admission, native answer, and precision contracts."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx2
import pytest
from gemini_offline import isolate
from test_decisions_contracts import pair_spec
from test_decisions_lifecycle import native_spec

from judgmentllm.config import RunSpecV4, validate_run_spec
from judgmentllm.decisions.transport import (
    PreparedDecisionsRequestV4,
    fits,
    plan_source,
    prepare_request,
    serialize_request,
)
from judgmentllm.execution import execute_run, recover_run
from judgmentllm.lifecycle import (
    LifecycleError,
    estimate_run,
    run_live,
    validate_inputs,
)
from judgmentllm.models import ResponseItem
from judgmentllm.persistence.artifacts import ArtifactStore
from judgmentllm.persistence.hashing import bytes_digest
from judgmentllm.persistence.receipts import read_acknowledgment
from judgmentllm.persistence.repository import Repository
from judgmentllm.preparation import prepare_run
from judgmentllm.providers import gemini_decisions as adapter_module
from judgmentllm.providers.gemini_decisions import (
    GeminiDecisionsAdapter,
    decisions_context,
)
from judgmentllm.providers.gemini_decisions_response import analyze
from judgmentllm.providers.protocol import SubmissionUncertain
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
    spec: RunSpecV4, *, text: str = "Synthetic café 📝"
) -> PreparedDecisionsRequestV4:
    context = decisions_context(spec)
    item = ResponseItem(item_id="001", text=text)
    dependency = next(plan_source(spec, item, lambda _: item, context, None))
    wire = prepare_request(
        spec,
        dependency,
        physical_identity("001", dependency.request.request_uid, "attempt-91"),
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
    forwards: list[bytes] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        assert (
            str(request.url)
            == "https://generativelanguage.googleapis.com/v1beta/decisions"
        )
        assert request.headers["x-goog-api-key"] == "SYNTHETIC_SELECTED_GEMINI_KEY"
        assert "authorization" not in request.headers
        forwards.append(request.content)
        return httpx2.Response(200, json=envelope(json.loads(request.content)))

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
        assert result.summary.results == 3 and len(forwards) == 1
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


@pytest.mark.parametrize("encoding", ["escaped", "nested", "unicode", "unrelated"])
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
