"""Narrow, snapshot-bound Decisions companions; normalized schema 1 is unchanged."""

from __future__ import annotations

import csv
import os
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from pydantic import Field, field_validator

from judgmentllm.decisions._base import DecisionsModel
from judgmentllm.decisions.outcomes import AnswerAttributionV1, read_outcomes
from judgmentllm.decisions.records import (
    DecisionsLimitsV1,
    DecisionsLimitsV2,
    QuestionCommitmentV1,
)
from judgmentllm.decisions.replay import DecisionsReplayMetadataV1
from judgmentllm.decisions.transport import DecisionsCommitmentV1
from judgmentllm.evidence.usage import ProviderUsage
from judgmentllm.lifecycle_models import DecisionsStatusV1
from judgmentllm.models import ID
from judgmentllm.persistence.artifacts import ArtifactStore
from judgmentllm.persistence.decisions_invocations import (
    DecisionsInvocationV1,
    DecisionsInvocationV2,
    read_invocation,
)
from judgmentllm.persistence.evidence import authoritative_usage, usage_high_water_mark
from judgmentllm.persistence.hashing import (
    bytes_digest,
    canonical_bytes,
    semantic_digest,
)
from judgmentllm.persistence.models import (
    SHA256,
    ArtifactIdentity,
    IntegrityError,
)
from judgmentllm.persistence.receipts import (
    AcknowledgmentReceiptV2,
    CompleteResponseEvidenceV1,
    CompleteResponseEvidenceV2,
    read_acknowledgment,
)
from judgmentllm.persistence.repository import Repository
from judgmentllm.persistence.transport_receipts import read_transport_commitment
from judgmentllm.preparation import _load_manifest  # pyright: ignore[reportPrivateUsage]
from judgmentllm.preparation.manifest import (
    ExecutableRunManifestV8,
    ExecutableRunManifestV9,
)
from judgmentllm.transport import PhysicalRequestIdentityV1, physical_identity


class PhysicalProvenanceV1(DecisionsModel):
    physical_provenance_version: Literal[1] = 1
    identity: PhysicalRequestIdentityV1
    state: str
    invocation_evidence: Literal["known", "unknown", "not_started"]
    payload_sha256: SHA256
    payload_byte_length: int = Field(ge=0)
    prompt_sha256: SHA256
    prompt_byte_length: int = Field(ge=0)
    shared_input_sha256: SHA256
    shared_input_byte_length: int = Field(ge=0)
    commitment_sha256: SHA256
    transport_receipt_sha256: SHA256
    limits: DecisionsLimitsV1
    complete_response_evidence: CompleteResponseEvidenceV1 | None
    raw_response: ArtifactIdentity | None
    provider_remote_id: ID | None
    local_observation_key: ID | None
    observation_id: ID | None
    selected_usage: ProviderUsage | None
    usage_selection: Literal["revision", "forwarding_fact", "unavailable"]


class PhysicalProvenanceV2(PhysicalProvenanceV1):
    physical_provenance_version: Literal[2] = 2  # pyright: ignore[reportIncompatibleVariableOverride]
    limits: DecisionsLimitsV2  # pyright: ignore[reportIncompatibleVariableOverride]
    complete_response_evidence: CompleteResponseEvidenceV2 | None  # pyright: ignore[reportIncompatibleVariableOverride]
    original_response: ArtifactIdentity | None
    retained_response: ArtifactIdentity | None
    retained_kind: Literal["original", "filtered_replay", "omitted"]
    validation: DecisionsReplayMetadataV1 | None
    acknowledgment_receipt_sha256: SHA256 | None
    evidence_receipt_sha256: SHA256 | None


class QuestionProvenanceV1(DecisionsModel):
    question_provenance_version: Literal[1] = 1
    identity: PhysicalRequestIdentityV1
    question: QuestionCommitmentV1
    source_presentation_id: ID | None
    presentation_id: ID | None
    source_record_id: ID
    shared_input_sha256: SHA256
    parent_commitment_sha256: SHA256 | None
    requested_model: ID
    reported_model: str | None
    observation_id: ID | None
    answer: AnswerAttributionV1 | None
    status: str | None
    normalized_result_sha256: SHA256 | None
    whole_response_valid: bool | None


class CompanionFileV1(DecisionsModel):
    name: Literal["questions.jsonl", "questions.csv", "physical.jsonl", "physical.csv"]
    artifact: ArtifactIdentity
    rows: int = Field(ge=0)


class DecisionsProvenanceManifestV1(DecisionsModel):
    provenance_manifest_version: Literal[1] = 1
    run_id: ID
    bundle_id: SHA256
    source_manifest_sha256: SHA256
    result_high_water_mark: int = Field(ge=0)
    usage_high_water_mark: int = Field(ge=0)
    event_high_water_mark: int = Field(ge=0)
    normalized_result_schema: Literal[1] = 1
    csv_encoding: Literal["json_cells_v1"] = "json_cells_v1"
    totals: DecisionsStatusV1
    files: tuple[CompanionFileV1, ...]


class DecisionsProvenanceReportV1(DecisionsModel):
    provenance_report_version: Literal[1] = 1
    run_id: ID
    bundle_id: SHA256
    manifest_path: str
    manifest: DecisionsProvenanceManifestV1


class DecisionsProvenanceManifestV2(DecisionsProvenanceManifestV1):
    provenance_manifest_version: Literal[2] = 2  # pyright: ignore[reportIncompatibleVariableOverride]
    physical_row_version: Literal[2] = 2
    question_row_version: Literal[1] = 1

    @field_validator("physical_row_version", "question_row_version", mode="before")
    @classmethod
    def row_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("row versions must be integers")
        return value


class DecisionsProvenanceReportV2(DecisionsProvenanceReportV1):
    provenance_report_version: Literal[2] = 2  # pyright: ignore[reportIncompatibleVariableOverride]
    manifest: DecisionsProvenanceManifestV2  # pyright: ignore[reportIncompatibleVariableOverride]


def _parse_manifest(raw: bytes) -> DecisionsProvenanceManifestV1:
    from judgmentllm.config import parse_json

    value = cast(dict[str, object], parse_json(raw.decode("utf-8")))
    model = (
        DecisionsProvenanceManifestV2
        if isinstance(cast(object, value), dict)
        and type(value.get("provenance_manifest_version")) is int
        and value["provenance_manifest_version"] == 2
        else DecisionsProvenanceManifestV1
    )
    return model.model_validate_json(raw)


@dataclass
class _Totals:
    known: int = 0
    unknown: int = 0
    responses: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0
    input_missing: int = 0
    output_missing: int = 0
    cost_missing: int = 0
    currency: str | None = None
    currency_conflict: bool = False

    def add(self, row: PhysicalProvenanceV1) -> None:
        self.record(
            row.selected_usage,
            known=row.invocation_evidence == "known",
            unknown=row.invocation_evidence == "unknown",
            observed=row.observation_id is not None,
        )

    def record(
        self, usage: ProviderUsage | None, *, known: bool, unknown: bool, observed: bool
    ) -> None:
        self.unknown += int(unknown)
        self.responses += int(observed)
        if not known:
            return
        self.known += 1
        for counter in ("input_tokens", "output_tokens"):
            value = getattr(usage, counter) if usage is not None else None
            if value is None:
                setattr(
                    self,
                    counter.replace("_tokens", "_missing"),
                    getattr(self, counter.replace("_tokens", "_missing")) + 1,
                )
            else:
                setattr(self, counter, getattr(self, counter) + value)
        cost = usage.reported_cost if usage is not None else None
        if cost is None:
            self.cost_missing += 1
        else:
            self.cost += float(cost.amount)
            self.currency_conflict |= (
                self.currency is not None and self.currency != cost.currency
            )
            self.currency = cost.currency

    def detail(self, manifest: ExecutableRunManifestV8) -> DecisionsStatusV1:
        unknown = bool(self.unknown)
        missing = self.input_missing + self.output_missing
        usage_state = (
            "unknown"
            if not self.known or missing == self.known * 2
            else "partial"
            if missing or unknown
            else "reported"
        )
        cost_state = (
            "unknown"
            if not self.known
            or self.cost_missing == self.known
            or self.currency_conflict
            else "partial"
            if self.cost_missing or unknown
            else "reported"
        )
        if not self.known:
            usage_state = cost_state = (
                "unknown"
                if unknown
                else "not_applicable"
                if manifest.initial_transport_count == 0
                else "unavailable"
            )
        return DecisionsStatusV1.model_validate(
            dict(
                logical_tasks=manifest.logical_task_count,
                physical_requests=manifest.initial_transport_count,
                presentations=manifest.presentation_count,
                known_invocations=self.known,
                invocation_unknown=self.unknown,
                observed_responses=self.responses,
                usage_evidence=usage_state,
                input_tokens=None
                if not self.known or self.input_missing or unknown
                else self.input_tokens,
                output_tokens=None
                if not self.known or self.output_missing or unknown
                else self.output_tokens,
                cost=None
                if not self.known
                or self.cost_missing
                or unknown
                or self.currency_conflict
                else self.cost,
                cost_currency=None if cost_state != "reported" else self.currency,
                cost_evidence=cost_state,
            )
        )


def physical_rows(
    repository: Repository, manifest: ExecutableRunManifestV8, usage_high: int
) -> Iterator[PhysicalProvenanceV1]:
    run_id = manifest.run_id
    artifacts = ArtifactStore(repository.run_directory(run_id), create=False)
    for attempt in repository.iter_attempts(run_id):
        commitment = read_transport_commitment(artifacts, attempt.attempt_id)
        if (
            not isinstance(commitment, DecisionsCommitmentV1)
            or commitment.identity
            != physical_identity(run_id, attempt.request_uid, attempt.attempt_id)
            or (
                commitment.prompt_sha256,
                commitment.payload_sha256,
                commitment.membership_sha256,
            )
            != (
                attempt.prompt_sha256,
                attempt.payload_sha256,
                attempt.membership_sha256,
            )
        ):
            raise IntegrityError("Decisions export commitment differs from attempt")
        submission = repository.get_submission_for_attempt(run_id, attempt.attempt_id)
        evidence = (
            repository.get_evidence_for_submission(run_id, submission.submission_id)
            if submission
            else None
        )
        fact = (
            read_invocation(artifacts, submission.submission_id)
            if submission and submission.state == "uncertain"
            else None
        )
        if fact is not None:
            expected_fact = (
                DecisionsInvocationV2
                if isinstance(manifest, ExecutableRunManifestV9)
                else DecisionsInvocationV1
            )
            if type(fact) is not expected_fact:
                raise IntegrityError("Decisions forwarding protocol differs")
            if (
                fact.identity != commitment.identity
                or fact.payload_sha256 != attempt.payload_sha256
            ):
                raise IntegrityError("Decisions forwarding fact differs from attempt")
            repository.get_artifact(run_id, bytes_digest(canonical_bytes(fact)))
        observation = (
            repository.get_observation_for_submission(run_id, submission.submission_id)
            if submission
            else None
        )
        ack = (
            read_acknowledgment(artifacts, submission.submission_id)
            if submission and submission.certainty == "accepted"
            else None
        )
        if ack is not None:
            assert submission is not None
            ack.verify_submission(submission)
        usage = (
            authoritative_usage(
                repository, run_id, submission.submission_id, high_water_mark=usage_high
            )
            if evidence is not None and submission
            else fact.usage
            if fact
            else None
        )
        known = evidence is not None or fact is not None or ack is not None
        invocation = (
            "known"
            if known
            else "unknown"
            if submission and submission.state != "prepared"
            else "not_started"
        )
        from judgmentllm.persistence.evidence import DecisionsEvidenceReceiptV3

        production = isinstance(manifest, ExecutableRunManifestV9)
        production_evidence = (
            evidence if isinstance(evidence, DecisionsEvidenceReceiptV3) else None
        )
        row_type = PhysicalProvenanceV2 if production else PhysicalProvenanceV1
        yield row_type.model_validate(
            dict(
                **(
                    dict(
                        original_response=production_evidence.original
                        if production_evidence
                        else None,
                        retained_response=production_evidence.retained
                        if production_evidence
                        else None,
                        retained_kind=production_evidence.retained_kind
                        if production_evidence
                        else "omitted",
                        validation=production_evidence.validation
                        if production_evidence
                        else None,
                        acknowledgment_receipt_sha256=bytes_digest(canonical_bytes(ack))
                        if ack
                        else None,
                        evidence_receipt_sha256=bytes_digest(canonical_bytes(evidence))
                        if evidence
                        else None,
                    )
                    if production
                    else {}
                ),
                identity=commitment.identity,
                state=submission.state if submission else "prepared",
                invocation_evidence=invocation,
                payload_sha256=attempt.payload_sha256,
                payload_byte_length=commitment.payload_byte_length,
                prompt_sha256=commitment.prompt_sha256,
                prompt_byte_length=commitment.prompt_byte_length,
                shared_input_sha256=commitment.shared_input_sha256,
                shared_input_byte_length=commitment.shared_input_byte_length,
                commitment_sha256=commitment.commitment_sha256,
                transport_receipt_sha256=bytes_digest(canonical_bytes(commitment)),
                limits=commitment.limits,
                complete_response_evidence=ack.response_evidence
                if isinstance(ack, AcknowledgmentReceiptV2)
                else None,
                raw_response=evidence.retained
                if evidence is not None
                and getattr(evidence, "retained_kind", None) != "filtered_replay"
                else None,
                provider_remote_id=ack.remote_id if ack else None,
                local_observation_key=observation.observation_key
                if observation
                else ack.observation_key
                if ack
                else None,
                observation_id=observation.observation_id if observation else None,
                selected_usage=usage,
                usage_selection="revision"
                if evidence
                else "forwarding_fact"
                if fact
                else "unavailable",
            )
        )


def decisions_status(
    repository: Repository, manifest: ExecutableRunManifestV8
) -> DecisionsStatusV1:
    """Inspect indexed safe facts without reopening provider bodies or exports."""
    totals = _Totals()
    artifacts = ArtifactStore(repository.run_directory(manifest.run_id), create=False)
    rows = repository.connection.execute(
        "SELECT a.attempt_id,a.request_uid,a.payload_sha256,s.submission_id,s.state,s.certainty,"
        "o.observation_id,e.receipt_sha256,u.usage_json,u.usage_sha256 "
        "FROM attempts a LEFT JOIN submissions s ON s.run_id=a.run_id AND s.attempt_id=a.attempt_id "
        "LEFT JOIN observations o ON o.run_id=s.run_id AND o.submission_id=s.submission_id "
        "LEFT JOIN evidence_receipts e ON e.run_id=s.run_id AND e.submission_id=s.submission_id "
        "LEFT JOIN usage_revisions u ON u.revision_seq=(SELECT max(v.revision_seq) FROM usage_revisions v WHERE v.run_id=s.run_id AND v.submission_id=s.submission_id) "
        "WHERE a.run_id=? ORDER BY a.request_uid,a.attempt_number",
        (manifest.run_id,),
    )
    for row in rows:
        usage = (
            ProviderUsage.model_validate_json(row["usage_json"])
            if row["usage_json"] is not None
            else None
        )
        if usage is not None and (
            bytes_digest(canonical_bytes(usage)) != row["usage_sha256"]
            or (usage.run_id, usage.request_uid, usage.attempt_id, usage.submission_id)
            != (
                manifest.run_id,
                row["request_uid"],
                row["attempt_id"],
                row["submission_id"],
            )
        ):
            raise IntegrityError(
                "Decisions selected usage differs from immutable identity"
            )
        known = row["receipt_sha256"] is not None or row["certainty"] == "accepted"
        if not known and row["state"] == "uncertain":
            fact = read_invocation(artifacts, row["submission_id"])
            if fact is not None:
                if (
                    fact.identity
                    != physical_identity(
                        manifest.run_id, row["request_uid"], row["attempt_id"]
                    )
                    or fact.payload_sha256 != row["payload_sha256"]
                ):
                    raise IntegrityError("Decisions forwarding identity conflicts")
                registered = repository.connection.execute(
                    "SELECT 1 FROM artifacts WHERE run_id=? AND sha256=?",
                    (manifest.run_id, bytes_digest(canonical_bytes(fact))),
                ).fetchone()
                if registered is not None:
                    expected_fact = (
                        DecisionsInvocationV2
                        if isinstance(manifest, ExecutableRunManifestV9)
                        else DecisionsInvocationV1
                    )
                    if type(fact) is not expected_fact:
                        raise IntegrityError("Decisions forwarding protocol differs")
                    known, usage = True, fact.usage
        totals.record(
            usage,
            known=known,
            unknown=not known and row["state"] not in (None, "prepared"),
            observed=row["observation_id"] is not None,
        )
    detail = totals.detail(manifest)
    directory = artifacts.root / "decisions-provenance"
    latest: tuple[int, int, int, str] | None = None
    if directory.is_dir():
        with os.scandir(directory) as entries:
            for entry in entries:
                if not entry.name.endswith(".json") or not entry.is_file(
                    follow_symlinks=False
                ):
                    raise IntegrityError("Decisions provenance reference is invalid")
                with artifacts.reference_path(
                    f"decisions-provenance/{entry.name}"
                ).open("rb") as stream:
                    raw = stream.read(1_048_577)
                bundle = _parse_manifest(raw)
                if (
                    len(raw) > 1_048_576
                    or canonical_bytes(bundle) != raw
                    or bundle.run_id != manifest.run_id
                    or entry.name != bundle.bundle_id + ".json"
                    or bundle.source_manifest_sha256
                    != bytes_digest(canonical_bytes(manifest))
                    or repository.connection.execute(
                        "SELECT 1 FROM artifacts WHERE run_id=? AND sha256=?",
                        (manifest.run_id, bytes_digest(raw)),
                    ).fetchone()
                    is None
                ):
                    raise IntegrityError(
                        "Decisions provenance reference is inconsistent"
                    )
                key = (
                    bundle.result_high_water_mark,
                    bundle.usage_high_water_mark,
                    bundle.event_high_water_mark,
                    entry.path,
                )
                latest = key if latest is None or key > latest else latest
    return detail.model_copy(
        update={"provenance_manifest": latest[-1] if latest else None}
    )


def _question_rows(
    repository: Repository,
    manifest: ExecutableRunManifestV8,
    physical: PhysicalProvenanceV1,
    result_high: int,
) -> Iterator[QuestionProvenanceV1]:
    artifacts = ArtifactStore(repository.run_directory(manifest.run_id), create=False)
    commitment = read_transport_commitment(artifacts, physical.identity.attempt_id)
    assert isinstance(commitment, DecisionsCommitmentV1)
    results = repository.get_results_for_attempt(
        manifest.run_id, physical.identity.attempt_id
    )
    vector = (
        read_outcomes(artifacts, physical.identity.submission_id)
        if results and physical.observation_id
        else None
    )
    if results:
        if vector is not None:
            repository.get_artifact(
                manifest.run_id, bytes_digest(canonical_bytes(vector))
            )
            if (
                vector.identity != physical.identity
                or vector.commitment_sha256 != commitment.commitment_sha256
                or vector.observation_id != physical.observation_id
                or vector.result_sha256
                != tuple(semantic_digest("normalized-result", r) for r in results)
                or tuple(a.key for a in vector.answers)
                != tuple(q.key for q in commitment.questions)
            ):
                raise IntegrityError(
                    "Decisions outcome attribution differs from committed results"
                )
        elif physical.observation_id is not None:
            raise IntegrityError("committed Decisions response lacks named outcomes")
        maximum = repository.connection.execute(
            "SELECT max(result_seq) FROM results WHERE run_id=? AND attempt_id=?",
            (manifest.run_id, physical.identity.attempt_id),
        ).fetchone()[0]
        if maximum > result_high or tuple(r.task_id for r in results) != tuple(
            q.task_id for q in commitment.questions
        ):
            raise IntegrityError("Decisions result vector differs from export snapshot")
    for ordinal, question in enumerate(commitment.questions):
        result = results[ordinal] if results else None
        yield QuestionProvenanceV1(
            identity=physical.identity,
            question=question,
            source_presentation_id=commitment.source_presentation_id,
            presentation_id=commitment.presentation_id,
            source_record_id=commitment.source_record_id,
            shared_input_sha256=commitment.shared_input_sha256,
            parent_commitment_sha256=commitment.parent_commitment_sha256,
            requested_model=manifest.generation.model,
            reported_model=physical.selected_usage.reported_model
            if physical.selected_usage
            else None,
            observation_id=physical.observation_id,
            answer=vector.answers[ordinal] if vector else None,
            status=result.status if result else None,
            normalized_result_sha256=semantic_digest("normalized-result", result)
            if result
            else None,
            whole_response_valid=vector.whole_response_valid if vector else None,
        )


def _reference(bundle_id: str) -> str:
    return f"decisions-provenance/{bundle_id}.json"


def read_decisions_provenance(
    repository: Repository, run_id: str, bundle_id: str
) -> DecisionsProvenanceReportV1 | DecisionsProvenanceReportV2 | None:
    if len(bundle_id) != 64 or any(c not in "0123456789abcdef" for c in bundle_id):
        raise ValueError("invalid Decisions bundle identity")
    artifacts = ArtifactStore(repository.run_directory(run_id), create=False)
    path = artifacts.reference_path(_reference(bundle_id))
    try:
        with path.open("rb") as stream:
            raw = stream.read(1_048_577)
    except FileNotFoundError:
        return None
    manifest = _parse_manifest(raw)
    if (
        len(raw) > 1_048_576
        or canonical_bytes(manifest) != raw
        or manifest.run_id != run_id
        or manifest.bundle_id != bundle_id
        or [f.name for f in manifest.files]
        != ["questions.jsonl", "questions.csv", "physical.jsonl", "physical.csv"]
    ):
        raise IntegrityError("Decisions provenance manifest is inconsistent")
    expected = semantic_digest(
        "decisions-provenance-v2"
        if isinstance(manifest, DecisionsProvenanceManifestV2)
        else "decisions-provenance-v1",
        [
            run_id,
            manifest.source_manifest_sha256,
            manifest.result_high_water_mark,
            manifest.usage_high_water_mark,
            manifest.event_high_water_mark,
        ],
    )
    if (
        repository.decisions_version(run_id) != 4
        or expected != bundle_id
        or manifest.source_manifest_sha256
        != repository.get_manifest(run_id).artifact.sha256
    ):
        raise IntegrityError("Decisions provenance snapshot identity differs")
    repository.get_artifact(run_id, bytes_digest(raw))
    for file in manifest.files:
        registered = repository.get_artifact(run_id, file.artifact.sha256)
        if (
            registered.byte_length != file.artifact.byte_length
            or file.artifact.media_type
            != ("application/x-ndjson" if file.name.endswith(".jsonl") else "text/csv")
            or file.artifact.relative_path
            != f"objects/{file.artifact.sha256[:2]}/{file.artifact.sha256}"
        ):
            raise IntegrityError("Decisions companion publication metadata differs")
        artifacts.verify(file.artifact)
    report_type = (
        DecisionsProvenanceReportV2
        if isinstance(manifest, DecisionsProvenanceManifestV2)
        else DecisionsProvenanceReportV1
    )
    return report_type.model_validate(
        dict(
            run_id=run_id,
            bundle_id=bundle_id,
            manifest_path=str(path),
            manifest=manifest,
        )
    )


def export_decisions_provenance(
    run_id: str, *, store: str | Path, fault: Callable[[str], None] | None = None
) -> DecisionsProvenanceReportV1 | DecisionsProvenanceReportV2:
    """Publish four complete companions, then their immutable manifest reference."""

    def checkpoint(stage: str) -> None:
        if fault is not None:
            fault(stage)

    with Repository(store) as repository:
        manifest = _load_manifest(repository, run_id, unsealed=False)
        if not isinstance(manifest, ExecutableRunManifestV8):
            raise ValueError("Decisions provenance requires a frozen V4 run")
        artifacts = ArtifactStore(repository.run_directory(run_id), create=False)
        repository.connection.execute("PRAGMA query_only=ON")
        repository.connection.execute("BEGIN")
        try:
            result_high = repository.connection.execute(
                "SELECT coalesce(max(result_seq),0) FROM results WHERE run_id=?",
                (run_id,),
            ).fetchone()[0]
            usage_high = usage_high_water_mark(repository, run_id)
            event_high = repository.connection.execute(
                "SELECT coalesce(max(event_seq),0) FROM events WHERE run_id=? AND kind!='artifact_registered'",
                (run_id,),
            ).fetchone()[0]
            bundle_id = semantic_digest(
                "decisions-provenance-v2"
                if isinstance(manifest, ExecutableRunManifestV9)
                else "decisions-provenance-v1",
                [
                    run_id,
                    repository.get_run(run_id).manifest_sha256,
                    result_high,
                    usage_high,
                    event_high,
                ],
            )
            existing = read_decisions_provenance(repository, run_id, bundle_id)
            if existing is not None:
                return existing
            totals = _Totals()
            with tempfile.TemporaryDirectory(
                prefix=".decisions-export-", dir=artifacts.root
            ) as temporary:
                root = Path(temporary)
                paths = [
                    root / n
                    for n in (
                        "questions.jsonl",
                        "questions.csv",
                        "physical.jsonl",
                        "physical.csv",
                    )
                ]
                counts = [0, 0]
                with (
                    paths[0].open("wb") as questions,
                    paths[1].open("w", encoding="utf-8", newline="") as questions_csv,
                    paths[2].open("wb") as physicals,
                    paths[3].open("w", encoding="utf-8", newline="") as physicals_csv,
                ):
                    question_writer, physical_writer = (
                        csv.writer(questions_csv),
                        csv.writer(physicals_csv),
                    )
                    question_writer.writerow(QuestionProvenanceV1.model_fields)
                    physical_type = (
                        PhysicalProvenanceV2
                        if isinstance(manifest, ExecutableRunManifestV9)
                        else PhysicalProvenanceV1
                    )
                    physical_writer.writerow(physical_type.model_fields)
                    for physical in physical_rows(repository, manifest, usage_high):
                        totals.add(physical)
                        physicals.write(canonical_bytes(physical) + b"\n")
                        physical_writer.writerow(
                            canonical_bytes(v).decode("utf-8")
                            for v in physical.model_dump(mode="json").values()
                        )
                        counts[1] += 1
                        for question in _question_rows(
                            repository, manifest, physical, result_high
                        ):
                            questions.write(canonical_bytes(question) + b"\n")
                            question_writer.writerow(
                                canonical_bytes(v).decode("utf-8")
                                for v in question.model_dump(mode="json").values()
                            )
                            counts[0] += 1
                    for stream in (questions, questions_csv, physicals, physicals_csv):
                        stream.flush()
                        os.fsync(stream.fileno())
                checkpoint("after_provenance_serialization")
                files: list[CompanionFileV1] = []
                for index, path in enumerate(paths):

                    def chunks(selected_path: Path = path) -> Iterator[bytes]:
                        with selected_path.open("rb") as stream:
                            while chunk := stream.read(65_536):
                                yield chunk

                    artifact = artifacts.put_stream(
                        chunks(),
                        "application/x-ndjson"
                        if path.suffix == ".jsonl"
                        else "text/csv",
                    )
                    files.append(
                        CompanionFileV1.model_validate(
                            dict(
                                name=path.name,
                                artifact=artifact,
                                rows=counts[index // 2],
                            )
                        )
                    )
                    checkpoint("after_provenance_artifact")
        finally:
            repository.connection.execute("ROLLBACK")
            repository.connection.execute("PRAGMA query_only=OFF")
        manifest_type = (
            DecisionsProvenanceManifestV2
            if isinstance(manifest, ExecutableRunManifestV9)
            else DecisionsProvenanceManifestV1
        )
        bundle = manifest_type(
            run_id=run_id,
            bundle_id=bundle_id,
            source_manifest_sha256=bytes_digest(canonical_bytes(manifest)),
            result_high_water_mark=result_high,
            usage_high_water_mark=usage_high,
            event_high_water_mark=event_high,
            totals=totals.detail(manifest),
            files=tuple(files),
        )
        identity = artifacts.put(canonical_bytes(bundle), "application/json")
        with repository.transaction():
            for file in bundle.files:
                artifact = file.artifact
                registered = repository.connection.execute(
                    "SELECT byte_length FROM artifacts WHERE run_id=? AND sha256=?",
                    (run_id, artifact.sha256),
                ).fetchone()
                if registered is None:
                    repository._register_artifact(run_id, artifact)  # pyright: ignore[reportPrivateUsage]
                elif registered["byte_length"] != artifact.byte_length:
                    raise IntegrityError(
                        "Decisions companion hash has conflicting byte length"
                    )
                # As in result exports, the manifest authorizes derived bytes;
                # an existing source identity keeps its media type and retention.
            repository._register_artifact(run_id, identity)  # pyright: ignore[reportPrivateUsage]
            checkpoint("during_provenance_registration")
        checkpoint("before_provenance_reference")
        artifacts.publish_reference(identity, _reference(bundle_id))
        checkpoint("after_provenance_reference")
        published = read_decisions_provenance(repository, run_id, bundle_id)
        assert published is not None
        return published
