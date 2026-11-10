"""Deferred source-bound offline proposal has no executable model or authority."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from judgmentllm.persistence.hashing import bytes_digest
from scripts import qualify_vertex as proposal


def committed_source(root: Path) -> str:
    return "a" * 40


def test_deferred_proposal_is_separate_immutable_and_credential_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(proposal, "source_commit", committed_source)
    for mode in ("adc", "api_key"):
        output = tmp_path / mode
        digest = proposal.prepare_proposal(
            source="a" * 40,
            authentication_mode=mode,
            location="global",
            output=output,
            root=tmp_path,
        )
        raw = (output / "proposal.json").read_bytes()
        assert (
            bytes_digest(raw)
            == digest
            == (output / "proposal.sha256").read_text().strip()
        )
        plan = proposal.DeferredProposal.model_validate_json(raw)
        assert plan.provider == "vertex" and plan.authentication_mode == mode
        assert not plan.execution_enabled and plan.authorized_requests == 0
        assert (
            plan.serving_model
            is plan.estimated_cost_usd
            is plan.input_reservation_tokens
            is None
        )
        assert plan.physical_requests == plan.physical_request_hashes == ()
        assert len(plan.proposed_cases) == plan.maximum_proposed_requests == 4
        with pytest.raises(FileExistsError):
            proposal.prepare_proposal(
                source="a" * 40,
                authentication_mode=mode,
                location="global",
                output=output,
                root=tmp_path,
            )
        assert (output / "proposal.json").read_bytes() == raw
    with pytest.raises(ValueError):
        proposal.prepare_proposal(
            source="b" * 40,
            authentication_mode="adc",
            location="global",
            output=tmp_path / "wrong",
            root=tmp_path,
        )
    with pytest.raises(ValueError):
        proposal.prepare_proposal(
            source="a" * 40,
            authentication_mode="adc",
            location="invalid",
            output=tmp_path / "invalid",
            root=tmp_path,
        )
    assert not (tmp_path / "wrong").exists() and not (tmp_path / "invalid").exists()


@pytest.mark.parametrize(
    "dirty,fail",
    [("", False), ("?? untracked.py\n", False), (" M source.py\n", False), ("", True)],
)
def test_exact_source_rejects_dirty_or_unreadable_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dirty: str, fail: bool
) -> None:
    def read(arguments: list[str], **kwargs: Any) -> Any:
        if fail:
            raise subprocess.CalledProcessError(1, arguments, stderr="PRIVATE_DETAIL")
        return subprocess.CompletedProcess(
            arguments, 0, stdout="a" * 40 if arguments[1] == "rev-parse" else dirty
        )

    monkeypatch.setattr(proposal.subprocess, "run", read)
    if dirty or fail:
        with pytest.raises(ValueError) as error:
            proposal.source_commit(tmp_path)
        assert "PRIVATE" not in str(error.value)
    else:
        assert proposal.source_commit(tmp_path) == "a" * 40


def test_no_execute_command_can_be_invoked(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(proposal.sys, "argv", ["qualify_vertex.py", "execute"])
    with pytest.raises(SystemExit) as error:
        proposal.main()
    assert error.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_cli_proposal_failure_is_safe(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    def fail(**kwargs: Any) -> str:
        raise OSError("PRIVATE_ERROR")

    monkeypatch.setattr(proposal, "prepare_proposal", fail)
    monkeypatch.setattr(
        proposal.sys,
        "argv",
        [
            "qualify_vertex.py",
            "plan",
            "--source-commit",
            "a" * 40,
            "--auth-mode",
            "adc",
            "--location",
            "global",
            "--evidence-dir",
            str(tmp_path / "out"),
        ],
    )
    with pytest.raises(SystemExit) as error:
        proposal.main()
    assert error.value.code == 2 and "PRIVATE_ERROR" not in capsys.readouterr().err


def test_cli_proposal_prints_only_digest(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setattr(proposal, "source_commit", committed_source)
    monkeypatch.setattr(
        proposal.sys,
        "argv",
        [
            "qualify_vertex.py",
            "plan",
            "--source-commit",
            "a" * 40,
            "--auth-mode",
            "api_key",
            "--location",
            "us-central1",
            "--evidence-dir",
            str(tmp_path / "out"),
        ],
    )
    proposal.main()
    assert capsys.readouterr().out.strip() == bytes_digest(
        (tmp_path / "out" / "proposal.json").read_bytes()
    )
    assert json.loads((tmp_path / "out" / "proposal.json").read_text())[
        "status"
    ].startswith("deferred")
