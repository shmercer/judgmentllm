"""Native batch audit bundle: all files originate in one immutable read cut."""

from __future__ import annotations

import csv
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Literal

from pydantic import Field

from judgmentllm.batch_models import (
    BatchCursorV1,
    BatchDiagnosticV1,
    BatchMemberReceiptV1,
    BatchMemberV1,
    BatchOperationIntentV1,
    BatchOperationReceiptV1,
    BatchShardV1,
    BatchStatusEvidenceV1,
    BatchStopV1,
)
from judgmentllm.evidence._base import EvidenceModel
from judgmentllm.io import export_results_csv, write_results_jsonl
from judgmentllm.persistence.artifacts import ArtifactStore
from judgmentllm.persistence.hashing import (
    bytes_digest,
    canonical_bytes,
    semantic_digest,
)
from judgmentllm.persistence.models import ArtifactIdentity, IntegrityError, RunState
from judgmentllm.persistence.native_batch import latest, register, rows
from judgmentllm.persistence.repository import Repository
from judgmentllm.preparation import _load_manifest  # pyright: ignore[reportPrivateUsage]
from judgmentllm.preparation.manifest import ExecutableRunManifestV12
from judgmentllm.r_exports import export_pairwise_r_csv, export_rubric_r_csv
from judgmentllm.results import PairwiseResult, RubricResult


class BatchAuditFileV1(EvidenceModel):
    name: str
    artifact: ArtifactIdentity
    rows: int = Field(ge=0)


class BatchAuditCutV1(EvidenceModel):
    source_manifest_sha256: str
    event_high_water_mark: int = Field(ge=0)
    result_high_water_mark: int = Field(ge=0)
    fact_high_water_mark: int = Field(ge=0)
    operation_count: int = Field(ge=0)
    member_receipt_count: int = Field(ge=0)


class BatchAuditManifestV1(EvidenceModel):
    kind: Literal["native_batch_audit"] = "native_batch_audit"
    run_id: str
    bundle_id: str
    cut: BatchAuditCutV1
    csv_encoding: Literal["json_cells_v1"] = "json_cells_v1"
    normalized_result_schema: Literal[1] = 1
    files: tuple[BatchAuditFileV1, ...]


class BatchAuditReportV1(EvidenceModel):
    run_id: str
    run_status: RunState
    manifest_path: str
    manifest: BatchAuditManifestV1
    artifacts: dict[str, str | None]


class BatchJobProvenanceV1(EvidenceModel):
    preparation: BatchShardV1
    submission_id: str | None
    remote_id: str | None


class BatchMemberProvenanceV1(EvidenceModel):
    preparation: BatchMemberV1
    evidence: BatchMemberReceiptV1 | None
    result_ids: tuple[int, ...]


class BatchOperationProvenanceV1(EvidenceModel):
    intent: BatchOperationIntentV1
    receipt: BatchOperationReceiptV1 | None


class BatchControlProvenanceV1(EvidenceModel):
    fact_seq: int
    kind: Literal[
        "operation", "stop", "cancel_all", "status", "cursor", "conflict", "withheld"
    ]
    entity_id: str
    receipt_sha256: str
    record: (
        BatchOperationReceiptV1
        | BatchStopV1
        | BatchStatusEvidenceV1
        | BatchCursorV1
        | BatchDiagnosticV1
    )


def _chunks(path: Path) -> Iterator[bytes]:
    with path.open("rb") as stream:
        while chunk := stream.read(65_536):
            yield chunk


def export_batch_audit(
    run_id: str, root: Path, *, bundle_id: str | None = None
) -> BatchAuditReportV1:
    with Repository(root) as repository:
        manifest = _load_manifest(repository, run_id, unsealed=False)
        if not isinstance(manifest, ExecutableRunManifestV12):
            raise ValueError("batch audit requires a native batch manifest")
        store = ArtifactStore(repository.run_directory(run_id), create=False)
        if bundle_id is not None:
            from judgmentllm.persistence.native_batch import read_reference

            bundle = read_reference(
                repository,
                run_id,
                f"batch/audits/{bundle_id}.json",
                BatchAuditManifestV1,
            )
            if (
                bundle is None
                or bundle.bundle_id != bundle_id
                or bundle.run_id != run_id
            ):
                raise IntegrityError("batch audit bundle identity differs")
            for file in bundle.files:
                store.verify(file.artifact)
            return report(repository, store, bundle)
        connection = repository.connection
        connection.execute("BEGIN")
        try:

            def high(table: str, column: str) -> int:
                return int(
                    connection.execute(
                        f"SELECT coalesce(max({column}),0) FROM {table} WHERE run_id=?",
                        (run_id,),
                    ).fetchone()[0]
                )

            cut = BatchAuditCutV1(
                source_manifest_sha256=repository.get_manifest(run_id).artifact.sha256,
                event_high_water_mark=high("events", "event_seq"),
                result_high_water_mark=high("results", "result_seq"),
                fact_high_water_mark=high("batch_facts", "fact_seq"),
                operation_count=int(
                    connection.execute(
                        "SELECT count(*) FROM batch_operations WHERE run_id=?",
                        (run_id,),
                    ).fetchone()[0]
                ),
                member_receipt_count=int(
                    connection.execute(
                        "SELECT count(*) FROM batch_member_receipts WHERE run_id=?",
                        (run_id,),
                    ).fetchone()[0]
                ),
            )
            identifier = semantic_digest("native-batch-audit-v1", [run_id, cut])
            files: list[BatchAuditFileV1] = []
            with tempfile.TemporaryDirectory(
                prefix=".batch-export-", dir=store.root
            ) as temporary:
                directory = Path(temporary)

                def companion(
                    name: str,
                    records: Iterator[EvidenceModel],
                    record_type: type[EvidenceModel],
                ) -> None:
                    path, cells = (
                        directory / (name + ".jsonl"),
                        directory / (name + ".csv"),
                    )
                    count = 0
                    with (
                        path.open("wb") as stream,
                        cells.open("w", encoding="utf-8", newline="") as cell_stream,
                    ):
                        writer = csv.writer(cell_stream)
                        fields = tuple(record_type.model_fields)
                        writer.writerow(fields)
                        for record in records:
                            stream.write(canonical_bytes(record) + b"\n")
                            data = record.model_dump(mode="json")
                            writer.writerow(
                                canonical_bytes(data[field]).decode()
                                for field in fields
                            )
                            count += 1
                    files.extend(
                        (
                            BatchAuditFileV1(
                                name=path.name,
                                artifact=store.put_stream(
                                    _chunks(path), "application/x-ndjson"
                                ),
                                rows=count,
                            ),
                            BatchAuditFileV1(
                                name=cells.name,
                                artifact=store.put_stream(_chunks(cells), "text/csv"),
                                rows=count,
                            ),
                        )
                    )

                def jobs() -> Iterator[EvidenceModel]:
                    for shard in rows(
                        repository, "batch_preparations", run_id, BatchShardV1
                    ):
                        row = connection.execute(
                            "SELECT submission_id,remote_id FROM submissions WHERE run_id=? AND batch_uid=? AND certainty='accepted'",
                            (run_id, shard.shard_id),
                        ).fetchone()
                        yield BatchJobProvenanceV1(
                            preparation=shard,
                            submission_id=row[0] if row else None,
                            remote_id=row[1] if row else None,
                        )

                def members() -> Iterator[EvidenceModel]:
                    for member in rows(
                        repository, "batch_prepared_members", run_id, BatchMemberV1
                    ):
                        row = connection.execute(
                            "SELECT record_json,receipt_sha256 FROM batch_member_receipts WHERE run_id=? AND attempt_id=?",
                            (run_id, member.attempt_id),
                        ).fetchone()
                        evidence = (
                            BatchMemberReceiptV1.model_validate_json(row[0])
                            if row
                            else None
                        )
                        if evidence is not None:
                            if bytes_digest(canonical_bytes(evidence)) != row[1] or (
                                evidence.identity.attempt_id,
                                evidence.identity.request_uid,
                                evidence.member_key,
                            ) != (
                                member.attempt_id,
                                member.request_uid,
                                member.member_key,
                            ):
                                raise IntegrityError(
                                    "batch audit member differs from its bridge"
                                )
                            store.verify(repository.get_artifact(run_id, row[1]))
                            store.verify(evidence.retained)
                        result_ids = tuple(
                            int(r[0])
                            for r in connection.execute(
                                "SELECT result_seq FROM results WHERE run_id=? AND attempt_id=? AND result_seq<=? ORDER BY result_seq",
                                (run_id, member.attempt_id, cut.result_high_water_mark),
                            )
                        )
                        yield BatchMemberProvenanceV1(
                            preparation=member, evidence=evidence, result_ids=result_ids
                        )

                def operations() -> Iterator[EvidenceModel]:
                    for row in connection.execute(
                        "SELECT record_json,intent_sha256 FROM batch_operations WHERE run_id=? ORDER BY rowid",
                        (run_id,),
                    ):
                        intent = BatchOperationIntentV1.model_validate_json(row[0])
                        if bytes_digest(canonical_bytes(intent)) != row[1]:
                            raise IntegrityError(
                                "batch audit intent differs from its commitment"
                            )
                        store.verify(repository.get_artifact(run_id, row[1]))
                        yield BatchOperationProvenanceV1(
                            intent=intent,
                            receipt=latest(
                                repository,
                                run_id,
                                "operation",
                                intent.instance_id,
                                BatchOperationReceiptV1,
                            ),
                        )

                companion("jobs", jobs(), BatchJobProvenanceV1)
                companion("members", members(), BatchMemberProvenanceV1)
                companion("operations", operations(), BatchOperationProvenanceV1)

                def controls() -> Iterator[EvidenceModel]:
                    models = {
                        "operation": BatchOperationReceiptV1,
                        "stop": BatchStopV1,
                        "cancel_all": BatchStopV1,
                        "status": BatchStatusEvidenceV1,
                        "cursor": BatchCursorV1,
                        "conflict": BatchDiagnosticV1,
                        "withheld": BatchDiagnosticV1,
                    }
                    for row in connection.execute(
                        "SELECT fact_seq,kind,entity_id,receipt_sha256,record_json FROM batch_facts WHERE run_id=? AND fact_seq<=? ORDER BY fact_seq",
                        (run_id, cut.fact_high_water_mark),
                    ):
                        record = models[row[1]].model_validate_json(row[4])
                        if (
                            canonical_bytes(record).decode() != row[4]
                            or bytes_digest(canonical_bytes(record)) != row[3]
                        ):
                            raise IntegrityError(
                                "batch control evidence encoding conflicts"
                            )
                        store.verify(repository.get_artifact(run_id, row[3]))
                        yield BatchControlProvenanceV1(
                            fact_seq=row[0],
                            kind=row[1],
                            entity_id=row[2],
                            receipt_sha256=row[3],
                            record=record,
                        )

                companion("controls", controls(), BatchControlProvenanceV1)
                for name, format, view in (
                    ("results.jsonl", "jsonl", "canonical"),
                    ("results.csv", "csv", "canonical"),
                    ("results_r.csv", "csv", "r"),
                ):
                    path = directory / name
                    records = repository.iter_results(
                        run_id, high_water_mark=cut.result_high_water_mark
                    )
                    if format == "jsonl":
                        count = write_results_jsonl(path, records)
                    elif view == "canonical":
                        count = export_results_csv(path, records)
                    elif manifest.task_kind == "rubric":
                        count = export_rubric_r_csv(
                            path, (r for r in records if isinstance(r, RubricResult))
                        )
                    else:
                        count = export_pairwise_r_csv(
                            path, (r for r in records if isinstance(r, PairwiseResult))
                        )
                    files.append(
                        BatchAuditFileV1(
                            name=name,
                            artifact=store.put_stream(
                                _chunks(path),
                                "application/x-ndjson"
                                if format == "jsonl"
                                else "text/csv",
                            ),
                            rows=count,
                        )
                    )
                bundle = BatchAuditManifestV1(
                    run_id=run_id, bundle_id=identifier, cut=cut, files=tuple(files)
                )
        finally:
            connection.execute("ROLLBACK")
        identity = store.put(canonical_bytes(bundle), "application/json")
        with repository.transaction():
            for artifact in (*(file.artifact for file in bundle.files), identity):
                register(repository, run_id, artifact)
        store.publish_reference(identity, f"batch/audits/{identifier}.json")
        return report(repository, store, bundle)


def report(
    repository: Repository, store: ArtifactStore, bundle: BatchAuditManifestV1
) -> BatchAuditReportV1:
    path = str(store.reference_path(f"batch/audits/{bundle.bundle_id}.json"))
    return BatchAuditReportV1(
        run_id=bundle.run_id,
        run_status=repository.get_run(bundle.run_id).state,
        manifest_path=path,
        manifest=bundle,
        artifacts={"store": str(repository.root), "export": path},
    )
