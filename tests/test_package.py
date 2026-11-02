"""Offline checks for the installed package and its public distribution contract."""

from importlib import metadata, resources

import pytest
from packaging.requirements import Requirement
from packaging.version import Version

import judgmentllm

FEATURE_DEPENDENCIES = {
    "openai": {"openai"},
    "anthropic": {"anthropic"},
    "google": {"google-genai"},
    "together": {"together"},
    "ollama": {"ollama"},
    "typesafe": {"typesafe-sdk"},
    "parquet": {"pyarrow"},
    "api": {"fastapi", "uvicorn"},
}


def extra_requirements(extra: str) -> set[tuple[str, str]]:
    """Return dependencies activated by this extra, without core requirements."""
    requirements = metadata.requires("judgmentllm")
    assert requirements is not None
    parsed = [Requirement(value) for value in requirements]
    return {
        (requirement.name, str(requirement.specifier))
        for requirement in parsed
        if requirement.marker is not None
        and requirement.marker.evaluate({"extra": extra})
        and not requirement.marker.evaluate({"extra": ""})
    }


def test_package_version_matches_installed_distribution() -> None:
    assert Version(judgmentllm.__version__) == Version(metadata.version("judgmentllm"))


def test_package_declares_python_minimum_and_typing() -> None:
    package_metadata = metadata.metadata("judgmentllm")
    assert package_metadata["Requires-Python"] == ">=3.11"
    assert resources.files("judgmentllm").joinpath("py.typed").is_file()


def test_core_dependencies_match_contract() -> None:
    requirements = metadata.requires("judgmentllm")
    assert requirements is not None
    core = {
        requirement.name
        for value in requirements
        if (requirement := Requirement(value)).marker is None
    }
    assert {name.lower() for name in core} == {"typer", "pydantic", "pyyaml", "jinja2"}


def test_distribution_exposes_requested_extras() -> None:
    extras = metadata.metadata("judgmentllm").get_all("Provides-Extra")
    assert set(extras or []) == set(FEATURE_DEPENDENCIES) | {"all"}


@pytest.mark.parametrize("extra", FEATURE_DEPENDENCIES)
def test_feature_extra_dependencies(extra: str) -> None:
    assert {name for name, _ in extra_requirements(extra)} == FEATURE_DEPENDENCIES[
        extra
    ]


def test_all_extra_combines_feature_dependencies() -> None:
    expected = {
        requirement
        for extra in FEATURE_DEPENDENCIES
        for requirement in extra_requirements(extra)
    }
    assert extra_requirements("all") == expected


def test_console_entry_point_is_loadable() -> None:
    entry_points = list(
        metadata.entry_points(group="console_scripts", name="judgmentllm")
    )
    assert len(entry_points) == 1
    assert callable(entry_points[0].load())
