"""Synthetic exact-source authority, forgery and independent reconstruction tests."""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from coverage_authority import (
    FLOOR,
    REGISTRY,
    AuthorityError,
    admission_identity,
    commit_tree,
    describe,
    disk_bytes,
    fingerprint,
    git_bytes,
    load,
    registry,
    sha256,
)
from coverage_equivalence import compare, validate_summary
from test_coverage_policy import commit

MODULE = "src/judgmentllm/probe.py"
OLD = "def choose(flag):\n    if flag:\n        return 1\n    return 0\n"
NEW = (
    "from typing import TYPE_CHECKING\n"
    "if TYPE_CHECKING:\n    from typing import Any\n\n" + OLD
)
SETTINGS = (
    "[tool.coverage.run]\nbranch = true\nrelative_files = true\n"
    'source = ["src/judgmentllm"]\n[tool.coverage.report]\nfail_under = 90\n'
)
PARSER = {"implementation": "cpython", "python": "3.11", "coverage": "7.17.0"}
PRIMARY = pytest.mark.skipif(
    sys.implementation.name != "cpython" or sys.version_info[:2] != (3, 11),
    reason="Active reconciliations require the primary CPython 3.11 parser context",
)


@pytest.mark.parametrize(
    "module", ["coverage_equivalence", "ci.local.coverage_equivalence"]
)
def test_coverage_helpers_support_script_and_package_imports_without_parser(
    tmp_path: Path,
    module: str,
) -> None:
    root = Path(__file__).resolve().parents[2]
    program = (
        "import builtins,importlib,sys; from pathlib import Path; "
        "root=Path(sys.argv[1]); sys.path[:0]=[str(root),str(root/'ci/local')]; "
        "original=builtins.__import__; "
        "exec(\"def guarded(name,*args,**kwargs):\\n if name.split('.')[0]=='coverage': raise AssertionError('metadata imported Coverage')\\n return original(name,*args,**kwargs)\"); "
        "builtins.__import__=guarded; module=importlib.import_module(sys.argv[2]); "
        "assert Path(module.__file__).resolve()==root/'ci/local/coverage_equivalence.py'; "
        "assert 'coverage' not in sys.modules"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", program, str(root), module],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def measure(
    source: Path,
    output: Path,
    calls: list[tuple[str, bool]],
    filename: str = MODULE,
) -> dict[str, Any]:
    from coverage import Coverage

    coverage = Coverage(
        config_file=str(source / "pyproject.toml"),
        data_file=str(output.with_suffix(".coverage")),
    )
    namespace: dict[str, Any] = {"__name__": "synthetic_coverage"}
    coverage.start()
    try:
        path = source / filename
        exec(compile(path.read_bytes(), str(path), "exec"), namespace)
        for name, flag in calls:
            namespace[name](flag)
    finally:
        coverage.stop()
    coverage.json_report(morfs=[str(source / filename)], outfile=str(output))
    return json.loads(output.read_text())


def case(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    old: str = OLD,
    new: str = NEW,
    approved: bool = True,
    removed: list[int] | None = None,
    old_calls: list[tuple[str, bool]] | None = None,
    new_calls: list[tuple[str, bool]] | None = None,
) -> dict[str, Any]:
    source = root / "source"
    (source / MODULE).parent.mkdir(parents=True)
    (source / MODULE).write_text(old)
    (source / "pyproject.toml").write_text(SETTINGS)
    (source / "uv.lock").write_text(
        '[[package]]\nname = "coverage"\nversion = "7.17.0"\n'
    )
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    previous = commit(source)
    monkeypatch.chdir(source)
    calls = [("choose", True), ("choose", False)]
    before = measure(source, root / "before.json", old_calls or calls)
    measured = before["files"][MODULE]
    floor = {
        "source_commit": previous,
        "measurement_settings": {
            "run": {
                "branch": True,
                "relative_files": True,
                "source": ["src/judgmentllm"],
            },
            "report": {"fail_under": 90},
        },
        "totals": before["totals"],
        "files": {
            MODULE: {
                "source_sha256": sha256(old.encode()),
                "executed_lines": measured["executed_lines"],
                "executed_branches": measured["executed_branches"],
                "excluded_lines": measured["excluded_lines"],
                "denominators": {
                    k: measured["summary"][k]
                    for k in ("num_statements", "num_branches")
                },
            }
        },
    }
    write(source / FLOOR, floor)
    (source / MODULE).write_text(new)
    current = root / "current.json"
    after = measure(source, current, new_calls or calls)
    entry: dict[str, Any] = {
        "path": MODULE,
        "candidate_source_sha256": sha256(new.encode()),
        "historical_source_sha256": sha256(old.encode()),
        "added_excluded_lines": after["files"][MODULE]["excluded_lines"],
        "removed_excluded_lines": removed or [],
        "reason": "Synthetic reviewed type-only/structural exclusion delta",
    }
    registry = {
        "schema_version": 1,
        "coverage_floor_sha256": sha256((source / FLOOR).read_bytes()),
        "parser": PARSER,
        "entries": [entry] if approved else [],
    }
    (source / MODULE).write_text(old)
    write(source / REGISTRY, registry)
    base = commit(source)
    (source / MODULE).write_text(new)
    # A candidate can publish its own matching metadata, but it is not authority.
    write(source / REGISTRY, {**registry, "entries": [entry]})
    head = commit(source)
    return {
        "source": source,
        "base": base,
        "head": head,
        "current": current,
        "policy": describe(source, base),
        "entry": entry,
        "floor": floor,
        "after": after,
    }


def comparison(value: dict[str, Any], **changes: Any) -> dict[str, Any]:
    arguments = {
        "reviewed_ref": value["head"],
        "coverage_policy": value["policy"],
        "protected_base": value["base"],
        **changes,
    }
    return compare(
        value["current"], value["source"] / FLOOR, value["source"], **arguments
    )


@pytest.mark.parametrize(
    "fault", ["none", "duplicate-reason", "duplicate-delta", "alias", "digest"]
)
def test_legacy_registry_and_exact_pair_validation(fault: str) -> None:
    root = Path(__file__).resolve().parents[2]
    floor, lock = (root / FLOOR).read_bytes(), (root / "uv.lock").read_bytes()
    value = registry((root / REGISTRY).read_bytes(), floor, lock)
    assert (
        fingerprint(value["entries"][:5])
        == "94a5b568f86afd6c1bbd268d807b1dfe22201d986b9438c00054f89cd70ecb98"
    )
    if fault == "none":
        return
    if fault.startswith("duplicate-"):
        duplicate = copy.deepcopy(value["entries"][0])
        if fault == "duplicate-reason":
            duplicate["reason"] = "A different reason cannot authorize a duplicate"
        else:
            duplicate["added_excluded_lines"] = [106]
        value["entries"].append(duplicate)
        message = "Duplicate reconciliation path/source digest"
    elif fault == "alias":
        value["entries"][0]["path"] = "src/judgmentllm/./lifecycle.py"
        message = "Noncanonical evidence path"
    else:
        value["entries"][0]["candidate_source_sha256"] = []
        message = "Invalid reconciliation source digest"
    with pytest.raises(ValueError, match=message):
        registry(json.dumps(value).encode(), floor, lock)


@PRIMARY
@pytest.mark.parametrize("reverse", [False, True])
def test_exact_variants_keep_independent_pins_and_coverage_losses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reverse: bool
) -> None:
    value = case(tmp_path, monkeypatch)
    source = value["source"]
    calls = [("choose", True), ("choose", False)]
    second = "# Second reviewed variant\n" + NEW.replace("if flag:", "if not flag:")
    (source / MODULE).write_text(second)
    measured = measure(source, value["current"], calls)
    entries = [
        value["entry"],
        {
            **value["entry"],
            "candidate_source_sha256": sha256(second.encode()),
            "added_excluded_lines": measured["files"][MODULE]["excluded_lines"],
            "reason": "Independently reviewed second synthetic variant",
        },
    ]
    assert entries[0]["added_excluded_lines"] != entries[1]["added_excluded_lines"]
    raw = json.loads((source / REGISTRY).read_text())
    raw["entries"] = list(reversed(entries)) if reverse else entries
    write(source / REGISTRY, raw)
    (source / MODULE).write_text(OLD)
    value["base"] = commit(source)
    value["policy"] = describe(source, value["base"])
    for program, entry in zip((NEW, second), entries, strict=True):
        (source / MODULE).write_text(program)
        value["head"] = commit(source)
        measure(source, value["current"], calls)
        result = comparison(value)
        assert result["passed"], result
        assert result["applied_reconciliations"] == [entry]
        measure(source, value["current"], [("choose", True)])
        denied = comparison(value)
        assert not denied["passed"] and "fatal_error" not in denied, denied
        assert denied["applied_reconciliations"] == [entry]
        detail = denied["modified"][0]
        assert detail["lost_equal_lines"] and detail["regressions"]["branch"]
        if program == second:
            assert detail["missing_changed_branches"]
    for program in (second.replace("not flag", "flag"), NEW + "# altered bytes\n"):
        (source / MODULE).write_text(program)
        measured = measure(source, value["current"], calls)
        # A matching candidate-owned pin cannot extend the authenticated base.
        candidate_entry = {
            **value["entry"],
            "candidate_source_sha256": sha256(program.encode()),
            "added_excluded_lines": measured["files"][MODULE]["excluded_lines"],
        }
        write(
            source / REGISTRY,
            {**raw, "entries": [*raw["entries"], candidate_entry]},
        )
        value["head"] = commit(source)
        denied = comparison(value)
        assert not denied["passed"] and "fatal_error" not in denied, denied
        assert denied["applied_reconciliations"] == []
        assert any("exclusions" in loss.get("reason", "") for loss in denied["losses"])
        forged = comparison(value, coverage_policy=describe(source, value["head"]))
        assert forged["fatal_error"] == "invalid-authority"


@PRIMARY
def test_exact_approval_and_candidate_self_exemption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = case(tmp_path, monkeypatch, approved=False)
    denied = comparison(value)
    assert not denied["passed"] and "fatal_error" not in denied
    assert denied["applied_reconciliations"] == []
    # A standalone candidate claim can be internally consistent. Independent base
    # reconstruction, rather than a self-reported digest, must reject that claim.
    forged = describe(value["source"], value["head"])
    assert comparison(value, coverage_policy=forged, protected_base=value["head"])[
        "passed"
    ]
    assert not comparison(value, coverage_policy=forged)["passed"]
    assert not comparison(value, protected_base=None)["passed"]
    assert not comparison(value, coverage_policy=None)["passed"]


@PRIMARY
@pytest.mark.parametrize(
    "fault",
    [
        "none",
        "partial",
        "extra",
        "fake-runtime",
        "missing-runtime",
        "arc",
        "stale-byte",
        "candidate-lock",
        "candidate-config",
        "unrelated-loss",
    ],
)
def test_exact_entry_cannot_excuse_bad_or_uncovered_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    value = case(tmp_path, monkeypatch)
    measured = copy.deepcopy(value["after"])
    item = measured["files"][MODULE]
    if fault in {"partial", "extra", "fake-runtime"}:
        raw = json.loads((value["source"] / REGISTRY).read_text())
        if fault == "partial":
            raw["entries"][0]["added_excluded_lines"].pop()
        elif fault == "extra":
            raw["entries"][0]["removed_excluded_lines"] = [1]
        else:
            line = item["executed_lines"].pop()
            item["excluded_lines"] = sorted([*item["excluded_lines"], line])
            raw["entries"][0]["added_excluded_lines"] = item["excluded_lines"]
            item["summary"]["covered_lines"] -= 1
            item["summary"]["num_statements"] -= 1
            measured["totals"] = item["summary"].copy()
        # An independently reviewed but incorrect payload must also fail, not just
        # a candidate-owned file. Build a new synthetic authority commit.
        write(value["source"] / REGISTRY, raw)
        value["base"] = commit(value["source"])
        value["head"] = value["base"]
        value["policy"] = describe(value["source"], value["base"])
    elif fault in {"missing-runtime", "unrelated-loss"}:
        line = item["executed_lines"].pop()
        item["summary"]["covered_lines"] -= 1
        if fault == "missing-runtime":
            item["summary"]["num_statements"] -= 1
        else:
            item["missing_lines"] = [line]
        measured["totals"] = item["summary"].copy()
    elif fault == "arc":
        item["executed_branches"].pop()
        item["summary"]["covered_branches"] -= 1
        item["summary"]["num_branches"] -= 1
        measured["totals"] = item["summary"].copy()
    elif fault == "stale-byte":
        path = value["source"] / MODULE
        path.write_text(path.read_text() + "# different exact bytes\n")
        value["head"] = commit(value["source"])
    elif fault == "candidate-lock":
        (value["source"] / "uv.lock").write_text(
            '[[package]]\nname = "coverage"\nversion = "0.0"\n'
        )
        value["head"] = commit(value["source"])
    elif fault == "candidate-config":
        (value["source"] / "pyproject.toml").write_text(
            SETTINGS.replace("fail_under = 90", "fail_under = 1")
        )
        value["head"] = commit(value["source"])
    if fault in {"fake-runtime", "missing-runtime", "arc", "unrelated-loss"}:
        # These forged minimal summaries stay internally consistent so stale
        # optional presentation fields cannot mask the parser/coverage boundary.
        item["summary"] = {
            key: item["summary"][key]
            for key in (
                "covered_lines",
                "num_statements",
                "covered_branches",
                "num_branches",
            )
        }
        measured["totals"] = item["summary"].copy()
    write(value["current"], measured)
    result = comparison(value)
    assert result["passed"] is (fault == "none"), result
    if fault in {"partial", "extra", "fake-runtime", "missing-runtime", "arc"}:
        assert "fatal_error" in result
    if fault == "unrelated-loss":
        assert result["applied_reconciliations"] and result["losses"]
    if fault == "candidate-config":
        assert result["fatal_error"] == "invalid-evidence"


@PRIMARY
def test_removed_exclusions_and_unchanged_origin_outcomes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = "from typing import TYPE_CHECKING\ndef choose(flag):\n    if TYPE_CHECKING:\n        value = 1\n    return 0\n"
    value = case(
        tmp_path / "removed",
        monkeypatch,
        old=old,
        new=old.replace("if TYPE_CHECKING:", "if flag:"),
        removed=[4],
    )
    assert comparison(value)["passed"]
    # All statements execute and the old percentage gates improve. Removing the
    # pragma exposes BOTH outcomes at an unchanged origin, not only its body.
    program = (
        "from typing import TYPE_CHECKING\n"
        "def choose(flag):\n    if flag:\n        value = 1  # pragma: no cover\n    return 0\n"
        "def spare(flag):\n    if flag:\n        return 1\n    return 0\n"
    )
    new = (
        program.replace("  # pragma: no cover", "")
        + "\nif TYPE_CHECKING:\n    from typing import Any\n"
    )
    value = case(
        tmp_path / "branch",
        monkeypatch,
        old=program,
        new=new,
        old_calls=[("choose", False), ("spare", True)],
        new_calls=[("choose", True), ("spare", True), ("spare", False)],
    )
    result = comparison(value)
    assert not result["passed"] and "fatal_error" not in result, result
    assert result["combined_percent"] >= 90
    assert not any("metric" in loss for loss in result["losses"])
    detail = result["modified"][0]
    assert not detail["regressions"] and not detail["missing_changed_lines"]
    assert not detail["missing_changed_branches"] and not detail["lost_equal_lines"]
    assert detail["missing_exposed_branches"] == [[3, 5]]
    assert 3 not in detail["changed_executable_lines"]
    measure(
        value["source"],
        value["current"],
        [
            ("choose", True),
            ("choose", False),
            ("spare", True),
            ("spare", False),
        ],
    )
    positive = comparison(value)
    assert positive["passed"], positive
    detail = positive["modified"][0]
    assert [3, 5] in detail["newly_exposed_branches"]
    assert not detail["missing_exposed_branches"]
    assert 3 not in detail["changed_executable_lines"]


@PRIMARY
def test_real_parser_signed_exit_correspondence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = "def choose(flag):\n    if flag:\n        value = 1\n"
    new = (
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from typing import Any\n\n"
        + old
    )
    value = case(tmp_path, monkeypatch, old=old, new=new)
    assert [2, -1] in value["floor"]["files"][MODULE]["executed_branches"]
    assert [6, -5] in value["after"]["files"][MODULE]["executed_branches"]
    result = comparison(value)
    assert result["passed"], result
    assert result["modified"][0]["newly_exposed_branches"] == []


@PRIMARY
def test_declaration_approval_cannot_hide_previously_uncovered_runtime_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = (
        "from typing import TYPE_CHECKING\n"
        "def choose(flag):\n    if flag:\n        return 1\n    return 0\n"
    )
    value = case(
        tmp_path,
        monkeypatch,
        old=old,
        new=old.replace("if flag:", "if TYPE_CHECKING:"),
        old_calls=[("choose", False)],
        new_calls=[("choose", False)],
    )
    assert 4 not in value["floor"]["files"][MODULE]["executed_lines"]
    result = comparison(value)
    assert not result["passed"]
    assert result["fatal_error"] == "invalid-authority"
    assert "cannot hide historical executable source" in result["losses"][-1]["reason"]


def test_retained_schema_one_has_distinct_aggregate_provenance() -> None:
    root = Path(__file__).resolve().parents[2]
    raw = (root / FLOOR).read_bytes()
    assert (
        sha256(raw)
        == "2af90319c2e0c274f054153012dd3e1583f3215302f47bf2ad5bf57f3be2e95d"
    )
    floor = json.loads(raw)
    assert floor["source_commit"] == "1298bedf5e408d48ef1fd6e113677a0ece27aa24"
    assert floor["source_snapshot_commit"] == "a109deb5d3ef0aaa3d05e56b794aa3671984119b"
    assert len(floor["files"]) == 96
    totals = floor["totals"]
    assert (
        totals["covered_lines"],
        totals["num_statements"],
        totals["covered_branches"],
        totals["num_branches"],
    ) == (12821, 13533, 4088, 4656)
    validate_summary(totals)
    entries = list(floor["files"].values())
    assert (
        sum(len(item["executed_lines"]) for item in entries),
        sum(item["denominators"]["num_statements"] for item in entries),
        sum(len(item["executed_branches"]) for item in entries),
        sum(item["denominators"]["num_branches"] for item in entries),
    ) == (14608, 15409, 4639, 5278)


@PRIMARY
def test_ordinary_mismatches_accumulate_but_bad_evidence_aborts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = case(tmp_path, monkeypatch, approved=False)
    source = value["source"]
    # Five independent files, one of which is subsequently approved.
    for number in range(4):
        (source / MODULE).with_name(f"other{number}.py").write_bytes(
            (source / MODULE).read_bytes()
        )
    value["head"] = commit(source)
    report = copy.deepcopy(value["after"])
    for number in range(4):
        report["files"][MODULE.replace("probe.py", f"other{number}.py")] = (
            copy.deepcopy(report["files"][MODULE])
        )
    for key in ("covered_lines", "num_statements", "covered_branches", "num_branches"):
        report["totals"][key] *= 5
    write(value["current"], report)
    result = comparison(value)
    assert not result["passed"] and "fatal_error" not in result
    assert (
        len(
            [
                loss
                for loss in result["losses"]
                if "exclusions" in loss.get("reason", "")
            ]
        )
        == 5
    )
    # Publish the one synthetic approval in a separate authority commit.
    value["base"] = value["head"]
    value["policy"] = describe(source, value["base"])
    result = comparison(value)
    assert not result["passed"] and len(result["applied_reconciliations"]) == 1
    assert (
        len(
            [
                loss
                for loss in result["losses"]
                if "exclusions" in loss.get("reason", "")
            ]
        )
        == 4
    )
    item = report["files"][MODULE]
    for bad in ([True], [2, 2], [3, 2], [2.0]):
        item["excluded_lines"] = bad
        write(value["current"], report)
        result = comparison(value)
        assert result["fatal_error"] == "invalid-evidence"
        assert not any("file" in loss for loss in result["losses"])


def test_object_types_replacements_absence_and_canonical_paths(tmp_path: Path) -> None:
    source = tmp_path / "repo"
    source.mkdir()
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    (source / "file.py").write_text("original\n")
    first = commit(source)
    (source / "file.py").write_text("forged\n")
    second = commit(source)
    subprocess.run(["git", "replace", first, second], cwd=source, check=True)
    assert git_bytes(source, first, "file.py") == b"original\n"
    assert git_bytes(source, first, "absent.py") is None
    with pytest.raises((ValueError, subprocess.SubprocessError)):
        git_bytes(source, "0" * 40, "absent.py")
    tree = commit_tree(source, first)
    with pytest.raises(ValueError, match="not a commit"):
        git_bytes(source, tree, "file.py")
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=CI",
            "-c",
            "user.email=ci@example.invalid",
            "tag",
            "-am",
            "tag",
            "tagged",
            first,
        ],
        cwd=source,
        check=True,
    )
    tag = subprocess.check_output(
        ["git", "rev-parse", "tagged"], cwd=source, text=True
    ).strip()
    with pytest.raises(ValueError, match="not a commit"):
        git_bytes(source, tag, "file.py")
    for name in ("./file.py", "../file.py", "/file.py", "a//file.py", "a\\file.py"):
        with pytest.raises(ValueError):
            disk_bytes(source, name)
    assert git_bytes(source, first, "file*") is None


@pytest.mark.skipif(
    sys.platform == "win32", reason="Real symlink creation requires Windows privileges"
)
def test_disk_and_git_symlink_components_are_not_absence(tmp_path: Path) -> None:
    source = tmp_path / "repo"
    source.mkdir()
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    (source / "real").mkdir()
    (source / "real/file.py").write_text("value = 1\n")
    (source / "alias").symlink_to("real", target_is_directory=True)
    head = commit(source)
    with pytest.raises(ValueError, match="nonregular"):
        git_bytes(source, head, "alias/file.py")
    with pytest.raises(ValueError, match="Symlink"):
        disk_bytes(source, "alias/file.py")


def test_literal_event_identity_is_independent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    event = tmp_path / "event.json"
    write(
        event, {"pull_request": {"base": {"sha": "a" * 40}, "head": {"sha": "b" * 40}}}
    )
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request_target")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    assert admission_identity(None, None) == ("a" * 40, "b" * 40)
    with pytest.raises(AuthorityError):
        admission_identity("b" * 40, "b" * 40)


@PRIMARY
@pytest.mark.parametrize(
    "fault",
    [
        "duplicate-key",
        "duplicate-path",
        "bool-version",
        "history",
        "floor",
        "parser",
        "unknown",
        "non-int",
        "no-delta",
        "cross-file",
        "missing-object",
    ],
)
def test_corrupt_authority_aborts_before_measuring_any_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    value = case(tmp_path, monkeypatch)
    source = value["source"]
    if fault == "missing-object":
        with pytest.raises(
            AuthorityError, match="Invalid protected coverage authority:"
        ) as refused:
            load(source, "0" * 40, value["policy"])
        cause = refused.value.__cause__
        assert isinstance(cause, subprocess.CalledProcessError)
        assert cause.cmd[-3:] == ["cat-file", "-t", "0" * 40]
        return
    raw = json.loads((source / REGISTRY).read_text())
    if fault == "duplicate-key":
        (source / REGISTRY).write_text(
            json.dumps(raw).replace(
                '"schema_version": 1', '"schema_version": 1, "schema_version": 1'
            )
        )
    else:
        if fault == "duplicate-path":
            raw["entries"].append(raw["entries"][0].copy())
        elif fault == "bool-version":
            raw["schema_version"] = True
        elif fault == "history":
            raw["entries"][0]["historical_source_sha256"] = "0" * 64
        elif fault == "floor":
            raw["coverage_floor_sha256"] = "0" * 64
        elif fault == "parser":
            raw["parser"]["coverage"] = "0.0"
        elif fault == "unknown":
            raw["entries"][0]["approved"] = True
        elif fault == "non-int":
            raw["entries"][0]["added_excluded_lines"] = [True]
        elif fault == "no-delta":
            raw["entries"][0]["added_excluded_lines"] = []
        elif fault == "cross-file":
            raw["entries"][0]["path"] = "src/judgmentllm/another.py"
        write(source / REGISTRY, raw)
    base = commit(source)
    # Recomputed descriptor digests do not turn invalid authority into authority.
    with pytest.raises(AuthorityError):
        describe(source, base)


@PRIMARY
def test_active_entry_is_validated_even_for_unchanged_source_without_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = case(tmp_path, monkeypatch)
    source = value["source"]
    (source / MODULE).write_text(OLD)
    raw = json.loads((source / REGISTRY).read_text())
    raw["entries"][0]["candidate_source_sha256"] = sha256(OLD.encode())
    raw["entries"][0]["added_excluded_lines"] = [2]
    write(source / REGISTRY, raw)
    value["base"] = value["head"] = commit(source)
    value["policy"] = describe(source, value["base"])
    measure(source, value["current"], [("choose", True), ("choose", False)])
    result = comparison(value)
    assert not result["passed"] and result["fatal_error"] == "invalid-authority"


@PRIMARY
def test_new_protocol_module_requires_exact_path_and_null_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = case(tmp_path, monkeypatch, approved=False)
    source = value["source"]
    (source / MODULE).write_text(OLD)
    calls = [("choose", True), ("choose", False)]
    report = measure(source, value["current"], calls)
    name = "src/judgmentllm/adapter.py"
    program = (
        "from typing import Protocol\n"
        + OLD
        + "\nclass Adapter(Protocol):\n    def submit(self) -> None: ...\n"
    )
    (source / name).write_text(program)
    addition = measure(source, tmp_path / "adapter.json", calls, filename=name)
    item = addition["files"][name]
    assert item["excluded_lines"]
    entry: dict[str, Any] = {
        "path": name,
        "candidate_source_sha256": sha256(program.encode()),
        "historical_source_sha256": None,
        "added_excluded_lines": item["excluded_lines"],
        "removed_excluded_lines": [],
        "reason": "Synthetic structural Protocol",
    }
    registry = json.loads((source / REGISTRY).read_text())
    registry["entries"] = [entry]
    write(source / REGISTRY, registry)
    value["base"] = value["head"] = commit(source)
    value["policy"] = describe(source, value["base"])
    report["files"][name] = item
    for key in ("covered_lines", "num_statements", "covered_branches", "num_branches"):
        report["totals"][key] += addition["totals"][key]
    write(value["current"], report)
    assert comparison(value)["passed"]
    # Identical source bytes at another path do not inherit the approval.
    other = "src/judgmentllm/other.py"
    (source / other).write_text(program)
    value["head"] = commit(source)
    report["files"][other] = copy.deepcopy(item)
    for key in ("covered_lines", "num_statements", "covered_branches", "num_branches"):
        report["totals"][key] += addition["totals"][key]
    write(value["current"], report)
    result = comparison(value)
    assert not result["passed"] and "fatal_error" not in result
    assert len(result["applied_reconciliations"]) == 1


@PRIMARY
@pytest.mark.parametrize(
    "fault", ["none", "comparison", "dataset", "candidate-authority"]
)
def test_real_independent_reconstruction_rejects_rehashed_forgery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    from coverage import CoverageData
    from coverage_data import (
        checksum,
        combine,
        context_name,
        prepare_config,
        report,
        seal,
    )
    from preflight_provenance import receipt_provenance, source_record

    value = case(tmp_path, monkeypatch, approved=fault != "candidate-authority")
    source = value["source"]
    (source / "test_probe.py").write_text(
        "import runpy\ndef test_probe():\n    choose = runpy.run_path('src/judgmentllm/probe.py')['choose']\n    assert choose(True) == 1\n    assert choose(False) == 0\n"
    )
    value["head"] = commit(source)
    identity = receipt_provenance(
        source_record(source), "reconciliation", "one-attempt"
    )
    root = tmp_path / "suite"
    group_root = root / "coverage-groups/ordinary"
    group_root.mkdir(parents=True)
    config = prepare_config(source, group_root, identity, "ordinary")
    data = group_root / ".coverage"
    receipt_path = root / "ordinary.json"
    environment = {
        **os.environ,
        "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
        "PYTHONDONTWRITEBYTECODE": "1",
        "COVERAGE_FILE": str(data),
        "JUDGMENTLLM_CI_RUN_ID": "reconciliation",
        "JUDGMENTLLM_CI_GROUP": "ordinary",
        "JUDGMENTLLM_CI_ATTEMPT_ID": "one-attempt",
        "COVERAGE_RCFILE": str(config),
        "JUDGMENTLLM_COVERAGE_CONTEXT": context_name(identity, "ordinary"),
    }
    completed = subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "pytest",
            "-c",
            str(source / "pyproject.toml"),
            "-p",
            "ci.local.pytest_report",
            "--ci-coverage",
            "--cov=src/judgmentllm",
            "--cov-branch",
            "--cov-append",
            f"--cov-config={config}",
            "--cov-report=",
            f"--ci-report={receipt_path}",
            "-q",
            "-o",
            f"cache_dir={tmp_path / 'cache'}",
            "test_probe.py",
        ],
        cwd=source,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    receipt = json.loads(receipt_path.read_text())
    group = seal(data, receipt, source, identity, "ordinary")
    manifest = combine(
        [group], {"ordinary": receipt}, source, identity, root / ".coverage"
    )
    report(root / ".coverage", source, root / "coverage.json")
    policy = (
        describe(source, value["head"])
        if fault == "candidate-authority"
        else value["policy"]
    )
    declared = compare(
        root / "coverage.json",
        source / FLOOR,
        source,
        reviewed_ref=value["head"],
        coverage_policy=policy,
        protected_base=value["head"]
        if fault == "candidate-authority"
        else value["base"],
    )
    assert declared["passed"], declared
    if fault == "comparison":
        declared["applied_reconciliations"][0]["reason"] = "forged but rehashed"
    if fault == "dataset":
        altered = CoverageData(basename=str(root / ".coverage"))
        altered.read()
        altered.erase()
        altered.add_arcs({MODULE: [(1, 2)]})
        altered.write()
        manifest["combined_sha256"] = checksum(root / ".coverage")
    write(root / "coverage-comparison.json", declared)
    manifest["reports"] = {
        name: checksum(root / name)
        for name in ("coverage.json", "coverage.xml", "coverage-comparison.json")
    }
    write(root / "coverage-manifest.json", manifest)
    plan = {
        "coverage_mode": "historical",
        "source_sha": value["head"],
        "source_tree_sha": commit_tree(source, value["head"]),
        "base_sha": value["base"],
        "coverage_policy": value["policy"],
        "coverage_policy_sha256": fingerprint(value["policy"]),
    }
    # Call the actual verifier from a fresh copied policy, without importing the
    # candidate tree as policy and without stubbing compare/Coverage/receipts.
    policy_root = tmp_path / "trusted-policy"
    policy_root.mkdir()
    original = Path(__file__).parent
    for name in (
        "coverage_data.py",
        "coverage_equivalence.py",
        "coverage_authority.py",
        "suite.py",
        "preflight_provenance.py",
        "resource_budget.py",
        "resource_specs.py",
        "telemetry.py",
    ):
        shutil.copyfile(original / name, policy_root / name)
    for name in ("environment.py", "selection.py", "additions.py"):
        shutil.copyfile(original.parent / name, policy_root / name)
    write(tmp_path / "plan.json", plan)
    write(tmp_path / "identity.json", identity)
    verified = subprocess.run(
        [
            sys.executable,
            "-E",
            "-s",
            "-B",
            "-c",
            "import sys,json; from pathlib import Path; sys.path.insert(0,sys.argv[1]); "
            "from coverage_data import validate_suite_coverage; "
            "validate_suite_coverage(Path(sys.argv[2]),Path(sys.argv[3]),json.loads(Path(sys.argv[4]).read_text()),json.loads(Path(sys.argv[5]).read_text()),{'ordinary'})",
            str(policy_root),
            str(root),
            str(source),
            str(tmp_path / "plan.json"),
            str(tmp_path / "identity.json"),
        ],
        cwd=source,
        capture_output=True,
        text=True,
        check=False,
    )
    assert (verified.returncode == 0) is (fault == "none"), (
        verified.stdout + verified.stderr
    )
