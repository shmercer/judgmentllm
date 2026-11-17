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


def requires_review(tree: ast.Module) -> bool:
    """Accept a small declarative subset; do not interpret executable Python."""
    literals = (
        ast.Constant,
        ast.List,
        ast.Tuple,
        ast.Set,
        ast.Dict,
        ast.UnaryOp,
        ast.UAdd,
        ast.USub,
        ast.Load,
    )
    for node in tree.body:
        if isinstance(node, ast.Pass):
            continue
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue
        elif (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and not node.targets[0].id.startswith("__")
            and all(isinstance(value, literals) for value in ast.walk(node.value))
        ):
            continue
        else:
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


def type_stub(raw: bytes) -> bool:
    """Recognize only static reexports and overloads; never execute annotations."""
    try:
        tree = ast.parse(raw)
    except (SyntaxError, UnicodeError):
        return False
    imports: set[str] = set()
    functions: set[str] = set()
    overload: str | None = None
    annotation_nodes = (
        ast.Name,
        ast.Attribute,
        ast.Subscript,
        ast.Tuple,
        ast.BinOp,
        ast.BitOr,
        ast.Constant,
        ast.Load,
    )
    for node in tree.body:
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                name = alias.asname or (
                    alias.name.split(".", 1)[0]
                    if isinstance(node, ast.Import)
                    else alias.name
                )
                if (
                    alias.name == "*"
                    or name.startswith("__")
                    or name in imports | functions
                ):
                    return False
                imports.add(name)
                if (
                    isinstance(node, ast.ImportFrom)
                    and not node.level
                    and node.module == "typing"
                    and alias.name == "overload"
                ):
                    if overload is not None:
                        return False
                    overload = name
            continue
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return False
        if (
            overload is None
            or node.name.startswith("__")
            or node.name in imports
            or getattr(node, "type_params", ())
            or len(node.decorator_list) != 1
            or not isinstance(node.decorator_list[0], ast.Name)
            or node.decorator_list[0].id != overload
            or len(node.body) != 1
            or not isinstance(node.body[0], ast.Expr)
            or not isinstance(node.body[0].value, ast.Constant)
            or node.body[0].value.value is not Ellipsis
        ):
            return False
        functions.add(node.name)
        args = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
        if node.args.vararg is not None:
            args.append(node.args.vararg)
        if node.args.kwarg is not None:
            args.append(node.args.kwarg)
        annotations = [arg.annotation for arg in args if arg.annotation is not None]
        if node.returns is not None:
            annotations.append(node.returns)
        if any(
            not all(
                isinstance(value, annotation_nodes) for value in ast.walk(annotation)
            )
            for annotation in annotations
        ):
            return False
        for value in [
            *node.args.defaults,
            *(value for value in node.args.kw_defaults if value is not None),
        ]:
            if any(isinstance(part, ast.Call) for part in ast.walk(value)):
                return False
            try:
                ast.literal_eval(value)
            except (ValueError, TypeError):
                return False
    return True


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
    variants: dict[str, list[str]] = {}
    for path in sorted(paths):
        entry = Path(path)
        stem = entry.parent / entry.name.split(".", 1)[0]
        variants.setdefault(stem.as_posix().casefold(), []).append(path)
    test = selector.split("::", 1)[0]
    pending = [(path, True) for path in [test, *scope["support_modules"]]]
    for parent in [Path(test).parent, *Path(test).parent.parents]:
        conftest = (parent / "conftest.py").as_posix()
        if conftest in paths:
            pending.append((conftest, True))
    visited: set[tuple[str, bool]] = set()
    problems: list[str] = []

    def sibling_stub(path: str) -> bool:
        entry = Path(path)
        source = entry.with_suffix(".py").as_posix()
        stem = entry.with_suffix("")
        raw = files.get(path)
        return (
            entry.suffix == ".pyi"
            and entry.stem.isidentifier()
            and entry.stem != "__init__"
            and raw is not None
            and files.get(source) is not None
            and set(variants.get(stem.as_posix().casefold(), [])) == {path, source}
            and not variants.get(
                (entry.parent / "__pycache__" / entry.stem).as_posix().casefold()
            )
            and not any(
                value.casefold().startswith(stem.as_posix().casefold() + "/")
                for value in paths
            )
            and type_stub(raw)
        )

    def include(candidate: Path) -> bool:
        """Retain all layout alternatives; never guess package/loader precedence."""
        found = False
        for part in [candidate, *candidate.parents]:
            alternatives = [part / "__init__", part / "__pycache__" / "__init__"]
            if part.name:
                alternatives.extend([part, part.parent / "__pycache__" / part.name])
            for alternative in alternatives:
                names = variants.get(alternative.as_posix().casefold(), [])
                pending.extend((name, not sibling_stub(name)) for name in names)
                if part == candidate and names:
                    found = True
        return found

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
            found = include(candidate) or found
            if any(
                path.casefold().startswith(candidate.as_posix().casefold() + "/")
                for path in paths
            ):
                found = True  # Repository-local namespace package.
        return found

    while pending:
        path, execution = pending.pop()
        if (path, execution) in visited:
            continue
        visited.add((path, execution))
        required.add(path)
        include(Path(path).with_suffix(""))
        raw = files.get(path)
        binding = scope["dynamic_imports"].get(path)
        if binding is not None:
            if (
                raw is None
                or hashlib.sha256(raw).hexdigest() != binding["source_sha256"]
            ):
                problems.append(f"{path}: exact-source execution review is stale")
            pending.extend((target, True) for target in binding["targets"])
        if raw is None:
            problems.append(f"{path}: missing or nonregular helper")
            continue
        if not execution:
            continue
        if Path(path).suffix != ".py":
            problems.append(
                f"{path}: unsupported executable format; only .py source is supported"
            )
            continue
        try:
            tree = ast.parse(raw)
        except (SyntaxError, UnicodeError):
            problems.append(f"{path}: helper imports cannot be assessed")
            continue
        plugins, dynamic = plugin_dispatch(tree)
        dynamic = requires_review(tree) or dynamic
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
        if dynamic and binding is None:
            problems.append(f"{path}: executable module needs independent review")
    return required, sorted(problems)
