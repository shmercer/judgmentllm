"""Actual base-produced schema-3 bytes retain their original reader contract."""

import base64
import io
import json
import tarfile
from pathlib import Path
from typing import Any, Literal, get_args

import pytest
from pydantic import BaseModel
from test_native_batch import batch_spec

from judgmentllm import export_run_results, get_run, prepare_batch
from judgmentllm.config import RunSpecV2, validate_run_spec_json
from judgmentllm.lifecycle_models import ErrorCategory, LifecycleError
from judgmentllm.persistence.artifacts import ArtifactStore
from judgmentllm.persistence.hashing import bytes_digest, canonical_bytes
from judgmentllm.persistence.repository import ReadOnlyRepository
from judgmentllm.preparation import open_prepared_run
from judgmentllm.preparation.manifest import parse_executable_manifest


def schema3_failure_note(
    root: Path, run_id: str, raw: bytes, error: LifecycleError
) -> str:
    fields: list[dict[str, str]] = []
    report: dict[str, Any] = {
        "phase": "manifest_parse",
        "category": error.category
        if error.category in get_args(ErrorCategory)
        else None,
        "fields": fields,
        "omitted_fields": 0,
    }

    def differences(model: BaseModel, original: dict[str, Any], prefix: str) -> None:
        encoded = model.model_dump(mode="json")
        for name in type(model).model_fields:
            if encoded.get(name) == original.get(name) and (name in encoded) == (
                name in original
            ):
                continue
            value = getattr(model, name)
            supplied = original.get(name)
            if isinstance(value, BaseModel) and isinstance(supplied, dict):
                nested: dict[str, Any] = original[name]
                differences(value, nested, prefix + "." + name)
            elif len(fields) < 16:
                fields.append(
                    {"field": prefix + "." + name, "type": type(value).__name__}
                )
            else:
                report["omitted_fields"] += 1

    def compare(phase: str, original: bytes, model: BaseModel) -> bool:
        report["phase"] = phase
        encoded = canonical_bytes(model)
        report[phase] = {
            "raw_length": len(original),
            "canonical_length": len(encoded),
            "raw_sha256": bytes_digest(original),
            "canonical_sha256": bytes_digest(encoded),
            "equal": original == encoded,
        }
        differences(model, json.loads(original), phase)
        return original == encoded

    try:
        manifest = parse_executable_manifest(raw)
        if compare("manifest_canonical", raw, manifest):
            report["phase"] = "manifest_object_and_seal"
            with ReadOnlyRepository(root) as repository:
                store = ArtifactStore(repository.run_directory(run_id), create=False)
                identity = repository.get_artifact(run_id, bytes_digest(raw))
                report["manifest_object_matches"] = store.read(identity) == raw
                report["manifest_seal_matches"] = (
                    repository.get_manifest(run_id).artifact == identity
                )
                if (
                    report["manifest_object_matches"]
                    and report["manifest_seal_matches"]
                ):
                    report["phase"] = "specification_parse"
                    specification = store.read(manifest.specification)
                    spec = validate_run_spec_json(
                        specification.decode("utf-8"),
                        _cache_policy_version=manifest.cache_policy_version,
                    )
                    report["specification_v2"] = isinstance(spec, RunSpecV2)
                    compare("specification_canonical", specification, spec)
    except BaseException:
        report["diagnostic_failed"] = True
    note = json.dumps(report, sort_keys=True, separators=(",", ":"))
    while len(note.encode("utf-8")) > 4096 and fields:
        fields.pop()
        report["omitted_fields"] += 1
        note = json.dumps(report, sort_keys=True, separators=(",", ":"))
    assert len(note.encode("utf-8")) <= 4096
    return note


def test_genuine_schema3_stays_byte_compatible_and_cannot_accept_batch(
    tmp_path: Path,
) -> None:
    fixture = Path(__file__).parent / "fixtures" / "batch-history"
    metadata = json.loads((fixture / "schema3-store.json").read_text())
    raw = base64.b64decode(
        (fixture / "schema3-store.tar.gz.base64").read_bytes(), validate=True
    )
    assert bytes_digest(raw) == metadata["archive_sha256"]
    assert metadata["producer_commit"] == "49623bad468d41909d7032f87a010cb495cac663"
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
        archive.extractall(tmp_path, filter="data")
    root, run_id = tmp_path / "store", metadata["run_id"]
    with ReadOnlyRepository(root) as repository:
        manifest = repository.get_manifest(run_id)
        sql = [
            tuple(r)
            for r in repository.connection.execute(
                "SELECT type,name,sql FROM sqlite_master ORDER BY type,name"
            )
        ]
        migrations = [
            tuple(r)
            for r in repository.connection.execute("SELECT * FROM schema_migrations")
        ]
        original = (repository.run_directory(run_id) / "manifest.json").read_bytes()
        assert repository.connection.execute("PRAGMA user_version").fetchone()[0] == 3
    try:
        assert get_run(run_id, store=root).batch is None
    except LifecycleError as error:
        try:
            error.add_note(schema3_failure_note(root, run_id, original, error))
        except BaseException:
            pass  # Diagnostics must never replace the original public failure.
        raise
    with open_prepared_run(run_id, store=root, use_frozen_snapshot=True) as prepared:
        assert prepared.manifest.schema_version == 2
    combinations: tuple[
        tuple[Literal["jsonl", "csv"], Literal["canonical", "r"]], ...
    ] = (("jsonl", "canonical"), ("csv", "canonical"), ("csv", "r"))
    for format_, view in combinations:
        exported = export_run_results(run_id, store=root, format=format_, view=view)
        assert exported.row_count == 0
    with pytest.raises(LifecycleError, match="integrity"):
        prepare_batch(batch_spec(tmp_path / "new"), store=root, run_id="new")
    assert not (root / "mock-batch-service.sqlite3").exists()
    with ReadOnlyRepository(root) as repository:
        assert [
            tuple(r)
            for r in repository.connection.execute(
                "SELECT type,name,sql FROM sqlite_master ORDER BY type,name"
            )
        ] == sql
        assert [
            tuple(r)
            for r in repository.connection.execute("SELECT * FROM schema_migrations")
        ] == migrations
        assert repository.get_manifest(run_id) == manifest
        assert (
            repository.run_directory(run_id) / "manifest.json"
        ).read_bytes() == original
        assert (
            repository.connection.execute("SELECT count(*) FROM runs").fetchone()[0]
            == 1
        )
