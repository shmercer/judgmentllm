"""Verified prepared-run sessions, request dependencies and shared prompt rendering."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import ExitStack
from pathlib import Path
from typing import Any, BinaryIO, Literal

from judgmentllm.config import (
    RunSpec,
)
from judgmentllm.io import _revalidate_record  # pyright: ignore[reportPrivateUsage]
from judgmentllm.models import PairTask, ResponseItem
from judgmentllm.persistence.artifacts import ArtifactStore
from judgmentllm.persistence.generation import (
    validate_generation_compatibility,
)
from judgmentllm.persistence.hashing import (
    bytes_digest,
    canonical_bytes,
    semantic_digest,
)
from judgmentllm.persistence.models import (
    SHA256,
    ExecutionModel,
)
from judgmentllm.persistence.repository import Repository
from judgmentllm.planning import (
    CapabilityProfile,
    PlannedRequest,
    plan_record,
    plan_run,
)
from judgmentllm.preparation.freezing import (
    PreparationError,
    require_signature,
)
from judgmentllm.preparation.index import (
    read_record,
)
from judgmentllm.preparation.manifest import (
    ExecutableRunManifest,
    configuration_digest,
)
from judgmentllm.preparation.records import (  # pyright: ignore[reportPrivateUsage]
    RecordReader,
    check_record,
    record_digest,
    record_locator,
)
from judgmentllm.prompts import RenderedPrompt, render_prompt


class RequestDependency(ExecutionModel):
    """Bounded, verified groundwork; no serialized provider body or attempt."""

    request: PlannedRequest
    dependency_sha256: SHA256
    response_sha256: SHA256 | None = None
    left_sha256: SHA256 | None = None
    right_sha256: SHA256 | None = None


def _request_dependency(
    manifest: ExecutableRunManifest,
    request: PlannedRequest,
    record: ResponseItem | PairTask,
    left: ResponseItem | None = None,
    right: ResponseItem | None = None,
) -> RequestDependency:
    response_hash = record_digest(record) if isinstance(record, ResponseItem) else None
    left_hash = record_digest(left) if left else None
    right_hash = record_digest(right) if right else None
    return RequestDependency(
        request=request,
        response_sha256=response_hash,
        left_sha256=left_hash,
        right_sha256=right_hash,
        dependency_sha256=semantic_digest(
            "request-dependencies",
            {
                "request_uid": request.request_uid,
                "configuration_sha256": manifest.configuration_sha256,
                "record_sha256": record_digest(record),
                "response_sha256": response_hash,
                "left_sha256": left_hash,
                "right_sha256": right_hash,
            },
        ),
    )


def _render_request(
    spec: RunSpec,
    template: str | None,
    request: PlannedRequest,
    lookup: Callable[[Literal["response", "pair"], str], ResponseItem | PairTask],
) -> RenderedPrompt:
    options: dict[str, Any] = dict(
        explanations=spec.execution.explanations,
        allow_ties=spec.allow_ties,
        preset=spec.prompt.preset,
        template=template,
        advanced=spec.prompt.advanced,
        limits=spec.prompt.limits,
        exemplars=spec.exemplars,
    )
    if request.pair is not None:
        pair = lookup("pair", request.pair.pair_uid)
        assert isinstance(pair, PairTask)
        left, right = (
            lookup("response", pair.left_item_id),
            lookup("response", pair.right_item_id),
        )
        assert isinstance(left, ResponseItem) and isinstance(right, ResponseItem)
        criterion = next(c for c in spec.criteria if c.trait_id == pair.trait_id)
        return render_prompt(
            pair=pair, left=left, right=right, criterion=criterion, **options
        )
    assert request.response is not None and spec.rubric is not None
    response = lookup("response", request.response.item_id)
    assert isinstance(response, ResponseItem)
    return render_prompt(
        rubric=spec.rubric,
        traits=[
            t
            for trait_id in request.trait_ids
            for t in spec.rubric.traits
            if t.trait_id == trait_id
        ],
        response=response,
        **options,
    )


class PreparedRun:
    """Verified session over durable input commitments, not an executor."""

    def __init__(
        self,
        repository: Repository,
        manifest: ExecutableRunManifest,
        spec: RunSpec,
        template: str | None,
        capabilities: CapabilityProfile | None,
        *,
        authorized_sources: frozenset[str] = frozenset(),
        record_reader: RecordReader = read_record,
    ) -> None:
        self._read_record = record_reader
        self.repository = repository
        self.manifest = manifest
        self.spec = spec
        self.template = template
        self.capabilities = capabilities
        self.store = ArtifactStore(repository.run_directory(manifest.run_id))
        self.manifest_sha256 = bytes_digest(canonical_bytes(manifest))
        self._handles: dict[str, BinaryIO] = {}
        self._by_id = {s.source_id: s for s in manifest.sources}
        self._closed = False
        self._authorized_sources = authorized_sources

    def _source_guard(self) -> None:
        for source in self.manifest.sources:
            if source.source_id not in self._authorized_sources:
                require_signature(Path(source.original_location), source.generation)

    def close(self) -> None:
        if self._closed:
            return
        handles = tuple(self._handles.values())
        self._handles.clear()
        self._closed = True
        # ExitStack runs every closer even when an earlier descriptor close
        # fails. The repository must outlive, then close after, all source files.
        with ExitStack() as cleanup:
            cleanup.callback(self.repository.close)
            for handle in handles:
                cleanup.callback(handle.close)

    def __enter__(self) -> PreparedRun:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _get(
        self, kind: Literal["response", "pair"], identifier: str
    ) -> ResponseItem | PairTask:
        if self._closed:
            raise PreparationError("prepared run session is closed")
        self._source_guard()
        indexed = self.repository.get_input_record(
            self.manifest.run_id, kind, identifier
        )
        source = self._by_id[indexed.source_id]
        spec = self.manifest.input if kind == "response" else self.manifest.pairs
        assert spec is not None and source.parsing is not None
        path = (
            self.store.root / source.snapshot.relative_path
            if source.snapshot and source.snapshot.relative_path
            else Path(source.original_location)
        )
        assert path is not None
        if source.source_id not in self._handles:
            if source.snapshot is not None:
                self.store.verify(source.snapshot)
            self._handles[source.source_id] = path.open("rb")
        stream = self._handles[source.source_id]
        if source.snapshot is None:
            require_signature(path, source.generation, stream)
        try:
            record = self._read_record(
                stream, spec, record_locator(indexed), source.parsing
            )
            check_record(record, indexed)
            if source.snapshot is None:
                require_signature(path, source.generation, stream)
                # Filesystem clocks can leave same-length writes within one
                # tick indistinguishable through metadata. A shared buffered
                # reader can also retain the old record after an in-place edit.
                # Independently reparse just this locator and its strict header
                # context; exact source hashes remain the session-entry check.
                with path.open("rb") as verification:
                    require_signature(path, source.generation, verification)
                    verified = self._read_record(
                        verification, spec, record_locator(indexed), source.parsing
                    )
                    check_record(verified, indexed)
                    require_signature(path, source.generation, verification)
        except (ValueError, OSError):
            raise PreparationError(
                "indexed record failed source verification"
            ) from None
        if source.snapshot is None:
            require_signature(path, source.generation, stream)
        return record

    def get_response(self, item_id: str) -> ResponseItem:
        record = self._get("response", item_id)
        assert isinstance(record, ResponseItem)
        return record

    def get_pair(self, pair_uid: str) -> PairTask:
        record = self._get("pair", pair_uid)
        assert isinstance(record, PairTask)
        return record

    def iter_responses(self) -> Iterator[ResponseItem]:
        for record in self.repository.iter_input_records(
            self.manifest.run_id, "response"
        ):
            yield self.get_response(record.record_id)

    def iter_pairs(self) -> Iterator[PairTask]:
        for record in self.repository.iter_input_records(self.manifest.run_id, "pair"):
            yield self.get_pair(record.record_id)

    def verify_compatibility(
        self, *, capabilities: CapabilityProfile | None = None
    ) -> None:
        try:
            self._verify_compatibility(capabilities=capabilities)
        except (ValueError, OSError):
            raise PreparationError(
                "prepared configuration or current capabilities are incompatible"
            ) from None

    def _verify_compatibility(
        self, *, capabilities: CapabilityProfile | None = None
    ) -> None:
        self._source_guard()
        if bytes_digest(canonical_bytes(self.manifest)) != self.manifest_sha256:
            raise PreparationError("manifest context was mutated after verification")
        current = _revalidate_record(self.spec)
        if configuration_digest(current) != self.manifest.configuration_sha256:
            raise PreparationError("configuration differs from the immutable manifest")
        report = validate_generation_compatibility(
            self.manifest.generation,
            current,
            capabilities=capabilities.generation if capabilities else None,
        )
        if (
            not report.valid
            or plan_run(current, capabilities=capabilities).trait_groups
            != self.manifest.trait_groups
        ):
            raise PreparationError(
                "current capabilities or generation settings are incompatible"
            )
        if (
            semantic_digest("prompt-template", self.template)
            != self.manifest.template.sha256
        ):
            raise PreparationError("template differs from the immutable manifest")

    def iter_request_dependencies(self) -> Iterator[RequestDependency]:
        self.verify_compatibility(capabilities=self.capabilities)
        records = (
            self.iter_responses()
            if self.spec.task_kind == "rubric"
            else self.iter_pairs()
        )
        for record in records:
            # Revalidate mutable caller-visible context before each source record.
            self.verify_compatibility(capabilities=self.capabilities)
            left = (
                self.get_response(record.left_item_id)
                if isinstance(record, PairTask)
                else None
            )
            right = (
                self.get_response(record.right_item_id)
                if isinstance(record, PairTask)
                else None
            )
            for request in plan_record(
                self.spec,
                record,
                template_text=self.template,
                capabilities=self.capabilities,
            ):
                self.verify_compatibility(capabilities=self.capabilities)
                yield _request_dependency(self.manifest, request, record, left, right)

    def render(self, dependency: RequestDependency) -> RenderedPrompt:
        """Render with the established renderer; never retain text in minimal mode."""
        self.verify_compatibility(capabilities=self.capabilities)
        try:
            dependency = _revalidate_record(dependency)
            supplied = dependency.request
            if supplied.pair is not None:
                record = self.get_pair(supplied.pair.pair_uid)
                left, right = (
                    self.get_response(record.left_item_id),
                    self.get_response(record.right_item_id),
                )
            else:
                assert supplied.response is not None
                record = self.get_response(supplied.response.item_id)
                left = right = None
            expected = next(
                (
                    request
                    for request in plan_record(
                        self.spec,
                        record,
                        template_text=self.template,
                        capabilities=self.capabilities,
                    )
                    if request.request_uid == supplied.request_uid
                ),
                None,
            )
            if expected is None or canonical_bytes(
                _request_dependency(self.manifest, expected, record, left, right)
            ) != canonical_bytes(dependency):
                raise PreparationError(
                    "request dependencies differ from verified inputs"
                )
            return _render_request(self.spec, self.template, expected, self._get)
        except (ValueError, OSError):
            raise PreparationError("verified request could not be rendered") from None
