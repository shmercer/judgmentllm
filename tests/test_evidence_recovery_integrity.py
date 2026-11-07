"""Public recovery verifies committed evidence links independently of valid FKs."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Literal

import pytest
from durable_evidence_fixture import EvidenceFixtureAdapter, specification
from test_durable_evidence_lifecycle import use_adapter

from judgmentllm import LifecycleError, resume_run, run_live
from judgmentllm.persistence.repository import ReadOnlyRepository


@pytest.mark.parametrize("capture", ["full", "minimal"])
def test_committed_result_recovery_rejects_initial_usage_receipt_rebinding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capture: Literal["full", "minimal"],
) -> None:
    source = specification(tmp_path / "inputs", capture=capture)
    store = tmp_path / "runtime"
    adapter = EvidenceFixtureAdapter(store / "mock-service.sqlite3")
    use_adapter(monkeypatch, adapter)
    report = run_live(source, store=store)
    assert report.completed_tasks == report.results == 2
    with ReadOnlyRepository(store) as repository:
        receipt = next(repository.iter_evidence(report.run_id))
        run_directory = repository.run_directory(report.run_id)
        submission_id = receipt.identity.submission_id
        results = tuple(repository.iter_results(report.run_id))

    with closing(sqlite3.connect(store / "registry.sqlite3")) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        original_sha = connection.execute(
            "SELECT receipt_sha256 FROM usage_revisions "
            "WHERE run_id=? AND submission_id=? AND revision_number=0",
            (report.run_id, submission_id),
        ).fetchone()[0]
        replacement = connection.execute(
            "SELECT sha256,relative_path FROM artifacts "
            "WHERE run_id=? AND sha256<>? AND relative_path IS NOT NULL "
            "ORDER BY sha256 LIMIT 1",
            (report.run_id, original_sha),
        ).fetchone()
        assert replacement is not None and replacement[0] != original_sha
        assert (run_directory / replacement[1]).is_file()
        schema = connection.execute(
            "SELECT type,name,sql FROM sqlite_master ORDER BY type,name"
        ).fetchall()
        trigger = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' "
            "AND name='usage_revisions_immutable_update'"
        ).fetchone()[0]
        # Model tampering after bypassing append-only enforcement; restore the
        # exact trigger before reopening so only evidence verification can fail.
        with connection:
            connection.execute("DROP TRIGGER usage_revisions_immutable_update")
            connection.execute(
                "UPDATE usage_revisions SET receipt_sha256=? "
                "WHERE run_id=? AND submission_id=? AND revision_number=0",
                (replacement[0], report.run_id, submission_id),
            )
            connection.execute(trigger)
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert (
            connection.execute(
                "SELECT type,name,sql FROM sqlite_master ORDER BY type,name"
            ).fetchall()
            == schema
        )

    with ReadOnlyRepository(store) as repository:
        assert tuple(repository.iter_results(report.run_id)) == results
    with pytest.raises(LifecycleError) as failure:
        resume_run(report.run_id, store=store)
    assert failure.value.category == "execution"
    assert failure.value.message in {
        "durable run integrity or storage operation failed",
        "provider evidence or execution operation is unavailable",
    }
    assert original_sha not in str(failure.value)
    assert replacement[0] not in str(failure.value)
    assert adapter.submit_calls == adapter.ledger.acceptance_count() == 1
