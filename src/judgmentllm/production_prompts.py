"""Additive production output representation using renderer 3's protected text."""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal, cast

from pydantic import Field, JsonValue, model_validator

from judgmentllm.cache_policy import CacheBreakpoint
from judgmentllm.config import RunSpecV3
from judgmentllm.models import PairTask, ResponseItem
from judgmentllm.native_prompts import _render_text_v3
from judgmentllm.output_contracts import generate_output_schema
from judgmentllm.persistence.hashing import bytes_digest
from judgmentllm.planning import PlannedRequest
from judgmentllm.production_transport import (
    CachePrefixV1,
    ExplicitCacheLayoutV1,
    OutputCommitmentV4,
)
from judgmentllm.prompts import PromptError, _ProtectedSpan
from judgmentllm.transport import OutputCommitmentV3, _TransportModel

# Shared renderer primitives retain all existing protection and size checks.
# pyright: reportPrivateUsage=false


class RenderedProductionPromptV1(_TransportModel):
    """Renderer 3 text attached to an independently versioned production output."""

    renderer_version: Literal[3] = 3
    text: str = Field(repr=False)
    exemplar_ids: tuple[str, ...]
    output_schema: dict[str, JsonValue] = Field(repr=False)
    output_commitment: OutputCommitmentV4 = Field(repr=False)
    explicit_cache: ExplicitCacheLayoutV1 | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def exact_schema(self) -> RenderedProductionPromptV1:
        if self.output_schema != generate_output_schema(
            self.output_commitment.contract
        ):
            raise ValueError("production rendered schema differs")
        return self


def _cache_layout(
    text: str,
    spans: list[_ProtectedSpan],
    selectors: tuple[CacheBreakpoint, ...],
) -> ExplicitCacheLayoutV1:
    raw = text.encode("utf-8")
    student_start = min(
        span.start for span in spans if span.kind in {"response", "first", "second"}
    )
    prefixes: list[CachePrefixV1] = []
    previous = 0
    for selector in selectors:
        if selector.after == "assessment":
            end = max(
                span.end
                for span in spans
                if span.kind in {"rubric", "trait", "criterion"}
            )
        else:
            matching = [
                span.end
                for span in spans
                if span.kind == selector.after
                and (
                    selector.after != "exemplar"
                    or span.identifier == selector.exemplar_id
                )
            ]
            if len(matching) != 1:
                raise PromptError("Cache breakpoint is missing or ambiguous.")
            end = matching[0]
        if not previous < end < len(raw) or end > student_start:
            raise PromptError(
                "Cache breakpoint is out of order or crosses student input."
            )
        prefixes.append(
            CachePrefixV1(
                anchor=selector.after,
                prefix_bytes=end,
                prefix_sha256=bytes_digest(raw[:end]),
            )
        )
        previous = end
    return ExplicitCacheLayoutV1(prefixes=tuple(prefixes))


def render_production_request(
    spec: RunSpecV3,
    template: str | None,
    request: PlannedRequest,
    lookup: Callable[[Literal["response", "pair"], str], ResponseItem | PairTask],
    output_commitment: OutputCommitmentV4,
) -> RenderedProductionPromptV1:
    """Render independently validated output through the unchanged renderer 3."""
    from judgmentllm.production_transport import OutputCommitmentV4

    try:
        output = OutputCommitmentV4.model_validate(dict(output_commitment))
        # The private renderer consumes the common contract/presentation fields;
        # it does not validate or dispatch a synthetic physical transport.
        spans: list[_ProtectedSpan] | None = (
            [] if output.cache_breakpoints is not None else None
        )
        text = _render_text_v3(
            spec,
            template,
            request,
            lookup,
            cast(OutputCommitmentV3, output),
            protected_spans=spans,
        )
        layout = None
        if spans is not None:
            selectors = output.cache_breakpoints
            if spec.prompt.advanced or selectors != tuple(
                spec.prompt.cache_breakpoints or ()
            ):
                raise PromptError(
                    "Cache breakpoint intent differs from its committed template."
                )
            assert selectors is not None
            layout = _cache_layout(text, spans, selectors)
        return RenderedProductionPromptV1(
            text=text,
            exemplar_ids=tuple(item.exemplar_id for item in spec.exemplars),
            output_schema=generate_output_schema(output.contract),
            output_commitment=output,
            explicit_cache=layout,
        )
    except (ValueError, TypeError, AttributeError, KeyError, RecursionError):
        raise PromptError(
            "Production prompt does not satisfy its output contract."
        ) from None
