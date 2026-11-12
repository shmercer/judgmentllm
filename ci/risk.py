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

PRIMARY = {"id": "linux-3.11", "os": "ubuntu-24.04", "python": "3.11"}
REPRESENTATIVE = [
    PRIMARY,
    {"id": "windows-3.12", "os": "windows-latest", "python": "3.12"},
    {"id": "macos-3.11", "os": "macos-latest", "python": "3.11"},
]
COMPREHENSIVE = [
    *REPRESENTATIVE,
    {"id": "linux-3.12", "os": "ubuntu-24.04", "python": "3.12"},
    {"id": "linux-3.15", "os": "ubuntu-24.04", "python": "3.15"},
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


def instruction_wording(tree: ast.Module, filename: str) -> ast.Module:
    """Mask only designated instruction text, keeping predicates and data exact."""
    slots = {
        "src/judgmentllm/native_prompts.py": {
            "output_instructions_v3": {"instructions", "instructions[0]"},
        },
        "src/judgmentllm/prompts.py": {
            "_output_instructions_v2": {"prefix", "text"},
            "render_prompt": {"output_instructions"},
        },
    }.get(filename, {})

    def text(expression: ast.expr) -> ast.expr:
        if isinstance(expression, ast.Constant) and isinstance(expression.value, str):
            return ast.copy_location(ast.Constant(value="<wording>"), expression)
        if isinstance(expression, ast.BinOp) and isinstance(expression.op, ast.Add):
            expression.left = text(expression.left)
            expression.right = text(expression.right)
        elif isinstance(expression, ast.IfExp):
            # Selection values in the predicate are never instruction wording.
            expression.body = text(expression.body)
            expression.orelse = text(expression.orelse)
        elif isinstance(expression, ast.JoinedStr):
            expression.values = [
                text(part) if isinstance(part, ast.Constant) else part
                for part in expression.values
            ]
        # Calls, schema dictionaries, subscripts and formatted values stay exact.
        return expression

    for function in tree.body:
        if not isinstance(function, ast.FunctionDef) or function.name not in slots:
            continue
        for node in ast.walk(function):
            if isinstance(node, (ast.Assign, ast.AugAssign)):
                targets = (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
                if (
                    len(targets) == 1
                    and ast.unparse(targets[0]) in slots[function.name]
                ):
                    if ast.unparse(targets[0]) == "instructions" and isinstance(
                        node.value, ast.List
                    ):
                        node.value.elts = [text(part) for part in node.value.elts]
                    else:
                        node.value = text(node.value)
            elif (
                function.name == "output_instructions_v3"
                and isinstance(node, ast.Call)
                and ast.unparse(node.func) == "instructions.append"
                and len(node.args) == 1
                and not node.keywords
            ):
                node.args[0] = text(node.args[0])
    return tree


def wording_only(filename: str, before: str, after: str) -> bool:

    try:
        return ast.dump(instruction_wording(ast.parse(before), filename)) == ast.dump(
            instruction_wording(ast.parse(after), filename)
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
    """Recognize only PR #27's reviewed additive instruction replacement.

    This recognizes PR #27 without permitting changed renderer logic or new
    calls. Unknown prompt control flow still escalates.
    """
    predicate = ast.dump(
        ast.parse("not native and output.commitment_version == 5", mode="eval").body
    )
    rubric_guard = ast.dump(
        ast.parse("isinstance(contract, RubricOutputContract)", mode="eval").body
    )
    type_import = ast.dump(
        ast.parse(
            "if TYPE_CHECKING:\n    from judgmentllm.anthropic_transport import OutputCommitmentV5"
        ).body[0]
    )
    annotation = ast.dump(
        ast.parse("OutputCommitmentV3 | OutputCommitmentV5", mode="eval").body
    )

    class Wording(ast.NodeTransformer):
        replacements = 0

        def visit_ImportFrom(self, node: ast.ImportFrom) -> ast.AST:
            if node.module == "typing":
                node.names = [
                    alias
                    for alias in node.names
                    if (alias.name, alias.asname) != ("TYPE_CHECKING", None)
                ]
            return node

        def visit_If(self, node: ast.If) -> ast.AST | None:
            if ast.dump(node) == type_import:
                return None
            return self.generic_visit(node)

        def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
            if node.name != "output_instructions_v3":
                return node
            for arg in node.args.args:
                if (
                    arg.annotation is not None
                    and ast.dump(arg.annotation) == annotation
                ):
                    arg.annotation = ast.Name(id="OutputCommitmentV3", ctx=ast.Load())
            for parent in node.body:
                if (
                    not isinstance(parent, ast.If)
                    or ast.dump(parent.test) != rubric_guard
                ):
                    continue
                for child in parent.body[:1]:
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
                        and ast.dump(child.test) == predicate
                    ):
                        parent.body.remove(child)
                        self.replacements += 1
            return node

    try:
        old, new = Wording(), Wording()
        old_tree, new_tree = old.visit(ast.parse(before)), new.visit(ast.parse(after))
        return (
            old.replacements == 0
            and new.replacements == 1
            and wording_only(
                "src/judgmentllm/native_prompts.py",
                ast.unparse(old_tree),
                ast.unparse(new_tree),
            )
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
        if wording_only(filename, before, after):
            return (
                1,
                "designated instruction wording; predicates, schemas and sandbox unchanged",
            )
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
        "r_execution": "local",
        "resources": level == 4 or level >= 2 and resource_sensitive,
        "resource_execution": "local",
        "packaging": packaging,
        "coverage": level >= 2,
        "coverage_execution": "local",
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


def required_steps(
    plan: dict[str, Any], lane: str, *, execution_scope: str = "hosted"
) -> set[str]:
    if execution_scope not in {"local", "hosted"}:
        raise ValueError("Execution scope must be local or hosted")
    if any(
        plan.get(name) != "local"
        for name in ("resource_execution", "coverage_execution", "r_execution")
    ):
        raise ValueError("Plan must declare local resource, coverage and R policy")
    primary = lane == "linux-3.11"
    names: set[str] = {"docs"} if primary else set()
    if plan["risk"] >= 1 and primary:
        names.update(("ruff", "format", "pyright"))
    if plan["risk"] >= 1:
        names.add("tests")
    if execution_scope == "local" and plan["risk"] >= 2 and primary:
        names.add("coverage")
    if plan["risk"] >= 3 and primary:
        names.add("infrastructure")
    if (
        execution_scope == "local"
        and plan["resources"]
        and (primary or plan["risk"] == 4)
    ):
        names.add("resources")
    if execution_scope == "local" and (primary or plan["risk"] == 4):
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
            or receipt.get("execution_scope") != "hosted"
        ):
            errors.append(f"Failed or incorrect source/plan/run identity: {lane}")
        steps = receipt.get("steps", [])
        names = [step.get("name") for step in steps]
        if any(
            name in {"coverage", "resources"}
            or isinstance(name, str)
            and name.startswith("r-")
            for name in names
        ):
            errors.append(f"Local-only checks claimed in a hosted receipt: {lane}")
        if len(names) != len(set(names)) or not required_steps(plan, lane) <= set(
            names
        ):
            errors.append(f"Missing or duplicated required steps: {lane}")
        if any(step.get("exit_code") != 0 for step in steps):
            errors.append(f"Failed required step: {lane}")
    return errors
