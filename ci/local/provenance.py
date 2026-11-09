"""Source and interpreter provenance for reusable CI preflight receipts."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

RUN_ID_ENV = "JUDGMENTLLM_CI_RUN_ID"


def source_record(source: Path) -> dict[str, Any]:
    manifest = source / ".judgmentllm-source.json"
    if manifest.is_file():
        recorded = cast(
            dict[str, Any], json.loads(manifest.read_text(encoding="utf-8"))
        )
        identity: dict[str, Any] = {
            "commit": recorded["commit"],
            "archive_sha256": recorded["archive_sha256"],
            "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
            "dirty": False,
        }
    elif shutil.which("git") is not None:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=source,
            capture_output=True,
            text=True,
            check=False,
        )
        state = subprocess.run(
            ["git", "status", "--porcelain=v1"],
            cwd=source,
            capture_output=True,
            text=True,
            check=False,
        )
        identity = {
            "commit": commit.stdout.strip() if commit.returncode == 0 else None,
            "dirty": bool(state.stdout.strip()) if state.returncode == 0 else None,
        }
    else:
        # Synthetic runner acceptance uses unversioned temporary projects. The
        # native guest needs no Git installation when qualifying manifest-bound
        # archives; unknown identity here must not prevent a failure receipt.
        identity = {"commit": None, "dirty": None}
    lock = source / "uv.lock"
    identity["lockfile_sha256"] = (
        hashlib.sha256(lock.read_bytes()).hexdigest() if lock.exists() else None
    )
    versions: dict[str, str | None] = {}
    for name in (
        "pytest",
        "pytest-xdist",
        "pytest-cov",
        "coverage",
        "openai",
        "pydantic",
        "psutil",
    ):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    identity["dependencies"] = versions
    identity["python"] = sys.version
    return identity


def receipt_provenance(
    source: dict[str, Any], qualification_run_id: str | None
) -> dict[str, Any]:
    """Bind results to their actual source/environment and one launcher run."""
    return {
        "source": {
            key: source.get(key)
            for key in ("commit", "archive_sha256", "manifest_sha256", "dirty")
        },
        "lockfile_sha256": source["lockfile_sha256"],
        "interpreter": {
            "implementation": sys.implementation.name,
            "version": sys.version,
            "executable": os.path.normcase(str(Path(sys.executable).resolve())),
            "prefix": os.path.normcase(str(Path(sys.prefix).resolve())),
            "platform": sys.platform,
            "dependencies": source["dependencies"],
        },
        "qualification_run_id": qualification_run_id,
    }


def verify_preflight_provenance(
    receipt: dict[str, Any], expected: dict[str, Any]
) -> None:
    """Reject stale, foreign or unbound results before crediting any cases."""
    source = expected["source"]
    if (
        source["dirty"] is not False
        or re.fullmatch(r"[0-9a-f]{40}", str(source["commit"])) is None
        or re.fullmatch(r"[0-9a-f]{64}", str(expected["lockfile_sha256"])) is None
        or not isinstance(expected["qualification_run_id"], str)
        or not expected["qualification_run_id"].strip()
    ):
        raise ValueError(
            "Preflight reuse requires known, clean source/lock/run identities"
        )
    raw = receipt.get("provenance")
    if not isinstance(raw, dict):
        raise ValueError("Preflight receipt has missing or invalid provenance")
    recorded = cast(dict[str, Any], raw)
    if set(recorded) != set(expected):
        raise ValueError("Preflight receipt has missing or invalid provenance")
    for field, value in expected.items():
        # Canonical JSON comparison also rejects bool/int coercion, such as
        # dirty=0 being treated as the required dirty=False.
        if json.dumps(recorded[field], sort_keys=True) != json.dumps(
            value, sort_keys=True
        ):
            raise ValueError(f"Preflight provenance differs: {field}")
