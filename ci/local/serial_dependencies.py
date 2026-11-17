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
    pending = [selector.split("::", 1)[0], *scope["support_modules"]]
    visited: set[str] = set()
    problems: list[str] = []
    modules = {
        path.removesuffix(".py").replace("/", "."): path
        for path in set(files) | (known_paths or set())
        if path.endswith(".py")
    }
    modules.update(
        (path.removeprefix("tests/").removesuffix(".py").replace("/", "."), path)
        for path in set(files) | (known_paths or set())
        if path.startswith("tests/") and path.endswith(".py")
    )
    while pending:
        path = pending.pop()
        if path in visited:
            continue
        visited.add(path)
        required.add(path)
        raw = files.get(path)
        if raw is None:
            problems.append(f"{path}: missing or nonregular helper")
            continue
        try:
            tree = ast.parse(raw)
        except (SyntaxError, UnicodeError):
            problems.append(f"{path}: helper imports cannot be assessed")
            continue
        dynamic: list[ast.Call] = []
        names: set[str] = set()
        importers = {"importlib.import_module", "__import__"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
                importers.update(
                    (alias.asname or alias.name) + ".import_module"
                    for alias in node.names
                    if alias.name == "importlib"
                )
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if node.level:
                    parent = Path(path).parent.parts
                    module = ".".join(
                        (*parent[: len(parent) - node.level + 1], module)
                    ).strip(".")
                names.add(module)
                names.update(module + "." + alias.name for alias in node.names)
                if module == "importlib":
                    importers.update(
                        alias.asname or alias.name
                        for alias in node.names
                        if alias.name == "import_module"
                    )
            elif isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "pytest_plugins"
                for target in node.targets
            ):
                try:
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
                        names.update(cast(list[str] | tuple[str, ...], plugins))
                    else:
                        problems.append(
                            f"{path}: pytest plugin dispatch needs independent review"
                        )
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and ast.unparse(node.func) in importers:
                if (
                    node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)
                ):
                    names.add(node.args[0].value)
                else:
                    dynamic.append(node)
        for name in names:
            targets = [modules.get(name)] + [
                modules.get(".".join(name.split(".")[:index]) + ".__init__")
                for index in range(1, len(name.split(".")) + 1)
            ]
            for target in targets:
                if target is None:
                    continue
                required.add(target)
                if target.startswith("tests/"):
                    pending.append(target)
        if dynamic:
            binding = scope["dynamic_imports"].get(path)
            if (
                binding is None
                or hashlib.sha256(raw).hexdigest() != binding["source_sha256"]
            ):
                problems.append(f"{path}: dynamic dispatch needs independent review")
            else:
                for target in binding["targets"]:
                    required.add(target)
                    if target.startswith("tests/"):
                        pending.append(target)
    return required, sorted(problems)
