"""Admission checks keep local CI tied to committed, safe, immutable input."""

from __future__ import annotations

import hashlib
import json
import subprocess
import tarfile
from pathlib import Path

import pytest
from grid import asset
from source import stage


def repository(path: Path) -> str:
    path.mkdir()
    subprocess.run(["git", "init", "--quiet", str(path)], check=True)
    (path / "ci/local").mkdir(parents=True)
    for name in ("pyproject.toml", "uv.lock", "ci/local/gates.py"):
        (path / name).write_text("committed synthetic fixture\n")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(path),
            "-c",
            "user.name=CI fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "fixture",
        ],
        check=True,
    )
    return subprocess.check_output(
        ["git", "-C", str(path), "rev-parse", "HEAD"], text=True
    ).strip()


def test_staging_uses_only_requested_commit_and_preserves_existing_output(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    commit = repository(repo)
    (repo / "uv.lock").write_text("uncommitted replacement")
    (repo / ".env").write_text("SYNTHETIC_UNTRACKED_PRIVATE_SENTINEL")
    output = tmp_path / "bundle"
    manifest = stage(repo, commit, output)
    assert manifest["commit"] == commit
    assert (
        manifest["lockfile_sha256"]
        == hashlib.sha256(b"committed synthetic fixture\n").hexdigest()
    )
    assert json.loads((output / "manifest.json").read_text()) == manifest
    with tarfile.open(output / "source.tar") as archive:
        assert ".env" not in archive.getnames()
        member = archive.extractfile("uv.lock")
        assert member is not None and member.read() == b"committed synthetic fixture\n"
    before = (output / "source.tar").read_bytes()
    with pytest.raises(FileExistsError):
        stage(repo, commit, output)
    assert (output / "source.tar").read_bytes() == before


def test_ref_is_a_full_commit_before_any_output_is_created(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repository(repo)
    for ref in ("HEAD", "main", "abc123", "x" * 40):
        with pytest.raises(ValueError, match="complete proposed commit"):
            stage(repo, ref, tmp_path / "output")
        assert not (tmp_path / "output").exists()


def test_cached_prerequisites_are_never_overwritten_on_mismatch(tmp_path: Path) -> None:
    cached = tmp_path / "uv.zip"
    cached.write_bytes(b"existing bytes")
    with pytest.raises(ValueError, match="refusing overwrite"):
        asset(tmp_path, "uv.zip", "0" * 64, "https://invalid.example/unused")
    assert cached.read_bytes() == b"existing bytes"
