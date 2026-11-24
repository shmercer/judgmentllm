"""Credential-free OpenAI Batch compilation using the shared generative body."""

from __future__ import annotations

from collections.abc import Callable
from importlib import import_module, metadata
from typing import Literal

from pydantic import field_validator

from judgmentllm.batch_models import OpenAIBatchContextV1
from judgmentllm.cache_policy import validate_openai_off
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
from judgmentllm.planning import PlannedRequest
from judgmentllm.preparation.session import RequestDependency
from judgmentllm.production_prompts import RenderedProductionPromptV1
from judgmentllm.production_transport import (
    OutputCommitmentV4,
    ProductionSelectionV1,
    build_openai_request_body,
)
from judgmentllm.providers.protocol import PreparedRequest
from judgmentllm.providers.reasoning import build_reasoning_policy
from judgmentllm.service_tiers import service_policy
from judgmentllm.transport import _contract

# Shared pure builders preserve numeric values, omission and prompt protections.
# pyright: reportPrivateUsage=false

SDK_VERSION = "3.42.1"
ROUTES = {"responses": "/v1/responses", "chat.completions": "/v1/chat/completions"}


def require_sdk() -> None:
    """Check actual distribution AND loaded module, before wire work or secrets."""
    from judgmentllm.providers.openai import OpenAIReadinessError

    try:
        installed = metadata.version("openai")
    except metadata.PackageNotFoundError:
        raise OpenAIReadinessError("missing_extra") from None
    if installed != SDK_VERSION:
        raise OpenAIReadinessError("sdk_scope_mismatch")
    if getattr(import_module("openai"), "__version__", None) != SDK_VERSION:
        raise OpenAIReadinessError("sdk_scope_mismatch")


def require_openai_batch(spec: RunSpecification) -> RunSpecV3:
    if (
        not isinstance(spec, RunSpecV3)
        or spec.provider != "openai"
        or spec.endpoint not in ROUTES
        or spec.execution.mode != "batch_native"
        or spec.execution.cache_policy != "off"
        or spec.execution.tier is not None
        or spec.execution.bulk_limits is not None
        or spec.prompt.cache_breakpoints is not None
        or spec.prompt.structured_output == "native"
        or (spec.prompt.structured_output == "auto" and spec.strict_capabilities)
    ):
        raise ValueError(
            "OpenAI Batch requires V3 generative, cache OFF, no tier/bulk limits and unqualified text output"
        )
    validate_openai_off(scope_for(spec.endpoint, spec.model))
    return spec


def scope_for(endpoint: str, model: str) -> NativeProjectionScopeV1:
    return NativeProjectionScopeV1(
        provider="openai",
        endpoint=endpoint,
        model=model,
        model_scope="exact",
        sdk_package="openai",
        sdk_version=SDK_VERSION,
    )


def batch_context(spec: RunSpecification) -> OpenAIBatchContextV1:
    checked = require_openai_batch(spec)
    return OpenAIBatchContextV1.model_validate(
        dict(endpoint=checked.endpoint, model=checked.model)
    )


def build_output(spec: RunSpecification, request: PlannedRequest) -> OutputCommitmentV4:
    checked = require_openai_batch(spec)
    require_sdk()
    context = batch_context(checked)
    scope = scope_for(checked.endpoint, checked.model)
    contract = _contract(checked, request, presentation_version=2)
    raw = serialize_output_schema(contract)
    authority = semantic_digest("openai-batch-authority-v1", context)
    return OutputCommitmentV4(
        cache_policy_version=checked.cache_policy_version,
        reasoning_policy=build_reasoning_policy(checked, scope),
        service_policy=service_policy(checked),
        scope=scope,
        contract=contract,
        selection=ProductionSelectionV1(
            kind="text",
            policy=checked.prompt.structured_output,
            requested_mechanism=checked.prompt.native_mechanism,
            mechanism="text",
            native_support="unknown",
            authority_kind="unqualified",
            features=_features(contract),
            authority_sha256=authority,
        ),
        schema_text=raw.decode("utf-8"),
        schema_sha256=bytes_digest(raw),
        neutral_semantic_sha256=semantic_digest(
            "neutral-schema-semantics-v1", generate_output_schema(contract)
        ),
        repetition="canonical_neutral",
        context_sha256=authority,
    )


class PreparedOpenAIBatchRequest(PreparedRequest):
    preparation_version: Literal[10] = 10
    output_commitment: OutputCommitmentV4

    @field_validator("preparation_version", mode="before")
    @classmethod
    def exact_version(cls, value: object) -> object:
        if type(value) is not int or value != 10:
            raise ValueError("OpenAI Batch preparation version must be integer 10")
        return value


def encode_row(member_key: str, wire: PreparedOpenAIBatchRequest) -> bytes:
    # Splice the exact shared body bytes, preserving integer/float representation.
    return (
        b'{"custom_id":'
        + canonical_bytes(member_key)
        + b',"method":"POST","url":'
        + canonical_bytes(ROUTES[wire.endpoint])
        + b',"body":'
        + wire.payload
        + b"}\n"
    )


def compile_request(
    spec: RunSpecification,
    dependency: RequestDependency,
    rendered: RenderedProductionPromptV1,
) -> PreparedOpenAIBatchRequest:
    checked = require_openai_batch(spec)
    require_sdk()
    output = build_output(checked, dependency.request)
    if rendered.output_commitment != output:
        raise ValueError("Batch prompt/output commitment differs")
    payload = build_openai_request_body(
        endpoint=checked.endpoint,
        model=checked.model,
        prompt=rendered.text,
        output=output,
        generation=checked.generation,
    )
    return PreparedOpenAIBatchRequest(
        request_uid=dependency.request.request_uid,
        task_ids=tuple(task.task_id for task in dependency.request.tasks),
        dependency_sha256=dependency.dependency_sha256,
        prompt_sha256=bytes_digest(rendered.text.encode("utf-8")),
        payload_sha256=bytes_digest(payload),
        provider="openai",
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
) -> RenderedProductionPromptV1:
    from judgmentllm.production_prompts import render_production_request

    return render_production_request(
        require_openai_batch(spec),
        template,
        request,
        lookup,
        build_output(spec, request),
    )
