"""Actual pinned SDK Batch calls, private evidence, and offline recovery."""

import asyncio
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from judgmentllm import (
    RetrySelection,
    cancel_batch,
    collect_batch,
    export_batch_provenance,
    get_batch_status,
    poll_batch,
    prepare_batch,
    resume_batch,
    resume_run,
    stop_batch,
    submit_batch,
)
from judgmentllm.anthropic_batch_transport import BATCH_PATH
from judgmentllm.batch_models import BatchControlOptionsV1
from judgmentllm.evidence.usage import ReportedUsageInput
from judgmentllm.execution.anthropic_batch import AnthropicBatchSession
from judgmentllm.execution.native_batch import InvocationBudget
from judgmentllm.lifecycle_models import LifecycleError
from judgmentllm.persistence.artifacts import ArtifactStore
from judgmentllm.persistence.hashing import bytes_digest, canonical_bytes
from judgmentllm.persistence.models import ArtifactIdentity, IntegrityError, RunRecord
from judgmentllm.persistence.repository import ReadOnlyRepository, Repository
from scripts.anthropic_batch_fixture import (
    PRIVATE,
    REJECTION,
    SECRET,
    Service,
    installed,
    write_case,
)


def records(
    root: Path, where: str = "entity_id LIKE 'anthropic-http:%'"
) -> list[dict[str, Any]]:
    with ReadOnlyRepository(root / "store", preserve_files=True) as repo:
        return [
            json.loads(row[0])
            for row in repo.connection.execute(
                "SELECT record_json FROM batch_facts WHERE "
                + where
                + " ORDER BY fact_seq"
            )
        ]


def ready(root: Path, *, capture: str = "full", count: int = 2) -> Service:
    config = write_case(root, capture=capture, count=count)
    prepare_batch(config, store=root / "store", run_id="001")
    service = Service(root / "service.sqlite")
    with installed(service):
        assert submit_batch("001", store=root / "store").outcome == "ok"
        assert poll_batch("001", store=root / "store").outcome == "ok"
    return service


def private_body(root: Path, record: dict[str, Any]) -> bytes | None:
    assert record["body"] is not None
    with ReadOnlyRepository(root / "store", preserve_files=True) as repo:
        store = ArtifactStore(repo.run_directory("001"), create=False)
        identity = ArtifactIdentity.model_validate(record["body"])
        return None if identity.relative_path is None else store.read(identity)


def assert_private(root: Path, *, extra: tuple[bytes, ...] = ()) -> None:
    needles = (PRIVATE.encode(), SECRET.encode(), *extra)
    for path in (root / "store").rglob("*"):
        if path.is_file():
            content = path.read_bytes()
            assert not any(
                needle in content or bytes_digest(needle).encode() in content
                for needle in needles
            )


@pytest.mark.parametrize("capture", ["full", "minimal"])
def test_success_mixed_replay_and_http_provenance(tmp_path: Path, capture: str) -> None:
    root = tmp_path / "store"
    service = ready(tmp_path, capture=capture)
    service.set("duplicate", True)
    with installed(service):
        assert (
            collect_batch(
                "001", store=root, options=BatchControlOptionsV1(max_result_rows=1)
            ).outcome
            == "partial"
        )
        assert collect_batch("001", store=root).outcome == "ok"
    with ReadOnlyRepository(root) as repo:
        results = list(repo.iter_results("001"))
        assert len(results) == 4 and all(row.status == "complete" for row in results)
        usage = [
            json.loads(row[0])["usage"]
            for row in repo.connection.execute(
                "SELECT record_json FROM batch_member_receipts"
            )
        ]
        assert len(usage) == 2
        assert sum(item["input_tokens"] for item in usage) == 14
        assert all(
            item["total_tokens"] is None and item["reported_cost"] is None
            for item in usage
        )
    assert service.events().count(("POST", BATCH_PATH)) == 1
    assert service.events().count(("GET", BATCH_PATH + "/msgbatch_1/results")) == 2
    assert b"caf\xc3\xa9" in service.body()
    assert all(
        member["params"]["max_tokens"] == 4096
        and member["params"]["stream"] is False
        and member["params"]["service_tier"] == "standard_only"
        for member in json.loads(service.body())["requests"]
    )
    diagnostics = records(tmp_path)
    headers = {x["call_id"] for x in diagnostics if x["phase"] == "headers"}
    assert len(headers) == 7
    assert len(diagnostics) == 2 * len(headers)
    create = next(
        x for x in diagnostics if x["stage"] == "create" and x["phase"] == "completion"
    )
    assert create["http_status"] == 200 and create["request_id"] == "req_fixture"
    assert (
        create["intent_id"]
        and create["headers_sha256"]
        and create["disposition"] == "exact"
    )
    body = private_body(tmp_path, create)
    assert (body is not None) == (capture == "full")
    if body is not None:
        assert json.loads(body)["id"] == "msgbatch_1"
    audit = export_batch_provenance("001", store=root)
    assert audit.schema_version == 3 and len(audit.manifest.files) == 11
    assert (
        export_batch_provenance(
            "001", store=root, bundle_id=audit.manifest.bundle_id
        ).manifest
        == audit.manifest
    )
    assert_private(tmp_path)


@pytest.mark.parametrize("capture", ["full", "minimal"])
def test_rejection_is_diagnostic_and_never_reposts(
    tmp_path: Path, capture: str, caplog: pytest.LogCaptureFixture
) -> None:
    config = write_case(tmp_path, capture=capture)
    root = tmp_path / "store"
    prepare_batch(config, store=root, run_id="001")
    service = Service(tmp_path / "service.sqlite")
    service.set("reject", True)
    service.set("request_id", "req_" + SECRET)
    with installed(service), caplog.at_level(logging.DEBUG):
        assert submit_batch("001", store=root).outcome == "partial"
        assert resume_batch("001", store=root).batch.unresolved_operations == 1
    assert service.events() == [("POST", BATCH_PATH), ("CLOSE", BATCH_PATH)]
    header, completion = records(tmp_path)
    assert header["phase"] == "headers" and header["request_id"] is None
    assert (
        completion["call_id"] == header["call_id"] and completion["http_status"] == 400
    )
    assert (
        completion["error_type"] == "invalid_request_error"
        and completion["disposition"] == "redacted"
    )
    retained = private_body(tmp_path, completion)
    if capture == "full":
        assert retained is not None and json.loads(retained) == {
            "type": "error",
            "error": {"type": "invalid_request_error"},
        }
    else:
        assert retained is None and completion["body"]["sha256"]
    assert PRIVATE not in caplog.text and SECRET not in caplog.text
    assert_private(tmp_path, extra=(REJECTION,))


@pytest.mark.parametrize("capture", ["full", "minimal"])
def test_stream_interrupt_commits_first_short_row_and_replays(
    tmp_path: Path, capture: str
) -> None:
    service = ready(tmp_path, capture=capture)
    store = tmp_path / "store"
    service.set("interrupt", True)
    with installed(service):
        assert collect_batch("001", store=store).batch.missing_members == 1
    result = [x for x in records(tmp_path) if x["stage"] == "results_content"]
    assert [x["phase"] for x in result] == ["headers", "completion"]
    assert result[-1]["interruption"] == "transport_error"
    assert ("CLOSE", "/results") in service.events()
    service.set("interrupt", False)
    with installed(Service(service.path)):
        assert not collect_batch("001", store=store).batch.recovery_required
    assert service.events().count(("POST", BATCH_PATH)) == 1
    with ReadOnlyRepository(store) as repo:
        assert (
            repo.connection.execute(
                "SELECT count(*) FROM batch_member_receipts"
            ).fetchone()[0]
            == 2
        )


@pytest.mark.parametrize("fault", ["unsafe", "oversized"])
def test_hidden_prelude_is_bounded_before_content_admission(
    tmp_path: Path, fault: str
) -> None:
    service = ready(tmp_path)
    service.set("metadata_fault", fault)
    with installed(service):
        assert collect_batch("001", store=tmp_path / "store").outcome == "error"
    assert not any(p.endswith("/results") for m, p in service.events() if m == "GET")
    assert records(tmp_path, "entity_id LIKE 'anthropic-admission:%'")[-1]["code"] in {
        "source_limit",
        "unsafe_metadata",
    }
    assert all(
        entry["code"] != "admitted"
        for entry in records(tmp_path, "entity_id LIKE 'anthropic-admission:%'")
    )


@pytest.mark.parametrize(
    ("status", "headers"),
    [
        pytest.param(206, {}, id="partial-status"),
        pytest.param(200, {"content-range": "bytes 0-511/1024"}, id="content-range"),
        pytest.param(
            200, {"content-type": "multipart/byteranges; boundary=abc"}, id="multipart"
        ),
        pytest.param(
            200,
            {"transfer-encoding": "chunked", "content-length": "1024"},
            id="ambiguous-length",
        ),
    ],
)
def test_non_whole_source_is_closed_before_ingestion(
    tmp_path: Path,
    status: int,
    headers: dict[str, str],
) -> None:
    service = ready(tmp_path)
    service.set("result_status", status)
    service.set("result_headers", headers)
    service.set("track_result_reads", True)
    with installed(service):
        result = collect_batch("001", store=tmp_path / "store")
    assert result.outcome == "error" and result.batch.missing_members == 2
    assert result.collection is not None and result.collection.codes == (
        "source_framing",
    )
    assert ("CLOSE", "/results") in service.events() and (
        "READ",
        "/results",
    ) not in service.events()
    assert not records(
        tmp_path, "kind='cursor' AND json_extract(record_json,'$.position') IS NOT NULL"
    )
    assert service.events().count(("POST", BATCH_PATH)) == 1


def test_buffered_bad_length_is_debited_before_framing_rejection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from httpx2 import Response

    service = ready(tmp_path)
    service.set("frame_size", [1024, 1024])
    service.set("buffer_result", True)
    service.set("result_headers", {"content-length": "malformed"})
    debit = InvocationBudget.receive_bytes
    charged: list[int] = []

    def receive(budget: InvocationBudget, size: int) -> None:
        charged.append(size)
        debit(budget, size)

    monkeypatch.setattr(InvocationBudget, "receive_bytes", receive)
    with installed(service):
        result = collect_batch("001", store=tmp_path / "store")
    assert result.collection is not None and result.collection.codes == (
        "source_framing",
    )
    assert result.batch.missing_members == 2
    requests = json.loads(service.body())["requests"]
    prelude = Response(200, json=service.metadata(1, requests, "ended", False))
    assert charged == [len(prelude.content), 2048]


def test_budget_is_checked_between_rows_from_one_transport_chunk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ready(tmp_path)
    service.set("coalesce_results", True)
    checkpoint = AnthropicBatchSession.checkpoint

    def expire(session: AnthropicBatchSession, stage: str, identity: str = "") -> None:
        checkpoint(session, stage, identity)
        if stage == "after_batch_cursor":
            session.budget.deadline = asyncio.get_running_loop().time()

    with installed(service):
        with monkeypatch.context() as clock:
            clock.setattr(AnthropicBatchSession, "checkpoint", expire)
            result = collect_batch("001", store=tmp_path / "store")
        assert result.outcome == "partial" and result.batch.missing_members == 1
        (frontier,) = records(
            tmp_path,
            "kind='cursor' AND json_extract(record_json,'$.position') IS NOT NULL",
        )
        assert frontier["position"] == 1 and not frontier.get("exhausted", False)
        assert collect_batch("001", store=tmp_path / "store").outcome == "ok"
    assert service.events().count(("POST", BATCH_PATH)) == 1


def test_public_remote_controls_fail_before_writer(tmp_path: Path) -> None:
    config = write_case(tmp_path, capture="full")
    store = tmp_path / "store"
    prepare_batch(config, store=store, run_id="001")
    before = {p: p.read_bytes() for p in store.rglob("*") if p.is_file()}
    with (
        patch(
            "judgmentllm.native_batch.Repository",
            side_effect=AssertionError("writable store opened"),
        ),
        patch(
            "judgmentllm.providers.anthropic._resolve_credentials",
            side_effect=AssertionError("credential lookup"),
        ),
        patch(
            "judgmentllm.providers.anthropic_batch._protected_client",
            side_effect=AssertionError("client constructed"),
        ),
    ):
        for operate in (
            submit_batch,
            cancel_batch,
            poll_batch,
            collect_batch,
            resume_batch,
        ):
            with pytest.raises(LifecycleError, match="remote controls are disabled"):
                operate("001", store=store)
    assert {p: p.read_bytes() for p in store.rglob("*") if p.is_file()} == before


def copy_with_orphan_wal(source: Path, destination: Path) -> Path:
    wal = "registry.sqlite3-wal"
    shm = "registry.sqlite3-shm"
    assert (source / wal).stat().st_size > 0 and (source / shm).is_file()
    shutil.copytree(source, destination, ignore=shutil.ignore_patterns(shm))
    assert (destination / wal).is_file() and not (destination / shm).exists()
    with closing(
        sqlite3.connect(
            (destination / "registry.sqlite3").as_uri() + "?mode=ro&immutable=1",
            uri=True,
        )
    ) as main:
        assert main.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
    return destination


@pytest.mark.parametrize("orphan", [False, True], ids=["active-wal", "orphan-wal"])
def test_both_resume_routes_deny_v14_without_touching_registry_files(
    tmp_path: Path,
    orphan: bool,
) -> None:
    from typer.testing import CliRunner

    from judgmentllm.cli import app

    config = write_case(tmp_path, capture="minimal")
    original = tmp_path / "original"
    with Repository(original) as held:
        held.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        prepare_batch(config, store=original, run_id="001")
        store = (
            copy_with_orphan_wal(original, tmp_path / "store") if orphan else original
        )
        directory = store / "runs" / bytes_digest(b"001")
        before = {p: p.read_bytes() for p in store.rglob("*") if p.is_file()}
        with (
            patch(
                "judgmentllm.native_batch.Repository",
                side_effect=AssertionError("writer constructed"),
            ),
            patch(
                "judgmentllm.providers.anthropic_batch._protected_client",
                side_effect=AssertionError("client constructed"),
            ),
        ):
            for resume in (resume_batch, resume_run):
                for reference, root in (("001", store), (directory, None)):
                    with pytest.raises(
                        LifecycleError, match="remote controls are disabled"
                    ):
                        resume(reference, store=root)
            for group in ("batch", "runs"):
                result = CliRunner().invoke(
                    app,
                    [
                        group,
                        "resume",
                        "--run",
                        "001",
                        "--store",
                        str(store),
                        "--config",
                        str(config),
                        "--json",
                    ],
                )
                assert result.exit_code == 2
                assert (
                    "remote controls are disabled"
                    in json.loads(result.stdout)["error"]["message"]
                )
        assert {p: p.read_bytes() for p in store.rglob("*") if p.is_file()} == before


def legacy_batch_config(directory: Path, version: int) -> Path:
    config = write_case(directory, capture="minimal")
    data = json.loads(config.read_bytes())
    if version == 12:
        data.update(
            schema_version=2, provider="mock", endpoint="synthetic", model="synthetic"
        )
    else:
        data.update(provider="openai", endpoint="responses", model="gpt-6-luna")
    config.write_text(json.dumps(data), encoding="utf-8")
    return config


@pytest.mark.parametrize("version", [12, 13])
def test_legacy_batch_resume_reads_committed_orphan_wal(
    tmp_path: Path, version: int
) -> None:
    config = legacy_batch_config(tmp_path, version)
    original = tmp_path / "original"
    with Repository(original) as held:
        held.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        prepare_batch(config, store=original, run_id="001")
        assert stop_batch("001", store=original).batch.local_stop
        store = copy_with_orphan_wal(original, tmp_path / "store")

    # The sticky stop lives in the WAL and prevents any provider transport.
    with patch(
        "judgmentllm.providers.openai._resolve_credentials",
        side_effect=AssertionError("credential lookup"),
    ):
        resumed = resume_run("001", store=store)
    assert resumed.batch is not None and resumed.batch.local_stop
    assert resumed.batch.accepted_jobs == 0


@pytest.mark.parametrize("version", [12, 13])
@pytest.mark.parametrize("entry", ["batch", "runs"])
@pytest.mark.parametrize("checkpoint", [False, True], ids=["append", "checkpoint"])
def test_legacy_preflight_observes_concurrent_committed_wal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    version: int,
    entry: str,
    checkpoint: bool,
) -> None:
    from judgmentllm import native_batch, preparation

    config = legacy_batch_config(tmp_path, version)
    store = tmp_path / "store"
    database = store / "registry.sqlite3"
    wal = store / "registry.sqlite3-wal"
    committed = False
    live_reads: list[Path] = []
    signature = ReadOnlyRepository._file_signature  # pyright: ignore[reportPrivateUsage]
    check = preparation._preflight_v12_write  # pyright: ignore[reportPrivateUsage]

    with Repository(store) as writer:
        writer.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        prepare_batch(config, store=store, run_id="001")
        assert stop_batch("001", store=store).batch.local_stop

        def commit_during_probe(path: Path) -> tuple[int, int, int, int, int]:
            nonlocal committed
            observed = signature(path)
            if path == wal and not committed:
                writer.create_run(
                    RunRecord(
                        run_id="002",
                        task_kind="rubric",
                        provider="mock",
                        endpoint="synthetic",
                        model="synthetic",
                    )
                )
                assert wal.stat().st_size > observed[2]
                if checkpoint:
                    assert (
                        writer.connection.execute(
                            "PRAGMA wal_checkpoint(TRUNCATE)"
                        ).fetchone()[0]
                        == 0
                    )
                committed = True
            return observed

        def observe_preflight(repository: Repository, identifier: str) -> None:
            assert committed and repository.get_run("002").state == "created"
            opened = Path(
                repository.connection.execute("PRAGMA database_list").fetchone()[2]
            )
            assert opened.resolve() == database.resolve()
            live_reads.append(opened)
            check(repository, identifier)

        monkeypatch.setattr(
            ReadOnlyRepository, "_file_signature", staticmethod(commit_during_probe)
        )
        monkeypatch.setattr(preparation, "_preflight_v12_write", observe_preflight)
        monkeypatch.setattr(native_batch, "_preflight_v12_write", observe_preflight)
        with patch(
            "judgmentllm.providers.openai._resolve_credentials",
            side_effect=AssertionError("credential lookup"),
        ):
            resume = resume_batch if entry == "batch" else resume_run
            reference = (
                "001" if entry == "batch" else store / "runs" / bytes_digest(b"001")
            )
            resumed = resume(reference, store=store)

    assert committed and live_reads
    assert resumed.batch is not None and resumed.batch.local_stop
    assert resumed.batch.accepted_jobs == 0


def test_control_probe_rejects_wal_growth_with_a_rewritten_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = write_case(tmp_path, capture="minimal")
    original = tmp_path / "original"
    with Repository(original) as held:
        held.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        prepare_batch(config, store=original, run_id="001")
        store = copy_with_orphan_wal(original, tmp_path / "store")
    wal = store / "registry.sqlite3-wal"
    signature = ReadOnlyRepository._file_signature  # pyright: ignore[reportPrivateUsage]
    samples = 0

    def rewrite_after_copy(path: Path) -> tuple[int, int, int, int, int]:
        nonlocal samples
        if path == wal:
            samples += 1
            if samples == 2:
                with wal.open("r+b") as stream:
                    first_byte = stream.read(1)
                    stream.seek(0)
                    stream.write(bytes([first_byte[0] ^ 1]))
                    stream.seek(0, os.SEEK_END)
                    stream.write(b"synthetic append")
        return signature(path)

    monkeypatch.setattr(
        ReadOnlyRepository, "_file_signature", staticmethod(rewrite_after_copy)
    )
    with (
        pytest.raises(IntegrityError, match="registry changed during inspection"),
        ReadOnlyRepository(store, preserve_files=True, allow_wal_appends=True),
    ):
        pass
    assert samples >= 2 and not (store / "registry.sqlite3-shm").exists()


@pytest.mark.parametrize(
    "selection",
    [
        "cache",
        "tier",
        "account",
        "reasoning",
        "sampling",
        "native",
        "strict",
        "tokens",
        "sdk",
    ],
)
def test_preparation_rejects_unqualified_controls_before_store_and_client(
    tmp_path: Path, selection: str
) -> None:
    path = write_case(tmp_path, capture="full")
    config = json.loads(path.read_bytes())
    if selection in ("cache", "tier"):
        config["execution"].update(
            {"cache_policy": "auto"}
            if selection == "cache"
            else {"tier": "standard_only"}
        )
    elif selection == "native":
        config["prompt"] = {
            "structured_output": "native",
            "native_mechanism": "anthropic.messages.output_config.format",
        }
    elif selection in ("reasoning", "sampling", "tokens"):
        config["generation"] = {
            "reasoning": {"reasoning": {"effort": "low", "mode": "adaptive"}},
            "sampling": {"temperature": 0},
            "tokens": {"max_output_tokens": 128001},
        }[selection]
    elif selection == "strict":
        config["strict_capabilities"] = True
    elif selection == "account":
        config["account_scope"] = "synthetic"
    path.write_text(json.dumps(config), encoding="utf-8")
    sdk_access = (
        patch(
            "judgmentllm.anthropic_batch_transport.metadata.version",
            return_value="mismatched SDK",
        )
        if selection == "sdk"
        else patch(
            "judgmentllm.providers.anthropic_batch.require_sdk",
            side_effect=AssertionError("SDK accessed"),
        )
    )
    with (
        patch(
            "judgmentllm.providers.anthropic._resolve_credentials",
            side_effect=AssertionError("credential lookup"),
        ),
        patch(
            "judgmentllm.providers.anthropic_batch._protected_client",
            side_effect=AssertionError("client constructed"),
        ),
        sdk_access,
        pytest.raises(LifecycleError),
    ):
        prepare_batch(path, store=tmp_path / "store", run_id="001")
    assert not (tmp_path / "store").exists()


def test_offline_auto_seals_the_requested_model_and_cap(tmp_path: Path) -> None:
    path = write_case(tmp_path, capture="full")
    config = json.loads(path.read_bytes())
    config["model"] = "claude-synthetic-passthrough"
    config["prompt"] = {
        "structured_output": "auto",
        "native_mechanism": "anthropic.messages.output_config.format",
    }
    config["generation"] = {"max_output_tokens": 128000}
    path.write_text(json.dumps(config), encoding="utf-8")
    store = tmp_path / "store"
    prepare_batch(path, store=store, run_id="001")
    service = Service(tmp_path / "service.sqlite")
    with installed(service):
        assert submit_batch("001", store=store).outcome == "ok"
    for row in json.loads(service.body())["requests"]:
        body = row["params"]
        assert body["model"] == config["model"] and body["max_tokens"] == 128000
        assert not {"output_config", "thinking", "temperature", "top_p"} & body.keys()


@pytest.mark.parametrize("capture", ["full", "minimal"])
def test_remote_derivative_and_credential_echo_cannot_enter_the_store(
    tmp_path: Path,
    capture: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    service = ready(tmp_path, capture=capture)
    member = json.loads(service.body())["requests"][-1]
    forged = canonical_bytes(
        {
            "schema_version": 1,
            "record_type": "screened_anthropic_batch_row_v1",
            "custom_id": member["custom_id"],
            "provider_result": "succeeded",
            "kind": "response",
            "assessment_text": PRIVATE,
            "usage": ReportedUsageInput().model_dump(mode="json"),
            "remote_response_id": "msg_forged",
            "reported_model": member["params"]["model"],
            "provider_outcome": "completed",
        }
    )
    service.set("first_wire_row", forged.decode())
    service.set("message_override", SECRET)
    with installed(service), caplog.at_level(logging.DEBUG):
        result = collect_batch("001", store=tmp_path / "store")
    assert result.batch.missing_members == 2 and result.batch.recovery_required
    assert len(records(tmp_path, "kind='withheld'")) == 2
    assert PRIVATE not in caplog.text and SECRET not in caplog.text
    export_batch_provenance("001", store=tmp_path / "store")
    assert_private(tmp_path, extra=(forged,))


def child(root: Path, action: str, fault: str = "") -> subprocess.CompletedProcess[str]:
    repo = Path(__file__).resolve().parents[1]
    env = {
        "PATH": os.defpath,
        "PYTHONPATH": os.pathsep.join((str(repo), str(repo / "src"))),
        "TMPDIR": str(root),
        "TMP": str(root),
        "TEMP": str(root),
    }
    if sys.platform == "win32":
        env["SystemRoot"] = os.environ["SystemRoot"]
    return subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            """
import os, sys
from pathlib import Path
from scripts.anthropic_batch_fixture import Service, installed
from judgmentllm import submit_batch, collect_batch, resume_batch, cancel_batch, poll_batch, get_batch_status
root = Path(sys.argv[1])
action, selected = sys.argv[2:]
if action == 'status':
    print(get_batch_status('001', store=root / 'store').model_dump_json())
    sys.exit(0)
def fault(stage, identity):
    if stage == selected:
        os._exit(77)
with installed(Service(root / 'service.sqlite')):
    result = {'submit': submit_batch, 'collect': collect_batch, 'resume': resume_batch, 'cancel': cancel_batch, 'poll': poll_batch}[action]('001', store=root / 'store', _fault=fault)
    print(result.model_dump_json())
""",
            str(root),
            action,
            fault,
        ],
        env=env,
        cwd=root,
        text=True,
        capture_output=True,
        timeout=90,
        check=False,
    )


@pytest.mark.parametrize("capture", ["full", "minimal"])
@pytest.mark.parametrize("death", ["headers", "result"])
def test_hard_death_preserves_diagnostics_and_authority(
    tmp_path: Path, capture: str, death: str
) -> None:
    store = tmp_path / "store"
    service = Service(tmp_path / "service.sqlite")
    if death == "headers":
        prepare_batch(write_case(tmp_path, capture=capture), store=store, run_id="001")
        stopped = child(tmp_path, "submit", "after_anthropic_batch_headers")
    else:
        ready(tmp_path, capture=capture)
        stopped = child(tmp_path, "collect", "after_batch_member_results")
    assert stopped.returncode == 77, stopped.stderr
    diagnosed = records(tmp_path)
    last = diagnosed[-1]
    assert last["phase"] == "headers" and last["http_status"] == 200
    assert last["stage"] == ("create" if death == "headers" else "results_content")
    inspected = child(tmp_path, "status")
    assert inspected.returncode == 0, inspected.stderr
    snapshot = json.loads(inspected.stdout)["batch"]
    assert snapshot["unresolved_operations"] == (1 if death == "headers" else 0)
    assert snapshot["missing_members"] == (2 if death == "headers" else 1)
    recovered = child(tmp_path, "resume")
    assert recovered.returncode == 0, recovered.stderr
    status = get_batch_status("001", store=store)
    assert status.batch.unresolved_operations == (1 if death == "headers" else 0)
    assert status.batch.missing_members == (2 if death == "headers" else 0)
    assert service.events().count(("POST", BATCH_PATH)) == 1
    assert_private(tmp_path)


@pytest.mark.parametrize("capture", ["full", "minimal"])
@pytest.mark.parametrize("receipt_kind", ["create", "member"])
def test_named_receipt_survives_death_before_sql(
    tmp_path: Path,
    capture: str,
    receipt_kind: str,
) -> None:
    store = tmp_path / "store"
    if receipt_kind == "create":
        config = write_case(tmp_path, capture=capture)
        prepare_batch(config, store=store, run_id="001")
        service = Service(tmp_path / "service.sqlite")
        stopped = child(tmp_path, "submit", "after_batch_operation_receipt")
        reference = "operations"
    else:
        service = ready(tmp_path, capture=capture)
        stopped = child(tmp_path, "collect", "after_batch_member_receipt")
        reference = "members"
    assert stopped.returncode == 77, stopped.stderr
    with ReadOnlyRepository(store, preserve_files=True) as repo:
        (path,) = (repo.run_directory("001") / "batch" / reference).glob("*.json")
        proof = json.loads(path.read_bytes())
        assert repo.summary("001").results == 0
        assert not repo.connection.execute(
            "SELECT 1 FROM batch_member_receipts"
        ).fetchone()
        if receipt_kind == "create":
            assert not repo.connection.execute(
                "SELECT 1 FROM batch_facts WHERE kind='operation'"
            ).fetchone()
            assert (proof["remote_id"], proof["created_at"], proof["expires_at"]) == (
                "msgbatch_1",
                "2026-11-01T00:00:00Z",
                "2026-11-02T00:00:00Z",
            )
        else:
            assert proof["source_position"] == 0 and proof["capture"] == capture
            assert bool(proof["retained"]["relative_path"]) == (capture == "full")

    if receipt_kind == "member":
        service.set("interrupt_result_job", 1)
        recovering = child(tmp_path, "resume")
        assert recovering.returncode == 0, recovering.stderr
        with ReadOnlyRepository(store) as repo:
            assert repo.summary("001").results == (2 if capture == "full" else 0)
        service.set("interrupt_result_job", None)

    recovered = child(tmp_path, "resume")
    assert recovered.returncode == 0, recovered.stderr
    assert not json.loads(recovered.stdout)["batch"]["recovery_required"]
    assert service.events().count(("POST", BATCH_PATH)) == 1
    with ReadOnlyRepository(store) as repo:
        assert repo.summary("001").successful_results == 4
        receipts = [
            json.loads(row[0])
            for row in repo.connection.execute(
                "SELECT record_json FROM batch_member_receipts"
            )
        ]
        assert len(receipts) == 2
        assert sum(row["usage"]["input_tokens"] for row in receipts) == 14
        assert sum(row["usage"]["output_tokens"] for row in receipts) == 6
        if receipt_kind == "member":
            assert proof in receipts
    assert_private(tmp_path)


@pytest.mark.parametrize("field", ["created_at", "expires_at"])
def test_first_status_cannot_rebind_create_timestamps(
    tmp_path: Path, field: str
) -> None:
    store = tmp_path / "store"
    prepare_batch(write_case(tmp_path, capture="minimal"), store=store, run_id="001")
    service = Service(tmp_path / "service.sqlite")
    with installed(service):
        assert submit_batch("001", store=store).outcome == "ok"
        service.set("time_drift", field)
        assert poll_batch("001", store=store).outcome == "error"
        assert collect_batch("001", store=store).outcome == "error"
    assert not records(tmp_path, "entity_id LIKE 'anthropic-ready:%'")
    assert (
        records(tmp_path, "entity_id LIKE 'anthropic-admission:%'")[-1]["code"]
        == "source_conflict"
    )
    assert not any(
        path.endswith("/results")
        for method, path in service.events()
        if method == "GET"
    )
    assert service.events().count(("POST", BATCH_PATH)) == 1


def test_restart_rejects_moving_a_receipt_ahead_of_the_cursor(tmp_path: Path) -> None:
    service = ready(tmp_path, capture="minimal")
    stopped = child(tmp_path, "collect", "after_batch_member_results")
    assert stopped.returncode == 77, stopped.stderr
    assert not records(
        tmp_path, "kind='cursor' AND json_extract(record_json,'$.position') IS NOT NULL"
    )
    service.set("swap", True)
    with installed(service):
        result = collect_batch("001", store=tmp_path / "store")
    assert result.outcome == "error" and result.batch.missing_members == 1
    assert result.collection is not None and result.collection.codes == (
        "source_conflict",
    )
    assert service.events().count(("POST", BATCH_PATH)) == 1
    with ReadOnlyRepository(tmp_path / "store") as repo:
        assert (
            repo.connection.execute(
                "SELECT count(*) FROM batch_member_receipts"
            ).fetchone()[0]
            == 1
        )


def test_explicit_retry_seals_one_selected_task_in_a_new_direct_create(
    tmp_path: Path,
) -> None:
    config = write_case(tmp_path, capture="minimal")
    store = tmp_path / "store"
    service = Service(tmp_path / "service.sqlite")
    service.set("first_result", "errored")
    prepare_batch(config, store=store, run_id="001")
    with installed(service):
        assert resume_batch("001", store=store).outcome == "ok"
    with ReadOnlyRepository(store) as repo:
        parents = list(repo.iter_results("001"))
        failed = next(row for row in parents if row.status == "errored")
    retry = RetrySelection(task_ids=(failed.task_id,), operation_id="retry-01")
    service.set("traits_2", [failed.trait_id])
    with installed(service):
        assert resume_batch("001", store=store, retry=retry).outcome == "ok"
        assert resume_batch("001", store=store, retry=retry).outcome == "ok"
    assert service.events().count(("POST", BATCH_PATH)) == 2
    original = json.loads(service.body())["requests"]
    (child_request,) = json.loads(service.body(2))["requests"]
    assert child_request["custom_id"] not in {r["custom_id"] for r in original}
    assert child_request["params"]["service_tier"] == "standard_only"
    with ReadOnlyRepository(store) as repo:
        results = list(repo.iter_results("001"))
        assert results[: len(parents)] == parents
        (retried,) = results[len(parents) :]
        assert retried.status == "complete" and retried.task_id == failed.task_id
        assert (
            retried.request_uid == failed.request_uid
            and retried.attempt_id != failed.attempt_id
        )


def test_second_shard_cannot_accept_an_already_bound_job(tmp_path: Path) -> None:
    service = ready(tmp_path, capture="minimal")
    store = tmp_path / "store"
    with installed(service):
        assert collect_batch("001", store=store).outcome == "ok"
        with ReadOnlyRepository(store) as repo:
            parent = next(repo.iter_results("001"))
        service.set("duplicate_create_id", 1)
        retry = RetrySelection(
            task_ids=(parent.task_id,),
            operation_id="retry-duplicate",
            allow_completed=True,
        )
        report = resume_batch("001", store=store, retry=retry)
        assert report.outcome == "partial"
        assert report.batch.accepted_jobs == report.batch.unresolved_operations == 1
    restarted = child(tmp_path, "resume")
    assert restarted.returncode == 0, restarted.stderr
    assert json.loads(restarted.stdout)["batch"]["unresolved_operations"] == 1
    assert service.events().count(("POST", BATCH_PATH)) == 2
    with ReadOnlyRepository(store) as repo:
        assert (
            len(
                list(
                    (repo.run_directory("001") / "batch" / "operations").glob("*.json")
                )
            )
            == 1
        )
        assert [
            row[0]
            for row in repo.connection.execute(
                "SELECT remote_id FROM submissions WHERE certainty='accepted'"
            )
        ] == ["msgbatch_1"]
        assert repo.summary("001").successful_results == 4


def test_ready_rotation_picks_another_shard_after_a_stall_and_restart(
    tmp_path: Path,
) -> None:
    service = ready(tmp_path, capture="minimal")
    store = tmp_path / "store"
    with installed(service):
        assert collect_batch("001", store=store).outcome == "ok"
        with ReadOnlyRepository(store) as repo:
            parents = [row for row in repo.iter_results("001") if row.trait_id == "01"]
        service.set("status", "in_progress")
        for ordinal, row in enumerate(parents, start=2):
            service.set(f"traits_{ordinal}", ["01"])
            retry = RetrySelection(
                task_ids=(row.task_id,),
                operation_id=f"retry-{ordinal}",
                allow_completed=True,
            )
            assert (
                resume_batch("001", store=store, retry=retry).batch.accepted_jobs
                == ordinal
            )
        service.set("status", "ended")
        assert poll_batch("001", store=store).outcome == "ok"

        service.set("stall_result_job", 2)
        until_stall = collect_batch(
            "001", store=store, options=BatchControlOptionsV1(deadline_seconds=1.5)
        )
        assert (
            until_stall.outcome == "partial" and until_stall.batch.missing_members == 2
        )
    resumed = child(tmp_path, "resume")
    assert resumed.returncode == 0, resumed.stderr
    assert json.loads(resumed.stdout)["batch"]["missing_members"] == 1
    service.set("stall_result_job", None)
    with installed(service):
        assert not collect_batch("001", store=store).batch.recovery_required
    assert [
        path
        for method, path in service.events()
        if method == "GET" and path.endswith("/results")
    ] == [f"{BATCH_PATH}/msgbatch_{ordinal}/results" for ordinal in (1, 2, 3, 2)]
    assert service.events().count(("POST", BATCH_PATH)) == 3


def test_named_audit_reopen_verifies_retained_diagnostic_body(tmp_path: Path) -> None:
    ready(tmp_path)
    store = tmp_path / "store"
    audit = export_batch_provenance("001", store=store)
    stored = next(r["body"] for r in records(tmp_path) if r["body"])
    run_dir = Path(audit.manifest_path).parents[2]
    (run_dir / stored["relative_path"]).write_bytes(b"damaged synthetic body")
    with pytest.raises(LifecycleError, match="durable run integrity"):
        export_batch_provenance("001", store=store, bundle_id=audit.manifest.bundle_id)


@pytest.mark.parametrize("first", ["errored", "expired", "succeeded"])
def test_outer_tally_is_independent_of_assessment_validity(
    tmp_path: Path, first: str
) -> None:
    config = write_case(tmp_path, capture="full")
    store = tmp_path / "store"
    service = Service(tmp_path / "service.sqlite")
    service.set("first_result", first)
    service.set("invalid_assessment", first == "succeeded")
    prepare_batch(config, store=store, run_id="001")
    with installed(service):
        submit_batch("001", store=store)
        poll_batch("001", store=store)
        assert collect_batch("001", store=store).outcome == "ok"
    with ReadOnlyRepository(store) as repo:
        usage = [
            json.loads(row[0])
            for row in repo.connection.execute(
                "SELECT record_json FROM batch_member_receipts"
            )
        ]
        assert {row["provider_result"] for row in usage} == {first, "succeeded"}
        assert (
            sum(result.status == "complete" for result in repo.iter_results("001")) == 2
        )
        assert sum(row["usage"]["input_tokens"] or 0 for row in usage) == (
            14 if first == "succeeded" else 7
        )
    assert service.events().count(("POST", BATCH_PATH)) == 1


def test_pending_ready_repair_and_archive(tmp_path: Path) -> None:
    config = write_case(tmp_path, capture="full")
    store = tmp_path / "store"
    service = Service(tmp_path / "service.sqlite")
    prepare_batch(config, store=store, run_id="001")
    service.set("pending_url", True)
    with installed(service):
        submit_batch("001", store=store)
        poll_batch("001", store=store)
        assert collect_batch("001", store=store).batch.recovery_required
    assert not any(p.endswith("/results") for m, p in service.events() if m == "GET")
    service.set("pending_url", False)
    stopped = child(tmp_path, "poll", "after_anthropic_terminal_receipt")
    assert stopped.returncode == 77, stopped.stderr
    service.set("archived", True)
    recovered = child(tmp_path, "collect")
    assert recovered.returncode == 0, recovered.stderr
    state = get_batch_status("001", store=store)
    assert state.collection is not None and state.collection.codes == (
        "source_unavailable",
    )
    assert not any(p.endswith("/results") for m, p in service.events() if m == "GET")


def test_cancel_read_does_not_create_post_receipt(tmp_path: Path) -> None:
    config = write_case(tmp_path, capture="minimal")
    store = tmp_path / "store"
    service = Service(tmp_path / "service.sqlite")
    prepare_batch(config, store=store, run_id="001")
    with installed(service):
        submit_batch("001", store=store)
    stopped = child(tmp_path, "cancel", "after_batch_forward")
    assert stopped.returncode == 77, stopped.stderr
    with installed(service):
        result = resume_batch("001", store=store)
    assert result.batch.cancel_all and result.batch.unresolved_operations == 1
    observed = records(tmp_path, "entity_id LIKE 'anthropic-cancel:%'")
    assert (
        len(observed) == 1 and observed[0]["status"]["cancel_initiated_at"] is not None
    )
    with ReadOnlyRepository(store) as repo:
        assert sorted(r.status for r in repo.iter_results("001")) == [
            "cancelled",
            "cancelled",
            "complete",
            "complete",
        ]
    with installed(service):
        resume_batch("001", store=store)
    assert service.events().count(("POST", BATCH_PATH + "/msgbatch_1/cancel")) == 1
    assert (
        len(
            {
                x["call_id"]
                for x in records(tmp_path)
                if x["stage"] == "cancel_reconciliation"
            }
        )
        == 1
    )


@pytest.mark.parametrize("dimension", ["bytes", "steps"])
@pytest.mark.parametrize("fit", [False, True])
def test_collection_reserve_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dimension: str, fit: bool
) -> None:
    service = ready(tmp_path)
    store = tmp_path / "store"
    options = BatchControlOptionsV1()
    if dimension == "steps":
        options = BatchControlOptionsV1(max_io_steps=581 if fit else 580)
    else:
        from httpx2 import Response

        metadata = service.metadata(
            1, json.loads(service.body())["requests"], "ended", False
        )
        total = len(Response(200, json=metadata).content) + 17 * 1_048_576
        monkeypatch.setattr(
            InvocationBudget,
            "byte_limit",
            property(lambda _: total if fit else total - 1),
        )
    with installed(service):
        result = collect_batch("001", store=store, options=options)
    assert result.outcome == ("ok" if fit else "partial")
    assert service.events().count(("GET", BATCH_PATH + "/msgbatch_1/results")) == int(
        fit
    )
    assert len(records(tmp_path, "entity_id LIKE 'anthropic-admission:%'")) == int(fit)


@pytest.mark.serial
def test_exact_local_source_and_record_limits_replay_over_default_quota(
    tmp_path: Path,
) -> None:
    service = ready(tmp_path, capture="minimal", count=129)
    service.set("frame_size", [1_048_576] + [57_344] * 128)
    with installed(service):
        first = collect_batch("001", store=tmp_path / "store")
        assert first.batch.missing_members == 1 and first.batch.recovery_required
        second = collect_batch("001", store=tmp_path / "store")
    assert not second.batch.recovery_required
    assert service.events().count(("POST", BATCH_PATH)) == 1


def test_only_three_admitted_replays_without_progress(tmp_path: Path) -> None:
    service = ready(tmp_path, capture="minimal")
    store = tmp_path / "store"
    service.set("interrupt", True)
    with installed(service):
        assert collect_batch("001", store=store).batch.missing_members == 1
    for _ in range(4):
        with installed(Service(service.path)):
            collect_batch("001", store=store)
    result = get_batch_status("001", store=store)
    assert result.outcome == "error" and result.batch.missing_members == 1
    assert result.collection is not None and result.collection.codes == (
        "collection_no_progress",
    )
    assert service.events().count(("GET", BATCH_PATH + "/msgbatch_1/results")) == 4
    assert service.events().count(("POST", BATCH_PATH)) == 1


@pytest.mark.parametrize(
    "fault",
    [
        "over_source",
        "false_length",
        "fragmented_frame",
        "incomplete_json",
        "outer_tally",
        "missing",
        "stalled_eof",
    ],
)
def test_source_failures_do_not_claim_completion(tmp_path: Path, fault: str) -> None:
    service = ready(tmp_path, count=9 if fault == "over_source" else 2)
    options = BatchControlOptionsV1()
    if fault == "over_source":
        service.set("frame_size", [1_048_576] * 8 + [1024])
    elif fault == "false_length":
        service.set("declared_length", 65_536)
    elif fault == "fragmented_frame":
        service.set("frame_size", [1_048_577, 1024])
        service.set("fragment_bytes", 33_147)
    elif fault == "incomplete_json":
        service.set("last_wire_row", '{"custom_id":')
        service.set("cut_final_newline", True)
        service.set("fragment_bytes", 32)
    elif fault == "outer_tally":
        member = json.loads(service.body())["requests"][0]
        service.set(
            "last_wire_row",
            json.dumps(
                {"custom_id": member["custom_id"], "result": {"type": "expired"}}
            ),
        )
    elif fault == "stalled_eof":
        service.set("stall_eof", True)
        options = BatchControlOptionsV1(deadline_seconds=0.8)
    else:
        service.set("missing", True)
    with installed(service):
        state = collect_batch("001", store=tmp_path / "store", options=options)
    assert state.batch.recovery_required
    assert service.events().count(("POST", BATCH_PATH)) == 1
    if fault in ("incomplete_json", "outer_tally"):
        assert state.batch.missing_members == 1
        assert state.collection is not None
        assert state.collection.codes == (
            "source_framing" if fault == "incomplete_json" else "source_conflict",
        )
    assert any(
        row["exhausted"]
        for row in records(
            tmp_path, "kind='cursor' AND json_extract(record_json,'$.exhausted')=1"
        )
    ) == (fault == "missing")
