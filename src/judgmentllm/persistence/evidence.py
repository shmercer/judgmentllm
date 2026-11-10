"""Versioned safe evidence receipts and append-only repository integration.

All text-bearing provider input has already crossed the pre-retention boundary.
This module accepts only typed safe metadata and protected artifact identities.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from sqlite3 import Row
from typing import TYPE_CHECKING, Annotated, Literal, TypeVar, cast

from pydantic import Field, field_validator, model_validator

from judgmentllm import gemini_types, vertex_types
from judgmentllm.anthropic_assessment import AnthropicAssessmentDiagnosticV2
from judgmentllm.evidence._base import EvidenceIdentity, EvidenceModel
from judgmentllm.evidence.assessment import AssessmentDiagnosticV1
from judgmentllm.evidence.order import ProviderObservationMetadata
from judgmentllm.evidence.reasoning import (
    ReasoningAuthorization,
    ReasoningEvidence,
    authorize_reasoning,
)
from judgmentllm.evidence.usage import ProviderUsage
from judgmentllm.models import ID
from judgmentllm.persistence.artifacts import ArtifactStore
from judgmentllm.persistence.hashing import bytes_digest, canonical_bytes
from judgmentllm.persistence.models import (
    SHA256,
    ArtifactIdentity,
    ArtifactIntegrityError,
    IntegrityError,
)
from judgmentllm.persistence.receipts import (
    AcknowledgmentReceipt,
    ResponseReceipt,
    read_acknowledgment,
    read_receipt,
)

if TYPE_CHECKING:
    from judgmentllm.persistence.repository import Repository

_MAX_RECEIPT_BYTES = 4_194_304
ModelT = TypeVar("ModelT", bound=EvidenceModel)


def evidence_digest(record: EvidenceModel) -> str:
    """Commit exact strict evidence JSON, including nulls and ordered members."""
    return bytes_digest(canonical_bytes(record))


class AuthorizationCommitment(EvidenceModel):
    """Separate immutable authorization; ordinary RunSpec never grants retention."""

    run_id: ID
    manifest_sha256: SHA256
    provider: ID
    authorization: ReasoningAuthorization
    source: Literal["ordinary", "synthetic_fixture"] = "ordinary"

    @model_validator(mode="after")
    def permitted(self) -> AuthorizationCommitment:
        if self.source == "ordinary" and self.authorization.retention != "none":
            raise ValueError("ordinary retention must be none")
        return self


class EvidenceReceipt(EvidenceModel):
    """Companion v1; historical acknowledgment/response bytes stay unchanged."""

    identity: EvidenceIdentity
    correlation_id: ID
    payload_sha256: SHA256
    acknowledgment_sha256: SHA256 | None = None
    response_receipt_sha256: SHA256 | None = None
    response_receipt: ResponseReceipt | None = None
    observation_key: ID | None = None
    remote_id: ID | None = None
    capture: Literal["full", "minimal"]
    authorization: ReasoningAuthorization
    authorization_sha256: SHA256
    usage: ProviderUsage
    order: ProviderObservationMetadata | None = None
    reasoning: Annotated[tuple[ReasoningEvidence, ...], Field(max_length=4096)] = ()
    original: ArtifactIdentity | None = None
    retained: ArtifactIdentity | None = None
    response_retention: Literal["exact", "redacted", "omitted"]
    assessment_diagnostic: (
        AssessmentDiagnosticV1
        | AnthropicAssessmentDiagnosticV2
        | vertex_types.VertexAssessmentDiagnosticV4
        | gemini_types.GeminiAssessmentDiagnosticV3
        | None
    ) = Field(default=None, exclude_if=lambda value: value is None)
    invocation_acceptance: Literal["unresolved"] | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    failure_transient: bool = False
    failure_retry_after: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None = (
        None
    )

    @model_validator(mode="after")
    def consistent(self) -> EvidenceReceipt:
        identity = self.identity
        diagnostic = self.assessment_diagnostic
        if diagnostic is not None and (
            diagnostic.identity != identity
            or diagnostic.capture != self.capture
            or diagnostic.endpoint != self.usage.endpoint
            or diagnostic.requested_model != self.usage.requested_model
            or diagnostic.reported_model != self.usage.reported_model
            or diagnostic.provider_outcome != self.usage.provider_outcome
            or diagnostic.remote_response_id != self.remote_id
            or self.usage.provider != diagnostic.provider
        ):
            raise ValueError("assessment diagnostic identities conflict")
        if self.invocation_acceptance == "unresolved" and (
            identity.observation_id is not None
            or self.failure_transient
            or self.failure_retry_after is not None
            or self.usage.definitive_failure_id is not None
            or self.usage.invocation_failure_code is not None
            or self.original is not None
            or self.response_retention != "omitted"
            or (self.usage.provider, self.usage.endpoint)
            not in {
                ("openai", "responses"),
                ("openai", "chat.completions"),
                ("anthropic", "messages"),
                ("gemini", "models.generate_content"),
                ("vertex", "models.generate_content"),
            }
        ):
            raise ValueError(
                "unresolved invocation cannot claim failure or observation"
            )
        if self.failure_retry_after is not None and not self.failure_transient:
            raise ValueError("retry delay requires transient failure")
        if identity.observation_id is not None and (
            self.failure_transient or self.failure_retry_after is not None
        ):
            raise ValueError("failure policy cannot attach to an observation")
        values = identity.model_dump()
        if (
            EvidenceIdentity.model_validate(
                {key: getattr(self.usage, key) for key in values}
            )
            != identity
        ):
            raise ValueError("usage identity mismatch")
        if self.authorization.capture != self.capture:
            raise ValueError("capture authorization mismatch")
        if self.original is not None and self.original.relative_path is not None:
            raise ValueError("original identity must not retain bytes")
        if identity.observation_id is None:
            if (
                any(
                    value is not None
                    for value in (
                        self.order,
                        self.response_receipt,
                        self.observation_key,
                        self.remote_id,
                        self.retained,
                        self.acknowledgment_sha256,
                        self.response_receipt_sha256,
                    )
                )
                or self.reasoning
            ):
                raise ValueError("failure evidence cannot invent an observation")
            if self.usage.observation_kind != "invocation":
                raise ValueError("failure evidence requires invocation usage")
        else:
            if (
                self.order is None
                or self.response_receipt is None
                or self.observation_key is None
                or self.remote_id is None
                or self.retained is None
                or self.acknowledgment_sha256 is None
            ):
                raise ValueError("observation evidence is incomplete")
            if self.response_receipt.capture != self.capture:
                raise ValueError("response receipt capture policy mismatch")
            if self.response_retention == "omitted" and (
                self.retained.byte_length != 0
                or self.retained.sha256 != bytes_digest(b"")
            ):
                raise ValueError("omitted response requires an empty sentinel")
            if self.response_receipt_sha256 != bytes_digest(
                canonical_bytes(self.response_receipt)
            ):
                raise ValueError("response receipt digest mismatch")
            if self.retained != self.response_receipt.raw:
                raise ValueError("retained response identity mismatch")
            if (
                EvidenceIdentity.model_validate(
                    {key: getattr(self.order, key) for key in values}
                )
                != identity
            ):
                raise ValueError("order identity mismatch")
            previous = -1
            for block in self.reasoning:
                if (
                    EvidenceIdentity.model_validate(
                        {key: getattr(block, key) for key in values}
                    )
                    != identity
                    or block.block_ordinal <= previous
                ):
                    raise ValueError("reasoning identity mismatch")
                previous = block.block_ordinal
                if (
                    block.artifact is not None
                    and not authorize_reasoning(
                        self.authorization, block.kind
                    ).retain_text
                ):
                    raise ValueError("reasoning is unauthorized")
        if (
            self.capture == "minimal"
            and self.retained is not None
            and self.retained.relative_path is not None
        ):
            raise ValueError("minimal response body cannot be retained")
        if self.response_retention == "exact" and (
            self.original is None
            or self.retained is None
            or self.original.sha256 != self.retained.sha256
        ):
            raise ValueError("exact response requires matching source identity")
        return self


class UsageRevision(EvidenceModel):
    """Explicit authoritative correction; revisions replace selection, never add."""

    usage: ProviderUsage
    revision_id: ID
    revision_number: Annotated[int, Field(ge=1)]
    source: Literal["synthetic.authoritative", "provider.authoritative"]
    source_version: Literal[1] = 1

    @field_validator("source_version", mode="before")
    @classmethod
    def strict_source_version(cls, value: object) -> object:
        if type(value) is not int or value != 1:
            raise ValueError("unsupported usage revision source version")
        return value

    @model_validator(mode="after")
    def provenance(self) -> UsageRevision:
        if self.source == "synthetic.authoritative" and self.usage.provider != "mock":
            raise ValueError("synthetic revision requires mock provider")
        if (
            self.source == "provider.authoritative"
            and self.usage.usage_source == "synthetic.reported_usage"
        ):
            raise ValueError("synthetic usage is not provider billing")
        return self


class UsageRevisionRecord(EvidenceModel):
    revision_seq: Annotated[int, Field(ge=1)]
    revision_id: ID
    revision_number: Annotated[int, Field(ge=0)]
    source: Literal["initial", "synthetic.authoritative", "provider.authoritative"]
    source_version: Literal[1] = 1
    usage: ProviderUsage
    usage_sha256: SHA256
    receipt_sha256: SHA256

    @model_validator(mode="after")
    def initial_revision(self) -> UsageRevisionRecord:
        if (self.revision_number == 0) != (self.source == "initial") or (
            self.revision_number == 0 and self.revision_id != "initial"
        ):
            raise ValueError("usage revision identity is inconsistent")
        return self


def _reference(submission_id: str) -> str:
    return f"evidence/{bytes_digest(submission_id.encode('utf-8'))}.json"


def _read(store: ArtifactStore, path: str, model: type[ModelT]) -> ModelT | None:
    try:
        with store.reference_path(path).open("rb") as stream:
            data = stream.read(_MAX_RECEIPT_BYTES + 1)
    except FileNotFoundError:
        return None
    except OSError:
        raise ArtifactIntegrityError("evidence receipt is unavailable") from None
    try:
        if len(data) > _MAX_RECEIPT_BYTES:
            raise ValueError
        record = model.model_validate_json(data)
        if canonical_bytes(record) != data:
            raise ValueError
    except (ValueError, TypeError):
        raise ArtifactIntegrityError("evidence receipt is invalid") from None
    digest = bytes_digest(data)
    store.verify(
        ArtifactIdentity(
            sha256=digest,
            byte_length=len(data),
            media_type="application/json",
            relative_path=f"objects/{digest[:2]}/{digest}",
        )
    )
    return record


def publish_authorization(
    store: ArtifactStore, commitment: AuthorizationCommitment
) -> ArtifactIdentity:
    """Publish once before submission, independently of mutable adapter state."""
    commitment = AuthorizationCommitment.model_validate_json(
        canonical_bytes(commitment)
    )
    identity = store.put(canonical_bytes(commitment), "application/json")
    store.publish_reference(identity, "evidence-authorization.json")
    return identity


def read_authorization(store: ArtifactStore) -> AuthorizationCommitment | None:
    return _read(store, "evidence-authorization.json", AuthorizationCommitment)


def _verify_artifacts(store: ArtifactStore, receipt: EvidenceReceipt) -> None:
    authorization = read_authorization(store)
    if (
        authorization is None
        or authorization.run_id != receipt.identity.run_id
        or authorization.authorization != receipt.authorization
        or evidence_digest(authorization) != receipt.authorization_sha256
    ):
        raise ArtifactIntegrityError("evidence authorization does not match")
    if receipt.retained is not None:
        store.verify(receipt.retained)
    diagnostic = receipt.assessment_diagnostic
    if diagnostic is not None and diagnostic.artifact is not None:
        store.verify(diagnostic.artifact)
    for block in receipt.reasoning:
        if block.artifact is not None:
            if (
                authorization.source != "synthetic_fixture"
                or receipt.usage.provider != "mock"
            ):
                raise ArtifactIntegrityError("reasoning retention is unauthorized")
            artifact = block.artifact
            store.verify(
                ArtifactIdentity(
                    sha256=artifact.sha256,
                    byte_length=artifact.byte_length,
                    media_type="text/plain; charset=utf-8",
                    relative_path=f"objects/{artifact.sha256[:2]}/{artifact.sha256}",
                )
            )


def publish_evidence_receipt(
    store: ArtifactStore, receipt: EvidenceReceipt
) -> ArtifactIdentity:
    """Durably publish metadata before observation/evidence database commit."""
    receipt = EvidenceReceipt.model_validate_json(canonical_bytes(receipt))
    _verify_artifacts(store, receipt)
    identity = store.put(canonical_bytes(receipt), "application/json")
    store.publish_reference(identity, _reference(receipt.identity.submission_id))
    return identity


def read_evidence_receipt(
    store: ArtifactStore, submission_id: str
) -> EvidenceReceipt | None:
    receipt = _read(store, _reference(submission_id), EvidenceReceipt)
    if receipt is not None:
        if receipt.identity.submission_id != submission_id:
            raise ArtifactIntegrityError("evidence submission identity does not match")
        _verify_artifacts(store, receipt)
    return receipt


def _supports(repository: Repository) -> bool:
    return repository.connection.execute("PRAGMA user_version").fetchone()[0] == 2


_VERIFICATION_AUTHORITY = object()


@dataclass(frozen=True)
class VerifiedEvidenceCommit:
    """Factory-bound verification snapshot; no provider or reasoning body bytes.

    Only prepare_evidence_commit issues this object after verifying publication.
    Database identity relationships are checked again within the owned write.
    """

    receipt: EvidenceReceipt
    repository_root: Path
    authorization: AuthorizationCommitment
    acknowledgment: AcknowledgmentReceipt | None
    artifacts: tuple[ArtifactIdentity, ...]
    _authority: object = field(repr=False)

    def verify(self, repository_root: Path, receipt: EvidenceReceipt) -> None:
        """Reject constructed, cross-store or mismatched verification snapshots."""
        if (
            self._authority is not _VERIFICATION_AUTHORITY
            or self.repository_root != repository_root
            or self.receipt != receipt
        ):
            raise IntegrityError("evidence verification does not match its commit")


def _validate_scope(
    repository: Repository,
    receipt: EvidenceReceipt,
    authorization: AuthorizationCommitment,
) -> None:
    identity = receipt.identity
    submission = repository.get_submission(identity.run_id, identity.submission_id)
    attempt = repository.get_attempt(identity.run_id, identity.attempt_id)
    run = repository.get_run(identity.run_id)
    repository.get_request(identity.run_id, identity.request_uid)
    if (
        submission.attempt_id != identity.attempt_id
        or attempt.request_uid != identity.request_uid
        or submission.correlation_id != receipt.correlation_id
        or submission.payload_sha256 != receipt.payload_sha256
        or run.capture != receipt.capture
        or (run.provider, run.endpoint, run.model)
        != (
            receipt.usage.provider,
            receipt.usage.endpoint,
            receipt.usage.requested_model,
        )
        or authorization.run_id != run.run_id
        or authorization.manifest_sha256 != run.manifest_sha256
        or authorization.provider != run.provider
    ):
        raise IntegrityError("evidence execution identities do not match")


def prepare_evidence_commit(
    repository: Repository, receipt: EvidenceReceipt
) -> VerifiedEvidenceCommit:
    """Verify published bytes before starting an observation/evidence transaction."""
    if (
        repository.connection.in_transaction
        and not repository.connection.execute("PRAGMA query_only").fetchone()[0]
    ):
        raise IntegrityError(
            "evidence publication must be verified before a write transaction"
        )
    if not _supports(repository):
        raise IntegrityError(
            "evidence requires a schema-2 store; historical evidence is unavailable"
        )
    receipt = EvidenceReceipt.model_validate_json(canonical_bytes(receipt))
    identity = receipt.identity
    store = ArtifactStore(repository.run_directory(identity.run_id), create=False)
    published = read_evidence_receipt(store, identity.submission_id)
    if published != receipt:
        raise IntegrityError("published evidence receipt does not match")
    authorization = read_authorization(store)
    assert authorization is not None  # verified by the companion reader
    _validate_scope(repository, receipt, authorization)
    acknowledgment = None
    if identity.observation_id is not None:
        acknowledgment = read_acknowledgment(store, identity.submission_id)
        response = read_receipt(store, identity.submission_id)
        if acknowledgment is None or response is None:
            raise IntegrityError("evidence acknowledgment or response is unavailable")
        submission = repository.get_submission(identity.run_id, identity.submission_id)
        acknowledgment.verify_submission(submission)
        response.verify_submission(submission)
        if (
            bytes_digest(canonical_bytes(acknowledgment))
            != receipt.acknowledgment_sha256
            or response != receipt.response_receipt
            or receipt.remote_id != acknowledgment.remote_id
            or receipt.observation_key != acknowledgment.observation_key
        ):
            raise IntegrityError("evidence observation relationship does not match")
    artifacts = [_metadata_identity(receipt)]
    diagnostic = receipt.assessment_diagnostic
    if diagnostic is not None and diagnostic.artifact is not None:
        artifacts.append(diagnostic.artifact)
    for block in receipt.reasoning:
        if block.artifact is not None:
            artifact = block.artifact
            artifacts.append(
                ArtifactIdentity(
                    sha256=artifact.sha256,
                    byte_length=artifact.byte_length,
                    media_type="text/plain; charset=utf-8",
                    relative_path=f"objects/{artifact.sha256[:2]}/{artifact.sha256}",
                )
            )
    return VerifiedEvidenceCommit(
        receipt,
        repository.root,
        authorization,
        acknowledgment,
        tuple(artifacts),
        _VERIFICATION_AUTHORITY,
    )


def _validate_commit_links(
    repository: Repository, prepared: VerifiedEvidenceCommit
) -> None:
    receipt = prepared.receipt
    identity = receipt.identity
    _validate_scope(repository, receipt, prepared.authorization)
    submission = repository.get_submission(identity.run_id, identity.submission_id)
    if identity.observation_id is not None:
        observation = repository.get_observation(
            identity.run_id, identity.observation_id
        )
        acknowledgment = prepared.acknowledgment
        response = receipt.response_receipt
        if acknowledgment is None or response is None:
            raise IntegrityError("verified observation metadata is unavailable")
        acknowledgment.verify_submission(submission)
        response.verify_submission(submission)
        if observation != response.observation():
            raise IntegrityError("evidence observation relationship does not match")
    elif receipt.invocation_acceptance == "unresolved":
        if submission.state != "uncertain" or submission.certainty != "unknown":
            raise IntegrityError("unresolved invocation requires uncertain submission")
    else:
        failure = repository.connection.execute(
            "SELECT 1 FROM submission_failures WHERE run_id=? AND submission_id=? AND attempt_id=?",
            (identity.run_id, identity.submission_id, identity.attempt_id),
        ).fetchone()
        if failure is None or submission.certainty != "not_submitted":
            raise IntegrityError("evidence requires definitive submission failure")


def _validate_links(repository: Repository, receipt: EvidenceReceipt) -> None:
    """Complete inspection verification; readers need no write transaction."""
    prepared = prepare_evidence_commit(repository, receipt)
    _validate_commit_links(repository, prepared)


def record_evidence(
    repository: Repository,
    receipt: EvidenceReceipt,
    *,
    owner_token: str,
    prepared: VerifiedEvidenceCommit | None = None,
) -> None:
    """Insert exactly one initial fact and observation metadata, or verify replay."""
    if not _supports(repository):
        raise IntegrityError(
            "evidence requires a schema-2 store; historical evidence is unavailable"
        )
    receipt = EvidenceReceipt.model_validate_json(canonical_bytes(receipt))
    verified = (
        prepare_evidence_commit(repository, receipt) if prepared is None else prepared
    )
    verified.verify(repository.root, receipt)
    identity = receipt.identity
    with repository.execution_transaction(identity.run_id, owner_token):
        _validate_commit_links(repository, verified)
        existing = repository.connection.execute(
            "SELECT receipt_sha256 FROM evidence_receipts WHERE run_id=? AND submission_id=?",
            (identity.run_id, identity.submission_id),
        ).fetchone()
        if existing is not None:
            if existing["receipt_sha256"] != evidence_digest(receipt):
                raise IntegrityError(
                    "evidence receipt conflicts with committed evidence"
                )
            _verify_committed_metadata(repository, receipt)
            return
        artifact = verified.artifacts[0]
        for artifact_identity in verified.artifacts:
            _register_verified_artifact(repository, identity.run_id, artifact_identity)
        repository.connection.execute(
            "INSERT INTO evidence_receipts VALUES (?,?,?,?,?,?)",
            (
                identity.run_id,
                identity.submission_id,
                identity.attempt_id,
                identity.request_uid,
                identity.observation_id,
                artifact.sha256,
            ),
        )
        usage = canonical_bytes(receipt.usage)
        repository.connection.execute(
            "INSERT INTO usage_revisions(run_id,submission_id,revision_id,revision_number,source,source_version,usage_json,usage_sha256,receipt_sha256) VALUES (?,?,?,0,'initial',1,?,?,?)",
            (
                identity.run_id,
                identity.submission_id,
                "initial",
                usage.decode(),
                bytes_digest(usage),
                artifact.sha256,
            ),
        )
        if receipt.order is not None:
            encoded = canonical_bytes(receipt.order)
            repository.connection.execute(
                "INSERT INTO observation_evidence VALUES (?,?,?,?,?)",
                (
                    identity.run_id,
                    identity.observation_id,
                    identity.submission_id,
                    encoded.decode(),
                    bytes_digest(encoded),
                ),
            )
        for block in receipt.reasoning:
            encoded = canonical_bytes(block)
            artifact_sha = None
            if block.artifact is not None:
                artifact_sha = block.artifact.sha256

            repository.connection.execute(
                "INSERT INTO reasoning_blocks VALUES (?,?,?,?,?,?)",
                (
                    identity.run_id,
                    identity.observation_id,
                    block.block_ordinal,
                    encoded.decode(),
                    bytes_digest(encoded),
                    artifact_sha,
                ),
            )


def get_evidence_for_submission(
    repository: Repository, run_id: str, submission_id: str
) -> EvidenceReceipt | None:
    repository.get_submission(run_id, submission_id)
    if not _supports(repository):
        return None
    row = repository.connection.execute(
        "SELECT receipt_sha256 FROM evidence_receipts WHERE run_id=? AND submission_id=?",
        (run_id, submission_id),
    ).fetchone()
    if row is None:
        return None
    store = ArtifactStore(repository.run_directory(run_id), create=False)
    receipt = read_evidence_receipt(store, submission_id)
    if (
        receipt is None
        or evidence_digest(receipt) != row["receipt_sha256"]
        or receipt.identity.run_id != run_id
    ):
        raise IntegrityError("committed evidence receipt failed verification")
    _verify_committed_metadata(repository, receipt)
    _validate_links(repository, receipt)
    return receipt


def _verify_committed_metadata(
    repository: Repository, receipt: EvidenceReceipt
) -> None:
    run_id, submission_id = receipt.identity.run_id, receipt.identity.submission_id
    order = repository.connection.execute(
        "SELECT order_json,order_sha256 FROM observation_evidence WHERE run_id=? AND observation_id=?",
        (run_id, receipt.identity.observation_id),
    ).fetchone()
    if (order is None) != (receipt.order is None) or (
        order is not None
        and (
            bytes_digest(cast(str, order["order_json"]).encode())
            != order["order_sha256"]
            or canonical_bytes(receipt.order).decode() != order["order_json"]
        )
    ):
        raise IntegrityError("committed observation evidence failed verification")
    blocks = repository.connection.execute(
        "SELECT metadata_json,metadata_sha256 FROM reasoning_blocks WHERE run_id=? AND observation_id=? ORDER BY block_ordinal LIMIT 4097",
        (run_id, receipt.identity.observation_id),
    ).fetchall()
    if len(blocks) != len(receipt.reasoning) or any(
        bytes_digest(cast(str, row["metadata_json"]).encode()) != row["metadata_sha256"]
        or canonical_bytes(block).decode() != row["metadata_json"]
        for row, block in zip(blocks, receipt.reasoning, strict=True)
    ):
        raise IntegrityError("committed reasoning evidence failed verification")
    initial = repository.connection.execute(
        "SELECT revision_id,revision_number,source,source_version,receipt_sha256,usage_json,usage_sha256 FROM usage_revisions WHERE run_id=? AND submission_id=? AND revision_number=0",
        (run_id, submission_id),
    ).fetchone()
    if initial is None:
        raise IntegrityError("committed initial usage failed verification")
    _verify_initial_usage_row(initial, receipt)


def _verify_initial_usage_row(row: Row, receipt: EvidenceReceipt) -> None:
    """Bind revision zero's complete audit metadata to its immutable companion."""
    if (
        row["revision_id"] != "initial"
        or row["revision_number"] != 0
        or row["source"] != "initial"
        or row["source_version"] != 1
        or row["receipt_sha256"] != evidence_digest(receipt)
        or row["usage_json"] != canonical_bytes(receipt.usage).decode()
        or row["usage_sha256"] != evidence_digest(receipt.usage)
    ):
        raise IntegrityError("committed initial usage failed verification")


def get_evidence_for_observation(
    repository: Repository, run_id: str, observation_id: str
) -> EvidenceReceipt | None:
    observation = repository.get_observation(run_id, observation_id)
    receipt = get_evidence_for_submission(repository, run_id, observation.submission_id)
    if _supports(repository) and (
        receipt is None or receipt.identity.observation_id != observation_id
    ):
        raise IntegrityError("committed observation evidence is unavailable")
    return receipt


def iter_evidence(
    repository: Repository, run_id: str, *, page_size: int = 100
) -> Iterator[EvidenceReceipt]:
    repository.get_run(run_id)
    _page_size(page_size)
    if not _supports(repository):
        return
    last = ""
    while True:
        rows = repository.connection.execute(
            "SELECT submission_id FROM evidence_receipts WHERE run_id=? AND submission_id>? ORDER BY submission_id LIMIT ?",
            (run_id, last, page_size),
        ).fetchall()
        if not rows:
            return
        for row in rows:
            last = row["submission_id"]
            receipt = get_evidence_for_submission(repository, run_id, last)
            assert receipt is not None
            yield receipt


def usage_high_water_mark(repository: Repository, run_id: str) -> int:
    repository.get_run(run_id)
    if not _supports(repository):
        return 0
    return repository.connection.execute(
        "SELECT coalesce(max(revision_seq),0) FROM usage_revisions WHERE run_id=?",
        (run_id,),
    ).fetchone()[0]


def usage_revision_history(
    repository: Repository,
    run_id: str,
    submission_id: str,
    *,
    page_size: int = 100,
    high_water_mark: int | None = None,
) -> Iterator[UsageRevisionRecord]:
    _page_size(page_size)
    receipt = get_evidence_for_submission(repository, run_id, submission_id)
    if receipt is None:
        return
    high = (
        usage_high_water_mark(repository, run_id)
        if high_water_mark is None
        else high_water_mark
    )
    if type(high) is not int or high < 0:
        raise ValueError("usage high-water mark must be nonnegative")
    last = 0
    while True:
        rows = repository.connection.execute(
            "SELECT * FROM usage_revisions INDEXED BY idx_usage_revisions_submission_revision WHERE run_id=? AND submission_id=? AND revision_seq>? AND revision_seq<=? ORDER BY revision_seq LIMIT ?",
            (run_id, submission_id, last, high, page_size),
        ).fetchall()
        if not rows:
            return
        for row in rows:
            if row["revision_number"] == 0:
                _verify_initial_usage_row(row, receipt)
            try:
                usage = ProviderUsage.model_validate_json(row["usage_json"])
                if (
                    evidence_digest(usage) != row["usage_sha256"]
                    or EvidenceIdentity.model_validate(
                        {
                            key: getattr(usage, key)
                            for key in receipt.identity.model_dump()
                        }
                    )
                    != receipt.identity
                ):
                    raise ValueError
                if row["revision_number"] > 0:
                    revision = UsageRevision(
                        usage=usage,
                        revision_id=row["revision_id"],
                        revision_number=row["revision_number"],
                        source=row["source"],
                        source_version=row["source_version"],
                    )
                    store = ArtifactStore(
                        repository.run_directory(run_id), create=False
                    )
                    if (
                        _read(
                            store,
                            _revision_reference(submission_id, revision.revision_id),
                            UsageRevision,
                        )
                        != revision
                        or evidence_digest(revision) != row["receipt_sha256"]
                    ):
                        raise ValueError
                result = UsageRevisionRecord(
                    revision_seq=row["revision_seq"],
                    revision_id=row["revision_id"],
                    revision_number=row["revision_number"],
                    source=row["source"],
                    source_version=row["source_version"],
                    usage=usage,
                    usage_sha256=row["usage_sha256"],
                    receipt_sha256=row["receipt_sha256"],
                )
            except (ValueError, TypeError):
                raise IntegrityError("usage revision failed verification") from None
            last = result.revision_seq
            yield result


def authoritative_usage(
    repository: Repository,
    run_id: str,
    submission_id: str,
    *,
    high_water_mark: int | None = None,
) -> ProviderUsage | None:
    latest = None
    for record in usage_revision_history(
        repository, run_id, submission_id, high_water_mark=high_water_mark
    ):
        latest = record.usage
    return latest


def _revision_reference(submission_id: str, revision_id: str) -> str:
    return f"usage-revisions/{bytes_digest(canonical_bytes([submission_id, revision_id]))}.json"


def publish_usage_revision(
    store: ArtifactStore, revision: UsageRevision
) -> ArtifactIdentity:
    revision = UsageRevision.model_validate_json(canonical_bytes(revision))
    artifact = store.put(canonical_bytes(revision), "application/json")
    store.publish_reference(
        artifact,
        _revision_reference(revision.usage.submission_id, revision.revision_id),
    )
    return artifact


def append_usage_revision(
    repository: Repository,
    revision: UsageRevision,
    *,
    owner_token: str,
    fault: Callable[[str, str], None] | None = None,
) -> None:
    """Verify revision publication and history before the short owned write."""
    revision = UsageRevision.model_validate_json(canonical_bytes(revision))
    identity = revision.usage
    receipt = get_evidence_for_submission(
        repository, identity.run_id, identity.submission_id
    )
    if (
        receipt is None
        or EvidenceIdentity.model_validate(
            {key: getattr(identity, key) for key in receipt.identity.model_dump()}
        )
        != receipt.identity
    ):
        raise IntegrityError("usage revision identity does not match")
    if (identity.provider, identity.endpoint, identity.requested_model) != (
        receipt.usage.provider,
        receipt.usage.endpoint,
        receipt.usage.requested_model,
    ):
        raise IntegrityError("usage revision scope does not match")
    prepared = prepare_evidence_commit(repository, receipt)
    store = ArtifactStore(repository.run_directory(identity.run_id), create=False)
    if (
        _read(
            store,
            _revision_reference(identity.submission_id, revision.revision_id),
            UsageRevision,
        )
        != revision
    ):
        raise IntegrityError("usage revision receipt does not match")
    last_revision, last_sequence = -1, 0
    replay = False
    for record in usage_revision_history(
        repository, identity.run_id, identity.submission_id
    ):
        last_revision, last_sequence = record.revision_number, record.revision_seq
        if (
            record.revision_id == revision.revision_id
            or record.revision_number == revision.revision_number
        ):
            if (
                record.revision_id == revision.revision_id
                and record.revision_number == revision.revision_number
                and record.receipt_sha256 == evidence_digest(revision)
            ):
                replay = True
            else:
                raise IntegrityError("usage revision conflicts with committed history")
    if not replay and revision.revision_number != last_revision + 1:
        raise IntegrityError("usage revision sequence is not consecutive")
    if fault is not None:
        fault("after_revision_receipt", revision.revision_id)
    with repository.execution_transaction(identity.run_id, owner_token):
        _validate_commit_links(repository, prepared)
        _verify_committed_metadata(repository, receipt)
        current_sequence = repository.connection.execute(
            "SELECT coalesce(max(revision_seq),0) FROM usage_revisions WHERE run_id=? AND submission_id=?",
            (identity.run_id, identity.submission_id),
        ).fetchone()[0]
        if current_sequence != last_sequence:
            raise IntegrityError("usage revision history changed during verification")
        if replay:
            return
        artifact = _metadata_identity(revision)
        _register_verified_artifact(repository, identity.run_id, artifact)
        repository.connection.execute(
            "INSERT INTO usage_revisions(run_id,submission_id,revision_id,revision_number,source,source_version,usage_json,usage_sha256,receipt_sha256) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                identity.run_id,
                identity.submission_id,
                revision.revision_id,
                revision.revision_number,
                revision.source,
                revision.source_version,
                canonical_bytes(identity).decode(),
                evidence_digest(identity),
                artifact.sha256,
            ),
        )
        if fault is not None:
            fault("during_usage_revision", revision.revision_id)
    if fault is not None:
        fault("after_usage_revision", revision.revision_id)


def _metadata_identity(record: EvidenceModel) -> ArtifactIdentity:
    encoded = canonical_bytes(record)
    digest = bytes_digest(encoded)
    return ArtifactIdentity(
        sha256=digest,
        byte_length=len(encoded),
        media_type="application/json",
        relative_path=f"objects/{digest[:2]}/{digest}",
    )


def _page_size(page_size: int) -> None:
    if type(page_size) is not int or not 1 <= page_size <= 10000:
        raise ValueError("page size must be between 1 and 10000")


def _register_verified_artifact(
    repository: Repository, run_id: str, artifact: ArtifactIdentity
) -> None:
    """Share verified bytes while preserving the first registered metadata.

    Immutable receipts retain their independent permitted paths and media types.
    The registry's existing content identity must never be rewritten when those
    same bytes also serve as another artifact, including an empty export.
    """
    row = repository.connection.execute(
        "SELECT byte_length FROM artifacts WHERE run_id=? AND sha256=?",
        (run_id, artifact.sha256),
    ).fetchone()
    if row is not None:
        if row["byte_length"] != artifact.byte_length:
            raise IntegrityError(
                "verified artifact length conflicts with registered identity"
            )
        return
    repository._register_artifact(run_id, artifact)  # pyright: ignore[reportPrivateUsage]
