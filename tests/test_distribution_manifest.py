"""Real pinned-backend artifacts and isolated installs from committed source."""

import json
import subprocess
import sys
from pathlib import Path

import pytest


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
