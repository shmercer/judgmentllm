"""Immutable generative bulk-live provenance, streamed from one read cut."""

from __future__ import annotations

import csv
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Literal

from pydantic import Field

from judgmentllm.decisions.provenance import CompanionFileV1
from judgmentllm.evidence.usage import ProviderUsage
from judgmentllm.models import ID
from judgmentllm.persistence.artifacts import ArtifactStore
from judgmentllm.persistence.bulk import (
    CommittedBulkReservation,
    ReservationRows,
    committed_reservations,
    event_high_water,
)
from judgmentllm.persistence.hashing import (
    bytes_digest,
    canonical_bytes,
    semantic_digest,
)
from judgmentllm.persistence.models import (
    SHA256,
    AttemptState,
    ExecutionModel,
    IntegrityError,
    NotFoundError,
    SubmissionCertainty,
    SubmissionState,
)
from judgmentllm.persistence.repository import Repository
from judgmentllm.preparation import _load_manifest  # pyright: ignore[reportPrivateUsage]


class BulkMemberLink(ExecutionModel):
    task_id: ID
    result_seq: int | None
    result_sha256: SHA256 | None


class BulkPhysicalProvenance(ExecutionModel):
    run_id: ID
    request_uid: ID
    attempt_id: ID
    attempt_number: int
    authorization_id: ID | None
    attempt_state: AttemptState
    submission_id: ID | None
    submission_state: SubmissionState | None
    certainty: SubmissionCertainty | None
    payload_sha256: SHA256
    observation_id: ID | None
    members: tuple[BulkMemberLink, ...]
    selected_usage: ProviderUsage | None
    reservation: CommittedBulkReservation | None


class BulkProvenanceManifest(ExecutionModel):
    kind: Literal["generative_bulk_live"] = "generative_bulk_live"
    run_id: ID
    bundle_id: SHA256
    source_manifest_sha256: SHA256
    event_high_water_mark: int = Field(ge=0)
    result_high_water_mark: int = Field(ge=0)
    usage_high_water_mark: int = Field(ge=0)
    normalized_result_schema: Literal[1] = 1
    csv_encoding: Literal["json_cells_v1"] = "json_cells_v1"
    files: tuple[CompanionFileV1, CompanionFileV1]


class BulkProvenanceReport(ExecutionModel):
    run_id: ID
    bundle_id: SHA256
    manifest_path: str
    manifest: BulkProvenanceManifest


def _rows(
    repository: Repository,
    run_id: str,
    reservations: ReservationRows,
    usage_high: int,
    result_high: int,
) -> Iterator[BulkPhysicalProvenance]:
    for attempt in repository.iter_attempts(run_id):
        submission = repository.get_submission_for_attempt(run_id, attempt.attempt_id)
        usage = None
        observation = None
        if submission is not None:
            observation = repository.get_observation_for_submission(
                run_id, submission.submission_id
            )
            usage = repository.authoritative_usage(
                run_id, submission.submission_id, high_water_mark=usage_high
            )
            if usage is not None and (
                usage.invocation_kind != "managed_bulk_live"
                or usage.attempt_id != attempt.attempt_id
                or usage.request_uid != attempt.request_uid
            ):
                raise IntegrityError("bulk provenance usage differs from its attempt")
        members = repository.connection.execute(
            """
            SELECT m.task_id, r.result_seq, r.record_sha256
              FROM attempt_tasks m
              LEFT JOIN results r USING (run_id, attempt_id, task_id)
             WHERE m.run_id=? AND m.attempt_id=?
               AND (r.result_seq IS NULL OR r.result_seq<=?)
             ORDER BY m.position
            """,
            (run_id, attempt.attempt_id, result_high),
        )
        yield BulkPhysicalProvenance(
            run_id=run_id,
            request_uid=attempt.request_uid,
            attempt_id=attempt.attempt_id,
            attempt_number=attempt.attempt_number,
            authorization_id=attempt.authorization_id,
            attempt_state=attempt.state,
            submission_id=submission.submission_id if submission else None,
            submission_state=submission.state if submission else None,
            certainty=submission.certainty if submission else None,
            payload_sha256=attempt.payload_sha256,
            observation_id=observation.observation_id if observation else None,
            selected_usage=usage,
            members=tuple(
                BulkMemberLink(task_id=r[0], result_seq=r[1], result_sha256=r[2])
                for r in members
            ),
            reservation=reservations.for_attempt(attempt.attempt_id),
        )
    reservations.finish()


def _chunks(path: Path) -> Iterator[bytes]:
    with path.open("rb") as stream:
        while chunk := stream.read(65_536):
            yield chunk


def export_bulk_provenance(run_id: str, *, root: Path) -> BulkProvenanceReport:
    with Repository(root) as repository:
        manifest = _load_manifest(repository, run_id, unsealed=False)
        if manifest.execution.mode != "bulk_live":
            raise ValueError("generative provenance requires a sealed bulk-live run")
        manifest_sha = repository.get_manifest(run_id).artifact.sha256
        store = ArtifactStore(repository.run_directory(run_id), create=False)
        connection = repository.connection
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        try:
            result_high = int(
                connection.execute(
                    "SELECT coalesce(max(result_seq),0) FROM results WHERE run_id=?",
                    (run_id,),
                ).fetchone()[0]
            )
            event_high = event_high_water(repository, run_id)
            usage_high = repository.usage_high_water_mark(run_id)
            bundle_id = semantic_digest(
                "generative-bulk-live-provenance-v1",
                [run_id, manifest_sha, event_high, result_high, usage_high],
            )
            reference = f"bulk-provenance/{bundle_id}.json"
            target = store.reference_path(reference)
            if target.exists():
                with target.open("rb") as stream:
                    raw = stream.read(1_048_577)
                bundle = BulkProvenanceManifest.model_validate_json(raw)
                if (
                    canonical_bytes(bundle) != raw
                    or (
                        bundle.run_id,
                        bundle.bundle_id,
                        bundle.source_manifest_sha256,
                        bundle.event_high_water_mark,
                        bundle.result_high_water_mark,
                        bundle.usage_high_water_mark,
                    )
                    != (
                        run_id,
                        bundle_id,
                        manifest_sha,
                        event_high,
                        result_high,
                        usage_high,
                    )
                    or tuple(file.name for file in bundle.files)
                    != ("physical.jsonl", "physical.csv")
                ):
                    raise IntegrityError(
                        "bulk provenance bundle differs from its snapshot"
                    )
                store.verify(repository.get_artifact(run_id, bytes_digest(raw)))
                for file in bundle.files:
                    repository.get_artifact(run_id, file.artifact.sha256)
                    store.verify(file.artifact)
                return BulkProvenanceReport(
                    run_id=run_id,
                    bundle_id=bundle_id,
                    manifest_path=str(target),
                    manifest=bundle,
                )
            reservations = ReservationRows(
                committed_reservations(
                    repository,
                    run_id,
                    manifest_sha,
                    manifest.execution.bulk_limits,
                    event_cut=event_high,
                )
            )
            with tempfile.TemporaryDirectory(
                prefix=".bulk-export-", dir=store.root
            ) as work:
                json_path, csv_path = (
                    Path(work) / "physical.jsonl",
                    Path(work) / "physical.csv",
                )
                count = 0
                with (
                    json_path.open("wb") as lines,
                    csv_path.open("w", encoding="utf-8", newline="") as cells,
                ):
                    writer = csv.writer(cells)
                    writer.writerow(BulkPhysicalProvenance.model_fields)
                    for row in _rows(
                        repository, run_id, reservations, usage_high, result_high
                    ):
                        lines.write(canonical_bytes(row) + b"\n")
                        writer.writerow(
                            canonical_bytes(value).decode()
                            for value in row.model_dump(mode="json").values()
                        )
                        count += 1
                files = (
                    CompanionFileV1(
                        name="physical.jsonl",
                        artifact=store.put_stream(
                            _chunks(json_path), "application/x-ndjson"
                        ),
                        rows=count,
                    ),
                    CompanionFileV1(
                        name="physical.csv",
                        artifact=store.put_stream(_chunks(csv_path), "text/csv"),
                        rows=count,
                    ),
                )
        finally:
            connection.execute("ROLLBACK")
            connection.execute("PRAGMA query_only=OFF")
        bundle = BulkProvenanceManifest(
            run_id=run_id,
            bundle_id=bundle_id,
            source_manifest_sha256=manifest_sha,
            event_high_water_mark=event_high,
            result_high_water_mark=result_high,
            usage_high_water_mark=usage_high,
            files=files,
        )
        identity = store.put(canonical_bytes(bundle), "application/json")
        with repository.transaction():
            for artifact in (*(file.artifact for file in files), identity):
                try:
                    registered = repository.get_artifact(run_id, artifact.sha256)
                except NotFoundError:
                    repository.register_artifact(run_id, artifact)
                else:
                    if registered.byte_length != artifact.byte_length:
                        raise IntegrityError("bulk provenance artifact length differs")
        store.publish_reference(identity, reference)
        return BulkProvenanceReport(
            run_id=run_id,
            bundle_id=bundle_id,
            manifest_path=str(target),
            manifest=bundle,
        )
