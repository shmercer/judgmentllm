"""Mandatory local scheduling admission, independent of selected test depth."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any, cast


def policy_sha() -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=Path(__file__).resolve().parents[2],
        text=True,
        capture_output=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def build(
    source: Path,
    base: str,
    head: str,
    policy: dict[str, Any],
    known: dict[str, Any],
    *,
    policy_code_sha256: str | None = None,
) -> dict[str, Any]:
    from local.policy import fingerprint, serial_placement_report
    from selection import HERE, digest, inventory_items, matches

    if policy_code_sha256 is None:
        policy_code_sha256 = digest(
            {
                path.relative_to(HERE).as_posix(): hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
                for path in sorted(HERE.rglob("*.py"))
            }
        )

    modules = sorted(
        {entry["selector"].split("::", 1)[0] for entry in policy["serial"]}
    )
    present: set[str] = set()
    if modules:
        for ref in (base, head):
            present.update(
                subprocess.check_output(
                    ["git", "ls-tree", "-r", "--name-only", ref, "--", *modules],
                    cwd=source,
                    text=True,
                ).splitlines()
            )
    selectors = [
        entry["selector"]
        for entry in policy["serial"]
        if entry["selector"].split("::", 1)[0] in present
    ]
    rows = serial_placement_report(selectors, policy, source, ref=head)
    original = inventory_items(known)
    inventory_blob = subprocess.run(
        ["git", "show", f"{head}:ci/test-inventory.json"],
        cwd=source,
        capture_output=True,
        text=True,
        check=False,
    )
    if inventory_blob.returncode == 0:
        from additions import loads

        current = inventory_items(loads(inventory_blob.stdout))
    else:
        current = original
    for selector, row in rows.items():
        before = [item for item in original if matches(item["nodeid"], selector)]
        after = [item for item in current if matches(item["nodeid"], selector)]
        row["cases"] = after
        if (
            inventory_blob.returncode != 0
            or not before
            or before != after
            or any(
                "serial" not in item["markers"] or "resource" in item["markers"]
                for item in after
            )
        ):
            row["status"] = "requalification-required"
            row["placement"] = "globally-exclusive-serial"
            row["closure_problems"].append(
                f"{selector}: candidate inventory is missing or reviewed case IDs/isolation markers differ"
            )
    old = {
        item["nodeid"]: set(item["markers"]) & {"serial", "resource"}
        for item in original
    }
    reasons = dict(policy.get("isolation_reasons", {}))
    candidate_policy = subprocess.run(
        ["git", "show", f"{head}:ci/local-policy.json"],
        cwd=source,
        capture_output=True,
        text=True,
        check=False,
    )
    policy_change = None
    if candidate_policy.returncode == 0:
        from additions import loads

        candidate = loads(candidate_policy.stdout)
        if rows and {
            key: value for key, value in candidate.items() if key != "isolation_reasons"
        } != {
            key: value for key, value in policy.items() if key != "isolation_reasons"
        }:
            policy_change = "ci/local-policy.json: candidate scheduling controls differ from the executing policy; independent review required"
        additions = candidate.get("isolation_reasons", {})
        if not isinstance(additions, dict) or any(
            not isinstance(key, str)
            or not key.startswith("tests/")
            or key.count("::") != 1
            or not isinstance(value, str)
            or not value.strip()
            for key, value in cast(dict[object, object], additions).items()
        ):
            raise ValueError(
                "Candidate isolation reasons must be nonblank selector/text pairs"
            )
        # Candidate documentation can add reasons, never alter trusted placement authority.
        reasons = {**cast(dict[str, str], additions), **reasons}
    elif rows:
        policy_change = "ci/local-policy.json: candidate scheduling policy is missing"
        for row in rows.values():
            row["closure_problems"].append(policy_change)
            row["status"] = "requalification-required"
            row["placement"] = "globally-exclusive-serial"
    isolation_problems: list[str] = []
    for item in current:
        isolation = set(item["markers"]) & {"serial", "resource"}
        if (
            isolation
            and isolation != old.get(item["nodeid"], set())
            and not any(matches(item["nodeid"], selector) for selector in reasons)
        ):
            isolation_problems.append(
                f"{item['nodeid']}: new serial/resource placement needs an independently reviewed isolation reason"
            )
    result: dict[str, Any] = {
        "schema_version": 1,
        "policy_sha": policy_sha(),
        "policy_sha256": fingerprint(policy),
        "policy_code_sha256": policy_code_sha256,
        "source_sha": head,
        "placements": rows,
        "isolation_problems": isolation_problems,
        "policy_change": policy_change,
    }
    result["contract_sha256"] = digest(result)
    result["status"] = (
        "requalification-required"
        if isolation_problems
        or policy_change
        or any(row["status"] != "passed" for row in rows.values())
        else "passed"
    )
    result["review"] = None
    return result


def apply_review(
    contract: dict[str, Any], path: Path | None, expected_sha256: str | None
) -> dict[str, Any]:
    """The supervisor supplies the independent pin; a plan cannot approve itself."""
    from additions import loads

    if path is None and expected_sha256 is None:
        return contract
    if path is None or re.fullmatch(r"[0-9a-f]{64}", expected_sha256 or "") is None:
        raise ValueError(
            "Scheduling review requires an independently supplied path and SHA-256"
        )
    if path.is_symlink() or not path.is_file():
        raise ValueError("Scheduling review must be a regular file")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError(
            "Scheduling review differs from the supervisor's independent pin"
        )
    review = loads(raw.decode("utf-8"))
    fields = {
        "schema_version",
        "purpose",
        "status",
        "policy_sha",
        "source_sha",
        "contract_sha256",
        "decision",
        "placements",
        "reviewer",
        "review_reference",
        "reason",
        "evidence",
    }
    rows = contract["placements"]
    if (
        set(review) != fields
        or type(review["schema_version"]) is not int
        or review["schema_version"] != 1
        or review["purpose"] != "scheduling-only"
        or review["status"] != "approved"
        or review["decision"] not in {"requalified", "placement-change"}
        or any(
            review[key] != contract[key]
            for key in ("policy_sha", "source_sha", "contract_sha256")
        )
        or re.fullmatch(r"[0-9a-f]{40}", str(review["policy_sha"])) is None
        or not isinstance(review["placements"], dict)
        or set(cast(dict[str, Any], review["placements"])) != set(rows)
        or contract["isolation_problems"]
        or any(
            not isinstance(review[key], str) or not review[key].strip()
            for key in ("reviewer", "review_reference", "reason")
        )
        or not isinstance(review["evidence"], list)
        or not review["evidence"]
    ):
        raise ValueError(
            "Scheduling review is unapproved, incomplete or bound to another policy/source/contract"
        )
    rank = {
        "concurrency-safe-serial": 0,
        "host-exclusive-serial": 1,
        "globally-exclusive-serial": 2,
    }
    placements = cast(dict[str, Any], review["placements"])
    for selector, placement in placements.items():
        row = rows[selector]
        if (
            not isinstance(placement, str)
            or placement not in rank
            or review["decision"] == "requalified"
            and (
                placement != row["declared_placement"]
                or row["closure_problems"]
                or any(value is None for value in row["dependencies"].values())
            )
            or review["decision"] == "placement-change"
            and rank[placement] < max(1, rank[row["declared_placement"]])
        ):
            raise ValueError(
                "Scheduling review cannot preserve uncertain or authorize weaker isolation"
            )
    expected_cases = {item["nodeid"] for row in rows.values() for item in row["cases"]}
    for raw_item in cast(list[object], review["evidence"]):
        if not isinstance(raw_item, dict) or set(cast(dict[str, Any], raw_item)) != {
            "path",
            "sha256",
        }:
            raise ValueError("Scheduling evidence needs an exact artifact and checksum")
        item = cast(dict[str, Any], raw_item)
        if (
            not isinstance(item["path"], str)
            or re.fullmatch(r"[0-9a-f]{64}", str(item["sha256"])) is None
        ):
            raise ValueError("Scheduling evidence path or checksum is invalid")
        evidence = Path(item["path"])
        if (
            not evidence.is_absolute()
            or evidence.is_symlink()
            or not evidence.is_file()
        ):
            raise ValueError("Scheduling evidence must be an absolute regular file")
        content = evidence.read_bytes()
        if hashlib.sha256(content).hexdigest() != item["sha256"]:
            raise ValueError("Scheduling evidence bytes differ")
        record = loads(content.decode("utf-8"))
        case_ids = record.get("case_ids", [])
        if (
            record.get("source_sha") != contract["source_sha"]
            or record.get("status") != "passed"
            or record.get("cleanup_complete") is not True
            or review["decision"] == "requalified"
            and (
                record.get("isolation_passed") is not True
                or not isinstance(case_ids, list)
                or any(
                    not isinstance(case_id, str)
                    for case_id in cast(list[object], case_ids)
                )
                or len(cast(list[object], case_ids)) != len(expected_cases)
                or set(cast(list[str], case_ids)) != expected_cases
            )
        ):
            raise ValueError(
                "Scheduling evidence is failed, incomplete or from another source"
            )
    result = json.loads(json.dumps(contract))
    result["status"] = "passed"
    result["review"] = {
        "sha256": expected_sha256,
        "decision": review["decision"],
        "review_reference": review["review_reference"],
    }
    for selector, placement in placements.items():
        result["placements"][selector]["placement"] = placement
        result["placements"][selector]["status"] = "independently-reviewed"
    return result


def require(
    plan: dict[str, Any],
    source: Path,
    *,
    review: Path | None = None,
    review_sha256: str | None = None,
) -> dict[str, Any]:
    """Recompute all placements, even when the lane selects neither long test."""
    from local.policy import read
    from selection import HERE

    head = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=source, text=True
    ).strip()
    dirty = subprocess.check_output(
        ["git", "status", "--porcelain", "--untracked-files=normal"],
        cwd=source,
        text=True,
    ).strip()
    if head != plan["source_sha"] or dirty:
        raise ValueError(
            "Scheduling admission requires the clean exact candidate source"
        )
    actual = apply_review(
        build(
            source,
            plan["base_sha"],
            plan["source_sha"],
            read(HERE),
            plan["selection_policy"]["inventory"],
        ),
        review,
        review_sha256,
    )
    if actual != plan.get("scheduling_contract"):
        raise ValueError(
            "Scheduling contract differs from independent source/policy reconstruction"
        )
    if actual["status"] != "passed":
        pending = [
            selector
            for selector, row in actual["placements"].items()
            if row["status"] != "passed"
        ]
        raise ValueError(
            "requalification-required: "
            + ", ".join(
                pending
                + actual["isolation_problems"]
                + ([actual["policy_change"]] if actual["policy_change"] else [])
            )
            + "; runtime retains exclusive isolation; supply independently reviewed exact-source evidence"
        )
    return actual
