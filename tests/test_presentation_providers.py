"""Pure production builders and native schemas agree on positional wire IDs."""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from anthropic_fixture import wire_for as anthropic_wire
from gemini_fixture import wire_for as gemini_wire
from test_production_transport import wire_for as openai_wire
from vertex_fixture import wire_for as vertex_wire

from judgmentllm.io import read_results
from judgmentllm.native_schema import source_contract
from judgmentllm.output_contracts import (
    PairwiseOutputContract,
    generate_output_schema,
    validate_pairwise_contract_output,
)
from judgmentllm.results import PairwiseResult

ROUTES: list[tuple[Callable[..., Any], dict[str, Any]]] = [
    (openai_wire, {"endpoint": "responses"}),
    (openai_wire, {"endpoint": "chat.completions"}),
    (anthropic_wire, {}),
    (gemini_wire, {"mechanism": "gemini.generate_content.response_json_schema"}),
    (gemini_wire, {"mechanism": "gemini.generate_content.response_schema"}),
    (vertex_wire, {"mechanism": "vertex.generate_content.response_json_schema"}),
    (vertex_wire, {"mechanism": "vertex.generate_content.response_schema"}),
]


@pytest.mark.parametrize("factory,settings", ROUTES)
@pytest.mark.parametrize(
    "explanations,ties", [(False, False), (True, False), (False, True), (True, True)]
)
def test_production_native_and_text_contracts_agree(
    tmp_path: Path,
    factory: Callable[..., Any],
    settings: dict[str, Any],
    explanations: bool,
    ties: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "synthetic-presentation-project")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "global")
    monkeypatch.setenv("JUDGMENTLLM_VERTEX_AUTH_MODE", "api_key")
    text, *_ = factory(
        tmp_path,
        policy="text",
        pairwise=True,
        explanations=explanations,
        ties=ties,
        **settings,
    )
    native, *_ = factory(
        tmp_path,
        policy="native",
        probe=True,
        pairwise=True,
        explanations=explanations,
        ties=ties,
        **settings,
    )
    contract = text.output_commitment.contract
    assert isinstance(contract, PairwiseOutputContract)
    assert contract.presentation_version == 2
    assert contract == native.output_commitment.contract
    assert generate_output_schema(contract) == generate_output_schema(
        native.output_commitment.contract
    )
    projection = native.output_commitment.projection
    assert projection is not None
    assert generate_output_schema(
        source_contract(projection.source)
    ) == generate_output_schema(contract)
    assert (
        text.transport_commitment.presentation_version
        == native.transport_commitment.presentation_version
        == 2
    )
    assert text.output_commitment.schema_text in text.rendered_text
    for wire in (text, native):
        assert "essay_01:" in wire.rendered_text and "essay_02:" in wire.rendered_text
        assert "## Comparison Criterion" in wire.rendered_text
        assert wire.rendered_text.count("## Output Format") == 1
        if ties:
            assert "Ties are not allowed" not in wire.rendered_text
        else:
            assert "Ties are not allowed" in wire.rendered_text
        # The fixture essays themselves intentionally contain their own source IDs;
        # protected source text remains exact. Automatic headers/schema stay clean.
        assert "First response 001" not in wire.rendered_text
        assert "Second response 002" not in wire.rendered_text
        assert '"001"' not in wire.output_commitment.schema_text
        assert '"002"' not in wire.output_commitment.schema_text
    assert '"001"' not in projection.native_schema_text
    assert '"002"' not in projection.native_schema_text
    assert (
        "essay_01" in projection.sdk_config_fragment_text
        if hasattr(projection, "sdk_config_fragment_text")
        else "essay_01" in projection.format_fragment_text
    )
    raw: dict[str, object] = {"winner_id": "essay_01"}
    if ties:
        raw["tie"] = False
    if explanations:
        raw["explanation"] = "Specific evidence supports the first presented essay."
    assert (
        validate_pairwise_contract_output(json.dumps(raw), contract).winner_id
        == contract.pair.presented_order[0]
    )


def test_exact_examples_regenerate_without_drift(tmp_path: Path) -> None:
    subprocess.run(
        [
            sys.executable,
            str(Path(__file__).parents[1] / "examples/rendering_revision2.py"),
            "--output",
            str(tmp_path),
        ],
        check=True,
    )
    rows = list(read_results(tmp_path / "presentation_results.jsonl"))
    expected = Path(__file__).parents[1] / "examples/rendering-revision2"
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == {
        p.name: p.read_bytes()
        for p in expected.iterdir()
        if p.is_file() and p.suffix in {".txt", ".json", ".jsonl", ".csv"}
    }
    assert [row.winner_id for row in rows if isinstance(row, PairwiseResult)] == [
        "001"
    ] * 3
