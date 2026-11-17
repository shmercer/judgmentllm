"""Sibling type data cannot erase a later demand to execute that file."""

from __future__ import annotations

import copy
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import risk
from local import policy as local_policy
from local import scheduling_contract as contract
from local.serial_dependencies import closure, snapshot, type_stub
from test_import_formats import execute
from test_risk import freeze
from test_scheduling_contract import SAFE, checksum, review_execution
from test_scheduling_contract import project as project

STUB = b"from absent_type_only_module import StaticType as StaticType\n"
OVERLOAD = b"""from typing import Literal, overload as ol
@ol
async def choose(value: Literal['one'], /, count: int = -1, *args: object,
                 option: tuple[str, ...] | None = None, **kwargs: object) -> str: ...
@ol
def choose(value: str, *, options: dict[str, int] = {'one': 1}) -> str | None: ...
"""


def pair(project: Any) -> tuple[dict[str, bytes | None], dict[str, Any]]:
    source, _, policy, _ = project
    files = snapshot(source)
    files.update(
        {
            "implicit.py": b"pytest_plugins = ['plugins']\n",
            "dispatch.py": b"VALUE = 1\n",
            "plugins.py": b"VALUE = 1\n",
            "plugins.pyi": STUB,
            "extra.py": b"VALUE = 2\n",
        }
    )
    scope = copy.deepcopy(policy["serial_dependency_scope"])
    scope["support_modules"] = ["implicit.py"]
    return files, scope


@pytest.mark.parametrize(
    "name", ["decisions_types", "gemini_types", "provider_diagnostics", "vertex_types"]
)
def test_current_stubs_are_only_fingerprinted_layout_data(
    project: Any, name: str
) -> None:
    path = Path(__file__).resolve().parents[2] / "src/judgmentllm" / (name + ".pyi")
    raw = path.read_bytes()
    assert type_stub(raw)
    files, scope = pair(project)
    files["plugins.pyi"] = raw
    required, problems = closure(files, SAFE, scope)
    assert not problems
    assert {"plugins.py", "plugins.pyi"} <= required
    assert not any(path.startswith("src/judgmentllm/gemini") for path in required)


@pytest.mark.parametrize(
    "raw", [STUB, OVERLOAD, b"'type documentation'\nimport typing\n"]
)
def test_supported_stub_grammar(raw: bytes) -> None:
    assert type_stub(raw)


def test_type_reexports_do_not_expand_the_execution_closure(project: Any) -> None:
    files, scope = pair(project)
    files["absent_type_only_module.py"] = b"def unknown(): return dynamic_loader()\n"
    required, problems = closure(files, SAFE, scope)
    assert not problems
    assert "plugins.pyi" in required
    assert "absent_type_only_module.py" not in required


@pytest.mark.parametrize(
    "raw",
    [
        b"\xff",
        b"def broken(",
        b"VALUE = 1",
        b"class Value: ...",
        b"if enabled:\n    from types import Value",
        b"from types import *",
        b"from types import Value as __getattr__",
        b"from types import Value as T\nfrom other import Value as T",
        b"from typing import overload\noverload = replacement",
        b"from other import overload\n@overload\ndef value() -> str: ...",
        b"from typing import overload\nfrom other import load as overload",
        b"from typing import overload as first, overload as second",
        b"from typing import overload\n@overload()\ndef value() -> str: ...",
        b"from typing import overload\n@overload\n@other\ndef value() -> str: ...",
        b"from typing import overload\n@overload\ndef value() -> str: return 'x'",
        b"from typing import overload\ndef value() -> str: ...",
        b"from typing import overload\n@overload\ndef __getattr__(name: str) -> object: ...",
        b"from typing import overload\n@overload\ndef overload() -> str: ...",
        b"from typing import overload\n@overload\ndef value(arg: lookup()) -> str: ...",
        b"from typing import overload\n@overload\ndef value(arg: lookup()): ...",
        b"from typing import overload\n@overload\ndef value() -> lookup(): ...",
        b"from typing import overload\n@overload\ndef value(arg: str = lookup()) -> str: ...",
        b"from typing import overload\n@overload\ndef value(*, arg: set = set()) -> str: ...",
        b"from typing import overload\n@overload\ndef value(arg: object = external) -> str: ...",
        b"from typing import overload\n@overload\ndef value(arg: str & int) -> str: ...",
        b"from typing import overload\n@overload\ndef value[T: lookup()]() -> T: ...",
        b"from typing import overload\n@overload\ndef value() -> str: ...\nfrom other import value",
    ],
)
def test_unsupported_stub_grammar_fails_closed(raw: bytes) -> None:
    assert not type_stub(raw)


@pytest.mark.parametrize(
    "fault",
    [
        "source-missing",
        "source-nonregular",
        "stub-nonregular",
        "source-case",
        "stub-case-collision",
        "namespace-collision",
        "cache",
        "bytecode",
        "extension",
        "unknown",
        "ancestor",
    ],
)
def test_pairing_never_suppresses_unsupported_layouts(project: Any, fault: str) -> None:
    files, scope = pair(project)
    if fault == "source-missing":
        files.pop("plugins.py")
    elif fault == "source-nonregular":
        files["plugins.py"] = None
    elif fault == "stub-nonregular":
        files["plugins.pyi"] = None
    elif fault == "source-case":
        files["PLUGINS.py"] = files.pop("plugins.py")
    elif fault == "stub-case-collision":
        files["PLUGINS.PYI"] = STUB
    elif fault == "namespace-collision":
        files["plugins/child.py"] = b"VALUE = 1\n"
    elif fault == "ancestor":
        files["implicit.py"] = b"pytest_plugins = ['parent.plugins']\n"
        files["parent/plugins.py"] = files.pop("plugins.py")
        files["parent/plugins.pyi"] = files.pop("plugins.pyi")
        files["parent/__init__.pyc"] = b"VALUE = 1\n"
    else:
        name = {
            "cache": "__pycache__/plugins.cpython-311.pyc",
            "bytecode": "plugins.pyc",
            "extension": "plugins.abi3.so",
            "unknown": "plugins.unrecognized",
        }[fault]
        files[name] = b"VALUE = 1\n"
    required, problems = closure(files, SAFE, scope)
    assert problems
    assert "parent/plugins.pyi" in required or "plugins.pyi" in required


@pytest.mark.parametrize("order", ["implicit-first", "execution-first"])
@pytest.mark.parametrize("demand", ["support", "binding"])
def test_explicit_stub_execution_rejects_both_discovery_orders(
    project: Any, order: str, demand: str
) -> None:
    files, scope = pair(project)
    explicit = "plugins.pyi" if demand == "support" else "dispatch.py"
    scope["support_modules"] = (
        [explicit, "implicit.py"]
        if order == "implicit-first"
        else ["implicit.py", explicit]
    )
    if demand == "binding":
        scope["dynamic_imports"]["dispatch.py"] = {
            "source_sha256": hashlib.sha256(files["dispatch.py"] or b"").hexdigest(),
            "targets": ["plugins.pyi"],
            "reason": "Synthetic execution demand, never a format exemption",
        }
    scope["dynamic_imports"]["plugins.pyi"] = {
        "source_sha256": hashlib.sha256(STUB).hexdigest(),
        "targets": ["extra.py"],
        "reason": "Synthetic matching binding must still traverse and reject execution",
    }
    required, problems = closure(files, SAFE, scope)
    assert {"plugins.py", "plugins.pyi", "extra.py"} <= required
    assert (
        "plugins.pyi: unsupported executable format; only .py source is supported"
        in problems
    )
    source, _, policy, known = project
    for name in ["implicit.py", "dispatch.py", "plugins.py", "plugins.pyi", "extra.py"]:
        (source / name).write_bytes(files[name] or b"")
        policy["serial_shared_dependencies"][name] = checksum(source / name)
    policy["serial_dependency_scope"] = scope
    (source / "ci/local-policy.json").write_text(json.dumps(policy))
    head = freeze(source)
    row = contract.build(source, head, head, policy, known)["placements"][SAFE]
    assert not row["differences"]
    assert row["closure_problems"] == [
        "plugins.pyi: unsupported executable format; only .py source is supported"
    ]
    for level in range(5):
        with pytest.raises(ValueError, match="requalification-required"):
            contract.require(risk.make_plan(source, head, head, minimum=level), source)


@pytest.mark.parametrize(
    "fault", ["matching", "stale", "missing-target", "self-target"]
)
def test_implicit_type_data_still_validates_binding_and_follows_targets(
    project: Any, fault: str
) -> None:
    files, scope = pair(project)
    target = (
        "absent.py"
        if fault == "missing-target"
        else "plugins.pyi"
        if fault == "self-target"
        else "extra.py"
    )
    scope["dynamic_imports"]["plugins.pyi"] = {
        "source_sha256": "0" * 64
        if fault == "stale"
        else hashlib.sha256(STUB).hexdigest(),
        "targets": [target],
        "reason": "Synthetic data-role binding remains an obligation",
    }
    required, problems = closure(files, SAFE, scope)
    assert target in required
    assert bool(problems) is (fault != "matching")
    if fault == "stale":
        assert "plugins.pyi: exact-source execution review is stale" in problems


@pytest.mark.parametrize("fault", ["stub-change", "source-change", "explicit-demand"])
def test_real_source_import_and_same_source_admission_at_all_levels(
    project: Any, tmp_path: Path, fault: str
) -> None:
    source, _, policy, known = project
    marker = tmp_path / "source-imported.txt"
    implementation = source / "plugins.py"
    implementation.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('source')\n"
    )
    stub = source / "plugins.pyi"
    stub.write_bytes(STUB)
    conftest = source / "tests/conftest.py"
    conftest.write_text("pytest_plugins = ['plugins']\n")
    for path in [implementation, stub, conftest]:
        policy["serial_shared_dependencies"][path.relative_to(source).as_posix()] = (
            checksum(path)
        )
    review_execution(policy, source, "plugins.py", [])

    def commit() -> str:
        (source / "ci/local-policy.json").write_text(json.dumps(policy))
        return freeze(source)

    base = commit()
    execute(source, tmp_path / "source-loader")
    assert marker.read_text() == "source"
    before = contract.build(source, base, base, policy, known)
    assert before["status"] == "passed", before["placements"][SAFE]
    assert before["placements"][SAFE]["dependencies"]["plugins.pyi"] == checksum(stub)
    for level in range(5):
        contract.require(risk.make_plan(source, base, base, minimum=level), source)
    if fault == "explicit-demand":
        policy["serial_dependency_scope"]["dynamic_imports"]["plugins.py"][
            "targets"
        ] = ["plugins.pyi"]
    else:
        target = stub if fault == "stub-change" else implementation
        target.write_bytes(target.read_bytes() + b"# source-bound bytes changed\n")
    head = commit()
    after = contract.build(source, base, head, policy, known)
    assert after["status"] == "requalification-required"
    assert after["placements"][SAFE]["placement"] == "globally-exclusive-serial"
    assert local_policy.serial_groups([SAFE], policy, source)[
        "globally-exclusive-serial"
    ] == [SAFE]
    for level in range(5):
        with pytest.raises(ValueError, match="requalification-required"):
            contract.require(risk.make_plan(source, head, head, minimum=level), source)
