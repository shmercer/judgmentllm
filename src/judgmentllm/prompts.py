"""Offline text prompts with protected assessment content and a narrow Jinja sandbox.

Templates control wording and layout, never provider wire payloads or validation.
Advanced templates may loop over ``traits`` and ``exemplar_items``; each dictionary
contains its identifier and an immutable ``block`` to include verbatim.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Literal, cast

from jinja2 import StrictUndefined, TemplateError, meta, nodes
from jinja2.sandbox import SandboxedEnvironment
from pydantic import Field, JsonValue

from judgmentllm.models import (
    Exemplar,
    PairTask,
    PairwiseCriterion,
    ResponseItem,
    Rubric,
    StrictModel,
    Trait,
    VersionedModel,
    validate_exemplars,
)


class PromptError(ValueError):
    """An invalid template, incompatible preset, or incomplete rendered prompt."""


class RenderLimits(StrictModel):
    """Rendering safeguards; exceeding a limit fails without truncating content."""

    max_source_bytes: int = Field(default=256 * 1024, gt=0)
    max_ast_nodes: int = Field(default=10_000, gt=0)
    max_ast_depth: int = Field(default=16, gt=0)
    max_loop_iterations: int = Field(default=100_000, gt=0)
    max_output_bytes: int = Field(default=16 * 1024 * 1024, gt=0)


class TemplateInfo(StrictModel):
    """An installed, editable text prompt preset."""

    preset: str
    description: str


class RenderedPrompt(VersionedModel):
    """Exact provider-neutral text and the separately enforced output contract."""

    text: str
    exemplar_ids: list[str]
    output_schema: dict[str, JsonValue]


_PRESETS = {
    "rubric_scores_only": "Ordinal rubric categories without explanations.",
    "rubric_scores_with_explanations": "Ordinal rubric categories and explanations.",
    "pairwise_forced_choice": "Select exactly one winner from the presented pair.",
}


def list_templates() -> list[TemplateInfo]:
    """List the three bundled presets in their documented order."""
    return [
        TemplateInfo(preset=preset, description=description)
        for preset, description in _PRESETS.items()
    ]


def get_template(preset: str) -> str:
    """Read an installed text preset without requiring a source checkout."""
    if preset not in _PRESETS:
        raise PromptError(
            f"Unknown prompt preset {preset!r}; choose one of {', '.join(_PRESETS)}."
        )
    return (
        files("judgmentllm")
        .joinpath("templates", f"{preset}.txt")
        .read_text(encoding="utf-8")
    )


def init_template(preset: str, output: str | Path) -> Path:
    """Copy a preset to a new UTF-8 file; never overwrite an existing file."""
    text = get_template(preset)
    path = Path(output)
    with path.open("x", encoding="utf-8", newline="") as stream:
        stream.write(text)
    return path


class _Environment(SandboxedEnvironment):
    def is_safe_attribute(self, obj: object, attr: str, value: object) -> bool:
        return False

    def is_safe_callable(self, obj: object) -> bool:
        return False


def _environment() -> _Environment:
    environment = _Environment(undefined=StrictUndefined, keep_trailing_newline=True)
    environment.globals.clear()
    environment.filters = {
        key: value
        for key, value in environment.filters.items()
        if key in {"length", "lower", "upper", "trim"}
    }
    environment.tests.clear()
    return environment


_ALLOWED_NODES = (
    nodes.Template,
    nodes.Output,
    nodes.TemplateData,
    nodes.Name,
    nodes.Const,
    nodes.Getitem,
    nodes.For,
    nodes.If,
    nodes.Compare,
    nodes.Operand,
    nodes.And,
    nodes.Or,
    nodes.Not,
    nodes.Filter,
)


def _walk(node: nodes.Node, depth: int = 1) -> Iterator[tuple[nodes.Node, int]]:
    yield node, depth
    for child in node.iter_child_nodes():
        yield from _walk(child, depth + 1)


def _validate_ast(
    ast: nodes.Template,
    context: dict[str, object],
    *,
    advanced: bool,
    limits: RenderLimits,
) -> None:
    count = 0
    iterations = 0
    for node, depth in _walk(ast):
        count += 1
        if count > limits.max_ast_nodes or depth > limits.max_ast_depth:
            raise PromptError(
                "Template exceeds the AST size/depth rendering safeguard."
            )
        if not isinstance(node, _ALLOWED_NODES):
            raise PromptError(
                f"Template operation {type(node).__name__} is not allowed."
            )
        if not advanced and not isinstance(
            node, (nodes.Template, nodes.Output, nodes.TemplateData, nodes.Name)
        ):
            raise PromptError(
                "Simple templates allow {{ name }} placeholders only; "
                "enable advanced templating for loops, conditions, or filters."
            )
        if isinstance(node, nodes.Name) and (
            node.name.startswith("_") or node.name in {"self", "super", "loop"}
        ):
            raise PromptError(f"Template name {node.name!r} is not allowed.")
        if isinstance(node, nodes.For):
            if (
                node.recursive
                or not isinstance(node.iter, nodes.Name)
                or node.iter.name not in {"traits", "exemplar_items"}
                or not isinstance(node.target, nodes.Name)
                or node.target.name in context
                or any(True for _ in node.find_all(nodes.For))
            ):
                raise PromptError(
                    "Loops must be nonnested and iterate directly over traits or "
                    "exemplar_items using a new variable name."
                )
            iterable = cast(list[object], context[node.iter.name])
            # A loop may emit nothing. Budget its whole AST body, not output chunks.
            iterations += len(iterable) * sum(1 for _ in _walk(node))
        if isinstance(node, nodes.Getitem) and not isinstance(node.arg, nodes.Const):
            raise PromptError(
                "Template subscriptions require a literal dictionary key."
            )
    if iterations > limits.max_loop_iterations:
        raise PromptError("Template exceeds the loop iteration rendering safeguard.")
    unknown = meta.find_undeclared_variables(ast).difference(context)
    if unknown:
        raise PromptError(
            f"Unknown prompt placeholder(s): {', '.join(sorted(unknown))}."
        )


@dataclass(frozen=True)
class _Block:
    kind: str
    identifier: str
    content: str
    wrapped: str


def _namespace(contents: Sequence[str], source: str) -> str:
    digest = hashlib.sha256("\0".join(contents).encode()).hexdigest()[:16]
    index = 0
    while True:
        namespace = f"{digest}_{index}"
        prefix = f"<<JUDGMENTLLM_{namespace}_"
        if prefix not in source and all(prefix not in text for text in contents):
            return namespace
        index += 1


def _protect(contents: Sequence[tuple[str, str, str]], namespace: str) -> list[_Block]:
    blocks: list[_Block] = []
    for index, (kind, identifier, content) in enumerate(contents):
        wrapped = (
            f"<<JUDGMENTLLM_{namespace}_BEGIN_{index}>>\n{content}\n"
            f"<<JUDGMENTLLM_{namespace}_END_{index}>>"
        )
        blocks.append(_Block(kind, identifier, content, wrapped))
    return blocks


def _verify_and_unwrap(text: str, blocks: Sequence[_Block], namespace: str) -> str:
    pattern = re.compile(rf"<<JUDGMENTLLM_{namespace}_(BEGIN|END)_(\d+)>>")
    matches = list(pattern.finditer(text))
    seen: list[int] = []
    seen_ids: set[int] = set()
    if len(matches) != 2 * len(blocks):
        raise PromptError(
            "Prompt must include each required response, criterion, exemplar, and "
            "output_instructions block exactly once without changing its content."
        )
    for offset in range(0, len(matches), 2):
        begin, end = matches[offset : offset + 2]
        index = int(begin.group(2))
        if (
            begin.group(1) != "BEGIN"
            or end.group(1) != "END"
            or int(end.group(2)) != index
            or index >= len(blocks)
            or index in seen_ids
        ):
            raise PromptError("Prompt has duplicated or malformed assessment blocks.")
        block = blocks[index]
        if text[begin.end() : end.start()] != f"\n{block.content}\n":
            raise PromptError(
                f"Prompt changed protected {block.kind} content {block.identifier!r}."
            )
        seen.append(index)
        seen_ids.add(index)
    actual_exemplars = [index for index in seen if blocks[index].kind == "exemplar"]
    expected_exemplars = [
        index for index, block in enumerate(blocks) if block.kind == "exemplar"
    ]
    if actual_exemplars != expected_exemplars:
        raise PromptError("Prompt must preserve the complete selected exemplar order.")
    # Internal markers are validation machinery, not text for providers.
    return pattern.sub("", text)


def _trait_text(trait: Trait) -> str:
    lines = [
        f"Trait {trait.trait_id}: {trait.name}",
        trait.description,
        "Ordered categories (label -> numeric code):",
    ]
    for level, code in zip(trait.levels, trait.numeric_values, strict=True):
        lines.append(f"- {json.dumps(level.label)} -> {code}: {level.description}")
    return "\n".join(lines)


def _exemplar_text(exemplar: Exemplar) -> str:
    return "\n".join(
        [
            f"Exemplar {exemplar.exemplar_id} (reference data):",
            exemplar.text,
            "Scores: " + json.dumps(exemplar.scores, ensure_ascii=False),
            "Rationales: " + json.dumps(exemplar.rationales, ensure_ascii=False),
            "Metadata: " + json.dumps(exemplar.metadata, ensure_ascii=False),
        ]
    )


def _rubric_schema(traits: Sequence[Trait], explanations: bool) -> dict[str, JsonValue]:
    choices: list[JsonValue] = []
    for trait in traits:
        properties: dict[str, JsonValue] = {
            "trait_id": {"const": trait.trait_id},
            "category_label": {
                "type": "string",
                "enum": [v.label for v in trait.levels],
            },
        }
        required: list[JsonValue] = ["trait_id", "category_label"]
        if explanations:
            properties["explanation"] = {"type": "string", "minLength": 1}
            required.append("explanation")
        choices.append(
            {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            }
        )
    return {
        "type": "object",
        "properties": {
            "scores": {
                "type": "array",
                "minItems": len(traits),
                "maxItems": len(traits),
                "items": {"oneOf": choices},
            }
        },
        "required": ["scores"],
        "additionalProperties": False,
    }


def _pair_schema(
    pair: PairTask, explanations: bool, allow_ties: bool
) -> dict[str, JsonValue]:
    winners: list[JsonValue] = [pair.left_item_id, pair.right_item_id]
    if allow_ties:
        winners.append(None)
    properties: dict[str, JsonValue] = {
        "winner_id": {"enum": winners},
        "tie": {"type": "boolean"} if allow_ties else {"const": False},
    }
    required: list[JsonValue] = ["winner_id", "tie"]
    if explanations:
        properties["explanation"] = {"type": "string", "minLength": 1}
        required.append("explanation")
    schema: dict[str, JsonValue] = {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }
    if allow_ties:
        schema["oneOf"] = [
            {"properties": {"winner_id": {"const": None}, "tie": {"const": True}}},
            {
                "properties": {
                    "winner_id": {"enum": [pair.left_item_id, pair.right_item_id]},
                    "tie": {"const": False},
                }
            },
        ]
    return schema


def render_prompt(
    *,
    rubric: Rubric | None = None,
    traits: Sequence[Trait] | None = None,
    response: ResponseItem | None = None,
    exemplars: Sequence[Exemplar] = (),
    pair: PairTask | None = None,
    left: ResponseItem | None = None,
    right: ResponseItem | None = None,
    criterion: PairwiseCriterion | None = None,
    explanations: bool = False,
    allow_ties: bool = False,
    preset: str | None = None,
    template: str | None = None,
    advanced: bool = False,
    limits: RenderLimits | None = None,
) -> RenderedPrompt:
    """Render one offline assessment request without credentials or provider calls.

    ``traits`` selects an explicit group in its requested order. All supplied
    exemplars are included in order, including their complete annotations.
    Custom templates must include protected content blocks without alteration.
    Advanced loops use ``entry['block']`` on ``traits``/``exemplar_items`` lists.
    """
    limits = limits or RenderLimits()
    if preset is not None and template is not None:
        raise PromptError("Choose a prompt preset or custom template, not both.")
    if pair is not None:
        kind: Literal["rubric", "pairwise"] = "pairwise"
        if rubric is not None or traits is not None or response is not None:
            raise PromptError(
                "Pairwise prompt arguments cannot include rubric responses."
            )
        if left is None or right is None or criterion is None:
            raise PromptError("Pairwise prompts require left, right, and criterion.")
        if (
            left.item_id != pair.left_item_id
            or right.item_id != pair.right_item_id
            or criterion.trait_id != pair.trait_id
        ):
            raise PromptError("Pairwise items and criterion must match the pair IDs.")
        schema = _pair_schema(pair, explanations, allow_ties)
        criteria = [
            (
                "criterion",
                criterion.trait_id,
                f"Trait {criterion.trait_id}: {criterion.name}\n{criterion.description}",
            )
        ]
        items = {left.item_id: left, right.item_id: right}
        response_contents = [
            (
                position,
                item_id,
                f"{position.title()} response {item_id} (assessment data):\n{items[item_id].text}",
            )
            for position, item_id in zip(
                ("first", "second"), pair.presented_order, strict=True
            )
        ]
        selected: list[Trait] = []
    else:
        kind = "rubric"
        if left is not None or right is not None or criterion is not None or allow_ties:
            raise PromptError("Rubric prompts cannot include pairwise arguments.")
        if response is None or (rubric is None and traits is None):
            raise PromptError("Rubric prompts require a response and rubric or traits.")
        selected = list(
            traits if traits is not None else rubric.traits if rubric else []
        )
        if not selected or len({trait.trait_id for trait in selected}) != len(selected):
            raise PromptError("Select at least one trait with distinct trait IDs.")
        if rubric is not None:
            stored = {trait.trait_id: trait for trait in rubric.traits}
            if any(stored.get(trait.trait_id) != trait for trait in selected):
                raise PromptError(
                    "Selected traits must match traits stored in the rubric."
                )
        schema = _rubric_schema(selected, explanations)
        criteria = [("trait", trait.trait_id, _trait_text(trait)) for trait in selected]
        if rubric is not None:
            first_kind, first_id, first_content = criteria[0]
            criteria[0] = (
                first_kind,
                first_id,
                f"Rubric {rubric.rubric_id}: {rubric.name}\n{rubric.description}\n\n{first_content}",
            )
        response_contents = [
            (
                "response",
                response.item_id,
                f"Response {response.item_id} (assessment data):\n{response.text}",
            )
        ]
    try:
        validation_rubric = rubric or (
            Rubric(rubric_id="prompt", name="Prompt criteria", traits=selected)
            if selected
            else None
        )
        validate_exemplars(
            exemplars,
            rubric=validation_rubric,
            criteria=[criterion] if criterion is not None else (),
        )
    except ValueError as exc:
        raise PromptError(str(exc)) from exc
    if template is None:
        chosen = preset or (
            "pairwise_forced_choice"
            if kind == "pairwise"
            else "rubric_scores_with_explanations"
            if explanations
            else "rubric_scores_only"
        )
        if (chosen == "pairwise_forced_choice") != (kind == "pairwise"):
            raise PromptError("Prompt preset does not match the assessment kind.")
        if chosen == "pairwise_forced_choice" and allow_ties:
            raise PromptError(
                "pairwise_forced_choice does not allow ties; use a custom template."
            )
        if (
            kind == "rubric"
            and (chosen == "rubric_scores_with_explanations") != explanations
        ):
            raise PromptError("Rubric preset must match the explanations setting.")
        source = get_template(chosen)
    else:
        source = template
    if len(source.encode("utf-8")) > limits.max_source_bytes:
        raise PromptError("Template exceeds the source size rendering safeguard.")
    output_instructions = (
        (
            "Return only a JSON object matching this schema. Include every requested "
            "trait exactly once. Preserve category labels exactly."
            if kind == "rubric"
            else "Return only a JSON object matching this schema. winner_id must identify "
            "the winning response; tie=true requires winner_id=null."
        )
        + "\n"
        + json.dumps(schema, ensure_ascii=False, sort_keys=True)
    )
    contents = (
        criteria
        + [
            ("exemplar", exemplar.exemplar_id, _exemplar_text(exemplar))
            for exemplar in exemplars
        ]
        + response_contents
        + [("output_instructions", "output", output_instructions)]
    )
    namespace = _namespace([content for _, _, content in contents], source)
    blocks = _protect(contents, namespace)
    by_kind: dict[str, list[_Block]] = {}
    for block in blocks:
        by_kind.setdefault(block.kind, []).append(block)
    rubric_blocks = [
        block for block in blocks if block.kind in {"rubric", "trait", "criterion"}
    ]
    context: dict[str, object] = {
        "rubric": "\n\n".join(block.wrapped for block in rubric_blocks),
        "trait": "\n\n".join(block.wrapped for block in rubric_blocks),
        "criterion": "\n\n".join(block.wrapped for block in rubric_blocks),
        "exemplars": "\n\n".join(
            block.wrapped for block in by_kind.get("exemplar", [])
        ),
        "output_instructions": by_kind["output_instructions"][0].wrapped,
        "explanations": explanations,
        "allow_ties": allow_ties,
        "traits": [
            {"trait_id": block.identifier, "block": block.wrapped}
            for block in rubric_blocks
        ],
        "exemplar_items": [
            {"exemplar_id": block.identifier, "block": block.wrapped}
            for block in by_kind.get("exemplar", [])
        ],
    }
    if kind == "rubric":
        context["response"] = by_kind["response"][0].wrapped
    else:
        context["left_response"] = by_kind["first"][0].wrapped
        context["right_response"] = by_kind["second"][0].wrapped
        context["first_response"] = by_kind["first"][0].wrapped
        context["second_response"] = by_kind["second"][0].wrapped
    try:
        environment = _environment()
        ast = environment.parse(source)
        _validate_ast(ast, context, advanced=advanced, limits=limits)
        rendered: list[str] = []
        size = 0
        for chunk in environment.from_string(ast).generate(context):
            size += len(chunk.encode("utf-8"))
            if size > limits.max_output_bytes:
                raise PromptError("Prompt exceeds the output size rendering safeguard.")
            rendered.append(chunk)
    except PromptError:
        raise
    except (TemplateError, RecursionError, TypeError, ValueError) as exc:
        raise PromptError(f"Cannot render prompt template: {exc}") from exc
    return RenderedPrompt(
        text=_verify_and_unwrap("".join(rendered), blocks, namespace),
        exemplar_ids=[exemplar.exemplar_id for exemplar in exemplars],
        output_schema=schema,
    )
