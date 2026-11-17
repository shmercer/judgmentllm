"""Unsupported import formats cannot inherit source-only overlap evidence."""

from __future__ import annotations

import hashlib
import json
import os
import py_compile
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import risk
from environment import clean_environment
from local import policy as local_policy
from local import scheduling_contract as contract
from local.serial_dependencies import closure, snapshot
from test_risk import freeze
from test_scheduling_contract import SAFE, checksum, review_execution
from test_scheduling_contract import project as project


def execute(source: Path, output: Path) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "pytest",
            "--no-cov",
            "-q",
            SAFE,
            f"--confcutdir={source}",
            f"--junitxml={output}.xml",
            "-o",
            f"cache_dir={output}-cache",
        ],
        cwd=source,
        env={
            **clean_environment(),
            "PYTHONPATH": os.pathsep.join((str(source), str(source / "tests"))),
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    output.with_suffix(".log").write_text(result.stdout + result.stderr)
    assert result.returncode == 0, result.stdout + result.stderr
    return result


@pytest.mark.parametrize(
    "layout", ["package", "module", "package-over-module", "ancestor"]
)
def test_actual_bytecode_plugins_block_same_source_admission_at_every_level(
    project: Any, tmp_path: Path, layout: str
) -> None:
    source, _, policy, known = project
    package = source / "plugins"
    package.mkdir()
    (package / "data.txt").write_text("A directory alone does not prove a namespace\n")
    plugin = "plugins.child" if layout == "ancestor" else "plugins"
    (source / "tests/conftest.py").write_text(f"pytest_plugins = [{plugin!r}]\n")
    policy["serial_shared_dependencies"]["tests/conftest.py"] = checksum(
        source / "tests/conftest.py"
    )
    if layout == "ancestor":
        (package / "child.py").write_text("VALUE = 1\n")
        policy["serial_shared_dependencies"]["plugins/child.py"] = checksum(
            package / "child.py"
        )
    if layout == "package-over-module":
        (source / "plugins.py").write_text("VALUE = 'source decoy'\n")
        policy["serial_shared_dependencies"]["plugins.py"] = checksum(
            source / "plugins.py"
        )
    compiled = (
        source / "plugins.pyc" if layout == "module" else package / "__init__.pyc"
    )
    marker = tmp_path / "executed.txt"
    reports: list[dict[str, Any]] = []
    for phase in ["before", "after"]:
        generator = tmp_path / "compiled_source.py"
        generator.write_text(
            f"from pathlib import Path\nPath({str(marker)!r}).write_text({phase!r})\n"
        )
        py_compile.compile(str(generator), cfile=str(compiled), doraise=True)
        (source / "ci/local-policy.json").write_text(json.dumps(policy))
        head = freeze(source)
        marker.unlink(missing_ok=True)
        execute(source, tmp_path / phase)
        assert marker.read_text() == phase
        # Same-source planning exercises the grid pattern without decoding a binary diff.
        row = contract.build(source, head, head, policy, known)["placements"][SAFE]
        admissions: list[str] = []
        for level in range(5):
            plan = risk.make_plan(source, head, head, minimum=level)
            assert plan["risk"] == level
            try:
                contract.require(plan, source)
            except ValueError as error:
                admissions.append(str(error))
            else:
                admissions.append("incorrectly admitted")
        reports.append(
            {
                "phase": phase,
                "head": head,
                "compiled_path": compiled.relative_to(source).as_posix(),
                "compiled_sha256": checksum(compiled),
                "loaded_marker": marker.read_text(),
                "placement": row,
                "admissions_0_through_4": admissions,
                "runtime_groups": local_policy.serial_groups([SAFE], policy, source),
            }
        )
    (tmp_path / "format-report.json").write_text(json.dumps(reports, indent=2))
    for report in reports:
        row = report["placement"]
        assert row["status"] == "requalification-required", report
        assert report["compiled_path"] in row["dependencies"]
        assert row["closure_problems"]
        assert row["placement"] == "globally-exclusive-serial"
        assert all(
            "requalification-required" in result
            for result in report["admissions_0_through_4"]
        )
        assert report["runtime_groups"]["globally-exclusive-serial"] == [SAFE]


@pytest.mark.parametrize(
    "path,plugin",
    [
        ("plugins.pyc", "plugins"),
        ("plugins/__init__.pyc", "plugins"),
        ("plugins/__init__.pyc", "plugins.child"),
        ("PLUGINS.PYC", "plugins"),
        ("plugins/__INIT__.PYC", "plugins"),
        ("plugins.cpython-311-x86_64-linux-gnu.so", "plugins.child"),
        ("plugins.abi3.so", "plugins"),
        ("plugins.pyd", "plugins"),
        ("plugins/__init__.pyd", "plugins"),
        ("plugins/__init__.pyi", "plugins"),
        ("plugins/__init__.unknown", "plugins"),
        ("plugins.unknown", "plugins"),
        ("plugins/__init__", "plugins"),
        ("plugins/__pycache__/__init__.cpython-311.pyc", "plugins"),
        ("__pycache__/plugins.cpython-312.pyc", "plugins"),
    ],
)
@pytest.mark.parametrize("binding", [False, True])
def test_unsupported_formats_block_even_with_a_source_shaped_body_or_binding(
    project: Any, path: str, plugin: str, binding: bool
) -> None:
    source, _, policy, _ = project
    files = snapshot(source)
    files["tests/conftest.py"] = f"pytest_plugins = [{plugin!r}]\n".encode()
    files["plugins/child.py"] = b"VALUE = 1\n"
    files["plugins.py"] = b"VALUE = 'source alternative'\n"
    files[path] = (
        b"VALUE = 1\n"  # Parsing bytes as Python cannot establish format safety.
    )
    scope = policy["serial_dependency_scope"]
    if binding:
        scope["dynamic_imports"][path] = {
            "source_sha256": hashlib.sha256(files[path] or b"").hexdigest(),
            "targets": [],
            "reason": "Synthetic record must not authorize an unsupported format",
        }
    required, problems = closure(files, SAFE, scope)
    assert path in required
    assert (
        f"{path}: unsupported executable format; only .py source is supported"
        in problems
    )


def test_supported_namespace_and_unrelated_addition_remain_independent(
    project: Any, tmp_path: Path
) -> None:
    source, _, policy, known = project
    child = source / "plugins/child.py"
    child.parent.mkdir()
    marker = tmp_path / "namespace-child.txt"
    child.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('source child')\n"
    )
    (child.parent / "data.json").write_text("{}\n")
    conftest = source / "tests/conftest.py"
    conftest.write_text("pytest_plugins = ['plugins.child']\n")
    for path in [child, conftest]:
        policy["serial_shared_dependencies"][path.relative_to(source).as_posix()] = (
            checksum(path)
        )
    review_execution(policy, source, "plugins/child.py", [])
    (source / "ci/local-policy.json").write_text(json.dumps(policy))
    base = freeze(source)
    execute(source, tmp_path / "namespace")
    assert marker.read_text() == "source child"
    assert contract.build(source, base, base, policy, known)["status"] == "passed"
    other = source / "tests/test_unrelated.py"
    other.write_text("def test_unrelated(): assert True\n")
    known["modules"]["tests/test_unrelated.py"] = {"test_unrelated": {"": []}}
    (source / "ci/test-inventory.json").write_text(json.dumps(known))
    taxonomy_path = source / "ci/test-profiles.json"
    taxonomy = json.loads(taxonomy_path.read_text())
    taxonomy["modules"]["tests/test_unrelated.py"] = taxonomy["modules"][
        "tests/test_other.py"
    ]
    taxonomy_path.write_text(json.dumps(taxonomy))
    head = freeze(source)
    assert contract.build(source, base, head, policy, known)["status"] == "passed"
    contract.require(risk.make_plan(source, head, head), source)
    assert local_policy.serial_groups([SAFE], policy, source)[
        "concurrency-safe-serial"
    ] == [SAFE]
