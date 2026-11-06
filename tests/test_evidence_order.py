"""Safe emitted-order diagnostics are detached from execution and scoring."""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Any, cast

import pytest
from pydantic import ValidationError
from test_auth_cli import private_failure

from judgmentllm.evidence._base import EvidenceIdentity
from judgmentllm.evidence.order import (
    ObservedMemberOrder,
    OrderEvidence,
    ProviderObservationMetadata,
    extract_observation_metadata,
)
from judgmentllm.models import Level, PairTask, Trait
from judgmentllm.output_commitments import (
    OutputCommitmentV2,
    SchemaSelectionV2,
    _property_orders,  # pyright: ignore[reportPrivateUsage]
)
from judgmentllm.output_contracts import (
    AssessmentOutputContract,
    OutputOrder,
    PairwiseOutputContract,
    RubricOutputContract,
    serialize_output_schema,
)

ORDERS: tuple[OutputOrder, ...] = ("together", "scores_first", "explanations_first")


def identity() -> EvidenceIdentity:
    return EvidenceIdentity(
        run_id="run-001",
        request_uid="request-001",
        attempt_id="attempt-001",
        submission_id="submission-001",
        observation_id="observation-001",
    )


def commitment(
    order: OutputOrder = "together",
    *,
    explanations: bool = True,
    trait_ids: tuple[str, ...] = ("01", "02"),
    pairwise: bool = False,
) -> OutputCommitmentV2:
    contract: AssessmentOutputContract
    if pairwise:
        contract = PairwiseOutputContract(
            pair=PairTask(
                pair_id="p01",
                pair_uid="p01",
                left_item_id="001",
                right_item_id="002",
                trait_id="01",
            ),
            explanations=explanations,
        )
    else:
        contract = RubricOutputContract(
            traits=tuple(
                Trait(
                    trait_id=trait_id,
                    name="Synthetic",
                    description="Synthetic trait",
                    levels=[Level(label="low"), Level(label="high")],
                )
                for trait_id in trait_ids
            ),
            output_order=order,
            explanations=explanations,
        )
    raw = serialize_output_schema(contract)
    root, members = _property_orders(contract)
    return OutputCommitmentV2(
        contract=contract,
        selection=SchemaSelectionV2(
            policy="text",
            native_support="unsupported",
            reason="explicit_text",
        ),
        schema_text=raw.decode(),
        schema_sha256=sha256(raw).hexdigest(),
        root_properties=root,
        member_properties=members,
    )


def payload(
    order: OutputOrder = "together",
    *,
    explanations: bool = True,
    trait_ids: tuple[str, ...] = ("01", "02"),
) -> dict[str, Any]:
    scores: list[dict[str, Any]] = [
        {"trait_id": trait_id, "category_label": "high"} for trait_id in trait_ids
    ]
    if order == "together":
        if explanations:
            for score in scores:
                score["explanation"] = "Synthetic explanation"
        return {"scores": scores}
    texts = [{"trait_id": trait_id, "text": "Synthetic"} for trait_id in trait_ids]
    return (
        {"scores": scores, "explanations": texts}
        if order == "scores_first"
        else {"explanations": texts, "scores": scores}
    )


def inspect(
    raw: str | bytes | None,
    *,
    committed: OutputCommitmentV2 | None = None,
    source: OrderEvidence = "raw_output_text",
) -> ProviderObservationMetadata:
    return extract_observation_metadata(
        committed or commitment(),
        identity=identity(),
        output=raw,
        evidence_source=source,
    )


@pytest.mark.parametrize("order", ORDERS)
@pytest.mark.parametrize("trait_ids", [("01",), ("01", "02")])
@pytest.mark.parametrize(
    "source", ["raw_output_text", "raw_tokens", "reassembled_stream"]
)
def test_complete_matching_layouts(
    order: OutputOrder,
    trait_ids: tuple[str, ...],
    source: OrderEvidence,
) -> None:
    record = inspect(
        json.dumps(payload(order, trait_ids=trait_ids)),
        committed=commitment(order, trait_ids=trait_ids),
        source=source,
    )
    assert record.order_adherence == "adherent"
    assert record.unknown_reason is None
    assert record.requested_order == order
    assert record.order_evidence == source
    assert all(member.trait_id in trait_ids for member in record.member_key_orders)
    assert record == ProviderObservationMetadata.model_validate_json(
        record.model_dump_json()
    )


@pytest.mark.parametrize("order", ORDERS)
def test_member_order_difference_is_diagnostic(order: OutputOrder) -> None:
    body = payload(order)
    body["scores"][0] = dict(reversed(list(body["scores"][0].items())))
    record = inspect(json.dumps(body), committed=commitment(order))
    assert record.order_adherence == "mismatch"
    assert record.member_key_orders[
        0 if order != "explanations_first" else 2
    ].keys == tuple(body["scores"][0])


@pytest.mark.parametrize("order", ["scores_first", "explanations_first"])
def test_root_order_difference(order: OutputOrder) -> None:
    body = dict(reversed(list(payload(order).items())))
    assert (
        inspect(json.dumps(body), committed=commitment(order)).order_adherence
        == "mismatch"
    )


@pytest.mark.parametrize("order", ["scores_first", "explanations_first"])
def test_explanation_member_order_difference(order: OutputOrder) -> None:
    body = payload(order)
    body["explanations"][0] = dict(reversed(list(body["explanations"][0].items())))
    assert (
        inspect(json.dumps(body), committed=commitment(order)).order_adherence
        == "mismatch"
    )


@pytest.mark.parametrize("order", ORDERS)
def test_trait_array_order_is_not_property_order(order: OutputOrder) -> None:
    body = payload(order, trait_ids=("02", "01"))
    if "explanations" in body:
        body["explanations"].reverse()
    record = inspect(json.dumps(body), committed=commitment(order))
    assert record.order_adherence == "adherent"
    first_score = next(
        member for member in record.member_key_orders if member.array == "scores"
    )
    assert first_score.trait_id == "02"
    assert first_score.index == 0


def test_scores_only() -> None:
    record = inspect(
        json.dumps(payload(explanations=False)),
        committed=commitment(explanations=False),
    )
    assert record.order_adherence == "adherent"
    assert record.member_key_orders[0].keys == ("trait_id", "category_label")


def test_pairwise_is_not_applicable() -> None:
    record = inspect('{"winner_id":"001"}', committed=commitment(pairwise=True))
    assert record.requested_order is None
    assert record.order_adherence == "unknown"
    assert record.unknown_reason == "not_applicable"


@pytest.mark.parametrize(
    "raw",
    [
        '{"scores":[],"scores":[]}',
        '{"scores":[{"trait_id":"01","trait_id":"02","category_label":"high"}]}',
        '{"scores":[{"trait_id":"01","category_label":"high","explanation":{"x":1,"x":2}}]}',
        "{",
        '{"scores":[',
        '{"scores":NaN}',
        '{"scores":Infinity}',
        "null",
        "[]",
        b"\xff",
    ],
)
def test_malformed_duplicate_truncated_nonfinite_output(raw: str | bytes) -> None:
    record = inspect(raw)
    assert record.order_adherence == "unknown"
    assert record.unknown_reason in ("malformed_output", "incomplete_evidence")
    assert record.observed_key_order is None
    assert not record.member_key_orders


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"scores": []},
        {"scores": None},
        {"scores": [None]},
        {"scores": [{"trait_id": "01", "category_label": "high"}]},
        {"scores": [{"trait_id": "01"}, {"trait_id": "02"}]},
        {
            "scores": [
                {"trait_id": "01", "category_label": "high", "explanation": "x"},
                {"trait_id": "01", "category_label": "high", "explanation": "x"},
            ]
        },
    ],
)
def test_incomplete_member_evidence(body: dict[str, Any]) -> None:
    record = inspect(json.dumps(body))
    assert record.order_adherence == "unknown"
    assert record.unknown_reason == "incomplete_evidence"


@pytest.mark.parametrize("location", ["root", "member", "trait", "unicode_key"])
@private_failure
def test_untrusted_names_and_ids_never_escape(
    location: str,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "SYNTHETIC_SECRET_unsafe_密钥"
    body = payload()
    if location == "root":
        body[secret] = "sensitive"
    elif location == "member":
        body["scores"][0][secret] = "sensitive"
    elif location == "unicode_key":
        body["scores"][0]["explanatiоn" + secret] = "sensitive"
    else:
        body["scores"][0]["trait_id"] = secret
    record = inspect(json.dumps(body))
    assert record.order_adherence == "unknown"
    assert record.unknown_reason == (
        "incomplete_evidence" if location == "trait" else "unsafe_key_names"
    )
    assert secret not in repr(record)
    assert secret not in str(record.model_dump())
    assert secret not in record.model_dump_json()
    assert secret not in capsys.readouterr().out + capsys.readouterr().err + caplog.text
    if location == "member" or location == "unicode_key":
        assert record.member_key_orders[0].keys is None
    if location == "root":
        assert record.observed_key_order is None
    if location == "trait":
        assert record.member_key_orders[0].trait_id is None


def test_unicode_known_trait_is_preserved_without_normalization() -> None:
    trait_ids = ("文01",)
    record = inspect(
        json.dumps(payload(trait_ids=trait_ids)),
        committed=commitment(trait_ids=trait_ids),
    )
    assert record.order_adherence == "adherent"
    assert record.member_key_orders[0].trait_id == "文01"


def test_sdk_and_unavailable_evidence() -> None:
    record = extract_observation_metadata(
        commitment(),
        identity=identity(),
        output=None,
        evidence_source="unavailable",
        unavailable_reason="sdk_object_only",
    )
    assert record.order_adherence == "unknown"
    assert record.order_evidence == "unavailable"
    assert record.unknown_reason == "sdk_object_only"
    assert inspect(None).unknown_reason == "not_returned"
    assert inspect(None, source="unavailable").unknown_reason == "incomplete_evidence"
    assert inspect(cast(str, {"scores": []})).unknown_reason == "sdk_object_only"
    assert (
        inspect(json.dumps(payload()), source="unavailable").order_adherence
        == "unknown"
    )


@pytest.mark.parametrize(
    "raw",
    [
        '"' + "x" * 1_048_576 + '"',
        "[" * 65 + "]" * 65,
        '"' + "密" * 400_000 + '"',
        '{"scores":[' + ",".join("null" for _ in range(4097)) + "]}",
    ],
)
def test_bounded_inspection(raw: str) -> None:
    record = inspect(raw)
    assert record.order_adherence == "unknown"
    assert record.unknown_reason == "incomplete_evidence"
    assert record.member_key_orders == ()


def test_json_strings_do_not_count_as_nesting() -> None:
    body = payload()
    body["scores"][0]["explanation"] = '" \\ ' + "[" * 100 + "}" * 100
    assert inspect(json.dumps(body)).order_adherence == "adherent"
    assert inspect(json.dumps(body).encode()).order_adherence == "adherent"


@pytest.mark.parametrize(
    "changes",
    [
        {"schema_version": 2},
        {"schema_version": True},
        {"schema_version": 1.0},
        {"order_adherence": "adherent", "unknown_reason": "incomplete_evidence"},
        {"order_adherence": "unknown", "unknown_reason": None},
        {"order_evidence": "unavailable"},
        {"observation_id": None},
        {"requested_order": None},
        {"observed_key_order": None},
        {"observed_key_order": ("scores", "scores")},
        {"member_key_orders": (ObservedMemberOrder(array="scores", index=0),) * 2},
        {"SYNTHETIC_SECRET_untrusted_key": "SYNTHETIC_SECRET_value"},
        {"observed_key_order": ("SYNTHETIC_SECRET_value",)},
    ],
)
@private_failure
def test_strict_public_record_sanitizes_validation(changes: dict[str, Any]) -> None:
    record = inspect(json.dumps(payload()))
    with pytest.raises(ValidationError) as caught:
        ProviderObservationMetadata.model_validate(record.model_dump() | changes)
    assert "SYNTHETIC_SECRET" not in str(caught.value)
    assert "SYNTHETIC_SECRET" not in repr(caught.value)
    assert "SYNTHETIC_SECRET" not in str(caught.value.errors())
    assert "SYNTHETIC_SECRET" not in caught.value.json()


@pytest.mark.parametrize(
    "changes",
    [
        {"index": -1},
        {"index": True},
        {"index": 1.0},
        {"array": "unknown"},
        {"keys": ("trait_id", "trait_id")},
        {"keys": ("SYNTHETIC_SECRET",)},
        {"trait_id": "\nSYNTHETIC_SECRET"},
    ],
)
@private_failure
def test_member_strictness(changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError) as caught:
        ObservedMemberOrder.model_validate({"array": "scores", "index": 0} | changes)
    assert "SYNTHETIC_SECRET" not in str(caught.value.errors())


def test_models_frozen_and_missing_observation_rejected() -> None:
    record = inspect(json.dumps(payload()))
    with pytest.raises(ValidationError):
        record.requested_order = "scores_first"
    with pytest.raises(ValidationError):
        record.member_key_orders[0].index = 1
    no_observation = identity().model_dump() | {"observation_id": None}
    with pytest.raises(ValidationError):
        extract_observation_metadata(
            commitment(),
            identity=EvidenceIdentity.model_validate(no_observation),
            output=json.dumps(payload()),
            evidence_source="raw_output_text",
        )


def test_direct_record_requires_complete_safe_adherence_evidence() -> None:
    record = inspect(json.dumps(payload()))
    for update in (
        {"member_key_orders": ()},
        {"member_key_orders": (ObservedMemberOrder(array="scores", index=0),)},
        {"observed_key_order": ("winner_id",)},
        {
            "member_key_orders": (
                ObservedMemberOrder(
                    array="explanations",
                    index=0,
                    trait_id="01",
                    keys=("trait_id", "text"),
                ),
            )
        },
    ):
        with pytest.raises(ValidationError):
            ProviderObservationMetadata.model_validate(record.model_dump() | update)
    with pytest.raises(ValidationError):
        ObservedMemberOrder(array="explanations", index=0, keys=("category_label",))


def test_commitment_injection_is_rejected_before_inspection() -> None:
    forged = commitment().model_copy(update={"member_properties": ()})
    with pytest.raises(ValueError, match="^invalid_output_commitment$"):
        inspect(json.dumps(payload()), committed=forged)


def test_verified_root_mismatch_survives_incomplete_safe_members() -> None:
    raw = '{"explanations": [], "scores": []}'
    record = inspect(raw, committed=commitment("scores_first"))
    assert record.order_adherence == "mismatch"
    assert record.unknown_reason is None
