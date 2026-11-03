"""Consistent source capture, durable indexing and bounded renderer preflight."""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Literal, cast

from judgmentllm.config import (
    InputSpec,
    RunSpec,
    parse_document,
)
from judgmentllm.models import PairTask, ResponseItem
from judgmentllm.persistence.artifacts import ArtifactStore, StagedArtifact
from judgmentllm.persistence.hashing import (
    semantic_digest,
)
from judgmentllm.persistence.models import (
    NotFoundError,
)
from judgmentllm.persistence.repository import Repository
from judgmentllm.persistence.sources import InputRecord, PairStructure, SourceRecord
from judgmentllm.planning import (
    CapabilityProfile,
    PlannedRequest,
    plan_record,
    stable_id,
)
from judgmentllm.preparation.freezing import (
    FileSignature,
    HashingReader,
    PreparationError,
    chunks,
    hash_stream,
    require_signature,
    signature,
    verify_original,
)
from judgmentllm.preparation.index import (
    iter_records,
    parsing_context,
)
from judgmentllm.preparation.manifest import (
    SourceCommitment,
    SourceFormat,
)
from judgmentllm.preparation.records import (  # pyright: ignore[reportPrivateUsage]
    RecordReader,
    check_record,
    index_digest,
    record_digest,
    record_locator,
)
from judgmentllm.preparation.session import _render_request  # pyright: ignore[reportPrivateUsage]

SourceRole = Literal[
    "configuration", "rubric", "criteria", "exemplars", "template", "response", "pair"
]


@dataclass
class _Frozen:
    commitment: SourceCommitment
    stage: StagedArtifact | None


class SourceBuilder:
    def __init__(
        self,
        repository: Repository,
        run_id: str,
        capture: Literal["full", "minimal"],
        owner_token: str,
        stack: ExitStack,
        *,
        checkpoint: Callable[[str, str | None], None],
        memory_source: Callable[[Path], tuple[bytes, FileSignature, str]],
        record_reader: RecordReader,
    ) -> None:
        self._checkpoint = checkpoint
        self._memory_source = memory_source
        self._read_record = record_reader
        self.repository = repository
        self.run_id = run_id
        self.capture = capture
        self.owner_token = owner_token
        self.stack = stack
        self.store = ArtifactStore(repository.run_directory(run_id))
        self.sources: list[_Frozen] = []
        self.initial_root: tuple[FileSignature, str] | None = None
        self.discovered: dict[str, tuple[FileSignature, str]] = {}

    def freeze(
        self,
        path: Path,
        role: SourceRole,
        format: SourceFormat,
        initial: tuple[FileSignature, str] | None = None,
    ) -> _Frozen:
        path = path.absolute()
        ordinal = len(self.sources)
        source_id = stable_id("source", role, ordinal, str(path))
        self._checkpoint("source_open", source_id)
        with path.open("rb") as stream:
            generation = signature(path, stream)
            if initial is not None and generation != initial[0]:
                raise PreparationError("source generation changed during discovery")
            if self.capture == "full":
                stage = self.stack.enter_context(
                    self.store.staged_stream(chunks(stream))
                )
                digest, length = stage.identity.sha256, stage.identity.byte_length
            else:
                stage = None
                digest, length = hash_stream(stream)
            self._checkpoint("source_copied", source_id)
            require_signature(path, generation, stream)
        if initial is not None and digest != initial[1]:
            raise PreparationError("source content changed during discovery")
        self._checkpoint("independent_hash", source_id)
        verify_original(path, generation, digest, length)
        if stage is not None:
            stage.verify()
        frozen = _Frozen(
            SourceCommitment(
                source_id=source_id,
                role=role,
                ordinal=ordinal,
                original_location=str(path),
                raw_sha256=digest,
                byte_length=length,
                generation=generation,
                format=format,
            ),
            stage,
        )
        self.sources.append(frozen)
        return frozen

    def document(self, path: Path) -> object:
        # The loader calls the root first. Remaining document types have strict,
        # distinct document schemas; role inference is never input interpretation.
        raw, generation, digest = self._memory_source(path)
        first = self.discovered.get(str(path))
        if first is not None and first != (generation, digest):
            raise PreparationError("assessment source changed after initial discovery")
        if (
            not self.sources
            and self.initial_root is not None
            and (generation, digest) != self.initial_root
        ):
            raise PreparationError("configuration changed during initial discovery")
        data = parse_document(raw.decode("utf-8"), path)
        role: SourceRole = "configuration"
        if self.sources:
            if not isinstance(data, dict):
                raise PreparationError("assessment document must contain an object")
            role = "rubric"
            if "exemplars" in data:
                role = "exemplars"
            elif "criteria" in data:
                role = "criteria"
        frozen = self.freeze(
            path,
            role,
            cast(SourceFormat, path.suffix.lower().lstrip(".")),
            (generation, digest),
        )
        if frozen.stage is not None:
            # Full capture always parses the verified staged bytes.
            data = parse_document(
                frozen.stage.verify().read_text(encoding="utf-8"), path
            )
        frozen.commitment = frozen.commitment.model_copy(
            update={
                "canonical_sha256": semantic_digest(
                    "source-document", cast(object, data)
                )
            }
        )
        self._checkpoint("document_parsed", frozen.commitment.source_id)
        return cast(object, data)

    def register(self, frozen: _Frozen) -> None:
        source = frozen.commitment
        self.repository.add_source(
            SourceRecord(
                run_id=self.run_id,
                source_id=source.source_id,
                role=source.role,
                ordinal=source.ordinal,
                original_location=source.original_location,
                raw_sha256=source.raw_sha256,
                byte_length=source.byte_length,
                canonical_sha256=source.canonical_sha256,
                format=source.format,
                generation_json=json.dumps(
                    source.generation.model_dump(exclude={"schema_version"})
                ),
            ),
            owner_token=self.owner_token,
        )

    def index(self, spec: InputSpec, kind: Literal["response", "pair"]) -> None:
        frozen = self.freeze(spec.path, kind, cast(SourceFormat, spec.format))
        self.register(frozen)
        source = frozen.commitment
        path = frozen.stage.verify() if frozen.stage else Path(source.original_location)
        batch: list[InputRecord] = []
        with path.open("rb") as stream:
            if frozen.stage is None:
                require_signature(path, source.generation, stream)
            scanner = HashingReader(stream)
            for ordinal, located in enumerate(
                iter_records(cast(BinaryIO, scanner), spec, kind=kind)
            ):
                record = located.record
                structure = "{}"
                if isinstance(record, PairTask):
                    if record.trait_id not in self.criterion_ids:
                        raise PreparationError("pair references an unknown criterion")
                    # Strict projection: never copy free-form pair metadata.
                    structure = PairStructure.model_validate(
                        record.model_dump(exclude={"metadata"})
                    ).model_dump_json()
                    for item_id in (record.left_item_id, record.right_item_id):
                        try:
                            self.repository.get_input_record(
                                self.run_id, "response", item_id
                            )
                        except NotFoundError:
                            raise PreparationError(
                                "pair references a missing response"
                            ) from None
                batch.append(
                    InputRecord(
                        run_id=self.run_id,
                        record_kind=kind,
                        record_id=record.item_id
                        if isinstance(record, ResponseItem)
                        else record.pair_uid,
                        source_id=source.source_id,
                        ordinal=ordinal,
                        record_sha256=record_digest(record),
                        byte_offset=located.locator.offset,
                        byte_length=located.locator.byte_length,
                        structural_json=structure,
                    )
                )
                if len(batch) == 128:
                    self.repository.add_input_records(
                        batch, owner_token=self.owner_token
                    )
                    batch.clear()
                self._checkpoint("record_indexed", source.source_id)
            if batch:
                self.repository.add_input_records(batch, owner_token=self.owner_token)
            if (scanner.digest.hexdigest(), scanner.length) != (
                source.raw_sha256,
                source.byte_length,
            ):
                raise PreparationError(
                    "indexed stream differs from frozen source bytes"
                )
            if frozen.stage is None:
                require_signature(path, source.generation, stream)
            context = parsing_context(stream, spec, kind=kind)
        digest, count = index_digest(
            self.repository.iter_input_records(self.run_id, kind)
        )
        frozen.commitment = source.model_copy(
            update={
                "parsing": context,
                "index_sha256": digest,
                "record_count": count,
            }
        )
        self.verify_index(frozen, spec, kind)
        self._checkpoint("index_verified", source.source_id)

    criterion_ids: set[str]

    def lookup(
        self, kind: Literal["response", "pair"], identifier: str
    ) -> ResponseItem | PairTask:
        indexed = self.repository.get_input_record(self.run_id, kind, identifier)
        frozen = next(
            f for f in self.sources if f.commitment.source_id == indexed.source_id
        )
        source = frozen.commitment
        assert source.parsing is not None
        spec = self.input_spec if kind == "response" else self.pair_spec
        assert spec is not None
        path = frozen.stage.path if frozen.stage else Path(source.original_location)
        with path.open("rb") as stream:
            if frozen.stage is None:
                require_signature(path, source.generation, stream)
            record = self._read_record(
                stream, spec, record_locator(indexed), source.parsing
            )
            check_record(record, indexed)
            if frozen.stage is None:
                require_signature(path, source.generation, stream)
        return record

    input_spec: InputSpec
    pair_spec: InputSpec | None

    def validate_rendering(
        self,
        spec: RunSpec,
        template: str | None,
        capabilities: CapabilityProfile | None,
        *,
        exhaustive: bool = False,
    ) -> None:
        """Validate static context with bounded representatives by default.

        Real inputs are completely validated/indexed independently. Rendering
        every real response/group combination is an explicit diagnostic option;
        the normal path checks each configured group/criterion once, including
        when its source has no records. Actual selected work must pass the same
        renderer through PreparedRun.render before a future submission.
        """
        self.input_spec, self.pair_spec = spec.input, spec.pairs
        self._validate_representatives(spec, template, capabilities)
        if not exhaustive:
            self._checkpoint("rendering_verified", None)
            return
        kind = "response" if spec.task_kind == "rubric" else "pair"
        for indexed in self.repository.iter_input_records(self.run_id, kind):
            record = self.lookup(kind, indexed.record_id)
            for request in plan_record(
                spec, record, template_text=template, capabilities=capabilities
            ):
                _render_request(spec, template, request, self.lookup)
        self._checkpoint("rendering_verified", None)

    def _validate_representatives(
        self,
        spec: RunSpec,
        template: str | None,
        capabilities: CapabilityProfile | None,
    ) -> None:
        # Empty synthetic text checks the common schema/template/assessment
        # contract without imposing a made-up minimum response length. These
        # objects never enter source indexes, manifests, or request registries.
        left = ResponseItem(item_id="0", text="")
        right = ResponseItem(item_id="1", text="")

        def render(request: PlannedRequest) -> None:
            def lookup(
                kind: Literal["response", "pair"], identifier: str
            ) -> ResponseItem | PairTask:
                if kind == "pair":
                    assert request.pair is not None
                    return request.pair
                return left if identifier == left.item_id else right

            _render_request(spec, template, request, lookup)

        if spec.task_kind == "rubric":
            for request in plan_record(
                spec, left, template_text=template, capabilities=capabilities
            ):
                render(request)
        else:
            for criterion in spec.criteria:
                pair = PairTask(
                    pair_id="0",
                    pair_uid="0",
                    left_item_id=left.item_id,
                    right_item_id=right.item_id,
                    trait_id=criterion.trait_id,
                )
                for request in plan_record(
                    spec, pair, template_text=template, capabilities=capabilities
                ):
                    render(request)

    def verify_index(
        self, frozen: _Frozen, spec: InputSpec, kind: Literal["response", "pair"]
    ) -> None:
        source = frozen.commitment
        path = frozen.stage.verify() if frozen.stage else Path(source.original_location)
        assert source.parsing is not None
        with path.open("rb") as stream:
            if frozen.stage is None:
                require_signature(path, source.generation, stream)
            for indexed in self.repository.iter_input_records(self.run_id, kind):
                record = self._read_record(
                    stream, spec, record_locator(indexed), source.parsing
                )
                check_record(record, indexed)
                self._checkpoint("locator_verified", source.source_id)
            if frozen.stage is None:
                require_signature(path, source.generation, stream)

    def finish(self) -> tuple[SourceCommitment, ...]:
        for frozen in self.sources:
            source = frozen.commitment
            self._checkpoint("final_source_verification", source.source_id)
            verify_original(
                Path(source.original_location),
                source.generation,
                source.raw_sha256,
                source.byte_length,
            )
        self._checkpoint("all_sources_verified", None)
        for frozen in self.sources:
            source = frozen.commitment
            if frozen.stage is not None:
                identity = frozen.stage.publish()
                self.repository.register_artifact(self.run_id, identity)
                self.repository.update_source_snapshot(
                    self.run_id,
                    source.source_id,
                    identity.sha256,
                    owner_token=self.owner_token,
                )
                frozen.commitment = source.model_copy(update={"snapshot": identity})
            else:
                self.repository.register_artifact(
                    self.run_id,
                    ArtifactStore.hash_only(source.raw_sha256, source.byte_length),
                )
            self._checkpoint("snapshot_published", source.source_id)
        return tuple(f.commitment for f in self.sources)
