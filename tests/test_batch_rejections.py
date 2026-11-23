"""Corrupt durable batch evidence must fail before changing scientific state."""

from pathlib import Path

import pytest
from test_native_batch import batch_spec

from judgmentllm import RunSpecV2, prepare_batch, stop_batch
from judgmentllm.batch_models import BatchMemberV1, BatchStopV1
from judgmentllm.persistence.artifacts import ArtifactStore
from judgmentllm.persistence.hashing import canonical_bytes
from judgmentllm.persistence.models import IntegrityError
from judgmentllm.persistence.native_batch import (
    latest,
    put,
    read_reference,
    register,
    restore_stops,
    rows,
)
from judgmentllm.persistence.repository import Repository


@pytest.fixture
def prepared_batch(tmp_path: Path) -> tuple[RunSpecV2, Path]:
    spec = batch_spec(tmp_path, responses=1)
    root = tmp_path / "store"
    prepare_batch(spec, store=root, run_id="001")
    return spec, root


def test_registered_batch_artifact_cannot_change_its_length(
    prepared_batch: tuple[RunSpecV2, Path],
) -> None:
    _, root = prepared_batch
    with Repository(root) as repository:
        artifact = put(repository, "001", BatchStopV1(run_id="001", kind="stop"))
        register(repository, "001", artifact)
        with pytest.raises(IntegrityError, match="registration conflicts"):
            register(
                repository,
                "001",
                artifact.model_copy(update={"byte_length": artifact.byte_length + 1}),
            )
        assert repository.get_artifact("001", artifact.sha256) == artifact


@pytest.mark.parametrize("fault", ["symlink", "oversized", "noncanonical"])
def test_metadata_reference_rejects_unsafe_files_before_acceptance(
    prepared_batch: tuple[RunSpecV2, Path],
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    _, root = prepared_batch
    with Repository(root) as repository:
        command = BatchStopV1(run_id="001", kind="stop")
        put(repository, "001", command, reference="batch/stop.json")
        path = ArtifactStore(
            repository.run_directory("001"), create=False
        ).reference_path("batch/stop.json")
        if fault == "symlink":
            original = Path.is_symlink

            def is_symlink(value: Path) -> bool:
                return value == path or original(value)

            def reference_path(store: ArtifactStore, reference: str) -> Path:
                return path

            # Check the receipt guard independently of ArtifactStore's path guard.
            monkeypatch.setattr(ArtifactStore, "reference_path", reference_path)
            monkeypatch.setattr(Path, "is_symlink", is_symlink)
        else:
            path.write_bytes(
                b" " * 4_194_305
                if fault == "oversized"
                else canonical_bytes(command) + b"\n"
            )
        with pytest.raises(IntegrityError):
            read_reference(repository, "001", "batch/stop.json", BatchStopV1)
        assert (
            repository.connection.execute(
                "SELECT count(*) FROM batch_facts"
            ).fetchone()[0]
            == 0
        )


@pytest.mark.parametrize("kind", ["fact", "member"])
def test_stored_records_are_checked_against_their_immutable_receipts(
    prepared_batch: tuple[RunSpecV2, Path],
    kind: str,
) -> None:
    _, root = prepared_batch
    if kind == "fact":
        stop_batch("001", store=root)
    with Repository(root) as repository:
        connection = repository.connection
        table = "batch_facts" if kind == "fact" else "batch_prepared_members"
        connection.execute("BEGIN")
        try:
            # Model an externally damaged database, then restore its original bytes.
            connection.execute(f"DROP TRIGGER {table}_immutable_update")
            if kind == "fact":
                connection.execute(
                    "UPDATE batch_facts SET record_json=json_set(record_json,'$.run_id','other')"
                )
                with pytest.raises(IntegrityError, match="fact identity"):
                    latest(repository, "001", "stop", "001", BatchStopV1)
            else:
                connection.execute(
                    "UPDATE batch_prepared_members SET record_json=json_set(record_json,'$.position',7)"
                )
                with pytest.raises(IntegrityError, match="preparation receipt"):
                    tuple(
                        rows(repository, "batch_prepared_members", "001", BatchMemberV1)
                    )
        finally:
            connection.execute("ROLLBACK")


def test_stop_reference_cannot_authorize_a_different_run(
    prepared_batch: tuple[RunSpecV2, Path],
) -> None:
    _, root = prepared_batch
    with Repository(root) as repository:
        before = repository.get_run("001")
        put(
            repository,
            "001",
            BatchStopV1(run_id="other", kind="stop"),
            reference="batch/stop.json",
        )
        with pytest.raises(IntegrityError, match="conflicts with its run"):
            restore_stops(repository, "001", "unused-owner")
        assert repository.get_run("001") == before


@pytest.mark.parametrize("provided_mock", [False, True])
def test_general_resume_dispatches_the_frozen_native_batch(
    prepared_batch: tuple[RunSpecV2, Path],
    provided_mock: bool,
) -> None:
    from judgmentllm import MockExecutionOptions, resume_run

    spec, root = prepared_batch
    result = resume_run(
        "001",
        store=root,
        spec=spec,
        mock=MockExecutionOptions() if provided_mock else None,
    )
    assert result.outcome == "ok" and result.run_status == "completed"
    with Repository(root) as repository:
        assert repository.summary("001").results == 1
