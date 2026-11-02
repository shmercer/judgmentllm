"""Bundled-file integration through the shared offline preview interface."""

import subprocess
import sys
from pathlib import Path

import pytest

from judgmentllm import iter_requests, iter_tasks, load_run_spec, preview_prompt
from judgmentllm.models import PairTask
from judgmentllm.planning import CapabilityProfile

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def test_bundled_previews_and_streamed_default_inputs() -> None:
    spec = load_run_spec(EXAMPLES / "run.yaml")
    prompt = preview_prompt(spec, item_id="essay_001", trait_id="organization")
    assert prompt.exemplar_ids == ["anchor_001", "anchor_002", "anchor_003"]
    assert "essay_001" in prompt.text
    assert len(list(iter_tasks(spec))) == 4
    assert len(list(iter_requests(spec))) == 4
    grouped = preview_prompt(EXAMPLES / "advanced.yaml", item_id="essay_002")
    assert "Trait organization" in grouped.text and "Trait evidence" in grouped.text
    pair = load_run_spec(EXAMPLES / "pairwise.yaml")
    assert len(list(iter_tasks(pair))) == 2
    assert len(list(iter_requests(pair))) == 2
    text = preview_prompt(pair, pair_uid="comparison_001_reverse").text
    assert text.index("First response essay_002") < text.index(
        "Second response essay_001"
    )


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"item_id": "essay_001", "pair_uid": "wrong"},
        {"item_id": "essay_001", "trait_id": "unknown"},
        {"item_id": "missing", "trait_id": "organization"},
    ],
)
def test_rubric_preview_invalid_selection(arguments: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        preview_prompt(
            EXAMPLES / "run.yaml",
            item_id=arguments.get("item_id"),
            trait_id=arguments.get("trait_id"),
            pair_uid=arguments.get("pair_uid"),
        )


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"pair_uid": "missing"},
        {"pair_uid": "comparison_001_original", "item_id": "essay_001"},
    ],
)
def test_pairwise_preview_invalid_selection(arguments: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        preview_prompt(
            EXAMPLES / "pairwise.yaml",
            item_id=arguments.get("item_id"),
            trait_id=arguments.get("trait_id"),
            pair_uid=arguments.get("pair_uid"),
        )


def test_missing_pair_references_and_unknown_criteria(tmp_path: Path) -> None:
    spec = load_run_spec(EXAMPLES / "pairwise.yaml")
    pair = PairTask(
        pair_id="pair",
        pair_uid="occ",
        left_item_id="essay_001",
        right_item_id="missing",
        trait_id="evidence",
    )
    path = tmp_path / "pairs.jsonl"
    path.write_text(pair.model_dump_json() + "\n")
    assert spec.pairs is not None
    spec.pairs.path = path
    with pytest.raises(ValueError, match="Missing response"):
        preview_prompt(spec, pair_uid="occ")
    pair.trait_id = "unknown"
    path.write_text(pair.model_dump_json() + "\n")
    with pytest.raises(ValueError, match="Unknown pair criterion"):
        preview_prompt(spec, pair_uid="occ")


def test_preview_known_capabilities_and_group_selection() -> None:
    spec = load_run_spec(EXAMPLES / "advanced.yaml")
    rendered = preview_prompt(
        spec,
        item_id="essay_001",
        trait_id="evidence",
        capabilities=CapabilityProfile(grouped=True, modes=("live",)),
    )
    assert "Trait evidence" in rendered.text and "Trait organization" in rendered.text


def test_python_offline_example_runs_without_credentials() -> None:
    completed = subprocess.run(
        [sys.executable, str(EXAMPLES / "offline.py")],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert (
        "Mapped expected score: 6.05; provider selection: None; mode: 3"
        in completed.stdout
    )
    assert "Pairwise occurrences: 4; unique IDs: 4" in completed.stdout
