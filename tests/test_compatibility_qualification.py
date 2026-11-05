"""Cheap guards for the separately executed cross-version qualification tool."""

from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from scripts.qualify_version_compatibility import (
    compare_identities,
    expected_cases,
    expected_counts,
    verify_boundary_before,
)
from scripts.version_compatibility_fixture import digest, evidence, extract_fixture

ROOT = Path(__file__).resolve().parents[1]


def test_qualification_inventory_retains_preparation_and_response_loss_windows() -> (
    None
):
    kinds = {
        "completed",
        "partial",
        "interrupted",
        "uncertain",
        "sealed",
        "after_preparation",
        "after_receipt",
        "after_observation",
    }
    assert expected_cases() == {
        f"{kind}-{capture}" for kind in kinds for capture in ("full", "minimal")
    }
    assert len(expected_cases()) == 16
    assert expected_counts("partial", "minimal") == (6, 2, 4, 4, 8)
    assert expected_counts("completed", "full") == (2, 2, 2, 2, 2)
    for kind in ("sealed", "after_preparation", "after_receipt", "after_observation"):
        assert (
            expected_counts(kind, "full")
            == expected_counts(kind, "minimal")
            == (3, 1, 1, 1, 3)
        )


def test_existing_results_cannot_impersonate_a_response_loss_fixture(
    tmp_path: Path,
) -> None:
    root = tmp_path / "fixture"
    extract_fixture(
        ROOT / "tests/fixtures/a2-compatibility.tar.gz",
        ROOT / "tests/fixtures/a2-compatibility.json",
        root,
    )
    with pytest.raises(AssertionError, match="prepared membership"):
        verify_boundary_before(root, "after_receipt", "full", evidence(root))


@pytest.mark.parametrize(
    "damage", ["payload", "result", "duplicate-acceptance", "none"]
)
def test_identity_comparison_detects_real_fixture_damage(
    tmp_path: Path, damage: str
) -> None:
    root = tmp_path / "fixture"
    extract_fixture(
        ROOT / "tests/fixtures/a2-compatibility.tar.gz",
        ROOT / "tests/fixtures/a2-compatibility.json",
        root,
    )
    before = evidence(root)
    after = copy.deepcopy(before)
    if damage == "payload":
        after["submissions"][0]["payload_sha256"] = "0" * 64
    elif damage == "result":
        after["results"].pop()
    elif damage == "duplicate-acceptance":
        after["acceptances"][0]["acceptance_count"] = 2
    else:
        after["attempts"][0]["state"] = "responded"
        compare_identities(before, after)
        return
    with pytest.raises(AssertionError):
        compare_identities(before, after)


def test_qualification_requires_prepared_inputs_without_creating_output(
    tmp_path: Path,
) -> None:
    output = tmp_path / "output"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/qualify_version_compatibility.py"),
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 2
    assert not output.exists()


@pytest.mark.parametrize(
    "name", ["../escape", "/absolute", "dir\\escape", "C:escape", "./file", "link"]
)
def test_compatibility_extraction_rejects_unsafe_members(
    tmp_path: Path, name: str
) -> None:
    archive = tmp_path / "unsafe.tar.gz"
    member = tarfile.TarInfo(name)
    if name == "link":
        member.type = tarfile.SYMTYPE
        member.linkname = "/outside"
    with tarfile.open(archive, "w:gz") as target:
        target.addfile(member)
    checksums = tmp_path / "checksums.json"
    checksums.write_text(
        json.dumps(
            {
                "archive_sha256": digest(archive),
                "files": {
                    name: {"bytes": 0, "sha256": hashlib.sha256(b"").hexdigest()}
                },
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unsafe"):
        extract_fixture(archive, checksums, tmp_path / "extracted")
