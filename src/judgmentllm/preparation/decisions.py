"""Indexed V4 preparation using existing source capture, ownership and journals."""

from __future__ import annotations

import csv
import hashlib
import io
import json
from collections.abc import Iterator
from contextlib import ExitStack
from pathlib import Path
from typing import BinaryIO, cast

from pydantic import ValidationError

from judgmentllm import __version__
from judgmentllm.config import (
    ConfigurationError,
    InputSpec,
    RunSpecV4,
    _load_run_spec,  # pyright: ignore[reportPrivateUsage]
    parse_document,
    parse_json,
    validate_run_spec,
)
from judgmentllm.decisions.records import (
    GEMINI_DECISIONS_ROUTE,
    TYPESAFE_ROUTE,
    GeminiDecisionsContextV1,
    PairPresentationInputV1,
    PreparedPairPresentationV1,
    TypeSafeDecisionsContextV1,
    project_presentation,
)
from judgmentllm.decisions.transport import (
    DecisionsCommitmentV1,
    DecisionsContext,
    DecisionsContextV1,
    DecisionsDependencyV1,
    OpenAIDecisionsContextV1,
    PreparedDecisionsRequestV1,
    plan_source,
    prepare_request,
    render_question,
)
from judgmentllm.io import _revalidate_record  # pyright: ignore[reportPrivateUsage]
from judgmentllm.models import PairTask, ResponseItem
from judgmentllm.persistence.artifacts import ArtifactStore
from judgmentllm.persistence.generation import build_generation_commitment
from judgmentllm.persistence.hashing import (
    bytes_digest,
    canonical_bytes,
    semantic_digest,
)
from judgmentllm.persistence.models import (
    ArtifactIdentity,
    AttemptRecord,
    ManifestReference,
    NotFoundError,
    RequestRecord,
    RunRecord,
    TaskRecord,
)
from judgmentllm.persistence.ownership import ExecutorOwnership
from judgmentllm.persistence.presentations import (
    PresentationInputIndexV1,
    add_presentation,
    get_presentation,
    iter_presentations,
    verify_members,
)
from judgmentllm.persistence.repository import Repository
from judgmentllm.persistence.transport_receipts import (
    publish_transport_commitment,
    read_transport_commitment,
)
from judgmentllm.planning import CapabilityProfile, bulk_execution_issue, stable_id
from judgmentllm.preparation.capture import SourceBuilder
from judgmentllm.preparation.freezing import (
    FileSignature,
    PreparationError,
    require_signature,
    verify_original,
)
from judgmentllm.preparation.index import ParsingContext, _BinaryLines, read_record  # pyright: ignore[reportPrivateUsage]
from judgmentllm.preparation.manifest import (
    CategoryCommitment,
    ExecutableRunManifestV8,
    ExecutableRunManifestV9,
    ExecutableRunManifestV10,
    ExecutableRunManifestV11,
    ExemplarCommitment,
    SourceFormat,
    TemplateCommitment,
    TraitCommitment,
    assessment_digest,
    configuration_digest,
)
from judgmentllm.preparation.native import InitialTransportDigest
from judgmentllm.preparation.session import PreparedRun, RequestDependency
from judgmentllm.transport import PhysicalRequestIdentityV1, physical_identity


def presentation_records(
    stream: BinaryIO, spec: InputSpec
) -> Iterator[tuple[PairPresentationInputV1, int, int, ParsingContext]]:
    lines = _BinaryLines(stream)
    if spec.format == "jsonl":
        context = ParsingContext(kind="pair", format="jsonl")
        while True:
            offset = lines.position
            text = next(lines, None)
            if text is None:
                return
            parse_json(text)
            yield (
                PairPresentationInputV1.model_validate_json(text),
                offset,
                lines.position - offset,
                context,
            )
    else:
        reader = csv.reader(lines, strict=True)
        header = next(reader, None)
        required = {
            "source_presentation_id",
            "pair_id",
            "left_item_id",
            "right_item_id",
            "members",
        }
        if (
            header is None
            or len(set(header)) != len(header)
            or not required <= set(header)
            or set(header) - set(PairPresentationInputV1.model_fields)
        ):
            raise PreparationError("invalid presentation CSV header")
        context = ParsingContext(
            kind="pair",
            format="csv",
            csv_header=tuple(header),
            header_byte_length=lines.position,
            header_sha256=lines.digest.hexdigest(),
        )
        while True:
            offset = lines.position
            row = next(reader, None)
            if row is None:
                return
            if len(row) != len(header):
                raise PreparationError("presentation CSV column count differs")
            data: dict[str, object] = dict(zip(header, row, strict=True))
            for key in (
                "schema_version",
                "pair_presentation_version",
                "schedule",
                "members",
            ):
                if key in data:
                    data[key] = parse_json(cast(str, data[key]))
            yield (
                PairPresentationInputV1.model_validate_json(json.dumps(data)),
                offset,
                lines.position - offset,
                context,
            )


def presentation_index_digest(repository: Repository, run_id: str) -> tuple[str, int]:
    digest = hashlib.sha256(b"judgmentllm.presentation-index.v1\n")
    count = 0
    for index in iter_presentations(repository, run_id):
        source = PairPresentationInputV1.model_validate_json(index.structure_json)
        verify_members(repository, index, source)
        digest.update(canonical_bytes(index) + b"\n")
        count += 1
    return digest.hexdigest(), count


def index_presentations(builder: SourceBuilder, spec: RunSpecV4) -> None:
    reference = spec.pair_presentations
    assert reference is not None
    frozen = builder.freeze(
        reference.path, "pair_presentations", cast("SourceFormat", reference.format)
    )
    builder.register(frozen)
    source = frozen.commitment
    path = frozen.stage.verify() if frozen.stage else Path(source.original_location)
    context: ParsingContext | None = None
    criteria = {c.trait_id for c in spec.criteria}
    batch: list[tuple[PresentationInputIndexV1, PairPresentationInputV1]] = []

    def commit_batch() -> None:
        with builder.repository.execution_transaction(
            builder.run_id, builder.owner_token
        ):
            for index, record in batch:
                add_presentation(builder.repository, index, record, builder.owner_token)
        batch.clear()

    with path.open("rb") as stream:
        for ordinal, (record, offset, length, current_context) in enumerate(
            presentation_records(stream, reference)
        ):
            context = current_context
            if (
                any(m.trait_id not in criteria for m in record.members)
                or spec.seed is not None
                and spec.seed != record.schedule.seed
            ):
                raise PreparationError(
                    "presentation criterion or run/source seed conflicts"
                )
            index = PresentationInputIndexV1(
                run_id=builder.run_id,
                source_presentation_id=record.source_presentation_id,
                source_id=source.source_id,
                ordinal=ordinal,
                byte_offset=offset,
                byte_length=length,
                record_sha256=semantic_digest("presentation-input-v1", record),
                structure_json=canonical_bytes(record).decode("utf-8"),
            )
            batch.append((index, record))
            if len(batch) == 128:
                commit_batch()
            builder._checkpoint("record_indexed", source.source_id)  # pyright: ignore[reportPrivateUsage]
        if batch:
            commit_batch()
    verify_original(
        path, source.generation, source.raw_sha256, source.byte_length
    ) if frozen.stage is None else frozen.stage.verify()
    digest, count = presentation_index_digest(builder.repository, builder.run_id)
    frozen.commitment = source.model_copy(
        update={"parsing": context, "record_count": count, "index_sha256": digest}
    )
    builder._checkpoint("presentation_index_verified", source.source_id)  # pyright: ignore[reportPrivateUsage]


def publish_decisions_request(
    repository: Repository,
    store: ArtifactStore,
    wire: PreparedDecisionsRequestV1,
    capture: str,
) -> tuple[ArtifactIdentity, ...]:
    artifacts = tuple(
        store.put(content, "text/plain" if name == "prompt" else "application/json")
        if capture == "full"
        else store.hash_only(
            bytes_digest(content),
            len(content),
            "text/plain" if name == "prompt" else "application/json",
        )
        for name, content in wire.content_artifacts().items()
    )
    return (*artifacts, publish_transport_commitment(store, wire.transport_commitment))


def assessment_commitments(
    spec: RunSpecV4,
) -> tuple[tuple[TraitCommitment, ...], tuple[ExemplarCommitment, ...]]:
    traits = (
        tuple(
            TraitCommitment(
                trait_id=trait.trait_id,
                sha256=semantic_digest("trait", trait),
                coding_kind=trait.coding_kind,
                categories=tuple(
                    CategoryCommitment(
                        label_sha256=semantic_digest("category-label", level.label),
                        numeric_value=level.numeric_value,
                    )
                    for level in trait.resolved_levels
                ),
            )
            for trait in spec.rubric.traits
        )
        if spec.rubric
        else tuple(
            TraitCommitment(trait_id=c.trait_id, sha256=semantic_digest("criterion", c))
            for c in spec.criteria
        )
    )
    return traits, tuple(
        ExemplarCommitment(
            exemplar_id=e.exemplar_id, sha256=semantic_digest("exemplar", e)
        )
        for e in spec.exemplars
    )


def _verify_bulk_policy(spec: RunSpecV4) -> None:
    if (issue := bulk_execution_issue(spec)) is not None:
        raise PreparationError(issue.message)


class DecisionsPreparedRun(PreparedRun):
    """Native-specific reconstruction; legacy source readers remain unchanged."""

    def context(self) -> DecisionsContext:
        assert isinstance(self.manifest, ExecutableRunManifestV8)
        if isinstance(self.manifest, ExecutableRunManifestV9):
            return self.manifest.production_context
        return DecisionsContextV1(
            service_identity=self.manifest.service_identity, limits=self.manifest.limits
        )

    def verify_compatibility(
        self, *, capabilities: CapabilityProfile | None = None
    ) -> None:
        assert isinstance(self.spec, RunSpecV4) and isinstance(
            self.manifest, ExecutableRunManifestV8
        )
        self._source_guard()
        checked = validate_run_spec(self.spec)
        assert isinstance(checked, RunSpecV4)
        _verify_bulk_policy(checked)
        traits, exemplars = assessment_commitments(checked)
        if (
            (
                self.manifest.question_renderer_version == 1
                and checked.prompt.output_instructions is not None
            )
            or configuration_digest(checked) != self.manifest.configuration_sha256
            or assessment_digest(checked) != self.manifest.assessment_sha256
            or self.manifest_sha256 != self._sealed_manifest_sha256
            or bytes_digest(canonical_bytes(self.manifest)) != self.manifest_sha256
            or semantic_digest("prompt-template", self.template)
            != self.manifest.template.sha256
            or self.manifest.template
            != TemplateCommitment(
                sha256=semantic_digest("prompt-template", self.template),
                preset=None,
                advanced=checked.prompt.advanced,
                limits_sha256=semantic_digest("render-limits", checked.prompt.limits),
                presentation_version=2,
            )
            or self.manifest.task_kind != checked.task_kind
            or self.manifest.capture != checked.capture
            or self.manifest.input != checked.input
            or self.manifest.pairs != checked.pairs
            or self.manifest.pair_presentations != checked.pair_presentations
            or self.manifest.execution != checked.execution
            or self.manifest.seed != checked.seed
            or self.manifest.allow_ties != checked.allow_ties
            or self.manifest.strict_capabilities != checked.strict_capabilities
            or self.manifest.cache_policy_version != checked.cache_policy_version
            or self.manifest.generation != build_generation_commitment(checked)
            or self.manifest.trait_groups != ()
            or self.manifest.traits != traits
            or self.manifest.exemplars != exemplars
        ):
            raise PreparationError("Decisions configuration or seal changed")
        if (
            semantic_digest("decisions-context-v1", self.context())
            != self.manifest.transport_context_sha256
            or self.transport_context is not None
            and cast(object, self.transport_context) != self.context()
        ):
            raise PreparationError("Decisions context differs from seal")
        if capabilities is not None and (
            capabilities.grouped is False
            and self.spec.execution.request_grouping.max_questions_per_request > 1
            and self.context().limits.max_questions != 1
            or capabilities.modes is not None
            and checked.execution.mode not in capabilities.modes
        ):
            raise PreparationError("current Decisions capabilities are incompatible")

    def get_presentation(self, identifier: str) -> PairPresentationInputV1:
        self._source_guard()
        index = get_presentation(self.repository, self.manifest.run_id, identifier)
        source = self._by_id[index.source_id]
        path = (
            self.store.root / source.snapshot.relative_path
            if source.snapshot and source.snapshot.relative_path
            else Path(source.original_location)
        )
        with path.open("rb") as stream:
            if source.snapshot is not None:
                self.store.verify(source.snapshot)
            else:
                require_signature(path, source.generation, stream)
            stream.seek(index.byte_offset)
            raw = stream.read(index.byte_length)
        if len(raw) != index.byte_length:
            raise PreparationError("presentation locator is truncated")
        if source.format == "csv":
            assert source.parsing is not None and source.parsing.csv_header is not None
            prefix = io.StringIO(newline="")
            csv.writer(prefix).writerow(source.parsing.csv_header)
            raw = prefix.getvalue().encode("utf-8") + raw
        reference = cast(RunSpecV4, self.spec).pair_presentations
        assert reference is not None
        source_record = next(presentation_records(io.BytesIO(raw), reference))[0]
        if (
            canonical_bytes(source_record).decode("utf-8") != index.structure_json
            or semantic_digest("presentation-input-v1", source_record)
            != index.record_sha256
        ):
            raise PreparationError("presentation source changed")
        verify_members(self.repository, index, source_record)
        self._source_guard()
        return source_record

    def source_dependencies(
        self, record: ResponseItem | PairTask | PreparedPairPresentationV1
    ) -> Iterator[DecisionsDependencyV1]:
        assert isinstance(self.spec, RunSpecV4) and isinstance(
            self.manifest, ExecutableRunManifestV8
        )
        yield from plan_source(
            self.spec,
            record,
            self.get_response,
            self.context(),
            self.template,
            question_renderer_version=self.manifest.question_renderer_version,
        )

    def iter_request_dependencies(
        self, *, start_ordinal: int = 0
    ) -> Iterator[DecisionsDependencyV1]:
        assert isinstance(self.manifest, ExecutableRunManifestV8)
        self.verify_compatibility(capabilities=self.capabilities)
        if start_ordinal < 0:
            raise ValueError("request ordinal must be nonnegative")
        if (
            start_ordinal >= self.manifest.initial_transport_count
            and self.repository.get_run(self.manifest.run_id).manifest_sha256
            is not None
        ):
            return
        ordinal = 0
        if self.manifest.pair_presentations is not None:
            for indexed in iter_presentations(self.repository, self.manifest.run_id):
                for record in project_presentation(
                    self.get_presentation(indexed.source_presentation_id)
                ):
                    for dependency in self.source_dependencies(record):
                        if ordinal >= start_ordinal:
                            yield dependency
                        ordinal += 1
        else:
            records = (
                self.iter_responses()
                if self.spec.task_kind == "rubric"
                else self.iter_pairs()
            )
            for source in records:
                for dependency in self.source_dependencies(source):
                    if ordinal >= start_ordinal:
                        yield dependency
                    ordinal += 1

    def dependency_for_request(self, request_uid: str) -> DecisionsDependencyV1:
        first = self.repository.get_attempt_for_request(
            self.manifest.run_id, request_uid, attempt_number=1
        )
        if first is None:
            raise PreparationError("Decisions parent has no original attempt")
        receipt = self.transport_for_attempt(first.attempt_id)
        if receipt.source_presentation_id is not None:
            sources: Iterator[ResponseItem | PairTask | PreparedPairPresentationV1] = (
                p
                for p in project_presentation(
                    self.get_presentation(receipt.source_presentation_id)
                )
                if p.presentation_id == receipt.presentation_id
            )
        else:
            sources = iter(
                (
                    self.get_response(receipt.source_record_id)
                    if self.spec.task_kind == "rubric"
                    else self.get_pair(receipt.source_record_id),
                )
            )
        for source in sources:
            for dependency in self.source_dependencies(source):
                if dependency.request.request_uid == request_uid:
                    if (
                        dependency.dependency_sha256
                        != self.repository.get_request(
                            self.manifest.run_id, request_uid
                        ).dependency_sha256
                    ):
                        raise PreparationError("Decisions dependency changed")
                    return dependency
        raise PreparationError("Decisions request is absent from original membership")

    def transport_for_attempt(self, attempt_id: str) -> DecisionsCommitmentV1:
        receipt = read_transport_commitment(self.store, attempt_id)
        if not isinstance(receipt, DecisionsCommitmentV1):
            raise PreparationError("Decisions commitment is unavailable")
        attempt = self.repository.get_attempt(self.manifest.run_id, attempt_id)
        if receipt.identity != physical_identity(
            self.manifest.run_id, attempt.request_uid, attempt_id
        ) or (
            receipt.prompt_sha256,
            receipt.payload_sha256,
            receipt.membership_sha256,
        ) != (attempt.prompt_sha256, attempt.payload_sha256, attempt.membership_sha256):
            raise PreparationError("Decisions attempt differs from commitment")
        artifact = self.repository.get_artifact(
            self.manifest.run_id, bytes_digest(canonical_bytes(receipt))
        )
        if self.store.read(artifact) != canonical_bytes(receipt):
            raise PreparationError("Decisions receipt is not retained exactly")
        return receipt

    def prepare_transport_attempt(
        self,
        dependency: RequestDependency,
        identity: PhysicalRequestIdentityV1,
        *,
        frozen: object = None,
        parent: object = None,
    ) -> PreparedDecisionsRequestV1:
        from judgmentllm.execution.retry import subset_dependency

        assert isinstance(self.spec, RunSpecV4) and isinstance(
            dependency, DecisionsDependencyV1
        )
        self.verify_compatibility(capabilities=self.capabilities)
        original = self.dependency_for_request(identity.request_uid)
        selected = tuple(t.task_id for t in dependency.request.tasks)
        if canonical_bytes(subset_dependency(original, selected)) != canonical_bytes(
            dependency
        ):
            raise PreparationError(
                "Decisions attempt is not its original ordered subset"
            )
        first = self.repository.get_attempt_for_request(
            self.manifest.run_id, identity.request_uid, attempt_number=1
        )
        assert first is not None
        original_receipt = self.transport_for_attempt(first.attempt_id)
        expected_parent = (
            original_receipt if identity.attempt_id != first.attempt_id else None
        )
        if parent is not None and parent != expected_parent:
            raise PreparationError("Decisions retry parent conflicts")
        wire = prepare_request(
            self.spec, dependency, identity, self.context(), parent=expected_parent
        )
        try:
            existing = self.repository.get_attempt(
                self.manifest.run_id, identity.attempt_id
            )
        except NotFoundError:
            existing = None
        if existing is not None:
            pinned = self.transport_for_attempt(identity.attempt_id)
            if (
                pinned != wire.transport_commitment
                or frozen is not None
                and frozen != pinned
            ):
                raise PreparationError("Decisions frozen request changed")
            for content in wire.content_artifacts().values():
                artifact = self.repository.get_artifact(
                    self.manifest.run_id, bytes_digest(content)
                )
                if artifact.byte_length != len(content) or (
                    artifact.relative_path is not None
                    and self.store.read(artifact) != content
                ):
                    raise PreparationError("Decisions content artifact changed")
        elif expected_parent is None or parent is None:
            raise PreparationError(
                "new Decisions attempt requires original retry ancestry"
            )
        return wire

    def verify_transport_index(self) -> None:
        assert isinstance(self.manifest, ExecutableRunManifestV8)
        digest = InitialTransportDigest()
        tasks = 0
        # Native membership is already indexed; no corpus-sized map is retained.
        for request in self.repository.iter_requests(self.manifest.run_id):
            first = self.repository.get_attempt_for_request(
                self.manifest.run_id, request.request_uid, attempt_number=1
            )
            if first is None:
                raise PreparationError("Decisions request lacks its initial attempt")
            receipt = self.transport_for_attempt(first.attempt_id)
            expected = tuple(q.task_id for q in receipt.questions)
            if (
                self.repository.request_task_ids(
                    self.manifest.run_id, request.request_uid
                )
                != expected
                or self.repository.attempt_task_ids(
                    self.manifest.run_id, first.attempt_id
                )
                != expected
                or receipt.parent_commitment_sha256 is not None
            ):
                raise PreparationError("Decisions initial membership changed")
            digest.add(
                digest.count, request, first, bytes_digest(canonical_bytes(receipt))
            )
            tasks += len(expected)
        if (digest.count, digest.hexdigest(), tasks) != (
            self.manifest.initial_transport_count,
            self.manifest.initial_transport_sha256,
            self.manifest.logical_task_count,
        ):
            raise PreparationError("Decisions initial index differs from manifest")
        for source in self.manifest.sources:
            if source.role == "pair_presentations" and presentation_index_digest(
                self.repository, self.manifest.run_id
            ) != (source.index_sha256, source.record_count):
                raise PreparationError("presentation index differs from manifest")


def prepare_decisions_run(
    spec_or_path: RunSpecV4 | str | Path,
    *,
    store: str | Path,
    run_id: str,
    capabilities: CapabilityProfile | None = None,
    context: DecisionsContext | None = None,
) -> DecisionsPreparedRun:
    from judgmentllm.preparation import _checkpoint, _memory_source  # pyright: ignore[reportPrivateUsage]
    from judgmentllm.providers.decisions_mock import MockDecisionsAdapter

    repository = Repository(store)
    prepared: DecisionsPreparedRun | None = None
    try:
        repository.require_decisions_storage()
        try:
            repository.get_run(run_id)
        except NotFoundError:
            pass
        else:
            raise PreparationError("run already exists; reopen original preparation")
        origin = (
            None
            if isinstance(spec_or_path, RunSpecV4)
            else Path(spec_or_path).resolve()
        )
        initial = _memory_source(origin) if origin else None
        discovered: dict[str, tuple[FileSignature, str]] = {}

        def discover(path: Path) -> object:
            content, generation, digest = _memory_source(path)
            first = discovered.setdefault(str(path), (generation, digest))
            if first != (generation, digest):
                raise PreparationError("source changed during initial discovery")
            return parse_document(content.decode("utf-8"), path)

        spec = (
            validate_run_spec(spec_or_path)
            if isinstance(spec_or_path, RunSpecV4)
            else _load_run_spec(cast(Path, origin), document_reader=discover)
        )
        assert isinstance(spec, RunSpecV4)
        _verify_bulk_policy(spec)
        route = spec.provider, spec.endpoint
        if route in {("openai", "decisions"), TYPESAFE_ROUTE, GEMINI_DECISIONS_ROUTE}:
            if route == GEMINI_DECISIONS_ROUTE:
                from judgmentllm.providers.gemini_decisions import (
                    decisions_context as gemini_context,
                )

                current_context = gemini_context(spec)
            elif route == TYPESAFE_ROUTE:
                from judgmentllm.providers.typesafe_decisions import (
                    decisions_context as typesafe_context,
                )

                current_context = typesafe_context(spec)
            else:
                from judgmentllm.providers.openai_decisions import (
                    decisions_context as openai_context,
                )

                current_context = openai_context(spec)
            if context is not None and context != current_context:
                raise PreparationError(
                    "Production Decisions context differs from configuration"
                )
            context = current_context
        context = (
            context
            or MockDecisionsAdapter(
                Path(store) / "mock-service.sqlite3"
            ).transport_context
        )
        context = _revalidate_record(context)
        repository.create_run(
            RunRecord(
                run_id=run_id,
                task_kind=spec.task_kind,
                provider=spec.provider,
                endpoint=spec.endpoint,
                model=spec.model,
                capture=spec.capture,
            ),
            decisions_version=4,
        )
        with ExecutorOwnership(repository, run_id) as owner, ExitStack() as stack:
            assert owner.owner is not None
            builder = SourceBuilder(
                repository,
                run_id,
                spec.capture,
                owner.owner.token,
                stack,
                checkpoint=_checkpoint,
                memory_source=_memory_source,
                record_reader=read_record,
            )
            builder.discovered = discovered
            if initial is not None:
                builder.initial_root = initial[1], initial[2]
                spec = _load_run_spec(
                    cast(Path, origin), document_reader=builder.document
                )
                assert isinstance(spec, RunSpecV4)
                for frozen in builder.sources:
                    builder.register(frozen)
            template = None
            if spec.prompt.file is not None:
                frozen = builder.freeze(spec.prompt.file, "template", "text")
                builder.register(frozen)
                path = (
                    frozen.stage.verify()
                    if frozen.stage
                    else Path(frozen.commitment.original_location)
                )
                template = path.read_text(encoding="utf-8")
            builder.criterion_ids = {c.trait_id for c in spec.criteria}
            builder.input_spec, builder.pair_spec = spec.input, spec.pairs
            builder.index(spec.input, "response")
            if spec.pairs is not None:
                builder.index(spec.pairs, "pair")
            if spec.pair_presentations is not None:
                index_presentations(builder, spec)
            for trait_id in (
                [t.trait_id for t in spec.rubric.traits]
                if spec.rubric
                else [c.trait_id for c in spec.criteria]
            ):
                render_question(spec, trait_id, template)
            sources = builder.finish()
            raw = canonical_bytes(spec)
            specification = (
                builder.store.put(raw)
                if spec.capture == "full"
                else builder.store.hash_only(bytes_digest(raw), len(raw))
            )
            repository.register_artifact(run_id, specification)
            traits, exemplars = assessment_commitments(spec)
            manifest_type = (
                ExecutableRunManifestV11
                if isinstance(context, GeminiDecisionsContextV1)
                else ExecutableRunManifestV10
                if isinstance(context, TypeSafeDecisionsContextV1)
                else ExecutableRunManifestV9
                if isinstance(context, OpenAIDecisionsContextV1)
                else ExecutableRunManifestV8
            )
            manifest = manifest_type.model_validate(
                dict(
                    **(
                        {"production_context": context}
                        if not isinstance(context, DecisionsContextV1)
                        else {}
                    ),
                    run_id=run_id,
                    task_kind=spec.task_kind,
                    capture=spec.capture,
                    configuration_origin="file" if origin else "python",
                    configuration_location=str(origin) if origin else None,
                    configuration_sha256=configuration_digest(spec),
                    assessment_sha256=assessment_digest(spec),
                    specification=specification,
                    sources=sources,
                    input=spec.input,
                    pairs=spec.pairs,
                    pair_presentations=spec.pair_presentations,
                    traits=traits,
                    exemplars=exemplars,
                    template=TemplateCommitment(
                        sha256=semantic_digest("prompt-template", template),
                        preset=None,
                        advanced=spec.prompt.advanced,
                        limits_sha256=semantic_digest(
                            "render-limits", spec.prompt.limits
                        ),
                        presentation_version=2,
                    ),
                    execution=spec.execution,
                    trait_groups=(),
                    seed=spec.seed,
                    allow_ties=False,
                    strict_capabilities=spec.strict_capabilities,
                    generation=build_generation_commitment(spec),
                    capabilities=capabilities,
                    recovery_policy="explicit_verified_snapshot"
                    if spec.capture == "full"
                    else "unchanged_originals",
                    package_version=__version__,
                    cache_policy_version=2,
                    question_renderer_version=2,
                    service_identity=context.service_identity,
                    limits=context.limits,
                    transport_context_sha256=semantic_digest(
                        "decisions-context-v1", context
                    ),
                    initial_transport_count=0,
                    initial_transport_sha256="0" * 64,
                    logical_task_count=0,
                    presentation_count=0 if spec.pair_presentations else None,
                )
            )
            prepared = DecisionsPreparedRun(
                repository, manifest, spec, template, capabilities
            )
            digest = InitialTransportDigest()
            tasks = presentations = 0
            previous_presentation = None
            for dependency in prepared.iter_request_dependencies():
                uid = dependency.request.request_uid
                identity = physical_identity(
                    run_id, uid, stable_id("attempt", run_id, uid, 1)
                )
                wire = prepare_request(spec, dependency, identity, context)
                artifacts = publish_decisions_request(
                    repository, builder.store, wire, spec.capture
                )
                _checkpoint("after_transport_artifacts", uid)
                request = RequestRecord(
                    run_id=run_id,
                    request_uid=uid,
                    scheduling_ordinal=digest.count,
                    dependency_sha256=dependency.dependency_sha256,
                )
                attempt = AttemptRecord(
                    run_id=run_id,
                    request_uid=uid,
                    attempt_id=identity.attempt_id,
                    attempt_number=1,
                    prompt_sha256=wire.prompt_sha256,
                    payload_sha256=wire.payload_sha256,
                    membership_sha256=wire.transport_commitment.membership_sha256,
                )
                with repository.execution_transaction(run_id, owner.owner.token):
                    for artifact in artifacts:
                        repository._register_artifact(run_id, artifact)  # pyright: ignore[reportPrivateUsage]
                    for task in dependency.request.tasks:
                        repository.create_task(
                            TaskRecord(
                                run_id=run_id,
                                scheduling_ordinal=tasks,
                                **task.model_dump(exclude={"schema_version"}),
                            )
                        )
                        tasks += 1
                    repository.create_request(request, wire.task_ids)
                    repository.create_attempt(attempt, wire.task_ids)
                    repository.transition("request", run_id, uid, "prepared")
                    repository.advance_execution_checkpoint(
                        run_id, digest.count + 1, owner_token=owner.owner.token
                    )
                digest.add(
                    digest.count,
                    request,
                    attempt,
                    bytes_digest(canonical_bytes(wire.transport_commitment)),
                )
                if (
                    dependency.presentation_id is not None
                    and dependency.presentation_id != previous_presentation
                ):
                    presentations += 1
                    previous_presentation = dependency.presentation_id
                _checkpoint("after_transport_preparation", uid)
            manifest = manifest.model_copy(
                update={
                    "initial_transport_count": digest.count,
                    "initial_transport_sha256": digest.hexdigest(),
                    "logical_task_count": tasks,
                    "presentation_count": presentations
                    if spec.pair_presentations
                    else None,
                }
            )
            prepared.manifest = manifest
            prepared.manifest_sha256 = bytes_digest(canonical_bytes(manifest))
            prepared._sealed_manifest_sha256 = prepared.manifest_sha256  # pyright: ignore[reportPrivateUsage]
            prepared.verify_transport_index()
            artifact = builder.store.put(canonical_bytes(manifest), "application/json")
            repository.register_artifact(run_id, artifact)
            builder.store.publish_manifest(artifact)
            _checkpoint("manifest_published")
            for source in sources:
                require_signature(Path(source.original_location), source.generation)
            repository.seal_run(
                run_id, ManifestReference(artifact=artifact), decisions_spec=spec
            )
        return prepared
    except BaseException as error:
        prepared.close() if prepared is not None else repository.close()
        if isinstance(
            error, (OSError, ConfigurationError, ValidationError, UnicodeError)
        ):
            raise PreparationError(
                "source/configuration validation failed during preparation"
            ) from None
        if isinstance(error, ValueError):
            raise PreparationError(str(error)) from None
        raise
