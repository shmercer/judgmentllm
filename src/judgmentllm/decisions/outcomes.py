"""Safe named-answer attribution, published with an atomic result vector."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from judgmentllm.decisions._base import DecisionsModel
from judgmentllm.models import ID
from judgmentllm.persistence.artifacts import ArtifactStore
from judgmentllm.persistence.hashing import bytes_digest, canonical_bytes
from judgmentllm.persistence.models import (
    SHA256,
    ArtifactIdentity,
    ArtifactIntegrityError,
)
from judgmentllm.transport import PhysicalRequestIdentityV1


class AnswerAttributionV1(DecisionsModel):
    key: str
    answer_ordinals: tuple[int, ...]
    answer_type: Literal["score", "choice", "refusal", "invalid", "unavailable"]


class DecisionsOutcomeVectorV1(DecisionsModel):
    outcome_vector_version: Literal[1] = 1
    identity: PhysicalRequestIdentityV1
    observation_id: ID
    response_sha256: SHA256
    commitment_sha256: SHA256
    recognizable: bool
    whole_response_valid: bool
    unexpected_members: int = Field(ge=0)
    unattributable_members: int = Field(ge=0)
    answers: tuple[AnswerAttributionV1, ...]
    result_sha256: tuple[SHA256, ...]


def outcome_reference(submission_id: str) -> str:
    return f"decisions-outcomes/{bytes_digest(submission_id.encode('utf-8'))}.json"


def publish_outcomes(
    store: ArtifactStore, vector: DecisionsOutcomeVectorV1
) -> ArtifactIdentity:
    artifact = store.put(canonical_bytes(vector), "application/json")
    store.publish_reference(artifact, outcome_reference(vector.identity.submission_id))
    return artifact


def read_outcomes(
    store: ArtifactStore, submission_id: str
) -> DecisionsOutcomeVectorV1 | None:
    try:
        with store.reference_path(outcome_reference(submission_id)).open(
            "rb"
        ) as stream:
            raw = stream.read(8_388_609)
    except FileNotFoundError:
        return None
    except OSError:
        raise ArtifactIntegrityError("Decisions outcomes are unavailable") from None
    try:
        vector = DecisionsOutcomeVectorV1.model_validate_json(raw)
        if (
            len(raw) > 8_388_608
            or canonical_bytes(vector) != raw
            or vector.identity.submission_id != submission_id
        ):
            raise ValueError
        digest = bytes_digest(raw)
        store.verify(
            ArtifactIdentity(
                sha256=digest,
                byte_length=len(raw),
                media_type="application/json",
                relative_path=f"objects/{digest[:2]}/{digest}",
            )
        )
    except (ValueError, TypeError):
        raise ArtifactIntegrityError("Decisions outcomes are invalid") from None
    return vector
