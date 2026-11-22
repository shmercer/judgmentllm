"""Actual base-produced schema-3 bytes retain their original reader contract."""

import base64
import io
import json
import tarfile
from pathlib import Path, PureWindowsPath
from typing import Any, Literal, get_args

import pytest
from pydantic import BaseModel
from test_native_batch import batch_spec
from test_version_compatibility import old_store as old_store

from judgmentllm import (
    MockExecutionOptions,
    RetrySelection,
    export_run_results,
    get_run,
    prepare_batch,
    resume_run,
)
from judgmentllm.config import PromptSpec, RunSpec, RunSpecV2, validate_run_spec_json
from judgmentllm.lifecycle_models import ErrorCategory, LifecycleError
from judgmentllm.persistence.artifacts import ArtifactStore
from judgmentllm.persistence.hashing import bytes_digest, canonical_bytes
from judgmentllm.persistence.models import IntegrityError
from judgmentllm.persistence.repository import ReadOnlyRepository, Repository
from judgmentllm.preparation import open_prepared_run
from judgmentllm.preparation.manifest import (
    ExecutableRunManifestV2,
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
    historical_copy: HistoricalCopy,
) -> None:
    from judgmentllm.preparation.historical_wire import HistoricalWire

    _, _, manifest, raw, specification = historical_copy
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

    def classified(location: str) -> bool:
        return location in originals

    monkeypatch.setattr(preparation, "foreign_original", classified)
    for method in ("open", "stat", "resolve"):
        original = getattr(Path, method)

        def forbidden(
            self: Path, *args: Any, call: Any = original, **kwargs: Any
        ) -> Any:
            if str(self) in originals:
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
