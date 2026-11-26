"""Fresh-process duplicate safety, exact source size and durable fair selection."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from openai_batch_fixture import (
    Service,
    child,
    diagnostic_records,
    install,
    specification,
)

from judgmentllm import (
    BatchControlOptionsV1,
    collect_batch,
    poll_batch,
    prepare_batch,
    submit_batch,
)
from judgmentllm.persistence.repository import ReadOnlyRepository


def setup(
    tmp_path: Any,
    monkeypatch: Any,
    *,
    endpoint: Any = "responses",
    capture: Any = "full",
    responses: Any = 2,
) -> Any:
    service = Service(tmp_path / "service.sqlite")
    install(monkeypatch, service)
    spec = specification(
        tmp_path, endpoint=endpoint, capture=capture, responses=responses
    )
    store = tmp_path / "store"
    prepare_batch(spec, store=store, run_id="001")
    service.configure(store, spec)
    submit_batch("001", store=store, spec=spec)
    poll_batch("001", store=store)
    return service, spec, store


@pytest.mark.parametrize("capture", ["full", "minimal"])
@pytest.mark.parametrize("endpoint", ["responses", "chat.completions"])
def test_accepted_finalizing_poll_transient_same_ids(
    tmp_path: Any, monkeypatch: Any, endpoint: Any, capture: Any
) -> Any:
    service = Service(tmp_path / "service.sqlite")
    install(monkeypatch, service)
    spec = specification(tmp_path, endpoint=endpoint, capture=capture)
    (tmp_path / "spec.json").write_text(spec.model_dump_json())
    child(tmp_path, "prepare")
    service.configure(tmp_path / "store", spec)
    service.set("initial_status", "finalizing")
    child(tmp_path, "submit")
    child(tmp_path, "poll")
    before = [e for e in service.events() if e[0] == "POST"]
    service.set("stall_poll", "batch_1")
    assert child(tmp_path, "resume")["outcome"] == "partial"
    service.set("stall_poll", None)
    with service.connect() as db:
        body = json.loads(
            db.execute("SELECT body FROM jobs WHERE id='batch_1'").fetchone()[0]
        )
        body["status"] = "completed"
        db.execute("UPDATE jobs SET body=? WHERE id='batch_1'", (json.dumps(body),))
    final = None
    for _ in range(4):
        final = child(tmp_path, "resume")
        if not final["batch"]["recovery_required"]:
            break
    assert final is not None
    assert not final["batch"]["recovery_required"]
    assert [e for e in service.events() if e[0] == "POST"] == before
    with ReadOnlyRepository(tmp_path / "store") as repo:
        assert repo.summary("001").attempts == 2
        assert (
            repo.connection.execute("SELECT remote_id FROM submissions").fetchall()[0][
                0
            ]
            == "batch_1"
        )


@pytest.mark.parametrize("capture", ["full", "minimal"])
@pytest.mark.parametrize("fault", ["lose_upload_ack", "lose_create_ack"])
def test_acceptance_uncertainty_never_resubmits(
    tmp_path: Any, monkeypatch: Any, capture: Any, fault: Any
) -> Any:
    service = Service(tmp_path / "service.sqlite")
    install(monkeypatch, service)
    spec = specification(tmp_path, capture=capture)
    (tmp_path / "spec.json").write_text(spec.model_dump_json())
    child(tmp_path, "prepare")
    service.configure(tmp_path / "store", spec)
    service.set(fault, True)
    first = child(tmp_path, "submit")
    assert first["outcome"] == "partial"
    service.set(fault, False)
    child(tmp_path, "resume")
    assert len([e for e in service.events() if e == ("POST", "/v1/files")]) == 1
    assert len([e for e in service.events() if e == ("POST", "/v1/batches")]) == 1
    operation = "upload" if fault == "lose_upload_ack" else "create"
    (lost,) = diagnostic_records(tmp_path / "store", f"{operation}_post")
    (listing,) = diagnostic_records(tmp_path / "store", f"{operation}_list")
    (positive,) = diagnostic_records(
        tmp_path / "store", "upload_content" if operation == "upload" else "create_get"
    )
    assert lost["reason"] == "delegate" and lost["http_status"] is None
    assert lost["binding_id"] == listing["binding_id"] == positive["binding_id"]
    assert len({lost["call_id"], listing["call_id"], positive["call_id"]}) == 3
    with ReadOnlyRepository(tmp_path / "store") as repo:
        assert tuple(
            repo.connection.execute(
                "SELECT certainty,remote_id FROM submissions"
            ).fetchone()
        ) == (
            "accepted",
            "batch_1",
        )


@pytest.mark.parametrize(
    "declared,actual,code",
    [
        (0, 0, None),
        (8_388_609, 0, "source_limit"),
        (1_073_741_824, 0, "source_limit"),
        (1, 2, "source_overrun"),
        (2, 1, "source_underrun"),
    ],
)
def test_pinned_size_and_actual_eof(
    tmp_path: Any, monkeypatch: Any, declared: Any, actual: Any, code: Any
) -> Any:
    service, spec, store = setup(tmp_path, monkeypatch)
    service.file("file-output_1", body=b" " * actual, declared=declared)
    before = len(service.events())
    result = collect_batch("001", store=store, spec=spec)
    assert result.collection is not None
    if code is None:
        assert result.collection.failed_sources == 0
        with ReadOnlyRepository(store) as repo:
            cursors = [
                json.loads(r[0])
                for r in repo.connection.execute(
                    "SELECT record_json FROM batch_facts WHERE kind='cursor' AND entity_id NOT LIKE 'openai-%'"
                )
            ]
            assert any(c.get("exhausted") and c["position"] == 0 for c in cursors)
    else:
        assert code in result.collection.codes
        assert result.outcome == "error"
    if declared > 8_388_608:
        assert ("GET", "/v1/files/file-output_1/content") not in service.events()[
            before:
        ]


@pytest.mark.parametrize("capture", ["full", "minimal"])
def test_changed_metadata_preserves_old_evidence(
    tmp_path: Any, monkeypatch: Any, capture: Any
) -> Any:
    service, spec, store = setup(tmp_path, monkeypatch, capture=capture)
    first = collect_batch(
        "001", store=store, spec=spec, options=BatchControlOptionsV1(max_result_rows=1)
    )
    assert first.batch.missing_members == 1
    with ReadOnlyRepository(store) as repo:
        before = repo.connection.execute(
            "SELECT receipt_sha256 FROM batch_member_receipts"
        ).fetchall()
        binding = repo.connection.execute(
            "SELECT record_json FROM batch_facts WHERE entity_id LIKE 'openai-binding:%'"
        ).fetchone()[0]
    _body, declared = service.file("file-output_1")
    service.file("file-output_1", declared=declared + 1)
    collect_batch("001", store=store, spec=spec)  # Independent zero-byte error file.
    result = collect_batch("001", store=store, spec=spec)
    assert result.collection is not None
    assert result.outcome == "error" and "source_conflict" in result.collection.codes
    with ReadOnlyRepository(store) as repo:
        assert (
            repo.connection.execute(
                "SELECT receipt_sha256 FROM batch_member_receipts"
            ).fetchall()
            == before
        )
        assert (
            repo.connection.execute(
                "SELECT record_json FROM batch_facts WHERE entity_id LIKE 'openai-binding:%'"
            ).fetchone()[0]
            == binding
        )


@pytest.mark.parametrize("capture", ["full", "minimal"])
@pytest.mark.parametrize("endpoint", ["responses", "chat.completions"])
@pytest.mark.serial
def test_default_fresh_process_pressure_and_early_poll_fairness(
    tmp_path: Any, endpoint: Any, capture: Any
) -> Any:
    spec = specification(
        tmp_path, endpoint=endpoint, capture=capture, responses=384, explanations=True
    )
    (tmp_path / "spec.json").write_text(spec.model_dump_json())
    service = Service(tmp_path / "service.sqlite")
    child(tmp_path, "prepare", small_shards=192)
    service.configure(tmp_path / "store", spec, padding=32_000)
    child(tmp_path, "submit", small_shards=192)
    with service.connect() as db:
        body = json.loads(
            db.execute("SELECT body FROM jobs WHERE id='batch_1'").fetchone()[0]
        )
        body["status"] = "in_progress"
        db.execute("UPDATE jobs SET body=? WHERE id='batch_1'", (json.dumps(body),))
    child(tmp_path, "poll", small_shards=192)
    service.set("stall_poll", "batch_1")
    start = len(service.events())
    first = child(tmp_path, "resume", small_shards=192)
    assert first["batch"]["missing_members"] == 256
    assert first["outcome"] == "partial"
    child(tmp_path, "resume", small_shards=192)  # Independent error source, real EOF.
    third = child(tmp_path, "resume", small_shards=192)
    assert third["batch"]["missing_members"] == 192
    events = service.events()[start:]
    assert ("GET", "/v1/batches/batch_1") not in events
    assert (
        len([e for e in events if e == ("GET", "/v1/files/file-output_2/content")]) == 2
    )
    with service.connect() as db:
        size = db.execute(
            "SELECT declared FROM files WHERE id='file-output_2'"
        ).fetchone()[0]
        reads = db.execute(
            "SELECT sum(byte_count) FROM reads WHERE file='file-output_2'"
        ).fetchone()[0]
        assert 6_000_000 < size <= 8_388_608
        assert reads > size + 4_000_000  # A fresh process actually replays the prefix.
    assert len([e for e in service.events() if e[0] == "POST"]) == 4


@pytest.mark.parametrize("capture", ["full", "minimal"])
def test_exact_eight_mib_multiple_bounded_rows_and_overrun(
    tmp_path: Any, monkeypatch: Any, capture: Any
) -> Any:
    service, spec, store = setup(tmp_path, monkeypatch, capture=capture)
    original = service.file("file-output_1")[0].splitlines()[0]
    row = b" " * (1_048_576 - len(original) - 1) + original + b"\n"
    service.file("file-output_1", body=row * 8, declared=8_388_608)
    result = collect_batch("001", store=store, spec=spec)
    assert result.collection is not None
    assert result.collection.failed_sources == 0
    with ReadOnlyRepository(store) as repo:
        cursor = [
            json.loads(r[0])
            for r in repo.connection.execute(
                "SELECT record_json FROM batch_facts WHERE kind='cursor' AND entity_id NOT LIKE 'openai-%' ORDER BY fact_seq"
            )
        ][-1]
        assert cursor["position"] == 8 and cursor["exhausted"]
        assert (
            repo.connection.execute(
                "SELECT count(*) FROM batch_member_receipts"
            ).fetchone()[0]
            == 1
        )


@pytest.mark.parametrize("field", ["input_file_id", "endpoint", "id"])
def test_job_association_is_required_even_with_matching_content(
    tmp_path: Any, monkeypatch: Any, field: Any
) -> Any:
    service, _spec, store = setup(tmp_path, monkeypatch)
    before = service.events()
    with service.connect() as db:
        body = json.loads(db.execute("SELECT body FROM jobs").fetchone()[0])
        body[field] = {
            "input_file_id": "file-wrong",
            "endpoint": "/v1/chat/completions",
            "id": "batch_wrong",
        }[field]
        db.execute("UPDATE jobs SET body=?", (json.dumps(body),))
    result = poll_batch("001", store=store)
    assert result.outcome != "ok"
    assert len([e for e in service.events() if e[0] == "POST"]) == len(
        [e for e in before if e[0] == "POST"]
    )
    assert not any(path.endswith("/content") for _, path in service.events())


@pytest.mark.parametrize("capture", ["full", "minimal"])
@pytest.mark.serial
def test_three_fragmentation_stalls_survive_fresh_processes_without_fourth_read(
    tmp_path: Any, capture: Any
) -> Any:
    spec = specification(tmp_path, capture=capture, responses=192, explanations=True)
    (tmp_path / "spec.json").write_text(spec.model_dump_json())
    service = Service(tmp_path / "service.sqlite")
    child(tmp_path, "prepare")
    service.configure(tmp_path / "store", spec, padding=32_000)
    child(tmp_path, "submit")
    child(tmp_path, "poll")
    first = child(tmp_path, "collect")
    assert first["batch"]["missing_members"] == 64
    service.set("fragment_bytes", 1)
    child(tmp_path, "collect")  # empty error file, independently exhausted
    for expected in (1, 2, 3):
        result = child(tmp_path, "collect")
        with ReadOnlyRepository(tmp_path / "store") as repo:
            latest = json.loads(
                repo.connection.execute(
                    "SELECT record_json FROM batch_facts WHERE entity_id LIKE 'openai-admission:%' AND json_extract(record_json,'$.stalls')>0 ORDER BY fact_seq DESC LIMIT 1"
                ).fetchone()[0]
            )
            assert latest["stalls"] == expected
        assert result["outcome"] == ("error" if expected == 3 else "partial")
    before = service.events()
    result = child(tmp_path, "collect")
    assert result["outcome"] == "error"
    assert not any(
        path.endswith("/content") for _, path in service.events()[len(before) :]
    )


@pytest.mark.parametrize("capture", ["full", "minimal"])
@pytest.mark.serial
def test_real_default_deadline_at_eof_cannot_loop_forever_after_restart(
    tmp_path: Any, capture: Any
) -> None:
    import time

    deadline = time.monotonic() + 180

    def run(action: str) -> Any:
        return child(tmp_path, action, timeout=max(0.1, deadline - time.monotonic()))

    spec = specification(tmp_path, capture=capture, responses=128)
    (tmp_path / "spec.json").write_text(spec.model_dump_json())
    service = Service(tmp_path / "service.sqlite")
    run("prepare")
    service.configure(tmp_path / "store", spec)
    run("submit")
    run("poll")
    service.set("eof_delay_file", "file-output_1")
    service.set("eof_delay", 31)
    first = run("collect")
    assert first["batch"]["missing_members"] == 0
    assert first["outcome"] == "partial"
    run("collect")  # independent zero-byte error file genuinely reaches EOF
    for count in (1, 2, 3):
        result = run("collect")
        assert result["outcome"] == ("error" if count == 3 else "partial")
        with ReadOnlyRepository(tmp_path / "store") as repo:
            state = json.loads(
                repo.connection.execute(
                    "SELECT record_json FROM batch_facts WHERE entity_id LIKE 'openai-admission:%' AND json_extract(record_json,'$.stalls')>0 ORDER BY fact_seq DESC LIMIT 1"
                ).fetchone()[0]
            )
            assert state["stalls"] == count
            assert state["frontier"] == 128
            assert not repo.connection.execute(
                "SELECT 1 FROM batch_facts WHERE kind='cursor' AND json_extract(record_json,'$.exhausted')=1 AND json_extract(record_json,'$.position')=128"
            ).fetchone()
    before = len(service.events())
    assert run("collect")["outcome"] == "error"
    assert not any(path.endswith("/content") for _, path in service.events()[before:])
    assert len([x for x in service.events() if x[0] == "POST"]) == 2
    assert time.monotonic() < deadline


@pytest.mark.parametrize(
    "platform,system_root,filesystem,expected_system_root",
    [
        pytest.param("win32", r"C:\Windows", "directory", True, id="windows-present"),
        pytest.param("win32", None, "forbidden", False, id="windows-missing"),
        pytest.param("win32", "", "forbidden", False, id="windows-empty"),
        pytest.param("linux", r"C:\Windows", "forbidden", False, id="posix-present"),
        pytest.param("linux", None, "forbidden", False, id="posix-missing"),
        pytest.param("win32", " \t\r\n", "forbidden", False, id="windows-whitespace"),
        pytest.param("win32", "Windows", "forbidden", False, id="windows-relative"),
        pytest.param(
            "win32", "C:Windows", "forbidden", False, id="windows-drive-relative"
        ),
        pytest.param(
            "win32", r"\Windows", "forbidden", False, id="windows-root-relative"
        ),
        pytest.param("win32", r"C:\Windows", "file", False, id="windows-nondirectory"),
        pytest.param(
            "win32", r"C:\Windows", "lookup-error", False, id="windows-lookup-error"
        ),
        pytest.param(
            "win32",
            r"\\synthetic-server\share\Windows",
            "forbidden",
            False,
            id="windows-unc",
        ),
        pytest.param(
            "win32", r"\\?\C:\Windows", "forbidden", False, id="windows-device"
        ),
        pytest.param(
            "win32", r"\\.\C:\Windows", "forbidden", False, id="windows-dos-device"
        ),
        pytest.param(
            "win32", r"C:\NUL\Windows", "forbidden", False, id="windows-reserved-device"
        ),
        pytest.param("win32", "C:\\Win\0dows", "forbidden", False, id="windows-nul"),
        pytest.param(
            "win32",
            r"C:\link\Windows",
            "parent-reparse",
            False,
            id="windows-parent-reparse",
        ),
        pytest.param(
            "win32", r"C:\Windows", "leaf-reparse", False, id="windows-leaf-reparse"
        ),
        pytest.param(
            "win32", r"Z:\Windows", "remote-drive", False, id="windows-remote-drive"
        ),
        pytest.param(
            "win32", r"C:\Windows", "unknown-drive", False, id="windows-unknown-drive"
        ),
        pytest.param(
            "win32",
            r"C:\Windows",
            "drive-error",
            False,
            id="windows-drive-lookup-error",
        ),
    ],
)
def test_child_environment_inherits_only_windows_system_root(
    tmp_path: Any,
    monkeypatch: Any,
    platform: str,
    system_root: str | None,
    filesystem: str,
    expected_system_root: bool,
) -> None:
    import ctypes
    import stat
    from pathlib import Path
    from types import SimpleNamespace

    import openai_batch_fixture as fixture

    inherited = {
        "PATH": "synthetic-parent-path",
        "OPENAI_API_KEY": "synthetic-secret",
        "GITHUB_TOKEN": "synthetic-token",
        "HTTPS_PROXY": "synthetic-proxy",
        "PYTHONPATH": "synthetic-parent-pythonpath",
        "PYTHONHOME": "synthetic-python-home",
        "PYTHONSTARTUP": "synthetic-startup",
        "COVERAGE_PROCESS_START": "synthetic-coverage-startup",
    }
    if system_root is not None:
        inherited["SystemRoot"] = system_root
    captured: dict[str, Any] = {}
    drive_lookups: list[str] = []
    directory_lookups: list[str] = []

    def drive_type(anchor: str) -> int:
        assert filesystem != "forbidden"
        drive_lookups.append(anchor)
        if filesystem == "drive-error":
            raise OSError("synthetic drive lookup unavailable")
        return {"remote-drive": 4, "unknown-drive": 0}.get(filesystem, 3)

    def directory_info(path: str) -> Any:
        assert filesystem not in {
            "forbidden",
            "remote-drive",
            "unknown-drive",
            "drive-error",
        }
        directory_lookups.append(path)
        assert len(directory_lookups) <= 3
        if filesystem == "lookup-error" and path == r"C:\Windows":
            raise OSError("synthetic directory lookup unavailable")
        reparse = (filesystem == "parent-reparse" and path == r"C:\link") or (
            filesystem == "leaf-reparse" and path == r"C:\Windows"
        )
        return SimpleNamespace(
            st_mode=stat.S_IFREG
            if filesystem == "file" and path == r"C:\Windows"
            else stat.S_IFDIR,
            st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT if reparse else 0,
        )

    def run(command: Any, **kwargs: Any) -> Any:
        captured.update(command=command, **kwargs)
        return SimpleNamespace(returncode=0, stdout='{"ok":true}', stderr="")

    with monkeypatch.context() as patch:
        patch.setattr(fixture.os, "environ", inherited)
        patch.setattr(fixture.sys, "platform", platform)
        patch.setattr(
            ctypes,
            "windll",
            SimpleNamespace(kernel32=SimpleNamespace(GetDriveTypeW=drive_type)),
            raising=False,
        )
        patch.setattr(fixture, "lstat", directory_info)
        patch.setattr(fixture.subprocess, "run", run)
        assert child(tmp_path, "poll", options={"max_polls": 1}) == {"ok": True}
    scratch = tmp_path / "child-0"
    expected = {
        "PATH": fixture.os.defpath,
        "PYTHONPATH": fixture.os.pathsep.join(
            [
                str(Path(fixture.__file__).resolve().parents[1] / "src"),
                str(Path(fixture.__file__).resolve().parent),
            ]
        ),
        "TMPDIR": str(scratch),
        "TMP": str(scratch),
        "TEMP": str(scratch),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    if expected_system_root:
        assert system_root is not None
        expected["SystemRoot"] = system_root
        assert drive_lookups == ["C:\\"]
        assert directory_lookups == ["C:\\", r"C:\Windows"]
    elif filesystem == "forbidden":
        assert not drive_lookups and not directory_lookups
    elif filesystem in {"remote-drive", "unknown-drive", "drive-error"}:
        assert len(drive_lookups) == 1 and not directory_lookups
    elif filesystem == "parent-reparse":
        assert directory_lookups == ["C:\\", r"C:\link"]
    assert captured["env"] == expected
    assert captured["cwd"] == scratch
    assert captured["timeout"] == 180
    assert captured["capture_output"] is True and captured["text"] is True
    assert captured["command"] == [
        fixture.sys.executable,
        "-B",
        str(Path(fixture.__file__).resolve()),
        str(tmp_path),
        "poll",
        '{"max_polls": 1}',
        "",
        "",
    ]


@pytest.mark.parametrize("endpoint", ["responses", "chat.completions"])
@pytest.mark.parametrize("capture", ["full", "minimal"])
def test_terminal_status_receipt_recovers_failed_sql_projection(
    tmp_path: Any, monkeypatch: Any, endpoint: str, capture: str
) -> None:
    from judgmentllm.batch_models import OpenAIBatchStatusV1
    from judgmentllm.execution import openai_batch
    from judgmentllm.lifecycle_models import LifecycleError
    from judgmentllm.persistence.native_batch import read_reference
    from judgmentllm.persistence.ownership import executor_is_active

    service = Service(tmp_path / "service.sqlite")
    install(monkeypatch, service)
    spec = specification(tmp_path, endpoint=endpoint, capture=capture)
    store = tmp_path / "store"
    prepare_batch(spec, store=store, run_id="001")
    service.configure(store, spec)
    submit_batch("001", store=store, spec=spec)
    with ReadOnlyRepository(store) as repo:
        identities = [
            tuple(row)
            for row in repo.connection.execute(
                "SELECT submission_id,remote_id FROM submissions ORDER BY submission_id"
            )
        ]
    original = openai_batch.durable_fact

    def unavailable(*args: Any, **kwargs: Any) -> Any:
        if args[3] == "status":
            raise OSError("synthetic projection unavailable")
        return original(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(openai_batch, "durable_fact", unavailable)
        with pytest.raises(LifecycleError) as error:
            poll_batch("001", store=store)
        assert error.value.category == "execution"
    reference = "batch/openai-status/" + identities[0][0] + ".json"
    with ReadOnlyRepository(store) as repo:
        terminal = read_reference(repo, "001", reference, OpenAIBatchStatusV1)
        assert terminal is not None and terminal.state == "completed"
        assert (terminal.submission_id, terminal.remote_id) == identities[0]
        assert not repo.connection.execute(
            "SELECT 1 FROM batch_facts WHERE kind='status' OR entity_id LIKE 'openai-ready:%'"
        ).fetchone()
        assert repo.summary("001").results == 0
        assert not executor_is_active(repo, "001")
    before = len(service.events())
    service.set("stall_poll", "batch_1")
    final = None
    for _ in range(4):
        final = collect_batch("001", store=store, spec=spec)
        if not final.batch.recovery_required:
            break
    assert final is not None and not final.batch.recovery_required
    assert not any(
        path.startswith("/v1/batches") for _, path in service.events()[before:]
    )
    assert len([event for event in service.events() if event[0] == "POST"]) == 2
    with ReadOnlyRepository(store) as repo:
        assert read_reference(repo, "001", reference, OpenAIBatchStatusV1) == terminal
        assert [
            tuple(row)
            for row in repo.connection.execute(
                "SELECT submission_id,remote_id FROM submissions ORDER BY submission_id"
            )
        ] == identities
        assert repo.summary("001").successful_results == 2
        assert (
            repo.connection.execute(
                "SELECT count(*) FROM batch_member_receipts"
            ).fetchone()[0]
            == 2
        )
        assert not executor_is_active(repo, "001")


@pytest.mark.parametrize("endpoint", ["responses", "chat.completions"])
@pytest.mark.parametrize("capture", ["full", "minimal"])
def test_one_row_budget_replays_prefix_and_finishes_eligible_sibling(
    tmp_path: Any, monkeypatch: Any, endpoint: str, capture: str
) -> None:
    service, spec, store = setup(
        tmp_path, monkeypatch, endpoint=endpoint, capture=capture, responses=3
    )
    options = BatchControlOptionsV1(max_result_rows=1)
    prior: dict[str, str] = {}
    before = len(service.events())
    final = None
    for _ in range(8):
        final = collect_batch("001", store=store, spec=spec, options=options)
        with ReadOnlyRepository(store) as repo:
            current = dict(
                repo.connection.execute(
                    "SELECT member_key,receipt_sha256 FROM batch_member_receipts"
                )
            )
            assert len(current) - len(prior) in (0, 1)
            assert all(current[key] == digest for key, digest in prior.items())
            prior = current
        if not final.batch.recovery_required:
            break
    assert final is not None and final.outcome == "ok"
    assert not final.batch.recovery_required and len(prior) == 3
    content = [
        path for _, path in service.events()[before:] if path.endswith("/content")
    ]
    assert content[:2] == [
        "/v1/files/file-output_1/content",
        "/v1/files/file-error_1/content",
    ]
    assert content.count("/v1/files/file-output_1/content") >= 3
    with ReadOnlyRepository(store) as repo:
        cursors = [
            json.loads(row[0])
            for row in repo.connection.execute(
                "SELECT record_json FROM batch_facts WHERE kind='cursor' AND json_extract(record_json,'$.exhausted')=1"
            )
        ]
        assert sorted(c["position"] for c in cursors) == [0, 3]
        summary = repo.summary("001")
        assert summary.attempts == summary.successful_results == 3
        receipts = [
            json.loads(row[0])
            for row in repo.connection.execute(
                "SELECT record_json FROM batch_member_receipts"
            )
        ]
        assert sum(r["usage"]["input_tokens"] for r in receipts) == 60
        assert sum(r["usage"]["output_tokens"] for r in receipts) == 30
    assert len([event for event in service.events() if event[0] == "POST"]) == 2


@pytest.mark.parametrize("capture", ["full", "minimal"])
def test_fresh_process_prelude_failure_is_surfaced_and_sticky(
    tmp_path: Any, capture: Any
) -> None:
    """Retain the selector while correcting its former sticky-budget assertion."""
    spec = specification(tmp_path, capture=capture)
    (tmp_path / "spec.json").write_text(spec.model_dump_json())
    service = Service(tmp_path / "service.sqlite")
    child(tmp_path, "prepare")
    service.configure(tmp_path / "store", spec)
    child(tmp_path, "submit")
    child(tmp_path, "poll")
    before = len(service.events())
    service.set("expire_before_selection", True)
    result = child(tmp_path, "collect")
    assert result["outcome"] == "partial"
    assert result["collection"]["selection_pending"]
    assert not result["collection"]["prelude_failed"]
    assert not service.events()[before:]
    with ReadOnlyRepository(tmp_path / "store") as repo:
        assert not repo.connection.execute(
            "SELECT 1 FROM batch_facts WHERE entity_id LIKE 'openai-admission:%' OR entity_id='openai-prelude'"
        ).fetchone()
    service.set("expire_before_selection", False)
    for _ in range(3):
        result = child(tmp_path, "resume")
        if not result["batch"]["recovery_required"]:
            break
    assert result["outcome"] == "ok" and not result["batch"]["recovery_required"]
    assert len([event for event in service.events() if event[0] == "POST"]) == 2


@pytest.mark.parametrize("capture", ["full", "minimal"])
@pytest.mark.parametrize("fresh", [False, True], ids=["persistent", "fresh"])
def test_ready_rotation_survives_repeated_wrap_and_stalled_source(
    tmp_path: Any, monkeypatch: Any, capture: str, fresh: bool
) -> None:
    from judgmentllm import openai_batch_transport, resume_batch
    from judgmentllm.batch_models import BatchLimitsV1
    from judgmentllm.providers import openai_batch

    original_context = openai_batch_transport.batch_context

    def context(spec: Any) -> Any:
        return original_context(spec).model_copy(
            update={"limits": BatchLimitsV1(max_members=3)}
        )

    monkeypatch.setattr(openai_batch_transport, "batch_context", context)
    monkeypatch.setattr(openai_batch, "batch_context", context)

    spec = specification(tmp_path, capture=capture, responses=6)
    (tmp_path / "spec.json").write_text(spec.model_dump_json())
    from judgmentllm.config import load_run_spec

    spec = load_run_spec(tmp_path / "spec.json")
    service = Service(tmp_path / "service.sqlite")
    install(monkeypatch, service)
    child(tmp_path, "prepare", small_shards=3)
    store = tmp_path / "store"
    service.configure(store, spec)
    child(tmp_path, "submit", small_shards=3)
    child(tmp_path, "poll", small_shards=3)

    def resume() -> Any:
        if fresh:
            return child(
                tmp_path, "resume", small_shards=3, options={"max_result_rows": 1}
            )
        return resume_batch(
            "001",
            store=store,
            spec=spec,
            options=BatchControlOptionsV1(max_result_rows=1),
        ).model_dump(mode="json")

    start = len(service.events())
    for _ in range(8):
        resume()
    reads = [
        path
        for method, path in service.events()[start:]
        if method == "GET" and path.endswith("/content")
    ]
    assert reads == [
        "/v1/files/file-output_1/content",
        "/v1/files/file-error_1/content",
        "/v1/files/file-output_2/content",
        "/v1/files/file-error_2/content",
        "/v1/files/file-output_1/content",
        "/v1/files/file-output_2/content",
        "/v1/files/file-output_1/content",
        "/v1/files/file-output_2/content",
    ]
    with ReadOnlyRepository(store) as repo:
        assert repo.summary("001").results == 6
    assert len([e for e in service.events() if e[0] == "POST"]) == 4


@pytest.mark.parametrize("capture", ["full", "minimal"])
def test_earlier_ready_stall_cannot_starve_later_ready_after_restart(
    tmp_path: Any, capture: str
) -> None:
    spec = specification(tmp_path, capture=capture, responses=6)
    (tmp_path / "spec.json").write_text(spec.model_dump_json())
    service = Service(tmp_path / "service.sqlite")
    child(tmp_path, "prepare", small_shards=3)
    service.configure(tmp_path / "store", spec)
    child(tmp_path, "submit", small_shards=3)
    child(tmp_path, "poll", small_shards=3)
    service.set("stall_content", "file-output_1")
    for _ in range(4):
        child(tmp_path, "resume", small_shards=3)
    with ReadOnlyRepository(tmp_path / "store") as repo:
        assert repo.summary("001").successful_results == 3
    # Progress on B must not replenish A's debited no-progress allowance.
    result = None
    for _ in range(2):
        result = child(tmp_path, "resume", small_shards=3)
    assert result is not None
    assert result["outcome"] == "error"
    assert result["collection"]["codes"] == ["collection_no_progress"]
    before = len(service.events())
    child(tmp_path, "resume", small_shards=3)
    assert not any(path.endswith("/content") for _, path in service.events()[before:])
    assert len([e for e in service.events() if e[0] == "POST"]) == 4


@pytest.mark.parametrize("capture", ["full", "minimal"])
def test_crashed_member_receipt_recovers_without_duplicate_submission(
    tmp_path: Any, capture: str
) -> None:
    spec = specification(tmp_path, capture=capture)
    (tmp_path / "spec.json").write_text(spec.model_dump_json())
    service = Service(tmp_path / "service.sqlite")
    child(tmp_path, "prepare")
    service.configure(tmp_path / "store", spec)
    child(tmp_path, "submit")
    child(tmp_path, "poll")
    child(tmp_path, "collect", fault="after_batch_member_receipt")
    with ReadOnlyRepository(tmp_path / "store") as repo:
        assert repo.summary("001").results == 0
    service.set("stall_content", "file-output_1")
    child(tmp_path, "resume")  # The independently empty error file can run first.
    child(tmp_path, "resume")
    with ReadOnlyRepository(tmp_path / "store") as repo:
        assert repo.summary("001").results == int(capture == "full")
    service.set("stall_content", None)
    for _ in range(4):
        result = child(tmp_path, "resume")
        if not result["batch"]["recovery_required"]:
            break
    with ReadOnlyRepository(tmp_path / "store") as repo:
        assert repo.summary("001").successful_results == 2
        assert (
            repo.connection.execute(
                "SELECT count(*) FROM batch_member_receipts"
            ).fetchone()[0]
            == 2
        )
    assert len([e for e in service.events() if e[0] == "POST"]) == 2


@pytest.mark.parametrize("case", ["interrupted", "historical-prefix"])
def test_row_diagnostics_survive_restart_without_backfilling_prefix(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    from judgmentllm.config import load_run_spec
    from judgmentllm.execution.openai_batch import OpenAIBatchSession

    spec_path = tmp_path / "spec.json"
    spec_path.write_text(specification(tmp_path, capture="minimal").model_dump_json())
    spec = load_run_spec(spec_path)
    service = Service(tmp_path / "service.sqlite")
    store = tmp_path / "store"
    child(tmp_path, "prepare")
    service.configure(store, spec)
    child(tmp_path, "submit")
    child(tmp_path, "poll")
    first, second = service.file("file-output_1")[0].splitlines()
    row = json.loads(first)
    row["response"] = {"status_code": 429, "request_id": "req_first", "body": {}}
    first = json.dumps(row).encode()
    data = first + b"\n" + second + b"\n"
    service.file("file-output_1", body=data, declared=len(data))
    saved = None
    if case == "interrupted":
        child(tmp_path, "collect", fault="after_openai_batch_row_diagnostic")
        (saved,) = diagnostic_records(store, "result_row")
        assert saved["member_request_id"] == "req_first"
        with ReadOnlyRepository(store) as repo:
            assert repo.summary("001").results == 0
    else:

        def omit_diagnostic(*_: Any) -> None:
            pass

        install(monkeypatch, service)
        with monkeypatch.context() as patch:
            patch.setattr(OpenAIBatchSession, "publish_row_diagnostic", omit_diagnostic)
            collect_batch(
                "001",
                store=store,
                spec=spec,
                options=BatchControlOptionsV1(max_result_rows=1),
            )
        assert diagnostic_records(store, "result_row") == []
        with ReadOnlyRepository(store) as repo:
            assert repo.summary("001").results == 1
    changed = data.replace(b"req_first", b"req_other")
    service.file("file-output_1", body=changed, declared=len(changed))
    for _ in range(4):
        if not child(tmp_path, "resume")["batch"]["recovery_required"]:
            break
    recorded = diagnostic_records(store, "result_row")
    assert [item["row_position"] for item in recorded] == (
        [0, 1] if case == "interrupted" else [1]
    )
    if saved is not None:
        assert recorded[0] == saved
    assert all(item["member_request_id"] != "req_other" for item in recorded)
    with ReadOnlyRepository(store) as repo:
        assert repo.summary("001").results == 2
        assert (
            repo.connection.execute(
                "SELECT count(*) FROM batch_member_receipts"
            ).fetchone()[0]
            == 2
        )
        assert tuple(
            repo.connection.execute(
                "SELECT certainty,remote_id FROM submissions"
            ).fetchone()
        ) == (
            "accepted",
            "batch_1",
        )
    assert [event for event in service.events() if event[0] == "POST"] == [
        ("POST", "/v1/files"),
        ("POST", "/v1/batches"),
    ]


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", "file-other"),
        ("purpose", "assistants"),
        ("bytes", True),
        ("bytes", "0"),
        ("bytes", -1),
        ("bytes", None),
    ],
)
def test_raw_files_metadata_is_strict_before_content_or_durability(
    tmp_path: Any, monkeypatch: Any, field: str, value: Any
) -> None:
    import httpx2

    service, spec, store = setup(tmp_path, monkeypatch)
    request = service.request

    def changed(req: Any) -> Any:
        response = request(req)
        if req.url.path == "/v1/files/file-output_1":
            metadata = json.loads(response.read())
            metadata[field] = value
            return httpx2.Response(200, json=metadata)
        return response

    monkeypatch.setattr(service, "request", changed)
    result = collect_batch("001", store=store, spec=spec)
    assert (
        result.collection is not None and "unsafe_metadata" in result.collection.codes
    )
    assert not any(path.endswith("/content") for _, path in service.events())
    with ReadOnlyRepository(store) as repo:
        assert repo.summary("001").results == 0
        assert not repo.connection.execute(
            "SELECT 1 FROM batch_facts WHERE entity_id LIKE 'openai-binding:%'"
        ).fetchone()


def test_poll_never_downloads_files_even_for_uncertain_upload(
    tmp_path: Any, monkeypatch: Any
) -> None:
    service = Service(tmp_path / "service.sqlite")
    install(monkeypatch, service)
    spec = specification(tmp_path)
    store = tmp_path / "store"
    prepare_batch(spec, store=store, run_id="001")
    service.configure(store, spec)
    service.set("lose_upload_ack", True)
    submit_batch("001", store=store)
    before = len(service.events())
    poll_batch("001", store=store)
    assert not service.events()[before:]


@pytest.mark.parametrize("capture", ["full", "minimal"])
@pytest.mark.parametrize("uncertain", ["metadata", "incomplete", "multiple"])
def test_inconclusive_create_listing_never_resubmits(
    tmp_path: Any, monkeypatch: Any, capture: str, uncertain: str
) -> None:
    import httpx2

    from judgmentllm import resume_batch

    service = Service(tmp_path / "service.sqlite")
    install(monkeypatch, service)
    spec = specification(tmp_path, capture=capture)
    store = tmp_path / "store"
    prepare_batch(spec, store=store, run_id="001")
    service.configure(store, spec)
    service.set("lose_create_ack", True)
    assert submit_batch("001", store=store, spec=spec).outcome == "partial"
    original = service.request

    def inconclusive(request: Any) -> Any:
        response = original(request)
        if request.url.path == "/v1/batches" and request.method == "GET":
            body = json.loads(response.read())
            if uncertain == "metadata":
                body["data"][0]["metadata"] = None
            elif uncertain == "incomplete":
                body["has_more"] = True
            else:
                body["data"] *= 2
            return httpx2.Response(200, json=body)
        return response

    monkeypatch.setattr(service, "request", inconclusive)
    for _ in range(2):
        result = resume_batch("001", store=store, spec=spec)
        assert result.outcome == "partial" and result.batch.unresolved_operations == 1
    assert len([event for event in service.events() if event[0] == "POST"]) == 2
    is_lookup = [
        row
        for row in diagnostic_records(store, "create_list")
        if row["outcome"] == "indeterminate"
    ]
    assert [row["reason"] for row in is_lookup] == [
        "no_match" if uncertain == "metadata" else "inconclusive_lookup",
    ] * 2
    assert len({row["binding_id"] for row in is_lookup}) == 1


@pytest.mark.parametrize("endpoint", ["responses", "chat.completions"])
@pytest.mark.parametrize("capture", ["full", "minimal"])
@pytest.mark.parametrize("pressure", ["selection", "steps", "metadata_expiry"])
def test_unfit_or_expired_invocation_can_resume_without_collection_debit(
    tmp_path: Any, endpoint: str, capture: str, pressure: str
) -> None:
    spec = specification(tmp_path, endpoint=endpoint, capture=capture)
    (tmp_path / "spec.json").write_text(spec.model_dump_json())
    service = Service(tmp_path / "service.sqlite")
    child(tmp_path, "prepare")
    store = tmp_path / "store"
    service.configure(store, spec)
    child(tmp_path, "submit")
    child(tmp_path, "poll")
    service.set("expire_after_metadata", pressure == "metadata_expiry")
    options = (
        {}
        if pressure == "metadata_expiry"
        else {
            "max_io_steps": 1 if pressure == "selection" else 16,
        }
    )
    before = len(service.events())
    first = child(tmp_path, "collect", options=options)
    assert first["outcome"] == "partial"
    assert first["collection"]["selection_pending"]
    assert not first["collection"]["failed_sources"]
    assert not any(path.endswith("/content") for _, path in service.events()[before:])
    with ReadOnlyRepository(store) as repo:
        assert not repo.connection.execute(
            "SELECT 1 FROM batch_facts WHERE entity_id LIKE 'openai-admission:%' OR entity_id='openai-prelude'"
        ).fetchone()
        submissions = [
            tuple(r)
            for r in repo.connection.execute(
                "SELECT submission_id,remote_id FROM submissions ORDER BY submission_id"
            )
        ]
    service.set("expire_after_metadata", False)
    final = first
    for _ in range(3):
        final = child(tmp_path, "resume")
        if not final["batch"]["recovery_required"]:
            break
    assert final["outcome"] == "ok" and final["batch"]["missing_members"] == 0
    with ReadOnlyRepository(store) as repo:
        assert repo.summary("001").successful_results == 2
        assert [
            tuple(r)
            for r in repo.connection.execute(
                "SELECT submission_id,remote_id FROM submissions ORDER BY submission_id"
            )
        ] == submissions
    assert len([event for event in service.events() if event[0] == "POST"]) == 2


@pytest.mark.parametrize("endpoint", ["responses", "chat.completions"])
@pytest.mark.parametrize("capture", ["full", "minimal"])
def test_exact_replay_and_control_byte_reserve_precedes_durable_admission(
    tmp_path: Any, endpoint: str, capture: str
) -> None:
    spec = specification(tmp_path, endpoint=endpoint, capture=capture)
    (tmp_path / "spec.json").write_text(spec.model_dump_json())
    service = Service(tmp_path / "service.sqlite")
    child(tmp_path, "prepare")
    store = tmp_path / "store"
    service.configure(store, spec)
    child(tmp_path, "submit")
    child(tmp_path, "poll")
    size = service.file("file-output_1")[1]
    metadata_bytes = len(
        json.dumps(
            {
                "id": "file-output_1",
                "object": "file",
                "purpose": "batch_output",
                "filename": "output.jsonl",
                "bytes": size,
                "created_at": 1,
            },
            separators=(",", ":"),
        ).encode()
    )
    exact = metadata_bytes + 2 * size + 1_048_576
    service.set("byte_limit", exact - 1)
    before = len(service.events())
    first = child(tmp_path, "collect")
    assert first["outcome"] == "partial"
    assert "collection_budget_unfit" in first["collection"]["codes"]
    assert not any(path.endswith("/content") for _, path in service.events()[before:])
    with ReadOnlyRepository(store) as repo:
        assert not repo.connection.execute(
            "SELECT 1 FROM batch_facts WHERE entity_id LIKE 'openai-admission:%'"
        ).fetchone()
    # The exact boundary admits the same file; the independent error slot rotates first.
    service.set("byte_limit", exact)
    child(tmp_path, "collect")
    final = child(tmp_path, "collect")
    assert final["outcome"] == "ok" and not final["batch"]["recovery_required"]
    service.set("byte_limit", None)
    assert child(tmp_path, "resume")["outcome"] == "ok"
    assert len([event for event in service.events() if event[0] == "POST"]) == 2


@pytest.mark.parametrize("endpoint", ["responses", "chat.completions"])
@pytest.mark.parametrize("capture", ["full", "minimal"])
def test_fresh_process_death_after_admission_keeps_three_actual_debits(
    tmp_path: Any, endpoint: str, capture: str
) -> None:
    import time

    deadline = time.monotonic() + 180

    def run(action: str, **kwargs: Any) -> Any:
        return child(
            tmp_path,
            action,
            timeout=max(0.1, deadline - time.monotonic()),
            **kwargs,
        )

    spec = specification(tmp_path, endpoint=endpoint, capture=capture)
    (tmp_path / "spec.json").write_text(spec.model_dump_json())
    service = Service(tmp_path / "service.sqlite")
    run("prepare")
    store = tmp_path / "store"
    service.configure(store, spec)
    run("submit")
    run("poll")
    with ReadOnlyRepository(store) as repo:
        original = (repo.run_directory("001") / "manifest.json").read_bytes()
        identities = [
            tuple(r)
            for r in repo.connection.execute(
                "SELECT attempt_id,request_uid FROM attempts ORDER BY attempt_id"
            )
        ]
    # Rotation gives both present slots exactly three independent process deaths.
    for _ in range(6):
        run("collect", fault="after_openai_batch_admission")
    with ReadOnlyRepository(store) as repo:
        admitted = [
            json.loads(r[0])
            for r in repo.connection.execute(
                "SELECT record_json FROM batch_facts WHERE entity_id LIKE 'openai-admission:%' AND json_extract(record_json,'$.code')='admitted'"
            )
        ]
        assert len(admitted) == 6
        for source in {row["source_id"] for row in admitted}:
            assert sorted(
                row["stalls"] for row in admitted if row["source_id"] == source
            ) == [1, 2, 3]
        assert repo.summary("001").results == 0
    final = run("resume")
    assert final["outcome"] == "error" and final["collection"]["failed_sources"] == 2
    assert final["collection"]["codes"] == ["collection_no_progress"]
    run("resume")
    assert not any(path.endswith("/content") for _, path in service.events())
    assert len([event for event in service.events() if event[0] == "POST"]) == 2
    with ReadOnlyRepository(store) as repo:
        assert (repo.run_directory("001") / "manifest.json").read_bytes() == original
        assert [
            tuple(r)
            for r in repo.connection.execute(
                "SELECT attempt_id,request_uid FROM attempts ORDER BY attempt_id"
            )
        ] == identities


@pytest.mark.parametrize("endpoint", ["responses", "chat.completions"])
@pytest.mark.parametrize("capture", ["full", "minimal"])
def test_fresh_process_lost_cancel_ack_is_reconciled_without_repeat(
    tmp_path: Any, endpoint: str, capture: str
) -> None:
    import time

    deadline = time.monotonic() + 180

    def run(action: str, **kwargs: Any) -> Any:
        return child(
            tmp_path,
            action,
            timeout=max(0.1, deadline - time.monotonic()),
            **kwargs,
        )

    spec = specification(tmp_path, endpoint=endpoint, capture=capture)
    (tmp_path / "spec.json").write_text(spec.model_dump_json())
    service = Service(tmp_path / "service.sqlite")
    run("prepare")
    store = tmp_path / "store"
    service.configure(store, spec)
    service.set("initial_status", "in_progress")
    run("submit")
    service.set("lose_cancel_ack", True)
    first = run("cancel")
    assert (
        first["outcome"] == "partial" and first["batch"]["unresolved_operations"] == 1
    )
    with service.connect() as db:
        remote = json.loads(
            db.execute("SELECT body FROM jobs WHERE id='batch_1'").fetchone()[0]
        )
        assert remote["status"] == "cancelled"
    service.set("lose_cancel_ack", False)
    run("resume")
    run("resume")
    repeated = run("cancel")
    assert repeated["batch"]["unresolved_operations"] == 0
    assert service.events().count(("POST", "/v1/batches/batch_1/cancel")) == 1
    assert len([event for event in service.events() if event[0] == "POST"]) == 3
    with ReadOnlyRepository(store) as repo:
        assert [
            tuple(r)
            for r in repo.connection.execute("SELECT remote_id FROM submissions")
        ] == [("batch_1",)]
        assert repo.summary("001").attempts == 2


@pytest.mark.parametrize("endpoint", ["responses", "chat.completions"])
@pytest.mark.parametrize("capture", ["full", "minimal"])
def test_zero_byte_503_recovers_same_job_in_fresh_process(
    tmp_path: Any, endpoint: str, capture: str
) -> None:
    import time

    deadline = time.monotonic() + 180

    def run(action: str) -> Any:
        return child(tmp_path, action, timeout=max(0.1, deadline - time.monotonic()))

    spec = specification(tmp_path, endpoint=endpoint, capture=capture)
    (tmp_path / "spec.json").write_text(spec.model_dump_json())
    service = Service(tmp_path / "service.sqlite")
    run("prepare")
    store = tmp_path / "store"
    service.configure(store, spec)
    run("submit")
    run("poll")
    completed = run("collect")  # Commit output before the independent empty source.
    assert completed["batch"]["missing_members"] == 0
    assert service.file("file-error_1") == (b"", 0)
    posts = [event for event in service.events() if event[0] == "POST"]
    assert posts == [("POST", "/v1/files"), ("POST", "/v1/batches")]

    def progress() -> Any:
        with ReadOnlyRepository(store) as repo:
            return {
                "manifest": (repo.run_directory("001") / "manifest.json").read_bytes(),
                "results": tuple(r.model_dump_json() for r in repo.iter_results("001")),
                "members": tuple(
                    tuple(row)
                    for row in repo.connection.execute(
                        "SELECT member_key,receipt_sha256,record_json FROM batch_member_receipts ORDER BY member_key"
                    )
                ),
                "operations": tuple(
                    tuple(row)
                    for row in repo.connection.execute(
                        "SELECT * FROM batch_operations ORDER BY instance_id"
                    )
                ),
                "accepted": tuple(
                    tuple(row)
                    for row in repo.connection.execute(
                        "SELECT entity_id,receipt_sha256,record_json FROM batch_facts WHERE kind='operation' ORDER BY fact_seq"
                    )
                ),
                "jobs": tuple(
                    tuple(row)
                    for row in repo.connection.execute(
                        "SELECT submission_id,remote_id FROM submissions ORDER BY submission_id"
                    )
                ),
                "cursors": tuple(
                    tuple(row)
                    for row in repo.connection.execute(
                        "SELECT entity_id,record_json FROM batch_facts WHERE kind='cursor' AND entity_id NOT LIKE 'openai-%' ORDER BY fact_seq"
                    )
                ),
            }

    original = progress()
    assert len(original["members"]) == 2 and original["cursors"]
    service.set("stall_content", "file-error_1")  # Nonempty HTTP 503 body is {}.
    before = len(service.events())
    failed = run("collect")
    assert failed["outcome"] == "partial"
    assert failed["collection"]["failed_sources"] == 0
    assert "source_overrun" not in failed["collection"]["codes"]
    assert progress() == original
    assert [
        event for event in service.events()[before:] if event[1].endswith("/content")
    ] == [("GET", "/v1/files/file-error_1/content")]
    with ReadOnlyRepository(store) as repo:
        binding = tuple(
            repo.connection.execute(
                "SELECT entity_id,record_json FROM batch_facts WHERE entity_id LIKE 'openai-binding:%' AND json_extract(record_json,'$.file_id')='file-error_1'"
            ).fetchone()
        )
        pinned = json.loads(binding[1])
        assert pinned["byte_size"] == 0 and pinned["job"]["remote_id"] == "batch_1"
        source = binding[0].removeprefix("openai-binding:")
        assert not repo.connection.execute(
            "SELECT 1 FROM batch_facts WHERE kind='cursor' AND entity_id=?", (source,)
        ).fetchone()

    service.set("stall_content", None)
    before = len(service.events())
    final = run("resume")  # Explicit new process, same accepted job/file binding.
    assert final["outcome"] == "ok" and not final["batch"]["recovery_required"]
    assert final["collection"]["failed_sources"] == 0
    assert [
        event for event in service.events()[before:] if event[1].endswith("/content")
    ] == [("GET", "/v1/files/file-error_1/content")]
    assert [event for event in service.events() if event[0] == "POST"] == posts
    after = progress()
    assert {k: v for k, v in after.items() if k != "cursors"} == {
        k: v for k, v in original.items() if k != "cursors"
    }
    assert after["cursors"][: len(original["cursors"])] == original["cursors"]
    with ReadOnlyRepository(store) as repo:
        cursor = json.loads(
            repo.connection.execute(
                "SELECT record_json FROM batch_facts WHERE kind='cursor' AND entity_id=? ORDER BY fact_seq DESC LIMIT 1",
                (source,),
            ).fetchone()[0]
        )
        assert cursor["position"] == 0 and cursor["exhausted"] is True
        assert (
            tuple(
                repo.connection.execute(
                    "SELECT entity_id,record_json FROM batch_facts WHERE entity_id=?",
                    (binding[0],),
                ).fetchone()
            )
            == binding
        )
        assert repo.summary("001").successful_results == 2
    assert time.monotonic() < deadline


@pytest.mark.parametrize("case", ["blank-row", "oversized-row", "unterminated-record"])
def test_collection_framing_precedes_member_durability(
    tmp_path: Any, monkeypatch: Any, case: str
) -> None:
    from judgmentllm.batch_models import (
        BatchCursorV1,
        OpenAIBatchAdmissionV1,
        OpenAIBatchSourceBindingV1,
    )
    from judgmentllm.persistence.native_batch import latest
    from judgmentllm.persistence.ownership import executor_is_active
    from judgmentllm.preparation.manifest import (
        ExecutableRunManifestV13,
        parse_executable_manifest,
    )

    service, spec, store = setup(tmp_path, monkeypatch, responses=1)
    with ReadOnlyRepository(store) as repo:
        manifest_bytes = (repo.run_directory("001") / "manifest.json").read_bytes()
        manifest = parse_executable_manifest(manifest_bytes)
        assert isinstance(manifest, ExecutableRunManifestV13)
        limit = manifest.batch_context.limits.max_record_bytes
        identities = [
            tuple(row)
            for row in repo.connection.execute(
                "SELECT submission_id,remote_id FROM submissions ORDER BY submission_id"
            )
        ]
    body = {
        "blank-row": b"\n",
        "oversized-row": b"x" * (limit + 1) + b"\n",
        "unterminated-record": b"x" * (limit + 1),
    }[case]
    assert len(body) <= manifest.batch_context.max_source_bytes
    service.file("file-output_1", body=body, declared=len(body))
    posts = [event for event in service.events() if event[0] == "POST"]
    final = None
    for _ in range(4):
        final = collect_batch("001", store=store, spec=spec)
        if final.collection is not None and "source_framing" in final.collection.codes:
            break
    assert final is not None and final.collection is not None
    assert "source_framing" in final.collection.codes
    before = len(service.events())
    collect_batch("001", store=store, spec=spec)
    assert not any(
        path == "/v1/files/file-output_1/content"
        for _, path in service.events()[before:]
    )
    with ReadOnlyRepository(store) as repo:
        row = repo.connection.execute(
            "SELECT record_json FROM batch_facts "
            "WHERE entity_id LIKE 'openai-binding:%' "
            "AND json_extract(record_json,'$.file_id')='file-output_1'"
        ).fetchone()
        assert row is not None
        binding = OpenAIBatchSourceBindingV1.model_validate_json(row[0])
        assert binding.byte_size == len(body)
        source = binding.ready.source_id
        admission = latest(
            repo, "001", "cursor", "openai-admission:" + source, OpenAIBatchAdmissionV1
        )
        assert admission is not None and admission.code == "source_framing"
        assert admission.frontier == 0
        assert latest(repo, "001", "cursor", source, BatchCursorV1) is None
        assert not tuple(repo.iter_results("001"))
        assert (
            repo.connection.execute(
                "SELECT count(*) FROM batch_member_receipts"
            ).fetchone()[0]
            == 0
        )
        assert [
            tuple(row)
            for row in repo.connection.execute(
                "SELECT submission_id,remote_id FROM submissions ORDER BY submission_id"
            )
        ] == identities
        assert (repo.run_directory("001") / "manifest.json").read_bytes() == (
            manifest_bytes
        )
        assert not executor_is_active(repo, "001")
    assert [event for event in service.events() if event[0] == "POST"] == posts


@pytest.mark.parametrize("case", ["changed-prefix", "shortened-prefix"])
def test_replay_prefix_conflict_preserves_committed_results(
    tmp_path: Any, monkeypatch: Any, case: str
) -> None:
    from judgmentllm.batch_models import (
        BatchCursorV1,
        OpenAIBatchAdmissionV1,
        OpenAIBatchSourceBindingV1,
    )
    from judgmentllm.persistence.native_batch import latest
    from judgmentllm.persistence.ownership import executor_is_active

    service, spec, store = setup(tmp_path, monkeypatch, responses=3)
    committed = 1 if case == "changed-prefix" else 2
    collect_batch(
        "001",
        store=store,
        spec=spec,
        options=BatchControlOptionsV1(max_result_rows=committed),
    )
    with ReadOnlyRepository(store) as repo:
        results = tuple(repo.iter_results("001"))
        assert len(results) == committed
        assert all(r.status == "complete" for r in results)
        receipts = [
            tuple(row)
            for row in repo.connection.execute(
                "SELECT member_key,receipt_sha256,record_json "
                "FROM batch_member_receipts ORDER BY member_key"
            )
        ]
        assert len(receipts) == committed
        row = repo.connection.execute(
            "SELECT record_json FROM batch_facts "
            "WHERE entity_id LIKE 'openai-binding:%' "
            "AND json_extract(record_json,'$.file_id')='file-output_1'"
        ).fetchone()
        assert row is not None
        binding = OpenAIBatchSourceBindingV1.model_validate_json(row[0])
        source = binding.ready.source_id
        cursor = latest(repo, "001", "cursor", source, BatchCursorV1)
        assert cursor is not None and not cursor.exhausted
        assert cursor.position == committed
        identities = [
            tuple(row)
            for row in repo.connection.execute(
                "SELECT submission_id,remote_id FROM submissions ORDER BY submission_id"
            )
        ]
        manifest = (repo.run_directory("001") / "manifest.json").read_bytes()
    original, declared = service.file("file-output_1")
    assert declared == len(original) == binding.byte_size
    assert original.count(b"\n") == 3
    if case == "changed-prefix":
        lines = original.splitlines(keepends=True)
        row = json.loads(lines[0])
        text = row["response"]["body"]["output"][0]["content"][0]["text"]
        changed_text = text.replace(
            '"category_label": "low"', '"category_label":"high"', 1
        )
        assert changed_text != text
        assert json.loads(changed_text)["scores"][0]["category_label"] == "high"
        row["response"]["body"]["output"][0]["content"][0]["text"] = changed_text
        changed_line = (
            json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
        )
        changed = changed_line + b"".join(lines[1:])
    else:
        changed = original.replace(b"\n", b" ", 2)
        assert changed.count(b"\n") == 1
    assert changed != original and len(changed) == declared
    service.file("file-output_1", body=changed)
    assert service.file("file-output_1")[1] == declared
    posts = [event for event in service.events() if event[0] == "POST"]
    with service.connect() as db:
        read_marker = db.execute("SELECT coalesce(max(rowid),0) FROM reads").fetchone()[
            0
        ]
    final = None
    for _ in range(4):
        final = collect_batch("001", store=store, spec=spec)
        if final.collection is not None and "source_conflict" in final.collection.codes:
            break
    assert final is not None and final.collection is not None
    assert "source_conflict" in final.collection.codes
    with ReadOnlyRepository(store) as repo:
        assert tuple(repo.iter_results("001")) == results
        assert [
            tuple(row)
            for row in repo.connection.execute(
                "SELECT member_key,receipt_sha256,record_json "
                "FROM batch_member_receipts ORDER BY member_key"
            )
        ] == receipts
        assert latest(repo, "001", "cursor", source, BatchCursorV1) == cursor
        admission = latest(
            repo, "001", "cursor", "openai-admission:" + source, OpenAIBatchAdmissionV1
        )
        assert admission is not None and admission.code == "source_conflict"
        assert admission.frontier == committed
        assert (
            latest(
                repo,
                "001",
                "cursor",
                "openai-binding:" + source,
                OpenAIBatchSourceBindingV1,
            )
            == binding
        )
        assert [
            tuple(row)
            for row in repo.connection.execute(
                "SELECT submission_id,remote_id FROM submissions ORDER BY submission_id"
            )
        ] == identities
        assert (repo.run_directory("001") / "manifest.json").read_bytes() == manifest
        assert not executor_is_active(repo, "001")
    if case == "shortened-prefix":
        with service.connect() as db:
            assert (
                db.execute(
                    "SELECT coalesce(sum(byte_count),0) FROM reads "
                    "WHERE rowid>? AND file=?",
                    (read_marker, "file-output_1"),
                ).fetchone()[0]
                == declared
            )
    assert [event for event in service.events() if event[0] == "POST"] == posts


def accepted_chain_snapshot(
    store: Any, *, include_executor_lock: bool = True
) -> dict[str, Any]:
    with ReadOnlyRepository(store) as repo:
        projections = {
            table: tuple(
                tuple(row)
                for row in repo.connection.execute(
                    f"SELECT * FROM {table} WHERE run_id='001' ORDER BY rowid"
                )
            )
            for table in (
                "tasks",
                "attempts",
                "batches",
                "submissions",
                "observations",
                "results",
                "batch_preparations",
                "batch_prepared_members",
                "batch_operations",
                "batch_member_receipts",
            )
        }
        for kind in ("operation", "status"):
            projections[kind + "_facts"] = tuple(
                tuple(row)
                for row in repo.connection.execute(
                    "SELECT * FROM batch_facts WHERE run_id='001' AND kind=? "
                    "ORDER BY fact_seq",
                    (kind,),
                )
            )
        projections["ready_facts"] = tuple(
            tuple(row)
            for row in repo.connection.execute(
                "SELECT * FROM batch_facts WHERE run_id='001' AND kind='cursor' "
                "AND entity_id LIKE 'openai-ready:%' ORDER BY fact_seq"
            )
        )
        guards = tuple(
            tuple(row)
            for row in repo.connection.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='trigger' ORDER BY name"
            )
        )
        registered = {
            row[0]: tuple(row)
            for row in repo.connection.execute(
                "SELECT sha256,byte_length,media_type,relative_path FROM artifacts "
                "WHERE run_id='001' ORDER BY sha256"
            )
        }
        directory = repo.run_directory("001")
        executor_lock = directory / "executor.lock"
        files = {
            path.relative_to(directory).as_posix(): path.read_bytes()
            for path in directory.rglob("*")
            if (include_executor_lock or path != executor_lock) and path.is_file()
        }
    return {
        "projections": projections,
        "guards": guards,
        "registered": registered,
        "objects": {n: raw for n, raw in files.items() if n.startswith("objects/")},
        "references": {
            n: raw for n, raw in files.items() if not n.startswith("objects/")
        },
    }


def assert_accepted_chain_unchanged(store: Any, before: dict[str, Any]) -> None:
    from judgmentllm.persistence.ownership import executor_is_active

    after = accepted_chain_snapshot(store)
    assert after["projections"] == before["projections"]
    assert after["guards"] == before["guards"]
    assert after["references"] == before["references"]
    # Selection bookkeeping may register new objects; earlier evidence cannot change.
    for kind in ("objects", "registered"):
        assert {n: after[kind].get(n) for n in before[kind]} == before[kind]
    with ReadOnlyRepository(store) as repo:
        assert not executor_is_active(repo, "001")


@pytest.mark.parametrize(
    "case",
    [
        "create-intent-missing",
        "create-intent-digest",
        "create-receipt-missing",
        "upload-intent-missing",
        "upload-intent-digest",
        "upload-receipt-missing",
    ],
)
def test_accepted_chain_requires_original_persisted_evidence(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    from test_batch_preparation_rejections import corrupt_row

    from judgmentllm.batch_models import BatchOperationIntentV1
    from judgmentllm.lifecycle_models import LifecycleError
    from judgmentllm.persistence.hashing import bytes_digest, canonical_bytes
    from judgmentllm.persistence.models import IntegrityError

    service, spec, store = setup(tmp_path, monkeypatch, responses=1)
    original = accepted_chain_snapshot(store)
    with ReadOnlyRepository(store) as repo:
        operations = {
            row[0]: tuple(row[1:])
            for row in repo.connection.execute(
                "SELECT kind,instance_id,record_json,intent_sha256 "
                "FROM batch_operations WHERE run_id='001'"
            )
        }
        directory = repo.run_directory("001")
        assert len(original["projections"]["ready_facts"]) == 2
        assert not original["projections"]["results"]
        assert not original["projections"]["batch_member_receipts"]
    kind = case.split("-", 1)[0]
    instance, raw, digest = operations[kind]
    if case.endswith("intent-missing"):
        corrupt_row(
            store,
            "batch_operations",
            "UPDATE batch_operations SET kind='cancel' WHERE instance_id=?",
            (instance,),
        )
        with ReadOnlyRepository(store) as repo:
            assert (
                repo.connection.execute(
                    "SELECT 1 FROM batch_operations WHERE run_id='001' AND kind=?",
                    (kind,),
                ).fetchone()
                is None
            )
    elif case.endswith("intent-digest"):
        values = json.loads(raw)
        values["owner_token"] = "synthetic-corrupt-owner"
        changed = BatchOperationIntentV1.model_validate(values)
        encoded = canonical_bytes(changed)
        assert bytes_digest(encoded) != digest
        corrupt_row(
            store,
            "batch_operations",
            "UPDATE batch_operations SET record_json=? WHERE instance_id=?",
            (encoded.decode(), instance),
        )
        with ReadOnlyRepository(store) as repo:
            assert (
                repo.connection.execute(
                    "SELECT intent_sha256 FROM batch_operations WHERE instance_id=?",
                    (instance,),
                ).fetchone()[0]
                == digest
            )
    else:
        assert case == kind + "-receipt-missing"
        reference = directory / "batch" / "operations" / (instance + ".json")
        retained = reference.read_bytes()
        corrupt_row(
            store,
            "batch_facts",
            "UPDATE batch_facts SET entity_id=? WHERE run_id='001' "
            "AND kind='operation' AND entity_id=?",
            (instance + ":unavailable", instance),
        )
        backup = tmp_path / (kind + "-unavailable-receipt.json")
        reference.rename(backup)
        assert backup.read_bytes() == retained and not reference.exists()
        with ReadOnlyRepository(store) as repo:
            assert (
                repo.connection.execute(
                    "SELECT 1 FROM batch_facts WHERE run_id='001' "
                    "AND kind='operation' AND entity_id=?",
                    (instance,),
                ).fetchone()
                is None
            )
    before = accepted_chain_snapshot(store)
    assert before["guards"] == original["guards"]
    assert before["objects"] == original["objects"]
    assert before["registered"] == original["registered"]
    messages = {
        "create-intent-missing": "accepted job lacks its original create intent",
        "create-intent-digest": "accepted job create intent differs",
        "create-receipt-missing": "accepted job lacks its original receipt",
        "upload-intent-missing": "accepted create lacks its upload intent",
        "upload-intent-digest": "accepted upload intent differs",
        "upload-receipt-missing": "accepted create differs from its upload receipt",
    }
    events = service.events()
    with pytest.raises(LifecycleError) as error:
        collect_batch("001", store=store, spec=spec)
    assert error.value.category == "execution"
    assert error.value.message == "durable run integrity or storage operation failed"
    assert error.value.__suppress_context__ is True
    assert isinstance(error.value.__context__, IntegrityError)
    assert error.value.__context__.args == (messages[case],)
    assert service.events() == events
    assert_accepted_chain_unchanged(store, before)


def _setup_terminal_sibling(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    from judgmentllm import openai_batch_transport
    from judgmentllm.batch_models import BatchLimitsV1
    from judgmentllm.providers import openai_batch as provider

    original_context = openai_batch_transport.batch_context

    def single_member(spec: Any) -> Any:
        return original_context(spec).model_copy(
            update={"limits": BatchLimitsV1(max_members=1)}
        )

    monkeypatch.setattr(openai_batch_transport, "batch_context", single_member)
    monkeypatch.setattr(provider, "batch_context", single_member)
    service = Service(tmp_path / "service.sqlite")
    install(monkeypatch, service)
    spec = specification(tmp_path, responses=2)
    store = tmp_path / "store"
    prepare_batch(spec, store=store, run_id="001")
    service.configure(store, spec)
    service.set("initial_status", "in_progress")
    submit_batch("001", store=store, spec=spec)
    with ReadOnlyRepository(store) as repo:
        accepted = [
            tuple(row)
            for row in repo.connection.execute(
                "SELECT submission_id,remote_id,rowid FROM submissions "
                "WHERE run_id='001' AND certainty='accepted' ORDER BY rowid"
            )
        ]
        directory = repo.run_directory("001")
    assert len(accepted) == 2
    with service.connect() as db:
        job = json.loads(
            db.execute(
                "SELECT body FROM jobs WHERE id=?", (accepted[1][1],)
            ).fetchone()[0]
        )
        job["status"] = "completed"
        db.execute(
            "UPDATE jobs SET body=? WHERE id=?", (json.dumps(job), accepted[1][1])
        )
    return service, spec, store, accepted, directory


def test_ready_projection_cut_resumes_without_remote_prelude(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from judgmentllm.evidence._base import EvidenceModel
    from judgmentllm.execution import openai_batch as execution
    from judgmentllm.lifecycle_models import LifecycleError
    from judgmentllm.persistence.models import IntegrityError
    from judgmentllm.persistence.ownership import executor_is_active
    from judgmentllm.persistence.repository import Repository

    service, spec, store, accepted, directory = _setup_terminal_sibling(
        tmp_path, monkeypatch
    )
    original_fact = execution.durable_fact
    faults: list[str] = []

    def second_projection(
        repository: Repository,
        run_id: str,
        token: str,
        kind: str,
        entity: str,
        model: EvidenceModel,
        *,
        reference: str | None = None,
    ) -> str:
        if kind == "status" and entity == accepted[1][0]:
            faults.append(entity)
            raise OSError("synthetic second status projection unavailable")
        return original_fact(
            repository, run_id, token, kind, entity, model, reference=reference
        )

    guards = accepted_chain_snapshot(store)["guards"]
    with monkeypatch.context() as patch:
        patch.setattr(execution, "durable_fact", second_projection)
        with pytest.raises(LifecycleError) as error:
            poll_batch("001", store=store)
        assert error.value.category == "execution"
    assert faults == [accepted[1][0]]
    terminal_reference = (
        directory / "batch" / "openai-status" / (accepted[1][0] + ".json")
    )
    terminal = terminal_reference.read_bytes()
    assert json.loads(terminal)["state"] == "completed"
    assert not (
        directory / "batch" / "openai-status" / (accepted[0][0] + ".json")
    ).exists()
    baseline = accepted_chain_snapshot(store)
    assert baseline["guards"] == guards
    assert not baseline["projections"]["ready_facts"]
    assert not baseline["projections"]["results"]
    with ReadOnlyRepository(store) as repo:
        statuses = [
            json.loads(row[0])
            for row in repo.connection.execute(
                "SELECT record_json FROM batch_facts "
                "WHERE run_id='001' AND kind='status'"
            )
        ]
        assert len(statuses) == 1
        assert statuses[0]["submission_id"] == accepted[0][0]
        assert statuses[0]["state"] == "running"
        assert not executor_is_active(repo, "001")
    events = service.events()
    posts = [event for event in events if event[0] == "POST"]
    assert len(posts) == 4
    for sequence, accepted_row in enumerate(accepted, 1):
        result = collect_batch(
            "001", store=store, spec=spec, options=BatchControlOptionsV1(max_io_steps=1)
        )
        assert result.outcome == "partial"
        assert result.collection is not None and result.collection.selection_pending
        assert not result.collection.failed_sources
        with ReadOnlyRepository(store) as repo:
            row = repo.connection.execute(
                "SELECT record_json FROM batch_facts WHERE run_id='001' "
                "AND entity_id='openai-projection' ORDER BY fact_seq DESC LIMIT 1"
            ).fetchone()
            assert row is not None
            projection = json.loads(row[0])
            assert projection["after"] == accepted_row[2]
            assert projection["cut"] == accepted[-1][2]
            assert projection["sequence"] == sequence
            assert not repo.connection.execute("SELECT 1 FROM results").fetchone()
            assert not repo.connection.execute(
                "SELECT 1 FROM batch_member_receipts"
            ).fetchone()
            assert not executor_is_active(repo, "001")
        assert service.events() == events
        assert terminal_reference.read_bytes() == terminal
    with ReadOnlyRepository(store) as repo:
        ready = [
            json.loads(row[0])
            for row in repo.connection.execute(
                "SELECT record_json FROM batch_facts WHERE run_id='001' "
                "AND entity_id LIKE 'openai-ready:%' ORDER BY fact_seq"
            )
        ]
        assert len(ready) == 2
        assert {r["submission_id"] for r in ready} == {accepted[1][0]}
        assert {r["role"] for r in ready} == {"output", "error"}
    result = collect_batch("001", store=store, spec=spec)
    assert result.batch.missing_members == 1
    with ReadOnlyRepository(store) as repo:
        assert (
            repo.connection.execute("SELECT count(*) FROM results").fetchone()[0] == 1
        )
        assert [
            row[0]
            for row in repo.connection.execute(
                "SELECT submission_id FROM batch_member_receipts"
            )
        ] == [accepted[1][0]]
        assert not executor_is_active(repo, "001")
    assert not any(
        path.startswith("/v1/batches") for _, path in service.events()[len(events) :]
    )
    with service.connect() as db:
        job = json.loads(
            db.execute(
                "SELECT body FROM jobs WHERE id=?", (accepted[0][1],)
            ).fetchone()[0]
        )
        job["status"] = "completed"
        db.execute(
            "UPDATE jobs SET body=? WHERE id=?", (json.dumps(job), accepted[0][1])
        )
    poll_batch("001", store=store)
    for _ in range(4):
        result = collect_batch("001", store=store, spec=spec)
        if not result.batch.recovery_required:
            break
    assert result.outcome == "ok" and not result.batch.recovery_required
    assert result.batch.missing_members == 0
    with ReadOnlyRepository(store) as repo:
        assert (
            repo.connection.execute("SELECT count(*) FROM results").fetchone()[0] == 2
        )
        assert [
            tuple(row)
            for row in repo.connection.execute(
                "SELECT submission_id,remote_id,rowid FROM submissions "
                "WHERE run_id='001' AND certainty='accepted' ORDER BY rowid"
            )
        ] == accepted
        assert (
            repo.connection.execute(
                "SELECT count(*) FROM batch_member_receipts"
            ).fetchone()[0]
            == 2
        )
        assert not executor_is_active(repo, "001")
    final = accepted_chain_snapshot(store)
    assert final["guards"] == guards
    for table in ("batch_operations", "operation_facts"):
        assert final["projections"][table] == baseline["projections"][table]
    for kind in ("objects", "registered"):
        assert {n: final[kind].get(n) for n in baseline[kind]} == baseline[kind]
    assert terminal_reference.read_bytes() == terminal
    assert [event for event in service.events() if event[0] == "POST"] == posts

    first_reference = terminal_reference.with_name(accepted[0][0] + ".json")
    assert json.loads(first_reference.read_bytes())["submission_id"] == accepted[0][0]
    assert json.loads(terminal)["submission_id"] == accepted[1][0]
    saved = tmp_path / "original-status.json"
    events = service.events()
    first_reference.replace(saved)  # Preserve the original object behind the hard link.
    try:
        first_reference.write_bytes(terminal)
        with pytest.raises(LifecycleError) as error:
            collect_batch("001", store=store, spec=spec)
        assert error.value.category == "execution"
        assert isinstance(error.value.__context__, IntegrityError)
        assert error.value.__context__.args == (
            "screened status reference conflicts with accepted job",
        )
        assert (
            first_reference.read_bytes() == terminal_reference.read_bytes() == terminal
        )
    finally:
        saved.replace(first_reference)
    assert service.events() == events
    assert_accepted_chain_unchanged(store, final)


def test_selected_ready_source_rejects_sibling_terminal_status(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from judgmentllm.batch_models import OpenAIBatchReadyV1, OpenAIBatchStatusV1
    from judgmentllm.execution.openai_batch import source_id
    from judgmentllm.lifecycle_models import LifecycleError
    from judgmentllm.persistence.hashing import bytes_digest, canonical_bytes
    from judgmentllm.persistence.models import IntegrityError
    from judgmentllm.persistence.native_batch import fact, latest
    from judgmentllm.persistence.ownership import ExecutorOwnership
    from judgmentllm.persistence.repository import Repository

    service, spec, store, accepted, _ = _setup_terminal_sibling(tmp_path, monkeypatch)
    poll_batch("001", store=store)
    collect_batch("001", store=store, spec=spec)  # Sibling output.
    collected = collect_batch("001", store=store, spec=spec)  # Sibling error EOF.
    assert collected.batch.missing_members == 1
    assert collected.collection is not None
    assert collected.collection.pending_sources == 0
    original = accepted_chain_snapshot(store)
    assert len(original["projections"]["results"]) == 1
    assert len(original["projections"]["batch_member_receipts"]) == 1

    with Repository(store) as repo, ExecutorOwnership(repo, "001") as ownership:
        assert ownership.owner is not None
        target = repo.get_submission("001", accepted[0][0])
        sibling = latest(repo, "001", "status", accepted[1][0], OpenAIBatchStatusV1)
        assert target.batch_uid is not None and target.certainty == "accepted"
        assert sibling is not None and sibling.state == "completed"
        assert (sibling.submission_id, sibling.remote_id) == accepted[1][:2]
        manifest = repo.get_manifest("001").artifact.sha256
        source = source_id(manifest, target.batch_uid, target.submission_id, "output")
        candidate = OpenAIBatchReadyV1(
            run_id="001",
            manifest_sha256=manifest,
            shard_id=target.batch_uid,
            submission_id=target.submission_id,
            source_id=source,
            role="output",
            status_sha256=bytes_digest(canonical_bytes(sibling)),
        )
        entity = "openai-ready:" + source
        fact(repo, "001", ownership.owner.token, "cursor", entity, candidate)
        assert latest(repo, "001", "cursor", entity, OpenAIBatchReadyV1) == candidate

    before = accepted_chain_snapshot(store)
    ready_facts = before["projections"]["ready_facts"]
    assert ready_facts[:-1] == original["projections"]["ready_facts"]
    assert before["projections"] == {
        **original["projections"],
        "ready_facts": ready_facts,
    }
    events = service.events()
    with pytest.raises(LifecycleError) as error:
        collect_batch("001", store=store, spec=spec)
    assert error.value.category == "execution"
    assert isinstance(error.value.__context__, IntegrityError)
    assert error.value.__context__.args == ("ready source job association differs",)
    assert service.events() == events
    assert_accepted_chain_unchanged(store, before)


def test_ready_projection_digest_rejection_preserves_acceptance(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_batch_preparation_rejections import corrupt_row

    from judgmentllm.batch_models import OpenAIBatchReadyV1
    from judgmentllm.lifecycle_models import LifecycleError
    from judgmentllm.persistence.hashing import canonical_bytes
    from judgmentllm.persistence.models import IntegrityError

    service, spec, store = setup(tmp_path, monkeypatch, responses=1)
    original = accepted_chain_snapshot(store)
    with ReadOnlyRepository(store) as repo:
        row = repo.connection.execute(
            "SELECT fact_seq,record_json,receipt_sha256 FROM batch_facts "
            "WHERE run_id='001' AND entity_id LIKE 'openai-ready:%' "
            "ORDER BY fact_seq LIMIT 1"
        ).fetchone()
        assert row is not None
        sequence, raw, digest = tuple(row)
    values = json.loads(raw)
    assert values["status_sha256"] != "0" * 64
    values["status_sha256"] = "0" * 64
    changed = OpenAIBatchReadyV1.model_validate(values)
    corrupt_row(
        store,
        "batch_facts",
        "UPDATE batch_facts SET record_json=? WHERE fact_seq=?",
        (canonical_bytes(changed).decode(), sequence),
    )
    with ReadOnlyRepository(store) as repo:
        assert (
            repo.connection.execute(
                "SELECT receipt_sha256 FROM batch_facts WHERE fact_seq=?", (sequence,)
            ).fetchone()[0]
            == digest
        )
    before = accepted_chain_snapshot(store)
    assert before["guards"] == original["guards"]
    assert before["objects"] == original["objects"]
    assert before["references"] == original["references"]
    events = service.events()
    with pytest.raises(LifecycleError) as error:
        collect_batch("001", store=store, spec=spec)
    assert error.value.category == "execution"
    assert error.value.message == "durable run integrity or storage operation failed"
    assert error.value.__suppress_context__ is True
    assert isinstance(error.value.__context__, IntegrityError)
    assert error.value.__context__.args == ("ready-source projection differs",)
    assert service.events() == events
    assert_accepted_chain_unchanged(store, before)


def test_ready_terminal_projection_cannot_rebind_existing_source(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_batch_preparation_rejections import corrupt_row

    from judgmentllm.lifecycle_models import LifecycleError
    from judgmentllm.persistence.models import IntegrityError

    service = Service(tmp_path / "service.sqlite")
    install(monkeypatch, service)
    spec = specification(tmp_path, responses=1)
    store = tmp_path / "store"
    prepare_batch(spec, store=store, run_id="001")
    service.configure(store, spec)
    service.set("initial_status", "in_progress")
    submit_batch("001", store=store, spec=spec)
    poll_batch("001", store=store)
    with ReadOnlyRepository(store) as repo:
        submission, remote = tuple(
            repo.connection.execute(
                "SELECT submission_id,remote_id FROM submissions "
                "WHERE certainty='accepted'"
            ).fetchone()
        )
        running = tuple(
            repo.connection.execute(
                "SELECT fact_seq,record_json,receipt_sha256 FROM batch_facts "
                "WHERE kind='status' AND entity_id=? ORDER BY fact_seq DESC LIMIT 1",
                (submission,),
            ).fetchone()
        )
        assert json.loads(running[1])["state"] == "running"
        directory = repo.run_directory("001")
    with service.connect() as db:
        job = json.loads(
            db.execute("SELECT body FROM jobs WHERE id=?", (remote,)).fetchone()[0]
        )
        job["status"] = "completed"
        db.execute("UPDATE jobs SET body=? WHERE id=?", (json.dumps(job), remote))
    poll_batch("001", store=store)
    original = accepted_chain_snapshot(store)
    assert len(original["projections"]["ready_facts"]) == 2
    reference = directory / "batch" / "openai-status" / (submission + ".json")
    terminal = reference.read_bytes()
    assert json.loads(terminal)["state"] == "completed"
    with ReadOnlyRepository(store) as repo:
        terminal_row = repo.connection.execute(
            "SELECT fact_seq,record_json FROM batch_facts WHERE kind='status' "
            "AND entity_id=? ORDER BY fact_seq DESC LIMIT 1",
            (submission,),
        ).fetchone()
        assert terminal_row is not None
        assert terminal_row[0] != running[0] and terminal_row[1].encode() == terminal
    corrupt_row(
        store,
        "batch_facts",
        "UPDATE batch_facts SET entity_id=? WHERE fact_seq=?",
        (submission + ":terminal-unavailable", terminal_row[0]),
    )
    before = accepted_chain_snapshot(store)
    assert before["guards"] == original["guards"]
    assert before["objects"] == original["objects"]
    assert before["references"] == original["references"]
    assert (
        before["projections"]["ready_facts"] == original["projections"]["ready_facts"]
    )
    with ReadOnlyRepository(store) as repo:
        assert (
            tuple(
                repo.connection.execute(
                    "SELECT fact_seq,record_json,receipt_sha256 FROM batch_facts "
                    "WHERE kind='status' AND entity_id=? ORDER BY fact_seq DESC LIMIT 1",
                    (submission,),
                ).fetchone()
            )
            == running
        )
    assert reference.read_bytes() == terminal
    events = service.events()
    with pytest.raises(LifecycleError) as error:
        collect_batch("001", store=store, spec=spec)
    assert error.value.category == "execution"
    assert error.value.message == "durable run integrity or storage operation failed"
    assert error.value.__suppress_context__ is True
    assert isinstance(error.value.__context__, IntegrityError)
    assert error.value.__context__.args == (
        "ready source terminal association changed",
    )
    assert service.events() == events
    assert_accepted_chain_unchanged(store, before)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "unopened",
        "other-owned-shard",
        "other-owned-ready",
        "shard-digest",
        "payload-seal",
    ],
)
async def test_upload_resolver_requires_its_owned_preparation(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    from test_batch_preparation_rejections import corrupt_row

    from judgmentllm import apoll_batch, asubmit_batch
    from judgmentllm.batch_models import BatchOperationIntentV1, OpenAIBatchReadyV1
    from judgmentllm.execution.native_batch import InvocationBudget
    from judgmentllm.execution.openai_batch import OpenAIBatchSession
    from judgmentllm.openai_batch_transport import batch_context
    from judgmentllm.persistence.hashing import canonical_bytes
    from judgmentllm.persistence.models import IntegrityError
    from judgmentllm.persistence.ownership import executor_is_active
    from judgmentllm.providers.openai_batch import OpenAIBatchAdapter

    service = Service(tmp_path / "service.sqlite")
    install(monkeypatch, service)
    spec = specification(tmp_path, responses=1)
    store = tmp_path / "store"
    prepare_batch(spec, store=store, run_id="001")
    service.configure(store, spec)
    await asubmit_batch("001", store=store, spec=spec)
    with ReadOnlyRepository(store) as repo:
        intent = BatchOperationIntentV1.model_validate_json(
            repo.connection.execute(
                "SELECT record_json FROM batch_operations WHERE kind='upload'"
            ).fetchone()[0]
        )
    other_ready = None
    commits: list[str] = []
    if case in ("other-owned-shard", "other-owned-ready"):
        other = tmp_path / "other"
        other.mkdir()
        other_spec = specification(other, responses=1)
        other_store = other / "store"
        other_run = "other" if case == "other-owned-shard" else "001"
        prepare_batch(other_spec, store=other_store, run_id=other_run)
        if case == "other-owned-shard":

            class UploadCommitted(BaseException):
                pass

            def stop_after_upload(stage: str, identity: str) -> None:
                if stage == "after_batch_operation_commit":
                    commits.append(identity)
                    raise UploadCommitted

            # Only an input upload is needed; configure() assumes run 001.
            with pytest.raises(UploadCommitted):
                await asubmit_batch(
                    other_run,
                    store=other_store,
                    spec=other_spec,
                    _fault=stop_after_upload,
                )
        else:
            service.configure(other_store, other_spec)
            await asubmit_batch(other_run, store=other_store, spec=other_spec)
            await apoll_batch(other_run, store=other_store)
        with ReadOnlyRepository(other_store) as repo:
            intent = BatchOperationIntentV1.model_validate_json(
                repo.connection.execute(
                    "SELECT record_json FROM batch_operations "
                    "WHERE run_id=? AND kind='upload'",
                    (other_run,),
                ).fetchone()[0]
            )
            assert not executor_is_active(repo, other_run)
            if case == "other-owned-shard":
                assert commits == [intent.instance_id]
            else:
                other_ready = OpenAIBatchReadyV1.model_validate_json(
                    repo.connection.execute(
                        "SELECT record_json FROM batch_facts "
                        "WHERE entity_id LIKE 'openai-ready:%' ORDER BY fact_seq LIMIT 1"
                    ).fetchone()[0]
                )
    budget = InvocationBudget(BatchControlOptionsV1())
    adapter = OpenAIBatchAdapter(batch_context(spec), budget=budget)
    events = service.events()
    original = accepted_chain_snapshot(store)
    try:
        session = OpenAIBatchSession("001", store, adapter, budget)
        with session:
            if case != "unopened":
                session.open_sources(spec, False)
            if case == "shard-digest":
                raw, digest = session.repository.connection.execute(
                    "SELECT record_json,receipt_sha256 FROM batch_preparations "
                    "WHERE shard_id=?",
                    (intent.shard_id,),
                ).fetchone()
                fields = json.loads(raw)
                fields["ordinal"] += 1
                changed = session.shard_model.model_validate(fields)
                corrupt_row(
                    store,
                    "batch_preparations",
                    "UPDATE batch_preparations SET record_json=? WHERE shard_id=?",
                    (canonical_bytes(changed).decode(), intent.shard_id),
                )
                assert (
                    session.repository.connection.execute(
                        "SELECT receipt_sha256 FROM batch_preparations WHERE shard_id=?",
                        (intent.shard_id,),
                    ).fetchone()[0]
                    == digest
                )
            elif case == "payload-seal":
                raw = session.repository.connection.execute(
                    "SELECT record_json FROM batch_preparations WHERE shard_id=?",
                    (intent.shard_id,),
                ).fetchone()[0]
                shard = session.shard_model.model_validate_json(raw)
                authorized = session.upload_bytes(intent)
                assert authorized == session.store.read(shard.input)
                assert authorized is not None
                alternate = json.loads(authorized)
                assert alternate["custom_id"] != "0" * 64
                alternate["custom_id"] = "0" * 64
                replacement = session.store.put(
                    canonical_bytes(alternate) + b"\n", shard.input.media_type
                )
                fields = json.loads(raw)
                fields["input"] = replacement.model_dump(mode="json")
                record = canonical_bytes(session.shard_model.model_validate(fields))
                receipt = session.store.put(record, "application/json")
                session.repository.register_artifact("001", replacement)
                session.repository.register_artifact("001", receipt)
                corrupt_row(
                    store,
                    "batch_preparations",
                    "UPDATE batch_preparations SET record_json=?,receipt_sha256=? "
                    "WHERE shard_id=?",
                    (record.decode(), receipt.sha256, intent.shard_id),
                )
            before = accepted_chain_snapshot(store, include_executor_lock=False)
            assert before["guards"] == original["guards"]
            if case == "unopened":
                assert session.upload_bytes(intent) is None
            elif case == "other-owned-ready":
                assert other_ready is not None
                with pytest.raises(
                    IntegrityError,
                    match="ready source differs from its accepted submission",
                ):
                    session.selected_job(other_ready)
            else:
                if case == "other-owned-shard":
                    assert (
                        session.repository.connection.execute(
                            "SELECT 1 FROM batch_preparations "
                            "WHERE run_id=? AND shard_id=?",
                            (session.run_id, intent.shard_id),
                        ).fetchone()
                        is None
                    )
                message = {
                    "other-owned-shard": "upload recovery lacks its original shard",
                    "shard-digest": "upload recovery shard differs from seal",
                    "payload-seal": "upload recovery payload differs from seal",
                }[case]
                with pytest.raises(IntegrityError, match=message):
                    session.upload_bytes(intent)
            owner = session.repository.get_owner("001")
            assert owner is not None and owner.token == session.token
            assert executor_is_active(session.repository, "001") is True
    finally:
        await adapter.aclose()
    assert service.events() == events
    with ReadOnlyRepository(store) as repo:
        assert repo.get_owner("001") is None
        assert executor_is_active(repo, "001") is False
    # Compare the permanent lock bytes from outside the owned session.
    before["references"]["executor.lock"] = original["references"]["executor.lock"]
    assert_accepted_chain_unchanged(store, before)


def test_ready_upload_receipt_is_rescreened_before_collection(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    from types import SimpleNamespace

    from judgmentllm.batch_models import BatchCursorV1, OpenAIBatchSourceBindingV1
    from judgmentllm.execution.native_batch import InvocationBudget
    from judgmentllm.execution.openai_batch import OpenAIBatchSession, source_state
    from judgmentllm.openai_batch_transport import batch_context
    from judgmentllm.persistence.native_batch import latest
    from judgmentllm.providers import openai_batch

    service, spec, store = setup(tmp_path, monkeypatch, responses=2)
    options = BatchControlOptionsV1(max_result_rows=1)
    collect_batch("001", store=store, spec=spec, options=options)
    collect_batch("001", store=store, spec=spec, options=options)
    with ReadOnlyRepository(store) as repo:
        (raw,) = repo.connection.execute(
            "SELECT record_json FROM batch_facts WHERE kind='cursor' "
            "AND entity_id LIKE 'openai-binding:%' "
            "AND json_extract(record_json,'$.ready.role')='output'"
        ).fetchone()
        binding = OpenAIBatchSourceBindingV1.model_validate_json(raw)
        source = binding.ready.source_id
        cursor = latest(repo, "001", "cursor", source, BatchCursorV1)
        assert cursor is not None and cursor.position == 1 and not cursor.exhausted
        assert repo.summary("001").successful_results == 1
    before = accepted_chain_snapshot(store)
    events = service.events()

    def fixture_credentials(_provider: str) -> SimpleNamespace:
        return SimpleNamespace(_api_key=lambda: binding.job.input_file_id)

    monkeypatch.setattr(openai_batch, "_resolve_credentials", fixture_credentials)

    async def collect() -> None:
        budget = InvocationBudget(BatchControlOptionsV1())
        adapter = openai_batch.OpenAIBatchAdapter(batch_context(spec), budget=budget)
        try:
            session = OpenAIBatchSession("001", store, adapter, budget)
            with session:
                await adapter.client()
                assert await session.collect_ready(spec, False)
        finally:
            await adapter.aclose()

    asyncio.run(collect())
    with ReadOnlyRepository(store) as repo:
        admission = source_state(repo, "001", source)
        assert admission is not None and admission.code == "unsafe_metadata"
        assert admission.frontier == cursor.position
        assert latest(repo, "001", "cursor", source, BatchCursorV1) == cursor
    assert service.events() == events
    assert_accepted_chain_unchanged(store, before)


def test_two_poll_rounds_keep_the_original_remote_identity(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx2

    service = Service(tmp_path / "service.sqlite")
    install(monkeypatch, service)
    spec = specification(tmp_path, responses=1)
    store = tmp_path / "store"
    prepare_batch(spec, store=store, run_id="001")
    service.configure(store, spec)
    service.set("initial_status", "in_progress")
    submit_batch("001", store=store, spec=spec)
    before = accepted_chain_snapshot(store)
    original_request = service.request
    polls: list[str] = []

    def advance(request: httpx2.Request) -> httpx2.Response:
        response = original_request(request)
        if request.method == "GET" and request.url.path == "/v1/batches/batch_1":
            polls.append(request.url.path)
            if len(polls) == 1:
                with service.connect() as db:
                    body = json.loads(
                        db.execute(
                            "SELECT body FROM jobs WHERE id='batch_1'"
                        ).fetchone()[0]
                    )
                    body["status"] = "completed"
                    db.execute(
                        "UPDATE jobs SET body=? WHERE id='batch_1'", (json.dumps(body),)
                    )
        return response

    with monkeypatch.context() as patch:
        patch.setattr(service, "request", advance)
        result = poll_batch(
            "001",
            store=store,
            options=BatchControlOptionsV1(max_polls=2, poll_interval_seconds=0),
        )
    assert result.outcome == "ok" and polls == ["/v1/batches/batch_1"] * 2
    after = accepted_chain_snapshot(store)
    assert after["projections"]["submissions"] == before["projections"]["submissions"]
    assert (
        after["projections"]["batch_operations"]
        == before["projections"]["batch_operations"]
    )
    assert not after["projections"]["results"]
    assert len(after["projections"]["status_facts"]) == 2
    assert [e for e in service.events() if e[0] == "POST"] == [
        ("POST", "/v1/files"),
        ("POST", "/v1/batches"),
    ]


@pytest.mark.parametrize("boundary", ["projection", "metadata"])
def test_bounded_collection_failure_defers_work_without_duplicate_effects(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    import httpx2

    service = Service(tmp_path / "service.sqlite")
    install(monkeypatch, service)
    spec = specification(tmp_path, responses=1)
    store = tmp_path / "store"
    prepare_batch(spec, store=store, run_id="001")
    service.configure(store, spec)
    submit_batch("001", store=store, spec=spec)
    if boundary == "metadata":
        poll_batch("001", store=store)
    before = accepted_chain_snapshot(store)
    events = service.events()
    original_request = service.request
    failures: list[str] = []

    def unavailable(request: httpx2.Request) -> httpx2.Response:
        response = original_request(request)
        if request.url.path == "/v1/files/file-output_1":
            response.close()
            failures.append(request.url.path)
            return httpx2.Response(503, stream=httpx2.ByteStream(b""))
        return response

    with monkeypatch.context() as patch:
        if boundary == "metadata":
            patch.setattr(service, "request", unavailable)
        pending = collect_batch(
            "001",
            store=store,
            spec=spec,
            options=BatchControlOptionsV1(max_io_steps=1)
            if boundary == "projection"
            else BatchControlOptionsV1(),
        )
    assert pending.collection is not None
    assert pending.outcome == "partial" and pending.collection.selection_pending
    assert not pending.collection.failed_sources
    expected = [] if boundary == "projection" else [("GET", "/v1/files/file-output_1")]
    assert service.events()[len(events) :] == expected
    assert len(failures) == int(boundary == "metadata")
    assert_accepted_chain_unchanged(store, before)
    poll_batch("001", store=store)
    final = None
    for _ in range(4):
        final = collect_batch("001", store=store, spec=spec)
        if not final.batch.recovery_required:
            break
    assert final is not None
    assert final.outcome == "ok" and final.batch.missing_members == 0
    assert [e for e in service.events() if e[0] == "POST"] == [
        ("POST", "/v1/files"),
        ("POST", "/v1/batches"),
    ]


def test_one_step_ready_selection_defers_the_callers_retry(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from judgmentllm import RetrySelection, resume_batch

    service, spec, store = setup(tmp_path, monkeypatch, responses=1)
    with ReadOnlyRepository(store) as repo:
        task = repo.connection.execute(
            "SELECT task_id FROM tasks ORDER BY scheduling_ordinal LIMIT 1"
        ).fetchone()[0]
    retry = RetrySelection(task_ids=(task,), operation_id="deferred-ready-retry")
    original = retry.model_dump_json()
    before = accepted_chain_snapshot(store)
    events = service.events()
    pending = resume_batch(
        "001",
        store=store,
        spec=spec,
        retry=retry,
        options=BatchControlOptionsV1(max_io_steps=1),
    )
    assert pending.collection is not None
    assert pending.outcome == "partial" and pending.collection.selection_pending
    assert retry.model_dump_json() == original
    with ReadOnlyRepository(store) as repo:
        assert (
            repo.connection.execute("SELECT count(*) FROM retry_operations").fetchone()[
                0
            ]
            == 0
        )
        assert (
            repo.connection.execute(
                "SELECT count(*) FROM batch_facts WHERE entity_id LIKE 'openai-admission:%'"
            ).fetchone()[0]
            == 0
        )
    assert service.events() == events
    assert_accepted_chain_unchanged(store, before)


def test_scientific_subset_retry_recovers_its_original_attempt(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from judgmentllm import RetrySelection, resume_batch
    from judgmentllm.persistence.ownership import executor_is_active

    class RetryCut(BaseException):
        pass

    service = Service(tmp_path / "service.sqlite")
    install(monkeypatch, service)
    spec = specification(tmp_path, responses=1, traits=2)
    store = tmp_path / "store"
    prepare_batch(spec, store=store, run_id="001")
    service.configure(store, spec)
    with service.connect() as db:
        db.execute("UPDATE outputs SET text='not assessment JSON'")
    submit_batch("001", store=store, spec=spec)
    poll_batch("001", store=store)
    for _ in range(4):
        collected = collect_batch("001", store=store, spec=spec)
        if not collected.batch.recovery_required:
            break
    with ReadOnlyRepository(store) as repo:
        prior = tuple(repo.iter_results("001"))
        assert len(prior) == 2
        assert all(result.status == "invalid" for result in prior)
        assert len({result.request_uid for result in prior}) == 1
        manifest = (repo.run_directory("001") / "manifest.json").read_bytes()
    retry = RetrySelection(
        task_ids=(prior[0].task_id,), operation_id="owned-subset-retry"
    )
    checkpoints: list[str] = []

    def interrupted(stage: str, identity: str) -> None:
        if stage == "after_batch_retry_attempt":
            checkpoints.append(identity)
            raise RetryCut

    with pytest.raises(RetryCut):
        resume_batch("001", store=store, spec=spec, retry=retry, _fault=interrupted)
    assert checkpoints == [retry.operation_id]
    with ReadOnlyRepository(store) as repo:
        prepared = [
            attempt
            for attempt in repo.iter_attempts("001")
            if attempt.authorization_id == retry.operation_id
        ]
        assert len(prepared) == 1
        child = prepared[0]
        assert repo.attempt_task_ids("001", child.attempt_id) == retry.task_ids
        assert not repo.connection.execute(
            "SELECT 1 FROM batch_prepared_members WHERE attempt_id=?",
            (child.attempt_id,),
        ).fetchone()
        assert (
            repo.connection.execute(
                "SELECT count(*) FROM submissions WHERE run_id=? AND attempt_id=?",
                ("001", child.attempt_id),
            ).fetchone()[0]
            == 0
        )
        assert not executor_is_active(repo, "001")
    posts = [event for event in service.events() if event[0] == "POST"]
    assert posts == [("POST", "/v1/files"), ("POST", "/v1/batches")]

    def configure_child(stage: str, identity: str) -> None:
        if stage == "after_batch_retry_shard":
            assert identity == retry.operation_id
            service.configure(store, spec)

    result = resume_batch(
        "001", store=store, spec=spec, retry=retry, _fault=configure_child
    )
    for _ in range(4):
        if not result.batch.recovery_required:
            break
        result = collect_batch("001", store=store, spec=spec)
    assert result.batch.missing_members == 0
    with ReadOnlyRepository(store) as repo:
        results = tuple(repo.iter_results("001"))
        assert len(results) == 3
        prior_keys = {(r.task_id, r.attempt_id) for r in prior}
        assert {
            (r.task_id, r.attempt_id): r.model_dump_json()
            for r in results
            if (r.task_id, r.attempt_id) in prior_keys
        } == {(r.task_id, r.attempt_id): r.model_dump_json() for r in prior}
        success = [r for r in results if r.status == "complete"]
        assert len(success) == 1 and success[0].task_id == retry.task_ids[0]
        assert success[0].attempt_id == child.attempt_id
        assert len(tuple(repo.iter_attempts("001"))) == 2
        assert (repo.run_directory("001") / "manifest.json").read_bytes() == manifest
        assert (
            repo.connection.execute(
                "SELECT count(*) FROM submissions WHERE run_id=? AND attempt_id=?",
                ("001", child.attempt_id),
            ).fetchone()[0]
            == 0
        )
        child_members = repo.connection.execute(
            "SELECT shard_id,member_key FROM batch_prepared_members "
            "WHERE run_id=? AND attempt_id=?",
            ("001", child.attempt_id),
        ).fetchall()
        child_receipts = repo.connection.execute(
            "SELECT submission_id,shard_id,member_key FROM batch_member_receipts "
            "WHERE run_id=? AND attempt_id=?",
            ("001", child.attempt_id),
        ).fetchall()
        assert len(child_members) == len(child_receipts) == 1
        accepted = repo.get_submission_for_attempt("001", child.attempt_id)
        assert accepted is not None
        assert accepted.attempt_id is None
        assert accepted.batch_uid == child_members[0][0]
        assert tuple(child_receipts[0]) == (
            accepted.submission_id,
            accepted.batch_uid,
            child_members[0][1],
        )
        assert accepted.certainty == "accepted" and accepted.state == "resolved"
    after = [event for event in service.events() if event[0] == "POST"]
    assert after == posts * 2
    resume_batch("001", store=store, spec=spec, retry=retry)
    assert [event for event in service.events() if event[0] == "POST"] == after
    with ReadOnlyRepository(store) as repo:
        assert len(tuple(repo.iter_attempts("001"))) == 2
        assert tuple(repo.iter_results("001")) == results
        assert (repo.run_directory("001") / "manifest.json").read_bytes() == manifest
        assert not executor_is_active(repo, "001")


def test_scientific_subset_retry_rejects_changed_attempt_payload(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3

    from judgmentllm import RetrySelection, resume_batch
    from judgmentllm.lifecycle_models import LifecycleError
    from judgmentllm.persistence.models import IntegrityError
    from judgmentllm.persistence.repository import Repository

    class RetryCut(BaseException):
        pass

    service = Service(tmp_path / "service.sqlite")
    install(monkeypatch, service)
    spec = specification(tmp_path, responses=1, traits=2)
    store = tmp_path / "store"
    prepare_batch(spec, store=store, run_id="001")
    service.configure(store, spec)
    with service.connect() as db:
        db.execute("UPDATE outputs SET text='not assessment JSON'")
    submit_batch("001", store=store, spec=spec)
    poll_batch("001", store=store)
    collected = None
    for _ in range(4):
        collected = collect_batch("001", store=store, spec=spec)
        if not collected.batch.recovery_required:
            break
    assert collected is not None and not collected.batch.recovery_required
    with ReadOnlyRepository(store) as repo:
        manifest = json.loads(
            (repo.run_directory("001") / "manifest.json").read_bytes()
        )
        assert manifest["schema_version"] == 13
        prior = tuple(repo.iter_results("001"))
        assert len(prior) == 2 and all(result.status == "invalid" for result in prior)
        (parent,) = repo.iter_attempts("001")
    retry = RetrySelection(
        task_ids=(prior[0].task_id,), operation_id="owned-subset-retry"
    )
    checkpoints: list[tuple[str, str]] = []

    def interrupted(stage: str, identity: str) -> None:
        checkpoints.append((stage, identity))
        if stage == "after_batch_retry_attempt":
            raise RetryCut

    with pytest.raises(RetryCut):
        resume_batch("001", store=store, spec=spec, retry=retry, _fault=interrupted)
    assert checkpoints[-1] == ("after_batch_retry_attempt", retry.operation_id)
    with Repository(store) as repo:
        (retained,) = (
            attempt
            for attempt in repo.iter_attempts("001")
            if attempt.authorization_id == retry.operation_id
        )
        assert repo.attempt_task_ids("001", retained.attempt_id) == retry.task_ids
        assert parent.payload_sha256 != retained.payload_sha256
        connection = repo.connection
        trigger_sql = "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='attempts_identity'"
        (trigger,) = connection.execute(trigger_sql).fetchone()
        update = "UPDATE attempts SET payload_sha256=? WHERE run_id=? AND attempt_id=?"
        changed = (parent.payload_sha256, "001", retained.attempt_id)
        with repo.transaction():
            connection.execute("DROP TRIGGER attempts_identity")
            assert connection.execute(update, changed).rowcount == 1
            connection.execute(trigger)
        assert connection.execute(trigger_sql).fetchone()[0] == trigger
        assert repo.get_attempt("001", retained.attempt_id) == retained.model_copy(
            update={"payload_sha256": parent.payload_sha256}
        )
        with repo.transaction():
            with pytest.raises(
                sqlite3.IntegrityError, match="immutable persistence record"
            ):
                connection.execute(update, (retained.payload_sha256, *changed[1:]))
    before = accepted_chain_snapshot(store)
    assert len(before["projections"]["batch_preparations"]) == 1
    assert len(before["projections"]["batch_prepared_members"]) == 1
    posts = [event for event in service.events() if event[0] == "POST"]
    assert posts == [("POST", "/v1/files"), ("POST", "/v1/batches")]

    with pytest.raises(LifecycleError) as error:
        resume_batch("001", store=store, spec=spec, retry=retry)
    assert error.value.category == "execution"
    assert isinstance(error.value.__context__, IntegrityError)
    assert error.value.__context__.args == ("existing Batch retry wire differs",)
    assert_accepted_chain_unchanged(store, before)
    assert [event for event in service.events() if event[0] == "POST"] == posts


@pytest.mark.asyncio
async def test_three_durable_admission_cuts_refuse_a_fourth_read(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from judgmentllm import acollect_batch, apoll_batch, asubmit_batch
    from judgmentllm.batch_models import OpenAIBatchReadyV1
    from judgmentllm.execution.native_batch import InvocationBudget
    from judgmentllm.execution.openai_batch import OpenAIBatchSession
    from judgmentllm.openai_batch_transport import batch_context
    from judgmentllm.providers.openai_batch import OpenAIBatchAdapter

    class AdmissionCut(BaseException):
        pass

    service = Service(tmp_path / "service.sqlite")
    install(monkeypatch, service)
    spec = specification(tmp_path, responses=1)
    store = tmp_path / "store"
    prepare_batch(spec, store=store, run_id="001")
    service.configure(store, spec)
    await asubmit_batch("001", store=store, spec=spec)
    await apoll_batch("001", store=store)
    cuts: list[str] = []

    def cut(stage: str, source: str) -> None:
        if stage == "after_openai_batch_admission":
            cuts.append(source)
            raise AdmissionCut

    for _ in range(6):
        with pytest.raises(AdmissionCut):
            await acollect_batch("001", store=store, spec=spec, _fault=cut)
    before = accepted_chain_snapshot(store)
    with ReadOnlyRepository(store) as repo:
        admitted = [
            json.loads(row[0])
            for row in repo.connection.execute(
                "SELECT record_json FROM batch_facts "
                "WHERE entity_id LIKE 'openai-admission:%' "
                "AND json_extract(record_json,'$.code')='admitted'"
            )
        ]
        ready = OpenAIBatchReadyV1.model_validate_json(
            repo.connection.execute(
                "SELECT record_json FROM batch_facts WHERE entity_id LIKE 'openai-ready:%' "
                "ORDER BY fact_seq LIMIT 1"
            ).fetchone()[0]
        )
    assert len(admitted) == len(cuts) == 6 and len(set(cuts)) == 2
    for source in set(cuts):
        stalls = sorted(row["stalls"] for row in admitted if row["source_id"] == source)
        assert stalls == [1, 2, 3]
    budget = InvocationBudget(BatchControlOptionsV1())
    adapter = OpenAIBatchAdapter(batch_context(spec), budget=budget)
    try:
        session = OpenAIBatchSession("001", store, adapter, budget)
        with session:
            session.open_sources(spec, False)
            assert session.admit(ready) is None
            submission, receipt, status = session.selected_job(ready)
            file_id = (
                status.output_file_id
                if ready.role == "output"
                else status.error_file_id
            )
            assert file_id is not None
            await session.collect_file(ready, submission, receipt, status, file_id)
    finally:
        await adapter.aclose()
    result = await acollect_batch("001", store=store, spec=spec)
    assert result.outcome == "error"
    assert result.collection is not None
    assert result.collection.failed_sources == 2
    assert result.collection.codes == ("collection_no_progress",)
    assert not any(path.endswith("/content") for _, path in service.events())
    with ReadOnlyRepository(store) as repo:
        assert (
            repo.connection.execute(
                "SELECT count(*) FROM batch_facts "
                "WHERE entity_id LIKE 'openai-admission:%' "
                "AND json_extract(record_json,'$.code')='admitted'"
            ).fetchone()[0]
            == 6
        )
    assert_accepted_chain_unchanged(store, before)


@pytest.mark.parametrize("boundary", ["next-chunk", "replay-reserve"])
def test_physical_row_quota_preserves_the_committed_prefix(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    from judgmentllm.batch_models import BatchCursorV1, OpenAIBatchSourceBindingV1

    service, spec, store = setup(tmp_path, monkeypatch, responses=2)
    raw, size = service.file("file-output_1")
    first_row = raw.splitlines(keepends=True)[0]
    assert size == len(raw) and first_row.endswith(b"\n")
    service.set("fragment_bytes", len(first_row))
    first = collect_batch(
        "001", store=store, spec=spec, options=BatchControlOptionsV1(max_result_rows=1)
    )
    assert first.batch.missing_members == 1 and first.outcome == "partial"
    with ReadOnlyRepository(store) as repo:
        receipts = tuple(
            tuple(row)
            for row in repo.connection.execute(
                "SELECT member_key,receipt_sha256,record_json FROM batch_member_receipts"
            )
        )
        assert len(receipts) == 1
        assert receipts[0][0] == json.loads(first_row)["custom_id"]
        bindings = repo.connection.execute(
            "SELECT record_json FROM batch_facts WHERE kind='cursor' "
            "AND entity_id LIKE 'openai-binding:%' "
            "AND json_extract(record_json,'$.ready.role')='output'"
        ).fetchall()
        assert len(bindings) == 1
        binding = OpenAIBatchSourceBindingV1.model_validate_json(bindings[0][0])
        cursor_row = repo.connection.execute(
            "SELECT entity_id,record_json FROM batch_facts WHERE kind='cursor' "
            "AND entity_id=? ORDER BY fact_seq DESC LIMIT 1",
            (binding.ready.source_id,),
        ).fetchone()
        assert cursor_row is not None
        cursor = tuple(cursor_row)
        saved = BatchCursorV1.model_validate_json(cursor[1])
        assert saved.run_id == binding.ready.run_id == "001"
        assert saved.source_id == binding.ready.source_id
        assert saved.submission_id == binding.ready.submission_id
        assert saved.position == 1
        assert saved.exhausted is False
    with service.connect() as db:
        physical = db.execute(
            "SELECT byte_count FROM reads WHERE file='file-output_1' ORDER BY rowid"
        ).fetchall()
    assert len(physical) == 2 and physical[0][0] == len(first_row)
    if boundary == "replay-reserve":
        # Exhaust the independent empty error source before resuming this prefix.
        collect_batch("001", store=store, spec=spec)
        with service.connect() as db:
            read_cut = db.execute(
                "SELECT coalesce(max(rowid),0) FROM reads"
            ).fetchone()[0]
        service.set("fragment_bytes", 1)
        partial = collect_batch(
            "001",
            store=store,
            spec=spec,
            options=BatchControlOptionsV1(max_io_steps=128),
        )
        assert partial.outcome == "partial" and partial.batch.missing_members == 1
        with service.connect() as db:
            replayed = db.execute(
                "SELECT coalesce(sum(byte_count),0) FROM reads "
                "WHERE file='file-output_1' AND rowid>?",
                (read_cut,),
            ).fetchone()[0]
        assert 0 < replayed < len(first_row)
        with ReadOnlyRepository(store) as repo:
            assert (
                tuple(
                    tuple(row)
                    for row in repo.connection.execute(
                        "SELECT member_key,receipt_sha256,record_json "
                        "FROM batch_member_receipts"
                    )
                )
                == receipts
            )
            assert (
                tuple(
                    repo.connection.execute(
                        "SELECT entity_id,record_json FROM batch_facts WHERE kind='cursor' "
                        "AND entity_id=? ORDER BY fact_seq DESC LIMIT 1",
                        (cursor[0],),
                    ).fetchone()
                )
                == cursor
            )
    service.set("fragment_bytes", 65_536)
    final = first
    for _ in range(4):
        final = collect_batch("001", store=store, spec=spec)
        if not final.batch.recovery_required:
            break
    assert final.outcome == "ok" and final.batch.missing_members == 0
    with ReadOnlyRepository(store) as repo:
        assert repo.summary("001").successful_results == 2
        assert (
            tuple(
                repo.connection.execute(
                    "SELECT member_key,receipt_sha256,record_json "
                    "FROM batch_member_receipts WHERE member_key=?",
                    (receipts[0][0],),
                ).fetchone()
            )
            == receipts[0]
        )
        selector = json.loads(
            repo.connection.execute(
                "SELECT record_json FROM batch_facts WHERE entity_id='openai-selector' "
                "ORDER BY fact_seq DESC LIMIT 1"
            ).fetchone()[0]
        )
        assert selector["after"] < selector["cut"]
    events = service.events()
    assert collect_batch("001", store=store, spec=spec).batch.missing_members == 0
    assert service.events() == events
    assert [e for e in service.events() if e[0] == "POST"] == [
        ("POST", "/v1/files"),
        ("POST", "/v1/batches"),
    ]


def test_final_metadata_rejection_follows_real_source_eof(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx2

    from judgmentllm.batch_models import BatchCursorV1, OpenAIBatchSourceBindingV1

    service, spec, store = setup(tmp_path, monkeypatch, responses=1)
    original_request = service.request
    eof: list[bool] = []
    metadata: list[bool] = []
    replacements: list[httpx2.Response] = []

    class ObservedStream(httpx2.AsyncByteStream):
        def __init__(self, stream: httpx2.AsyncByteStream) -> None:
            self.stream = stream

        async def __aiter__(self) -> AsyncIterator[bytes]:
            async for chunk in self.stream:
                yield chunk
            eof.append(True)

        async def aclose(self) -> None:
            await self.stream.aclose()

    def final_fault(request: httpx2.Request) -> httpx2.Response:
        response: httpx2.Response = original_request(request)
        if request.url.path == "/v1/files/file-output_1/content":
            assert isinstance(response.stream, httpx2.AsyncByteStream)
            response.stream = ObservedStream(response.stream)
        elif request.url.path == "/v1/files/file-output_1":
            metadata.append(bool(eof))
            if eof:
                try:
                    body = json.loads(response.read())
                finally:
                    response.close()
                body["bytes"] = True
                response = httpx2.Response(200, json=body)
                response.close()
                replacements.append(response)
        return response

    with monkeypatch.context() as patch:
        patch.setattr(service, "request", final_fault)
        result = collect_batch("001", store=store, spec=spec)
    assert eof == [True] and metadata == [False, True]
    assert result.collection is not None
    assert result.outcome == "error" and "source_conflict" in result.collection.codes
    with ReadOnlyRepository(store) as repo:
        assert repo.summary("001").successful_results == 1
        assert (
            repo.connection.execute(
                "SELECT count(*) FROM batch_member_receipts"
            ).fetchone()[0]
            == 1
        )
        bindings = repo.connection.execute(
            "SELECT record_json FROM batch_facts WHERE kind='cursor' "
            "AND entity_id LIKE 'openai-binding:%' "
            "AND json_extract(record_json,'$.ready.role')='output'"
        ).fetchall()
        assert len(bindings) == 1
        binding = OpenAIBatchSourceBindingV1.model_validate_json(bindings[0][0])
        cursor_row = repo.connection.execute(
            "SELECT record_json FROM batch_facts WHERE kind='cursor' "
            "AND entity_id=? ORDER BY fact_seq DESC LIMIT 1",
            (binding.ready.source_id,),
        ).fetchone()
        assert cursor_row is not None
        cursor = BatchCursorV1.model_validate_json(cursor_row[0])
        assert cursor.run_id == binding.ready.run_id == "001"
        assert cursor.source_id == binding.ready.source_id
        assert cursor.submission_id == binding.ready.submission_id
        assert cursor.position == 1
        assert cursor.exhausted is False
    assert replacements and all(response.is_closed for response in replacements)
    assert [e for e in service.events() if e[0] == "POST"] == [
        ("POST", "/v1/files"),
        ("POST", "/v1/batches"),
    ]


@pytest.mark.parametrize("fault", ["stream-read", "post-eof-metadata"])
def test_collection_recovers_committed_row_after_io_failure(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    import httpx2

    from judgmentllm import resume_batch
    from judgmentllm.batch_models import (
        BatchCursorV1,
        BatchMemberReceiptV1,
        OpenAIBatchSourceBindingV1,
    )

    member_count = 2 if fault == "stream-read" else 1
    service, spec, store = setup(tmp_path, monkeypatch, responses=member_count)
    file_id = "file-output_1"
    metadata_path = "/v1/files/" + file_id
    content_path = metadata_path + "/content"
    raw, declared = service.file(file_id)
    rows = raw.splitlines(keepends=True)
    assert declared == len(raw) and len(rows) == member_count
    assert all(row.endswith(b"\n") for row in rows)
    service.set("fragment_bytes", len(rows[0]))
    original_request = service.request
    steps: list[str] = []

    class ObservedStream(httpx2.AsyncByteStream):
        def __init__(self, stream: httpx2.AsyncByteStream) -> None:
            self.stream = stream

        async def __aiter__(self) -> AsyncIterator[bytes]:
            async for chunk in self.stream:
                assert chunk == rows[0]
                yield chunk
                if fault == "stream-read":
                    assert progress()[1].position == 1
                    steps.append("read-error")
                    raise httpx2.ReadError("synthetic read failed after a complete row")
            steps.append("eof")

        async def aclose(self) -> None:
            await self.stream.aclose()

    def interrupted(request: httpx2.Request) -> httpx2.Response:
        response: httpx2.Response = original_request(request)
        if request.url.path == content_path:
            steps.append("content")
            assert isinstance(response.stream, httpx2.AsyncByteStream)
            response.stream = ObservedStream(response.stream)
        elif request.url.path == metadata_path:
            if fault == "post-eof-metadata" and steps[-1:] == ["eof"]:
                steps.append("metadata-503")
                response.close()
                return httpx2.Response(503, stream=httpx2.ByteStream(b""))
            steps.append("metadata")
        return response

    def progress() -> tuple[
        OpenAIBatchSourceBindingV1,
        BatchCursorV1,
        tuple[Any, ...],
        tuple[BatchMemberReceiptV1, ...],
    ]:
        with ReadOnlyRepository(store) as repo:
            (binding_row,) = repo.connection.execute(
                "SELECT record_json FROM batch_facts WHERE kind='cursor' "
                "AND entity_id LIKE 'openai-binding:%' "
                "AND json_extract(record_json,'$.ready.role')='output'"
            )
            binding = OpenAIBatchSourceBindingV1.model_validate_json(binding_row[0])
            cursor_row = repo.connection.execute(
                "SELECT record_json FROM batch_facts WHERE kind='cursor' "
                "AND entity_id=? ORDER BY fact_seq DESC LIMIT 1",
                (binding.ready.source_id,),
            ).fetchone()
            assert cursor_row is not None
            return (
                binding,
                BatchCursorV1.model_validate_json(cursor_row[0]),
                tuple(repo.iter_results("001")),
                tuple(
                    BatchMemberReceiptV1.model_validate_json(row[0])
                    for row in repo.connection.execute(
                        "SELECT record_json FROM batch_member_receipts ORDER BY rowid"
                    )
                ),
            )

    with monkeypatch.context() as patch:
        patch.setattr(service, "request", interrupted)
        pending = collect_batch("001", store=store, spec=spec)
    expected_steps = (
        ["read-error"] if fault == "stream-read" else ["eof", "metadata-503"]
    )
    assert steps == ["metadata", "content", *expected_steps]
    assert pending.outcome == "partial" and pending.batch.recovery_required
    assert pending.batch.missing_members == member_count - 1
    assert pending.collection is not None
    assert pending.collection.failed_sources == 0
    binding, cursor, committed_results, committed_receipts = progress()
    assert binding.file_id == file_id and binding.job.remote_id == "batch_1"
    assert cursor.source_id == binding.ready.source_id
    assert cursor.submission_id == binding.ready.submission_id
    assert cursor.position == 1 and cursor.exhausted is False
    assert len(committed_results) == len(committed_receipts) == 1
    assert committed_receipts[0].member_key == json.loads(rows[0])["custom_id"]

    final = resume_batch("001", store=store, spec=spec)
    for _ in range(4):
        if not final.batch.recovery_required:
            break
        final = collect_batch("001", store=store, spec=spec)
    assert final.outcome == "ok" and not final.batch.recovery_required
    recovered = progress()
    same_binding, exhausted, results, receipts = recovered
    assert same_binding == binding
    assert exhausted.source_id == cursor.source_id
    assert exhausted.submission_id == cursor.submission_id
    assert exhausted.position == member_count and exhausted.exhausted is True
    if fault == "post-eof-metadata":
        assert exhausted.prefix_sha256 == cursor.prefix_sha256
    assert results[:1] == committed_results and receipts[:1] == committed_receipts
    assert len(results) == len(receipts) == member_count
    assert all(result.status == "complete" for result in results)
    assert [receipt.source_position for receipt in receipts] == list(
        range(member_count)
    )
    assert [receipt.member_key for receipt in receipts] == [
        json.loads(row)["custom_id"] for row in rows
    ]
    assert all(receipt.source_id == cursor.source_id for receipt in receipts)
    assert all(
        (receipt.usage.input_tokens, receipt.usage.output_tokens) == (20, 10)
        for receipt in receipts
    )
    events = service.events()
    assert events.count(("GET", content_path)) == 2
    assert [event for event in events if event[0] == "POST"] == [
        ("POST", "/v1/files"),
        ("POST", "/v1/batches"),
    ]
    assert collect_batch("001", store=store, spec=spec).outcome == "ok"
    assert service.events() == events and progress() == recovered


@pytest.mark.parametrize("boundary", ["accepted-reference", "minimal-source"])
def test_real_read_boundaries_preserve_accepted_work_on_io_failure(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    from pathlib import Path

    from test_batch_preparation_rejections import corrupt_row

    from judgmentllm.lifecycle_models import LifecycleError
    from judgmentllm.persistence.models import PersistenceError

    if boundary == "accepted-reference":
        service, spec, store = setup(tmp_path, monkeypatch, responses=1)
        guards = accepted_chain_snapshot(store)["guards"]
        with ReadOnlyRepository(store) as repo:
            instance = repo.connection.execute(
                "SELECT instance_id FROM batch_operations WHERE kind='create'"
            ).fetchone()[0]
            target = repo.run_directory("001") / f"batch/operations/{instance}.json"
        corrupt_row(
            store,
            "batch_facts",
            "UPDATE batch_facts SET entity_id=? WHERE kind='operation' AND entity_id=?",
            (instance + ":unavailable", instance),
        )
        message = "OpenAI Batch accepted source evidence is unavailable"
    else:
        from judgmentllm.config import load_run_spec

        service = Service(tmp_path / "service.sqlite")
        install(monkeypatch, service)
        spec = specification(tmp_path, responses=1, capture="minimal")
        target = tmp_path / "spec.json"
        target.write_text(spec.model_dump_json())
        spec = load_run_spec(target)
        store = tmp_path / "store"
        prepare_batch(target, store=store, run_id="001")
        service.configure(store, spec)
        submit_batch("001", store=store, spec=spec)
        poll_batch("001", store=store)
        guards = accepted_chain_snapshot(store)["guards"]
        message = "prepared source or manifest validation failed"
    before = accepted_chain_snapshot(store)
    assert before["guards"] == guards
    events = service.events()
    original_open = Path.open
    reads: list[str] = []

    def unavailable(path: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if path == target and mode == "rb":
            reads.append(mode)
            raise OSError("synthetic owned read unavailable")
        return original_open(path, mode, *args, **kwargs)

    with monkeypatch.context() as patch:
        if boundary == "minimal-source":
            from judgmentllm import preparation
            from judgmentllm.preparation import _memory_source  # pyright: ignore[reportPrivateUsage]

            def reopen_document(path: Path) -> Any:
                with patch.context() as read:
                    read.setattr(Path, "open", unavailable)
                    return _memory_source(path)

            patch.setattr(preparation, "_memory_source", reopen_document)
        else:
            patch.setattr(Path, "open", unavailable)
        with pytest.raises(LifecycleError) as error:
            collect_batch("001", store=store, spec=spec)
    assert reads == ["rb"]
    assert error.value.category == "execution"
    assert error.value.message == "durable run integrity or storage operation failed"
    assert isinstance(error.value.__context__, PersistenceError)
    if boundary == "minimal-source":
        from judgmentllm.preparation import PreparationError

        assert type(error.value.__context__) is PreparationError
    assert error.value.__context__.args == (message,)
    assert service.events() == events
    assert_accepted_chain_unchanged(store, before)


def test_reconstruction_store_open_failure_preserves_accepted_work(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pathlib import Path

    from judgmentllm import preparation
    from judgmentllm.lifecycle_models import LifecycleError
    from judgmentllm.persistence.models import PersistenceError
    from judgmentllm.persistence.repository import Repository

    service, spec, store = setup(tmp_path, monkeypatch, responses=1)
    before = accepted_chain_snapshot(store)
    events = service.events()
    root = store.resolve()
    mkdir = Path.mkdir
    attempts: list[Path] = []

    def unavailable(path: Path, *args: Any, **kwargs: Any) -> None:
        if path == root:
            attempts.append(path)
            raise OSError("synthetic preparation store unavailable")
        mkdir(path, *args, **kwargs)

    def open_repository(path: Path) -> Repository:
        with monkeypatch.context() as access:
            access.setattr(Path, "mkdir", unavailable)
            return Repository(path)

    with monkeypatch.context() as patch:
        patch.setattr(preparation, "Repository", open_repository)
        with pytest.raises(LifecycleError) as error:
            collect_batch("001", store=store, spec=spec)
    assert attempts == [root]
    assert error.value.category == "execution"
    assert error.value.message == "durable run integrity or storage operation failed"
    context = error.value.__context__
    assert type(context) is PersistenceError
    assert context.args == ("OpenAI Batch source reconstruction is unavailable",)
    assert service.events() == events
    assert_accepted_chain_unchanged(store, before)
