"""Deterministic structural plans. No requests are sent or tokens estimated."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Iterator
from typing import Literal

from pydantic import Field

from judgmentllm.config import RunSpec
from judgmentllm.generation import GenerationCapabilities
from judgmentllm.io import _revalidate_record, read_pairs, read_responses  # pyright: ignore[reportPrivateUsage]
from judgmentllm.models import PairTask, ResponseItem, StrictModel, VersionedModel


def stable_id(kind: str, *parts: object) -> str:
    """Hash versioned canonical JSON; IDs do not depend on Python hash state."""
    payload = json.dumps(
        [1, kind, *parts],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return f"{kind}-{hashlib.sha256(payload).hexdigest()}"


class CapabilityProfile(StrictModel):
    """Caller-supplied facts, with None meaning unknown rather than supported."""

    explanations: bool | None = None
    grouped: bool | None = None
    ties: bool | None = None
    modes: tuple[Literal["live", "bulk_live", "batch_native"], ...] | None = None
    max_levels: int | None = Field(default=None, ge=2)
    max_traits_per_request: int | None = Field(default=None, ge=1)
    generation: GenerationCapabilities | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


class ValidationIssue(StrictModel):
    """An actionable structural preflight diagnostic."""

    code: str
    field: str
    message: str
    severity: Literal["warning", "error"]


class ValidationReport(StrictModel):
    """Local errors and explicit capability unknowns."""

    issues: tuple[ValidationIssue, ...] = ()

    @property
    def valid(self) -> bool:
        return not any(issue.severity == "error" for issue in self.issues)


class PreflightError(ValueError):
    """A requested structural plan cannot satisfy its declared constraints."""


def validate_generation(
    spec: RunSpec, *, capabilities: GenerationCapabilities | None = None
) -> ValidationReport:
    """Check explicit controls only; this is not complete submission preflight.

    Call again with current applicable facts before submission or resumption.
    A previously recorded capability snapshot is audit evidence, not permission
    to bypass newly known restrictions or source-integrity verification.
    """
    if spec.generation is None:
        return ValidationReport()
    spec = _revalidate_record(spec)
    assert spec.generation is not None
    if capabilities is not None:
        capabilities = _revalidate_record(capabilities)
        if (capabilities.provider, capabilities.endpoint, capabilities.model) != (
            spec.provider,
            spec.endpoint,
            spec.model,
        ):
            return ValidationReport(
                issues=(
                    ValidationIssue(
                        code="capability_scope_mismatch",
                        field="generation",
                        message="Generation capability facts must match the exact provider, endpoint, and model",
                        severity="error",
                    ),
                )
            )
    issues: list[ValidationIssue] = []
    requested = spec.generation.model_dump()
    for name in ("temperature", "top_p", "max_output_tokens"):
        value = getattr(spec.generation, name)
        if value is None:
            continue
        rule = getattr(capabilities, name) if capabilities is not None else None
        support = rule.supported if rule is not None else None
        if support is not True:
            unknown = support is None
            issues.append(
                ValidationIssue(
                    code="unknown_capability" if unknown else "unsupported",
                    field=f"generation.{name}",
                    message=(
                        f"generation.{name} support is unknown; no provider qualification was inferred"
                        if unknown
                        else f"generation.{name} is unsupported for this provider, endpoint, and model"
                    ),
                    severity=(
                        "warning"
                        if unknown and not spec.strict_capabilities
                        else "error"
                    ),
                )
            )
        if rule is not None and (
            (rule.minimum is not None and value < rule.minimum)
            or (rule.maximum is not None and value > rule.maximum)
        ):
            bounds = ", ".join(
                f"{bound}={limit}"
                for bound, limit in (
                    ("minimum", rule.minimum),
                    ("maximum", rule.maximum),
                )
                if limit is not None
            )
            issues.append(
                ValidationIssue(
                    code="generation_out_of_range",
                    field=f"generation.{name}",
                    message=f"generation.{name} is outside the known inclusive capability bounds ({bounds}); revise the requested value explicitly",
                    severity="error",
                )
            )
    if capabilities is not None:
        for combination in capabilities.incompatible_combinations:
            if all(name in requested for name in combination):
                issues.append(
                    ValidationIssue(
                        code="incompatible_generation",
                        field="generation",
                        message=f"These generation controls cannot be requested together: {', '.join(combination)}",
                        severity="error",
                    )
                )
    return ValidationReport(issues=tuple(issues))


class RunPlan(VersionedModel):
    """Small reusable grouping plan, independent of response count."""

    task_kind: Literal["rubric", "pairwise"]
    trait_groups: tuple[tuple[str, ...], ...]
    exemplar_ids: tuple[str, ...]
    report: ValidationReport


class AssessmentTask(VersionedModel):
    """One desired output unit, not an execution attempt."""

    task_id: str
    trait_id: str
    item_id: str | None = None
    pair_uid: str | None = None
    pair_id: str | None = None


class PlannedRequest(VersionedModel):
    """Ordered logical membership and input context; never provider wire JSON."""

    request_uid: str
    tasks: tuple[AssessmentTask, ...]
    trait_ids: tuple[str, ...]
    exemplar_ids: tuple[str, ...]
    response: ResponseItem | None = None
    pair: PairTask | None = None


def _groups(
    spec: RunSpec, capabilities: CapabilityProfile
) -> tuple[tuple[str, ...], ...]:
    if spec.task_kind == "pairwise":
        return tuple((criterion.trait_id,) for criterion in spec.criteria)
    assert spec.rubric is not None
    trait_ids = tuple(trait.trait_id for trait in spec.rubric.traits)
    execution = spec.execution
    limits = [
        value
        for value in (
            execution.max_traits_per_request,
            capabilities.max_traits_per_request,
        )
        if value is not None
    ]
    limit = min(limits) if limits else None
    if execution.grouping == "independent":
        return tuple((trait,) for trait in trait_ids)
    if execution.grouping == "auto":
        if limit is None:
            raise PreflightError(
                "execution.grouping: auto requires a known max_traits_per_request"
            )
        return tuple(
            trait_ids[start : start + limit]
            for start in range(0, len(trait_ids), limit)
        )
    groups = (
        tuple(tuple(group) for group in execution.groups)
        if execution.groups is not None
        else (trait_ids,)
    )
    flattened = [trait for group in groups for trait in group]
    if (
        not groups
        or any(not group for group in groups)
        or len(flattened) != len(set(flattened))
        or set(flattened) != set(trait_ids)
    ):
        raise PreflightError(
            "execution.groups must partition all trait IDs exactly once, without empty groups"
        )
    if limit is not None and any(len(group) > limit for group in groups):
        raise PreflightError(
            f"execution.groups exceed known max_traits_per_request={limit}; fixed groups cannot be split"
        )
    return groups


def validate_run(
    spec: RunSpec, *, capabilities: CapabilityProfile | None = None
) -> ValidationReport:
    """Validate known compatibility, preserving unknowns as explicit diagnostics."""
    profile = capabilities or CapabilityProfile()
    issues: list[ValidationIssue] = []

    def issue(code: str, field: str, message: str, *, unknown: bool = False) -> None:
        severity: Literal["warning", "error"] = (
            "warning" if unknown and not spec.strict_capabilities else "error"
        )
        issues.append(
            ValidationIssue(code=code, field=field, message=message, severity=severity)
        )

    def required(name: str, support: bool | None) -> None:
        if support is False:
            issue(
                "unsupported",
                name,
                f"{name} is not supported by the supplied capability profile",
            )
        elif support is None:
            issue(
                "unknown_capability",
                name,
                f"{name} support is unknown; no provider capability was inferred",
                unknown=True,
            )

    if profile.modes is None:
        issue(
            "unknown_capability",
            "execution.mode",
            "Execution mode support is unknown; this is an offline structural plan",
            unknown=True,
        )
    elif spec.execution.mode not in profile.modes:
        issue(
            "unsupported",
            "execution.mode",
            f"mode {spec.execution.mode!r} is not supported",
        )
    if spec.execution.explanations:
        score_only = (spec.provider == "openai" and spec.endpoint == "decisions") or (
            spec.provider == "typesafe" and spec.endpoint in {"jev", "systemone"}
        )
        if score_only:
            issue(
                "score_only",
                "execution.explanations",
                f"{spec.provider}/{spec.endpoint} is score-only; disable explanations or explicitly choose a compatible endpoint",
            )
        else:
            required("execution.explanations", profile.explanations)
    if spec.allow_ties:
        required("allow_ties", profile.ties)
    try:
        groups = _groups(spec, profile)
        if any(len(group) > 1 for group in groups):
            required("execution.grouping", profile.grouped)
    except PreflightError as error:
        issue("invalid_grouping", "execution.grouping", str(error))
    if spec.rubric is not None:
        for trait in spec.rubric.traits:
            # Verified 2026-11-02: the Decisions score contract is 2–10 levels.
            # https://developers.openai.com/api/docs/guides/decisions#score-against-a-rubric
            # This endpoint bound cannot be widened by caller-supplied capabilities.
            if (
                spec.provider == "openai"
                and spec.endpoint == "decisions"
                and not 2 <= len(trait.levels) <= 10
            ):
                issue(
                    "incompatible_levels",
                    f"rubric.{trait.trait_id}.levels",
                    f"openai/decisions requires 2–10 ordered score levels; "
                    f"received {len(trait.levels)}. Choose a compatible endpoint or "
                    "revise the rubric explicitly; categories cannot be collapsed",
                )
            elif (
                profile.max_levels is not None
                and len(trait.levels) > profile.max_levels
            ):
                issue(
                    "too_many_levels",
                    f"rubric.{trait.trait_id}.levels",
                    f"{len(trait.levels)} levels exceed known limit {profile.max_levels}; categories cannot be collapsed",
                )
            if trait.coding_kind == "indices":
                for index, level in enumerate(trait.levels):
                    try:
                        value = float(level.label)
                    except ValueError:
                        continue
                    if value != index:
                        issues.append(
                            ValidationIssue(
                                code="mixed_label_code",
                                field=f"rubric.{trait.trait_id}.levels",
                                message=f"Label {level.label!r} receives index code {index}; supply an explicit mapping if unintended",
                                severity="warning",
                            )
                        )
    issues.extend(validate_generation(spec, capabilities=profile.generation).issues)
    return ValidationReport(issues=tuple(issues))


def plan_run(
    spec: RunSpec, *, capabilities: CapabilityProfile | None = None
) -> RunPlan:
    """Return structural trait groups without reading/materializing response files."""
    report = validate_run(spec, capabilities=capabilities)
    if not report.valid:
        raise PreflightError(
            "; ".join(
                issue.message for issue in report.issues if issue.severity == "error"
            )
        )
    return RunPlan(
        task_kind=spec.task_kind,
        trait_groups=_groups(spec, capabilities or CapabilityProfile()),
        exemplar_ids=tuple(exemplar.exemplar_id for exemplar in spec.exemplars),
        report=report,
    )


def iter_tasks(
    spec: RunSpec, records: Iterable[ResponseItem | PairTask] | None = None
) -> Iterator[AssessmentTask]:
    """Stream logical tasks; retain IDs only for duplicate detection."""
    if records is None:
        if spec.task_kind == "rubric":
            records = read_responses(spec.input)
        else:
            assert spec.pairs is not None
            records = read_pairs(spec.pairs)
    seen: set[str] = set()
    for record in records:
        if spec.task_kind == "rubric" and isinstance(record, ResponseItem):
            if record.item_id in seen:
                raise ValueError(f"Duplicate response item_id {record.item_id!r}")
            seen.add(record.item_id)
            assert spec.rubric is not None
            for trait in spec.rubric.traits:
                yield AssessmentTask(
                    task_id=stable_id("task", "rubric", record.item_id, trait.trait_id),
                    item_id=record.item_id,
                    trait_id=trait.trait_id,
                )
        elif spec.task_kind == "pairwise" and isinstance(record, PairTask):
            if record.pair_uid in seen:
                raise ValueError(f"Duplicate pair_uid {record.pair_uid!r}")
            seen.add(record.pair_uid)
            if record.trait_id not in {
                criterion.trait_id for criterion in spec.criteria
            }:
                raise ValueError(f"Unknown pair criterion {record.trait_id!r}")
            yield AssessmentTask(
                task_id=stable_id("task", "pairwise", record.pair_uid, record.trait_id),
                trait_id=record.trait_id,
                pair_uid=record.pair_uid,
                pair_id=record.pair_id,
            )
        else:
            raise ValueError(
                f"Expected {spec.task_kind} input record, received {type(record).__name__}"
            )


def iter_requests(
    spec: RunSpec,
    records: Iterable[ResponseItem | PairTask] | None = None,
    *,
    capabilities: CapabilityProfile | None = None,
) -> Iterator[PlannedRequest]:
    """Stream request groups without execution, billing, or provider formatting."""
    plan = plan_run(spec, capabilities=capabilities)
    if records is None:
        if spec.task_kind == "rubric":
            records = read_responses(spec.input)
        else:
            assert spec.pairs is not None
            records = read_pairs(spec.pairs)
    template_text = (
        spec.prompt.file.read_text(encoding="utf-8") if spec.prompt.file else None
    )
    context_parts: list[object] = [
        spec.rubric.model_dump(mode="json")
        if spec.rubric
        else [c.model_dump(mode="json") for c in spec.criteria],
        [e.model_dump(mode="json") for e in spec.exemplars],
        spec.prompt.preset,
        template_text,
        spec.prompt.advanced,
        spec.execution.explanations,
        spec.allow_ties,
        spec.provider,
        spec.endpoint,
        spec.model,
        spec.execution.tier,
        spec.execution.mode,
    ]
    if spec.generation is not None:
        # Leave every legacy context argument unchanged for unspecified controls.
        context_parts.append({"generation": spec.generation.model_dump(mode="json")})
    fingerprint = stable_id("context", *context_parts)
    seen: set[str] = set()
    for record in records:
        record_id = (
            record.item_id if isinstance(record, ResponseItem) else record.pair_uid
        )
        if record_id in seen:
            raise ValueError(f"Duplicate input ID {record_id!r}")
        seen.add(record_id)
        tasks = tuple(iter_tasks(spec, (record,)))
        by_trait = {task.trait_id: task for task in tasks}
        groups = (
            plan.trait_groups
            if isinstance(record, ResponseItem)
            else ((record.trait_id,),)
        )
        for group in groups:
            members = tuple(by_trait[trait] for trait in group)
            request_uid = stable_id(
                "request",
                [task.task_id for task in members],
                fingerprint,
                record.model_dump(mode="json"),
            )
            yield PlannedRequest(
                request_uid=request_uid,
                tasks=members,
                trait_ids=group,
                exemplar_ids=plan.exemplar_ids,
                response=record if isinstance(record, ResponseItem) else None,
                pair=record if isinstance(record, PairTask) else None,
            )


def prepare_pairs(
    pairs: Iterable[PairTask],
    *,
    repetitions: int = 1,
    include_reversals: bool = False,
    seed: int | None = None,
) -> Iterator[PairTask]:
    """Expand pairs in input/repetition order using portable seeded presentation."""
    if type(repetitions) is not int or repetitions < 1:
        raise ValueError("repetitions must be a positive integer")
    if seed is not None and (type(seed) is not int):
        raise ValueError("seed must be an integer or None")
    if type(include_reversals) is not bool:
        raise ValueError("include_reversals must be boolean")
    seen: set[str] = set()
    for pair in pairs:
        if (
            pair.is_reversal
            or pair.repetition_index != 0
            or pair.source_pair_uid is not None
            or pair.reversal_group is not None
        ):
            raise ValueError(
                "prepare_pairs requires unscheduled source occurrences; existing repetition/reversal metadata cannot be overwritten"
            )
        if pair.pair_uid in seen:
            raise ValueError(f"Duplicate source pair_uid {pair.pair_uid!r}")
        seen.add(pair.pair_uid)
        for repetition in range(repetitions):
            order = pair.presented_order
            if seed is not None:
                decision = stable_id(
                    "orientation", seed, pair.pair_uid, pair.pair_id, repetition
                )
                order = (
                    pair.original_order
                    if int(decision[-1], 16) % 2 == 0
                    else list(reversed(pair.original_order))
                )
            reversal_group = stable_id("reversal", pair.pair_uid, repetition)
            for reversed_occurrence in (False, True) if include_reversals else (False,):
                data = pair.model_dump()
                data.update(
                    pair_uid=stable_id(
                        "pair",
                        pair.pair_uid,
                        pair.pair_id,
                        pair.left_item_id,
                        pair.right_item_id,
                        pair.trait_id,
                        repetition,
                        reversed_occurrence,
                    ),
                    source_pair_uid=pair.pair_uid,
                    repetition_index=repetition,
                    is_reversal=reversed_occurrence,
                    reversal_group=reversal_group,
                    presented_order=list(reversed(order))
                    if reversed_occurrence
                    else order,
                )
                yield PairTask.model_validate(data)
