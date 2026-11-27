"""Offline compilation for the pinned Anthropic Messages Batch route."""

from collections.abc import Callable
from importlib import import_module, metadata
from typing import Literal

from pydantic import field_validator

from judgmentllm.anthropic_prompts import (
    RenderedAnthropicPromptV1,
    render_anthropic_request,
)
from judgmentllm.anthropic_registry import production_generation_capabilities
from judgmentllm.anthropic_transport import (
    AnthropicRequestPolicyV1,
    AnthropicSelectionV1,
    OutputCommitmentV5,
    build_anthropic_request_body,
)
from judgmentllm.batch_models import AnthropicBatchContextV1
from judgmentllm.config import RunSpecification, RunSpecV3
from judgmentllm.models import PairTask, ResponseItem
from judgmentllm.native_schema import NativeProjectionScopeV1
from judgmentllm.native_selection import _features
from judgmentllm.output_contracts import generate_output_schema, serialize_output_schema
from judgmentllm.persistence.hashing import (
    bytes_digest,
    canonical_bytes,
    semantic_digest,
)
from judgmentllm.planning import CapabilityProfile, PlannedRequest
from judgmentllm.preparation.session import RequestDependency
from judgmentllm.providers.protocol import PreparedRequest
from judgmentllm.service_tiers import service_policy
from judgmentllm.transport import _contract

# pyright: reportPrivateUsage=false

BATCH_PATH = "/v1/messages/batches"
BATCH_ORIGIN = "https://api.anthropic.com"


def batch_source_id(manifest_sha256: str, shard_id: str, submission_id: str) -> str:
    return semantic_digest(
        "anthropic-batch-source-v1", [manifest_sha256, shard_id, submission_id]
    )


def require_sdk() -> None:
    from judgmentllm.providers.anthropic import AnthropicReadinessError

    for name, pinned in (("anthropic", "2.3.0"), ("httpx2", "2.16.0")):
        try:
            installed = metadata.version(name)
            if (
                installed != pinned
                or getattr(import_module(name), "__version__", None) != pinned
            ):
                raise AnthropicReadinessError("sdk_scope_mismatch")
        except (metadata.PackageNotFoundError, ImportError):
            raise AnthropicReadinessError("missing_extra") from None


def require_anthropic_batch(spec: RunSpecification) -> RunSpecV3:
    if (
        not isinstance(spec, RunSpecV3)
        or (spec.provider, spec.endpoint, spec.execution.mode)
        != ("anthropic", "messages", "batch_native")
        or spec.execution.cache_policy != "off"
        or spec.execution.tier is not None
        or spec.execution.bulk_limits is not None
        or spec.prompt.cache_breakpoints is not None
        or spec.account_scope is not None
        or spec.prompt.structured_output == "native"
        or spec.strict_capabilities
    ):
        raise ValueError(
            "Anthropic Batch requires V3 non-strict Messages text, cache OFF, and no tier, account or bulk override"
        )
    if spec.generation is not None:
        if any(
            value is not None
            for value in (
                spec.generation.temperature,
                spec.generation.top_p,
                spec.generation.reasoning,
            )
        ):
            raise ValueError(
                "Anthropic Batch has no qualified sampling or explicit reasoning controls"
            )
        limit = spec.generation.max_output_tokens
        if spec.model == "claude-sonnet-4-6" and limit is not None and limit > 128_000:
            raise ValueError(
                "Anthropic Batch output token limit exceeds the exact model bound"
            )
    return spec


def batch_context(spec: RunSpecification) -> AnthropicBatchContextV1:
    return AnthropicBatchContextV1(model=require_anthropic_batch(spec).model)


def scope_for(model: str) -> NativeProjectionScopeV1:
    return NativeProjectionScopeV1(
        provider="anthropic",
        endpoint="messages",
        model=model,
        model_scope="exact",
        sdk_package="anthropic",
        sdk_version="2.3.0",
        api_version="2026-10-21",
    )


def capabilities_for(provider: str, endpoint: str, model: str) -> CapabilityProfile:
    if (provider, endpoint) != ("anthropic", "messages"):
        raise ValueError("unsupported Anthropic Batch route")
    return CapabilityProfile(
        modes=("batch_native",),
        generation=production_generation_capabilities(scope_for(model)),
    )


def _output(spec: RunSpecV3, request: PlannedRequest) -> OutputCommitmentV5:
    contract = _contract(spec, request, presentation_version=2)
    raw = serialize_output_schema(contract)
    authority = semantic_digest("anthropic-batch-authority-v1", batch_context(spec))
    limit = spec.generation.max_output_tokens if spec.generation else None
    return OutputCommitmentV5(
        scope=scope_for(spec.model),
        contract=contract,
        service_policy=service_policy(spec),
        selection=AnthropicSelectionV1(
            kind="text",
            policy=spec.prompt.structured_output,
            requested_mechanism=spec.prompt.native_mechanism,
            mechanism="text",
            native_support="unknown",
            authority_kind="unqualified",
            features=_features(contract),
            authority_sha256=authority,
        ),
        request_policy=AnthropicRequestPolicyV1(
            max_tokens_source="requested"
            if limit is not None
            else "adapter_default_4096",
            effective_max_tokens=4096 if limit is None else limit,
            service_tier="standard_only",
            cache="off",
        ),
        schema_text=raw.decode("utf-8"),
        schema_sha256=bytes_digest(raw),
        neutral_semantic_sha256=semantic_digest(
            "neutral-schema-semantics-v1", generate_output_schema(contract)
        ),
        repetition="canonical_neutral",
        context_sha256=authority,
    )


class PreparedAnthropicBatchRequest(PreparedRequest):
    preparation_version: Literal[11] = 11
    output_commitment: OutputCommitmentV5

    @field_validator("preparation_version", mode="before")
    @classmethod
    def exact_version(cls, value: object) -> object:
        if type(value) is not int or value != 11:
            raise ValueError("Anthropic Batch preparation version must be integer 11")
        return value


def encode_row(member_key: str, wire: PreparedAnthropicBatchRequest) -> bytes:
    return (
        b'{"custom_id":'
        + canonical_bytes(member_key)
        + b',"params":'
        + wire.payload
        + b"}"
    )


def compile_request(
    spec: RunSpecification,
    dependency: RequestDependency,
    rendered: RenderedAnthropicPromptV1,
) -> PreparedAnthropicBatchRequest:
    checked = require_anthropic_batch(spec)
    require_sdk()
    output = _output(checked, dependency.request)
    if rendered.output_commitment != output:
        raise ValueError("Anthropic Batch prompt/output commitment differs")
    payload = build_anthropic_request_body(
        endpoint=checked.endpoint,
        model=checked.model,
        prompt=rendered.text,
        output=output,
        generation=checked.generation,
    )
    return PreparedAnthropicBatchRequest(
        request_uid=dependency.request.request_uid,
        task_ids=tuple(task.task_id for task in dependency.request.tasks),
        dependency_sha256=dependency.dependency_sha256,
        prompt_sha256=bytes_digest(rendered.text.encode("utf-8")),
        payload_sha256=bytes_digest(payload),
        provider="anthropic",
        endpoint=checked.endpoint,
        model=checked.model,
        generation=checked.generation,
        payload=payload,
        output_commitment=output,
    )


def render_request(
    spec: RunSpecification,
    template: str | None,
    request: PlannedRequest,
    lookup: Callable[[Literal["response", "pair"], str], ResponseItem | PairTask],
) -> RenderedAnthropicPromptV1:
    checked = require_anthropic_batch(spec)
    require_sdk()
    return render_anthropic_request(
        checked, template, request, lookup, _output(checked, request)
    )
