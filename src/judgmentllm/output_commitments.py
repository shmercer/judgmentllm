"""Provider-neutral v2 presentation selection and exact output commitments.

Full contracts are request content: they live in memory and full-capture payloads,
never in minimal-capture registry metadata.
"""

from __future__ import annotations

import json
from hashlib import sha256
from typing import TYPE_CHECKING, Literal

from pydantic import ConfigDict, field_validator, model_validator

from judgmentllm.models import StrictModel
from judgmentllm.output_contracts import (
    AssessmentOutputContract,
    PairwiseOutputContract,
    RubricOutputContract,
    generate_output_schema,
    serialize_output_schema,
)
from judgmentllm.persistence.models import SHA256

if TYPE_CHECKING:
    from judgmentllm.config import RunSpecification
    from judgmentllm.planning import CapabilityProfile, PlannedRequest


class SchemaSelectionV2(StrictModel):
    """Preflight choice; no provider-native translator exists in this phase."""

    model_config = ConfigDict(frozen=True)
    selection_version: Literal[2] = 2
    policy: Literal["auto", "native", "text"]
    mechanism: Literal["text"] = "text"
    native_support: Literal["unsupported", "unknown"]
    reason: Literal[
        "explicit_text",
        "native_unavailable_text_selected",
        "native_unknown_text_selected",
    ]

    @field_validator("selection_version", mode="before")
    @classmethod
    def strict_version(cls, value: object) -> object:
        if type(value) is not int or value != 2:
            raise ValueError("selection_version must be integer 2")
        return value

    @model_validator(mode="after")
    def consistent(self) -> SchemaSelectionV2:
        expected = (
            "explicit_text"
            if self.policy == "text"
            else "native_unavailable_text_selected"
            if self.native_support == "unsupported"
            else "native_unknown_text_selected"
        )
        if self.policy == "native" or self.reason != expected:
            raise ValueError(
                "schema selection must describe supported text presentation"
            )
        return self


def resolve_schema_selection(
    spec: RunSpecification, capabilities: CapabilityProfile | None = None
) -> SchemaSelectionV2:
    """Resolve before commitment, distinguishing unknown from known unsupported."""
    from judgmentllm.planning import PreflightError

    if spec.schema_version != 2:
        raise ValueError("schema presentation selection requires RunSpec v2")
    policy = spec.prompt.structured_output
    known = (spec.provider, spec.endpoint) == ("mock", "synthetic")
    if policy == "native":
        raise PreflightError(
            "prompt.structured_output=native requires an implemented fixture-tested native schema translator; unavailable for this endpoint"
        )
    if policy == "auto" and not known and spec.strict_capabilities:
        raise PreflightError(
            "native structured-output support is unknown under strict_capabilities; explicitly choose text or a supported endpoint"
        )
    return SchemaSelectionV2(
        policy=policy,
        native_support="unsupported" if known else "unknown",
        reason="explicit_text"
        if policy == "text"
        else "native_unavailable_text_selected"
        if known
        else "native_unknown_text_selected",
    )


def validate_schema_selection(
    spec: RunSpecification,
    selection: SchemaSelectionV2,
    capabilities: CapabilityProfile | None = None,
) -> SchemaSelectionV2:
    """Check the pinned text mechanism, never perform selection during replay."""
    from judgmentllm.io import _revalidate_record  # pyright: ignore[reportPrivateUsage]

    checked = _revalidate_record(selection)
    if spec.schema_version != 2 or checked.policy != spec.prompt.structured_output:
        raise ValueError("schema selection differs from the committed run policy")
    known = (spec.provider, spec.endpoint) == ("mock", "synthetic")
    if checked.native_support != ("unsupported" if known else "unknown"):
        raise ValueError("schema selection has incompatible endpoint evidence")
    if (
        checked.policy == "auto"
        and checked.native_support == "unknown"
        and spec.strict_capabilities
    ):
        raise ValueError(
            "unknown native support is disallowed under strict_capabilities"
        )
    return checked


def build_output_contract(
    spec: RunSpecification, request: PlannedRequest
) -> AssessmentOutputContract:
    """Build only from exact planned membership and the validated v2 run."""
    if spec.schema_version != 2:
        raise ValueError("output contracts require RunSpec v2")
    if request.pair is not None:
        contract = PairwiseOutputContract(
            pair=request.pair,
            allow_ties=spec.allow_ties,
            explanations=spec.execution.explanations,
            output_order=spec.prompt.output_order,
        )
    else:
        if spec.rubric is None:
            raise ValueError("rubric request requires a rubric")
        by_id = {trait.trait_id: trait for trait in spec.rubric.traits}
        try:
            contract = RubricOutputContract(
                traits=tuple(by_id[trait_id] for trait_id in request.trait_ids),
                explanations=spec.execution.explanations,
                output_order=spec.prompt.output_order,
            )
        except KeyError:
            raise ValueError("request references an unknown trait") from None
    # Shared Phase 1 consistency gate, without a second validation engine.
    from judgmentllm.execution.processing import _request_contract  # pyright: ignore[reportPrivateUsage]
    from judgmentllm.preparation.session import RequestDependency

    _request_contract(
        contract, spec, RequestDependency(request=request, dependency_sha256="0" * 64)
    )
    return contract


class MemberPropertyOrder(StrictModel):
    model_config = ConfigDict(frozen=True)
    array: Literal["scores", "explanations"]
    trait_id: str | None = None
    properties: tuple[str, ...]


def _property_orders(
    contract: AssessmentOutputContract,
) -> tuple[tuple[str, ...], tuple[MemberPropertyOrder, ...]]:
    schema = generate_output_schema(contract)
    properties = schema["properties"]
    assert isinstance(properties, dict)
    members: list[MemberPropertyOrder] = []
    if isinstance(contract, RubricOutputContract):
        for trait in contract.traits:
            keys = (
                ("trait_id", "category_label", "explanation")
                if contract.explanations and contract.output_order == "together"
                else ("trait_id", "category_label")
            )
            members.append(
                MemberPropertyOrder(
                    array="scores", trait_id=trait.trait_id, properties=keys
                )
            )
        if contract.output_order != "together":
            members.append(
                MemberPropertyOrder(
                    array="explanations", properties=("trait_id", "text")
                )
            )
    return tuple(properties), tuple(members)


class OutputCommitmentV2(StrictModel):
    """Exact schema representation, requested order and immutable selection."""

    model_config = ConfigDict(frozen=True)
    commitment_version: Literal[2] = 2
    renderer_version: Literal[2] = 2
    contract: AssessmentOutputContract
    selection: SchemaSelectionV2
    schema_text: str
    schema_sha256: SHA256
    root_properties: tuple[str, ...]
    member_properties: tuple[MemberPropertyOrder, ...]

    @field_validator("commitment_version", "renderer_version", mode="before")
    @classmethod
    def strict_version(cls, value: object) -> object:
        if type(value) is not int or value != 2:
            raise ValueError("commitment and renderer versions must be integer 2")
        return value

    @model_validator(mode="after")
    def exact_representation(self) -> OutputCommitmentV2:
        raw = serialize_output_schema(self.contract)
        root, members = _property_orders(self.contract)
        if (
            self.schema_text.encode("utf-8") != raw
            or self.schema_sha256 != sha256(raw).hexdigest()
            or self.root_properties != root
            or self.member_properties != members
        ):
            raise ValueError(
                "output commitment does not match its exact ordered schema"
            )
        return self


def build_output_commitment(
    spec: RunSpecification,
    request: PlannedRequest,
    selection: SchemaSelectionV2 | None = None,
) -> OutputCommitmentV2:
    contract = build_output_contract(spec, request)
    raw = serialize_output_schema(contract)
    root, members = _property_orders(contract)
    return OutputCommitmentV2(
        contract=contract,
        selection=validate_schema_selection(spec, selection)
        if selection is not None
        else resolve_schema_selection(spec),
        schema_text=raw.decode("utf-8"),
        schema_sha256=sha256(raw).hexdigest(),
        root_properties=root,
        member_properties=members,
    )


def validate_output_commitment(
    commitment: OutputCommitmentV2,
    spec: RunSpecification,
    request: PlannedRequest,
) -> OutputCommitmentV2:
    from judgmentllm.persistence.hashing import canonical_bytes

    if spec.schema_version != 2:
        raise ValueError("output commitments require RunSpec v2")
    checked = OutputCommitmentV2.model_validate_json(canonical_bytes(commitment))
    expected = build_output_commitment(spec, request, checked.selection)
    if checked.selection.policy != spec.prompt.structured_output or canonical_bytes(
        checked
    ) != canonical_bytes(expected):
        raise ValueError(
            "output commitment differs from the committed run or membership"
        )
    return checked


def ordered_json_bytes(value: object) -> bytes:
    """Deterministic insertion-order JSON, separate from semantic hashing."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
