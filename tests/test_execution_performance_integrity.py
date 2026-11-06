"""Verified-session reuse preserves exact commitments and mutation boundaries."""

from __future__ import annotations

import gc
import os
import sqlite3
import sys
import weakref
from contextlib import closing
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Literal

import pytest
from test_execution import setup
from test_preparation_attempt_rendering import seal_request
from test_preparation_rendering import specification

from judgmentllm.config import GenerationSpec, RunSpec, RunSpecification
from judgmentllm.execution import execute_run, recover_run
from judgmentllm.models import Exemplar, PairTask, ResponseItem
from judgmentllm.persistence.hashing import canonical_bytes
from judgmentllm.persistence.models import IntegrityError, TaskRecord
from judgmentllm.persistence.repository import Repository
from judgmentllm.planning import (
    CapabilityProfile,
    _request_for_group,  # pyright: ignore[reportPrivateUsage]
    iter_requests,
    request_context_fingerprint,
)
from judgmentllm.preparation import PreparedRun, open_prepared_run, prepare_run, session
from judgmentllm.preparation.freezing import PreparationError
from judgmentllm.preparation.manifest import configuration_digest
from judgmentllm.preparation.session import (
    RequestDependency,
    _request_dependency,  # pyright: ignore[reportPrivateUsage]
)
from judgmentllm.prompts import RenderedAssessmentPrompt, render_prompt
from judgmentllm.providers import MockAdapter, PreparedRequest


def guarded_spec(directory: Path) -> RunSpec:
    spec = specification(directory, responses=2, traits=3)
    spec.provider, spec.endpoint = "mock", "synthetic"
    spec.generation = GenerationSpec(temperature=0.0)
    spec.seed = 1
    spec.execution.concurrency = 1
    spec.exemplars = [
        Exemplar(
            exemplar_id="001",
            text="Memory-only exemplar.",
            metadata={"number": 1, "flag": True},
        )
    ]
    assert spec.rubric is not None
    spec.rubric.traits[0].levels[0].numeric_value = 0.0
    spec.rubric.traits[0].levels[1].numeric_value = 3.0
    return RunSpec.model_validate(spec.model_dump())


def render_current_rubric(
    prepared: PreparedRun, dependency: RequestDependency
) -> RenderedAssessmentPrompt:
    current = prepared.spec
    assert current.rubric is not None
    return render_prompt(
        response=prepared.get_response("000"),
        rubric=current.rubric,
        traits=[
            trait
            for trait_id in dependency.request.trait_ids
            for trait in current.rubric.traits
            if trait.trait_id == trait_id
        ],
        exemplars=current.exemplars,
        explanations=current.execution.explanations,
        allow_ties=current.allow_ties,
        preset=current.prompt.preset,
        template=prepared.template,
        advanced=current.prompt.advanced,
        limits=current.prompt.limits,
    )


@pytest.mark.parametrize("capture", ["full", "minimal"])
def test_equivalent_replacement_objects_preserve_exact_rendering(
    tmp_path: Path, capture: Literal["full", "minimal"]
) -> None:
    spec = guarded_spec(tmp_path / "inputs")
    spec.capture = capture
    adapter = MockAdapter(tmp_path / "ledger.sqlite3")
    capabilities = adapter.capabilities(spec)
    with prepare_run(
        spec, store=tmp_path / "store", run_id="001", capabilities=capabilities
    ) as prepared:
        dependency = next(prepared.iter_request_dependencies())
        original = prepared.render(dependency)
        wire = adapter.prepare(prepared.spec, dependency, original)
        manifest_digest = prepared.manifest_sha256
        for _ in range(2):
            prepared.spec = prepared.spec.model_copy(deep=True)
            prepared.manifest = prepared.manifest.model_copy(deep=True)
            prepared.capabilities = capabilities.model_copy(deep=True)
            prepared.verify_compatibility(capabilities=prepared.capabilities)
            selected = next(prepared.iter_request_dependencies())
            rendered = prepared.render(selected)
            assert selected == dependency
            assert rendered == original
            assert adapter.prepare(prepared.spec, selected, rendered) == wire
            assert prepared.manifest_sha256 == manifest_digest


def change_context(prepared: PreparedRun, change: str) -> None:
    spec = prepared.spec
    assert spec.rubric is not None and spec.generation is not None
    trait = spec.rubric.traits[0]
    if change == "boolean_concurrency":
        object.__setattr__(spec.execution, "concurrency", True)
    elif change == "float_seed":
        object.__setattr__(spec, "seed", 1.0)
    elif change == "signed_zero":
        object.__setattr__(spec.generation, "temperature", -0.0)
    elif change == "json_number_type":
        spec.exemplars[0].metadata["number"] = 1.0
    elif change == "json_boolean_type":
        spec.exemplars[0].metadata["flag"] = 1
    elif change == "rubric_description":
        trait.description += " Changed."
    elif change == "category_value":
        trait.levels[1].numeric_value = 4.0
    elif change == "generation":
        spec.generation = GenerationSpec(temperature=0.5)
    elif change == "grouping":
        spec.execution.grouping = "grouped"
    elif change == "exemplar":
        spec.exemplars[0].text += " Changed."
    elif change == "extra_field":
        object.__setattr__(spec, "unexpected", "EXTRA_PRIVATE_SENTINEL")
    elif change == "nested_extra_field":
        object.__setattr__(trait, "unexpected", "EXTRA_PRIVATE_SENTINEL")
    elif change == "cycle":
        object.__setattr__(spec, "unexpected", spec)
    elif change == "manifest":
        object.__setattr__(prepared.manifest, "allow_ties", True)
    elif change == "template":
        prepared.template = "A changed template."
    elif change == "capabilities":
        assert prepared.capabilities is not None
        prepared.capabilities.modes = ("batch_native",)
    elif change == "boolean_capability":
        assert prepared.capabilities is not None
        object.__setattr__(prepared.capabilities, "explanations", 1)
    else:
        raise AssertionError(change)


CONTEXT_CHANGES = [
    "boolean_concurrency",
    "float_seed",
    "signed_zero",
    "json_number_type",
    "json_boolean_type",
    "rubric_description",
    "category_value",
    "generation",
    "grouping",
    "exemplar",
    "extra_field",
    "nested_extra_field",
    "cycle",
    "manifest",
    "template",
    "capabilities",
    "boolean_capability",
]


@pytest.mark.parametrize("change", CONTEXT_CHANGES)
def test_changed_caller_visible_context_cannot_reuse_verified_state(
    tmp_path: Path, change: str
) -> None:
    spec = guarded_spec(tmp_path / "inputs")
    store = tmp_path / "store"
    adapter = MockAdapter(tmp_path / "ledger.sqlite3")
    capabilities = adapter.capabilities(spec)
    with prepare_run(
        spec, store=store, run_id="001", capabilities=capabilities
    ) as prepared:
        dependency = next(prepared.iter_request_dependencies())
        expected = prepared.render(dependency)
        change_context(prepared, change)
        with pytest.raises(IntegrityError) as error:
            prepared.render(dependency)
        assert "EXTRA_PRIVATE_SENTINEL" not in str(error.value)
    with open_prepared_run(
        "001", store=store, spec=spec, capabilities=adapter.capabilities(spec)
    ) as reopened:
        assert reopened.render(next(reopened.iter_request_dependencies())) == expected
    assert adapter.ledger.acceptance_count() == 0


def test_separate_current_capability_clone_is_revalidated(tmp_path: Path) -> None:
    spec = guarded_spec(tmp_path / "inputs")
    adapter = MockAdapter(tmp_path / "ledger.sqlite3")
    with prepare_run(
        spec,
        store=tmp_path / "store",
        run_id="001",
        capabilities=adapter.capabilities(spec),
    ) as prepared:
        dependency = next(prepared.iter_request_dependencies())
        expected = prepared.render(dependency)
        assert prepared.capabilities is not None
        valid = prepared.capabilities.model_copy(deep=True)
        prepared.verify_compatibility(capabilities=valid)
        assert prepared.render(dependency) == expected

        invalid = valid.model_copy(update={"explanations": 1})
        # This clone does not alias the frozen manifest: rejecting it requires
        # checking the current capability data, not just the manifest digest.
        assert invalid is not prepared.manifest.capabilities
        with pytest.raises(IntegrityError):
            prepared.verify_compatibility(capabilities=invalid)

        prepared.verify_compatibility(capabilities=valid.model_copy(deep=True))
        assert prepared.render(dependency) == expected
    assert adapter.ledger.acceptance_count() == 0


@pytest.mark.parametrize("change", ["configuration", "capabilities"])
def test_cache_refresh_cannot_borrow_validation_from_transient_public_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    spec = guarded_spec(tmp_path / "inputs")
    spec.execution.grouping = "grouped"
    adapter = MockAdapter(tmp_path / "ledger.sqlite3")
    capabilities = adapter.capabilities(spec)
    with prepare_run(
        spec, store=tmp_path / "store", run_id="001", capabilities=capabilities
    ) as prepared:
        dependency = next(prepared.iter_request_dependencies())
        prepared.render(dependency)
        allowed_generation = prepared.spec.generation
        rejected_generation = GenerationSpec(temperature=0.5)
        if change == "configuration":
            prepared.spec.generation = rejected_generation
            public: RunSpecification | CapabilityProfile = prepared.spec
        else:
            capabilities.grouped = False
            public = capabilities
        original_validate = session._revalidate_record  # pyright: ignore[reportPrivateUsage]

        def transient_validation(
            value: RunSpecification | CapabilityProfile,
        ) -> RunSpecification | CapabilityProfile:
            if value is not public:
                return original_validate(value)
            if change == "configuration":
                prepared.spec.generation = allowed_generation
                try:
                    return original_validate(value)
                finally:
                    prepared.spec.generation = rejected_generation
            capabilities.grouped = True
            try:
                return original_validate(value)
            finally:
                capabilities.grouped = False

        # Deterministically model another caller temporarily exposing valid B
        # during validation, then restoring incompatible A before cache capture.
        monkeypatch.setattr(session, "_revalidate_record", transient_validation)
        for _ in range(2):
            with pytest.raises(IntegrityError):
                prepared.verify_compatibility(capabilities=capabilities)
        if change == "configuration":
            assert prepared.spec.generation is rejected_generation
        else:
            assert capabilities.grouped is False


def test_cache_refresh_cannot_hash_a_different_public_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = guarded_spec(tmp_path / "inputs")
    with prepare_run(spec, store=tmp_path / "store", run_id="001") as prepared:
        dependency = next(prepared.iter_request_dependencies())
        prepared.render(dependency)
        public = prepared.manifest
        object.__setattr__(public, "run_id", "changed-run")
        original_bytes = session.canonical_bytes

        def transient_manifest(value: object) -> bytes:
            if value is not public:
                return original_bytes(value)
            object.__setattr__(public, "run_id", "001")
            try:
                return original_bytes(value)
            finally:
                object.__setattr__(public, "run_id", "changed-run")

        monkeypatch.setattr(session, "canonical_bytes", transient_manifest)
        for _ in range(2):
            with pytest.raises(IntegrityError):
                prepared.verify_compatibility(capabilities=prepared.capabilities)
        assert prepared.manifest.run_id == "changed-run"


def test_numeric_replacement_accepted_when_existing_validation_preserves_digest(
    tmp_path: Path,
) -> None:
    spec = guarded_spec(tmp_path / "inputs")
    caller_bytes = canonical_bytes(spec)
    with prepare_run(spec, store=tmp_path / "store", run_id="001") as prepared:
        dependency = next(prepared.iter_request_dependencies())
        prepared.render(dependency)
        assert prepared.spec.rubric is not None
        object.__setattr__(prepared.spec.rubric.traits[0].levels[0], "numeric_value", 0)
        prepared.verify_compatibility(capabilities=prepared.capabilities)
        assert prepared.render(dependency) == render_current_rubric(
            prepared, dependency
        )
        assert prepared.spec is not spec
        assert canonical_bytes(spec) == caller_bytes
        assert spec.rubric is not None
        assert type(spec.rubric.traits[0].levels[0].numeric_value) is float


@pytest.mark.parametrize(
    "value", [Decimal("0"), Fraction(0)], ids=["decimal", "fraction"]
)
def test_other_numeric_types_preserve_legacy_compatibility_and_render_rejection(
    tmp_path: Path, value: object
) -> None:
    spec = guarded_spec(tmp_path / "inputs")
    with prepare_run(spec, store=tmp_path / "store", run_id="001") as prepared:
        dependency = next(prepared.iter_request_dependencies())
        prepared.render(dependency)
        assert prepared.spec.rubric is not None
        object.__setattr__(
            prepared.spec.rubric.traits[0].levels[0], "numeric_value", value
        )
        for _ in range(2):
            prepared.verify_compatibility(capabilities=prepared.capabilities)
        # Legacy semantic configuration validation accepts an equivalent numeric
        # value, while raw structural request serialization changes its identity.
        with (
            pytest.warns(UserWarning, match="Pydantic serializer warnings"),
            pytest.raises(IntegrityError),
        ):
            prepared.render(dependency)


def test_equivalent_mapping_order_preserves_current_exact_prompt(
    tmp_path: Path,
) -> None:
    spec = guarded_spec(tmp_path / "inputs")
    with prepare_run(spec, store=tmp_path / "store", run_id="001") as prepared:
        dependency = next(prepared.iter_request_dependencies())
        original = prepared.render(dependency)
        exemplar = prepared.spec.exemplars[0]
        digest = configuration_digest(prepared.spec)
        exemplar.metadata = dict(reversed(list(exemplar.metadata.items())))
        assert configuration_digest(prepared.spec) == digest
        prepared.verify_compatibility(capabilities=prepared.capabilities)
        assert next(prepared.iter_request_dependencies()) == dependency
        expected = render_current_rubric(prepared, dependency)
        assert expected.text != original.text
        assert prepared.render(dependency) == expected
        assert list(spec.exemplars[0].metadata) == ["number", "flag"]


@pytest.mark.parametrize("retain_iterator", [False, True])
@pytest.mark.parametrize("capture", ["full", "minimal"])
def test_close_releases_owned_context_with_normal_iterator_lifetime(
    tmp_path: Path, retain_iterator: bool, capture: Literal["full", "minimal"]
) -> None:
    spec = guarded_spec(tmp_path / "inputs")
    spec.capture = capture
    with prepare_run(spec, store=tmp_path / "store", run_id="001") as prepared:
        dependencies = prepared.iter_request_dependencies() if retain_iterator else None
        dependency = (
            next(dependencies)
            if dependencies is not None
            else next(prepared.iter_request_dependencies())
        )
        prepared.render(dependency)
        assert prepared._context is not None  # pyright: ignore[reportPrivateUsage]
        reference = weakref.ref(prepared._context)  # pyright: ignore[reportPrivateUsage]
        prepared.close()
        assert prepared._context is None  # pyright: ignore[reportPrivateUsage]
        assert prepared.spec == spec
        gc.collect()
        if retain_iterator:
            # A suspended iterator owns ordinary frame references. Closing the
            # session releases its cache ownership, not those caller-held frames.
            assert reference() is not None
        else:
            assert reference() is None
        with pytest.raises(IntegrityError, match="closed"):
            prepared.verify_compatibility(capabilities=prepared.capabilities)
        assert prepared._context is None  # pyright: ignore[reportPrivateUsage]
        dependencies = None
        gc.collect()
        assert reference() is None


def test_pairwise_session_rejects_fabricated_rubric_dependency(tmp_path: Path) -> None:
    spec = specification(tmp_path / "inputs", responses=2, traits=1, pairwise=True)
    with prepare_run(spec, store=tmp_path / "store", run_id="001") as prepared:
        next(prepared.iter_request_dependencies())
        response = prepared.get_response("000")
        fabricated = _request_for_group(
            response,
            (prepared.spec.criteria[0].trait_id,),
            (),
            request_context_fingerprint(prepared.spec, template_text=prepared.template),
        )
        dependency = _request_dependency(prepared.manifest, fabricated, response)
        with pytest.raises(PreparationError):
            prepared.render(dependency)
        assert not list(prepared.repository.iter_attempts("001"))
        assert not list(prepared.repository.iter_results("001"))


@pytest.mark.parametrize("capture", ["full", "minimal"])
@pytest.mark.parametrize("read_number", [1, 2], ids=["initial_read", "renderer_read"])
@pytest.mark.parametrize(
    "change", ["rubric_description", "template", "equivalent_copy", "metadata_order"]
)
def test_caller_mutation_during_selected_reads_cannot_use_stale_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capture: Literal["full", "minimal"],
    read_number: int,
    change: str,
) -> None:
    spec = guarded_spec(tmp_path / "inputs")
    spec.capture = capture
    original_source = spec.input.path.read_bytes()
    with prepare_run(spec, store=tmp_path / "store", run_id="001") as prepared:
        dependency = next(prepared.iter_request_dependencies())
        expected = prepared.render(dependency)
        original_get = prepared._get  # pyright: ignore[reportPrivateUsage]
        reads = 0
        changed = False

        def changing_get(
            kind: Literal["response", "pair"], identifier: str
        ) -> ResponseItem | PairTask:
            nonlocal reads, changed
            result = original_get(kind, identifier)
            reads += 1
            if reads == read_number:
                changed = True
                if change == "rubric_description":
                    assert prepared.spec.rubric is not None
                    prepared.spec.rubric.traits[0].description += " Changed."
                elif change == "template":
                    prepared.template = "A changed template during source lookup."
                elif change == "equivalent_copy":
                    prepared.spec = prepared.spec.model_copy(deep=True)
                    prepared.manifest = prepared.manifest.model_copy(deep=True)
                else:
                    exemplar = prepared.spec.exemplars[0]
                    exemplar.metadata = dict(reversed(list(exemplar.metadata.items())))
            return result

        monkeypatch.setattr(prepared, "_get", changing_get)
        if change == "equivalent_copy":
            assert prepared.render(dependency) == expected
        elif change == "metadata_order" and read_number == 1:
            actual = prepared.render(dependency)
            assert actual.text != expected.text
            assert actual == render_current_rubric(prepared, dependency)
        else:
            with pytest.raises(IntegrityError):
                prepared.render(dependency)
        assert changed and reads >= read_number
        assert spec.input.path.read_bytes() == original_source
        assert not list(prepared.repository.iter_attempts("001"))
        assert not list(prepared.repository.iter_submissions("001"))
        assert not list(prepared.repository.iter_results("001"))


def test_suspended_request_iterator_does_not_pin_sqlite_wal(tmp_path: Path) -> None:
    spec = specification(tmp_path / "inputs", responses=3, traits=2)
    store = tmp_path / "store"
    with prepare_run(spec, store=store, run_id="001") as prepared:
        dependencies = prepared.iter_request_dependencies()
        first = next(dependencies)
        task = first.request.tasks[0]
        prepared.repository.create_task(
            TaskRecord(
                run_id="001",
                scheduling_ordinal=0,
                **task.model_dump(exclude={"schema_version"}),
            )
        )
        with closing(sqlite3.connect(store / "registry.sqlite3", timeout=5)) as probe:
            busy, frames, checkpointed = probe.execute(
                "PRAGMA wal_checkpoint(PASSIVE)"
            ).fetchone()
        assert busy == 0
        assert frames > 0
        assert checkpointed == frames, (frames, checkpointed)
        complete = [first, *dependencies]
        assert len(complete) == 6
        assert list(prepared.iter_request_dependencies(start_ordinal=3)) == complete[3:]


@pytest.mark.parametrize("pairwise", [False, True], ids=["rubric", "pairwise"])
def test_paged_request_order_and_resume_across_index_boundaries(
    tmp_path: Path, pairwise: bool
) -> None:
    spec = specification(
        tmp_path / "inputs", responses=260, traits=3, pairwise=pairwise
    )
    with prepare_run(spec, store=tmp_path / "store", run_id="001") as prepared:
        complete = list(prepared.iter_request_dependencies())
        group_count = 1 if pairwise else 3
        assert len(complete) == 260 * group_count
        assert [dependency.request for dependency in complete] == list(
            iter_requests(spec)
        )
        for ordinal, dependency in enumerate(complete):
            record = (
                dependency.request.pair if pairwise else dependency.request.response
            )
            assert record is not None
            identifier = (
                record.pair_uid if isinstance(record, PairTask) else record.item_id
            )
            assert identifier == f"{ordinal // group_count:03d}"

        starts = {0, len(complete), len(complete) + 1}
        starts.update(
            record * group_count + group
            for record in (127, 128, 129, 255, 256)
            for group in range(group_count)
        )
        for start in sorted(starts):
            assert (
                list(prepared.iter_request_dependencies(start_ordinal=start))
                == complete[start:]
            )


@pytest.mark.parametrize("capture", ["full", "minimal"])
@pytest.mark.parametrize("stage", ["before_request_creation", "after_claim"])
@pytest.mark.parametrize("change", ["same_length", "restored_mtime", "replacement"])
@pytest.mark.asyncio
async def test_source_mutation_before_submission_still_blocks_acceptance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capture: Literal["full", "minimal"],
    stage: str,
    change: str,
) -> None:
    spec, store, ledger = setup(tmp_path, capture=capture)
    adapter = MockAdapter(ledger)
    changed = False
    original = spec.input.path.read_bytes()
    signature = spec.input.path.stat()
    replacement = original.replace(b"Synthetic", b"Different")
    assert replacement != original and len(replacement) == len(original)
    active: PreparedRun | None = None
    original_get = PreparedRun._get  # pyright: ignore[reportPrivateUsage]

    def tracked_get(
        prepared: PreparedRun, kind: Literal["response", "pair"], identifier: str
    ) -> ResponseItem | PairTask:
        nonlocal active
        active = prepared
        return original_get(prepared, kind, identifier)

    monkeypatch.setattr(PreparedRun, "_get", tracked_get)

    def mutate(current: str, _: str) -> None:
        nonlocal changed
        if current != stage or changed:
            return
        if change == "replacement":
            if sys.platform == "win32" and capture == "minimal":
                assert active is not None
                source_id = next(
                    source.source_id
                    for source in active.manifest.sources
                    if source.role == "response"
                )
                # Windows may prohibit replacing an open file. Release only the
                # test target's reader so the replacement actually takes place.
                reader = active._handles.pop(source_id, None)  # pyright: ignore[reportPrivateUsage]
                if reader is not None:
                    reader.close()
            path = spec.input.path.with_suffix(".replacement")
            path.write_bytes(replacement)
            os.replace(path, spec.input.path)
        else:
            spec.input.path.write_bytes(replacement)
            if change == "restored_mtime":
                os.utime(
                    spec.input.path,
                    ns=(signature.st_atime_ns, signature.st_mtime_ns),
                )
        changed = True

    with pytest.raises(ExceptionGroup) as failure:
        await execute_run("001", store=store, adapter=adapter, spec=spec, fault=mutate)
    rejected, unexpected = failure.value.split(IntegrityError)
    assert rejected is not None and unexpected is None
    assert changed
    assert spec.input.path.read_bytes() == replacement
    if change == "restored_mtime":
        assert spec.input.path.stat().st_mtime_ns == signature.st_mtime_ns
    assert adapter.submit_calls == adapter.ledger.acceptance_count() == 0
    with Repository(store) as repository:
        assert not list(repository.iter_results("001"))


@pytest.mark.asyncio
async def test_changed_adapter_rendering_after_claim_cannot_reuse_prepared_bytes(
    tmp_path: Path,
) -> None:
    spec, store, ledger = setup(tmp_path)

    class ChangedRendering(MockAdapter):
        changed = False

        def prepare(
            self,
            spec: RunSpecification,
            dependency: RequestDependency,
            rendered: RenderedAssessmentPrompt,
        ) -> PreparedRequest:
            if self.changed:
                rendered = rendered.model_copy(update={"text": rendered.text + "!"})
            return super().prepare(spec, dependency, rendered)

    adapter = ChangedRendering(ledger)

    def mutate(stage: str, _: str) -> None:
        if stage == "after_claim":
            adapter.changed = True

    with pytest.raises(ExceptionGroup):
        await execute_run("001", store=store, adapter=adapter, spec=spec, fault=mutate)
    assert adapter.changed
    assert adapter.submit_calls == adapter.ledger.acceptance_count() == 0
    adapter.changed = False
    report = await recover_run("001", store=store, adapter=adapter, spec=spec)
    assert report.summary.successful_results == 1
    assert adapter.ledger.acceptance_count() == 1


@pytest.mark.parametrize("capture", ["full", "minimal"])
@pytest.mark.parametrize("change", ["rendered_text", "source"])
@pytest.mark.asyncio
async def test_adapter_mutation_during_preparation_cannot_change_commitments(
    tmp_path: Path, capture: Literal["full", "minimal"], change: str
) -> None:
    spec, store, ledger = setup(tmp_path, capture=capture)

    class MutatingPreparation(MockAdapter):
        changed = False

        def prepare(
            self,
            spec: RunSpecification,
            dependency: RequestDependency,
            rendered: RenderedAssessmentPrompt,
        ) -> PreparedRequest:
            if change == "rendered_text":
                rendered.text += " ADAPTER_CHANGED_PROMPT"
            prepared = super().prepare(spec, dependency, rendered)
            if change == "source":
                original = spec.input.path.read_bytes()
                changed = original.replace(b"Synthetic", b"Different")
                assert changed != original and len(changed) == len(original)
                spec.input.path.write_bytes(changed)
            self.changed = True
            return prepared

    adapter = MutatingPreparation(ledger)
    with pytest.raises(ExceptionGroup):
        await execute_run("001", store=store, adapter=adapter, spec=spec)
    assert adapter.changed
    assert adapter.submit_calls == adapter.ledger.acceptance_count() == 0
    with Repository(store) as repository:
        assert not list(repository.iter_attempts("001"))
        assert not list(repository.iter_submissions("001"))
        assert not list(repository.iter_results("001"))


@pytest.mark.parametrize(
    "change",
    [
        "rubric_description",
        "category_value",
        "generation",
        "grouping",
        "exemplar",
        "criterion",
        "metadata_order",
    ],
)
@pytest.mark.asyncio
async def test_configuration_mutated_after_preparation_is_rejected_at_claim(
    tmp_path: Path, change: str
) -> None:
    if change == "criterion":
        spec = specification(tmp_path / "inputs", responses=2, pairwise=True)
        spec.provider, spec.endpoint = "mock", "synthetic"
        spec.execution.concurrency = 1
    else:
        spec = guarded_spec(tmp_path / "inputs")
        if change == "metadata_order":
            assert spec.rubric is not None
            spec.rubric.traits = spec.rubric.traits[:1]
            first = spec.input.path.read_text(encoding="utf-8").splitlines(
                keepends=True
            )[0]
            spec.input.path.write_text(first, encoding="utf-8")

    class CapturingAdapter(MockAdapter):
        active_spec: RunSpecification | None = None

        def validate(self, spec: RunSpecification) -> None:
            super().validate(spec)
            self.active_spec = spec

    store = tmp_path / "store"
    adapter = CapturingAdapter(tmp_path / "ledger.sqlite3")
    with prepare_run(
        spec, store=store, run_id="001", capabilities=adapter.capabilities(spec)
    ):
        pass
    changed = False

    def mutate(stage: str, _: str) -> None:
        nonlocal changed
        if stage != "after_claim" or changed:
            return
        changed = True
        current = adapter.active_spec
        assert current is not None
        if change == "criterion":
            current.criteria[0].description += " A changed criterion."
        elif change == "rubric_description":
            assert current.rubric is not None
            current.rubric.traits[0].description += " Changed."
        elif change == "category_value":
            assert current.rubric is not None
            current.rubric.traits[0].levels[1].numeric_value = 4.0
        elif change == "generation":
            current.generation = GenerationSpec(temperature=0.5)
        elif change == "grouping":
            current.execution.grouping = "grouped"
        elif change == "metadata_order":
            digest = configuration_digest(current)
            exemplar = current.exemplars[0]
            exemplar.metadata = dict(reversed(list(exemplar.metadata.items())))
            assert configuration_digest(current) == digest
        else:
            current.exemplars[0].text += " Changed."

    with pytest.raises(ExceptionGroup):
        await execute_run("001", store=store, adapter=adapter, spec=spec, fault=mutate)
    assert changed
    assert adapter.submit_calls == adapter.ledger.acceptance_count() == 0
    with Repository(store) as repository:
        assert not list(repository.iter_results("001"))
    report = await recover_run("001", store=store, adapter=adapter, spec=spec)
    expected = 2 if change == "criterion" else 1 if change == "metadata_order" else 6
    assert report.summary.successful_results == expected
    assert adapter.ledger.acceptance_count() == expected


@pytest.mark.parametrize("change", ["task_id", "trait_id", "duplicate", "reorder"])
def test_cached_parent_does_not_authorize_changed_retry_membership(
    tmp_path: Path, change: str
) -> None:
    spec = guarded_spec(tmp_path / "inputs")
    spec.execution.grouping = "grouped"
    with prepare_run(spec, store=tmp_path / "store", run_id="001") as prepared:
        dependency = next(prepared.iter_request_dependencies())
        seal_request(prepared, dependency)
        prepared.render_attempt(dependency)
        tasks = dependency.request.tasks
        if change == "task_id":
            tasks[0].task_id = "changed-task"
        elif change == "trait_id":
            tasks[0].trait_id = "changed-trait"
        elif change == "duplicate":
            dependency.request.tasks = (tasks[0], tasks[0])
        else:
            dependency.request.tasks = tuple(reversed(tasks))
        with pytest.raises(IntegrityError):
            prepared.render_attempt(dependency)
