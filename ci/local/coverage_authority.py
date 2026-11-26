"""Coverage authority bytes; protection comes from the supervisor, never a hash.

No Coverage imports: planning and hosted admission run before its installation.
Local review/replay trusts its launcher and host; this is not same-UID isolation.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tomllib
from pathlib import Path, PurePosixPath
from typing import Any, cast

FLOOR = "ci/coverage-floor.json"
REGISTRY = "ci/coverage-exclusion-reconciliations.json"
EXCEPTIONS = "ci/changed-coverage-exceptions.json"


class AuthorityError(ValueError):
    """Invalid or unauthenticated coverage authority; never an ordinary loss."""


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def fingerprint(value: Any) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


def document(raw: bytes) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Duplicate evidence key: {key}")
            result[key] = value
        return result

    value = json.loads(raw, object_pairs_hook=unique)
    if not isinstance(value, dict):
        raise ValueError("Evidence must be an object")
    return cast(dict[str, Any], value)


def canonical_path(name: object) -> None:
    if (
        not isinstance(name, str)
        or not name
        or PurePosixPath(name).is_absolute()
        or ".." in PurePosixPath(name).parts
        or PurePosixPath(name).as_posix() != name
        or any(c in name for c in ("\\", ":", "\x00"))
        or any(ord(c) < 32 or ord(c) == 127 for c in name)
    ):
        raise ValueError(f"Noncanonical evidence path: {name!r}")


def disk_bytes(source: Path, name: str, *, executable: bool | None = None) -> bytes:
    canonical_path(name)
    root = source.resolve(strict=True)
    path = root
    for part in PurePosixPath(name).parts:
        path = path / part
        if path.is_symlink():
            raise ValueError(f"Symlink evidence path: {name}")
    if not path.resolve(strict=True).is_relative_to(root):
        raise ValueError(f"Escaping evidence path: {name}")
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError(f"Nonregular evidence file: {name}")
    with path.open("rb") as stream:
        mode = os.fstat(stream.fileno()).st_mode
        if not stat.S_ISREG(mode):
            raise ValueError(f"Nonregular evidence file: {name}")
        if executable is not None and bool(mode & stat.S_IXUSR) != executable:
            raise ValueError(f"Unexpected executable mode: {name}")
        return stream.read()


def git(source: Path, *args: str) -> bytes:
    # The trusted launcher supplies PATH; candidate Git environment is discarded.
    executable = shutil.which("git")
    if executable is None:
        raise OSError("Git is required for authenticated coverage history")
    environment = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    environment["GIT_NO_LAZY_FETCH"] = "1"
    return subprocess.check_output(
        [executable, "--no-replace-objects", "--literal-pathspecs", *args],
        cwd=source,
        env=environment,
        stderr=subprocess.PIPE,
    )


def commit_tree(source: Path, ref: object) -> str:
    if not isinstance(ref, str) or re.fullmatch(r"[0-9a-f]{40}", ref) is None:
        raise ValueError("Coverage history requires a literal commit SHA")
    first = git_object(source, ref, "commit").splitlines()[0]
    if re.fullmatch(rb"tree [0-9a-f]{40}", first) is None:
        raise ValueError("Malformed coverage commit tree")
    tree = first[5:].decode("ascii")
    git_object(source, tree, "tree")
    return tree


def git_object(source: Path, oid: str, kind: str) -> bytes:
    if git(source, "cat-file", "-t", oid).strip() != kind.encode("ascii"):
        raise ValueError(f"Coverage history object is not a {kind}")
    raw = git(source, "cat-file", kind, oid)
    if git_object_sha(kind, raw) != oid:
        raise ValueError("Coverage Git object identity mismatch")
    return raw


def git_object_sha(kind: str, raw: bytes) -> str:
    checksum = hashlib.sha1(f"{kind} {len(raw)}\0".encode("ascii"))
    checksum.update(raw)
    return checksum.hexdigest()


def git_bytes(source: Path, ref: str, name: str) -> bytes | None:
    """None means a successful tree lookup proved absence, never a failed tool."""
    canonical_path(name)
    tree = commit_tree(source, ref)
    parts = PurePosixPath(name).parts
    for index, part in enumerate(parts):
        entries = git(source, "ls-tree", "-z", tree, "--", part)
        if not entries:
            return None
        if not entries.endswith(b"\0"):
            raise ValueError("Truncated coverage tree lookup")
        records = entries.rstrip(b"\0").split(b"\0")
        if len(records) != 1:
            raise ValueError("Ambiguous coverage blob lookup")
        metadata, actual = records[0].split(b"\t", 1)
        mode, kind, oid = metadata.split()
        final = index == len(parts) - 1
        allowed = (
            mode in {b"100644", b"100755"} and kind == b"blob"
            if final
            else mode == b"040000" and kind == b"tree"
        )
        if actual != part.encode("utf-8") or not allowed:
            raise ValueError(f"Coverage path has a nonregular component: {name}")
        if re.fullmatch(rb"[0-9a-f]{40}", oid) is None:
            raise ValueError("Malformed coverage tree object")
        tree = oid.decode("ascii")
        if not final:
            git_object(source, tree, "tree")
    return git_object(source, tree, "blob")


def integer_list(value: Any, *, arcs: bool = False) -> None:
    if not isinstance(value, list):
        raise ValueError("Coverage evidence must contain lists")
    values = cast(list[Any], value)
    items: list[Any]
    if arcs:
        if any(
            not isinstance(arc, list)
            or len(cast(list[Any], arc)) != 2
            or any(type(end) is not int or end == 0 for end in cast(list[Any], arc))
            for arc in values
        ):
            raise ValueError("Coverage arcs require two strict signed integers")
        items = [tuple(arc) for arc in values]
    else:
        if any(type(line) is not int or line < 1 for line in values):
            raise ValueError("Coverage lines require strict positive integers")
        items = values
    if items != sorted(set(items)):
        raise ValueError("Coverage evidence must be sorted and unique")


def registry(raw: bytes, floor: bytes, lock: bytes) -> dict[str, Any]:
    value = document(raw)
    if (
        set(value) != {"schema_version", "coverage_floor_sha256", "parser", "entries"}
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or value["coverage_floor_sha256"] != sha256(floor)
        or value["parser"]
        != {"implementation": "cpython", "python": "3.11", "coverage": "7.17.0"}
        or not isinstance(value["entries"], list)
    ):
        raise ValueError("Invalid coverage reconciliation registry/context")
    pins = [
        p["version"]
        for p in tomllib.loads(lock.decode("utf-8"))["package"]
        if p["name"] == "coverage"
    ]
    if pins != [value["parser"]["coverage"]]:
        raise ValueError("Reconciliation parser differs from the protected lock")
    historical = document(floor)["files"]
    seen: set[tuple[str, str]] = set()
    for raw_entry in cast(list[Any], value["entries"]):
        if not isinstance(raw_entry, dict):
            raise ValueError("Malformed exclusion reconciliation entry")
        entry = cast(dict[str, Any], raw_entry)
        if set(entry) != {
            "path",
            "candidate_source_sha256",
            "historical_source_sha256",
            "added_excluded_lines",
            "removed_excluded_lines",
            "reason",
        }:
            raise ValueError("Malformed exclusion reconciliation entry")
        name = entry["path"]
        canonical_path(name)
        if not name.startswith("src/judgmentllm/") or not name.endswith(".py"):
            raise ValueError("Invalid reconciliation path")
        for key in ("candidate_source_sha256", "historical_source_sha256"):
            token = entry[key]
            if key == "historical_source_sha256" and token is None:
                continue
            if (
                not isinstance(token, str)
                or re.fullmatch(r"[0-9a-f]{64}", token) is None
            ):
                raise ValueError("Invalid reconciliation source digest")
        identity = (name, entry["candidate_source_sha256"])
        if identity in seen:
            raise ValueError("Duplicate reconciliation path/source digest")
        seen.add(identity)
        for key in ("added_excluded_lines", "removed_excluded_lines"):
            integer_list(entry[key])
        expected = historical.get(name)
        if entry["historical_source_sha256"] != (
            expected["source_sha256"] if expected else None
        ):
            raise ValueError("Reconciliation history differs from the protected floor")
        added, removed = entry["added_excluded_lines"], entry["removed_excluded_lines"]
        if (
            not (added or removed)
            or set(added) & set(removed)
            or not isinstance(entry["reason"], str)
            or not entry["reason"].strip()
        ):
            raise ValueError("Invalid reconciliation delta/reason")
    return value


def exception_entries(raw: bytes) -> list[dict[str, Any]]:
    value = document(raw)
    if (
        set(value) != {"schema_version", "entries"}
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
        or not isinstance(value["entries"], list)
    ):
        raise ValueError("Invalid changed coverage exception registry")
    entries: list[dict[str, Any]] = []
    seen: set[tuple[str, str, int | tuple[int, ...]]] = set()
    for raw_entry in cast(list[Any], value["entries"]):
        if not isinstance(raw_entry, dict):
            raise ValueError("Malformed changed coverage exception entry")
        entry = cast(dict[str, Any], raw_entry)
        if set(entry) != {
            "scope_sha256",
            "path",
            "lines",
            "arcs",
            "classification",
            "justification",
            "test_nodeids",
        }:
            raise ValueError("Malformed changed coverage exception entry")
        scope, name = entry["scope_sha256"], entry["path"]
        canonical_path(name)
        if (
            not name.startswith("src/judgmentllm/")
            or not name.endswith(".py")
            or any(char in name for char in "*?[]")
            or not isinstance(scope, str)
            or re.fullmatch(r"[0-9a-f]{64}", scope) is None
        ):
            raise ValueError("Invalid changed coverage exception path or scope")
        lines, arcs = entry["lines"], entry["arcs"]
        integer_list(lines)
        integer_list(arcs, arcs=True)
        if (
            not (lines or arcs)
            or entry["classification"] not in ("unreachable", "redundant")
            or not isinstance(entry["justification"], str)
            or not entry["justification"].strip()
        ):
            raise ValueError(
                "Invalid changed coverage exception coordinates or justification"
            )
        identities = {(scope, name, item) for item in [*lines, *map(tuple, arcs)]}
        if seen & identities:
            raise ValueError("Overlapping changed coverage exception coordinates")
        seen.update(identities)
        raw_nodeids = entry["test_nodeids"]
        if not isinstance(raw_nodeids, list) or not raw_nodeids:
            raise ValueError("Changed coverage exception requires exact proof tests")
        nodeids = cast(list[Any], raw_nodeids)
        for nodeid in nodeids:
            if not isinstance(nodeid, str):
                raise ValueError("Invalid changed coverage proof node ID")
            path, _, test = nodeid.partition("::")
            canonical_path(path)
            if (
                not path.startswith("tests/")
                or not path.endswith(".py")
                or not test.strip()
                or any(char in path for char in "[]")
                or any(char in nodeid for char in "*?")
                or any(ord(char) < 32 or ord(char) == 127 for char in nodeid)
            ):
                raise ValueError("Invalid changed coverage proof node ID")
        if nodeids != sorted(set(nodeids)):
            raise ValueError(
                "Changed coverage proof node IDs must be sorted and unique"
            )
        entries.append(entry)
    return entries


def describe(source: Path, base: str) -> dict[str, Any] | None:
    """Describe bytes at the supervisor's base; this does not prove protection."""
    try:
        raw = git_bytes(source, base, REGISTRY)
        exceptions = git_bytes(source, base, EXCEPTIONS)
        if raw is None:
            if exceptions is not None:
                raise ValueError(
                    "Changed coverage exceptions require protected coverage authority"
                )
            return None
        floor = git_bytes(source, base, FLOOR)
        lock = git_bytes(source, base, "uv.lock")
        if floor is None or lock is None:
            raise ValueError("Missing protected coverage floor/lock")
        value = registry(raw, floor, lock)
        descriptor = {
            "base_sha": base,
            "base_tree_sha": commit_tree(source, base),
            "floor_sha256": sha256(floor),
            "registry_sha256": sha256(raw),
            "parser": value["parser"],
        }
        if exceptions is not None:
            exception_entries(exceptions)
            descriptor["exceptions_sha256"] = sha256(exceptions)
        return descriptor
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        IndexError,
        AttributeError,
        subprocess.SubprocessError,
    ) as error:
        raise AuthorityError(
            f"Invalid protected coverage authority: {error}"
        ) from error


def load(
    source: Path, base: str | None, descriptor: dict[str, Any]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if base is None or descriptor != describe(source, base):
        raise AuthorityError(
            "Coverage authority differs from the independently supplied base"
        )
    floor = git_bytes(source, base, FLOOR)
    raw = git_bytes(source, base, REGISTRY)
    if floor is None or raw is None:
        raise AuthorityError("Advertised coverage authority is unavailable")
    if (
        sha256(floor) != descriptor["floor_sha256"]
        or sha256(raw) != descriptor["registry_sha256"]
    ):
        raise AuthorityError("Coverage authority changed during reading")
    value = document(raw)
    return document(floor), value["entries"]


def tree_records(source: Path, tree: str) -> list[bytes]:
    manifest = git(source, "ls-tree", "-r", "-z", "--full-tree", tree)
    records = manifest.split(b"\0")
    if records.pop():
        raise AuthorityError("Truncated changed coverage exception source manifest")
    return records


def exception_scope(source: Path, tree: str, descriptor: dict[str, Any]) -> str:
    """Bind tracked paths, modes and objects, omitting only the approval file."""
    included: list[bytes] = []
    for record in tree_records(source, tree):
        metadata, path = record.split(b"\t", 1)
        if path == EXCEPTIONS.encode():
            if not metadata.startswith((b"100644 blob ", b"100755 blob ")):
                raise AuthorityError(
                    "Nonregular changed coverage exception registry path"
                )
        else:
            included.append(record)
    return fingerprint(
        {
            "source_manifest_sha256": sha256(b"\0".join(included)),
            "floor_sha256": descriptor["floor_sha256"],
            "registry_sha256": descriptor["registry_sha256"],
            "parser": descriptor["parser"],
        }
    )


def checked_exceptions(
    source: Path, base: str, tree: str, descriptor: dict[str, Any]
) -> tuple[list[dict[str, Any]], str | None]:
    raw = git_bytes(source, base, EXCEPTIONS)
    if raw is None or sha256(raw) != descriptor["exceptions_sha256"]:
        raise AuthorityError("Advertised changed coverage exception registry differs")
    entries = exception_entries(raw)
    if not entries:
        return entries, None
    scope = exception_scope(source, tree, descriptor)
    if any(entry["scope_sha256"] == scope for entry in entries):
        # Git index flags and clean filters can hide differences in working bytes.
        for record in tree_records(source, tree):
            metadata, path = record.split(b"\t", 1)
            mode, kind, oid = metadata.split()
            name = path.decode("utf-8")
            if kind != b"blob" or mode not in {b"100644", b"100755"}:
                raise AuthorityError(
                    f"Reviewed checkout requires a regular file: {name!r}"
                )
            mismatch = f"Reviewed checkout differs from committed source: {name!r}"
            try:
                content = disk_bytes(source, name, executable=mode == b"100755")
            except (OSError, ValueError) as error:
                raise AuthorityError(mismatch) from error
            if git_object_sha("blob", content).encode("ascii") != oid:
                raise AuthorityError(mismatch)
    return entries, scope


def exception_proof_enabled(
    plan: dict[str, Any] | None, scope: str, lane: str, partition: str
) -> bool:
    return (
        plan is not None
        and plan.get("schema_version", 1) >= 2
        and plan.get("coverage_mode") == "historical"
        and scope == "local"
        and lane == "linux-3.11"
        and partition == "all"
        and "exceptions_sha256" in (plan.get("coverage_policy") or {})
    )


def admission_identity(base: str | None, head: str | None) -> tuple[str, str]:
    """Bind PR admission to its event; local inputs belong to the trusted reviewer.

    A standalone invocation is only a claim, not evidence of branch protection.
    """
    if (
        os.environ.get("GITHUB_ACTIONS") == "true"
        and os.environ.get("GITHUB_EVENT_NAME") == "pull_request_target"
    ):
        event = document(Path(os.environ["GITHUB_EVENT_PATH"]).read_bytes())
        expected_base = event["pull_request"]["base"]["sha"]
        expected_head = event["pull_request"]["head"]["sha"]
        if (
            base is not None
            and base != expected_base
            or head is not None
            and head != expected_head
        ):
            raise AuthorityError("Admission identity differs from the literal PR event")
        base, head = expected_base, expected_head
    if base is None or head is None:
        raise AuthorityError("Independent supervisor base/head identities are required")
    return base, head
