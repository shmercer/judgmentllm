"""Stage one committed checkout without copying untracked files or credentials."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import tarfile
from pathlib import Path, PurePosixPath


def stage(repo: Path, ref: str, output: Path) -> dict[str, object]:
    if re.fullmatch(r"[0-9a-f]{40}", ref) is None:
        raise ValueError("--ref must be the complete proposed commit SHA")
    repository = repo.resolve(strict=True)
    resolved = subprocess.check_output(
        ["git", "-C", str(repository), "rev-parse", "--verify", f"{ref}^{{commit}}"],
        text=True,
    ).strip()
    if resolved != ref:
        raise ValueError("source reference did not resolve to the requested commit")
    # Exclusive creation is intentional: never replace a checkout, cache or run.
    output.mkdir(parents=True, exist_ok=False)
    archive = output / "source.tar"
    with archive.open("xb") as stream:
        subprocess.run(
            ["git", "-C", str(repository), "archive", "--format=tar", ref],
            stdout=stream,
            check=True,
        )
    files: dict[str, str] = {}
    with tarfile.open(archive, "r:") as source:
        for member in source:
            name = PurePosixPath(member.name)
            if (
                name.is_absolute()
                or ".." in name.parts
                or "\\" in member.name
                or ":" in member.name
                or name.as_posix() != member.name.rstrip("/")
            ):
                raise ValueError("source archive contains an unsafe path")
            if member.isdir():
                continue
            if not member.isfile() or member.name in files:
                raise ValueError("source archive requires unique regular files")
            if member.name == ".judgmentllm-source.json":
                raise ValueError("reserved source provenance filename is tracked")
            stream = source.extractfile(member)
            if stream is None:
                raise ValueError("source archive member is unreadable")
            with stream:
                identity = hashlib.sha256()
                while chunk := stream.read(1024 * 1024):
                    identity.update(chunk)
                files[member.name] = identity.hexdigest()
    required = {"pyproject.toml", "uv.lock", "ci/local/gates.py"}
    if not required.issubset(files):
        raise ValueError(
            "proposed commit must contain the local CI harness and lockfile"
        )
    with archive.open("rb") as stream:
        archive_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    manifest: dict[str, object] = {
        "format_version": 1,
        "commit": ref,
        "archive_sha256": archive_hash,
        "lockfile_sha256": files["uv.lock"],
        "files": files,
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--ref", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = stage(args.repo, args.ref, args.output)
    print(json.dumps({key: value for key, value in result.items() if key != "files"}))


if __name__ == "__main__":
    main()
