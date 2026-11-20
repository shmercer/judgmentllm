"""Versioned run configuration and safe, location-aware document loading.

Loading resolves file references; model validation never opens files. Execution
settings describe intent only: Milestone 1 performs no provider operations.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Hashable
from pathlib import Path
from typing import Annotated, Generic, Literal, TypeAlias, TypeVar, cast

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)
from yaml.nodes import MappingNode

from judgmentllm.cache_policy import (
    OPENAI_CACHE_MECHANISM,
    OPENAI_EXPLICIT_ROUTE,
    CacheBreakpoint,
    CachePolicyVersion,
)
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
from judgmentllm.reasoning import ReasoningSpec
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
    reasoning: ReasoningSpec | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @field_validator("reasoning")
    @classmethod
    def normalize_reasoning(cls, value: ReasoningSpec | None) -> ReasoningSpec | None:
        if value is None:
            return None
        checked = ReasoningSpec.model_validate(dict(value))
        return checked if checked.model_dump() else None


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


class BulkLimits(StrictModel):
    """Optional per-run limits on managed bulk-live submissions."""

    model_config = ConfigDict(frozen=True)
    requests_per_minute: int | None = Field(
        default=None, gt=0, exclude_if=lambda value: value is None
    )
    tokens_per_minute: int | None = Field(
        default=None, gt=0, exclude_if=lambda value: value is None
    )


class ExecutionSpec(StrictModel):
    """Structural request configuration; no execution is implemented here."""

    mode: Literal["live", "bulk_live", "batch_native"] = "live"
    grouping: Literal["independent", "grouped", "auto"] = "independent"
    groups: list[list[str]] | None = None
    max_traits_per_request: int | None = Field(default=None, gt=0)
    explanations: bool = False
    cache_policy: Literal["off", "auto", "explicit"] | None = Field(
        default=None,
        description="New runs accept off (default) or the scoped explicit candidate; auto is historical-only.",
        json_schema_extra={
            "anyOf": [
                {"enum": ["off", "explicit"], "type": "string"},
                {"type": "null"},
            ],
            "default": "off",
        },
    )
    tier: str | None = None
    concurrency: int = Field(default=4, gt=0)
    bulk_limits: BulkLimits | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def validate_group_configuration(self) -> ExecutionSpec:
        if self.bulk_limits is not None and self.mode != "bulk_live":
            raise ValueError("execution.bulk_limits requires mode: bulk_live")
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


class PromptSpecV2(PromptSpec):
    """Version-2 output presentation; assessment explanations are not reasoning."""

    output_order: Literal["together", "scores_first", "explanations_first"] = "together"
    structured_output: Literal["auto", "native", "text"] = "auto"


NativeTransportMechanism: TypeAlias = Literal[
    "openai.responses.text.format",
    "openai.chat.response_format",
    "anthropic.messages.output_config.format",
    "gemini.generate_content.response_schema",
    "gemini.generate_content.response_json_schema",
    "vertex.generate_content.response_schema",
    "vertex.generate_content.response_json_schema",
    "together.chat.response_format",
    "ollama.chat.format",
]


class PromptSpecV3(PromptSpecV2):
    """Explicit native route intent; never an arbitrary provider request body."""

    native_mechanism: NativeTransportMechanism | None = None
    cache_breakpoints: list[CacheBreakpoint] | None = Field(
        default=None, min_length=1, max_length=4, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def explicit_route(self) -> PromptSpecV3:
        if self.structured_output != "text" and self.native_mechanism is None:
            raise ValueError("auto/native transport requires prompt.native_mechanism")
        if self.cache_breakpoints and len(set(self.cache_breakpoints)) != len(
            self.cache_breakpoints
        ):
            raise ValueError("prompt.cache_breakpoints contains duplicate selectors")
        return self


_Version = TypeVar("_Version", bound=int)
_Prompt = TypeVar("_Prompt", bound=PromptSpec)


class _RunSpecFields(StrictModel, Generic[_Version, _Prompt]):
    """Shared assessment fields and validation, independent of root version."""

    schema_version: _Version
    _cache_policy_version: CachePolicyVersion = PrivateAttr(default=2)

    @property
    def cache_policy_version(self) -> CachePolicyVersion:
        """Internal sealed revision; never a user-selectable configuration field."""
        return self._cache_policy_version

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
    prompt: _Prompt
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
    def validate_assessment(
        self, info: ValidationInfo
    ) -> _RunSpecFields[_Version, _Prompt]:
        context = cast(dict[str, object], info.context) if info.context else {}
        explicit = (
            isinstance(self.prompt, PromptSpecV3)
            and self.prompt.cache_breakpoints is not None
            and self.execution.cache_policy == "explicit"
        )
        revision = context.get("cache_policy_version", 3 if explicit else 2)
        if type(revision) is not int or revision not in (1, 2, 3):
            raise ValueError("unsupported cache policy version")
        if (revision == 3) != explicit:
            raise ValueError(
                "cache policy version differs from explicit breakpoint intent"
            )
        self._cache_policy_version = revision
        if self.execution.cache_policy == "auto" and self.cache_policy_version != 1:
            raise ValueError(
                "execution.cache_policy: auto is legacy-only; replace it with off "
                "or use the scoped explicit candidate with semantic breakpoints"
            )
        if (
            self.schema_version != 3
            and self.generation is not None
            and self.generation.reasoning is not None
        ):
            raise ValueError("generation.reasoning requires schema_version: 3")
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
                update={
                    "cache_policy": "auto"
                    if self.cache_policy_version == 1 and self.task_kind == "rubric"
                    else "off"
                }
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


class RunSpec(_RunSpecFields[Literal[1], PromptSpec], VersionedModel):
    """Resolved provider-neutral assessment specification, schema version 1."""

    schema_version: Literal[1] = 1
    prompt: PromptSpec = Field(default_factory=PromptSpec)


class RunSpecV2(_RunSpecFields[Literal[2], PromptSpecV2]):
    """Explicit version-2 run configuration using the shared assessment workflow.

    Constructing this class opts in to v2. Root document loaders dispatch only
    on an explicitly supplied integer ``schema_version: 2``. Referenced
    assessment documents and normalized outputs retain their own versions.
    """

    schema_version: Literal[2] = 2
    prompt: PromptSpecV2 = Field(default_factory=PromptSpecV2)

    @field_validator("schema_version", mode="before")
    @classmethod
    def _version(cls, value: object) -> object:
        if type(value) is not int or value != 2:
            raise ValueError("schema_version must be the supported integer 2")
        return value

    @model_validator(mode="after")
    def validate_output_settings(self) -> RunSpecV2:
        if self.prompt.output_order != "together":
            if self.task_kind == "pairwise":
                raise ValueError(
                    "pairwise assessments require prompt.output_order: together"
                )
            if not self.execution.explanations:
                raise ValueError(
                    "nondefault output order requires execution.explanations: true"
                )
        return self


class RunSpecV3(_RunSpecFields[Literal[3], PromptSpecV3]):
    """Additive native-capable run intent; execution still requires an adapter."""

    schema_version: Literal[3] = 3
    prompt: PromptSpecV3
    account_scope: ID | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @field_validator("schema_version", mode="before")
    @classmethod
    def _version(cls, value: object) -> object:
        if type(value) is not int or value != 3:
            raise ValueError("schema_version must be the supported integer 3")
        return value

    @model_validator(mode="after")
    def transport_settings(self) -> RunSpecV3:
        if self.prompt.cache_breakpoints is not None and (
            self.execution.cache_policy != "explicit"
            or (self.provider, self.endpoint, self.model) != OPENAI_EXPLICIT_ROUTE
            or self.execution.mode != "live"
            or self.execution.tier not in (None, "default")
            or self.prompt.structured_output != "native"
            or self.prompt.native_mechanism != OPENAI_CACHE_MECHANISM
            or self.prompt.advanced
        ):
            raise ValueError(
                "prompt.cache_breakpoints requires explicit caching on Luna Responses "
                "Standard live with native output and a simple template"
            )
        if self.prompt.output_order != "together" and (
            self.task_kind == "pairwise" or not self.execution.explanations
        ):
            raise ValueError("nondefault output order requires rubric explanations")
        mechanism = self.prompt.native_mechanism
        routes = {
            "openai.responses.text.format": ("openai", "responses"),
            "openai.chat.response_format": ("openai", "chat.completions"),
            "anthropic.messages.output_config.format": ("anthropic", "messages"),
            "gemini.generate_content.response_schema": (
                "gemini",
                "models.generate_content",
            ),
            "gemini.generate_content.response_json_schema": (
                "gemini",
                "models.generate_content",
            ),
            "vertex.generate_content.response_schema": (
                "vertex",
                "models.generate_content",
            ),
            "vertex.generate_content.response_json_schema": (
                "vertex",
                "models.generate_content",
            ),
            "together.chat.response_format": ("together", "chat.completions"),
            "ollama.chat.format": ("ollama", "chat"),
        }
        if mechanism is not None and routes[mechanism] != (
            self.provider,
            self.endpoint,
        ):
            raise ValueError("native mechanism conflicts with provider/endpoint")
        return self


class SingleRequestGroupingV1(StrictModel):
    """One question per physical request, with no implicit packing."""

    model_config = ConfigDict(frozen=True)
    grouping_version: Literal[1] = 1
    mode: Literal["single"]
    max_questions_per_request: Literal[1] = 1

    @field_validator("grouping_version", "max_questions_per_request", mode="before")
    @classmethod
    def exact_integer(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("grouping version and maximum must be integers")
        return value


class AutoRequestGroupingV1(StrictModel):
    """Pack only eligible contiguous questions up to an explicit maximum."""

    model_config = ConfigDict(frozen=True)
    grouping_version: Literal[1] = 1
    mode: Literal["auto"]
    max_questions_per_request: int = Field(gt=0)

    @field_validator("grouping_version", mode="before")
    @classmethod
    def exact_integer(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("grouping_version must be integer 1")
        return value


RequestGroupingSpecV1: TypeAlias = Annotated[
    SingleRequestGroupingV1 | AutoRequestGroupingV1, Field(discriminator="mode")
]


class DecisionsExecutionSpecV1(StrictModel):
    """Native question intent; generative grouping is a different authority."""

    model_config = ConfigDict(frozen=True)
    request_grouping: RequestGroupingSpecV1 = Field(
        default_factory=lambda: SingleRequestGroupingV1(mode="single")
    )
    mode: Literal["live", "bulk_live"] = "live"
    explanations: Literal[False] = False
    cache_policy: Literal["off"] = "off"
    tier: ID | None = None
    concurrency: int = Field(default=4, gt=0)
    bulk_limits: BulkLimits | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def validate_bulk_limits(self) -> DecisionsExecutionSpecV1:
        if self.bulk_limits is not None and self.mode != "bulk_live":
            raise ValueError("execution.bulk_limits requires mode: bulk_live")
        return self

    @field_validator("explanations", mode="before")
    @classmethod
    def exact_false(cls, value: object) -> object:
        if value is not False:
            raise ValueError("Decisions does not generate explanations")
        return value


class QuestionPromptSpecV1(StrictModel):
    """One independently rendered question template and explicit exemplar map."""

    file: Path | None = None
    advanced: bool = False
    limits: RenderLimits = Field(default_factory=RenderLimits)
    exemplar_ids_by_trait: dict[ID, tuple[ID, ...]] = Field(default_factory=dict)
    output_instructions: str | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @field_validator("output_instructions")
    @classmethod
    def exact_instructions(cls, value: str | None) -> str | None:
        if value is not None:
            if not value.strip():
                raise ValueError("prompt.output_instructions must be nonblank text")
            try:
                value.encode("utf-8")
            except UnicodeError:
                raise ValueError(
                    "prompt.output_instructions must be valid UTF-8 text"
                ) from None
        return value

    @field_validator("exemplar_ids_by_trait", mode="before")
    @classmethod
    def ordered_ids(cls, value: object) -> object:
        if isinstance(value, dict):
            return {
                key: tuple(cast(list[object], ids)) if isinstance(ids, list) else ids
                for key, ids in cast(dict[object, object], value).items()
            }
        return value

    @field_validator("file", mode="before")
    @classmethod
    def parse_path(cls, value: object, info: ValidationInfo) -> object:
        return PromptSpec.parse_template_path(value, info)

    @model_validator(mode="after")
    def custom_advanced(self) -> QuestionPromptSpecV1:
        if self.advanced and self.file is None:
            raise ValueError("advanced templating requires prompt.file")
        return self


class RunSpecV4(StrictModel):
    """Explicit native Decisions configuration; provider adapters remain separate."""

    schema_version: Literal[4] = 4
    task_kind: Literal["rubric", "pairwise"]
    input: InputSpec
    rubric: Rubric | None = None
    criteria: list[PairwiseCriterion] = Field(
        default_factory=lambda: list[PairwiseCriterion]()
    )
    pairs: InputSpec | None = None
    pair_presentations: InputSpec | None = None
    exemplars: list[Exemplar] = Field(default_factory=lambda: list[Exemplar]())
    provider: ID
    endpoint: ID
    model: ID
    generation: GenerationSpec | None = Field(
        default=None, exclude_if=lambda v: v is None
    )
    execution: DecisionsExecutionSpecV1 = Field(
        default_factory=DecisionsExecutionSpecV1
    )
    prompt: QuestionPromptSpecV1 = Field(default_factory=QuestionPromptSpecV1)
    normalization: NormalizationOptions = Field(default_factory=NormalizationOptions)
    capture: Literal["full", "minimal"] = "full"
    seed: int | None = None
    output_path: Path = Path("run-data")
    strict_capabilities: bool = False
    allow_ties: Literal[False] = False
    account_scope: ID | None = Field(default=None, exclude_if=lambda v: v is None)

    @property
    def cache_policy_version(self) -> Literal[2]:
        return 2

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_version(cls, value: object) -> object:
        if type(value) is not int or value != 4:
            raise ValueError("schema_version must be integer 4")
        return value

    @field_validator("allow_ties", mode="before")
    @classmethod
    def no_ties(cls, value: object) -> object:
        if value is not False:
            raise ValueError("Decisions requires forced choice")
        return value

    @field_validator("input", "pairs", "pair_presentations", mode="before")
    @classmethod
    def input_path(cls, value: object) -> object:
        return {"path": value} if isinstance(value, (str, Path)) else value

    @field_validator("output_path", mode="before")
    @classmethod
    def output_path_value(cls, value: object, info: ValidationInfo) -> object:
        return _RunSpecFields.parse_output_path(value, info)

    @field_validator("generation")
    @classmethod
    def no_generation(cls, value: GenerationSpec | None) -> None:
        if (
            value is not None
            and GenerationSpec.model_validate(_spec_snapshot(value)).model_dump()
        ):
            raise ValueError("generation controls are inapplicable to Decisions")
        return None

    @model_validator(mode="after")
    def assessment(self) -> RunSpecV4:
        if (self.provider, self.endpoint) not in {
            ("mock", "synthetic"),
            ("openai", "decisions"),
            ("typesafe", "systemone"),
            ("gemini", "decisions"),
        }:
            raise ValueError("schema_version 4 requires a native Decisions route")
        if self.task_kind == "rubric":
            if (
                self.rubric is None
                or self.criteria
                or self.pairs
                or self.pair_presentations
            ):
                raise ValueError(
                    "rubric Decisions requires only a rubric and responses"
                )
            ids = [t.trait_id for t in self.rubric.traits]
        else:
            if (
                self.rubric is not None
                or not self.criteria
                or (self.pairs is None) == (self.pair_presentations is None)
            ):
                raise ValueError(
                    "pairwise Decisions requires criteria and exactly one pair source"
                )
            ids = [c.trait_id for c in self.criteria]
            if len(ids) != len(set(ids)):
                raise ValueError("duplicate criterion")
        validate_exemplars(self.exemplars, self.rubric, self.criteria)
        mapping = self.prompt.exemplar_ids_by_trait
        if (self.exemplars and set(mapping) != set(ids)) or set(mapping) - set(ids):
            raise ValueError(
                "prompt.exemplar_ids_by_trait must explicitly cover every question"
            )
        examples = {e.exemplar_id: e for e in self.exemplars}
        for trait, selected in mapping.items():
            if len(selected) != len(set(selected)) or any(
                e not in examples for e in selected
            ):
                raise ValueError(
                    "question exemplar IDs must be unique known references"
                )
            for identifier in selected:
                example = examples[identifier]
                annotated = set(example.scores) | set(example.rationales)
                if annotated and trait not in annotated:
                    raise ValueError("exemplar is annotated only for another question")
        return self


RunSpecification = RunSpec | RunSpecV2 | RunSpecV3 | RunSpecV4


def _spec_snapshot(value: object) -> object:
    """Retain all current fields, including invalid model_copy injections."""
    if isinstance(value, BaseModel):
        return {key: _spec_snapshot(item) for key, item in value}
    if isinstance(value, list):
        return [_spec_snapshot(item) for item in cast(list[object], value)]
    if isinstance(value, tuple):
        return tuple(_spec_snapshot(item) for item in cast(tuple[object, ...], value))
    if isinstance(value, dict):
        return {
            key: _spec_snapshot(item)
            for key, item in cast(dict[object, object], value).items()
        }
    return value


def validate_run_spec(
    value: object, *, _cache_policy_version: CachePolicyVersion | None = None
) -> RunSpecification:
    """Dispatch strict root run versions and revalidate mutable model snapshots.

    Omitted versions select v1. Strings, floats, booleans, and unsupported
    versions fail rather than coerce or migrate. Existing models are detached
    and rejected if validation would repair their current contents.
    """
    if isinstance(value, (RunSpec, RunSpecV2, RunSpecV3, RunSpecV4)):
        expected_version = (
            4
            if isinstance(value, RunSpecV4)
            else 3
            if isinstance(value, RunSpecV3)
            else 2
            if isinstance(value, RunSpecV2)
            else 1
        )
        if (
            type(value.schema_version) is not int
            or value.schema_version != expected_version
        ):
            raise ConfigurationError(
                "run specification version differs from its typed model"
            )
    snapshot = _spec_snapshot(value)
    if not isinstance(snapshot, dict):
        raise ConfigurationError("run specification must be an object")
    data = cast(dict[str, object], snapshot)
    version = data.get("schema_version", 1)
    if type(version) is not int or version not in (1, 2, 3, 4):
        raise ConfigurationError(
            "schema_version must be a supported integer 1, 2, 3, or 4"
        )
    model = (
        RunSpecV4
        if version == 4
        else RunSpecV3
        if version == 3
        else RunSpecV2
        if version == 2
        else RunSpec
    )
    revision = _cache_policy_version
    if (
        revision is None
        and isinstance(value, (RunSpec, RunSpecV2, RunSpecV3))
        and value.cache_policy_version == 1
    ):
        revision = 1
    checked = model.model_validate(
        data, context={} if revision is None else {"cache_policy_version": revision}
    )
    if (
        isinstance(value, (RunSpec, RunSpecV2, RunSpecV3, RunSpecV4))
        and _spec_snapshot(checked) != snapshot
    ):
        raise ConfigurationError("run specification would change during validation")
    normalization = (
        cast(object, value.normalization) if isinstance(value, RunSpecV4) else None
    )
    if (
        isinstance(checked, RunSpecV4)
        and isinstance(normalization, NormalizationOptions)
        and "probability_sum_tolerance" not in normalization.model_fields_set
        and normalization == NormalizationOptions()
    ):
        # Preserve omission without changing canonical configuration bytes.
        checked.normalization.model_fields_set.discard("probability_sum_tolerance")
    return checked


def validate_run_spec_json(
    text: str, *, _cache_policy_version: CachePolicyVersion | None = None
) -> RunSpecification:
    """Decode strict JSON, then dispatch without resolving filesystem references."""
    return validate_run_spec(
        parse_json(text), _cache_policy_version=_cache_policy_version
    )


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


def load_run_spec(path: str | Path) -> RunSpecification:
    """Load run configuration and resolve references against containing files.

    Referenced criteria/exemplar YAML or JSON documents contain schema_version
    and a criteria/exemplars list respectively. Paths inside the root run file
    always resolve against that file, including output paths and templates.
    """
    return _load_run_spec(path, document_reader=read_document)


def _load_run_spec(
    path: str | Path,
    *,
    document_reader: Callable[[Path], object],
    cache_policy_version: CachePolicyVersion | None = None,
) -> RunSpecification:
    """Resolve a run using a caller-owned, verified document reader.

    The reader receives original resolved paths, allowing source preparation to
    capture referenced documents before parsing without changing resolution or
    assessment validation. The public loader retains its existing behavior.
    """
    source = Path(path).resolve()
    data = _mapping(document_reader(source), source)
    for field in ("input", "pairs", "pair_presentations"):
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
        return validate_run_spec(data, _cache_policy_version=cache_policy_version)
    except ValidationError as exc:
        raise ConfigurationError(f"{source}: {_validation_message(exc)}") from exc
    except ConfigurationError as exc:
        raise ConfigurationError(f"{source}: {exc}") from exc
