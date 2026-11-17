"""Reviewed local scheduling and resource applicability; unknowns strengthen."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, cast


def read(root: Path) -> dict[str, Any]:
    path = root / "local-policy.json"
    if not path.is_file():
        return {
            "schema_version": 1,
            "serial": [],
            "resource_sources": {},
            "profiles": {"conservative": {"ordinary_workers": 12, "r_workers": 1}},
            "auto_default": "conservative",
        }
    from additions import loads

    policy = loads(path.read_text())
    if (
        type(policy.get("schema_version")) is not int
        or policy["schema_version"] != 1
        or policy.get("auto_default") not in {"conservative", "concurrent"}
    ):
        raise ValueError("Unsupported local policy or default")
    if set(policy["profiles"]) != {"conservative", "concurrent"} or any(
        type(p.get("ordinary_workers")) is not int
        or not 1 <= p["ordinary_workers"] <= 12
        or type(p.get("r_workers")) is not int
        or not 1 <= p["r_workers"] <= 2
        for p in policy["profiles"].values()
    ):
        raise ValueError("Invalid local process budget")
    seen: set[str] = set()
    shared = policy.get("serial_shared_dependencies", {})
    if any(
        not (
            path.startswith(
                ("src/", "tests/", "scripts/", "examples/", "benchmarks/", "ci/")
            )
            or path
            in {
                "pyproject.toml",
                "uv.lock",
                ".gitattributes",
                ".github/workflows/ci.yml",
            }
        )
        or ".." in Path(path).parts
        or len(value) != 64
        for path, value in shared.items()
    ):
        raise ValueError("Invalid shared serial dependency manifest")
    for item in policy["serial"]:
        if (
            item["selector"] in seen
            or item["placement"]
            not in {
                "concurrency-safe-serial",
                "host-exclusive-serial",
                "globally-exclusive-serial",
            }
            or not item["reason"]
            or not item["dependencies"]
            or any(
                not path.startswith(("src/", "tests/"))
                or ".." in Path(path).parts
                or len(value) != 64
                for path, value in item["dependencies"].items()
            )
        ):
            raise ValueError("Invalid serial placement")
        seen.add(item["selector"])
    for path, item in policy["resource_sources"].items():
        if (
            not path.startswith("src/")
            or ".." in Path(path).parts
            or not item["reason"]
            or not isinstance(item["selectors"], list)
            or len(item["selectors"]) != len(set(item["selectors"]))
        ):
            raise ValueError("Invalid resource dependency mapping")
    scope = policy.get("serial_dependency_scope")
    if scope is not None:
        if (
            set(scope)
            != {"roots", "controls", "support_modules", "dynamic_imports", "reason"}
            or not scope["reason"]
            or any(
                not isinstance(values, list)
                or not values
                or len(cast(list[object], values))
                != len({str(value) for value in cast(list[object], values)})
                or any(not safe_path(value) for value in cast(list[object], values))
                for values in (
                    scope["roots"],
                    scope["controls"],
                    scope["support_modules"],
                )
            )
        ):
            raise ValueError("Invalid serial dependency scope")
        for path, binding in scope["dynamic_imports"].items():
            if (
                not safe_path(path)
                or set(binding) != {"source_sha256", "targets", "reason"}
                or re.fullmatch(r"[0-9a-f]{64}", str(binding["source_sha256"])) is None
                or not binding["reason"]
                or not binding["targets"]
                or len(binding["targets"]) != len(set(binding["targets"]))
                or any(not safe_path(target) for target in binding["targets"])
            ):
                raise ValueError("Invalid serial dynamic dependency binding")
    if any(
        not isinstance(selector, str)
        or not selector.startswith("tests/")
        or selector.count("::") != 1
        or not isinstance(reason, str)
        or not reason.strip()
        for selector, reason in policy.get("isolation_reasons", {}).items()
    ):
        raise ValueError("Invalid isolation reason")
    return policy


def safe_path(path: object) -> bool:
    return (
        isinstance(path, str)
        and bool(path)
        and not Path(path).is_absolute()
        and ".." not in Path(path).parts
        and "\\" not in path
        and ":" not in path
    )


def serial_placement_report(
    nodes: list[str],
    policy: dict[str, Any],
    source: Path,
    *,
    ref: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Stale evidence changes placement safely, and explains how to requalify it."""
    from local.serial_dependencies import closure, snapshot
    from selection import digest, matches

    reviewed = any(
        matches(node, item["selector"]) for node in nodes for item in policy["serial"]
    )
    files = snapshot(source, ref) if reviewed else {}
    result: dict[str, dict[str, Any]] = {}
    for node in nodes:
        matching = [
            item for item in policy["serial"] if matches(node, item["selector"])
        ]
        if len(matching) > 1:
            raise ValueError("Overlapping serial placements")
        if not matching:
            result[node] = {
                "placement": "globally-exclusive-serial",
                "status": "unreviewed",
                "reason": "No reviewed placement; retain global isolation",
                "differences": [],
            }
            continue
        item = matching[0]
        expected = {
            **policy.get("serial_shared_dependencies", {}),
            **item["dependencies"],
        }
        scope = policy.get("serial_dependency_scope")
        required, problems = (
            closure(files, item["selector"], scope, known_paths=set(expected))
            if scope is not None
            else (set(expected), [])
        )
        # Retain removed paths inside the reviewed scope, too.
        if scope is not None:
            required.update(
                path
                for path in expected
                if path in scope["controls"]
                or any(path.startswith(root + "/") for root in scope["roots"])
                or path.startswith("ci/")
                and path.endswith(".py")
                and not Path(path).name.startswith("test_")
            )
        actual: dict[str, str | None] = {}
        for path in sorted(required):
            raw = files.get(path)
            actual[path] = hashlib.sha256(raw).hexdigest() if raw is not None else None
        differences = [
            {
                "path": path,
                "expected_sha256": expected.get(path),
                "actual_sha256": actual[path],
                "reason": "missing-or-nonregular"
                if actual[path] is None
                else "unrecorded-dependency"
                if path not in expected
                else "changed-dependency",
            }
            for path in sorted(required)
            if actual[path] is None or actual[path] != expected.get(path)
        ]
        stale = bool(differences or problems)
        result[node] = {
            "selector": item["selector"],
            "declared_placement": item["placement"],
            "placement": "globally-exclusive-serial" if stale else item["placement"],
            "status": "requalification-required" if stale else "passed",
            "reason": (
                "Stale isolation evidence: independently review the listed dependencies and requalify this exact source"
                if stale
                else item["reason"]
            ),
            "dependencies": actual,
            "dependency_sha256": digest(actual),
            "differences": differences,
            "closure_problems": problems,
        }
    return result


def serial_groups(
    nodes: list[str],
    policy: dict[str, Any],
    source: Path,
    *,
    placements: dict[str, str] | None = None,
) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {
        key: []
        for key in (
            "concurrency-safe-serial",
            "host-exclusive-serial",
            "globally-exclusive-serial",
        )
    }
    for node, row in serial_placement_report(nodes, policy, source).items():
        # Only a separately validated, source-bound scheduling contract supplies overrides.
        placement: str = row["placement"]
        if placements is not None and "selector" in row:
            placement = placements.get(row["selector"], placement)
        if placement not in groups:
            raise ValueError("Unknown reviewed serial placement")
        groups[placement].append(node)
    return groups


def resource_scope(
    paths: list[str], policy: dict[str, Any], *, full: bool, sensitive: bool
) -> tuple[list[str] | None, list[str]]:
    """None means complete resources; [] means no known affected measurements."""
    if full:
        return None, ["comprehensive resources"]
    selected: set[str] = set()
    reasons: list[str] = []
    for path in paths:
        entry = policy["resource_sources"].get(path)
        if entry is not None:
            selected.update(entry["selectors"])
            reasons.append(f"{path}: {entry['reason']}")
        elif path.startswith(("src/", "scripts/", "benchmarks/")):
            return None, [f"{path}: resource dependency unresolved; complete fallback"]
    if sensitive:
        return None, ["measurement/orchestration boundary: complete resource fallback"]
    return sorted(selected), reasons


def fingerprint(policy: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest()
