"""Independent exact-command and immutable-artifact checks for named R gates."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any, cast

FIXTURES = {
    "exports": ("examples/export_r.py", "examples/r_interoperability.R"),
    "persistence": (
        "scripts/persistence_r_fixture.py",
        "examples/persistence_interoperability.R",
    ),
    "generation": (
        "scripts/generation_r_fixture.py",
        "examples/generation_interoperability.R",
    ),
    "source-preparation": (
        "scripts/source_preparation_r_fixture.py",
        "examples/source_preparation_interoperability.R",
    ),
    "execution": (
        "scripts/execution_r_fixture.py",
        "examples/execution_interoperability.R",
    ),
    "grouped-lifecycle": (
        "scripts/grouped_lifecycle_r_fixture.py",
        "examples/grouped_lifecycle_interoperability.R",
    ),
}


def commands(name: str, python: str, output: Path) -> list[list[str]]:
    if name == "cli-lifecycle":
        return [[python, "scripts/cli_lifecycle_r_gate.py", "--output", str(output)]]
    fixture, assertions = FIXTURES[name]
    phases: list[list[str]] = (
        [[]] if name == "exports" else [["--phase", "persist"], ["--phase", "export"]]
    )
    return [[python, fixture, "--output", str(output), *phase] for phase in phases] + [
        ["Rscript", "--vanilla", assertions, str(output)]
    ]


def validate(
    name: str, root: Path, receipt: dict[str, Any], identity: dict[str, Any]
) -> None:
    steps = receipt.get("steps", [])
    python = steps[0]["command"][0] if steps else ""
    if (
        not Path(python).is_absolute()
        or os.path.normcase(str(Path(python).resolve()))
        != identity["interpreter"]["executable"]
        or os.path.normcase(str(Path(python).parent.parent.resolve()))
        != identity["interpreter"]["prefix"]
    ):
        raise ValueError("R gate did not use its declared installed interpreter")
    expected = commands(name, python, root)
    if (
        receipt.get("schema_version") != 2
        or receipt.get("gate") != name
        or receipt.get("status") != "passed"
        or receipt.get("phase") != "complete"
        or receipt.get("provenance") != identity
        or receipt.get("source_verified") is not True
    ):
        raise ValueError("Incomplete or foreign R gate identity")
    if len(steps) != len(expected) or any(
        step.get("command") != command
        or type(step.get("exit_code")) is not int
        or step["exit_code"] != 0
        or step.get("processes", {}).get("cleanup_complete") is not True
        or step.get("processes", {}).get("command_exit_code") != 0
        or step.get("processes", {}).get("cancelled") is not False
        or step.get("processes", {}).get("leaked_descendants") is not False
        for step, command in zip(steps, expected, strict=True)
    ):
        raise ValueError("Missing, duplicated or failed R phases")
    raw_artifacts = receipt.get("artifacts")
    if not isinstance(raw_artifacts, dict) or not raw_artifacts:
        raise ValueError("Missing actual R artifacts")
    artifacts = cast(dict[str, str], raw_artifacts)
    actual = {
        p.relative_to(root).as_posix()
        for p in root.rglob("*")
        if p.is_file() and p.name != "receipt.json"
    }
    if set(artifacts) != actual:
        raise ValueError("Missing or unexpected R gate artifacts")
    for relative, checksum in artifacts.items():
        path = root / relative
        if (
            not path.resolve().is_relative_to(root.resolve())
            or path.is_symlink()
            or hashlib.sha256(path.read_bytes()).hexdigest() != checksum
        ):
            raise ValueError("R gate artifact changed")
