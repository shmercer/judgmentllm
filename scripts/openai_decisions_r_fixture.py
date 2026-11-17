"""Installed offline Decisions producer and separate-process public exports for R."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from judgmentllm.decisions.provenance import export_decisions_provenance
from judgmentllm.lifecycle import export_run_results
from judgmentllm.persistence.repository import Repository


def persist(root: Path) -> None:
    if __package__:
        from scripts.decisions_r_fixture import source
    else:
        from decisions_r_fixture import source

    root.mkdir(parents=True, exist_ok=True)
    runs: list[dict[str, Any]] = []
    console = Path(sys.executable).with_name(
        "judgmentllm.exe" if os.name == "nt" else "judgmentllm"
    )
    hook = Path(__file__).resolve().parents[1] / "tests/fixtures/openai_decisions"
    for kind in ("rubric", "pairwise"):
        for capture in ("full", "minimal"):
            location = root / f"{kind}-{capture}"
            config = source(location, kind, capture)
            spec = json.loads(config.read_text())
            spec.update(provider="openai", endpoint="decisions", model="gpt-6-luna")
            config.write_text(json.dumps(spec), encoding="utf-8")
            env = {
                key: os.environ[key]
                for key in (
                    "PATH",
                    "HOME",
                    "LANG",
                    "LC_ALL",
                    "SystemRoot",
                    "WINDIR",
                    "TEMP",
                    "TMP",
                )
                if key in os.environ
            }
            env.update(
                PYTHONPATH=str(hook),
                PYTHONDONTWRITEBYTECODE="1",
                OPENAI_API_KEY="synthetic-decisions-key",
                DECISIONS_FIXTURE_ROOT=str(location),
                DECISIONS_FIXTURE_FILTER="1",
            )
            result = subprocess.run(
                [
                    str(console),
                    "run",
                    "--config",
                    str(config),
                    "--store",
                    str(location / "store"),
                    "--json",
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert result.returncode == 3, (result.stdout, result.stderr)
            report = json.loads(result.stdout)
            with Repository(location / "store") as repository:
                failed = next(
                    row
                    for row in repository.iter_results(report["run_id"])
                    if row.status == "invalid"
                )
            env["DECISIONS_FIXTURE_FILTER"] = "0"
            retried = subprocess.run(
                [
                    str(console),
                    "resume",
                    "--run",
                    report["run_id"],
                    "--store",
                    str(location / "store"),
                    "--retry-task",
                    failed.task_id,
                    "--retry-id",
                    "r-retry-001",
                    "--json",
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert retried.returncode == 3, (retried.stdout, retried.stderr)
            runs.append(
                dict(
                    run_id=report["run_id"],
                    kind=kind,
                    capture=capture,
                    location=str(location),
                )
            )
    (root / "runs.json").write_text(json.dumps(runs), encoding="utf-8")


def export(root: Path) -> None:
    runs = json.loads((root / "runs.json").read_text())
    for run in runs:
        store = Path(run["location"]) / "store"
        run["canonical"] = export_run_results(run["run_id"], store=store).model_dump(
            mode="json"
        )
        run["csv"] = export_run_results(
            run["run_id"], store=store, format="csv"
        ).model_dump(mode="json")
        run["r"] = export_run_results(
            run["run_id"], store=store, format="csv", view="r"
        ).model_dump(mode="json")
        run["provenance"] = export_decisions_provenance(
            run["run_id"], store=store
        ).model_dump(mode="json")
    (root / "exports.json").write_text(json.dumps(runs), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=("persist", "export"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    (persist if args.phase == "persist" else export)(args.output)


if __name__ == "__main__":
    main()
