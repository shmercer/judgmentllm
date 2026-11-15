"""Conservative structural admission for independent tests and generated data."""

from __future__ import annotations

import ast
import json
from typing import Any, cast


def loads(value: str) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError(f"Duplicate metadata key: {key}")
            result[key] = item
        return result

    result = json.loads(value, object_pairs_hook=unique)
    if not isinstance(result, dict):
        raise ValueError("Metadata must be an object")
    return cast(dict[str, Any], result)


def independent_test(node: ast.stmt, occupied: set[str]) -> bool:
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    if not node.name.startswith("test_") or node.name in occupied:
        return False
    # Calls in defaults, annotations or decorators execute during collection.
    expressions = [*node.args.defaults, *node.args.kw_defaults, node.returns]
    try:
        for default in (*node.args.defaults, *node.args.kw_defaults):
            if default is not None:
                ast.literal_eval(default)
    except (ValueError, TypeError, SyntaxError):
        return False
    expressions.extend(
        arg.annotation
        for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)
    )
    expressions.extend(
        argument.annotation
        for argument in (node.args.vararg, node.args.kwarg)
        if argument is not None
    )
    if any(
        isinstance(part, (ast.Call, ast.NamedExpr, ast.Lambda))
        for expr in expressions
        if expr is not None
        for part in ast.walk(expr)
    ):
        return False
    for decorator in node.decorator_list:
        if (
            not isinstance(decorator, ast.Call)
            or ast.unparse(decorator.func) != "pytest.mark.parametrize"
        ):
            return False
        if any(
            isinstance(part, (ast.Call, ast.NamedExpr, ast.Lambda))
            for expr in [*decorator.args, *(k.value for k in decorator.keywords)]
            for part in ast.walk(expr)
        ):
            return False
        try:
            for value in [
                *decorator.args,
                *(keyword.value for keyword in decorator.keywords),
            ]:
                if isinstance(ast.literal_eval(value), set):
                    return False
        except (ValueError, TypeError, SyntaxError):
            return False
    if any(
        isinstance(part, (ast.Global, ast.Nonlocal))
        or isinstance(part, ast.Name)
        and part.id in {"exec", "eval", "globals", "pytest_plugins", "pytestmark"}
        for part in ast.walk(node)
    ):
        return False
    occupied.add(node.name)
    return True


def additive_tests(before: str, after: str) -> bool:
    """Preserve the complete old module AST and its statement order."""
    try:
        old, new = (
            ast.parse(before, type_comments=True),
            ast.parse(after, type_comments=True),
        )
        if any(
            isinstance(part, ast.ImportFrom)
            and any(alias.name == "*" for alias in part.names)
            for part in ast.walk(old)
        ):
            return False
        occupied = {part.id for part in ast.walk(old) if isinstance(part, ast.Name)}
        occupied.update(
            part.name
            for part in ast.walk(old)
            if isinstance(part, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        )
        occupied.update(
            alias.asname or alias.name.split(".")[0]
            for part in ast.walk(old)
            if isinstance(part, (ast.Import, ast.ImportFrom))
            for alias in part.names
        )
        remaining = iter(old.body)
        expected = next(remaining, None)
        added = False
        for node in new.body:
            if expected is not None and ast.dump(node) == ast.dump(expected):
                expected = next(remaining, None)
            elif independent_test(node, occupied):
                added = True
            else:
                return False
        return expected is None and added
    except (SyntaxError, ValueError, TypeError):
        return False


def independent_module(after: str) -> bool:
    try:
        tree = ast.parse(after, type_comments=True)
        occupied: set[str] = set()
        found = False
        for index, node in enumerate(tree.body):
            if (
                isinstance(node, ast.Expr)
                and index == 0
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                continue
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    name = alias.asname or alias.name.split(".")[0]
                    if (
                        alias.name == "*"
                        or name in occupied
                        or name.startswith("pytest_")
                        or name == "pytestmark"
                    ):
                        return False
                    occupied.add(name)
            elif independent_test(node, occupied):
                found = True
            else:
                return False
        return found
    except (SyntaxError, ValueError, TypeError):
        return False


def metadata_additions(path: str, before: str | None, after: str | None) -> bool:
    """Only preserve-and-add data qualifies; collection proves the additions later."""
    if before is None or after is None:
        return False
    try:
        old, new = loads(before), loads(after)
        if old == new:
            return True
        if path == "ci/test-profiles.json":
            if set(old) != set(new) or any(
                old[key] != new[key] for key in old if key != "modules"
            ):
                return False
            return all(
                new["modules"].get(key) == value
                for key, value in old["modules"].items()
            ) and set(old["modules"]) < set(new["modules"])
        if (
            path != "ci/test-inventory.json"
            or set(old) != {"schema_version", "modules"}
            or set(new) != set(old)
            or type(new["schema_version"]) is not int
            or old["schema_version"] != new["schema_version"]
        ):
            return False
        return all(
            new["modules"].get(module, {}).get(function, {}).get(parameter) == markers
            for module, functions in old["modules"].items()
            for function, parameters in functions.items()
            for parameter, markers in parameters.items()
        )
    except (KeyError, TypeError, ValueError, AttributeError):
        return False
