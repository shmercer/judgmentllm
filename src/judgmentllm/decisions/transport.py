"""Pure native question rendering, synthetic serialization and exact commitments."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from typing import Literal

from pydantic import Field, field_validator, model_validator

from judgmentllm.config import RunSpecV4, validate_run_spec
from judgmentllm.decisions._base import DecisionsModel
from judgmentllm.decisions.records import (
    DecisionQuestionV1,
    DecisionsLimitsV1,
    PreparedPairPresentationV1,
    QuestionCommitmentV1,
    question_key,
)
from judgmentllm.io import _revalidate_record  # pyright: ignore[reportPrivateUsage]
from judgmentllm.models import ID, PairTask, ResponseItem
from judgmentllm.persistence.hashing import (
    bytes_digest,
    semantic_digest,
)
from judgmentllm.persistence.models import SHA256, IntegrityError
from judgmentllm.persistence.sources import PairStructure
from judgmentllm.planning import AssessmentTask, PlannedRequest, stable_id
from judgmentllm.preparation.session import RequestDependency
from judgmentllm.presentation_blocks import (
    criterion_blocks,
    essay_blocks,
    rubric_blocks,
)
from judgmentllm.prompts import (  # pyright: ignore[reportPrivateUsage]
    _compile_prompt_template,  # pyright: ignore[reportPrivateUsage]
    _exemplar_text,  # pyright: ignore[reportPrivateUsage]
    _namespace,  # pyright: ignore[reportPrivateUsage]
    _protect,  # pyright: ignore[reportPrivateUsage]
    _verify_and_unwrap,  # pyright: ignore[reportPrivateUsage]
)
from judgmentllm.providers.protocol import PreparedRequest
from judgmentllm.transport import PhysicalRequestIdentityV1


class DecisionsContextV1(DecisionsModel):
    protocol: Literal["judgmentllm-decisions-offline-v1"] = (
        "judgmentllm-decisions-offline-v1"
    )
    provider: Literal["mock"] = "mock"
    endpoint: Literal["synthetic"] = "synthetic"
    service_identity: ID
    limits: DecisionsLimitsV1 = Field(default_factory=DecisionsLimitsV1)


class DecisionsDependencyV1(RequestDependency):
    questions: tuple[DecisionQuestionV1, ...]
    shared_input: str = Field(repr=False)
    source_presentation_id: ID | None = None
    presentation_id: ID | None = None

    @model_validator(mode="after")
    def complete_membership(self) -> DecisionsDependencyV1:
        if (
            not self.questions
            or tuple(q.task_id for q in self.questions)
            != tuple(t.task_id for t in self.request.tasks)
            or len({q.key for q in self.questions}) != len(self.questions)
        ):
            raise ValueError("question crosswalk differs from ordered task membership")
        return self


class DecisionsCommitmentV1(DecisionsModel):
    decisions_commitment_version: Literal[1] = 1
    identity: PhysicalRequestIdentityV1
    provider: ID
    endpoint: ID
    requested_model: ID
    configuration_sha256: SHA256
    context_sha256: SHA256
    dependency_sha256: SHA256
    membership_sha256: SHA256
    shared_input_sha256: SHA256
    shared_input_byte_length: int = Field(ge=0)
    questions: tuple[QuestionCommitmentV1, ...]
    source_presentation_id: ID | None
    presentation_id: ID | None
    source_record_id: ID
    prompt_sha256: SHA256
    prompt_byte_length: int = Field(ge=0)
    payload_sha256: SHA256
    payload_byte_length: int = Field(ge=0)
    limits: DecisionsLimitsV1
    context_count: int | None = Field(ge=0)
    parent_commitment_sha256: SHA256 | None = None

    @model_validator(mode="after")
    def exact_members(self) -> DecisionsCommitmentV1:
        tasks = [q.task_id for q in self.questions]
        if (
            not tasks
            or len(set(tasks)) != len(tasks)
            or len({q.key for q in self.questions}) != len(tasks)
            or [q.ordinal for q in self.questions] != list(range(len(tasks)))
            or self.membership_sha256 != semantic_digest("attempt-membership", tasks)
            or (self.source_presentation_id is None) != (self.presentation_id is None)
        ):
            raise ValueError("Decisions commitment membership differs")
        return self

    @property
    def commitment_sha256(self) -> str:
        return semantic_digest("decisions-commitment-v1", self)


class PreparedDecisionsRequestV1(PreparedRequest):
    preparation_version: Literal[8] = 8
    rendered_text: str = Field(repr=False)
    transport_commitment: DecisionsCommitmentV1

    @field_validator("preparation_version", mode="before")
    @classmethod
    def strict_preparation_version(cls, value: object) -> object:
        if type(value) is not int or value != 8:
            raise ValueError("preparation_version must be integer 8")
        return value

    @model_validator(mode="after")
    def exact_wire(self) -> PreparedDecisionsRequestV1:
        receipt = self.transport_commitment
        prompt = self.rendered_text.encode("utf-8")
        if (
            self.request_uid != receipt.identity.request_uid
            or self.task_ids != tuple(q.task_id for q in receipt.questions)
            or self.dependency_sha256 != receipt.dependency_sha256
            or (self.provider, self.endpoint, self.model)
            != (receipt.provider, receipt.endpoint, receipt.requested_model)
            or (self.prompt_sha256, self.payload_sha256)
            != (receipt.prompt_sha256, receipt.payload_sha256)
            or (bytes_digest(prompt), len(prompt), len(self.payload))
            != (
                receipt.prompt_sha256,
                receipt.prompt_byte_length,
                receipt.payload_byte_length,
            )
        ):
            raise ValueError("prepared Decisions wire differs from exact commitment")
        return self

    def content_artifacts(self) -> dict[str, bytes]:
        return {"prompt": self.rendered_text.encode("utf-8"), "payload": self.payload}


def render_question(
    spec: RunSpecV4,
    trait_id: str,
    template: str | None,
    *,
    question_renderer_version: Literal[1, 2] = 2,
) -> str:
    """The shared essay is deliberately absent from the question context."""
    if type(question_renderer_version) is not int or question_renderer_version not in (
        1,
        2,
    ):
        raise ValueError("unsupported Decisions question renderer version")
    if question_renderer_version == 1 and spec.prompt.output_instructions is not None:
        raise ValueError(
            "historical question renderer cannot acquire an instructions override"
        )
    trait = (
        next((t for t in spec.rubric.traits if t.trait_id == trait_id), None)
        if spec.rubric
        else None
    )
    criterion = next((c for c in spec.criteria if c.trait_id == trait_id), None)
    contents = (
        rubric_blocks([trait], spec.rubric)
        if trait is not None
        else criterion_blocks(criterion)
        if criterion is not None
        else []
    )
    selected = spec.prompt.exemplar_ids_by_trait.get(trait_id, ())
    by_id = {e.exemplar_id: e for e in spec.exemplars}
    for identifier in selected:
        example = by_id[identifier]
        # Preserve exact text; only this question's explicitly applicable annotations.
        example = example.model_copy(
            update={
                "scores": {k: v for k, v in example.scores.items() if k == trait_id},
                "rationales": {
                    k: v for k, v in example.rationales.items() if k == trait_id
                },
            }
        )
        contents.append(("exemplar", identifier, _exemplar_text(example)))
    instructions = spec.prompt.output_instructions
    if question_renderer_version == 1:
        instructions = (
            "Evaluate the shared essay independently against every ordered level of this rubric. Do not generate an explanation."
            if trait is not None
            else "Select exactly essay_01 or essay_02 from the shared presented pair against this criterion. Ties and generated explanations are not permitted."
        )
    elif instructions is None:
        instructions = (
            "Score the shared essay on this trait using the rubric’s ordered levels and their descriptions. Base the assessment on evidence in the essay. Do not generate an explanation."
            if trait is not None
            else "Compare the two essays using this criterion. Choose the essay that is stronger on this criterion: essay_01 or essay_02. Do not choose a tie or include an explanation."
        )
    contents.append(("output_instructions", "native", instructions))
    source = (
        template
        if template is not None
        else "{{ rubric }}\n\n{{ exemplars }}\n\n{{ output_instructions }}"
    )
    namespace = _namespace([c[2] for c in contents], source)
    blocks = _protect(contents, namespace)
    rubric = "\n\n".join(b.wrapped for b in blocks if b.kind in {"trait", "criterion"})
    context: dict[str, object] = {
        "rubric": rubric,
        "trait": rubric,
        "criterion": rubric,
        "exemplars": "\n\n".join(b.wrapped for b in blocks if b.kind == "exemplar"),
        "exemplar_items": [
            {"exemplar_id": b.identifier, "block": b.wrapped}
            for b in blocks
            if b.kind == "exemplar"
        ],
        "traits": [{"trait_id": trait_id, "block": rubric}],
        "output_instructions": blocks[-1].wrapped,
    }
    chunks: list[str] = []
    size = 0
    for chunk in _compile_prompt_template(
        source, context, advanced=spec.prompt.advanced, limits=spec.prompt.limits
    ).generate(context):
        size += len(chunk.encode("utf-8"))
        if size > spec.prompt.limits.max_output_bytes:
            raise ValueError("question exceeds rendering safeguard")
        chunks.append(chunk)
    return _verify_and_unwrap(
        "".join(chunks), blocks, namespace, renderer_version=2, presentation_version=2
    )


def serialize_request(
    shared: str,
    questions: Sequence[DecisionQuestionV1],
    spec: RunSpecV4,
    context: DecisionsContextV1,
) -> bytes:
    """B's own synthetic protocol, never vendor wire JSON or a schema projection."""
    from judgmentllm.providers.decisions_wire import serialize_synthetic_decisions

    return serialize_synthetic_decisions(shared, questions, spec, context)


def fits(
    payload: bytes, count: int, context: DecisionsContextV1, *, strict: bool
) -> bool:
    limits = context.limits
    if limits.max_context_tokens is not None and limits.counter is None:
        raise ValueError("known context ceiling requires a trustworthy local counter")
    if strict and limits.max_context_tokens is None:
        raise ValueError(
            "Decisions context ceiling is unknown under strict capabilities"
        )
    return (
        (limits.max_questions is None or count <= limits.max_questions)
        and len(payload)
        <= min(
            limits.local_body_bytes, limits.max_body_bytes or limits.local_body_bytes
        )
        and (
            limits.max_context_tokens is None
            or len(payload) <= limits.max_context_tokens
        )
    )


def plan_source(
    spec: RunSpecV4,
    record: ResponseItem | PairTask | PreparedPairPresentationV1,
    lookup: Callable[[str], ResponseItem],
    context: DecisionsContextV1,
    template: str | None,
    *,
    question_renderer_version: Literal[1, 2] = 2,
) -> Iterator[DecisionsDependencyV1]:
    """One source occurrence at a time, with only one user-bounded prefix buffered."""
    checked = validate_run_spec(spec)
    assert isinstance(checked, RunSpecV4)
    spec = checked
    context = _revalidate_record(context)
    if (spec.provider, spec.endpoint) != (context.provider, context.endpoint):
        raise ValueError("unimplemented Decisions serialization scope")
    if spec.execution.tier is not None:
        raise ValueError("synthetic Decisions has no service tier")
    if isinstance(record, ResponseItem):
        assert spec.rubric is not None
        shared = record.text
        source_id = record.item_id
        pairs: tuple[PairTask, ...] = ()
        trait_ids = tuple(t.trait_id for t in spec.rubric.traits)
        presentation_id = source_presentation_id = None
    else:
        pairs = (
            tuple(PairTask.model_validate(p.model_dump()) for p in record.members)
            if isinstance(record, PreparedPairPresentationV1)
            else (record,)
        )
        pair = pairs[0]
        left, right = lookup(pair.left_item_id), lookup(pair.right_item_id)
        shared = "\n\n".join(c[2] for c in essay_blocks(pair, left, right))
        trait_ids = tuple(p.trait_id for p in pairs)
        source_id = (
            record.source_presentation_id
            if isinstance(record, PreparedPairPresentationV1)
            else record.pair_uid
        )
        presentation_id = (
            record.presentation_id
            if isinstance(record, PreparedPairPresentationV1)
            else None
        )
        source_presentation_id = (
            record.source_presentation_id
            if isinstance(record, PreparedPairPresentationV1)
            else None
        )
    source_hash = semantic_digest("decisions-source-v1", record)
    input_hash = bytes_digest(shared.encode("utf-8"))
    maximum = spec.execution.request_grouping.max_questions_per_request
    if context.limits.max_questions is not None:
        maximum = min(maximum, context.limits.max_questions)
    # Historical criterion rows remain singleton, even under auto.
    if isinstance(record, PairTask):
        maximum = 1
    buffer: list[DecisionQuestionV1] = []

    def dependency(questions: list[DecisionQuestionV1]) -> DecisionsDependencyV1:
        tasks = tuple(
            AssessmentTask(
                task_id=q.task_id,
                trait_id=q.trait_id,
                item_id=record.item_id if isinstance(record, ResponseItem) else None,
                pair_uid=q.pair.pair_uid if q.pair else None,
                pair_id=q.pair.pair_id if q.pair else None,
            )
            for q in questions
        )
        uid = stable_id(
            "decisions-request-v1",
            semantic_digest("run-configuration", checked),
            source_id,
            presentation_id,
            source_hash,
            input_hash,
            [q.task_id for q in questions],
        )
        request = PlannedRequest(
            request_uid=uid,
            tasks=tasks,
            trait_ids=tuple(q.trait_id for q in questions),
            exemplar_ids=tuple(
                dict.fromkeys(eid for q in questions for eid in q.exemplar_ids)
            ),
            response=record if isinstance(record, ResponseItem) else None,
            pair=questions[0].pair,
        )
        return DecisionsDependencyV1(
            request=request,
            dependency_sha256=semantic_digest(
                "decisions-dependencies-v1",
                [
                    uid,
                    source_hash,
                    input_hash,
                    [semantic_digest("native-question-v1", q) for q in questions],
                ],
            ),
            response_sha256=source_hash if isinstance(record, ResponseItem) else None,
            left_sha256=semantic_digest("input-record", lookup(pairs[0].left_item_id))
            if pairs
            else None,
            right_sha256=semantic_digest("input-record", lookup(pairs[0].right_item_id))
            if pairs
            else None,
            questions=tuple(questions),
            shared_input=shared,
            source_presentation_id=source_presentation_id,
            presentation_id=presentation_id,
        )

    for index, trait_id in enumerate(trait_ids):
        pair = pairs[index] if pairs else None
        task_id = (
            stable_id("task", "pairwise", pair.pair_uid, trait_id)
            if pair
            else stable_id("task", "rubric", source_id, trait_id)
        )
        q = DecisionQuestionV1(
            task_id=task_id,
            key=question_key(task_id),
            trait_id=trait_id,
            kind="choice" if pair else "score",
            instructions=render_question(
                spec,
                trait_id,
                template,
                question_renderer_version=question_renderer_version,
            ),
            trait=next((t for t in spec.rubric.traits if t.trait_id == trait_id), None)
            if spec.rubric
            else None,
            criterion=next((c for c in spec.criteria if c.trait_id == trait_id), None),
            pair=pair,
            exemplar_ids=spec.prompt.exemplar_ids_by_trait.get(trait_id, ()),
        )
        candidate = [*buffer, q]
        if len(candidate) > maximum or not fits(
            serialize_request(shared, candidate, spec, context),
            len(candidate),
            context,
            strict=spec.strict_capabilities,
        ):
            if buffer:
                yield dependency(buffer)
                buffer = []
            if not fits(
                serialize_request(shared, [q], spec, context),
                1,
                context,
                strict=spec.strict_capabilities,
            ):
                raise ValueError(
                    f"oversized Decisions member {q.task_id}: body/context ceiling"
                )
        buffer.append(q)
    yield dependency(buffer)


def prepare_request(
    spec: RunSpecV4,
    dependency: DecisionsDependencyV1,
    identity: PhysicalRequestIdentityV1,
    context: DecisionsContextV1,
    *,
    parent: DecisionsCommitmentV1 | None = None,
) -> PreparedDecisionsRequestV1:
    dependency = _revalidate_record(dependency)
    payload = serialize_request(
        dependency.shared_input, dependency.questions, spec, context
    )
    if not fits(
        payload, len(dependency.questions), context, strict=spec.strict_capabilities
    ):
        raise ValueError(
            "sealed Decisions request no longer fits; repacking is forbidden"
        )
    text = (
        dependency.shared_input
        + "\n\n"
        + "\n\n".join(q.instructions for q in dependency.questions)
    )
    prompt = text.encode("utf-8")
    commitments = tuple(
        QuestionCommitmentV1(
            task_id=q.task_id,
            trait_id=q.trait_id,
            key=q.key,
            ordinal=i,
            kind=q.kind,
            question_sha256=bytes_digest(q.instructions.encode("utf-8")),
            question_byte_length=len(q.instructions.encode("utf-8")),
            semantics_sha256=semantic_digest("native-question-v1", q),
            exemplar_ids=q.exemplar_ids,
            scale_mapping=tuple(q.trait.resolved_levels) if q.trait else (),
            pair=PairStructure.model_validate(q.pair.model_dump(exclude={"metadata"}))
            if q.pair
            else None,
        )
        for i, q in enumerate(dependency.questions)
    )
    receipt = DecisionsCommitmentV1(
        identity=identity,
        provider=spec.provider,
        endpoint=spec.endpoint,
        requested_model=spec.model,
        configuration_sha256=semantic_digest("run-configuration", spec),
        context_sha256=semantic_digest("decisions-context-v1", context),
        dependency_sha256=dependency.dependency_sha256,
        membership_sha256=semantic_digest(
            "attempt-membership", [q.task_id for q in dependency.questions]
        ),
        shared_input_sha256=bytes_digest(dependency.shared_input.encode("utf-8")),
        shared_input_byte_length=len(dependency.shared_input.encode("utf-8")),
        questions=commitments,
        source_presentation_id=dependency.source_presentation_id,
        presentation_id=dependency.presentation_id,
        source_record_id=parent.source_record_id
        if parent
        else dependency.source_presentation_id
        or (
            dependency.request.response.item_id
            if dependency.request.response
            else dependency.request.pair.pair_uid
            if dependency.request.pair
            else ""
        ),
        prompt_sha256=bytes_digest(prompt),
        prompt_byte_length=len(prompt),
        payload_sha256=bytes_digest(payload),
        payload_byte_length=len(payload),
        limits=context.limits,
        context_count=len(payload) if context.limits.counter else None,
        parent_commitment_sha256=parent.commitment_sha256 if parent else None,
    )
    if identity.request_uid != dependency.request.request_uid:
        raise IntegrityError("Decisions identity differs from parent request")
    return PreparedDecisionsRequestV1(
        request_uid=identity.request_uid,
        task_ids=tuple(q.task_id for q in dependency.questions),
        dependency_sha256=dependency.dependency_sha256,
        prompt_sha256=receipt.prompt_sha256,
        payload_sha256=receipt.payload_sha256,
        provider=spec.provider,
        endpoint=spec.endpoint,
        model=spec.model,
        payload=payload,
        rendered_text=text,
        transport_commitment=receipt,
    )
