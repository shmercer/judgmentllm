"""Detached provider-neutral projections; no SDK, execution or storage integration.

The evidence envelope remains version 1. Projection version 2 is an additive
boundary: OpenAI's version-1 implementations and identities are unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypeAlias, cast

from pydantic import Field, JsonValue, model_validator

from judgmentllm.evidence.scope import EvidenceProvenance
from judgmentllm.native_schema import (
    ConstraintIdentifier,
    ConstraintPath,
    NativeAssessmentSourceV1,
    NativeMemberOrderV1,
    NativeModel,
    NativeProjectionScopeV1,
    PropertyName,
    snapshot_source,
    source_contract,
)
from judgmentllm.output_contracts import (
    AssessmentOutputContract,
    PairwiseOutputContract,
    RubricOutputContract,
    decode_assessment_output,
    serialize_output_schema,
    wire_winner_ids,
)
from judgmentllm.persistence.hashing import (
    bytes_digest,
    canonical_bytes,
    semantic_digest,
)
from judgmentllm.persistence.models import SHA256

NativeMechanismV2: TypeAlias = Literal[
    "anthropic.messages.output_config.format",
    "gemini.generate_content.response_schema",
    "gemini.generate_content.response_json_schema",
    "vertex.generate_content.response_schema",
    "vertex.generate_content.response_json_schema",
    "together.chat.response_format",
    "ollama.chat.format",
]
NativeSchemaDialectV2: TypeAlias = Literal[
    "anthropic.json-schema.v1",
    "gemini.cloud-schema.v1",
    "gemini.json-schema.v1",
    "vertex.cloud-schema.v1",
    "vertex.json-schema.v1",
    "together.json-schema.v1",
    "ollama.local-json-schema.v1",
]
ConstraintIdentifierV2: TypeAlias = (
    ConstraintIdentifier | Literal["requested_property_order", "string_length"]
)
ProjectionReasonV2: TypeAlias = Literal[
    "unsupported_scope",
    "unsupported_schema",
    "schema_limit_exceeded",
    "invalid_contract",
]
SchemaLimitV2: TypeAlias = Literal[
    "properties",
    "nesting",
    "string_characters",
    "enum_values",
    "enum_characters",
    "schema_bytes",
    "alternatives",
]


class NativeProjectionDiagnosticV2(NativeModel):
    """Controlled codes only; schema or caller text is never diagnostic data."""

    reason: ProjectionReasonV2
    limit: SchemaLimitV2 | None = None

    @model_validator(mode="after")
    def consistent(self) -> NativeProjectionDiagnosticV2:
        if (self.reason == "schema_limit_exceeded") != (self.limit is not None):
            raise ValueError("inconsistent projection diagnostic")
        return self


class NativeProjectionErrorV2(ValueError):
    def __init__(
        self, reason: ProjectionReasonV2, *, limit: SchemaLimitV2 | None = None
    ) -> None:
        self.diagnostic = NativeProjectionDiagnosticV2(reason=reason, limit=limit)
        self.reason = reason
        super().__init__("Native schema projection unavailable: " + reason + ".")


class NativeConstraintCoverageV2(NativeModel):
    """Independent representation, converter, documentation and enforcement facts.

    Representation does not imply serving support. Every assessment still passes
    the complete local validator, including constraints represented in a schema.
    """

    constraint: ConstraintIdentifierV2
    path: ConstraintPath
    member_index: int | None = Field(default=None, ge=0)
    operation: Literal[
        "preserve",
        "const_to_enum",
        "oneof_to_anyof",
        "add_explicit_type",
        "omit_bounds",
        "omit_min_length",
        "omit_root_relation",
        "local_validation",
        "nullable_to_flag",
        "nullable_to_anyof",
        "add_property_ordering",
        "preserve_bounds",
    ] = "preserve"
    reason: Literal[
        "supported_core",
        "equivalent_construct",
        "conservative_core",
        "unsupported_root_composition",
        "relational_requirement",
        "decoder_requirement",
        "unsupported_keyword",
        "documented_subset",
        "scoped_unknown",
        "unqualified_serving_support",
        "enum_casing_unqualified",
    ] = "supported_core"
    represented: bool
    sdk_retained: bool | None
    documented_support: bool | None
    enforcement_verified: Literal[None] = None

    @model_validator(mode="after")
    def independent_dimensions(self) -> NativeConstraintCoverageV2:
        if not self.represented and self.sdk_retained is True:
            raise ValueError("omitted constraint cannot be retained")
        return self


@dataclass(frozen=True)
class _RouteV2:
    provider: str
    endpoint: str
    mechanism: NativeMechanismV2
    schema_dialect: NativeSchemaDialectV2
    sdk_package: str
    sdk_version: str
    api_version: str | None
    implementation_version: int = 1


_ROUTES_V2: tuple[_RouteV2, ...] = (
    _RouteV2(
        "anthropic",
        "messages",
        "anthropic.messages.output_config.format",
        "anthropic.json-schema.v1",
        "anthropic",
        "2.3.0",
        "2026-10-21",
    ),
    _RouteV2(
        "gemini",
        "models.generate_content",
        "gemini.generate_content.response_schema",
        "gemini.cloud-schema.v1",
        "google-genai",
        "2.34.0",
        "v1beta",
    ),
    _RouteV2(
        "gemini",
        "models.generate_content",
        "gemini.generate_content.response_json_schema",
        "gemini.json-schema.v1",
        "google-genai",
        "2.34.0",
        "v1beta",
    ),
    _RouteV2(
        "vertex",
        "models.generate_content",
        "vertex.generate_content.response_schema",
        "vertex.cloud-schema.v1",
        "google-genai",
        "2.34.0",
        "v1beta1",
    ),
    _RouteV2(
        "vertex",
        "models.generate_content",
        "vertex.generate_content.response_json_schema",
        "vertex.json-schema.v1",
        "google-genai",
        "2.34.0",
        "v1beta1",
    ),
    _RouteV2(
        "together",
        "chat.completions",
        "together.chat.response_format",
        "together.json-schema.v1",
        "together",
        "3.0.0",
        None,
    ),
    _RouteV2(
        "ollama",
        "chat",
        "ollama.chat.format",
        "ollama.local-json-schema.v1",
        "ollama",
        "0.6.3",
        None,
    ),
)


def _route_v2(mechanism: str) -> _RouteV2:
    for route in _ROUTES_V2:
        if route.mechanism == mechanism:
            return route
    raise NativeProjectionErrorV2("unsupported_scope")


def _checked_scope_v2(
    scope: NativeProjectionScopeV1, route: _RouteV2
) -> NativeProjectionScopeV1:
    checked = NativeProjectionScopeV1.model_validate(dict(scope))
    if any(
        getattr(checked, name) != getattr(route, name)
        for name in (
            "provider",
            "endpoint",
            "sdk_package",
            "sdk_version",
            "api_version",
        )
    ):
        raise NativeProjectionErrorV2("unsupported_scope")
    return checked


def _pre_expansion_budget_v2(contract: object) -> None:
    """Local implementation budgets, not provider or model acceptance claims."""
    if isinstance(contract, RubricOutputContract):
        if type(contract.traits) is not tuple:
            raise NativeProjectionErrorV2("invalid_contract")
        if len(contract.traits) > 256:
            raise NativeProjectionErrorV2("schema_limit_exceeded", limit="properties")
        enums = characters = 0
        for trait in contract.traits:
            if type(trait.levels) is not list or type(trait.trait_id) is not str:
                raise NativeProjectionErrorV2("invalid_contract")
            enums += len(trait.levels) + 1
            if enums > 10_000:
                raise NativeProjectionErrorV2(
                    "schema_limit_exceeded", limit="enum_values"
                )
            characters += len(trait.trait_id)
            for level in trait.levels:
                if type(level.label) is not str:
                    raise NativeProjectionErrorV2("invalid_contract")
                characters += len(level.label)
                if characters > 1_000_000:
                    raise NativeProjectionErrorV2(
                        "schema_limit_exceeded", limit="string_characters"
                    )
    elif isinstance(contract, PairwiseOutputContract):
        if sum(len(item) for item in wire_winner_ids(contract)) > 1_000_000:
            raise NativeProjectionErrorV2(
                "schema_limit_exceeded", limit="string_characters"
            )
    else:
        raise NativeProjectionErrorV2("invalid_contract")


def _fragment_budget_v2(text: str) -> None:
    if type(text) is not str or len(text.encode("utf-8")) > 1_000_000:
        raise NativeProjectionErrorV2("schema_limit_exceeded", limit="schema_bytes")
    value = cast(JsonValue, decode_assessment_output(text))
    stack: list[tuple[JsonValue, int]] = [(value, 0)]
    alternatives = 0
    while stack:
        node, depth = stack.pop()
        if depth > 32:
            raise NativeProjectionErrorV2("schema_limit_exceeded", limit="nesting")
        if isinstance(node, dict):
            for key, child in node.items():
                if key in {"oneOf", "anyOf", "allOf"} and isinstance(child, list):
                    alternatives += len(child)
                stack.append((child, depth + 1))
        elif isinstance(node, list):
            stack.extend((child, depth + 1) for child in node)
        if alternatives > 256:
            raise NativeProjectionErrorV2("schema_limit_exceeded", limit="alternatives")


def _provider_parts_v2(
    source: NativeAssessmentSourceV1,
    scope: NativeProjectionScopeV1,
    mechanism: NativeMechanismV2,
) -> dict[str, object]:
    if scope.provider in {"gemini", "vertex"}:
        from judgmentllm.providers import google_schema

        return google_schema._projection_parts(source, scope, mechanism)  # pyright: ignore[reportPrivateUsage]
    if scope.provider == "anthropic":
        from judgmentllm.providers import anthropic_schema

        return anthropic_schema._projection_parts(source, scope, mechanism)  # pyright: ignore[reportPrivateUsage]
    if scope.provider == "together":
        from judgmentllm.providers import together_schema

        return together_schema._projection_parts(source, scope, mechanism)  # pyright: ignore[reportPrivateUsage]
    if scope.provider == "ollama":
        from judgmentllm.providers import ollama_schema

        return ollama_schema._projection_parts(source, scope, mechanism)  # pyright: ignore[reportPrivateUsage]
    raise NativeProjectionErrorV2("unsupported_scope")


class NativeProjectionContentV2(NativeModel):
    """Content-bearing in-memory candidate; never minimal-capture metadata."""

    projection_version: Literal[2] = 2
    implementation_version: Literal[1] = 1
    scope: NativeProjectionScopeV1 = Field(repr=False)
    mechanism: NativeMechanismV2
    schema_dialect: NativeSchemaDialectV2
    source: NativeAssessmentSourceV1 = Field(repr=False)
    source_contract_version: Literal[2] = 2
    source_sha256: SHA256
    neutral_schema_text: str = Field(repr=False)
    neutral_schema_sha256: SHA256
    native_schema_text: str = Field(repr=False)
    native_schema_sha256: SHA256
    sdk_config_fragment_text: str = Field(repr=False)
    sdk_config_fragment_sha256: SHA256
    format_fragment_text: str = Field(repr=False)
    format_fragment_sha256: SHA256
    root_properties: tuple[PropertyName, ...]
    member_properties: tuple[NativeMemberOrderV1, ...] = Field(repr=False)
    native_constraints: tuple[NativeConstraintCoverageV2, ...]
    local_constraints: tuple[NativeConstraintCoverageV2, ...]
    provenance: tuple[EvidenceProvenance, ...] = Field(min_length=1, max_length=8)
    prompt_schema_policy: Literal[
        "none", "schema_in_prompt_recommended", "schema_in_prompt_discouraged"
    ]
    local_validation_required: Literal[True] = True

    @model_validator(mode="after")
    def exact_partitions(self) -> NativeProjectionContentV2:
        if any(not item.represented for item in self.native_constraints) or any(
            item.represented for item in self.local_constraints
        ):
            raise ValueError("constraint partition differs")
        return self


class NativeSchemaProjectionV2(NativeProjectionContentV2):
    projection_sha256: SHA256

    @model_validator(mode="after")
    def exact_representation(self) -> NativeSchemaProjectionV2:
        expected = _content_parts_v2(self.source, self.scope, self.mechanism)
        for name, value in expected.items():
            if getattr(self, name) != value:
                raise ValueError("projection differs from reviewed implementation")
        if self.projection_sha256 != projection_digest_v2(self):
            raise ValueError("projection identity differs")
        return self


def projection_digest_v2(projection: NativeProjectionContentV2) -> str:
    return semantic_digest(
        "native-schema-projection-v2",
        projection.model_dump(mode="json", exclude={"projection_sha256"}),
    )


def _content_parts_v2(
    source: NativeAssessmentSourceV1,
    scope: NativeProjectionScopeV1,
    mechanism: NativeMechanismV2,
) -> dict[str, object]:
    route = _route_v2(mechanism)
    scope = _checked_scope_v2(scope, route)
    contract = source_contract(source)
    _pre_expansion_budget_v2(contract)
    neutral = serialize_output_schema(contract)
    parts = _provider_parts_v2(source, scope, mechanism)
    if set(parts) != {
        "native_schema_text",
        "sdk_config_fragment_text",
        "format_fragment_text",
        "root_properties",
        "member_properties",
        "native_constraints",
        "local_constraints",
        "provenance",
        "prompt_schema_policy",
    }:
        raise NativeProjectionErrorV2("unsupported_schema")
    for name in (
        "native_schema_text",
        "sdk_config_fragment_text",
        "format_fragment_text",
    ):
        value = parts[name]
        if type(value) is not str:
            raise NativeProjectionErrorV2("unsupported_schema")
        _fragment_budget_v2(value)
        parts[name.removesuffix("_text") + "_sha256"] = bytes_digest(
            value.encode("utf-8")
        )
    return {
        "schema_version": 1,
        "projection_version": 2,
        "implementation_version": 1,
        "scope": scope,
        "mechanism": mechanism,
        "schema_dialect": route.schema_dialect,
        "source": source,
        "source_contract_version": 2,
        "source_sha256": bytes_digest(canonical_bytes(source)),
        "neutral_schema_text": neutral.decode("utf-8"),
        "neutral_schema_sha256": bytes_digest(neutral),
        **parts,
        "local_validation_required": True,
    }


def project_native_schema_v2(
    contract: AssessmentOutputContract,
    *,
    scope: NativeProjectionScopeV1,
    mechanism: NativeMechanismV2,
) -> NativeSchemaProjectionV2:
    """Project one explicit reviewed route; SDK installation grants no support."""
    try:
        route = _route_v2(mechanism)
        scope = _checked_scope_v2(scope, route)
        _pre_expansion_budget_v2(contract)
        source = snapshot_source(contract)
        content = NativeProjectionContentV2.model_validate(
            _content_parts_v2(source, scope, mechanism)
        )
        return NativeSchemaProjectionV2.model_validate(
            {**content.model_dump(), "projection_sha256": projection_digest_v2(content)}
        )
    except NativeProjectionErrorV2:
        raise
    except (
        ValueError,
        TypeError,
        AttributeError,
        KeyError,
        IndexError,
        RecursionError,
    ):
        raise NativeProjectionErrorV2("invalid_contract") from None
