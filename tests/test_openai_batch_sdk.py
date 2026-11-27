"""Exact SDK, per-member wire parity and public offline lifecycle."""

import json
from pathlib import Path
from typing import Any

import pytest
from openai_batch_fixture import Service, diagnostic_records, install, specification

from judgmentllm import (
    collect_batch,
    poll_batch,
    prepare_batch,
    resume_batch,
    submit_batch,
)
from judgmentllm.persistence.repository import ReadOnlyRepository


@pytest.mark.parametrize("endpoint", ["responses", "chat.completions"])
@pytest.mark.parametrize("capture", ["full", "minimal"])
def test_round_trip(
    tmp_path: Path, monkeypatch: Any, endpoint: Any, capture: Any
) -> Any:
    service = Service(tmp_path / "service.sqlite")
    install(monkeypatch, service)
    spec = specification(tmp_path, endpoint=endpoint, capture=capture)
    store = tmp_path / "store"
    prepare_batch(spec, store=store, run_id="001")
    service.configure(store, spec)
    assert submit_batch("001", store=store, spec=spec).outcome == "ok"
    assert poll_batch("001", store=store).outcome == "ok"
    final = None
    for _ in range(4):
        final = collect_batch("001", store=store, spec=spec)
        if not final.batch.recovery_required:
            break
    assert final is not None
    assert final.outcome == "ok"
    assert not final.batch.recovery_required
    with ReadOnlyRepository(store) as repo:
        results = tuple(repo.iter_results("001"))
        assert len(results) == 2
        assert all(r.status == "complete" for r in results)
        usage = [
            json.loads(r[0])["usage"]
            for r in repo.connection.execute(
                "SELECT record_json FROM batch_member_receipts"
            )
        ]
        assert sum(u["input_tokens"] for u in usage) == 40
        assert sum(u["output_tokens"] for u in usage) == 20
        assert {u["sdk_version"] for u in usage} == {"3.42.1"}
        assert {u["invocation_kind"] for u in usage} == {"native_batch"}
    before = service.events()
    resume_batch("001", store=store, spec=spec)
    assert [e for e in service.events() if e[0] == "POST"] == [
        e for e in before if e[0] == "POST"
    ]


@pytest.mark.parametrize("mismatch", ["metadata", "loaded"])
def test_actual_sdk_guard_precedes_credentials_store_and_wire(
    tmp_path: Any, monkeypatch: Any, mismatch: Any
) -> Any:
    import openai

    from judgmentllm import openai_batch_transport
    from judgmentllm.lifecycle_models import LifecycleError
    from judgmentllm.providers import openai_batch

    spec = specification(tmp_path)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("credential/wire work preceded actual SDK guard")

    monkeypatch.setattr(openai_batch, "_resolve_credentials", forbidden)
    monkeypatch.setattr(openai_batch_transport, "build_openai_request_body", forbidden)
    if mismatch == "metadata":

        def wrong_version(_name: str) -> str:
            return "3.42.0"

        monkeypatch.setattr(openai_batch_transport.metadata, "version", wrong_version)
    else:
        monkeypatch.setattr(openai, "__version__", "3.42.0")
    with pytest.raises(LifecycleError, match="3.42.1"):
        prepare_batch(spec, store=tmp_path / "never", run_id="001")
    assert not (tmp_path / "never").exists()


@pytest.mark.parametrize(
    "setting,value", [("mode", "live"), ("cache_policy", "explicit"), ("tier", "flex")]
)
def test_incompatible_scope_fails_before_sdk_or_credentials(
    tmp_path: Any, monkeypatch: Any, setting: Any, value: Any
) -> Any:
    from judgmentllm import openai_batch_transport
    from judgmentllm.execution import openai_batch as openai_batch_execution
    from judgmentllm.lifecycle_models import LifecycleError
    from judgmentllm.providers import openai_batch as openai_batch_provider

    spec = specification(tmp_path)
    setattr(spec.execution, setting, value)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("SDK guard reached for an incompatible scope")

    # Load both consumers above so their import-time bindings can be restored.
    with monkeypatch.context() as patch:
        patch.setattr(openai_batch_transport, "require_sdk", forbidden)
        patch.setattr(openai_batch_execution, "require_sdk", forbidden)
        patch.setattr(openai_batch_provider, "require_sdk", forbidden)
        with pytest.raises(LifecycleError):
            prepare_batch(spec, store=tmp_path / "never")
    assert not (tmp_path / "never").exists()


@pytest.mark.parametrize("endpoint", ["responses", "chat.completions"])
@pytest.mark.parametrize(
    "generation", [None, {"temperature": 0, "top_p": 0.75, "max_output_tokens": 123}]
)
@pytest.mark.parametrize("layout", ["together", "scores_first", "explanations_first"])
def test_exact_live_body_parity_omission_store_caps_and_grouped_usage(
    tmp_path: Any, monkeypatch: Any, endpoint: Any, generation: Any, layout: Any
) -> Any:
    from judgmentllm.config import RunSpecV3, validate_run_spec
    from judgmentllm.openai_batch_transport import batch_context
    from judgmentllm.planning import stable_id
    from judgmentllm.preparation import open_prepared_run, prepare_run
    from judgmentllm.preparation.batch import encode_row, members_for, reconstruct
    from judgmentllm.production_transport import production_context
    from judgmentllm.providers.openai import OpenAIAdapter
    from judgmentllm.providers.openai_batch import OpenAIBatchAdapter

    service = Service(tmp_path / "service.sqlite")
    install(monkeypatch, service)
    data = specification(
        tmp_path, endpoint=endpoint, responses=1, traits=2, explanations=True
    ).model_dump(mode="json")
    data["generation"] = (
        {**generation, "reasoning": {"effort": "none"}}
        if generation is not None and endpoint == "responses"
        else generation
    )
    data["prompt"]["output_order"] = layout
    spec = validate_run_spec(data)
    store = tmp_path / "store"
    prepare_batch(spec, store=store, run_id="001")
    adapter = OpenAIBatchAdapter(batch_context(spec))
    with open_prepared_run(
        "001", store=store, spec=spec, capabilities=adapter.capabilities(spec)
    ) as prepared:
        shard = prepared.repository.connection.execute(
            "SELECT shard_id FROM batch_preparations"
        ).fetchone()[0]
        member = next(members_for(prepared, shard))
        _, wire = reconstruct(prepared, adapter, member)
        body = wire.payload
        wrapped = encode_row(member.member_key, wire)
        assert b'"body":' + body + b"}\n" in wrapped
    live_data = spec.model_dump(mode="json")
    live_data["execution"]["mode"] = "live"
    live = validate_run_spec(live_data)
    assert isinstance(live, RunSpecV3)
    with prepare_run(
        live,
        store=tmp_path / "live",
        run_id="parity",
        capabilities=OpenAIAdapter(endpoint, spec.model).capabilities(live),
        transport_context=production_context(live),
    ) as prepared:
        dependency = next(prepared.iter_request_dependencies())
        attempt = stable_id("attempt", "parity", dependency.request.request_uid, 1)
        receipt = prepared.transport_for_attempt(attempt)
        assert (
            prepared.store.read(
                prepared.repository.get_artifact("parity", receipt.payload_sha256)
            )
            == body
        )
    decoded = json.loads(body)
    assert decoded["store"] is False and decoded["stream"] is False
    assert decoded["prompt_cache_options"] == {"mode": "explicit"}
    cap = "max_output_tokens" if endpoint == "responses" else "max_completion_tokens"
    assert (cap in decoded) == (generation is not None)
    if generation is not None:
        assert (
            decoded[cap] == 123
            and decoded["temperature"] == 0
            and decoded["top_p"] == 0.75
        )
    if endpoint == "responses":
        assert decoded["truncation"] == "disabled"
    else:
        assert decoded["n"] == 1


def test_status_stop_export_remain_sdk_and_credential_free(
    tmp_path: Any, monkeypatch: Any
) -> Any:
    from judgmentllm import (
        export_batch_provenance,
        get_batch_status,
        openai_batch_transport,
        stop_batch,
    )
    from judgmentllm.providers import openai_batch

    service = Service(tmp_path / "service.sqlite")
    install(monkeypatch, service)
    spec = specification(tmp_path)
    store = tmp_path / "store"
    prepare_batch(spec, store=store, run_id="001")

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("local control attempted SDK/credentials")

    monkeypatch.setattr(openai_batch_transport, "require_sdk", forbidden)
    monkeypatch.setattr(openai_batch, "_resolve_credentials", forbidden)
    assert get_batch_status("001", store=store).run_id == "001"
    assert stop_batch("001", store=store).batch.local_stop
    report = export_batch_provenance("001", store=store)
    assert report.schema_version == report.manifest.schema_version == 2


@pytest.mark.parametrize("capture", ["full", "minimal"])
def test_generic_resume_provenance_and_local_cancel_dispatch_from_manifest13(
    tmp_path: Any, monkeypatch: Any, capture: str
) -> None:
    from judgmentllm import resume_run
    from judgmentllm.lifecycle import cancel_run, export_run_provenance

    service = Service(tmp_path / "service.sqlite")
    install(monkeypatch, service)
    spec = specification(tmp_path, capture=capture)
    store = tmp_path / "store"
    prepare_batch(spec, store=store, run_id="001")
    service.configure(store, spec)
    result = None
    for _ in range(3):
        result = resume_run("001", store=store, spec=spec)
        if result.run_status == "completed":
            break
    assert (
        result is not None and result.results == 2 and result.run_status == "completed"
    )
    report = export_run_provenance("001", store=store)
    assert report.schema_version == 2
    before = service.events()
    cancelled = cancel_run("001", store=store)
    assert cancelled.batch is not None and cancelled.batch.local_stop
    assert service.events() == before


@pytest.mark.parametrize("policy,strict", [("native", False), ("auto", True)])
def test_unqualified_batch_native_authority_cannot_inherit_live_registry(
    tmp_path: Any, policy: str, strict: bool
) -> None:
    from judgmentllm.lifecycle_models import LifecycleError

    spec = specification(tmp_path)
    spec.prompt.structured_output = policy
    spec.strict_capabilities = strict
    with pytest.raises(LifecycleError) as error:
        prepare_batch(spec, store=tmp_path / "never")
    assert error.value.category == "validation"
    assert not (tmp_path / "never").exists()


def test_explicit_default_mock_cli_option_is_rejected_for_openai(
    tmp_path: Any, monkeypatch: Any
) -> None:
    from test_cli_lifecycle import control

    service = Service(tmp_path / "service.sqlite")
    install(monkeypatch, service)
    spec = specification(tmp_path)
    config = tmp_path / "config.json"
    config.write_text(spec.model_dump_json())
    result, _ = control(
        [
            "batch",
            "prepare",
            "--config",
            str(config),
            "--store",
            str(tmp_path / "never"),
            "--mock-max-members",
            "1000",
        ],
        2,
    )
    assert result["error"]["category"] in {"validation", "unsupported_capability"}
    assert not (tmp_path / "never").exists()
    assert not service.events()


def test_actual_sdk_upload_serialization_is_validated_before_forward(
    tmp_path: Any, monkeypatch: Any
) -> None:
    from openai.resources.files import AsyncFiles

    from judgmentllm.lifecycle_models import LifecycleError

    service = Service(tmp_path / "service.sqlite")
    install(monkeypatch, service)
    spec = specification(tmp_path)
    store = tmp_path / "store"
    prepare_batch(spec, store=store, run_id="001")
    service.configure(store, spec)
    original = AsyncFiles.create

    async def tampered(self: Any, **kwargs: Any) -> Any:
        kwargs["purpose"] = "assistants"
        return await original(self, **kwargs)

    monkeypatch.setattr(AsyncFiles, "create", tampered)
    with pytest.raises(LifecycleError) as error:
        submit_batch("001", store=store)
    assert error.value.category == "execution"
    assert not service.events()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value", [("endpoint", "chat.completions"), ("model", "different-model")]
)
async def test_wrong_route_context_releases_real_executor_ownership(
    tmp_path: Any, monkeypatch: Any, field: str, value: str
) -> None:
    from judgmentllm import BatchControlOptionsV1
    from judgmentllm.execution.native_batch import InvocationBudget
    from judgmentllm.execution.openai_batch import OpenAIBatchSession
    from judgmentllm.openai_batch_transport import batch_context
    from judgmentllm.persistence.models import IntegrityError
    from judgmentllm.persistence.ownership import executor_is_active
    from judgmentllm.providers.openai_batch import OpenAIBatchAdapter

    service = Service(tmp_path / "service.sqlite")
    install(monkeypatch, service)
    spec = specification(tmp_path)
    store = tmp_path / "store"
    prepare_batch(spec, store=store, run_id="001")
    context = batch_context(spec).model_copy(update={field: value})
    with pytest.raises(IntegrityError, match="sealed route"):
        OpenAIBatchSession(
            "001",
            store,
            OpenAIBatchAdapter(context),
            InvocationBudget(BatchControlOptionsV1()),
        )
    with ReadOnlyRepository(store) as repo:
        assert not executor_is_active(repo, "001")
    assert not service.events()


@pytest.mark.parametrize("endpoint", ["responses", "chat.completions"])
@pytest.mark.parametrize("capture", ["full", "minimal"])
@pytest.mark.parametrize("failure", ["invalid_json", "output_limit", "http_error"])
def test_failed_member_never_becomes_a_score_and_usage_is_not_invented(
    tmp_path: Any, monkeypatch: Any, endpoint: str, capture: str, failure: str
) -> None:
    from judgmentllm.results import RubricResult

    service = Service(tmp_path / "service.sqlite")
    install(monkeypatch, service)
    spec = specification(tmp_path, endpoint=endpoint, capture=capture, responses=1)
    store = tmp_path / "store"
    prepare_batch(spec, store=store, run_id="001")
    service.configure(store, spec)
    submit_batch("001", store=store, spec=spec)
    poll_batch("001", store=store)
    row = json.loads(service.file("file-output_1")[0])
    body = row["response"]["body"]
    if failure == "http_error":
        row["response"].update(
            status_code=500, body={"error": {"message": "PROHIBITED_ERROR_TEXT"}}
        )
    elif endpoint == "responses":
        if failure == "invalid_json":
            body["output"][0]["content"][0]["text"] = "invalid JSON"
        else:
            body.update(
                status="incomplete", incomplete_details={"reason": "max_output_tokens"}
            )
    elif failure == "invalid_json":
        body["choices"][0]["message"]["content"] = "invalid JSON"
    else:
        body["choices"][0]["finish_reason"] = "length"
    raw = json.dumps(row).encode() + b"\n"
    service.file("file-output_1", body=raw, declared=len(raw))
    collect_batch("001", store=store, spec=spec)
    with ReadOnlyRepository(store) as repo:
        result = next(repo.iter_results("001"))
        assert isinstance(result, RubricResult)
        assert result.status == ("invalid" if failure == "invalid_json" else "errored")
        assert result.primary_score is None
        receipts = [
            json.loads(r[0])
            for r in repo.connection.execute(
                "SELECT record_json FROM batch_member_receipts"
            )
        ]
        assert len(receipts) == 1
        assert receipts[0]["usage"]["input_tokens"] == (
            None if failure == "http_error" else 20
        )
        assert receipts[0]["usage"]["output_tokens"] == (
            None if failure == "http_error" else 10
        )


@pytest.mark.parametrize("endpoint", ["responses", "chat.completions"])
@pytest.mark.parametrize("action", ["submit", "poll", "collect", "resume", "cancel"])
@pytest.mark.parametrize("mismatch", ["missing", "installed", "loaded"])
def test_continuation_sdk_diagnostic_is_safe_before_credentials_or_clients(
    tmp_path: Any, monkeypatch: Any, endpoint: str, action: str, mismatch: str
) -> None:
    import openai

    from judgmentllm import native_batch, openai_batch_transport
    from judgmentllm.lifecycle_models import LifecycleError
    from judgmentllm.providers import openai_batch

    service = Service(tmp_path / "service.sqlite")
    install(monkeypatch, service)
    spec = specification(tmp_path, endpoint=endpoint)
    store = tmp_path / "store"
    prepare_batch(spec, store=store, run_id="001")
    calls: list[str] = []

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        calls.append("credential/client/HTTP access")
        raise AssertionError("SDK readiness did not precede resource access")

    monkeypatch.setattr(openai_batch, "_resolve_credentials", forbidden)
    monkeypatch.setattr(openai_batch, "_sdk", forbidden)
    monkeypatch.setattr(openai_batch, "_protected_client", forbidden)
    monkeypatch.setattr(openai_batch.OpenAIBatchAdapter, "__init__", forbidden)
    monkeypatch.setattr(service, "request", forbidden)
    if mismatch == "missing":

        def missing(_name: str) -> str:
            raise openai_batch_transport.metadata.PackageNotFoundError(
                "PROHIBITED_METADATA_SECRET"
            )

        monkeypatch.setattr(openai_batch_transport.metadata, "version", missing)
    elif mismatch == "installed":

        def wrong(_name: str) -> str:
            return "PROHIBITED_VERSION_SECRET"

        monkeypatch.setattr(openai_batch_transport.metadata, "version", wrong)
    else:
        monkeypatch.setattr(openai, "__version__", "PROHIBITED_VERSION_SECRET")
    with pytest.raises(LifecycleError) as error:
        getattr(native_batch, action + "_batch")("001", store=store, spec=spec)
    assert error.value.category == "unsupported_capability"
    assert error.value.run_id == "001" and error.value.store == str(store)
    assert "PROHIBITED_" not in str(error.value)
    assert not calls and not service.events()


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["responses", "chat.completions"])
@pytest.mark.parametrize("cached", [False, True])
async def test_accumulated_metadata_byte_ceiling_precedes_json_processing(
    tmp_path: Any, monkeypatch: Any, endpoint: str, cached: bool
) -> None:
    import httpx2

    from judgmentllm import BatchControlOptionsV1
    from judgmentllm.execution.native_batch import InvocationBudget
    from judgmentllm.openai_batch_transport import batch_context
    from judgmentllm.providers import openai_batch

    service = Service(tmp_path / "service.sqlite")
    install(monkeypatch, service)
    spec = specification(tmp_path, endpoint=endpoint)
    body = json.dumps(
        {
            "id": "file-output_1",
            "purpose": "batch_output",
            "bytes": 0,
        }
    ).encode()
    raw = b" " * (700_000 - len(body)) + body
    requests: list[str] = []
    decoded: list[int] = []
    original_decode = openai_batch.decode_assessment_output

    def response(request: Any) -> Any:
        requests.append(request.url.path)
        if cached:
            return httpx2.Response(200, content=raw)
        return httpx2.Response(200, stream=httpx2.ByteStream(raw))

    def decode(data: Any) -> Any:
        decoded.append(len(data))
        return original_decode(data)

    monkeypatch.setattr(service, "request", response)
    monkeypatch.setattr(openai_batch, "decode_assessment_output", decode)
    budget = InvocationBudget(BatchControlOptionsV1(max_io_steps=64))
    adapter = openai_batch.OpenAIBatchAdapter(batch_context(spec), budget=budget)
    try:
        for _ in range(5):
            assert await adapter.file_metadata("file-output_1") == (
                "file-output_1",
                "batch_output",
                0,
            )
        with pytest.raises(OSError):
            await adapter.file_metadata("file-output_1")
        assert decoded == [700_000] * 5
        assert len(requests) == 6 and budget.bytes == 4_200_000
        assert budget.closed and budget.steps < 64
        with pytest.raises(OSError):
            await adapter.file_metadata("file-output_1")
        assert len(requests) == 6 and len(decoded) == 5
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["responses", "chat.completions"])
@pytest.mark.parametrize("cached", [False, True])
async def test_physical_prefix_replay_byte_ceiling_precedes_any_yield(
    tmp_path: Any, monkeypatch: Any, endpoint: str, cached: bool
) -> None:
    from collections.abc import AsyncIterator

    import httpx2

    from judgmentllm import BatchControlOptionsV1
    from judgmentllm.execution.native_batch import InvocationBudget
    from judgmentllm.openai_batch_transport import batch_context
    from judgmentllm.providers.openai_batch import OpenAIBatchAdapter

    service = Service(tmp_path / "service.sqlite")
    install(monkeypatch, service)
    spec = specification(tmp_path, endpoint=endpoint)
    body = b"x" * (3 * 1_048_576)
    requests: list[str] = []

    class CoarseStream(httpx2.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield body

    def response(request: Any) -> Any:
        requests.append(request.url.path)
        if cached:
            return httpx2.Response(200, content=body)
        return httpx2.Response(200, stream=CoarseStream())

    monkeypatch.setattr(service, "request", response)
    budget = InvocationBudget(BatchControlOptionsV1(max_io_steps=64))
    adapter = OpenAIBatchAdapter(batch_context(spec), budget=budget)
    try:
        first = [chunk async for chunk in adapter.content("file-output_1", len(body))]
        assert sum(map(len, first)) == len(body)
        assert all(len(chunk) <= 65_536 for chunk in first)
        assert budget.bytes == len(body)
        replay: list[bytes] = []
        with pytest.raises(OSError):
            async for chunk in adapter.content("file-output_1", len(body)):
                replay.append(chunk)
        assert replay == [] and budget.bytes == 2 * len(body)
        assert requests == ["/v1/files/file-output_1/content"] * 2
        assert budget.closed and budget.steps < 64
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["responses", "chat.completions"])
@pytest.mark.parametrize("cached", [False, True])
async def test_source_size_overrun_keeps_its_integrity_classification(
    tmp_path: Any, monkeypatch: Any, endpoint: str, cached: bool
) -> None:
    import httpx2

    from judgmentllm import BatchControlOptionsV1
    from judgmentllm.execution.native_batch import InvocationBudget
    from judgmentllm.openai_batch_transport import batch_context
    from judgmentllm.providers.batch_protocol import BatchSourceOverrun
    from judgmentllm.providers.openai_batch import OpenAIBatchAdapter

    service = Service(tmp_path / "service.sqlite")
    install(monkeypatch, service)
    spec = specification(tmp_path, endpoint=endpoint)

    def response(_request: Any) -> Any:
        if cached:
            return httpx2.Response(200, content=b"xx")
        return httpx2.Response(200, stream=httpx2.ByteStream(b"xx"))

    monkeypatch.setattr(service, "request", response)
    budget = InvocationBudget(BatchControlOptionsV1())
    adapter = OpenAIBatchAdapter(batch_context(spec), budget=budget)
    yielded: list[bytes] = []
    try:
        with pytest.raises(BatchSourceOverrun):
            async for chunk in adapter.content("file-output_1", 1):
                yielded.append(chunk)
        assert yielded == [] and budget.bytes == 2
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["responses", "chat.completions"])
@pytest.mark.parametrize("shape", ["cached", "sync-eager", "async-eager", "streamed"])
@pytest.mark.parametrize("status", [200, 503])
async def test_zero_byte_content_classifies_http_status_before_sdk_error_reads(
    tmp_path: Any, monkeypatch: Any, endpoint: str, shape: str, status: int
) -> None:
    from collections.abc import AsyncIterator

    import httpx2
    from openai._base_client import BaseClient

    from judgmentllm import BatchControlOptionsV1
    from judgmentllm.execution.native_batch import InvocationBudget
    from judgmentllm.openai_batch_transport import batch_context
    from judgmentllm.providers.batch_protocol import BatchSourceOverrun
    from judgmentllm.providers.openai_batch import OpenAIBatchAdapter

    service = Service(tmp_path / "service.sqlite")
    install(monkeypatch, service)
    spec = specification(tmp_path, endpoint=endpoint)
    body = b'{"error":{"message":"PROHIBITED_HTTP_ERROR_DETAIL"}}'
    requests: list[str] = []
    stream_events: list[str] = []
    sdk_reads: list[str] = []
    responses: list[Any] = []

    class Body(httpx2.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            stream_events.append("read")
            yield body

        async def aclose(self) -> None:
            stream_events.append("close")

    async def response(request: Any) -> Any:
        requests.append(request.url.path)
        if shape == "cached":
            result = httpx2.Response(status, content=body)
        elif shape == "sync-eager":
            result = httpx2.Response(status, stream=httpx2.ByteStream(body))
            result.read()
        else:
            result = httpx2.Response(status, stream=Body())
            if shape == "async-eager":
                await result.aread()

        async def forbidden_read() -> bytes:
            sdk_reads.append("aread")
            raise AssertionError("HTTP error body reached SDK eager read")

        if status != 200:
            monkeypatch.setattr(result, "aread", forbidden_read)
        responses.append(result)
        return result

    def forbidden_status(*args: Any, **kwargs: Any) -> Any:
        sdk_reads.append("status_details")
        raise AssertionError("HTTP error body reached SDK detail parsing")

    monkeypatch.setattr(service, "request", response)
    monkeypatch.setattr(
        BaseClient, "_make_status_error_from_response", forbidden_status
    )
    budget = InvocationBudget(BatchControlOptionsV1())
    adapter = OpenAIBatchAdapter(batch_context(spec), budget=budget)
    yielded: list[bytes] = []
    try:
        expected = BatchSourceOverrun if status == 200 else OSError
        with pytest.raises(expected) as error:
            async for chunk in adapter.content("file-output_1", 0):
                yielded.append(chunk)
        assert type(error.value) is expected
        assert "PROHIBITED_" not in str(error.value)
        assert yielded == [] and sdk_reads == []
        assert requests == ["/v1/files/file-output_1/content"]
        assert budget.bytes == (
            0 if status == 503 and shape == "streamed" else len(body)
        )
        assert not budget.closed
        if shape in ("async-eager", "streamed"):
            assert stream_events == (
                ["close"]
                if status == 503 and shape == "streamed"
                else ["read", "close"]
            )
        assert responses[0].is_closed
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["responses", "chat.completions"])
@pytest.mark.parametrize(
    "shape,failure",
    [
        ("cached", "control"),
        ("async-eager", "control"),
        ("cached", "global"),
        ("async-eager", "global"),
        ("streamed", "close"),
        ("drained", "unaccounted"),
    ],
)
async def test_http_error_control_and_unsettled_cleanup_remain_opaque(
    tmp_path: Any, monkeypatch: Any, endpoint: str, shape: str, failure: str
) -> None:
    from collections.abc import AsyncIterator

    import httpx2

    from judgmentllm import BatchControlOptionsV1
    from judgmentllm.execution.native_batch import InvocationBudget
    from judgmentllm.openai_batch_transport import batch_context
    from judgmentllm.providers.openai_batch import (
        CONTROL_MAX_BYTES,
        OpenAIBatchAdapter,
    )

    service = Service(tmp_path / "service.sqlite")
    install(monkeypatch, service)
    spec = specification(tmp_path, endpoint=endpoint)
    size = CONTROL_MAX_BYTES + 1 if failure == "control" else 65_537
    body = b"PROHIBITED_HTTP_ERROR_DETAIL".ljust(size, b"x")
    requests: list[str] = []
    events: list[str] = []

    class Body(httpx2.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            events.append("read")
            yield body

        async def aclose(self) -> None:
            events.append("close")
            if failure == "close":
                raise OSError("PROHIBITED_CLOSE_DETAIL")

    async def response(request: Any) -> Any:
        requests.append(request.url.path)
        if shape == "cached":
            return httpx2.Response(503, content=body)
        result = httpx2.Response(503, stream=Body())
        if shape == "async-eager":
            await result.aread()
        elif shape == "drained":
            async for _ in result.aiter_raw():
                pass
        return result

    monkeypatch.setattr(service, "request", response)
    options = BatchControlOptionsV1(max_io_steps=1 if failure == "global" else 1024)
    budget = InvocationBudget(options)
    adapter = OpenAIBatchAdapter(batch_context(spec), budget=budget)
    yielded: list[bytes] = []
    try:
        with pytest.raises(OSError) as error:
            async for chunk in adapter.content("file-output_1", 0):
                yielded.append(chunk)
        assert type(error.value) is OSError
        assert "PROHIBITED_" not in str(error.value)
        assert yielded == [] and budget.closed
        assert budget.bytes == (len(body) if failure in ("control", "global") else 0)
        if shape != "cached":
            assert events == (["close"] if failure == "close" else ["read", "close"])
        with pytest.raises(OSError):
            await anext(adapter.content("file-output_1", 0))
        assert requests == ["/v1/files/file-output_1/content"]
    finally:
        await adapter.aclose()


@pytest.mark.parametrize("endpoint", ["responses", "chat.completions"])
@pytest.mark.parametrize(
    "match", ["exact", "changed-body", "wrong-length", "ambiguous"]
)
def test_lost_upload_ack_requires_unique_exact_authorized_input(
    tmp_path: Any, monkeypatch: Any, endpoint: str, match: str
) -> None:
    service = Service(tmp_path / "service.sqlite")
    install(monkeypatch, service)
    spec = specification(tmp_path, endpoint=endpoint)
    store = tmp_path / "store"
    prepare_batch(spec, store=store, run_id="001")
    service.configure(store, spec)
    service.set("lose_upload_ack", True)
    first = submit_batch("001", store=store, spec=spec)
    assert first.batch.accepted_jobs == 0 and first.batch.unresolved_operations
    service.set("lose_upload_ack", False)
    body, size = service.file("file-input_1")
    if match == "changed-body":
        service.file("file-input_1", body=b"!" + body[1:])
    elif match == "wrong-length":
        service.file("file-input_1", declared=size + 1)
    elif match == "ambiguous":
        with service.connect() as db:
            db.execute(
                "INSERT INTO files SELECT 'file-duplicate',purpose,filename,body,declared FROM files WHERE id='file-input_1'"
            )
    with ReadOnlyRepository(store) as repo:
        intent = tuple(
            repo.connection.execute(
                "SELECT instance_id,intent_sha256 FROM batch_operations WHERE kind='upload'"
            ).fetchone()
        )
    before = len(service.events())
    resumed = resume_batch("001", store=store, spec=spec)
    assert ("GET", "/v1/files") in service.events()[before:]
    content = [
        path for _, path in service.events()[before:] if path.endswith("/content")
    ]
    with ReadOnlyRepository(store) as repo:
        assert (
            tuple(
                repo.connection.execute(
                    "SELECT instance_id,intent_sha256 FROM batch_operations WHERE kind='upload'"
                ).fetchone()
            )
            == intent
        )
        if match == "exact":
            assert resumed.batch.accepted_jobs == 1
            assert not resumed.batch.unresolved_operations
            assert (
                repo.connection.execute(
                    "SELECT remote_id FROM submissions WHERE certainty='accepted'"
                ).fetchone()[0]
                == "batch_1"
            )
            assert content.count("/v1/files/file-input_1/content") == 1
        else:
            assert resumed.batch.accepted_jobs == 0
            assert resumed.batch.unresolved_operations
            assert not repo.connection.execute(
                "SELECT 1 FROM batch_facts WHERE kind='operation'"
            ).fetchone()
            assert repo.summary("001").results == 0
            assert content == (
                ["/v1/files/file-input_1/content"] if match == "changed-body" else []
            )
    posts = [event for event in service.events() if event[0] == "POST"]
    assert posts == [("POST", "/v1/files")] + (
        [("POST", "/v1/batches")] if match == "exact" else []
    )


@pytest.mark.parametrize("endpoint", ["responses", "chat.completions"])
def test_batch_preview_uses_native_output_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, endpoint: str
) -> None:
    from judgmentllm import preview_prompt
    from judgmentllm.openai_batch_transport import (
        PreparedOpenAIBatchRequest,
        batch_context,
    )
    from judgmentllm.persistence.hashing import bytes_digest
    from judgmentllm.preparation import open_prepared_run
    from judgmentllm.preparation.batch import members_for, reconstruct
    from judgmentllm.production_prompts import RenderedProductionPromptV1
    from judgmentllm.providers import openai_batch

    service = Service(tmp_path / "service.sqlite")
    install(monkeypatch, service)
    spec = specification(tmp_path, endpoint=endpoint, responses=1)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("offline preview reached credentials or a client")

    monkeypatch.setattr(openai_batch, "_resolve_credentials", forbidden)
    monkeypatch.setattr(openai_batch.OpenAIBatchAdapter, "client", forbidden)
    preview = preview_prompt(spec, item_id="000", trait_id="000")
    assert isinstance(preview, RenderedProductionPromptV1)
    store = tmp_path / "store"
    prepare_batch(spec, store=store, run_id="001")
    adapter = openai_batch.OpenAIBatchAdapter(batch_context(spec))
    with open_prepared_run(
        "001", store=store, spec=spec, capabilities=adapter.capabilities(spec)
    ) as prepared:
        shard = prepared.repository.connection.execute(
            "SELECT shard_id FROM batch_preparations"
        ).fetchone()[0]
        member = next(members_for(prepared, shard))
        _, wire = reconstruct(prepared, adapter, member)
        assert isinstance(wire, PreparedOpenAIBatchRequest)
        assert wire.endpoint == endpoint and wire.preparation_version == 10
        assert preview.output_commitment == wire.output_commitment
        assert bytes_digest(preview.text.encode("utf-8")) == wire.prompt_sha256
        assert preview.output_commitment.selection.authority_kind == "unqualified"
    assert not service.events()


@pytest.mark.parametrize(
    "case", ["openai-local", "openai-explicit-mock", "mock-explicit-mock"]
)
def test_local_batch_status_keeps_provider_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    from test_cli_lifecycle import control
    from test_native_batch import batch_spec

    from judgmentllm import openai_batch_transport
    from judgmentllm.execution import openai_batch as execution
    from judgmentllm.persistence.ownership import executor_is_active
    from judgmentllm.providers import openai_batch

    service = Service(tmp_path / "service.sqlite")
    install(monkeypatch, service)
    spec = (
        batch_spec(tmp_path / "inputs")
        if case == "mock-explicit-mock"
        else specification(tmp_path, responses=1)
    )
    store = tmp_path / "store"
    prepare_batch(spec, store=store, run_id="001")
    with ReadOnlyRepository(store) as repo:
        original = repo.summary("001")
        manifest = (repo.run_directory("001") / "manifest.json").read_bytes()

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("local status reached OpenAI SDK or credentials")

    monkeypatch.setattr(openai_batch_transport, "require_sdk", forbidden)
    monkeypatch.setattr(openai_batch, "require_sdk", forbidden)
    monkeypatch.setattr(execution, "require_sdk", forbidden)
    monkeypatch.setattr(openai_batch, "_resolve_credentials", forbidden)
    arguments = ["batch", "status", "--run", "001", "--store", str(store)]
    if case != "openai-local":
        arguments += ["--mock-max-members", "1000"]
    rejected = case == "openai-explicit-mock"
    envelope, _ = control(arguments, 2 if rejected else 0)
    if rejected:
        assert envelope["error"]["category"] == "validation"
        assert envelope["error"]["message"] == (
            "mock controls cannot configure an OpenAI Batch run"
        )
    else:
        assert envelope["run_id"] == "001" and envelope["command"] == "batch status"
        assert envelope["error"] is None
        assert envelope["data"]["batch"]["accepted_jobs"] == 0
    with ReadOnlyRepository(store) as repo:
        assert repo.summary("001") == original
        assert (repo.run_directory("001") / "manifest.json").read_bytes() == manifest
        assert not executor_is_active(repo, "001")
    assert not service.events()


@pytest.mark.parametrize(
    "version,accepted",
    [
        pytest.param(10, True, id="valid-int"),
        pytest.param(9, False, id="wrong-int"),
        pytest.param(True, False, id="boolean"),
        pytest.param(10.0, False, id="float"),
        pytest.param("10", False, id="string"),
    ],
)
def test_prepared_batch_version_requires_exact_integer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    version: object,
    accepted: bool,
) -> None:
    from pydantic import ValidationError

    from judgmentllm.openai_batch_transport import (
        PreparedOpenAIBatchRequest,
        batch_context,
    )
    from judgmentllm.preparation import open_prepared_run
    from judgmentllm.preparation.batch import members_for, reconstruct
    from judgmentllm.providers.openai_batch import OpenAIBatchAdapter

    service = Service(tmp_path / "service.sqlite")
    install(monkeypatch, service)
    spec = specification(tmp_path, responses=1)
    store = tmp_path / "store"
    prepare_batch(spec, store=store, run_id="001")
    adapter = OpenAIBatchAdapter(batch_context(spec))
    with open_prepared_run(
        "001", store=store, spec=spec, capabilities=adapter.capabilities(spec)
    ) as prepared:
        shard = prepared.repository.connection.execute(
            "SELECT shard_id FROM batch_preparations"
        ).fetchone()[0]
        _, wire = reconstruct(prepared, adapter, next(members_for(prepared, shard)))
        assert isinstance(wire, PreparedOpenAIBatchRequest)
        data = {**wire.model_dump(), "preparation_version": version}
        if accepted:
            validated = PreparedOpenAIBatchRequest.model_validate(data)
            assert type(validated.preparation_version) is int
            assert validated.preparation_version == 10 and validated == wire
        else:
            with pytest.raises(
                ValidationError, match="preparation version must be integer 10"
            ) as error:
                PreparedOpenAIBatchRequest.model_validate(data)
            assert [entry["loc"] for entry in error.value.errors()] == [
                ("preparation_version",)
            ]
    assert not service.events()


def test_batch_compile_rejects_mismatched_render_commitment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from judgmentllm.openai_batch_transport import batch_context, compile_request
    from judgmentllm.preparation import open_prepared_run
    from judgmentllm.preparation.batch import members_for, reconstruct
    from judgmentllm.production_prompts import RenderedProductionPromptV1
    from judgmentllm.providers.openai_batch import OpenAIBatchAdapter

    service = Service(tmp_path / "service.sqlite")
    install(monkeypatch, service)
    spec = specification(tmp_path, responses=1)
    store = tmp_path / "store"
    prepare_batch(spec, store=store, run_id="001")
    adapter = OpenAIBatchAdapter(batch_context(spec))
    with open_prepared_run(
        "001", store=store, spec=spec, capabilities=adapter.capabilities(spec)
    ) as prepared:
        shard = prepared.repository.connection.execute(
            "SELECT shard_id FROM batch_preparations"
        ).fetchone()[0]
        dependency, wire = reconstruct(
            prepared, adapter, next(members_for(prepared, shard))
        )
        rendered = prepared.render_attempt(dependency)
        assert isinstance(rendered, RenderedProductionPromptV1)
        assert compile_request(spec, dependency, rendered) == wire
        assert rendered.output_commitment.context_sha256 != "0" * 64
        changed = RenderedProductionPromptV1.model_validate(
            {
                **rendered.model_dump(),
                "output_commitment": {
                    **rendered.output_commitment.model_dump(),
                    "context_sha256": "0" * 64,
                },
            }
        )
        assert changed.text == rendered.text and changed != rendered
        with pytest.raises(ValueError, match="Batch prompt/output commitment differs"):
            compile_request(spec, dependency, changed)
    assert not service.events()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "unowned",
        "route",
        "authorization",
        "forbidden-header",
        "multipart-bound",
        "duplicate-part",
        "body",
        "repeat",
    ],
)
async def test_sdk_control_guard_blocks_unapproved_forward(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    from email.message import Message

    import httpx2

    from judgmentllm import BatchControlOptionsV1, asubmit_batch
    from judgmentllm.execution.native_batch import InvocationBudget
    from judgmentllm.lifecycle_models import LifecycleError
    from judgmentllm.openai_batch_transport import batch_context
    from judgmentllm.providers.openai_batch import OpenAIBatchAdapter
    from judgmentllm.providers.protocol import ProviderIntegrityError

    service = Service(tmp_path / "service.sqlite")
    install(monkeypatch, service)
    spec = specification(tmp_path, responses=1)
    file_id = "file-output_guard"
    with service.connect() as db:
        db.execute(
            "INSERT INTO files VALUES (?,?,?,?,?)",
            (file_id, "batch_output", "guard.jsonl", b"", 0),
        )
    upload = case in ("multipart-bound", "duplicate-part")
    store = tmp_path / "store"
    if upload:
        prepare_batch(spec, store=store, run_id="001")
        service.configure(store, spec)
    messages = {
        "unowned": "OpenAI Batch request has no operation authorization",
        "route": "OpenAI Batch routing differs",
        "authorization": "OpenAI Batch authorization differs",
        "forbidden-header": "OpenAI Batch headers or path differ",
        "multipart-bound": "OpenAI Batch multipart bound differs",
        "duplicate-part": "duplicate OpenAI Batch multipart field",
        "body": "OpenAI Batch body differs from authorized bytes",
        "repeat": "OpenAI Batch forwarding was already consumed",
    }
    original_send = httpx2.AsyncClient.send

    async def changed_send(self: Any, request: Any, **kwargs: Any) -> Any:
        if case == "repeat":
            first = await original_send(self, request, **kwargs)
            try:
                return await original_send(self, request, **kwargs)
            finally:
                await first.aclose()
        if case == "unowned":
            return await original_send(self, request, **kwargs)
        body = await request.aread()
        headers = httpx2.Headers(request.headers)
        url = request.url
        if case == "route":
            url = url.copy_with(path=url.path + "/unapproved")
        elif case == "authorization":
            headers["authorization"] = "Bearer mismatched-synthetic"
        elif case == "forbidden-header":
            headers["cookie"] = "synthetic=1"
        elif case == "multipart-bound":
            body += b"x" * 16_385
        elif case == "duplicate-part":
            mime = Message()
            mime["content-type"] = headers["content-type"]
            boundary = mime.get_param("boundary")
            assert isinstance(boundary, str) and boundary
            delimiter = b"--" + boundary.encode("ascii")
            closing = delimiter + b"--\r\n"
            assert body.endswith(closing) and body.count(b'name="purpose"') == 1
            part = (
                delimiter
                + b'\r\nContent-Disposition: form-data; name="purpose"\r\n'
                + b"\r\nbatch\r\n"
            )
            body = body[: -len(closing)] + part + closing
            assert body.count(b'name="purpose"') == 2
        else:
            assert case == "body"
            body = b"{}"
        if case in ("multipart-bound", "duplicate-part", "body"):
            headers.pop("content-length", None)
            headers.pop("transfer-encoding", None)
        changed = httpx2.Request(
            request.method,
            url,
            headers=headers,
            content=body,
            extensions=request.extensions,
        )
        return await original_send(self, changed, **kwargs)

    adapter = OpenAIBatchAdapter(
        batch_context(spec),
        budget=InvocationBudget(BatchControlOptionsV1(max_io_steps=64)),
    )
    try:
        with monkeypatch.context() as patch:
            patch.setattr(httpx2.AsyncClient, "send", changed_send)
            if upload:
                with pytest.raises(LifecycleError) as error:
                    await asubmit_batch("001", store=store, spec=spec)
                assert error.value.category == "execution"
                context = error.value.__context__
                assert isinstance(context, ProviderIntegrityError)
                assert context.args == (messages[case],)
            else:
                with pytest.raises(ProviderIntegrityError) as error:
                    if case == "unowned":
                        client = await adapter.client()
                        await client.files.with_raw_response.retrieve(file_id)
                    else:
                        await adapter.file_metadata(file_id)
                assert error.value.args == (messages[case],)
        forwarded = 1 if case == "repeat" else 0
        expected = [("GET", "/v1/files/" + file_id)]
        assert service.events() == expected * forwarded
        assert await adapter.file_metadata(file_id) == (file_id, "batch_output", 0)
        assert service.events() == expected * (forwarded + 1)
    finally:
        await adapter.aclose()


@pytest.mark.asyncio
async def test_encoded_control_response_closes_without_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import gzip
    from collections.abc import AsyncIterator

    import httpx2

    from judgmentllm import BatchControlOptionsV1, asubmit_batch
    from judgmentllm.batch_models import BatchOperationReceiptV1
    from judgmentllm.execution.native_batch import InvocationBudget
    from judgmentllm.openai_batch_transport import batch_context
    from judgmentllm.persistence.native_batch import read_reference
    from judgmentllm.persistence.ownership import executor_is_active
    from judgmentllm.providers.openai_batch import OpenAIBatchAdapter

    service = Service(tmp_path / "service.sqlite")
    install(monkeypatch, service)
    spec = specification(tmp_path, responses=1)
    store = tmp_path / "store"
    prepare_batch(spec, store=store, run_id="001")
    service.configure(store, spec)
    state = {"reads": 0, "closes": 0}

    class EncodedBody(httpx2.AsyncByteStream):
        def __init__(self, raw: bytes) -> None:
            self.raw = gzip.compress(raw)

        async def __aiter__(self) -> AsyncIterator[bytes]:
            state["reads"] += 1
            yield self.raw

        async def aclose(self) -> None:
            state["closes"] += 1

    original_request = service.request
    encoded = True

    def response(request: Any) -> Any:
        nonlocal encoded
        result = original_request(request)
        if encoded:
            assert (request.method, request.url.path) == ("POST", "/v1/files")
            encoded = False
            raw = result.read()
            result.close()
            return httpx2.Response(
                200,
                headers={
                    "content-type": "application/json",
                    "content-encoding": "gzip",
                },
                stream=EncodedBody(raw),
            )
        return result

    monkeypatch.setattr(service, "request", response)
    result = await asubmit_batch("001", store=store, spec=spec)
    assert result.outcome == "partial"
    assert result.batch.accepted_jobs == 0 and result.batch.unresolved_operations == 1
    assert not encoded and state == {"reads": 0, "closes": 1}
    assert service.events() == [("POST", "/v1/files")]
    adapter = OpenAIBatchAdapter(
        batch_context(spec),
        budget=InvocationBudget(BatchControlOptionsV1(max_io_steps=64)),
    )
    try:
        assert await adapter.file_metadata("file-input_1", purpose="batch") == (
            "file-input_1",
            "batch",
            service.file("file-input_1")[1],
        )
    finally:
        await adapter.aclose()
    assert service.events() == [
        ("POST", "/v1/files"),
        ("GET", "/v1/files/file-input_1"),
    ]
    assert state == {"reads": 0, "closes": 1}
    with ReadOnlyRepository(store) as repo:
        row = repo.connection.execute(
            "SELECT instance_id FROM batch_operations WHERE kind='upload'"
        ).fetchone()
        assert row is not None
        assert (
            read_reference(
                repo,
                "001",
                "batch/operations/" + row[0] + ".json",
                BatchOperationReceiptV1,
            )
            is None
        )
        assert not repo.connection.execute(
            "SELECT 1 FROM batch_facts WHERE kind='operation'"
        ).fetchone()
        assert repo.summary("001").results == 0
        assert (
            repo.connection.execute(
                "SELECT count(*) FROM batch_member_receipts"
            ).fetchone()[0]
            == 0
        )
        assert not executor_is_active(repo, "001")


def _sdk_operation_evidence(store: Path, kind: str) -> dict[str, Any]:
    from judgmentllm.persistence.ownership import executor_is_active

    with ReadOnlyRepository(store) as repo:
        journal = tuple(
            tuple(row)
            for row in repo.connection.execute(
                "SELECT kind,instance_id,intent_sha256,record_json "
                "FROM batch_operations WHERE run_id='001' ORDER BY rowid"
            )
        )
        intents = [row for row in journal if row[0] == kind]
        assert len(intents) == 1
        intent = intents[0]
        receipts = [
            row[0]
            for row in repo.connection.execute(
                "SELECT record_json FROM batch_facts WHERE run_id='001' "
                "AND kind='operation' AND entity_id=? ORDER BY fact_seq",
                (intent[1],),
            )
        ]
        assert len(receipts) <= 1
        submissions = tuple(
            tuple(row)
            for row in repo.connection.execute(
                "SELECT submission_id,remote_id,certainty FROM submissions "
                "WHERE run_id='001' ORDER BY submission_id"
            )
        )
        directory = repo.run_directory("001")
        reference = directory / "batch" / "operations" / (intent[1] + ".json")
        assert not executor_is_active(repo, "001")
        return {
            "journal": journal,
            "intent": intent,
            "receipt": receipts[0] if receipts else None,
            "reference": reference.read_bytes() if reference.exists() else None,
            "submissions": submissions,
            "accepted": tuple((s[0], s[1]) for s in submissions if s[2] == "accepted"),
            "results": repo.connection.execute(
                "SELECT count(*) FROM results WHERE run_id='001'"
            ).fetchone()[0],
            "members": repo.connection.execute(
                "SELECT count(*) FROM batch_member_receipts WHERE run_id='001'"
            ).fetchone()[0],
            "manifest": (directory / "manifest.json").read_bytes(),
        }


@pytest.mark.parametrize(
    "case", ["upload-filename", "create-input", "cancel-id", "create-output-file"]
)
def test_sdk_control_ack_requires_original_operation_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    import httpx2

    from judgmentllm import cancel_batch
    from judgmentllm.persistence.hashing import bytes_digest

    service = Service(tmp_path / "service.sqlite")
    install(monkeypatch, service)
    spec = specification(tmp_path, responses=1)
    store = tmp_path / "store"
    prepare_batch(spec, store=store, run_id="001")
    service.configure(store, spec)
    kind = {
        "upload-filename": "upload",
        "create-input": "create",
        "cancel-id": "cancel",
        "create-output-file": "create",
    }[case]
    field = {
        "upload-filename": "filename",
        "create-input": "input_file_id",
        "cancel-id": "id",
        "create-output-file": "output_file_id",
    }[case]
    initial_acceptance = None
    if case == "cancel-id":
        service.set("initial_status", "in_progress")
        initial = submit_batch("001", store=store, spec=spec)
        assert initial.batch.accepted_jobs == 1
        initial_acceptance = _sdk_operation_evidence(store, "create")
        remote = initial_acceptance["accepted"][0][1]
        target = ("POST", "/v1/batches/" + remote + "/cancel")
        with service.connect() as db:
            assert (
                json.loads(
                    db.execute(
                        "SELECT body FROM jobs WHERE id=?", (remote,)
                    ).fetchone()[0]
                )["status"]
                == "in_progress"
            )
    else:
        target = ("POST", "/v1/files" if case == "upload-filename" else "/v1/batches")
    original_request = service.request
    mutations: list[tuple[dict[str, object], dict[str, object]]] = []
    replacements: list[httpx2.Response] = []

    def changed(request: httpx2.Request) -> httpx2.Response:
        response: httpx2.Response = original_request(request)
        if (request.method, request.url.path) != target or mutations:
            return response
        headers = dict(response.headers)
        headers.pop("content-length", None)
        try:
            body: dict[str, object] = json.loads(response.read())
        finally:
            response.close()
        replacement_body = dict(body)
        if case == "upload-filename":
            filename = body["filename"]
            assert isinstance(filename, str)
            replacement_body["filename"] = filename + ".unrelated"
        elif case == "create-input":
            replacement_body["input_file_id"] = "file-unrelated_input"
        elif case == "create-output-file":
            replacement_body["output_file_id"] = "invalid-optional-file"
        else:
            assert case == "cancel-id"
            remote_id = body["id"]
            assert isinstance(remote_id, str)
            replacement_body["id"] = remote_id + "_unrelated"
        replacement = httpx2.Response(
            response.status_code, headers=headers, json=replacement_body
        )
        replacement.close()
        mutations.append((body, replacement_body))
        replacements.append(replacement)
        return replacement

    with monkeypatch.context() as patch:
        patch.setattr(service, "request", changed)
        first = (
            cancel_batch("001", store=store, spec=spec)
            if case == "cancel-id"
            else submit_batch("001", store=store, spec=spec)
        )
    assert service.request == original_request
    assert len(mutations) == 1 and all(r.is_closed for r in replacements)
    original_ack, changed_ack = mutations[0]
    assert original_ack.keys() == changed_ack.keys()
    assert {k for k in original_ack if original_ack[k] != changed_ack[k]} == {field}
    genuine_remote = original_ack["id"]
    assert isinstance(genuine_remote, str)
    assert first.outcome == "partial" and first.batch.unresolved_operations == 1
    assert first.batch.accepted_jobs == (1 if case == "cancel-id" else 0)
    before = _sdk_operation_evidence(store, kind)
    assert before["receipt"] is None and before["reference"] is None
    with ReadOnlyRepository(store) as repo:
        ack = repo.run_directory("001") / "batch" / "openai-ack" / before["intent"][1]
        assert not ack.exists()
    assert before["results"] == before["members"] == 0
    if case == "cancel-id":
        assert initial_acceptance is not None
        assert before["accepted"] == initial_acceptance["accepted"]
        assert before["manifest"] == initial_acceptance["manifest"]
    else:
        assert not before["accepted"]
    if case == "upload-filename":
        raw, declared = service.file(genuine_remote)
        assert len(raw) == declared == original_ack["bytes"] == changed_ack["bytes"]
        assert original_ack["purpose"] == changed_ack["purpose"] == "batch"
        assert bytes_digest(raw) == json.loads(before["intent"][3])["payload_sha256"]
    with service.connect() as db:
        if case != "upload-filename":
            assert [row[0] for row in db.execute("SELECT id FROM jobs")] == [
                genuine_remote
            ]
    posts = [event for event in service.events() if event[0] == "POST"]
    assert posts == [("POST", "/v1/files")] + (
        [("POST", "/v1/batches")] if case != "upload-filename" else []
    ) + ([target] if case == "cancel-id" else [])
    marker = len(service.events())
    if case == "cancel-id":
        recovered = poll_batch("001", store=store)
        requests: list[tuple[str, str]] = service.events()[marker:]
        assert requests and all(
            method == "GET" and path == "/v1/batches/" + genuine_remote
            for method, path in requests
        )
        repeated = cancel_batch("001", store=store, spec=spec)
        assert repeated.batch.unresolved_operations == 0
    else:
        recovered = resume_batch("001", store=store, spec=spec)
    assert recovered.batch.accepted_jobs == 1
    assert recovered.batch.unresolved_operations == 0 and not recovered.batch.conflicts
    after = _sdk_operation_evidence(store, kind)
    assert after["intent"] == before["intent"]
    assert after["manifest"] == before["manifest"]
    assert after["receipt"] is not None
    assert after["reference"] == after["receipt"].encode()
    receipt = json.loads(after["receipt"])
    assert (receipt["instance_id"], receipt["intent_sha256"], receipt["remote_id"]) == (
        before["intent"][1],
        before["intent"][2],
        genuine_remote,
    )
    assert receipt["outcome"] == "accepted" and receipt["fence"] is None
    if case == "upload-filename":
        create_intents = [row for row in after["journal"] if row[0] == "create"]
        assert len(create_intents) == 1
        with service.connect() as db:
            jobs = db.execute("SELECT id,body FROM jobs").fetchall()
        assert len(jobs) == 1
        job = json.loads(jobs[0][1])
        assert job["input_file_id"] == genuine_remote
        assert job["metadata"]["judgmentllm_operation"] == create_intents[0][1]
        assert after["accepted"] == ((create_intents[0][1], jobs[0][0]),)
        assert [event for event in service.events() if event[0] == "POST"] == posts + [
            ("POST", "/v1/batches")
        ]
    else:
        assert after["journal"] == before["journal"]
        assert [s[0] for s in after["submissions"]] == [
            s[0] for s in before["submissions"]
        ]
        assert after["accepted"] == ((before["submissions"][0][0], genuine_remote),)
        assert [event for event in service.events() if event[0] == "POST"] == posts
    if case == "cancel-id":
        assert after["accepted"] == before["accepted"]
        assert after["results"] == after["members"] == 0
        assert service.events().count(target) == 1


def test_uncertain_create_requires_complete_consistent_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, ack_body in (("malformed", b"{"), ("non-object", b"[]")):
        directory = tmp_path / name
        directory.mkdir()
        with monkeypatch.context() as patch:
            _assert_uncertain_create_discovery(directory, patch, ack_body)


def _assert_uncertain_create_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ack_body: bytes
) -> None:
    import httpx2

    service = Service(tmp_path / "service.sqlite")
    install(monkeypatch, service)
    spec = specification(tmp_path, responses=1)
    store = tmp_path / "store"
    prepare_batch(spec, store=store, run_id="001")
    service.configure(store, spec)
    original_request = service.request
    phase: str
    remote: str | None = None
    mutations: list[str] = []
    replacements: list[httpx2.Response] = []

    def changed(request: httpx2.Request) -> httpx2.Response:
        nonlocal remote
        response: httpx2.Response = original_request(request)
        key = (request.method, request.url.path)
        mutate_ack = phase == "ack" and key == ("POST", "/v1/batches")
        mutate_list = phase == "listing" and key == ("GET", "/v1/batches")
        mutate_retrieve = (
            phase == "disagree"
            and remote is not None
            and key == ("GET", "/v1/batches/" + remote)
        )
        if not (mutate_ack or mutate_list or mutate_retrieve):
            return response
        headers = dict(response.headers)
        headers.pop("content-length", None)
        try:
            body = json.loads(response.read())
        finally:
            response.close()
        if mutate_ack:
            assert response.status_code == 200
            assert isinstance(body["id"], str)
            remote = body["id"]
            replacement = httpx2.Response(200, headers=headers, content=ack_body)
        elif mutate_list:
            assert body["has_more"] is False
            assert len(body["data"]) == 1 and body["data"][0]["id"] == remote
            body["data"] = [None]
            replacement = httpx2.Response(
                response.status_code, headers=headers, json=body
            )
        else:
            assert body["id"] == remote and body["status"] == "completed"
            body["status"] = "finalizing"
            replacement = httpx2.Response(
                response.status_code, headers=headers, json=body
            )
        replacement.close()
        mutations.append(phase)
        replacements.append(replacement)
        return replacement

    phase = "ack"
    with monkeypatch.context() as patch:
        patch.setattr(service, "request", changed)
        first = submit_batch("001", store=store, spec=spec)
        assert first.outcome == "partial" and first.batch.unresolved_operations == 1
        assert first.batch.accepted_jobs == 0
        assert isinstance(remote, str)
        before = _sdk_operation_evidence(store, "create")
        assert before["receipt"] is None and before["reference"] is None
        assert not before["accepted"]
        assert before["results"] == before["members"] == 0
        assert mutations == ["ack"]
        with service.connect() as db:
            assert [row[0] for row in db.execute("SELECT id FROM jobs")] == [remote]
        failed_discoveries = ()
        if ack_body == b"{":
            failed_discoveries = (
                ("listing", [("GET", "/v1/batches")]),
                (
                    "disagree",
                    [("GET", "/v1/batches"), ("GET", "/v1/batches/" + remote)],
                ),
            )
        for _phase, expected_requests in failed_discoveries:
            phase = _phase
            marker = len(service.events())
            uncertain = resume_batch("001", store=store, spec=spec)
            assert uncertain.outcome == "partial"
            assert uncertain.batch.unresolved_operations == 1
            assert uncertain.batch.accepted_jobs == 0
            assert service.events()[marker:] == expected_requests
            current = _sdk_operation_evidence(store, "create")
            for key in ("journal", "intent", "manifest", "submissions"):
                assert current[key] == before[key]
            assert current["receipt"] is None and current["reference"] is None
            assert not current["accepted"]
            assert current["results"] == current["members"] == 0
        assert mutations == ["ack", *(phase for phase, _ in failed_discoveries)]
    assert service.request == original_request
    assert all(r.is_closed for r in replacements)
    assert isinstance(remote, str)
    marker = len(service.events())
    recovered = resume_batch("001", store=store, spec=spec)
    assert recovered.batch.accepted_jobs == 1
    assert recovered.batch.unresolved_operations == 0
    assert ("GET", "/v1/batches") in service.events()[marker:]
    assert ("GET", "/v1/batches/" + remote) in service.events()[marker:]
    after = _sdk_operation_evidence(store, "create")
    for key in ("journal", "intent", "manifest"):
        assert after[key] == before[key]
    assert after["receipt"] is not None
    assert after["reference"] == after["receipt"].encode()
    receipt = json.loads(after["receipt"])
    assert (receipt["instance_id"], receipt["intent_sha256"], receipt["remote_id"]) == (
        before["intent"][1],
        before["intent"][2],
        remote,
    )
    assert receipt["outcome"] == "accepted" and receipt["fence"] is None
    assert after["accepted"] == ((before["intent"][1], remote),)
    assert [s[0] for s in after["submissions"]] == [s[0] for s in before["submissions"]]
    final = recovered
    for _ in range(2):
        if not final.batch.recovery_required:
            break
        final = collect_batch("001", store=store, spec=spec)
    assert final.outcome == "ok" and not final.batch.recovery_required
    completed = _sdk_operation_evidence(store, "create")
    assert completed["results"] == completed["members"] == 1
    assert completed["journal"] == before["journal"]
    assert completed["intent"] == before["intent"]
    assert completed["manifest"] == before["manifest"]
    assert completed["accepted"] == ((before["intent"][1], remote),)
    assert completed["receipt"] == after["receipt"]
    assert completed["reference"] == after["reference"]
    with service.connect() as db:
        assert [row[0] for row in db.execute("SELECT id FROM jobs")] == [remote]
    assert [event for event in service.events() if event[0] == "POST"] == [
        ("POST", "/v1/files"),
        ("POST", "/v1/batches"),
    ]


@pytest.mark.parametrize(
    "case",
    [
        "manifest-version",
        "manifest-integer",
        "manifest-scope",
        "source-binding",
        "audit-version",
        "audit-report-version",
    ],
)
def test_batch_documents_revalidate_their_original_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    from pydantic import ValidationError
    from test_openai_batch_recovery import (
        accepted_chain_snapshot,
        assert_accepted_chain_unchanged,
        setup,
    )

    from judgmentllm import BatchControlOptionsV1, export_batch_provenance
    from judgmentllm.batch_models import OpenAIBatchSourceBindingV1
    from judgmentllm.execution.batch_exports import (
        BatchAuditManifestV2,
        BatchAuditReportV2,
    )
    from judgmentllm.persistence.hashing import canonical_bytes
    from judgmentllm.preparation.manifest import ExecutableRunManifestV13

    service, spec, store = setup(tmp_path, monkeypatch, responses=1)
    pending = collect_batch(
        "001", store=store, spec=spec, options=BatchControlOptionsV1(max_io_steps=16)
    )
    assert pending.collection is not None
    assert "collection_budget_unfit" in pending.collection.codes
    report = export_batch_provenance("001", store=store)
    with ReadOnlyRepository(store) as repo:
        raw_manifest = (repo.run_directory("001") / "manifest.json").read_bytes()
        raw_binding = repo.connection.execute(
            "SELECT record_json FROM batch_facts "
            "WHERE entity_id LIKE 'openai-binding:%' ORDER BY fact_seq LIMIT 1"
        ).fetchone()[0]
        assert repo.summary("001").results == 0
    manifest = ExecutableRunManifestV13.model_validate_json(raw_manifest)
    binding = OpenAIBatchSourceBindingV1.model_validate_json(raw_binding)
    assert canonical_bytes(manifest) == raw_manifest
    assert canonical_bytes(binding).decode() == raw_binding
    restored = BatchAuditManifestV2.model_validate(report.manifest.model_dump())
    assert restored == report.manifest
    assert BatchAuditReportV2.model_validate(report.model_dump()) == report
    before = accepted_chain_snapshot(store)
    events = service.events()
    if case.startswith("manifest-"):
        model = ExecutableRunManifestV13
        fields = manifest.model_dump(mode="json")
        if case == "manifest-version":
            fields["schema_version"] = 12
            message = "schema_version must be integer 13"
        elif case == "manifest-integer":
            fields["run_spec_version"] = "3"
            message = "batch versions must be integers"
        else:
            assert model.model_validate_json(json.dumps(fields)) == manifest
            fields["batch_context"]["model"] += "-different"
            message = "OpenAI Batch seal has incompatible scope"
    elif case == "source-binding":
        model = OpenAIBatchSourceBindingV1
        fields = binding.model_dump(mode="json")
        assert model.model_validate_json(json.dumps(fields)) == binding
        other = "error" if binding.ready.role == "output" else "output"
        fields["file_id"] = getattr(binding.job, other + "_file_id")
        assert fields["file_id"] != binding.file_id
        message = None
    else:
        model = BatchAuditManifestV2 if case == "audit-version" else BatchAuditReportV2
        original = report.manifest if case == "audit-version" else report
        fields = original.model_dump(mode="json")
        assert model.model_validate_json(json.dumps(fields)) == original
        fields["schema_version"] = "2"
        message = None
    with pytest.raises(ValidationError, match=message) as error:
        if case in ("manifest-version", "manifest-integer"):
            model.model_validate(fields)
        else:
            model.model_validate_json(json.dumps(fields))
    if case in ("source-binding", "audit-version", "audit-report-version"):
        assert error.value.errors(
            include_url=False, include_input=False, include_context=False
        ) == [
            {"type": "evidence_invalid", "loc": (), "msg": "Invalid evidence record."}
        ]
    assert service.events() == events
    assert_accepted_chain_unchanged(store, before)


def test_batch_input_planning_never_constructs_a_provider_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from judgmentllm import estimate_run, validate_inputs
    from judgmentllm.providers import openai_batch

    service = Service(tmp_path / "service.sqlite")
    install(monkeypatch, service)
    spec = specification(tmp_path, responses=2)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(
            "offline planning attempted a provider client or credential"
        )

    monkeypatch.setattr(openai_batch, "_resolve_credentials", forbidden)
    monkeypatch.setattr(openai_batch.OpenAIBatchAdapter, "client", forbidden)
    inputs = validate_inputs(spec)
    assert inputs.valid and inputs.response_count == 2
    plan = estimate_run(spec)
    assert plan.logical_tasks == 2 and plan.requests > 0
    assert spec.execution.mode == "batch_native"
    assert not service.events()
    assert not (tmp_path / "store").exists()


def test_v3_live_preparation_requires_explicit_transport_without_sending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from judgmentllm.config import validate_run_spec
    from judgmentllm.preparation import PreparationError, prepare_run
    from judgmentllm.providers import openai_batch

    service = Service(tmp_path / "service.sqlite")
    install(monkeypatch, service)
    fields = specification(tmp_path, responses=1).model_dump(mode="json")
    fields["execution"]["mode"] = "live"
    spec = validate_run_spec(fields)
    assert spec.schema_version == 3 and spec.execution.mode == "live"

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("missing-context preparation attempted provider work")

    monkeypatch.setattr(openai_batch, "_resolve_credentials", forbidden)
    with pytest.raises(
        PreparationError, match="native-capable preparation requires transport context"
    ):
        with prepare_run(spec, store=tmp_path / "store", run_id="001"):
            pytest.fail("live preparation acquired implicit transport authority")
    with ReadOnlyRepository(tmp_path / "store") as repo:
        assert (
            repo.connection.execute("SELECT count(*) FROM submissions").fetchone()[0]
            == 0
        )
    assert not service.events()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "route",
        "scope",
        "unbounded",
        "receipt",
        "payload",
        "legacy-source",
        "legacy-index",
        "legacy-results",
    ],
)
async def test_batch_adapter_refusals_preserve_owned_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    from test_openai_batch_recovery import (
        accepted_chain_snapshot,
        assert_accepted_chain_unchanged,
    )

    from judgmentllm import BatchControlOptionsV1, asubmit_batch
    from judgmentllm.batch_models import BatchOperationIntentV1
    from judgmentllm.config import validate_run_spec
    from judgmentllm.execution.native_batch import InvocationBudget
    from judgmentllm.openai_batch_transport import batch_context
    from judgmentllm.providers.openai_batch import OpenAIBatchAdapter
    from judgmentllm.providers.protocol import ProviderIntegrityError

    service = Service(tmp_path / "service.sqlite")
    install(monkeypatch, service)
    spec = specification(tmp_path, responses=1)
    store = tmp_path / "store"
    prepare_batch(spec, store=store, run_id="001")
    service.configure(store, spec)
    submitted = await asubmit_batch("001", store=store, spec=spec)
    assert submitted.batch.accepted_jobs == 1
    with ReadOnlyRepository(store) as repo:
        intents = {
            row[0]: BatchOperationIntentV1.model_validate_json(row[1])
            for row in repo.connection.execute(
                "SELECT kind,record_json FROM batch_operations"
            )
        }
        upload = intents["upload"]
        receipt = repo.connection.execute(
            "SELECT record_json FROM batch_facts "
            "WHERE kind='operation' AND entity_id=?",
            (upload.instance_id,),
        ).fetchone()[0]
    before = accepted_chain_snapshot(store)
    events = service.events()
    budget = InvocationBudget(BatchControlOptionsV1())
    adapter = OpenAIBatchAdapter(
        batch_context(spec), budget=None if case == "unbounded" else budget
    )
    try:
        if case == "route":
            with pytest.raises(ValueError, match="unsupported OpenAI Batch route"):
                adapter.capabilities_for("mock", spec.endpoint, spec.model)
        elif case == "scope":
            fields = spec.model_dump(mode="json")
            fields["endpoint"] = "chat.completions"
            other = validate_run_spec(fields)
            with pytest.raises(ValueError, match="OpenAI Batch scope differs"):
                adapter.validate(other)
        elif case == "unbounded":
            with pytest.raises(
                ProviderIntegrityError, match="requires a bounded invocation"
            ):
                await adapter.client()
        elif case == "receipt":
            with pytest.raises(
                ProviderIntegrityError, match="receipt differs from original intent"
            ):
                adapter.decode_receipt(receipt, intents["create"])
        elif case == "payload":
            raw = service.file("file-input_1")[0]
            with pytest.raises(
                ProviderIntegrityError, match="operation payload differs"
            ):
                await adapter.invoke(
                    upload,
                    budget.payload(raw + b"\n"),
                    lambda: pytest.fail("forwarded invalid input"),
                )
        elif case == "legacy-source":
            with pytest.raises(
                ProviderIntegrityError, match="uses pinned Files sources"
            ):
                await adapter.result_source()
        elif case == "legacy-index":
            with pytest.raises(
                ProviderIntegrityError, match="has no provider prefix index"
            ):
                await adapter.result_index()
        else:
            assert case == "legacy-results"
            with pytest.raises(
                ProviderIntegrityError, match="requires screened full-file replay"
            ):
                adapter.results()
    finally:
        await adapter.aclose()
    assert service.events() == events
    assert_accepted_chain_unchanged(store, before)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "message"),
    [
        pytest.param(
            "create", "OpenAI Batch create binding differs", id="create-binding"
        ),
        pytest.param("cancel", "OpenAI Batch cancel body differs", id="cancel-body"),
    ],
)
async def test_sdk_mutation_payload_rejection_preserves_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str, message: str
) -> None:
    from judgmentllm import BatchControlOptionsV1, asubmit_batch
    from judgmentllm.batch_models import BatchOperationIntentV1, OpenAIBatchShardV1
    from judgmentllm.execution.native_batch import InvocationBudget
    from judgmentllm.execution.openai_batch import OpenAIBatchSession
    from judgmentllm.openai_batch_transport import batch_context
    from judgmentllm.persistence.artifacts import ArtifactStore
    from judgmentllm.persistence.hashing import bytes_digest, canonical_bytes
    from judgmentllm.persistence.native_batch import rows
    from judgmentllm.providers.openai_batch import OpenAIBatchAdapter
    from judgmentllm.providers.protocol import ProviderIntegrityError

    service = Service(tmp_path / "service.sqlite")
    install(monkeypatch, service)
    spec = specification(tmp_path, responses=1)
    store = tmp_path / "store"
    prepare_batch(spec, store=store, run_id="001")
    service.configure(store, spec)
    if kind == "cancel":
        service.set("initial_status", "in_progress")
        submitted = await asubmit_batch("001", store=store, spec=spec)
        assert submitted.batch.accepted_jobs == 1
    budget = InvocationBudget(BatchControlOptionsV1())
    adapter = OpenAIBatchAdapter(batch_context(spec), budget=budget)
    journal: list[tuple[str, str, str, str]] = []

    def checkpoint(stage: str, identity: str) -> None:
        if stage == "after_batch_intent":
            row = session.repository.connection.execute(
                "SELECT kind,instance_id,intent_sha256,record_json FROM batch_operations "
                "WHERE run_id=? AND instance_id=?",
                ("001", identity),
            ).fetchone()
            assert row is not None
            row_kind, instance, digest, raw = row
            if row_kind == kind:
                assert isinstance(row_kind, str) and isinstance(instance, str)
                assert isinstance(digest, str) and isinstance(raw, str)
                journal.append((row_kind, instance, digest, raw))
        if stage == "before_batch_forward" and journal:
            pytest.fail("rejected mutation reached the forward guard")

    try:
        with OpenAIBatchSession("001", store, adapter, budget, checkpoint) as session:
            (shard,) = rows(
                session.repository, "batch_preparations", "001", OpenAIBatchShardV1
            )
            if kind == "create":
                uploaded = await session.mutate(
                    shard, "upload", session.store.read(shard.input)
                )
                assert uploaded is not None and uploaded.outcome == "accepted"
                remote_id = uploaded.remote_id
                body = json.loads(
                    adapter.create_payload(session.manifest_sha256, shard, uploaded)
                )
                assert body["input_file_id"] == remote_id
                body["input_file_id"] = "file-other_input"
                payload = canonical_bytes(body)
            else:
                accepted = session.accepted_submission(shard.shard_id)
                assert accepted is not None
                remote_id = accepted.remote_id
                session.stop(remote=True)
                payload = b"{}"
            assert remote_id is not None
            events = service.events()
            with pytest.raises(ProviderIntegrityError, match=f"^{message}$"):
                await session.mutate(shard, kind, payload, remote_id)
    finally:
        await adapter.aclose()

    evidence = _sdk_operation_evidence(store, kind)
    assert journal == [evidence["intent"]]
    _, instance, digest, raw = journal[0]
    intent = BatchOperationIntentV1.model_validate_json(raw)
    assert (intent.kind, intent.instance_id, intent.remote_id, intent.sequence) == (
        kind,
        instance,
        remote_id,
        1,
    )
    assert digest == bytes_digest(canonical_bytes(intent))
    assert intent.payload_sha256 == bytes_digest(payload)
    with ReadOnlyRepository(store) as repo:
        artifacts = ArtifactStore(repo.run_directory("001"), create=False)
        assert artifacts.read(repo.get_artifact("001", digest)) == canonical_bytes(
            intent
        )
        assert (
            artifacts.read(repo.get_artifact("001", intent.payload_sha256)) == payload
        )
    assert evidence["reference"] is None and evidence["receipt"] is None
    assert evidence["results"] == evidence["members"] == 0
    assert service.events() == events
    if kind == "create":
        (record,) = diagnostic_records(store, "create_post")
        assert record["binding_id"] == instance
        assert (record["outcome"], record["reason"]) == ("local_failure", "pre_forward")
        assert record["http_status"] is record["http_request_id"] is None


@pytest.mark.parametrize(
    "case",
    [
        "buffered-upload",
        "streamed-create",
        "body-read",
        "sdk-wrapper",
        "unknown-stage",
        "unbound-sdk",
        "strict-json",
    ],
)
def test_diagnostic_correlates_headers_with_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    case: str,
) -> None:
    from collections.abc import AsyncIterator

    import httpx2
    from openai import APIResponseValidationError
    from openai._base_client import AsyncAPIClient

    service = Service(tmp_path / "service.sqlite")
    spec = specification(tmp_path, responses=1, capture="minimal")
    store = tmp_path / "store"
    target = "/v1/files" if case == "buffered-upload" else "/v1/batches"
    original = service.request
    events: list[str] = []
    posts: list[str] = []
    http = (
        400 if case == "buffered-upload" else 503 if case == "streamed-create" else 200
    )
    secret = b"PROHIBITED_DIAGNOSTIC_BODY"
    body = b'{"error":{"code":"invalid_json_line","message":"' + secret + b'"}}'
    headers = [(b"x-request-id", b"req_synthetic_failure")]
    if case == "streamed-create":
        headers.append((b"x-request-id", b"req_duplicate"))

    class Stream(httpx2.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            events.append("read")
            raise httpx2.ReadError("PROHIBITED_STREAM_ERROR")
            yield b""  # pragma: no cover

        async def aclose(self) -> None:
            events.append("close")

    def wire(request: Any) -> Any:
        if request.method == "POST":
            posts.append(request.url.path)
        if request.method != "POST" or request.url.path != target:
            return original(request)
        if case in ("streamed-create", "body-read"):
            return httpx2.Response(http, headers=headers, stream=Stream())
        return httpx2.Response(
            http, headers=headers, content=b"{" if case == "strict-json" else body
        )

    original_process = AsyncAPIClient._process_response  # pyright: ignore[reportPrivateUsage]

    async def process(self: Any, **kwargs: Any) -> Any:
        response = kwargs["response"]
        request = response.request
        if request.method == "POST" and request.url.path == target:
            if case == "unknown-stage":
                raise ValueError("PROHIBITED_UNATTRIBUTED_ERROR")
            if case == "unbound-sdk":
                response = httpx2.Response(200, request=request)
            raise APIResponseValidationError(
                response, body=None, message="PROHIBITED_SDK_WRAPPER"
            )
        return await original_process(self, **kwargs)

    monkeypatch.setattr(service, "request", wire)
    if case in ("sdk-wrapper", "unknown-stage", "unbound-sdk"):
        monkeypatch.setattr(AsyncAPIClient, "_process_response", process)
    install(monkeypatch, service)
    prepare_batch(spec, store=store, run_id="001")
    service.configure(store, spec)
    caplog.set_level("DEBUG")
    result = submit_batch("001", store=store, spec=spec)
    assert result.batch.unresolved_operations == 1 and result.batch.accepted_jobs == 0
    operation = "upload_post" if case == "buffered-upload" else "create_post"
    first, failure = diagnostic_records(store, operation)
    assert first["outcome"] == "observed" and first["reason"] == "headers"
    assert first["provider_code"] is None
    assert first["call_id"] == failure["call_id"]
    assert first["binding_id"] == failure["binding_id"]
    assert first["http_status"] == failure["http_status"] == http
    assert (
        first["http_request_id"]
        == failure["http_request_id"]
        == (None if case == "streamed-create" else "req_synthetic_failure")
    )
    assert (failure["outcome"], failure["reason"]) == {
        "buffered-upload": ("http_failure", "http_status"),
        "streamed-create": ("http_failure", "http_status"),
        "body-read": ("transport_failure", "body_read"),
        "sdk-wrapper": ("indeterminate", "sdk_wrapper"),
        "unknown-stage": ("indeterminate", "unknown_stage"),
        "unbound-sdk": ("indeterminate", "unknown_stage"),
        "strict-json": ("withheld", "body_decode"),
    }[case]
    assert failure["provider_code"] == (
        "invalid_json_line" if case == "buffered-upload" else None
    )
    assert events == {
        "streamed-create": ["close"],
        "body-read": ["read", "close"],
    }.get(case, [])
    assert posts.count(target) == 1
    assert "PROHIBITED_" not in caplog.text
    assert not list(store.rglob("openai-ack"))
    for path in store.rglob("*"):
        if path.is_file():
            assert secret not in path.read_bytes()
    if operation == "create_post":
        original_intent = _sdk_operation_evidence(store, "create")
        assert original_intent["submissions"] == (
            (first["binding_id"], None, "unknown"),
        )
        assert original_intent["reference"] is original_intent["receipt"] is None
        restarted = resume_batch("001", store=store, spec=spec)
        assert restarted.batch.unresolved_operations == 1
        _, lookup = diagnostic_records(store, "create_list")
        assert (
            lookup["reason"] == "no_match"
            and lookup["binding_id"] == first["binding_id"]
        )
        assert posts == ["/v1/files", "/v1/batches"]
        final_intent = _sdk_operation_evidence(store, "create")
        assert final_intent["journal"] == original_intent["journal"]
        assert final_intent["submissions"] == original_intent["submissions"]
        assert final_intent["reference"] is final_intent["receipt"] is None


def test_diagnostic_job_failure_requires_bound_success_status() -> None:
    from judgmentllm.providers.openai_batch_diagnostics import Call

    for status in (None, 101, 404, 503):
        call = Call("job_get", "a" * 64, http_status=status)
        with pytest.raises(ValueError):
            call.record("job_failed")
        with pytest.raises(ValueError):
            call.record("job_expired")
        with pytest.raises(ValueError):
            call.record("sdk_wrapper")
    assert (
        Call("job_get", "a" * 64, http_status=200).record("job_failed").outcome
        == "provider_rejected"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery", ["buffered", "streamed"])
async def test_diagnostic_deadline_preserves_only_arrived_upload_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, delivery: str
) -> None:
    from collections.abc import AsyncIterator

    import httpx2
    from batch_clock import batch_clock

    from judgmentllm import asubmit_batch
    from judgmentllm.execution.openai_batch import OpenAIBatchSession

    service = Service(tmp_path / "service.sqlite")
    request = service.request
    io: list[str] = []

    def wire(req: Any) -> Any:
        response = request(req)
        if (req.method, req.url.path) != ("POST", "/v1/files"):
            return response
        data = response.read()

        class Unread(httpx2.AsyncByteStream):
            async def __aiter__(self) -> AsyncIterator[bytes]:
                io.append("read")
                yield data

            async def aclose(self) -> None:
                io.append("close")

        return (
            httpx2.Response(200, content=data)
            if delivery == "buffered"
            else httpx2.Response(200, stream=Unread())
        )

    monkeypatch.setattr(service, "request", wire)
    install(monkeypatch, service)
    spec = specification(tmp_path, responses=1)
    store = tmp_path / "store"
    prepare_batch(spec, store=store, run_id="001")
    service.configure(store, spec)
    write = OpenAIBatchSession.write_fact
    with batch_clock(monkeypatch) as clock, monkeypatch.context() as patch:

        def slow(self: Any, kind: str, entity: str, record: Any) -> None:
            if (
                getattr(record, "operation", None) == "upload_post"
                and record.reason == "headers"
            ):
                clock.advance(clock.budgets[0].options.deadline_seconds + 1)
            write(self, kind, entity, record)

        patch.setattr(OpenAIBatchSession, "write_fact", slow)
        result = await asubmit_batch("001", store=store, spec=spec)
    assert clock.now > clock.budgets[0].deadline
    assert result.batch.accepted_jobs == 0
    assert service.events() == [("POST", "/v1/files")]
    evidence = _sdk_operation_evidence(store, "upload")
    assert not evidence["submissions"] and len(evidence["journal"]) == 1
    if delivery == "streamed":
        assert io == ["close"]
        assert evidence["receipt"] is evidence["reference"] is None
    else:
        assert not io
        assert evidence["receipt"] is not None
        assert evidence["reference"] == evidence["receipt"].encode()
        receipt = json.loads(evidence["receipt"])
        assert (receipt["outcome"], receipt["remote_id"]) == (
            "accepted",
            "file-input_1",
        )
        assert receipt["intent_sha256"] == evidence["intent"][2]
        assert (
            await asubmit_batch("001", store=store, spec=spec)
        ).batch.accepted_jobs == 1
        assert service.events() == [("POST", "/v1/files"), ("POST", "/v1/batches")]


def test_diagnostic_header_write_failure_preserves_accepted_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from judgmentllm.execution.openai_batch import OpenAIBatchSession
    from judgmentllm.lifecycle_models import LifecycleError

    service = Service(tmp_path / "service.sqlite")
    install(monkeypatch, service)
    spec = specification(tmp_path, responses=1)
    store = tmp_path / "store"
    prepare_batch(spec, store=store, run_id="001")
    service.configure(store, spec)
    original = OpenAIBatchSession.write_fact
    failed: list[str] = []

    def fail_create_headers(self: Any, kind: str, entity: str, record: Any) -> None:
        if (
            getattr(record, "operation", None) == "create_post"
            and record.reason == "headers"
        ):
            failed.append(entity)
            raise OSError("PROHIBITED_HEADER_WRITE")
        original(self, kind, entity, record)

    with monkeypatch.context() as patch:
        patch.setattr(OpenAIBatchSession, "write_fact", fail_create_headers)
        with pytest.raises(LifecycleError) as error:
            submit_batch("001", store=store, spec=spec)
    assert error.value.category == "execution" and "PROHIBITED_" not in str(error.value)
    assert len(failed) == 1
    evidence = _sdk_operation_evidence(store, "create")
    assert evidence["reference"] is not None and evidence["receipt"] is not None
    with ReadOnlyRepository(store) as repo:
        assert tuple(
            repo.connection.execute(
                "SELECT certainty,remote_id FROM submissions"
            ).fetchone()
        ) == (
            "accepted",
            "batch_1",
        )
    posts = [event for event in service.events() if event[0] == "POST"]
    assert posts == [("POST", "/v1/files"), ("POST", "/v1/batches")]
    assert resume_batch("001", store=store, spec=spec).batch.accepted_jobs == 1
    assert [event for event in service.events() if event[0] == "POST"] == posts


@pytest.mark.asyncio
async def test_control_overrun_closes_budget_without_a_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx2

    from judgmentllm import BatchControlOptionsV1, asubmit_batch
    from judgmentllm.execution.native_batch import InvocationBudget
    from judgmentllm.openai_batch_transport import batch_context
    from judgmentllm.providers.batch_protocol import BatchEvidenceWithheld
    from judgmentllm.providers.openai_batch import CONTROL_MAX_BYTES, OpenAIBatchAdapter

    service = Service(tmp_path / "service.sqlite")
    install(monkeypatch, service)
    spec = specification(tmp_path, responses=1)
    store = tmp_path / "store"
    prepare_batch(spec, store=store, run_id="001")
    service.configure(store, spec)
    await asubmit_batch("001", store=store, spec=spec)
    before = _sdk_operation_evidence(store, "create")
    original_request = service.request
    replacements: list[httpx2.Response] = []

    def oversized(request: httpx2.Request) -> httpx2.Response:
        response: httpx2.Response = original_request(request)
        assert request.method == "GET" and request.url.path == "/v1/files/file-output_1"
        try:
            body = response.read()
        finally:
            response.close()
        raw = b" " * (CONTROL_MAX_BYTES + 1 - len(body)) + body
        replacement = httpx2.Response(200, content=raw)
        replacement.close()
        replacements.append(replacement)
        return replacement

    with monkeypatch.context() as patch:
        patch.setattr(service, "request", oversized)
        budget = InvocationBudget(BatchControlOptionsV1(max_io_steps=16))
        adapter = OpenAIBatchAdapter(batch_context(spec), budget=budget)
        try:
            with pytest.raises(
                BatchEvidenceWithheld, match="oversized control representation"
            ):
                await adapter.file_metadata("file-output_1")
            assert budget.closed and budget.bytes == CONTROL_MAX_BYTES + 1
        finally:
            await adapter.aclose()
    assert replacements and all(response.is_closed for response in replacements)
    assert _sdk_operation_evidence(store, "create") == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case", ["unavailable", "short", "cached-with-distractor", "non-list", "oversize"]
)
async def test_upload_discovery_requires_complete_original_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    import httpx2

    from judgmentllm import BatchControlOptionsV1, aresume_batch, asubmit_batch
    from judgmentllm.batch_models import BatchOperationIntentV1, BatchOperationReceiptV1
    from judgmentllm.execution.native_batch import InvocationBudget
    from judgmentllm.execution.openai_batch import OpenAIBatchSession
    from judgmentllm.openai_batch_transport import batch_context
    from judgmentllm.persistence.hashing import bytes_digest
    from judgmentllm.persistence.ownership import executor_is_active
    from judgmentllm.providers.openai_batch import OpenAIBatchAdapter

    service = Service(tmp_path / "service.sqlite")
    install(monkeypatch, service)
    distractor = None
    if case == "cached-with-distractor":

        class UploadCommitted(BaseException):
            pass

        other = tmp_path / "other"
        other.mkdir()
        other_spec = specification(other, responses=1)
        other_store = other / "store"
        other_run = "distractor"
        prepare_batch(other_spec, store=other_store, run_id=other_run)
        commits: list[str] = []

        def stop_after_upload(stage: str, identity: str) -> None:
            if stage == "after_batch_operation_commit":
                commits.append(identity)
                raise UploadCommitted

        # A committed real upload suffices; no distractor result/job is needed.
        with pytest.raises(UploadCommitted):
            await asubmit_batch(
                other_run,
                store=other_store,
                spec=other_spec,
                _fault=stop_after_upload,
            )
        with ReadOnlyRepository(other_store) as repo:
            other_intent = BatchOperationIntentV1.model_validate_json(
                repo.connection.execute(
                    "SELECT record_json FROM batch_operations "
                    "WHERE run_id=? AND kind='upload'",
                    (other_run,),
                ).fetchone()[0]
            )
            receipt = BatchOperationReceiptV1.model_validate_json(
                repo.connection.execute(
                    "SELECT record_json FROM batch_facts "
                    "WHERE run_id=? AND kind='operation' AND entity_id=?",
                    (other_run, other_intent.instance_id),
                ).fetchone()[0]
            )
            assert commits == [other_intent.instance_id]
            assert receipt.outcome == "accepted" and receipt.remote_id is not None
            assert not executor_is_active(repo, other_run)
        with service.connect() as db:
            filename, purpose, body, size = db.execute(
                "SELECT filename,purpose,body,declared FROM files WHERE id=?",
                (receipt.remote_id,),
            ).fetchone()
        assert filename == OpenAIBatchAdapter.filename(other_intent)
        assert purpose == "batch" and len(body) == size
        assert bytes_digest(body) == other_intent.payload_sha256
        distractor = (filename, receipt.remote_id)
    spec = specification(tmp_path, responses=1)
    store = tmp_path / "store"
    prepare_batch(spec, store=store, run_id="001")
    service.configure(store, spec)
    service.set("lose_upload_ack", True)
    submitted = await asubmit_batch("001", store=store, spec=spec)
    assert submitted.batch.unresolved_operations
    service.set("lose_upload_ack", False)
    before = _sdk_operation_evidence(store, "upload")
    intent = BatchOperationIntentV1.model_validate_json(before["intent"][3])
    intended_filename = OpenAIBatchAdapter.filename(intent)
    with service.connect() as db:
        matches = db.execute(
            "SELECT id,body,declared FROM files WHERE purpose='batch' AND filename=?",
            (intended_filename,),
        ).fetchall()
    assert len(matches) == 1
    remote, original, declared = matches[0]
    if case == "cached-with-distractor":
        assert distractor is not None
        assert intended_filename != distractor[0] and remote != distractor[1]
    assert len(original) == declared
    if case == "oversize":
        service.file(remote, body=original + b" ", declared=declared)
        assert before["receipt"] is None and before["reference"] is None
        pending = await aresume_batch("001", store=store, spec=spec)
        assert pending.outcome == "partial" and pending.batch.unresolved_operations
        assert service.events() == [
            ("POST", "/v1/files"),
            ("GET", "/v1/files"),
            ("GET", "/v1/files/" + remote),
            ("GET", "/v1/files/" + remote + "/content"),
        ]
        with service.connect() as db:
            (received,) = db.execute(
                "SELECT sum(byte_count) FROM reads WHERE file=?", (remote,)
            ).fetchone()
        assert received == declared + 1
        assert _sdk_operation_evidence(store, "upload") == before
        return

    original_request = service.request
    replacements: list[httpx2.Response] = []

    def invalid_list(request: httpx2.Request) -> httpx2.Response:
        response: httpx2.Response = original_request(request)
        if request.url.path != "/v1/files":
            return response
        try:
            body = json.loads(response.read())
        finally:
            response.close()
        body["data"] = None
        replacement = httpx2.Response(200, json=body)
        replacement.close()
        replacements.append(replacement)
        return replacement

    events = service.events()
    budget = InvocationBudget(BatchControlOptionsV1())
    with monkeypatch.context() as patch:
        if case == "non-list":
            patch.setattr(service, "request", invalid_list)
        adapter = OpenAIBatchAdapter(batch_context(spec), budget=budget)
        try:
            if case in ("unavailable", "non-list"):
                # A newly constructed adapter has neither cached input nor a resolver.
                assert adapter.upload_resolver is None
                assert await adapter.reconcile_operation(intent) is None
            else:
                session = OpenAIBatchSession("001", store, adapter, budget)
                with session:
                    session.open_sources(spec, False)
                    authorized = session.upload_bytes(intent)
                    assert authorized is not None and authorized == original
                    if case == "short":
                        service.file(remote, body=original[:-1], declared=declared)
                        assert await adapter.reconcile_operation(intent) is None
                        with service.connect() as db:
                            received = db.execute(
                                "SELECT sum(byte_count) FROM reads WHERE file=?",
                                (remote,),
                            ).fetchone()[0]
                            assert received == declared - 1
                    else:
                        assert distractor is not None
                        with service.connect() as db:
                            assert db.execute(
                                "SELECT id FROM files WHERE purpose='batch' "
                                "AND filename=?",
                                (intended_filename,),
                            ).fetchall() == [(remote,)]
                            assert db.execute(
                                "SELECT id FROM files WHERE purpose='batch' "
                                "AND filename=?",
                                (distractor[0],),
                            ).fetchall() == [(distractor[1],)]
                        adapter.authorize_upload(authorized)
                        raw = await adapter.reconcile_operation(intent)
                        assert raw is not None
                        assert adapter.decode_receipt(raw, intent).remote_id == remote
        finally:
            await adapter.aclose()
    assert all(method == "GET" for method, _ in service.events()[len(events) :])
    if case in ("unavailable", "non-list"):
        assert service.events()[len(events) :] == [("GET", "/v1/files")]
    assert _sdk_operation_evidence(store, "upload") == before
    assert all(response.is_closed for response in replacements)
    if case == "short":
        service.file(remote, body=original, declared=declared)


def test_valid_cancel_ack_never_reposts_the_owned_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from judgmentllm import cancel_batch

    service = Service(tmp_path / "service.sqlite")
    install(monkeypatch, service)
    spec = specification(tmp_path, responses=1)
    store = tmp_path / "store"
    prepare_batch(spec, store=store, run_id="001")
    service.configure(store, spec)
    service.set("initial_status", "in_progress")
    assert submit_batch("001", store=store, spec=spec).batch.accepted_jobs == 1
    before = _sdk_operation_evidence(store, "create")
    cancel_batch("001", store=store)
    cancelled = _sdk_operation_evidence(store, "cancel")
    assert cancelled["receipt"] is not None and cancelled["reference"] is not None
    assert json.loads(cancelled["receipt"])["remote_id"] == before["accepted"][0][1]
    assert cancelled["accepted"] == before["accepted"]
    assert cancelled["results"] == cancelled["members"] == 0
    cancel_batch("001", store=store)
    assert _sdk_operation_evidence(store, "cancel")["intent"] == cancelled["intent"]
    assert [event for event in service.events() if event[0] == "POST"] == [
        ("POST", "/v1/files"),
        ("POST", "/v1/batches"),
        ("POST", "/v1/batches/" + before["accepted"][0][1] + "/cancel"),
    ]


def test_mock_options_cannot_continue_an_owned_openai_preparation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_openai_batch_recovery import (
        accepted_chain_snapshot,
        assert_accepted_chain_unchanged,
    )

    from judgmentllm import BatchMockOptions, openai_batch_transport
    from judgmentllm.execution import openai_batch as execution
    from judgmentllm.lifecycle_models import LifecycleError
    from judgmentllm.providers import openai_batch as provider

    service = Service(tmp_path / "service.sqlite")
    install(monkeypatch, service)
    spec = specification(tmp_path, responses=1)
    store = tmp_path / "store"
    prepare_batch(spec, store=store, run_id="001")
    before = accepted_chain_snapshot(store)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("mock-option refusal reached SDK or credentials")

    # Import every direct lookup owner before the scoped sentinel is installed.
    with monkeypatch.context() as patch:
        for module in (openai_batch_transport, execution, provider):
            patch.setattr(module, "require_sdk", forbidden)
        patch.setattr(provider, "_resolve_credentials", forbidden)
        with pytest.raises(LifecycleError) as error:
            resume_batch("001", store=store, spec=spec, mock=BatchMockOptions())
    assert error.value.category == "validation"
    assert isinstance(error.value.__context__, ValueError)
    assert error.value.__context__.args == (
        "mock controls cannot configure an OpenAI Batch run",
    )
    assert not service.events()
    assert_accepted_chain_unchanged(store, before)


def test_audit_rejects_nonobject_cursor_without_replacing_prior_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_batch_exports_retry import file_path
    from test_batch_preparation_rejections import corrupt_row
    from test_openai_batch_recovery import (
        accepted_chain_snapshot,
        assert_accepted_chain_unchanged,
        setup,
    )

    from judgmentllm import export_batch_provenance
    from judgmentllm.lifecycle_models import LifecycleError
    from judgmentllm.persistence.models import IntegrityError

    service, _spec, store = setup(tmp_path, monkeypatch, responses=1)
    report = export_batch_provenance("001", store=store)
    guards = accepted_chain_snapshot(store)["guards"]
    prior = {
        file_path(report, file.name): file_path(report, file.name).read_bytes()
        for file in report.manifest.files
    }
    prior[Path(report.manifest_path)] = Path(report.manifest_path).read_bytes()
    with ReadOnlyRepository(store) as repo:
        fact = repo.connection.execute(
            "SELECT fact_seq FROM batch_facts WHERE entity_id LIKE 'openai-ready:%' "
            "ORDER BY fact_seq LIMIT 1"
        ).fetchone()[0]
    corrupt_row(
        store,
        "batch_facts",
        "UPDATE batch_facts SET record_json='[]' WHERE fact_seq=?",
        (fact,),
    )
    before = accepted_chain_snapshot(store)
    assert before["guards"] == guards
    events = service.events()
    with pytest.raises(LifecycleError) as error:
        export_batch_provenance("001", store=store)
    assert error.value.category == "execution"
    assert isinstance(error.value.__context__, IntegrityError)
    assert error.value.__context__.args == ("batch cursor record is invalid",)
    assert {path: path.read_bytes() for path in prior} == prior
    assert service.events() == events
    assert_accepted_chain_unchanged(store, before)


@pytest.mark.parametrize("http_status", [None, 503])
def test_audit_rejects_impossible_no_match_with_valid_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    http_status: int | None,
) -> None:
    from test_batch_exports_retry import file_path
    from test_openai_batch_recovery import setup

    from judgmentllm import export_batch_provenance
    from judgmentllm.lifecycle_models import LifecycleError
    from judgmentllm.persistence.artifacts import ArtifactStore
    from judgmentllm.persistence.hashing import bytes_digest, canonical_bytes
    from judgmentllm.persistence.native_batch import register
    from judgmentllm.persistence.repository import Repository
    from judgmentllm.providers.openai_batch_diagnostics import Call

    service, _, store = setup(tmp_path, monkeypatch, responses=1)
    old = export_batch_provenance("001", store=store)
    controls = file_path(old, "controls.jsonl").read_bytes()
    events = service.events()
    with Repository(store) as repo, repo.transaction():
        binding = repo.connection.execute(
            "SELECT submission_id FROM submissions"
        ).fetchone()[0]
        good = Call("create_list", binding, http_status=200).record("no_match")
        value = good.model_dump()
        value["http_status"] = http_status
        raw = canonical_bytes(value)
        assert canonical_bytes(json.loads(raw)) == raw
        artifacts = ArtifactStore(repo.run_directory("001"), create=False)
        identity = artifacts.put(raw, "application/json")
        assert identity.sha256 == bytes_digest(raw) and artifacts.read(identity) == raw
        register(repo, "001", identity)
        repo.connection.execute(
            "INSERT INTO batch_facts(run_id,kind,entity_id,receipt_sha256,record_json) VALUES (?,?,?,?,?)",
            ("001", "cursor", good.entity, identity.sha256, raw.decode()),
        )
        assert repo.connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not repo.connection.execute("PRAGMA foreign_key_check").fetchall()
    with pytest.raises(LifecycleError) as error:
        export_batch_provenance("001", store=store)
    assert isinstance(error.value.__context__, ValueError)
    assert file_path(old, "controls.jsonl").read_bytes() == controls
    assert service.events() == events


@pytest.mark.parametrize(
    ("defect", "message"),
    [
        ("namespace", "OpenAI Batch diagnostic namespace differs"),
        ("kind", "OpenAI Batch diagnostic kind differs"),
        ("identity", "OpenAI Batch diagnostic identity differs"),
    ],
)
def test_audit_rejects_misbound_diagnostic_fact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, defect: str, message: str
) -> None:
    from test_batch_exports_retry import file_path
    from test_openai_batch_recovery import setup

    from judgmentllm import export_batch_provenance
    from judgmentllm.lifecycle_models import LifecycleError
    from judgmentllm.persistence.artifacts import ArtifactStore
    from judgmentllm.persistence.hashing import canonical_bytes
    from judgmentllm.persistence.models import IntegrityError
    from judgmentllm.persistence.native_batch import register
    from judgmentllm.persistence.repository import Repository
    from judgmentllm.providers.openai_batch_diagnostics import Call

    service, _, store = setup(tmp_path, monkeypatch, responses=1)
    old = export_batch_provenance("001", store=store)
    before = {
        file.name: file_path(old, file.name).read_bytes() for file in old.manifest.files
    }
    events = service.events()
    with Repository(store) as repo, repo.transaction():
        (binding,) = repo.connection.execute(
            "SELECT submission_id FROM submissions"
        ).fetchone()
        good = Call("job_get", binding, http_status=200).record("headers")
        kind, entity = "cursor", good.entity
        if defect == "namespace":
            entity = "ordinary-cursor"
        elif defect == "kind":
            kind = "status"
        else:
            entity = Call("job_get", binding, http_status=200).record("headers").entity
            assert entity != good.entity
        raw = canonical_bytes(good)
        artifacts = ArtifactStore(repo.run_directory("001"), create=False)
        identity = artifacts.put(raw, "application/json")
        assert artifacts.read(identity) == raw
        register(repo, "001", identity)
        repo.connection.execute(
            "INSERT INTO batch_facts(run_id,kind,entity_id,receipt_sha256,record_json) VALUES (?,?,?,?,?)",
            ("001", kind, entity, identity.sha256, raw.decode()),
        )
    with pytest.raises(LifecycleError) as error:
        export_batch_provenance("001", store=store)
    assert isinstance(error.value.__context__, IntegrityError)
    assert str(error.value.__context__) == message
    reopened = export_batch_provenance(
        "001", store=store, bundle_id=old.manifest.bundle_id
    )
    assert reopened.manifest == old.manifest
    assert {name: file_path(reopened, name).read_bytes() for name in before} == before
    assert service.events() == events


@pytest.mark.parametrize("fail_write", [False, True])
def test_malformed_buffered_create_error_preserves_unresolved_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fail_write: bool
) -> None:
    import httpx2

    from judgmentllm.execution.openai_batch import OpenAIBatchSession
    from judgmentllm.lifecycle_models import LifecycleError
    from judgmentllm.persistence.models import PersistenceError

    service = Service(tmp_path / "service.sqlite")
    spec = specification(tmp_path, responses=1, capture="minimal")
    store = tmp_path / "store"
    request = service.request
    lookups: list[tuple[int, Any]] = []

    def wire(req: httpx2.Request) -> httpx2.Response:
        if (req.method, req.url.path) == ("POST", "/v1/batches"):
            with service.connect() as db:
                db.execute(
                    "INSERT INTO events(method,path) VALUES (?,?)",
                    (req.method, req.url.path),
                )
            return httpx2.Response(
                503, content=b"{", headers={"x-request-id": "req_test_503"}
            )
        response: httpx2.Response = request(req)
        if (req.method, req.url.path) == ("GET", "/v1/batches"):
            lookups.append((response.status_code, json.loads(response.read())["data"]))
        return response

    monkeypatch.setattr(service, "request", wire)
    install(monkeypatch, service)
    prepare_batch(spec, store=store, run_id="001")
    service.configure(store, spec)
    writes: list[str] = []
    notify: list[str] = []
    write = OpenAIBatchSession.write_fact
    publish = OpenAIBatchSession.publish_diagnostic

    def write_fact(
        session: OpenAIBatchSession, kind: str, entity: str, record: Any
    ) -> None:
        if getattr(record, "operation", None) == "create_post":
            writes.append(record.reason)
            if record.reason == "headers":
                raise OSError("synthetic header write failure")
        write(session, kind, entity, record)

    def observe_publish(session: OpenAIBatchSession, record: Any) -> None:
        if record.operation == "create_post":
            notify.append(record.reason)
        publish(session, record)

    if fail_write:
        with monkeypatch.context() as patch:
            patch.setattr(OpenAIBatchSession, "write_fact", write_fact)
            patch.setattr(OpenAIBatchSession, "publish_diagnostic", observe_publish)
            with pytest.raises(LifecycleError) as error:
                submit_batch("001", store=store, spec=spec)
        assert isinstance(error.value.__context__, PersistenceError)
        assert (
            str(error.value.__context__)
            == "OpenAI Batch diagnostic evidence could not be published"
        )
        assert notify == ["headers", "http_status"] and writes == ["headers"]
        assert not diagnostic_records(store, "create_post")
    else:
        assert (
            submit_batch("001", store=store, spec=spec).batch.unresolved_operations == 1
        )
        headers, rejected = diagnostic_records(store, "create_post")
        assert (headers["reason"], rejected["reason"]) == ("headers", "http_status")
        assert headers["call_id"] == rejected["call_id"]
        assert headers["http_status"] == rejected["http_status"] == 503
        assert (
            headers["http_request_id"] == rejected["http_request_id"] == "req_test_503"
        )
        assert (
            rejected["outcome"] == "http_failure" and rejected["provider_code"] is None
        )

    before = _sdk_operation_evidence(store, "create")
    assert before["submissions"] == ((before["intent"][1], None, "unknown"),)
    assert before["reference"] is before["receipt"] is None
    events = service.events()
    posts = [event for event in events if event[0] == "POST"]
    assert posts == [("POST", "/v1/files"), ("POST", "/v1/batches")]
    with service.connect() as db:
        assert db.execute("SELECT count(*) FROM jobs").fetchone() == (0,)
    lookups.clear()
    assert resume_batch("001", store=store, spec=spec).batch.unresolved_operations == 1
    assert lookups == [(200, [])]
    assert service.events()[len(events) :] == [("GET", "/v1/batches")]
    assert _sdk_operation_evidence(store, "create") == before
    _, no_match = diagnostic_records(store, "create_list")
    assert no_match["reason"] == "no_match"
    assert no_match["binding_id"] == before["intent"][1]


def test_private_ack_write_failure_preserves_upload_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from judgmentllm.lifecycle_models import LifecycleError
    from judgmentllm.persistence.artifacts import ArtifactStore
    from judgmentllm.persistence.models import PersistenceError

    service = Service(tmp_path / "service.sqlite")
    install(monkeypatch, service)
    spec = specification(tmp_path, responses=1, capture="full")
    store = tmp_path / "store"
    prepare_batch(spec, store=store, run_id="001")
    service.configure(store, spec)
    publish = ArtifactStore.publish_reference
    failed: list[str] = []

    def fail_private(self: ArtifactStore, identity: Any, path: str) -> Path:
        if path.startswith("batch/openai-ack/"):
            failed.append(path)
            raise OSError("synthetic private reference failure")
        return publish(self, identity, path)

    with monkeypatch.context() as patch:
        patch.setattr(ArtifactStore, "publish_reference", fail_private)
        with pytest.raises(LifecycleError) as error:
            submit_batch("001", store=store, spec=spec)
    assert isinstance(error.value.__context__, PersistenceError)
    assert (
        str(error.value.__context__)
        == "OpenAI Batch private acknowledgment could not be published"
    )
    (failure,) = failed
    before = _sdk_operation_evidence(store, "upload")
    assert before["receipt"] and before["reference"] == before["receipt"].encode()
    assert json.loads(before["receipt"])["remote_id"] == "file-input_1"
    assert failure.startswith(f"batch/openai-ack/{before['intent'][1]}/")
    assert [row[0] for row in before["journal"]] == ["upload"]
    assert service.events() == [("POST", "/v1/files")]
    assert resume_batch("001", store=store, spec=spec).batch.accepted_jobs == 1
    after = _sdk_operation_evidence(store, "upload")
    assert (after["intent"], after["receipt"], after["reference"]) == (
        before["intent"],
        before["receipt"],
        before["reference"],
    )
    assert [event for event in service.events() if event[0] == "POST"] == [
        ("POST", "/v1/files"),
        ("POST", "/v1/batches"),
    ]


@pytest.mark.parametrize("fault", ["decode", "close"])
def test_job_get_failure_preserves_observed_status_and_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    from collections.abc import AsyncIterator

    import httpx2
    from test_openai_batch_recovery import setup

    service, _, store = setup(tmp_path, monkeypatch, responses=1)
    before = _sdk_operation_evidence(store, "create")
    _, remote = before["accepted"][0]
    count = len(diagnostic_records(store, "job_get"))
    events: list[str] = []
    request = service.request

    class FailingClose(httpx2.AsyncByteStream):
        def __init__(self, body: bytes) -> None:
            self.body = body

        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield self.body
            events.append("eof")

        async def aclose(self) -> None:
            events.append("close")
            raise OSError("synthetic close failure")

    def wire(req: httpx2.Request) -> httpx2.Response:
        response: httpx2.Response = request(req)
        if (req.method, req.url.path) != ("GET", f"/v1/batches/{remote}"):
            return response
        headers = {"x-request-id": "req_job_test"}
        if fault == "decode":
            return httpx2.Response(200, headers=headers, content=b"{")
        return httpx2.Response(
            200, headers=headers, stream=FailingClose(response.read())
        )

    monkeypatch.setattr(service, "request", wire)
    assert poll_batch("001", store=store).outcome == "partial"
    headers, failure = diagnostic_records(store, "job_get")[count:]
    assert headers["reason"] == "headers"
    assert headers["http_status"] == failure["http_status"] == 200
    assert headers["http_request_id"] == failure["http_request_id"] == "req_job_test"
    assert headers["call_id"] == failure["call_id"]
    assert failure["binding_id"] == before["intent"][1]
    if fault == "decode":
        assert (failure["outcome"], failure["reason"]) == ("withheld", "body_decode")
        assert events == []
    else:
        assert (failure["outcome"], failure["reason"]) == (
            "indeterminate",
            "unknown_stage",
        )
        assert events == ["eof", "close"]
    assert _sdk_operation_evidence(store, "create") == before
    assert [event for event in service.events() if event[0] == "POST"] == [
        ("POST", "/v1/files"),
        ("POST", "/v1/batches"),
    ]


@pytest.mark.parametrize("fault", ["delegate", "read", "close", "encoding", "decode"])
def test_cancel_failures_have_no_diagnostic_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    from collections.abc import AsyncIterator

    import httpx2

    from judgmentllm import cancel_batch

    service = Service(tmp_path / "service.sqlite")
    install(monkeypatch, service)
    spec = specification(tmp_path, responses=1)
    store = tmp_path / "store"
    prepare_batch(spec, store=store, run_id="001")
    service.configure(store, spec)
    service.set("initial_status", "in_progress")
    assert submit_batch("001", store=store, spec=spec).batch.accepted_jobs == 1
    before = _sdk_operation_evidence(store, "create")
    _, remote = before["accepted"][0]
    target = ("POST", f"/v1/batches/{remote}/cancel")
    request = service.request
    events: list[str] = []

    def diagnostics() -> tuple[Any, ...]:
        with ReadOnlyRepository(store) as repo:
            return tuple(
                repo.connection.execute(
                    "SELECT fact_seq, record_json FROM batch_facts "
                    "WHERE entity_id LIKE 'openai-diagnostic:%' ORDER BY fact_seq"
                )
            )

    class Stream(httpx2.AsyncByteStream):
        def __init__(self, body: bytes) -> None:
            self.body = body

        async def __aiter__(self) -> AsyncIterator[bytes]:
            events.append("read")
            if fault == "read":
                raise httpx2.ReadError("synthetic cancel read failure")
            yield self.body

        async def aclose(self) -> None:
            events.append("close")
            if fault == "close":
                raise OSError("synthetic cancel close failure")

    def wire(req: httpx2.Request) -> httpx2.Response:
        if (req.method, req.url.path) != target:
            return request(req)
        if fault == "delegate":
            with service.connect() as db:
                db.execute("INSERT INTO events(method,path) VALUES (?,?)", target)
            raise httpx2.ConnectError("synthetic cancel delegate failure")
        response: httpx2.Response = request(req)
        if fault == "decode":
            return httpx2.Response(200, content=b"{")
        headers = {"content-encoding": "gzip"} if fault == "encoding" else {}
        return httpx2.Response(200, headers=headers, stream=Stream(response.read()))

    facts = diagnostics()
    monkeypatch.setattr(service, "request", wire)
    result = cancel_batch("001", store=store)
    assert result.batch.unresolved_operations == 1
    assert result.batch.accepted_jobs == 1
    cancelled = _sdk_operation_evidence(store, "cancel")
    assert cancelled["receipt"] is cancelled["reference"] is None
    assert cancelled["accepted"] == before["accepted"]
    after = _sdk_operation_evidence(store, "create")
    assert (after["receipt"], after["reference"]) == (
        before["receipt"],
        before["reference"],
    )
    assert diagnostics() == facts
    assert (
        events
        == {
            "delegate": [],
            "read": ["read", "close"],
            "close": ["read", "close"],
            "encoding": ["close"],
            "decode": [],
        }[fault]
    )
    assert [event for event in service.events() if event[0] == "POST"] == [
        ("POST", "/v1/files"),
        ("POST", "/v1/batches"),
        target,
    ]


@pytest.mark.parametrize(
    "defect", ["classification", "row-binding", "call-binding", "code"]
)
def test_diagnostic_validation_rejects_unattributed_fields(defect: str) -> None:
    from pydantic import ValidationError

    from judgmentllm.providers.openai_batch_diagnostics import (
        Call,
        OpenAIBatchDiagnosticV1,
        row_record,
    )

    call = Call("job_get", "a" * 64, http_status=200).record("headers").model_dump()
    if defect == "row-binding":
        fields = row_record(None, "a" * 64, 0, None, None, None).model_dump()
        fields["call_id"] = call["call_id"]
    else:
        fields = {
            **call,
            **{
                "classification": {"outcome": "withheld"},
                "call-binding": {"member_status": 200},
                "code": {"provider_code": "invalid_json_line"},
            }[defect],
        }
    with pytest.raises(ValidationError, match="Invalid evidence record"):
        OpenAIBatchDiagnosticV1.model_validate(fields)


@pytest.mark.parametrize(
    ("items", "code"),
    [
        ([{"code": "invalid_json_line"}] * 33, None),
        (["not-an-object"], None),
        (
            [{}, {"code": "invalid_json_line"}, {"code": "invalid_json_line"}],
            "invalid_json_line",
        ),
    ],
    ids=["over-limit", "non-object", "repeated"],
)
def test_failed_create_reports_only_bound_allowed_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, items: list[Any], code: str | None
) -> None:
    import httpx2

    service = Service(tmp_path / "service.sqlite")
    service.set("initial_status", "failed")
    spec = specification(tmp_path, responses=1, capture="full")
    store = tmp_path / "store"
    request = service.request
    marker = "PROHIBITED_TEST_JOB_ERROR"

    def wire(req: httpx2.Request) -> httpx2.Response:
        response: httpx2.Response = request(req)
        if (req.method, req.url.path) != ("POST", "/v1/batches"):
            return response
        body = json.loads(response.read())
        body["errors"] = {"data": items, "message": marker}
        return httpx2.Response(200, json=body)

    monkeypatch.setattr(service, "request", wire)
    install(monkeypatch, service)
    prepare_batch(spec, store=store, run_id="001")
    service.configure(store, spec)
    assert submit_batch("001", store=store, spec=spec).batch.accepted_jobs == 1
    headers, rejection = diagnostic_records(store, "create_post")
    assert (rejection["outcome"], rejection["reason"]) == (
        "provider_rejected",
        "job_failed",
    )
    assert headers["call_id"] == rejection["call_id"]
    assert headers["http_status"] == rejection["http_status"] == 200
    assert rejection["provider_code"] == code
    accepted = _sdk_operation_evidence(store, "create")
    assert accepted["accepted"] == ((rejection["binding_id"], "batch_1"),)
    assert accepted["receipt"] is not None
    assert accepted["reference"] == accepted["receipt"].encode()
    assert service.events() == [("POST", "/v1/files"), ("POST", "/v1/batches")]
    for path in store.rglob("*"):
        if path.is_file():
            assert marker.encode() not in path.read_bytes()


def test_sdk_upload_then_create_stays_cookie_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx2

    service = Service(tmp_path / "service.sqlite")
    request = service.request
    marker = "batch-upload-cookie-canary"

    def wire(req: httpx2.Request) -> httpx2.Response:
        response: httpx2.Response = request(req)
        if (req.method, req.url.path) == ("POST", "/v1/files"):
            assert response.status_code == 200
            response.headers["Set-Cookie"] = f"probe={marker}; Path=/; Secure"
        if (req.method, req.url.path) == ("POST", "/v1/batches"):
            assert "cookie" not in req.headers
        return response

    monkeypatch.setattr(service, "request", wire)
    install(monkeypatch, service)
    spec = specification(tmp_path, responses=1, capture="full")
    store = tmp_path / "store"
    prepare_batch(spec, store=store, run_id="001")
    service.configure(store, spec)

    send = httpx2.AsyncClient.send
    clients: list[httpx2.AsyncClient] = []
    upload_cookie_counts: list[int] = []

    async def observe_send(
        client: httpx2.AsyncClient, req: httpx2.Request, **kwargs: Any
    ) -> httpx2.Response:
        clients.append(client)
        response = await send(client, req, **kwargs)
        if (req.method, req.url.path) == ("POST", "/v1/files"):
            upload_cookie_counts.append(len(client.cookies))
        return response

    monkeypatch.setattr(httpx2.AsyncClient, "send", observe_send)
    result = submit_batch("001", store=store, spec=spec)
    assert service.events() == [("POST", "/v1/files"), ("POST", "/v1/batches")]
    assert result.outcome == "ok" and result.batch.accepted_jobs == 1
    assert len(clients) == 2 and clients[0] is clients[1]
    assert upload_cookie_counts == [0]
    for path in store.rglob("*"):
        if path.is_file():
            assert marker.encode() not in path.read_bytes()
