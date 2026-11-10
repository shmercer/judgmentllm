"""Deterministic change classification and fail-closed CI admission.

Hosted CI imports this module from the protected base checkout. Candidate edits
to the map, selector, workflow or floor cannot lower its plan.
"""

from __future__ import annotations

import ast
import difflib
import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any

PRIMARY = {"id": "linux-3.11", "os": "ubuntu-latest", "python": "3.11"}
REPRESENTATIVE = [
    PRIMARY,
    {"id": "windows-3.12", "os": "windows-latest", "python": "3.12"},
    {"id": "macos-3.11", "os": "macos-latest", "python": "3.11"},
]
COMPREHENSIVE = [
    *REPRESENTATIVE,
    {"id": "linux-3.12", "os": "ubuntu-latest", "python": "3.12"},
    {"id": "linux-3.15", "os": "ubuntu-latest", "python": "3.15"},
    {"id": "windows-3.11", "os": "windows-latest", "python": "3.11"},
    {"id": "macos-3.12", "os": "macos-latest", "python": "3.12"},
    {"id": "macos-3.15", "os": "macos-latest", "python": "3.15"},
]
R_GATES = [
    "exports",
    "persistence",
    "generation",
    "source-preparation",
    "execution",
    "grouped-lifecycle",
    "cli-lifecycle",
]
PROMPT_TESTS = [
    "tests/test_prompts.py",
    "tests/test_anthropic_transport.py",
    "tests/test_anthropic_response.py",
    "tests/test_v2_rendering.py",
    "tests/test_native_v2_compatibility.py",
    "tests/test_transport_rendering.py",
    "tests/test_native_transport_rendering.py",
]
SHARED_PROVIDER_FILES = {
    "src/judgmentllm/providers/" + name
    for name in (
        "__init__.py",
        "protocol.py",
        "ledger.py",
        "mock.py",
        "_assessment_schema.py",
        "evidence.py",
    )
}
SENSITIVE_PROVIDER = re.compile(
    r"auth|credential|secret|api_?key|headers|privacy|capture|retain|withhold|reasoning|"
    r"safe|saniti|logging|logfilter|ambient|authority|retry|journal|ledger|recover|sqlite|"
    r"ownership|uncertain|notsubmitted|not_submitted|acceptance|accepted|"
    r"protected_client|execution_client|\btoken\b|\bverify\b|tls|ssl|certificate",
    re.I,
)


def sensitive_provider_edit(before: str | None, after: str) -> bool:
    changed = "\n".join(
        line[2:]
        for line in difflib.ndiff((before or "").splitlines(), after.splitlines())
        if line.startswith(("+ ", "- "))
    )
    if SENSITIVE_PROVIDER.search(changed):
        return True

    # Also protect a credential/authority helper when only its predicate or
    # return value changes; the sensitive function name may be outside the diff.
    def boundaries(text: str) -> set[tuple[str, str]]:
        return {
            (node.name, ast.dump(node))
            for node in ast.walk(ast.parse(text))
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and SENSITIVE_PROVIDER.search(node.name)
        }

    try:
        return boundaries(before or "") != boundaries(after)
    except SyntaxError:
        return True


def git(source: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=source, text=True).strip()


def blob(source: Path, ref: str, filename: str) -> str | None:
    result = subprocess.run(
        ["git", "show", f"{ref}:{filename}"],
        cwd=source,
        text=True,
        capture_output=True,
        check=False,
    )
    return result.stdout if result.returncode == 0 else None


def wording_only(before: str, after: str) -> bool:
    class Strings(ast.NodeTransformer):
        def visit_Constant(self, node: ast.Constant) -> ast.AST:
            return (
                ast.copy_location(ast.Constant(value="<wording>"), node)
                if isinstance(node.value, str)
                else node
            )

    try:
        return ast.dump(Strings().visit(ast.parse(before))) == ast.dump(
            Strings().visit(ast.parse(after))
        )
    except SyntaxError:
        return False


def additive_prompt_tests(before: str, after: str) -> bool:
    try:
        old, new = ast.parse(before), ast.parse(after)

        def imports(tree: ast.Module) -> set[tuple[str, str, str | None]]:
            return {
                (node.module or "", alias.name, alias.asname)
                for node in tree.body
                if isinstance(node, ast.ImportFrom)
                for alias in node.names
            }

        old_imports, new_imports = imports(old), imports(new)
        if not old_imports <= new_imports:
            return False
        old_bindings = {alias or name for _, name, alias in old_imports} | {
            node.name
            for node in old.body
            if isinstance(node, (ast.FunctionDef, ast.ClassDef))
        }
        for module, name, alias in new_imports - old_imports:
            if (
                not (
                    module in {item[0] for item in old_imports} | {"hashlib", "typing"}
                    or module.startswith("judgmentllm.")
                )
                or (alias or name) in old_bindings
            ):
                return False
        # Additional from-imports support new regression assertions. Existing
        # statements/tests, including ordinary imports and fixtures, remain exact.
        remaining = [node for node in old.body if not isinstance(node, ast.ImportFrom)]
        for node in (node for node in new.body if not isinstance(node, ast.ImportFrom)):
            if remaining and ast.dump(node) == ast.dump(remaining[0]):
                remaining.pop(0)
            elif not isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef)
            ) or not node.name.startswith("test_"):
                return False
            elif any(
                not isinstance(decorator, ast.Call)
                or ast.unparse(decorator.func) != "pytest.mark.parametrize"
                for decorator in node.decorator_list
            ):
                return False
        return not remaining
    except SyntaxError:
        return False


def conditional_wording_only(before: str, after: str) -> bool:
    """Allow a literal instruction replacement under a version-only predicate.

    This recognizes PR #27 without permitting changed renderer logic or new
    calls. Unknown prompt control flow still escalates.
    """
    predicate_nodes = (
        ast.BoolOp,
        ast.And,
        ast.Or,
        ast.UnaryOp,
        ast.Not,
        ast.Compare,
        ast.Eq,
        ast.NotEq,
        ast.Name,
        ast.Load,
        ast.Attribute,
        ast.Constant,
    )

    class Wording(ast.NodeTransformer):
        def visit_ImportFrom(self, node: ast.ImportFrom) -> ast.AST:
            if node.module == "typing":
                node.names = [
                    alias
                    for alias in node.names
                    if (alias.name, alias.asname) != ("TYPE_CHECKING", None)
                ]
            return node

        def visit_If(self, node: ast.If) -> ast.AST | None:
            if (
                isinstance(node.test, ast.Name)
                and node.test.id == "TYPE_CHECKING"
                and not node.orelse
                and all(isinstance(child, ast.ImportFrom) for child in node.body)
            ):
                return None
            return self.generic_visit(node)

        def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
            if node.name != "output_instructions_v3":
                return node
            for arg in node.args.args:
                arg.annotation = None
            for parent in ast.walk(node):
                if not isinstance(parent, ast.If):
                    continue
                for child in list(parent.body):
                    if (
                        not isinstance(child, ast.If)
                        or child.orelse
                        or len(child.body) != 1
                    ):
                        continue
                    assignment = child.body[0]
                    if (
                        isinstance(assignment, ast.Assign)
                        and len(assignment.targets) == 1
                        and ast.unparse(assignment.targets[0]) == "instructions[0]"
                        and isinstance(assignment.value, ast.Constant)
                        and isinstance(assignment.value.value, str)
                        and all(
                            isinstance(part, predicate_nodes)
                            for part in ast.walk(child.test)
                        )
                        and all(
                            part.id in {"native", "output"}
                            for part in ast.walk(child.test)
                            if isinstance(part, ast.Name)
                        )
                        and all(
                            ast.unparse(part) == "output.commitment_version"
                            for part in ast.walk(child.test)
                            if isinstance(part, ast.Attribute)
                        )
                    ):
                        parent.body.remove(child)
            return node

    try:
        return ast.dump(Wording().visit(ast.parse(before))) == ast.dump(
            Wording().visit(ast.parse(after))
        )
    except SyntaxError:
        return False


def classify(filename: str, before: str | None, after: str | None) -> tuple[int, str]:
    """Boundary rules precede convenience rules; unknowns are always high risk."""
    if (
        filename.startswith(("ci/", ".github/", ".agents/", ".codex/"))
        or filename
        in {
            "AGENTS.md",
            "pyproject.toml",
            "uv.lock",
            ".coveragerc",
            "pytest.ini",
            "conftest.py",
        }
        or filename.endswith("/conftest.py")
        or filename
        in {
            "docs/ci-optimization.md",
            "docs/ci-test-audit.md",
            "docs/local-ci.md",
            "docs/implementation-plan.md",
        }
    ):
        return 3, "verification, dependency, selection or acceptance controls"
    if after is None:
        return 3, "deletion requires review of lost behavior or evidence"
    if filename.endswith(".md") and (
        filename.startswith("docs/") or filename in {"README.md", "CHANGELOG.md"}
    ):
        fence = chr(96) * 3

        def blocks(text: str) -> list[str]:
            return re.findall(
                rf"(?ms)^{fence}(?:python|bash|sh|powershell|r|R)\n(.*?)^{fence}", text
            )

        return (
            (2, "executable documentation changed")
            if blocks(before or "") != blocks(after)
            else (0, "non-executable documentation")
        )
    if (
        filename
        in {
            "src/judgmentllm/native_prompts.py",
            "src/judgmentllm/anthropic_prompts.py",
            "src/judgmentllm/prompts.py",
        }
        and before is not None
    ):
        if wording_only(before, after):
            return 1, "only string wording changed; Python control flow is identical"
        if filename.endswith("/native_prompts.py") and conditional_wording_only(
            before, after
        ):
            return (
                1,
                "literal instruction replacement by commitment version; renderer unchanged",
            )
    if filename.startswith("src/judgmentllm/templates/") and filename.endswith(
        (".txt", ".j2")
    ):

        def tokens(text: str) -> list[str]:
            return re.findall(r"\{[{%#].*?[%}#]\}", text, re.S)

        if before is not None and tokens(before) == tokens(after):
            return 1, "template wording; control blocks and placeholders unchanged"
        return 3, "template control flow or protected placeholders changed"
    if filename.startswith("tests/"):
        if filename in PROMPT_TESTS and additive_prompt_tests(before or "", after):
            return 1, "additive prompt regressions; existing tests unchanged"
        if (
            before is None
            and filename.startswith("tests/test_")
            and filename.endswith(".py")
        ):
            # New independent modules cannot remove existing case assertions.
            # Collection hooks and suite-wide overrides are selection controls.
            if not re.search(
                r"\b(?:pytest_plugins|pytestmark|pytest_[a-z_]*(?:collect|configure|session)[a-z_]*)\b",
                after,
            ):
                return 2, "new independent test module"
        return 3, "test assertions, collection or historical evidence changed"
    if filename.startswith("src/judgmentllm/providers/") and filename.endswith(".py"):
        if filename in SHARED_PROVIDER_FILES or filename.endswith("_cli.py"):
            return (
                3,
                "shared provider protocol, durable evidence, schema or authentication CLI",
            )
        if sensitive_provider_edit(before, after):
            return 3, "provider credential, privacy, authority or recovery behavior"
        if any(
            token in filename for token in ("auth", "evidence", "registry", "transport")
        ):
            return 3, "provider authority, privacy or transport boundary"
        if re.fullmatch(
            r"(?:openai|anthropic|google|gemini|vertex|together|ollama|typesafe)(?:_[a-z0-9_]+)?\.py",
            Path(filename).name,
        ):
            return 2, "independent provider adapter or response handling"
        return 3, "unknown provider-directory boundary"
    return 3, "shared behavior or unknown change; conservative escalation"


def make_plan(
    source: Path,
    base: str,
    head: str,
    *,
    minimum: int = 0,
) -> dict[str, Any]:
    if type(minimum) is not int or not 0 <= minimum <= 4:
        raise ValueError("Minimum risk must be 0 through 4")
    for ref in (base, head):
        if (
            re.fullmatch(r"[0-9a-f]{40}", ref) is None
            or git(source, "rev-parse", ref) != ref
        ):
            raise ValueError("Plans require full, existing commit identities")
    names = git(
        source, "diff", "--name-only", "--no-renames", base, head, "--"
    ).splitlines()
    changes: list[dict[str, Any]] = []
    for name in names:
        before, after = blob(source, base, name), blob(source, head, name)
        level, reason = classify(name, before, after)
        changes.append({"path": name, "level": level, "reason": reason})
    level = max([minimum, *(item["level"] for item in changes)])
    platform_sensitive = any(
        name.startswith(("src/", "scripts/"))
        and any(
            re.search(rf"\b{module}\b", blob(source, head, name) or "")
            for module in (
                "os",
                "pathlib",
                "subprocess",
                "sqlite3",
                "signal",
                "multiprocessing",
            )
        )
        for name in names
    )
    resource_sensitive = any(
        name.startswith(
            (
                "ci/",
                ".github/",
                "src/judgmentllm/execution/",
                "src/judgmentllm/preparation/",
                "src/judgmentllm/persistence/",
                "benchmarks/",
            )
        )
        or any(token in name for token in ("resource", "memory", "concurrency"))
        or name in {"pyproject.toml", "uv.lock"}
        or name in SHARED_PROVIDER_FILES
        for name in names
    )
    lanes = (
        COMPREHENSIVE
        if level == 4
        else (
            REPRESENTATIVE
            if level >= 3 or level == 2 and platform_sensitive
            else [PRIMARY]
        )
    )
    r_gates = (
        R_GATES
        if level >= 3
        else (
            ["exports", "execution"]
            if level == 2 and any(name.startswith("src/") for name in names)
            else []
        )
    )
    packaging = level == 4 or any(
        name in {"pyproject.toml", "uv.lock"}
        or name.startswith(("ci/", ".github/", "scripts/smoke_install"))
        for name in names
    )
    plan: dict[str, Any] = {
        "schema_version": 1,
        "base_sha": base,
        "source_sha": head,
        "risk": level,
        "requested_minimum": minimum,
        "changes": changes,
        "lanes": lanes,
        "targets": PROMPT_TESTS if level == 1 else ["tests"],
        "r_gates": r_gates,
        "resources": level == 4 or level >= 2 and resource_sensitive,
        "packaging": packaging,
        "coverage": level >= 2,
        "verification_map_sha256": hashlib.sha256(
            Path(__file__).with_name("verification-map.json").read_bytes()
        ).hexdigest(),
        "deferred": {
            "comprehensive_matrix": level < 4,
            "unrelated_resources": not (
                level == 4 or level >= 2 and resource_sensitive
            ),
            "packaging": not packaging,
        },
    }
    plan["plan_sha256"] = hashlib.sha256(
        json.dumps(plan, sort_keys=True).encode()
    ).hexdigest()
    return plan


def required_steps(plan: dict[str, Any], lane: str) -> set[str]:
    primary = lane == "linux-3.11"
    names: set[str] = {"docs"} if primary else set()
    if plan["risk"] >= 1 and primary:
        names.update(("ruff", "format", "pyright"))
    if plan["risk"] >= 1:
        names.add("tests")
    if plan["risk"] >= 2 and primary:
        names.add("coverage")
    if plan["risk"] >= 3 and primary:
        names.add("infrastructure")
    if plan["resources"] and (primary or plan["risk"] == 4):
        names.add("resources")
    if primary or plan["risk"] == 4:
        names.update(f"r-{name}" for name in plan["r_gates"])
    if plan["packaging"] and (primary or plan["risk"] == 4):
        names.update(("build", "metadata", "wheel", "sdist"))
    return names


def admit(
    plan: dict[str, Any], receipts: list[dict[str, Any]], run_id: str
) -> list[str]:
    """Missing/extra/duplicate/stale/failed lanes or required steps never pass."""
    errors: list[str] = []
    expected = {item["id"] for item in plan["lanes"]}
    actual = [item.get("lane") for item in receipts]
    if len(actual) != len(expected) or set(actual) != expected:
        errors.append("Missing, duplicate or unexpected lane receipts")
    for receipt in receipts:
        lane = receipt.get("lane")
        if not isinstance(lane, str) or lane not in expected:
            continue
        if (
            receipt.get("source_sha") != plan["source_sha"]
            or receipt.get("plan_sha256") != plan["plan_sha256"]
            or receipt.get("run_id") != run_id
            or receipt.get("status") != "passed"
            or receipt.get("source_verified") is not True
            or receipt.get("phase") != "complete"
        ):
            errors.append(f"Failed or incorrect source/plan/run identity: {lane}")
        steps = receipt.get("steps", [])
        names = [step.get("name") for step in steps]
        if len(names) != len(set(names)) or not required_steps(plan, lane) <= set(
            names
        ):
            errors.append(f"Missing or duplicated required steps: {lane}")
        if any(step.get("exit_code") != 0 for step in steps):
            errors.append(f"Failed required step: {lane}")
    return errors
