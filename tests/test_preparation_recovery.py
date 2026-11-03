"""Preparation interruption, immutable recovery, and process ownership."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Literal, cast

import pytest

import judgmentllm.preparation as preparation
from judgmentllm.persistence.artifacts import ArtifactStore
from judgmentllm.persistence.hashing import bytes_digest
from judgmentllm.persistence.models import (
    ArtifactIdentity,
    IntegrityError,
    OwnershipError,
)
from judgmentllm.persistence.repository import Repository
from judgmentllm.preparation.freezing import PreparationError


def fixture_spec(tmp_path: Path, capture: Literal["full", "minimal"] = "full") -> Path:
    (tmp_path / "responses.csv").write_bytes(
        b'item_id,text\r\n001,"first synthetic writing"\r\n002,"second synthetic writing"\r\n'
    )
    data = {
        "task_kind": "rubric",
        "input": "responses.csv",
        "capture": capture,
        "provider": "fixture",
        "endpoint": "text",
        "model": "test",
        "rubric": {
            "rubric_id": "001",
            "name": "synthetic",
            "traits": [
                {
                    "trait_id": "001",
                    "name": "quality",
                    "description": "criterion",
                    "levels": [{"label": "low"}, {"label": "high"}],
                }
            ],
        },
    }
    config = tmp_path / "run.json"
    config.write_text(json.dumps(data), encoding="utf-8")
    return config


def interrupt_at(monkeypatch: pytest.MonkeyPatch, stage: str) -> None:
    def checkpoint(current: str, source_id: str | None = None) -> None:
        if current == stage:
            raise KeyboardInterrupt()

    monkeypatch.setattr(preparation, "_checkpoint", checkpoint)


@pytest.mark.parametrize("capture", ["full", "minimal"])
@pytest.mark.parametrize(
    "stage",
    [
        "source_copied",
        "record_indexed",
        "index_verified",
        "all_sources_verified",
        "snapshot_published",
    ],
)
def test_incomplete_scan_requires_new_run_and_never_seals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capture: Literal["full", "minimal"],
    stage: str,
) -> None:
    config = fixture_spec(tmp_path, capture)
    store = tmp_path / "runtime"
    interrupt_at(monkeypatch, stage)
    with pytest.raises(KeyboardInterrupt):
        preparation.prepare_run(config, store=store, run_id="001")
    monkeypatch.undo()
    with Repository(store) as repository:
        run = repository.get_run("001")
        assert run.state == "created" and run.manifest_sha256 is None
        assert repository.get_owner("001") is None
        directory = repository.run_directory("001")
        assert list(directory.rglob(".pending-*")) == []
        assert not (directory / "manifest.json").exists()
    with pytest.raises(PreparationError):
        preparation.prepare_run(config, store=store, run_id="001")
    with pytest.raises(PreparationError):
        preparation.open_prepared_run("001", store=store)
    with preparation.prepare_run(config, store=store, run_id="002") as recovered:
        assert recovered.get_response("001").text == "first synthetic writing"
    with Repository(store) as repository:
        assert repository.get_run("001").manifest_sha256 is None


@pytest.mark.parametrize("capture", ["full", "minimal"])
@pytest.mark.parametrize("stage", ["manifest_published", "before_seal"])
def test_verified_fixed_manifest_can_finish_interrupted_sealing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capture: Literal["full", "minimal"],
    stage: str,
) -> None:
    config = fixture_spec(tmp_path, capture)
    store = tmp_path / "runtime"
    interrupt_at(monkeypatch, stage)
    with pytest.raises(KeyboardInterrupt):
        preparation.prepare_run(config, store=store, run_id="001")
    monkeypatch.undo()
    with Repository(store) as repository:
        run = repository.get_run("001")
        assert run.state == "created" and run.manifest_sha256 is None
        fixed = repository.run_directory("001") / "manifest.json"
        original_manifest = fixed.read_bytes()
    with preparation.open_prepared_run("001", store=store) as recovered:
        assert recovered.manifest_sha256 == bytes_digest(original_manifest)
        assert recovered.get_response("002").text == "second synthetic writing"
    assert fixed.read_bytes() == original_manifest
    with preparation.open_prepared_run("001", store=store) as reopened:
        assert reopened.manifest_sha256 == bytes_digest(original_manifest)
    with Repository(store) as repository:
        assert repository.get_run("001").state == "submitted"
        assert (
            sum(event.kind == "manifest_sealed" for event in repository.events("001"))
            == 1
        )


def test_interruption_after_seal_does_not_replace_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = fixture_spec(tmp_path)
    store = tmp_path / "runtime"
    interrupt_at(monkeypatch, "manifest_sealed")
    with pytest.raises(KeyboardInterrupt):
        preparation.prepare_run(config, store=store, run_id="001")
    monkeypatch.undo()
    with Repository(store) as repository:
        manifest_hash = repository.get_run("001").manifest_sha256
        assert manifest_hash is not None
    with preparation.open_prepared_run("001", store=store) as reopened:
        assert reopened.manifest_sha256 == manifest_hash
    with preparation.open_prepared_run("001", store=store) as repeated:
        assert repeated.manifest_sha256 == manifest_hash
    with pytest.raises(PreparationError):
        preparation.prepare_run(config, store=store, run_id="001")
    with Repository(store) as repository:
        assert (
            sum(event.kind == "manifest_sealed" for event in repository.events("001"))
            == 1
        )


@pytest.mark.parametrize("damage", ["original", "snapshot", "fixed_manifest", "index"])
def test_interrupted_seal_recovery_reverifies_every_commitment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    damage: str,
) -> None:
    config = fixture_spec(tmp_path)
    store = tmp_path / "runtime"
    interrupt_at(monkeypatch, "before_seal")
    with pytest.raises(KeyboardInterrupt):
        preparation.prepare_run(config, store=store, run_id="001")
    monkeypatch.undo()
    with Repository(store) as repository:
        fixed = repository.run_directory("001") / "manifest.json"
        if damage == "original":
            (tmp_path / "responses.csv").write_bytes(b"modified source")
        elif damage == "snapshot":
            source = next(
                item
                for item in repository.iter_sources("001")
                if item.role == "response"
            )
            assert source.snapshot_sha256 is not None
            identity = repository.get_artifact("001", source.snapshot_sha256)
            path = ArtifactStore(repository.run_directory("001")).verify(identity)
            assert path is not None
            path.write_bytes(b"corrupted snapshot")
        elif damage == "fixed_manifest":
            # Replacement preserves the original retained object's bytes, leaving
            # a fixed/reference mismatch rather than merely corrupting both links.
            replacement = fixed.with_suffix(".replacement")
            replacement.write_bytes(b'{"kind":"conflicting"}')
            replacement.replace(fixed)
        else:
            # A created source row may still be edited at SQL level. Recovery
            # must compare it against the canonical manifest before sealing.
            repository.connection.execute(
                "UPDATE sources SET byte_length=byte_length+1 WHERE run_id='001' AND role='response'"
            )
        unchanged_fixed = fixed.read_bytes()
    with pytest.raises(IntegrityError):
        preparation.open_prepared_run("001", store=store)
    assert fixed.read_bytes() == unchanged_fixed
    with Repository(store) as repository:
        assert repository.get_run("001").manifest_sha256 is None


def test_full_snapshot_authorization_is_audited_and_session_scoped(
    tmp_path: Path,
) -> None:
    config = fixture_spec(tmp_path)
    store = tmp_path / "runtime"
    with preparation.prepare_run(config, store=store, run_id="001") as prepared:
        manifest_hash = prepared.manifest_sha256
    (tmp_path / "responses.csv").unlink()
    with pytest.raises(PreparationError):
        preparation.open_prepared_run("001", store=store)
    with preparation.open_prepared_run(
        "001", store=store, use_frozen_snapshot=True
    ) as recovered:
        assert recovered.manifest_sha256 == manifest_hash
        assert recovered.get_response("001").text == "first synthetic writing"
    with Repository(store) as repository:
        identities = list(repository.iter_artifacts("001"))
        events = list(repository.events("001"))
        artifacts = ArtifactStore(repository.run_directory("001"))
        authorization: list[tuple[ArtifactIdentity, dict[str, Any]]] = []
        for identity in identities:
            if (
                identity.relative_path is not None
                and identity.media_type == "application/json"
            ):
                data = cast(dict[str, Any], json.loads(artifacts.read(identity)))
                if data.get("kind") == "frozen_snapshot_authorization":
                    authorization.append((identity, data))
        assert len(authorization) == 1
        identity, decision = authorization[0]
        assert decision["manifest_sha256"] == manifest_hash
        assert decision["explicit_caller_decision"] is True
        assert decision["scope"] == "this_prepared_run_session"
        assert any(
            event.kind == "artifact_registered" and event.entity_id == identity.sha256
            for event in events
        )
    with pytest.raises(PreparationError):
        preparation.open_prepared_run("001", store=store)


def test_minimal_capture_cannot_authorize_missing_originals(tmp_path: Path) -> None:
    config = fixture_spec(tmp_path, "minimal")
    store = tmp_path / "runtime"
    preparation.prepare_run(config, store=store, run_id="001").close()
    (tmp_path / "responses.csv").unlink()
    with pytest.raises(PreparationError):
        preparation.open_prepared_run("001", store=store, use_frozen_snapshot=True)


def test_sealed_manifest_rejects_replacement_configuration(tmp_path: Path) -> None:
    config = fixture_spec(tmp_path)
    store = tmp_path / "runtime"
    preparation.prepare_run(config, store=store, run_id="001").close()
    data = json.loads(config.read_text(encoding="utf-8"))
    data["rubric"]["traits"][0]["description"] = "changed criterion"
    config.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(PreparationError):
        preparation.prepare_run(config, store=store, run_id="001")
    with preparation.open_prepared_run(
        "001", store=store, use_frozen_snapshot=True
    ) as original:
        assert original.spec.rubric is not None
        assert original.spec.rubric.traits[0].description == "criterion"


def test_separate_process_owner_blocks_preparation_while_readers_work(
    tmp_path: Path,
) -> None:
    config = fixture_spec(tmp_path)
    store = tmp_path / "runtime"
    ready = tmp_path / "ready"
    script = """
import pathlib, sys
import judgmentllm.preparation as preparation
config, store, ready = sys.argv[1:]
paused = False
def checkpoint(stage, source_id=None):
    global paused
    if stage == 'source_open' and not paused:
        paused = True
        pathlib.Path(ready).write_text('ready')
        sys.stdin.readline()
preparation._checkpoint = checkpoint
with preparation.prepare_run(config, store=store, run_id='001') as prepared:
    assert prepared.get_response('001').item_id == '001'
"""
    child = subprocess.Popen(
        [sys.executable, "-c", script, str(config), str(store), str(ready)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while (
            not ready.exists() and child.poll() is None and time.monotonic() < deadline
        ):
            time.sleep(0.02)
        assert ready.exists(), child.communicate(timeout=2)
        with Repository(store) as repository:
            assert repository.get_run("001").state == "created"
            assert repository.get_owner("001") is not None
            assert repository.summary("001").tasks == 0
        with pytest.raises((PreparationError, OwnershipError)):
            preparation.prepare_run(config, store=store, run_id="001")
        with pytest.raises((PreparationError, OwnershipError)):
            preparation.open_prepared_run("001", store=store)
        stdout, stderr = child.communicate("\n", timeout=20)
        assert child.returncode == 0, (stdout, stderr)
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=5)
    with preparation.open_prepared_run("001", store=store) as prepared:
        assert prepared.get_response("002").item_id == "002"


@pytest.mark.parametrize(
    "stage",
    ["record_indexed", "snapshot_published", "manifest_published", "manifest_sealed"],
)
def test_process_death_releases_lock_and_never_promotes_partial_staging(
    tmp_path: Path, stage: str
) -> None:
    config = fixture_spec(tmp_path)
    store = tmp_path / "runtime"
    script = """
import os, sys
import judgmentllm.preparation as preparation
config, store, crash_stage = sys.argv[1:]
def checkpoint(stage, source_id=None):
    if stage == crash_stage:
        os._exit(77)
preparation._checkpoint = checkpoint
preparation.prepare_run(config, store=store, run_id='001')
"""
    outcome = subprocess.run(
        [sys.executable, "-c", script, str(config), str(store), stage],
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert outcome.returncode == 77, (outcome.stdout, outcome.stderr)
    with Repository(store) as repository:
        original = repository.get_run("001")
        assert repository.get_owner("001") is not None
        directory = repository.run_directory("001")
        orphans = {
            path.name: path.read_bytes() for path in directory.glob(".pending-*")
        }
    if stage in ("manifest_published", "manifest_sealed"):
        with preparation.open_prepared_run("001", store=store) as recovered:
            assert recovered.get_response("001").item_id == "001"
        with Repository(store) as repository:
            assert repository.get_run("001").manifest_sha256 is not None
            if stage == "manifest_published":
                assert any(
                    event.kind == "owner_recovered"
                    for event in repository.events("001")
                )
    else:
        assert original.manifest_sha256 is None
        with pytest.raises(PreparationError):
            preparation.open_prepared_run("001", store=store)
        with preparation.prepare_run(config, store=store, run_id="002") as separate:
            assert separate.get_response("001").item_id == "001"
    # Inert orphan files are neither deleted under a different operation's
    # ownership nor treated as references that could authorize a source read.
    assert {
        path.name: path.read_bytes() for path in directory.glob(".pending-*")
    } == orphans


@pytest.mark.parametrize("capture", ["full", "minimal"])
@pytest.mark.parametrize("changed", ["configuration", "response"])
def test_active_session_blocks_further_work_after_original_source_change(
    tmp_path: Path,
    capture: Literal["full", "minimal"],
    changed: str,
) -> None:
    config = fixture_spec(tmp_path, capture)
    store = tmp_path / "runtime"
    with preparation.prepare_run(config, store=store, run_id="001") as prepared:
        original = next(prepared.iter_request_dependencies())
        assert original.request.tasks[0].item_id == "001"
        if changed == "configuration":
            data = json.loads(config.read_text(encoding="utf-8"))
            data["rubric"]["traits"][0]["description"] = "altered criterion"
            config.write_text(json.dumps(data), encoding="utf-8")
        else:
            (tmp_path / "responses.csv").write_bytes(
                b"item_id,text\n001,altered\n002,changed\n"
            )
        with pytest.raises(PreparationError):
            next(prepared.iter_request_dependencies())
