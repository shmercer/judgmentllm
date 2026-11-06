"""V2 exact wire commitments, raw decoding, salvage and crash recovery."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_v2_public_lifecycle import configuration

from judgmentllm.config import RunSpecV2, load_run_spec
from judgmentllm.execution import execute_run
from judgmentllm.execution.retry import authorize_subset_retry
from judgmentllm.output_commitments import (
    OutputCommitmentV2,
    SchemaSelectionV2,
    build_output_commitment,
    resolve_schema_selection,
    validate_output_commitment,
    validate_schema_selection,
)
from judgmentllm.output_contracts import RubricOutputContract, serialize_output_schema
from judgmentllm.persistence.hashing import bytes_digest, canonical_bytes
from judgmentllm.persistence.repository import Repository
from judgmentllm.preparation import open_prepared_run, prepare_run
from judgmentllm.preparation.freezing import PreparationError
from judgmentllm.preparation.manifest import (
    ExecutableRunManifestV2,
    parse_executable_manifest,
)
from judgmentllm.prompts import RenderedPromptV2
from judgmentllm.providers import (
    MockAdapter,
    MockMemberScenario,
    MockScenario,
    PreparedRequestV2,
    ProviderError,
)
from judgmentllm.providers.ledger import MockAcceptanceLedger
from judgmentllm.providers.mock import MockPayloadV2
from judgmentllm.results import RubricResult


def setup(
    directory: Path, *, capture: str = "full", order: str = "scores_first"
) -> tuple[RunSpecV2, Path, Path]:
    path = configuration(directory, capture=capture, order=order)
    spec = load_run_spec(path)
    assert isinstance(spec, RunSpecV2)
    store, ledger = directory / "store", directory / "ledger.sqlite3"
    adapter = MockAdapter(ledger)
    with prepare_run(
        path, store=store, run_id="001", capabilities=adapter.capabilities(spec)
    ):
        pass
    return spec, store, ledger


@pytest.mark.asyncio
@pytest.mark.parametrize("order", ["together", "scores_first", "explanations_first"])
async def test_v2_wire_exact_ordered_schema_and_response(
    order: str, tmp_path: Path
) -> None:
    spec, store, ledger = setup(tmp_path, order=order)
    adapter = MockAdapter(ledger)
    with open_prepared_run(
        "001", store=store, spec=spec, capabilities=adapter.capabilities(spec)
    ) as prepared:
        assert isinstance(prepared.manifest, ExecutableRunManifestV2)
        assert prepared.manifest.output_selection.mechanism == "text"
        assert prepared.manifest.template.renderer_version == 2
        dependency = next(prepared.iter_request_dependencies())
        rendered = prepared.render(dependency)
        assert isinstance(rendered, RenderedPromptV2)
        wire = adapter.prepare(spec, dependency, rendered)
        assert isinstance(wire, PreparedRequestV2)
        payload = MockPayloadV2.model_validate_json(wire.payload)
        commitment = payload.output_commitment
        assert payload.protocol == "judgmentllm-mock-v2"
        assert commitment == wire.output_commitment == rendered.output_commitment
        schema_bytes = serialize_output_schema(commitment.contract)
        assert commitment.schema_text.encode() == schema_bytes
        assert commitment.schema_sha256 == bytes_digest(schema_bytes)
        assert schema_bytes.decode() in rendered.text
        acknowledgment = await adapter.submit(
            wire, submission_id="s", correlation_id="c"
        )
        response = await adapter.retrieve(wire, submission_id="s", correlation_id="c")
        assert response.observation_key == acknowledgment.observation_key
        observed = json.loads(response.raw)["output"]
        assert tuple(observed) == commitment.root_properties
        assert (
            tuple(observed["scores"][0]) == commitment.member_properties[0].properties
        )
        assert adapter.parse(response, strict_output=True).output == observed
        assert adapter.ledger.acceptance_count() == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "corruption",
    [
        "invalid_json",
        "duplicate_root_key",
        "duplicate_member_key",
        "duplicate_protocol",
        "missing_protocol",
        "unexpected_root",
        "missing_scores",
        "missing_explanations",
    ],
)
async def test_corrupt_original_raw_cannot_generate_scores_or_retry(
    tmp_path: Path, corruption: str
) -> None:
    spec, store, ledger = setup(tmp_path)
    scenario = MockScenario.model_validate({"wire_corruption": corruption})
    adapter = MockAdapter(ledger, scenario=scenario)
    report = await execute_run("001", store=store, adapter=adapter, spec=spec)
    assert report.summary.unsuccessful_results == 2
    assert report.summary.successful_results == 0
    assert adapter.submit_calls == 1
    await execute_run("001", store=store, adapter=adapter, spec=spec)
    assert adapter.submit_calls == 1
    assert adapter.ledger.acceptance_count() == 1
    with Repository(store) as repository:
        for result in repository.iter_results("001"):
            assert isinstance(result, RubricResult)
            assert result.primary_score is None
        assert len(tuple(repository.iter_attempts("001"))) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "order,outcome",
    [
        (order, outcome)
        for order in ("together", "scores_first", "explanations_first")
        for outcome in (
            "missing_explanation",
            "invalid_explanation",
            "duplicate_explanation",
        )
        if (order, outcome) != ("together", "duplicate_explanation")
    ],
)
async def test_v2_valid_sibling_salvage_and_explicit_subset_retry(
    tmp_path: Path, order: str, outcome: str
) -> None:
    spec, store, ledger = setup(tmp_path, order=order)
    scenario = MockScenario(
        members={"02": MockMemberScenario.model_validate({"outcome": outcome})},
        reverse_members=True,
    )
    adapter = MockAdapter(ledger, scenario=scenario)
    report = await execute_run("001", store=store, adapter=adapter, spec=spec)
    assert report.summary.successful_results == 1
    assert report.summary.unsuccessful_results == 1
    with Repository(store) as repository:
        original = tuple(repository.iter_results("001"))
        failed = next(result for result in original if result.status != "complete")
        assert failed.trait_id == "02"
    # Keep the original fixture hash stable; the subset still represents a fresh
    # authorized assessment, never an additional explanation-generation stage.
    attempt = authorize_subset_retry(
        "001",
        store=store,
        adapter=adapter,
        task_ids=[failed.task_id],
        operation_id="retry-v2",
        spec=spec,
    )
    with Repository(store) as repository:
        assert repository.attempt_task_ids("001", attempt.attempt_id) == (
            failed.task_id,
        )
    await execute_run("001", store=store, adapter=adapter, spec=spec)
    assert adapter.submit_calls == 2
    assert adapter.ledger.acceptance_count() == 2
    with Repository(store) as repository:
        assert len(tuple(repository.iter_results("001"))) == 3
        assert (
            tuple(repository.get_results_for_attempt("001", original[0].attempt_id))
            == original
        )


@pytest.mark.parametrize(
    "field",
    [
        "schema_text",
        "schema_sha256",
        "root_properties",
        "member_properties",
        "renderer_version",
        "commitment_version",
    ],
)
def test_exact_schema_commitment_rejects_mutation(tmp_path: Path, field: str) -> None:
    spec, store, ledger = setup(tmp_path)
    adapter = MockAdapter(ledger)
    with open_prepared_run("001", store=store, spec=spec) as prepared:
        dependency = next(prepared.iter_request_dependencies())
        rendered = prepared.render(dependency)
        assert isinstance(rendered, RenderedPromptV2)
        value = rendered.output_commitment.model_dump()
        value[field] = (
            "changed"
            if field == "schema_text"
            else "0" * 64
            if field == "schema_sha256"
            else ()
            if field in {"root_properties", "member_properties"}
            else 2.0
        )
        with pytest.raises(ValueError):
            OutputCommitmentV2.model_validate(value)
        altered = rendered.model_copy(
            update={
                "output_commitment": rendered.output_commitment.model_copy(
                    update={field: value[field]}
                )
            }
        )
        with pytest.raises(ProviderError):
            adapter.prepare(spec, dependency, altered)
        assert adapter.submit_calls == 0


def test_contract_membership_settings_and_pinned_selection(tmp_path: Path) -> None:
    spec, store, _ = setup(tmp_path)
    with open_prepared_run("001", store=store, spec=spec) as prepared:
        dependency = next(prepared.iter_request_dependencies())
        commitment = build_output_commitment(spec, dependency.request)
        changed = spec.model_copy(deep=True)
        changed.prompt.output_order = "explanations_first"
        with pytest.raises(ValueError):
            validate_output_commitment(commitment, changed, dependency.request)
        changed = spec.model_copy(deep=True)
        changed.prompt.structured_output = "text"
        with pytest.raises(ValueError):
            validate_output_commitment(commitment, changed, dependency.request)
        with pytest.raises(ValueError):
            validate_schema_selection(changed, commitment.selection)
        with pytest.raises(ValueError):
            validate_schema_selection(
                spec,
                commitment.selection.model_copy(
                    update={
                        "native_support": "unknown",
                        "reason": "native_unknown_text_selected",
                    }
                ),
            )
        with pytest.raises(PreparationError):
            prepared.render(
                dependency.model_copy(
                    update={
                        "request": dependency.request.model_copy(
                            update={
                                "trait_ids": tuple(
                                    reversed(dependency.request.trait_ids)
                                )
                            }
                        )
                    }
                )
            )
        assert serialize_output_schema(commitment.contract) != serialize_output_schema(
            build_output_commitment(
                changed.model_copy(
                    update={
                        "prompt": changed.prompt.model_copy(
                            update={"output_order": "explanations_first"}
                        )
                    }
                ),
                dependency.request,
            ).contract
        )


def test_schema_selection_strict_versions_and_dispatch(tmp_path: Path) -> None:
    spec, store, _ = setup(tmp_path)
    selection = resolve_schema_selection(spec)
    assert selection.native_support == "unsupported"
    strict = spec.model_copy(update={"strict_capabilities": True})
    assert resolve_schema_selection(strict) == selection
    for version in (True, 2.0, "2", 1):
        with pytest.raises(ValidationError):
            SchemaSelectionV2.model_validate(
                {**selection.model_dump(), "selection_version": version}
            )
    with pytest.raises(ValidationError):
        SchemaSelectionV2.model_validate({**selection.model_dump(), "policy": "native"})
    with open_prepared_run("001", store=store, spec=spec) as prepared:
        raw = canonical_bytes(prepared.manifest)
        assert isinstance(parse_executable_manifest(raw), ExecutableRunManifestV2)
        altered = json.loads(raw)
        altered["schema_version"] = 2.0
        with pytest.raises(ValueError):
            parse_executable_manifest(json.dumps(altered).encode())


CHILD = r"""
import asyncio, os, sys
from judgmentllm.config import load_run_spec
from judgmentllm.execution import execute_run
from judgmentllm.providers import MockAdapter, MockScenario, MockMemberScenario
configuration, store, ledger, stage = sys.argv[1:]
spec = load_run_spec(configuration)
adapter = MockAdapter(ledger, scenario=MockScenario(members={'02': MockMemberScenario(outcome='missing_explanation')}))
def fault(current, uid):
    if current == stage: os._exit(73)
report = asyncio.run(execute_run('001', store=store, adapter=adapter, spec=spec, fault=fault))
print(report.model_dump_json())
"""


@pytest.mark.parametrize("capture", ["full", "minimal"])
@pytest.mark.parametrize("order", ["together", "scores_first", "explanations_first"])
@pytest.mark.parametrize(
    "stage",
    [
        "after_acknowledgment_receipt",
        "after_receipt",
        "during_member_validation",
        "during_result_transaction",
        "after_result",
    ],
)
def test_v2_crash_atomicity_and_original_observation_recovery(
    tmp_path: Path, capture: str, order: str, stage: str
) -> None:
    _, store, ledger = setup(tmp_path, capture=capture, order=order)
    config_path = tmp_path / "run.json"

    def child(boundary: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                "-c",
                CHILD,
                str(config_path),
                str(store),
                str(ledger),
                boundary,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

    crashed = child(stage)
    assert crashed.returncode == 73, crashed.stderr
    with Repository(store) as repository:
        assert len(tuple(repository.iter_results("001"))) == (
            2 if stage == "after_result" else 0
        )
    recovered = child("")
    assert recovered.returncode == 0, recovered.stderr
    summary = json.loads(recovered.stdout)["summary"]
    assert summary["successful_results"] == summary["unsuccessful_results"] == 1
    assert MockAcceptanceLedger(ledger).acceptance_count() == 1
    with Repository(store) as repository:
        assert len(tuple(repository.iter_attempts("001"))) == 1
        assert len(tuple(repository.iter_results("001"))) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("capture", ["full", "minimal"])
@pytest.mark.parametrize("order", ["together", "scores_first", "explanations_first"])
async def test_unavailable_original_raw_never_becomes_invalid_result_or_resubmission(
    tmp_path: Path, capture: str, order: str
) -> None:
    spec, store, ledger = setup(tmp_path, capture=capture, order=order)
    adapter = MockAdapter(ledger)

    def fault(stage: str, request: str) -> None:
        if stage == "after_receipt":
            raise RuntimeError("synthetic interruption")

    with pytest.raises(ExceptionGroup):
        await execute_run("001", store=store, adapter=adapter, spec=spec, fault=fault)
    unavailable = MockAdapter(ledger, retrieval_available=False)
    report = await execute_run("001", store=store, adapter=unavailable, spec=spec)
    assert unavailable.submit_calls == 0
    assert unavailable.ledger.acceptance_count() == 1
    if capture == "minimal":
        assert report.summary.results == 0
        assert report.summary.unresolved_submissions == 1
    else:
        assert report.summary.successful_results == 2
    restored = MockAdapter(ledger)
    recovered = await execute_run("001", store=store, adapter=restored, spec=spec)
    assert recovered.summary.successful_results == 2
    assert restored.submit_calls == 0
    assert restored.ledger.acceptance_count() == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "grouping,groups,limit,expected",
    [
        ("independent", None, None, 2),
        ("grouped", None, None, 1),
        ("grouped", [["02"], ["01"]], None, 2),
        ("auto", None, 1, 2),
    ],
)
async def test_v2_grouping_uses_exact_planned_contract_membership(
    tmp_path: Path,
    grouping: str,
    groups: list[list[str]] | None,
    limit: int | None,
    expected: int,
) -> None:
    path = configuration(tmp_path)
    document = json.loads(path.read_text())
    document["execution"]["grouping"] = grouping
    if groups is not None:
        document["execution"]["groups"] = groups
    if limit is not None:
        document["execution"]["max_traits_per_request"] = limit
    path.write_text(json.dumps(document))
    spec = load_run_spec(path)
    assert isinstance(spec, RunSpecV2)
    adapter = MockAdapter(tmp_path / "ledger.sqlite3")
    store = tmp_path / "store"
    with prepare_run(
        path, store=store, run_id="001", capabilities=adapter.capabilities(spec)
    ) as prepared:
        dependencies = tuple(prepared.iter_request_dependencies())
        assert len(dependencies) == expected
        for dependency in dependencies:
            wire = adapter.prepare(spec, dependency, prepared.render(dependency))
            assert isinstance(wire, PreparedRequestV2)
            assert isinstance(wire.output_commitment.contract, RubricOutputContract)
            assert (
                tuple(
                    trait.trait_id for trait in wire.output_commitment.contract.traits
                )
                == dependency.request.trait_ids
            )
    report = await execute_run("001", store=store, adapter=adapter, spec=spec)
    assert report.summary.successful_results == 2
    assert adapter.submit_calls == expected


@pytest.mark.parametrize("capture", ["full", "minimal"])
def test_v2_changed_sources_fail_and_only_full_allows_explicit_snapshot(
    tmp_path: Path, capture: str
) -> None:
    spec, store, _ = setup(tmp_path, capture=capture)
    with open_prepared_run("001", store=store, spec=spec) as prepared:
        original = prepared.render(next(prepared.iter_request_dependencies()))
    source = tmp_path / "responses.jsonl"
    source.write_text(
        source.read_text().replace("PRIVATE_RESPONSE", "CHANGED_RESPONSE")
    )
    with pytest.raises(PreparationError):
        open_prepared_run("001", store=store, spec=spec)
    if capture == "full":
        with open_prepared_run(
            "001", store=store, spec=spec, use_frozen_snapshot=True
        ) as prepared:
            assert (
                prepared.render(next(prepared.iter_request_dependencies())) == original
            )
    else:
        with pytest.raises(PreparationError):
            open_prepared_run("001", store=store, spec=spec, use_frozen_snapshot=True)


@pytest.mark.parametrize(
    "scenario",
    [
        {"wire_corruption": "missing_explanations"},
        {"members": {"02": {"outcome": "duplicate_explanation"}}},
    ],
)
def test_incompatible_together_fixture_controls_fail_before_submit(
    tmp_path: Path, scenario: dict[str, object]
) -> None:
    spec, store, ledger = setup(tmp_path, order="together")
    adapter = MockAdapter(ledger, scenario=MockScenario.model_validate(scenario))
    with open_prepared_run("001", store=store, spec=spec) as prepared:
        dependency = next(prepared.iter_request_dependencies())
        with pytest.raises(ProviderError):
            adapter.prepare(spec, dependency, prepared.render(dependency))
        assert adapter.submit_calls == 0


def test_direct_builder_and_payload_reject_substituted_commitments(
    tmp_path: Path,
) -> None:
    spec, store, ledger = setup(tmp_path)
    adapter = MockAdapter(ledger)
    with open_prepared_run("001", store=store, spec=spec) as prepared:
        dependency = next(prepared.iter_request_dependencies())
        selection = resolve_schema_selection(spec)
        injected = selection.model_copy(update={"arbitrary_schema": {}})
        with pytest.raises(ValueError):
            build_output_commitment(spec, dependency.request, injected)
        wire = adapter.prepare(spec, dependency, prepared.render(dependency))
        assert isinstance(wire, PreparedRequestV2)
        payload = MockPayloadV2.model_validate_json(wire.payload)
        mutations: tuple[tuple[str, object], ...] = (
            ("output_schema", {}),
            ("explanations", False),
            ("allow_ties", True),
            ("task_kind", "pairwise"),
            ("members", ()),
        )
        for field, value in mutations:
            data = payload.model_dump()
            data[field] = value
            with pytest.raises(ValueError):
                MockPayloadV2.model_validate(data)
