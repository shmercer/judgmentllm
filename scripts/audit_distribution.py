"""Build and inspect committed public artifacts, then test isolated offline installs."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from email.message import Message
from email.parser import BytesParser
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[1]


def readme_description(metadata: Message) -> str:
    """Compare the complete UTF-8 description across platform line endings."""
    raw = metadata.get_payload(decode=True)
    assert isinstance(raw, bytes), "Package description must be a text payload"
    return raw.decode("utf-8").replace("\r\n", "\n").strip()


def members(path: Path) -> dict[str, bytes]:
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            return {
                n: archive.read(n) for n in archive.namelist() if not n.endswith("/")
            }
    with tarfile.open(path) as archive:
        result: dict[str, bytes] = {}
        for member in archive:
            if member.isfile():
                stream = archive.extractfile(member)
                assert stream is not None
                result[member.name.split("/", 1)[1]] = stream.read()
        return result


def measure(path: Path) -> dict[str, Any]:
    files = members(path)
    return {
        "filename": path.name,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "compressed_bytes": path.stat().st_size,
        "files": len(files),
        "uncompressed_bytes": sum(map(len, files.values())),
        "members": sorted(files),
    }


def audit(source: Path, output: Path) -> dict[str, Any]:
    source, output = source.resolve(), output.resolve()
    assert not output.is_relative_to(source), (
        "Distribution evidence must be outside the checkout"
    )
    sys.path.insert(0, str(source))
    sys.path.insert(0, str(source / "ci/local"))
    from ci.local.gates import verify_source
    from ci.local.source import stage
    from scripts.smoke_install import smoke_install

    output.mkdir(parents=True, exist_ok=False)
    clean = output / "source"
    if (source / ".git").exists():
        assert not subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=source
        ).strip(), "Commit changes before distribution acceptance"
        commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=source, text=True
        ).strip()
        stage(source, commit, output / "staging")
        manifest = cast(
            dict[str, Any], json.loads((output / "staging/manifest.json").read_text())
        )
        with tarfile.open(output / "staging/source.tar") as archive:
            archive.extractall(clean, filter="data")
    else:
        manifest = cast(
            dict[str, Any],
            json.loads((source / ".judgmentllm-source.json").read_text()),
        )
        commit = str(manifest["commit"])
        files = verify_source(source, commit, str(manifest["archive_sha256"]))
        for name in files:
            target = clean / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / name, target)
    lock = tomllib.loads((clean / "uv.lock").read_text())
    pinned = next(p["version"] for p in lock["package"] if p["name"] == "hatchling")
    assert importlib.metadata.version("hatchling") == pinned
    environment = {
        name: os.environ[name]
        for name in (
            "PATH",
            "HOME",
            "SystemRoot",
            "WINDIR",
            "TEMP",
            "TMP",
            "TMPDIR",
            "LANG",
            "LC_ALL",
            "UV_CACHE_DIR",
        )
        if name in os.environ
    }
    environment.update(
        UV_OFFLINE="1", UV_PYTHON_DOWNLOADS="never", PYTHONDONTWRITEBYTECODE="1"
    )

    def run(command: list[str], cwd: Path) -> None:
        with (output / "commands.log").open("a", encoding="utf-8") as log:
            result = subprocess.run(
                command,
                cwd=cwd,
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=180,
                check=False,
            )
        assert result.returncode == 0, (
            f"Distribution command failed: {command[:3]}; see commands.log"
        )

    artifacts = output / "artifacts"
    run(
        [
            sys.executable,
            "-m",
            "hatchling",
            "build",
            "-t",
            "wheel",
            "-t",
            "sdist",
            "-d",
            str(artifacts),
        ],
        clean,
    )
    (wheel,) = artifacts.glob("*.whl")
    (sdist,) = artifacts.glob("*.tar.gz")
    with tarfile.open(sdist) as archive:
        archive.extractall(output / "unpacked", filter="data")
    (extracted,) = (output / "unpacked").iterdir()
    run(
        [
            sys.executable,
            "-m",
            "hatchling",
            "build",
            "-t",
            "wheel",
            "-d",
            str(output / "rebuilt"),
        ],
        extracted,
    )
    (rebuilt,) = (output / "rebuilt").glob("*.whl")
    expected = {
        p.relative_to(clean / "src").as_posix(): p.read_bytes()
        for p in (clean / "src/judgmentllm").rglob("*")
        if p.is_file()
        and "study" not in p.relative_to(clean / "src/judgmentllm").parts
        and p != clean / "src/judgmentllm/keyring.py"
    }
    first, second, source_files = members(wheel), members(rebuilt), members(sdist)
    assert {n: b for n, b in first.items() if n.startswith("judgmentllm/")} == expected
    assert {n: b for n, b in second.items() if n.startswith("judgmentllm/")} == expected
    assert set(source_files) == {"src/" + n for n in expected} | {
        "README.md",
        "LICENSE",
        "pyproject.toml",
        "PKG-INFO",
        ".gitignore",
    }
    assert first == second, "Wheel rebuilt from sdist differs from direct wheel"
    assert "judgmentllm/py.typed" in first
    assert any(n.startswith("judgmentllm/templates/") for n in first)
    (metadata_name,) = (n for n in first if n.endswith(".dist-info/METADATA"))
    metadata = BytesParser().parsebytes(first[metadata_name])
    assert (
        metadata["License-Expression"] == "MIT"
        and metadata["Requires-Python"] == ">=3.11"
    )
    assert metadata["Description-Content-Type"] == "text/markdown"
    assert (
        readme_description(metadata)
        == (clean / "README.md").read_text(encoding="utf-8").strip()
    )
    (entry,) = (n for n in first if n.endswith(".dist-info/entry_points.txt"))
    assert b"judgmentllm = judgmentllm.cli:app" in first[entry]
    assert b"judgmentllm-keyring" not in first[entry]
    assert "judgmentllm/keyring.py" not in first
    assert "src/judgmentllm/keyring.py" not in source_files
    assert tomllib.loads(source_files["pyproject.toml"].decode())["project"][
        "scripts"
    ] == {"judgmentllm": "judgmentllm.cli:app"}
    (license_name,) = (n for n in first if n.endswith(".dist-info/licenses/LICENSE"))
    assert first[license_name] == (clean / "LICENSE").read_bytes()
    for name, raw in (first | source_files).items():
        assert not name.endswith((".pyc", ".pth", ".pem", ".key"))
        assert "__pycache__" not in name and "/study/" not in name
        assert str(source).encode() not in raw and str(output).encode() not in raw
        assert (
            b"-----BEGIN PRIVATE KEY-----" not in raw
            and b"-----BEGIN RSA PRIVATE KEY-----" not in raw
        )
    run(
        [
            sys.executable,
            "-m",
            "twine",
            "check",
            "--strict",
            str(wheel),
            str(sdist),
            str(rebuilt),
        ],
        clean,
    )
    uv = shutil.which("uv")
    assert uv is not None
    constraints = output / "core-constraints.txt"
    run(
        [
            uv,
            "export",
            "--locked",
            "--no-default-groups",
            "--no-emit-project",
            "--no-hashes",
            "--output-file",
            str(constraints),
        ],
        clean,
    )
    for name, artifact in (("wheel", wheel), ("sdist", sdist)):
        installation = output / ("installed-" + name)
        installation.mkdir()
        smoke_install(
            uv, artifact, constraints, installation, offline=True, locked_project=clean
        )
    report = {
        "source_commit": commit,
        "source_archive_sha256": manifest["archive_sha256"],
        "hatchling": pinned,
        "wheel": measure(wheel),
        "sdist": measure(sdist),
        "rebuilt_wheel": measure(rebuilt),
        "runtime_members_identical": True,
        "isolated_installs": ["wheel", "sdist"],
        "twine_strict": "passed",
        "passed": True,
    }
    (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = audit(args.source, args.output)
    print(
        json.dumps(
            {
                k: v
                for k, v in result.items()
                if k not in {"wheel", "sdist", "rebuilt_wheel"}
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
