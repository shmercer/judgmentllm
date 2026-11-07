"""Internal immutable capture grants, separate from RunSpec and manifests."""

from __future__ import annotations

from judgmentllm.evidence.reasoning import ReasoningAuthorization
from judgmentllm.persistence.artifacts import ArtifactStore
from judgmentllm.persistence.evidence import (
    AuthorizationCommitment,
    publish_authorization,
)
from judgmentllm.persistence.hashing import bytes_digest, canonical_bytes
from judgmentllm.persistence.models import ArtifactIdentity, IntegrityError
from judgmentllm.providers import MockAdapter, ProviderAdapter


def committed_authorization(
    store: ArtifactStore,
    *,
    run_id: str,
    manifest_sha256: str,
    provider: str,
    capture: str,
    adapter: ProviderAdapter,
) -> tuple[ReasoningAuthorization, str]:
    """Publish before submission; reopening uses the immutable grant unchanged."""
    supplied = getattr(adapter, "evidence_authorization", None)
    if supplied is not None and not isinstance(supplied, ReasoningAuthorization):
        raise IntegrityError("invalid evidence authorization")
    policy = supplied or ReasoningAuthorization.model_validate({"capture": capture})
    if policy.capture != capture or (
        policy.retention != "none"
        and (provider != "mock" or not isinstance(adapter, MockAdapter))
    ):
        raise IntegrityError("evidence authorization is not permitted")
    relative = "evidence-authorization.json"
    path = store.reference_path(relative)
    try:
        with path.open("rb") as stream:
            encoded = stream.read(65537)
    except FileNotFoundError:
        encoded = None
    except OSError:
        raise IntegrityError("evidence authorization is unavailable") from None
    if encoded is not None:
        try:
            existing = AuthorizationCommitment.model_validate_json(encoded)
            if len(encoded) > 65536 or canonical_bytes(existing) != encoded:
                raise ValueError
            if (existing.run_id, existing.manifest_sha256, existing.provider) != (
                run_id,
                manifest_sha256,
                provider,
            ) or existing.authorization.capture != capture:
                raise ValueError
            if supplied is not None and supplied != existing.authorization:
                raise ValueError
            if existing.authorization.retention != "none" and (
                provider != "mock" or not isinstance(adapter, MockAdapter)
            ):
                raise ValueError
        except ValueError:
            raise IntegrityError("evidence authorization conflicts") from None
        digest = bytes_digest(encoded)
        store.verify(
            ArtifactIdentity(
                sha256=digest,
                byte_length=len(encoded),
                media_type="application/json",
                relative_path=f"objects/{digest[:2]}/{digest}",
            )
        )
        return existing.authorization, digest
    commitment = AuthorizationCommitment(
        run_id=run_id,
        manifest_sha256=manifest_sha256,
        provider=provider,
        authorization=policy,
        source="synthetic_fixture" if policy.retention != "none" else "ordinary",
    )
    artifact = publish_authorization(store, commitment)
    return policy, artifact.sha256
