"""Explicit trusted protocol dispatch, independent of RunSpec version.

Historical synthetic records never acquire production interpretation. Imports
are local to keep the preparation/provider dependency graph acyclic.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Callable

    from judgmentllm.config import RunSpecV3
    from judgmentllm.models import PairTask, ResponseItem
    from judgmentllm.native_prompts import RenderedPromptV3
    from judgmentllm.planning import PlannedRequest
    from judgmentllm.preparation.manifest import ExecutableManifest
    from judgmentllm.preparation.session import RequestDependency
    from judgmentllm.production_prompts import RenderedProductionPromptV1
    from judgmentllm.production_transport import (
        OutputCommitmentV4,
        PreparedRequestV4,
        ProductionContext,
        TransportCommitmentV2,
    )
    from judgmentllm.transport import (
        OutputCommitmentV3,
        PhysicalRequestIdentityV1,
        PreparedRequestV3,
        TransportCommitmentV1,
        TransportContext,
    )


def context_protocol(
    context: TransportContext | ProductionContext | None,
) -> Literal["synthetic", "production"]:
    from judgmentllm.production_transport import ProductionContext
    from judgmentllm.transport import TransportContext

    if type(context) is TransportContext:
        return "synthetic"
    if type(context) is ProductionContext:
        return "production"
    raise ValueError("an explicit trusted transport context is required")


def context_digest(context: TransportContext | ProductionContext) -> str:
    if context_protocol(context) == "production":
        from judgmentllm.production_transport import (
            ProductionContext,
            production_context_digest,
        )

        assert isinstance(context, ProductionContext)
        return production_context_digest(context)
    from judgmentllm.transport import TransportContext, transport_context_digest

    assert isinstance(context, TransportContext)
    return transport_context_digest(context)


def manifest_protocol(
    manifest: ExecutableManifest,
) -> Literal["historical", "synthetic", "production"]:
    from judgmentllm.preparation.manifest import (
        ExecutableRunManifest,
        ExecutableRunManifestV2,
        ExecutableRunManifestV3,
        ExecutableRunManifestV4,
    )

    if type(manifest) is ExecutableRunManifestV4:
        return "production"
    if type(manifest) is ExecutableRunManifestV3:
        return "synthetic"
    if type(manifest) in (ExecutableRunManifest, ExecutableRunManifestV2):
        return "historical"
    raise ValueError("unknown sealed manifest protocol")


def verify_protocol(
    manifest: ExecutableManifest, context: TransportContext | ProductionContext | None
) -> None:
    protocol = manifest_protocol(manifest)
    if protocol == "historical":
        if context is not None:
            raise ValueError("historical preparation cannot acquire a transport")
    elif context_protocol(context) != protocol:
        raise ValueError("sealed transport protocol and adapter differ")


def prepare_request(
    spec: RunSpecV3,
    dependency: RequestDependency,
    identity: PhysicalRequestIdentityV1,
    context: TransportContext | ProductionContext,
    *,
    template: str | None = None,
    lookup: Callable[[Literal["response", "pair"], str], ResponseItem | PairTask]
    | None = None,
    frozen: TransportCommitmentV1 | TransportCommitmentV2 | None = None,
    parent: TransportCommitmentV1 | TransportCommitmentV2 | None = None,
) -> PreparedRequestV3 | PreparedRequestV4:
    if context_protocol(context) == "production":
        from judgmentllm.production_transport import (
            ProductionContext,
            TransportCommitmentV2,
            prepare_production_request,
        )

        assert isinstance(context, ProductionContext)
        if (frozen is not None and not isinstance(frozen, TransportCommitmentV2)) or (
            parent is not None and not isinstance(parent, TransportCommitmentV2)
        ):
            raise ValueError(
                "production preparation cannot consume synthetic commitments"
            )
        return prepare_production_request(
            spec,
            dependency,
            identity,
            context,
            template=template,
            lookup=lookup,
            frozen=frozen,
            parent=parent,
        )
    from judgmentllm.transport import (
        TransportCommitmentV1,
        TransportContext,
        prepare_transport_request,
    )

    assert isinstance(context, TransportContext)
    if (frozen is not None and not isinstance(frozen, TransportCommitmentV1)) or (
        parent is not None and not isinstance(parent, TransportCommitmentV1)
    ):
        raise ValueError("synthetic preparation cannot consume production commitments")
    return prepare_transport_request(
        spec,
        dependency,
        identity,
        context,
        template=template,
        lookup=lookup,
        frozen=frozen,
        parent=parent,
    )


def render_request(
    spec: RunSpecV3,
    template: str | None,
    request: PlannedRequest,
    lookup: Callable[[Literal["response", "pair"], str], ResponseItem | PairTask],
    wire: PreparedRequestV3 | PreparedRequestV4,
) -> RenderedPromptV3 | RenderedProductionPromptV1:
    from judgmentllm.production_transport import PreparedRequestV4

    if isinstance(wire, PreparedRequestV4):
        from judgmentllm.production_prompts import render_production_request

        return render_production_request(
            spec, template, request, lookup, wire.output_commitment
        )
    from judgmentllm.native_prompts import render_request_v3

    return render_request_v3(spec, template, request, lookup, wire.output_commitment)


def build_output(
    spec: RunSpecV3,
    request: PlannedRequest,
    context: TransportContext | ProductionContext,
    parent: TransportCommitmentV1 | TransportCommitmentV2,
) -> OutputCommitmentV3 | OutputCommitmentV4:
    """Render an ordered subset under its original protocol and selection."""
    if context_protocol(context) == "production":
        from judgmentllm.production_transport import (
            ProductionContext,
            TransportCommitmentV2,
            build_output_commitment_v4,
        )

        assert isinstance(context, ProductionContext)
        if not isinstance(parent, TransportCommitmentV2):
            raise ValueError("production subset cannot inherit a synthetic selection")
        return build_output_commitment_v4(
            spec, request, context, inherited_commitment=parent
        )
    from judgmentllm.transport import (
        TransportCommitmentV1,
        TransportContext,
        build_output_commitment_v3,
    )

    assert isinstance(context, TransportContext)
    if not isinstance(parent, TransportCommitmentV1):
        raise ValueError("synthetic subset cannot inherit a production selection")
    return build_output_commitment_v3(
        spec, request, context, inherited_commitment=parent
    )
