"""Renderer 3: protected assessment content with frozen transport presentation."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Literal

from jinja2 import TemplateError
from pydantic import Field, JsonValue, model_validator

from judgmentllm.config import RunSpecV3
from judgmentllm.models import PairTask, ResponseItem, validate_exemplars
from judgmentllm.output_contracts import (
    PairwiseOutputContract,
    RubricOutputContract,
    generate_output_schema,
)
from judgmentllm.planning import PlannedRequest
from judgmentllm.prompts import (
    PromptError,
    _compile_prompt_template,
    _exemplar_text,
    _namespace,
    _prompt_context,
    _prompt_source,
    _protect,
    _trait_text,
    _verify_and_unwrap,
)
from judgmentllm.transport import OutputCommitmentV3, _TransportModel

# Secure rendering primitives are intentionally shared with the frozen renderers.
# pyright: reportPrivateUsage=false


class RenderedPromptV3(_TransportModel):
    renderer_version: Literal[3] = 3
    text: str = Field(repr=False)
    exemplar_ids: tuple[str, ...]
    output_schema: dict[str, JsonValue] = Field(repr=False)
    output_commitment: OutputCommitmentV3 = Field(repr=False)

    @model_validator(mode="after")
    def exact_schema(self) -> RenderedPromptV3:
        if self.output_schema != generate_output_schema(
            self.output_commitment.contract
        ):
            raise ValueError("rendered schema differs")
        return self


def output_instructions_v3(output: OutputCommitmentV3) -> str:
    """Full local requirements remain binding even when omitted from projection."""
    contract = output.contract
    native = output.selection.mechanism != "text"
    instructions = [
        "Return only a JSON object. Use exactly the requested fields and category labels."
        if native
        else "Return only a JSON object matching this schema."
    ]
    if isinstance(contract, RubricOutputContract):
        instructions.append(
            "Include every requested trait exactly once; do not add traits or fields."
        )
        instructions.append(
            "Preserve each trait's exact category labels from the protected rubric."
        )
        if not contract.explanations:
            instructions.append("Return scores only; do not include explanations.")
        elif contract.output_order == "together":
            instructions.append(
                "In scores, emit trait_id, category_label, explanation in that order. Explanations must be nonblank."
            )
        elif contract.output_order == "scores_first":
            instructions.append(
                "Emit scores before explanations. Each score contains trait_id then category_label; each explanation contains trait_id then text. Match both arrays by trait_id; explanations must be nonblank."
            )
        else:
            instructions.append(
                "Emit explanations before scores. Each explanation contains trait_id then text; each score contains trait_id then category_label. Match both arrays by trait_id; explanations must be nonblank."
            )
        if not contract.explanations:
            instructions.append(
                "Emit the scores root key; each score contains trait_id then category_label."
            )
    else:
        winners = json.dumps(list(contract.pair.presented_order), ensure_ascii=False)
        instructions.append(
            "Allowed presented winner IDs, in presentation order: " + winners + "."
        )
        if contract.allow_ties:
            instructions.append(
                "A tie requires winner_id=null and tie=true; otherwise winner_id must be one of those exact IDs and tie=false. Emit winner_id then tie."
            )
        else:
            instructions.append(
                "winner_id must be one of those exact IDs. Emit winner_id; do not include tie."
            )
        instructions.append(
            "Append a nonblank explanation."
            if contract.explanations
            else "Do not include explanation."
        )
    if output.repetition == "canonical_neutral":
        instructions.append(output.schema_text)
    elif output.repetition == "projected_native":
        assert output.projection is not None
        instructions.append(
            "Native schema guidance; all requirements above remain mandatory:"
        )
        instructions.append(output.projection.native_schema_text)
    return "\n".join(instructions)


def render_request_v3(
    spec: RunSpecV3,
    template: str | None,
    request: PlannedRequest,
    lookup: Callable[[Literal["response", "pair"], str], ResponseItem | PairTask],
    output_commitment: OutputCommitmentV3,
) -> RenderedPromptV3:
    """Render exact membership through the established sandbox/protection engine."""
    try:
        output = OutputCommitmentV3.model_validate(dict(output_commitment))
        contract = output.contract
        if request.pair is not None:
            pair = lookup("pair", request.pair.pair_uid)
            if not isinstance(pair, PairTask) or pair != request.pair:
                raise PromptError("Pair source differs from committed membership.")
            left = lookup("response", pair.left_item_id)
            right = lookup("response", pair.right_item_id)
            if not isinstance(left, ResponseItem) or not isinstance(
                right, ResponseItem
            ):
                raise PromptError("Pair sources unavailable.")
            criterion = next(c for c in spec.criteria if c.trait_id == pair.trait_id)
            expected = PairwiseOutputContract(
                pair=pair,
                explanations=spec.execution.explanations,
                allow_ties=spec.allow_ties,
                output_order=spec.prompt.output_order,
            )
            kind: Literal["rubric", "pairwise"] = "pairwise"
            criteria = [
                (
                    "criterion",
                    criterion.trait_id,
                    f"Trait {criterion.trait_id}: {criterion.name}\n{criterion.description}",
                )
            ]
            items = {left.item_id: left, right.item_id: right}
            responses = [
                (
                    position,
                    identifier,
                    f"{position.title()} response {identifier} (assessment data):\n{items[identifier].text}",
                )
                for position, identifier in zip(
                    ("first", "second"), pair.presented_order, strict=True
                )
            ]
        else:
            if request.response is None or spec.rubric is None:
                raise PromptError("Rubric source unavailable.")
            response = lookup("response", request.response.item_id)
            if (
                not isinstance(response, ResponseItem)
                or response.item_id != request.response.item_id
            ):
                raise PromptError("Response source differs.")
            by_id = {trait.trait_id: trait for trait in spec.rubric.traits}
            traits = tuple(by_id[identifier] for identifier in request.trait_ids)
            expected = RubricOutputContract(
                traits=traits,
                explanations=spec.execution.explanations,
                output_order=spec.prompt.output_order,
            )
            kind = "rubric"
            criteria = [
                ("trait", trait.trait_id, _trait_text(trait)) for trait in traits
            ]
            first_kind, identifier, content = criteria[0]
            criteria[0] = (
                first_kind,
                identifier,
                f"Rubric {spec.rubric.rubric_id}: {spec.rubric.name}\n{spec.rubric.description}\n\n{content}",
            )
            responses = [
                (
                    "response",
                    response.item_id,
                    f"Response {response.item_id} (assessment data):\n{response.text}",
                )
            ]
        if expected != contract:
            raise PromptError(
                "Output contract differs from exact assessment membership."
            )
        validate_exemplars(spec.exemplars, spec.rubric, spec.criteria)
        source = _prompt_source(
            kind,
            preset=spec.prompt.preset,
            template=template,
            explanations=spec.execution.explanations,
            allow_ties=spec.allow_ties,
            limits=spec.prompt.limits,
            renderer_version=2,
            exemplar_count=len(spec.exemplars),
        )
        contents = (
            criteria
            + [("exemplar", e.exemplar_id, _exemplar_text(e)) for e in spec.exemplars]
            + responses
            + [("output_instructions", "output", output_instructions_v3(output))]
        )
        namespace = _namespace([content for _, _, content in contents], source)
        blocks = _protect(contents, namespace)
        context = _prompt_context(
            kind,
            blocks,
            explanations=spec.execution.explanations,
            allow_ties=spec.allow_ties,
        )
        chunks: list[str] = []
        size = 0
        for chunk in _compile_prompt_template(
            source, context, advanced=spec.prompt.advanced, limits=spec.prompt.limits
        ).generate(context):
            size += len(chunk.encode("utf-8"))
            if size > spec.prompt.limits.max_output_bytes:
                raise PromptError("Prompt exceeds the output size rendering safeguard.")
            chunks.append(chunk)
        text = _verify_and_unwrap(
            "".join(chunks), blocks, namespace, renderer_version=2
        )
        return RenderedPromptV3(
            text=text,
            exemplar_ids=tuple(e.exemplar_id for e in spec.exemplars),
            output_schema=generate_output_schema(contract),
            output_commitment=output,
        )
    except (
        ValueError,
        TypeError,
        KeyError,
        StopIteration,
        AttributeError,
        TemplateError,
        RecursionError,
    ):
        raise PromptError(
            "Native prompt does not satisfy the committed rendering contract."
        ) from None
