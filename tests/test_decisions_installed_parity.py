"""Compare single Python runs and packed console runs through the pinned SDKs."""

from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import pytest
from test_decisions_contracts import pair_spec
from test_decisions_lifecycle import native_spec

from judgmentllm import AutoRequestGroupingV1, RunSpecV4, export_run_results
from judgmentllm.decisions.provenance import export_decisions_provenance


def _grouped(spec: RunSpecV4, maximum: int) -> RunSpecV4:
    return spec.model_copy(
        update={
            "execution": spec.execution.model_copy(
                update={
                    "request_grouping": AutoRequestGroupingV1(
                        mode="auto", max_questions_per_request=maximum
                    )
                }
            )
        }
    )


def _run(spec: RunSpecV4, root: Path, *, console: bool) -> tuple[str, int]:
    root.mkdir()
    config = root / "run.json"
    config.write_text(spec.model_dump_json(exclude_unset=True), encoding="utf-8")
    store = root / "store"
    env = {
        key: os.environ[key]
        for key in (
            "PATH",
            "LANG",
            "LC_ALL",
            "SystemRoot",
            "WINDIR",
            "TEMP",
            "TMP",
            "TMPDIR",
        )
        if key in os.environ
    }
    env.update(
        HOME=str(root),
        PYTHONPATH=str(Path(__file__).parent / "fixtures/decisions_parity"),
        DECISIONS_PARITY_PROVIDER=spec.provider,
        PYTHONDONTWRITEBYTECODE="1",
    )
    if spec.provider == "openai":
        env.update(
            OPENAI_API_KEY="synthetic-decisions-key", DECISIONS_FIXTURE_ROOT=str(root)
        )
        forward_log = root / "forwarding.jsonl"
    elif spec.provider == "typesafe":
        env.update(
            TYPESAFE_API_KEY="synthetic-jev-key",
            JLLM_TYPESAFE_FIXTURE_ROOT=str(root),
            JLLM_TYPESAFE_FIXTURE_FAULT="",
            JLLM_TYPESAFE_FIXTURE_RESPONSE="clean",
        )
        forward_log = root / "forwards"
    else:
        assert spec.provider == "gemini"
        env.update(
            GOOGLE_API_KEY="SYNTHETIC_CLI_GEMINI_KEY",
            JLLM_GEMINI_DECISIONS_ROOT=str(root),
            JLLM_GEMINI_DECISIONS_FAULT="",
            JLLM_GEMINI_DECISIONS_SCENARIO="clean",
            GOOGLE_GENAI_HTTP_CLIENT="httpx",
        )
        forward_log = root / "forwards"
    if console:
        executable = Path(sys.executable).with_name(
            "judgmentllm.exe" if os.name == "nt" else "judgmentllm"
        )
        command = [
            str(executable),
            "run",
            "--config",
            str(config),
            "--store",
            str(store),
            "--json",
        ]
    else:
        command = [
            sys.executable,
            "-c",
            "import sys; from judgmentllm import run_live; "
            "print(run_live(sys.argv[1], store=sys.argv[2]).model_dump_json())",
            str(config),
            str(store),
        ]
    result = subprocess.run(
        command, cwd=root, env=env, capture_output=True, text=True, timeout=30
    )
    assert "Error in sitecustomize" not in result.stderr
    assert result.returncode == 0, (result.stdout, result.stderr)
    return str(json.loads(result.stdout)["run_id"]), len(
        forward_log.read_text().splitlines()
    )


def _rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _observe(
    spec: RunSpecV4, root: Path, sizes: tuple[int, ...], *, console: bool
) -> tuple[dict[str, tuple[object, ...]], int]:
    presentation_count = 1 if spec.task_kind == "rubric" else 4
    run_id, forwards = _run(spec, root, console=console)
    wire_rows = _rows(root / "wire.jsonl")
    wire = {frozenset(row["keys"]): row for row in wire_rows}
    assert len(wire_rows) == len(wire) == forwards == presentation_count * len(sizes)
    store = root / "store"
    export = export_run_results(run_id, store=store)
    results = {r["task_id"]: r for r in _rows(Path(export.files[0].path))}
    assert export.row_count == len(results) == 12

    bundle = export_decisions_provenance(run_id, store=store).manifest
    (run_root,) = (store / "runs").iterdir()
    companions: dict[str, list[dict[str, Any]]] = {}
    for file in bundle.files:
        if file.name.endswith(".jsonl"):
            assert file.artifact.relative_path is not None
            companions[file.name] = _rows(run_root / file.artifact.relative_path)
    physical = {r["identity"]["attempt_id"]: r for r in companions["physical.jsonl"]}
    assert len(companions["physical.jsonl"]) == len(physical) == forwards
    assert bundle.totals.known_invocations == forwards
    assert bundle.usage_high_water_mark == forwards
    assert bundle.totals.input_tokens == 120 * forwards
    assert all(p["selected_usage"]["input_tokens"] == 120 for p in physical.values())

    members: dict[str, tuple[object, ...]] = {}
    keys_by_attempt: dict[str, set[str]] = defaultdict(set)
    distinct_by_attempt: dict[str, set[str]] = defaultdict(set)
    presentations: dict[str, set[str | None]] = defaultdict(set)
    questions = companions["questions.jsonl"]
    distinct_trait = "05" if spec.task_kind == "rubric" else "02"
    assert len(questions) == 12
    assert len({q["question"]["key"] for q in questions}) == 12
    for row in questions:
        question, identity = row["question"], row["identity"]
        task, attempt = question["task_id"], identity["attempt_id"]
        result = results[task]
        assert row["answer"]["key"] == question["key"]
        assert row["status"] == result["status"] == "complete"
        assert question["trait_id"] == result["trait_id"]
        assert result["attempt_id"] == attempt
        assert result["request_uid"] == identity["request_uid"]
        assert physical[attempt]["identity"] == identity
        assert physical[attempt]["shared_input_sha256"] == row["shared_input_sha256"]
        assert result["input_tokens"] is None and result["output_tokens"] is None
        keys_by_attempt[attempt].add(question["key"])
        if result["trait_id"] == distinct_trait:
            distinct_by_attempt[attempt].add(question["key"])
        presentations[attempt].add(row["presentation_id"])
        members[task] = (
            question["key"],
            row["source_record_id"],
            row["source_presentation_id"],
            row["presentation_id"],
            result["trait_id"],
            result.get("pair_uid"),
            result.get("reversal_group"),
            result["confidence"],
        )
    assert members.keys() == results.keys()
    assert keys_by_attempt.keys() == physical.keys()
    counts_by_presentation: dict[str | None, list[int]] = defaultdict(list)
    for attempt, keys in keys_by_attempt.items():
        (presentation,) = presentations[attempt]
        counts_by_presentation[presentation].append(len(keys))
        observed = wire.pop(frozenset(keys))
        assert set(observed["distinct"]) == distinct_by_attempt[attempt]
        assert (
            physical[attempt]["shared_input_byte_length"]
            == observed["shared_input_bytes"]
        )
    assert not wire
    assert len(counts_by_presentation) == presentation_count
    assert all(
        tuple(sorted(counts)) == sizes for counts in counts_by_presentation.values()
    )

    if spec.task_kind == "rubric":
        assert {r["trait_id"] for r in results.values()} == {
            f"{i:02d}" for i in range(12)
        }
        for row in results.values():
            distinct = row["trait_id"] == distinct_trait
            index, mapped = (0.45, 1.85) if distinct else (1.55, 5.15)
            assert row["item_id"] == "000"
            assert row["provider_expected_index"] == pytest.approx(index)
            assert (
                row["primary_score"] == row["expected_value"] == pytest.approx(mapped)
            )
            assert row["primary_score_kind"] == "expected"
            assert row["category_label"] is None
            assert row["modal_category_label"] == ("00" if distinct else "02")
            probabilities = (0.65, 0.25, 0.1) if distinct else (0.1, 0.25, 0.65)
            assert [
                (p["level_label"], p["probability"]) for p in row["probabilities"]
            ] == list(zip(("00", "01", "02"), probabilities, strict=True))
            assert [level["numeric_value"] for level in row["resolved_levels"]] == [
                1,
                2,
                7,
            ]
    else:
        assert None not in counts_by_presentation
        assert {
            (r["trait_id"], r["repetition_index"], r["is_reversal"])
            for r in results.values()
        } == {
            (trait, repeat, reversal)
            for trait in ("01", "02", "03")
            for repeat in (0, 1)
            for reversal in (False, True)
        }
        r_export = export_run_results(run_id, store=store, format="csv", view="r")
        with Path(r_export.files[0].path).open(encoding="utf-8", newline="") as source:
            audit = {r["task_id"]: r for r in csv.DictReader(source)}
        assert audit.keys() == results.keys()
        for task, row in results.items():
            assert row["original_order"] == ["001", "002"]
            assert row["source_pair_uid"] == f"criterion-{row['trait_id']}"
            order = ["001", "002"] if row["is_reversal"] else ["002", "001"]
            assert row["presented_order"] == order
            distinct = row["trait_id"] == distinct_trait
            first, second = (0.8, 0.2) if distinct else (0.2, 0.8)
            assert row["winner_id"] == order[0 if distinct else 1]
            assert (row["p_left"], row["p_right"]) == (
                (first, second) if row["is_reversal"] else (second, first)
            )
            assert row["tie"] is False and row["p_tie"] is None
            assert (audit[task]["p_first"], audit[task]["p_second"]) == (
                str(first),
                str(second),
            )
    return members, sum(row["shared_input_bytes"] for row in wire_rows)


@pytest.mark.parametrize(
    "provider,endpoint,model",
    [
        ("openai", "decisions", "gpt-6-luna"),
        ("typesafe", "systemone", "jev-1.14.0"),
        ("gemini", "decisions", "gemini-decision-1"),
    ],
    ids=["openai", "typesafe", "gemini"],
)
def test_installed_decisions_packing_parity(
    tmp_path: Path, provider: str, endpoint: str, model: str
) -> None:
    for name, base, group_size in (
        ("r", native_spec(tmp_path / "rubric", traits=12, maximum=None), 12),
        ("p", pair_spec(tmp_path / "pair", maximum=None), 3),
    ):
        spec = RunSpecV4.model_validate(
            {
                **base.model_dump(),
                "provider": provider,
                "endpoint": endpoint,
                "model": model,
            }
        )
        single, single_bytes = _observe(
            spec, tmp_path / f"s-{name}", (1,) * group_size, console=False
        )
        grouped, grouped_bytes = _observe(
            _grouped(spec, 32), tmp_path / f"g-{name}", (group_size,), console=True
        )
        assert grouped == single
        assert grouped_bytes > 0
        assert single_bytes == grouped_bytes * group_size
        if spec.task_kind == "pairwise":
            split, split_bytes = _observe(
                _grouped(spec, 2), tmp_path / "split", (1, 2), console=True
            )
            assert split == single
            assert split_bytes == grouped_bytes * 2
