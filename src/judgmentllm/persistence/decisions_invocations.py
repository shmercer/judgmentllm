"""Immutable forwarding facts, without asserting acceptance or a provider ID."""

from __future__ import annotations

from typing import ClassVar, Literal, cast

from pydantic import model_validator

from judgmentllm.decisions._base import DecisionsModel
from judgmentllm.evidence.usage import ProviderUsage
from judgmentllm.persistence.artifacts import ArtifactStore
from judgmentllm.persistence.hashing import bytes_digest, canonical_bytes
from judgmentllm.persistence.models import (
    SHA256,
    ArtifactIdentity,
    ArtifactIntegrityError,
)
from judgmentllm.transport import PhysicalRequestIdentityV1


class DecisionsInvocationV1(DecisionsModel):
    _route: ClassVar[tuple[str, str]] = ("mock", "synthetic")
    invocation_evidence_version: Literal[1] = 1
    identity: PhysicalRequestIdentityV1
    payload_sha256: SHA256
    known_forwarded: Literal[True] = True
    usage: ProviderUsage

    @model_validator(mode="after")
    def bound(self) -> DecisionsInvocationV1:
        if (
            any(
                getattr(self.usage, key) != getattr(self.identity, key)
                for key in ("run_id", "request_uid", "attempt_id", "submission_id")
            )
            or self.usage.observation_id is not None
            or self.usage.observation_kind != "invocation"
            or self.usage.definitive_failure_id is not None
            or self.usage.invocation_failure_code is not None
            or (self.usage.provider, self.usage.endpoint) != self._route
        ):
            raise ValueError("forwarding fact cannot claim acceptance or failure")
        return self


class DecisionsInvocationV2(DecisionsInvocationV1):
    _route: ClassVar[tuple[str, str]] = ("openai", "decisions")
    invocation_evidence_version: Literal[2] = 2  # pyright: ignore[reportIncompatibleVariableOverride]


class DecisionsInvocationV3(DecisionsInvocationV1):
    _route: ClassVar[tuple[str, str]] = ("typesafe", "systemone")
    invocation_evidence_version: Literal[3] = 3  # pyright: ignore[reportIncompatibleVariableOverride]


class DecisionsInvocationV4(DecisionsInvocationV1):
    _route: ClassVar[tuple[str, str]] = ("gemini", "decisions")
    invocation_evidence_version: Literal[4] = 4  # pyright: ignore[reportIncompatibleVariableOverride]


def invocation_reference(submission_id: str) -> str:
    return f"decisions-invocations/{bytes_digest(submission_id.encode('utf-8'))}.json"


def publish_invocation(
    store: ArtifactStore, fact: DecisionsInvocationV1
) -> ArtifactIdentity:
    raw = canonical_bytes(fact)
    artifact = store.put(raw, "application/json")
    store.publish_reference(artifact, invocation_reference(fact.identity.submission_id))
    return artifact


def read_invocation(
    store: ArtifactStore, submission_id: str
) -> DecisionsInvocationV1 | None:
    try:
        with store.reference_path(invocation_reference(submission_id)).open(
            "rb"
        ) as stream:
            raw = stream.read(1_048_577)
    except FileNotFoundError:
        return None
    except OSError:
        raise ArtifactIntegrityError(
            "Decisions forwarding fact is unavailable"
        ) from None
    try:
        from judgmentllm.config import parse_json

        value = cast(dict[str, object], parse_json(raw.decode("utf-8")))
        version = (
            value.get("invocation_evidence_version")
            if isinstance(cast(object, value), dict)
            else None
        )
        model = (
            DecisionsInvocationV4
            if type(version) is int and version == 4
            else DecisionsInvocationV3
            if type(version) is int and version == 3
            else DecisionsInvocationV2
            if type(version) is int and version == 2
            else DecisionsInvocationV1
        )
        fact = model.model_validate_json(raw)
        if (
            len(raw) > 1_048_576
            or canonical_bytes(fact) != raw
            or fact.identity.submission_id != submission_id
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
        raise ArtifactIntegrityError("Decisions forwarding fact is invalid") from None
    return fact
