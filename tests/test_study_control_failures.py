"""Apply the existing failure/recovery oracles to the new explicit control cases."""

from pathlib import Path

import pytest
import test_study_failures as failures
from test_study_native import offline as offline

from judgmentllm.study.records import Proposal


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome", ["refusal", "truncated", "wrong_model", "missing_usage", "ambiguous_503"]
)
async def test_control_failures_keep_original_evidence_and_reservations(
    tmp_path: Path, control_proposal: tuple[Path, Proposal], outcome: str
) -> None:
    await failures.test_failed_and_incomplete_observations_never_rescore(
        tmp_path, control_proposal, 1, outcome
    )


@pytest.mark.asyncio
async def test_control_cancellation_keeps_reservation(
    tmp_path: Path, control_proposal: tuple[Path, Proposal]
) -> None:
    await failures.test_cancelled_handoff_keeps_reservation(
        tmp_path, control_proposal, 1
    )


@pytest.mark.serial
@pytest.mark.asyncio
async def test_control_process_death_cannot_replenish_authority(
    tmp_path: Path, control_proposal: tuple[Path, Proposal]
) -> None:
    directory, proposal = control_proposal
    explicit = proposal.calls[1]
    reordered = proposal.model_copy(
        update={
            "calls": (explicit,) + tuple(c for c in proposal.calls if c != explicit)
        }
    )
    await failures.test_process_death_cannot_replenish_calls_tokens_or_money(
        tmp_path, (directory, reordered)
    )
