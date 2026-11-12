"""Defensive version boundaries and historical wire reconstruction."""

# pyright: reportPrivateUsage=false
from __future__ import annotations

import copy
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from native_transport_fixture import context_for, specification
from test_durable_native_preparation import native_specification
from test_mock_provider import request
from test_presentation_revision import pair, rubric
from test_production_transport import case

from judgmentllm.native_schema_v2 import (
    NativeProjectionErrorV2,
    _pre_expansion_budget_v2,
)
from judgmentllm.output_commitments import (
    SchemaSelectionV2,
    build_output_commitment,
    build_output_contract,
    detached_output_commitment,
    validate_output_commitment,
)
from judgmentllm.output_contracts import PairwiseOutputContract, RubricOutputContract
from judgmentllm.preparation import prepare_run
from judgmentllm.preparation.freezing import PreparationError
from judgmentllm.production_transport import (
    prepare_production_request,
    production_context,
)
from judgmentllm.prompts import PromptError, _output_instructions_v2, render_prompt
from judgmentllm.providers import MockAdapter
from judgmentllm.transport import PreparedRequestV3, physical_identity


@pytest.mark.parametrize("version", [True, "2", 2.0, 0, 3])
def test_renderer_rejects_invalid_presentation_before_rendering(version: Any) -> None:
    with pytest.raises(PromptError, match="presentation version"):
        render_prompt(presentation_version=version)


@pytest.mark.parametrize("version", [True, 3])
def test_renderer_envelope_stays_independently_versioned(version: Any) -> None:
    with pytest.raises(PromptError, match="renderer version"):
        render_prompt(renderer_version=version)


def test_renderer_rejects_cross_revision_and_incompatible_envelopes() -> None:
    old = detached_output_commitment(PairwiseOutputContract(pair=pair()))
    with pytest.raises(PromptError, match="differs from its output commitment"):
        render_prompt(output_commitment=old, presentation_version=2)
    with pytest.raises(PromptError, match="exact output commitment"):
        render_prompt(output_commitment=old, renderer_version=1)
    with pytest.raises(PromptError, match="exact output commitment"):
        render_prompt(renderer_version=2)


@pytest.mark.parametrize("ties", [False, True])
@pytest.mark.parametrize("explanations", [False, True])
def test_historical_pair_instructions_remain_original_id_based(
    ties: bool, explanations: bool
) -> None:
    occurrence = pair()
    from judgmentllm.models import PairwiseCriterion, ResponseItem

    contract = PairwiseOutputContract(
        pair=occurrence, allow_ties=ties, explanations=explanations
    )
    rendered = render_prompt(
        pair=occurrence,
        left=ResponseItem(item_id=occurrence.left_item_id, text="Original A."),
        right=ResponseItem(item_id=occurrence.right_item_id, text="Original B."),
        criterion=PairwiseCriterion(
            trait_id=occurrence.trait_id, name="Historical", description="Compare."
        ),
        allow_ties=ties,
        explanations=explanations,
        renderer_version=2,
        output_commitment=detached_output_commitment(contract),
        template="{{ criterion }}\n{{ first_response }}\n{{ second_response }}\n{{ output_instructions }}",
    )
    assert "First response student_A034" in rendered.text
    assert "Second response student_B119" in rendered.text
    assert "essay_01" not in rendered.text
    assert rendered.output_commitment is not None
    assert '"student_A034"' in rendered.output_commitment.schema_text


@pytest.mark.parametrize("explanations", [False, True])
def test_historical_rubric_output_instructions_keep_exact_bytes(
    explanations: bool,
) -> None:
    contract = RubricOutputContract(
        traits=(rubric().traits[8],), explanations=explanations
    )
    commitment = detached_output_commitment(contract)
    expected = (
        "Return only a JSON object matching this schema. Include every requested "
        "trait exactly once. Preserve category labels exactly. "
        + (
            "In scores, put each category_label before its explanation. Explanations must be nonblank."
            if explanations
            else "Do not include explanations."
        )
        + "\n"
        + commitment.schema_text
    )
    assert _output_instructions_v2(commitment).encode("utf-8") == expected.encode(
        "utf-8"
    )


def test_v1_contract_admission_cannot_reinterpret_historical_requests(
    tmp_path: Path,
) -> None:
    adapter = MockAdapter(tmp_path / "ledger.sqlite3")
    spec, dependency, _ = request(adapter, pairwise=True)
    current = build_output_commitment(spec, dependency.request)
    assert validate_output_commitment(current, spec, dependency.request) == current
    with pytest.raises(ValueError, match="RunSpec v2"):
        build_output_contract(spec, dependency.request, presentation_version=1)
    with pytest.raises(ValueError, match="historical schema selection"):
        build_output_commitment(
            spec,
            dependency.request,
            SchemaSelectionV2(
                policy="text", native_support="unsupported", reason="explicit_text"
            ),
        )
    assert dependency.request.pair is not None
    historical = detached_output_commitment(
        PairwiseOutputContract(pair=dependency.request.pair)
    )
    with pytest.raises(ValueError, match="RunSpec v2"):
        validate_output_commitment(historical, spec, dependency.request)


def test_alias_budget_counts_wire_ids_and_preserves_historical_limits() -> None:
    historical = copy.deepcopy(PairwiseOutputContract(pair=pair()))
    object.__setattr__(
        historical.pair, "presented_order", ["x" * 500_001, "y" * 500_001]
    )
    with pytest.raises(NativeProjectionErrorV2) as failure:
        _pre_expansion_budget_v2(historical)
    assert failure.value.diagnostic.limit == "string_characters"
    current = historical.model_copy(update={"presentation_version": 2})
    _pre_expansion_budget_v2(current)


@pytest.mark.parametrize("endpoint", ["responses", "chat.completions"])
def test_original_production_text_receipts_reconstruct_without_new_marker(
    tmp_path: Path, endpoint: str
) -> None:
    spec, dependency, items = case(tmp_path, endpoint=endpoint, pairwise=True)
    context = production_context(spec)
    identity = physical_identity("001", dependency.request.request_uid, "attempt-001")

    def lookup(kind: str, identifier: str) -> Any:
        return dependency.request.pair if kind == "pair" else items[identifier]

    wire = prepare_production_request(
        spec, dependency, identity, context, lookup=lookup, presentation_version=1
    )
    receipt = wire.transport_commitment
    assert receipt.presentation_version == 1
    assert "presentation_version" not in receipt.model_dump()
    assert '"002"' in wire.output_commitment.schema_text
    reopened = prepare_production_request(
        spec, dependency, identity, context, lookup=lookup, frozen=receipt
    )
    assert reopened.payload == wire.payload
    assert reopened.transport_commitment == receipt


def test_new_marker_does_not_hide_missing_native_implementation_features(
    tmp_path: Path,
) -> None:
    mechanism = "openai.responses.text.format"
    context = context_for(mechanism)
    assert context.openai_catalog is not None
    implementation = replace(
        context.openai_catalog.implementations[0], features=("rubric_scores",)
    )
    context = replace(
        context,
        openai_catalog=replace(
            context.openai_catalog, implementations=(implementation,)
        ),
    )
    config = specification(tmp_path / "inputs", policy="auto")
    with prepare_run(
        config, store=tmp_path / "store", run_id="001", transport_context=context
    ) as prepared:
        dependency = next(prepared.iter_request_dependencies())
        attempt = prepared.repository.get_attempt_for_request(
            "001", dependency.request.request_uid
        )
        assert attempt is not None
        wire = prepared.prepare_transport_attempt(
            dependency,
            physical_identity("001", attempt.request_uid, attempt.attempt_id),
        )
        assert isinstance(wire, PreparedRequestV3)
        assert wire.transport_commitment.mechanism == "text"
        decision = wire.output_commitment.selection.openai_decision
        assert decision is not None
        assert decision.diagnostic.reason == ("projection_features_incompatible")


def test_prepared_transport_rejects_revision_from_another_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config, context = native_specification(tmp_path / "inputs", grouped=False)
    with prepare_run(
        config, store=tmp_path / "store", run_id="001", transport_context=context
    ) as prepared:
        dependency = next(prepared.iter_request_dependencies())
        attempt = prepared.repository.get_attempt_for_request(
            "001", dependency.request.request_uid
        )
        assert attempt is not None
        identity = physical_identity("001", attempt.request_uid, attempt.attempt_id)
        wire = prepared.prepare_transport_attempt(dependency, identity)
        old_contract = wire.output_commitment.contract.model_copy(
            update={"presentation_version": 1}
        )
        # Inject an adapter reconstruction defect at the dispatch boundary.
        mismatched = copy.deepcopy(wire)
        object.__setattr__(mismatched.output_commitment, "contract", old_contract)

        def mismatched_reconstruction(*args: object, **kwargs: object) -> Any:
            return mismatched

        monkeypatch.setattr(
            "judgmentllm.transport_dispatch.prepare_request",
            mismatched_reconstruction,
        )
        with pytest.raises(PreparationError, match="sealed manifest"):
            prepared.prepare_transport_attempt(dependency, identity)
