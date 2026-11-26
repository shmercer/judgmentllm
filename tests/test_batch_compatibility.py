"""Base-produced preparations and native Batch archives keep their wire identity."""

import asyncio
import base64
import io
import json
import sqlite3
import tarfile
from contextlib import ExitStack
from functools import partial
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import SimpleNamespace
from typing import Any, Literal, get_args

import pytest
from pydantic import BaseModel
from test_native_batch import batch_spec
from test_version_compatibility import old_store as old_store

from judgmentllm import (
    BatchMockOptions,
    MockExecutionOptions,
    RetrySelection,
    cancel_batch,
    export_batch_provenance,
    export_run_provenance,
    export_run_results,
    get_batch_status,
    get_run,
    list_runs,
    prepare_batch,
    resume_batch,
    resume_run,
    stop_batch,
)
from judgmentllm.config import PromptSpec, RunSpec, RunSpecV2, validate_run_spec_json
from judgmentllm.execution.batch_exports import BatchAuditManifestV1, BatchAuditReportV1
from judgmentllm.lifecycle import cancel_run
from judgmentllm.lifecycle_models import ErrorCategory, LifecycleError
from judgmentllm.persistence.artifacts import ArtifactStore
from judgmentllm.persistence.hashing import (
    bytes_digest,
    canonical_bytes,
    semantic_digest,
)
from judgmentllm.persistence.models import IntegrityError, NotFoundError, RunRecord
from judgmentllm.persistence.ownership import ExecutorOwnership
from judgmentllm.persistence.repository import ReadOnlyRepository, Repository
from judgmentllm.preparation import open_prepared_run
from judgmentllm.preparation.manifest import (
    ExecutableRunManifestV2,
    ExecutableRunManifestV12,
    parse_executable_manifest,
)
from scripts.version_compatibility_fixture import evidence

HistoricalCopy = tuple[Path, str, ExecutableRunManifestV2, bytes, bytes]


@pytest.fixture
def historical_copy(tmp_path: Path) -> HistoricalCopy:
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
        assert repository.connection.execute("PRAGMA user_version").fetchone()[0] == 3
        store = ArtifactStore(repository.run_directory(run_id), create=False)
        raw_manifest = store.read(repository.get_manifest(run_id).artifact)
        manifest = parse_executable_manifest(raw_manifest)
        assert type(manifest) is ExecutableRunManifestV2
        specification = store.read(manifest.specification)
    return root, run_id, manifest, raw_manifest, specification


def test_historical_wire_preserves_native_writer_and_exact_identity(
    historical_copy: HistoricalCopy, monkeypatch: pytest.MonkeyPatch
) -> None:
    import judgmentllm.preparation as preparation
    from judgmentllm.preparation import _restore_context  # pyright: ignore[reportPrivateUsage]
    from judgmentllm.preparation.historical_wire import HistoricalWire

    root, run_id, manifest, raw, specification = historical_copy
    wire = HistoricalWire.checked(manifest, raw, specification)
    restored = wire.restore()
    assert wire.manifest.encode(manifest) == raw
    assert wire.specification.encode(restored) == specification
    assert wire.configuration_digest(restored) == manifest.configuration_sha256
    # The public native Path serializer is unchanged by the historical reader.
    assert json.loads(canonical_bytes(restored))["output_path"] == str(
        restored.output_path
    )
    assert bytes_digest(wire.manifest.raw) == bytes_digest(raw)
    # Foreign native spelling still requires implicit restoration of the wire.
    supplied = restored if canonical_bytes(restored) == specification else None
    with Repository(root) as repository:
        current, template = _restore_context(repository, manifest, supplied, wire)
    assert template is None
    assert wire.specification.encode(current) == specification
    assert wire.configuration_digest(current) == manifest.configuration_sha256
    attempts = unavailable_originals(
        monkeypatch, {source.original_location for source in manifest.sources}
    )
    with open_prepared_run(
        run_id, store=root, spec=supplied, use_frozen_snapshot=True
    ) as prepared:
        assert prepared.manifest_sha256 == bytes_digest(raw)
        responses = tuple(prepared.iter_responses())
        assert [response.item_id for response in responses] == ["000", "001"]
        assert [
            dependency.request.response
            for dependency in prepared.iter_request_dependencies()
        ] == list(responses)
        prepared.verify_compatibility()
    (authorization,) = preparation.iter_snapshot_authorizations(run_id, store=root)
    assert authorization.manifest_sha256 == bytes_digest(raw)
    assert {d.source_id for d in authorization.discrepancies} == {
        source.source_id for source in manifest.sources
    }
    assert attempts == []


@pytest.fixture
def windows_wire_presentation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise Path presentation only; this does not run native Windows."""
    import judgmentllm.preparation.historical_wire as wire_module

    for model_type, paths in (
        (ExecutableRunManifestV2, (("input", "path"), ("pairs", "path"))),
        (
            RunSpecV2,
            (
                ("input", "path"),
                ("pairs", "path"),
                ("prompt", "file"),
                ("output_path",),
            ),
        ),
    ):
        original = model_type.model_dump

        def windows_dump(
            self: BaseModel,
            *args: Any,
            dump: Any = original,
            leaves: Any = paths,
            **kwargs: Any,
        ) -> dict[str, Any]:
            data: dict[str, Any] = dump(self, *args, **kwargs)
            for path in leaves:
                parent = data
                for key in path[:-1]:
                    if parent.get(key) is None:
                        break
                    parent = parent[key]
                else:
                    if parent.get(path[-1]) is not None:
                        parent[path[-1]] = str(PureWindowsPath(parent[path[-1]]))
            return data

        monkeypatch.setattr(model_type, "model_dump", windows_dump)
    monkeypatch.setattr(wire_module, "Path", PureWindowsPath)


def unavailable_originals(
    monkeypatch: pytest.MonkeyPatch, originals: set[str]
) -> list[str]:
    import judgmentllm.preparation as preparation

    attempts: list[str] = []
    aliases = originals | {str(Path(location)) for location in originals}

    def classified(location: str) -> bool:
        return location in originals

    monkeypatch.setattr(preparation, "foreign_original", classified)
    for method in ("open", "stat", "resolve"):
        original = getattr(Path, method)

        def forbidden(
            self: Path, *args: Any, call: Any = original, **kwargs: Any
        ) -> Any:
            if str(self) in aliases:
                attempts.append(str(self))
                raise AssertionError("foreign source alias was accessed")
            return call(self, *args, **kwargs)

        monkeypatch.setattr(Path, method, forbidden)
    return attempts


@pytest.fixture
def canonical_without_wire(monkeypatch: pytest.MonkeyPatch) -> None:
    from judgmentllm.preparation.historical_wire import HistoricalWire

    def unexpected_wire(*args: object, **kwargs: object) -> None:
        raise AssertionError("canonical manifest entered the special wire proof")

    monkeypatch.setattr(HistoricalWire, "checked", unexpected_wire)


def test_posix_wire_checks_pure_windows_presentation_without_native_execution(
    historical_copy: HistoricalCopy, windows_wire_presentation: None
) -> None:
    import judgmentllm.preparation.historical_wire as wire_module

    _, _, manifest, raw, specification = historical_copy
    wire = wire_module.HistoricalWire.checked(manifest, raw, specification)
    assert canonical_bytes(manifest) != raw
    assert wire.manifest.encode(manifest) == raw
    assert wire.specification.encode(wire.restore()) == specification


def test_canonical_a2_foreign_provenance_uses_existing_snapshot_gate(
    old_store: tuple[Path, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    canonical_without_wire: None,
) -> None:
    root, metadata = old_store
    store, run_id = root / "store", metadata["producer"]["run_id"]
    with ReadOnlyRepository(store) as repository:
        identity = repository.get_manifest(run_id).artifact
        raw = ArtifactStore(repository.run_directory(run_id), create=False).read(
            identity
        )
    manifest = parse_executable_manifest(raw)
    assert canonical_bytes(manifest) == raw
    aliases = unavailable_originals(
        monkeypatch, {s.original_location for s in manifest.sources}
    )
    with pytest.raises(IntegrityError):
        open_prepared_run(run_id, store=store)
    with open_prepared_run(run_id, store=store, use_frozen_snapshot=True) as prepared:
        assert prepared.manifest_sha256 == identity.sha256 == bytes_digest(raw)
        response = next(prepared.iter_responses())
        assert next(prepared.iter_request_dependencies()).request.response == response
    assert aliases == []
    with ReadOnlyRepository(store) as repository:
        assert repository.get_manifest(run_id).artifact == identity
        assert (repository.run_directory(run_id) / "manifest.json").read_bytes() == raw


def test_canonical_a2_foreign_snapshot_authorizes_subset_without_submission(
    old_store: tuple[Path, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    canonical_without_wire: None,
) -> None:
    from judgmentllm.execution.retry import authorize_subset_retry
    from judgmentllm.preparation import iter_snapshot_authorizations
    from judgmentllm.providers import MockAdapter

    root, metadata = old_store
    store, run_id = root / "store", metadata["producer"]["run_id"]
    with ReadOnlyRepository(store) as repository:
        seal = repository.get_manifest(run_id)
        raw = ArtifactStore(repository.run_directory(run_id), create=False).read(
            seal.artifact
        )
        assert repository.get_run(run_id).manifest_sha256 == seal.artifact.sha256
    manifest = parse_executable_manifest(raw)
    assert manifest.schema_version == 1 and canonical_bytes(manifest) == raw
    aliases = unavailable_originals(
        monkeypatch, {s.original_location for s in manifest.sources}
    )
    before = evidence(root)
    failed = [row for row in before["results"] if row["status"] == "invalid"]
    assert len(failed) == 1
    selected = (failed[0]["task_id"],)
    audit_before = {
        a.operation_id for a in iter_snapshot_authorizations(run_id, store=store)
    }
    adapter = MockAdapter(root / "ledger.sqlite3")

    def no_submit(*args: object, **kwargs: object) -> None:
        raise AssertionError("authorization must not submit provider work")

    monkeypatch.setattr(adapter, "submit", no_submit)
    operation_id = "canonical-foreign-a2-authorization"
    attempt = authorize_subset_retry(
        run_id,
        store=store,
        adapter=adapter,
        task_ids=selected,
        operation_id=operation_id,
        use_frozen_snapshot=True,
    )
    assert attempt.state == "prepared" and attempt.attempt_number == 2
    assert attempt.authorization_id == operation_id
    after = evidence(root)
    for table in ("attempts", "attempt_tasks", "retry_operations", "submissions"):
        assert after[table][: len(before[table])] == before[table]
        assert len(after[table]) == len(before[table]) + 1
    for table in ("observations", "results", "submission_failures", "acceptances"):
        assert after[table] == before[table]
    assert after["retry_operations"][-1]["operation_id"] == operation_id
    assert after["attempt_tasks"][-1]["task_id"] == selected[0]
    assert after["attempt_tasks"][-1]["attempt_id"] == attempt.attempt_id
    replay = authorize_subset_retry(
        run_id,
        store=store,
        adapter=adapter,
        task_ids=selected,
        operation_id=operation_id,
        use_frozen_snapshot=True,
    )
    assert replay == attempt
    assert evidence(root) == after
    assert adapter.submit_calls == 0 and aliases == []
    with ReadOnlyRepository(store) as repository:
        assert repository.get_attempt(run_id, attempt.attempt_id) == attempt
        assert repository.attempt_task_ids(run_id, attempt.attempt_id) == selected
        original = repository.get_attempt(run_id, failed[0]["attempt_id"])
        assert attempt.request_uid == original.request_uid
        submission = repository.get_submission_for_attempt(run_id, attempt.attempt_id)
        assert submission is not None and submission.state == "prepared"
        assert submission.certainty == "not_started" and submission.remote_id is None
        assert submission.payload_sha256 == attempt.payload_sha256
        assert repository.get_manifest(run_id) == seal
        assert repository.get_run(run_id).manifest_sha256 == seal.artifact.sha256
        artifacts = ArtifactStore(repository.run_directory(run_id), create=False)
        assert artifacts.read(seal.artifact) == raw
        assert seal.artifact.sha256 == bytes_digest(raw)
        assert (artifacts.root / "manifest.json").read_bytes() == raw
    audit = [
        a
        for a in iter_snapshot_authorizations(run_id, store=store)
        if a.operation_id not in audit_before
    ]
    assert len(audit) >= 2
    expected = tuple(
        (s.source_id, "missing_or_unreadable", s.raw_sha256) for s in manifest.sources
    )
    assert expected
    for authorization in audit:
        assert authorization.run_id == run_id
        assert authorization.manifest_sha256 == seal.artifact.sha256
        assert authorization.explicit_caller_decision is True
        assert authorization.scope == "this_prepared_run_session"
        assert (
            tuple(
                (d.source_id, d.reason, d.expected_sha256)
                for d in authorization.discrepancies
            )
            == expected
        )


def test_canonical_a2_foreign_snapshot_keeps_authorized_retry_and_replay(
    old_store: tuple[Path, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    canonical_without_wire: None,
) -> None:
    root, archive = old_store
    store, run_id = root / "store", archive["producer"]["run_id"]
    with ReadOnlyRepository(store) as repository:
        identity = repository.get_manifest(run_id).artifact
        raw = ArtifactStore(repository.run_directory(run_id), create=False).read(
            identity
        )
    manifest = parse_executable_manifest(raw)
    assert manifest.schema_version == 1 and canonical_bytes(manifest) == raw
    aliases = unavailable_originals(
        monkeypatch, {s.original_location for s in manifest.sources}
    )

    before = evidence(root)
    with pytest.raises(IntegrityError):
        open_prepared_run(run_id, store=store)
    with open_prepared_run(run_id, store=store, use_frozen_snapshot=True) as prepared:
        assert prepared.manifest_sha256 == identity.sha256 == bytes_digest(raw)
        response = next(prepared.iter_responses())
        assert next(prepared.iter_request_dependencies()).request.response == response
        wrong = prepared.spec.model_copy(update={"model": "changed-synthetic-model"})
        prepared.spec.output_path = Path("changed-after-verification")
        with pytest.raises(IntegrityError):
            prepared.verify_compatibility()
    with pytest.raises(IntegrityError):
        open_prepared_run(run_id, store=store, spec=wrong, use_frozen_snapshot=True)
    failed = [r for r in before["results"] if r["status"] == "invalid"]
    assert len(failed) == 1
    fixture = root / "valid.json"
    fixture.write_text(
        '{"schema_version":1,"scenario":{"probabilities":true,"category_index":0}}'
    )
    options = MockExecutionOptions(
        ledger_path=root / "ledger.sqlite3", fixture_path=fixture
    )
    retry = RetrySelection(
        task_ids=(failed[0]["task_id"],), operation_id="canonical-foreign-a2-retry"
    )
    resumed = resume_run(
        run_id, store=store, use_frozen_snapshot=True, mock=options, retry=retry
    )
    assert (resumed.completed_tasks, resumed.results, resumed.unsuccessful_results) == (
        3,
        4,
        1,
    )
    after = evidence(root)
    for table in ("submissions", "attempts", "results", "acceptances"):
        assert after[table][: len(before[table])] == before[table]
    assert len(after["submissions"]) == len(after["acceptances"]) == 2
    assert all(r["acceptance_count"] == 1 for r in after["acceptances"])
    assert after["attempt_tasks"][-1]["task_id"] == failed[0]["task_id"]
    resume_run(run_id, store=store, use_frozen_snapshot=True, mock=options, retry=retry)
    assert evidence(root)["acceptances"] == after["acceptances"]
    assert aliases == []
    with ReadOnlyRepository(store) as repository:
        assert (
            ArtifactStore(repository.run_directory(run_id), create=False).read(identity)
            == raw
        )


def test_canonical_archive_does_not_project_a_damaged_frozen_specification(
    old_store: tuple[Path, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
    canonical_without_wire: None,
) -> None:
    root, metadata = old_store
    store, run_id = root / "store", metadata["producer"]["run_id"]
    with ReadOnlyRepository(store) as repository:
        artifacts = ArtifactStore(repository.run_directory(run_id), create=False)
        raw = artifacts.read(repository.get_manifest(run_id).artifact)
    manifest = parse_executable_manifest(raw)
    assert canonical_bytes(manifest) == raw
    aliases = unavailable_originals(
        monkeypatch, {s.original_location for s in manifest.sources}
    )
    original = evidence(root)
    changed = json.loads(artifacts.read(manifest.specification))
    changed["model"] = "changed-frozen-context"
    assert manifest.specification.relative_path is not None
    (artifacts.root / manifest.specification.relative_path).write_bytes(
        canonical_bytes(changed)
    )
    with pytest.raises(IntegrityError):
        open_prepared_run(run_id, store=store, use_frozen_snapshot=True)
    assert (artifacts.root / "manifest.json").read_bytes() == raw
    assert aliases == []
    after = evidence(root)
    for table in ("attempts", "submissions", "results", "acceptances"):
        assert after[table] == original[table]


def test_foreign_canonical_unsealed_preparation_is_not_recovered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_preparation_recovery import fixture_spec, interrupt_at

    import judgmentllm.preparation as preparation

    config, store = fixture_spec(tmp_path), tmp_path / "store"
    with monkeypatch.context() as interruption:
        interrupt_at(interruption, "before_seal")
        with pytest.raises(KeyboardInterrupt):
            preparation.prepare_run(config, store=store, run_id="001")
    with ReadOnlyRepository(store) as repository:
        assert repository.get_run("001").manifest_sha256 is None
        raw = (repository.run_directory("001") / "manifest.json").read_bytes()
    manifest = parse_executable_manifest(raw)
    assert canonical_bytes(manifest) == raw
    aliases = unavailable_originals(
        monkeypatch, {s.original_location for s in manifest.sources}
    )
    with pytest.raises(IntegrityError, match="foreign originals"):
        open_prepared_run("001", store=store, use_frozen_snapshot=True)
    assert aliases == []
    with ReadOnlyRepository(store) as repository:
        assert repository.get_run("001").manifest_sha256 is None
        assert repository.get_owner("001") is None
        assert not tuple(repository.iter_attempts("001"))


@pytest.mark.parametrize(
    "fault",
    [
        "whitespace",
        "duplicate",
        "unknown",
        "version",
        "path",
        "container",
        "empty-path",
        "nul-path",
        "path-type",
        "nonobject",
    ],
)
def test_historical_wire_rejects_noncanonical_or_uncontrolled_values(
    historical_copy: HistoricalCopy, fault: str
) -> None:
    from judgmentllm.preparation.historical_wire import manifest_wire

    _, _, manifest, raw, _ = historical_copy
    data = json.loads(raw)
    if fault == "whitespace":
        raw += b" "
    elif fault == "duplicate":
        raw = b'{"schema_version":2,' + raw[1:]
    elif fault == "nonobject":
        raw = b"[]"
    else:
        if fault == "unknown":
            data["unknown"] = True
        elif fault == "version":
            data["schema_version"] = 12
        elif fault == "path":
            data["input"]["path"] = "/noncanonical//source.jsonl"
        elif fault == "container":
            data["input"] = "not-an-input-object"
        else:
            data["input"]["path"] = {
                "empty-path": "",
                "nul-path": "/bad\0/path",
                "path-type": 123,
            }[fault]
        raw = canonical_bytes(data)
    with pytest.raises((IntegrityError, ValueError)):
        manifest_wire(manifest, raw)


@pytest.mark.parametrize(
    "fault", ["manifest-path", "manifest-nonpath", "spec-path", "spec-nonpath", "extra"]
)
def test_historical_wire_rechecks_current_model_not_just_original_bytes(
    historical_copy: HistoricalCopy, fault: str
) -> None:
    from judgmentllm.preparation.historical_wire import HistoricalWire

    _, _, manifest, raw, specification = historical_copy
    wire = HistoricalWire.checked(manifest, raw, specification)
    spec = wire.restore()
    if fault == "manifest-path":
        manifest.input.path = Path("/changed/source.jsonl")
    elif fault == "manifest-nonpath":
        manifest = manifest.model_copy(update={"seed": 12345})
    elif fault == "spec-path":
        spec.output_path = Path("changed-output")
    elif fault == "spec-nonpath":
        spec.model = "different-model"
    else:
        spec = spec.model_copy(update={"unknown": "must reject"})
    with pytest.raises((IntegrityError, ValueError)):
        wire.manifest.encode(manifest)
        wire.specification.encode(spec)


def test_historical_wire_rejects_subtypes_and_rechecks_restored_protocol(
    historical_copy: HistoricalCopy, monkeypatch: pytest.MonkeyPatch
) -> None:
    import judgmentllm.preparation.historical_wire as wire_module

    _, _, manifest, raw, specification = historical_copy
    wire = wire_module.HistoricalWire.checked(manifest, raw, specification)

    class DifferentManifest(ExecutableRunManifestV2):
        pass

    with pytest.raises(IntegrityError):
        type(wire.manifest).checked(DifferentManifest.model_validate_json(raw), raw)
    with pytest.raises(IntegrityError):
        wire.manifest.encode(wire.restore())
    data = json.loads(specification)
    data["schema_version"] = 1
    data["prompt"] = {
        k: v for k, v in data["prompt"].items() if k in PromptSpec.model_fields
    }
    legacy = validate_run_spec_json(
        canonical_bytes(data).decode(),
        _cache_policy_version=manifest.cache_policy_version,
    )
    assert isinstance(legacy, RunSpec)
    with pytest.raises(IntegrityError):
        wire_module.HistoricalWire.checked(manifest, raw, canonical_bytes(data))

    def wrong_protocol(text: str, **kwargs: object) -> RunSpec:
        return legacy

    monkeypatch.setattr(wire_module, "validate_run_spec_json", wrong_protocol)
    with pytest.raises(IntegrityError):
        wire.restore()


@pytest.mark.parametrize(
    "mutation", ["manifest-path", "manifest-nonpath", "spec-path", "spec-nonpath"]
)
def test_foreign_snapshot_preserves_proof_without_touching_original_aliases(
    historical_copy: HistoricalCopy,
    monkeypatch: pytest.MonkeyPatch,
    windows_wire_presentation: None,
    mutation: str,
) -> None:
    import judgmentllm.preparation as preparation

    root, run_id, manifest, raw, _ = historical_copy
    attempts = unavailable_originals(
        monkeypatch, {source.original_location for source in manifest.sources}
    )
    assert canonical_bytes(manifest) != raw
    assert get_run(run_id, store=root).batch is None
    with pytest.raises(IntegrityError, match="non-native manifest"):
        open_prepared_run(run_id, store=root)
    with open_prepared_run(run_id, store=root, use_frozen_snapshot=True) as prepared:
        assert prepared.manifest_sha256 == bytes_digest(raw)
        assert tuple(prepared.iter_responses())
        assert tuple(prepared.iter_request_dependencies())
        prepared.verify_compatibility()
        if mutation == "manifest-path":
            prepared.manifest.input.path = Path("/mutated-source.jsonl")
        elif mutation == "manifest-nonpath":
            prepared.manifest = prepared.manifest.model_copy(update={"seed": 54321})
        elif mutation == "spec-path":
            prepared.spec.output_path = Path("mutated-after-warm-verification")
        else:
            prepared.spec.model = "mutated-after-warm-verification"
        with pytest.raises((IntegrityError, ValueError)):
            prepared.verify_compatibility()
    records = list(preparation.iter_snapshot_authorizations(run_id, store=root))
    assert len(records) == 1 and records[0].manifest_sha256 == bytes_digest(raw)
    assert {d.source_id for d in records[0].discrepancies} == {
        s.source_id for s in manifest.sources
    }
    with pytest.raises(IntegrityError, match="non-native manifest"):
        open_prepared_run(run_id, store=root)
    assert attempts == []


def test_historical_manifest_projection_is_manifest_only_and_readonly(
    historical_copy: HistoricalCopy,
    monkeypatch: pytest.MonkeyPatch,
    windows_wire_presentation: None,
) -> None:
    root, run_id, _, raw, _ = historical_copy
    reads: list[str] = []
    read = ArtifactStore.read

    def watched(self: ArtifactStore, identity: Any) -> bytes:
        reads.append(identity.sha256)
        return read(self, identity)

    monkeypatch.setattr(ArtifactStore, "read", watched)
    with ReadOnlyRepository(root) as repository:
        before = repository.connection.execute(
            "SELECT count(*) FROM artifacts"
        ).fetchone()[0]
    assert get_run(run_id, store=root).batch is None
    assert reads and set(reads) == {bytes_digest(raw)}
    with ReadOnlyRepository(root) as repository:
        assert (
            repository.connection.execute("SELECT count(*) FROM artifacts").fetchone()[
                0
            ]
            == before
        )
        assert repository.get_manifest(run_id).artifact.sha256 == bytes_digest(raw)


def test_historical_foreign_dialect_is_classified_without_filesystem_access(
    historical_copy: HistoricalCopy, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    import judgmentllm.preparation.historical_wire as wire_module

    root, _, manifest, raw, specification = historical_copy
    monkeypatch.setattr(wire_module, "os", SimpleNamespace(name="posix"))
    assert wire_module.foreign_original(r"C:\foreign\input.jsonl")
    assert not wire_module.foreign_original("/native/input.jsonl")
    monkeypatch.setattr(wire_module, "os", SimpleNamespace(name="nt"))
    assert wire_module.foreign_original("/foreign/input.jsonl")
    assert not wire_module.foreign_original(r"C:\native\input.jsonl")
    changed = manifest.model_copy(
        update={
            "sources": (
                manifest.sources[0].model_copy(
                    update={"original_location": r"C:\unknown\input.jsonl"}
                ),
                *manifest.sources[1:],
            )
        }
    )
    with pytest.raises(IntegrityError):
        wire_module.manifest_wire(changed, raw)
    for location in (r"/foreign/back\slash.jsonl", "C:relative.jsonl"):
        data = json.loads(raw)
        data["input"]["path"] = location
        encoded = canonical_bytes(data)
        ambiguous = parse_executable_manifest(encoded)
        assert type(ambiguous) is ExecutableRunManifestV2
        with pytest.raises(IntegrityError):
            wire_module.manifest_wire(ambiguous, encoded)
    monkeypatch.setattr(wire_module, "os", SimpleNamespace(name="posix"))
    from judgmentllm.preparation.session import PreparedRun

    spec = validate_run_spec_json(
        specification.decode(), _cache_policy_version=manifest.cache_policy_version
    )
    # Classification alone grants no session permission to use frozen sources.
    with ReadOnlyRepository(root) as repository:
        with PreparedRun(repository, changed, spec, None, None) as prepared:
            with pytest.raises(IntegrityError, match="foreign original"):
                prepared.verify_compatibility()


def test_foreign_supplied_specification_keeps_exact_equality_boundary(
    historical_copy: HistoricalCopy,
    monkeypatch: pytest.MonkeyPatch,
    windows_wire_presentation: None,
) -> None:
    import judgmentllm.preparation as preparation

    root, run_id, manifest, raw, specification = historical_copy

    def unavailable(location: str) -> bool:
        return True

    monkeypatch.setattr(preparation, "foreign_original", unavailable)
    supplied = validate_run_spec_json(
        specification.decode(), _cache_policy_version=manifest.cache_policy_version
    )
    changed = supplied.model_copy(update={"output_path": Path("other-output")})
    with pytest.raises(IntegrityError, match="supplied specification"):
        open_prepared_run(run_id, store=root, spec=changed, use_frozen_snapshot=True)
    if canonical_bytes(supplied) == specification:
        with open_prepared_run(
            run_id, store=root, spec=supplied, use_frozen_snapshot=True
        ) as prepared:
            assert prepared.manifest_sha256 == bytes_digest(raw)
    else:
        with pytest.raises(IntegrityError, match="supplied specification"):
            open_prepared_run(
                run_id, store=root, spec=supplied, use_frozen_snapshot=True
            )


@pytest.mark.parametrize(
    "damage", ["specification", "snapshot", "seal", "minimal", "unsealed"]
)
def test_foreign_historical_scope_and_artifacts_fail_closed(
    historical_copy: HistoricalCopy,
    monkeypatch: pytest.MonkeyPatch,
    windows_wire_presentation: None,
    damage: str,
) -> None:
    import judgmentllm.preparation as preparation
    from judgmentllm.preparation.historical_wire import HistoricalWire, manifest_wire

    root, run_id, manifest, raw, specification = historical_copy

    def classified(location: str) -> bool:
        return True

    monkeypatch.setattr(preparation, "foreign_original", classified)
    if damage == "specification":
        data = json.loads(specification)
        data["output_path"] = "different-output"
        with pytest.raises((IntegrityError, ValueError)):
            HistoricalWire.checked(manifest, raw, canonical_bytes(data))
        return
    if damage == "minimal":
        with pytest.raises((IntegrityError, ValueError)):
            manifest_wire(manifest.model_copy(update={"capture": "minimal"}), raw)
        return
    if damage == "unsealed":
        with ReadOnlyRepository(root) as repository:
            from judgmentllm.preparation import _load_manifest  # pyright: ignore[reportPrivateUsage]

            def mismatched(model: object) -> bytes:
                return b"mismatch"

            monkeypatch.setattr(preparation, "canonical_bytes", mismatched)
            with pytest.raises((IntegrityError, ValueError)):
                _load_manifest(repository, run_id, unsealed=True)
        return
    with ReadOnlyRepository(root) as repository:
        if damage == "snapshot":
            snapshot = manifest.sources[0].snapshot
            assert snapshot is not None and snapshot.relative_path is not None
            (repository.run_directory(run_id) / snapshot.relative_path).write_bytes(
                b"damaged"
            )
    if damage == "seal":
        data = json.loads(raw)
        data["seed"] = 987
        changed = canonical_bytes(data)
        # A registered valid object still cannot replace the old database seal.
        with Repository(root) as repository:
            store = ArtifactStore(repository.run_directory(run_id))
            repository.register_artifact(run_id, store.put(changed, "application/json"))
            (store.root / "manifest.json").write_bytes(changed)
    with pytest.raises((IntegrityError, ValueError)):
        open_prepared_run(run_id, store=root, use_frozen_snapshot=True)


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
        assert (
            prepared.manifest_sha256
            == manifest.artifact.sha256
            == bytes_digest(original)
        )
        response = next(prepared.iter_responses())
        assert next(prepared.iter_request_dependencies()).request.response == response
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


def _pr2_store(
    tmp_path: Path, name: str = "schema4-manifest12-store"
) -> tuple[Path, str, dict[str, Any]]:
    fixture = Path(__file__).parent / "fixtures" / "batch-history"
    metadata = json.loads((fixture / f"{name}.json").read_text())
    raw = base64.b64decode(
        (fixture / f"{name}.tar.gz.base64").read_bytes(), validate=True
    )
    assert bytes_digest(raw) == metadata["archive_sha256"]
    assert metadata["producer_commit"] == "35adfdf1829d1c201a0bcb57dc5cf4292e2d6dd4"
    assert metadata["producer_tree"] == "2e49dfad4a1d2aba33176ad3e29dff074d595797"
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
        archive.extractall(tmp_path, filter="data")
    return tmp_path / "store", metadata["run_id"], metadata


def _v12_location(root: Path, run_id: str) -> Path:
    with ReadOnlyRepository(root) as repository:
        return repository.run_directory(run_id)


def _block_v12_writes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def forbid(boundary: str) -> Any:
        def enter(*args: object, **kwargs: object) -> None:
            calls.append(boundary)
            pytest.fail(f"historical V12 reached {boundary}")

        return enter

    monkeypatch.setattr(Repository, "__init__", forbid("writable repository"))
    monkeypatch.setattr(ExecutorOwnership, "acquire", forbid("executor ownership"))
    return calls


def _durable_v12_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and not path.name.endswith(("-shm", "-wal", ".lock"))
    }


def _v12_rejection(error: LifecycleError) -> BaseException | None:
    cause = error.__context__
    while isinstance(cause, LifecycleError):
        cause = cause.__context__
    return cause


@pytest.fixture
def windows_v12_consumer(
    monkeypatch: pytest.MonkeyPatch, windows_wire_presentation: None
) -> None:
    import judgmentllm.preparation.historical_wire as wire_module

    monkeypatch.setattr(wire_module, "os", SimpleNamespace(name="nt"))


def test_genuine_pr2_schema4_manifest12_audit1_remain_immutable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_batch_exports_retry import file_path

    from judgmentllm import openai_batch_transport
    from judgmentllm.execution import openai_batch as openai_batch_execution
    from judgmentllm.providers import openai_batch as openai_batch_provider

    root, run_id, metadata = _pr2_store(tmp_path)

    def forbidden() -> None:
        raise AssertionError("historical local/mock operations reached OpenAI SDK")

    monkeypatch.setattr(openai_batch_transport, "require_sdk", forbidden)
    monkeypatch.setattr(openai_batch_provider, "require_sdk", forbidden)
    monkeypatch.setattr(openai_batch_execution, "require_sdk", forbidden)
    with ReadOnlyRepository(root) as repo:
        assert repo.connection.execute("PRAGMA user_version").fetchone()[0] == 4
        directory = repo.run_directory(run_id)
        sql = [
            tuple(r)
            for r in repo.connection.execute(
                "SELECT type,name,sql FROM sqlite_master ORDER BY type,name"
            )
        ]
        migrations = [
            tuple(r) for r in repo.connection.execute("SELECT * FROM schema_migrations")
        ]
        original = (directory / "manifest.json").read_bytes()
        manifest = parse_executable_manifest(original)
        assert type(manifest) is ExecutableRunManifestV12
        assert bytes_digest(original) == metadata["manifest_sha256"]
        seal = repo.get_manifest(run_id)
        assert seal.artifact.sha256 == metadata["manifest_sha256"]
        store = ArtifactStore(directory, create=False)
        assert store.read(seal.artifact) == original
        results = tuple(repo.iter_results(run_id))
        assert len(results) == metadata["result_count"] == 2
        assert {r.status for r in results} == {"complete", "cancelled"}
        receipts = [
            r[0]
            for r in repo.connection.execute(
                "SELECT record_json FROM batch_member_receipts ORDER BY member_key"
            )
        ]
        usage = [json.loads(r)["usage"] for r in receipts]
        assert sum(u["input_tokens"] or 0 for u in usage) == 7
        assert sum(u["output_tokens"] or 0 for u in usage) == 3
        bundle_id = metadata["bundle_id"]
        audit_path = directory / "batch" / "audits" / f"{bundle_id}.json"
        audit = audit_path.read_bytes()
        bundle = BatchAuditManifestV1.model_validate_json(audit)
        assert store.read(repo.get_artifact(run_id, bytes_digest(audit))) == audit
        assert bundle.cut.source_manifest_sha256 == seal.artifact.sha256
        assert bundle.bundle_id == bundle_id
        files = {file.name: store.read(file.artifact) for file in bundle.files}
        assert len(files) == len(bundle.files) == 11
    before = _durable_v12_bytes(root)
    with monkeypatch.context() as inspection:
        alias_reads = unavailable_originals(
            inspection, {source.original_location for source in manifest.sources}
        )
        writes = _block_v12_writes(inspection)
        for identifier, selected_store in ((run_id, root), (directory, None)):
            assert get_run(identifier, store=selected_store).batch is not None
            assert not get_batch_status(
                identifier, store=selected_store
            ).batch.recovery_required
            old = export_batch_provenance(
                identifier, store=selected_store, bundle_id=bundle_id
            )
            assert type(old) is BatchAuditReportV1
            assert old.schema_version == old.manifest.schema_version == 1
            assert (
                old.manifest == bundle and Path(old.manifest_path).read_bytes() == audit
            )
            assert {
                f.name: file_path(old, f.name).read_bytes() for f in old.manifest.files
            } == files
        assert alias_reads == writes == []
    assert _durable_v12_bytes(root) == before
    if canonical_bytes(manifest) == original:
        result = resume_batch(
            run_id,
            store=root,
            use_frozen_snapshot=True,
            mock=BatchMockOptions(fixture_path=root / "fixture.json"),
        )
        assert not result.batch.recovery_required
    else:
        # Keep PR2's foreign native-Path rejection; never rewrite historical IDs.
        with pytest.raises(LifecycleError, match="integrity"):
            resume_batch(
                run_id,
                store=root,
                use_frozen_snapshot=True,
                mock=BatchMockOptions(fixture_path=root / "fixture.json"),
            )
    reopened = export_batch_provenance(
        run_id, store=root, bundle_id=metadata["bundle_id"]
    )
    assert Path(reopened.manifest_path).read_bytes() == audit
    assert {
        f.name: file_path(reopened, f.name).read_bytes()
        for f in reopened.manifest.files
    } == files
    with ReadOnlyRepository(root) as repo:
        assert (directory / "manifest.json").read_bytes() == original
        assert repo.get_manifest(run_id) == seal
        assert store.read(seal.artifact) == original
        assert tuple(repo.iter_results(run_id)) == results
        assert [
            r[0]
            for r in repo.connection.execute(
                "SELECT record_json FROM batch_member_receipts ORDER BY member_key"
            )
        ] == receipts
        assert [
            tuple(r)
            for r in repo.connection.execute(
                "SELECT type,name,sql FROM sqlite_master ORDER BY type,name"
            )
        ] == sql
        assert [
            tuple(r) for r in repo.connection.execute("SELECT * FROM schema_migrations")
        ] == migrations


def _edit_v12_row(
    repository: Repository, trigger: str, statement: str, bindings: tuple[object, ...]
) -> None:
    # Isolated corrupt archive copies keep the original schema and checksums.
    ddl = repository.connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (trigger,)
    ).fetchone()[0]
    with repository.transaction():
        repository.connection.execute(f"DROP TRIGGER {trigger}")
        repository.connection.execute(statement, bindings)
        repository.connection.execute(ddl)


def test_genuine_pr2_v12_pairwise_preserves_both_posix_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, windows_v12_consumer: None
) -> None:
    root, run_id, metadata = _pr2_store(tmp_path, "schema4-manifest12-pairwise-store")
    directory = _v12_location(root, run_id)
    fixed = directory / "manifest.json"
    raw = fixed.read_bytes()
    manifest = parse_executable_manifest(raw)
    assert type(manifest) is ExecutableRunManifestV12
    assert manifest.pairs is not None and canonical_bytes(manifest) != raw
    fields = json.loads(raw)
    assert PurePosixPath(fields["input"]["path"]).is_absolute()
    assert PurePosixPath(fields["pairs"]["path"]).is_absolute()
    before = _durable_v12_bytes(root)
    with monkeypatch.context() as inspection:
        aliases = unavailable_originals(
            inspection, {source.original_location for source in manifest.sources}
        )
        writes = _block_v12_writes(inspection)
        assert get_run(run_id, store=root).task_kind == "pairwise"
        assert get_batch_status(directory).run_id == run_id
        with pytest.raises(LifecycleError, match="integrity"):
            list_runs(store=root)
        assert aliases == writes == []
    assert _durable_v12_bytes(root) == before
    with Repository(root) as repository:
        seal = repository.get_manifest(run_id)
        assert seal.artifact.sha256 == bytes_digest(raw) == metadata["manifest_sha256"]
        objects = ArtifactStore(directory, create=False)
        assert objects.read(seal.artifact) == raw
        fields["pairs"]["path"] = str(
            PurePosixPath(fields["pairs"]["path"]).with_name("different-pairs.jsonl")
        )
        changed = canonical_bytes(fields)
        repository.register_artifact(run_id, objects.put(changed, "application/json"))
        fixed.unlink()
        fixed.write_bytes(changed)
    with pytest.raises(LifecycleError, match="integrity") as rejected:
        get_run(directory)
    assert "database seal" in str(rejected.value.__context__)


@pytest.mark.parametrize(
    ("damage", "reason"),
    [
        pytest.param("malformed", "invalid or incomplete", id="malformed"),
        pytest.param("whitespace", "canonical wire differs", id="noncanonical"),
        pytest.param("dialect", "canonical POSIX", id="unsupported-dialect"),
        pytest.param("host", "foreign POSIX originals", id="unsupported-host"),
        pytest.param("route", "V12 mock route", id="unsupported-route"),
        pytest.param("minimal", "identity or encoding", id="minimal"),
        pytest.param("nonpath", "database seal", id="changed-nonpath"),
        pytest.param("object", "missing or unreadable", id="missing-object"),
        pytest.param("seal", "database seal", id="wrong-seal"),
        pytest.param("unsealed", "identity or encoding", id="missing-seal"),
    ],
)
def test_v12_inspection_rejects_invalid_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    windows_v12_consumer: None,
    damage: str,
    reason: str,
) -> None:
    import judgmentllm.preparation.historical_wire as wire_module

    root, run_id, _ = _pr2_store(tmp_path)
    with Repository(root) as repository:
        directory = repository.run_directory(run_id)
        fixed = directory / "manifest.json"
        raw = fixed.read_bytes()
        seal = repository.get_manifest(run_id).artifact
        manifest = parse_executable_manifest(raw)
        assert type(manifest) is ExecutableRunManifestV12
        if damage == "host":
            monkeypatch.setattr(wire_module, "os", SimpleNamespace(name="posix"))
        elif damage == "object":
            assert seal.relative_path is not None
            (directory / seal.relative_path).unlink()
        elif damage in {"seal", "unsealed"}:
            repository.connection.execute("PRAGMA ignore_check_constraints=ON")
            _edit_v12_row(
                repository,
                "runs_manifest_immutable",
                "UPDATE runs SET manifest_sha256=? WHERE run_id=?",
                (manifest.specification.sha256 if damage == "seal" else None, run_id),
            )
            repository.connection.execute("PRAGMA ignore_check_constraints=OFF")
        else:
            fields = json.loads(raw)
            if damage == "malformed":
                changed = raw[:-1]
            elif damage == "whitespace":
                changed = raw + b"\n"
            elif damage == "dialect":
                fields["input"]["path"] = r"/foreign/embedded\backslash.jsonl"
                changed = canonical_bytes(fields)
            elif damage == "route":
                fields["execution"]["mode"] = "live"
                changed = canonical_bytes(fields)
            elif damage == "minimal":
                fields["capture"] = "minimal"
                fields["recovery_policy"] = "unchanged_originals"
                fields["specification"]["relative_path"] = None
                for source in fields["sources"]:
                    source["snapshot"] = None
                changed = canonical_bytes(fields)
                assert parse_executable_manifest(changed).capture == "minimal"
            else:
                assert damage == "nonpath"
                fields["seed"] = 987
                changed = canonical_bytes(fields)
                store = ArtifactStore(directory, create=False)
                repository.register_artifact(
                    run_id, store.put(changed, "application/json")
                )
            fixed.unlink()
            fixed.write_bytes(changed)
    before = _durable_v12_bytes(root)
    writes = _block_v12_writes(monkeypatch)
    with pytest.raises(LifecycleError) as rejected:
        get_batch_status(run_id, store=root)
    assert reason in str(_v12_rejection(rejected.value))
    assert writes == [] and _durable_v12_bytes(root) == before


@pytest.mark.parametrize(
    ("damage", "reason"),
    [
        pytest.param("cut", "cut differs from the run seal", id="wrong-cut-seal"),
        pytest.param("id", "cut differs from the bundle ID", id="wrong-bundle-id"),
        pytest.param("unregistered", "resource was not found", id="unregistered"),
        pytest.param("omit", "companion set differs", id="omitted-companion"),
        pytest.param("duplicate", "companion set differs", id="duplicate-companion"),
        pytest.param("damage", "failed content verification", id="damaged-companion"),
        pytest.param("hash-only", "bytes were not retained", id="hash-only-companion"),
        pytest.param("missing", "bundle identity differs", id="missing-bundle"),
    ],
)
def test_v12_named_audit_requires_original_seal_and_retained_companions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    windows_v12_consumer: None,
    damage: str,
    reason: str,
) -> None:
    root, run_id, metadata = _pr2_store(tmp_path)
    with Repository(root) as repository:
        store = ArtifactStore(repository.run_directory(run_id), create=False)
        bundle_id = metadata["bundle_id"]
        reference = f"batch/audits/{bundle_id}.json"
        path = store.reference_path(reference)
        bundle = BatchAuditManifestV1.model_validate_json(path.read_bytes())
        fields = bundle.model_dump(mode="json")
        last = fields["files"][-1]
        if damage == "missing":
            path.unlink()
        elif damage == "damage":
            companion = store.root / last["artifact"]["relative_path"]
            companion.write_bytes(b"damaged companion")
        else:
            if damage == "cut":
                fields["cut"]["source_manifest_sha256"] = "a" * 64
            elif damage in {"id", "unregistered"}:
                fields["cut"]["event_high_water_mark"] += 1
            elif damage == "omit":
                fields["files"] = fields["files"][:-1]
            elif damage == "duplicate":
                fields["files"] = [*fields["files"][:-1], fields["files"][0]]
            else:
                assert damage == "hash-only"
                (store.root / last["artifact"]["relative_path"]).unlink()
                last["artifact"]["relative_path"] = None
            if damage in {"cut", "unregistered"}:
                bundle_id = semantic_digest(
                    "native-batch-audit-v1", [run_id, fields["cut"]]
                )
                fields["bundle_id"] = bundle_id
            else:
                path.unlink()
            changed = BatchAuditManifestV1.model_validate_json(canonical_bytes(fields))
            identity = store.put(canonical_bytes(changed), "application/json")
            if damage != "unregistered":
                repository.register_artifact(run_id, identity)
            store.publish_reference(identity, f"batch/audits/{bundle_id}.json")
    before = _durable_v12_bytes(root)
    writes = _block_v12_writes(monkeypatch)
    with pytest.raises(LifecycleError) as rejected:
        export_batch_provenance(store.root, bundle_id=bundle_id)
    failure = _v12_rejection(rejected.value)
    if damage == "unregistered":
        assert isinstance(failure, NotFoundError) and reason in str(rejected.value)
    else:
        assert reason in str(failure)
    assert writes == [] and _durable_v12_bytes(root) == before


def test_native_minimal_v12_named_audit_requires_retained_companions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, run_id = tmp_path / "store", "001"
    prepare_batch(batch_spec(tmp_path, capture="minimal"), store=root, run_id=run_id)
    published = export_batch_provenance(run_id, store=root)
    assert type(published) is BatchAuditReportV1
    with ReadOnlyRepository(root) as repository:
        store = ArtifactStore(repository.run_directory(run_id), create=False)
        seal = repository.get_manifest(run_id).artifact
        raw = store.read(seal)
    manifest = parse_executable_manifest(raw)
    assert type(manifest) is ExecutableRunManifestV12
    assert manifest.capture == "minimal" and canonical_bytes(manifest) == raw
    bundle = published.manifest
    assert bundle.cut.source_manifest_sha256 == seal.sha256 and len(bundle.files) == 11
    bundle_id = bundle.bundle_id
    before = _durable_v12_bytes(root)
    with monkeypatch.context() as inspection:
        writes = _block_v12_writes(inspection)
        reopened = export_batch_provenance(run_id, store=root, bundle_id=bundle_id)
        assert reopened == published
        assert writes == [] and _durable_v12_bytes(root) == before

    fields = bundle.model_dump(mode="json")
    companion = next(
        file for file in fields["files"] if file["name"] == "results_r.csv"
    )
    (store.root / companion["artifact"]["relative_path"]).unlink()
    companion["artifact"]["relative_path"] = None
    reference = f"batch/audits/{bundle_id}.json"
    with Repository(root) as repository:
        changed = store.put(canonical_bytes(fields), "application/json")
        repository.register_artifact(run_id, changed)
        store.reference_path(reference).unlink()
        store.publish_reference(changed, reference)
    before = _durable_v12_bytes(root)
    writes = _block_v12_writes(monkeypatch)
    with pytest.raises(LifecycleError) as rejected:
        export_batch_provenance(run_id, store=root, bundle_id=bundle_id)
    assert "batch audit companion bytes were not retained" in str(
        _v12_rejection(rejected.value)
    )
    assert writes == [] and _durable_v12_bytes(root) == before


@pytest.mark.parametrize(
    "active_wal", [False, True], ids=["checkpointed", "active-wal"]
)
def test_named_audit_reopen_preserves_sqlite_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, active_wal: bool
) -> None:
    root, run_id = tmp_path / "store", "001"
    database = root / "registry.sqlite3"
    wal = root / "registry.sqlite3-wal"
    shm = root / "registry.sqlite3-shm"

    def registry_files() -> dict[str, bytes]:
        return {
            path.name: path.read_bytes()
            for path in (database, wal, shm)
            if path.is_file()
        }

    with ExitStack() as stack:
        if active_wal:
            stack.enter_context(Repository(root))
        prepare_batch(
            batch_spec(tmp_path, capture="minimal"), store=root, run_id=run_id
        )
        published = export_batch_provenance(run_id, store=root)
        directory = Path(published.manifest_path).parents[2]
        bundle_id = published.manifest.bundle_id
        before = registry_files()
        assert set(before) == (
            {database.name, wal.name, shm.name} if active_wal else {database.name}
        )
        if active_wal:
            assert before[wal.name] and before[shm.name]

        original_uri = database.resolve().as_uri()
        connect = sqlite3.connect
        opened: list[str] = []

        def observe(database_uri: str, *args: Any, **kwargs: Any) -> sqlite3.Connection:
            assert registry_files() == before
            if active_wal:
                assert database_uri.partition("?")[
                    0
                ] != original_uri and database_uri.endswith("?mode=ro")
            else:
                assert database_uri == original_uri + "?mode=ro&immutable=1"
            opened.append(database_uri)
            return connect(database_uri, *args, **kwargs)

        with monkeypatch.context() as inspection:
            inspection.setenv("JUDGMENTLLM_RUN_HOME", str(root))
            inspection.setattr(sqlite3, "connect", observe)
            writes = _block_v12_writes(inspection)
            for reference, store in (
                (run_id, root),
                (run_id, None),
                (directory, root),
                (directory, None),
            ):
                opened.clear()
                reopened = export_batch_provenance(
                    reference, store=store, bundle_id=bundle_id
                )
                assert reopened == published
                # Resolution and bundle reopen each open SQLite.
                assert len(opened) >= 2 and registry_files() == before

            (directory / "manifest.json").unlink()
            opened.clear()
            with pytest.raises(LifecycleError, match="integrity") as rejected:
                export_batch_provenance(directory, bundle_id=bundle_id)
            assert "no complete published manifest" in str(
                _v12_rejection(rejected.value)
            )
            assert len(opened) >= 2 and registry_files() == before
            assert writes == []


@pytest.mark.parametrize(
    ("state", "reason"),
    [
        pytest.param("sealed", "native V12 source provenance", id="sealed"),
        pytest.param(
            "unsealed", "native V12 source provenance", id="published-unsealed"
        ),
        pytest.param("unmarked", "batch marker differs", id="version-only"),
        pytest.param("marker", "no complete published manifest", id="mock-marker-only"),
        pytest.param(
            "invalid", "manifest is invalid or incomplete", id="invalid-marked"
        ),
    ],
)
def test_v12_refuses_mutation_before_writer_or_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str, reason: str
) -> None:
    from test_preparation_recovery import interrupt_at

    from judgmentllm import lifecycle, native_batch
    from judgmentllm.execution.cancellation import cancel_run as cancel_idle
    from judgmentllm.execution.core import execute_run, recover_run
    from judgmentllm.execution.exports import export_run
    from judgmentllm.execution.retry import authorize_subset_retry
    from judgmentllm.providers.mock import MockAdapter

    root, run_id = tmp_path / "store", "001"
    spec = batch_spec(tmp_path)
    if state == "sealed":
        prepare_batch(spec, store=root, run_id=run_id)
    else:
        with monkeypatch.context() as interruption:
            interrupt_at(interruption, "before_seal")
            with pytest.raises(KeyboardInterrupt):
                prepare_batch(spec, store=root, run_id=run_id)
    with Repository(root) as repository:
        directory = repository.run_directory(run_id)
        fixed = directory / "manifest.json"
        raw = fixed.read_bytes()
        manifest = parse_executable_manifest(raw)
        assert type(manifest) is ExecutableRunManifestV12
        assert canonical_bytes(manifest) == raw
        assert (repository.get_run(run_id).manifest_sha256 is not None) == (
            state == "sealed"
        )
        if state == "unmarked":
            _edit_v12_row(
                repository,
                "batch_runs_immutable_delete",
                "DELETE FROM batch_runs WHERE run_id=?",
                (run_id,),
            )
        elif state in {"marker", "invalid"}:
            fixed.unlink()
            if state == "invalid":
                fixed.write_bytes(raw[:-1])
    adapter = MockAdapter(tmp_path / "direct-service.sqlite3")
    before = _durable_v12_bytes(root)
    aliases = unavailable_originals(
        monkeypatch, {source.original_location for source in manifest.sources}
    )
    writes = _block_v12_writes(monkeypatch)

    def no_adapter(*args: object, **kwargs: object) -> None:
        pytest.fail("historical V12 reached adapter work")

    monkeypatch.setattr(lifecycle, "_execution_adapter", no_adapter)
    monkeypatch.setattr(native_batch, "MockBatchAdapter", no_adapter)
    monkeypatch.setattr(adapter, "capabilities", no_adapter)

    public_calls = (
        stop_batch,
        cancel_batch,
        cancel_run,
        export_run_results,
        export_batch_provenance,
        partial(resume_run, use_frozen_snapshot=True),
        partial(resume_batch, use_frozen_snapshot=True),
    )
    selectors: list[tuple[str | Path, Path | None]] = [(run_id, root)]
    if state != "invalid":
        selectors.append((directory, None))
    for identifier, store in selectors:
        for operation in public_calls:
            with pytest.raises(LifecycleError, match="integrity") as rejected:
                operation(identifier, store=store)
            assert reason in str(_v12_rejection(rejected.value))
        if state == "sealed":
            with pytest.raises(LifecycleError, match="integrity") as rejected:
                export_run_provenance(identifier, store=store)
            assert reason in str(_v12_rejection(rejected.value))
    with pytest.raises(IntegrityError, match=reason):
        open_prepared_run(run_id, store=root, use_frozen_snapshot=True)
    with pytest.raises(IntegrityError, match=reason):
        cancel_idle(run_id, store=root)
    with pytest.raises(IntegrityError, match=reason):
        export_run(run_id, store=root)
    with pytest.raises(IntegrityError, match=reason):
        authorize_subset_retry(
            run_id, store=root, adapter=adapter, task_ids=("001",), operation_id="retry"
        )
    for execute in (execute_run, recover_run):
        with pytest.raises(IntegrityError, match=reason):
            asyncio.run(
                execute(run_id, store=root, adapter=adapter, use_frozen_snapshot=True)
            )
    assert adapter.submit_calls == 0
    assert aliases == writes == []
    assert _durable_v12_bytes(root) == before


def test_native_v12_interrupted_publication_keeps_owned_sealing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_native_batch import service_counts
    from test_preparation_recovery import interrupt_at

    from judgmentllm.providers.batch_mock import MockBatchAdapter

    root, run_id = tmp_path / "store", "001"
    spec = batch_spec(tmp_path)
    with monkeypatch.context() as interruption:
        interrupt_at(interruption, "before_seal")
        with pytest.raises(KeyboardInterrupt):
            prepare_batch(spec, store=root, run_id=run_id)
    with ReadOnlyRepository(root) as repository:
        assert repository.get_run(run_id).manifest_sha256 is None
        assert repository.get_owner(run_id) is None
        fixed = repository.run_directory(run_id) / "manifest.json"
        original = fixed.read_bytes()
        assert type(parse_executable_manifest(original)) is ExecutableRunManifestV12
    profile = MockBatchAdapter.capabilities_for(
        spec.provider, spec.endpoint, spec.model
    ).model_copy(update={"modes": ("batch_native",)})
    with open_prepared_run(run_id, store=root, capabilities=profile) as recovered:
        assert recovered.manifest_sha256 == bytes_digest(original)
        assert tuple(response.item_id for response in recovered.iter_responses()) == (
            "000",
            "001",
        )
    assert fixed.read_bytes() == original
    with ReadOnlyRepository(root) as repository:
        assert repository.get_manifest(run_id).artifact.sha256 == bytes_digest(original)
        assert repository.get_owner(run_id) is None
        assert (
            sum(event.kind == "manifest_sealed" for event in repository.events(run_id))
            == 1
        )
    assert service_counts(root) == {"forwards": 0, "jobs": 0, "cancelled": 0}


def test_ordinary_unsealed_result_export_needs_no_published_manifest(
    tmp_path: Path,
) -> None:
    with Repository(tmp_path) as repository:
        repository.create_run(
            RunRecord(
                run_id="001",
                task_kind="rubric",
                provider="mock",
                endpoint="synthetic",
                model="fixture",
            )
        )
        directory = repository.run_directory("001")
    assert not (directory / "manifest.json").exists()
    exported = export_run_results("001", store=tmp_path)
    assert exported.row_count == 0 and exported.incomplete_source
    with ReadOnlyRepository(tmp_path) as repository:
        assert repository.get_run("001").manifest_sha256 is None
        assert (
            repository.connection.execute("SELECT count(*) FROM exports").fetchone()[0]
            == 1
        )
    assert not (directory / "manifest.json").exists()
