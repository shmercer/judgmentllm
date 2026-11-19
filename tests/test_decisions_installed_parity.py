"""Compare single Python runs and packed console runs through the pinned SDKs."""

from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import pytest
from test_decisions_contracts import pair_spec
from test_decisions_lifecycle import native_spec

from judgmentllm import AutoRequestGroupingV1, RunSpecV4, export_run_results
from judgmentllm.decisions.provenance import export_decisions_provenance
from judgmentllm.preparation import prepare_run


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
        PYTHONPATH=str(Path(__file__).parent / f"fixtures/{spec.provider}_decisions"),
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
    spec: RunSpecV4, root: Path, sizes: list[int], *, console: bool
) -> tuple[dict[str, tuple[object, ...]], int]:
    run_id, forwards = _run(spec, root, console=console)
    assert forwards == len(sizes)
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
    sizes_by_attempt: Counter[str] = Counter()
    presentations: dict[str, set[str | None]] = defaultdict(set)
    questions = companions["questions.jsonl"]
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
        sizes_by_attempt[attempt] += 1
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
    assert sorted(sizes_by_attempt.values()) == sizes
    assert all(len(items) == 1 for items in presentations.values())

    if spec.task_kind == "rubric":
        assert {r["trait_id"] for r in results.values()} == {
            f"{i:02d}" for i in range(12)
        }
        for row in results.values():
            assert row["item_id"] == "000"
            assert row["provider_expected_index"] == pytest.approx(1.55)
            assert row["primary_score"] == row["expected_value"] == pytest.approx(5.15)
            assert row["primary_score_kind"] == "expected"
            assert row["category_label"] is None and row["modal_category_label"] == "02"
            assert [
                (p["level_label"], p["probability"]) for p in row["probabilities"]
            ] == [("00", 0.1), ("01", 0.25), ("02", 0.65)]
            assert [level["numeric_value"] for level in row["resolved_levels"]] == [
                1,
                2,
                7,
            ]
    else:
        assert len({p for items in presentations.values() for p in items}) == 4
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
            if row["is_reversal"]:
                assert row["presented_order"] == ["001", "002"]
                assert (row["winner_id"], row["p_left"], row["p_right"]) == (
                    "002",
                    0.2,
                    0.8,
                )
            else:
                assert row["presented_order"] == ["002", "001"]
                assert (row["winner_id"], row["p_left"], row["p_right"]) == (
                    "001",
                    0.8,
                    0.2,
                )
            assert row["tie"] is False and row["p_tie"] is None
            assert (audit[task]["p_first"], audit[task]["p_second"]) == ("0.2", "0.8")
    return members, sum(p["shared_input_byte_length"] for p in physical.values())


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
    for name, base, sizes in (
        ("r", native_spec(tmp_path / "rubric", traits=12, maximum=None), [12]),
        ("p", pair_spec(tmp_path / "pair", maximum=None), [3] * 4),
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
            spec, tmp_path / f"s-{name}", [1] * 12, console=False
        )
        grouped, grouped_bytes = _observe(
            _grouped(spec, 32), tmp_path / f"g-{name}", sizes, console=True
        )
        assert grouped == single
        assert grouped_bytes > 0
        assert single_bytes == grouped_bytes * (12 // len(sizes))
        if spec.task_kind == "pairwise":
            with prepare_run(
                _grouped(spec, 2), store=tmp_path / "split", run_id="split"
            ) as prepared:
                dependencies = list(prepared.iter_request_dependencies())
                assert (
                    sorted(len(d.request.tasks) for d in dependencies)
                    == [1] * 4 + [2] * 4
                )
                assert {
                    t.task_id for d in dependencies for t in d.request.tasks
                } == single.keys()
