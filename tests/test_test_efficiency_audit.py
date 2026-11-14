"""The audit must flag lost execution even when aggregate percentages look healthy."""

from copy import deepcopy
from typing import Any

import pytest

from scripts.audit_test_efficiency import coverage_losses


@pytest.mark.parametrize(
    "field", ["executed_lines", "executed_branches", "excluded_lines"]
)
def test_audit_reports_line_arc_or_exclusion_loss(field: str) -> None:
    before: dict[str, Any] = {
        "files": {
            "a.py": {
                "executed_lines": [1, 2],
                "executed_branches": [[1, 2], [1, 3]],
                "excluded_lines": [4],
                "summary": {"num_statements": 3, "num_branches": 2},
            }
        }
    }
    after = deepcopy(before)
    assert coverage_losses(before, after, {"a.py"}) == []
    after["files"]["a.py"][field].pop()
    assert coverage_losses(before, after, {"a.py"})
    assert coverage_losses(before, after, set()) == []
