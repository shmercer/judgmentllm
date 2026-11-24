"""Fresh-process duplicate safety, exact source size and durable fair selection."""

from __future__ import annotations

import json
from typing import Any

import pytest
from openai_batch_fixture import Service, child, install, specification

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
