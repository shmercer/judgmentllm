"""Real pinned-backend artifacts and isolated installs from committed source."""

import json
import subprocess
import sys
from email.parser import BytesParser
from pathlib import Path

import pytest


@pytest.mark.parametrize("source_newline", ["\n", "\r\n"])
@pytest.mark.parametrize("metadata_newline", ["\n", "\r\n"])
def test_readme_metadata_preserves_complete_text_across_platform_newlines(
    tmp_path: Path, source_newline: str, metadata_newline: str
) -> None:
    from scripts.audit_distribution import readme_description

    content = "# Synthetic café 雪\n\nKeep `001` and **all** Markdown content.\n"
    path = tmp_path / "README.md"
    path.write_bytes(content.replace("\n", source_newline).encode("utf-8"))
    raw = (
        "Metadata-Version: 2.4\nName: synthetic\nDescription-Content-Type: text/markdown\n\n"
        + content
    ).replace("\n", metadata_newline)
    metadata = BytesParser().parsebytes(raw.encode("utf-8"))
    assert readme_description(metadata) == path.read_text(encoding="utf-8").strip()
    assert readme_description(metadata) == content.strip()


@pytest.mark.serial
def test_committed_distribution_manifest_and_isolated_installs(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    output = tmp_path / "distribution"
    result = subprocess.run(
        [
            sys.executable,
            str(root / "scripts/audit_distribution.py"),
            "--source",
            str(root),
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads((output / "report.json").read_text())
    assert report["passed"] and report["runtime_members_identical"]
    assert report["isolated_installs"] == ["wheel", "sdist"]
