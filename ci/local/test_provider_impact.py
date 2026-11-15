"""Reviewed source closures must retain native schemas and early feedback."""

from __future__ import annotations

import ast
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import check
import risk
from selection import HERE, impact, inventory_items, manifest, read
from test_risk import freeze
from test_risk import repository as repository

SCHEMAS = [
    ("together_schema", {"tests/test_native_other_providers.py"}),
    ("ollama_schema", {"tests/test_native_other_providers.py"}),
    (
        "openai_schema",
        {
            "tests/test_native_schema.py",
            "tests/test_native_preseal_validation.py",
            "tests/test_native_selection.py",
            "tests/test_native_v2_compatibility.py",
            "tests/test_native_schema_privacy.py",
        },
    ),
    ("anthropic_schema", {"tests/test_native_other_providers.py"}),
    ("google_schema", {"tests/test_google_schema.py", "tests/test_native_v2.py"}),
]


@pytest.mark.parametrize("module,required_modules", SCHEMAS)
def test_level_two_schema_changes_select_every_required_parameter(
    repository: Any, module: str, required_modules: set[str]
) -> None:
    source, base = repository
    path = source / f"src/judgmentllm/providers/{module}.py"
    path.parent.mkdir(parents=True)
    path.write_text("def project(): return 1\n")
    plan = risk.make_plan(source, base, freeze(source))
    assert plan["risk"] == 2 and not plan["unknown_production_dependencies"]
    assert required_modules <= set(plan["impact"])
    items = inventory_items(read(HERE / "test-inventory.json"))
    selected = manifest(
        items,
        profile="impacted",
        impacted=plan["impact"],
        include_r=False,
        include_resources=False,
        platform="linux",
    )
    required = {
        item["nodeid"]
        for item in items
        if item["nodeid"].split("::")[0] in required_modules
    }
    assert required
    assert required <= {
        case["nodeid"]
        for case in selected["cases"]
        if case["disposition"] == "required"
    }


@pytest.mark.parametrize(
    "module", ["gemini_developer", "typesafe_schema", "openai_future_schema"]
)
def test_unreviewed_level_two_source_escalates_to_complete_platforms(
    repository: Any, module: str
) -> None:
    source, base = repository
    name = f"src/judgmentllm/providers/{module}.py"
    path = source / name
    path.parent.mkdir(parents=True)
    path.write_text("def project(): return 1\n")
    assert risk.classify(name, None, path.read_text())[0] == 2
    plan = risk.make_plan(source, base, freeze(source))
    assert plan["risk"] == 3 and name in plan["unknown_production_dependencies"]
    assert all(lane["profile"] == "full" for lane in plan["lanes"])


def test_known_source_with_missing_closure_also_escalates(
    repository: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, base = repository
    name = "src/judgmentllm/providers/together_schema.py"
    path = source / name
    path.parent.mkdir(parents=True)
    path.write_text("def project(): return 1\n")

    def missing(
        paths: list[str], policy: dict[str, Any]
    ) -> tuple[list[str], list[str]]:
        policy = {
            **policy,
            "provider_sources": {
                key: value
                for key, value in policy["provider_sources"].items()
                if key != name
            },
        }
        return impact(paths, policy)

    monkeypatch.setattr(risk, "impact", missing)
    plan = risk.make_plan(source, base, freeze(source))
    assert name in plan["unknown_production_dependencies"]
    assert plan["risk"] == 3 and all(
        lane["profile"] == "full" for lane in plan["lanes"]
    )


def test_reviewed_closures_cover_direct_test_imports_and_real_source_paths() -> None:
    policy = read(HERE / "test-profiles.json")
    for source in policy["provider_sources"]:
        assert (HERE.parent / source).is_file()
    for test in sorted((HERE.parent / "tests").glob("test_*.py")):
        modules: set[str] = set()
        for node in ast.walk(ast.parse(test.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module.startswith("judgmentllm.providers."):
                    modules.add(node.module.split(".")[2])
                elif node.module == "judgmentllm.providers":
                    modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.Import):
                modules.update(
                    alias.name.split(".")[2]
                    for alias in node.names
                    if alias.name.startswith("judgmentllm.providers.")
                )
        for module in modules:
            path = f"src/judgmentllm/providers/{module}.py"
            if path in policy["provider_sources"]:
                selected, _ = impact([path], policy)
                assert test.relative_to(HERE.parent).as_posix() in selected, path


@pytest.mark.parametrize(
    "module,required_modules", [*SCHEMAS[:3], ("gemini_future", set[str]())]
)
def test_fast_feedback_uses_exact_closure_or_expands(
    repository: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    module: str,
    required_modules: set[str],
) -> None:
    source, base = repository
    python = (
        source
        / ".venv"
        / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    )
    python.parent.mkdir(parents=True)
    python.touch()
    (source / "ci").mkdir()
    shutil.copyfile(HERE / "test-profiles.json", source / "ci/test-profiles.json")
    (source / "uv.lock").write_text("synthetic offline lock\n")
    base = freeze(source)
    path = source / f"src/judgmentllm/providers/{module}.py"
    path.parent.mkdir(parents=True)
    path.write_text("def project(): return 1\n")
    calls: list[list[str]] = []
    original = subprocess.run

    def execute(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if command[0] == "git":
            return cast(subprocess.CompletedProcess[str], original(command, **kwargs))
        calls.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(check, "ROOT", source)
    monkeypatch.setattr(check.subprocess, "run", execute)
    output = tmp_path / "fast"
    assert check.fast(base, output, [], 2) == 0
    report = read(output / "summary.json")
    assert report["qualification"] is False
    assert report["expanded_selection"] is (not required_modules)
    if required_modules:
        assert required_modules <= set(calls[-1])
    else:
        assert report["targets"] == ["tests", "ci/local"]


def test_mingit_documentation_matches_the_launcher_directory() -> None:
    guide = (HERE.parent / "docs/local-ci.md").read_text(encoding="utf-8")
    launcher = (HERE / "local/windows.ps1").read_text(encoding="utf-8")
    assert r"C:\JudgmentLLM-ci\tools\git" in guide
    assert r"'tools\git\cmd'" in launcher
    assert "mingit-2.56.0.4" not in guide
