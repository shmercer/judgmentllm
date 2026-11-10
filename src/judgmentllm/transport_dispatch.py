"""Explicit trusted protocol dispatch, independent of RunSpec version.

Historical synthetic records never acquire production interpretation. Imports
are local to keep the preparation/provider dependency graph acyclic.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from judgmentllm import gemini_types, vertex_types

if TYPE_CHECKING:
    from collections.abc import Callable

    from judgmentllm.anthropic_prompts import RenderedAnthropicPromptV1
    from judgmentllm.anthropic_transport import (
        AnthropicProductionContext,
        OutputCommitmentV5,
        PreparedRequestV5,
        TransportCommitmentV3,
    )
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
    context: TransportContext
    | ProductionContext
    | AnthropicProductionContext
    | vertex_types.VertexProductionContext
    | gemini_types.GeminiProductionContext
    | None,
) -> Literal["synthetic", "production", "anthropic", "gemini", "vertex"]:
    from judgmentllm.anthropic_transport import AnthropicProductionContext
    from judgmentllm.production_transport import ProductionContext
    from judgmentllm.transport import TransportContext

    if type(context) is TransportContext:
        return "synthetic"
    if type(context) is vertex_types.VertexProductionContext:
        return "vertex"
    if type(context) is gemini_types.GeminiProductionContext:
        return "gemini"
    if type(context) is AnthropicProductionContext:
        return "anthropic"
    if type(context) is ProductionContext:
        return "production"
    raise ValueError("an explicit trusted transport context is required")


def context_digest(
    context: TransportContext
    | ProductionContext
    | AnthropicProductionContext
    | vertex_types.VertexProductionContext
    | gemini_types.GeminiProductionContext,
) -> str:
    if context_protocol(context) == "vertex":
        from judgmentllm.vertex_transport import production_context_digest

        assert isinstance(context, vertex_types.VertexProductionContext)
        return production_context_digest(context)
    if context_protocol(context) == "gemini":
        from judgmentllm.gemini_transport import production_context_digest

        assert isinstance(context, gemini_types.GeminiProductionContext)
        return production_context_digest(context)
    if context_protocol(context) == "anthropic":
        from judgmentllm.anthropic_transport import (
            AnthropicProductionContext,
            production_context_digest,
        )

        assert isinstance(context, AnthropicProductionContext)
        return production_context_digest(context)
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
) -> Literal["historical", "synthetic", "production", "anthropic", "gemini", "vertex"]:
    from judgmentllm.preparation.manifest import (
        ExecutableRunManifest,
        ExecutableRunManifestV2,
        ExecutableRunManifestV3,
        ExecutableRunManifestV4,
        ExecutableRunManifestV5,
    )

    if type(manifest) is vertex_types.ExecutableRunManifestV7:
        return "vertex"
    if type(manifest) is gemini_types.ExecutableRunManifestV6:
        return "gemini"
    if type(manifest) is ExecutableRunManifestV5:
        return "anthropic"
    if type(manifest) is ExecutableRunManifestV4:
        return "production"
    if type(manifest) is ExecutableRunManifestV3:
        return "synthetic"
    if type(manifest) in (ExecutableRunManifest, ExecutableRunManifestV2):
        return "historical"
    raise ValueError("unknown sealed manifest protocol")


def verify_protocol(
    manifest: ExecutableManifest,
    context: TransportContext
    | ProductionContext
    | AnthropicProductionContext
    | vertex_types.VertexProductionContext
    | gemini_types.GeminiProductionContext
    | None,
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
    context: TransportContext
    | ProductionContext
    | AnthropicProductionContext
    | vertex_types.VertexProductionContext
    | gemini_types.GeminiProductionContext,
    *,
    template: str | None = None,
    lookup: Callable[[Literal["response", "pair"], str], ResponseItem | PairTask]
    | None = None,
    frozen: TransportCommitmentV1
    | TransportCommitmentV2
    | TransportCommitmentV3
    | vertex_types.TransportCommitmentV5
    | gemini_types.TransportCommitmentV4
    | None = None,
    parent: TransportCommitmentV1
    | TransportCommitmentV2
    | TransportCommitmentV3
    | vertex_types.TransportCommitmentV5
    | gemini_types.TransportCommitmentV4
    | None = None,
) -> (
    PreparedRequestV3
    | PreparedRequestV4
    | PreparedRequestV5
    | vertex_types.PreparedRequestV7
    | gemini_types.PreparedRequestV6
):
    if context_protocol(context) == "vertex":
        from judgmentllm.vertex_transport import prepare_production_request

        assert isinstance(context, vertex_types.VertexProductionContext)
        if (
            frozen is not None
            and type(frozen) is not vertex_types.TransportCommitmentV5
        ) or (
            parent is not None
            and type(parent) is not vertex_types.TransportCommitmentV5
        ):
            raise ValueError("Vertex preparation requires Vertex commitments")
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
    if context_protocol(context) == "gemini":
        from judgmentllm.gemini_transport import prepare_production_request

        assert isinstance(context, gemini_types.GeminiProductionContext)
        if (
            frozen is not None
            and type(frozen) is not gemini_types.TransportCommitmentV4
        ) or (
            parent is not None
            and type(parent) is not gemini_types.TransportCommitmentV4
        ):
            raise ValueError("Gemini preparation requires Gemini commitments")
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
    if context_protocol(context) == "anthropic":
        from judgmentllm.anthropic_transport import (
            AnthropicProductionContext,
            TransportCommitmentV3,
            prepare_production_request,
        )

        assert isinstance(context, AnthropicProductionContext)
        if (frozen is not None and type(frozen) is not TransportCommitmentV3) or (
            parent is not None and type(parent) is not TransportCommitmentV3
        ):
            raise ValueError("Anthropic preparation requires Anthropic commitments")
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
    wire: PreparedRequestV3
    | PreparedRequestV4
    | PreparedRequestV5
    | vertex_types.PreparedRequestV7
    | gemini_types.PreparedRequestV6,
) -> (
    RenderedPromptV3
    | RenderedProductionPromptV1
    | RenderedAnthropicPromptV1
    | vertex_types.RenderedVertexPromptV1
    | gemini_types.RenderedGeminiPromptV1
):
    from judgmentllm.anthropic_transport import PreparedRequestV5

    if isinstance(wire, vertex_types.PreparedRequestV7):
        from judgmentllm.vertex_prompts import render_vertex_request

        return render_vertex_request(
            spec, template, request, lookup, wire.output_commitment
        )
    if isinstance(wire, gemini_types.PreparedRequestV6):
        from judgmentllm.gemini_prompts import render_gemini_request

        return render_gemini_request(
            spec, template, request, lookup, wire.output_commitment
        )
    if isinstance(wire, PreparedRequestV5):
        from judgmentllm.anthropic_prompts import render_anthropic_request

        return render_anthropic_request(
            spec, template, request, lookup, wire.output_commitment
        )
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
    context: TransportContext
    | ProductionContext
    | AnthropicProductionContext
    | vertex_types.VertexProductionContext
    | gemini_types.GeminiProductionContext,
    parent: TransportCommitmentV1
    | TransportCommitmentV2
    | TransportCommitmentV3
    | vertex_types.TransportCommitmentV5
    | gemini_types.TransportCommitmentV4,
) -> (
    OutputCommitmentV3
    | OutputCommitmentV4
    | OutputCommitmentV5
    | vertex_types.OutputCommitmentV7
    | gemini_types.OutputCommitmentV6
):
    """Render an ordered subset under its original protocol and selection."""
    if context_protocol(context) == "vertex":
        from judgmentllm.vertex_transport import build_output_commitment_v7

        assert isinstance(context, vertex_types.VertexProductionContext)
        if type(parent) is not vertex_types.TransportCommitmentV5:
            raise ValueError("Vertex subsets require Vertex parent commitments")
        return build_output_commitment_v7(
            spec, request, context, inherited_commitment=parent
        )
    if context_protocol(context) == "gemini":
        from judgmentllm.gemini_transport import build_output_commitment_v6

        assert isinstance(context, gemini_types.GeminiProductionContext)
        if type(parent) is not gemini_types.TransportCommitmentV4:
            raise ValueError("Gemini subsets require Gemini parent commitments")
        return build_output_commitment_v6(
            spec, request, context, inherited_commitment=parent
        )
    if context_protocol(context) == "anthropic":
        from judgmentllm.anthropic_transport import (
            AnthropicProductionContext,
            TransportCommitmentV3,
            build_output_commitment_v5,
        )

        assert isinstance(context, AnthropicProductionContext)
        if type(parent) is not TransportCommitmentV3:
            raise ValueError("Anthropic subsets require Anthropic parent commitments")
        return build_output_commitment_v5(
            spec, request, context, inherited_commitment=parent
        )
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
