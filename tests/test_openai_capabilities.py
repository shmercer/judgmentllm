"""Generic serving behavior is unknown until controlled exact-scope evidence."""

from __future__ import annotations

from pathlib import Path

import pytest
from test_openai_sdk_adapter import authorize_fixture
from test_production_transport import case

from judgmentllm.planning import validate_run
from judgmentllm.production_transport import production_context
from judgmentllm.providers.openai import OpenAIAdapter


@pytest.mark.parametrize("endpoint", ["responses", "chat.completions"])
def test_unknown_features_obey_strict_capabilities(
    tmp_path: Path, endpoint: str
) -> None:
    spec, _, _ = case(tmp_path, endpoint=endpoint, explanations=True, grouped=True)
    profile = OpenAIAdapter.capabilities_for("openai", endpoint, spec.model)
    assert profile.explanations is profile.grouped is profile.ties is None
    assert profile.modes == ("live",)
    assert profile.generation is None
    report = validate_run(spec, capabilities=profile)
    issues = {
        issue.field: issue.severity
        for issue in report.issues
        if issue.code == "unknown_capability"
    }
    assert issues == {
        "execution.explanations": "warning",
        "execution.grouping": "warning",
    }
    assert report.valid
    strict = spec.model_copy(update={"strict_capabilities": True})
    assert not validate_run(strict, capabilities=profile).valid
    with pytest.raises(RuntimeError):
        OpenAIAdapter(endpoint, spec.model).validate(strict)


def test_one_layout_does_not_authorize_all_explanations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec, _, _ = case(tmp_path)
    authorize_fixture(
        monkeypatch,
        production_context(spec),
        ("rubric_explanations_together", "rubric_grouped", "pairwise_ties"),
    )
    profile = OpenAIAdapter.capabilities_for("openai", spec.endpoint, spec.model)
    assert profile.grouped is profile.ties is True
    assert profile.explanations is None
    other_model = OpenAIAdapter.capabilities_for(
        "openai", spec.endpoint, "unqualified-exact-model"
    )
    other_endpoint = OpenAIAdapter.capabilities_for(
        "openai", "chat.completions", spec.model
    )
    assert other_model.explanations is other_model.grouped is other_model.ties is None
    assert (
        other_endpoint.explanations
        is other_endpoint.grouped
        is other_endpoint.ties
        is None
    )


def test_all_explanation_features_are_needed_for_generic_positive_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec, _, _ = case(tmp_path)
    authorize_fixture(
        monkeypatch,
        production_context(spec),
        (
            "rubric_explanations_together",
            "rubric_scores_first",
            "rubric_explanations_first",
            "pairwise_explanations",
        ),
    )
    profile = OpenAIAdapter.capabilities_for("openai", spec.endpoint, spec.model)
    assert profile.explanations is True
    assert profile.grouped is profile.ties is None


@pytest.mark.parametrize("endpoint", ["responses", "chat.completions"])
def test_unknown_ties_obey_strict_policy(tmp_path: Path, endpoint: str) -> None:
    spec, _, _ = case(tmp_path, endpoint=endpoint, pairwise=True, ties=True)
    profile = OpenAIAdapter.capabilities_for("openai", endpoint, spec.model)
    report = validate_run(spec, capabilities=profile)
    assert report.valid
    assert any(
        issue.field == "allow_ties"
        and issue.code == "unknown_capability"
        and issue.severity == "warning"
        for issue in report.issues
    )
    strict = spec.model_copy(update={"strict_capabilities": True})
    assert not validate_run(strict, capabilities=profile).valid


def test_reviewed_unsupported_grouping_fails_even_nonstrict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from judgmentllm import production_registry

    spec, _, _ = case(tmp_path, grouped=True)
    authorize_fixture(monkeypatch, production_context(spec), ())
    original = production_registry._QUALIFIED_PROFILES[0]  # pyright: ignore[reportPrivateUsage]
    profile = original.model_copy(update={"unsupported_features": ("rubric_grouped",)})
    monkeypatch.setattr(production_registry, "_QUALIFIED_PROFILES", (profile,))
    capabilities = OpenAIAdapter.capabilities_for("openai", spec.endpoint, spec.model)
    assert capabilities.grouped is False
    assert not validate_run(spec, capabilities=capabilities).valid
