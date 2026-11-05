"""A genuine baseline-produced store remains immutable across a2 to a3 reuse."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

import pytest

import judgmentllm
import judgmentllm.preparation as preparation
from judgmentllm import (
    LifecycleError,
    MockExecutionOptions,
    RetrySelection,
    export_run_results,
    get_run,
    list_runs,
    resume_run,
)
from scripts.version_compatibility_fixture import (
    BASELINE,
    digest,
    evidence,
    extract_fixture,
)

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def old_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, dict[str, Any]]:
    root = tmp_path / "baseline"
    archive = extract_fixture(
        FIXTURES / "a2-compatibility.tar.gz", FIXTURES / "a2-compatibility.json", root
    )
    assert archive["producer"]["commit"] == BASELINE
    assert archive["producer"]["package_version"] == "0.1.0a2"
    assert archive["producer"]["producer_helper_sha256"]
    # During staged development only the consumer is a future-version stand-in.
    # After the package bump the actual a3 loader executes without patching.
    if judgmentllm.__version__ == "0.1.0a2":
        monkeypatch.setattr(preparation, "__version__", "0.1.0a3")
    else:
        assert judgmentllm.__version__ == "0.1.0a3"
    return root, archive


def test_actual_a2_state_can_be_inspected_retried_and_exported(
    old_store: tuple[Path, dict[str, Any]],
) -> None:
    root, archive = old_store
    run_id = archive["producer"]["run_id"]
    store = root / "store"
    before = evidence(root)
    manifest = next(store.glob("runs/*/manifest.json"))
    original_manifest = manifest.read_bytes()
    assert json.loads(original_manifest)["package_version"] == "0.1.0a2"
    assert digest(manifest) == archive["producer"]["manifest_sha256"]
    assert get_run(run_id, store=store).failed_tasks == 1
    assert list_runs(store=store).runs[0].run_id == run_id
    assert get_run(manifest.parent, store=store).run_id == run_id
    snapshots: list[
        tuple[Literal["jsonl", "csv"], Literal["canonical", "r"], bytes, str]
    ] = []
    views: tuple[tuple[Literal["jsonl", "csv"], Literal["canonical", "r"]], ...] = (
        ("jsonl", "canonical"),
        ("csv", "canonical"),
        ("csv", "r"),
    )
    for format_, view in views:
        report = export_run_results(run_id, store=store, format=format_, view=view)
        path = Path(report.files[0].path)
        snapshots.append((format_, view, path.read_bytes(), report.export_id))
        assert report.row_count == 3
    failed = [row for row in before["results"] if row["status"] == "invalid"]
    assert len(failed) == 1
    fixture = root / "valid.json"
    fixture.write_text(
        '{"schema_version":1,"scenario":{"probabilities":true,"category_index":0}}',
        encoding="utf-8",
    )
    options = MockExecutionOptions(
        ledger_path=root / "ledger.sqlite3", fixture_path=fixture
    )
    selection = RetrySelection(
        task_ids=(failed[0]["task_id"],), operation_id="a2-to-a3-retry"
    )
    resumed = resume_run(
        run_id, store=store, use_frozen_snapshot=True, mock=options, retry=selection
    )
    assert resumed.completed_tasks == 3 and resumed.results == 4
    assert resumed.unsuccessful_results == 1
    after = evidence(root)
    assert after["submissions"][: len(before["submissions"])] == before["submissions"]
    assert after["attempts"][: len(before["attempts"])] == before["attempts"]
    assert after["results"][:3] == before["results"]
    assert after["acceptances"][:1] == before["acceptances"]
    assert len(after["submissions"]) == len(after["acceptances"]) == 2
    assert all(row["acceptance_count"] == 1 for row in after["acceptances"])
    assert len(after["attempt_tasks"]) == 4
    assert after["attempt_tasks"][-1]["task_id"] == failed[0]["task_id"]
    for record in after["results"]:
        assert (
            json.loads(record["record_json"])["manifest_sha256"]
            == archive["producer"]["manifest_sha256"]
        )
    for format_, view, raw, export_id in snapshots:
        report = export_run_results(
            run_id, store=store, format=format_, view=view, high_water_mark=3
        )
        assert (
            report.export_id == export_id
            and Path(report.files[0].path).read_bytes() == raw
        )
    resume_run(
        run_id, store=store, use_frozen_snapshot=True, mock=options, retry=selection
    )
    assert evidence(root)["acceptances"] == after["acceptances"]
    assert manifest.read_bytes() == original_manifest


@pytest.mark.parametrize(
    "producer,consumer,allowed",
    [
        ("0.1.0a2", "0.1.0a2", True),
        ("0.1.0a2", "0.1.0a3", True),
        ("0.1.0a3", "0.1.0a3", True),
        ("0.1.0a3", "0.1.0a2", False),
        ("0.1.0a1", "0.1.0a3", False),
        ("0.1.0a4", "0.1.0a3", False),
        ("0.1.0a2", "0.1.0a4", False),
        ("0.1.0a4", "0.1.0a4", False),
        ("unknown", "0.1.0a3", False),
    ],
)
def test_qualified_versions_are_directional(
    producer: str, consumer: str, allowed: bool
) -> None:
    assert preparation._compatible_package_version(producer, consumer) is allowed  # pyright: ignore[reportPrivateUsage]


def test_unknown_consumer_cannot_read_old_state(
    old_store: tuple[Path, dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, archive = old_store
    monkeypatch.setattr(preparation, "__version__", "0.1.0a4")
    with pytest.raises(LifecycleError):
        get_run(archive["producer"]["run_id"], store=root / "store")


@pytest.mark.parametrize("damage", ["manifest", "snapshot", "ledger", "context"])
def test_old_version_does_not_bypass_evidence_validation(
    old_store: tuple[Path, dict[str, Any]], damage: str
) -> None:
    root, archive = old_store
    run_id = archive["producer"]["run_id"]
    store = root / "store"
    manifest = next(store.glob("runs/*/manifest.json"))
    before = evidence(root)
    spec = None
    if damage == "manifest":
        manifest.write_bytes(manifest.read_bytes() + b" ")
    elif damage == "snapshot":
        value = json.loads(manifest.read_text(encoding="utf-8"))
        source = value["sources"][0]["snapshot"]["relative_path"]
        (manifest.parent / source).write_bytes(b"changed synthetic source")
    elif damage == "ledger":
        (root / "ledger.sqlite3").rename(root / "missing-ledger.sqlite3")
    else:
        with preparation.open_prepared_run(
            run_id, store=store, use_frozen_snapshot=True
        ) as prepared:
            spec = prepared.spec.model_copy(update={"model": "changed-synthetic-model"})
    with pytest.raises(LifecycleError):
        resume_run(
            run_id,
            store=store,
            spec=spec,
            use_frozen_snapshot=True,
            mock=MockExecutionOptions(
                ledger_path=root / "ledger.sqlite3", fixture_path=root / "fixture.json"
            ),
        )
    if damage == "ledger":
        assert not (root / "ledger.sqlite3").exists()
        (root / "missing-ledger.sqlite3").rename(root / "ledger.sqlite3")
    assert evidence(root)["acceptances"] == before["acceptances"]


def test_archive_checksum_mismatch_is_rejected(tmp_path: Path) -> None:
    corrupt = tmp_path / "corrupt.tar.gz"
    corrupt.write_bytes(
        (FIXTURES / "a2-compatibility.tar.gz").read_bytes() + b"changed"
    )
    with pytest.raises(ValueError, match="checksum"):
        extract_fixture(corrupt, FIXTURES / "a2-compatibility.json", tmp_path / "store")
