"""Implicit pytest setup and local import dispatch must invalidate stale overlap."""

from __future__ import annotations

import copy
import hashlib
import json
import os
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
from local.policy import read as read_policy
from local.serial_dependencies import closure, snapshot
from test_risk import freeze
from test_scheduling_contract import SAFE, checksum, review_execution
from test_scheduling_contract import project as project


@pytest.mark.parametrize(
    "implicit", ["conftest.py", "tests/conftest.py", "tests/__init__.py"]
)
@pytest.mark.parametrize("changed", ["implicit", "transitive"])
def test_implicit_setup_changes_block_previously_passed_overlap(
    project: Any, tmp_path: Path, implicit: str, changed: str
) -> None:
    source, _, policy, known = project
    marker = tmp_path / "implicit-ran"
    helper = source / "support/fixture.py"
    helper.parent.mkdir()
    helper.write_text("VALUE = 1\n")
    entry = source / implicit
    body = "from pathlib import Path\nfrom support.fixture import VALUE\n"
    if entry.name == "conftest.py":
        body += "import pytest\n@pytest.fixture(autouse=True)\ndef implicit_setup():\n"
        body += f"    assert VALUE == 1\n    Path({str(marker)!r}).write_text('ran')\n"
    else:
        body += f"assert VALUE == 1\nPath({str(marker)!r}).write_text('ran')\n"
    entry.write_text(body)
    config = source / "pyproject.toml"
    config.write_text(
        '[tool.pytest.ini_options]\nmarkers=["serial: synthetic isolation"]\n'
    )
    # Prove implicit discovery rather than relying on an explicit conftest listing.
    policy["serial_dependency_scope"]["support_modules"] = ["tests/helper.py"]
    for path in (entry, helper, config):
        policy["serial_shared_dependencies"][path.relative_to(source).as_posix()] = (
            checksum(path)
        )
    review_execution(policy, source, implicit, [])
    (source / "ci/local-policy.json").write_text(json.dumps(policy))
    base = freeze(source)
    assert contract.build(source, base, base, policy, known)["status"] == "passed"
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
            f"--basetemp={tmp_path / 'pytest-tmp'}",
            "-o",
            f"cache_dir={tmp_path / 'cache'}",
        ],
        cwd=source,
        env={
            **clean_environment(),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": os.pathsep.join((str(source), str(source / "tests"))),
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert marker.read_text() == "ran"
    target = entry if changed == "implicit" else helper
    target.write_text(target.read_text() + "# changed after reviewed setup\n")
    report = contract.build(source, base, freeze(source), policy, known)
    assert report["status"] == "requalification-required"
    assert any(
        item["path"] == target.relative_to(source).as_posix()
        for item in report["placements"][SAFE]["differences"]
    )
    assert local_policy.serial_groups([SAFE], policy, source)[
        "globally-exclusive-serial"
    ] == [SAFE]


def test_nested_pytest_parents_and_initializers_are_transitive(project: Any) -> None:
    _, _, policy, _ = project
    files: dict[str, bytes | None] = {
        "tests/nested/test_safe.py": b"def test_safe(): pass\n",
        "conftest.py": b"import root_helper\n",
        "tests/conftest.py": b"import parent_helper\n",
        "tests/nested/conftest.py": b"import nearest_helper\n",
        "tests/__init__.py": b"import package_helper\n",
        "tests/nested/__init__.py": b"import nested_helper\n",
        "tests/unrelated/conftest.py": b"raise AssertionError('unrelated')\n",
        "tests/helper.py": b"",
        **{
            name + ".py": b""
            for name in (
                "root_helper",
                "parent_helper",
                "nearest_helper",
                "package_helper",
                "nested_helper",
            )
        },
    }
    scope = copy.deepcopy(policy["serial_dependency_scope"])
    scope["support_modules"] = ["tests/helper.py"]
    scope["dynamic_imports"]["tests/nested/test_safe.py"] = {
        "source_sha256": hashlib.sha256(
            files["tests/nested/test_safe.py"] or b""
        ).hexdigest(),
        "targets": [],
        "reason": "Synthetic no-op test body",
    }
    for path in [
        "conftest.py",
        "tests/conftest.py",
        "tests/nested/conftest.py",
        "tests/__init__.py",
        "tests/nested/__init__.py",
    ]:
        scope["dynamic_imports"][path] = {
            "source_sha256": hashlib.sha256(files[path] or b"").hexdigest(),
            "targets": [],
            "reason": "Synthetic static import reviewed for execution effects",
        }
    required, problems = closure(files, "tests/nested/test_safe.py::test_safe", scope)
    assert not problems
    assert set(files) - {"tests/unrelated/conftest.py"} <= required
    assert "tests/unrelated/conftest.py" not in required


@pytest.mark.parametrize(
    "dispatch",
    [
        "import importlib\nimportlib.import_module('.choice', 'plugins')\n",
        "import importlib as lib\nload = lib.import_module\nagain = load\nagain('.choice', package='plugins')\n",
        "from importlib import import_module\nimport_module(name='.choice', package='plugins')\n",
        "from importlib import import_module as load\nload('plugins.choice')\n",
        "import importlib\nlib = importlib\nlib.import_module('plugins.choice')\n",
        "import importlib\nload: object = importlib.import_module\nload('plugins.choice')\n",
        "load = __import__\nload('plugins.choice')\n",
    ],
)
def test_local_plugins_aliases_and_relative_dispatch_follow_all_helpers(
    project: Any, dispatch: str
) -> None:
    source, _, policy, known = project
    additions = {
        "plugins/__init__.py": "from . import initializer\n",
        "plugins/initializer.py": "VALUE = 1\n",
        "plugins/plugin.py": dispatch,
        "plugins/choice.py": "from shared.helper import VALUE\n",
        "shared/helper.py": "from deep.leaf import VALUE\n",
        "deep/leaf.py": "VALUE = 1\n",
    }
    test = source / "tests/test_safe.py"
    test.write_text("pytest_plugins = ('plugins.plugin',)\n" + test.read_text())
    for name, text in additions.items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        policy["serial_shared_dependencies"][name] = checksum(path)
    policy["serial"][0]["dependencies"]["tests/test_safe.py"] = checksum(test)
    review_execution(policy, source, "tests/test_safe.py", [])
    for path in ["plugins/__init__.py", "plugins/choice.py", "shared/helper.py"]:
        review_execution(policy, source, path, [])
    policy["serial_dependency_scope"]["dynamic_imports"]["plugins/plugin.py"] = {
        "source_sha256": checksum(source / "plugins/plugin.py"),
        "targets": ["plugins/choice.py"],
        "reason": "Synthetic reviewed dispatch; literal-looking calls are not authority",
    }
    (source / "ci/local-policy.json").write_text(json.dumps(policy))
    base = freeze(source)
    row = contract.build(source, base, base, policy, known)["placements"][SAFE]
    assert row["status"] == "passed", row
    assert set(additions) <= set(row["dependencies"])
    (source / "deep/leaf.py").write_text("VALUE = 2\n")
    report = contract.build(source, base, freeze(source), policy, known)
    assert report["status"] == "requalification-required"
    assert report["placements"][SAFE]["placement"] == "globally-exclusive-serial"


@pytest.mark.parametrize(
    "dispatch",
    [
        "import importlib\nimportlib.import_module('.choice', package)\n",
        "import importlib\nimportlib.import_module('/outside/choice')\n",
        "import importlib\nimportlib.import_module('plugins.choice-alias')\n",
        "from importlib import import_module\npackage = 'plugins'\nimport_module('.choice', package)\n",
        "from importlib import import_module\npackage = 'plugins'\ndef load(package): return import_module('.choice', package)\n",
        "import importlib\nload = importlib.import_module\nfrom collections import defaultdict as load\nload('plugins.choice')\n",
        "import importlib\nload = importlib.import_module\ndef load(name): return other_loader(name)\nload('plugins.choice')\n",
        "import importlib\nload = importlib.import_module\nfor load in loaders: load('plugins.choice')\n",
        "from plugins import import_module as load\nload('plugins.choice')\n",
        "from . import import_module as load\nload('plugins.choice')\n",
        "import importlib\nload = importlib.import_module\nload(module_name)\n",
        "import importlib\nload = getattr(importlib, attribute)\nload('plugins.choice')\n",
        "import builtins\nload = builtins.__import__\nload('plugins.choice')\n",
        "load = unknown_loader.import_module\nload('plugins.choice')\n",
        "import importlib\nloaders = [importlib.import_module]\nloaders[0]('plugins.choice')\n",
        "import importlib\nload = importlib.import_module\nload = choose_loader()\nload('plugins.choice')\n",
        "import importlib\nload = importlib.import_module\nload = choose_loader()\nagain = load\nagain('plugins.choice')\n",
        "import importlib\nlib = importlib\nlib = choose_module()\nagain = lib\nagain.import_module('plugins.choice')\n",
        "import importlib\nimportlib.import_module('.missing', 'plugins')\n",
        "import importlib\nimportlib.import_module(*args, **kwargs)\n",
        "__import__('plugins.choice', globals(), locals(), ['VALUE'], 1)\n",
    ],
)
def test_unresolved_dispatch_requires_exact_reviewed_binding(
    project: Any, dispatch: str
) -> None:
    source, _, policy, _ = project
    test = source / "tests/test_safe.py"
    test.write_text(dispatch)
    for name, text in {
        "plugins/choice.py": "from shared import VALUE\n",
        "shared.py": "VALUE = 1\n",
    }.items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    files = snapshot(source)
    scope = policy["serial_dependency_scope"]
    review_execution(policy, source, "plugins/choice.py", [])
    assert closure(files, SAFE, scope)[1]
    scope["dynamic_imports"]["tests/test_safe.py"] = {
        "source_sha256": checksum(test),
        "targets": ["plugins/choice.py"],
        "reason": "Disposable synthetic finite dispatch review",
    }
    required, problems = closure(files, SAFE, scope)
    assert not problems and {"plugins/choice.py", "shared.py"} <= required
    test.write_text(test.read_text() + "# source changed\n")
    assert closure(snapshot(source), SAFE, scope)[1]


@pytest.mark.parametrize(
    "path", ["conftest.py", "__init__.py", "plugins/plugin.py", "support/helper.py"]
)
def test_reviewed_manifests_can_name_implicit_and_external_helpers(
    tmp_path: Path, path: str
) -> None:
    original = Path(__file__).resolve().parents[1] / "local-policy.json"
    policy = json.loads(original.read_text())
    policy["serial_shared_dependencies"][path] = "0" * 64
    (tmp_path / "local-policy.json").write_text(json.dumps(policy))
    assert path in local_policy.read(tmp_path)["serial_shared_dependencies"]


def test_exact_module_review_may_declare_no_additional_targets(tmp_path: Path) -> None:
    policy = json.loads(
        (Path(__file__).resolve().parents[1] / "local-policy.json").read_text()
    )
    policy["serial_dependency_scope"]["dynamic_imports"]["tests/synthetic.py"] = {
        "source_sha256": "0" * 64,
        "targets": [],
        "reason": "Synthetic module reviewed with no additional imports",
    }
    (tmp_path / "local-policy.json").write_text(json.dumps(policy))
    assert (
        local_policy.read(tmp_path)["serial_dependency_scope"]["dynamic_imports"][
            "tests/synthetic.py"
        ]["targets"]
        == []
    )


@pytest.mark.parametrize(
    "path",
    [
        "../conftest.py",
        "/conftest.py",
        "./conftest.py",
        "support/../helper.py",
        "support\\helper.py",
    ],
)
def test_reviewed_helper_paths_cannot_escape_or_alias_the_source(
    tmp_path: Path, path: str
) -> None:
    policy = json.loads(
        (Path(__file__).resolve().parents[1] / "local-policy.json").read_text()
    )
    policy["serial_shared_dependencies"][path] = "0" * 64
    (tmp_path / "local-policy.json").write_text(json.dumps(policy))
    with pytest.raises(ValueError, match="Invalid shared serial dependency"):
        local_policy.read(tmp_path)


@pytest.mark.parametrize(
    "path",
    [
        ".gitattributes",
        "ci/acceptance.py",
        "benchmarks/focused_execution.py",
        "scripts/grouped_lifecycle_r_fixture.py",
        "docs/vertex-qualification-price-evidence.json",
        "src/judgmentllm/study/plans.py",
        "tests/test_study_workflow.py",
    ],
)
def test_serial_dependency_overrides_accept_canonical_control_paths(
    tmp_path: Path, path: str
) -> None:
    policy = json.loads(
        (Path(__file__).resolve().parents[1] / "local-policy.json").read_text()
    )
    historical = copy.deepcopy(policy["serial_shared_dependencies"])
    policy["serial"][0]["dependencies"][path] = "ab" * 32
    (tmp_path / "local-policy.json").write_text(json.dumps(policy))
    loaded = read_policy(tmp_path)
    assert loaded == policy
    assert loaded["serial_shared_dependencies"] == historical


@pytest.mark.parametrize(
    "path",
    [
        "",
        ".",
        "..",
        "../policy.py",
        "/ci/policy.py",
        "C:/ci/policy.py",
        "C:\\ci\\policy.py",
        "ci\\policy.py",
        "ci:policy.py",
        "tests/../policy.py",
        "./tests/policy.py",
        "tests//policy.py",
        "src/./policy.py",
        "tests/policy.py/",
    ],
)
def test_serial_dependency_override_paths_are_canonical(
    tmp_path: Path, path: str
) -> None:
    policy = json.loads(
        (Path(__file__).resolve().parents[1] / "local-policy.json").read_text()
    )
    policy["serial"][0]["dependencies"][path] = "0" * 64
    (tmp_path / "local-policy.json").write_text(json.dumps(policy))
    with pytest.raises(ValueError, match="Invalid serial placement"):
        read_policy(tmp_path)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "a" * 63,
        "a" * 65,
        "A" * 64,
        "g" * 64,
        " " * 64,
        "0" * 63 + "\n",
        "0" * 63 + " ",
        "\uff10" * 64,
        None,
        False,
        int("1" * 64),
        ["0"] * 64,
        {"value": "0"},
    ],
)
def test_serial_dependency_overrides_require_lowercase_sha256_strings(
    tmp_path: Path, value: object
) -> None:
    policy = json.loads(
        (Path(__file__).resolve().parents[1] / "local-policy.json").read_text()
    )
    policy["serial"][0]["dependencies"]["tests/test_study_workflow.py"] = value
    (tmp_path / "local-policy.json").write_text(json.dumps(policy))
    with pytest.raises(ValueError, match="Invalid serial placement"):
        read_policy(tmp_path)


def test_serial_dependency_overrides_remain_nonempty(tmp_path: Path) -> None:
    policy = json.loads(
        (Path(__file__).resolve().parents[1] / "local-policy.json").read_text()
    )
    policy["serial"][0]["dependencies"] = {}
    (tmp_path / "local-policy.json").write_text(json.dumps(policy))
    with pytest.raises(ValueError, match="Invalid serial placement"):
        read_policy(tmp_path)


def test_serial_dependency_control_overrides_are_per_placement_and_source_bound(
    project: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, _, policy, known = project
    second = "tests/test_other.py::test_other"
    other = source / "tests/test_other.py"
    other.write_text(
        "import pytest\n\n\n@pytest.mark.serial\ndef test_other():\n    assert True\n"
    )
    known["modules"]["tests/test_other.py"]["test_other"][""] = ["serial"]
    (source / "ci/test-inventory.json").write_text(json.dumps(known))
    policy["serial"].append(
        {
            "selector": second,
            "placement": "concurrency-safe-serial",
            "reason": "Second disposable private-store placement",
            "dependencies": {"tests/test_other.py": checksum(other)},
        }
    )
    policy["isolation_reasons"][second] = "Second disposable private-store case"
    review_execution(policy, source, "tests/test_other.py", [])
    control = source / ".gitattributes"
    control.write_text("*.txt text eol=lf\n")
    policy["serial_dependency_scope"]["controls"].append(".gitattributes")
    policy["serial_shared_dependencies"][".gitattributes"] = checksum(control)
    historical = copy.deepcopy(policy["serial_shared_dependencies"])
    control.write_text("*.txt text eol=crlf\n")
    policy["serial"][0]["dependencies"][".gitattributes"] = checksum(control)
    (source / "ci/local-policy.json").write_text(json.dumps(policy))
    # The imported callable predates project's deepcopy-reader monkeypatch.
    monkeypatch.setattr(local_policy, "read", read_policy)
    loaded = read_policy(source / "ci")
    rows = local_policy.serial_placement_report([SAFE, second], loaded, source)
    assert rows[SAFE]["status"] == "passed"
    assert rows[SAFE]["dependencies"][".gitattributes"] == checksum(control)
    assert rows[second]["status"] == "requalification-required"
    assert rows[second]["differences"] == [
        {
            "path": ".gitattributes",
            "expected_sha256": historical[".gitattributes"],
            "actual_sha256": checksum(control),
            "reason": "changed-dependency",
        }
    ]
    groups = local_policy.serial_groups([SAFE, second], loaded, source)
    assert groups["concurrency-safe-serial"] == [SAFE]
    assert groups["globally-exclusive-serial"] == [second]
    assert loaded["serial_shared_dependencies"] == historical

    loaded["serial"][1]["dependencies"][".gitattributes"] = checksum(control)
    (source / "ci/local-policy.json").write_text(json.dumps(loaded))
    validated = read_policy(source / "ci")
    base = freeze(source)
    plan = risk.make_plan(source, base, base)
    assert plan["local_policy"] == validated
    assert contract.require(plan, source)["status"] == "passed"
    before = plan["scheduling_contract"]["placements"]
    assert all(row["status"] == "passed" for row in before.values())
    assert validated["serial_shared_dependencies"] == historical

    (source / "tests/test_added.py").write_text("def test_added(): assert True\n")
    added = copy.deepcopy(known)
    added["modules"]["tests/test_added.py"] = {"test_added": {"": []}}
    (source / "ci/test-inventory.json").write_text(json.dumps(added))
    after = contract.build(source, base, freeze(source), validated, known)
    assert after["status"] == "passed"
    assert all(
        row["dependency_sha256"] == before[selector]["dependency_sha256"]
        for selector, row in after["placements"].items()
    )
