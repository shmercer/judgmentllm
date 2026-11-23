"""Native batch contracts: operation identity never substitutes for an attempt."""

from pathlib import Path

import pytest

from judgmentllm.batch_models import (
    BatchControlOptionsV1,
    BatchCursorV1,
    BatchLimitsV1,
    BatchOperationIntentV1,
    BatchOperationReceiptV1,
)
from judgmentllm.persistence.hashing import canonical_bytes


@pytest.mark.parametrize("value", [True, 0, -1, 1.5, "2"])
def test_batch_limits_are_strict(value: object) -> None:
    with pytest.raises(ValueError):
        BatchLimitsV1.model_validate({"max_members": value})


def test_batch_control_has_finite_shared_bounds() -> None:
    options = BatchControlOptionsV1()
    assert (options.deadline_seconds, options.max_polls) == (30, 1)
    assert (options.max_io_steps, options.max_result_rows) == (1024, 128)
    for field, value in (("deadline_seconds", float("inf")), ("max_io_steps", 0)):
        with pytest.raises(ValueError):
            BatchControlOptionsV1.model_validate({field: value})


def test_operation_cap_and_receipt_binding_are_explicit() -> None:
    data = dict(
        run_id="001",
        shard_id="shard-01",
        logical_id="a" * 64,
        instance_id="b" * 64,
        sequence=1,
        kind="create",
        service_id="service-01",
        manifest_sha256="c" * 64,
        payload_sha256="d" * 64,
        owner_token="owner-01",
    )
    intent = BatchOperationIntentV1.model_validate(data)
    assert "attempt_id" not in type(intent).model_fields
    with pytest.raises(ValueError):
        BatchOperationIntentV1.model_validate({**data, "sequence": 4})
    with pytest.raises(ValueError):
        BatchOperationReceiptV1.model_validate(
            {
                "instance_id": intent.instance_id,
                "intent_sha256": "e" * 64,
                "outcome": "not_accepted",
            }
        )


def test_cursor_is_typed_frozen_and_not_a_provider_token(tmp_path: Path) -> None:
    cursor = BatchCursorV1(
        run_id="001",
        submission_id="s-01",
        source_id="a" * 64,
        position=0,
        prefix_sha256="b" * 64,
    )
    assert cursor.run_id == "001"
    with pytest.raises(ValueError):
        cursor.model_copy(update={"provider_token": "untrusted"})
    with pytest.raises(ValueError):
        cursor.position = 2  # type: ignore[misc]


def test_historical_cursor_does_not_gain_source_exhaustion_proof() -> None:
    raw = (
        b'{"position":1,"prefix_sha256":"'
        + b"b" * 64
        + b'","run_id":"001","schema_version":1,"source_id":"'
        + b"a" * 64
        + b'","submission_id":"s-01"}'
    )
    cursor = BatchCursorV1.model_validate_json(raw)
    assert cursor.exhausted is False
    assert canonical_bytes(cursor) == raw
    assert b'"exhausted":true' in canonical_bytes(
        cursor.model_copy(update={"exhausted": True})
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"sequence": 2},
        {"predecessor_sha256": "f" * 64},
        {"kind": "cancel"},
    ],
)
def test_operation_rejects_missing_or_unexpected_predecessor_and_job(
    changes: dict[str, object],
) -> None:
    fields: dict[str, object] = dict(
        run_id="001",
        shard_id="shard",
        logical_id="a" * 64,
        instance_id="b" * 64,
        sequence=1,
        kind="upload",
        service_id="service",
        manifest_sha256="c" * 64,
        payload_sha256="d" * 64,
        owner_token="owner",
    )
    assert BatchOperationIntentV1.model_validate(fields).sequence == 1
    with pytest.raises(ValueError):
        BatchOperationIntentV1.model_validate({**fields, **changes})


@pytest.mark.parametrize("outcome", ["accepted", "unresolved"])
def test_receipt_rejects_missing_job_or_inapplicable_fence(outcome: str) -> None:
    fields: dict[str, object] = dict(
        instance_id="a" * 64,
        intent_sha256="b" * 64,
        outcome=outcome,
    )
    if outcome == "unresolved":
        fields["fence"] = dict(
            service_id="service",
            instance_id="a" * 64,
            intent_sha256="b" * 64,
            sender_token="owner",
            proof_sha256="c" * 64,
        )
    with pytest.raises(ValueError):
        BatchOperationReceiptV1.model_validate(fields)


@pytest.mark.parametrize(
    "fault",
    [
        "original-path",
        "minimal-path",
        "redacted-original",
        "submission",
        "attempt",
        "observation",
    ],
)
def test_member_receipt_rejects_retention_and_physical_identity_conflicts(
    fault: str,
) -> None:
    from judgmentllm.batch_models import BatchMemberReceiptV1

    identity = dict(
        run_id="001",
        request_uid="request",
        attempt_id="attempt",
        submission_id="submission",
        observation_id="observation",
    )
    original: dict[str, object] = dict(
        sha256="c" * 64,
        byte_length=1,
        media_type="application/json",
        relative_path=None,
    )
    retained = {**original, "sha256": "d" * 64}
    usage: dict[str, object] = dict(
        **identity,
        provider="mock",
        endpoint="synthetic",
        requested_model="mock",
        observation_kind="response",
    )
    fields: dict[str, object] = dict(
        identity=identity,
        member_key="a" * 64,
        source_id="b" * 64,
        source_position=0,
        observation_key="key",
        kind="response",
        capture="full",
        original=original,
        retained=retained,
        disposition="exact",
        usage=usage,
    )
    assert BatchMemberReceiptV1.model_validate(fields).identity.run_id == "001"
    if fault == "original-path":
        original["relative_path"] = "objects/cc/" + "c" * 64
    elif fault == "minimal-path":
        fields["capture"] = "minimal"
        retained["relative_path"] = "objects/dd/" + "d" * 64
    elif fault == "redacted-original":
        fields["disposition"] = "redacted"
    else:
        usage[fault + "_id"] = "different"
    with pytest.raises(ValueError):
        BatchMemberReceiptV1.model_validate(fields)
