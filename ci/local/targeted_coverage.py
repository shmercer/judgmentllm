"""Strict changed-code coverage without a partial historical denominator."""

from __future__ import annotations

import ast
import json
import subprocess
import tempfile
import tomllib
from pathlib import Path
from typing import Any

from coverage import Coverage
from coverage_equivalence import validate_measurement
from source_lines import correspondence


def base_analysis(raw: bytes, config: Path) -> dict[str, Any]:
    """Analyze committed Python with coverage's parser; never import that code."""
    with tempfile.TemporaryDirectory(
        prefix="judgmentllm-coverage-analysis-"
    ) as temporary:
        root = Path(temporary)
        path = root / "previous.py"
        path.write_bytes(raw)
        coverage = Coverage(
            config_file=str(config), data_file=str(root / ".coverage"), branch=True
        )
        coverage.get_data().add_arcs({str(path): []})
        output = root / "analysis.json"
        coverage.json_report(morfs=[str(path)], outfile=str(output))
        return next(iter(json.loads(output.read_text())["files"].values()))


def compare(
    current: Path,
    source: Path,
    base: str,
    head: str,
    targets: list[str],
    baseline: Path,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema_version": 1,
        "mode": "targeted",
        "historical_coverage": "not-assessed",
        "passed": False,
        "files": [],
        "errors": [],
    }
    try:
        measured = json.loads(current.read_text())
        if len(targets) != len(set(targets)) or any(
            not path.startswith("src/judgmentllm/")
            or not path.endswith(".py")
            or ".." in Path(path).parts
            for path in targets
        ):
            raise ValueError("Invalid targeted production scope")
        if not targets:
            result.update(passed=True, disposition="no changed production code")
            return result
        validate_measurement(measured, source, expected_files=set(targets))
        protected = json.loads(baseline.read_text())
        settings = tomllib.loads((source / "pyproject.toml").read_text())
        if (
            settings["tool"]["coverage"] != protected["measurement_settings"]
            or protected.get(
                "pytest_addopts", settings["tool"]["pytest"]["ini_options"]["addopts"]
            )
            != settings["tool"]["pytest"]["ini_options"]["addopts"]
        ):
            raise ValueError("Protected coverage measurement settings changed")
        for filename in sorted(targets):
            raw = subprocess.check_output(
                ["git", "show", f"{head}:{filename}"], cwd=source
            )
            if raw != (source / filename).read_bytes():
                raise ValueError("Measured source does not match committed head")
            previous = subprocess.run(
                ["git", "show", f"{base}:{filename}"],
                cwd=source,
                capture_output=True,
                check=False,
            )
            if (
                previous.returncode
                and subprocess.run(
                    ["git", "cat-file", "-e", base + "^{commit}"],
                    cwd=source,
                    capture_output=True,
                    check=False,
                ).returncode
            ):
                raise ValueError("Protected comparison history is unavailable")
            old = previous.stdout if previous.returncode == 0 else b""
            before: dict[str, Any] = (
                base_analysis(old, source / "pyproject.toml")
                if old
                else {
                    "executed_branches": [],
                    "missing_branches": [],
                    "excluded_lines": [],
                }
            )
            mapping = correspondence(old.decode(), raw.decode())
            actual = measured["files"][filename]
            executable = set(actual["executed_lines"] + actual["missing_lines"])
            changed = executable - set(mapping.values())
            changed_text = set(range(1, len(raw.decode().splitlines()) + 1)) - set(
                mapping.values()
            )
            origins: set[int] = set()
            for node in ast.walk(ast.parse(raw)):
                predicate = (
                    getattr(node, "test", None)
                    or getattr(node, "iter", None)
                    or getattr(node, "subject", None)
                )
                if (
                    isinstance(node, (ast.stmt, ast.expr))
                    and isinstance(predicate, ast.expr)
                    and changed_text.intersection(
                        range(
                            predicate.lineno,
                            (predicate.end_lineno or predicate.lineno) + 1,
                        )
                    )
                ):
                    origins.add(node.lineno)

            def mapped(line: int, mapping: dict[int, int] = mapping) -> int | None:
                value = mapping.get(abs(line))
                return value * (-1 if line < 0 else 1) if value is not None else None

            previous_arcs = {
                (mapped(a), mapped(b))
                for a, b in before["executed_branches"] + before["missing_branches"]
            }
            possible = {
                tuple(arc)
                for arc in actual["executed_branches"] + actual["missing_branches"]
            }
            required = {
                arc
                for arc in possible
                if arc[0] in changed | origins or arc not in previous_arcs
            }
            missing_lines = sorted(changed.intersection(actual["missing_lines"]))
            missing_arcs = sorted(
                required.intersection(map(tuple, actual["missing_branches"]))
            )
            exclusions = sorted(
                mapping[line] for line in before["excluded_lines"] if line in mapping
            )
            if actual["excluded_lines"] != exclusions:
                raise ValueError(
                    f"Changed exclusions require Level 3 review: {filename}"
                )
            result["files"].append(
                {
                    "path": filename,
                    "changed_executable_lines": sorted(changed),
                    "required_branches": [list(arc) for arc in sorted(required)],
                    "uncovered_changed_lines": missing_lines,
                    "uncovered_changed_branches": [list(arc) for arc in missing_arcs],
                    "excluded_lines": exclusions,
                }
            )
        result["passed"] = all(
            not item["uncovered_changed_lines"]
            and not item["uncovered_changed_branches"]
            for item in result["files"]
        )
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        SyntaxError,
        subprocess.SubprocessError,
    ) as error:
        result["errors"].append(str(error))
    return result
