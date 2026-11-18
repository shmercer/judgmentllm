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
from judgmentllm.decisions.transport import PreparedDecisionsRequestV2
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
    "explicit,legacy,mass,delta,expected",
    [
        (None, False, 5e-7, 8e-6, "complete"),
        (None, False, 1.1e-6, 0, "invalid"),
        (None, False, 0, 1.1e-5, "invalid"),
        (1e-8, False, 5e-7, 0, "invalid"),
        (0, False, 0, 1e-9, "invalid"),
        ("model_copy_zero", False, 5e-7, 0, "invalid"),
        (None, True, 5e-7, 0, "invalid"),
    ],
)
@pytest.mark.asyncio
async def test_sealed_openai_precision_through_replay_and_retry(
    tmp_path: Path,
    explicit: float | str | None,
    legacy: bool,
    mass: float,
    delta: float,
    expected: str,
) -> None:
    from judgmentllm.decisions.records import OpenAIDecisionsContextV1
    from judgmentllm.execution.retry import authorize_subset_retry
    from judgmentllm.preparation.decisions import prepare_decisions_run

    data = spec_for(tmp_path, traits=1).model_dump(exclude={"normalization"})
    if (explicit is not None and explicit != "model_copy_zero") or legacy:
        # The historical writer materialized 1e-8 without a policy tag.
        data["normalization"] = {
            "probability_sum_tolerance": 1e-8 if legacy else explicit
        }
    spec = RunSpecV4.model_validate(data)
    if explicit == "model_copy_zero":
        spec = spec.model_copy(
            update={"normalization": {"probability_sum_tolerance": 0.0}}
        )
    context = OpenAIDecisionsContextV1() if legacy else None
    store = tmp_path / "store"
    with prepare_decisions_run(
        spec, store=store, run_id="001", context=context
    ) as prepared:
        assert prepared.manifest.schema_version == 9
        assert prepared.manifest.production_context.precision_policy_version == (
            1 if explicit is None and not legacy else None
        )
        frozen = prepared.manifest.production_context

    def respond(request: httpx2.Request) -> httpx2.Response:
        body = envelope(json.loads(request.content))
        entry = body["answers"][0]
        entry["probabilities"][0]["probability"] += mass
        entry["score"] += delta
        # Force filtered replay without changing the recognized member.
        body["unrecognized"] = "UNSAFE_PRECISION_TEST"
        return httpx2.Response(200, json=body)

    def fault(stage: str, _request: str) -> None:
        if stage == "after_evidence_receipt":
            raise RuntimeError("stop before normalization")

    for first in (True, False):
        adapter = OpenAIDecisionsAdapter(
            spec.model, context=frozen, http_transport=httpx2.MockTransport(respond)
        )
        try:
            if first:
                with pytest.raises(ExceptionGroup, match="unhandled errors"):
                    await execute_run("001", store=store, adapter=adapter, fault=fault)
            else:
                await recover_run("001", store=store, adapter=adapter)
                assert adapter.actual_call_count == 0
                with Repository(store) as repository:
                    (row,) = repository.iter_results("001")
                    assert row.status == expected
                    if expected == "complete":
                        assert isinstance(row, RubricResult)
                        assert row.probabilities is not None
                        assert row.probabilities[0].probability == 0.1 + mass
                        assert row.provider_expected_index == 1.55 + delta
                if legacy:
                    authorize_subset_retry(
                        "001",
                        store=store,
                        adapter=adapter,
                        task_ids=(row.task_id,),
                        operation_id="retry-001",
                    )
                    await recover_run("001", store=store, adapter=adapter)
                    with Repository(store) as repository:
                        assert [r.status for r in repository.iter_results("001")] == [
                            "invalid",
                            "invalid",
                        ]
                    assert adapter.actual_call_count == 1
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


def production_wire(
    root: Path, *, pairwise: bool = False, attempt_id: str | None = None
) -> tuple[RunSpecV4, PreparedDecisionsRequestV2]:
    from test_decisions_contracts import pair_spec

    from judgmentllm.decisions.transport import prepare_request
    from judgmentllm.preparation.decisions import DecisionsPreparedRun
    from judgmentllm.transport import physical_identity

    spec = (
        RunSpecV4.model_validate(
            {
                **pair_spec(root).model_dump(),
                "provider": "openai",
                "endpoint": "decisions",
                "model": "gpt-6-luna",
            }
        )
        if pairwise
        else spec_for(root)
    )
    with prepare_run(spec, store=root / "store", run_id="001") as prepared:
        assert isinstance(prepared, DecisionsPreparedRun)
        dependency = next(prepared.iter_request_dependencies())
        first = prepared.repository.get_attempt_for_request(
            "001", dependency.request.request_uid, attempt_number=1
        )
        assert first is not None
        identity = physical_identity(
            "001", dependency.request.request_uid, attempt_id or first.attempt_id
        )
        wire = (
            prepare_request(
                spec,
                dependency,
                identity,
                prepared.context(),
                parent=prepared.transport_for_attempt(first.attempt_id),
            )
            if attempt_id is not None
            else prepared.prepare_transport_attempt(dependency, identity)
        )
    assert isinstance(wire, PreparedDecisionsRequestV2)
    return spec, wire


@pytest.mark.parametrize("scope", ["model", "account_scope"])
def test_adapter_scope_and_legacy_entrypoints_never_resolve_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scope: str
) -> None:
    import judgmentllm.providers.openai_decisions as module
    from judgmentllm.providers.protocol import ProviderError, ProviderIntegrityError

    def forbidden(*_args: Any) -> Any:
        raise AssertionError("Preflight resolved credentials")

    monkeypatch.setattr(module, "_resolve_credentials", forbidden)
    spec = spec_for(tmp_path)
    adapter = OpenAIDecisionsAdapter(spec.model)
    changed = RunSpecV4.model_validate({**spec.model_dump(), scope: "foreign"})
    with pytest.raises(ProviderIntegrityError, match="scope"):
        adapter.validate(changed)
    with pytest.raises(ProviderError):
        module.decisions_context(native_spec(tmp_path / "synthetic"))
    with pytest.raises(ProviderError, match="route"):
        adapter.capabilities_for("mock", "synthetic", "unlisted-model")
    with pytest.raises(ProviderError, match="sealed preparation"):
        adapter.prepare()
    with pytest.raises(ProviderError, match="named-member"):
        adapter.parse()
    assert adapter.actual_call_count == 0


def test_missing_credential_resolution_has_only_the_readiness_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import judgmentllm.providers.openai_decisions as module
    from judgmentllm.providers.openai import OpenAIReadinessError

    def missing(_provider: str) -> Any:
        raise ValueError("UNSAFE_RESOLVER_CANARY")

    monkeypatch.setattr(module, "_resolve_credentials", missing)
    adapter = OpenAIDecisionsAdapter(spec_for(tmp_path).model)
    with pytest.raises(OpenAIReadinessError) as caught:
        adapter.validate_credentials()
    assert caught.value.diagnostic_code == "missing_credentials"
    assert "UNSAFE_RESOLVER_CANARY" not in str(caught.value)
    assert adapter.actual_call_count == 0


@pytest.mark.asyncio
async def test_submission_identity_and_handoff_capacity_do_not_lose_responses(
    tmp_path: Path,
) -> None:
    from test_decisions_validation import native_request

    from judgmentllm.providers.protocol import (
        ProviderIntegrityError,
        RetrievalUnavailable,
    )

    spec, first = production_wire(tmp_path / "first")
    _, second = production_wire(tmp_path / "second", attempt_id="second-attempt")
    _, _, synthetic = native_request(tmp_path / "synthetic")
    forwarded: list[bytes] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        forwarded.append(request.content)
        return httpx2.Response(200, json=envelope(json.loads(request.content)))

    adapter = OpenAIDecisionsAdapter(
        spec.model, concurrency=1, http_transport=httpx2.MockTransport(respond)
    )
    one, two = first.transport_commitment.identity, second.transport_commitment.identity
    try:
        with pytest.raises(ProviderIntegrityError, match="production preparation"):
            await adapter.submit(
                synthetic, submission_id="foreign", correlation_id="foreign"
            )
        with pytest.raises(ProviderIntegrityError, match="identity"):
            await adapter.submit(
                first, submission_id=one.submission_id, correlation_id="foreign"
            )
        assert forwarded == []
        await adapter.submit(
            first, submission_id=one.submission_id, correlation_id=one.correlation_id
        )
        with pytest.raises(ProviderIntegrityError, match="handoff"):
            await adapter.submit(
                second,
                submission_id=two.submission_id,
                correlation_id=two.correlation_id,
            )
        assert forwarded == [first.payload]
        response = await adapter.retrieve(
            first, submission_id=one.submission_id, correlation_id=one.correlation_id
        )
        assert len(adapter.parse_decisions(response, first).answers) == 3
        # Capacity is now available; the completed identity still cannot forward twice.
        with pytest.raises(ProviderIntegrityError):
            await adapter.submit(
                first,
                submission_id=one.submission_id,
                correlation_id=one.correlation_id,
            )
        await adapter.submit(
            second, submission_id=two.submission_id, correlation_id=two.correlation_id
        )
        with pytest.raises(ProviderIntegrityError, match="retrieval identity"):
            await adapter.retrieve(
                second, submission_id=two.submission_id, correlation_id="foreign"
            )
        with pytest.raises(RetrievalUnavailable):
            await adapter.retrieve(
                second,
                submission_id=two.submission_id,
                correlation_id=two.correlation_id,
            )
        assert forwarded == [first.payload, second.payload]
        assert adapter.actual_call_count == 2
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_sdk_wrong_route_is_rejected_before_forwarding(tmp_path: Path) -> None:
    from judgmentllm.providers.protocol import ProviderIntegrityError

    spec, wire = production_wire(tmp_path)

    def forbidden(_request: httpx2.Request) -> httpx2.Response:
        raise AssertionError("Wrong route reached HTTP receiver")

    adapter = OpenAIDecisionsAdapter(
        spec.model, http_transport=httpx2.MockTransport(forbidden)
    )
    identity = wire.transport_commitment.identity
    try:
        client = await adapter._execution_client()  # pyright: ignore[reportPrivateUsage]

        async def wrong_route(**_kwargs: Any) -> Any:
            return await client._client.post(
                "https://api.openai.com/v1/responses", content=wire.payload
            )

        client.decisions.with_raw_response.create = wrong_route
        with pytest.raises(ProviderIntegrityError, match="forwarding"):
            await adapter.submit(
                wire,
                submission_id=identity.submission_id,
                correlation_id=identity.correlation_id,
            )
        assert adapter.actual_call_count == 0
    finally:
        await adapter.aclose()


@pytest.mark.parametrize("boundary", ["response_stream", "retained_serializer"])
@pytest.mark.asyncio
async def test_body_limits_do_not_publish_an_oversized_handoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    import judgmentllm.providers.openai_decisions as module
    from judgmentllm.decisions.replay import OpenAIDecisionsReplayV1
    from judgmentllm.providers.protocol import RetrievalUnavailable, SubmissionUncertain

    spec, wire = production_wire(tmp_path)
    received: list[httpx2.Response] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        if boundary == "response_stream":

            class Oversized(httpx2.AsyncByteStream):
                async def __aiter__(self):
                    yield b"x" * 8_388_608
                    yield b"x"

            response = httpx2.Response(200, stream=Oversized())
        else:
            doc = envelope(json.loads(request.content))
            doc["private"] = "UNSAFE_BOUNDARY_CANARY"
            response = httpx2.Response(200, json=doc)
        received.append(response)
        return response

    if boundary == "retained_serializer":
        original = module.canonical_bytes

        def oversized(value: object) -> bytes:
            # Serializer-contract fault injection; the real size guard is unchanged.
            return (
                b"x" * 8_388_609
                if isinstance(value, OpenAIDecisionsReplayV1)
                else original(value)
            )

        monkeypatch.setattr(module, "canonical_bytes", oversized)
    adapter = OpenAIDecisionsAdapter(
        spec.model, http_transport=httpx2.MockTransport(respond)
    )
    identity = wire.transport_commitment.identity
    try:
        with pytest.raises(SubmissionUncertain) as caught:
            await adapter.submit(
                wire,
                submission_id=identity.submission_id,
                correlation_id=identity.correlation_id,
            )
        assert caught.value.invoked is True
        assert adapter.actual_call_count == 1 and received[0].is_closed
        with pytest.raises(RetrievalUnavailable):
            await adapter.retrieve(
                wire,
                submission_id=identity.submission_id,
                correlation_id=identity.correlation_id,
            )
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_non_exception_after_receipt_propagates_and_restores_context(
    tmp_path: Path,
) -> None:
    import judgmentllm.providers.openai_decisions as module
    from judgmentllm.providers.protocol import RetrievalUnavailable

    class StopAfterReceipt(BaseException):
        pass

    spec, wire = production_wire(tmp_path)
    adapter = OpenAIDecisionsAdapter(
        spec.model,
        http_transport=httpx2.MockTransport(
            lambda r: httpx2.Response(200, json=envelope(json.loads(r.content)))
        ),
    )
    identity = wire.transport_commitment.identity
    try:
        client = await adapter._execution_client()  # pyright: ignore[reportPrivateUsage]
        original = client.decisions.with_raw_response.create

        async def stop(**kwargs: Any) -> Any:
            await original(**kwargs)
            raise StopAfterReceipt()

        client.decisions.with_raw_response.create = stop
        with pytest.raises(StopAfterReceipt):
            await adapter.submit(
                wire,
                submission_id=identity.submission_id,
                correlation_id=identity.correlation_id,
            )
        assert module._ACTIVE.get() is None  # pyright: ignore[reportPrivateUsage]
        assert module._SDK_LOGGING.get() is False  # pyright: ignore[reportPrivateUsage]
        assert adapter.actual_call_count == 1
        with pytest.raises(RetrievalUnavailable):
            await adapter.retrieve(
                wire,
                submission_id=identity.submission_id,
                correlation_id=identity.correlation_id,
            )
    finally:
        await adapter.aclose()


@pytest.mark.parametrize(
    "fault",
    [
        "score_not_list",
        "score_count",
        "score_mean",
        "choice_not_list",
        "choice_count",
        "choice_entry",
        "choice_mapping",
        "unrecognized",
        "nonobject",
        "nameless",
        "model",
    ],
)
def test_wire_faults_preserve_content_free_findings_and_named_siblings(
    tmp_path: Path, fault: str
) -> None:
    from judgmentllm.providers.openai_decisions_response import analyze, verify_replay
    from judgmentllm.providers.protocol import ProviderIntegrityError

    _, wire = production_wire(tmp_path, pairwise=fault.startswith("choice"))
    doc = envelope(json.loads(wire.payload))
    answer = doc["answers"][0]
    if fault.endswith("not_list"):
        answer["probabilities"] = {}
    elif fault.endswith("count"):
        answer["probabilities"].pop()
    elif fault == "score_mean":
        answer["score"] = 0.25
    elif fault == "choice_entry":
        answer["probabilities"][0] = None
    elif fault == "choice_mapping":
        answer["probabilities"][0]["value"] = "foreign"
    elif fault == "unrecognized":
        doc["answers"] = {}
    elif fault in {"nonobject", "nameless"}:
        doc["answers"].insert(1, None if fault == "nonobject" else {"type": "refusal"})
    else:
        doc["model"] = "different-model"
    replay, safe = analyze(json.dumps(doc).encode(), wire)
    assert not safe
    if fault in {"nonobject", "nameless"}:
        assert replay.metadata.unattributable_members == 1
        assert [m.answer_ordinals for m in replay.metadata.members] == [
            (0,),
            (2,),
            (3,),
        ]
        assert all(m.failure is None for m in replay.metadata.members)
    elif fault in {"unrecognized", "model"}:
        assert not replay.metadata.recognizable
        assert all(m.failure == "invalid_output" for m in replay.metadata.members)
        assert (
            "model_conflict" if fault == "model" else "unrecognized_envelope"
        ) in replay.metadata.findings
    else:
        assert [m.failure for m in replay.metadata.members] == [
            "invalid_output",
            None,
            None,
        ]
        assert replay.answers[0] is None and all(
            a is not None for a in replay.answers[1:]
        )
    if fault == "model":
        with pytest.raises(ProviderIntegrityError, match="reported model"):
            verify_replay(replay, wire)
    else:
        verify_replay(replay, wire)


@pytest.mark.parametrize("fault", ["identity", "member_order", "answer_name"])
def test_replay_cannot_rebind_an_invocation_or_named_answer(
    tmp_path: Path, fault: str
) -> None:
    from judgmentllm.decisions.replay import OpenAIDecisionsReplayV1
    from judgmentllm.providers.openai_decisions_response import analyze, verify_replay
    from judgmentllm.providers.protocol import ProviderIntegrityError

    _, wire = production_wire(tmp_path)
    original, _ = analyze(json.dumps(envelope(json.loads(wire.payload))).encode(), wire)
    data = original.model_dump(mode="json")
    if fault == "identity":
        data["metadata"]["correlation_id"] = "foreign"
    elif fault == "member_order":
        data["metadata"]["members"].reverse()
    else:
        data["answers"][0]["name"] = "foreign"
    replay = OpenAIDecisionsReplayV1.model_validate_json(json.dumps(data))
    with pytest.raises(ProviderIntegrityError):
        verify_replay(replay, wire)
    verify_replay(original, wire)
