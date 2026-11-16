"""Companions distinguish missing evidence and reject corrupted publications."""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from test_decisions_lifecycle import native_spec
from test_decisions_persistence_boundaries import forwarding_fact
from test_preparation_rendering import specification
from typer.testing import CliRunner

from judgmentllm.cli import app
from judgmentllm.decisions import provenance
from judgmentllm.decisions.outcomes import outcome_reference, read_outcomes
from judgmentllm.decisions.provenance import (
    export_decisions_provenance,
    read_decisions_provenance,
)
from judgmentllm.evidence.usage import ProviderUsage
from judgmentllm.execution import execute_run
from judgmentllm.execution.session import ExecutionSession
from judgmentllm.lifecycle import get_run
from judgmentllm.persistence.artifacts import ArtifactStore
from judgmentllm.persistence.decisions_invocations import (
    invocation_reference,
    read_invocation,
)
from judgmentllm.persistence.hashing import bytes_digest, canonical_bytes
from judgmentllm.persistence.models import (
    ArtifactIntegrityError,
    IntegrityError,
    NotFoundError,
)
from judgmentllm.persistence.repository import Repository
from judgmentllm.preparation import prepare_run
from judgmentllm.preparation.manifest import ExecutableRunManifestV8
from judgmentllm.providers.decisions_mock import (
    DecisionsScenarioV1,
    MockDecisionsAdapter,
)
from judgmentllm.transport import physical_identity


@pytest.mark.parametrize(
    "case", ["partial", "currency", "unavailable", "unknown", "empty"]
)
def test_physical_totals_never_infer_zero_from_missing_evidence(
    tmp_path: Path, case: str
) -> None:
    spec = native_spec(tmp_path)
    with prepare_run(spec, store=tmp_path / "store", run_id="001") as prepared:
        assert isinstance(prepared.manifest, ExecutableRunManifestV8)
        manifest = prepared.manifest
    usage = forwarding_fact(tmp_path / "fact").usage.model_dump(mode="json")
    totals = provenance._Totals()  # pyright: ignore[reportPrivateUsage]
    if case in {"partial", "currency"}:
        first = ProviderUsage.model_validate_json(
            canonical_bytes(
                {
                    **usage,
                    "usage_source": "synthetic.reported_usage",
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "reported_cost": {
                        "amount": "0",
                        "currency": "USD",
                        "source": "synthetic.reported_cost",
                    },
                }
            )
        )
        totals.record(first, known=True, unknown=False, observed=True)
        second = ProviderUsage.model_validate_json(
            canonical_bytes(
                {
                    **first.model_dump(mode="json"),
                    "reported_cost": None
                    if case == "partial"
                    else {
                        "amount": "0",
                        "currency": "EUR",
                        "source": "synthetic.reported_cost",
                    },
                    "output_tokens": None if case == "partial" else 0,
                }
            )
        )
        totals.record(second, known=True, unknown=False, observed=True)
    elif case == "unknown":
        totals.record(None, known=False, unknown=True, observed=False)
    elif case == "empty":
        manifest = manifest.model_copy(
            update={"initial_transport_count": 0, "logical_task_count": 0}
        )
    detail = totals.detail(manifest)
    if case == "partial":
        assert detail.usage_evidence == detail.cost_evidence == "partial"
        assert detail.input_tokens == 0 and detail.output_tokens is detail.cost is None
    elif case == "currency":
        assert (
            detail.usage_evidence == "reported"
            and detail.input_tokens == detail.output_tokens == 0
        )
        assert (
            detail.cost_evidence == "unknown"
            and detail.cost is detail.cost_currency is None
        )
    else:
        assert (
            detail.usage_evidence
            == {
                "unavailable": "unavailable",
                "unknown": "unknown",
                "empty": "not_applicable",
            }[case]
        )
        assert detail.input_tokens is detail.output_tokens is detail.cost is None


@pytest.mark.parametrize("capture", ["full", "minimal"])
@pytest.mark.asyncio
async def test_empty_run_and_in_process_cli_publish_the_narrow_companion(
    tmp_path: Path, capture: str
) -> None:
    spec = native_spec(tmp_path, sources=0, capture=capture)
    store = tmp_path / "store"
    with prepare_run(spec, store=store, run_id="001"):
        pass
    adapter = MockDecisionsAdapter(store / "mock-service.sqlite3")
    await execute_run("001", store=store, adapter=adapter, spec=spec)
    result = CliRunner().invoke(
        app, ["results", "provenance", "--run", "001", "--store", str(store), "--json"]
    )
    assert result.exit_code == 0, result.output
    envelope = json.loads(result.output)
    assert envelope["schema_version"] == 1 and envelope["status"] == "ok"
    bundle = export_decisions_provenance("001", store=store)
    assert all(f.rows == 0 for f in bundle.manifest.files)
    assert bundle.manifest.totals.usage_evidence == "not_applicable"
    assert adapter.forward_entries == 0


@pytest.mark.parametrize(
    "case",
    ["bad_id", "missing", "noncanonical", "snapshot", "file_metadata", "file_bytes"],
)
@pytest.mark.asyncio
async def test_bundle_reader_rejects_changed_manifest_or_companion(
    tmp_path: Path, case: str
) -> None:
    spec = native_spec(tmp_path)
    store = tmp_path / "store"
    with prepare_run(spec, store=store, run_id="001"):
        pass
    await execute_run(
        "001", store=store, adapter=MockDecisionsAdapter(store / "mock-service.sqlite3")
    )
    bundle = export_decisions_provenance("001", store=store)
    path = Path(bundle.manifest_path)
    with Repository(store) as repository:
        artifacts = ArtifactStore(repository.run_directory("001"), create=False)
        if case == "bad_id":
            with pytest.raises(ValueError):
                read_decisions_provenance(repository, "001", "invalid")
            return
        if case == "missing":
            assert read_decisions_provenance(repository, "001", "f" * 64) is None
            return
        if case == "file_bytes":
            file = bundle.manifest.files[0]
            assert file.artifact.relative_path is not None
            (artifacts.root / file.artifact.relative_path).write_bytes(b"corrupt")
        else:
            data = bundle.manifest.model_dump(mode="json")
            if case == "snapshot":
                data["event_high_water_mark"] += 1
            elif case == "file_metadata":
                data["files"][0]["artifact"]["media_type"] = "application/foreign"
            raw = canonical_bytes(data) + (b"\n" if case == "noncanonical" else b"")
            if case == "file_metadata":
                repository.register_artifact(
                    "001", artifacts.put(raw, "application/json")
                )
            path.write_bytes(raw)
        with pytest.raises((IntegrityError, ArtifactIntegrityError)):
            read_decisions_provenance(repository, "001", bundle.bundle_id)


@pytest.mark.parametrize("case", ["entry", "noncanonical", "identity"])
def test_status_discovers_only_complete_immutable_bundle_references(
    tmp_path: Path, case: str
) -> None:
    spec = native_spec(tmp_path)
    store = tmp_path / "store"
    with prepare_run(spec, store=store, run_id="001") as prepared:
        assert isinstance(prepared.manifest, ExecutableRunManifestV8)
        manifest = prepared.manifest
    bundle = export_decisions_provenance("001", store=store)
    reference = Path(bundle.manifest_path)
    if case == "entry":
        (reference.parent / "unpublished").write_text("not a manifest")
    elif case == "noncanonical":
        reference.write_bytes(reference.read_bytes() + b"\n")
    else:
        data = bundle.manifest.model_dump(mode="json")
        data["source_manifest_sha256"] = "f" * 64
        reference.write_bytes(canonical_bytes(data))
    with Repository(store) as repository:
        with pytest.raises(IntegrityError):
            provenance.decisions_status(repository, manifest)


@pytest.mark.asyncio
async def test_missing_attribution_cannot_be_backfilled_from_successful_results(
    tmp_path: Path,
) -> None:
    spec = native_spec(tmp_path)
    store = tmp_path / "store"
    with prepare_run(spec, store=store, run_id="001"):
        pass
    await execute_run(
        "001", store=store, adapter=MockDecisionsAdapter(store / "mock-service.sqlite3")
    )
    with Repository(store) as repository:
        submission = next(repository.iter_submissions("001"))
        artifacts = ArtifactStore(repository.run_directory("001"), create=False)
        assert read_outcomes(artifacts, submission.submission_id) is not None
        artifacts.reference_path(outcome_reference(submission.submission_id)).unlink()
    with pytest.raises(IntegrityError, match="lacks named outcomes"):
        export_decisions_provenance("001", store=store)


def test_historical_normalized_export_is_not_a_native_companion(tmp_path: Path) -> None:
    spec = specification(tmp_path / "inputs", responses=1, traits=1)
    store = tmp_path / "store"
    with prepare_run(spec, store=store, run_id="old"):
        pass
    with pytest.raises(ValueError, match="frozen V4"):
        export_decisions_provenance("old", store=store)
    assert get_run("old", store=store).model_dump().get("decisions") is None


def test_companion_authorizes_derived_empty_bytes_without_upgrading_the_source(
    tmp_path: Path,
) -> None:
    spec = native_spec(tmp_path, sources=0, capture="minimal")
    store = tmp_path / "store"
    with prepare_run(spec, store=store, run_id="001") as prepared:
        original = prepared.repository.get_artifact("001", bytes_digest(b""))
        assert original.relative_path is None
    bundle = export_decisions_provenance("001", store=store)
    assert bundle == export_decisions_provenance("001", store=store)
    with Repository(store) as repository:
        assert read_decisions_provenance(repository, "001", bundle.bundle_id) == bundle
        assert repository.get_artifact("001", bytes_digest(b"")) == original
        for file in bundle.manifest.files:
            if file.name.endswith(".jsonl"):
                assert (
                    file.artifact.relative_path
                    == f"objects/{original.sha256[:2]}/{original.sha256}"
                )
                assert file.artifact.byte_length == file.rows == 0
                assert (
                    ArtifactStore(repository.run_directory("001"), create=False).read(
                        file.artifact
                    )
                    == b""
                )
                with pytest.raises(IntegrityError, match="registered differently"):
                    repository.register_artifact("001", file.artifact)
        assert (
            repository.connection.execute("SELECT count(*) FROM exports").fetchone()[0]
            == 0
        )


@pytest.mark.parametrize("capture", ["full", "minimal"])
@pytest.mark.parametrize(
    "fault",
    ["path", "null_path", "length", "bytes", "media", "unregistered", "snapshot"],
)
def test_empty_derived_publication_revalidates_manifest_location_metadata_and_bytes(
    tmp_path: Path, capture: str, fault: str
) -> None:
    spec = native_spec(tmp_path, sources=0, capture=capture)
    store = tmp_path / "store"
    with prepare_run(spec, store=store, run_id="001"):
        pass
    bundle = export_decisions_provenance("001", store=store)
    with Repository(store) as repository:
        artifacts = ArtifactStore(repository.run_directory("001"), create=False)
        if fault == "bytes":
            file = bundle.manifest.files[0].artifact
            assert file.relative_path is not None
            (artifacts.root / file.relative_path).write_bytes(b"corrupt")
        else:
            data = bundle.manifest.model_dump(mode="json")
            file = data["files"][0]["artifact"]
            if fault == "path":
                file["relative_path"] = "objects/aa/" + file["sha256"]
            elif fault == "null_path":
                file["relative_path"] = None
            elif fault == "length":
                file["byte_length"] = 1
            elif fault == "media":
                file["media_type"] = "application/foreign"
            elif fault == "unregistered":
                file["sha256"] = "f" * 64
                file["relative_path"] = "objects/ff/" + "f" * 64
            else:
                data["result_high_water_mark"] += 1
            raw = canonical_bytes(data)
            repository.register_artifact("001", artifacts.put(raw, "application/json"))
            Path(bundle.manifest_path).write_bytes(raw)
        with pytest.raises((IntegrityError, ArtifactIntegrityError, NotFoundError)):
            read_decisions_provenance(repository, "001", bundle.bundle_id)


def test_derived_publication_rejects_conflicting_registered_byte_length(
    tmp_path: Path,
) -> None:
    spec = native_spec(tmp_path, sources=0, capture="minimal")
    store = tmp_path / "store"
    with prepare_run(spec, store=store, run_id="001") as prepared:
        connection = prepared.repository.connection
        trigger = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='artifacts_no_update'"
        ).fetchone()[0]
        # Model an already-corrupt catalog; restore the exact guard before reopen.
        connection.execute("DROP TRIGGER artifacts_no_update")
        try:
            connection.execute(
                "UPDATE artifacts SET byte_length=1 WHERE sha256=?",
                (bytes_digest(b""),),
            )
        finally:
            connection.execute(trigger)
    with pytest.raises(IntegrityError, match="conflicting byte length"):
        export_decisions_provenance("001", store=store)
    with Repository(store) as repository:
        assert not list(
            (repository.run_directory("001") / "decisions-provenance").glob("*.json")
        )


@pytest.mark.parametrize(
    "case", ["valid", "payload", "identity", "unregistered", "absent"]
)
@pytest.mark.asyncio
async def test_uncertain_forwarding_totals_require_registered_bound_evidence(
    tmp_path: Path, case: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = native_spec(tmp_path)
    store = tmp_path / "store"
    with prepare_run(spec, store=store, run_id="001") as prepared:
        assert isinstance(prepared.manifest, ExecutableRunManifestV8)
        manifest = prepared.manifest
    adapter = MockDecisionsAdapter(
        store / "mock-service.sqlite3",
        scenario=DecisionsScenarioV1(transport="timeout_after"),
        reconciliation_available=False,
    )
    if case == "absent":
        session = ExecutionSession("001", store, adapter, spec, False, None)
        try:
            work = session.next_work()
            assert work is not None and session.boundary(work)
            session.uncertain(work)
        finally:
            session.close()
    else:
        await execute_run("001", store=store, adapter=adapter)
    with Repository(store) as repository:
        submission = next(repository.iter_submissions("001"))
        artifacts = ArtifactStore(repository.run_directory("001"), create=False)
        fact = read_invocation(artifacts, submission.submission_id)
        assert (fact is None) == (case == "absent")
        if case not in {"valid", "absent"}:
            assert fact is not None
            if case == "identity":
                identity = physical_identity(
                    "foreign", fact.identity.request_uid, fact.identity.attempt_id
                )
                usage = fact.usage.model_copy(
                    update={
                        "run_id": "foreign",
                        "submission_id": identity.submission_id,
                    }
                )
                fact = fact.model_copy(update={"identity": identity, "usage": usage})

                def foreign_reader(*_args: Any) -> Any:
                    return fact

                monkeypatch.setattr(provenance, "read_invocation", foreign_reader)
            elif case == "payload":
                fact = fact.model_copy(update={"payload_sha256": "f" * 64})
            else:
                fact = fact.model_copy(
                    update={
                        "usage": fact.usage.model_copy(
                            update={"reported_model": "unregistered"}
                        )
                    }
                )
            raw = canonical_bytes(fact)
            artifact = artifacts.put(raw, "application/json")
            if case != "unregistered":
                repository.register_artifact("001", artifact)
            if case != "identity":
                artifacts.reference_path(
                    invocation_reference(submission.submission_id)
                ).write_bytes(raw)
        if case in {"identity", "payload"}:
            with pytest.raises(IntegrityError, match="forwarding"):
                provenance.decisions_status(repository, manifest)
            with pytest.raises(IntegrityError, match="forwarding"):
                list(provenance.physical_rows(repository, manifest, 0))
        else:
            detail = provenance.decisions_status(repository, manifest)
            assert detail.known_invocations == (1 if case == "valid" else 0)
            assert detail.invocation_unknown == (0 if case == "valid" else 1)
            assert detail.input_tokens is detail.output_tokens is None
            if case == "valid":
                rows = list(provenance.physical_rows(repository, manifest, 0))
                assert len(rows) == 1 and rows[0].usage_selection == "forwarding_fact"
        assert adapter.forward_entries == (0 if case == "absent" else 1)
        assert repository.summary("001").results == 0


@pytest.mark.asyncio
async def test_export_rejects_changed_current_commitment_and_outcome_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = native_spec(tmp_path)
    store = tmp_path / "store"
    with prepare_run(spec, store=store, run_id="001") as prepared:
        assert isinstance(prepared.manifest, ExecutableRunManifestV8)
        manifest = prepared.manifest
    await execute_run(
        "001", store=store, adapter=MockDecisionsAdapter(store / "mock-service.sqlite3")
    )
    with Repository(store) as repository:
        physical = next(provenance.physical_rows(repository, manifest, 1_000))
        with pytest.raises(IntegrityError, match="snapshot"):
            list(provenance._question_rows(repository, manifest, physical, 0))  # pyright: ignore[reportPrivateUsage]
        artifacts = ArtifactStore(repository.run_directory("001"), create=False)
        vector = read_outcomes(artifacts, physical.identity.submission_id)
        assert vector is not None
        wrong = vector.model_copy(update={"commitment_sha256": "f" * 64})
        raw = canonical_bytes(wrong)
        repository.register_artifact("001", artifacts.put(raw, "application/json"))
        artifacts.reference_path(
            outcome_reference(physical.identity.submission_id)
        ).write_bytes(raw)
        with pytest.raises(IntegrityError, match="attribution"):
            list(provenance._question_rows(repository, manifest, physical, 1_000))  # pyright: ignore[reportPrivateUsage]

        def missing(*_args: Any) -> None:
            return None

        monkeypatch.setattr(provenance, "read_transport_commitment", missing)
        with pytest.raises(IntegrityError, match="commitment"):
            list(provenance.physical_rows(repository, manifest, 1_000))


def test_empty_publication_directory_is_not_a_published_bundle(tmp_path: Path) -> None:
    spec = native_spec(tmp_path)
    with prepare_run(spec, store=tmp_path / "store", run_id="001") as prepared:
        assert isinstance(prepared.manifest, ExecutableRunManifestV8)
        (prepared.store.root / "decisions-provenance").mkdir()
        assert (
            provenance.decisions_status(
                prepared.repository, prepared.manifest
            ).provenance_manifest
            is None
        )


@pytest.mark.asyncio
async def test_definitive_nonacceptance_exports_failed_members_without_fabricated_answers(
    tmp_path: Path,
) -> None:
    spec = native_spec(tmp_path)
    store = tmp_path / "store"
    with prepare_run(spec, store=store, run_id="001"):
        pass
    adapter = MockDecisionsAdapter(
        store / "mock-service.sqlite3",
        scenario=DecisionsScenarioV1(transport="not_submitted"),
    )
    await execute_run("001", store=store, adapter=adapter)
    bundle = export_decisions_provenance("001", store=store)
    questions = next(f for f in bundle.manifest.files if f.name == "questions.jsonl")
    with Repository(store) as repository:
        artifacts = ArtifactStore(repository.run_directory("001"), create=False)
        rows = [
            json.loads(line) for line in artifacts.read(questions.artifact).splitlines()
        ]
    assert len(rows) == 3 and all(
        r["answer"] is None and r["status"] == "errored" for r in rows
    )
    assert bundle.manifest.totals.known_invocations == adapter.forward_entries == 1
    assert adapter.ledger.acceptance_count() == 0


@pytest.mark.parametrize("fault", ["hash", "identity"])
@pytest.mark.asyncio
async def test_status_revalidates_the_selected_usage_revision(
    tmp_path: Path, fault: str
) -> None:
    spec = native_spec(tmp_path)
    store = tmp_path / "store"
    with prepare_run(spec, store=store, run_id="001") as prepared:
        assert isinstance(prepared.manifest, ExecutableRunManifestV8)
        manifest = prepared.manifest
    await execute_run(
        "001", store=store, adapter=MockDecisionsAdapter(store / "mock-service.sqlite3")
    )
    with Repository(store) as repository:
        row = dict(
            repository.connection.execute("SELECT * FROM usage_revisions").fetchone()
        )
        usage = json.loads(row["usage_json"])
        if fault == "identity":
            usage["request_uid"] = "foreign"
        raw = canonical_bytes(usage)
        # A syntactically valid catalog row still has to bind its selected content.
        row.update(
            revision_seq=row["revision_seq"] + 1,
            revision_id="corrupt-fixture",
            revision_number=1,
            source="synthetic.authoritative",
            usage_json=raw.decode(),
            usage_sha256="f" * 64 if fault == "hash" else bytes_digest(raw),
        )
        repository.connection.execute(
            "INSERT INTO usage_revisions VALUES (?,?,?,?,?,?,?,?,?,?)",
            tuple(row.values()),
        )
        with pytest.raises(IntegrityError, match="selected usage"):
            provenance.decisions_status(repository, manifest)


@pytest.mark.asyncio
async def test_status_selects_latest_complete_bundle_independent_of_directory_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = native_spec(tmp_path)
    store = tmp_path / "store"
    with prepare_run(spec, store=store, run_id="001") as prepared:
        assert isinstance(prepared.manifest, ExecutableRunManifestV8)
        manifest = prepared.manifest
    first = export_decisions_provenance("001", store=store)
    await execute_run(
        "001", store=store, adapter=MockDecisionsAdapter(store / "mock-service.sqlite3")
    )
    latest = export_decisions_provenance("001", store=store)
    assert first.bundle_id != latest.bundle_id
    original = provenance.os.scandir
    directory = Path(latest.manifest_path).parent
    with original(directory) as entries:
        real_entries = list(entries)
    ordered = sorted(
        real_entries,
        key=lambda e: e.name == Path(latest.manifest_path).name,
        reverse=True,
    )

    @contextmanager
    def descending(path: Any) -> Any:
        if Path(path) == directory:
            yield iter(ordered)
        else:
            with original(path) as entries:
                yield entries

    monkeypatch.setattr(provenance.os, "scandir", descending)
    with Repository(store) as repository:
        detail = provenance.decisions_status(repository, manifest)
    assert detail.provenance_manifest == latest.manifest_path
