"""Create and verify small, authentic installed-package compatibility fixtures.

The producer must be the unmodified baseline package. Run this helper using its
installed interpreter; a consumer version is never assigned to a saved manifest.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import sqlite3
import tarfile
from contextlib import closing
from pathlib import Path, PurePosixPath
from typing import Any, Literal

import judgmentllm
from judgmentllm import MockExecutionOptions, export_run_results, run_live
from judgmentllm.config import RunSpec

BASELINE = "4b4563102d1bf2dbb7ede8e40b87a7734f9437dd"


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def producer_identity(source_archive: Path) -> dict[str, Any]:
    """Verify every installed package resource against the archived baseline."""
    if judgmentllm.__version__ != "0.1.0a2":
        raise ValueError("Fixture production requires the genuine 0.1.0a2 package")
    if importlib.metadata.version("judgmentllm") != "0.1.0a2":
        raise ValueError("Installed producer metadata differs from package version")
    package = Path(judgmentllm.__file__).resolve().parent
    files: dict[str, str] = {}
    with tarfile.open(source_archive, "r:") as source:
        if source.pax_headers.get("comment") != BASELINE:
            raise ValueError("Producer source archive is not the approved baseline")
        for member in source:
            if not member.name.startswith("src/judgmentllm/") or not member.isfile():
                continue
            name = PurePosixPath(member.name).relative_to("src/judgmentllm")
            stream = source.extractfile(member)
            if stream is None:
                raise ValueError("Baseline package resource is unreadable")
            with stream:
                expected = hashlib.sha256(stream.read()).hexdigest()
            if digest(package / str(name)) != expected:
                raise ValueError("Installed producer differs from baseline source")
            files[member.name] = expected
    if not files:
        raise ValueError("Baseline source archive contains no package")
    return {
        "commit": BASELINE,
        "package_version": judgmentllm.__version__,
        "archive_sha256": digest(source_archive),
        "package_files": files,
        "producer_helper_sha256": digest(Path(__file__)),
    }


def evidence(root: Path) -> dict[str, Any]:
    """Read exact identities independently of public lifecycle summaries."""
    result: dict[str, Any] = {}
    with closing(
        sqlite3.connect(
            (root / "store/registry.sqlite3").as_uri() + "?mode=ro", uri=True
        )
    ) as connection:
        connection.row_factory = sqlite3.Row
        for table in (
            "runs",
            "tasks",
            "requests",
            "attempts",
            "attempt_tasks",
            "submissions",
            "observations",
            "submission_failures",
            "results",
            "retry_operations",
            "exports",
        ):
            result[table] = [
                dict(row) for row in connection.execute(f"SELECT * FROM {table}")
            ]
    with closing(
        sqlite3.connect((root / "ledger.sqlite3").as_uri() + "?mode=ro", uri=True)
    ) as connection:
        connection.row_factory = sqlite3.Row
        result["acceptances"] = [
            dict(row) for row in connection.execute("SELECT * FROM acceptances")
        ]
    return result


def portable_fixture(root: Path, source_archive: Path) -> dict[str, Any]:
    """Produce real full-capture state; only explicit snapshot recovery relocates it."""
    identity = producer_identity(source_archive)
    root.mkdir(parents=True, exist_ok=False)
    (root / "responses.jsonl").write_text(
        '{"item_id":"001","text":"Synthetic writing with an organized claim and example."}\n',
        encoding="utf-8",
    )
    fixture = root / "fixture.json"
    write_json(
        fixture,
        {
            "schema_version": 1,
            "scenario": {
                "probabilities": True,
                "category_index": 0,
                "members": {"003": {"outcome": "invalid_category"}},
                "reverse_members": True,
            },
        },
    )
    # Relative Path fields retain identical canonical spelling on Windows/POSIX.
    # SourceBuilder independently commits the true original absolute location.
    spec = RunSpec.model_validate(
        {
            "task_kind": "rubric",
            "input": "responses.jsonl",
            "provider": "mock",
            "endpoint": "synthetic",
            "model": "synthetic-v1",
            "capture": "full",
            "output_path": "store",
            "execution": {"grouping": "grouped", "concurrency": 1},
            "rubric": {
                "rubric_id": "001",
                "name": "Synthetic compatibility rubric",
                "traits": [
                    {
                        "trait_id": key,
                        "name": "Writing",
                        "description": "Evaluate this writing dimension.",
                        "levels": [
                            {"label": label, "numeric_value": code}
                            for label, code in (("low", 1), ("middle", 3), ("high", 8))
                        ],
                    }
                    for key in ("001", "002", "003")
                ],
            },
        }
    )
    import os

    previous = Path.cwd()
    try:
        os.chdir(root)
        summary = run_live(
            spec,
            store=root / "store",
            mock=MockExecutionOptions(
                ledger_path=root / "ledger.sqlite3",
                fixture_path=fixture,
            ),
        )
    finally:
        os.chdir(previous)
    views: tuple[tuple[Literal["jsonl", "csv"], Literal["canonical", "r"]], ...] = (
        ("jsonl", "canonical"),
        ("csv", "canonical"),
        ("csv", "r"),
    )
    for format_, view in views:
        export_run_results(
            summary.run_id, store=root / "store", format=format_, view=view
        )
    manifests = list((root / "store/runs").glob("*/manifest.json"))
    if len(manifests) != 1:
        raise ValueError("Portable producer did not create one run")
    raw = json.loads(manifests[0].read_text(encoding="utf-8"))
    if raw["package_version"] != "0.1.0a2":
        raise ValueError("Producer did not retain its authentic version")
    before = evidence(root)
    write_json(root / "before.json", before)
    write_json(root / "producer.json", identity)
    return {
        "run_id": summary.run_id,
        "manifest_sha256": digest(manifests[0]),
        **identity,
    }


def pack_fixture(
    root: Path, archive: Path, checksums: Path, identity: dict[str, Any]
) -> None:
    """Package only the small synthetic store, original ledger and audit sidecars."""
    files: dict[str, dict[str, str | int]] = {}
    # Immutable artifact aliases may be hard links on the producer filesystem.
    # Store their bytes independently; extraction never needs to accept links.
    with tarfile.open(archive, "w:gz", dereference=True) as target:
        selected = sorted((root / "store").rglob("*")) + [
            root / "ledger.sqlite3",
            root / "fixture.json",
            root / "before.json",
            root / "producer.json",
        ]
        for path in selected:
            if not path.is_file():
                continue
            name = path.relative_to(root).as_posix()
            files[name] = {"sha256": digest(path), "bytes": path.stat().st_size}
            target.add(path, arcname=name, recursive=False)
    write_json(
        checksums,
        {
            "schema_version": 1,
            "archive_sha256": digest(archive),
            "producer": identity,
            "files": files,
        },
    )


def extract_fixture(archive: Path, checksums: Path, output: Path) -> dict[str, Any]:
    """Reject links, traversal, duplicate/unexpected members and changed bytes."""
    manifest: dict[str, Any] = json.loads(checksums.read_text(encoding="utf-8"))
    if digest(archive) != manifest["archive_sha256"]:
        raise ValueError("Compatibility archive checksum differs")
    output.mkdir(parents=True, exist_ok=False)
    seen: set[str] = set()
    with tarfile.open(archive, "r:gz") as source:
        for member in source:
            name = PurePosixPath(member.name)
            if (
                not member.isfile()
                or name.is_absolute()
                or ".." in name.parts
                or "\\" in member.name
                or ":" in member.name
                or name.as_posix() != member.name
                or member.name in seen
                or member.name not in manifest["files"]
            ):
                raise ValueError("Compatibility archive contains an unsafe member")
            expected = manifest["files"][member.name]
            if member.size != expected["bytes"]:
                raise ValueError("Compatibility member length differs")
            stream = source.extractfile(member)
            if stream is None:
                raise ValueError("Compatibility member is unreadable")
            with stream:
                raw = stream.read()
            if hashlib.sha256(raw).hexdigest() != expected["sha256"]:
                raise ValueError("Compatibility member checksum differs")
            path = output / member.name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
            seen.add(member.name)
    if seen != set(manifest["files"]):
        raise ValueError("Compatibility archive is incomplete")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--baseline-source-archive", required=True, type=Path)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--checksums", required=True, type=Path)
    options = parser.parse_args()
    identity = portable_fixture(
        options.output.resolve(), options.baseline_source_archive
    )
    pack_fixture(options.output.resolve(), options.archive, options.checksums, identity)
    print(json.dumps(identity, sort_keys=True))


if __name__ == "__main__":
    main()
