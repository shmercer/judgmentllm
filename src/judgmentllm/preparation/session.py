"""Verified prepared-run sessions, request dependencies and shared prompt rendering."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import ExitStack
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, Literal

from judgmentllm import gemini_types, vertex_types
from judgmentllm._verification import ExactToken, exact_token
from judgmentllm.config import (
    RunSpecification,
    RunSpecV3,
)
from judgmentllm.io import _revalidate_record  # pyright: ignore[reportPrivateUsage]
from judgmentllm.models import PairTask, ResponseItem
from judgmentllm.output_commitments import SchemaSelectionV2, validate_schema_selection
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
    NotFoundError,
)
from judgmentllm.persistence.repository import Repository
from judgmentllm.planning import (
    CapabilityProfile,
    PlannedRequest,
    RunPlan,
    _request_for_group,  # pyright: ignore[reportPrivateUsage]
    _requests_for_record,  # pyright: ignore[reportPrivateUsage]
    plan_record,
    plan_run,
    request_context_fingerprint,
)
from judgmentllm.preparation.freezing import (
    PreparationError,
    require_signature,
)
from judgmentllm.preparation.index import (
    read_record,
)
from judgmentllm.preparation.manifest import (
    ExecutableManifest,
    ExecutableRunManifestV2,
    ExecutableRunManifestV3,
    ExecutableRunManifestV4,
    ExecutableRunManifestV5,
    configuration_digest,
)
from judgmentllm.preparation.records import (  # pyright: ignore[reportPrivateUsage]
    RecordReader,
    check_record,
    record_digest,
    record_locator,
)
from judgmentllm.presentation import PresentationVersion
from judgmentllm.prompts import RenderedAssessmentPrompt

if TYPE_CHECKING:
    from judgmentllm.anthropic_prompts import RenderedAnthropicPromptV1
    from judgmentllm.anthropic_transport import (
        AnthropicProductionContext,
        PreparedRequestV5,
        TransportCommitmentV3,
    )
    from judgmentllm.native_prompts import RenderedPromptV3
    from judgmentllm.production_prompts import RenderedProductionPromptV1
    from judgmentllm.production_transport import (
        PreparedRequestV4,
        ProductionContext,
        TransportCommitmentV2,
    )
    from judgmentllm.transport import (
        PhysicalRequestIdentityV1,
        PreparedRequestV3,
        TransportCommitmentV1,
        TransportContext,
    )


class RequestDependency(ExecutionModel):
    """Bounded, verified groundwork; no serialized provider body or attempt."""

    request: PlannedRequest
    dependency_sha256: SHA256
    response_sha256: SHA256 | None = None
    left_sha256: SHA256 | None = None
    right_sha256: SHA256 | None = None


@dataclass(frozen=True)
class _VerifiedContext:
    """One detached assessment context; no student records or rendered text."""

    spec: RunSpecification
    manifest: ExecutableManifest
    template: str | None
    plan: RunPlan
    fingerprint: str
    groups_by_trait: dict[str, tuple[str, ...]]
    tokens: tuple[ExactToken, ...]


def _request_dependency(
    manifest: ExecutableManifest,
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
    spec: RunSpecification,
    template: str | None,
    request: PlannedRequest,
    lookup: Callable[[Literal["response", "pair"], str], ResponseItem | PairTask],
    selection: SchemaSelectionV2 | None = None,
    presentation_version: PresentationVersion = 2,
) -> RenderedAssessmentPrompt:
    from judgmentllm.request_rendering import render_request

    return render_request(
        spec,
        template,
        request,
        lookup,
        selection=selection,
        presentation_version=presentation_version,
    )


class PreparedRun:
    """Verified session over durable input commitments, not an executor."""

    def __init__(
        self,
        repository: Repository,
        manifest: ExecutableManifest,
        spec: RunSpecification,
        template: str | None,
        capabilities: CapabilityProfile | None,
        *,
        authorized_sources: frozenset[str] = frozenset(),
        record_reader: RecordReader = read_record,
        transport_context: TransportContext
        | ProductionContext
        | AnthropicProductionContext
        | vertex_types.VertexProductionContext
        | gemini_types.GeminiProductionContext
        | None = None,
    ) -> None:
        self._read_record = record_reader
        self.repository = repository
        self.manifest = manifest
        self.spec = spec
        self.template = template
        self.capabilities = capabilities
        self.transport_context = transport_context
        self._transport_index_verified = False
        self.store = ArtifactStore(repository.run_directory(manifest.run_id))
        self.manifest_sha256 = bytes_digest(canonical_bytes(manifest))
        self._sealed_manifest_sha256 = self.manifest_sha256
        self._context: _VerifiedContext | None = None
        self._handles: dict[str, BinaryIO] = {}
        self._by_id = {s.source_id: s for s in manifest.sources}
        self._closed = False
        self._authorized_sources = authorized_sources

    def _source_guard(self) -> None:
        manifest = (
            self._context.manifest if self._context is not None else self.manifest
        )
        for source in manifest.sources:
            if source.source_id not in self._authorized_sources:
                require_signature(Path(source.original_location), source.generation)

    def close(self) -> None:
        if self._closed:
            return
        handles = tuple(self._handles.values())
        self._handles.clear()
        self._context = None
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
        except (ValueError, OSError, RecursionError):
            raise PreparationError(
                "prepared configuration or current capabilities are incompatible"
            ) from None

    def _verify_compatibility(
        self, *, capabilities: CapabilityProfile | None = None
    ) -> None:
        if self._closed:
            raise PreparationError("prepared run session is closed")
        template = self.template
        if isinstance(
            self.manifest,
            (
                ExecutableRunManifestV3,
                ExecutableRunManifestV4,
                ExecutableRunManifestV5,
                vertex_types.ExecutableRunManifestV7,
                gemini_types.ExecutableRunManifestV6,
            ),
        ):
            from judgmentllm.transport_dispatch import context_digest, verify_protocol

            verify_protocol(self.manifest, self.transport_context)

            if (
                self.transport_context is None
                or context_digest(self.transport_context)
                != self.manifest.transport_context_sha256
            ):
                raise PreparationError("committed transport context is incompatible")
            if (
                self._transport_index_verified
                and self.repository.get_execution_checkpoint(self.manifest.run_id)
                != self.manifest.initial_transport_count
            ):
                raise PreparationError("native preparation checkpoint changed")
        elif self.transport_context is not None:
            raise PreparationError(
                "historical preparation cannot acquire native transport"
            )
        self._source_guard()
        if self.template != template:
            raise PreparationError("configuration changed during verification")
        if self.manifest_sha256 != self._sealed_manifest_sha256:
            raise PreparationError("manifest context was mutated after verification")
        tokens = tuple(
            exact_token(value)
            for value in (self.spec, self.manifest, template, capabilities)
        )
        if (
            self._context is not None
            and all(token is not None for token in tokens)
            and tokens == self._context.tokens
        ):
            return
        # A changed token is not evidence of incompatibility: validation can
        # accept value-equivalent replacements. Preserve the original identity
        # checks, including normalization, before refreshing private metadata.
        self._context = None
        cacheable = all(token is not None for token in tokens)
        spec, manifest, current_capabilities = self.spec, self.manifest, capabilities
        if cacheable:
            # Validate and derive metadata from the same detached raw values.
            # A caller changing and restoring public values during refresh
            # cannot associate unvalidated values with an accepted token.
            spec = deepcopy(spec)
            manifest = deepcopy(manifest)
            current_capabilities = deepcopy(current_capabilities)
            if (
                tuple(
                    exact_token(value)
                    for value in (spec, manifest, template, current_capabilities)
                )
                != tokens
            ):
                raise PreparationError("configuration changed during verification")
        if bytes_digest(canonical_bytes(manifest)) != self._sealed_manifest_sha256:
            raise PreparationError("manifest context was mutated after verification")
        current = _revalidate_record(spec)
        if isinstance(manifest, ExecutableRunManifestV2):
            validate_schema_selection(
                current, manifest.output_selection, current_capabilities
            )
        if configuration_digest(current) != manifest.configuration_sha256:
            raise PreparationError("configuration differs from the immutable manifest")
        profile = (
            _revalidate_record(current_capabilities)
            if current_capabilities is not None
            else None
        )
        report = validate_generation_compatibility(
            manifest.generation,
            current,
            capabilities=profile.generation if profile else None,
        )
        plan = plan_run(current, capabilities=profile)
        if not report.valid or plan.trait_groups != manifest.trait_groups:
            raise PreparationError(
                "current capabilities or generation settings are incompatible"
            )
        if semantic_digest("prompt-template", template) != manifest.template.sha256:
            raise PreparationError("template differs from the immutable manifest")
        if not cacheable:
            # Opaque/custom values retain the exact legacy validation and
            # rendering path. None must never act as an equal cached token.
            return
        # Keep accepted raw presentation (e.g. int versus float codes and
        # exemplar metadata key order), rather than rendering normalized data.
        self._context = _VerifiedContext(
            spec=spec,
            manifest=manifest,
            template=template,
            plan=plan,
            fingerprint=request_context_fingerprint(spec, template_text=template),
            groups_by_trait={
                trait_id: group for group in plan.trait_groups for trait_id in group
            },
            tokens=tuple(token for token in tokens if token is not None),
        )

    def _plan_requests(
        self, record: ResponseItem | PairTask, *, group: tuple[str, ...] | None = None
    ) -> Iterator[PlannedRequest]:
        context = self._context
        if context is None:
            yield from plan_record(
                self.spec,
                record,
                template_text=self.template,
                capabilities=self.capabilities,
            )
        elif isinstance(record, ResponseItem) != (context.spec.task_kind == "rubric"):
            raise ValueError("input record kind differs from the verified assessment")
        elif group is None:
            yield from _requests_for_record(
                context.spec, record, context.plan, context.fingerprint
            )
        elif group in context.plan.trait_groups:
            yield _request_for_group(
                record, group, context.plan.exemplar_ids, context.fingerprint
            )

    def _render_verified_request(
        self, request: PlannedRequest
    ) -> (
        RenderedAssessmentPrompt
        | RenderedPromptV3
        | RenderedProductionPromptV1
        | RenderedAnthropicPromptV1
        | vertex_types.RenderedVertexPromptV1
        | gemini_types.RenderedGeminiPromptV1
    ):
        context = self._context
        spec = context.spec if context is not None else self.spec
        template = context.template if context is not None else self.template
        if isinstance(
            self.manifest,
            (
                ExecutableRunManifestV3,
                ExecutableRunManifestV4,
                ExecutableRunManifestV5,
                vertex_types.ExecutableRunManifestV7,
                gemini_types.ExecutableRunManifestV6,
            ),
        ):
            from judgmentllm.planning import stable_id
            from judgmentllm.transport import (
                physical_identity,
            )
            from judgmentllm.transport_dispatch import prepare_request, render_request

            if self.transport_context is None or not isinstance(spec, RunSpecV3):
                raise PreparationError(
                    "native-capable rendering requires transport context"
                )
            attempt_id = stable_id(
                "attempt", self.manifest.run_id, request.request_uid, 1
            )
            dependency = self.dependency_for_request(request.request_uid)
            if canonical_bytes(dependency.request) != canonical_bytes(request):
                raise PreparationError(
                    "native rendering requires exact physical membership"
                )
            wire = prepare_request(
                spec,
                dependency,
                physical_identity(
                    self.manifest.run_id, request.request_uid, attempt_id
                ),
                self.transport_context,
                template=template,
                lookup=self._get,
                frozen=self.transport_for_attempt(attempt_id),
            )
            rendered = render_request(spec, template, request, self._get, wire)
        elif isinstance(self.manifest, ExecutableRunManifestV2):
            rendered = _render_request(
                spec,
                template,
                request,
                self._get,
                selection=self.manifest.output_selection,
                presentation_version=self.manifest.template.presentation_version,
            )
        else:
            rendered = _render_request(
                spec,
                template,
                request,
                self._get,
                presentation_version=self.manifest.template.presentation_version,
            )
        if context is not None:
            self.verify_compatibility(capabilities=self.capabilities)
            if self._context is not context:
                raise PreparationError("configuration changed while rendering")
        return rendered

    def transport_for_attempt(
        self, attempt_id: str
    ) -> (
        TransportCommitmentV1
        | TransportCommitmentV2
        | TransportCommitmentV3
        | vertex_types.TransportCommitmentV5
        | gemini_types.TransportCommitmentV4
    ):
        """Read original safe metadata; missing commitments are never repaired."""
        from judgmentllm.persistence.transport_receipts import read_transport_commitment
        from judgmentllm.planning import stable_id
        from judgmentllm.transport import physical_identity

        if not isinstance(
            self.manifest,
            (
                ExecutableRunManifestV3,
                ExecutableRunManifestV4,
                ExecutableRunManifestV5,
                vertex_types.ExecutableRunManifestV7,
                gemini_types.ExecutableRunManifestV6,
            ),
        ):
            raise PreparationError("historical preparation has no native transport")
        attempt = self.repository.get_attempt(self.manifest.run_id, attempt_id)
        receipt = read_transport_commitment(self.store, attempt_id)
        if receipt is None or (
            attempt.attempt_id
            != stable_id(
                "attempt",
                self.manifest.run_id,
                attempt.request_uid,
                attempt.attempt_number,
            )
            or receipt.identity
            != physical_identity(self.manifest.run_id, attempt.request_uid, attempt_id)
            or receipt.configuration_sha256 != self.manifest.configuration_sha256
            or receipt.context_sha256 != self.manifest.transport_context_sha256
            or receipt.dependency_sha256
            != self.repository.get_request(
                self.manifest.run_id, attempt.request_uid
            ).dependency_sha256
            or receipt.membership_sha256 != attempt.membership_sha256
            or receipt.prompt_sha256 != attempt.prompt_sha256
            or receipt.payload_sha256 != attempt.payload_sha256
        ):
            raise PreparationError("committed transport identity is inconsistent")
        from judgmentllm.transport_dispatch import manifest_protocol

        expected_version = (
            5
            if manifest_protocol(self.manifest) == "vertex"
            else 4
            if manifest_protocol(self.manifest) == "gemini"
            else 3
            if manifest_protocol(self.manifest) == "anthropic"
            else 2
            if manifest_protocol(self.manifest) == "production"
            else 1
        )
        if receipt.transport_commitment_version != expected_version:
            raise PreparationError(
                "transport receipt protocol differs from sealed manifest"
            )
        raw = canonical_bytes(receipt)
        artifact = self.repository.get_artifact(self.manifest.run_id, bytes_digest(raw))
        if (
            artifact.relative_path is None
            or artifact.byte_length != len(raw)
            or self.store.read(artifact) != raw
        ):
            raise PreparationError(
                "committed transport metadata is not retained exactly"
            )
        return receipt

    def prepare_transport_attempt(
        self,
        dependency: RequestDependency,
        identity: PhysicalRequestIdentityV1,
        *,
        frozen: TransportCommitmentV1
        | TransportCommitmentV2
        | TransportCommitmentV3
        | vertex_types.TransportCommitmentV5
        | gemini_types.TransportCommitmentV4
        | None = None,
        parent: TransportCommitmentV1
        | TransportCommitmentV2
        | TransportCommitmentV3
        | vertex_types.TransportCommitmentV5
        | gemini_types.TransportCommitmentV4
        | None = None,
    ) -> (
        PreparedRequestV3
        | PreparedRequestV4
        | PreparedRequestV5
        | vertex_types.PreparedRequestV7
        | gemini_types.PreparedRequestV6
    ):
        """Reconstruct pinned bytes or prepare an authorized ordered retry subset."""
        from judgmentllm.execution.retry import subset_dependency
        from judgmentllm.planning import stable_id
        from judgmentllm.transport_dispatch import prepare_request

        self.verify_compatibility(capabilities=self.capabilities)
        if (
            not isinstance(
                self.manifest,
                (
                    ExecutableRunManifestV3,
                    ExecutableRunManifestV4,
                    ExecutableRunManifestV5,
                    vertex_types.ExecutableRunManifestV7,
                    gemini_types.ExecutableRunManifestV6,
                ),
            )
            or self.transport_context is None
            or not isinstance(self.spec, RunSpecV3)
        ):
            raise PreparationError(
                "native-capable preparation requires transport context"
            )
        original = self.dependency_for_request(dependency.request.request_uid)
        selected = tuple(task.task_id for task in dependency.request.tasks)
        if canonical_bytes(subset_dependency(original, selected)) != canonical_bytes(
            dependency
        ):
            raise PreparationError(
                "native attempt differs from original ordered membership"
            )
        if (
            identity.run_id != self.manifest.run_id
            or identity.request_uid != dependency.request.request_uid
        ):
            raise PreparationError("native physical identity differs from its run")
        try:
            existing = self.repository.get_attempt(
                self.manifest.run_id, identity.attempt_id
            )
        except NotFoundError:
            existing = None
        if existing is not None:
            pinned = self.transport_for_attempt(identity.attempt_id)
            if frozen is not None and canonical_bytes(pinned) != canonical_bytes(
                frozen
            ):
                raise PreparationError(
                    "supplied transport commitment differs from original"
                )
            # An omitted argument never permits an already committed request to
            # enter ordinary auto selection again.
            frozen = pinned
        elif frozen is not None:
            raise PreparationError("frozen transport has no matching prepared attempt")
        first_id = stable_id("attempt", self.manifest.run_id, identity.request_uid, 1)
        if identity.attempt_id != first_id:
            original_receipt = self.transport_for_attempt(first_id)
            if frozen is not None and (
                frozen.parent_commitment_sha256 != original_receipt.commitment_sha256
                or frozen.mechanism != original_receipt.mechanism
                or frozen.requested_mechanism != original_receipt.requested_mechanism
                or frozen.selection.policy != original_receipt.selection.policy
            ):
                raise PreparationError(
                    "retry transport lineage differs from its original parent"
                )
            if parent is not None and canonical_bytes(parent) != canonical_bytes(
                original_receipt
            ):
                raise PreparationError("retry transport parent differs from original")
            parent = original_receipt
            from judgmentllm.transport import physical_identity

            # A retry cannot inherit a damaged parent's schema/format artifacts.
            self.prepare_transport_attempt(
                original,
                physical_identity(self.manifest.run_id, identity.request_uid, first_id),
                frozen=original_receipt,
            )
        elif parent is not None:
            raise PreparationError("initial transport cannot inherit another attempt")
        elif existing is None:
            raise PreparationError(
                "initial native transport must be prepared before sealing"
            )
        wire = prepare_request(
            self.spec,
            dependency,
            identity,
            self.transport_context,
            template=self.template,
            lookup=self._get,
            frozen=frozen,
            parent=parent,
        )
        if self.spec.provider in {"openai", "anthropic", "gemini"} and (
            getattr(wire.output_commitment, "service_policy", None)
            != getattr(self.manifest, "service_policy", None)
        ):
            raise PreparationError(
                "transport service policy differs from its sealed manifest"
            )
        if (
            wire.output_commitment.contract.presentation_version
            != self.manifest.template.presentation_version
        ):
            raise PreparationError(
                "transport presentation differs from its sealed manifest"
            )
        if frozen is not None:
            # Reopening an attempt verifies every promised retained artifact,
            # including subset-specific schemas and fragments, before replay.
            for content in wire.content_artifacts().values():
                artifact = self.repository.get_artifact(
                    self.manifest.run_id, bytes_digest(content)
                )
                if artifact.byte_length != len(content) or (
                    artifact.relative_path is not None
                ) != (self.manifest.capture == "full"):
                    raise PreparationError(
                        "committed transport artifact capture differs"
                    )
                self.store.verify(artifact)
        return wire

    def verify_transport_index(self) -> None:
        """Verify the complete initial index once, never select a replacement."""
        if (
            isinstance(
                self.manifest,
                (
                    ExecutableRunManifestV3,
                    ExecutableRunManifestV4,
                    ExecutableRunManifestV5,
                    vertex_types.ExecutableRunManifestV7,
                    gemini_types.ExecutableRunManifestV6,
                ),
            )
            and not self._transport_index_verified
        ):
            from judgmentllm.preparation.native import verify_initial_transports

            verify_initial_transports(self)
            self._transport_index_verified = True

    def _input_ids(
        self, kind: Literal["response", "pair"], start: int
    ) -> Iterator[str]:
        """Finish each bounded index read before execution writes to the WAL."""
        manifest = (
            self._context.manifest if self._context is not None else self.manifest
        )
        source_id = next(
            source.source_id for source in manifest.sources if source.role == kind
        )
        after = start
        while True:
            # A sealed manifest has exactly one source of each input kind, with
            # contiguous ingestion ordinals. Use the existing source-order index.
            rows = self.repository.connection.execute(
                "SELECT record_id,ordinal FROM input_records WHERE run_id=? "
                "AND source_id=? AND record_kind=? AND ordinal>=? "
                "ORDER BY ordinal LIMIT 128",
                (manifest.run_id, source_id, kind, after),
            ).fetchall()
            if not rows:
                return
            for row in rows:
                yield str(row["record_id"])
            after = int(rows[-1]["ordinal"]) + 1

    def iter_request_dependencies(
        self, *, start_ordinal: int = 0
    ) -> Iterator[RequestDependency]:
        self.verify_compatibility(capabilities=self.capabilities)
        if start_ordinal < 0:
            raise ValueError("request ordinal must be nonnegative")
        kind = "response" if self.spec.task_kind == "rubric" else "pair"
        group_count = len(self.manifest.trait_groups) if kind == "response" else 1
        record_start, group_start = divmod(start_ordinal, group_count)
        first = True
        for identifier in self._input_ids(kind, record_start):
            record = self._get(kind, identifier)
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
            for position, request in enumerate(self._plan_requests(record)):
                if first and position < group_start:
                    continue
                self.verify_compatibility(capabilities=self.capabilities)
                yield _request_dependency(self.manifest, request, record, left, right)
            first = False

    def dependency_for_request(self, request_uid: str) -> RequestDependency:
        """Reopen one immutable parent without enumerating previously scheduled work."""
        if self._context is not None:
            self.verify_compatibility(capabilities=self.capabilities)
        task_id = self.repository.request_task_ids(self.manifest.run_id, request_uid)[0]
        task = self.repository.get_task(self.manifest.run_id, task_id)
        record = (
            self.get_response(task.item_id)
            if task.item_id is not None
            else self.get_pair(task.pair_uid or "")
        )
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
        if self._context is not None:
            self.verify_compatibility(capabilities=self.capabilities)
        group = (
            self._context.groups_by_trait.get(task.trait_id)
            if self._context is not None
            else None
        )
        for request in self._plan_requests(record, group=group):
            if request.request_uid == request_uid:
                dependency = _request_dependency(
                    self.manifest, request, record, left, right
                )
                if (
                    dependency.dependency_sha256
                    != self.repository.get_request(
                        self.manifest.run_id, request_uid
                    ).dependency_sha256
                ):
                    raise PreparationError("parent request dependency changed")
                return dependency
        raise PreparationError("parent request is absent from verified plan")

    def render_attempt(
        self, dependency: RequestDependency
    ) -> (
        RenderedAssessmentPrompt
        | RenderedPromptV3
        | RenderedProductionPromptV1
        | RenderedAnthropicPromptV1
        | vertex_types.RenderedVertexPromptV1
        | gemini_types.RenderedGeminiPromptV1
    ):
        """Render an exact ordered subset while retaining its parent identity."""
        selected = tuple(t.task_id for t in dependency.request.tasks)
        if selected == self.repository.request_task_ids(
            self.manifest.run_id, dependency.request.request_uid
        ):
            if (
                dependency.dependency_sha256
                != self.repository.get_request(
                    self.manifest.run_id, dependency.request.request_uid
                ).dependency_sha256
            ):
                raise PreparationError("parent request dependency changed")
            # Whole-membership attempts use the strict original-request path.
            # It already rechecks sources, configuration, the original plan and
            # every dependency before rendering; no parent render is discarded.
            return self.render(dependency)
        parent = self.dependency_for_request(dependency.request.request_uid)
        self.render(parent)
        expected_tasks = tuple(t for t in parent.request.tasks if t.task_id in selected)
        expected = parent.model_copy(
            update={
                "request": parent.request.model_copy(
                    update={
                        "tasks": expected_tasks,
                        "trait_ids": tuple(t.trait_id for t in expected_tasks),
                    }
                )
            }
        )
        if (
            not selected
            or len(set(selected)) != len(selected)
            or canonical_bytes(expected) != canonical_bytes(dependency)
        ):
            raise PreparationError("attempt membership differs from its parent")
        if isinstance(
            self.manifest,
            (
                ExecutableRunManifestV3,
                ExecutableRunManifestV4,
                ExecutableRunManifestV5,
                vertex_types.ExecutableRunManifestV7,
                gemini_types.ExecutableRunManifestV6,
            ),
        ):
            from judgmentllm.native_prompts import render_request_v3
            from judgmentllm.planning import stable_id
            from judgmentllm.transport_dispatch import build_output

            if self.transport_context is None or not isinstance(self.spec, RunSpecV3):
                raise PreparationError(
                    "native-capable rendering requires transport context"
                )
            first_id = stable_id(
                "attempt", self.manifest.run_id, dependency.request.request_uid, 1
            )
            output = build_output(
                self.spec,
                dependency.request,
                self.transport_context,
                self.transport_for_attempt(first_id),
            )
            from judgmentllm.anthropic_transport import OutputCommitmentV5
            from judgmentllm.production_transport import OutputCommitmentV4

            if isinstance(output, vertex_types.OutputCommitmentV7):
                from judgmentllm.vertex_prompts import render_vertex_request

                return render_vertex_request(
                    self.spec, self.template, dependency.request, self._get, output
                )
            if isinstance(output, gemini_types.OutputCommitmentV6):
                from judgmentllm.gemini_prompts import render_gemini_request

                return render_gemini_request(
                    self.spec, self.template, dependency.request, self._get, output
                )
            if isinstance(output, OutputCommitmentV5):
                from judgmentllm.anthropic_prompts import render_anthropic_request

                return render_anthropic_request(
                    self.spec, self.template, dependency.request, self._get, output
                )
            if isinstance(output, OutputCommitmentV4):
                from judgmentllm.production_prompts import render_production_request

                return render_production_request(
                    self.spec, self.template, dependency.request, self._get, output
                )
            return render_request_v3(
                self.spec, self.template, dependency.request, self._get, output
            )
        return self._render_verified_request(dependency.request)

    def render(
        self, dependency: RequestDependency
    ) -> (
        RenderedAssessmentPrompt
        | RenderedPromptV3
        | RenderedProductionPromptV1
        | RenderedAnthropicPromptV1
        | vertex_types.RenderedVertexPromptV1
        | gemini_types.RenderedGeminiPromptV1
    ):
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
            if self._context is not None:
                self.verify_compatibility(capabilities=self.capabilities)
            expected = next(
                (
                    request
                    for request in self._plan_requests(record, group=supplied.trait_ids)
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
            return self._render_verified_request(expected)
        except (ValueError, OSError):
            raise PreparationError("verified request could not be rendered") from None
