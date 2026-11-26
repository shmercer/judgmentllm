"""Native batch audit bundle: all files originate in one immutable read cut."""

from __future__ import annotations

import csv
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import Field, field_validator

from judgmentllm.anthropic_batch_transport import batch_source_id
from judgmentllm.batch_models import (
    AnthropicBatchAdmissionV1,
    AnthropicBatchCancelObservedV1,
    AnthropicBatchCreateReceiptV1,
    AnthropicBatchHTTPDiagnosticV1,
    AnthropicBatchMemberReceiptV1,
    AnthropicBatchReadyV1,
    AnthropicBatchSelectionV1,
    AnthropicBatchShardV1,
    AnthropicBatchStatusV1,
    BatchCursorV1,
    BatchDiagnosticV1,
    BatchMemberReceiptV1,
    BatchMemberV1,
    BatchOperationIntentV1,
    BatchOperationReceiptV1,
    BatchShardV1,
    BatchStatusEvidenceV1,
    BatchStopV1,
    OpenAIBatchAbsentV1,
    OpenAIBatchAdmissionV1,
    OpenAIBatchPreludeFailureV1,
    OpenAIBatchProjectionV1,
    OpenAIBatchReadyV1,
    OpenAIBatchSelectionV1,
    OpenAIBatchShardV1,
    OpenAIBatchSourceBindingV1,
    OpenAIBatchStatusV1,
    member_receipt_model,
    operation_receipt_model,
    shard_model,
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
from judgmentllm.persistence.native_batch import latest, read_reference, register, rows
from judgmentllm.persistence.repository import ReadOnlyRepository, Repository
from judgmentllm.preparation import (
    _load_manifest,  # pyright: ignore[reportPrivateUsage]
    _preflight_v12_write,  # pyright: ignore[reportPrivateUsage]
)
from judgmentllm.preparation.manifest import (
    ExecutableRunManifestV12,
    ExecutableRunManifestV13,
    ExecutableRunManifestV14,
    NativeBatchManifest,
)
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


class OpenAIBatchJobProvenanceV1(EvidenceModel):
    preparation: OpenAIBatchShardV1
    submission_id: str | None
    remote_id: str | None


class OpenAIBatchControlProvenanceV1(EvidenceModel):
    fact_seq: int
    kind: Literal[
        "operation", "stop", "cancel_all", "status", "cursor", "conflict", "withheld"
    ]
    entity_id: str
    receipt_sha256: str
    record: (
        BatchOperationReceiptV1
        | BatchStopV1
        | OpenAIBatchStatusV1
        | BatchCursorV1
        | BatchDiagnosticV1
        | OpenAIBatchReadyV1
        | OpenAIBatchSelectionV1
        | OpenAIBatchProjectionV1
        | OpenAIBatchPreludeFailureV1
        | OpenAIBatchAdmissionV1
        | OpenAIBatchSourceBindingV1
        | OpenAIBatchAbsentV1
    )


class BatchAuditManifestV2(BatchAuditManifestV1):
    schema_version: Literal[2] = 2  # pyright: ignore[reportIncompatibleVariableOverride]

    @field_validator("schema_version", mode="before")
    @classmethod
    def strict_version(cls, value: object) -> object:
        if type(value) is not int or value != 2:
            raise ValueError("OpenAI audit version must be integer 2")
        return value


class BatchAuditReportV2(BatchAuditReportV1):
    schema_version: Literal[2] = 2  # pyright: ignore[reportIncompatibleVariableOverride]
    manifest: BatchAuditManifestV2  # pyright: ignore[reportIncompatibleVariableOverride]

    @field_validator("schema_version", mode="before")
    @classmethod
    def strict_version(cls, value: object) -> object:
        if type(value) is not int or value != 2:
            raise ValueError("OpenAI audit report version must be integer 2")
        return value


class AnthropicBatchJobProvenanceV1(BatchJobProvenanceV1):
    preparation: AnthropicBatchShardV1  # pyright: ignore[reportIncompatibleVariableOverride]


class AnthropicBatchMemberProvenanceV1(BatchMemberProvenanceV1):
    evidence: AnthropicBatchMemberReceiptV1 | None  # pyright: ignore[reportIncompatibleVariableOverride]


class AnthropicBatchOperationProvenanceV1(BatchOperationProvenanceV1):
    receipt: AnthropicBatchCreateReceiptV1 | BatchOperationReceiptV1 | None  # pyright: ignore[reportIncompatibleVariableOverride]


class AnthropicBatchControlProvenanceV1(BatchControlProvenanceV1):
    record: (  # pyright: ignore[reportIncompatibleVariableOverride]
        AnthropicBatchCreateReceiptV1
        | BatchOperationReceiptV1
        | BatchStopV1
        | BatchCursorV1
        | BatchDiagnosticV1
        | AnthropicBatchStatusV1
        | AnthropicBatchReadyV1
        | AnthropicBatchSelectionV1
        | AnthropicBatchAdmissionV1
        | AnthropicBatchCancelObservedV1
        | AnthropicBatchHTTPDiagnosticV1
    )


class BatchAuditManifestV3(BatchAuditManifestV1):
    schema_version: Literal[3] = 3  # pyright: ignore[reportIncompatibleVariableOverride]

    @field_validator("schema_version", mode="before")
    @classmethod
    def strict_version(cls, value: object) -> object:
        if type(value) is not int or value != 3:
            raise ValueError("Anthropic Batch audit version must be integer 3")
        return value


class BatchAuditReportV3(BatchAuditReportV1):
    schema_version: Literal[3] = 3  # pyright: ignore[reportIncompatibleVariableOverride]
    manifest: BatchAuditManifestV3  # pyright: ignore[reportIncompatibleVariableOverride]

    @field_validator("schema_version", mode="before")
    @classmethod
    def strict_version(cls, value: object) -> object:
        if type(value) is not int or value != 3:
            raise ValueError("Anthropic Batch audit report version must be integer 3")
        return value


_ANTHROPIC_CURSORS = {
    model.model_fields["record_type"].default: model
    for model in (
        AnthropicBatchReadyV1,
        AnthropicBatchSelectionV1,
        AnthropicBatchAdmissionV1,
        AnthropicBatchCancelObservedV1,
    )
}

_OPENAI_CONTROLS = {
    model.model_fields["record_type"].default: model
    for model in (
        OpenAIBatchReadyV1,
        OpenAIBatchSelectionV1,
        OpenAIBatchProjectionV1,
        OpenAIBatchPreludeFailureV1,
        OpenAIBatchAdmissionV1,
        OpenAIBatchSourceBindingV1,
        OpenAIBatchAbsentV1,
    )
}


def _chunks(path: Path) -> Iterator[bytes]:
    with path.open("rb") as stream:
        while chunk := stream.read(65_536):
            yield chunk


_V1_COMPANIONS = {
    "jobs.jsonl",
    "jobs.csv",
    "members.jsonl",
    "members.csv",
    "operations.jsonl",
    "operations.csv",
    "controls.jsonl",
    "controls.csv",
    "results.jsonl",
    "results.csv",
    "results_r.csv",
}


def export_batch_audit(
    run_id: str, root: Path, *, bundle_id: str | None = None
) -> BatchAuditReportV1 | BatchAuditReportV2 | BatchAuditReportV3:
    with ReadOnlyRepository(root, preserve_files=bundle_id is not None) as inspection:
        if bundle_id is None:
            _preflight_v12_write(inspection, run_id)
        manifest = _load_manifest(
            inspection,
            run_id,
            unsealed=False,
            allow_v12_posix_read=bundle_id is not None,
        )
        if not isinstance(manifest, NativeBatchManifest):
            raise ValueError("batch audit requires a native batch manifest")
        openai = isinstance(manifest, ExecutableRunManifestV13)
        anthropic = isinstance(manifest, ExecutableRunManifestV14)
        bundle_model = (
            BatchAuditManifestV3
            if anthropic
            else BatchAuditManifestV2
            if openai
            else BatchAuditManifestV1
        )
        store = ArtifactStore(inspection.run_directory(run_id), create=False)
        if bundle_id is not None:
            bundle = read_reference(
                inspection,
                run_id,
                f"batch/audits/{bundle_id}.json",
                bundle_model,
            )
            if (
                bundle is None
                or bundle.bundle_id != bundle_id
                or bundle.run_id != run_id
            ):
                raise IntegrityError("batch audit bundle identity differs")
            verify_named = type(manifest) is ExecutableRunManifestV12 or anthropic
            if verify_named:
                seal = inspection.get_manifest(run_id).artifact.sha256
                if bundle.cut.source_manifest_sha256 != seal:
                    raise IntegrityError("batch audit cut differs from the run seal")
                if (
                    semantic_digest("native-batch-audit-v1", [run_id, bundle.cut])
                    != bundle_id
                ):
                    raise IntegrityError("batch audit cut differs from the bundle ID")
                raw = canonical_bytes(bundle)
                store.read(inspection.get_artifact(run_id, bytes_digest(raw)))
                if (
                    len(bundle.files) != len(_V1_COMPANIONS)
                    or {file.name for file in bundle.files} != _V1_COMPANIONS
                ):
                    raise IntegrityError("batch audit companion set differs")
            for file in bundle.files:
                retained = store.verify(file.artifact)
                if verify_named and retained is None:
                    raise IntegrityError(
                        "batch audit companion bytes were not retained"
                    )
            if anthropic:
                _verify_named_http_diagnostics(
                    inspection, store, manifest.capture, bundle
                )
            return report(inspection, store, bundle)
    job_model = (
        AnthropicBatchJobProvenanceV1
        if anthropic
        else OpenAIBatchJobProvenanceV1
        if openai
        else BatchJobProvenanceV1
    )
    member_model = (
        AnthropicBatchMemberProvenanceV1 if anthropic else BatchMemberProvenanceV1
    )
    receipt_model = member_receipt_model(manifest.batch_context)
    operation_model = (
        AnthropicBatchOperationProvenanceV1 if anthropic else BatchOperationProvenanceV1
    )
    control_model = (
        AnthropicBatchControlProvenanceV1
        if anthropic
        else OpenAIBatchControlProvenanceV1
        if openai
        else BatchControlProvenanceV1
    )
    with Repository(root) as repository:
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
                        repository,
                        "batch_preparations",
                        run_id,
                        shard_model(manifest.batch_context),
                    ):
                        row = connection.execute(
                            "SELECT submission_id,remote_id FROM submissions WHERE run_id=? AND batch_uid=? AND certainty='accepted'",
                            (run_id, shard.shard_id),
                        ).fetchone()
                        yield job_model.model_validate(
                            dict(
                                preparation=shard,
                                submission_id=row[0] if row else None,
                                remote_id=row[1] if row else None,
                            )
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
                            receipt_model.model_validate_json(row[0]) if row else None
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
                        yield member_model.model_validate(
                            dict(
                                preparation=member,
                                evidence=evidence,
                                result_ids=result_ids,
                            )
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
                        receipt = latest(
                            repository,
                            run_id,
                            "operation",
                            intent.instance_id,
                            operation_receipt_model(
                                manifest.batch_context, intent.kind
                            ),
                        )
                        if (
                            anthropic
                            and receipt is not None
                            and (
                                receipt.instance_id != intent.instance_id
                                or receipt.intent_sha256 != row[1]
                            )
                        ):
                            raise IntegrityError(
                                "Anthropic Batch audit receipt differs from its intent"
                            )
                        yield operation_model(intent=intent, receipt=receipt)

                companion("jobs", jobs(), job_model)
                companion("members", members(), member_model)
                companion("operations", operations(), operation_model)

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
                    if openai:
                        models["status"] = OpenAIBatchStatusV1
                    elif anthropic:
                        models["status"] = AnthropicBatchStatusV1
                    for row in connection.execute(
                        "SELECT fact_seq,kind,entity_id,receipt_sha256,record_json FROM batch_facts WHERE run_id=? AND fact_seq<=? ORDER BY fact_seq",
                        (run_id, cut.fact_high_water_mark),
                    ):
                        model = models[row[1]]
                        if (openai or anthropic) and row[1] == "cursor":
                            from judgmentllm.output_contracts import (
                                decode_assessment_output,
                            )

                            fields = decode_assessment_output(row[4])
                            if not isinstance(fields, dict):
                                raise IntegrityError("batch cursor record is invalid")
                            tag = cast(dict[str, Any], fields).get("record_type")
                            model = (
                                _OPENAI_CONTROLS if openai else _ANTHROPIC_CURSORS
                            ).get(tag, BatchCursorV1)
                        elif (
                            anthropic
                            and row[1] == "status"
                            and row[2].startswith("anthropic-http:")
                        ):
                            model = AnthropicBatchHTTPDiagnosticV1
                        elif anthropic and row[1] == "operation":
                            intent_kind = connection.execute(
                                "SELECT kind FROM batch_operations WHERE run_id=? AND instance_id=?",
                                (run_id, row[2]),
                            ).fetchone()
                            if intent_kind is None:
                                raise IntegrityError(
                                    "Anthropic Batch operation fact lacks its intent"
                                )
                            model = operation_receipt_model(
                                manifest.batch_context, intent_kind[0]
                            )
                        record = model.model_validate_json(row[4])
                        if (
                            canonical_bytes(record).decode() != row[4]
                            or bytes_digest(canonical_bytes(record)) != row[3]
                        ):
                            raise IntegrityError(
                                "batch control evidence encoding conflicts"
                            )
                        store.verify(repository.get_artifact(run_id, row[3]))
                        if isinstance(record, AnthropicBatchHTTPDiagnosticV1):
                            _verify_http_diagnostic(
                                repository,
                                store,
                                run_id,
                                manifest.capture,
                                cut.source_manifest_sha256,
                                row[0],
                                row[2],
                                record,
                            )
                        yield control_model.model_validate(
                            dict(
                                fact_seq=row[0],
                                kind=row[1],
                                entity_id=row[2],
                                receipt_sha256=row[3],
                                record=record,
                            )
                        )

                companion("controls", controls(), control_model)
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
                bundle = bundle_model(
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


def _verify_named_http_diagnostics(
    repository: Repository,
    store: ArtifactStore,
    capture: str,
    bundle: BatchAuditManifestV1,
) -> None:
    if not isinstance(bundle, BatchAuditManifestV3):
        raise IntegrityError("Anthropic Batch audit manifest differs")
    for sequence, entity, digest, raw in repository.connection.execute(
        "SELECT fact_seq,entity_id,receipt_sha256,record_json FROM batch_facts "
        "WHERE run_id=? AND kind='status' AND entity_id LIKE 'anthropic-http:%' AND fact_seq<=?",
        (bundle.run_id, bundle.cut.fact_high_water_mark),
    ):
        diagnostic = AnthropicBatchHTTPDiagnosticV1.model_validate_json(raw)
        if (
            canonical_bytes(diagnostic) != raw.encode()
            or store.read(repository.get_artifact(bundle.run_id, digest))
            != raw.encode()
        ):
            raise IntegrityError("Anthropic Batch HTTP diagnostic encoding differs")
        _verify_http_diagnostic(
            repository,
            store,
            bundle.run_id,
            capture,
            bundle.cut.source_manifest_sha256,
            sequence,
            entity,
            diagnostic,
        )


def _verify_http_diagnostic(
    repository: Repository,
    store: ArtifactStore,
    run_id: str,
    capture: str,
    manifest_sha256: str,
    fact_seq: int,
    entity: str,
    record: AnthropicBatchHTTPDiagnosticV1,
) -> None:
    if entity != "anthropic-http:" + record.call_id:
        raise IntegrityError("Anthropic Batch HTTP diagnostic call differs")
    shard = None
    if record.submission_id is not None:
        submission = repository.connection.execute(
            "SELECT batch_uid,remote_id,certainty FROM submissions WHERE run_id=? AND submission_id=?",
            (run_id, record.submission_id),
        ).fetchone()
        if (
            submission is None
            or submission[0] is None
            or (submission[1], submission[2]) != (record.job_id, "accepted")
        ):
            raise IntegrityError("Anthropic Batch HTTP diagnostic job differs")
        shard = str(submission[0])
        if record.source_id is not None and record.source_id != batch_source_id(
            manifest_sha256, shard, record.submission_id
        ):
            raise IntegrityError("Anthropic Batch HTTP diagnostic source differs")
    if record.intent_id is not None:
        row = repository.connection.execute(
            "SELECT intent_sha256 FROM batch_operations WHERE run_id=? AND instance_id=?",
            (run_id, record.intent_id),
        ).fetchone()
        if row is None:
            raise IntegrityError("Anthropic Batch HTTP diagnostic lacks its intent")
        intent = BatchOperationIntentV1.model_validate_json(
            store.read(repository.get_artifact(run_id, row[0]))
        )
        if (
            intent.run_id != run_id
            or intent.instance_id != record.intent_id
            or intent.manifest_sha256 != manifest_sha256
            or intent.service_id != "anthropic-v1"
            or intent.sequence != 1
            or intent.kind != ("create" if record.stage == "create" else "cancel")
            or intent.remote_id != record.job_id
            or (shard is not None and intent.shard_id != shard)
        ):
            raise IntegrityError("Anthropic Batch HTTP diagnostic intent differs")
    if record.body is not None:
        if (record.body.relative_path is not None) != (
            capture == "full"
        ) or repository.get_artifact(run_id, record.body.sha256) != record.body:
            raise IntegrityError("Anthropic Batch HTTP diagnostic retention differs")
        store.verify(record.body)
    if record.headers_sha256 is not None:
        row = repository.connection.execute(
            "SELECT 1 FROM batch_facts WHERE run_id=? AND kind='status' AND entity_id=? AND receipt_sha256=? AND fact_seq<?",
            (run_id, entity, record.headers_sha256, fact_seq),
        ).fetchone()
        if row is None:
            raise IntegrityError("Anthropic Batch HTTP diagnostic lacks its headers")
        header = AnthropicBatchHTTPDiagnosticV1.model_validate_json(
            store.read(repository.get_artifact(run_id, record.headers_sha256))
        )
        common = (
            "call_id",
            "stage",
            "intent_id",
            "submission_id",
            "job_id",
            "source_id",
            "http_status",
            "request_id",
        )
        if header.phase != "headers" or any(
            getattr(header, name) != getattr(record, name) for name in common
        ):
            raise IntegrityError("Anthropic Batch HTTP diagnostic headers differ")


def report(
    repository: Repository, store: ArtifactStore, bundle: BatchAuditManifestV1
) -> BatchAuditReportV1 | BatchAuditReportV2 | BatchAuditReportV3:
    path = str(store.reference_path(f"batch/audits/{bundle.bundle_id}.json"))
    model = (
        BatchAuditReportV3
        if isinstance(bundle, BatchAuditManifestV3)
        else BatchAuditReportV2
        if isinstance(bundle, BatchAuditManifestV2)
        else BatchAuditReportV1
    )
    return model.model_validate(
        dict(
            run_id=bundle.run_id,
            run_status=repository.get_run(bundle.run_id).state,
            manifest_path=path,
            manifest=bundle,
            artifacts={"store": str(repository.root), "export": path},
        )
    )
