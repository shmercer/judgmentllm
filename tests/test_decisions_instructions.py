"""Exact V4 prose replaces defaults without acquiring structured authority."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any, cast

import pytest
from test_decisions_contracts import pair_spec
from test_decisions_lifecycle import native_spec
from test_decisions_preparation_boundaries import published_native
from test_decisions_processes import run_child
from test_preparation_rendering import specification
from test_v2_public_lifecycle import configuration

import judgmentllm.preparation as preparation
from judgmentllm.config import (
    QuestionPromptSpecV1,
    RunSpecV3,
    RunSpecV4,
    load_run_spec,
    validate_run_spec,
)
from judgmentllm.decisions.interpretation import normalize_answer
from judgmentllm.decisions.records import DecisionsLimitsV1
from judgmentllm.decisions.transport import (
    DecisionsContextV1,
    plan_source,
    prepare_request,
    render_question,
    serialize_request,
)
from judgmentllm.models import ResponseItem
from judgmentllm.persistence.artifacts import ArtifactStore
from judgmentllm.persistence.hashing import canonical_bytes
from judgmentllm.persistence.models import IntegrityError, ManifestReference
from judgmentllm.persistence.repository import Repository
from judgmentllm.preparation import PreparationError, open_prepared_run, prepare_run
from judgmentllm.preparation.decisions import DecisionsPreparedRun
from judgmentllm.preparation.manifest import ExecutableRunManifestV8
from judgmentllm.preview import RenderedDecisionsPreviewV1, preview_prompt
from judgmentllm.prompts import PromptError
from judgmentllm.providers.decisions_mock import MockDecisionsAdapter
from judgmentllm.transport import physical_identity
from scripts.version_compatibility_fixture import extract_fixture

DEFAULTS = {
    "rubric": "Score the shared essay on this trait using the rubric’s ordered levels and their descriptions. Base the assessment on evidence in the essay. Do not generate an explanation.",
    "pairwise": "Compare the two essays using this criterion. Choose the essay that is stronger on this criterion: essay_01 or essay_02. Do not choose a tie or include an explanation.",
}
OVERRIDE = " \tÉvaluez cette qualité.\r\nKeep {{ 7 * 7 }} literal. 雪\u2028  "


def configured(root: Path, kind: str, text: str | None = None) -> RunSpecV4:
    spec = native_spec(root) if kind == "rubric" else pair_spec(root)
    return RunSpecV4.model_validate(
        {**spec.model_dump(), "prompt": {"output_instructions": text}}
    )


@pytest.mark.parametrize("kind", ["rubric", "pairwise"])
def test_task_defaults_and_explicit_null_have_identical_sparse_bytes(
    tmp_path: Path, kind: str
) -> None:
    spec = configured(tmp_path, kind)
    omitted = RunSpecV4.model_validate({**spec.model_dump(), "prompt": {}})
    assert canonical_bytes(spec) == canonical_bytes(omitted)
    assert "output_instructions" not in spec.prompt.model_dump()
    assert "output_instructions" not in json.loads(spec.prompt.model_dump_json())
    assert set(spec.prompt.model_dump()) == {
        "file",
        "advanced",
        "limits",
        "exemplar_ids_by_trait",
    }
    trait = "00" if kind == "rubric" else "01"
    default = render_question(omitted, trait, None)
    assert default == render_question(spec, trait, None)
    assert default.endswith(DEFAULTS[kind]) and default.count(DEFAULTS[kind]) == 1
    with prepare_run(spec, store=tmp_path / "store", run_id="001") as prepared:
        assert isinstance(prepared.manifest, ExecutableRunManifestV8)
        assert prepared.manifest.question_renderer_version == 2


@pytest.mark.parametrize("kind", ["rubric", "pairwise"])
@pytest.mark.parametrize(
    "text", [OVERRIDE, "Use this exact instruction.", '{"tie":true}']
)
def test_override_is_literal_exact_replacement_without_a_hidden_append(
    tmp_path: Path, kind: str, text: str
) -> None:
    spec = configured(tmp_path, kind, text)
    trait = "00" if kind == "rubric" else "01"
    default = render_question(configured(tmp_path, kind), trait, None)
    actual = render_question(spec, trait, None)
    assert actual.encode() == (default[: -len(DEFAULTS[kind])] + text).encode()
    assert spec.prompt.output_instructions == text
    assert json.loads(spec.model_dump_json())["prompt"]["output_instructions"] == text
    assert DEFAULTS[kind] not in actual


@pytest.mark.parametrize(
    "value", ["", " \t\r\n", "\u2003\u00a0", True, 1, 1.5, b"text", [], {}, "\ud800"]
)
def test_override_rejects_blank_nontext_and_invalid_unicode(value: object) -> None:
    with pytest.raises(ValueError):
        QuestionPromptSpecV1.model_validate({"output_instructions": value})


@pytest.mark.parametrize("kind", ["rubric", "pairwise"])
def test_override_preserves_every_selected_exemplar_and_its_order(
    tmp_path: Path, kind: str
) -> None:
    spec = configured(tmp_path, kind, OVERRIDE)
    traits = (
        [t.trait_id for t in spec.rubric.traits]
        if spec.rubric
        else [c.trait_id for c in spec.criteria]
    )
    spec = RunSpecV4.model_validate(
        {
            **spec.model_dump(),
            "exemplars": [
                {"exemplar_id": "a", "text": "First exact example α."},
                {"exemplar_id": "b", "text": "Second exact example β."},
            ],
            "prompt": {
                "output_instructions": OVERRIDE,
                "exemplar_ids_by_trait": {t: ["b", "a"] for t in traits},
            },
        }
    )
    text = render_question(
        spec, traits[0], "{{ rubric }}\n{{ exemplars }}\n{{ output_instructions }}"
    )
    assert (
        text.index("Second exact example β.")
        < text.index("First exact example α.")
        < text.index(OVERRIDE)
    )
    for template in (
        "{{ exemplars }}\n{{ output_instructions }}",
        "{{ rubric }}\n{{ output_instructions }}",
        "{{ rubric }}\n{{ exemplars }}",
        "{{ rubric }}\n{{ exemplars }}\n{{ output_instructions }}\n{{ output_instructions }}",
    ):
        with pytest.raises(PromptError, match="each required|duplicated|protected"):
            render_question(spec, traits[0], template)
    advanced = RunSpecV4.model_validate(
        {
            **spec.model_dump(),
            "prompt": {
                **spec.prompt.model_dump(),
                "file": tmp_path / "template",
                "advanced": True,
            },
        }
    )
    with pytest.raises(PromptError, match="exemplar order"):
        render_question(
            advanced,
            traits[0],
            "{{ rubric }}\n{{ exemplar_items[1]['block'] }}\n{{ exemplar_items[0]['block'] }}\n{{ output_instructions }}",
        )


def test_override_obeys_render_and_physical_limits_without_truncation(
    tmp_path: Path,
) -> None:
    spec = configured(tmp_path, "rubric", "é" * 1000)
    limited = RunSpecV4.model_validate(
        {
            **spec.model_dump(),
            "prompt": {
                **spec.prompt.model_dump(),
                "limits": {"max_output_bytes": 1000},
            },
        }
    )
    with pytest.raises(ValueError, match="rendering safeguard"):
        render_question(limited, "00", None)
    record = ResponseItem(item_id="000", text="Synthetic shared essay.")
    base = RunSpecV4.model_validate(
        {**configured(tmp_path, "rubric").model_dump(), "execution": {}}
    )
    spec = RunSpecV4.model_validate(
        {**base.model_dump(), "prompt": {"output_instructions": "é" * 1000}}
    )
    context = DecisionsContextV1(service_identity="offline")
    first = next(plan_source(base, record, lambda _: record, context, None))
    ceiling = (
        len(serialize_request(first.shared_input, first.questions, base, context)) + 64
    )
    for limits in (
        DecisionsLimitsV1(max_body_bytes=ceiling),
        DecisionsLimitsV1(max_context_tokens=ceiling, counter="synthetic_utf8_v1"),
    ):
        scoped = DecisionsContextV1(service_identity="offline", limits=limits)
        assert len(list(plan_source(base, record, lambda _: record, scoped, None))) == 3
        with pytest.raises(ValueError, match="oversized"):
            list(
                plan_source(
                    spec,
                    record,
                    lambda _: record,
                    scoped,
                    None,
                )
            )
    assert spec.prompt.output_instructions == "é" * 1000


def test_mutated_instruction_intent_is_deeply_revalidated(tmp_path: Path) -> None:
    spec = configured(tmp_path, "rubric", OVERRIDE)
    spec.prompt.output_instructions = " \t"
    with pytest.raises(ValueError, match="nonblank"):
        validate_run_spec(spec)
    with pytest.raises(ValueError, match="extra_forbidden|Extra inputs"):
        QuestionPromptSpecV1.model_validate(
            {"output_instructions": OVERRIDE, "question_renderer_version": 1}
        )


def test_override_changes_question_and_wire_commitments_while_task_keys_stay_stable(
    tmp_path: Path,
) -> None:
    old, new = configured(tmp_path, "rubric"), configured(tmp_path, "rubric", OVERRIDE)
    record = ResponseItem(item_id="000", text="Synthetic shared essay.")
    context = DecisionsContextV1(service_identity="offline")
    dependencies = [
        next(plan_source(s, record, lambda _: record, context, None))
        for s in (old, new)
    ]
    wires = [
        prepare_request(
            s, d, physical_identity("001", d.request.request_uid, "attempt"), context
        )
        for s, d in zip((old, new), dependencies, strict=True)
    ]
    assert [q.key for q in dependencies[0].questions] == [
        q.key for q in dependencies[1].questions
    ]
    assert wires[0].task_ids == wires[1].task_ids
    assert dependencies[0].shared_input == dependencies[1].shared_input
    assert dependencies[0].dependency_sha256 != dependencies[1].dependency_sha256
    assert wires[0].payload_sha256 != wires[1].payload_sha256
    assert wires[0].prompt_sha256 != wires[1].prompt_sha256
    assert (
        wires[0].transport_commitment.configuration_sha256
        != wires[1].transport_commitment.configuration_sha256
    )
    assert (
        wires[0].transport_commitment.questions
        != wires[1].transport_commitment.questions
    )


@pytest.mark.parametrize("kind", ["rubric", "pairwise"])
def test_prose_never_relaxes_structured_ties_or_explanations(
    tmp_path: Path, kind: str
) -> None:
    spec = configured(tmp_path, kind, "Choose a tie and provide an explanation.")
    for patch in (
        {"allow_ties": True},
        {"execution": {**spec.execution.model_dump(), "explanations": True}},
    ):
        with pytest.raises(ValueError):
            RunSpecV4.model_validate({**spec.model_dump(), **patch})
    with prepare_run(spec, store=tmp_path / "store", run_id="001") as prepared:
        assert isinstance(prepared, DecisionsPreparedRun)
        dependency = next(prepared.iter_request_dependencies())
        attempt = next(prepared.repository.iter_attempts("001"))
        wire = prepared.prepare_transport_attempt(
            dependency,
            physical_identity("001", attempt.request_uid, attempt.attempt_id),
        )
        adapter = MockDecisionsAdapter(tmp_path / "ledger.sqlite3")
        response = adapter._response(wire, "local")  # pyright: ignore[reportPrivateUsage]
        answer = json.loads(response.raw)["answers"][0]
        for extra in (
            {"explanation": "Forbidden even when prose requests it."},
            {"tie": True},
        ):
            with pytest.raises(ValueError):
                normalize_answer(
                    {**answer, **extra},
                    dependency.questions[0],
                    spec,
                    wire,
                    "000" if kind == "rubric" else None,
                )
        if kind == "pairwise":
            for selection in (None, "tie"):
                with pytest.raises(ValueError):
                    normalize_answer(
                        {**answer, "selection": selection},
                        dependency.questions[0],
                        spec,
                        wire,
                        None,
                    )


@pytest.mark.parametrize("kind", ["rubric", "pairwise"])
@pytest.mark.parametrize("capture", ["full", "minimal"])
def test_override_survives_fresh_process_receipt_recovery_and_rejects_tampering(
    tmp_path: Path, kind: str, capture: str
) -> None:
    spec = configured(tmp_path, kind, OVERRIDE)
    spec = RunSpecV4.model_validate({**spec.model_dump(), "capture": capture})
    config = tmp_path / "config.json"
    config.write_bytes(canonical_bytes(spec))
    store = tmp_path / "store"
    with prepare_run(config, store=store, run_id="001") as prepared:
        assert isinstance(prepared.manifest, ExecutableRunManifestV8)
        manifest_sha = prepared.manifest_sha256
        requests = prepared.manifest.initial_transport_count
    crash = run_child(store, config, "after_receipt")
    assert crash.returncode == 73, crash.stderr
    for _ in range(2):
        recovered = run_child(store, config)
        assert recovered.returncode == 0, recovered.stderr
    entries = (store / "receiver.jsonl").read_text().splitlines()
    assert len(entries) == len(set(entries)) == requests
    with open_prepared_run("001", store=store) as reopened:
        assert isinstance(reopened, DecisionsPreparedRun)
        assert reopened.manifest_sha256 == manifest_sha
        assert all(
            q.instructions.endswith(OVERRIDE)
            for d in reopened.iter_request_dependencies()
            for q in d.questions
        )
    changed = RunSpecV4.model_validate(
        {
            **spec.model_dump(),
            "prompt": {"output_instructions": "Different instruction."},
        }
    )
    with pytest.raises(PreparationError):
        open_prepared_run(
            "001", store=store, spec=changed, use_frozen_snapshot=capture == "full"
        )
    config.write_bytes(canonical_bytes(changed))
    with pytest.raises(PreparationError):
        open_prepared_run("001", store=store)
    with Repository(store) as repository:
        assert repository.get_run("001").manifest_sha256 == manifest_sha
        assert repository.summary("001").results == (3 if kind == "rubric" else 12)
    assert (store / "receiver.jsonl").read_text().splitlines() == entries


@pytest.mark.parametrize("kind", ["rubric", "pairwise"])
def test_preview_uses_the_exact_independent_instruction_override(
    tmp_path: Path, kind: str
) -> None:
    spec = configured(tmp_path, kind, OVERRIDE)
    if kind == "rubric":
        preview = preview_prompt(spec, item_id="000", trait_id="00")
    else:
        with prepare_run(spec, store=tmp_path / "store", run_id="001") as prepared:
            first = next(prepared.iter_request_dependencies()).request.tasks[0]
        preview = preview_prompt(spec, pair_uid=first.pair_uid, trait_id="01")
    assert isinstance(preview, RenderedDecisionsPreviewV1)
    assert all(q["instructions"].endswith(OVERRIDE) for q in preview.questions)
    assert DEFAULTS[kind] not in preview.text


@pytest.mark.parametrize("version", [1, 2, 3])
def test_old_configuration_versions_do_not_acquire_the_override(
    tmp_path: Path, version: int
) -> None:
    if version == 1:
        spec = specification(tmp_path / "inputs", responses=1, traits=1)
    elif version == 2:
        spec = load_run_spec(configuration(tmp_path))
    else:
        from native_transport_fixture import MECHANISMS
        from native_transport_fixture import specification as native_source

        spec = load_run_spec(native_source(tmp_path, MECHANISMS[0]))
        assert isinstance(spec, RunSpecV3)
    raw = canonical_bytes(spec)
    for text in (None, OVERRIDE):
        data = spec.model_dump()
        data["prompt"]["output_instructions"] = text
        with pytest.raises(ValueError):
            type(spec).model_validate(data)
    assert canonical_bytes(spec) == raw


@pytest.mark.parametrize("kind", ["rubric", "pairwise"])
def test_genuine_renderer_one_sealed_runs_keep_original_bytes(
    tmp_path: Path, kind: str
) -> None:
    fixture = Path(__file__).parent / "fixtures/decisions"
    archive = tmp_path / "history.tar.gz"
    archive.write_bytes(
        base64.b64decode(
            (fixture / "question-renderer-v1.tar.gz.base64").read_bytes(), validate=True
        )
    )
    metadata = extract_fixture(
        archive, fixture / "question-renderer-v1.json", tmp_path / "history"
    )
    assert metadata["producer_commit"] == "23c855f60f0cbb61700793e764340c7651719b72"
    expected = metadata["runs"][kind]
    with open_prepared_run(
        kind, store=tmp_path / "history/store", use_frozen_snapshot=True
    ) as prepared:
        assert isinstance(prepared, DecisionsPreparedRun)
        assert isinstance(prepared.manifest, ExecutableRunManifestV8)
        assert prepared.manifest.question_renderer_version == 1
        assert prepared.manifest_sha256 == expected["manifest_sha256"]
        assert isinstance(prepared.spec, RunSpecV4)
        with pytest.raises(IntegrityError, match="sealed once"):
            prepared.repository.seal_run(
                kind,
                prepared.repository.get_manifest(kind),
                decisions_spec=prepared.spec,
            )
        for stored in expected["requests"]:
            dependency = prepared.dependency_for_request(stored["request_uid"])
            assert [q.instructions for q in dependency.questions] == stored["questions"]
            receipt = prepared.transport_for_attempt(stored["attempt_id"])
            wire = prepared.prepare_transport_attempt(dependency, receipt.identity)
            assert (wire.prompt_sha256, wire.payload_sha256) == (
                stored["prompt_sha256"],
                stored["payload_sha256"],
            )
        prepared.spec = RunSpecV4.model_validate(
            {**prepared.spec.model_dump(), "prompt": {"output_instructions": OVERRIDE}}
        )
        with pytest.raises(PreparationError, match="configuration or seal"):
            prepared.verify_compatibility()


def test_renderer_authority_cannot_be_downgraded_to_admit_new_prose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = configured(tmp_path, "rubric", OVERRIDE)
    with pytest.raises(ValueError, match="historical question renderer"):
        render_question(spec, "00", None, question_renderer_version=1)
    for value in (True, 0, 3):
        with pytest.raises(ValueError, match="renderer version"):
            render_question(
                spec, "00", None, question_renderer_version=cast(Any, value)
            )
    store = published_native(tmp_path, spec, monkeypatch)
    with Repository(store) as repository:
        manifest = preparation._load_manifest(repository, "001", unsealed=True)  # pyright: ignore[reportPrivateUsage]
        assert isinstance(manifest, ExecutableRunManifestV8)
        manifest = manifest.model_copy(update={"question_renderer_version": 1})
        artifacts = ArtifactStore(repository.run_directory("001"), create=False)
        artifact = artifacts.put(canonical_bytes(manifest), "application/json")
        with pytest.raises(IntegrityError, match="RunSpec V4"):
            repository.seal_run(
                "001", ManifestReference(artifact=artifact), decisions_spec=spec
            )
        assert repository.get_run("001").state == "created"
