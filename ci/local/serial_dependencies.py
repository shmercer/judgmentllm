"""Conservative serial dependencies, separate from functional test selection."""

from __future__ import annotations

import ast
import hashlib
import io
import stat
import subprocess
import tarfile
from importlib.util import resolve_name
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


def import_dispatch(tree: ast.Module) -> tuple[list[tuple[str, bool]], bool]:
    """Resolve literal calls and simple aliases; escaped/rebound loaders need review."""
    modules: set[str] = set()
    loaders = {"__import__": "builtin"}
    other_bindings: set[str] = set()
    writes: dict[str, list[ast.expr]] = {}
    parents = {
        child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if (
                    alias.name == "importlib"
                    or alias.name.startswith("importlib.")
                    and not alias.asname
                ):
                    modules.add(alias.asname or "importlib")
                else:
                    other_bindings.add(alias.asname or alias.name.split(".", 1)[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if (
                    not node.level
                    and node.module == "importlib"
                    and alias.name == "import_module"
                ):
                    loaders[alias.asname or alias.name] = "module"
                elif (
                    not node.level
                    and node.module == "builtins"
                    and alias.name == "__import__"
                ):
                    loaders[alias.asname or alias.name] = "builtin"
                else:
                    other_bindings.add(alias.asname or alias.name)
                    if alias.name in {"import_module", "__import__"}:
                        loaders.setdefault(alias.asname or alias.name, "unknown")
        elif isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, (ast.Name, ast.Attribute)):
                    writes.setdefault(ast.unparse(target), []).append(node.value)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            other_bindings.add(node.name)
        if isinstance(node, (ast.Name, ast.Attribute)) and isinstance(
            node.ctx, ast.Store
        ):
            parent = parents.get(node)
            if not (
                isinstance(parent, ast.Assign)
                and node in parent.targets
                or isinstance(parent, ast.AnnAssign)
                and parent.target is node
            ):
                other_bindings.add(ast.unparse(node))
    while True:
        previous = (set(modules), dict(loaders))
        loaders.update({name + ".import_module": "module" for name in modules})
        for target, values in writes.items():
            for value in values:
                name = ast.unparse(value)
                if name in modules:
                    modules.add(target)
                if name in loaders:
                    loaders.setdefault(target, loaders[name])
        if previous == (modules, loaders):
            break
    uncertain = {
        target
        for target, values in writes.items()
        if target in modules | set(loaders)
        and any(
            ast.unparse(value) not in (modules if target in modules else loaders)
            or target in loaders
            and loaders.get(ast.unparse(value)) != loaders[target]
            for value in values
        )
    }
    other_bindings.update(
        node.arg for node in ast.walk(tree) if isinstance(node, ast.arg)
    )
    uncertain.update(other_bindings & (modules | set(loaders)))
    while True:
        inherited = {
            target
            for target, values in writes.items()
            if target in modules | set(loaders)
            and any(ast.unparse(value) in uncertain for value in values)
        }
        if inherited <= uncertain:
            break
        uncertain.update(inherited)

    def literal(node: ast.expr | None) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        return None

    names: list[tuple[str, bool]] = []
    unresolved = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = ast.unparse(node.func)
            kind = loaders.get(name)
            if kind is not None:
                if kind == "unknown":
                    unresolved = True
                    continue
                if name in uncertain or any(
                    name.startswith(module + ".") for module in uncertain
                ):
                    unresolved = True
                kwargs = {item.arg: item.value for item in node.keywords}
                if (
                    any(isinstance(value, ast.Starred) for value in node.args)
                    or None in kwargs
                ):
                    unresolved = True
                    continue
                if kind == "builtin" and (len(node.args) != 1 or kwargs):
                    unresolved = True
                    continue
                if kind == "module" and (
                    len(node.args) > 2
                    or set(kwargs) - {"name", "package"}
                    or node.args
                    and "name" in kwargs
                    or len(node.args) > 1
                    and "package" in kwargs
                ):
                    unresolved = True
                    continue
                target = literal(node.args[0] if node.args else kwargs.get("name"))
                if not target:
                    unresolved = True
                    continue
                relative = target.startswith(".")
                if relative:
                    package = literal(
                        node.args[1] if len(node.args) > 1 else kwargs.get("package")
                    )
                    try:
                        if package is None:
                            raise ValueError("Unknown package")
                        target = resolve_name(target, package)
                    except (ValueError, ImportError):
                        unresolved = True
                        continue
                if not all(part.isidentifier() for part in target.split(".")):
                    unresolved = True
                    continue
                names.append((target, relative))
            elif isinstance(node.func, ast.Attribute) and node.func.attr in {
                "import_module",
                "exec_module",
                "spec_from_file_location",
                "module_from_spec",
            }:
                unresolved = True
        if isinstance(node, (ast.Name, ast.Attribute)) and isinstance(
            node.ctx, ast.Load
        ):
            name = ast.unparse(node)
            if name not in modules | set(loaders):
                if isinstance(node, ast.Attribute) and node.attr in {
                    "import_module",
                    "__import__",
                    "exec_module",
                    "spec_from_file_location",
                    "module_from_spec",
                }:
                    unresolved = True
                continue
            parent = parents.get(node)
            if isinstance(parent, ast.Call) and parent.func is node:
                continue
            if (
                name in modules
                and isinstance(parent, ast.Attribute)
                and parent.value is node
            ):
                continue
            if isinstance(parent, (ast.Assign, ast.AnnAssign)) and parent.value is node:
                targets = (
                    parent.targets
                    if isinstance(parent, ast.Assign)
                    else [parent.target]
                )
                if all(
                    ast.unparse(target) in modules | set(loaders) for target in targets
                ):
                    continue
            unresolved = True
    return names, unresolved


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
        dynamic_names, dynamic = import_dispatch(tree)
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
            elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)) and any(
                isinstance(target, ast.Name) and target.id == "pytest_plugins"
                for target in (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
            ):
                try:
                    if node.value is None or isinstance(node, ast.AugAssign):
                        raise ValueError("Unresolved plugin assignment")
                    plugins = ast.literal_eval(node.value)
                except (ValueError, TypeError):
                    problems.append(
                        f"{path}: pytest plugin dispatch needs independent review"
                    )
                else:
                    plugins = [plugins] if isinstance(plugins, str) else plugins
                    if isinstance(plugins, (list, tuple)) and all(
                        isinstance(plugin, str)
                        for plugin in cast(list[object] | tuple[object, ...], plugins)
                    ):
                        for name in cast(list[str] | tuple[str, ...], plugins):
                            if not local_module(name, path):
                                problems.append(
                                    f"{path}: plugin {name} is not repository-local; independent review required"
                                )
                    else:
                        problems.append(
                            f"{path}: pytest plugin dispatch needs independent review"
                        )
        for name, relative in dynamic_names:
            if not local_module(name, path) and (
                relative or local_module(name.split(".", 1)[0], path)
            ):
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
