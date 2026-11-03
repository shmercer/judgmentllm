"""Versioned run configuration and safe, location-aware document loading.

Loading resolves file references; model validation never opens files. Execution
settings describe intent only: Milestone 1 performs no provider operations.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Hashable
from pathlib import Path
from typing import Literal, cast

import yaml
from pydantic import (
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)
from yaml.nodes import MappingNode

from judgmentllm.models import (
    ID,
    Exemplar,
    PairwiseCriterion,
    Rubric,
    StrictModel,
    VersionedModel,
    validate_exemplars,
)
from judgmentllm.prompts import RenderLimits
from judgmentllm.results import NormalizationOptions


class ConfigurationError(ValueError):
    """A configuration document could not be read or validated safely."""


class GenerationSpec(StrictModel):
    """Explicit requested controls, never provider defaults or wire JSON.

    Null members are unspecified and omitted from serialization. Integer and
    float inputs retain their representation; booleans and strings are rejected.
    Provider/model-specific bounds are checked separately by preflight.
    """

    model_config = ConfigDict(frozen=True)

    temperature: int | float | None = Field(
        default=None, ge=0, exclude_if=lambda value: value is None
    )
    top_p: int | float | None = Field(
        default=None, ge=0, le=1, exclude_if=lambda value: value is None
    )
    max_output_tokens: int | None = Field(
        default=None, gt=0, exclude_if=lambda value: value is None
    )


class InputSpec(StrictModel):
    """CSV/JSONL input reference, with an explicit empty-response policy."""

    path: Path
    format: Literal["csv", "jsonl"] | None = None
    allow_empty_text: bool = False

    @field_validator("path", mode="before")
    @classmethod
    def parse_path(cls, value: object, info: ValidationInfo) -> object:
        if isinstance(value, str):
            if not value.strip():
                raise ValueError("path must not be empty")
            return Path(value) if info.mode == "python" else value
        return value

    @model_validator(mode="after")
    def infer_format(self) -> InputSpec:
        if self.format is None:
            extension = self.path.suffix.lower().removeprefix(".")
            if extension not in ("csv", "jsonl"):
                raise ValueError(
                    "input format must be csv or jsonl; specify format explicitly"
                )
            self.format = extension
        return self


class ExecutionSpec(StrictModel):
    """Structural request configuration; no execution is implemented here."""

    mode: Literal["live", "bulk_live", "batch_native"] = "live"
    grouping: Literal["independent", "grouped", "auto"] = "independent"
    groups: list[list[str]] | None = None
    max_traits_per_request: int | None = Field(default=None, gt=0)
    explanations: bool = False
    cache_policy: Literal["off", "auto", "explicit"] | None = None
    tier: str | None = None
    concurrency: int = Field(default=4, gt=0)

    @model_validator(mode="after")
    def validate_group_configuration(self) -> ExecutionSpec:
        if self.groups is not None and self.grouping != "grouped":
            raise ValueError("execution.groups requires grouping: grouped")
        return self


class PromptSpec(StrictModel):
    """One built-in preset or a custom text template; advanced mode is opt-in."""

    preset: str | None = None
    file: Path | None = None
    advanced: bool = False
    limits: RenderLimits = Field(default_factory=RenderLimits)

    @field_validator("file", mode="before")
    @classmethod
    def parse_template_path(cls, value: object, info: ValidationInfo) -> object:
        if isinstance(value, str) and not value.strip():
            raise ValueError("prompt.file must not be empty")
        return (
            Path(value) if isinstance(value, str) and info.mode == "python" else value
        )

    @model_validator(mode="after")
    def validate_source(self) -> PromptSpec:
        if self.preset is not None and self.file is not None:
            raise ValueError("prompt must choose preset or file, not both")
        if self.advanced and self.file is None:
            raise ValueError("advanced templating requires a custom prompt.file")
        return self


class RunSpec(VersionedModel):
    """Resolved provider-neutral assessment specification, schema version 1."""

    task_kind: Literal["rubric", "pairwise"]
    input: InputSpec
    rubric: Rubric | None = None
    criteria: list[PairwiseCriterion] = Field(
        default_factory=lambda: list[PairwiseCriterion]()
    )
    pairs: InputSpec | None = None
    exemplars: list[Exemplar] = Field(default_factory=lambda: list[Exemplar]())
    provider: ID
    endpoint: ID
    model: ID
    generation: GenerationSpec | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    execution: ExecutionSpec = Field(default_factory=ExecutionSpec)
    prompt: PromptSpec = Field(default_factory=PromptSpec)
    normalization: NormalizationOptions = Field(default_factory=NormalizationOptions)
    capture: Literal["full", "minimal"] = "full"
    seed: int | None = None
    output_path: Path = Path("run-data")
    strict_capabilities: bool = False
    allow_ties: bool = False

    @field_validator("generation")
    @classmethod
    def normalize_generation(
        cls, value: GenerationSpec | None
    ) -> GenerationSpec | None:
        if value is None:
            return None
        # Reconstruct instances too: model_copy can inject invalid/unknown fields
        # that an ordinary serializer would silently drop before normalization.
        checked = GenerationSpec.model_validate(dict(value))
        return checked if checked.model_dump() else None

    @field_validator("input", "pairs", mode="before")
    @classmethod
    def expand_input_path(cls, value: object) -> object:
        if isinstance(value, (str, Path)):
            return {"path": value}
        return value

    @field_validator("output_path", mode="before")
    @classmethod
    def parse_output_path(cls, value: object, info: ValidationInfo) -> object:
        if isinstance(value, str) and not value.strip():
            raise ValueError("output_path must not be empty")
        return (
            Path(value) if isinstance(value, str) and info.mode == "python" else value
        )

    @field_validator("provider", "endpoint", "model")
    @classmethod
    def require_nonempty_identifier(cls, value: str) -> str:
        if not value.strip() or value != value.strip():
            raise ValueError("must be nonempty without surrounding whitespace")
        return value

    @model_validator(mode="after")
    def validate_assessment(self) -> RunSpec:
        if self.task_kind == "rubric":
            if self.rubric is None:
                raise ValueError("task_kind: rubric requires rubric")
            if self.criteria or self.pairs is not None or self.allow_ties:
                raise ValueError(
                    "criteria, pairs, and allow_ties are pairwise-only settings"
                )
        else:
            if self.rubric is not None:
                raise ValueError("task_kind: pairwise uses criteria, not rubric")
            if not self.criteria or self.pairs is None:
                raise ValueError("task_kind: pairwise requires criteria and pairs")
            if self.execution.grouping != "independent":
                raise ValueError("pairwise preparation requires grouping: independent")
            ids = [criterion.trait_id for criterion in self.criteria]
            if len(ids) != len(set(ids)):
                raise ValueError("duplicate pairwise criterion trait_id")
        validate_exemplars(self.exemplars, self.rubric, self.criteria)
        if self.execution.cache_policy is None:
            self.execution = self.execution.model_copy(
                update={"cache_policy": "auto" if self.task_kind == "rubric" else "off"}
            )
        if self.prompt.preset is None and self.prompt.file is None:
            preset = "pairwise_forced_choice"
            if self.task_kind == "rubric":
                preset = (
                    "rubric_scores_with_explanations"
                    if self.execution.explanations
                    else "rubric_scores_only"
                )
            self.prompt = self.prompt.model_copy(update={"preset": preset})
        preset = self.prompt.preset
        if preset is not None:
            expected = {
                "rubric_scores_only": ("rubric", False),
                "rubric_scores_with_explanations": ("rubric", True),
                "pairwise_forced_choice": ("pairwise", self.execution.explanations),
            }
            if preset not in expected:
                raise ValueError(f"unknown prompt preset: {preset}")
            if expected[preset] != (self.task_kind, self.execution.explanations):
                raise ValueError(
                    "prompt preset is incompatible with task_kind or explanations"
                )
            if preset == "pairwise_forced_choice" and self.allow_ties:
                raise ValueError("allow_ties requires a custom prompt.file")
        return self


class _ExemplarDocument(VersionedModel):
    exemplars: list[Exemplar]


class _CriteriaDocument(VersionedModel):
    criteria: list[PairwiseCriterion]


class _UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader with duplicate-key rejection instead of last-value wins."""

    def construct_mapping(
        self, node: MappingNode, deep: bool = False
    ) -> dict[Hashable, object]:
        self.flatten_mapping(node)
        result: dict[Hashable, object] = {}
        for key_node, value_node in node.value:
            # PyYAML's stub leaves the construct_object node parameter untyped.
            key: object = self.construct_object(key_node, deep=deep)  # pyright: ignore[reportUnknownMemberType]
            if not isinstance(key, Hashable):
                raise ConfigurationError("YAML mapping keys must be scalar values")
            if key in result:
                raise ConfigurationError(
                    f"duplicate YAML key {key!r} at line {key_node.start_mark.line + 1}"
                )
            result[key] = self.construct_object(value_node, deep=deep)  # pyright: ignore[reportUnknownMemberType]
        return result


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ConfigurationError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise ConfigurationError(f"nonfinite JSON value {value} is not permitted")


def parse_json(text: str) -> object:
    """Parse strict JSON, rejecting duplicate keys and nonfinite constants."""
    return cast(
        object,
        json.loads(
            text,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
        ),
    )


def read_document(path: str | Path) -> object:
    """Read safe YAML/JSON without accepting duplicate mapping keys."""
    source = Path(path)
    try:
        content = source.read_text(encoding="utf-8")
        return parse_document(content, source)
    except (OSError, UnicodeError, yaml.YAMLError, ValueError) as exc:
        raise ConfigurationError(f"{source}: {exc}") from exc


def parse_document(content: str, source: str | Path) -> object:
    """Parse verified document text using its original source extension.

    Snapshot names need not retain the original extension. Source preparation
    supplies the original location here and never reopens that location.
    """
    path = Path(source)
    if path.suffix.lower() == ".json":
        return parse_json(content)
    if path.suffix.lower() not in (".yaml", ".yml"):
        raise ConfigurationError(
            "configuration extension must be .yaml, .yml, or .json"
        )
    try:
        return cast(object, yaml.load(content, Loader=_UniqueKeyLoader))
    except yaml.YAMLError as exc:
        # Keep the established configuration-error boundary for snapshot users
        # too, so preparation can suppress source-bearing parser diagnostics.
        raise ConfigurationError(str(exc)) from exc


def _validation_message(error: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(map(str, entry['loc'])) or '<document>'}: {entry['msg']}"
        for entry in error.errors(include_input=False, include_url=False)
    )


def _mapping(value: object, source: Path) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ConfigurationError(f"{source}: expected an object with string keys")
    mapping = cast(dict[object, object], value)
    if any(not isinstance(key, str) for key in mapping):
        raise ConfigurationError(f"{source}: expected an object with string keys")
    return cast(dict[str, object], mapping).copy()


def _relative_path(value: str | Path, source: Path) -> Path:
    if isinstance(value, str) and not value.strip():
        raise ConfigurationError(f"{source}: file path must not be empty")
    path = Path(value)
    return (
        (source.parent / path).resolve() if not path.is_absolute() else path.resolve()
    )


def load_rubric(path: str | Path) -> Rubric:
    """Load and validate a versioned rubric from YAML or JSON."""
    source = Path(path)
    try:
        return Rubric.model_validate(read_document(source))
    except ValidationError as exc:
        raise ConfigurationError(f"{source}: {_validation_message(exc)}") from exc


def load_run_spec(path: str | Path) -> RunSpec:
    """Load run configuration and resolve references against containing files.

    Referenced criteria/exemplar YAML or JSON documents contain schema_version
    and a criteria/exemplars list respectively. Paths inside the root run file
    always resolve against that file, including output paths and templates.
    """
    return _load_run_spec(path, document_reader=read_document)


def _load_run_spec(
    path: str | Path, *, document_reader: Callable[[Path], object]
) -> RunSpec:
    """Resolve a run using a caller-owned, verified document reader.

    The reader receives original resolved paths, allowing source preparation to
    capture referenced documents before parsing without changing resolution or
    assessment validation. The public loader retains its existing behavior.
    """
    source = Path(path).resolve()
    data = _mapping(document_reader(source), source)
    for field in ("input", "pairs"):
        value = data.get(field)
        if isinstance(value, (str, Path)):
            data[field] = {"path": _relative_path(value, source)}
        elif isinstance(value, dict):
            reference = _mapping(cast(dict[object, object], value), source)
            if isinstance(reference.get("path"), (str, Path)):
                reference["path"] = _relative_path(
                    cast(str | Path, reference["path"]), source
                )
            data[field] = reference
    rubric = data.get("rubric")
    if isinstance(rubric, (str, Path)):
        target = _relative_path(rubric, source)
        try:
            data["rubric"] = Rubric.model_validate(document_reader(target))
        except ValidationError as exc:
            raise ConfigurationError(f"{target}: {_validation_message(exc)}") from exc
    try:
        for field, document_type in (
            ("exemplars", _ExemplarDocument),
            ("criteria", _CriteriaDocument),
        ):
            reference = data.get(field)
            if isinstance(reference, (str, Path)):
                target = _relative_path(reference, source)
                try:
                    document = document_type.model_validate(document_reader(target))
                except ValidationError as exc:
                    raise ConfigurationError(
                        f"{target}: {_validation_message(exc)}"
                    ) from exc
                data[field] = (
                    document.exemplars
                    if isinstance(document, _ExemplarDocument)
                    else document.criteria
                )
        prompt = data.get("prompt")
        if isinstance(prompt, dict):
            prompt_data = _mapping(cast(dict[object, object], prompt), source)
            if isinstance(prompt_data.get("file"), (str, Path)):
                prompt_data["file"] = _relative_path(
                    cast(str | Path, prompt_data["file"]), source
                )
            data["prompt"] = prompt_data
        output = data.get("output_path", "run-data")
        if isinstance(output, (str, Path)):
            data["output_path"] = _relative_path(output, source)
        return RunSpec.model_validate(data)
    except ValidationError as exc:
        raise ConfigurationError(f"{source}: {_validation_message(exc)}") from exc
