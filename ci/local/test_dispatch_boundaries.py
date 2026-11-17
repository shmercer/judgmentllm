"""Real pytest imports must not bypass source-bound scheduling reconstruction."""

from __future__ import annotations

import ast
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
from local.serial_dependencies import plugin_dispatch, requires_review
from test_risk import freeze
from test_scheduling_contract import SAFE, checksum, review_execution
from test_scheduling_contract import project as project

PLUGIN = "plugins.fixture_plugin"
PLUGIN_CASES = {
    "plugin_append": f"pytest_plugins = []\npytest_plugins.append({PLUGIN!r})\n",
    "plugin_alias": f"pytest_plugins = []\nescaped = pytest_plugins\nescaped.extend([{PLUGIN!r}])\n",
    "plugin_argument": f"def register(names): names.append({PLUGIN!r})\npytest_plugins = []\nregister(pytest_plugins)\n",
    "plugin_attribute": f"import sys\npytest_plugins = []\nsys.modules[__name__].pytest_plugins.append({PLUGIN!r})\n",
    "plugin_getattr": f"import sys\npytest_plugins = []\ngetattr(sys.modules[__name__], 'pytest_plugins').append({PLUGIN!r})\n",
    "plugin_subscript": f"pytest_plugins = [None]\npytest_plugins[0] = {PLUGIN!r}\n",
    "plugin_augmented": f"pytest_plugins = ()\npytest_plugins += ({PLUGIN!r},)\n",
    "plugin_globals": f"pytest_plugins = []\nglobals()['pytest_plugins'].append({PLUGIN!r})\n",
    "module_getattr_plugin": f"def __getattr__(name):\n    if name == 'pytest_' + 'plugins': return [{PLUGIN!r}]\n    raise AttributeError(name)\n",
    "module_type_plugin": f"import sys\nsys.modules[__name__].__class__ = type('PluginModule', (type(sys),), {{'pytest_' + 'plugins': [{PLUGIN!r}]}})\n",
}
LOADER_PREFIX = """import importlib as lib
import builtins
from types import SimpleNamespace
real_loader = getattr(builtins, '__import__')
def alternate(_apparent_name):
    return real_loader('hidden_support')
"""
LOADER_CASES = {
    "builtins_getattr": "import builtins\nload = getattr(builtins, '__import__')\nmodule_name = 'hidden_support'\nloaded = load(module_name)\n",
    "importlib_star": "from importlib import *\nmodule_name = 'hidden_support'\nloaded = import_module(module_name)\n",
    "pytest_importorskip": "import pytest\nloaded = pytest.importorskip('hidden_support')\n",
    "pkgutil_resolve_name": "from pkgutil import resolve_name as load\nloaded = load('hidden_support')\n",
    "pydoc_locate": "from pydoc import locate as load\nloaded = load('hidden_support')\n",
    "runpy_run_path": "import runpy\nloaded = runpy.run_path('hidden_support.py')\n",
    "runpy_run_module": "import runpy\nloaded = runpy.run_module('hidden_support')\n",
    "match_module": LOADER_PREFIX
    + "replacement = SimpleNamespace(import_module=alternate)\nmatch replacement:\n    case lib: pass\nloaded = lib.import_module('declared_support')\n",
    "match_loader": LOADER_PREFIX
    + "from importlib import import_module as load\nmatch [alternate]:\n    case [load]: pass\nloaded = load('declared_support')\n",
    "except_module": LOADER_PREFIX
    + "class LoaderError(Exception):\n    import_module = staticmethod(alternate)\ntry:\n    raise LoaderError()\nexcept LoaderError as lib:\n    loaded = lib.import_module('declared_support')\n",
    "except_loader": LOADER_PREFIX
    + "from importlib import import_module as load\nclass LoaderError(Exception):\n    __call__ = staticmethod(alternate)\ntry:\n    raise LoaderError()\nexcept LoaderError as load:\n    loaded = load('declared_support')\n",
    "module_alias_rebind": LOADER_PREFIX
    + "saved = lib.import_module\nalias = lib\nalias.import_module = alternate\ntry:\n    loaded = lib.import_module('declared_support')\nfinally:\n    alias.import_module = saved\n",
}
CASES = {**PLUGIN_CASES, **LOADER_CASES}


@pytest.mark.parametrize(
    "source",
    [
        "from arbitrary_helper import *",
        "import builtins as b\nlookup = getattr(b, method)",
        "getattr(object_name, attribute)",
        "setattr(object_name, attribute, value)",
        "registry = vars(object_name)",
        "registry = globals()",
        "registry = __builtins__",
        "key = '__import__'\nregistry[key](target)",
        "module.__dict__[computed_key].append(value)",
        "frame.f_globals[computed_key](target)",
        "operator.attrgetter(computed_key)(module)",
        "exec(code)",
        "callback = unknown_loader.import_module",
    ],
)
def test_unknown_reflective_loader_forms_require_review(source: str) -> None:
    assert requires_review(ast.parse(source))


@pytest.mark.parametrize(
    "source",
    [
        "pytest_plugins = []\nescaped = pytest_plugins",
        "pytest_plugins = []\nconsume(pytest_plugins)",
        "if enabled:\n    pytest_plugins = ['plugin']",
        "pytest_plugins = []\npytest_plugins = ['plugin']",
        "pytest_plugins: list[str]",
        "pytest_plugins = escaped = []",
        "pytest_plugins = []\ndel pytest_plugins[:]",
        "pytest_plugins = [*unknown]",
        "def capture(pytest_plugins): pass",
        "def mutate():\n    global pytest_plugins",
        "try:\n    operation()\nexcept Exception as pytest_plugins:\n    pass",
        "match value:\n    case {'plugins': pytest_plugins}: pass",
    ],
)
def test_nonliteral_or_escaped_plugin_declarations_require_review(source: str) -> None:
    assert plugin_dispatch(ast.parse(source))[1]


@pytest.mark.parametrize(
    "source,plugins",
    [
        ("pytest_plugins = []", []),
        ("pytest_plugins = 'plugin'", ["plugin"]),
        ("pytest_plugins = ['plugin']", ["plugin"]),
        ("pytest_plugins = ('one', 'two')", ["one", "two"]),
    ],
)
def test_only_simple_literal_plugin_declarations_are_automatic(
    source: str, plugins: list[str]
) -> None:
    assert plugin_dispatch(ast.parse(source)) == (plugins, False)
    assert not requires_review(ast.parse(source))


@pytest.mark.parametrize(
    "source",
    [
        "",
        "'Documentation'\npass",
        "VALUE = [None, True, -2, +3, ('x',), {'key': {4}}]",
    ],
)
def test_supported_static_module_subset(source: str) -> None:
    assert not requires_review(ast.parse(source))


@pytest.mark.parametrize(
    "source",
    [
        "import helper as alias",
        "from helper import VALUE as value",
        "from __future__ import annotations",
        "unknown_api(module_name)",
        "def ordinary_function(): return 1",
        "async def ordinary_function(): return 1",
        "def __getattr__(name): return registry[name]",
        "class Module: pass",
        "callback = lambda: 1",
        "VALUE: unknown_annotation = 1",
        "pytest_plugins: tuple[str, ...] = ('plugin',)",
        "alias = other_name",
        "from helper import *",
        "from external import hook as __getattr__",
        "import external as __getattr__",
        "from external import pytest_plugins",
        "from external import plugins as pytest_plugins",
        "__path__ = ['other']",
        "module.attribute = 1",
        "namespace[key] = 1",
        "VALUE = set()",
        "VALUE = 1 + 2",
        "if flag: import helper",
        "for value in values: pass",
        "assert condition",
    ],
)
def test_every_form_outside_static_subset_requires_review(source: str) -> None:
    assert requires_review(ast.parse(source))


@pytest.mark.parametrize("case", CASES, ids=CASES)
@pytest.mark.parametrize("changed", ["entry", "transitive"])
def test_real_dispatch_requires_binding_and_tracks_actual_helpers(
    project: Any, tmp_path: Path, case: str, changed: str
) -> None:
    source, _, policy, known = project
    marker = tmp_path / "actual-import.marker"
    bodies = {
        "hidden_support.py": "from deep_support import VALUE\nORIGIN = 'hidden'\n",
        "declared_support.py": "VALUE = 7\nORIGIN = 'declared'\n",
        "deep_support.py": f"from pathlib import Path\nVALUE = 7\nPath({str(marker)!r}).write_text('hidden')\n",
        "plugins/__init__.py": "",
        "plugins/fixture_plugin.py": "import pytest\nfrom hidden_support import VALUE\n@pytest.fixture\ndef loaded(): return VALUE\n",
        "pyproject.toml": '[tool.pytest.ini_options]\nmarkers=["serial: synthetic isolation"]\n',
    }
    plugin = case in PLUGIN_CASES
    declaration = "conftest.py" if plugin else "tests/test_safe.py"
    bodies[declaration] = CASES[case]
    if plugin:
        bodies["tests/test_safe.py"] = (
            "import pytest\n@pytest.mark.serial\ndef test_safe(loaded): assert loaded == 7\n"
        )
    else:
        assertion = (
            "loaded['VALUE'] == 7 and loaded['ORIGIN'] == 'hidden'"
            if case.startswith("runpy_")
            else "loaded.VALUE == 7 and loaded.ORIGIN == 'hidden'"
        )
        bodies[declaration] += (
            "import pytest\n@pytest.mark.serial\ndef test_safe(): assert "
            + assertion
            + "\n"
        )
    for name, text in bodies.items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        if name != "tests/test_safe.py":
            policy["serial_shared_dependencies"][name] = checksum(path)
    policy["serial"][0]["dependencies"]["tests/test_safe.py"] = checksum(
        source / "tests/test_safe.py"
    )
    for name in [
        "hidden_support.py",
        "deep_support.py",
        "plugins/fixture_plugin.py",
        "tests/test_safe.py",
    ]:
        review_execution(policy, source, name, [])
    policy["serial_dependency_scope"]["dynamic_imports"].pop(declaration, None)

    def commit() -> str:
        (source / "ci/local-policy.json").write_text(json.dumps(policy))
        return freeze(source)

    def execute(label: str) -> None:
        marker.unlink(missing_ok=True)
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
                f"--basetemp={tmp_path / (label + '-tmp')}",
                f"--junitxml={tmp_path / (label + '.xml')}",
                "-o",
                f"cache_dir={tmp_path / (label + '-cache')}",
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
        (tmp_path / (label + ".log")).write_text(result.stdout + result.stderr)
        assert result.returncode == 0, result.stdout + result.stderr
        assert marker.read_text() == "hidden"

    base = commit()
    execute("unbound-pytest")
    unbound = contract.build(source, base, base, policy, known)
    assert unbound["status"] == "requalification-required", unbound["placements"][SAFE]
    assert unbound["placements"][SAFE]["closure_problems"]
    assert local_policy.serial_groups([SAFE], policy, source)[
        "globally-exclusive-serial"
    ] == [SAFE]
    with pytest.raises(ValueError, match="requalification-required"):
        contract.require(risk.make_plan(source, base, base), source)

    # This approval-shaped metadata belongs only to this disposable test repository.
    target = "plugins/fixture_plugin.py" if plugin else "hidden_support.py"
    policy["serial_dependency_scope"]["dynamic_imports"][declaration] = {
        "source_sha256": checksum(source / declaration),
        "targets": [target],
        "reason": "Synthetic finite dispatch fixture; no approval for repository source",
    }
    bound = commit()
    before = contract.build(source, bound, bound, policy, known)
    row = before["placements"][SAFE]
    assert before["status"] == "passed", row
    assert {target, "hidden_support.py", "deep_support.py"} <= set(row["dependencies"])
    assert not row["closure_problems"]
    # A literal-looking argument is not authority for the actual, shadowed loader.
    if not plugin:
        assert "declared_support.py" not in row["dependencies"]

    modified = "deep_support.py" if changed == "transitive" else target
    path = source / modified
    path.write_text(
        path.read_text() + "# changed while pytest correctness still passes\n"
    )
    after_head = commit()
    execute("changed-helper-pytest")
    after = contract.build(source, bound, after_head, policy, known)
    row = after["placements"][SAFE]
    assert after["status"] == "requalification-required"
    assert not any(
        problem.startswith(declaration + ":") for problem in row["closure_problems"]
    )  # The declaration's binding still matches; changed executable helpers do not.
    if modified in policy["serial_dependency_scope"]["dynamic_imports"]:
        assert (
            f"{modified}: exact-source execution review is stale"
            in row["closure_problems"]
        )
    assert any(item["path"] == modified for item in row["differences"])
    assert local_policy.serial_groups([SAFE], policy, source)[
        "globally-exclusive-serial"
    ] == [SAFE]
    with pytest.raises(ValueError, match="requalification-required"):
        contract.require(risk.make_plan(source, bound, after_head), source)

    entry = source / declaration
    entry.write_text(entry.read_text() + "# declaration source changed\n")
    changed_head = commit()
    stale = contract.build(source, bound, changed_head, policy, known)
    assert stale["placements"][SAFE]["closure_problems"]
    assert stale["status"] == "requalification-required"


def test_unrelated_reflective_ordinary_addition_preserves_placement(
    project: Any,
) -> None:
    source, base, policy, known = project
    other = source / "tests/test_unrelated.py"
    other.write_text(
        "def test_unrelated():\n    assert getattr('x', 'upper')() == 'X'\n"
    )
    updated = json.loads(json.dumps(known))
    updated["modules"]["tests/test_unrelated.py"] = {"test_unrelated": {"": []}}
    (source / "ci/test-inventory.json").write_text(json.dumps(updated))
    after = contract.build(source, base, freeze(source), policy, known)
    assert after["status"] == "passed"
    assert after["placements"][SAFE]["placement"] == "concurrency-safe-serial"


@pytest.mark.parametrize(
    "fault",
    [
        "stale-binding",
        "changed-declaration",
        "changed-target",
        "changed-transitive",
        "deleted-target",
        "deleted-transitive",
        "missing-target",
        "unreviewed-transitive",
    ],
)
def test_declared_binding_is_enforced_even_for_static_source(
    project: Any, tmp_path: Path, fault: str
) -> None:
    source, _, policy, known = project
    declaration = "tests/helper.py"
    for name, body in {
        "support/target.py": "from support.transitive import VALUE\n",
        "support/transitive.py": "VALUE = 1\n",
    }.items():
        path = source / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(body)
        policy["serial_shared_dependencies"][name] = checksum(path)
    review_execution(policy, source, "support/target.py", [])
    review_execution(policy, source, declaration, ["support/target.py"])
    binding = policy["serial_dependency_scope"]["dynamic_imports"][declaration]

    def commit() -> str:
        (source / "ci/local-policy.json").write_text(json.dumps(policy))
        return freeze(source)

    def execute(label: str) -> None:
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
                "-o",
                f"cache_dir={tmp_path / (label + '-cache')}",
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
        (tmp_path / (label + ".log")).write_text(result.stdout + result.stderr)
        assert result.returncode == 0, result.stdout + result.stderr

    base = commit()
    execute("before-binding-fault")
    assert not requires_review(ast.parse((source / declaration).read_bytes()))
    before = contract.build(source, base, base, policy, known)
    row = before["placements"][SAFE]
    assert before["status"] == "passed", row
    assert {"support/target.py", "support/transitive.py"} <= set(row["dependencies"])
    contract.require(risk.make_plan(source, base, base), source)

    if fault == "stale-binding":
        binding["source_sha256"] = "0" * 64
    elif fault == "missing-target":
        binding["targets"] = ["support/missing.py"]
    else:
        path = source / (
            declaration
            if fault == "changed-declaration"
            else "support/target.py"
            if fault.endswith("target")
            else "support/transitive.py"
        )
        if fault.startswith("deleted"):
            path.unlink()
        elif fault == "unreviewed-transitive":
            path.write_text("VALUE = 1\ndef opaque(): return unknown_api()\n")
        else:
            path.write_text(path.read_text() + "# changed reviewed closure\n")
    head = commit()
    execute("after-binding-fault")
    after = contract.build(source, base, head, policy, known)
    row = after["placements"][SAFE]
    assert after["status"] == "requalification-required", row
    assert row["placement"] == "globally-exclusive-serial"
    assert "support/target.py" in row["dependencies"] or fault == "missing-target"
    if fault in {"stale-binding", "changed-declaration"}:
        assert (
            f"{declaration}: exact-source execution review is stale"
            in row["closure_problems"]
        )
    elif fault in {"missing-target", "unreviewed-transitive"}:
        assert row["closure_problems"]
    else:
        assert row["differences"]
    assert local_policy.serial_groups([SAFE], policy, source)[
        "globally-exclusive-serial"
    ] == [SAFE]
    with pytest.raises(ValueError, match="requalification-required"):
        contract.require(risk.make_plan(source, base, head), source)
