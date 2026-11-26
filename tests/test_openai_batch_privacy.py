"""Untrusted screening, immutable evidence and independent source retrieval."""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest
from test_batch_exports_retry import file_path
from test_openai_batch_recovery import setup

from judgmentllm import collect_batch, export_batch_provenance
from judgmentllm.persistence.repository import ReadOnlyRepository


def store_bytes(root: Path) -> bytes:
    return b"".join(path.read_bytes() for path in root.rglob("*") if path.is_file())


@pytest.mark.parametrize(
    "case", ["full-approved", "minimal", "escaped-credential", "extra-field"]
)
def test_diagnostic_private_ack_requires_closed_full_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    from types import SimpleNamespace

    import httpx2
    from openai_batch_fixture import Service, install, specification

    from judgmentllm import prepare_batch, submit_batch
    from judgmentllm.batch_models import BatchOperationReceiptV1
    from judgmentllm.persistence.hashing import canonical_bytes
    from judgmentllm.providers import openai_batch
    from judgmentllm.providers.openai_batch_diagnostics import approved_ack

    service = Service(tmp_path / "service.sqlite")
    original = service.request
    transmitted: dict[str, bytes] = {}
    status = "processed"
    dummy_key = status if case == "escaped-credential" else "synthetic-batch-key"

    def wire(request: Any) -> Any:
        response = original(request)
        if request.method != "POST":
            return response
        body = json.loads(response.read())
        if request.url.path == "/v1/batches":
            body["request_counts"] = {"total": 1, "completed": 1, "failed": 0}
            if case == "extra-field":
                body["request_counts"]["description"] = "PROHIBITED_PRIVATE_ACK"
        else:
            body["status"] = status
        raw = ("  " + json.dumps(body) + "\n").encode()
        if request.url.path == "/v1/files":
            raw = raw.replace(b'"processed"', rb'"\u0070rocessed"')
            assert json.loads(raw)["status"] == status and status.encode() not in raw
        transmitted[request.url.path] = raw
        return httpx2.Response(
            200, content=raw, headers={"x-request-id": f"req_{dummy_key}"}
        )

    monkeypatch.setattr(service, "request", wire)
    install(monkeypatch, service)

    def credentials(_: str) -> Any:
        return SimpleNamespace(_api_key=lambda: dummy_key)

    monkeypatch.setattr(openai_batch, "_resolve_credentials", credentials)
    spec = specification(
        tmp_path, responses=1, capture="minimal" if case == "minimal" else "full"
    )
    store = tmp_path / "store"
    prepare_batch(spec, store=store, run_id="001")
    service.configure(store, spec)
    assert submit_batch("001", store=store, spec=spec).batch.accepted_jobs == 1
    with ReadOnlyRepository(store) as repo:
        run = repo.run_directory("001")
        (submission,) = repo.connection.execute(
            "SELECT certainty,remote_id FROM submissions"
        )
        assert tuple(submission) == ("accepted", "batch_1")
        receipts = {
            kind: (instance, digest, intent, record)
            for kind, instance, digest, intent, record in repo.connection.execute(
                "SELECT o.kind,o.instance_id,f.receipt_sha256,o.intent_sha256,f.record_json FROM batch_operations o "
                "JOIN batch_facts f ON f.entity_id=o.instance_id AND f.kind='operation'"
            )
        }
    audit = export_batch_provenance("001", store=store)
    controls = file_path(audit, "controls.jsonl").read_bytes()
    assert dummy_key.encode() not in controls
    objects = [
        file_path(audit, record.name).read_bytes() for record in audit.manifest.files
    ]
    for kind, target, remote in (
        ("upload", "/v1/files", "file-input_1"),
        ("create", "/v1/batches", "batch_1"),
    ):
        instance, receipt, intent, record = receipts[kind]
        expected = canonical_bytes(
            BatchOperationReceiptV1(
                instance_id=instance,
                intent_sha256=intent,
                outcome="accepted",
                remote_id=remote,
            )
        )
        assert record.encode() == expected and sha256(expected).hexdigest() == receipt
        assert (
            run / "batch" / "operations" / f"{instance}.json"
        ).read_bytes() == expected
        reference = run / "batch" / "openai-ack" / instance / f"{receipt}.json"
        raw = transmitted[target]
        digest = sha256(raw).hexdigest()
        assert all(
            digest.encode() not in artifact and raw not in artifact
            for artifact in objects
        )
        if case == "full-approved" or (case, kind) in (
            ("extra-field", "upload"),
            ("escaped-credential", "create"),
        ):
            assert reference.read_bytes() == raw
        else:
            assert not reference.exists()
            assert not (run / "objects" / digest[:2] / digest).exists()
    saved = store_bytes(store)
    assert dummy_key.encode() not in saved
    if case == "escaped-credential":
        upload = transmitted["/v1/files"]
        body = json.loads(upload)
        assert approved_ack(upload, body, "upload", spec.model, "synthetic-batch-key")
        assert not approved_ack(upload, body, "upload", spec.model, dummy_key)
        assert rb"\u0070rocessed" not in saved
    assert b"PROHIBITED_PRIVATE_ACK" not in saved
    assert service.events() == [("POST", "/v1/files"), ("POST", "/v1/batches")]


def test_diagnostic_result_fields_remain_attributed_and_screened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openai_batch_fixture import Service, diagnostic_records, install, specification

    from judgmentllm import poll_batch, prepare_batch, submit_batch

    service = Service(tmp_path / "service.sqlite")
    original = service.request
    spec = specification(tmp_path, capture="minimal", responses=2)
    store = tmp_path / "store"

    def wire(request: Any) -> Any:
        response = original(request)
        if request.method == "GET":
            response.headers["x-request-id"] = "req_transport"
        return response

    monkeypatch.setattr(service, "request", wire)
    install(monkeypatch, service)
    prepare_batch(spec, store=store, run_id="001")
    service.configure(store, spec)
    submit_batch("001", store=store, spec=spec)
    with service.connect() as db:
        job = json.loads(db.execute("SELECT body FROM jobs").fetchone()[0])
        job["status"] = "failed"
        job["errors"] = {
            "data": [{"code": "invalid_json_line"}, {"code": "batch_expired"}]
        }
        db.execute("UPDATE jobs SET body=?", (json.dumps(job),))
    rows = [json.loads(line) for line in service.file("file-output_1")[0].splitlines()]
    for row, identifier in zip(
        rows, ["req_member", "req_synthetic-batch-key"], strict=True
    ):
        row["response"] = dict(
            status_code=429,
            request_id=identifier,
            body={
                "error": {"code": "batch_expired", "message": "PROHIBITED_MEMBER_BODY"}
            },
        )
    raw = b"".join(json.dumps(row).encode() + b"\n" for row in rows)
    service.file("file-output_1", body=raw, declared=len(raw))
    poll_batch("001", store=store)
    collect_batch("001", store=store, spec=spec)
    _, failure = diagnostic_records(store, "job_get")
    assert (failure["outcome"], failure["provider_code"]) == ("provider_rejected", None)
    (initial,) = diagnostic_records(store, "file_metadata_initial")
    (content,) = diagnostic_records(store, "file_content")
    (final,) = diagnostic_records(store, "file_metadata_final")
    assert initial["binding_id"] != content["binding_id"] == final["binding_id"]
    assert len({initial["call_id"], content["call_id"], final["call_id"]}) == 3
    assert (
        content["http_request_id"] == "req_transport"
        and content["member_request_id"] is None
    )
    first, second = diagnostic_records(store, "result_row")
    assert [row["member_request_id"] for row in (first, second)] == ["req_member", None]
    for row, source in zip((first, second), rows, strict=True):
        assert row["member_key"] == source["custom_id"]
        assert row["http_status"] is row["http_request_id"] is None
        assert (row["outcome"], row["member_status"], row["provider_code"]) == (
            "provider_rejected",
            429,
            "batch_expired",
        )
    assert b"PROHIBITED_MEMBER_BODY" not in store_bytes(store)
    assert b"synthetic-batch-key" not in store_bytes(store)


@pytest.mark.parametrize("case", ["full", "minimal", "legacy-prefix", "legacy-receipt"])
def test_decoded_result_marker_is_screened_only_for_new_retention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    from judgmentllm import BatchControlOptionsV1
    from judgmentllm.batch_models import BatchMemberReceiptV1
    from judgmentllm.execution import native_batch, openai_batch
    from judgmentllm.execution.openai_batch import OpenAIBatchSession
    from judgmentllm.persistence import hashing
    from judgmentllm.persistence.hashing import canonical_bytes
    from judgmentllm.providers.openai_batch import OpenAIBatchAdapter

    legacy = case.startswith("legacy")
    service, spec, store = setup(
        tmp_path,
        monkeypatch,
        responses=3,
        capture="minimal" if case == "minimal" else "full",
    )
    decoded: list[bytes] = []
    wire: list[bytes] = []
    markers = [b"PROHIBITED_OLD_REPLY", b"PROHIBITED_NEW_REPLY", b"PROHIBITED_NEW_TAIL"]
    for line, marker in zip(
        service.file("file-output_1")[0].splitlines(), markers, strict=True
    ):
        row = json.loads(line)
        # A documented message ID remains shape-valid. Only the wire spelling hides the marker.
        row["response"]["body"]["output"][0]["id"] = "msg_" + marker.decode()
        decoded.append(canonical_bytes(row))
        escaped = decoded[-1].replace(marker, rb"\u0050" + marker[1:])
        assert marker not in escaped
        wire.append(escaped)
    lines = [*wire[:2], wire[0], wire[2]] if legacy else wire
    data = b"\n".join(lines) + b"\n"
    service.file("file-output_1", body=data, declared=len(data))
    posts = [event for event in service.events() if event[0] == "POST"]
    options = BatchControlOptionsV1(max_result_rows=1)

    class Interrupted(BaseException):
        pass

    def interrupt(stage: str, _: str) -> None:
        if stage == "after_batch_member_receipt":
            raise Interrupted

    def receipts() -> list[BatchMemberReceiptV1]:
        with ReadOnlyRepository(store) as repo:
            return [
                BatchMemberReceiptV1.model_validate_json(path.read_bytes())
                for path in (repo.run_directory("001") / "batch" / "members").glob(
                    "*.json"
                )
            ]

    if legacy:
        original_write = OpenAIBatchSession.write_fact

        def previous_write(
            self: OpenAIBatchSession, kind: str, entity: str, record: Any
        ) -> None:
            if not entity.startswith("openai-result-screen:"):
                original_write(self, kind, entity, record)

        with monkeypatch.context() as old:
            old.setattr(
                OpenAIBatchAdapter,
                "screen_result",
                OpenAIBatchAdapter.verify_result_prefix,
            )
            old.setattr(OpenAIBatchSession, "write_fact", previous_write)
            if case == "legacy-receipt":
                with pytest.raises(Interrupted):
                    collect_batch(
                        "001", store=store, spec=spec, options=options, _fault=interrupt
                    )
            else:
                collect_batch("001", store=store, spec=spec, options=options)
        (prior,) = receipts()
        assert (
            prior.disposition == "exact"
            and prior.retained.sha256 == sha256(decoded[0]).hexdigest()
        )
        if case == "legacy-receipt":

            def interrupt_at_cut(
                self: OpenAIBatchSession, kind: str, entity: str, record: Any
            ) -> None:
                original_write(self, kind, entity, record)
                if entity.startswith("openai-result-screen:"):
                    assert record.legacy_positions == 1
                    raise Interrupted

            with monkeypatch.context() as transition:
                transition.setattr(OpenAIBatchSession, "write_fact", interrupt_at_cut)
                with pytest.raises(Interrupted):
                    for _ in range(3):
                        collect_batch("001", store=store, spec=spec, options=options)

    original_digest = hashing.bytes_digest
    new_unsafe = set(wire[int(legacy) :] + decoded[int(legacy) :])

    def screened_digest(data: bytes) -> str:
        assert data not in new_unsafe
        return original_digest(data)

    for module in (hashing, native_batch, openai_batch):
        monkeypatch.setattr(module, "bytes_digest", screened_digest)
    result = None
    for _ in range(6):
        result = collect_batch("001", store=store, spec=spec, options=options)
        if not result.batch.recovery_required:
            break
    assert result is not None and not result.batch.recovery_required
    assert result.batch.conflicts == 0
    with ReadOnlyRepository(store) as repo:
        assert repo.summary("001").successful_results == 3
    rows = sorted(receipts(), key=lambda item: item.source_position)
    assert len(rows) == 3
    saved = store_bytes(store)
    for i, receipt in enumerate(rows):
        if i == 0 and legacy:
            assert receipt.retained.sha256 == sha256(decoded[0]).hexdigest()
            assert receipt.disposition == "exact"
            continue
        assert receipt.disposition == "redacted" and receipt.original is None
        assert (receipt.retained.relative_path is None) == (case == "minimal")
        assert markers[i] not in saved
        assert sha256(wire[i]).hexdigest().encode() not in saved
        assert sha256(decoded[i]).hexdigest().encode() not in saved
    assert [event for event in service.events() if event[0] == "POST"] == posts


@pytest.mark.parametrize("capture", ["full", "minimal"])
def test_decoded_envelope_metadata_never_becomes_exact_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capture: str
) -> None:
    from judgmentllm.execution import native_batch, openai_batch
    from judgmentllm.persistence import hashing
    from judgmentllm.persistence.hashing import canonical_bytes
    from judgmentllm.providers.openai_batch_response import unsafe

    service, spec, store = setup(
        tmp_path, monkeypatch, endpoint="chat.completions", capture=capture
    )
    row, clean = map(json.loads, service.file("file-output_1")[0].splitlines())
    marker = b"offline-envelope-placeholder"
    # The optional fingerprint is part of the exact envelope, but not the derivative.
    row["response"]["body"]["system_fingerprint"] = "Bearer\t" + marker.decode()
    clean["response"]["body"]["system_fingerprint"] = "fp_offline_fixture"
    raw, canonical = json.dumps(row).encode(), canonical_bytes(row)
    assert raw != canonical and not unsafe(raw) and not unsafe(canonical)
    assert json.loads(raw)["response"]["body"]["system_fingerprint"] == (
        "Bearer\t" + marker.decode()
    )
    data = raw + b"\n" + canonical_bytes(clean) + b"\n"
    service.file("file-output_1", body=data, declared=len(data))
    digest = hashing.bytes_digest

    def screened_digest(value: bytes) -> str:
        assert marker not in value
        return digest(value)

    for module in (hashing, native_batch, openai_batch):
        monkeypatch.setattr(module, "bytes_digest", screened_digest)

    assert collect_batch("001", store=store, spec=spec).batch.missing_members == 0
    with ReadOnlyRepository(store) as repo:
        assert repo.summary("001").successful_results == 2
        receipts = {
            key: json.loads(record)
            for key, record in repo.connection.execute(
                "SELECT member_key,record_json FROM batch_member_receipts"
            )
        }
    derivative = receipts[row["custom_id"]]
    assert derivative["disposition"] == "redacted" and derivative["original"] is None
    assert (derivative["retained"]["relative_path"] is None) == (capture == "minimal")
    assert receipts[clean["custom_id"]]["disposition"] == "exact"
    export_batch_provenance("001", store=store)
    saved = store_bytes(store)
    assert marker not in saved
    for representation in (raw, canonical):
        assert representation not in saved
        assert sha256(representation).hexdigest().encode() not in saved


@pytest.mark.parametrize(
    "case",
    [
        "full",
        "minimal",
        "legacy-committed",
        "legacy-interrupted",
        "full-tab",
        "minimal-newline",
        "full-unicode-line",
    ],
)
def test_decoded_assessment_is_screened_before_new_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    from openai_batch_fixture import Service, install, specification

    from judgmentllm import (
        BatchControlOptionsV1,
        poll_batch,
        prepare_batch,
        submit_batch,
    )
    from judgmentllm.execution import native_batch, openai_batch
    from judgmentllm.persistence import hashing
    from judgmentllm.persistence.hashing import canonical_bytes
    from judgmentllm.providers.openai_batch import OpenAIBatchAdapter
    from judgmentllm.providers.openai_batch_response import unsafe
    from judgmentllm.results import CategoricalOutput

    service = Service(tmp_path / "service.sqlite")
    install(monkeypatch, service)
    spec = specification(
        tmp_path,
        responses=2,
        explanations=True,
        capture="minimal" if case.startswith("minimal") else "full",
    )
    store = tmp_path / "store"
    prepare_batch(spec, store=store, run_id="001")
    service.configure(store, spec)
    submit_batch("001", store=store, spec=spec)
    poll_batch("001", store=store)
    first, clean = service.file("file-output_1")[0].splitlines()
    row = json.loads(first)
    part = row["response"]["body"]["output"][0]["content"][0]
    output = json.loads(part["text"])
    assert output["scores"][0]["explanation"].startswith("Synthetic")
    separator = {
        "full-tab": "\t",
        "minimal-newline": "\n",
        "full-unicode-line": "\u2028",
    }.get(case)
    fragment = (
        b"ROHIBITED_DECODED_ASSESSMENT" if separator is None else b"offline-placeholder"
    )
    marker = (
        b"P" + fragment
        if separator is None
        else b"Bearer" + separator.encode() + fragment
    )
    output["scores"][0]["explanation"] = marker.decode()
    part["text"] = json.dumps(output)
    if separator is None:
        part["text"] = part["text"].replace(
            marker.decode(), r"\u0050" + fragment.decode()
        )
    else:
        assert not unsafe(canonical_bytes(output))
    validated = CategoricalOutput.model_validate_json(part["text"])
    assert validated.scores[0].explanation == marker.decode()
    wire = canonical_bytes(row)
    assert marker not in wire and not unsafe(wire)
    data = b"\n".join((wire, clean, wire)) + b"\n"
    service.file("file-output_1", body=data, declared=len(data))
    posts = [event for event in service.events() if event[0] == "POST"]
    options = BatchControlOptionsV1(max_result_rows=1)
    legacy = case.startswith("legacy")
    prior = None

    class Interrupted(BaseException):
        pass

    def interrupt(stage: str, _: str) -> None:
        if stage == "after_batch_member_receipt":
            raise Interrupted

    with ReadOnlyRepository(store) as repo:
        reference = (
            repo.run_directory("001") / "batch" / "members" / f"{row['custom_id']}.json"
        )
    if legacy:
        writer = openai_batch.OpenAIBatchSession.write_fact

        def before_cut(self: Any, kind: str, entity: str, record: Any) -> None:
            if not entity.startswith("openai-result-screen:"):
                writer(self, kind, entity, record)

        with monkeypatch.context() as old:
            old.setattr(
                OpenAIBatchAdapter,
                "screen_result",
                OpenAIBatchAdapter.verify_result_prefix,
            )
            old.setattr(openai_batch.OpenAIBatchSession, "write_fact", before_cut)
            if case == "legacy-interrupted":
                with pytest.raises(Interrupted):
                    collect_batch(
                        "001", store=store, spec=spec, options=options, _fault=interrupt
                    )
            else:
                collect_batch("001", store=store, spec=spec, options=options)
        prior = reference.read_bytes()
        with ReadOnlyRepository(store) as repo:
            results = tuple(repo.iter_results("001"))
        if case == "legacy-committed":
            (result,) = results
            assert getattr(result, "explanation", None) == marker.decode()
        else:
            assert not results
    else:
        original_digest = hashing.bytes_digest

        def screened_digest(data: bytes) -> str:
            assert data != wire and fragment not in data
            return original_digest(data)

        for module in (hashing, native_batch, openai_batch):
            monkeypatch.setattr(module, "bytes_digest", screened_digest)

    result = None
    for _ in range(6):
        result = collect_batch("001", store=store, spec=spec, options=options)
        if not result.batch.recovery_required:
            break
    assert result is not None and result.batch.conflicts == 0
    assert result.batch.missing_members == int(case != "legacy-committed")
    with ReadOnlyRepository(store) as repo:
        explanations = [
            getattr(item, "explanation", None) for item in repo.iter_results("001")
        ]
        ids = repo.connection.execute(
            "SELECT member_key FROM batch_member_receipts"
        ).fetchall()
    assert len(ids) == 1 + int(case == "legacy-committed")
    assert sum(str(text).startswith("Synthetic") for text in explanations) == 1
    assert explanations.count(marker.decode()) == int(case == "legacy-committed")
    if legacy:
        assert reference.read_bytes() == prior
    else:
        assert not reference.exists()
        saved = store_bytes(store)
        assert fragment not in saved
        assert sha256(wire).hexdigest().encode() not in saved
    assert [event for event in service.events() if event[0] == "POST"] == posts


@pytest.mark.parametrize("capture", ["full", "minimal"])
def test_untrusted_derivative_and_forbidden_hashes_never_reach_store(
    tmp_path: Path, monkeypatch: Any, capture: str
) -> None:
    service, spec, store = setup(tmp_path, monkeypatch, capture=capture)
    rows = service.file("file-output_1")[0].splitlines()
    row = json.loads(rows[0])
    row["api_key"] = "sk-forbidden-synthetic-1234"
    row["response"]["body"]["metadata"] = {"raw_marker": "PROHIBITED_PRIVATE_CONTROL"}
    raw = json.dumps(row).encode()
    service.file(
        "file-output_1",
        body=raw + b"\n" + rows[1] + b"\n",
        declared=len(raw) + len(rows[1]) + 2,
    )
    import batch_privacy_audit
    from batch_privacy_audit import PrivacyAudit

    from judgmentllm.execution import native_batch, openai_batch
    from judgmentllm.persistence import hashing

    audit = PrivacyAudit(store)
    audit.originals.append(raw)
    monkeypatch.setattr(
        batch_privacy_audit,
        "MARKERS",
        (
            *batch_privacy_audit.MARKERS,
            b"PROHIBITED_PRIVATE_CONTROL",
            b"sk-forbidden-synthetic-1234",
        ),
    )
    digest = hashing.bytes_digest

    def screened_digest(data: bytes) -> Any:
        assert data != raw and b"PROHIBITED_PRIVATE_CONTROL" not in data
        assert b"sk-forbidden-synthetic-1234" not in data
        return digest(data)

    for module in (hashing, native_batch, openai_batch):
        monkeypatch.setattr(module, "bytes_digest", screened_digest)
    result = audit.checked(
        lambda: collect_batch(
            "001",
            store=store,
            spec=spec,
            _fault=audit.inspect,
        )
    )
    assert result.batch.missing_members == 0
    report = export_batch_provenance("001", store=store)
    retained = store_bytes(store)
    for prohibited in (
        b"sk-forbidden-synthetic-1234",
        b"PROHIBITED_PRIVATE_CONTROL",
        sha256(raw).hexdigest().encode(),
        sha256(json.dumps(row, sort_keys=True, separators=(",", ":")).encode())
        .hexdigest()
        .encode(),
    ):
        assert prohibited not in retained
    with ReadOnlyRepository(store) as repo:
        receipts = [
            json.loads(r[0])
            for r in repo.connection.execute(
                "SELECT record_json FROM batch_member_receipts"
            )
        ]
        filtered = next(r for r in receipts if r["disposition"] == "redacted")
        assert filtered["original"] is None
        assert (filtered["retained"]["relative_path"] is None) == (capture == "minimal")
    assert report.manifest.schema_version == 2


@pytest.mark.parametrize("capture", ["full", "minimal"])
@pytest.mark.parametrize("where", ["upload_id", "result_model"])
def test_current_synthetic_key_is_screened_before_control_or_result_durability(
    tmp_path: Path, monkeypatch: Any, capture: str, where: str
) -> None:
    import httpx2
    from openai_batch_fixture import Service, install, specification

    from judgmentllm import poll_batch, prepare_batch, submit_batch

    service = Service(tmp_path / "service.sqlite")
    install(monkeypatch, service)
    spec = specification(tmp_path, capture=capture)
    store = tmp_path / "store"
    prepare_batch(spec, store=store, run_id="001")
    service.configure(store, spec)
    if where == "upload_id":
        request = service.request

        def changed(req: Any) -> Any:
            response = request(req)
            if req.method == "POST" and req.url.path == "/v1/files":
                body = json.loads(response.read())
                body["id"] = "file-synthetic-batch-key"
                return httpx2.Response(200, json=body)
            return response

        monkeypatch.setattr(service, "request", changed)
        assert submit_batch("001", store=store, spec=spec).outcome == "partial"
    else:
        submit_batch("001", store=store, spec=spec)
        poll_batch("001", store=store)
        rows = [
            json.loads(line) for line in service.file("file-output_1")[0].splitlines()
        ]
        rows[0]["response"]["body"]["model"] = "synthetic-batch-key"
        raw = b"".join(json.dumps(row).encode() + b"\n" for row in rows)
        service.file("file-output_1", body=raw, declared=len(raw))
        assert collect_batch("001", store=store, spec=spec).batch.missing_members == 1
    retained = store_bytes(store)
    assert b"synthetic-batch-key" not in retained
    with ReadOnlyRepository(store) as repo:
        assert repo.summary("001").successful_results == int(where == "result_model")
        if where == "upload_id":
            assert [
                (row[0], json.loads(row[1])["code"])
                for row in repo.connection.execute(
                    "SELECT entity_id,record_json FROM batch_facts WHERE kind='withheld'"
                )
            ] == [("001", "unsafe_evidence")]
            assert [event for event in service.events() if event[0] == "POST"] == [
                ("POST", "/v1/files")
            ]


@pytest.mark.parametrize("capture", ["full", "minimal"])
@pytest.mark.parametrize("prior_chunk", [False, True])
def test_overrun_rejected_before_any_row_from_bad_chunk(
    tmp_path: Path, monkeypatch: Any, capture: str, prior_chunk: bool
) -> None:
    service, spec, store = setup(tmp_path, monkeypatch, capture=capture)
    rows = service.file("file-output_1")[0].splitlines(keepends=True)
    service.file("file-output_1", declared=len(rows[0]))
    if prior_chunk:
        service.set("fragment_bytes", len(rows[0]))
    result = collect_batch("001", store=store, spec=spec)
    assert result.collection is not None
    assert "source_overrun" in result.collection.codes
    with ReadOnlyRepository(store) as repo:
        assert repo.connection.execute(
            "SELECT count(*) FROM batch_member_receipts"
        ).fetchone()[0] == int(prior_chunk)
        assert repo.summary("001").results == int(prior_chunk)
        assert not any(
            json.loads(r[0]).get("exhausted")
            for r in repo.connection.execute(
                "SELECT record_json FROM batch_facts WHERE entity_id NOT LIKE 'openai-%' AND kind='cursor'"
            )
        )


@pytest.mark.parametrize("capture", ["full", "minimal"])
def test_post_eof_size_change_conflicts_without_rewriting_evidence(
    tmp_path: Path, monkeypatch: Any, capture: str
) -> None:
    service, spec, store = setup(tmp_path, monkeypatch, capture=capture)
    original = service.request
    count = 0

    def change(request: Any) -> Any:
        nonlocal count
        if request.url.path == "/v1/files/file-output_1":
            count += 1
            if count == 2:
                _body, size = service.file("file-output_1")
                service.file("file-output_1", declared=size + 1)
        return original(request)

    monkeypatch.setattr(service, "request", change)
    result = collect_batch("001", store=store, spec=spec)
    assert result.batch.missing_members == 0
    assert result.collection is not None
    assert result.outcome == "error" and "source_conflict" in result.collection.codes
    with ReadOnlyRepository(store) as repo:
        assert (
            repo.connection.execute(
                "SELECT count(*) FROM batch_member_receipts"
            ).fetchone()[0]
            == 2
        )
        assert not any(
            json.loads(r[0]).get("exhausted")
            for r in repo.connection.execute(
                "SELECT record_json FROM batch_facts WHERE entity_id NOT LIKE 'openai-%' AND kind='cursor'"
            )
        )


@pytest.mark.parametrize("capture", ["full", "minimal"])
def test_error_only_absent_output_and_duplicate_usage_unknown(
    tmp_path: Path, monkeypatch: Any, capture: str
) -> None:
    # A fresh terminal receipt must identify an absent output independently of B=0.
    from openai_batch_fixture import Service, install, specification

    from judgmentllm import poll_batch, prepare_batch, submit_batch

    service = Service(tmp_path / "service.sqlite")
    install(monkeypatch, service)
    spec = specification(tmp_path, capture=capture)
    store = tmp_path / "store"
    prepare_batch(spec, store=store, run_id="001")
    service.configure(store, spec)
    submit_batch("001", store=store, spec=spec)
    rows = service.file("file-output_1")[0].splitlines()
    errors: list[bytes] = []
    for raw in reversed(rows):
        key = json.loads(raw)["custom_id"]
        errors.append(
            json.dumps(
                {
                    "id": "batch_req_" + key,
                    "custom_id": key,
                    "response": None,
                    "error": {
                        "code": "batch_expired",
                        "message": "PROHIBITED_ERROR_TEXT",
                    },
                }
            ).encode()
            + b"\n"
        )
    data = b"".join(errors + errors)
    service.file("file-error_1", body=data, declared=len(data))
    with service.connect() as db:
        body = json.loads(db.execute("SELECT body FROM jobs").fetchone()[0])
        body["output_file_id"] = None
        db.execute("UPDATE jobs SET body=?", (json.dumps(body),))
    poll_batch("001", store=store)
    result = collect_batch("001", store=store, spec=spec)
    assert not result.batch.recovery_required and result.batch.missing_members == 0
    with ReadOnlyRepository(store) as repo:
        receipts = [
            json.loads(r[0])
            for r in repo.connection.execute(
                "SELECT record_json FROM batch_member_receipts"
            )
        ]
        assert len(receipts) == 2
        assert all(
            r["usage"]["input_tokens"] is None
            and r["usage"]["output_tokens"] is None
            and r["usage"]["total_tokens"] is None
            for r in receipts
        )
        assert {r.status for r in repo.iter_results("001")} == {"errored"}
    assert b"PROHIBITED_ERROR_TEXT" not in store_bytes(store)


@pytest.mark.parametrize("capture", ["full", "minimal"])
def test_old_bundle_and_success_survive_conflicting_error_source(
    tmp_path: Path, monkeypatch: Any, capture: str
) -> None:
    service, spec, store = setup(tmp_path, monkeypatch, capture=capture)
    collect_batch("001", store=store, spec=spec)
    old = export_batch_provenance("001", store=store)
    with ReadOnlyRepository(store) as repo:
        before = repo.connection.execute(
            "SELECT receipt_sha256 FROM batch_member_receipts ORDER BY member_key"
        ).fetchall()
        bundle_files = {
            f.name: file_path(old, f.name).read_bytes() for f in old.manifest.files
        }
    first = json.loads(service.file("file-output_1")[0].splitlines()[0])
    error = (
        json.dumps(
            {
                "custom_id": first["custom_id"],
                "response": None,
                "error": {"code": "batch_expired", "message": "ignored"},
            }
        ).encode()
        + b"\n"
    )
    service.file("file-error_1", body=error, declared=len(error))
    result = collect_batch("001", store=store, spec=spec)
    assert result.batch.conflicts == 1
    reopened = export_batch_provenance(
        "001", store=store, bundle_id=old.manifest.bundle_id
    )
    assert reopened.manifest == old.manifest
    with ReadOnlyRepository(store) as repo:
        assert (
            repo.connection.execute(
                "SELECT receipt_sha256 FROM batch_member_receipts ORDER BY member_key"
            ).fetchall()
            == before
        )
        assert {
            f.name: file_path(old, f.name).read_bytes() for f in old.manifest.files
        } == bundle_files


@pytest.mark.parametrize("endpoint", ["responses", "chat.completions"])
@pytest.mark.parametrize("capture", ["full", "minimal"])
@pytest.mark.parametrize(
    "case",
    ["cancelled", "provider-error", "invalid-usage", "unsafe-id", "screened-wire"],
)
def test_member_wire_errors_and_untrusted_claims_cannot_forge_results_or_usage(
    tmp_path: Path, monkeypatch: Any, endpoint: str, capture: str, case: str
) -> None:
    from judgmentllm.providers.openai_batch_response import ScreenedOpenAIBatchRowV1
    from judgmentllm.results import RubricResult

    service, spec, store = setup(
        tmp_path, monkeypatch, endpoint=endpoint, capture=capture, responses=1
    )
    row = json.loads(service.file("file-output_1")[0])
    if case in ("cancelled", "provider-error"):
        row.update(
            response=None,
            error={
                "code": "batch_cancelled" if case == "cancelled" else "untrusted_code",
                "message": "PROHIBITED_ROW_DETAIL",
            },
        )
    elif case == "invalid-usage":
        field = "input_tokens" if endpoint == "responses" else "prompt_tokens"
        row["response"]["body"]["usage"][field] = True
    elif case == "unsafe-id":
        row["response"]["request_id"] = "PROHIBITED_ROW_DETAIL"
    else:
        with service.connect() as db:
            assessment = db.execute(
                "SELECT text FROM outputs WHERE member=?", (row["custom_id"],)
            ).fetchone()[0]
        row = ScreenedOpenAIBatchRowV1.model_validate(
            {
                "custom_id": row["custom_id"],
                "kind": "response",
                "assessment_text": assessment,
                "usage": {"input_tokens": 999, "output_tokens": 999},
                "reported_model": "synthetic-model",
                "remote_response_id": "resp_forged",
                "provider_outcome": "completed",
            }
        ).model_dump(mode="json")
    raw = json.dumps(row).encode() + b"\n"
    service.file("file-output_1", body=raw, declared=len(raw))
    final = None
    for _ in range(4):
        final = collect_batch("001", store=store, spec=spec)
        if not final.batch.recovery_required:
            break
    assert final is not None
    rejected = case in ("unsafe-id", "screened-wire")
    with ReadOnlyRepository(store) as repo:
        results = tuple(repo.iter_results("001"))
        receipts = [
            json.loads(row[0])
            for row in repo.connection.execute(
                "SELECT record_json FROM batch_member_receipts"
            )
        ]
        if rejected:
            assert not results and not receipts
            assert final.batch.missing_members == 1
            assert repo.connection.execute(
                "SELECT 1 FROM batch_facts WHERE kind='withheld'"
            ).fetchone()
        else:
            assert len(results) == len(receipts) == 1
            assert (
                results[0].status
                == {
                    "cancelled": "cancelled",
                    "provider-error": "errored",
                    "invalid-usage": "complete",
                }[case]
            )
            assert (
                receipts[0]["kind"]
                == {
                    "cancelled": "confirmed_cancellation",
                    "provider-error": "provider_error",
                    "invalid-usage": "response",
                }[case]
            )
            assert all(
                receipts[0]["usage"][field] is None
                for field in ("input_tokens", "output_tokens", "total_tokens")
            )
            if case != "invalid-usage":
                assert isinstance(results[0], RubricResult)
                assert results[0].primary_score is None
        exhausted = [
            json.loads(row[0])["position"]
            for row in repo.connection.execute(
                "SELECT record_json FROM batch_facts WHERE kind='cursor' AND json_extract(record_json,'$.exhausted')=1"
            )
        ]
        assert sorted(exhausted) == [0, 1]
    retained = store_bytes(store)
    assert b"PROHIBITED_ROW_DETAIL" not in retained and b"resp_forged" not in retained
    assert len([event for event in service.events() if event[0] == "POST"]) == 2


@pytest.mark.parametrize("endpoint", ["responses", "chat.completions"])
@pytest.mark.parametrize("capture", ["full", "minimal"])
def test_screened_member_receipt_replays_without_forged_provider_effects(
    tmp_path: Path, monkeypatch: Any, endpoint: str, capture: str
) -> None:
    from judgmentllm.batch_models import BatchMemberReceiptV1
    from judgmentllm.lifecycle_models import LifecycleError
    from judgmentllm.persistence.native_batch import read_reference
    from judgmentllm.persistence.ownership import executor_is_active

    service, spec, store = setup(
        tmp_path, monkeypatch, endpoint=endpoint, capture=capture, responses=1
    )
    row = json.loads(service.file("file-output_1")[0])
    row["api_key"] = "sk-forbidden-synthetic-member"
    raw = json.dumps(row).encode() + b"\n"
    service.file("file-output_1", body=raw, declared=len(raw))

    def unavailable(stage: str, _identity: str) -> None:
        if stage == "after_batch_member_receipt":
            raise OSError("synthetic member projection unavailable")

    with pytest.raises(LifecycleError) as error:
        collect_batch("001", store=store, spec=spec, _fault=unavailable)
    assert error.value.category == "execution"
    reference = "batch/members/" + row["custom_id"] + ".json"
    with ReadOnlyRepository(store) as repo:
        receipt = read_reference(repo, "001", reference, BatchMemberReceiptV1)
        assert receipt is not None and receipt.disposition == "redacted"
        assert receipt.original is None and receipt.kind == "response"
        assert repo.summary("001").results == 0
        assert not repo.connection.execute(
            "SELECT 1 FROM batch_member_receipts"
        ).fetchone()
        assert not executor_is_active(repo, "001")
    service.set("stall_content", "file-output_1")
    for _ in range(2):
        collect_batch("001", store=store, spec=spec)
    with ReadOnlyRepository(store) as repo:
        assert repo.summary("001").successful_results == int(capture == "full")
        assert read_reference(repo, "001", reference, BatchMemberReceiptV1) == receipt
    service.set("stall_content", None)
    final = None
    for _ in range(4):
        final = collect_batch("001", store=store, spec=spec)
        if not final.batch.recovery_required:
            break
    assert final is not None and not final.batch.recovery_required
    with ReadOnlyRepository(store) as repo:
        summary = repo.summary("001")
        assert summary.attempts == summary.successful_results == 1
        assert read_reference(repo, "001", reference, BatchMemberReceiptV1) == receipt
        records = [
            json.loads(row[0])
            for row in repo.connection.execute(
                "SELECT record_json FROM batch_member_receipts"
            )
        ]
        assert len(records) == 1
        assert records[0]["identity"] == receipt.identity.model_dump(mode="json")
        assert records[0]["usage"]["input_tokens"] == 20
        assert records[0]["usage"]["output_tokens"] == 10
        assert not executor_is_active(repo, "001")
    assert b"sk-forbidden-synthetic-member" not in store_bytes(store)
    assert len([event for event in service.events() if event[0] == "POST"]) == 2


@pytest.mark.parametrize(
    "case",
    [
        "unknown-member",
        "top-field",
        "response-field",
        "noninteger-status",
        "response-and-error",
        "error-field",
    ],
)
def test_wire_shape_rejection_cannot_publish_member(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    from judgmentllm.batch_models import (
        BatchCursorV1,
        BatchDiagnosticV1,
        OpenAIBatchSourceBindingV1,
    )
    from judgmentllm.persistence.hashing import semantic_digest
    from judgmentllm.persistence.native_batch import latest
    from judgmentllm.persistence.ownership import executor_is_active

    service, spec, store = setup(tmp_path, monkeypatch, responses=1)
    with ReadOnlyRepository(store) as repo:
        identities = [
            tuple(row)
            for row in repo.connection.execute(
                "SELECT submission_id,remote_id FROM submissions ORDER BY submission_id"
            )
        ]
    row = json.loads(service.file("file-output_1")[0])
    # This allowed wire field alone is redacted, so it cannot mask a shape defect.
    row["api_key"] = "PROHIBITED_WIRE_SHAPE"
    if case == "unknown-member":
        row["custom_id"] = "unknown-member"
    elif case == "top-field":
        row["extra"] = "synthetic"
    elif case == "response-field":
        row["response"]["extra"] = "synthetic"
    elif case == "noninteger-status":
        row["response"]["status_code"] = True
    elif case == "response-and-error":
        row["error"] = {"code": "batch_cancelled", "message": "synthetic"}
    else:
        assert case == "error-field"
        row["response"] = None
        row["error"] = {
            "code": "batch_cancelled",
            "message": "synthetic",
            "extra": "synthetic",
        }
    raw = json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
    service.file("file-output_1", body=raw, declared=len(raw))
    posts = [event for event in service.events() if event[0] == "POST"]
    final = None
    exhausted = []
    for _ in range(4):
        final = collect_batch("001", store=store, spec=spec)
        with ReadOnlyRepository(store) as repo:
            exhausted = [
                json.loads(record[0])["position"]
                for record in repo.connection.execute(
                    "SELECT record_json FROM batch_facts WHERE kind='cursor' "
                    "AND json_extract(record_json,'$.exhausted')=1"
                )
            ]
        if sorted(exhausted) == [0, 1]:
            break
    assert final is not None and final.batch.missing_members == 1
    assert sorted(exhausted) == [0, 1]
    with ReadOnlyRepository(store) as repo:
        assert repo.summary("001").results == 0
        assert not tuple(repo.iter_results("001"))
        assert (
            repo.connection.execute(
                "SELECT count(*) FROM batch_member_receipts"
            ).fetchone()[0]
            == 0
        )
        bindings = [
            OpenAIBatchSourceBindingV1.model_validate_json(record[0])
            for record in repo.connection.execute(
                "SELECT record_json FROM batch_facts "
                "WHERE entity_id LIKE 'openai-binding:%'"
            )
        ]
        assert len(bindings) == 2
        assert {binding.file_id for binding in bindings} == {
            "file-output_1",
            "file-error_1",
        }
        for binding in bindings:
            source = binding.ready.source_id
            cursor = latest(repo, "001", "cursor", source, BatchCursorV1)
            assert cursor is not None and cursor.exhausted
            assert cursor.submission_id == binding.ready.submission_id
            prefix = semantic_digest("openai-batch-prefix-v1", [source])
            if binding.ready.role == "output":
                digest = semantic_digest("batch-withheld-v1", [source, 0])
                prefix = semantic_digest(
                    "batch-result-prefix-step-v1", [prefix, 0, digest]
                )
                assert cursor.position == 1
                diagnostic = latest(
                    repo, "001", "withheld", source + ":0", BatchDiagnosticV1
                )
                assert diagnostic is not None and diagnostic.code == "unsafe_evidence"
            else:
                assert binding.ready.role == "error" and cursor.position == 0
            assert cursor.prefix_sha256 == prefix
        assert [
            tuple(record)
            for record in repo.connection.execute(
                "SELECT submission_id,remote_id FROM submissions ORDER BY submission_id"
            )
        ] == identities
        assert not executor_is_active(repo, "001")
    assert b"PROHIBITED_WIRE_SHAPE" not in store_bytes(store)
    assert [event for event in service.events() if event[0] == "POST"] == posts


@pytest.mark.parametrize(
    "case", ["absent-identifiers", "escaped-surrogate", "unsafe-model"]
)
def test_response_interpretation_preserves_only_screened_member_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    from judgmentllm.persistence.artifacts import ArtifactStore
    from judgmentllm.persistence.ownership import executor_is_active

    service, spec, store = setup(tmp_path, monkeypatch, responses=1)
    row = json.loads(service.file("file-output_1")[0])
    if case == "absent-identifiers":
        del row["response"]["request_id"]
        row["response"]["body"]["model"] = None
    elif case == "escaped-surrogate":
        row["response"]["body"]["output"][0]["content"][0]["text"] = "\ud800"
    else:
        row["response"]["body"]["model"] = "PROHIBITED_RESPONSE_MODEL"
    # The surrogate travels through JSON decoding and the real extractor.
    raw = json.dumps(row, ensure_ascii=True, separators=(",", ":")).encode() + b"\n"
    if case == "escaped-surrogate":
        assert b"\\ud800" in raw
    service.file("file-output_1", body=raw, declared=len(raw))
    posts = [event for event in service.events() if event[0] == "POST"]
    exhausted = []
    for _ in range(4):
        collect_batch("001", store=store, spec=spec)
        with ReadOnlyRepository(store) as repo:
            exhausted = [
                json.loads(record[0])["position"]
                for record in repo.connection.execute(
                    "SELECT record_json FROM batch_facts WHERE kind='cursor' "
                    "AND json_extract(record_json,'$.exhausted')=1"
                )
            ]
        if sorted(exhausted) == [0, 1]:
            break
    assert sorted(exhausted) == [0, 1]
    with ReadOnlyRepository(store) as repo:
        results = tuple(repo.iter_results("001"))
        members = [
            record[0]
            for record in repo.connection.execute(
                "SELECT record_json FROM batch_member_receipts"
            )
        ]
        if case == "absent-identifiers":
            assert len(results) == len(members) == 1
            assert results[0].status == "complete"
            retained = ArtifactStore(repo.run_directory("001"), create=False)
            from judgmentllm.batch_models import BatchMemberReceiptV1

            receipt = BatchMemberReceiptV1.model_validate_json(members[0])
            saved = json.loads(retained.read(receipt.retained))
            if saved.get("record_type") == "screened_openai_batch_row_v1":
                assert saved["reported_model"] is None
            else:
                assert saved["response"]["body"]["model"] is None
                assert "request_id" not in saved["response"]
        else:
            assert not results and not members
            assert (
                repo.connection.execute(
                    "SELECT 1 FROM batch_facts WHERE kind='withheld' "
                    "AND json_extract(record_json,'$.code')='unsafe_evidence'"
                ).fetchone()
                is not None
            )
        assert not executor_is_active(repo, "001")
    assert b"PROHIBITED_RESPONSE_MODEL" not in store_bytes(store)
    assert [event for event in service.events() if event[0] == "POST"] == posts


@pytest.mark.parametrize("case", ["unowned-replay", "kind-disagreement"])
def test_screened_response_requires_member_and_envelope_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    from test_openai_batch_recovery import (
        accepted_chain_snapshot,
        assert_accepted_chain_unchanged,
    )

    from judgmentllm.openai_batch_transport import batch_context
    from judgmentllm.preparation import open_prepared_run
    from judgmentllm.preparation.batch import members_for, reconstruct
    from judgmentllm.providers.openai_batch import OpenAIBatchAdapter

    service, spec, store = setup(tmp_path, monkeypatch, responses=1)
    raw = service.file("file-output_1")[0].splitlines()[0]
    adapter = OpenAIBatchAdapter(batch_context(spec))
    before = accepted_chain_snapshot(store)
    events = service.events()
    with open_prepared_run(
        "001", store=store, spec=spec, capabilities=adapter.capabilities(spec)
    ) as prepared:
        shard = prepared.repository.connection.execute(
            "SELECT shard_id FROM batch_preparations"
        ).fetchone()[0]
        member = next(members_for(prepared, shard))
        wire = reconstruct(prepared, adapter, member)[1]
        observed = adapter.screen_result(raw, {member.member_key: wire})
        assert observed is not None and observed[0] == member.member_key
        response = observed[1]
        assert response.kind == "response" and adapter.parse(response) is not None
        if case == "unowned-replay":
            # The screened representation is genuine; the receiving scope is empty.
            assert adapter.screen_replay(response.raw, {}) is None
        else:
            inconsistent = response.model_copy(update={"kind": "provider_error"})
            assert inconsistent.raw == response.raw
            with pytest.raises(
                ValueError, match="OpenAI Batch assessment identity differs"
            ):
                adapter.parse(inconsistent)
    assert service.events() == events
    assert_accepted_chain_unchanged(store, before)
