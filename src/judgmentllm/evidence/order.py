"""Bounded, transient inspection of exact emitted JSON property order.

The extractor reports diagnostics only. It never decides assessment validity,
changes a score, persists response text, or authorizes a submission or retry.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Literal, cast

from pydantic import Field, model_validator

from judgmentllm.evidence._base import EvidenceIdentity, EvidenceModel
from judgmentllm.models import ID
from judgmentllm.output_commitments import OutputCommitmentV2
from judgmentllm.output_contracts import (
    OutputOrder,
    RubricOutputContract,
    decode_assessment_output,
)

if TYPE_CHECKING:
    from judgmentllm.transport import OutputCommitmentV3

OrderEvidence = Literal[
    "raw_output_text", "raw_tokens", "reassembled_stream", "unavailable"
]
OrderAdherence = Literal["adherent", "mismatch", "unknown"]
OrderUnknownReason = Literal[
    "not_applicable",
    "not_returned",
    "malformed_output",
    "incomplete_evidence",
    "sdk_object_only",
    "unsafe_key_names",
]
RootKey = Literal["scores", "explanations", "winner_id", "tie", "explanation"]
MemberKey = Literal["trait_id", "category_label", "explanation", "text"]
ArrayName = Literal["scores", "explanations"]

# A single observation is bounded independently of total run size. These are
# inspection limits, not provider context limits; exceeding them loses evidence.
_MAX_OUTPUT_BYTES = 1_048_576
_MAX_DEPTH = 64
_MAX_MEMBERS = 4096


class ObservedMemberOrder(EvidenceModel):
    """Occurrence locator; trait IDs are copied only after membership lookup."""

    array: ArrayName
    index: Annotated[int, Field(ge=0)]
    trait_id: ID | None = None
    keys: tuple[MemberKey, ...] | None = None

    @model_validator(mode="after")
    def _unique_keys(self) -> ObservedMemberOrder:
        if self.keys is not None and len(set(self.keys)) != len(self.keys):
            raise ValueError("invalid_order_metadata")
        allowed = (
            {"trait_id", "text"}
            if self.array == "explanations"
            else {"trait_id", "category_label", "explanation"}
        )
        if self.keys is not None and not set(self.keys) <= allowed:
            raise ValueError("invalid_order_metadata")
        return self


class ProviderObservationMetadata(EvidenceIdentity):
    """Version-1 safe order evidence, once per established observation."""

    requested_order: OutputOrder | None
    observed_key_order: tuple[RootKey, ...] | None = None
    member_key_orders: Annotated[
        tuple[ObservedMemberOrder, ...], Field(max_length=_MAX_MEMBERS)
    ] = ()
    order_adherence: OrderAdherence
    order_evidence: OrderEvidence
    unknown_reason: OrderUnknownReason | None = None

    @model_validator(mode="after")
    def _consistent(self) -> ProviderObservationMetadata:
        if self.observation_id is None:
            raise ValueError("invalid_order_metadata")
        unknown = self.order_adherence == "unknown"
        if unknown != (self.unknown_reason is not None):
            raise ValueError("invalid_order_metadata")
        if self.requested_order is None and (
            not unknown or self.unknown_reason != "not_applicable"
        ):
            raise ValueError("invalid_order_metadata")
        if self.order_evidence == "unavailable" and (
            not unknown or self.observed_key_order is not None or self.member_key_orders
        ):
            raise ValueError("invalid_order_metadata")
        if self.observed_key_order is not None and len(
            set(self.observed_key_order)
        ) != len(self.observed_key_order):
            raise ValueError("invalid_order_metadata")
        if not unknown and self.observed_key_order is None:
            raise ValueError("invalid_order_metadata")
        if self.requested_order is not None and self.observed_key_order is not None:
            if not set(self.observed_key_order) <= {"scores", "explanations"}:
                raise ValueError("invalid_order_metadata")
        if self.order_adherence == "adherent" and (
            not self.member_key_orders
            or any(
                member.keys is None or member.trait_id is None
                for member in self.member_key_orders
            )
        ):
            raise ValueError("invalid_order_metadata")
        if self.observed_key_order is not None and any(
            member.array not in self.observed_key_order
            for member in self.member_key_orders
        ):
            raise ValueError("invalid_order_metadata")
        locators = {(member.array, member.index) for member in self.member_key_orders}
        if len(locators) != len(self.member_key_orders):
            raise ValueError("invalid_order_metadata")
        return self


def _bounded_text(output: str | bytes) -> bool:
    """Check size and nesting before the shared duplicate-detecting decoder."""
    if len(output) > _MAX_OUTPUT_BYTES:
        return False
    try:
        text = output.decode("utf-8") if isinstance(output, bytes) else output
        if len(text.encode("utf-8")) > _MAX_OUTPUT_BYTES:
            return False
    except UnicodeError:
        return False
    depth = 0
    quoted = False
    escaped = False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            if depth > _MAX_DEPTH:
                return False
        elif char in "]}":
            depth -= 1
    return True


def _sequence_status(observed: tuple[str, ...], expected: tuple[str, ...]) -> str:
    if len(observed) != len(expected) or set(observed) != set(expected):
        return "incomplete"
    return "match" if observed == expected else "mismatch"


def extract_observation_metadata(
    commitment: OutputCommitmentV2 | OutputCommitmentV3,
    *,
    identity: EvidenceIdentity,
    output: str | bytes | None,
    evidence_source: OrderEvidence,
    unavailable_reason: OrderUnknownReason | None = None,
) -> ProviderObservationMetadata:
    """Inspect original output text or an exactly decoded trustworthy token stream.

    ``raw_tokens`` and ``reassembled_stream`` require the caller to supply the
    exact, complete emitted text, without reserialization. An SDK object alone
    must use ``unavailable`` / ``sdk_object_only``. No object can recover order.
    All limits and failures produce fixed safe codes, with no response fragment.
    """
    try:
        if commitment.commitment_version == 3:
            from judgmentllm.transport import OutputCommitmentV3

            commitment = OutputCommitmentV3.model_validate_json(
                commitment.model_dump_json()
            )
        else:
            commitment = OutputCommitmentV2.model_validate_json(
                commitment.model_dump_json()
            )
    except (ValueError, TypeError, AttributeError):
        raise ValueError("invalid_output_commitment") from None
    requested = (
        commitment.contract.output_order
        if isinstance(commitment.contract, RubricOutputContract)
        else None
    )
    values = identity.model_dump()
    values.update(requested_order=requested, order_evidence=evidence_source)

    def unknown(reason: OrderUnknownReason) -> ProviderObservationMetadata:
        return ProviderObservationMetadata.model_validate(
            values | {"order_adherence": "unknown", "unknown_reason": reason}
        )

    if requested is None:
        return unknown("not_applicable")
    if evidence_source == "unavailable":
        return unknown(unavailable_reason or "incomplete_evidence")
    if output is None:
        return unknown("not_returned")
    if type(output) not in (str, bytes):
        values["order_evidence"] = "unavailable"
        return unknown("sdk_object_only")
    if not _bounded_text(output):
        return unknown("incomplete_evidence")
    try:
        decoded = decode_assessment_output(output)
    except ValueError:
        return unknown("malformed_output")
    if not isinstance(decoded, dict):
        return unknown("malformed_output")
    root = cast(dict[str, object], decoded)
    root_keys = tuple(root)
    expected_root = commitment.root_properties
    unsafe = bool(set(root_keys) - set(expected_root))
    root_status = _sequence_status(root_keys, expected_root)
    mismatch = root_status == "mismatch"
    incomplete = root_status == "incomplete"
    observed_root = None if unsafe else cast(tuple[RootKey, ...], root_keys)
    contract = commitment.contract
    assert isinstance(contract, RubricOutputContract)
    known = {trait.trait_id for trait in contract.traits}
    members: list[ObservedMemberOrder] = []
    member_shapes = {
        (member.array, member.trait_id): member.properties
        for member in commitment.member_properties
    }
    array_shapes = {
        member.array: member.properties for member in commitment.member_properties
    }
    for array_key in root:
        if (
            array_key not in ("scores", "explanations")
            or array_key not in expected_root
        ):
            continue
        array = array_key
        entries = root[array]
        if not isinstance(entries, list):
            incomplete = True
            continue
        entries = cast(list[object], entries)
        if len(entries) + len(members) > _MAX_MEMBERS:
            return unknown("incomplete_evidence")
        seen: set[str] = set()
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                incomplete = True
                members.append(ObservedMemberOrder(array=array, index=index))
                continue
            item = cast(dict[str, object], entry)
            trait = item.get("trait_id")
            trait_id = trait if isinstance(trait, str) and trait in known else None
            expected = member_shapes.get((array, trait_id)) or member_shapes.get(
                (array, None)
            )
            # Unknown traits use the committed shape only as a key allowlist;
            # this never establishes attribution to an expected trait.
            allowed = expected or array_shapes[array]
            keys = tuple(item)
            safe = not bool(set(keys) - set(allowed))
            unsafe = unsafe or not safe
            if trait_id is None or trait_id in seen:
                incomplete = True
            if trait_id is not None:
                seen.add(trait_id)
            if expected is not None:
                status = _sequence_status(keys, expected)
                mismatch = mismatch or status == "mismatch"
                incomplete = incomplete or status == "incomplete"
            members.append(
                ObservedMemberOrder(
                    array=array,
                    index=index,
                    trait_id=trait_id,
                    keys=cast(tuple[MemberKey, ...], keys) if safe else None,
                )
            )
        incomplete = incomplete or seen != known or len(entries) != len(known)
    values.update(observed_key_order=observed_root, member_key_orders=tuple(members))
    if unsafe:
        return unknown("unsafe_key_names")
    if mismatch:
        values["order_adherence"] = "mismatch"
    elif incomplete:
        return unknown("incomplete_evidence")
    else:
        values["order_adherence"] = "adherent"
    return ProviderObservationMetadata.model_validate(values)
