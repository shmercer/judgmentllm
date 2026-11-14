"""Protected test taxonomy, complete inventory, and deterministic lane selection.

Only data is read from receipts. Aggregation never imports candidate code.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ISOLATION = {"serial", "resource", "r_interop", "smoke"}
PROFILES = {"focused", "impacted", "platform-smoke", "platform-extended", "full"}


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def read(path: Path) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Duplicate selection metadata key: {key}")
            result[key] = value
        return result

    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique)


def inventory(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Group parameters under their function, without repeating full node IDs."""
    modules: dict[str, Any] = {}
    seen: set[str] = set()
    for item in sorted(items, key=lambda item: item["nodeid"]):
        node = item["nodeid"]
        if node in seen or not node.startswith("tests/test_") or "::" not in node:
            raise ValueError(f"Invalid or duplicate inventory case: {node}")
        seen.add(node)
        module, case = node.split("::", 1)
        function, bracket, parameter = case.partition("[")
        suffix = bracket + parameter
        markers = sorted(ISOLATION.intersection(item["markers"]))
        entry = modules.setdefault(module, {}).setdefault(function, {})
        entry[suffix] = markers
    return {"schema_version": 1, "modules": modules}


def inventory_items(record: dict[str, Any]) -> list[dict[str, Any]]:
    if record.get("schema_version") != 1 or type(record["schema_version"]) is not int:
        raise ValueError("Unsupported case inventory")
    return [
        {"nodeid": module + "::" + function + parameter, "markers": markers}
        for module, functions in record["modules"].items()
        for function, parameters in functions.items()
        for parameter, markers in parameters.items()
    ]


def write_inventory(path: Path, record: dict[str, Any]) -> None:
    # One generated line per function keeps parameter accounting reviewable.
    lines = ["{", '  "schema_version": 1,', '  "modules": {']
    modules = sorted(record["modules"].items())
    for index, (module, functions) in enumerate(modules):
        lines.append("    " + json.dumps(module) + ": {")
        entries = sorted(functions.items())
        for number, (function, params) in enumerate(entries):
            lines.append(
                "      "
                + json.dumps(function)
                + ": "
                + json.dumps(params, ensure_ascii=True, sort_keys=True)
                + ("," if number + 1 < len(entries) else "")
            )
        lines.append("    }" + ("," if index + 1 < len(modules) else ""))
    lines += ["  }", "}", ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def matches(node: str, selector: str) -> bool:
    return node == selector or node.startswith((selector + "::", selector + "["))


def validate_registry(policy: dict[str, Any], known: dict[str, Any]) -> None:
    if policy.get("schema_version") != 1:
        raise ValueError("Unsupported test taxonomy")
    nodes = {item["nodeid"] for item in inventory_items(known)}
    if set(policy["modules"]) != set(known["modules"]):
        raise ValueError("New test unclassified or stale module classification")
    for module, entry in policy["modules"].items():
        if (
            set(entry) != {"domain", "cost", "platforms", "reason"}
            or entry["domain"] not in policy["domains"]
            or entry["cost"] not in {"quick", "io", "process", "heavy", "resource"}
            or entry["platforms"] not in [[], ["linux", "win32", "darwin"]]
            or not entry["reason"]
        ):
            raise ValueError(f"Invalid test classification: {module}")
    for group in ("smoke", "extended", "essential"):
        selectors = policy[group]
        if len(selectors) != len(set(selectors)) or any(
            not any(matches(node, selector) for node in nodes) for selector in selectors
        ):
            raise ValueError(f"Duplicate or stale {group} selector")
    if not set(policy["mandatory_smoke"]) <= set(policy["smoke"]):
        raise ValueError("Mandatory cross-platform smoke/negative boundary removed")
    for selectors in policy["providers"].values():
        if not selectors or not set(selectors) <= set(policy["modules"]):
            raise ValueError("Unclassified provider impact mapping")


def manifest(
    items: list[dict[str, Any]],
    *,
    profile: str,
    impacted: list[str],
    include_r: bool,
    include_resources: bool,
    policy: dict[str, Any] | None = None,
    known: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if profile not in PROFILES:
        raise ValueError("Unknown lane test profile")
    policy = policy if policy is not None else read(HERE / "test-profiles.json")
    known = known if known is not None else read(HERE / "test-inventory.json")
    validate_registry(policy, known)
    actual = inventory(items)
    prior = {item["nodeid"]: item for item in inventory_items(known)}
    current = {item["nodeid"]: item for item in items}
    missing = sorted(set(prior) - set(current))
    if missing:
        raise ValueError(f"Required inventory cases removed/renamed: {missing[:8]}")
    for node, old in prior.items():
        if set(old["markers"]) != ISOLATION.intersection(current[node]["markers"]):
            raise ValueError(f"Isolation/marker semantics changed: {node}")
    records: list[dict[str, Any]] = []
    for node, item in sorted(current.items()):
        module = node.split("::", 1)[0]
        metadata = policy["modules"].get(module)
        markers = item["markers"]
        reason = (
            "complete profile" if profile == "full" else "platform-neutral or unrelated"
        )
        selected = profile == "full"
        if node not in prior or metadata is None:
            selected, reason = True, "new/unclassified case; mandatory on every lane"
        elif any(matches(node, selector) for selector in policy["smoke"]):
            selected, reason = True, "essential real platform smoke/negative boundary"
        elif any(matches(node, selector) for selector in impacted):
            selected, reason = True, "affected component or changed test module"
        elif profile in {"focused", "impacted"} and any(
            matches(node, selector) for selector in policy["essential"]
        ):
            selected, reason = True, "shared critical contract regression"
        elif profile == "platform-extended" and (
            metadata["platforms"] or any(matches(node, s) for s in policy["extended"])
        ):
            selected, reason = True, metadata["reason"]
        stage = (
            "resource"
            if "resource" in markers
            else "serial"
            if "serial" in markers
            else "ordinary"
        )
        disposition = "required" if selected else "not-required-for-this-lane"
        # All new R/resource cases retain their execution location and isolation.
        if "r_interop" in markers and not include_r:
            disposition, reason = "deferred-by-policy", "actual R executes locally"
        elif "resource" in markers and not include_resources:
            disposition, reason = (
                "deferred-by-policy",
                "resource measurements execute locally",
            )
        records.append(
            {
                "nodeid": node,
                "partition": stage,
                "markers": sorted(ISOLATION.intersection(markers)),
                "domain": metadata["domain"] if metadata else "unclassified",
                "cost": "resource"
                if "resource" in markers
                else metadata["cost"]
                if metadata
                else "unknown",
                "platforms": metadata["platforms"]
                if metadata
                else ["linux", "win32", "darwin"],
                "disposition": disposition,
                "reason": reason,
            }
        )
    result = {
        "schema_version": 1,
        "profile": profile,
        "impacted": sorted(impacted),
        "inventory_sha256": digest(actual),
        "policy_sha256": digest({"taxonomy": policy, "inventory": known}),
        "cases": records,
    }
    result["selection_sha256"] = digest(result)
    return result


def impact(paths: list[str], policy: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Only reviewed independent adapters get a narrow dependency closure."""
    selected: set[str] = set()
    reasons: list[str] = []
    for path in paths:
        if path in policy["modules"]:
            selected.add(path)
            reasons.append(f"{path}: entire changed test module")
        elif path.startswith("src/judgmentllm/providers/"):
            provider = Path(path).stem.split("_", 1)[0]
            if provider in policy["providers"]:
                selected.update(policy["providers"][provider])
                reasons.append(f"{path}: reviewed {provider} component closure")
    return sorted(selected), reasons


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--collection", type=Path, required=True)
    parser.add_argument("--write-inventory", type=Path)
    args = parser.parse_args()
    actual = inventory(read(args.collection)["collected"])
    if args.write_inventory:
        write_inventory(args.write_inventory, actual)
    else:
        known = read(HERE / "test-inventory.json")
        validate_registry(read(HERE / "test-profiles.json"), known)
        if actual != known:
            raise ValueError(
                "Collected inventory drift; reconcile new parameters/markers/cases explicitly"
            )
    print(
        f"Validated {len(inventory_items(actual))} cases in {len(actual['modules'])} modules"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
