"""Exact changed-code approvals and their raw evidence boundary."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from coverage_authority import (
    EXCEPTIONS,
    FLOOR,
    REGISTRY,
    commit_tree,
    describe,
    exception_entries,
    exception_proof_enabled,
    exception_scope,
    git_bytes,
)
from test_coverage_policy import commit
from test_coverage_reconciliation import (
    MODULE,
    PARSER,
    case,
    comparison,
    measure,
    write,
)

PRIMARY = pytest.mark.skipif(
    sys.platform != "linux"
    or sys.implementation.name != "cpython"
    or sys.version_info[:2] != (3, 11),
    reason="Changed coverage approvals require local CPython 3.11 on Linux",
)
TEST_FILE = "tests/test_probe.py"
PROOF = TEST_FILE + "::test_proof"
PROOF_INTERPRETER = {
    "platform": sys.platform,
    "implementation": sys.implementation.name,
    "version": sys.version,
    "dependencies": {"coverage": PARSER["coverage"]},
}
OLD = """def choose(flag):
    if flag:
        return 1
    return 0

def checked(flag):
    return finish(True)

def finish(flag):
    if flag:
        return True
    return False
"""
NEW = OLD.replace(
    "    if flag:\n        return True\n    return False\n",
    "    if flag is None:\n        raise AssertionError('validated flag')\n"
    "    if flag is True:\n        return True\n",
)
CALLS = [("choose", True), ("choose", False), ("checked", False)]


def changed_case(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    enough_coverage: bool = True,
    new_tail: str = "",
) -> dict[str, Any]:
    # Keep the absolute floor independent of the tested line and arc misses.
    stable = (
        "".join(f"stable_{i} = {i}\n" for i in range(40)) if enough_coverage else ""
    )
    value = case(
        root,
        monkeypatch,
        old=stable + OLD,
        new=stable + NEW + new_tail,
        approved=False,
        old_calls=[("choose", True), ("checked", False)],
        new_calls=CALLS,
    )
    source = value["source"]
    declarations = git_bytes(source, value["base"], REGISTRY)
    assert declarations is not None
    (source / REGISTRY).write_bytes(declarations)
    value["head"] = commit(source)
    return value


def approval(
    value: dict[str, Any], lines: list[int], arcs: list[list[int]]
) -> dict[str, Any]:
    source = value["source"]
    return {
        "scope_sha256": exception_scope(
            source, commit_tree(source, value["head"]), value["policy"]
        ),
        "path": MODULE,
        "lines": lines,
        "arcs": arcs,
        "classification": "unreachable",
        "justification": "The supported checked entry point always passes True to finish.",
        "test_nodeids": [PROOF],
    }


def protect(value: dict[str, Any], entry: dict[str, Any]) -> None:
    source = value["source"]
    write(source / EXCEPTIONS, {"schema_version": 1, "entries": [entry]})
    value["base"] = value["head"] = commit(source)
    value["policy"] = describe(source, value["base"])


def test_empty_exception_registry_supplies_no_approvals() -> None:
    assert exception_entries(b'{"schema_version":1,"entries":[]}') == []


@pytest.mark.parametrize(
    "override, message",
    [
        ({"lines": [True]}, "strict positive integers"),
        ({"arcs": [[3, 0]]}, "strict signed integers"),
        ({"path": "src/judgmentllm/*.py"}, "path or scope"),
        ({"scope_sha256": "*"}, "path or scope"),
        ({"justification": " "}, "justification"),
        ({"test_nodeids": []}, "requires exact proof"),
        ({"test_nodeids": ["tests/test_probe.py::test_*"]}, "proof node ID"),
        ({"approver": "candidate"}, "Malformed"),
    ],
)
def test_exception_registry_rejects_invalid_entry(
    override: dict[str, Any], message: str
) -> None:
    entry = {
        "scope_sha256": "a" * 64,
        "path": MODULE,
        "lines": [4],
        "arcs": [[3, -1]],
        "classification": "redundant",
        "justification": "The validated caller checks the same condition.",
        "test_nodeids": [PROOF],
    }
    raw = {"schema_version": 1, "entries": [{**entry, **override}]}
    with pytest.raises(ValueError, match=message):
        exception_entries(json.dumps(raw).encode())
    with pytest.raises(ValueError, match="Overlapping"):
        exception_entries(json.dumps({**raw, "entries": [entry, entry]}).encode())
    with pytest.raises(ValueError, match="Duplicate evidence key"):
        exception_entries(b'{"schema_version":1,"entries":[],"entries":[]}')


@pytest.mark.parametrize(
    "mode, scope, lane, partition, eligible",
    [
        ("historical", "local", "linux-3.11", "all", True),
        ("targeted", "local", "linux-3.11", "all", False),
        ("historical", "hosted", "linux-3.11", "all", False),
        ("historical", "local", "windows-3.12", "all", False),
        ("historical", "local", "linux-3.11", "ordinary", False),
        (None, "local", "linux-3.11", "all", False),
    ],
)
def test_exception_proof_requires_planned_historical_primary(
    mode: str | None, scope: str, lane: str, partition: str, eligible: bool
) -> None:
    plan = (
        {
            "schema_version": 2,
            "coverage_mode": mode,
            "coverage_policy": {"exceptions_sha256": "a" * 64},
        }
        if mode is not None
        else None
    )
    assert exception_proof_enabled(plan, scope, lane, partition) is eligible


@PRIMARY
@pytest.mark.parametrize(
    "fault",
    [
        "none",
        "candidate",
        "stale",
        "obsolete",
        "historical",
        "floor",
        "exclusion",
        "strict",
    ],
)
def test_exception_only_changes_the_exact_changed_code_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    value = changed_case(
        tmp_path,
        monkeypatch,
        enough_coverage=fault != "floor",
        new_tail="\ndef excluded():  # pragma: no cover\n    return False\n"
        if fault == "exclusion"
        else "",
    )
    strict = comparison(value)
    detail = strict["modified"][0]
    lines, arcs = detail["missing_changed_lines"], detail["missing_changed_branches"]
    assert not strict["passed"] and lines and any(end < 0 for _, end in arcs)
    entry = approval(value, [*lines, 9999] if fault == "obsolete" else lines, arcs)
    if fault == "candidate":
        write(value["source"] / EXCEPTIONS, {"schema_version": 1, "entries": []})
        value["base"] = commit(value["source"])
        value["policy"] = describe(value["source"], value["base"])
        write(value["source"] / EXCEPTIONS, {"schema_version": 1, "entries": [entry]})
        value["head"] = commit(value["source"])
        forged = comparison(
            value,
            coverage_policy=describe(value["source"], value["head"]),
            passing_nodes={PROOF},
            proof_interpreter=PROOF_INTERPRETER,
        )
        assert forged["fatal_error"] == "invalid-authority"
    else:
        protect(value, entry)
    if fault == "stale":
        (value["source"] / "README.md").write_text(
            "Changed outside the approved module"
        )
        value["head"] = commit(value["source"])
    if fault == "historical":
        measure(value["source"], value["current"], CALLS[1:])
    result = comparison(
        value,
        passing_nodes=None if fault == "strict" else {PROOF},
        proof_interpreter=None if fault == "strict" else PROOF_INTERPRETER,
    )
    assert result["passed"] is (fault == "none"), result
    assert result["modified"][0]["missing_changed_lines"] == lines
    assert result["modified"][0]["missing_changed_branches"] == arcs
    if fault in {"candidate", "strict"}:
        assert "changed_coverage_exceptions" not in result
    elif fault in {"stale", "obsolete"}:
        assert result["changed_coverage_exceptions"] == {
            "applied": [],
            "rejected": [
                {
                    "entry": entry,
                    "reason": "stale source scope"
                    if fault == "stale"
                    else "outcomes are no longer uncovered changed code",
                }
            ],
        }
    else:
        assert result["changed_coverage_exceptions"]["applied"] == [entry]
        if fault == "historical":
            assert result["modified"][0]["lost_equal_lines"]
            assert result["modified"][0]["regressions"]
            assert {
                "metric": "combined",
                "reason": "overall coverage decreased",
            } in result["losses"]
        elif fault == "floor":
            assert {"reason": "combined coverage below 90%"} in result["losses"]
        elif fault == "exclusion":
            assert any(
                loss.get("reason")
                == "Modified source exclusions require explicit reconciliation"
                for loss in result["losses"]
            )
        else:
            assert result["totals"] == strict["totals"]


@PRIMARY
@pytest.mark.parametrize(
    "form", ["unchanged-origin", "overlap", "covered", "covered-mixed"]
)
def test_newly_exposed_coordinates_reject_the_entire_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, form: str
) -> None:
    old = (
        "from typing import TYPE_CHECKING\n"
        "def choose(flag):\n"
        "    if flag:\n"
        "        value = 1  # pragma: no cover\n"
        "    return 0\n"
        "def spare(flag):\n"
        "    if flag:\n"
        "        return 1\n"
        "    return 0\n"
    )
    new = (
        old.replace("  # pragma: no cover", "")
        + "\nif TYPE_CHECKING:\n    from typing import Any\n"
    )
    if form == "overlap":
        new = new.replace("if flag:", "if flag is True:", 1)
    value = case(
        tmp_path,
        monkeypatch,
        old=old,
        new=new,
        old_calls=[("choose", False), ("spare", True)],
        new_calls=[("choose", form != "overlap"), ("spare", True), ("spare", False)],
    )
    detail = comparison(value)["modified"][0]
    lines = [] if form == "unchanged-origin" else [4]
    arc = [3, 4] if form == "overlap" else [3, 5]
    assert arc in detail["missing_exposed_branches"]
    if form == "overlap":
        assert (
            4 in detail["missing_changed_lines"]
            and 4 in detail["missing_exposed_lines"]
        )
        assert arc in detail["missing_changed_branches"]
    else:
        assert 3 not in detail["changed_executable_lines"]
        assert (
            4 in detail["newly_executable_lines"]
            and 4 not in detail["missing_exposed_lines"]
        )
    entry = approval(value, lines, [] if form == "covered" else [arc])
    protect(value, entry)
    result = comparison(
        value, passing_nodes={PROOF}, proof_interpreter=PROOF_INTERPRETER
    )
    assert not result["passed"]
    assert result["modified"][0] == detail
    assert result["changed_coverage_exceptions"] == {
        "applied": [],
        "rejected": [{"entry": entry, "reason": "newly exposed outcome"}],
    }


@PRIMARY
@pytest.mark.parametrize(
    "fault",
    [
        "none",
        "skipped",
        "absent",
        "missing-phase",
        "rehashed",
        "wrong-context",
        "checkout",
    ],
)
def test_verify_local_rederives_proof_from_raw_phases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    from coverage_data import checksum
    from environment import clean_environment
    from preflight_provenance import ATTEMPT_ENV, RUN_ID_ENV
    from risk import required_steps
    from selection import inventory, read

    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    value = changed_case(tmp_path, monkeypatch)
    source = value["source"]
    signal = tmp_path / "skip-proof"
    test = source / TEST_FILE
    test.parent.mkdir()
    test.write_text(
        "from pathlib import Path\nfrom runpy import run_path\nimport pytest\n"
        "def test_ordinary():\n"
        f"    probe = run_path({MODULE!r})\n"
        "    assert probe['choose'](True) == 1 and probe['choose'](False) == 0\n"
        "    assert probe['checked'](False) is True\n"
        "@pytest.mark.serial\n"
        "def test_proof():\n"
        f"    probe = run_path({MODULE!r})\n"
        f"    if Path({str(signal)!r}).exists():\n"
        "        pytest.skip('permitted synthetic skip')\n"
        "    assert probe['checked'](False) is True\n"
    )
    config = source / "pyproject.toml"
    config.write_text(
        config.read_text()
        + '\n[tool.pytest.ini_options]\nmarkers = ["serial: isolated"]\n'
        + 'addopts = "--cov=src/judgmentllm --cov-branch --strict-markers"\n'
    )
    value["head"] = commit(source)
    detail = comparison(value)["modified"][0]
    entry = approval(
        value, detail["missing_changed_lines"], detail["missing_changed_branches"]
    )
    entry["test_nodeids"] = sorted(
        [
            TEST_FILE + "::test_ordinary",
            TEST_FILE + "::test_absent" if fault == "absent" else PROOF,
        ]
    )
    program = source / MODULE
    current = program.read_bytes()
    program.write_bytes(current.replace(NEW.encode(), OLD.encode()))
    protect(value, entry)
    program.write_bytes(current)
    head = commit(source)
    if fault == "skipped":
        signal.touch()

    policy = tmp_path / "protected" / "ci"
    shutil.copytree(
        Path(__file__).resolve().parents[1],
        policy,
        ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"),
    )
    (policy / "local-policy.json").unlink()
    if fault == "wrong-context":
        # Generate consistent raw receipts and dataset contexts for a foreign parser.
        provenance = policy / "local/preflight_provenance.py"
        with provenance.open("a", encoding="utf-8") as stream:
            stream.write(
                "\n_actual_source_record = source_record\n"
                "def source_record(source):\n"
                "    record = _actual_source_record(source)\n"
                "    record['dependencies']['coverage'] = '0.0.0'\n"
                "    return record\n"
            )
    items: list[dict[str, Any]] = [
        {"nodeid": TEST_FILE + "::test_ordinary", "markers": []},
        {"nodeid": PROOF, "markers": ["serial"]},
    ]
    write(policy / "test-inventory.json", inventory(items))
    write(
        policy / "test-profiles.json",
        {
            "schema_version": 1,
            "domains": {"core-contract": "Synthetic coverage proof"},
            "modules": {
                TEST_FILE: {
                    "domain": "core-contract",
                    "cost": "quick",
                    "platforms": [],
                    "reason": "Synthetic",
                }
            },
            "smoke": [TEST_FILE],
            "mandatory_smoke": [TEST_FILE],
            "extended": [],
            "platform_overrides": {},
            "essential": [],
            "providers": {},
            "provider_sources": {},
            "shared_sources": [],
        },
    )
    write(
        policy / "test-contract.json",
        {
            "schema_version": 1,
            "required_nodes": [item["nodeid"] for item in items],
            "replacements": {},
            "platform_skips": {"linux": [PROOF]},
        },
    )

    # Only the synthetic plan omits unrelated named R workloads. Coverage,
    # selected pytest cases, process receipts and both verdict paths are real.
    protected = [
        sys.executable,
        "-I",
        "-B",
        "-c",
        "import sys; sys.path.insert(0, sys.argv.pop(1)); "
        "import risk; risk.R_GATES = []; "
        "import acceptance; raise SystemExit(acceptance.main())",
        str(policy),
    ]
    identity = ["--source", str(source), "--base", value["base"], "--head", head]
    plan_path = tmp_path / "plan.json"
    lane, run_id, attempt = "linux-3.11", "synthetic-proof", "one-attempt"
    env = {
        **clean_environment(),
        RUN_ID_ENV: f"{run_id}:{lane}",
        ATTEMPT_ENV: attempt,
    }
    planned = subprocess.run(
        [*protected, "plan", *identity, "--output", str(plan_path)],
        cwd=source,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert planned.returncode == 0, planned.stdout + planned.stderr
    plan = read(plan_path)
    assert plan["coverage_mode"] == "historical" and plan["r_gates"] == []
    output = tmp_path / "run"
    root = output / "suite"
    suite_command = [
        sys.executable,
        "-B",
        str(policy / "local/suite.py"),
        "--source",
        str(source),
        "--output",
        str(root),
        "--workers",
        "0",
        "--baseline",
        str(source / FLOOR),
        "--coverage-ref",
        head,
        "--plan",
        str(plan_path),
        "--contract",
        str(policy / "test-contract.json"),
        "--local-schedule",
        "conservative",
        "tests",
    ]
    executed = subprocess.run(
        suite_command,
        cwd=source,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    lacks_proof = fault in {"skipped", "absent"}
    failed_execution = lacks_proof or fault == "wrong-context"
    assert (executed.returncode == 0) is not failed_execution, (
        executed.stdout + executed.stderr
    )
    summary = read(root / "summary.json")
    declared = read(root / "coverage-comparison.json")
    assert summary["functional_coverage"]["passed"] is False
    assert summary["resource_coverage_path"] == "B"
    origin_error = "Changed coverage proof origin differs from the locked Linux interpreter/Coverage context"
    if fault == "wrong-context":
        for group in ("ordinary", "serial"):
            raw = read(root / (group + ".json"))
            assert (
                raw["provenance"]["interpreter"]["dependencies"]["coverage"] == "0.0.0"
            )
        assert declared["fatal_error"] == "invalid-evidence"
        assert {"reason": f"Invalid coverage evidence: {origin_error}"} in declared[
            "losses"
        ]
    elif lacks_proof:
        assert (
            declared["changed_coverage_exceptions"]["rejected"][0]["reason"]
            == "missing passing proof tests"
        )
    else:
        assert declared["changed_coverage_exceptions"]["applied"] == [entry]
        if fault == "rehashed":
            declared["changed_coverage_exceptions"]["applied"][0]["justification"] = (
                "Forged derived approval."
            )
    if fault == "missing-phase":
        raw = read(root / "serial.json")
        raw["reports"] = [
            report for report in raw["reports"] if report["phase"] != "call"
        ]
        write(root / "serial.json", raw)
    if failed_execution or fault == "rehashed":
        declared.update(passed=True, losses=[])
        write(root / "coverage-comparison.json", declared)
        manifest = read(root / "coverage-manifest.json")
        manifest["reports"]["coverage-comparison.json"] = checksum(
            root / "coverage-comparison.json"
        )
        write(root / "coverage-manifest.json", manifest)
        summary.update(status="passed", coverage=declared)
        write(root / "summary.json", summary)
    write(
        output / "receipt.json",
        {
            "source_verified": True,
            "attempt_id": attempt,
            "source_sha": head,
            "plan_sha256": plan["plan_sha256"],
            "run_id": run_id,
            "lane": lane,
            "execution_scope": "local",
            "partition": "all",
            "phase": "complete",
            "status": "passed",
            "selection_sha256": summary["selection_sha256"],
            "steps": [
                {"name": name, "exit_code": 0}
                for name in sorted(required_steps(plan, lane, execution_scope="local"))
            ],
        },
    )
    checkout_error = f"Reviewed checkout differs from committed source: {TEST_FILE!r}"
    if fault == "checkout":
        subprocess.run(
            ["git", "update-index", "--assume-unchanged", "--", TEST_FILE],
            cwd=source,
            check=True,
        )
        original = test.read_bytes()
        altered = original.replace(
            b"permitted synthetic skip", b"permitted synthetic edit"
        )
        assert len(altered) == len(original) and altered != original
        test.write_bytes(altered)
        assert (
            subprocess.check_output(["git", "status", "--porcelain"], cwd=source) == b""
        )
        blocked = tmp_path / "blocked"
        suite_command[suite_command.index("--output") + 1] = str(blocked)
        rerun = subprocess.run(
            suite_command,
            cwd=source,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert rerun.returncode == 1, rerun.stdout + rerun.stderr
        assert read(blocked / "summary.json")["error"]["message"] == checkout_error
        assert not (blocked / "collection.json").exists()
    verdict_path = tmp_path / "verdict.json"
    verified = subprocess.run(
        [
            *protected,
            "verify-local",
            *identity,
            "--plan",
            str(plan_path),
            "--receipts",
            str(output),
            "--output",
            str(verdict_path),
            "--run-id",
            run_id,
        ],
        cwd=source,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert verified.returncode == (0 if fault == "none" else 1), (
        verified.stdout + verified.stderr
    )
    verdict = read(verdict_path)
    assert verdict["passed"] is (fault == "none")
    if lacks_proof:
        assert "missing passing proof tests" in verdict["errors"][0]
    elif fault == "wrong-context":
        assert origin_error in verdict["errors"][0]
    elif fault == "checkout":
        assert checkout_error in verdict["errors"][0]
    elif fault == "missing-phase":
        assert "Case has no terminal outcome" in verdict["errors"][0]
    elif fault == "rehashed":
        assert "Stored coverage comparison differs" in verdict["errors"][0]
