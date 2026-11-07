"""Omitted observation bodies have only the documented empty-byte sentinel."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal
from uuid import uuid4

import pytest
from pydantic import ValidationError
from test_auth_cli import private_failure

from judgmentllm.evidence import (
    EvidenceIdentity,
    ProviderObservationMetadata,
    ProviderUsage,
    ReasoningAuthorization,
)
from judgmentllm.persistence.artifacts import ArtifactStore
from judgmentllm.persistence.evidence import (
    AuthorizationCommitment,
    EvidenceReceipt,
    publish_authorization,
    publish_evidence_receipt,
    read_evidence_receipt,
)
from judgmentllm.persistence.hashing import bytes_digest, canonical_bytes
from judgmentllm.persistence.models import ArtifactIdentity
from judgmentllm.persistence.receipts import ResponseReceipt

Capture = Literal["full", "minimal"]


def artifact(capture: Capture, body: bytes) -> ArtifactIdentity:
    digest = bytes_digest(body)
    return ArtifactIdentity(
        sha256=digest,
        byte_length=len(body),
        media_type="application/json",
        relative_path=f"objects/{digest[:2]}/{digest}" if capture == "full" else None,
    )


def receipt(
    capture: Capture,
    *,
    raw: ArtifactIdentity | None = None,
    authorization_sha256: str = "a" * 64,
    observed: bool = True,
) -> EvidenceReceipt:
    identity = EvidenceIdentity(
        run_id="r",
        request_uid="q",
        attempt_id="a",
        submission_id="s",
        observation_id="o" if observed else None,
    )
    retained = (raw or artifact(capture, b"")) if observed else None
    response = (
        ResponseReceipt(
            run_id="r",
            attempt_id="a",
            submission_id="s",
            correlation_id="c",
            payload_sha256="b" * 64,
            observation_id="o",
            observation_key="key",
            kind="response",
            capture=capture,
            raw=retained,
            received_at="2026-11-06T00:00:00Z",
        )
        if retained is not None
        else None
    )
    return EvidenceReceipt(
        identity=identity,
        correlation_id="c",
        payload_sha256="b" * 64,
        acknowledgment_sha256="c" * 64 if observed else None,
        response_receipt_sha256=bytes_digest(canonical_bytes(response))
        if response
        else None,
        response_receipt=response,
        observation_key="key" if observed else None,
        remote_id="remote" if observed else None,
        capture=capture,
        authorization=ReasoningAuthorization(capture=capture),
        authorization_sha256=authorization_sha256,
        usage=ProviderUsage(
            **identity.model_dump(),
            provider="mock",
            endpoint="synthetic",
            requested_model="fixture",
            observation_kind="response" if observed else "invocation",
        ),
        order=ProviderObservationMetadata(
            **identity.model_dump(),
            requested_order=None,
            order_adherence="unknown",
            order_evidence="unavailable",
            unknown_reason="not_applicable",
        )
        if observed
        else None,
        retained=retained,
        response_retention="omitted",
    )


@pytest.mark.parametrize("capture", ["full", "minimal"])
@pytest.mark.parametrize(
    "damage",
    ["nonempty", "wrong_digest", "nonzero_length", "missing_identity", "capture"],
)
@pytest.mark.parametrize("entrypoint", ["constructor", "json", "copy"])
@private_failure
def test_omission_metadata_cannot_hide_nonempty_or_inconsistent_response(
    capture: Capture, damage: str, entrypoint: str
) -> None:
    __tracebackhide__ = True
    original = receipt(capture)
    secret = "synthetic-private-" + uuid4().hex
    values = original.model_dump()
    usage = original.usage.model_copy(update={"requested_model": secret})
    values["usage"] = usage
    raw = original.retained
    assert raw is not None and original.response_receipt is not None
    response = original.response_receipt
    if damage == "nonempty":
        raw = artifact(capture, b"Synthetic retained response body.")
    elif damage == "wrong_digest":
        raw = raw.model_copy(
            update={"sha256": bytes_digest(b"different synthetic bytes")}
        )
    elif damage == "nonzero_length":
        raw = raw.model_copy(update={"byte_length": 1})
    elif damage == "missing_identity":
        raw = None
    else:
        other: Capture = "minimal" if capture == "full" else "full"
        raw = artifact(other, b"")
        response = response.model_copy(update={"capture": other, "raw": raw})
    if raw is not None and damage != "capture":
        response = response.model_copy(update={"raw": raw})
    values.update(
        retained=raw,
        response_receipt=response,
        response_receipt_sha256=bytes_digest(canonical_bytes(response)),
    )
    with pytest.raises(ValidationError) as caught:
        if entrypoint == "constructor":
            EvidenceReceipt(**values)
        elif entrypoint == "json":
            encoded = {
                **original.model_dump(mode="json"),
                **{
                    key: value.model_dump(mode="json")
                    if hasattr(value, "model_dump")
                    else value
                    for key, value in values.items()
                },
            }
            EvidenceReceipt.model_validate_json(json.dumps(encoded))
        else:
            original.model_copy(update=values)
    error = caught.value
    assert secret not in str(error) + repr(error) + repr(error.errors()) + error.json()
    assert all(row["input"] is None for row in error.errors())


@pytest.mark.parametrize("capture", ["full", "minimal"])
def test_legitimate_empty_observation_sentinel_publishes_and_reopens(
    tmp_path: Path, capture: Capture
) -> None:
    store = ArtifactStore(tmp_path / "run")
    policy = ReasoningAuthorization(capture=capture)
    authorization = publish_authorization(
        store,
        AuthorizationCommitment(
            run_id="r",
            manifest_sha256="d" * 64,
            provider="mock",
            authorization=policy,
        ),
    )
    raw = (
        store.put(b"", "application/json")
        if capture == "full"
        else artifact(capture, b"")
    )
    expected = receipt(capture, raw=raw, authorization_sha256=authorization.sha256)
    publish_evidence_receipt(store, expected)
    assert (
        read_evidence_receipt(ArtifactStore(store.root, create=False), "s") == expected
    )
    assert expected.retained is not None
    assert (
        expected.retained.byte_length == 0
        and expected.retained.sha256 == bytes_digest(b"")
    )
    empty_path = store.root / "objects" / bytes_digest(b"")[:2] / bytes_digest(b"")
    assert empty_path.exists() == (capture == "full")
    if capture == "full":
        assert store.read(expected.retained) == b""


@pytest.mark.parametrize("capture", ["full", "minimal"])
def test_definitive_failure_omission_has_no_observation_or_retained_identity(
    tmp_path: Path, capture: Capture
) -> None:
    store = ArtifactStore(tmp_path / "run")
    authorization = publish_authorization(
        store,
        AuthorizationCommitment(
            run_id="r",
            manifest_sha256="d" * 64,
            provider="mock",
            authorization=ReasoningAuthorization(capture=capture),
        ),
    )
    expected = receipt(
        capture, observed=False, authorization_sha256=authorization.sha256
    )
    assert expected.retained is None and expected.response_receipt is None
    publish_evidence_receipt(store, expected)
    assert (
        read_evidence_receipt(ArtifactStore(store.root, create=False), "s") == expected
    )
    with pytest.raises(ValidationError):
        expected.model_copy(update={"retained": artifact(capture, b"")})


@pytest.mark.parametrize("capture", ["full", "minimal"])
def test_publication_revalidates_mutated_omission_before_writing_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capture: Capture
) -> None:
    omitted = receipt(capture)
    assert omitted.response_receipt is not None
    nonempty = artifact(capture, b"Synthetic safe response body.")
    response = omitted.response_receipt.model_copy(update={"raw": nonempty})
    exact = omitted.model_copy(
        update={
            "response_retention": "exact",
            "retained": nonempty,
            "original": ArtifactStore.hash_only(
                nonempty.sha256, nonempty.byte_length, nonempty.media_type
            ),
            "response_receipt": response,
            "response_receipt_sha256": bytes_digest(canonical_bytes(response)),
        }
    )
    # Simulate corrupted in-memory state without invoking a validating entrypoint.
    object.__setattr__(exact, "response_retention", "omitted")
    store = ArtifactStore(tmp_path / "run")
    before = tuple(store.root.rglob("*"))

    def forbidden(*args: object, **kwargs: object) -> ArtifactIdentity:
        raise AssertionError("Invalid omission reached artifact publication")

    monkeypatch.setattr(store, "put", forbidden)
    with pytest.raises(ValueError, match="canonically encoded"):
        publish_evidence_receipt(store, exact)
    assert tuple(store.root.rglob("*")) == before
