"""Actual base-produced schema-3 bytes retain their original reader contract."""

import base64
import io
import json
import tarfile
from pathlib import Path
from typing import Literal

import pytest
from test_native_batch import batch_spec

from judgmentllm import export_run_results, get_run, prepare_batch
from judgmentllm.lifecycle_models import LifecycleError
from judgmentllm.persistence.hashing import bytes_digest
from judgmentllm.persistence.repository import ReadOnlyRepository
from judgmentllm.preparation import open_prepared_run


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
    assert get_run(run_id, store=root).batch is None
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
