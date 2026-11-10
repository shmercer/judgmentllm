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
            'def prompt(): return "JSON"',
            'def prompt(): return "raw JSON"',
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
    assert strong["lanes"] == COMPREHENSIVE
    assert strong["coverage"] and strong["resources"] and strong["packaging"]
    assert all(
        f"r-{gate}" in required_steps(strong, "windows-3.11") for gate in R_GATES
    )
    assert "coverage" not in required_steps(strong, "windows-3.11")


def test_moderate_adapter_gets_primary_coverage_and_affected_r(repository: Any) -> None:
    source, base = repository
    path = source / "src/judgmentllm/providers/gemini_developer.py"
    path.parent.mkdir(parents=True)
    path.write_text("def parse(): return 1\n")
    plan = make_plan(source, base, freeze(source))
    assert plan["risk"] == 2 and len(plan["lanes"]) == 1
    assert plan["r_gates"] == ["exports", "execution"]
    assert plan["coverage"] and not plan["resources"]


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
    assert "resources" in required_steps(plan, "linux-3.11")
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
        "missing-r",
        "failed-r",
        "missing-coverage",
        "partial-quality",
        "unverified-source",
        "duplicate-step",
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
    elif fault in {"missing-r", "missing-coverage"}:
        target = "r-execution" if fault == "missing-r" else "coverage"
        receipts[0]["steps"] = [
            item for item in receipts[0]["steps"] if item["name"] != target
        ]
    elif fault == "failed-r":
        next(item for item in receipts[0]["steps"] if item["name"] == "r-execution")[
            "exit_code"
        ] = 1
    elif fault == "partial-quality":
        receipts[0]["phase"] = "quality"
    elif fault == "unverified-source":
        receipts[0]["source_verified"] = False
    elif fault == "duplicate-step":
        receipts[0]["steps"].append(copy.deepcopy(receipts[0]["steps"][0]))
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
    "fault", ["none", "plan-tamper", "missing-r", "missing-receipt", "wrong-source"]
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
    elif fault == "missing-r":
        receipts[0]["steps"] = [
            item for item in receipts[0]["steps"] if item["name"] != "r-execution"
        ]
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
