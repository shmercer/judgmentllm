"""Realistic moderate changes, genuine coverage, and conservative policy faults."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import risk
from additions import additive_tests, independent_module, metadata_additions
from coverage_data import combine, prepare_config, seal
from orchestrator import Stage, choose, run
from preflight_provenance import receipt_provenance, source_record
from selection import inventory, manifest
from targeted_coverage import compare
from test_risk import freeze


@pytest.mark.parametrize(
    "addition",
    [
        "def test_added():\n    assert True\n",
        '@pytest.mark.parametrize("value", [1, 2])\ndef test_added(value):\n    assert value > 0\n',
    ],
)
def test_independent_additions_preserve_old_statements(addition: str) -> None:
    old = "import pytest\nFLAG = 1\ndef test_old():\n    assert FLAG == 1\n"
    assert additive_tests(old, old + addition)


@pytest.mark.parametrize(
    "addition",
    [
        "def test_old():\n    pass\n",
        "FLAG = 2\n",
        "import os\n",
        "@pytest.fixture\ndef test_added():\n    pass\n",
        "def test_added(x=print('side effect')):\n    pass\n",
        "@pytest.mark.parametrize('x', make_cases())\ndef test_added(x):\n    pass\n",
        "def test_added():\n    global FLAG\n    FLAG = 2\n",
        "def pytest_collection_modifyitems(items):\n    items.clear()\n",
        "def test_added(:\n    pass\n",
    ],
)
def test_disguised_changes_are_not_additive(addition: str) -> None:
    old = "FLAG = 1\ndef test_old():\n    assert FLAG == 1\n"
    assert not additive_tests(old, old + addition)
    assert not additive_tests(
        old, old.replace("== 1", "== 2") + "def test_new(): pass\n"
    )


def test_new_module_cannot_install_collection_controls() -> None:
    assert independent_module("import pytest\ndef test_example(): assert True\n")
    for text in (
        "pytestmark = []\ndef test_example(): pass",
        "import pytest_plugins\ndef test_example(): pass",
        "print('collect')\ndef test_example(): pass",
    ):
        assert not independent_module(text)


def test_metadata_exception_cannot_replace_parameters_or_markers() -> None:
    old = {
        "schema_version": 1,
        "modules": {"tests/test_x.py": {"test_old": {"[001]": ["serial"]}}},
    }
    new = json.loads(json.dumps(old))
    new["modules"]["tests/test_x.py"]["test_added"] = {"": []}
    assert metadata_additions(
        "ci/test-inventory.json", json.dumps(old), json.dumps(new)
    )
    new["modules"]["tests/test_x.py"]["test_old"]["[001]"] = []
    assert not metadata_additions(
        "ci/test-inventory.json", json.dumps(old), json.dumps(new)
    )
    assert not metadata_additions(
        "ci/test-inventory.json",
        json.dumps(old),
        '{"schema_version":1,"schema_version":1}',
    )


@pytest.mark.parametrize(
    "defensive", [False, True], ids=["adapter-positive", "defensive-negative"]
)
@pytest.mark.parametrize("untested", [False, True], ids=["covered", "uncovered"])
def test_realistic_level_two_change_executes_new_tests_and_targeted_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, defensive: bool, untested: bool
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    root = Path(__file__).resolve().parents[2]
    (source / "pyproject.toml").write_bytes((root / "pyproject.toml").read_bytes())
    module = "src/judgmentllm/providers/gemini_response.py"
    path = source / module
    path.parent.mkdir(parents=True)
    for package in (source / "src/judgmentllm", path.parent):
        (package / "__init__.py").write_text("")
    path.write_text('def parse(payload):\n    return payload["score"]\n')
    tests = source / "tests/test_gemini_response.py"
    tests.parent.mkdir()
    old_tests = 'import pytest\nfrom judgmentllm.providers.gemini_response import parse\ndef test_old():\n    assert parse({"score": 1}) == 1\n'
    tests.write_text(old_tests)
    policy_root = source / "ci"
    policy_root.mkdir()
    known = inventory(
        [{"nodeid": "tests/test_gemini_response.py::test_old", "markers": []}]
    )
    taxonomy = {
        "schema_version": 1,
        "domains": ["provider-sdk"],
        "modules": {
            "tests/test_gemini_response.py": {
                "domain": "provider-sdk",
                "cost": "quick",
                "platforms": ["linux", "win32", "darwin"],
                "reason": "actual independent parser assertions",
            }
        },
        "smoke": ["tests/test_gemini_response.py::test_old"],
        "mandatory_smoke": ["tests/test_gemini_response.py::test_old"],
        "extended": [],
        "platform_overrides": {},
        "essential": ["tests/test_gemini_response.py::test_old"],
        "providers": {"gemini": ["tests/test_gemini_response.py"]},
        "provider_sources": {module: "gemini"},
        "shared_sources": [module],
    }
    for name, value in (
        ("test-inventory.json", known),
        ("test-profiles.json", taxonomy),
    ):
        (policy_root / name).write_text(json.dumps(value))
    base = freeze(source)
    body = "def parse(payload):\n"
    if defensive:
        body += '    if not isinstance(payload.get("score"), int):\n        raise ValueError("invalid score")\n'
    if untested:
        body += '    if payload.get("unreachable_in_tests"):\n        raise ValueError("not tested")\n'
    body += '    return int(payload["score"])\n'
    path.write_text(body)
    added = 'def test_added():\n    assert parse({"score": 2}) == 2\n'
    if defensive:
        added += '@pytest.mark.parametrize("bad", [None, "bad"])\ndef test_invalid(bad):\n    with pytest.raises(ValueError, match="invalid score"):\n        parse({"score": bad})\n'
    tests.write_text(old_tests + added)
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join((str(source / "src"), str(root))),
        "PYTHONDONTWRITEBYTECODE": "1",
        "JUDGMENTLLM_CI_RUN_ID": "moderate-realistic",
        "JUDGMENTLLM_CI_GROUP": "ordinary",
        "JUDGMENTLLM_CI_ATTEMPT_ID": str(tmp_path),
    }
    collection = tmp_path / "collection.json"
    common = [
        sys.executable,
        "-B",
        "-m",
        "pytest",
        "-p",
        "ci.local.pytest_report",
        "tests",
        "-o",
        f"cache_dir={tmp_path / 'cache'}",
        f"--basetemp={tmp_path / 'temporary'}",
    ]
    collected = subprocess.run(
        [*common, "--collect-only", "--no-cov", "-q", f"--ci-report={collection}"],
        cwd=source,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert collected.returncode == 0, collected.stdout + collected.stderr
    items = json.loads(collection.read_text())["collected"]
    (policy_root / "test-inventory.json").write_text(json.dumps(inventory(items)))
    head = freeze(source)
    monkeypatch.setattr(risk, "SELECTION_ROOT", policy_root)
    plan = risk.make_plan(source, base, head)
    assert plan["risk"] == 2 and len(plan["hosted_tasks"]) == 3
    assert [lane["profile"] for lane in plan["lanes"]] == [
        "impacted",
        "platform-smoke",
        "platform-smoke",
    ]
    assert plan["impact"] == ["tests/test_gemini_response.py"]
    assert plan["coverage_mode"] == "targeted" and plan["coverage_targets"] == [module]
    assert risk.lane_profile(plan, "linux-3.11", "local") == "impacted"
    for platform in ("linux", "win32", "darwin"):
        selection = manifest(
            items,
            profile="platform-smoke",
            impacted=plan["impact"],
            include_r=False,
            include_resources=False,
            platform=platform,
            policy=taxonomy,
            known=known,
        )
        assert all(item["disposition"] == "required" for item in selection["cases"])
    receipt_file = tmp_path / "tests.json"
    data = tmp_path / ".coverage"
    env["COVERAGE_FILE"] = str(data)
    config = prepare_config(
        source,
        tmp_path,
        receipt_provenance(
            source_record(source),
            env["JUDGMENTLLM_CI_RUN_ID"],
            env["JUDGMENTLLM_CI_ATTEMPT_ID"],
        ),
        "ordinary",
    )
    result = subprocess.run(
        [
            *common,
            "-n",
            "2",
            "--dist=worksteal",
            "--max-worker-restart=0",
            "--cov-reset",
            "--cov=judgmentllm.providers.gemini_response",
            "--cov-branch",
            "--cov-fail-under=0",
            "--cov-context=test",
            "--cov-append",
            f"--cov-config={config}",
            "--ci-coverage",
            "--cov-report=",
            f"--ci-report={receipt_file}",
        ],
        cwd=source,
        env=env,
        capture_output=True,
        text=True,
        timeout=40,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    receipt = json.loads(receipt_file.read_text())
    assert {item["nodeid"] for item in receipt["collected"]} == {
        item["nodeid"] for item in items
    }
    group = seal(data, receipt, source, receipt["provenance"], "ordinary")
    combine(
        [group],
        {"ordinary": receipt},
        source,
        receipt["provenance"],
        tmp_path / "combined",
    )
    output = tmp_path / "coverage.json"
    from coverage_data import report

    previous = Path.cwd()
    try:
        os.chdir(source)
        report(tmp_path / "combined", source, output, [module])
    finally:
        os.chdir(previous)
    evaluation = compare(
        output, source, base, head, [module], root / "ci/coverage-floor.json"
    )
    assert evaluation["passed"] is not untested, evaluation
    assert evaluation["historical_coverage"] == "not-assessed"
    assert not untested or any(
        item["uncovered_changed_lines"] or item["uncovered_changed_branches"]
        for item in evaluation["files"]
    )


def test_scheduler_overlaps_independent_jobs_and_enforces_barrier(
    tmp_path: Path,
) -> None:
    active = threading.Barrier(2)
    completed: list[str] = []

    def cohort(name: str) -> dict[str, Any]:
        active.wait(timeout=5)
        completed.append(name)
        return {"exit_code": 0}

    def exclusive() -> dict[str, Any]:
        assert set(completed) == {"ordinary", "safe"}
        return {"exit_code": 0}

    result = run(
        [
            Stage("ordinary", lambda: cohort("ordinary")),
            Stage("safe", lambda: cohort("safe")),
            Stage("exclusive", exclusive, ("ordinary", "safe")),
        ],
        cpus=2,
        memory_bytes=0,
        output=tmp_path / "schedule.json",
        cancel=threading.Event(),
    )
    assert result["status"] == "passed"


def test_scheduler_failure_and_unqualified_default_are_honest(tmp_path: Path) -> None:
    cancel = threading.Event()
    result = run(
        [
            Stage("fail", lambda: {"exit_code": 9}),
            Stage("never", lambda: pytest.fail("must not start"), ("fail",)),
        ],
        cpus=1,
        memory_bytes=0,
        output=tmp_path / "failure.json",
        cancel=cancel,
    )
    assert result["status"] == "failed"
    assert result["stages"]["never"]["status"] == "not-executed"
    assert (
        choose("auto", certificate=None, machine={}, policy_hash="x")[0]
        == "conservative"
    )
    assert time.monotonic() > 0
