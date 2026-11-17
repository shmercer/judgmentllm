"""Conservative serial dependencies, separate from functional test selection."""

from __future__ import annotations

import ast
import hashlib
import io
import stat
import subprocess
import tarfile
from pathlib import Path
from typing import Any, cast


def snapshot(source: Path, ref: str | None = None) -> dict[str, bytes | None]:
    """Read committed bytes for planning; reject links in an executing checkout."""
    if ref is not None:
        raw = subprocess.check_output(
            ["git", "archive", "--format=tar", ref], cwd=source
        )
        result: dict[str, bytes | None] = {}
        with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
            for member in archive:
                if member.isdir():
                    continue
                stream = archive.extractfile(member) if member.isfile() else None
                result[member.name] = stream.read() if stream is not None else None
        return result
    try:
        listing = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=source,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError:
        listing = subprocess.CompletedProcess([], 1, stdout=b"")
    paths = (
        [source / value.decode() for value in listing.stdout.split(b"\0") if value]
        if listing.returncode == 0
        else [
            path
            for path in source.rglob("*")
            if not path.is_dir()
            and not {".git", ".venv", "__pycache__", ".pytest_cache", ".ruff_cache"}
            & set(path.relative_to(source).parts)
        ]
    )
    result = {}
    for path in paths:
        name = path.relative_to(source).as_posix()
        try:
            mode = path.lstat().st_mode
            result[name] = (
                path.read_bytes()
                if stat.S_ISREG(mode)
                and path.resolve().is_relative_to(source.resolve())
                and not any(
                    parent.is_symlink()
                    for parent in [path, *path.parents]
                    if parent.is_relative_to(source)
                )
                else None
            )
        except OSError:
            result[name] = None
    return result


def import_dispatch(tree: ast.Module) -> bool:
    """Recognize review boundaries, never infer Python loader bindings or targets."""
    modules = {"builtins", "importlib"}
    references = {
        "__builtins__",
        "__import__",
        "import_module",
        "exec_module",
        "load_module",
        "spec_from_file_location",
        "module_from_spec",
        "getattr",
        "setattr",
        "delattr",
        "globals",
        "locals",
        "vars",
        "eval",
        "exec",
        "compile",
        "__dict__",
        "__getattribute__",
        "__getattr__",
        "__setattr__",
        "__delattr__",
        "__globals__",
        "f_globals",
        "f_locals",
        "_getframe",
        "currentframe",
        "attrgetter",
        "methodcaller",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(
            alias.name.split(".", 1)[0] in modules for alias in node.names
        ):
            return True
        if isinstance(node, ast.ImportFrom) and (
            (node.module or "").split(".", 1)[0] in modules
            or any(
                alias.name == "*" or alias.name in references for alias in node.names
            )
        ):
            return True
        if isinstance(node, ast.Name) and node.id in modules | references:
            return True
        if isinstance(node, ast.Attribute) and node.attr in references:
            return True
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value in modules | references
        ):
            return True
    return False


def plugin_dispatch(tree: ast.Module) -> tuple[list[str], bool]:
    """Only one top-level literal declaration is automatic; every other use escapes."""
    allowed: set[ast.AST] = set()
    plugins: list[str] = []
    declarations = 0
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
        elif isinstance(node, ast.AnnAssign):
            target = node.target
        else:
            continue
        if not isinstance(target, ast.Name) or target.id != "pytest_plugins":
            continue
        declarations += 1
        try:
            value = ast.literal_eval(node.value) if node.value is not None else None
        except (ValueError, TypeError):
            continue
        values = [value] if isinstance(value, str) else value
        if isinstance(values, (tuple, list)) and all(
            isinstance(value, str)
            for value in cast(list[object] | tuple[object, ...], values)
        ):
            plugins.extend(cast(list[str] | tuple[str, ...], values))
            allowed.add(target)
    if declarations > 1:
        return plugins, True
    for node in ast.walk(tree):
        if node in allowed:
            continue
        for _, value in ast.iter_fields(node):
            if isinstance(value, str) and value == "pytest_plugins":
                return plugins, True
            if isinstance(value, list) and any(
                isinstance(item, str) and item == "pytest_plugins"
                for item in cast(list[object], value)
            ):
                return plugins, True
    return plugins, False


def closure(
    files: dict[str, bytes | None],
    selector: str,
    scope: dict[str, Any],
    *,
    known_paths: set[str] | None = None,
) -> tuple[set[str], list[str]]:
    """Follow local imports and explicitly reviewed dynamic helper dispatch."""
    required = {
        path
        for path in set(files) | (known_paths or set())
        if any(path.startswith(root + "/") for root in scope["roots"])
        or path.startswith("ci/")
        and path.endswith(".py")
        and not Path(path).name.startswith("test_")
    } | set(scope["controls"])
    paths = set(files) | (known_paths or set())
    test = selector.split("::", 1)[0]
    pending = [test, *scope["support_modules"]]
    for parent in [Path(test).parent, *Path(test).parent.parents]:
        conftest = (parent / "conftest.py").as_posix()
        if conftest in paths:
            pending.append(conftest)
    visited: set[str] = set()
    problems: list[str] = []

    def local_module(module: str, importer: str, *, relative: bool = False) -> bool:
        candidates = (
            [Path(module)]
            if relative
            else [
                root / module.replace(".", "/")
                for root in (Path(), Path("src"), Path("tests"), Path(importer).parent)
            ]
        )
        found = False
        for candidate in candidates:
            targets = [candidate / "__init__.py"]
            if candidate.name:
                targets.append(candidate.with_suffix(".py"))
            for target in targets:
                name = target.as_posix()
                if name in paths:
                    pending.append(name)
                    found = True
            for parent in candidate.parents:
                initializer = (parent / "__init__.py").as_posix()
                if initializer in paths:
                    pending.append(initializer)
            if any(path.startswith(candidate.as_posix() + "/") for path in paths):
                found = True  # Repository-local namespace package.
        return found

    while pending:
        path = pending.pop()
        if path in visited:
            continue
        visited.add(path)
        required.add(path)
        for parent in [Path(path).parent, *Path(path).parent.parents]:
            initializer = (parent / "__init__.py").as_posix()
            if initializer in paths and initializer not in visited:
                pending.append(initializer)
        raw = files.get(path)
        if raw is None:
            problems.append(f"{path}: missing or nonregular helper")
            continue
        try:
            tree = ast.parse(raw)
        except (SyntaxError, UnicodeError):
            problems.append(f"{path}: helper imports cannot be assessed")
            continue
        plugins, dynamic = plugin_dispatch(tree)
        dynamic = import_dispatch(tree) or dynamic
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    local_module(alias.name, path)
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if node.level:
                    parent = Path(path).parent
                    if node.level > len(parent.parts) + 1:
                        problems.append(
                            f"{path}: relative import leaves the repository"
                        )
                        continue
                    for _ in range(node.level - 1):
                        parent = parent.parent
                    module = (parent / module.replace(".", "/")).as_posix()
                if (
                    not local_module(module, path, relative=bool(node.level))
                    and node.level
                    and node.module
                ):
                    problems.append(
                        f"{path}: relative import target is missing: {module}"
                    )
                for alias in node.names:
                    local_module(
                        module + ("/" if node.level else ".") + alias.name,
                        path,
                        relative=bool(node.level),
                    )
        for name in plugins:
            if not local_module(name, path):
                dynamic = True
        if dynamic:
            binding = scope["dynamic_imports"].get(path)
            if (
                binding is None
                or hashlib.sha256(raw).hexdigest() != binding["source_sha256"]
            ):
                problems.append(f"{path}: dynamic dispatch needs independent review")
            else:
                for target in binding["targets"]:
                    pending.append(target)
    return required, sorted(problems)
