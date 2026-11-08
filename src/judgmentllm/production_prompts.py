"""Additive production output representation using renderer 3's protected text."""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal, cast

from pydantic import Field, JsonValue, model_validator

from judgmentllm.config import RunSpecV3
from judgmentllm.models import PairTask, ResponseItem
from judgmentllm.native_prompts import _render_text_v3
from judgmentllm.output_contracts import generate_output_schema
from judgmentllm.planning import PlannedRequest
from judgmentllm.production_transport import OutputCommitmentV4
from judgmentllm.prompts import PromptError
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

    @model_validator(mode="after")
    def exact_schema(self) -> RenderedProductionPromptV1:
        if self.output_schema != generate_output_schema(
            self.output_commitment.contract
        ):
            raise ValueError("production rendered schema differs")
        return self


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
        text = _render_text_v3(
            spec, template, request, lookup, cast(OutputCommitmentV3, output)
        )
        return RenderedProductionPromptV1(
            text=text,
            exemplar_ids=tuple(item.exemplar_id for item in spec.exemplars),
            output_schema=generate_output_schema(output.contract),
            output_commitment=output,
        )
    except (ValueError, TypeError, AttributeError, KeyError, RecursionError):
        raise PromptError(
            "Production prompt does not satisfy its output contract."
        ) from None
