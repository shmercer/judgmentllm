"""Production wire fixtures exercise the actual pinned SDK without networking."""

from __future__ import annotations

import asyncio
import json
import socket
from pathlib import Path
from typing import Any

import httpx2
import pytest
from test_decisions_lifecycle import native_spec

from judgmentllm.config import RunSpecV4
from judgmentllm.decisions.provenance import export_decisions_provenance
from judgmentllm.execution import execute_run, recover_run
from judgmentllm.persistence.repository import Repository
from judgmentllm.preparation import prepare_run
from judgmentllm.providers.openai_decisions import OpenAIDecisionsAdapter
from judgmentllm.results import RubricResult


def spec_for(root: Path, **options: Any) -> RunSpecV4:
    base = native_spec(root, **options).model_dump()
    return RunSpecV4.model_validate(
        {**base, "provider": "openai", "endpoint": "decisions", "model": "gpt-6-luna"}
    )


def envelope(body: dict[str, Any]) -> dict[str, Any]:
    return dict(
        model=body["model"],
        answers=[
            dict(
                name=q["name"],
                type="score",
                score=1.55,
                confidence=0.8,
                probabilities=[
                    dict(value=i, label=level["label"], probability=p)
                    for i, (level, p) in enumerate(
                        zip(q["levels"], (0.1, 0.25, 0.65), strict=True)
                    )
                ],
            )
            if q["type"] == "score"
            else dict(
                name=q["name"],
                type="choice",
                choice="essay_02",
                confidence=0.7,
                probabilities=[
                    dict(value="essay_01", probability=0.2),
                    dict(value="essay_02", probability=0.8),
                ],
            )
            for q in body["questions"]
        ],
        usage=dict(
            input_tokens=120,
            input_tokens_details=dict(cached_tokens=0, cache_write_tokens=0),
            output_tokens=4,
            output_tokens_details=dict(reasoning_tokens=0),
            total_tokens=124,
        ),
    )


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-decisions-key")

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("Unexpected provider network")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


@pytest.mark.parametrize("capture", ["full", "minimal"])
@pytest.mark.asyncio
async def test_sdk_lifecycle_and_versioned_provenance(
    tmp_path: Path, capture: str
) -> None:
    spec = spec_for(tmp_path, capture=capture)
    bodies: list[dict[str, Any]] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        bodies.append(body)
        assert set(body) == {"model", "input", "questions", "prompt_cache_options"}
        assert body["prompt_cache_options"] == {"mode": "explicit"}
        assert request.url.path == "/v1/decisions"
        assert len(body["questions"]) == 3
        return httpx2.Response(200, json=envelope(body))

    store = tmp_path / "store"
    with prepare_run(spec, store=store, run_id="001") as prepared:
        assert prepared.manifest.schema_version == 9
    adapter = OpenAIDecisionsAdapter(
        spec.model, http_transport=httpx2.MockTransport(respond)
    )
    try:
        await asyncio.wait_for(
            execute_run("001", store=store, spec=spec, adapter=adapter), timeout=15
        )
        await recover_run("001", store=store, spec=spec, adapter=adapter)
        with Repository(store) as repository:
            rows = [
                r for r in repository.iter_results("001") if isinstance(r, RubricResult)
            ]
            assert len(rows) == 3 and all(r.status == "complete" for r in rows)
            assert all(r.primary_score == pytest.approx(5.15) for r in rows)
            assert all(r.provider_expected_index == pytest.approx(1.55) for r in rows)
            assert all(
                r.confidence == 0.8
                and r.category_label is None
                and r.input_tokens is None
                for r in rows
            )
        bundle = export_decisions_provenance("001", store=store)
        assert bundle.provenance_report_version == 2
        physical = next(f for f in bundle.manifest.files if f.name == "physical.jsonl")
        root = next((store / "runs").iterdir())
        assert physical.artifact.relative_path is not None
        record = json.loads((root / physical.artifact.relative_path).read_text())
        assert record["physical_provenance_version"] == 2
        assert record["complete_response_evidence"]["acceptance_evidence_version"] == 2
        assert record["provider_remote_id"] is None
        assert record["selected_usage"]["usage_source"] == "openai.decisions.usage"
        assert record["selected_usage"]["input_tokens"] == 120
        assert len(bodies) == 1
    finally:
        await adapter.aclose()


@pytest.mark.parametrize(
    "failure", ["rejected", "rate", "redirect", "timeout", "truncated"]
)
@pytest.mark.asyncio
async def test_http_failures_never_gain_replay_authority(
    tmp_path: Path, failure: str
) -> None:
    spec = spec_for(tmp_path)
    calls = 0

    def respond(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        if failure == "timeout":
            raise httpx2.ReadTimeout("UNSAFE_PROVIDER_ERROR", request=request)
        if failure == "truncated":

            class Truncated(httpx2.AsyncByteStream):
                async def __aiter__(self):
                    yield b'{"answers":['
                    raise httpx2.ReadError("UNSAFE_PROVIDER_ERROR", request=request)

            return httpx2.Response(200, stream=Truncated())
        status = {"rejected": 400, "rate": 429, "redirect": 307}[failure]
        return httpx2.Response(
            status,
            content=b'{"secret":"UNSAFE_PROVIDER_ERROR"}',
            headers={"Location": "https://ambient.invalid"},
        )

    store = tmp_path / "store"
    with prepare_run(spec, store=store, run_id="001"):
        pass
    adapter = OpenAIDecisionsAdapter(
        spec.model, http_transport=httpx2.MockTransport(respond)
    )
    try:
        report = await asyncio.wait_for(
            execute_run("001", store=store, adapter=adapter), 15
        )
        await recover_run("001", store=store, adapter=adapter)
        with Repository(store) as repo:
            rows = list(repo.iter_results("001"))
            if failure in {"rejected", "rate"}:
                assert len(rows) == 3 and all(r.status == "errored" for r in rows)
            else:
                assert rows == [] and report.recovery_required
        assert calls == 1
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_sdk_hidden_second_forward_preserves_the_first_response(
    tmp_path: Path,
) -> None:
    spec = spec_for(tmp_path)
    calls = 0

    def respond(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        return httpx2.Response(200, json=envelope(json.loads(request.content)))

    store = tmp_path / "store"
    with prepare_run(spec, store=store, run_id="001"):
        pass
    adapter = OpenAIDecisionsAdapter(
        spec.model, http_transport=httpx2.MockTransport(respond)
    )
    try:
        client = await adapter._execution_client()  # pyright: ignore[reportPrivateUsage]
        original = client.decisions.with_raw_response.create

        async def duplicate(**kwargs: Any) -> Any:
            await original(**kwargs)
            return await original(**kwargs)

        client.decisions.with_raw_response.create = duplicate
        await asyncio.wait_for(execute_run("001", store=store, adapter=adapter), 15)
        with Repository(store) as repo:
            assert [r.status for r in repo.iter_results("001")] == ["complete"] * 3
        assert calls == adapter.actual_call_count == 1
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_actual_sdk_preserves_four_hundred_named_questions(
    tmp_path: Path,
) -> None:
    from judgmentllm.decisions.transport import (
        PreparedDecisionsRequestV2,
        plan_source,
        prepare_request,
    )
    from judgmentllm.models import ResponseItem
    from judgmentllm.providers.openai_decisions import decisions_context
    from judgmentllm.transport import physical_identity

    spec = spec_for(tmp_path, traits=400, maximum=400)
    context = decisions_context(spec)
    item = ResponseItem(item_id="000", text="Synthetic shared evidence.")
    dependency = next(plan_source(spec, item, lambda _: item, context, None))
    wire = prepare_request(
        spec,
        dependency,
        physical_identity("001", dependency.request.request_uid, "attempt-001"),
        context,
    )
    assert isinstance(wire, PreparedDecisionsRequestV2)
    observed: list[str] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        observed.extend(q["name"] for q in body["questions"])
        return httpx2.Response(200, json=envelope(body))

    adapter = OpenAIDecisionsAdapter(
        spec.model, http_transport=httpx2.MockTransport(respond)
    )
    identity = wire.transport_commitment.identity
    try:
        ack = await adapter.submit(
            wire,
            submission_id=identity.submission_id,
            correlation_id=identity.correlation_id,
        )
        assert ack.response_evidence.http_status == 200
        assert (
            observed == [q.key for q in dependency.questions] and len(observed) == 400
        )
    finally:
        await adapter.aclose()


@pytest.mark.parametrize("complete", [False, True])
@pytest.mark.asyncio
async def test_cancellation_preserves_receipt_authority_and_never_reforwards(
    tmp_path: Path, complete: bool
) -> None:
    spec = spec_for(tmp_path)
    entered = asyncio.Event()

    async def respond(request: httpx2.Request) -> httpx2.Response:
        if not complete:
            entered.set()
            await asyncio.Event().wait()
        return httpx2.Response(200, json=envelope(json.loads(request.content)))

    store = tmp_path / "store"
    with prepare_run(spec, store=store, run_id="001"):
        pass
    adapter = OpenAIDecisionsAdapter(
        spec.model, http_transport=httpx2.MockTransport(respond)
    )
    try:
        if complete:
            client = await adapter._execution_client()  # pyright: ignore[reportPrivateUsage]
            original = client.decisions.with_raw_response.create

            async def pause_after_receipt(**kwargs: Any) -> Any:
                await original(**kwargs)
                entered.set()
                await asyncio.Event().wait()

            client.decisions.with_raw_response.create = pause_after_receipt
        task = asyncio.create_task(execute_run("001", store=store, adapter=adapter))
        await asyncio.wait_for(entered.wait(), 10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 10)
        assert adapter.actual_call_count == 1
    finally:
        await adapter.aclose()

    def forbidden(_request: httpx2.Request) -> httpx2.Response:
        raise AssertionError("Recovery forwarded again")

    fresh = OpenAIDecisionsAdapter(
        spec.model, http_transport=httpx2.MockTransport(forbidden)
    )
    try:
        report = await recover_run("001", store=store, adapter=fresh)
        assert report.recovery_required is not complete
        assert fresh.actual_call_count == 0
        with Repository(store) as repo:
            rows = list(repo.iter_results("001"))
            assert [r.status for r in rows] == (["complete"] * 3 if complete else [])
            assert repo.connection.execute(
                "SELECT count(*) FROM usage_revisions"
            ).fetchone()[0] == (1 if complete else 0)
    finally:
        await fresh.aclose()
