"""Experimental admission is narrower than documented syntax and never production authority."""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest
from study_fixture import PACKAGE, SOURCE
from test_study_native import offline as offline

from judgmentllm.config import GenerationSpec
from judgmentllm.preparation import PreparationError, prepare_run
from judgmentllm.providers.protocol import ProviderError
from judgmentllm.study.admission import candidate_context
from judgmentllm.study.plans import selected_configuration, suite_selections


@pytest.mark.parametrize(
    "change",
    [
        {"max_output_tokens": 4096},
        {"temperature": 0.3},
        {"tier": "default"},
        {"explanations": True},
        {"grouping": "independent"},
        {"model": "unlisted-model"},
        {"endpoint": "chat.completions"},
        {"account_scope": None},
    ],
)
def test_study_control_scope_cannot_be_widened(change: dict[str, Any]) -> None:
    _, spec = selected_configuration(
        PACKAGE, suite_selections("reasoning_comparison")[1]
    )
    assert spec.generation is not None
    settings = spec.generation.model_dump()
    for key, value in change.items():
        if key in {"max_output_tokens", "temperature"}:
            spec.generation = GenerationSpec.model_validate(settings | change)
        elif key in {"tier", "explanations", "grouping"}:
            spec.execution = spec.execution.model_copy(update=change)
        else:
            spec = spec.model_copy(update={key: value})
    with pytest.raises((ValueError, ProviderError)):
        candidate_context(spec, SOURCE)


def test_every_explicit_candidate_is_denied_by_ordinary_production_before_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def deny(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("Ordinary production preflight resolved credentials")

    monkeypatch.setattr("judgmentllm._auth._resolve_credentials", deny)
    for suite in ("reasoning_comparison", "temperature_acceptance"):
        for index, selection in enumerate(suite_selections(suite)):
            _, spec = selected_configuration(PACKAGE, selection)
            module = importlib.import_module("judgmentllm.providers." + spec.provider)
            adapter = getattr(
                module,
                {
                    "openai": "OpenAIAdapter",
                    "anthropic": "AnthropicAdapter",
                    "gemini": "GeminiAdapter",
                }[spec.provider],
            )(spec.endpoint, spec.model)
            with pytest.raises(PreparationError):
                with prepare_run(
                    spec,
                    store=tmp_path / suite / str(index),
                    run_id="001",
                    transport_context=adapter.transport_context,
                    capabilities=adapter.capabilities(spec),
                ):
                    pytest.fail("Unexpected production native admission")
