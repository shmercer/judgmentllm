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
