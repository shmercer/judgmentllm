"""Risk and admission cases protect the boundaries that selection could bypass."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from risk import COMPREHENSIVE, R_GATES, admit, classify, make_plan, required_steps


@pytest.mark.parametrize(
    "path,before,after,level",
    [
        ("docs/guide.md", "# Guide\nOld words", "# Guide\nNew words", 0),
        (
            "src/judgmentllm/native_prompts.py",
            'def output_instructions_v3(output):\n    instructions = ["JSON"]\n    return instructions[0]',
            'def output_instructions_v3(output):\n    instructions = ["raw JSON"]\n    return instructions[0]',
            1,
        ),
        (
            "src/judgmentllm/providers/gemini_developer.py",
            None,
            "def parse(): return 1",
            2,
        ),
        ("src/judgmentllm/providers/anthropic_response.py", "x=1", "x=2", 2),
        ("src/judgmentllm/providers/protocol.py", "x=1", "x=2", 3),
        ("src/judgmentllm/providers/ledger.py", "x=1", "x=2", 3),
        ("src/judgmentllm/providers/mock.py", "x=1", "x=2", 3),
        ("src/judgmentllm/providers/_assessment_schema.py", "x=1", "x=2", 3),
        ("src/judgmentllm/providers/__init__.py", "x=1", "x=2", 3),
        ("src/judgmentllm/providers/openai_cli.py", "x=1", "x=2", 3),
        ("src/judgmentllm/providers/unknown_shared.py", None, "x=1", 3),
        (
            "src/judgmentllm/providers/openai.py",
            "def _check_authority(): return True",
            "def _check_authority(): return False",
            3,
        ),
        (
            "src/judgmentllm/providers/anthropic_response.py",
            "reasoning = None",
            "reasoning = payload",
            3,
        ),
        ("src/judgmentllm/execution/core.py", "x=1", "x=2", 3),
        ("src/judgmentllm/persistence/recovery.py", "x=1", "x=2", 3),
        ("src/judgmentllm/models.py", "x=1", "x=2", 3),
        ("src/judgmentllm/r_exports.py", "x=1", "x=2", 3),
        ("src/judgmentllm/auth.py", "x=1", "x=2", 3),
        ("src/judgmentllm/execution/privacy.py", "x=1", "x=2", 3),
        ("pyproject.toml", "", "", 3),
        (".github/workflows/ci.yml", "", "", 3),
        ("ci/coverage-floor.json", "", "", 3),
        ("ci/risk.py", "", "", 3),
        ("ci/test-contract.json", "", "", 3),
        ("tests/conftest.py", "", "", 3),
        ("unknown.file", "", "", 3),
        ("src/judgmentllm/native_prompts.py", "x=1", "import os\nos.unlink('data')", 3),
        ("src/judgmentllm/templates/a.j2", "{{ item }}", "{{ other }}", 3),
        ("tests/test_prompts.py", "def test_a(): assert True", "def test_a(): pass", 3),
        ("docs/guide.md", "Keep", None, 3),
    ],
)
def test_risk_boundaries(
    path: str, before: str | None, after: str | None, level: int
) -> None:
    assert classify(path, before, after)[0] == level


@pytest.fixture
def repository(tmp_path: Path) -> tuple[Path, str]:
    source = tmp_path / "repo"
    source.mkdir()
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    (source / "README.md").write_text("old")
    return source, freeze(source)


def freeze(source: Path) -> str:
    subprocess.run(["git", "add", "."], cwd=source, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=CI fixture",
            "-c",
            "user.email=ci@example.invalid",
            "commit",
            "-qm",
            "Synthetic changes",
        ],
        cwd=source,
        check=True,
    )
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=source, text=True
    ).strip()


def test_mixed_adapter_and_shared_execution_use_higher_level(repository: Any) -> None:
    source, base = repository
    for name in (
        "src/judgmentllm/providers/gemini_developer.py",
        "src/judgmentllm/execution/core.py",
    ):
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x=1")
    head = freeze(source)
    plan = make_plan(source, base, head)
    assert plan["risk"] == 3
    assert plan["r_gates"] == R_GATES
    assert len(plan["lanes"]) == 3
    assert plan["resources"]


def test_actual_pr27_diff_receives_focused_level_one_plan() -> None:
    root = Path(__file__).resolve().parents[2]
    base = subprocess.check_output(
        ["git", "rev-parse", "91e4079"], cwd=root, text=True
    ).strip()
    head = "37604d14a94f19f7d47b626625c9d742fc72124c"
    plan = make_plan(root, base, head)
    assert plan["risk"] == 1, plan["changes"]
    assert len(plan["lanes"]) == 1
    assert not plan["coverage"] and not plan["r_gates"]
    assert not plan["resources"] and not plan["packaging"]
    assert "tests/test_anthropic_response.py" in plan["targets"]


@pytest.mark.parametrize(
    "path,old,new,level",
    [
        (
            "native_prompts.py",
            "Return only a JSON object matching this schema.",
            "Return exactly one raw JSON object matching this schema.",
            1,
        ),
        (
            "native_prompts.py",
            "Include every requested trait exactly once; do not add traits or fields.",
            "Include each requested trait exactly once; do not add traits or fields.",
            1,
        ),
        (
            "prompts.py",
            "Include every requested trait exactly once. Preserve category labels exactly.",
            "Include each requested trait exactly once. Preserve category labels exactly.",
            1,
        ),
        (
            "native_prompts.py",
            '"canonical_neutral"',
            '"projected_native"',
            3,
        ),
        ("native_prompts.py", '!= "text"', '!= "native"', 3),
        ("native_prompts.py", '== "together"', '== "scores_first"', 3),
        (
            "native_prompts.py",
            "not native and output.commitment_version == 5",
            "not native and output.commitment_version == 4",
            3,
        ),
        (
            "prompts.py",
            '{"length", "lower", "upper", "trim"}',
            '{"length", "lower", "upper", "attr"}',
            3,
        ),
        (
            "prompts.py",
            '"trait_id": {"const": trait.trait_id}',
            '"trait": {"const": trait.trait_id}',
            3,
        ),
        ("prompts.py", 'kind == "rubric"', 'kind == "pairwise"', 3),
        ("prompts.py", '== "scores_first"', '== "explanations_first"', 3),
        ("anthropic_prompts.py", 'mode="after"', 'mode="before"', 3),
    ],
)
def test_real_prompt_modules_only_designated_instruction_text_is_low_risk(
    path: str, old: str, new: str, level: int
) -> None:
    filename = "src/judgmentllm/" + path
    before = (Path(__file__).resolve().parents[2] / filename).read_text()
    assert old in before
    assert classify(filename, before, before.replace(old, new, 1))[0] == level


@pytest.mark.parametrize(
    "before,after,level",
    [
        ('return "JSON"', 'return "raw JSON"', 3),
        ('instructions = ["JSON"]', 'instructions = ["raw JSON"]', 1),
        (
            'instructions = ["JSON" if mode == "text" else "native"]',
            'instructions = ["raw JSON" if mode == "text" else "native"]',
            1,
        ),
        (
            'instructions = ["JSON" if mode == "text" else "native"]',
            'instructions = ["JSON" if mode == "native" else "native"]',
            3,
        ),
        (
            'instructions.append(schema["scores"])',
            'instructions.append(schema["results"])',
            3,
        ),
        (
            'instructions.append(json.dumps(schema, separators=(",", ":")))',
            'instructions.append(json.dumps(schema, separators=(";", ":")))',
            3,
        ),
        (
            'instructions.append({"scores": "JSON"})',
            'instructions.append({"results": "JSON"})',
            3,
        ),
        (
            "instructions.append(f\"Return {schema['scores']}\")",
            "instructions.append(f\"Return {schema['results']}\")",
            3,
        ),
    ],
)
def test_instruction_expressions_preserve_selection_keys_and_call_arguments(
    before: str, after: str, level: int
) -> None:
    prefix = "def output_instructions_v3(output):\n    "
    assert (
        classify("src/judgmentllm/native_prompts.py", prefix + before, prefix + after)[
            0
        ]
        == level
    )


@pytest.mark.parametrize(
    "predicate",
    [
        "native and output.commitment_version == 5",
        "not native and output.commitment_version == 4",
        "not native and output.commitment_version != 5",
    ],
)
def test_pr27_exception_does_not_admit_unreviewed_predicates(predicate: str) -> None:
    root = Path(__file__).resolve().parents[2]
    filename = "src/judgmentllm/native_prompts.py"
    before, after = (
        subprocess.check_output(
            ["git", "show", f"{ref}:{filename}"], cwd=root, text=True
        )
        for ref in ("91e4079", "37604d14a94f19f7d47b626625c9d742fc72124c")
    )
    after = after.replace("not native and output.commitment_version == 5", predicate)
    assert classify(filename, before, after)[0] == 3


@pytest.mark.parametrize(
    "insertion",
    [
        "    import os\n",
        "    if output.commitment_version == 5:\n        output.contract = None\n",
        "    if output.commitment_version == 5:\n        instructions[0] = str(output)\n",
    ],
)
def test_prompt_boundary_cannot_hide_new_calls_imports_or_mutation(
    insertion: str,
) -> None:
    before = "def output_instructions_v3(output):\n    instructions = ['JSON']\n    return instructions[0]\n"
    after = before.replace("    return", insertion + "    return")
    assert classify("src/judgmentllm/native_prompts.py", before, after)[0] == 3


def test_candidate_classifier_edits_cannot_lower_base_classification(
    repository: Any,
) -> None:
    source, base = repository
    path = source / "ci/risk.py"
    path.parent.mkdir()
    path.write_text("def classify(*args): return 0\n")
    head = freeze(source)
    plan = make_plan(source, base, head)
    assert plan["risk"] == 3
    assert plan["changes"][0]["reason"].startswith("verification")


def test_shared_provider_ledger_requires_high_risk_and_resource_evidence(
    repository: Any,
) -> None:
    source, base = repository
    path = source / "src/judgmentllm/providers/ledger.py"
    path.parent.mkdir(parents=True)
    path.write_text("synthetic_shared_ledger = True\n")
    plan = make_plan(source, base, freeze(source))
    assert plan["risk"] == 3 and plan["resources"]
    assert len(plan["lanes"]) == 3 and plan["r_gates"] == R_GATES


def test_stronger_request_and_complete_matrix_are_recorded(repository: Any) -> None:
    source, base = repository
    (source / "README.md").write_text("new")
    head = freeze(source)
    low = make_plan(source, base, head)
    assert low["risk"] == 0 and len(low["lanes"]) == 1
    assert not low["coverage"] and not low["resources"] and not low["r_gates"]
    strong = make_plan(source, base, head, minimum=4)
    assert [
        {key: lane[key] for key in ("id", "os", "python")} for lane in strong["lanes"]
    ] == COMPREHENSIVE
    assert all(lane["profile"] == "full" for lane in strong["lanes"])
    assert strong["coverage"] and strong["resources"] and strong["packaging"]
    assert strong["resource_execution"] == "local"
    assert all(
        "resources" not in required_steps(strong, lane["id"]) for lane in COMPREHENSIVE
    )
    assert all(
        f"r-{gate}" in required_steps(strong, "windows-3.11", execution_scope="local")
        for gate in R_GATES
    )
    assert all(
        "coverage" not in required_steps(strong, lane["id"])
        and not any(
            name.startswith("r-") for name in required_steps(strong, lane["id"])
        )
        for lane in COMPREHENSIVE
    )
    assert "coverage" not in required_steps(strong, "windows-3.11")


def test_moderate_adapter_gets_primary_coverage_and_affected_r(repository: Any) -> None:
    source, base = repository
    path = source / "src/judgmentllm/providers/gemini_developer.py"
    path.parent.mkdir(parents=True)
    path.write_text("def parse(): return 1\n")
    plan = make_plan(source, base, freeze(source))
    assert plan["risk"] == 2 and len(plan["lanes"]) == 3
    assert [lane["profile"] for lane in plan["lanes"]] == [
        "impacted",
        "platform-smoke",
        "platform-smoke",
    ]
    assert plan["r_gates"] == ["exports", "execution"]
    assert plan["coverage"] and not plan["resources"]
    assert plan["coverage_execution"] == plan["r_execution"] == "local"
    assert "coverage" not in required_steps(plan, "linux-3.11")
    assert {"coverage", "r-exports", "r-execution"} <= required_steps(
        plan, "linux-3.11", execution_scope="local"
    )


def test_manual_dispatch_resolves_main_and_keeps_affected_ci_gates(
    repository: Any,
    tmp_path: Path,
) -> None:
    source, base = repository
    subprocess.run(
        ["git", "update-ref", "refs/remotes/origin/main", base], cwd=source, check=True
    )
    path = source / "ci/control.py"
    path.parent.mkdir()
    path.write_text("synthetic_ci_control = True\n")
    head = freeze(source)
    output = tmp_path / "plan.json"
    completed = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "acceptance.py"),
            "plan",
            "--source",
            str(source),
            "--base",
            "refs/remotes/origin/main",
            "--head",
            head,
            "--risk",
            "3",
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    plan = json.loads(output.read_text())
    assert plan["base_sha"] == base and plan["source_sha"] == head
    assert plan["risk"] == 3 and plan["changes"][0]["path"] == "ci/control.py"
    assert plan["resources"] and plan["packaging"]
    assert "resources" not in required_steps(plan, "linux-3.11")
    assert "resources" in required_steps(plan, "linux-3.11", execution_scope="local")
    assert "sdist" in required_steps(plan, "linux-3.11")


def passing_receipts(plan: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "lane": lane["id"],
            "source_sha": plan["source_sha"],
            "plan_sha256": plan["plan_sha256"],
            "run_id": "run:1",
            "source_verified": True,
            "status": "passed",
            "phase": "complete",
            "execution_scope": "hosted",
            "test_profile": lane["profile"],
            "selection_sha256": "a" * 64,
            "steps": [
                {"name": name, "exit_code": 0}
                for name in required_steps(plan, lane["id"])
            ],
        }
        for lane in plan["lanes"]
    ]


@pytest.mark.parametrize(
    "fault",
    [
        "none",
        "missing",
        "duplicate",
        "failed",
        "skipped-job",
        "wrong-source",
        "wrong-plan",
        "wrong-run",
        pytest.param("unexpected-hosted-r", id="missing-r"),
        pytest.param("failed-hosted-r", id="failed-r"),
        pytest.param("unexpected-hosted-coverage", id="missing-coverage"),
        "unexpected-hosted-resources",
        "partial-quality",
        "unverified-source",
        "duplicate-step",
        "local-scope",
        "missing-scope",
        "boolean-step-code",
    ],
)
def test_admission_never_conceals_failed_or_omitted_requirements(
    repository: Any, fault: str
) -> None:
    source, base = repository
    (source / "README.md").write_text("new")
    plan = make_plan(source, base, freeze(source), minimum=3)
    receipts = passing_receipts(plan)
    if fault == "missing":
        receipts.pop()
    elif fault == "duplicate":
        receipts.append(copy.deepcopy(receipts[0]))
    elif fault in {"failed", "skipped-job"}:
        receipts[-1]["status"] = fault
    elif fault in {"wrong-source", "wrong-plan", "wrong-run"}:
        key = {
            "wrong-source": "source_sha",
            "wrong-plan": "plan_sha256",
            "wrong-run": "run_id",
        }[fault]
        receipts[0][key] = "stale"
    elif fault in {
        "unexpected-hosted-r",
        "failed-hosted-r",
        "unexpected-hosted-coverage",
        "unexpected-hosted-resources",
    }:
        name = {
            "unexpected-hosted-r": "r-execution",
            "failed-hosted-r": "r-execution",
            "unexpected-hosted-coverage": "coverage",
            "unexpected-hosted-resources": "resources",
        }[fault]
        receipts[0]["steps"].append(
            {"name": name, "exit_code": 1 if fault == "failed-hosted-r" else 0}
        )
    elif fault == "partial-quality":
        receipts[0]["phase"] = "quality"
    elif fault == "unverified-source":
        receipts[0]["source_verified"] = False
    elif fault == "duplicate-step":
        receipts[0]["steps"].append(copy.deepcopy(receipts[0]["steps"][0]))
    elif fault == "local-scope":
        receipts[0]["execution_scope"] = "local"
    elif fault == "missing-scope":
        receipts[0].pop("execution_scope")
    elif fault == "boolean-step-code":
        receipts[0]["steps"][0]["exit_code"] = False
    assert bool(admit(plan, receipts, "run:1")) == (fault != "none")


def test_malformed_receipt_is_a_failing_command(
    tmp_path: Path, repository: Any
) -> None:
    source, base = repository
    plan = make_plan(source, base, base)
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan))
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    (receipts / "receipt.json").write_text("{broken")
    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "acceptance.py"),
            "aggregate",
            "--plan",
            str(plan_path),
            "--source",
            str(source),
            "--base",
            base,
            "--head",
            base,
            "--receipts",
            str(receipts),
            "--output",
            str(tmp_path / "aggregate.json"),
        ],
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0


@pytest.mark.parametrize(
    "fault",
    [
        "none",
        "plan-tamper",
        "boolean-plan",
        pytest.param("hosted-r-step", id="missing-r"),
        "missing-receipt",
        "wrong-source",
    ],
)
def test_aggregate_reconstructs_trusted_plan_and_checks_artifacts(
    tmp_path: Path,
    repository: Any,
    fault: str,
) -> None:
    source, base = repository
    (source / "README.md").write_text("new")
    head = freeze(source)
    plan = make_plan(source, base, head, minimum=3)
    receipts = passing_receipts(plan)
    if fault == "plan-tamper":
        plan["risk"] = 0
    elif fault == "boolean-plan":
        plan["coverage"] = 1
    elif fault == "hosted-r-step":
        receipts[0]["steps"].append({"name": "r-execution", "exit_code": 0})
    elif fault == "missing-receipt":
        receipts.pop()
    elif fault == "wrong-source":
        receipts[0]["source_sha"] = base
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan))
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    for index, receipt in enumerate(receipts):
        lane = evidence / str(index)
        lane.mkdir()
        from selection_fixture import write_suite

        receipt["selection_sha256"] = write_suite(plan, receipt["lane"], lane / "suite")
        (lane / "receipt.json").write_text(json.dumps(receipt))
    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "acceptance.py"),
            "aggregate",
            "--plan",
            str(plan_path),
            "--source",
            str(source),
            "--base",
            base,
            "--head",
            head,
            "--risk",
            "3",
            "--run-id",
            "run:1",
            "--receipts",
            str(evidence),
            "--output",
            str(tmp_path / "aggregate.json"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert (result.returncode == 0) == (fault == "none"), result.stdout + result.stderr
