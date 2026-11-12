"""Shared assessment construction keeps mutable inputs local to each test."""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from pathlib import Path

import pytest
from anthropic_fixture import case as anthropic_case
from gemini_fixture import case as gemini_case
from test_production_transport import case as openai_case
from vertex_fixture import case as vertex_case

from judgmentllm.config import RunSpecV3
from judgmentllm.models import ResponseItem
from judgmentllm.preparation.session import RequestDependency


@pytest.mark.parametrize(
    "case", [openai_case, anthropic_case, gemini_case, vertex_case]
)
@pytest.mark.parametrize("pairwise", [False, True])
def test_case_calls_do_not_share_mutable_inputs(
    tmp_path: Path,
    case: Callable[..., tuple[RunSpecV3, RequestDependency, dict[str, ResponseItem]]],
    pairwise: bool,
) -> None:
    expected = deepcopy(case(tmp_path, pairwise=pairwise))
    spec, dependency, responses = case(tmp_path, pairwise=pairwise)
    spec.model = "changed"
    responses["001"].text = "changed"
    if spec.rubric is not None:
        spec.rubric.traits[0].name = "changed"
    if dependency.request.pair is not None:
        dependency.request.pair.metadata["changed"] = True
    assert case(tmp_path, pairwise=pairwise) == expected
