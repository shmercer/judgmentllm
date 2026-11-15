"""Exercise real xdist coverage, child ownership, and durable queue failures."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import psutil
import pytest
from coverage import CoverageData
from coverage.exceptions import CoverageException

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from acceptance import clean_environment
from allocation import QualificationLease, foreground_pending
from coverage_data import combine, context_name, prepare_config, seal
from main_worker import Queue, install
from preflight_provenance import receipt_provenance, source_record
from process import run as owned_run
from test_risk import freeze


@pytest.fixture(scope="module", params=[2, 12], ids=["two-workers", "twelve-workers"])
def real_coverage(
    tmp_path_factory: pytest.TempPathFactory,
    request: pytest.FixtureRequest,
) -> tuple[Path, Path, dict[str, Any], dict[str, Any]]:
    root = tmp_path_factory.mktemp("actual-contributors")
    source = root / "source"
    source.mkdir()
    (source / "probe.py").write_text(
        "def first(value):\n    if value:\n        return 11\n    return 12\ndef second(value):\n    if value:\n        return 21\n    return 22\ndef child():\n    return 31\n"
    )
    (source / "test_probe.py").write_text(
        "import subprocess, sys, os\nfrom probe import first, second\ndef test_first_true(): assert first(True) == 11\ndef test_first_false(): assert first(False) == 12\ndef test_second_true(): assert second(True) == 21\ndef test_second_false(): assert second(False) == 22\ndef test_child():\n    subprocess.run([sys.executable, '-m', 'coverage', 'run', '--parallel-mode', '--branch', '--source=probe', '--context=' + os.environ['JUDGMENTLLM_COVERAGE_CONTEXT'] + '|test_probe.py::test_child|subprocess', 'child.py'], check=True)\n"
    )
    (source / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\naddopts=""\n[tool.coverage.run]\nsource=["probe"]\nbranch=true\nrelative_files=true\n'
    )
    (source / "child.py").write_text("from probe import child; assert child() == 31\n")
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    freeze(source)
    data = root / ".coverage"
    receipt = root / "cases.json"
    identity = receipt_provenance(
        source_record(source), "real-worker-contributions", str(root)
    )
    config = prepare_config(source, root, identity, "ordinary")
    env = {
        **clean_environment(),
        "PYTHONPATH": os.pathsep.join(
            (str(Path(__file__).resolve().parents[2]), str(source))
        ),
        "COVERAGE_FILE": str(data),
        "JUDGMENTLLM_CI_RUN_ID": "real-worker-contributions",
        "JUDGMENTLLM_CI_GROUP": "ordinary",
        "JUDGMENTLLM_CI_ATTEMPT_ID": str(root),
        "COVERAGE_RCFILE": str(config),
        "JUDGMENTLLM_COVERAGE_CONTEXT": context_name(identity, "ordinary"),
    }
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "pytest",
            "-p",
            "ci.local.pytest_report",
            "-n",
            str(request.param),
            "--dist=load",
            "--max-worker-restart=0",
            "--ci-coverage",
            "--cov=probe",
            "--cov-branch",
            "--cov-context=test",
            "--cov-append",
            f"--cov-config={config}",
            "--cov-report=",
            f"--ci-report={receipt}",
            "-q",
            "-o",
            f"cache_dir={root / 'cache'}",
            f"--basetemp={root / 'temporary'}",
        ],
        cwd=source,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    record = json.loads(receipt.read_text())
    assert len(record["coverage_contributors"]) == request.param
    coverage = CoverageData(basename=str(data))
    coverage.read()
    assert 10 in (coverage.lines("probe.py") or [])
    manifest = seal(data, record, source, record["provenance"], "ordinary")
    return source, data, record, manifest


def test_supported_worker_and_subprocess_combination_is_complete(
    real_coverage: tuple[Path, Path, dict[str, Any], dict[str, Any]], tmp_path: Path
) -> None:
    source, _, receipt, group = real_coverage
    result = combine(
        [group],
        {"ordinary": receipt},
        source,
        receipt["provenance"],
        tmp_path / "final",
    )
    assert result["groups"] == [group]
    data = CoverageData(basename=str(tmp_path / "final"))
    data.read()
    assert {(2, 3), (2, 4), (6, 7), (6, 8)} <= set(data.arcs("probe.py") or [])
    assert 10 in (data.lines("probe.py") or [])


@pytest.mark.parametrize(
    "fault",
    [
        "missing-worker",
        "duplicate-worker",
        "incomplete-worker",
        "foreign-source",
        "foreign-run",
        "failed-stage",
        "corrupt",
        "branch-disabled",
        "unexpected-source",
        "missing-child",
        "missing-active-contribution",
    ],
)
def test_real_coverage_rejects_incomplete_and_foreign_contributions(
    real_coverage: tuple[Path, Path, dict[str, Any], dict[str, Any]],
    tmp_path: Path,
    fault: str,
) -> None:
    source, original, original_receipt, original_group = real_coverage
    receipt, group = copy.deepcopy(original_receipt), copy.deepcopy(original_group)
    data = tmp_path / ".coverage"
    data.write_bytes(original.read_bytes())
    data.with_name("pyproject.toml").write_bytes(
        original.with_name("pyproject.toml").read_bytes()
    )
    group = seal(data, receipt, source, original_receipt["provenance"], "ordinary")
    if fault == "missing-worker":
        receipt["coverage_contributors"] = []
    elif fault == "duplicate-worker":
        receipt["coverage_contributors"].append(receipt["coverage_contributors"][0])
    elif fault == "incomplete-worker":
        receipt["coverage_contributors"][0]["complete"] = False
    elif fault == "foreign-source":
        receipt["provenance"]["source"]["commit"] = "f" * 40
    elif fault == "foreign-run":
        receipt["provenance"]["qualification_run_id"] = "different-attempt"
    elif fault == "failed-stage":
        receipt["exit_code"] = 1
    elif fault == "corrupt":
        data.write_bytes(b"invalid sqlite")
    elif fault == "branch-disabled":
        data.unlink()
        plain = CoverageData(basename=str(data))
        plain.add_lines({"probe.py": [1, 2]})
        plain.write()
    elif fault == "unexpected-source":
        other = tmp_path / "other.py"
        other.write_text("value = 1\n")
        changed = CoverageData(basename=str(data))
        changed.read()
        changed.add_arcs({str(other): [(-1, 1), (1, -1)]})
        changed.write()
    else:
        # Remove real contributions by querying public coverage contexts, never inventing data.
        old = CoverageData(basename=str(original))
        old.read()
        data.unlink()
        changed = CoverageData(basename=str(data))
        dropped = set(receipt["coverage_contributors"][0]["contexts"])
        for context in old.measured_contexts():
            if fault == "missing-active-contribution" and context in dropped:
                continue
            old.set_query_context(context)
            arcs = old.arcs("probe.py") or []
            if fault == "missing-child":
                arcs = [arc for arc in arcs if 10 not in arc]
            if arcs:
                changed.set_context(context)
                changed.add_arcs({"probe.py": arcs})
        changed.write()
    with pytest.raises((ValueError, CoverageException)):
        combine(
            [group],
            {"ordinary": receipt},
            source,
            original_receipt["provenance"],
            tmp_path / "final",
        )


def test_duplicate_execution_groups_fail(
    real_coverage: tuple[Path, Path, dict[str, Any], dict[str, Any]], tmp_path: Path
) -> None:
    source, _, receipt, group = real_coverage
    for groups, receipts in (([group, group], {"ordinary": receipt}), ([group], {})):
        with pytest.raises(ValueError):
            combine(groups, receipts, source, receipt["provenance"], tmp_path / "final")


def test_queue_preserves_failures_supersession_and_restart_recovery(
    tmp_path: Path,
) -> None:
    a, b, c = (value * 40 for value in "abc")
    queue = Queue(tmp_path, quiet_seconds=10)
    queue.observe([a], 100)
    assert queue.ready(109) is None
    queue.observe([a, b], 105)
    assert queue.data["commits"][a]["state"] == "superseded"
    assert queue.ready(115) == b
    queue.start(b, {"pid": 999999999, "birth": 0})
    recovered = Queue(tmp_path, quiet_seconds=10)
    recovered.recover()
    assert recovered.data["commits"][b]["attempts"][-1]["status"] == "interrupted"
    recovered.start(b, {"pid": os.getpid(), "birth": psutil.Process().create_time()})
    recovered.finish(b, "failed", exit_code=7)
    recovered.observe([b, c], 120)
    assert recovered.data["latest_failure"] == b
    assert recovered.data["commits"][b]["state"] == "failed"
    recovered.start(c, {"pid": os.getpid(), "birth": psutil.Process().create_time()})
    recovered.finish(c, "passed")
    assert recovered.data["latest_failure"] is None
    assert recovered.data["commits"][b]["attempts"][-1]["status"] == "failed"
    recovered.observe([c], 200)
    assert len(recovered.data["commits"]) == 3 and recovered.ready(300) is None


def test_strict_queue_qualifies_each_commit_and_never_borrows_newer_success(
    tmp_path: Path,
) -> None:
    a, b = "a" * 40, "b" * 40
    queue = Queue(tmp_path, strict=True, quiet_seconds=0)
    queue.observe([a, b], 1)
    assert queue.ready(1) == a
    with pytest.raises(ValueError):
        queue.finish(a, "passed")
    queue.start(a, {})
    queue.finish(a, "passed")
    assert queue.ready(2) == b and queue.data["commits"][b]["state"] == "pending"


def test_service_installation_creates_definitions_without_activation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("Installation must not execute systemctl")

    monkeypatch.setattr(subprocess, "run", forbidden)
    install(tmp_path / "units", tmp_path / "state")
    service = (tmp_path / "units/judgmentllm-main.service").read_text()
    assert "KillMode=control-group" in service and " once --state-root " in service
    assert "Persistent=true" in (tmp_path / "units/judgmentllm-main.timer").read_text()


def test_foreground_requests_preempt_background_and_release_capacity(
    tmp_path: Path,
) -> None:
    request = tmp_path / "foreground-test.json"
    request.write_text(
        json.dumps({"pid": os.getpid(), "birth": psutil.Process().create_time()})
    )
    assert foreground_pending(tmp_path)
    with pytest.raises(BlockingIOError), QualificationLease(tmp_path, background=True):
        pass
    request.unlink()
    with QualificationLease(tmp_path, background=True):
        with pytest.raises(TimeoutError), QualificationLease(tmp_path, background=True):
            pass
    with QualificationLease(tmp_path, timeout=0):
        assert foreground_pending(tmp_path)
    assert not foreground_pending(tmp_path)
    assert json.loads((tmp_path / "owner.json").read_text())["active"] is False


@pytest.mark.parametrize("cancelled", [False, True])
def test_owned_stage_accounts_for_real_children_and_cancellation(
    tmp_path: Path, cancelled: bool
) -> None:
    if sys.platform != "linux":
        pytest.skip("Linux local orchestration")
    child_identity = tmp_path / "child.json"
    program = "import subprocess,sys,time,json; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); open(sys.argv[1],'w').write(json.dumps({'pid':p.pid})); time.sleep(60)"
    if not cancelled:
        program = "import subprocess,sys; subprocess.run([sys.executable,'-c','assert 2+2==4'], check=True)"
    cancel = threading.Event()

    def interrupt() -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not child_identity.is_file():
            time.sleep(0.02)
        cancel.set()

    thread = threading.Thread(target=interrupt) if cancelled else None
    if thread:
        thread.start()
    with (tmp_path / "log").open("w") as stream:
        code, receipt = owned_run(
            [sys.executable, "-B", "-c", program, str(child_identity)],
            cwd=tmp_path,
            env=clean_environment(),
            stream=stream,
            evidence=tmp_path / "owner",
            cancel=cancel,
        )
    if thread:
        thread.join(timeout=12)
    assert receipt["cleanup_complete"] is True
    assert (code != 0) is cancelled
    if cancelled:
        assert receipt["cancelled"] is True
        assert not psutil.pid_exists(json.loads(child_identity.read_text())["pid"])


def test_resource_measurements_never_replace_required_functional_coverage(
    real_coverage: tuple[Path, Path, dict[str, Any], dict[str, Any]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import hashlib
    import tomllib

    from coverage_data import report
    from resource_coverage import admit_vm, assess
    from resource_vm import decide

    source, data, _, _ = real_coverage
    monkeypatch.chdir(source)
    measured = tmp_path / "functional.json"
    report(data, source, measured)
    coverage = json.loads(measured.read_text())
    settings = tomllib.loads((source / "pyproject.toml").read_text())
    baseline = tmp_path / "baseline.json"
    baseline.write_text(
        json.dumps(
            {
                "measurement_settings": settings["tool"]["coverage"],
                "pytest_addopts": "",
                "totals": coverage["totals"],
                "files": {
                    name: {
                        "source_sha256": hashlib.sha256(
                            (source / name).read_bytes()
                        ).hexdigest(),
                        "denominators": {
                            key: item["summary"][key]
                            for key in ("num_statements", "num_branches")
                        },
                        "executed_lines": item["executed_lines"],
                        "executed_branches": item["executed_branches"],
                        "excluded_lines": item["excluded_lines"],
                    }
                    for name, item in coverage["files"].items()
                },
            }
        )
    )
    head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=source, text=True
    ).strip()
    qualified = assess(measured, baseline, source, head)
    assert qualified["path"] == "A"
    admit_vm(
        qualified,
        source_sha=head,
        isolated=True,
        thresholds_passed=True,
        performance_passed=True,
    )
    for key in ("isolated", "thresholds_passed", "performance_passed"):
        flags = dict(isolated=True, thresholds_passed=True, performance_passed=True)
        flags[key] = False
        with pytest.raises(ValueError):
            admit_vm(qualified, source_sha=head, **flags)
    partial = copy.deepcopy(coverage)
    partial["files"]["probe.py"]["executed_lines"].remove(10)
    partial["files"]["probe.py"]["missing_lines"].append(10)
    for summary in (partial["files"]["probe.py"]["summary"], partial["totals"]):
        summary["covered_lines"] -= 1
        summary["missing_lines"] += 1
    missing = tmp_path / "missing-functional.json"
    missing.write_text(json.dumps(partial))
    rejected = assess(
        missing, baseline, source, head, complete=measured, resource_data=data
    )
    assert rejected["path"] == "B" and rejected["vm_coverage_eligible"] is False
    assert rejected["resource_contributions"][0]["lines"] == [10]
    assert rejected["resource_contributions"][0]["cases"]
    rejected["resource_measurement_receipts"] = [
        {"status": "passed", "coverage": "complete"}
    ]
    with pytest.raises(ValueError):
        admit_vm(
            rejected,
            source_sha=head,
            isolated=True,
            thresholds_passed=True,
            performance_passed=True,
        )
    with pytest.raises(ValueError):
        admit_vm(
            qualified,
            source_sha="f" * 40,
            isolated=True,
            thresholds_passed=True,
            performance_passed=True,
        )
    decision = decide(
        {
            "kvm_access": True,
            "qemu": "/usr/bin/qemu-system-x86_64",
            "physical_cores": list(range(16)),
            "resources": {"available_memory_bytes": 100 * 1024**3},
            "storage": [{"writable": True, "free_bytes": 200 * 1024**3}],
        },
        rejected,
    )
    assert decision["decision"] == "no-go" and decision["adopted"] is False


def test_parent_death_cleans_owned_children_and_cannot_pass(tmp_path: Path) -> None:
    if sys.platform != "linux":
        return
    local = Path(__file__).resolve().parent
    identity = tmp_path / "child.json"
    launcher = tmp_path / "launch.py"
    launcher.write_text(
        "from pathlib import Path\nimport sys\nfrom process import run\nfrom environment import clean_environment\nroot=Path(sys.argv[1])\nprogram=\"import os,json,time; from pathlib import Path; Path('child.json').write_text(json.dumps({'pid':os.getpid()})); time.sleep(60)\"\nwith (root/'log').open('w') as stream: run([sys.executable,'-B','-c',program],cwd=root,env=clean_environment(),stream=stream,evidence=root/'owner')\n"
    )
    env = {
        **clean_environment(),
        "PYTHONPATH": os.pathsep.join((str(local), str(local.parent))),
    }
    owner = subprocess.Popen(
        [sys.executable, "-B", str(launcher), str(tmp_path)], env=env
    )
    try:
        deadline = time.monotonic() + 15
        while not identity.is_file() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert identity.is_file()
        pid = json.loads(identity.read_text())["pid"]
        owner.kill()
        owner.wait(timeout=5)
        receipt = tmp_path / "owner/result.json"
        deadline = time.monotonic() + 15
        while not receipt.is_file() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert receipt.is_file()
        record = json.loads(receipt.read_text())
        assert record["cancelled"] is True and record["cleanup_complete"] is True
        assert not psutil.pid_exists(pid)
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=5)


def test_serial_placement_defaults_and_invalidates_changed_dependencies(
    tmp_path: Path,
) -> None:
    import hashlib

    from policy import serial_groups

    dependency = tmp_path / "test.py"
    dependency.write_text("original fixture")
    node = "tests/test_example.py::test_safe"
    policy = {
        "serial": [
            {
                "selector": node,
                "placement": "concurrency-safe-serial",
                "reason": "independent stores and process ownership",
                "dependencies": {
                    "test.py": hashlib.sha256(dependency.read_bytes()).hexdigest()
                },
            }
        ]
    }
    groups = serial_groups(
        [node, "tests/test_other.py::test_cold_sdk"], policy, tmp_path
    )
    assert groups["concurrency-safe-serial"] == [node]
    assert groups["globally-exclusive-serial"] == ["tests/test_other.py::test_cold_sdk"]
    dependency.write_text("changed fixture")
    assert serial_groups([node], policy, tmp_path)["globally-exclusive-serial"] == [
        node
    ]


def test_interference_or_duplicate_work_cannot_claim_exclusive_pass(
    tmp_path: Path,
) -> None:
    from orchestrator import Stage, run, validate_execution

    record_path = tmp_path / "execution.json"

    def passed() -> dict[str, Any]:
        time.sleep(0.02)
        return {"exit_code": 0}

    stages = [
        Stage("ordinary", passed),
        Stage("serial-host", passed, ("ordinary",)),
        Stage("serial-global", passed, ("serial-host",)),
        Stage("resource", passed, ("serial-global",)),
    ]
    assert (
        run(
            stages,
            cpus=4,
            memory_bytes=1024,
            output=record_path,
            cancel=threading.Event(),
        )["status"]
        == "passed"
    )
    record = json.loads(record_path.read_text())
    groups = {
        "ordinary": "ordinary",
        "serial-host": "serial",
        "serial-global": "serial",
        "resource": "resource",
    }
    validate_execution(record, groups, [], "concurrent")
    for name in ("serial-host", "serial-global", "resource"):
        bad = copy.deepcopy(record)
        bad["stages"][name]["start_seconds"] = record["stages"]["ordinary"][
            "start_seconds"
        ]
        with pytest.raises(ValueError):
            validate_execution(bad, groups, [], "concurrent")
    bad = copy.deepcopy(record)
    del bad["stages"]["serial-global"]
    with pytest.raises(ValueError):
        validate_execution(bad, groups, [], "concurrent")


def test_reviewed_resource_map_selects_affected_cases_and_defaults_to_complete() -> (
    None
):
    from policy import read, resource_scope
    from selection import inventory_items, matches

    root = Path(__file__).resolve().parents[1]
    policy = read(root)
    items = inventory_items(json.loads((root / "test-inventory.json").read_text()))
    resource_nodes = [item["nodeid"] for item in items if "resource" in item["markers"]]
    for binding in policy["resource_sources"].values():
        for selector in binding["selectors"]:
            assert any(matches(node, selector) for node in resource_nodes), selector
    selected, _ = resource_scope(
        ["src/judgmentllm/providers/openai_schema.py"],
        policy,
        full=False,
        sensitive=False,
    )
    assert selected and "tests/test_openai_resources.py" in selected
    assert (
        resource_scope(
            ["src/judgmentllm/unknown.py"], policy, full=False, sensitive=False
        )[0]
        is None
    )
    assert (
        resource_scope(["ci/local/suite.py"], policy, full=False, sensitive=True)[0]
        is None
    )
    assert resource_scope([], policy, full=True, sensitive=False)[0] is None


@pytest.mark.parametrize(
    "fault", ["r-crash", "missing-receipt", "stale-receipt", "duplicate-output"]
)
def test_concurrent_r_gates_cannot_borrow_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    import acceptance
    from orchestrator import Stage, run
    from r_evidence import validate

    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)

    source = tmp_path / "source"
    (source / "examples").mkdir(parents=True)
    (source / "scripts").mkdir()
    fixture = "from pathlib import Path\nimport argparse\np=argparse.ArgumentParser();p.add_argument('--output');p.add_argument('--phase');a=p.parse_args();root=Path(a.output);root.mkdir(exist_ok=True);(root/(a.phase or 'export')).write_text('offline fixture')\n"
    (source / "examples/export_r.py").write_text(fixture)
    (source / "scripts/persistence_r_fixture.py").write_text(fixture)
    for name in ("r_interoperability.R", "persistence_interoperability.R"):
        (source / "examples" / name).write_text(
            "# synthetic subprocess fault harness\n"
        )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    rscript = bin_dir / "Rscript"
    rscript.write_text(
        f"#!{Path(sys.executable).resolve()}\nimport sys,time\ntime.sleep(.05)\nsys.exit(17 if {fault == 'r-crash'} and any('persistence' in arg for arg in sys.argv[1:]) else 0)\n"
    )
    rscript.chmod(0o700)
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    freeze(source)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("JUDGMENTLLM_CI_RUN_ID", "r-faults")
    cancel = threading.Event()
    roots = {name: tmp_path / name for name in ("exports", "persistence")}
    if fault == "duplicate-output":
        roots["persistence"] = roots["exports"]

    def gate(name: str) -> dict[str, Any]:
        return {
            "exit_code": acceptance.r_gate(name, source, roots[name], cancel=cancel)
        }

    result = run(
        [Stage(name, lambda name=name: gate(name), kind="r") for name in roots],
        cpus=2,
        memory_bytes=1024,
        output=tmp_path / "execution.json",
        cancel=cancel,
        r_workers=2,
    )
    if fault in {"r-crash", "duplicate-output"}:
        assert result["status"] == "failed"
    else:
        assert result["status"] == "passed"
        path = roots["persistence"] / "receipt.json"
        record = json.loads(path.read_text())
        if fault == "missing-receipt":
            path.unlink()
            with pytest.raises(FileNotFoundError):
                json.loads(path.read_text())
        else:
            record["provenance"]["qualification_run_id"] = "stale"
            valid = json.loads((roots["exports"] / "receipt.json").read_text())
            with pytest.raises(ValueError):
                validate(
                    "persistence", roots["persistence"], record, valid["provenance"]
                )


def test_unqualified_or_noisy_scheduling_evidence_never_promotes_default() -> None:
    from orchestrator import choose

    certificate: dict[str, Any] = {
        "schema_version": 1,
        "policy_sha256": "p",
        "machine": {"cpu": 16},
        "equivalent": True,
        "cleanup_passed": True,
        "ordinary_workers": 10,
        "trials": [
            {"profile": profile, "seconds": seconds, "workers": workers, "passed": True}
            for profile, seconds, workers in (
                ("conservative", 100, 12),
                ("conservative", 102, 12),
                ("concurrent", 70, 10),
                ("concurrent", 71, 10),
            )
        ],
    }
    assert (
        choose("auto", certificate=certificate, machine={"cpu": 16}, policy_hash="p")[0]
        == "concurrent"
    )
    noisy = copy.deepcopy(certificate)
    noisy["trials"][-1]["seconds"] = 101
    assert (
        choose("auto", certificate=noisy, machine={"cpu": 16}, policy_hash="p")[0]
        == "conservative"
    )
    for key in ("equivalent", "cleanup_passed"):
        bad = {**certificate, key: False}
        assert (
            choose("auto", certificate=bad, machine={"cpu": 16}, policy_hash="p")[0]
            == "conservative"
        )


def test_empty_passing_group_cannot_supply_or_conceal_required_coverage(
    real_coverage: tuple[Path, Path, dict[str, Any], dict[str, Any]], tmp_path: Path
) -> None:
    source, original, record, group = real_coverage
    empty = tmp_path / "empty"
    data = CoverageData(basename=str(empty))
    assert data.measured_contexts() == set()
    data.write()
    receipt = copy.deepcopy(record)
    receipt["execution_group"] = "resource"
    prepare_config(source, tmp_path, record["provenance"], "resource")
    for contributor in receipt["coverage_contributors"]:
        contributor["contexts"] = []
        contributor["static_context"] = context_name(record["provenance"], "resource")
    zero = seal(empty, receipt, source, record["provenance"], "resource")
    assert zero["no_production_hits"] is True and zero["sources"] == {}
    combine(
        [group, zero],
        {"ordinary": record, "resource": receipt},
        source,
        record["provenance"],
        tmp_path / "combined",
    )
    combined = CoverageData(basename=str(tmp_path / "combined"))
    combined.read()
    actual = CoverageData(basename=str(original))
    actual.read()
    assert combined.arcs("probe.py") == actual.arcs("probe.py")
    with pytest.raises(ValueError):
        seal(empty, record, source, record["provenance"], "ordinary")


def test_background_scheduler_yields_without_suspending_active_tests(
    tmp_path: Path,
) -> None:
    from orchestrator import Stage, run

    requested = threading.Event()
    events: list[str] = []

    def first() -> dict[str, Any]:
        events.append("first-complete")
        requested.set()
        return {"exit_code": 0}

    result = run(
        [
            Stage("first", first),
            Stage(
                "second",
                lambda: events.append("forbidden") or {"exit_code": 0},
                ("first",),
            ),
        ],
        cpus=1,
        memory_bytes=0,
        output=tmp_path / "execution.json",
        cancel=threading.Event(),
        yield_at_boundary=requested.is_set,
    )
    assert result["status"] == "interrupted"
    assert result["stages"]["first"]["status"] == "passed"
    assert result["stages"]["second"]["status"] == "not-executed"
    assert events == ["first-complete"]


def test_performance_equivalence_does_not_reuse_verification_across_code_changes(
    tmp_path: Path,
) -> None:
    from profile_bundle import operational_fingerprint

    source = tmp_path / "source"
    (source / "src").mkdir(parents=True)
    (source / "README.md").write_text("Initial narrative\n")
    (source / "src/code.py").write_text("value = 1\n")
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    base = freeze(source)
    original = operational_fingerprint(source, base)
    (source / "README.md").write_text("Measured runtime narrative\n")
    prose = freeze(source)
    assert prose != base and operational_fingerprint(source, prose) == original
    (source / "src/code.py").write_text("value = 2\n")
    changed = freeze(source)
    assert operational_fingerprint(source, changed) != original
    with pytest.raises(ValueError):
        operational_fingerprint(source, "--help")


@pytest.mark.parametrize(
    "fault",
    ["none", "missing-report", "malformed-report", "changed-report", "failed-group"],
)
def test_independent_admission_rebuilds_exact_authorized_data(
    real_coverage: tuple[Path, Path, dict[str, Any], dict[str, Any]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    import coverage_equivalence
    from coverage_data import checksum, report, validate_suite_coverage

    source, original, receipt, _ = real_coverage
    monkeypatch.chdir(source)
    root = tmp_path / "suite"
    directory = root / "coverage-groups/ordinary"
    directory.mkdir(parents=True)
    data = directory / ".coverage"
    data.write_bytes(original.read_bytes())
    data.with_name("pyproject.toml").write_bytes(
        original.with_name("pyproject.toml").read_bytes()
    )
    group = seal(data, receipt, source, receipt["provenance"], "ordinary")
    manifest = combine(
        [group],
        {"ordinary": receipt},
        source,
        receipt["provenance"],
        root / ".coverage",
    )
    (root / "ordinary.json").write_text(json.dumps(receipt))
    report(root / ".coverage", source, root / "coverage.json")
    reference = json.loads((root / "coverage.json").read_text())

    def historical_comparison(
        current: Path, baseline: Path, source: Path, *, reviewed_ref: str
    ) -> dict[str, Any]:
        # The historical comparator has separate regression tests; this boundary checks the actual rebuilt data.
        measured = json.loads(current.read_text())
        assert (
            measured["files"] == reference["files"]
            and reviewed_ref == receipt["provenance"]["source"]["commit"]
        )
        return {"passed": True}

    monkeypatch.setattr(coverage_equivalence, "compare", historical_comparison)
    (root / "coverage-comparison.json").write_text('{"passed": true}')
    manifest["reports"] = {
        name: checksum(root / name)
        for name in ("coverage.json", "coverage.xml", "coverage-comparison.json")
    }
    (root / "coverage-manifest.json").write_text(json.dumps(manifest))
    plan = {
        "coverage_mode": "historical",
        "source_sha": receipt["provenance"]["source"]["commit"],
    }
    if fault == "missing-report":
        (root / "coverage.json").unlink()
    elif fault == "malformed-report":
        (root / "coverage.json").write_text("{broken")
    elif fault == "changed-report":
        changed = copy.deepcopy(reference)
        changed["files"]["probe.py"]["executed_lines"] = []
        (root / "coverage.json").write_text(json.dumps(changed))
        manifest["reports"]["coverage.json"] = checksum(root / "coverage.json")
        (root / "coverage-manifest.json").write_text(json.dumps(manifest))
    elif fault == "failed-group":
        changed = copy.deepcopy(receipt)
        changed["exit_code"] = 1
        (root / "ordinary.json").write_text(json.dumps(changed))
    if fault == "none":
        assert validate_suite_coverage(
            root, source, plan, receipt["provenance"], {"ordinary"}
        ) == {"passed": True}
    else:
        with pytest.raises((ValueError, OSError)):
            validate_suite_coverage(
                root, source, plan, receipt["provenance"], {"ordinary"}
            )


@pytest.mark.parametrize("field", ["attempt", "run", "source", "guest"])
def test_data_namespace_rejects_foreign_binary_even_with_rewritten_receipt(
    real_coverage: tuple[Path, Path, dict[str, Any], dict[str, Any]],
    tmp_path: Path,
    field: str,
) -> None:
    source, original, receipt, _ = real_coverage
    identity = copy.deepcopy(receipt["provenance"])
    if field == "attempt":
        identity["attempt_id"] = "another-attempt"
    elif field == "run":
        identity["qualification_run_id"] = "another-run"
    elif field == "source":
        identity["source"]["commit"] = "a" * 40
    else:
        identity["interpreter"]["prefix"] = "/guest/environment"
    declared = copy.deepcopy(receipt)
    declared["provenance"] = identity
    for contributor in declared["coverage_contributors"]:
        contributor["static_context"] = context_name(identity, "ordinary")
    data = tmp_path / ".coverage"
    data.write_bytes(original.read_bytes())
    prepare_config(source, tmp_path, identity, "ordinary")
    with pytest.raises(ValueError, match="Foreign source, run, group or attempt"):
        seal(data, declared, source, identity, "ordinary")
