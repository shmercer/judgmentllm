"""Linux-host admission tests; guest portable tests live in test_gates.py."""

from __future__ import annotations

import hashlib
import io
import json
import subprocess
from pathlib import Path
from typing import Any

import grid
import pytest
from source import stage
from test_source import repository


def summaries() -> list[dict[str, Any]]:
    return [
        {
            "commit": "a" * 40,
            "archive_sha256": "b" * 64,
            "lockfile_sha256": "c" * 64,
            "mode": "check",
            "status": "passed",
            "source_verified_before": True,
            "source_verified_after": True,
            "all_scheduled_gates_finished": True,
            "os": {"system": "Windows"},
            "python": {"version": version},
        }
        for version in ("3.11.18", "3.12.16")
    ]


def admit(items: list[dict[str, Any]]) -> list[str]:
    return grid.admission_errors(
        items,
        platform="windows",
        ref="a" * 40,
        mode="check",
        archive_sha256="b" * 64,
        lockfile_sha256="c" * 64,
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("commit", "d" * 40),
        ("archive_sha256", "d" * 64),
        ("lockfile_sha256", "d" * 64),
        ("os", {"system": "Linux"}),
        ("python", {"version": "3.11.17"}),
        ("status", "running"),
        ("mode", "prepare"),
        ("source_verified_after", False),
        ("all_scheduled_gates_finished", False),
    ],
)
def test_admission_rejects_wrong_source_os_or_partial_results(
    field: str, value: object
) -> None:
    items = summaries()
    assert not admit(items)
    items[0][field] = value
    assert admit(items)


def test_fresh_collection_ignores_old_results_and_rejects_extra_new_results(
    tmp_path: Path,
) -> None:
    old = tmp_path / "old/summary.json"
    old.parent.mkdir()
    old.write_text(json.dumps({"status": "passed", "commit": "old"}))
    before = set(tmp_path.glob("*/summary.json"))
    for index, item in enumerate(summaries()):
        path = tmp_path / f"new-{index}/summary.json"
        path.parent.mkdir()
        path.write_text(json.dumps(item), encoding="utf-8-sig")
    paths, items = grid.collect_fresh(tmp_path, "*/summary.json", before)
    assert old not in paths and len(paths) == 2
    assert not grid.admission_errors(
        items,
        platform="windows",
        ref="a" * 40,
        mode="check",
        archive_sha256="b" * 64,
        lockfile_sha256="c" * 64,
    )
    assert admit(summaries() + summaries())
    assert admit(summaries()[:1])


def test_download_checks_bytes_before_publishing_and_reuses_valid_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def download(url: str, timeout: int) -> io.BytesIO:
        return io.BytesIO(b"downloaded fixture")

    monkeypatch.setattr(grid.urllib.request, "urlopen", download)
    with pytest.raises(ValueError, match="checksum mismatch"):
        grid.asset(tmp_path, "uv.tar.gz", "0" * 64, "https://unused.invalid")
    assert not (tmp_path / "uv.tar.gz").exists()
    expected = hashlib.sha256(b"downloaded fixture").hexdigest()
    cached = grid.asset(tmp_path, "uv.tar.gz", expected, "https://unused.invalid")

    def no_network(url: str, timeout: int) -> io.BytesIO:
        raise AssertionError("Valid cached asset must not access the network")

    monkeypatch.setattr(grid.urllib.request, "urlopen", no_network)
    assert (
        grid.asset(tmp_path, "uv.tar.gz", expected, "https://unused.invalid") == cached
    )


def test_source_archive_rejects_windows_drive_or_stream_names(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repository(repo)
    (repo / "drive:stream").write_text("synthetic unsafe portable path")
    subprocess.run(
        ["git", "-C", str(repo), "-c", "core.protectNTFS=false", "add", "drive:stream"],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=CI fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "unsafe portable filename",
        ],
        check=True,
    )
    ref = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], text=True
    ).strip()
    with pytest.raises(ValueError, match="unsafe path"):
        stage(repo, ref, tmp_path / "bundle")
