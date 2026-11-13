"""Share unexecuted study plans; execution tests copy their own preparation stores."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from judgmentllm.study.records import Proposal


@pytest.fixture(scope="session")
def pilot(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Proposal]:
    from study_fixture import PACKAGE, SOURCE

    from judgmentllm.study import plans

    root = tmp_path_factory.mktemp("study-pilot")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(plans, "_source_commit", lambda: SOURCE)
        proposal = plans.build_proposal(PACKAGE, root / "proposal", "native_pilot")
    return root / "proposal", proposal


@pytest.fixture(scope="session")
def tier(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Proposal]:
    from study_fixture import PACKAGE, SOURCE

    from judgmentllm.study import plans

    root = tmp_path_factory.mktemp("study-tier")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(plans, "_source_commit", lambda: SOURCE)
        proposal = plans.build_proposal(
            PACKAGE, root / "proposal", "tier_feasibility", capture="minimal"
        )
    return root / "proposal", proposal
