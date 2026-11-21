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

from judgmentllm import export_run_results, get_run, prepare_batch
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


def test_posix_wire_checks_pure_windows_presentation_without_native_execution(
    historical_copy: HistoricalCopy, monkeypatch: pytest.MonkeyPatch
) -> None:
    import judgmentllm.preparation.historical_wire as wire_module
    from judgmentllm.preparation.manifest import ExecutableRunManifestV2

    _, _, manifest, raw, specification = historical_copy
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
    wire = wire_module.HistoricalWire.checked(manifest, raw, specification)
    assert canonical_bytes(manifest) != raw
    assert wire.manifest.encode(manifest) == raw
    assert wire.specification.encode(wire.restore()) == specification


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
    historical_copy: HistoricalCopy, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    import judgmentllm.preparation as preparation

    root, run_id, manifest, raw, _ = historical_copy
    originals = {source.original_location for source in manifest.sources}
    attempts: list[str] = []

    # Unit-test the foreign-source boundary on Linux; this is not a Windows run.
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
    assert get_run(run_id, store=root).batch is None
    with pytest.raises(IntegrityError, match="foreign originals"):
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
    with pytest.raises(IntegrityError, match="foreign originals"):
        open_prepared_run(run_id, store=root)
    assert attempts == []


def test_historical_manifest_projection_is_manifest_only_and_readonly(
    historical_copy: HistoricalCopy, monkeypatch: pytest.MonkeyPatch
) -> None:
    import judgmentllm.preparation as preparation

    root, run_id, _, raw, _ = historical_copy
    reads: list[str] = []
    read = ArtifactStore.read

    def watched(self: ArtifactStore, identity: Any) -> bytes:
        reads.append(identity.sha256)
        return read(self, identity)

    def native_presentation(model: object) -> bytes:
        data = json.loads(canonical_bytes(model))
        if isinstance(model, ExecutableRunManifestV2):
            data["input"]["path"] = str(PureWindowsPath(data["input"]["path"]))
            if data["pairs"] is not None:
                data["pairs"]["path"] = str(PureWindowsPath(data["pairs"]["path"]))
        return canonical_bytes(data)

    monkeypatch.setattr(preparation, "canonical_bytes", native_presentation)
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
    historical_copy: HistoricalCopy, monkeypatch: pytest.MonkeyPatch
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
