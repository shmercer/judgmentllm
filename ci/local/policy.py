"""Reviewed local scheduling and resource applicability; unknowns strengthen."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


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
            or path in {"pyproject.toml", "uv.lock", ".gitattributes"}
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
    return policy


def serial_groups(
    nodes: list[str], policy: dict[str, Any], source: Path
) -> dict[str, list[str]]:
    from selection import matches

    groups: dict[str, list[str]] = {
        key: []
        for key in (
            "concurrency-safe-serial",
            "host-exclusive-serial",
            "globally-exclusive-serial",
        )
    }
    for node in nodes:
        placement = "globally-exclusive-serial"
        matching = [
            item for item in policy["serial"] if matches(node, item["selector"])
        ]
        if len(matching) > 1:
            raise ValueError("Overlapping serial placements")
        if matching:
            item = matching[0]
            if all(
                (source / path).is_file()
                and (source / path).resolve().is_relative_to(source.resolve())
                and hashlib.sha256((source / path).read_bytes()).hexdigest() == expected
                for path, expected in {
                    **policy.get("serial_shared_dependencies", {}),
                    **item["dependencies"],
                }.items()
            ):
                placement = item["placement"]
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
