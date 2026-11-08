"""Production protocol dispatch and recovery with actual offline SDK HTTP calls."""

# pyright: reportPrivateUsage=false
from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any, Literal

import httpx2
import pytest
from durable_evidence_fixture import specification
from native_transport_fixture import NativeFixtureAdapter, context_for

from judgmentllm.config import RunSpecV3, load_run_spec
from judgmentllm.execution import execute_run, recover_run
from judgmentllm.lifecycle import (
    estimate_run,
    export_run_results,
    get_run,
    validate_inputs,
)
from judgmentllm.persistence.hashing import canonical_bytes
from judgmentllm.persistence.repository import ReadOnlyRepository
from judgmentllm.planning import stable_id
from judgmentllm.preparation import PreparationError, open_prepared_run, prepare_run
from judgmentllm.preparation.manifest import (
    ExecutableRunManifestV4,
    parse_executable_manifest,
)
from judgmentllm.preview import preview_prompt
from judgmentllm.production_transport import production_context
from judgmentllm.providers.openai import OpenAIAdapter
from judgmentllm.providers.protocol import ProviderError


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-key-never-real")

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("external network is forbidden")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def production_spec(
    directory: Path,
    endpoint: str = "responses",
    *,
    capture: Literal["full", "minimal"] = "full",
    **settings: Any,
) -> RunSpecV3:
    path = specification(directory, capture=capture, **settings)
    document = json.loads(path.read_text())
    document.update(
        schema_version=3,
        provider="openai",
        endpoint=endpoint,
        model="offline-unqualified-model",
    )
    document["prompt"]["structured_output"] = "text"
    path.write_text(json.dumps(document))
    spec = load_run_spec(path)
    assert isinstance(spec, RunSpecV3)
    return spec


def envelope(endpoint: str, output: str) -> dict[str, Any]:
    if endpoint == "responses":
        return {
            "id": "resp-offline",
            "object": "response",
            "created_at": 1,
            "status": "completed",
            "model": "reported-fixture-model",
            "output": [
                {
                    "id": "msg-offline",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {"type": "output_text", "text": output, "annotations": []}
                    ],
                }
            ],
            "usage": {
                "input_tokens": 20,
                "output_tokens": 10,
                "total_tokens": 30,
                "input_tokens_details": {"cached_tokens": 3},
                "output_tokens_details": {"reasoning_tokens": 2},
            },
        }
    return {
        "id": "chatcmpl-offline",
        "object": "chat.completion",
        "created": 1,
        "model": "reported-fixture-model",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": output},
            }
        ],
        "usage": {
            "prompt_tokens": 20,
            "completion_tokens": 10,
            "total_tokens": 30,
            "prompt_tokens_details": {"cached_tokens": 3},
            "completion_tokens_details": {"reasoning_tokens": 2},
        },
    }


def setup_case(
    root: Path,
    endpoint: str,
    *,
    capture: Literal["full", "minimal"] = "full",
    pairwise: bool = False,
    output: str | None = None,
) -> tuple[RunSpecV3, Path, OpenAIAdapter, list[bytes]]:
    spec = production_spec(
        root / "inputs",
        endpoint,
        capture=capture,
        pairwise=pairwise,
        explanations=False,
    )
    store = root / "runtime"
    assert pairwise or spec.rubric is not None
    with prepare_run(
        spec,
        store=store,
        run_id="001",
        capabilities=OpenAIAdapter.capabilities_for("openai", endpoint, spec.model),
        transport_context=production_context(spec),
    ) as prepared:
        assert type(prepared.manifest) is ExecutableRunManifestV4
        dependency = next(prepared.iter_request_dependencies())
        attempt_id = stable_id("attempt", "001", dependency.request.request_uid, 1)
        receipt = prepared.transport_for_attempt(attempt_id)
        payload_digest = receipt.payload_sha256
        if output is None:
            output = (
                json.dumps({"winner_id": "002"})
                if pairwise
                else json.dumps(
                    {
                        "scores": [
                            {
                                "trait_id": t.trait_id,
                                "category_label": t.levels[0].label,
                            }
                            for t in spec.rubric.traits  # pyright: ignore[reportOptionalMemberAccess]
                        ]
                    }
                )
            )  # type: ignore[union-attr]
    bodies: list[bytes] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        from judgmentllm.persistence.hashing import bytes_digest

        assert bytes_digest(request.content) == payload_digest
        bodies.append(request.content)
        return httpx2.Response(200, json=envelope(endpoint, output), request=request)

    adapter = OpenAIAdapter(
        endpoint,
        spec.model,
        concurrency=1,
        http_transport=httpx2.MockTransport(respond),
    )
    return spec, store, adapter, bodies


@pytest.mark.parametrize("endpoint", ["responses", "chat.completions"])
@pytest.mark.parametrize("pairwise", [False, True])
@pytest.mark.parametrize("capture", ["full", "minimal"])
@pytest.mark.asyncio
async def test_production_seal_execute_export_reopen(
    tmp_path: Path, endpoint: str, pairwise: bool, capture: Literal["full", "minimal"]
) -> None:
    spec, store, adapter, bodies = setup_case(
        tmp_path, endpoint, capture=capture, pairwise=pairwise
    )
    try:
        result = await execute_run("001", store=store, adapter=adapter, spec=spec)
        assert not result.recovery_required
        assert len(bodies) == adapter.actual_call_count == 1
        assert get_run("001", store=store).run_status == "completed"
        with ReadOnlyRepository(store) as repository:
            evidence = tuple(repository.iter_evidence("001"))
            assert len(evidence) == 1
            assert evidence[0].usage is not None
            assert evidence[0].usage.reported_model == "reported-fixture-model"
        exported = export_run_results("001", store=store, format="jsonl")
        assert exported.files
        # Reopening completed work must perform no second request.
        await recover_run("001", store=store, adapter=adapter, spec=spec)
        assert len(bodies) == 1
    finally:
        await adapter.aclose()


@pytest.mark.parametrize(
    "stage, recoverable",
    [
        ("after_submitting", False),
        ("after_submit", False),
        ("after_acknowledgment", False),
        ("after_response", False),
        ("after_evidence_commit", True),
        ("after_normalization", True),
    ],
)
@pytest.mark.asyncio
async def test_interruption_fresh_adapter_never_replaces_call(
    tmp_path: Path, stage: str, recoverable: bool
) -> None:
    spec, store, adapter, bodies = setup_case(tmp_path, "responses")

    def interrupt(current: str, request: str) -> None:
        if current == stage:
            raise RuntimeError("synthetic interruption")

    try:
        with pytest.raises(ExceptionGroup):
            await execute_run(
                "001", store=store, adapter=adapter, spec=spec, fault=interrupt
            )
    finally:
        await adapter.aclose()

    def forbidden(request: httpx2.Request) -> httpx2.Response:
        raise AssertionError("recovery must not issue a replacement request")

    fresh = OpenAIAdapter(
        "responses", spec.model, http_transport=httpx2.MockTransport(forbidden)
    )
    try:
        result = await recover_run("001", store=store, adapter=fresh, spec=spec)
        assert result.recovery_required is not recoverable
        assert fresh.actual_call_count == 0
        assert len(bodies) <= 1
    finally:
        await fresh.aclose()


def test_same_spec_version_explicit_manifest_dispatch(tmp_path: Path) -> None:
    spec = production_spec(tmp_path / "inputs", explanations=False)
    with prepare_run(
        spec,
        store=tmp_path / "production",
        run_id="prod",
        transport_context=production_context(spec),
    ) as prepared:
        assert spec.schema_version == 3
        assert prepared.manifest.schema_version == 4
        raw = canonical_bytes(prepared.manifest)
        assert type(parse_executable_manifest(raw)) is ExecutableRunManifestV4
        changed = json.loads(raw)
        changed["schema_version"] = 3
        with pytest.raises(ValueError):
            parse_executable_manifest(json.dumps(changed).encode())
    with pytest.raises(PreparationError):
        open_prepared_run(
            "prod",
            store=tmp_path / "production",
            spec=spec,
            transport_context=context_for("openai.responses.text.format"),
        )
    with pytest.raises(PreparationError):
        open_prepared_run("prod", store=tmp_path / "production", spec=spec)


@pytest.mark.asyncio
async def test_wrong_adapter_cannot_recover_production(tmp_path: Path) -> None:
    spec, store, adapter, bodies = setup_case(tmp_path, "responses")
    wrong = NativeFixtureAdapter(tmp_path / "independent.sqlite3")
    with pytest.raises(ProviderError):
        await recover_run("001", store=store, adapter=wrong, spec=spec)
    assert not bodies
    await adapter.aclose()


def test_offline_public_validation_plan_preview_without_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = production_spec(tmp_path / "inputs", explanations=False)
    monkeypatch.delenv("OPENAI_API_KEY")
    assert validate_inputs(spec).valid
    assert estimate_run(spec).requests == 1
    rendered = preview_prompt(spec, item_id="001")
    from judgmentllm.production_prompts import RenderedProductionPromptV1

    assert isinstance(rendered, RenderedProductionPromptV1)
    assert rendered.renderer_version == 3
    assert "Synthetic assessment input." in rendered.text


@pytest.mark.asyncio
async def test_grouped_salvage_retry_inherits_production_protocol(
    tmp_path: Path,
) -> None:
    from judgmentllm.execution.retry import authorize_subset_retry
    from judgmentllm.production_transport import TransportCommitmentV2

    spec, store, original, _ = setup_case(tmp_path, "responses")
    await original.aclose()
    assert spec.rubric is not None
    traits = spec.rubric.traits
    output = {
        "scores": [
            {
                "trait_id": trait.trait_id,
                "category_label": trait.levels[0].label
                if index == 0
                else "invalid category",
            }
            for index, trait in enumerate(traits)
        ]
    }
    calls: list[bytes] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        calls.append(request.content)
        return httpx2.Response(
            200, json=envelope("responses", json.dumps(output)), request=request
        )

    adapter = OpenAIAdapter(
        "responses", spec.model, http_transport=httpx2.MockTransport(respond)
    )
    try:
        await execute_run("001", store=store, adapter=adapter, spec=spec)
        with ReadOnlyRepository(store) as repository:
            rows = repository.connection.execute(
                "SELECT task_id,status FROM results ORDER BY task_id"
            ).fetchall()
            selected = tuple(
                str(row["task_id"]) for row in rows if row["status"] == "invalid"
            )
            assert len(selected) == 1
        output["scores"] = [
            {
                "trait_id": traits[1].trait_id,
                "category_label": traits[1].levels[0].label,
            }
        ]
        authorize_subset_retry(
            "001",
            store=store,
            adapter=adapter,
            spec=spec,
            task_ids=selected,
            operation_id="retry-invalid",
        )
        await recover_run("001", store=store, adapter=adapter, spec=spec)
        assert len(calls) == 2
        assert get_run("001", store=store).run_status == "completed"
        with open_prepared_run(
            "001",
            store=store,
            spec=spec,
            capabilities=adapter.capabilities(spec),
            transport_context=adapter.transport_context,
        ) as prepared:
            attempts = prepared.repository.connection.execute(
                "SELECT attempt_id FROM attempts ORDER BY attempt_number"
            ).fetchall()
            first = prepared.transport_for_attempt(str(attempts[0][0]))
            second = prepared.transport_for_attempt(str(attempts[1][0]))
            assert isinstance(first, TransportCommitmentV2) and isinstance(
                second, TransportCommitmentV2
            )
            assert second.parent_commitment_sha256 == first.commitment_sha256
            assert second.mechanism == first.mechanism == "text"
            assert len(tuple(prepared.repository.iter_evidence("001"))) == 2
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_public_run_closes_client_and_preserves_v3_requirement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from judgmentllm import lifecycle

    spec, _, adapter, bodies = setup_case(tmp_path, "responses")

    def injected(*args: Any, **kwargs: Any) -> OpenAIAdapter:
        return adapter

    monkeypatch.setattr(lifecycle, "_execution_adapter", injected)
    result = await lifecycle.arun_live(spec, store=tmp_path / "public")
    assert result.run_status == "completed"
    assert len(bodies) == 1
    assert adapter._client is None
    older = dict(spec)
    older["schema_version"] = 2
    older["prompt"] = {"structured_output": "text"}
    from judgmentllm.config import validate_run_spec

    historical = validate_run_spec(older)
    with pytest.raises(lifecycle.LifecycleError, match="version 3"):
        await lifecycle.arun_live(historical, store=tmp_path / "forbidden")
    assert not (tmp_path / "forbidden").exists()


@pytest.mark.asyncio
async def test_missing_credentials_fail_before_store_or_submission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from judgmentllm import lifecycle

    spec = production_spec(tmp_path / "inputs", explanations=False)
    monkeypatch.delenv("OPENAI_API_KEY")
    with pytest.raises(lifecycle.LifecycleError, match="OPENAI_API_KEY"):
        await lifecycle.arun_live(spec, store=tmp_path / "no-store")
    assert not (tmp_path / "no-store").exists()
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-key-never-real")
    _, store, adapter, _ = setup_case(tmp_path / "resume", "responses")
    monkeypatch.delenv("OPENAI_API_KEY")

    def injected(*args: Any, **kwargs: Any) -> OpenAIAdapter:
        return adapter

    monkeypatch.setattr(lifecycle, "_execution_adapter", injected)
    with pytest.raises(lifecycle.LifecycleError, match="OPENAI_API_KEY"):
        await lifecycle.aresume_run(
            "001",
            store=store,
            spec=load_run_spec(tmp_path / "resume" / "inputs" / "run.json"),
        )
    assert adapter.actual_call_count == 0
    with ReadOnlyRepository(store) as repository:
        assert all(
            attempt.state not in {"submitting", "uncertain", "acknowledged"}
            for attempt in repository.iter_attempts("001")
        )


@pytest.mark.asyncio
async def test_client_cleanup_survives_repeated_cancellation() -> None:
    import asyncio

    from judgmentllm.lifecycle import _close_adapter

    started = asyncio.Event()
    release = asyncio.Event()
    closed = asyncio.Event()

    class SlowClose(OpenAIAdapter):
        async def aclose(self) -> None:
            started.set()
            await release.wait()
            closed.set()

    adapter = SlowClose("responses", "offline-unqualified-model")
    task = asyncio.create_task(_close_adapter(adapter))
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()
