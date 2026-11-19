"""Exact historical store/schema/serialization dispatch and scoped SQL guards."""

from __future__ import annotations

import base64
import io
import json
import sqlite3
import tarfile
from contextlib import closing
from pathlib import Path
from typing import Literal

import pytest
from test_decisions_lifecycle import native_spec
from test_persistence_schema import insert_result, observe, prepare, result_record

from judgmentllm.cli_controls import ControlEnvelope, _parts  # pyright: ignore[reportPrivateUsage]
from judgmentllm.lifecycle import export_run_results, get_run, list_runs
from judgmentllm.lifecycle_models import RunPage, RunStatus, RunSummary
from judgmentllm.persistence import schema
from judgmentllm.persistence.hashing import bytes_digest, canonical_bytes
from judgmentllm.persistence.models import IntegrityError
from judgmentllm.persistence.repository import ReadOnlyRepository, Repository
from judgmentllm.preparation import open_prepared_run, prepare_run
from scripts.version_compatibility_fixture import extract_fixture


@pytest.mark.parametrize("version", [1, 2, 3])
def test_unmarked_response_backed_refusal_retains_historical_sql_rule(
    tmp_path: Path, version: int
) -> None:
    statements = (
        schema._LEGACY_STATEMENTS  # pyright: ignore[reportPrivateUsage]
        if version == 1
        else schema._V2_STATEMENTS  # pyright: ignore[reportPrivateUsage]
        if version == 2
        else schema._STATEMENTS  # pyright: ignore[reportPrivateUsage]
    )  # pyright: ignore[reportPrivateUsage]
    with closing(
        sqlite3.connect(tmp_path / "direct.sqlite3", isolation_level=None)
    ) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        for statement in statements:
            connection.execute(statement)
        prepare(connection)
        observe(connection)
        with pytest.raises(sqlite3.IntegrityError, match="established outcome"):
            insert_result(connection, result_record("refused", "provider_refusal"))
        insert_result(connection, result_record())
        assert connection.execute("SELECT count(*) FROM results").fetchone()[0] == 1


@pytest.mark.parametrize(
    "violation",
    [
        "wrong_attempt",
        "wrong_observation",
        "unaccepted",
        "wrong_error",
        "not_refused",
        "wrong_membership",
    ],
)
def test_marked_refusal_does_not_bypass_existing_evidence_guards(
    tmp_path: Path, violation: str
) -> None:
    with closing(
        sqlite3.connect(tmp_path / "direct.sqlite3", isolation_level=None)
    ) as connection:
        schema.initialize(connection)
        # The marker exists in the same INSERT that establishes the run.
        connection.execute(
            "INSERT INTO runs(run_id,task_kind,provider,endpoint,model,capture,created_at,decisions_version) VALUES('run','rubric','synthetic','test','test','full','2026-11-02',4)"
        )
        # Reuse the original direct SQL setup after its independent run INSERT.
        from test_persistence_schema import DIGEST, start_submission

        connection.execute(
            "INSERT INTO artifacts VALUES ('run',?,1,'application/json',?)",
            (DIGEST, f"objects/aa/{DIGEST}"),
        )
        connection.execute(
            "UPDATE runs SET state='submitted',manifest_sha256=? WHERE run_id='run'",
            (DIGEST,),
        )
        connection.execute(
            "INSERT INTO tasks(run_id,task_id,trait_id,item_id,scheduling_ordinal) VALUES('run','task','quality','001',0)"
        )
        connection.execute(
            "INSERT INTO requests(run_id,request_uid,scheduling_ordinal,dependency_sha256) VALUES('run','request',0,?)",
            (DIGEST,),
        )
        connection.execute("INSERT INTO request_tasks VALUES('run','request','task',0)")
        connection.execute("UPDATE requests SET membership_sealed=1")
        connection.execute(
            "INSERT INTO attempts(run_id,attempt_id,request_uid,attempt_number,prompt_sha256,payload_sha256,membership_sha256) VALUES('run','attempt','request',1,?,?,?)",
            (DIGEST, DIGEST, DIGEST),
        )
        connection.execute(
            "INSERT INTO attempt_tasks VALUES('run','attempt','request','task',0)"
        )
        connection.execute("UPDATE attempts SET membership_sealed=1")
        connection.execute(
            "INSERT INTO submissions(run_id,submission_id,correlation_id,attempt_id,payload_sha256) VALUES('run','submission','correlation','attempt',?)",
            (DIGEST,),
        )
        if violation == "unaccepted":
            start_submission(connection)
        else:
            observe(connection)
        result = result_record(
            "refused", "wrong" if violation == "wrong_error" else "provider_refusal"
        )
        if violation == "wrong_attempt":
            result = result.model_copy(update={"attempt_id": "foreign"})
        elif violation == "wrong_membership":
            result = result.model_copy(update={"task_id": "foreign"})
        elif violation == "not_refused":
            result = result_record("errored", "provider_refusal")
        with pytest.raises(sqlite3.IntegrityError):
            insert_result(
                connection,
                result,
                observation_id="foreign"
                if violation in {"wrong_observation", "unaccepted"}
                else "observation",
            )
        assert connection.execute("SELECT count(*) FROM results").fetchone()[0] == 0


@pytest.mark.parametrize("version", [1, 2])
def test_genuine_historical_store_inspection_export_and_v4_rejection_do_not_migrate(
    tmp_path: Path, version: int
) -> None:
    fixture = Path(__file__).parent / "fixtures"
    if version == 1:
        metadata = extract_fixture(
            fixture / "a2-compatibility.tar.gz",
            fixture / "a2-compatibility.json",
            tmp_path / "old",
        )
        store = tmp_path / "old/store"
        run_id = metadata["producer"]["run_id"]
    else:
        raw = base64.b64decode(
            (
                fixture / "presentation-history/portable/store.tar.gz.base64"
            ).read_bytes(),
            validate=True,
        )
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
            archive.extractall(tmp_path / "old", filter="data")
        store = tmp_path / "old/store"
        run_id = "v2-recovery"
    with ReadOnlyRepository(store) as repository:
        assert (
            repository.connection.execute("PRAGMA user_version").fetchone()[0]
            == version
        )
        sql_before = list(
            repository.connection.execute(
                "SELECT type,name,sql FROM sqlite_master ORDER BY type,name"
            )
        )
        migration_before = list(
            repository.connection.execute("SELECT * FROM schema_migrations")
        )
        manifest = repository.get_manifest(run_id)
        before_manifest = repository.get_artifact(run_id, manifest.artifact.sha256)
        before_counts = tuple(
            repository.connection.execute(
                "SELECT (SELECT count(*) FROM runs),(SELECT count(*) FROM tasks),(SELECT count(*) FROM requests),(SELECT count(*) FROM attempts),(SELECT count(*) FROM observations),(SELECT count(*) FROM results)"
            ).fetchone()
        )
    status = get_run(run_id, store=store)
    assert "decisions" not in status.model_dump(mode="json")
    assert all("decisions" not in r.model_dump() for r in list_runs(store=store).runs)
    # Explicit snapshot permission reopens the genuine historical prepared run;
    # its authorization is separate from migration/backfill of old evidence.
    with open_prepared_run(run_id, store=store, use_frozen_snapshot=True) as prepared:
        assert prepared.manifest.schema_version in {1, 2}
    combinations: tuple[
        tuple[Literal["jsonl", "csv"], Literal["canonical", "r"]], ...
    ] = (("jsonl", "canonical"), ("csv", "canonical"), ("csv", "r"))
    for format_, view in combinations:
        exported = export_run_results(run_id, store=store, format=format_, view=view)
        assert Path(exported.files[0].path).is_file()
    candidate = native_spec(tmp_path / "new")
    with pytest.raises(IntegrityError, match="schema-3"):
        prepare_run(candidate, store=store, run_id="new-v4")
    from test_bulk_live import bulk_spec

    for decisions in (False, True):
        with pytest.raises(IntegrityError, match="schema-3"):
            prepare_run(
                bulk_spec(tmp_path / f"bulk-{decisions}", decisions=decisions),
                store=store,
                run_id=f"new-bulk-{decisions}",
            )
    with ReadOnlyRepository(store) as repository:
        assert (
            repository.connection.execute("PRAGMA user_version").fetchone()[0]
            == version
        )
        assert [
            tuple(r)
            for r in repository.connection.execute(
                "SELECT type,name,sql FROM sqlite_master ORDER BY type,name"
            )
        ] == [tuple(r) for r in sql_before]
        assert [
            tuple(r)
            for r in repository.connection.execute("SELECT * FROM schema_migrations")
        ] == [tuple(r) for r in migration_before]
        assert (
            repository.get_artifact(run_id, manifest.artifact.sha256) == before_manifest
        )
        assert (
            tuple(
                repository.connection.execute(
                    "SELECT (SELECT count(*) FROM runs),(SELECT count(*) FROM tasks),(SELECT count(*) FROM requests),(SELECT count(*) FROM attempts),(SELECT count(*) FROM observations),(SELECT count(*) FROM results)"
                ).fetchone()
            )
            == before_counts
        )
        assert (
            repository.connection.execute(
                "SELECT count(*) FROM sqlite_master WHERE name LIKE 'presentation_%'"
            ).fetchone()[0]
            == 0
        )


def test_schema2_statement_checksum_remains_exact() -> None:
    assert (
        schema.SCHEMA_2_CHECKSUM
        == "cf01fd1959c4f930385c8c7947a9cdde38ee1b8fd6672d0b056151b26f4a3db3"
    )
    assert (
        bytes_digest(
            "\n".join(schema._V2_STATEMENTS).encode()  # pyright: ignore[reportPrivateUsage]
        )
        == schema.SCHEMA_2_CHECKSUM
    )  # pyright: ignore[reportPrivateUsage]
    assert (
        schema._V3_RESULT_EVIDENCE.replace(  # pyright: ignore[reportPrivateUsage]
            """(o.kind='response' AND (NEW.status IN ('complete','invalid')
                OR (NEW.status='refused'
                  AND json_extract(NEW.record_json,'$.error_kind')='provider_refusal'
                  AND EXISTS(SELECT 1 FROM runs r WHERE r.run_id=NEW.run_id
                    AND r.decisions_version=4))))""",
            "(o.kind='response' AND NEW.status IN ('complete','invalid'))",
        )
        == schema._V2_RESULT_EVIDENCE  # pyright: ignore[reportPrivateUsage]
    )  # pyright: ignore[reportPrivateUsage]


def test_old_status_listing_summary_and_cli_envelope_bytes_are_exact() -> None:
    raw = (
        (Path(__file__).parent / "fixtures/decisions/legacy-status-v1.json")
        .read_text(encoding="utf-8")
        .strip()
    )
    status = RunStatus.model_validate_json(raw)
    assert status.model_dump_json() == raw
    assert (
        status.usage.input_tokens
        is status.usage.output_tokens
        is status.usage.cost
        is None
    )
    assert status.requests == status.attempts == 0
    page = RunPage(runs=(status,), next_cursor=None)
    assert (
        page.model_dump_json()
        == '{"schema_version":1,"runs":[' + raw + '],"next_cursor":null}'
    )
    summary = RunSummary.model_validate({**status.model_dump(), "outcome": "ok"})
    assert (
        summary.model_dump_json()
        == raw[:-1] + ',"outcome":"ok","retry_operation_id":null}'
    )
    run_id, state, artifacts, data = _parts(status)
    envelope = ControlEnvelope(
        command="status",
        status="ok",
        run_id=run_id,
        run_status=state,
        artifacts=artifacts,
        data=data,
    )
    expected = json.loads(raw)
    for key in ("schema_version", "run_id", "run_status", "artifacts"):
        expected.pop(key)
    expected_envelope = dict(
        schema_version=1,
        command="status",
        status="ok",
        run_id="001",
        run_status="completed",
        artifacts=status.artifacts.model_dump(),
        data=expected,
        error=None,
    )
    assert envelope.model_dump_json() == json.dumps(
        expected_envelope, ensure_ascii=False, separators=(",", ":")
    )
    assert b'"decisions"' not in canonical_bytes(envelope)


def test_marker_mismatch_fails_seal_and_reopen_without_retrofit(tmp_path: Path) -> None:
    from judgmentllm.persistence.artifacts import ArtifactStore
    from judgmentllm.persistence.models import ManifestReference, RunRecord
    from judgmentllm.preparation import PreparationError

    spec = native_spec(tmp_path)
    with prepare_run(spec, store=tmp_path / "native", run_id="001") as prepared:
        raw = canonical_bytes(prepared.manifest)
    with Repository(tmp_path / "unmarked") as repository:
        repository.create_run(
            RunRecord(
                run_id="001",
                task_kind="rubric",
                provider="mock",
                endpoint="synthetic",
                model=spec.model,
            )
        )
        artifacts = ArtifactStore(repository.run_directory("001"))
        identity = artifacts.put(raw, "application/json")
        repository.register_artifact("001", identity)
        artifacts.publish_manifest(identity)
        with pytest.raises(IntegrityError, match="marker"):
            repository.seal_run("001", ManifestReference(artifact=identity))
        assert (
            repository.decisions_version("001") is None
            and repository.get_run("001").state == "created"
        )
    with pytest.raises(PreparationError, match="marker"):
        open_prepared_run("001", store=tmp_path / "unmarked")
    with Repository(tmp_path / "marked") as repository:
        repository.create_run(
            RunRecord(
                run_id="001",
                task_kind="rubric",
                provider="mock",
                endpoint="synthetic",
                model=spec.model,
            ),
            decisions_version=4,
        )
        artifacts = ArtifactStore(repository.run_directory("001"))
        identity = artifacts.put(b"{}", "application/json")
        with pytest.raises(IntegrityError, match="marker"):
            repository.seal_run("001", ManifestReference(artifact=identity))


def test_run_and_decisions_marker_are_created_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from judgmentllm.persistence.models import RunRecord

    with Repository(tmp_path) as repository:

        def fail(*_args: object, **_kwargs: object) -> int:
            raise RuntimeError("synthetic created-event failure")

        monkeypatch.setattr(repository, "_event", fail)
        with pytest.raises(RuntimeError):
            repository.create_run(
                RunRecord(
                    run_id="001",
                    task_kind="rubric",
                    provider="mock",
                    endpoint="synthetic",
                    model="offline",
                ),
                decisions_version=4,
            )
        assert (
            repository.connection.execute("SELECT count(*) FROM runs").fetchone()[0]
            == 0
        )


@pytest.mark.parametrize("capture", ["full", "minimal"])
def test_native_seal_requires_the_exact_frozen_v4_specification(
    tmp_path: Path, capture: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import judgmentllm.preparation as preparation
    from judgmentllm.config import RunSpecV4
    from judgmentllm.persistence.models import ManifestReference

    spec = native_spec(tmp_path, capture=capture)
    store = tmp_path / "store"

    def interrupt(stage: str, *_args: object) -> None:
        if stage == "manifest_published":
            raise RuntimeError("synthetic pre-seal interruption")

    with monkeypatch.context() as patch:
        patch.setattr(preparation, "_checkpoint", interrupt)
        with pytest.raises(RuntimeError, match="pre-seal"):
            prepare_run(spec, store=store, run_id="001")
    with Repository(store) as repository:
        manifest = preparation._load_manifest(repository, "001", unsealed=True)  # pyright: ignore[reportPrivateUsage]
        identity = repository.get_artifact(
            "001", bytes_digest(canonical_bytes(manifest))
        )
        reference = ManifestReference(artifact=identity)
        with pytest.raises(IntegrityError, match="RunSpec V4"):
            repository.seal_run("001", reference)
        changed = RunSpecV4.model_validate({**spec.model_dump(), "seed": 99})
        with pytest.raises(IntegrityError, match="RunSpec V4"):
            repository.seal_run("001", reference, decisions_spec=changed)
        assert repository.get_run("001").state == "created"
        assert repository.get_run("001").manifest_sha256 is None
        assert repository.decisions_version("001") == 4
    # Recovery performs the same binding before finishing this published seal.
    with open_prepared_run("001", store=store, spec=spec) as prepared:
        assert prepared.repository.get_run("001").state == "submitted"
        assert prepared.repository.decisions_version("001") == 4
