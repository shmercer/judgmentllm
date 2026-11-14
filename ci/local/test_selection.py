"""Inventory drift, all risk depths, platform reach and raw-evidence attacks."""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from risk import COMPREHENSIVE, admit, lane_profile, make_plan
from selection import (
    HERE,
    ISOLATION,
    digest,
    inventory,
    inventory_items,
    manifest,
    read,
    validate_registry,
)
from selection_fixture import write_suite
from suite_evidence import validate_suite
from test_risk import freeze, passing_receipts
from test_risk import repository as repository


@pytest.fixture(scope="module")
def cases() -> list[dict[str, Any]]:
    return inventory_items(read(HERE / "test-inventory.json"))


def test_inventory_taxonomy_is_complete_and_references_are_live(
    cases: list[dict[str, Any]],
) -> None:
    taxonomy, known = (
        read(HERE / "test-profiles.json"),
        read(HERE / "test-inventory.json"),
    )
    validate_registry(taxonomy, known)
    assert inventory(cases) == known
    assert set(taxonomy["domains"]) == {
        entry["domain"] for entry in taxonomy["modules"].values()
    }


@pytest.mark.parametrize("minimum", [2, 3])
@pytest.mark.parametrize(
    "module,distribution",
    [("test_study_limits.py", "worksteal"), ("test_models.py", "loadfile")],
)
def test_heavy_impact_uses_work_stealing_without_raising_platform_caps(
    repository: Any, minimum: int, module: str, distribution: str
) -> None:
    source, base = repository
    (source / "tests").mkdir()
    (source / "tests" / module).write_text("def test_synthetic(): assert True\n")
    plan = make_plan(source, base, freeze(source), minimum=minimum)
    for lane in plan["lanes"]:
        assert lane["distribution"] == (
            "worksteal" if lane["profile"] == "full" else distribution
        )
        assert lane["workers"] == (2 if lane["id"].startswith("windows-") else 4)


@pytest.mark.parametrize(
    "profile", ["focused", "impacted", "platform-smoke", "platform-extended", "full"]
)
@pytest.mark.parametrize(
    "new_case",
    [
        "tests/test_models.py::test_new_case[001]",
        "tests/test_persistence_ownership.py::test_new_sqlite_child",
        "tests/test_new_module.py::test_new_case",
    ],
)
def test_new_cases_and_parameters_enter_every_required_lane(
    cases: list[dict[str, Any]], profile: str, new_case: str
) -> None:
    result = manifest(
        [*cases, {"nodeid": new_case, "markers": []}],
        profile=profile,
        impacted=[],
        include_r=False,
        include_resources=False,
    )
    item = next(c for c in result["cases"] if c["nodeid"] == new_case)
    assert item["disposition"] == "required"
    assert item["reason"] == "new/unclassified case; mandatory on every lane"


@pytest.mark.parametrize(
    "fault", ["removed", "renamed", "duplicate", "marker", "parameter"]
)
def test_inventory_changes_cannot_disappear(
    cases: list[dict[str, Any]], fault: str
) -> None:
    changed = copy.deepcopy(cases)
    if fault == "removed":
        changed.pop()
    elif fault == "renamed":
        changed[-1]["nodeid"] += "_renamed"
    elif fault == "duplicate":
        changed.append(changed[-1])
    elif fault == "marker":
        changed[-1]["markers"].append("r_interop")
    else:
        index = next(i for i, item in enumerate(changed) if "[" in item["nodeid"])
        changed.pop(index)
    with pytest.raises(ValueError):
        manifest(
            changed,
            profile="platform-smoke",
            impacted=[],
            include_r=False,
            include_resources=False,
        )


@pytest.mark.parametrize(
    "fault",
    [
        "unknown-module",
        "stale-module",
        "stale-selector",
        "duplicate-selector",
        "missing-smoke",
        "unknown-domain",
        "bad-cost",
        "boolean-version",
        "unknown-field",
        "duplicate-source",
        "duplicate-provider",
    ],
)
def test_metadata_drift_fails_closed(fault: str) -> None:
    taxonomy, known = (
        read(HERE / "test-profiles.json"),
        read(HERE / "test-inventory.json"),
    )
    if fault == "unknown-module":
        known["modules"]["tests/test_new.py"] = {"test_new": {"": []}}
    elif fault == "stale-module":
        known["modules"].pop(next(iter(known["modules"])))
    elif fault == "stale-selector":
        taxonomy["extended"].append("tests/test_missing.py::test_gone")
    elif fault == "duplicate-selector":
        taxonomy["smoke"].append(taxonomy["smoke"][0])
    elif fault == "missing-smoke":
        taxonomy["smoke"].pop()
    elif fault == "unknown-domain":
        taxonomy["modules"]["tests/test_config.py"]["domain"] = "invented"
    elif fault == "boolean-version":
        taxonomy["schema_version"] = True
    elif fault == "unknown-field":
        taxonomy["ignored_control"] = "unsafe"
    elif fault == "duplicate-source":
        taxonomy["shared_sources"].append(taxonomy["shared_sources"][0])
    elif fault == "duplicate-provider":
        taxonomy["providers"]["openai"].append(taxonomy["providers"]["openai"][0])
    else:
        taxonomy["modules"]["tests/test_config.py"]["cost"] = "unbounded"
    with pytest.raises(ValueError):
        validate_registry(taxonomy, known)


def test_platform_profiles_retain_genuine_sdk_process_privacy_and_negative_paths(
    cases: list[dict[str, Any]],
) -> None:
    smoke = manifest(
        cases,
        profile="platform-smoke",
        impacted=[],
        include_r=False,
        include_resources=False,
    )
    extended = manifest(
        cases,
        profile="platform-extended",
        impacted=[],
        include_r=False,
        include_resources=False,
    )
    for record in (smoke, extended):
        selected = [c for c in record["cases"] if c["disposition"] == "required"]
        assert any(
            c["nodeid"].startswith("tests/test_native_other_sdk.py::")
            and c["partition"] == "serial"
            for c in selected
        )
        if record["profile"] == "platform-extended":
            children = [
                c
                for c in selected
                if c["nodeid"].startswith(
                    "tests/test_study_limits.py::test_separate_process_resume_and_recovery_never_replace_requests["
                )
            ]
            assert len(children) == 2
            assert all(c["partition"] == "serial" for c in children)
        for provider in ("openai", "anthropic", "gemini", "vertex"):
            assert any(
                c["nodeid"].startswith(
                    f"tests/test_{provider}_lifecycle.py::test_production_seal_execute_export_reopen"
                )
                for c in selected
            )
            assert any(
                c["nodeid"].startswith(
                    f"tests/test_{provider}_lifecycle.py::test_missing_credentials_fail_before_store_or_submission"
                )
                for c in selected
            )
        assert any("test_process_death_releases_lock" in c["nodeid"] for c in selected)
        assert any(
            "test_competing_process_cannot_cancel" in c["nodeid"] for c in selected
        )
        assert any("test_interrupted_publication" in c["nodeid"] for c in selected)
    assert any(
        c["nodeid"].startswith("tests/test_native_schema.py::")
        and c["disposition"] == "not-required-for-this-lane"
        for c in extended["cases"]
    )
    assert {c["nodeid"] for c in smoke["cases"] if c["disposition"] == "required"} <= {
        c["nodeid"] for c in extended["cases"] if c["disposition"] == "required"
    }


@pytest.mark.parametrize("level", range(5))
def test_depth_and_platform_reach_are_independent(repository: Any, level: int) -> None:
    source, base = repository
    (source / "README.md").write_text("New prose")
    plan = make_plan(source, base, freeze(source), minimum=level)
    assert len(plan["lanes"]) == (8 if level == 4 else 3 if level >= 2 else 1)
    assert plan["coverage"] == (level >= 2)
    if level >= 2:
        assert lane_profile(plan, "linux-3.11", "local") == "full"
    if level == 4:
        assert all(lane["profile"] == "full" for lane in plan["lanes"])
        assert {lane["id"] for lane in plan["lanes"]} == {
            lane["id"] for lane in COMPREHENSIVE
        }


def test_changed_neutral_module_gets_affected_platform_cases(repository: Any) -> None:
    source, _ = repository
    path = source / "tests/test_models.py"
    path.parent.mkdir()
    path.write_text("def test_new(): assert True\n")
    base = freeze(source)
    path.write_text("def test_new(): assert 1 == 1\n")
    plan = make_plan(source, base, freeze(source))
    assert plan["risk"] == 3 and plan["impact"] == ["tests/test_models.py"]
    assert plan["lanes"][1]["profile"] == "platform-extended"


@pytest.mark.parametrize(
    "path", ["tests/test_unknown.py", "src/judgmentllm/new_shared.py"]
)
def test_unknown_dependency_escalates_to_complete_platform_verification(
    repository: Any, path: str
) -> None:
    source, base = repository
    file = source / path
    file.parent.mkdir(parents=True)
    file.write_text("def test_new(): assert True\n")
    plan = make_plan(source, base, freeze(source))
    assert plan["risk"] == 3 and plan["unknown_dependencies"] == [path]
    assert all(lane["profile"] == "full" for lane in plan["lanes"])


@pytest.mark.parametrize(
    "fault",
    [
        "none",
        "missing-windows",
        "missing-macos",
        "wrong-profile",
        "bad-hash",
        "omitted-case",
        "duplicate-case",
        "missing-phase",
        "skip",
        "xfail",
        "stale-source",
        "wrong-interpreter",
        "wrong-platform",
        "wrong-run",
        "wrong-lock",
        "partition",
        "accounting",
        "unknown-outcome",
        "unknown-phase",
        "bad-xfail",
        "executed-collection",
        "wrong-workers",
        "wrong-distribution",
        "boolean-exit-code",
    ],
)
def test_raw_platform_evidence_is_independently_validated(
    repository: Any, tmp_path: Path, fault: str
) -> None:
    source, base = repository
    (source / "README.md").write_text("New prose")
    plan = make_plan(source, base, freeze(source), minimum=2)
    receipts = passing_receipts(plan)
    if fault.startswith("missing-") and fault in {"missing-windows", "missing-macos"}:
        receipts.pop(1 if fault == "missing-windows" else 2)
        assert admit(plan, receipts, "run:1")
        return
    root = tmp_path / "lane"
    root.mkdir()
    write_suite(plan, "windows-3.12", root / "suite")
    file = (
        "selection-manifest.json"
        if fault in {"wrong-profile", "bad-hash"}
        else "summary.json"
        if fault in {"partition", "accounting", "wrong-workers", "wrong-distribution"}
        else "collection.json"
        if fault == "executed-collection"
        else "ordinary.json"
    )
    path = root / "suite" / file
    raw = read(path)
    if fault == "wrong-profile":
        raw["profile"] = "full"
    elif fault == "bad-hash":
        raw["selection_sha256"] = digest("forged")
    elif fault == "omitted-case":
        raw["reports"] = raw["reports"][3:]
    elif fault == "duplicate-case":
        raw["reports"].append(raw["reports"][0])
    elif fault == "missing-phase":
        raw["reports"].pop(0)
    elif fault == "unknown-outcome":
        raw["reports"][1]["outcome"] = "unevaluated"
    elif fault == "unknown-phase":
        raw["reports"][1]["phase"] = "unrecognized"
    elif fault == "bad-xfail":
        raw["reports"][1]["xfail"] = ""
    elif fault == "executed-collection":
        raw["reports"] = [
            {
                "nodeid": "unplanned-call",
                "phase": "call",
                "outcome": "passed",
                "xfail": False,
            }
        ]
    elif fault == "wrong-workers":
        raw["requested_workers"] = 32
    elif fault == "wrong-distribution":
        raw["distribution"] = "worksteal"
    elif fault == "boolean-exit-code":
        raw["exit_code"] = False
    elif fault in {"skip", "xfail"}:
        raw["reports"][1]["outcome"] = "skipped" if fault == "skip" else "passed"
        raw["reports"][1]["xfail"] = fault == "xfail"
    elif fault == "stale-source":
        raw["provenance"]["source"]["commit"] = "0" * 40
    elif fault in {"wrong-interpreter", "wrong-platform"}:
        raw["provenance"]["interpreter"][
            "version" if fault == "wrong-interpreter" else "platform"
        ] = "foreign"
    elif fault in {"wrong-run", "wrong-lock"}:
        raw["provenance"][
            "qualification_run_id" if fault == "wrong-run" else "lockfile_sha256"
        ] = "foreign"
    elif fault == "partition":
        raw["partitions"].pop()
    elif fault == "accounting":
        raw["accounting_sha256"] = "0" * 64
    path.write_text(json.dumps(raw))
    if fault == "none":
        assert validate_suite(plan, "windows-3.12", root, "run:1")["status"] == "passed"
    else:
        with pytest.raises(ValueError):
            validate_suite(plan, "windows-3.12", root, "run:1")


def test_current_collection_matches_declared_inventory(tmp_path: Path) -> None:
    receipt = tmp_path / "collection.json"
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "pytest",
            "--no-cov",
            "--collect-only",
            "-q",
            "tests",
            "-p",
            "ci.local.pytest_report",
            f"--ci-report={receipt}",
            "-o",
            f"cache_dir={tmp_path / 'cache'}",
        ],
        cwd=HERE.parent,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert inventory(read(receipt)["collected"]) == read(HERE / "test-inventory.json")


@pytest.mark.parametrize("lane", ["linux-3.11", "windows-3.12"])
@pytest.mark.parametrize("checkout", ["lf", "crlf", "changed-content"])
def test_lock_identity_permits_only_committed_windows_newline_forms(
    repository: Any, tmp_path: Path, lane: str, checkout: str
) -> None:
    source, base = repository
    (source / "uv.lock").write_bytes(b"synthetic = true\n")
    plan = make_plan(source, base, freeze(source), minimum=2)
    assert plan["lockfile_sha256"] == hashlib.sha256(b"synthetic = true\n").hexdigest()
    assert (
        plan["windows_crlf_lockfile_sha256"]
        == hashlib.sha256(b"synthetic = true\r\n").hexdigest()
    )
    output = tmp_path / "lane"
    output.mkdir()
    write_suite(plan, lane, output / "suite")
    value = {
        "lf": plan["lockfile_sha256"],
        "crlf": plan["windows_crlf_lockfile_sha256"],
        "changed-content": "0" * 64,
    }[checkout]
    for name in ("collection", "summary", "ordinary", "serial", "resource"):
        path = output / "suite" / (name + ".json")
        if path.exists():
            raw = read(path)
            raw["provenance"]["lockfile_sha256"] = value
            path.write_text(json.dumps(raw))
    if checkout == "lf" or checkout == "crlf" and lane == "windows-3.12":
        assert validate_suite(plan, lane, output, "run:1")["status"] == "passed"
    else:
        with pytest.raises(ValueError, match="lock mismatch"):
            validate_suite(plan, lane, output, "run:1")


@pytest.mark.parametrize(
    "boundary,probe",
    [
        (
            "schema",
            "tests/test_output_contracts.py::test_whole_rubric_validation_rejects_invalid_scores",
        ),
        (
            "private-evidence",
            "tests/test_persistence_artifacts.py::test_hash_only_retains_no_bytes",
        ),
        (
            "duplicate-submission",
            "tests/test_public_lifecycle.py::test_shared_run_resume_export_and_durable_counts",
        ),
        (
            "corrupt-recovery",
            "tests/test_execution_processes.py::test_corrupt_published_full_receipt_blocks_recovery",
        ),
        (
            "subprocess-cancellation",
            "tests/test_cli_process_safety.py::test_os_ownership_rejects_foreign_pid_and_only_terminates_owned_process",
        ),
        (
            "windows-lock",
            "tests/test_execution_privacy.py::test_unexpected_lock_contents_cannot_hide_behind_windows_deferral",
        ),
        ("missing-platform-receipt", ""),
    ],
)
def test_critical_fault_probes_remain_required_and_cannot_report_success(
    repository: Any, tmp_path: Path, boundary: str, probe: str
) -> None:
    source, base = repository
    (source / "README.md").write_text("New prose")
    plan = make_plan(source, base, freeze(source), minimum=3)
    if not probe:
        assert admit(plan, passing_receipts(plan)[:-1], "run:1")
        return
    lane = "linux-3.11" if boundary == "schema" else "windows-3.12"
    output = tmp_path / "fault"
    output.mkdir()
    write_suite(plan, lane, output / "suite")
    selection = read(output / "suite/selection-manifest.json")
    required = [
        case for case in selection["cases"] if case["nodeid"].split("[", 1)[0] == probe
    ]
    assert required and all(case["disposition"] == "required" for case in required)
    # An injected failing call from each independent behavioral probe must
    # defeat otherwise successful lane/step summaries and passing totals.
    path = output / ("suite/" + required[0]["partition"] + ".json")
    receipt = read(path)
    report = next(
        report
        for report in receipt["reports"]
        if report["nodeid"] == required[0]["nodeid"] and report["phase"] == "call"
    )
    report["outcome"] = "failed"
    path.write_text(json.dumps(receipt))
    with pytest.raises(ValueError, match="Failed, incomplete or foreign"):
        validate_suite(plan, lane, output, "run:1")


def test_testing_guide_commands_and_marker_names_match_interfaces() -> None:
    import tomllib

    root = HERE.parent
    guide = (root / "docs/testing-guide.md").read_text(encoding="utf-8")
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    declared = {
        marker.split(":", 1)[0]
        for marker in project["tool"]["pytest"]["ini_options"]["markers"]
    }
    assert ISOLATION <= declared
    assert all(
        "--profile " + profile in guide
        for profile in ("fast", "pr", "comprehensive", "release")
    )
    for command in (
        [sys.executable, "ci/check.py", "--help"],
        [sys.executable, "ci/selection.py", "--help"],
    ):
        result = subprocess.run(
            command, cwd=root, capture_output=True, text=True, timeout=20
        )
        assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("fault", ["none", "ordinary", "serial", "worker-crash"])
def test_planned_runner_executes_real_disjoint_partitions_and_propagates_failure(
    tmp_path: Path, fault: str
) -> None:
    from acceptance import clean_environment

    source = tmp_path / "source"
    policy = source / "ci"
    for name in (
        "selection.py",
        "risk.py",
        "verification-map.json",
        "local/suite.py",
        "local/pytest_report.py",
        "local/preflight_provenance.py",
        "local/resource_budget.py",
        "local/resource_specs.py",
        "local/telemetry.py",
    ):
        path = policy / name
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(HERE / name, path)
    tests = source / "tests"
    tests.mkdir()
    ordinary = (
        "__import__('os')._exit(7)"
        if fault == "worker-crash"
        else f"assert {fault != 'ordinary'}"
    )
    (tests / "test_probe.py").write_text(
        f"import pytest\ndef test_ordinary():\n    {ordinary}\n@pytest.mark.serial\ndef test_serial():\n    assert {fault != 'serial'}\n"
    )
    items: list[dict[str, Any]] = [
        {"nodeid": "tests/test_probe.py::test_ordinary", "markers": []},
        {"nodeid": "tests/test_probe.py::test_serial", "markers": ["serial"]},
    ]
    known = inventory(items)
    taxonomy: dict[str, Any] = {
        "schema_version": 1,
        "domains": {"core-contract": "Synthetic runner checks"},
        "modules": {
            "tests/test_probe.py": {
                "domain": "core-contract",
                "cost": "quick",
                "platforms": [],
                "reason": "Synthetic",
            }
        },
        "smoke": ["tests/test_probe.py"],
        "mandatory_smoke": ["tests/test_probe.py"],
        "extended": [],
        "essential": [],
        "providers": {},
        "shared_sources": [],
    }
    metadata: list[tuple[str, dict[str, Any]]] = [
        ("test-inventory.json", known),
        ("test-profiles.json", taxonomy),
        (
            "test-contract.json",
            {
                "schema_version": 1,
                "required_nodes": [item["nodeid"] for item in items],
                "replacements": {},
            },
        ),
    ]
    for name, value in metadata:
        (policy / name).write_text(json.dumps(value))
    (source / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\nmarkers=["serial: isolated"]\naddopts="--strict-markers"\n'
    )
    (source / "uv.lock").write_text("synthetic offline lock\n")
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    head = freeze(source)
    # The synthetic policy owns only these two cases. No candidate fixture can
    # rewrite the real repository's protected inventory in this experiment.
    plan = {
        "source_sha": head,
        "coverage": False,
        "impact": [],
        "lanes": [{"id": "linux-3.11", "profile": "platform-smoke"}],
        "selection_policy_sha256": digest({"taxonomy": taxonomy, "inventory": known}),
    }
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan))
    environment = clean_environment()
    environment["JUDGMENTLLM_CI_RUN_ID"] = "synthetic-real-run"
    output = tmp_path / "run"
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            str(policy / "local/suite.py"),
            "--source",
            str(source),
            "--output",
            str(output),
            "--workers",
            "1",
            "--no-coverage",
            "--skip-r",
            "--skip-resources",
            "--plan",
            str(plan_path),
            "--execution-scope",
            "hosted",
            "--contract",
            str(policy / "test-contract.json"),
            "tests",
        ],
        cwd=source,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
    )
    summary = read(output / "summary.json")
    assert (result.returncode == 0) == (fault == "none"), result.stdout + result.stderr
    assert summary["status"] == ("passed" if fault == "none" else "failed")
    if fault == "none":
        assert summary["counts"]["passed"] == 2
        assert [part["name"] for part in summary["partitions"]] == [
            "ordinary",
            "serial",
        ]
        assert {
            case["disposition"]
            for case in json.loads((output / "case-accounting.json").read_text())
        } == {"executed"}
