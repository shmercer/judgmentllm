"""Immutable reservation evidence. Only an owned submitting event is a debit."""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING, Literal

from pydantic import Field, model_validator

from judgmentllm.config import BulkLimits
from judgmentllm.models import ID
from judgmentllm.persistence.artifacts import ArtifactStore
from judgmentllm.persistence.hashing import bytes_digest, canonical_bytes
from judgmentllm.persistence.models import (
    SHA256,
    ArtifactIdentity,
    ExecutionModel,
    IntegrityError,
)

if TYPE_CHECKING:
    from judgmentllm.persistence.repository import Repository

_MEDIA_TYPE = "application/vnd.judgmentllm.bulk-reservation+json"
_MAX_RECEIPT = 1_048_576


class BulkReservation(ExecutionModel):
    run_id: ID
    request_uid: ID
    attempt_id: ID
    submission_id: ID
    manifest_sha256: SHA256
    payload_sha256: SHA256
    limits: BulkLimits | None
    method: Literal["request_v1", "synthetic_utf8_v1"]
    requests: Literal[1] = 1
    tokens: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def bound_matches_limit(self) -> BulkReservation:
        cap = self.limits.tokens_per_minute if self.limits else None
        if cap is None:
            if self.tokens is not None or self.method != "request_v1":
                raise ValueError("unexpected token reservation")
        elif (
            self.tokens is None
            or self.tokens > cap
            or self.method != "synthetic_utf8_v1"
        ):
            raise ValueError("token bound is unavailable or exceeds the per-minute cap")
        return self


class CommittedBulkReservation(ExecutionModel):
    event_seq: int = Field(gt=0)
    receipt_sha256: SHA256
    receipt: BulkReservation


def reservation_reference(submission_id: str) -> str:
    return f"bulk/{bytes_digest(submission_id.encode('utf-8'))}.json"


def publish_reservation(
    store: ArtifactStore, receipt: BulkReservation
) -> ArtifactIdentity:
    raw = canonical_bytes(receipt)
    if len(raw) > _MAX_RECEIPT:
        raise IntegrityError("bulk reservation metadata exceeds its size limit")
    artifact = store.put(raw, _MEDIA_TYPE)
    store.publish_reference(artifact, reservation_reference(receipt.submission_id))
    return artifact


def committed_reservations(
    repository: Repository,
    run_id: str,
    manifest_sha256: str,
    limits: BulkLimits | None,
    *,
    event_cut: int,
) -> Iterator[CommittedBulkReservation]:
    """Read in attempt order from the caller's owned or read-snapshot connection."""
    connection = repository.connection
    counts = connection.execute(
        """
        SELECT
          (SELECT count(*) FROM submissions WHERE run_id=? AND state!='prepared'),
          (SELECT count(*) FROM artifacts WHERE run_id=? AND media_type=?),
          (SELECT count(*) FROM events WHERE run_id=? AND event_seq<=? AND
            kind='transition' AND entity_kind='submission' AND new_state='submitting')
        """,
        (run_id, run_id, _MEDIA_TYPE, run_id, event_cut),
    ).fetchone()
    if not counts[0] == counts[1] == counts[2]:
        raise IntegrityError("bulk reservation journal counts differ")
    if not counts[0]:
        return
    store = ArtifactStore(repository.run_directory(run_id), create=False)
    events = connection.execute(
        """
        SELECT debit.event_seq, debit.entity_id AS submission_id, s.payload_sha256,
               a.request_uid, a.attempt_id, a.payload_sha256 AS attempt_payload_sha256,
               artifact.sha256, artifact.byte_length, artifact.media_type, artifact.relative_path
          FROM events debit
          JOIN submissions s ON s.run_id=debit.run_id AND s.submission_id=debit.entity_id
          JOIN attempts a ON a.run_id=s.run_id AND a.attempt_id=s.attempt_id
          JOIN requests r ON r.run_id=a.run_id AND r.request_uid=a.request_uid
          JOIN events attempt ON attempt.event_seq=debit.event_seq-1
          JOIN events registration ON registration.event_seq=debit.event_seq-2
          JOIN artifacts artifact ON artifact.run_id=debit.run_id
                                 AND artifact.sha256=registration.entity_id
         WHERE debit.run_id=? AND debit.event_seq<=? AND debit.kind='transition'
           AND debit.entity_kind='submission' AND debit.previous_state='prepared'
           AND debit.new_state='submitting' AND s.state!='prepared'
           AND attempt.run_id=debit.run_id AND attempt.owner_token=debit.owner_token
           AND attempt.kind='transition' AND attempt.entity_kind='attempt'
           AND attempt.entity_id=a.attempt_id AND attempt.previous_state='claimed'
           AND attempt.new_state='submitting'
           AND registration.run_id=debit.run_id AND registration.owner_token=debit.owner_token
           AND registration.kind='artifact_registered' AND registration.entity_kind='artifact'
           AND artifact.media_type=?
         ORDER BY r.scheduling_ordinal, a.attempt_number, debit.event_seq
        """,
        (run_id, event_cut, _MEDIA_TYPE),
    )
    previous = None
    seen = 0
    for row in events:
        if row["attempt_id"] == previous or row["byte_length"] > _MAX_RECEIPT:
            raise IntegrityError("bulk reservation is duplicated or oversized")
        previous = row["attempt_id"]
        artifact = ArtifactIdentity.model_validate(
            {
                key: row[key]
                for key in ("sha256", "byte_length", "media_type", "relative_path")
            }
        )
        raw = store.read(artifact)
        reference = store.reference_path(reservation_reference(row["submission_id"]))
        try:
            with reference.open("rb") as stream:
                if stream.read(_MAX_RECEIPT + 1) != raw:
                    raise IntegrityError("bulk reservation reference differs")
            receipt = BulkReservation.model_validate_json(raw)
        except (OSError, ValueError):
            raise IntegrityError(
                "bulk reservation reference or metadata is invalid"
            ) from None
        if (
            canonical_bytes(receipt) != raw
            or receipt.run_id != run_id
            or receipt.request_uid != row["request_uid"]
            or receipt.attempt_id != row["attempt_id"]
            or receipt.submission_id != row["submission_id"]
            or receipt.manifest_sha256 != manifest_sha256
            or receipt.payload_sha256 != row["payload_sha256"]
            or receipt.payload_sha256 != row["attempt_payload_sha256"]
            or receipt.limits != limits
        ):
            raise IntegrityError("bulk reservation differs from the sealed submission")
        seen += 1
        yield CommittedBulkReservation(
            event_seq=row["event_seq"], receipt_sha256=artifact.sha256, receipt=receipt
        )
    if seen != counts[0]:
        raise IntegrityError("bulk reservation lacks its owned debit transaction")


def event_high_water(repository: Repository, run_id: str) -> int:
    row = repository.connection.execute(
        "SELECT coalesce(max(event_seq),0) FROM events WHERE run_id=? AND kind!='artifact_registered'",
        (run_id,),
    ).fetchone()
    return int(row[0])


class ReservationRows:
    """Pair two bounded streams in original request and attempt order."""

    def __init__(self, rows: Iterator[CommittedBulkReservation]) -> None:
        self.rows = rows
        self.current = next(rows, None)

    def for_attempt(self, attempt_id: str) -> CommittedBulkReservation | None:
        item = self.current
        if item is not None and item.receipt.attempt_id == attempt_id:
            self.current = next(self.rows, None)
            return item
        return None

    def finish(self) -> None:
        if self.current is not None:
            raise IntegrityError("bulk reservation has no ordered audit attempt")
