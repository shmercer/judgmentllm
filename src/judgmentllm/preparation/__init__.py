"""Provider-free immutable preparation services (internal, not lifecycle APIs).

Use local runtime stores and existing per-run OS ownership. Interrupted scans
never restart under the same run identity.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from contextlib import ExitStack
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, cast

import yaml

from judgmentllm import __version__
from judgmentllm.config import (
    InputSpec,
    RunSpec,
    RunSpecification,
    RunSpecV2,
    RunSpecV3,
    _load_run_spec,  # pyright: ignore[reportPrivateUsage]
    parse_document,
    validate_run_spec_json,
)
from judgmentllm.io import _revalidate_record  # pyright: ignore[reportPrivateUsage]
from judgmentllm.models import PairTask, ResponseItem
from judgmentllm.output_commitments import resolve_schema_selection
from judgmentllm.persistence.artifacts import ArtifactStore
from judgmentllm.persistence.generation import build_generation_commitment
from judgmentllm.persistence.hashing import (
    bytes_digest,
    canonical_bytes,
    semantic_digest,
)
from judgmentllm.persistence.models import (
    ExecutionModel,
    ManifestReference,
    NotFoundError,
    OwnershipError,
    RunRecord,
    utc_now,
)
from judgmentllm.persistence.ownership import ExecutorOwnership
from judgmentllm.persistence.repository import Repository
from judgmentllm.planning import CapabilityProfile, ValidationIssue, plan_run
from judgmentllm.preparation.capture import SourceBuilder as _Builder  # pyright: ignore[reportPrivateUsage]
from judgmentllm.preparation.freezing import (
    FileSignature,
    PreparationError,
    chunks,
    require_signature,
    signature,
    verify_original,
)
from judgmentllm.preparation.index import ParsingContext, RecordLocator, read_record
from judgmentllm.preparation.manifest import (
    CategoryCommitment,
    ExecutableManifest,
    ExecutableRunManifest,
    ExecutableRunManifestV2,
    ExecutableRunManifestV3,
    ExecutableRunManifestV4,
    ExemplarCommitment,
    FrozenSnapshotAuthorization,
    SourceDiscrepancy,
    TemplateCommitment,
    TemplateCommitmentV2,
    TemplateCommitmentV3,
    TraitCommitment,
    assessment_digest,
    configuration_digest,
    parse_executable_manifest,
)
from judgmentllm.preparation.records import index_digest  # pyright: ignore[reportPrivateUsage]
from judgmentllm.preparation.session import PreparedRun as PreparedRun
from judgmentllm.preparation.session import RequestDependency as RequestDependency

if TYPE_CHECKING:
    from judgmentllm.production_transport import ProductionContext
    from judgmentllm.transport import TransportContext


def _checkpoint(stage: str, source_id: str | None = None) -> None:
    """Deterministic fault-injection seam; receives identities, never content."""


def _memory_source(path: Path) -> tuple[bytes, FileSignature, str]:
    """Read one assessment document in memory with independent verification."""
    with path.open("rb") as stream:
        generation = signature(path, stream)
        data = bytearray()
        for block in chunks(stream):
            data.extend(block)
        require_signature(path, generation, stream)
    raw = bytes(data)
    digest = bytes_digest(raw)
    verify_original(path, generation, digest, len(raw))
    return raw, generation, digest


def _read_record(
    stream: BinaryIO, spec: InputSpec, locator: RecordLocator, context: ParsingContext
) -> ResponseItem | PairTask:
    """Forward the facade fault-injection seam for existing sessions."""
    return read_record(stream, spec, locator, context)


def _manifest(
    builder: _Builder,
    spec: RunSpecification,
    origin: Path | None,
    template: str | None,
    capabilities: CapabilityProfile | None,
    transport_context: TransportContext | ProductionContext | None = None,
) -> ExecutableManifest:
    plan = plan_run(spec, capabilities=capabilities)
    raw = canonical_bytes(spec)
    identity = (
        builder.store.put(raw)
        if spec.capture == "full"
        else ArtifactStore.hash_only(bytes_digest(raw), len(raw))
    )
    builder.repository.register_artifact(builder.run_id, identity)
    traits: list[TraitCommitment] = []
    if spec.rubric is not None:
        for trait in spec.rubric.traits:
            traits.append(
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
            )
    else:
        traits.extend(
            TraitCommitment(trait_id=c.trait_id, sha256=semantic_digest("criterion", c))
            for c in spec.criteria
        )
    from judgmentllm.transport_dispatch import context_protocol

    manifest_type = (
        ExecutableRunManifestV4
        if transport_context is not None
        and context_protocol(transport_context) == "production"
        else ExecutableRunManifestV3
        if spec.schema_version == 3
        else ExecutableRunManifestV2
        if spec.schema_version == 2
        else ExecutableRunManifest
    )
    template_type = (
        TemplateCommitmentV3
        if spec.schema_version == 3
        else TemplateCommitmentV2
        if spec.schema_version == 2
        else TemplateCommitment
    )
    manifest_data: dict[str, object] = dict(
        run_id=builder.run_id,
        task_kind=spec.task_kind,
        capture=spec.capture,
        configuration_origin="file" if origin else "python",
        configuration_location=str(origin) if origin else None,
        configuration_sha256=configuration_digest(spec),
        assessment_sha256=assessment_digest(spec),
        specification=identity,
        sources=tuple(f.commitment for f in builder.sources),
        input=spec.input,
        pairs=spec.pairs,
        traits=tuple(traits),
        exemplars=tuple(
            ExemplarCommitment(
                exemplar_id=e.exemplar_id, sha256=semantic_digest("exemplar", e)
            )
            for e in spec.exemplars
        ),
        template=template_type(
            sha256=semantic_digest("prompt-template", template),
            preset=spec.prompt.preset,
            advanced=spec.prompt.advanced,
            limits_sha256=semantic_digest("render-limits", spec.prompt.limits),
        ),
        execution=spec.execution,
        trait_groups=plan.trait_groups,
        seed=spec.seed,
        allow_ties=spec.allow_ties,
        strict_capabilities=spec.strict_capabilities,
        generation=build_generation_commitment(
            spec, capabilities=capabilities.generation if capabilities else None
        ),
        capabilities=capabilities,
        recovery_policy="explicit_verified_snapshot"
        if spec.capture == "full"
        else "unchanged_originals",
        package_version=__version__,
    )
    if spec.schema_version == 2:
        manifest_data["output_selection"] = resolve_schema_selection(spec, capabilities)
    if spec.schema_version == 3:
        from judgmentllm.transport_dispatch import context_digest

        if transport_context is None:
            raise PreparationError(
                "native-capable preparation requires transport context"
            )
        manifest_data.update(
            transport_context_sha256=context_digest(transport_context),
            initial_transport_count=0,
            initial_transport_sha256="0" * 64,
        )
    return manifest_type.model_validate(manifest_data)


def prepare_run(
    spec_or_path: RunSpecification | str | Path,
    *,
    store: str | Path,
    run_id: str,
    capabilities: CapabilityProfile | None = None,
    exhaustive_rendering: bool = False,
    transport_context: TransportContext | ProductionContext | None = None,
) -> PreparedRun:
    """Freeze, validate, index and seal a new run under exclusive OS ownership.

    Interrupted scan rows are append-only: only a completely published verified
    manifest can be finished by open_prepared_run. Otherwise use a new run ID.
    All sources/records are checked; rendering uses bounded representatives unless
    exhaustive_rendering explicitly requests every response/group combination.
    """
    if type(exhaustive_rendering) is not bool:
        raise PreparationError("exhaustive_rendering must be a boolean")
    repository = Repository(store)
    prepared: PreparedRun | None = None
    try:
        # Reject reused identities without attempting to delete/restart ingestion.
        try:
            repository.get_run(run_id)
        except NotFoundError:
            pass
        else:
            raise PreparationError(
                "run already exists; reopen verified preparation or use a new run"
            )
        origin = (
            None
            if isinstance(spec_or_path, (RunSpec, RunSpecV2, RunSpecV3))
            else Path(spec_or_path).resolve()
        )
        initial: tuple[FileSignature, str] | None = None
        discovered: dict[str, tuple[FileSignature, str]] = {}
        if origin is None:
            assert isinstance(spec_or_path, (RunSpec, RunSpecV2, RunSpecV3))
            spec = _revalidate_record(spec_or_path)
        else:
            raw, sig, digest = _memory_source(origin)
            # Bootstrap only in memory: privacy policy is unknown until parsed.
            data = parse_document(raw.decode("utf-8"), origin)
            if not isinstance(data, dict):
                raise PreparationError("configuration must contain an object")
            capture = cast(dict[str, object], data).get("capture", "full")
            if capture not in ("full", "minimal"):
                raise PreparationError("capture must be full or minimal")

            # Validate through the existing loader; actual frozen reconstruction
            # below is checked against the initial root identity before sealing.
            def discover_document(path: Path) -> object:
                content, generation, sha256 = _memory_source(path)
                first = discovered.setdefault(str(path), (generation, sha256))
                if first != (generation, sha256):
                    raise PreparationError("source changed during initial discovery")
                return parse_document(content.decode("utf-8"), path)

            discovered[str(origin)] = (sig, digest)
            spec = _load_run_spec(origin, document_reader=discover_document)
            initial = sig, digest
        profile = _revalidate_record(capabilities) if capabilities else None
        if spec.schema_version == 3 and transport_context is None:
            raise PreparationError(
                "native-capable preparation requires transport context"
            )
        if spec.schema_version != 3 and transport_context is not None:
            raise PreparationError(
                "historical preparation cannot acquire native transport"
            )
        if transport_context is not None:
            from judgmentllm.transport_dispatch import context_protocol

            context_protocol(transport_context)
        plan_run(spec, capabilities=profile)
        repository.create_run(
            RunRecord(
                run_id=run_id,
                task_kind=spec.task_kind,
                provider=spec.provider,
                endpoint=spec.endpoint,
                model=spec.model,
                capture=spec.capture,
            )
        )
        with ExecutorOwnership(repository, run_id) as ownership, ExitStack() as stack:
            assert ownership.owner is not None
            builder = _Builder(
                repository,
                run_id,
                spec.capture,
                ownership.owner.token,
                stack,
                checkpoint=_checkpoint,
                memory_source=_memory_source,
                record_reader=_read_record,
            )
            builder.initial_root = initial
            builder.discovered = discovered
            if origin is not None:
                spec = _load_run_spec(origin, document_reader=builder.document)
                if repository.get_run(run_id).capture != spec.capture:
                    raise PreparationError("capture changed during discovery")
                for frozen in builder.sources:
                    builder.register(frozen)
            template: str | None = None
            if spec.prompt.file is not None:
                frozen = builder.freeze(spec.prompt.file, "template", "text")
                builder.register(frozen)
                path = (
                    frozen.stage.verify()
                    if frozen.stage
                    else Path(frozen.commitment.original_location)
                )
                with path.open("r", encoding="utf-8") as stream:
                    template = stream.read()
                if frozen.stage is None:
                    verify_original(
                        path,
                        frozen.commitment.generation,
                        frozen.commitment.raw_sha256,
                        frozen.commitment.byte_length,
                    )
            builder.criterion_ids = {c.trait_id for c in spec.criteria}
            builder.index(spec.input, "response")
            if spec.pairs is not None:
                builder.index(spec.pairs, "pair")
            builder.validate_rendering(
                spec, template, profile, exhaustive=exhaustive_rendering
            )
            builder.finish()
            manifest = _manifest(
                builder, spec, origin, template, profile, transport_context
            )
            if isinstance(manifest, (ExecutableRunManifestV3, ExecutableRunManifestV4)):
                from judgmentllm.preparation.native import preseal_transports

                assert transport_context is not None and isinstance(spec, RunSpecV3)
                count, digest = preseal_transports(
                    builder,
                    spec,
                    template,
                    manifest,
                    transport_context,
                    _checkpoint,
                )
                manifest = manifest.model_copy(
                    update={
                        "initial_transport_count": count,
                        "initial_transport_sha256": digest,
                    }
                )
                # Independently verify the complete bounded index before a seal
                # can authorize execution. The same session is returned below.
                prepared = PreparedRun(
                    repository,
                    manifest,
                    spec,
                    template,
                    profile,
                    record_reader=_read_record,
                    transport_context=transport_context,
                )
                prepared.verify_compatibility(capabilities=profile)
                prepared.verify_transport_index()
            artifact = builder.store.put(canonical_bytes(manifest), "application/json")
            repository.register_artifact(run_id, artifact)
            builder.store.publish_manifest(artifact)
            _checkpoint("manifest_published")
            _checkpoint("before_seal")
            # Hashes were checked across the complete source set. Close the
            # publication window with a final generation sweep immediately
            # before the short sealing transaction; never adopt a new generation.
            for source in manifest.sources:
                require_signature(Path(source.original_location), source.generation)
            repository.seal_run(run_id, ManifestReference(artifact=artifact))
            _checkpoint("manifest_sealed")
        if prepared is None:
            prepared = PreparedRun(
                repository,
                manifest,
                spec,
                template,
                profile,
                record_reader=_read_record,
                transport_context=transport_context,
            )
        prepared.verify_compatibility(capabilities=profile)
        prepared.verify_transport_index()
        return prepared
    except BaseException as error:
        if prepared is not None:
            prepared.close()
        else:
            repository.close()
        if isinstance(error, (OSError, ValueError, yaml.YAMLError)):
            raise PreparationError(
                "source/configuration validation failed during preparation"
            ) from None
        raise


def _compatible_package_version(producer: str, consumer: str) -> bool:
    """Allow only explicitly qualified, directional preparation compatibility.

    Package provenance remains immutable. Schema/serializer, content, payload and
    provider-evidence validation still apply independently when reopening a run.
    Future releases must qualify their own producer set rather than inherit a
    permissive version range.
    """
    producers = {
        "0.1.0a2": {"0.1.0a2"},
        "0.1.0a3": {"0.1.0a2", "0.1.0a3"},
    }
    return producer in producers.get(consumer, set())


def _load_manifest(
    repository: Repository, run_id: str, *, unsealed: bool
) -> ExecutableManifest:
    directory = repository.run_directory(run_id)
    fixed = directory / "manifest.json"
    if fixed.is_symlink() or not fixed.is_file():
        raise PreparationError(
            "no complete published manifest; incomplete preparation requires a new run"
        )
    store = ArtifactStore(directory, create=False)
    try:
        raw = fixed.read_bytes()
        manifest = parse_executable_manifest(raw)
    except (ValueError, OSError):
        raise PreparationError("published manifest is invalid or incomplete") from None
    if manifest.run_id != run_id or canonical_bytes(manifest) != raw:
        raise PreparationError(
            "published manifest identity or encoding is inconsistent"
        )
    identity = repository.get_artifact(run_id, bytes_digest(raw))
    if store.read(identity) != raw:
        raise PreparationError("published manifest conflicts with its immutable object")
    if not unsealed and repository.get_manifest(run_id).artifact != identity:
        raise PreparationError("published manifest conflicts with the database seal")
    run = repository.get_run(run_id)
    if (run.task_kind, run.provider, run.endpoint, run.model, run.capture) != (
        manifest.task_kind,
        manifest.generation.provider,
        manifest.generation.endpoint,
        manifest.generation.model,
        manifest.capture,
    ):
        raise PreparationError("manifest run identity is inconsistent")
    if not _compatible_package_version(manifest.package_version, __version__):
        raise PreparationError("prepared software version is incompatible")
    return manifest


def _verify_metadata(repository: Repository, manifest: ExecutableManifest) -> None:
    rows = tuple(repository.iter_sources(manifest.run_id))
    if len(rows) != len(manifest.sources):
        raise PreparationError("source set differs from manifest")
    for row, source in zip(rows, manifest.sources, strict=True):
        expected_generation = source.generation.model_dump(exclude={"schema_version"})
        if (
            row.source_id != source.source_id
            or row.ordinal != source.ordinal
            or row.role != source.role
            or row.original_location != source.original_location
            or row.raw_sha256 != source.raw_sha256
            or row.byte_length != source.byte_length
            or row.canonical_sha256 != source.canonical_sha256
            or row.format != source.format
            or row.snapshot_sha256
            != (source.snapshot.sha256 if source.snapshot else None)
            or json.loads(row.generation_json or "null") != expected_generation
        ):
            raise PreparationError("source metadata differs from manifest")
        if source.role in ("response", "pair"):
            kind = source.role
            digest, count = index_digest(
                repository.iter_input_records(manifest.run_id, kind)
            )
            if digest != source.index_sha256 or count != source.record_count:
                raise PreparationError("input index differs from manifest commitment")


def _restore_context(
    repository: Repository,
    manifest: ExecutableManifest,
    supplied: RunSpecification | None,
) -> tuple[RunSpecification, str | None]:
    store = ArtifactStore(repository.run_directory(manifest.run_id))
    if manifest.capture == "full":
        frozen = validate_run_spec_json(
            store.read(manifest.specification).decode("utf-8")
        )
        spec = _revalidate_record(supplied) if supplied is not None else frozen
    elif manifest.configuration_location is not None:
        by_location = {s.original_location: s for s in manifest.sources}

        def verified_document(path: Path) -> object:
            source = by_location.get(str(path))
            if source is None:
                raise PreparationError("configuration discovered an unfrozen source")
            data, sig, digest = _memory_source(path)
            if sig != source.generation or digest != source.raw_sha256:
                raise PreparationError(
                    "configuration source differs from frozen identity"
                )
            return parse_document(data.decode("utf-8"), path)

        frozen = _load_run_spec(
            manifest.configuration_location, document_reader=verified_document
        )
        spec = _revalidate_record(supplied) if supplied is not None else frozen
    else:
        if supplied is None:
            raise PreparationError(
                "minimal Python-origin preparation requires the original RunSpec"
            )
        spec = _revalidate_record(supplied)
    if (
        configuration_digest(spec) != manifest.configuration_sha256
        or assessment_digest(spec) != manifest.assessment_sha256
    ):
        raise PreparationError(
            "configuration or assessment differs from immutable manifest"
        )
    raw = canonical_bytes(spec)
    if (bytes_digest(raw), len(raw)) != (
        manifest.specification.sha256,
        manifest.specification.byte_length,
    ):
        raise PreparationError("validated specification artifact differs from manifest")
    template: str | None = None
    for source in manifest.sources:
        if source.role == "template":
            if source.snapshot is not None:
                # Match established universal newline template loading semantics.
                import io

                with io.TextIOWrapper(
                    io.BytesIO(store.read(source.snapshot)), encoding="utf-8"
                ) as stream:
                    template = stream.read()
            else:
                data, sig, digest = _memory_source(Path(source.original_location))
                if sig != source.generation or digest != source.raw_sha256:
                    raise PreparationError(
                        "template source differs from frozen identity"
                    )
                template = (
                    data.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
                )
    return spec, template


def _source_discrepancies(
    manifest: ExecutableManifest,
) -> tuple[SourceDiscrepancy, ...]:
    discrepancies: list[SourceDiscrepancy] = []
    for source in manifest.sources:
        path = Path(source.original_location)
        try:
            verify_original(
                path, source.generation, source.raw_sha256, source.byte_length
            )
        except PreparationError:
            discrepancies.append(
                SourceDiscrepancy(
                    source_id=source.source_id,
                    reason="generation_or_content_changed"
                    if path.is_file()
                    else "missing_or_unreadable",
                    expected_sha256=source.raw_sha256,
                )
            )
    return tuple(discrepancies)


def open_prepared_run(
    run_id: str,
    *,
    store: str | Path,
    spec: RunSpecification | None = None,
    capabilities: CapabilityProfile | None = None,
    use_frozen_snapshot: bool = False,
    owner_token: str | None = None,
    transport_context: TransportContext | ProductionContext | None = None,
) -> PreparedRun:
    """Verify and reopen; explicit snapshot permission is scoped to this call.

    A registered prior authorization is evidence only. An unsealed published
    manifest can finish sealing only after full independent verification under
    ownership. Any earlier interrupted preparation requires a new run.
    """
    repository = Repository(store)
    prepared: PreparedRun | None = None
    try:
        run = repository.get_run(run_id)
        unsealed = run.manifest_sha256 is None
        with ExitStack() as stack:
            if owner_token is not None:
                owner = repository.get_owner(run_id)
                if owner is None or owner.token != owner_token:
                    raise OwnershipError("executor owner token does not match")
            elif unsealed or use_frozen_snapshot:
                stack.enter_context(ExecutorOwnership(repository, run_id))
            manifest = _load_manifest(repository, run_id, unsealed=unsealed)
            _verify_metadata(repository, manifest)
            artifacts = ArtifactStore(repository.run_directory(run_id))
            for source in manifest.sources:
                if source.snapshot is not None:
                    artifacts.verify(source.snapshot)
            discrepancies = _source_discrepancies(manifest)
            if discrepancies and (
                manifest.capture == "minimal" or not use_frozen_snapshot
            ):
                raise PreparationError(
                    "original sources changed or disappeared; continuation is blocked"
                )
            current, template = _restore_context(repository, manifest, spec)
            profile = _revalidate_record(capabilities) if capabilities else None
            prepared = PreparedRun(
                repository,
                manifest,
                current,
                template,
                profile,
                record_reader=_read_record,
                transport_context=transport_context,
                authorized_sources=frozenset(d.source_id for d in discrepancies)
                if use_frozen_snapshot and manifest.capture == "full"
                else frozenset(),
            )
            prepared.verify_compatibility(capabilities=profile)
            prepared.verify_transport_index()
            # Independently reparse every locator before completing an interrupted
            # seal. Ordinary reopened lookups verify selected records on demand.
            if unsealed:
                if run.state != "created":
                    raise PreparationError("incomplete run state cannot be sealed")
                for _ in prepared.iter_responses():
                    pass
                for _ in prepared.iter_pairs():
                    pass
                # Recheck earlier originals after potentially long index validation.
                if _source_discrepancies(manifest) != discrepancies:
                    raise PreparationError(
                        "sources changed while recovering preparation"
                    )
            if use_frozen_snapshot and manifest.capture == "full":
                authorization = FrozenSnapshotAuthorization(
                    operation_id=uuid.uuid4().hex,
                    run_id=run_id,
                    manifest_sha256=prepared.manifest_sha256,
                    discrepancies=discrepancies,
                    occurred_at=utc_now(),
                )
                repository.register_artifact(
                    run_id,
                    artifacts.put(canonical_bytes(authorization), "application/json"),
                )
            if unsealed:
                identity = repository.get_artifact(run_id, prepared.manifest_sha256)
                repository.seal_run(run_id, ManifestReference(artifact=identity))
        return prepared
    except BaseException as error:
        if prepared is not None:
            prepared.close()
        else:
            repository.close()
        if isinstance(error, (ValueError, OSError, yaml.YAMLError)):
            raise PreparationError(
                "prepared source or manifest validation failed"
            ) from None
        raise


def iter_snapshot_authorizations(
    run_id: str,
    *,
    store: str | Path,
) -> Iterator[FrozenSnapshotAuthorization]:
    """Independently discover verified audit evidence; grants no permission."""
    with Repository(store) as repository:
        digest = repository.get_manifest(run_id).artifact.sha256
        artifacts = ArtifactStore(repository.run_directory(run_id))
        for identity in repository.iter_artifacts(run_id):
            if (
                identity.media_type != "application/json"
                or identity.relative_path is None
            ):
                continue
            raw = artifacts.read(identity)
            try:
                value = json.loads(raw)
                if (
                    not isinstance(value, dict)
                    or cast(dict[str, object], value).get("kind")
                    != "frozen_snapshot_authorization"
                ):
                    continue
                authorization = FrozenSnapshotAuthorization.model_validate_json(raw)
            except ValueError:
                raise PreparationError(
                    "snapshot authorization evidence is invalid"
                ) from None
            if (
                authorization.run_id != run_id
                or authorization.manifest_sha256 != digest
            ):
                raise PreparationError(
                    "snapshot authorization evidence has inconsistent scope"
                )
            yield authorization


class InputValidationReport(ExecutionModel):
    valid: bool
    response_count: int = 0
    pair_count: int = 0
    source_count: int = 0
    issues: tuple[ValidationIssue, ...] = ()


def validate_inputs(
    spec_or_path: RunSpecification | str | Path,
    *,
    capabilities: CapabilityProfile | None = None,
    exhaustive_rendering: bool = False,
) -> InputValidationReport:
    """Complete offline validation using the same pipeline in a private local store.

    Capture follows the requested policy. The private store is removed on exit;
    minimal capture never writes assessment text even during validation.
    Set exhaustive_rendering=True to additionally render every actual request;
    default rendering checks bounded static representatives only.
    """
    import tempfile

    try:
        # Resolve via the same verified loader but keep original references by
        # validating the path itself below. prepare_run's private metadata store
        # uses requested capture; cleanup is unconditional and no user store exists.
        with tempfile.TemporaryDirectory(prefix="judgmentllm-validation-") as temporary:
            with prepare_run(
                spec_or_path,
                store=temporary,
                run_id="validation",
                capabilities=capabilities,
                exhaustive_rendering=exhaustive_rendering,
            ) as prepared:
                return InputValidationReport(
                    valid=True,
                    response_count=sum(
                        s.record_count
                        for s in prepared.manifest.sources
                        if s.role == "response"
                    ),
                    pair_count=sum(
                        s.record_count
                        for s in prepared.manifest.sources
                        if s.role == "pair"
                    ),
                    source_count=len(prepared.manifest.sources),
                )
    except (PreparationError, ValueError):
        return InputValidationReport(
            valid=False,
            issues=(
                ValidationIssue(
                    code="invalid_inputs",
                    field="input",
                    message="source/configuration validation failed",
                    severity="error",
                ),
            ),
        )
