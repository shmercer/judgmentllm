"""Reconstruction checks original membership and never replaces a sealed request."""

from __future__ import annotations

import base64
import csv
import gzip
import io
import json
from contextlib import ExitStack
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from pydantic import BaseModel
from test_decisions_contracts import pair_spec, presentation
from test_decisions_lifecycle import native_spec
from test_decisions_validation import native_request
from test_native_transport_integration import prepared_case

import judgmentllm.preparation as preparation
from judgmentllm.config import RunSpecV4
from judgmentllm.decisions.records import DecisionsLimitsV1
from judgmentllm.decisions.transport import (
    DecisionsCommitmentV1,
    DecisionsContextV1,
    prepare_request,
)
from judgmentllm.lifecycle import (
    LifecycleError,
    _execution_adapter,  # pyright: ignore[reportPrivateUsage]
    _require_presentation,  # pyright: ignore[reportPrivateUsage]
    resume_run,
)
from judgmentllm.persistence.artifacts import ArtifactStore
from judgmentllm.persistence.hashing import bytes_digest, canonical_bytes
from judgmentllm.persistence.models import (
    ArtifactIdentity,
    IntegrityError,
    ManifestReference,
    RunRecord,
)
from judgmentllm.persistence.ownership import ExecutorOwnership
from judgmentllm.persistence.presentations import add_presentation, get_presentation
from judgmentllm.persistence.repository import Repository
from judgmentllm.persistence.sources import SourceRecord
from judgmentllm.planning import CapabilityProfile, _groups, request_context_fingerprint  # pyright: ignore[reportPrivateUsage]
from judgmentllm.preparation import PreparationError, open_prepared_run, prepare_run
from judgmentllm.preparation.capture import SourceBuilder
from judgmentllm.preparation.decisions import (
    DecisionsPreparedRun,
    prepare_decisions_run,
    presentation_records,
)
from judgmentllm.preparation.index import read_record
from judgmentllm.preparation.manifest import ExecutableRunManifestV8
from judgmentllm.providers.decisions_mock import MockDecisionsAdapter
from judgmentllm.request_rendering import render_request
from judgmentllm.transport import physical_identity


@pytest.mark.parametrize(
    "case", ["dependency", "parent", "frozen", "missing_parent", "new_retry"]
)
def test_reconstruction_only_admits_original_ordered_subset_and_parent(
    tmp_path: Path, case: str
) -> None:
    spec = native_spec(tmp_path)
    with prepare_run(spec, store=tmp_path / "store", run_id="001") as prepared:
        assert isinstance(prepared, DecisionsPreparedRun)
        dependency = next(prepared.iter_request_dependencies())
        first = next(prepared.repository.iter_attempts("001"))
        receipt = prepared.transport_for_attempt(first.attempt_id)
        identity = (
            receipt.identity
            if case in {"dependency", "frozen"}
            else physical_identity("001", first.request_uid, "retry-01")
        )
        selected = (
            dependency.model_copy(update={"shared_input": "foreign"})
            if case == "dependency"
            else dependency
        )
        parent = (
            receipt.model_copy(update={"payload_sha256": "f" * 64})
            if case == "parent"
            else receipt
            if case == "new_retry"
            else None
        )
        frozen = (
            receipt.model_copy(update={"context_sha256": "f" * 64})
            if case == "frozen"
            else None
        )
        if case == "new_retry":
            wire = prepared.prepare_transport_attempt(selected, identity, parent=parent)
            assert wire.task_ids == tuple(t.task_id for t in dependency.request.tasks)
            assert (
                wire.transport_commitment.parent_commitment_sha256
                == receipt.commitment_sha256
            )
            assert (
                prepared.repository.get_attempt_for_request(
                    "001", first.request_uid, attempt_number=2
                )
                is None
            )
        else:
            with pytest.raises(PreparationError):
                prepared.prepare_transport_attempt(
                    selected, identity, parent=parent, frozen=frozen
                )
        assert prepared.repository.summary("001").results == 0


@pytest.mark.parametrize(
    "case",
    [
        "type",
        "identity",
        "bytes",
        "dependency",
        "missing_source",
        "empty_presentation",
        "missing_attempt",
        "membership",
        "digest",
        "source_index",
    ],
)
def test_current_catalog_substitution_cannot_change_sealed_native_work(
    tmp_path: Path, case: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import judgmentllm.preparation.decisions as native

    spec = (
        pair_spec(tmp_path)
        if case in {"empty_presentation", "source_index"}
        else native_spec(tmp_path, sources=2, maximum=2)
    )
    with prepare_run(spec, store=tmp_path / "store", run_id="001") as prepared:
        assert isinstance(prepared, DecisionsPreparedRun)
        first = next(prepared.repository.iter_attempts("001"))
        receipt = prepared.transport_for_attempt(first.attempt_id)
        if case in {"type", "identity", "bytes"}:
            if case == "type":

                def unavailable(*_args: Any) -> None:
                    return None

                monkeypatch.setattr(native, "read_transport_commitment", unavailable)
            elif case == "identity":

                def foreign(*_args: Any) -> DecisionsCommitmentV1:
                    return receipt.model_copy(
                        update={
                            "identity": physical_identity(
                                "other", first.request_uid, first.attempt_id
                            )
                        }
                    )

                monkeypatch.setattr(native, "read_transport_commitment", foreign)
            else:

                def wrong_bytes(*_args: Any) -> bytes:
                    return b"different bytes"

                monkeypatch.setattr(prepared.store, "read", wrong_bytes)
            with pytest.raises(PreparationError):
                prepared.transport_for_attempt(first.attempt_id)
        elif case == "dependency":
            original = prepared.repository.get_request

            def changed_request(*args: Any, **kwargs: Any) -> Any:
                return original(*args, **kwargs).model_copy(
                    update={"dependency_sha256": "f" * 64}
                )

            monkeypatch.setattr(prepared.repository, "get_request", changed_request)
            with pytest.raises(PreparationError, match="dependency changed"):
                prepared.dependency_for_request(first.request_uid)
        elif case in {"missing_source", "empty_presentation"}:
            patch = (
                {"source_record_id": "001"}
                if case == "missing_source"
                else {"presentation_id": "foreign"}
            )

            def changed_receipt(*_args: Any) -> DecisionsCommitmentV1:
                return receipt.model_copy(update=patch)

            monkeypatch.setattr(prepared, "transport_for_attempt", changed_receipt)
            with pytest.raises(PreparationError, match="absent from original"):
                prepared.dependency_for_request(first.request_uid)
        else:
            if case == "missing_attempt":

                def absent(*_args: Any, **_kwargs: Any) -> None:
                    return None

                monkeypatch.setattr(
                    prepared.repository, "get_attempt_for_request", absent
                )
            elif case == "membership":

                def members(*_args: Any) -> tuple[str, ...]:
                    return ("foreign",)

                monkeypatch.setattr(prepared.repository, "attempt_task_ids", members)
            elif case == "digest":
                prepared.manifest = prepared.manifest.model_copy(
                    update={"initial_transport_sha256": "f" * 64}
                )
            else:
                prepared.manifest = prepared.manifest.model_copy(
                    update={
                        "sources": tuple(
                            s.model_copy(update={"index_sha256": "f" * 64})
                            if s.role == "pair_presentations"
                            else s
                            for s in prepared.manifest.sources
                        )
                    }
                )
            with pytest.raises(PreparationError):
                prepared.verify_transport_index()


def test_native_and_historical_preparation_interfaces_remain_separate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec, dependency, wire = native_request(tmp_path / "new")
    with pytest.raises(IntegrityError):
        prepare_request(
            spec,
            dependency,
            physical_identity("001", "foreign", "attempt"),
            DecisionsContextV1(service_identity="offline"),
        )
    with pytest.raises(ValueError):
        request_context_fingerprint(spec, template_text=None)
    with pytest.raises(ValueError):
        _groups(spec, CapabilityProfile())

    def forbidden_lookup(*_args: Any) -> Any:
        pytest.fail("legacy rendering looked up native data")

    with pytest.raises(ValueError):
        render_request(spec, None, dependency.request, forbidden_lookup)
    adapter, store, old = prepared_case(tmp_path / "old")
    with open_prepared_run(
        "001", store=store, spec=old, transport_context=adapter.transport_context
    ) as previous:
        with pytest.raises(PreparationError):
            previous.prepare_transport_attempt(
                dependency,
                wire.transport_commitment.identity,
                parent=wire.transport_commitment,
            )
        import judgmentllm.persistence.transport_receipts as receipts

        def foreign_receipt(*_args: Any) -> DecisionsCommitmentV1:
            return wire.transport_commitment

        monkeypatch.setattr(receipts, "read_transport_commitment", foreign_receipt)
        attempt = next(previous.repository.iter_attempts("001"))
        with pytest.raises(PreparationError):
            previous.transport_for_attempt(attempt.attempt_id)


def test_source_loader_rechecks_repeated_documents_and_never_forwards(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = native_spec(tmp_path)
    config = tmp_path / "run.json"
    config.write_bytes(
        canonical_bytes({**spec.model_dump(mode="json"), "criteria": str(config)})
    )
    original = preparation._memory_source  # pyright: ignore[reportPrivateUsage]
    count = 0

    def race(path: Path) -> Any:
        nonlocal count
        result = original(path)
        if path == config:
            count += 1
            if count == 3:
                path.write_bytes(result[0] + b"\n")
        return result

    monkeypatch.setattr(preparation, "_memory_source", race)
    with pytest.raises(PreparationError, match="initial discovery"):
        prepare_run(config, store=tmp_path / "store", run_id="001")


def test_ordinary_preparation_reports_controls_safely_and_keeps_legacy_builder_closed(
    tmp_path: Path,
) -> None:
    spec = native_spec(tmp_path)
    context_adapter = MockDecisionsAdapter(tmp_path / "ledger.sqlite3")
    context = context_adapter.transport_context.model_copy(
        update={"limits": DecisionsLimitsV1(max_body_bytes=1)}
    )
    with pytest.raises(PreparationError, match="oversized"):
        prepare_decisions_run(
            spec, store=tmp_path / "oversized", run_id="001", context=context
        )
    spec.input.path.write_text('{"item_id":"PRIVATE_CANARY",broken\n')
    with pytest.raises(PreparationError) as invalid:
        prepare_decisions_run(spec, store=tmp_path / "malformed", run_id="001")
    assert "PRIVATE_CANARY" not in str(invalid.value)
    unimplemented = cast(
        RunSpecV4,
        BaseModel.model_copy(
            spec, update={"provider": "vertex", "endpoint": "decisions"}
        ),
    )
    with pytest.raises(PreparationError, match="configuration validation failed"):
        prepare_decisions_run(
            unimplemented, store=tmp_path / "unsupported", run_id="001"
        )
    with pytest.raises(LifecycleError, match="production Decisions adapters"):
        _execution_adapter(
            unimplemented.provider,
            unimplemented.endpoint,
            unimplemented.model,
            tmp_path,
            None,
            resume=False,
            decisions=True,
        )
    with pytest.raises(LifecycleError, match="production Decisions adapters"):
        _require_presentation(unimplemented)


@pytest.mark.asyncio
async def test_decisions_evidence_version_cannot_claim_a_vendor_scope(
    tmp_path: Path,
) -> None:
    from judgmentllm.execution import execute_run
    from judgmentllm.persistence.evidence import (
        DecisionsEvidenceReceiptV2,
        read_evidence_receipt,
    )

    spec = native_spec(tmp_path)
    store = tmp_path / "store"
    with prepare_run(spec, store=store, run_id="001"):
        pass
    await execute_run(
        "001", store=store, adapter=MockDecisionsAdapter(store / "mock-service.sqlite3")
    )
    with Repository(store) as repository:
        submission = next(repository.iter_submissions("001"))
        receipt = read_evidence_receipt(
            ArtifactStore(repository.run_directory("001"), create=False),
            submission.submission_id,
        )
        assert isinstance(receipt, DecisionsEvidenceReceiptV2)
        for field in ("version", "scope"):
            data = receipt.model_dump()
            if field == "version":
                data["evidence_receipt_version"] = True
            else:
                data["usage"]["endpoint"] = "foreign"
            with pytest.raises(ValueError):
                DecisionsEvidenceReceiptV2.model_validate(data)


def test_original_study_verifier_does_not_adopt_v4_native_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from judgmentllm.study import sources

    spec = native_spec(tmp_path / "inputs")
    source, package = tmp_path / "source", tmp_path / "package"
    root = source / "original"
    (root / "configs").mkdir(parents=True)
    package.mkdir()
    (root / "configs/run.yaml").write_text(yaml.safe_dump(spec.model_dump(mode="json")))
    files = {"configs/run.yaml": bytes_digest((root / "configs/run.yaml").read_bytes())}
    raw = canonical_bytes(files)
    (root / "manifest_sha256.json").write_bytes(raw)
    (package / "original-previews.jsonl.gz.base64").write_bytes(
        base64.b64encode(gzip.compress(b""))
    )

    def manifests(*_args: Any) -> Any:
        return {
            "rubric": {
                "package": "original",
                "manifest_sha256": bytes_digest(raw),
                "files": files,
            }
        }

    def inventory(*_args: Any) -> None:
        return None

    monkeypatch.setattr(sources, "source_manifest", manifests)
    monkeypatch.setattr(sources, "source_inventory", inventory)
    with pytest.raises(ValueError, match="historical study sources"):
        sources.verify_originals(source, package)


def published_native(
    tmp_path: Path, spec: RunSpecV4, monkeypatch: pytest.MonkeyPatch
) -> Path:
    store = tmp_path / "store"

    def interrupt(stage: str, *_args: object) -> None:
        if stage == "manifest_published":
            raise RuntimeError("synthetic pre-seal interruption")

    with monkeypatch.context() as patch:
        patch.setattr(preparation, "_checkpoint", interrupt)
        with pytest.raises(RuntimeError, match="pre-seal"):
            prepare_run(spec, store=store, run_id="001")
    return store


@pytest.mark.parametrize("capture", ["full", "minimal"])
@pytest.mark.parametrize("fault", ["manifest", "specification", "marker"])
def test_public_preseal_resume_does_not_bypass_frozen_authorities(
    tmp_path: Path, capture: str, fault: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = native_spec(tmp_path, capture=capture)
    store = published_native(tmp_path, spec, monkeypatch)
    with Repository(store) as repository:
        path = repository.run_directory("001") / "manifest.json"
        raw = path.read_bytes()
    if fault == "manifest":
        path.write_bytes(b"{}")
    elif fault == "specification":
        spec = RunSpecV4.model_validate({**spec.model_dump(), "seed": 99})
    else:
        from judgmentllm.providers import MockAdapter

        store = tmp_path / "unmarked"
        with Repository(store) as repository:
            repository.create_run(
                RunRecord(
                    run_id="001",
                    task_kind="rubric",
                    provider="mock",
                    endpoint="synthetic",
                    model=spec.model,
                    capture=spec.capture,
                )
            )
            artifacts = ArtifactStore(repository.run_directory("001"))
            identity = artifacts.put(raw, "application/json")
            repository.register_artifact("001", identity)
            artifacts.publish_manifest(identity)
        MockAdapter(store / "mock-service.sqlite3")
    with pytest.raises(LifecycleError):
        resume_run("001", store=store, spec=spec)
    with Repository(store) as repository:
        run = repository.get_run("001")
        assert run.state == "created" and run.manifest_sha256 is None
        assert repository.decisions_version("001") == (None if fault == "marker" else 4)
        assert repository.summary("001").results == 0
        assert not list(repository.iter_submissions("001"))
        assert repository.get_owner("001") is None


@pytest.mark.parametrize("capture", ["full", "minimal"])
def test_public_sealed_resume_checks_manifest_before_selecting_adapter(
    tmp_path: Path, capture: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import judgmentllm.lifecycle as lifecycle

    spec = native_spec(tmp_path, capture=capture)
    store = tmp_path / "store"
    with prepare_run(spec, store=store, run_id="001") as prepared:
        path = prepared.repository.run_directory("001") / "manifest.json"
    path.write_bytes(b"{}")

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("adapter selection preceded sealed manifest validation")

    monkeypatch.setattr(lifecycle, "_execution_adapter", forbidden)
    with pytest.raises(LifecycleError):
        resume_run("001", store=store, spec=spec)
    with Repository(store) as repository:
        assert repository.get_run("001").state == "submitted"
        assert repository.summary("001").results == 0
        assert not list(repository.iter_submissions("001"))


@pytest.mark.parametrize("fault", ["version", "run", "specification"])
def test_seal_checks_typed_run_and_retained_specification_authorities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    from test_preparation_rendering import specification

    spec = native_spec(tmp_path / "new")
    store = published_native(tmp_path, spec, monkeypatch)
    with Repository(store) as repository:
        manifest = preparation._load_manifest(repository, "001", unsealed=True)  # pyright: ignore[reportPrivateUsage]
        artifact = repository.get_artifact(
            "001", bytes_digest(canonical_bytes(manifest))
        )
        frozen = spec
        if fault == "version":
            frozen = cast(
                RunSpecV4, specification(tmp_path / "old", responses=1, traits=1)
            )
        elif fault == "run":
            original_run = repository.get_run

            def foreign(run_id: str) -> RunRecord:
                return original_run(run_id).model_copy(update={"model": "foreign"})

            monkeypatch.setattr(repository, "get_run", foreign)
        else:
            original_read = ArtifactStore.read

            def changed(self: ArtifactStore, identity: Any) -> bytes:
                return (
                    b"different"
                    if identity.sha256 == manifest.specification.sha256
                    else original_read(self, identity)
                )

            monkeypatch.setattr(ArtifactStore, "read", changed)
        with pytest.raises(IntegrityError, match="RunSpec V4|specification bytes"):
            repository.seal_run(
                "001", ManifestReference(artifact=artifact), decisions_spec=frozen
            )
        assert repository.get_run("001").state == "created"


def test_unmarked_run_cannot_acquire_native_source_or_seal_authority(
    tmp_path: Path,
) -> None:
    from test_preparation_rendering import specification

    native = native_spec(tmp_path / "new")
    old = specification(tmp_path / "old", responses=1, traits=1)
    with prepare_run(old, store=tmp_path / "store", run_id="old") as prepared:
        assert prepared.repository.get_run("old").manifest_sha256 is not None
        artifact = prepared.repository.get_artifact("old", prepared.manifest_sha256)
        with pytest.raises(IntegrityError, match="historical seal"):
            prepared.repository.seal_run(
                "old", ManifestReference(artifact=artifact), decisions_spec=native
            )
        source = SourceRecord(
            run_id="old",
            source_id="foreign",
            role="pair_presentations",
            ordinal=99,
            original_location="synthetic",
            raw_sha256="f" * 64,
            byte_length=1,
            format="jsonl",
        )
        with pytest.raises(IntegrityError, match="authority"):
            prepared.repository.add_source(source, owner_token="unused")


@pytest.mark.parametrize("fault", ["role", "length"])
def test_presentation_locator_requires_the_frozen_presentation_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    spec = pair_spec(tmp_path)
    store = published_native(tmp_path, spec, monkeypatch)
    with Repository(store) as repository, ExecutorOwnership(repository, "001") as owner:
        assert owner.owner is not None
        index = get_presentation(repository, "001", "comparison-001")
        parent = repository.get_source("001", index.source_id)

        def wrong_source(*_args: Any) -> SourceRecord:
            return parent.model_copy(
                update={"role": "response"} if fault == "role" else {"byte_length": 0}
            )

        monkeypatch.setattr(repository, "get_source", wrong_source)
        with pytest.raises(IntegrityError, match="locator"):
            add_presentation(repository, index, presentation(), owner.owner.token)


def test_run_seed_and_legacy_source_builder_cannot_reschedule_native_presentations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = pair_spec(tmp_path)
    conflict = RunSpecV4.model_validate({**spec.model_dump(), "seed": 42})
    with pytest.raises(PreparationError, match="seed conflicts"):
        prepare_run(conflict, store=tmp_path / "conflict", run_id="001")
    store = published_native(tmp_path, spec, monkeypatch)
    with (
        Repository(store) as repository,
        ExecutorOwnership(repository, "001") as owner,
        ExitStack() as stack,
    ):
        assert owner.owner is not None

        def checkpoint(_stage: str, _source: str | None) -> None:
            return None

        builder = SourceBuilder(
            repository,
            "001",
            "full",
            owner.owner.token,
            stack,
            checkpoint=checkpoint,
            memory_source=preparation._memory_source,  # pyright: ignore[reportPrivateUsage]
            record_reader=read_record,
        )
        with pytest.raises(PreparationError, match="V4 requires"):
            builder._validate_representatives(spec, None, None)  # pyright: ignore[reportPrivateUsage]


def test_optional_csv_versions_and_exact_source_page_are_independently_prepared(
    tmp_path: Path,
) -> None:
    spec = pair_spec(tmp_path)
    source = presentation(criteria=1).model_dump(mode="json")
    source.pop("schema_version")
    source.pop("pair_presentation_version")
    source.pop("schedule")
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=list(source))
    writer.writeheader()
    writer.writerow({**source, "members": json.dumps(source["members"])})
    assert spec.pair_presentations is not None
    reference = spec.pair_presentations.model_copy(update={"format": "csv"})
    record = next(
        presentation_records(io.BytesIO(stream.getvalue().encode()), reference)
    )[0]
    assert record.schedule.repetitions == 1 and record.schedule.seed is None
    path = spec.pair_presentations.path
    with path.open("w") as output:
        for i in range(128):
            data = record.model_dump(mode="json")
            data["source_presentation_id"] = f"occurrence-{i:03d}"
            data["members"][0]["pair_uid"] = f"source-{i:03d}"
            output.write(json.dumps(data) + "\n")
    spec = RunSpecV4.model_validate({**spec.model_dump(), "capture": "minimal"})
    with prepare_run(spec, store=tmp_path / "store", run_id="001") as prepared:
        assert isinstance(prepared, DecisionsPreparedRun)
        assert isinstance(prepared.manifest, ExecutableRunManifestV8)
        assert prepared.manifest.presentation_count == 128
        assert len(list(prepared.iter_request_dependencies())) == 128
        assert prepared.repository.summary("001").results == 0


def test_native_reconstruction_checks_content_and_request_start_without_repacking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = native_spec(tmp_path, sources=2)
    with prepare_run(spec, store=tmp_path / "store", run_id="001") as prepared:
        assert isinstance(prepared, DecisionsPreparedRun)
        assert len(list(prepared.iter_request_dependencies(start_ordinal=1))) == 1
        dependency = next(prepared.iter_request_dependencies())
        attempt = next(prepared.repository.iter_attempts("001"))
        receipt = prepared.transport_for_attempt(attempt.attempt_id)
        wire = prepared.prepare_transport_attempt(dependency, receipt.identity)
        target = bytes_digest(next(iter(wire.content_artifacts().values())))
        original = prepared.repository.get_artifact

        def altered(run_id: str, digest: str) -> ArtifactIdentity:
            artifact = original(run_id, digest)
            return (
                artifact.model_copy(update={"byte_length": artifact.byte_length + 1})
                if digest == target
                else artifact
            )

        monkeypatch.setattr(prepared.repository, "get_artifact", altered)
        with pytest.raises(PreparationError, match="content artifact changed"):
            prepared.prepare_transport_attempt(dependency, receipt.identity)


def test_direct_native_loader_sanitizes_source_open_failures(tmp_path: Path) -> None:
    with pytest.raises(
        PreparationError, match="source/configuration validation"
    ) as error:
        prepare_decisions_run(
            tmp_path / "PRIVATE_MISSING_SOURCE.yaml",
            store=tmp_path / "store",
            run_id="001",
        )
    assert "PRIVATE_MISSING_SOURCE" not in str(error.value)
