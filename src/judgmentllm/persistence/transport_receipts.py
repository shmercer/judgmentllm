"""Append-only safe transport references, using the established artifact store.

Content-bearing transport artifacts follow the run capture policy. The receipt
contains only typed identities/hashes and never authorizes provider execution.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

from judgmentllm.persistence.artifacts import ArtifactStore
from judgmentllm.persistence.hashing import bytes_digest, canonical_bytes
from judgmentllm.persistence.models import ArtifactIdentity, ArtifactIntegrityError

if TYPE_CHECKING:
    from judgmentllm.transport import TransportCommitmentV1

_MAX_RECEIPT_BYTES = 1_048_576


def transport_reference(attempt_id: str) -> str:
    return f"transports/{bytes_digest(attempt_id.encode('utf-8'))}.json"


def publish_transport_commitment(
    store: ArtifactStore, commitment: TransportCommitmentV1
) -> ArtifactIdentity:
    """Publish exact immutable metadata before registry preparation commits."""
    from judgmentllm.transport import TransportCommitmentV1

    try:
        checked = TransportCommitmentV1.model_validate_json(canonical_bytes(commitment))
        raw = canonical_bytes(checked)
        if len(raw) > _MAX_RECEIPT_BYTES:
            raise ValueError("oversized transport metadata")
    except (ValueError, TypeError):
        raise ArtifactIntegrityError("transport metadata is invalid") from None
    artifact = store.put(raw, "application/json")
    store.publish_reference(artifact, transport_reference(checked.identity.attempt_id))
    return artifact


def read_transport_commitment(
    store: ArtifactStore, attempt_id: str
) -> TransportCommitmentV1 | None:
    """Read the original receipt; absence is never repaired by reconstruction."""
    from judgmentllm.transport import TransportCommitmentV1

    path = store.reference_path(transport_reference(attempt_id))
    try:
        with path.open("rb") as stream:
            raw = stream.read(_MAX_RECEIPT_BYTES + 1)
    except FileNotFoundError:
        return None
    except OSError:
        raise ArtifactIntegrityError("transport metadata is unavailable") from None
    try:
        checked = TransportCommitmentV1.model_validate_json(raw)
        if (
            len(raw) > _MAX_RECEIPT_BYTES
            or canonical_bytes(checked) != raw
            or checked.identity.attempt_id != attempt_id
        ):
            raise ValueError("inconsistent transport metadata")
    except (ValueError, TypeError):
        raise ArtifactIntegrityError("transport metadata is invalid") from None
    digest = bytes_digest(raw)
    store.verify(
        ArtifactIdentity(
            sha256=digest,
            byte_length=len(raw),
            media_type="application/json",
            relative_path=f"objects/{digest[:2]}/{digest}",
        )
    )
    return checked


def iter_transport_commitments(store: ArtifactStore) -> Iterator[TransportCommitmentV1]:
    """Inspect named safe references in bounded memory, including orphan attempts."""
    from judgmentllm.transport import TransportCommitmentV1

    directory = store.reference_path("transports/.guard").parent
    try:
        entries = os.scandir(directory)
    except FileNotFoundError:
        return
    except OSError:
        raise ArtifactIntegrityError("transport references are unavailable") from None
    with entries:
        for entry in entries:
            if not entry.is_file(follow_symlinks=False) or not entry.name.endswith(
                ".json"
            ):
                raise ArtifactIntegrityError("transport reference is invalid")
            try:
                with open(entry.path, "rb") as stream:
                    raw = stream.read(_MAX_RECEIPT_BYTES + 1)
                receipt = TransportCommitmentV1.model_validate_json(raw)
                if (
                    len(raw) > _MAX_RECEIPT_BYTES
                    or entry.name
                    != Path(transport_reference(receipt.identity.attempt_id)).name
                ):
                    raise ValueError("transport reference identity differs")
                checked = read_transport_commitment(store, receipt.identity.attempt_id)
                if checked is None:
                    raise ValueError("transport reference disappeared")
            except (OSError, ValueError, TypeError):
                raise ArtifactIntegrityError("transport reference is invalid") from None
            yield checked
