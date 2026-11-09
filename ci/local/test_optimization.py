"""Failure-oriented acceptance for partitioning, budgets and override recovery."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tarfile
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NamedTuple, cast

import pytest
from benchmark import total_cpu
from coverage_equivalence import compare
from override_recovery import Allocation, Observation, RecoveryJournal, require_restored
from provenance import (
    RUN_ID_ENV,
    receipt_provenance,
    source_record,
    verify_preflight_provenance,
)
from resource_budget import ResourceLease
from scheduling import get_schedule
from suite import outcomes, partition, verify_contract, verify_skips
from telemetry import ProcessSampler


class FakeControl:
    mechanism = "synthetic-per-run-control"

    def __init__(self) -> None:
        self.current = Allocation(6, 16384)
        self.calls: list[Allocation] = []
        self.idle = True
        self.owned = True
        self.identity = "existing-macos-guest"
        self.partial = False
        self.restore_failure = False

    def inspect(self) -> Observation:
        return Observation(self.identity, self.current, self.idle, self.owned)

    def apply(self, allocation: Allocation) -> None:
        self.calls.append(allocation)
        if self.restore_failure and allocation.cpus == 6:
            raise OSError("synthetic restore failure")
        if self.partial and allocation.cpus == 8:
            self.current = Allocation(8, 16384)
            raise OSError("synthetic partial apply")
        self.current = allocation


def journal(tmp_path: Path, control: FakeControl) -> RecoveryJournal:
    return RecoveryJournal.begin(
        tmp_path,
        guest="macos",
        experiment="synthetic-1",
        identity=control.identity,
        original=control.current,
        temporary=Allocation(8, 24576),
        mechanism=control.mechanism,
        owner="pid-123-start-token",
        lock_identity="existing-lock-device-inode",
    )


@pytest.mark.parametrize("failure", [None, RuntimeError, KeyboardInterrupt, SystemExit])
def test_override_restores_after_every_handled_exit(
    tmp_path: Path, failure: type[BaseException] | None
) -> None:
    control = FakeControl()
    record = journal(tmp_path, control)

    def experiment() -> None:
        durable = json.loads(record.path.read_text())
        assert durable["state"] == "applied"
        assert control.current == Allocation(8, 24576)
        if failure is not None:
            raise failure("synthetic failure")

    if failure is None:
        record.execute(control, lambda: None, experiment)
    else:
        with pytest.raises(failure):
            record.execute(control, lambda: None, experiment)
    assert control.current == Allocation(6, 16384)
    assert record.value["history"] == [
        "planned",
        "applying",
        "applied",
        "restoring",
        "restored",
    ]
    require_restored(tmp_path)


def test_partially_applied_override_is_restored(tmp_path: Path) -> None:
    control = FakeControl()
    control.partial = True
    record = journal(tmp_path, control)
    with pytest.raises(OSError, match="partial apply"):
        record.execute(control, lambda: None, lambda: None)
    assert control.current == Allocation(6, 16384)
    assert record.value["restoration_verified"] is True


def test_crashed_override_blocks_until_actual_settings_are_reconciled(
    tmp_path: Path,
) -> None:
    control = FakeControl()
    record = journal(tmp_path, control)
    record.transition("applying")
    control.current = Allocation(8, 16384)
    # Simulate an abrupt death after only CPU allocation changed. A fresh reader
    # must inspect actual values under a newly verified ownership lock.
    with pytest.raises(RuntimeError, match="Unrestored"):
        require_restored(tmp_path)
    recovered = RecoveryJournal.load(record.path)
    recovered.restore(control, lambda: None)
    assert control.calls == [Allocation(6, 16384)]
    require_restored(tmp_path)


def test_failed_restore_and_lost_ownership_never_claim_restoration(
    tmp_path: Path,
) -> None:
    control = FakeControl()
    record = journal(tmp_path, control)
    control.current = Allocation(8, 24576)
    control.restore_failure = True
    with pytest.raises(OSError, match="restore failure"):
        record.restore(control, lambda: None)
    assert record.value["state"] == "needs_review"
    assert record.value["restoration_verified"] is False
    with pytest.raises(RuntimeError, match="Unrestored"):
        require_restored(tmp_path)
    control.restore_failure = False
    control.owned = False
    with pytest.raises(RuntimeError, match="ownership"):
        record.restore(control, lambda: None)
    assert control.current == Allocation(8, 24576)


@pytest.mark.parametrize(
    "changed", ["identity", "idle", "ownership", "lock", "allocation"]
)
def test_unsafe_override_preflight_never_changes_allocations(
    tmp_path: Path, changed: str
) -> None:
    control = FakeControl()
    record = journal(tmp_path, control)
    if changed == "identity":
        control.identity = "unrelated-guest"
    elif changed == "idle":
        control.idle = False
    elif changed == "ownership":
        control.owned = False
    elif changed == "allocation":
        control.current = Allocation(4, 16384)

    def lock() -> None:
        if changed == "lock":
            raise RuntimeError("lost lock")

    with pytest.raises(RuntimeError):
        record.execute(control, lock, lambda: None)
    assert not control.calls


def test_missing_journal_fields_and_duplicate_experiment_fail_closed(
    tmp_path: Path,
) -> None:
    control = FakeControl()
    record = journal(tmp_path, control)
    with pytest.raises(FileExistsError):
        journal(tmp_path, control)
    record.path.write_text('{"state":"restored"}')
    with pytest.raises(ValueError):
        require_restored(tmp_path)


def test_resource_budget_bounds_concurrency_and_releases_after_failure(
    tmp_path: Path,
) -> None:
    with ResourceLease(tmp_path, 2, timeout=0) as first:
        with ResourceLease(tmp_path, 2, timeout=0) as second:
            assert first.slot != second.slot
            with pytest.raises(TimeoutError):
                with ResourceLease(tmp_path, 2, timeout=0):
                    pass
    with pytest.raises(RuntimeError):
        with ResourceLease(tmp_path, 1, timeout=0):
            raise RuntimeError("synthetic workload failure")
    with ResourceLease(tmp_path, 1, timeout=0):
        pass


def test_os_reclaims_resource_slot_after_owner_process_death(tmp_path: Path) -> None:
    program = "from pathlib import Path; from resource_budget import ResourceLease; import sys; lease=ResourceLease(Path(sys.argv[1]),1); lease.__enter__(); print('held',flush=True); sys.stdin.read(1)"
    process = subprocess.Popen(
        [sys.executable, "-c", program, str(tmp_path)],
        cwd=Path(__file__).parent,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "held"
        with pytest.raises(TimeoutError):
            with ResourceLease(tmp_path, 1, timeout=0):
                pass
        process.kill()
        process.communicate(timeout=10)
        with ResourceLease(tmp_path, 1, timeout=0):
            pass
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)


def test_partitions_preserve_every_case_once() -> None:
    items: list[dict[str, Any]] = [
        {"nodeid": "ordinary", "markers": []},
        {"nodeid": "serial", "markers": ["serial"]},
        {"nodeid": "resource", "markers": ["resource", "serial"]},
    ]
    assert partition(items) == {
        "ordinary": ["ordinary"],
        "serial": ["serial"],
        "resource": ["resource"],
    }
    with pytest.raises(ValueError, match="Duplicate"):
        partition([items[0], items[0]])


@pytest.mark.parametrize(
    "damage", ["missing", "duplicate", "unknown", "xfail", "no_call", "no_teardown"]
)
def test_missing_or_duplicate_execution_cannot_pass(damage: str) -> None:
    reports: list[dict[str, Any]] = [
        {"nodeid": "case", "phase": phase, "outcome": "passed", "xfail": False}
        for phase in ["setup", "call", "teardown"]
    ]
    receipt: dict[str, Any] = {"collected": [{"nodeid": "case"}], "reports": reports}
    if damage == "missing":
        reports.clear()
    elif damage == "duplicate":
        reports.append(reports[1])
    elif damage == "unknown":
        reports[0]["nodeid"] = "unexpected"
    elif damage == "xfail":
        reports[1]["xfail"] = True
    else:
        reports[:] = [
            report
            for report in reports
            if report["phase"] != ("call" if damage == "no_call" else "teardown")
        ]
    with pytest.raises(ValueError):
        outcomes(receipt, {"case"})


def test_case_failure_and_dependency_skip_are_not_lost() -> None:
    receipt: dict[str, Any] = {
        "collected": [{"nodeid": "failed"}, {"nodeid": "skipped"}],
        "reports": [
            {"nodeid": node, "phase": phase, "outcome": outcome, "xfail": False}
            for node, phase, outcome in [
                ("failed", "setup", "passed"),
                ("failed", "call", "failed"),
                ("failed", "teardown", "passed"),
                ("skipped", "setup", "skipped"),
                ("skipped", "teardown", "passed"),
            ]
        ],
    }
    assert outcomes(receipt, {"failed", "skipped"}) == {"failed": 1, "skipped": 1}


@pytest.fixture
def preflight_identity(tmp_path: Path) -> dict[str, Any]:
    source = tmp_path / "source"
    source.mkdir()
    (source / ".judgmentllm-source.json").write_text(
        json.dumps({"commit": "a" * 40, "archive_sha256": "b" * 64})
    )
    (source / "uv.lock").write_bytes(b"synthetic locked environment\n")
    return receipt_provenance(source_record(source), "current-qualification-run")


def test_preflight_accepts_matching_actual_provenance(
    preflight_identity: dict[str, Any],
) -> None:
    receipt = json.loads(json.dumps({"provenance": preflight_identity}))
    verify_preflight_provenance(receipt, preflight_identity)
    assert preflight_identity["interpreter"]["version"] == sys.version
    assert preflight_identity["interpreter"]["prefix"] == os.path.normcase(
        str(Path(sys.prefix).resolve())
    )


@pytest.mark.parametrize(
    "field,key,value",
    [
        ("source", "commit", "c" * 40),
        ("source", "archive_sha256", "c" * 64),
        ("source", "manifest_sha256", "c" * 64),
        ("source", "dirty", 0),
        ("lockfile_sha256", None, "c" * 64),
        ("interpreter", "implementation", "different-python"),
        ("interpreter", "version", "different-version"),
        ("interpreter", "executable", "/different/python"),
        ("interpreter", "prefix", "/different/venv"),
        ("interpreter", "platform", "different-platform"),
        ("interpreter", "dependencies", {"pytest": "different-version"}),
        ("qualification_run_id", None, "previous-qualification-run"),
    ],
)
def test_preflight_rejects_foreign_or_stale_provenance(
    preflight_identity: dict[str, Any], field: str, key: str | None, value: Any
) -> None:
    receipt = json.loads(json.dumps({"provenance": preflight_identity}))
    if key is None:
        receipt["provenance"][field] = value
    else:
        receipt["provenance"][field][key] = value
    with pytest.raises(ValueError, match=f"provenance differs: {field}"):
        verify_preflight_provenance(receipt, preflight_identity)


@pytest.mark.parametrize(
    "missing",
    [None, "source", "lockfile_sha256", "interpreter", "qualification_run_id"],
)
def test_preflight_rejects_missing_provenance(
    preflight_identity: dict[str, Any], missing: str | None
) -> None:
    receipt = json.loads(json.dumps({"provenance": preflight_identity}))
    if missing is None:
        receipt.pop("provenance")
    else:
        receipt["provenance"].pop(missing)
    with pytest.raises(ValueError, match="missing or invalid provenance"):
        verify_preflight_provenance(receipt, preflight_identity)


@pytest.mark.parametrize(
    "field,key,value",
    [
        ("source", "commit", None),
        ("source", "dirty", True),
        ("lockfile_sha256", None, None),
        ("qualification_run_id", None, None),
        ("qualification_run_id", None, ""),
    ],
)
def test_preflight_cannot_credit_matching_unknown_or_dirty_identity(
    preflight_identity: dict[str, Any], field: str, key: str | None, value: Any
) -> None:
    if key is None:
        preflight_identity[field] = value
    else:
        preflight_identity[field][key] = value
    with pytest.raises(ValueError, match="known, clean source/lock/run identities"):
        verify_preflight_provenance(
            {"provenance": preflight_identity}, preflight_identity
        )


@pytest.mark.parametrize(
    "fault,distribution",
    [
        (fault, "loadfile")
        for fault in ("none", "failed", "worker_crash", "preflight", "stale_preflight")
    ]
    + [(fault, "worksteal") for fault in ("none", "failed")],
)
def test_real_distributed_runner_accounts_for_every_partition(
    tmp_path: Path, fault: str, distribution: str
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\nmarkers=["resource", "serial"]\nasyncio_default_fixture_loop_scope="function"\n'
    )
    code = """import os
import pytest
def test_ordinary():
    assert os.environ.get("PYTEST_XDIST_WORKER") is not None
@pytest.mark.serial
def test_serial():
    assert os.environ.get("PYTEST_XDIST_WORKER") is None
@pytest.mark.resource
def test_resource(tmp_path):
    assert os.environ.get("PYTEST_XDIST_WORKER") is None
    (tmp_path / "measurement.json").write_text('{"peak":64}')
"""
    if fault == "failed":
        code += '\ndef test_failure():\n    assert False, "synthetic fault"\n'
    elif fault == "worker_crash":
        code += "\ndef test_crash():\n    os._exit(7)\n"
    (source / "test_contract.py").write_text(code)
    preflights: list[str] = []
    run_identity = f"synthetic-run-{fault}-{distribution}"
    if fault in {"preflight", "stale_preflight"}:
        (source / ".judgmentllm-source.json").write_text(
            json.dumps({"commit": "a" * 40, "archive_sha256": "b" * 64})
        )
        (source / "uv.lock").write_bytes(b"synthetic locked environment\n")
        receipt = tmp_path / "preflight.json"
        environment = dict(os.environ)
        environment[RUN_ID_ENV] = run_identity
        environment["PATH"] = ""
        environment.pop("PYTEST_XDIST_WORKER", None)
        environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
        preflight = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "--no-cov",
                "-p",
                "ci.local.pytest_report",
                f"--ci-report={receipt}",
                "test_contract.py::test_serial",
                "-q",
            ],
            cwd=source,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert preflight.returncode == 0, preflight.stdout + preflight.stderr
        recorded = json.loads(receipt.read_text())
        assert recorded["provenance"] == receipt_provenance(
            source_record(source), run_identity
        )
        if fault == "stale_preflight":
            recorded["provenance"]["qualification_run_id"] = "previous-run"
            receipt.write_text(json.dumps(recorded))
        preflights = ["--preflight-report", str(receipt)]
    output = tmp_path / "evidence"
    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).with_name("suite.py")),
            "--source",
            str(source),
            "--output",
            str(output),
            "--workers",
            "2",
            "--distribution",
            distribution,
            "--no-coverage",
            *preflights,
            "test_contract.py",
        ],
        capture_output=True,
        text=True,
        timeout=30,
        env={
            **os.environ,
            RUN_ID_ENV: run_identity,
            "PYTEST_XDIST_WORKER": "outer-worker",
            "PATH": "",
        },
    )
    assert (output / "summary.json").is_file(), result.stdout + result.stderr
    summary = json.loads((output / "summary.json").read_text())
    assert summary["source"]["commit"] == ("a" * 40 if preflights else None)
    assert (result.returncode == 0) == (fault in {"none", "preflight"}), (
        result.stdout + result.stderr
    )
    if fault in {"worker_crash", "stale_preflight"}:
        assert summary["status"] == "failed"
        assert summary["manifest_complete"] is False
        if fault == "stale_preflight":
            assert "qualification_run_id" in summary["error"]["message"]
            assert summary["counts"]["passed"] == 0
            assert summary["partitions"] == []
            assert not (output / "ordinary.log").exists()
    else:
        assert summary["manifest_complete"] is True
        assert summary["counts"]["passed"] == 3
        assert summary["counts"]["failed"] == int(fault == "failed")
        assert {stage["name"] for stage in summary["partitions"]} == {
            "ordinary",
            "preflight" if fault == "preflight" else "serial",
            "resource",
        }
        assert summary["reused_preflight_cases"] == int(fault == "preflight")
        preserved = output / "resource-measurements"
        artifact = preserved / "evidence-000.json"
        assert json.loads(artifact.read_text()) == {"peak": 64}
        manifest = json.loads((preserved / "manifest.json").read_text())
        assert len(manifest) == 1
        assert (
            manifest[0]["sha256"] == hashlib.sha256(artifact.read_bytes()).hexdigest()
        )


def test_documented_test_consolidation_preserves_acceptance_without_a_count_gate(
    tmp_path: Path,
) -> None:
    contract = tmp_path / "contract.json"
    data: dict[str, Any] = {
        "schema_version": 1,
        "required_nodes": ["tests/old.py::one", "tests/old.py::two"],
        "replacements": {
            "tests/old.py::one": ["tests/new.py::combined"],
            "tests/old.py::two": ["tests/new.py::combined"],
        },
    }
    contract.write_text(json.dumps(data))
    result = verify_contract(contract, {"tests/new.py::combined"}, ["tests"])
    assert result["required_cases"] == 2
    assert result["replacement_cases"] == ["tests/old.py::one", "tests/old.py::two"]
    del data["replacements"]["tests/old.py::two"]
    contract.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="no documented replacement"):
        verify_contract(contract, {"tests/new.py::combined"}, ["tests"])


@pytest.mark.parametrize(
    "loss",
    ["none", "line", "branch", "source", "exclusions", "denominator", "settings"],
)
def test_equal_percentages_cannot_hide_lost_coverage(tmp_path: Path, loss: str) -> None:
    source = tmp_path / "source"
    source.mkdir()
    module = source / "sample.py"
    module.write_text("first\nsecond\nthird\n")
    (source / "pyproject.toml").write_text(
        "[tool.coverage.run]\nbranch = "
        + ("false" if loss == "settings" else "true")
        + "\n"
    )
    totals = {
        "covered_lines": 9,
        "num_statements": 10,
        "covered_branches": 9,
        "num_branches": 10,
    }
    expected: dict[str, Any] = {
        "measurement_settings": {"run": {"branch": True}},
        "totals": totals,
        "files": {
            "sample.py": {
                "source_sha256": hashlib.sha256(module.read_bytes()).hexdigest(),
                "executed_lines": [1, 2],
                "executed_branches": [[2, 3]],
                "excluded_lines": [3],
                "denominators": {"num_statements": 10, "num_branches": 10},
            }
        },
    }
    measured: dict[str, Any] = {
        "totals": totals,
        "files": {
            "sample.py": {
                "executed_lines": [1, 3] if loss == "line" else [1, 2],
                "executed_branches": [[2, -1]] if loss == "branch" else [[2, 3]],
                "excluded_lines": [3, 4] if loss == "exclusions" else [3],
                "summary": {
                    "num_statements": 11 if loss == "denominator" else 10,
                    "num_branches": 10,
                },
            }
        },
    }
    baseline = tmp_path / "baseline.json"
    current = tmp_path / "current.json"
    baseline.write_text(json.dumps(expected))
    current.write_text(json.dumps(measured))
    if loss == "source":
        module.write_text("changed\nsecond\nthird\n")
    comparison = compare(current, baseline, source)
    assert comparison["passed"] == (loss == "none")
    assert bool(comparison["losses"]) == (loss != "none")


@pytest.mark.parametrize("boundary", ["intent", "partial", "applied"])
def test_crashed_override_is_reconciled_from_durable_actual_settings(
    tmp_path: Path, boundary: str
) -> None:
    settings = tmp_path / "actual.json"
    settings.write_text(json.dumps({"cpus": 6, "ram_mib": 16384}))
    code = """
import json, os, pathlib, sys
from override_recovery import Allocation, Observation, RecoveryJournal
root, boundary = pathlib.Path(sys.argv[1]), sys.argv[2]
settings = root / 'actual.json'
class Control:
    mechanism = 'synthetic-per-run-control'
    def inspect(self):
        return Observation('existing-macos-guest', Allocation(**json.loads(settings.read_text())), True, True)
    def apply(self, allocation):
        value = {'cpus': allocation.cpus, 'ram_mib': allocation.ram_mib}
        if boundary == 'partial':
            value['ram_mib'] = 16384
        with settings.open('w') as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        if boundary == 'partial':
            os._exit(17)
record = RecoveryJournal.begin(root / 'journals', guest='macos', experiment='crashed', identity='existing-macos-guest', original=Allocation(6,16384), temporary=Allocation(8,24576), mechanism=Control.mechanism, owner='synthetic-child', lock_identity='held-existing-lock')
if boundary == 'intent':
    os._exit(17)
record.execute(Control(), lambda: None, lambda: os._exit(17))
"""
    environment = {**os.environ, "PYTHONPATH": str(Path(__file__).parent)}
    child = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path), boundary],
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert child.returncode == 17, child.stderr
    root = tmp_path / "journals"
    with pytest.raises(RuntimeError, match="Unrestored"):
        require_restored(root, {"macos"})
    require_restored(root, {"windows"})

    class FileControl(FakeControl):
        def inspect(self) -> Observation:
            self.current = Allocation(**json.loads(settings.read_text()))
            return super().inspect()

        def apply(self, allocation: Allocation) -> None:
            super().apply(allocation)
            settings.write_text(
                json.dumps({"cpus": allocation.cpus, "ram_mib": allocation.ram_mib})
            )

    record = RecoveryJournal.load(root / "macos-crashed.json")
    record.restore(FileControl(), lambda: None)
    assert json.loads(settings.read_text()) == {"cpus": 6, "ram_mib": 16384}
    assert RecoveryJournal.load(record.path).value["restoration_verified"] is True
    require_restored(root)


def test_skip_policy_rejects_an_unexpected_critical_skip() -> None:
    policy = {"platform_skips": {"win32": ["tests/p.py::posix_only"]}}
    receipt = {
        "reports": [{"nodeid": "tests/p.py::posix_only[x]", "outcome": "skipped"}]
    }
    verify_skips(receipt, policy, platform="win32", require_r=True)
    with pytest.raises(ValueError, match="Unexpected qualification skips"):
        verify_skips(receipt, policy, platform="linux", require_r=True)
    receipt["reports"][0]["nodeid"] = "tests/security.py::credential_isolation"
    with pytest.raises(ValueError, match="credential_isolation"):
        verify_skips(receipt, policy, platform="win32", require_r=True)


def test_process_sampler_measures_owned_child_and_joins_its_thread() -> None:
    with ProcessSampler(interval=0.01) as sampler:
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import sys; data=bytearray(30*1024*1024); data[::4096]=b'x'*(len(data)//4096); print('ready',flush=True); sys.stdin.readline()",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert child.stdout is not None
            assert child.stdout.readline().strip() == "ready"
            sampler.sample()
            child.communicate("done\n", timeout=15)
            assert child.returncode == 0
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=15)
    report = sampler.report()
    assert sampler.peak_children_rss >= 30 * 1024 * 1024
    assert len(sampler.cpu) >= 2
    assert report["samples"] == sampler.samples
    assert isinstance(report["owner_cpu_seconds"], float)
    total = report["visible_memory_total_bytes"]
    available = report["minimum_visible_memory_available_bytes"]
    assert isinstance(total, int) and isinstance(available, int)
    assert 0 < available <= total
    assert sampler.thread is not None and not sampler.thread.is_alive()


def test_sampler_excludes_parent_pid_aliases_and_future_births() -> None:
    class Process:
        def __init__(self, pid: int, created: float, rss: int) -> None:
            self.pid = pid
            self.created = created
            self.rss = rss
            self.descendants: list[Process] = []

        def create_time(self) -> float:
            return self.created

        def children(self, recursive: bool = False) -> list[Process]:
            return self.descendants

        def oneshot(self) -> nullcontext[None]:
            return nullcontext()

        def cpu_times(self) -> SimpleNamespace:
            return SimpleNamespace(user=1.0, system=0.0)

        def memory_info(self) -> SimpleNamespace:
            return SimpleNamespace(rss=self.rss)

    root = Process(1, 90.0, 10)
    root.descendants = [
        Process(2, 50.0, 10000),  # Orphan with a reused parent PID.
        Process(3, 200.0, 10000),  # Creation time before a clock correction.
        Process(4, 100.0, 20),  # The current command's real child.
    ]
    sampler = ProcessSampler(wall_clock=lambda: 100.0)
    sampler.root = cast(Any, root)
    with sampler:
        pass
    report = sampler.report()
    assert report["peak_tree_rss_bytes"] == 30
    assert report["peak_descendant_rss_bytes"] == 20
    assert report["peak_processes"] == 2
    discarded = report["discarded_descendant_samples"]
    assert isinstance(discarded, int) and discarded >= 4


def test_schedule_accounts_for_shared_guests_and_host_headroom() -> None:
    schedule = get_schedule("parallel")
    budget = schedule.budget(["linux", "windows", "macos"], 32, 128 * 1024**3)
    assert budget["cpu_cap_total"] == 28
    assert budget["ram_cap_total_gib"] == 76
    with pytest.raises(ValueError, match="exceeds host"):
        schedule.budget(["linux", "windows", "macos"], 16, 128 * 1024**3)


def test_host_cpu_total_does_not_count_guest_time_twice() -> None:
    class Times(NamedTuple):
        user: float
        idle: float
        guest: float
        guest_nice: float

    assert total_cpu(Times(10, 20, 5, 1)) == 30
    assert total_cpu((10, 20)) == 30


def test_release_profile_rejects_a_different_archive_before_starting_guests(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "candidate.tar"
    with tarfile.open(
        archive, "w", format=tarfile.PAX_FORMAT, pax_headers={"comment": "b" * 40}
    ) as stream:
        stream.addfile(tarfile.TarInfo("placeholder.txt"))
    command = [
        sys.executable,
        str(Path(__file__).resolve().parents[1] / "check.py"),
        "--profile",
        "release",
        "--ref",
        "a" * 40,
        "--baseline-python",
        sys.executable,
        "--candidate-python",
        sys.executable,
        "--baseline-source-archive",
        str(archive),
        "--candidate-source-archive",
        str(archive),
        "--release-evidence",
        str(tmp_path / "evidence"),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=15)
    assert result.returncode != 0
    assert "Candidate source archive must embed the requested commit" in result.stderr
    assert not (tmp_path / "evidence").exists()
