"""Initial usage audit metadata must remain bound to its exact companion."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Literal

import pytest
from test_evidence_persistence import prepared as prepared

from judgmentllm.persistence import evidence as evidence_store
from judgmentllm.persistence.evidence import EvidenceReceipt, evidence_digest
from judgmentllm.persistence.models import IntegrityError
from judgmentllm.persistence.ownership import ExecutorOwnership
from judgmentllm.persistence.repository import ReadOnlyRepository, Repository

Damage = Literal[
    "receipt_sha256", "revision_id", "revision_number", "source", "source_version"
]


def _commit(repository: Repository, receipt: EvidenceReceipt) -> None:
    with ExecutorOwnership(repository, receipt.identity.run_id) as ownership:
        assert ownership.owner is not None
        repository.record_evidence(receipt, owner_token=ownership.owner.token)


def _tamper_initial(repository: Repository, damage: Damage) -> None:
    """Simulate corruption, restoring exact schema guards before inspection."""
    trigger = repository.connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='usage_revisions_immutable_update'"
    ).fetchone()[0]
    replacement = repository.get_manifest("r").artifact.sha256
    original = repository.connection.execute(
        "SELECT receipt_sha256 FROM usage_revisions WHERE run_id='r' AND submission_id='s' AND revision_number=0"
    ).fetchone()[0]
    assert replacement != original
    assignments: dict[Damage, tuple[str, tuple[object, ...]]] = {
        "receipt_sha256": ("receipt_sha256=?", (replacement,)),
        "revision_id": ("revision_id=?", ("substituted-initial",)),
        "revision_number": (
            "revision_number=?,source=?",
            (1, "synthetic.authoritative"),
        ),
        "source": ("source=?", ("synthetic.authoritative",)),
        "source_version": ("source_version=?", (2,)),
    }
    sql, values = assignments[damage]
    repository.connection.execute("DROP TRIGGER usage_revisions_immutable_update")
    # Source/version CHECK violations represent a corrupted file, rather than a
    # supported SQL write. The foreign-key check stays enabled in every case.
    repository.connection.execute("PRAGMA ignore_check_constraints=ON")
    try:
        repository.connection.execute(
            f"UPDATE usage_revisions SET {sql} WHERE run_id='r' AND submission_id='s' AND revision_number=0",
            values,
        )
    finally:
        repository.connection.execute("PRAGMA ignore_check_constraints=OFF")
        repository.connection.execute(trigger)
    assert repository.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert repository.connection.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize(
    "damage",
    ["receipt_sha256", "revision_id", "revision_number", "source", "source_version"],
)
@pytest.mark.parametrize("reader", ["submission", "observation", "history"])
def test_initial_usage_metadata_is_verified_on_read_only_reopen(
    prepared: tuple[Repository, EvidenceReceipt], damage: Damage, reader: str
) -> None:
    repository, receipt = prepared
    _commit(repository, receipt)
    _tamper_initial(repository, damage)
    # Schema verification succeeds: rejection must come from the evidence link,
    # including when another real registered artifact satisfies the SHA FK.
    with ReadOnlyRepository(repository.root) as reopened:
        with pytest.raises(IntegrityError, match="initial usage failed verification"):
            if reader == "submission":
                reopened.get_evidence_for_submission("r", "s")
            elif reader == "observation":
                reopened.get_evidence_for_observation("r", "o")
            else:
                list(reopened.usage_revision_history("r", "s", page_size=1))


def test_revision_page_rechecks_initial_companion_after_receipt_read(
    prepared: tuple[Repository, EvidenceReceipt], monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, receipt = prepared
    _commit(repository, receipt)
    original = evidence_store.get_evidence_for_submission

    def read_then_tamper(
        registry: Repository, run_id: str, submission_id: str
    ) -> EvidenceReceipt | None:
        current = original(registry, run_id, submission_id)
        _tamper_initial(registry, "receipt_sha256")
        return current

    monkeypatch.setattr(evidence_store, "get_evidence_for_submission", read_then_tamper)
    with pytest.raises(IntegrityError, match="initial usage failed verification"):
        list(repository.usage_revision_history("r", "s", page_size=1))


def test_intact_initial_usage_metadata_round_trips(
    prepared: tuple[Repository, EvidenceReceipt],
) -> None:
    repository, receipt = prepared
    _commit(repository, receipt)
    with ReadOnlyRepository(repository.root) as reopened:
        assert reopened.get_evidence_for_submission("r", "s") == receipt
        history: Iterator[evidence_store.UsageRevisionRecord] = (
            reopened.usage_revision_history("r", "s", page_size=1)
        )
        initial = next(history)
        assert (
            initial.revision_id,
            initial.revision_number,
            initial.source,
            initial.source_version,
            initial.receipt_sha256,
        ) == ("initial", 0, "initial", 1, evidence_digest(receipt))
        assert list(history) == []
