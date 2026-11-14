"""Share unexecuted study plans; execution tests copy their own preparation stores."""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from fixture_artifacts import snapshot

if TYPE_CHECKING:
    from judgmentllm.study.records import Proposal


@pytest.fixture(scope="session")
def pilot(tmp_path_factory: pytest.TempPathFactory) -> Generator[tuple[Path, Proposal]]:
    from study_fixture import PACKAGE, SOURCE

    from judgmentllm.study import plans

    root = tmp_path_factory.mktemp("study-pilot")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(plans, "_source_commit", lambda: SOURCE)
        proposal = plans.build_proposal(PACKAGE, root / "proposal", "native_pilot")
    before, model = snapshot(root), proposal.model_dump_json()
    yield root / "proposal", proposal
    assert snapshot(root) == before and proposal.model_dump_json() == model


@pytest.fixture(scope="session")
def tier(tmp_path_factory: pytest.TempPathFactory) -> Generator[tuple[Path, Proposal]]:
    from study_fixture import PACKAGE, SOURCE

    from judgmentllm.study import plans

    root = tmp_path_factory.mktemp("study-tier")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(plans, "_source_commit", lambda: SOURCE)
        proposal = plans.build_proposal(
            PACKAGE, root / "proposal", "tier_feasibility", capture="minimal"
        )
    before, model = snapshot(root), proposal.model_dump_json()
    yield root / "proposal", proposal
    assert snapshot(root) == before and proposal.model_dump_json() == model


@pytest.fixture(
    scope="session", params=("reasoning_comparison", "temperature_acceptance")
)
def control_proposal(
    tmp_path_factory: pytest.TempPathFactory, request: pytest.FixtureRequest
) -> Generator[tuple[Path, Proposal]]:
    from study_fixture import PACKAGE, SOURCE

    from judgmentllm.study import plans

    root = tmp_path_factory.mktemp(request.param)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(plans, "_source_commit", lambda: SOURCE)
        proposal = plans.build_proposal(
            PACKAGE,
            root / "proposal",
            request.param,
            capture="minimal" if request.param == "temperature_acceptance" else "full",
        )
    before, model = snapshot(root), proposal.model_dump_json()
    yield root / "proposal", proposal
    assert snapshot(root) == before and proposal.model_dump_json() == model
