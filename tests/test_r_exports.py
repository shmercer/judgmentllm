"""Derived R audit views preserve observed outcomes, orientation, and precision."""

from __future__ import annotations

import csv
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from fixture_artifacts import immutable_exports

from judgmentllm import (
    PairTask,
    PairwiseResult,
    RubricResult,
    Trait,
    normalize_pairwise_result,
    normalize_rubric_result,
    render_prompt,
)
from judgmentllm.models import PairwiseCriterion, ResponseItem
from judgmentllm.r_exports import export_pairwise_r_csv, export_rubric_r_csv


def pair_result(
    *, reversed_order: bool = False, winner: str = "001", probabilities: bool = True
) -> PairwiseResult:
    pair = PairTask(
        pair_id="logical-pair",
        pair_uid="pair-occurrence",
        left_item_id="001",
        right_item_id="002",
        trait_id="quality",
        presented_order=["002", "001"] if reversed_order else ["001", "002"],
    )
    return normalize_pairwise_result(
        pair,
        run_id="run",
        task_id="task",
        request_uid="request",
        attempt_id="attempt-1",
        winner_id=winner,
        p_left=0.7 if probabilities else None,
        p_right=0.3 if probabilities else None,
        provider="synthetic",
        endpoint="offline",
        model="fixture",
    )


def rubric_result() -> RubricResult:
    trait = Trait.model_validate(
        {
            "trait_id": "evidence",
            "name": "Evidence",
            "description": "Synthetic",
            "levels": [
                {"label": 1, "numeric_value": 1},
                {"label": 2, "numeric_value": 3},
                {"label": 3, "numeric_value": 8},
            ],
        }
    )
    return normalize_rubric_result(
        trait,
        run_id="run",
        task_id="rubric-task",
        request_uid="request",
        attempt_id="attempt-1",
        item_id="001",
        category_label="2",
        probabilities=[
            {"level_label": "1", "probability": 0.1},
            {"level_label": "2", "probability": 0.25},
            {"level_label": "3", "probability": 0.65},
        ],
        provider_expected_index=1.55,
    )


def rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


@pytest.mark.parametrize("reversed_order", [False, True])
@pytest.mark.parametrize("winner", ["001", "002"])
def test_presented_outcome_probabilities_and_leading_zero_ids(
    tmp_path: Path, reversed_order: bool, winner: str
) -> None:
    result = pair_result(reversed_order=reversed_order, winner=winner)
    path = tmp_path / "pairs.csv"
    assert export_pairwise_r_csv(path, iter([result])) == 1
    row = rows(path)[0]
    first, second = result.presented_order
    assert row["ID1"] == row["A_id"] == first
    assert row["ID2"] == row["B_id"] == second
    assert row["better_id"] == winner
    assert row["result"] == ("1" if winner == first else "0")
    assert row["winner_pos"] == ("1" if winner == first else "2")
    assert row["p_left"] == "0.7" and row["p_right"] == "0.3"
    assert row["p_first"] == ("0.3" if reversed_order else "0.7")
    assert row["p_second"] == ("0.7" if reversed_order else "0.3")
    assert row["left_item_id"] == "001" and row["right_item_id"] == "002"
    assert row["task_id"] == "task" and row["provider"] == "synthetic"


def test_ties_failures_missing_probabilities_and_all_attempts(tmp_path: Path) -> None:
    base = pair_result()
    tie = PairwiseResult.model_validate(
        {
            **base.model_dump(),
            "winner_id": None,
            "tie": True,
            "allow_ties": True,
            "p_left": 0.25,
            "p_right": 0.25,
            "p_tie": 0.5,
            "attempt_id": "attempt-tie",
        }
    )
    failure = PairwiseResult.model_validate(
        {
            **base.model_dump(),
            "winner_id": None,
            "status": "errored",
            "attempt_id": "attempt-failed",
        }
    )
    failed_observation = PairwiseResult.model_validate(
        {**base.model_dump(), "status": "invalid", "attempt_id": "attempt-invalid"}
    )
    no_probabilities = pair_result(probabilities=False).model_copy(
        update={"attempt_id": "attempt-no-probs"}
    )
    path = tmp_path / "audit.csv"
    assert (
        export_pairwise_r_csv(
            path, iter([base, tie, failure, failed_observation, no_probabilities])
        )
        == 5
    )
    audit = rows(path)
    assert len({row["task_id"] for row in audit}) == 1  # no automatic deduplication
    assert len({row["attempt_id"] for row in audit}) == 5
    assert (
        audit[1]["result"] == "0.5"
        and audit[1]["better_id"] == "NA"
        and audit[1]["winner_pos"] == "NA"
    )
    assert audit[1]["p_tie"] == "0.5" and audit[1]["tie"] == "TRUE"
    assert audit[2]["result"] == audit[2]["winner_pos"] == audit[2]["better_id"] == "NA"
    assert audit[2]["p_first"] == "0.7"  # never turn probability evidence into a winner
    assert audit[3]["better_id"] == "001" and audit[3]["result"] == "NA"
    assert (
        audit[4]["p_first"]
        == audit[4]["p_second"]
        == audit[4]["p_left"]
        == audit[4]["p_right"]
        == "NA"
    )


def test_rubric_view_retains_probabilities_mapping_and_unrounded_scores(
    tmp_path: Path,
) -> None:
    expected = rubric_result()
    trait = Trait.model_validate(
        {
            "trait_id": "organization",
            "name": "Organization",
            "description": "Synthetic",
            "levels": [
                {"label": "Low"},
                {"label": "Medium"},
                {"label": "High"},
                {"label": "Best"},
            ],
        }
    )
    category = normalize_rubric_result(
        trait,
        run_id="run",
        task_id="organization-task",
        request_uid="request",
        attempt_id="attempt-1",
        item_id="001",
        category_label="High",
    )
    path = tmp_path / "rubric.csv"
    assert export_rubric_r_csv(path, iter([expected, category])) == 2
    first, second = rows(path)
    assert first["response_id"] == first["item_id"] == "001"
    assert first["category_label"] == "2" and first["numeric_category_value"] == "3.0"
    assert (
        first["modal_category_label"] == "3"
        and first["modal_numeric_category_value"] == "8.0"
    )
    assert float(first["expected_value"]) == expected.expected_value == 6.05
    assert float(first["provider_expected_index"]) == 1.55
    assert first["primary_score_kind"] == "expected"
    assert json.loads(first["probabilities_json"]) == [
        p.model_dump() for p in expected.probabilities or []
    ]
    assert json.loads(first["resolved_levels_json"]) == [
        level.model_dump() for level in expected.resolved_levels
    ]
    assert (
        second["primary_score_kind"] == "category_code"
        and second["primary_score"] == "2.0"
    )
    assert (
        second["expected_value"]
        == second["probabilities_json"]
        == second["modal_category_label"]
        == "NA"
    )
    invalid = RubricResult.model_validate(
        {
            **category.model_dump(),
            "status": "invalid",
            "primary_score": None,
            "primary_score_kind": None,
        }
    )
    assert (
        export_rubric_r_csv(path, [invalid]) == 1
        and rows(path)[0]["primary_score"] == "NA"
    )


@pytest.mark.parametrize("kind", ["pairwise", "rubric"])
def test_r_exports_revalidate_mutations_copies_and_nested_data(
    tmp_path: Path, kind: str
) -> None:
    if kind == "pairwise":
        pair = pair_result()
        pair.presented_order[0] = "invalid-id"
        with pytest.raises(ValueError):
            export_pairwise_r_csv(tmp_path / "bad.csv", [pair])
        with pytest.raises(ValueError):
            export_pairwise_r_csv(
                tmp_path / "bad-copy.csv",
                [pair_result().model_copy(update={"winner_id": "invalid-id"})],
            )
    else:
        rubric = rubric_result()
        assert rubric.probabilities is not None
        rubric.probabilities[0].probability = 0.9
        with pytest.raises(ValueError):
            export_rubric_r_csv(tmp_path / "bad.csv", [rubric])
        with pytest.raises(ValueError):
            export_rubric_r_csv(
                tmp_path / "bad-copy.csv",
                [rubric_result().model_copy(update={"primary_score": 123})],
            )


def test_prompt_reversal_observed_winner_and_export_positions_agree(
    tmp_path: Path,
) -> None:
    result = pair_result(reversed_order=True)
    pair = PairTask.model_validate(
        {
            field: getattr(result, field)
            for field in PairTask.model_fields
            if hasattr(result, field)
        }
    )
    rendered = render_prompt(
        pair=pair,
        left=ResponseItem(item_id="001", text="Synthetic left"),
        right=ResponseItem(item_id="002", text="Synthetic right"),
        criterion=PairwiseCriterion(
            trait_id="quality", name="Quality", description="Synthetic comparison"
        ),
    )
    assert rendered.text.index("essay_01:\nSynthetic right") < rendered.text.index(
        "essay_02:\nSynthetic left"
    )
    from judgmentllm.output_contracts import (
        PairwiseOutputContract,
        validate_pairwise_contract_output,
    )

    assert rendered.output_commitment is not None
    assert isinstance(rendered.output_commitment.contract, PairwiseOutputContract)
    selected = validate_pairwise_contract_output(
        {"winner_id": "essay_02"}, rendered.output_commitment.contract
    )
    assert selected.winner_id == result.winner_id
    path = tmp_path / "reversed.csv"
    export_pairwise_r_csv(path, [result])
    row = rows(path)[0]
    assert row["ID1"] == result.presented_first == "002"
    assert row["ID2"] == result.presented_second == "001"
    assert row["better_id"] == result.winner_id == "001"
    assert row["winner_pos"] == "2" and row["result"] == "0"
    assert (
        float(row["p_first"]) == result.p_right
        and float(row["p_second"]) == result.p_left
    )


@pytest.mark.parametrize("kind", ["pairwise", "rubric"])
def test_r_views_stream_one_attempt_at_a_time(tmp_path: Path, kind: str) -> None:
    path = tmp_path / "stream.csv"
    consumed: list[int] = []
    if kind == "pairwise":

        def pair_records() -> Iterator[PairwiseResult]:
            assert path.exists()
            for index in range(100):
                consumed.append(index)
                yield pair_result().model_copy(
                    update={"attempt_id": f"attempt-{index}"}
                )

        assert export_pairwise_r_csv(path, pair_records()) == 100
    else:

        def rubric_records() -> Iterator[RubricResult]:
            assert path.exists()
            for index in range(100):
                consumed.append(index)
                yield rubric_result().model_copy(
                    update={"attempt_id": f"attempt-{index}"}
                )

        assert export_rubric_r_csv(path, rubric_records()) == 100
    assert consumed == list(range(100)) and len(rows(path)) == 100


@pytest.mark.parametrize("marker", ["", "bad\nmarker", 12])
def test_invalid_missing_marker(tmp_path: Path, marker: object) -> None:
    with pytest.raises(ValueError, match="na_string"):
        export_pairwise_r_csv(tmp_path / "pair.csv", [], na_string=marker)  # pyright: ignore[reportArgumentType]
    with pytest.raises(ValueError, match="na_string"):
        export_rubric_r_csv(tmp_path / "rubric.csv", [], na_string=marker)  # pyright: ignore[reportArgumentType]


def test_missing_marker_never_collides_silently(tmp_path: Path) -> None:
    result = pair_result().model_copy(update={"model": "NA"})
    with pytest.raises(ValueError, match="missing-value marker"):
        export_pairwise_r_csv(tmp_path / "bad.csv", [result])
    export_pairwise_r_csv(tmp_path / "okay.csv", [result], na_string="MISSING")
    assert rows(tmp_path / "okay.csv")[0]["model"] == "NA"
    with pytest.raises(ValueError, match="missing-value marker"):
        export_pairwise_r_csv(tmp_path / "numeric.csv", [pair_result()], na_string="1")
    with pytest.raises(ValueError, match="missing-value marker"):
        export_rubric_r_csv(
            tmp_path / "bad-rubric.csv",
            [rubric_result().model_copy(update={"explanation": "NA"})],
        )


def test_wrong_record_kinds_and_empty_streams(tmp_path: Path) -> None:
    assert export_pairwise_r_csv(tmp_path / "empty-pair.csv", iter(())) == 0
    assert export_rubric_r_csv(tmp_path / "empty-rubric.csv", iter(())) == 0
    assert not rows(tmp_path / "empty-pair.csv")
    with pytest.raises(ValueError, match="PairwiseResult"):
        export_pairwise_r_csv(tmp_path / "wrong.csv", [rubric_result()])  # pyright: ignore[reportArgumentType]
    with pytest.raises(ValueError, match="RubricResult"):
        export_rubric_r_csv(tmp_path / "wrong.csv", [pair_result()])  # pyright: ignore[reportArgumentType]


@pytest.fixture(scope="module")
def python_export_example(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    root = Path(__file__).resolve().parents[1]

    def generate(output: Path) -> None:
        subprocess.run(
            [
                sys.executable,
                str(root / "examples" / "export_r.py"),
                "--output",
                str(output),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )

    with immutable_exports(
        tmp_path_factory.mktemp("export-example"), generate
    ) as output:
        yield output


def test_python_export_example_and_optional_r_smoke(
    python_export_example: Path,
) -> None:
    """Retain the original test ID for the hosted Python export assertions."""
    assert len(rows(python_export_example / "pairwise_r.csv")) == 6
    assert len(rows(python_export_example / "rubric_r.csv")) == 3


@pytest.mark.r_interop
def test_python_export_example_passes_required_r_smoke(
    python_export_example: Path,
) -> None:
    root = Path(__file__).resolve().parents[1]
    executable = shutil.which("Rscript")
    if executable is None:
        if os.environ.get("JUDGMENTLLM_R_STRICT") == "1":
            pytest.fail("Required Rscript is unavailable")
        pytest.skip("Rscript is optional outside required local R validation")
    completed = subprocess.run(
        [
            executable,
            "--vanilla",
            str(root / "examples" / "r_interoperability.R"),
            str(python_export_example),
        ],
        env={**os.environ, "JUDGMENTLLM_R_STRICT": "1"},
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert "R interoperability smoke passed" in completed.stdout


def test_expected_scores_and_probabilities_are_not_rounded(tmp_path: Path) -> None:
    trait = Trait.model_validate(
        {
            "trait_id": "nonuniform",
            "name": "Nonuniform",
            "description": "Synthetic",
            "levels": [
                {"label": "a", "numeric_value": 1},
                {"label": "b", "numeric_value": 3},
                {"label": "c", "numeric_value": 8},
            ],
        }
    )
    probabilities = [
        0.123456789012345,
        0.234567890123456,
        1 - 0.123456789012345 - 0.234567890123456,
    ]
    result = normalize_rubric_result(
        trait,
        run_id="run",
        task_id="precise",
        request_uid="request",
        attempt_id="attempt",
        item_id="00001",
        probabilities=[
            {"level_label": level.label, "probability": probability}
            for level, probability in zip(trait.levels, probabilities, strict=True)
        ],
    )
    path = tmp_path / "precise.csv"
    export_rubric_r_csv(path, [result])
    row = rows(path)[0]
    assert float(row["expected_value"]) == result.expected_value
    assert result.expected_value is not None and result.expected_value != round(
        result.expected_value, 6
    )
    assert [
        entry["probability"] for entry in json.loads(row["probabilities_json"])
    ] == probabilities
    assert row["category_label"] == "NA" and row["response_id"] == "00001"


@pytest.mark.r_interop
def test_strict_r_mode_cannot_skip_missing_jsonlite(tmp_path: Path) -> None:
    executable = shutil.which("Rscript")
    if executable is None:
        pytest.skip("Rscript is optional outside required local R validation")
    script = Path(__file__).resolve().parents[1] / "examples" / "r_interoperability.R"
    harness = tmp_path / "without-jsonlite.R"
    harness.write_text(
        """
arguments <- commandArgs(trailingOnly = TRUE)
real_require_namespace <- requireNamespace
requireNamespace <- function(package, ...) {
  if (identical(package, "jsonlite")) return(FALSE)
  real_require_namespace(package, ...)
}
commandArgs <- function(trailingOnly = FALSE) {
  if (!trailingOnly) stop("Harness expects trailing arguments")
  arguments[-1L]
}
source(arguments[[1L]])
""",
        encoding="utf-8",
    )
    completed = subprocess.run(
        [executable, "--vanilla", str(harness), str(script), str(tmp_path)],
        env={**os.environ, "JUDGMENTLLM_R_STRICT": "1"},
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode != 0
    assert "Strict R interoperability requires jsonlite" in completed.stderr
    assert "R interoperability smoke passed" not in completed.stdout


@pytest.mark.r_interop
@pytest.mark.parametrize("corruption", ["missing_column", "probability_values"])
def test_strict_r_mode_rejects_invalid_export(tmp_path: Path, corruption: str) -> None:
    executable = shutil.which("Rscript")
    if executable is None:
        pytest.skip("Rscript is optional outside required local R validation")
    probe = subprocess.run(
        [
            executable,
            "--vanilla",
            "-e",
            'quit(status = if (requireNamespace("jsonlite", quietly = TRUE)) 0L else 42L)',
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if probe.returncode == 42:
        pytest.skip("jsonlite is optional outside required local R validation")
    assert probe.returncode == 0, probe.stderr
    root = Path(__file__).resolve().parents[1]
    subprocess.run(
        [
            sys.executable,
            str(root / "examples" / "export_r.py"),
            "--output",
            str(tmp_path),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    source = tmp_path / (
        "pairwise_r.csv" if corruption == "missing_column" else "rubric_r.csv"
    )
    with source.open(encoding="utf-8", newline="") as stream:
        content = list(csv.reader(stream))
    if corruption == "missing_column":
        content[0][content[0].index("p_first")] = "unrecognized_probability"
    else:
        probability_column = content[0].index("probabilities_json")
        content[2][probability_column] = json.dumps(
            [
                {"level_label": "1", "probability": 0.9},
                {"level_label": "2", "probability": 0.05},
                {"level_label": "3", "probability": 0.05},
            ]
        )
    with source.open("w", encoding="utf-8", newline="") as stream:
        csv.writer(stream).writerows(content)
    completed = subprocess.run(
        [
            executable,
            "--vanilla",
            str(root / "examples" / "r_interoperability.R"),
            str(tmp_path),
        ],
        env={**os.environ, "JUDGMENTLLM_R_STRICT": "1"},
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode != 0
    if corruption == "missing_column":
        assert "Missing required CSV columns: p_first" in completed.stderr
    else:
        assert "probabilities$probability" in completed.stderr
    assert "R interoperability smoke passed" not in completed.stdout
