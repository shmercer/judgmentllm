"""Stale overlap evidence blocks admission independently of selected tests."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import acceptance
import risk
import selection
from local import policy as local_policy
from local import scheduling_contract as contract
from local.policy import read as read_policy
from local.serial_dependencies import closure, snapshot
from test_risk import freeze

SAFE = "tests/test_safe.py::test_safe"


def checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def review_execution(
    policy: dict[str, Any], source: Path, path: str, targets: list[str]
) -> None:
    """Explicit reviews belong only to these disposable synthetic repositories."""
    policy["serial_dependency_scope"]["dynamic_imports"][path] = {
        "source_sha256": checksum(source / path),
        "targets": targets,
        "reason": "Synthetic module review; no approval for repository source",
    }


@pytest.fixture
def project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, str, dict[str, Any], dict[str, Any]]:
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    contents = {
        "README.md": "Synthetic documentation\n",
        "uv.lock": "synthetic locked dependencies\n",
        "pyproject.toml": "[project]\nname='synthetic'\nversion='0.0.0'\n",
        "tests/test_safe.py": "import pytest\n\n\n@pytest.mark.serial\ndef test_safe():\n    from helper import VALUE\n\n    assert VALUE\n",
        "tests/helper.py": "VALUE = 1\n",
        "tests/conftest.py": '"""No shared mutation."""\n',
        "tests/test_other.py": "def test_other(): assert True\n",
        "src/judgmentllm/providers/openai_response.py": "def parse():\n    return 1\n",
    }
    for name, text in contents.items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    known = selection.inventory(
        [
            {"nodeid": SAFE, "markers": ["serial"]},
            {"nodeid": "tests/test_other.py::test_other", "markers": []},
        ]
    )
    policy: dict[str, Any] = {
        "schema_version": 1,
        "auto_default": "concurrent",
        "profiles": {
            "concurrent": {"ordinary_workers": 10, "r_workers": 2},
            "conservative": {"ordinary_workers": 12, "r_workers": 1},
        },
        "resource_sources": {
            "src/judgmentllm/providers/openai_response.py": {
                "selectors": [],
                "reason": "No synthetic measured workload",
            }
        },
        "serial": [
            {
                "selector": SAFE,
                "placement": "concurrency-safe-serial",
                "reason": "Private stores",
                "dependencies": {
                    "tests/test_safe.py": checksum(source / "tests/test_safe.py")
                },
            }
        ],
        "serial_shared_dependencies": {
            name: checksum(source / name)
            for name in contents
            if name != "README.md" and name != "tests/test_other.py"
        },
        "serial_dependency_scope": {
            "roots": ["src/judgmentllm"],
            "controls": ["uv.lock", "pyproject.toml"],
            "support_modules": ["tests/conftest.py"],
            "dynamic_imports": {},
            "reason": "Synthetic complete dependency boundary",
        },
        "isolation_reasons": {SAFE: "Independent private mutable state"},
    }
    taxonomy: dict[str, Any] = {
        "schema_version": 1,
        "domains": {"core-contract": "Synthetic"},
        "modules": {
            name: {
                "domain": "core-contract",
                "cost": "quick",
                "platforms": [],
                "reason": "Synthetic",
            }
            for name in known["modules"]
        },
        "essential": [],
        "extended": [],
        "smoke": [],
        "mandatory_smoke": [],
        "platform_overrides": {},
        "providers": {"openai": ["tests/test_other.py"]},
        "provider_sources": {"src/judgmentllm/providers/openai_response.py": "openai"},
        "shared_sources": ["src/judgmentllm/providers/openai_response.py"],
    }
    review_execution(policy, source, "tests/test_safe.py", ["tests/helper.py"])
    ci = source / "ci"
    ci.mkdir()
    metadata: list[tuple[str, dict[str, Any]]] = [
        ("test-inventory.json", known),
        ("test-profiles.json", taxonomy),
        ("local-policy.json", policy),
    ]
    for name, value in metadata:
        (ci / name).write_text(json.dumps(value))
    base = freeze(source)
    # This synthetic policy owns its own cases, not the real suite's inventory.
    monkeypatch.setattr(risk, "SELECTION_ROOT", ci)
    monkeypatch.setattr(selection, "HERE", ci)

    def read_policy(root: Path) -> dict[str, Any]:
        return copy.deepcopy(policy)

    monkeypatch.setattr(local_policy, "read", read_policy)
    return source, base, policy, known


def current_synthetic_lane() -> dict[str, str]:
    return {
        "id": "linux-3.11",
        "os": {
            "Linux": "ubuntu-24.04",
            "Windows": "windows-latest",
            "Darwin": "macos-latest",
        }[platform.system()],
        "python": f"{sys.version_info.major}.{sys.version_info.minor}",
    }


def test_unrelated_reconciled_test_addition_preserves_parallel_placement(
    project: Any,
) -> None:
    source, base, policy, known = project
    assert contract.build(source, base, base, policy, known)["status"] == "passed"
    (source / "tests/test_added.py").write_text("def test_added(): assert True\n")
    updated = copy.deepcopy(known)
    updated["modules"]["tests/test_added.py"] = {"test_added": {"": []}}
    (source / "ci/test-inventory.json").write_text(json.dumps(updated))
    after = contract.build(source, base, freeze(source), policy, known)
    assert after["status"] == "passed"
    assert after["placements"][SAFE]["placement"] == "concurrency-safe-serial"
    assert local_policy.serial_groups([SAFE], policy, source)[
        "concurrency-safe-serial"
    ] == [SAFE]


@pytest.mark.parametrize("level", range(5))
def test_all_levels_block_stale_evidence_when_safe_test_is_unselected(
    project: Any, tmp_path: Path, level: int
) -> None:
    source, base, policy, _ = project
    (source / "tests/helper.py").write_text("VALUE = 2\n")
    base = freeze(source)
    (source / "README.md").write_text("New narrative\n")
    plan = risk.make_plan(source, base, freeze(source), minimum=level)
    assert plan["risk"] == level
    assert plan["scheduling_contract"]["status"] == "requalification-required"
    assert "scheduling-contract" in risk.required_steps(
        plan, "linux-3.11", execution_scope="local"
    )
    with pytest.raises(ValueError, match="requalification-required"):
        contract.require(plan, source)
    plan["lanes"][0].update(current_synthetic_lane())
    output = tmp_path / "lane"
    assert (
        acceptance.run_lane(
            plan,
            source,
            output,
            "linux-3.11",
            "synthetic",
            execution_scope="local",
            local_schedule="conservative",
        )
        == 1
    )
    receipt = json.loads((output / "receipt.json").read_text())
    assert receipt["steps"] == [
        {"name": "scheduling-contract", "exit_code": 1, "error": receipt["error"]}
    ]
    assert not (output / "suite").exists()
    assert local_policy.serial_groups([SAFE], policy, source)[
        "globally-exclusive-serial"
    ] == [SAFE]


@pytest.mark.parametrize("level", range(5))
@pytest.mark.parametrize("fault", ["changed", "missing", "nonregular"])
def test_reviewed_control_overrides_block_stale_admission_at_all_levels(
    project: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    level: int,
    fault: str,
) -> None:
    import process
    import suite

    source, _, policy, known = project
    name = "ci/reviewed-control.json"
    control = source / name
    control.write_text('{"value": 1}\n')
    policy["serial_dependency_scope"]["controls"].append(name)
    policy["serial_shared_dependencies"][name] = checksum(control)
    historical = copy.deepcopy(policy["serial_shared_dependencies"])
    control.write_text('{"value": 2}\n')
    expected = checksum(control)
    policy["serial"][0]["dependencies"][name] = expected
    (source / "ci/local-policy.json").write_text(json.dumps(policy))
    # Planning and require must use the real, validated disk policy.
    monkeypatch.setattr(local_policy, "read", read_policy)
    validated = read_policy(source / "ci")
    reviewed = freeze(source)
    assert (
        contract.build(source, reviewed, reviewed, validated, known)["status"]
        == "passed"
    )
    if fault == "changed":
        control.write_text('{"value": 3}\n')
    elif fault == "missing":
        control.unlink()
    else:
        control.unlink()
        control.mkdir()
        (control / "payload.txt").write_text("A directory is not the reviewed file\n")
        assert control.is_dir()
    # Only unrelated prose changes in the planned interval, even at Level 0.
    base = freeze(source)
    (source / "README.md").write_text("Unrelated narrative\n")
    plan = risk.make_plan(source, base, freeze(source), minimum=level)
    assert plan["risk"] == level
    assert plan["local_policy"] == validated == read_policy(source / "ci")
    assert validated["serial_shared_dependencies"] == historical
    assert "tests/test_safe.py" not in plan["impact"]
    assert "scheduling-contract" in risk.required_steps(
        plan, "linux-3.11", execution_scope="local"
    )
    assert plan["scheduling_contract"]["status"] == "requalification-required"
    row = plan["scheduling_contract"]["placements"][SAFE]
    assert row["placement"] == "globally-exclusive-serial"
    assert row["differences"] == [
        {
            "path": name,
            "expected_sha256": expected,
            "actual_sha256": checksum(control) if fault == "changed" else None,
            "reason": "changed-dependency"
            if fault == "changed"
            else "missing-or-nonregular",
        }
    ]
    with pytest.raises(ValueError, match="requalification-required"):
        contract.require(plan, source)
    plan["lanes"][0].update(current_synthetic_lane())

    def unexpected(*arguments: object, **options: object) -> Any:
        pytest.fail("Stale scheduling reached suite allocation or child dispatch")

    monkeypatch.setattr(suite, "read_resources", unexpected)
    monkeypatch.setattr(process, "run", unexpected)
    monkeypatch.setattr(
        acceptance,
        "subprocess",
        SimpleNamespace(
            run=unexpected,
            STDOUT=subprocess.STDOUT,
            SubprocessError=subprocess.SubprocessError,
        ),
    )
    suite_output = tmp_path / "suite"
    with pytest.raises(ValueError, match="requalification-required"):
        suite.run(
            source=source,
            output=suite_output,
            targets=["tests"],
            workers=1,
            coverage=False,
            plan=plan,
            local_schedule="conservative",
        )
    assert not suite_output.exists()
    lane_output = tmp_path / "lane"
    assert (
        acceptance.run_lane(
            plan,
            source,
            lane_output,
            "linux-3.11",
            "synthetic-control",
            execution_scope="local",
            local_schedule="conservative",
        )
        == 1
    )
    receipt = json.loads((lane_output / "receipt.json").read_text())
    assert receipt["steps"] == [
        {"name": "scheduling-contract", "exit_code": 1, "error": receipt["error"]}
    ]
    assert not (lane_output / "suite").exists()
    assert local_policy.serial_groups([SAFE], validated, source)[
        "globally-exclusive-serial"
    ] == [SAFE]


def test_moderate_provider_edit_requires_gate_without_infrastructure_or_safe_selection(
    project: Any,
) -> None:
    source, base, _, _ = project
    (source / "src/judgmentllm/providers/openai_response.py").write_text(
        "def parse(): return 2\n"
    )
    plan = risk.make_plan(source, base, freeze(source))
    assert plan["risk"] == 2 and not plan["metadata_validation"]
    assert "tests/test_safe.py" not in plan["impact"]
    required = risk.required_steps(plan, "linux-3.11", execution_scope="local")
    assert "infrastructure" not in required and "scheduling-contract" in required
    with pytest.raises(ValueError, match="requalification-required"):
        contract.require(plan, source)


@pytest.mark.parametrize("level", range(5))
def test_planned_full_suite_blocks_staleness_before_evidence_allocation(
    project: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, level: int
) -> None:
    import suite

    source, base, _, _ = project
    (source / "tests/helper.py").write_text("VALUE = 2\n")
    plan = risk.make_plan(source, base, freeze(source), minimum=level)

    def unexpected() -> Any:
        pytest.fail("Stale scheduling reached evidence/resource allocation")

    monkeypatch.setattr(suite, "read_resources", unexpected)
    output = tmp_path / "suite"
    with pytest.raises(ValueError, match="requalification-required"):
        suite.run(
            source=source,
            output=output,
            targets=["tests"],
            workers=1,
            coverage=False,
            plan=plan,
            local_schedule="conservative",
        )
    assert not output.exists()


def test_unplanned_full_suite_is_explicitly_nonqualifying(
    project: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import process
    import suite

    source, _, _, _ = project
    (source / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\nmarkers=["serial: synthetic isolation"]\n'
    )
    contract_path = source / "ci/test-contract.json"
    contract_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "required_nodes": [SAFE, "tests/test_other.py::test_other"],
                "replacements": {},
            }
        )
    )
    freeze(source)
    monkeypatch.setattr(process, "systemd_available", lambda: False)
    monkeypatch.setenv(
        "JUDGMENTLLM_ALLOCATION_ROOT", str(tmp_path / "private-allocation")
    )
    monkeypatch.delenv("JUDGMENTLLM_ALLOCATION_TOKEN", raising=False)
    output = tmp_path / "unadmitted-suite"
    assert (
        suite.run(
            source=source,
            output=output,
            targets=["tests"],
            workers=1,
            coverage=False,
            contract=contract_path,
            local_schedule="conservative",
        )
        == 0
    )
    summary = json.loads((output / "summary.json").read_text())
    assert summary["status"] == "passed" and summary["manifest_complete"] is True
    assert summary["qualification"] is False and summary["scheduling_contract"] is None
    assert "no trusted scheduling admission" in summary["qualification_reason"]


@pytest.mark.parametrize("field", ["python", "os"])
def test_real_lane_interpreter_and_os_mismatch_still_fail_before_execution(
    project: Any, tmp_path: Path, field: str
) -> None:
    source, base, _, _ = project
    plan = risk.make_plan(source, base, base, minimum=1)
    lane = current_synthetic_lane()
    lane[field] = (
        "0.0"
        if field == "python"
        else ("windows-latest" if platform.system() != "Windows" else "ubuntu-24.04")
    )
    plan["lanes"][0].update(lane)
    output = tmp_path / "wrong-lane"
    with pytest.raises(ValueError, match="Lane interpreter/OS"):
        acceptance.run_lane(
            plan, source, output, "linux-3.11", "synthetic", execution_scope="local"
        )
    assert not output.exists()


def test_no_test_receipt_cannot_bypass_independent_scheduling_admission(
    project: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, _, _, _ = project
    (source / "tests/helper.py").write_text("VALUE = 2\n")
    base = freeze(source)
    (source / "README.md").write_text("Documentation-only candidate\n")
    head = freeze(source)
    plan = risk.make_plan(source, base, head)
    assert plan["risk"] == 0
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan))
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    (receipts / "receipt.json").write_text(
        json.dumps({"status": "passed", "steps": [{"name": "docs", "exit_code": 0}]})
    )
    output = tmp_path / "admission.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "acceptance.py",
            "verify-local",
            "--source",
            str(source),
            "--base",
            base,
            "--head",
            head,
            "--plan",
            str(plan_path),
            "--receipts",
            str(receipts),
            "--output",
            str(output),
        ],
    )
    assert acceptance.main() == 1
    result = json.loads(output.read_text())
    assert "requalification-required" in str(result)


def test_complete_grid_blocks_before_resource_or_guest_work(
    project: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import grid

    source, _, _, _ = project
    (source / "tests/helper.py").write_text("VALUE = 2\n")
    head = freeze(source)

    def unexpected() -> Any:
        pytest.fail("Stale scheduling must block before host/guest admission")

    monkeypatch.setattr(grid, "read_resources", unexpected)
    monkeypatch.setattr(sys, "argv", ["grid.py", "--repo", str(source), "--ref", head])
    with pytest.raises(ValueError, match="requalification-required"):
        grid.main()


@pytest.mark.parametrize(
    "fault",
    [
        "missing",
        "symlink",
        "same-size-mtime",
        "new-source",
        "new-autouse",
        "case-id",
        "markers",
        "inventory-missing",
    ],
)
def test_dependency_and_collection_drift_is_visible_and_fail_safe(
    project: Any, fault: str
) -> None:
    source, base, policy, known = project
    helper = source / "tests/helper.py"
    if fault == "missing":
        helper.unlink()
    elif fault == "symlink":
        target = source / "tests/alias.py"
        target.write_bytes(helper.read_bytes())
        helper.unlink()
        helper.symlink_to(target.name)
    elif fault == "same-size-mtime":
        before = helper.stat()
        helper.write_text("VALUE = 2\n")
        os.utime(helper, ns=(before.st_atime_ns, before.st_mtime_ns))
    elif fault == "new-source":
        (source / "src/judgmentllm/new.py").write_text("VALUE = 1\n")
    elif fault == "new-autouse":
        (source / "tests/conftest.py").write_text(
            "import pytest\n@pytest.fixture(autouse=True)\ndef shared(): yield\n"
        )
    elif fault == "inventory-missing":
        (source / "ci/test-inventory.json").unlink()
    else:
        changed = copy.deepcopy(known)
        changed["modules"]["tests/test_safe.py"] = {
            "test_safe": {"[new]" if fault == "case-id" else "": []}
        }
        (source / "ci/test-inventory.json").write_text(json.dumps(changed))
    report = contract.build(source, base, freeze(source), policy, known)
    row = report["placements"][SAFE]
    assert report["status"] == "requalification-required"
    assert row["differences"] or row["closure_problems"]
    assert row["placement"] == "globally-exclusive-serial"


def test_new_isolation_needs_a_reviewed_reason(project: Any) -> None:
    source, base, policy, known = project
    changed = copy.deepcopy(known)
    changed["modules"]["tests/test_other.py"]["test_other"][""] = ["resource"]
    (source / "ci/test-inventory.json").write_text(json.dumps(changed))
    head = freeze(source)
    report = contract.build(source, base, head, policy, known)
    assert (
        report["isolation_problems"] and report["status"] == "requalification-required"
    )
    policy["isolation_reasons"]["tests/test_other.py::test_other"] = (
        "Reviewed bounded fresh-process measurement"
    )
    assert contract.build(source, base, head, policy, known)["status"] == "passed"


@pytest.mark.parametrize("fault", ["controls", "missing", "reasons-only"])
def test_candidate_policy_cannot_change_placement_authority(
    project: Any, fault: str
) -> None:
    source, base, policy, known = project
    candidate = copy.deepcopy(policy)
    path = source / "ci/local-policy.json"
    if fault == "missing":
        path.unlink()
    else:
        if fault == "controls":
            candidate["serial"][0]["placement"] = "host-exclusive-serial"
        else:
            candidate["isolation_reasons"]["tests/test_added.py::test_added"] = (
                "A proposed independently reviewed process boundary"
            )
        path.write_text(json.dumps(candidate))
    report = contract.build(source, base, freeze(source), policy, known)
    assert bool(report["policy_change"]) is (fault != "reasons-only")
    assert (report["status"] == "passed") is (fault == "reasons-only")
    assert report["placements"][SAFE]["declared_placement"] == "concurrency-safe-serial"


@pytest.mark.parametrize(
    "path", ["ci/risk.py", "ci/local/pytest_report.py", "ci/local/coverage_data.py"]
)
def test_later_classifier_reporter_and_coverage_edits_require_requalification(
    project: Any, path: str
) -> None:
    source, _, policy, known = project
    control = source / path
    control.parent.mkdir(exist_ok=True)
    control.write_text("VALUE = 1\n")
    policy["serial_shared_dependencies"][path] = checksum(control)
    (source / "ci/local-policy.json").write_text(json.dumps(policy))
    base = freeze(source)
    assert contract.build(source, base, base, policy, known)["status"] == "passed"
    control.write_text("VALUE = 2\n")
    report = contract.build(source, base, freeze(source), policy, known)
    row = report["placements"][SAFE]
    assert report["status"] == "requalification-required"
    assert row["placement"] == "globally-exclusive-serial"
    assert row["differences"][0]["path"] == path


def test_helper_packages_plugins_and_literal_dispatch_are_transitive(
    project: Any,
) -> None:
    _, _, policy, _ = project
    files: dict[str, bytes | None] = {
        "tests/test_safe.py": b'from pkg import helper\npytest_plugins = ("plugin",)\n',
        "tests/conftest.py": b"",
        "tests/pkg/__init__.py": b"import initializer_dependency\n",
        "tests/pkg/helper.py": b'from importlib import import_module as load\nload("choice")\n',
        "tests/plugin.py": b"import plugin_dependency\n",
        "tests/initializer_dependency.py": b"",
        "tests/choice.py": b"",
        "tests/plugin_dependency.py": b"",
        "tests/test_unrelated.py": b"",
    }
    policy["serial_dependency_scope"]["dynamic_imports"]["tests/pkg/helper.py"] = {
        "source_sha256": hashlib.sha256(
            files["tests/pkg/helper.py"] or b""
        ).hexdigest(),
        "targets": ["tests/choice.py"],
        "reason": "Synthetic reviewed dispatch only",
    }
    for path in ["tests/test_safe.py", "tests/pkg/__init__.py", "tests/plugin.py"]:
        policy["serial_dependency_scope"]["dynamic_imports"][path] = {
            "source_sha256": hashlib.sha256(files[path] or b"").hexdigest(),
            "targets": [],
            "reason": "Synthetic static import reviewed for execution effects",
        }
    required, problems = closure(files, SAFE, policy["serial_dependency_scope"])
    assert not problems
    assert set(files) - {"tests/test_unrelated.py"} <= required
    assert "tests/test_unrelated.py" not in required


def test_archive_diagnostic_does_not_read_an_embedded_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "test.py").write_text("VALUE = 1\n")
    environment = tmp_path / ".venv"
    environment.mkdir()
    (environment / "unrelated.py").write_text("Not source evidence\n")

    def unavailable(*args: Any, **kwargs: Any) -> Any:
        raise FileNotFoundError("No git in archive")

    monkeypatch.setattr(subprocess, "run", unavailable)
    assert snapshot(tmp_path) == {"test.py": b"VALUE = 1\n"}


def review_fixture(root: Path, report: dict[str, Any]) -> tuple[Path, str]:
    """Synthetic approvals are confined to disposable policy test repositories."""
    evidence = root / "synthetic-evidence.json"
    evidence.write_text(
        json.dumps(
            {
                "source_sha": report["source_sha"],
                "status": "passed",
                "cleanup_complete": True,
                "isolation_passed": True,
                "case_ids": [SAFE],
            }
        )
    )
    record = {
        "schema_version": 1,
        "purpose": "scheduling-only",
        "status": "approved",
        "policy_sha": report["policy_sha"],
        "source_sha": report["source_sha"],
        "contract_sha256": report["contract_sha256"],
        "decision": "requalified",
        "placements": {SAFE: "concurrency-safe-serial"},
        "reviewer": "synthetic-reviewer",
        "review_reference": "synthetic-only",
        "reason": "Synthetic source-bound requalification",
        "evidence": [{"path": str(evidence), "sha256": checksum(evidence)}],
    }
    path = root / "synthetic-review.json"
    path.write_text(json.dumps(record))
    return path, checksum(path)


def test_review_cannot_be_self_approved_by_a_plan(project: Any, tmp_path: Path) -> None:
    source, base, policy, known = project
    (source / "tests/helper.py").write_text("VALUE = 2\n")
    head = freeze(source)
    report = contract.build(source, base, head, policy, known)
    path, pin = review_fixture(tmp_path, report)
    plan = risk.make_plan(
        source, base, head, scheduling_review=path, scheduling_review_sha256=pin
    )
    assert (
        contract.require(plan, source, review=path, review_sha256=pin)["status"]
        == "passed"
    )
    with pytest.raises(ValueError, match="independent"):
        contract.require(plan, source)
    with pytest.raises(ValueError, match="path and SHA"):
        contract.apply_review(report, path, None)


@pytest.mark.parametrize(
    "fault",
    [
        "pin",
        "source_sha",
        "policy_sha",
        "contract_sha256",
        "unapproved",
        "scope",
        "evidence",
        "cleanup",
        "case_ids",
        "duplicate",
        "unknown",
    ],
)
def test_foreign_or_incomplete_review_never_clears_staleness(
    project: Any, tmp_path: Path, fault: str
) -> None:
    source, base, policy, known = project
    (source / "tests/helper.py").write_text("VALUE = 2\n")
    report = contract.build(source, base, freeze(source), policy, known)
    path, pin = review_fixture(tmp_path, report)
    value = json.loads(path.read_text())
    if fault in {"source_sha", "policy_sha", "contract_sha256"}:
        value[fault] = "0" * len(value[fault])
    elif fault == "unapproved":
        value["status"] = "proposed"
    elif fault == "scope":
        value["purpose"] = "reuse"
    elif fault == "unknown":
        value["authorize_provider_calls"] = True
    elif fault in {"evidence", "cleanup", "case_ids"}:
        proof = Path(value["evidence"][0]["path"])
        evidence = json.loads(proof.read_text())
        if fault == "evidence":
            evidence["source_sha"] = "0" * 40
        elif fault == "cleanup":
            evidence["cleanup_complete"] = False
        else:
            evidence["case_ids"] = []
        proof.write_text(json.dumps(evidence))
        value["evidence"][0]["sha256"] = checksum(proof)
    path.write_text(json.dumps(value))
    if fault == "duplicate":
        path.write_text(
            path.read_text().replace(
                '"schema_version": 1', '"schema_version": 1, "schema_version": 1'
            )
        )
    pin = "0" * 64 if fault == "pin" else checksum(path)
    with pytest.raises(ValueError):
        contract.apply_review(report, path, pin)


def test_dynamic_dispatch_requires_complete_hashed_declaration(project: Any) -> None:
    source, _, policy, _ = project
    helper = source / "tests/helper.py"
    helper.write_text(
        "import importlib\ndef load(name): return importlib.import_module(name)\n"
    )
    (source / "tests/choice.py").write_text("VALUE = 1\n")
    files = snapshot(source)
    required, problems = closure(files, SAFE, policy["serial_dependency_scope"])
    assert problems and "tests/helper.py" in required
    policy["serial_dependency_scope"]["dynamic_imports"]["tests/helper.py"] = {
        "source_sha256": checksum(helper),
        "targets": ["tests/choice.py"],
        "reason": "Synthetic finite alternatives",
    }
    required, problems = closure(files, SAFE, policy["serial_dependency_scope"])
    assert not problems and "tests/choice.py" in required
    helper.write_text(helper.read_text() + "# changed dispatch\n")
    assert closure(snapshot(source), SAFE, policy["serial_dependency_scope"])[1]


@pytest.mark.parametrize(
    "fault",
    ["restore-later", "same-size-mtime", "symlink", "directory", "extra", "alias"],
)
def test_real_inventory_is_read_again_at_each_configuration_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    from judgmentllm.study import definitions

    package = tmp_path / "package"
    root = package / "sources/rubric"
    root.mkdir(parents=True)
    rubric = {
        "rubric_id": "001",
        "name": "Synthetic",
        "traits": [
            {
                "trait_id": "01",
                "name": "Quality",
                "description": "Synthetic trait",
                "levels": [{"label": "0"}, {"label": "1"}],
            }
        ],
    }
    (root / "rubric_12_traits.yaml").write_text(json.dumps(rubric))
    (root / "prompts").mkdir()
    (root / "prompts/all_12_score_only.txt").write_text(
        "{{ rubric }}\n{{ response }}\n{{ output_instructions }}"
    )
    support = root / "unused-support.txt"
    support.write_text("first\n")
    expected = {
        p.relative_to(root).as_posix(): checksum(p)
        for p in root.rglob("*")
        if p.is_file()
    }
    (root / "manifest_sha256.json").write_text(json.dumps(expected))
    manifest: dict[str, Any] = {
        "rubric": {
            "files": expected,
            "manifest_sha256": checksum(root / "manifest_sha256.json"),
        }
    }

    def source_manifest(current: Path) -> dict[str, Any]:
        return manifest

    monkeypatch.setattr(definitions, "source_manifest", source_manifest)
    original = definitions.source_inventory
    calls: list[tuple[Path, str]] = []

    def verify(current: Path, kind: str) -> dict[str, str]:
        calls.append((current, kind))
        return original(current, kind)

    monkeypatch.setattr(definitions, "source_inventory", verify)

    def configure(current: Path) -> Any:
        return definitions.configuration(
            current, "openai", "gpt-6-luna", "grouped_scores", "default"
        )

    before = configure(package)
    state = support.stat()
    alternate = package / "link-target.txt"
    added = root / "unexpected.txt"
    try:
        if fault == "symlink":
            alternate.write_text("first\n")
            support.unlink()
            support.symlink_to(alternate)
        elif fault == "directory":
            support.unlink()
            support.mkdir()
        elif fault == "extra":
            added.write_text("unexpected\n")
        else:
            support.write_text("other\n")
            if fault == "same-size-mtime":
                os.utime(support, ns=(state.st_atime_ns, state.st_mtime_ns))
        alias = (
            package.parent / package.name / ".." / package.name
            if fault == "alias"
            else package
        )
        with pytest.raises(ValueError, match="source"):
            configure(alias)
    finally:
        if support.is_dir():
            support.rmdir()
        else:
            support.unlink(missing_ok=True)
        support.write_text("first\n")
        added.unlink(missing_ok=True)
    after = configure(package)
    assert before == after and len(calls) == 3
    assert all(kind == "rubric" for _, kind in calls)


def test_pinned_driver_and_reporter_execute_separately_from_candidate_policy(
    project: Any, tmp_path: Path
) -> None:
    source, _, policy, known = project
    trusted = tmp_path / "trusted"
    shutil.copytree(
        Path(__file__).resolve().parents[1],
        trusted / "ci",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    # The toy local lane uses portable child ownership, never the host's systemd.
    process = trusted / "ci/local/process.py"
    process.write_text(
        process.read_text().replace(
            'SYSTEMCTL = shutil.which("systemctl", path=os.defpath)',
            "SYSTEMCTL = None",
        )
    )
    # Only the disposable policy owns this host-bound two-case lane.
    risk_file = trusted / "ci/risk.py"
    risk_file.write_text(
        risk_file.read_text() + f"\nPRIMARY = {current_synthetic_lane()!r}\n"
    )
    subprocess.run(["git", "init", "-q", str(trusted)], check=True)
    for name in (
        "risk.py",
        "suite_evidence.py",
        "local/policy.py",
        "local/pytest_report.py",
    ):
        path = source / "ci" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            'raise AssertionError("Candidate policy must not control admission")\n'
        )
        policy["serial_shared_dependencies"]["ci/" + name] = checksum(path)
    (source / "tests/test_other.py").write_text(
        "from pathlib import Path\n\n\n"
        "def test_other():\n"
        "    import ci.local.pytest_report as reporter\n\n"
        '    expected = Path(__file__).parents[2] / "trusted"\n'
        "    assert Path(reporter.__file__).resolve().is_relative_to(expected)\n"
    )
    (source / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\nmarkers=["serial: synthetic isolation"]\n'
    )
    policy["serial_shared_dependencies"]["pyproject.toml"] = checksum(
        source / "pyproject.toml"
    )
    taxonomy = json.loads((source / "ci/test-profiles.json").read_text())
    taxonomy["smoke"] = ["tests/test_other.py"]
    taxonomy["essential"] = [SAFE]
    metadata: list[tuple[str, dict[str, Any]]] = [
        ("test-inventory.json", known),
        ("test-profiles.json", taxonomy),
        ("local-policy.json", policy),
        (
            "test-contract.json",
            {
                "schema_version": 1,
                "required_nodes": [SAFE, "tests/test_other.py::test_other"],
                "replacements": {},
            },
        ),
    ]
    for name, value in metadata:
        (trusted / "ci" / name).write_text(json.dumps(value))
    trusted_sha = freeze(trusted)
    base = freeze(source)
    (source / "README.md").write_text("Separate synthetic candidate\n")
    head = freeze(source)
    plan_path = tmp_path / "plan.json"
    driver = [sys.executable, "-B", str(trusted / "ci/acceptance.py")]
    env = acceptance.clean_environment()
    env["GITHUB_ACTIONS"] = "true"
    for command in (
        [
            *driver,
            "plan",
            "--source",
            str(source),
            "--base",
            base,
            "--head",
            head,
            "--risk",
            "1",
            "--output",
            str(plan_path),
        ],
        [
            *driver,
            "lane",
            "--source",
            str(source),
            "--plan",
            str(plan_path),
            "--execution-scope",
            "hosted",
            "--run-id",
            "synthetic-driver",
            "--output",
            str(tmp_path / "lane"),
        ],
    ):
        result = subprocess.run(
            command, env=env, cwd=tmp_path, capture_output=True, text=True, timeout=60
        )
        assert result.returncode == 0, result.stdout + result.stderr
    plan = json.loads(plan_path.read_text())
    assert (
        plan["source_sha"] == head
        and plan["scheduling_contract"]["policy_sha"] == trusted_sha
    )
    receipt = json.loads((tmp_path / "lane/receipt.json").read_text())
    assert receipt["status"] == "passed" and receipt["deferred"]["scheduling_contract"]
    # Exercise the complete local trust boundary only in these disposable repositories.
    review_path, pin = review_fixture(tmp_path, plan["scheduling_contract"])
    review_flags = [
        "--scheduling-review",
        str(review_path),
        "--scheduling-review-sha256",
        pin,
    ]
    local_plan = tmp_path / "local-plan.json"
    local_lane = tmp_path / "local-lane"
    admission = tmp_path / "local-admission.json"
    env.pop("GITHUB_ACTIONS", None)
    env["JUDGMENTLLM_ALLOCATION_ROOT"] = str(tmp_path / "private-allocation")
    env.pop("JUDGMENTLLM_ALLOCATION_TOKEN", None)
    for command in (
        [
            *driver,
            "plan",
            "--source",
            str(source),
            "--base",
            base,
            "--head",
            head,
            "--risk",
            "1",
            "--output",
            str(local_plan),
            *review_flags,
        ],
        [
            *driver,
            "lane",
            "--source",
            str(source),
            "--plan",
            str(local_plan),
            "--execution-scope",
            "local",
            "--run-id",
            "synthetic-local",
            "--workers",
            "1",
            "--local-schedule",
            "conservative",
            "--output",
            str(local_lane),
            *review_flags,
        ],
        [
            *driver,
            "verify-local",
            "--source",
            str(source),
            "--risk",
            "1",
            "--base",
            base,
            "--head",
            head,
            "--plan",
            str(local_plan),
            "--receipts",
            str(local_lane),
            "--run-id",
            "synthetic-local",
            "--output",
            str(admission),
            *review_flags,
        ],
    ):
        result = subprocess.run(
            command, env=env, cwd=tmp_path, capture_output=True, text=True, timeout=60
        )
        assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(admission.read_text())["passed"] is True
    local_receipt = json.loads((local_lane / "receipt.json").read_text())
    assert local_receipt["scheduling_contract"]["status"] == "passed"
    assert any(
        step["name"] == "scheduling-contract" and step["exit_code"] == 0
        for step in local_receipt["steps"]
    )
    assert (
        subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=trusted, text=True
        )
        == ""
    )
