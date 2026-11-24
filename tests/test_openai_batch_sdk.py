"""Exact SDK, per-member wire parity and public offline lifecycle."""

import json
from pathlib import Path
from typing import Any

import pytest
from openai_batch_fixture import Service, install, specification

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
    from judgmentllm.lifecycle_models import LifecycleError

    spec = specification(tmp_path)
    setattr(spec.execution, setting, value)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("SDK guard reached for an incompatible scope")

    monkeypatch.setattr(openai_batch_transport, "require_sdk", forbidden)
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
