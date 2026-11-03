"""Offline generation intent, scoped preflight, and Milestone 1 compatibility."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from judgmentllm import GenerationSpec
from judgmentllm.config import ConfigurationError, RunSpec, load_run_spec
from judgmentllm.generation import GenerationCapabilities, GenerationParameterCapability
from judgmentllm.io import write_jsonl
from judgmentllm.models import PairTask, ResponseItem
from judgmentllm.persistence.hashing import (
    bytes_digest,
    canonical_bytes,
    semantic_digest,
)
from judgmentllm.planning import (
    CapabilityProfile,
    PreflightError,
    iter_requests,
    iter_tasks,
    plan_run,
    prepare_pairs,
    validate_generation,
    validate_run,
)


def run_spec(**updates: object) -> RunSpec:
    data: dict[str, object] = {
        "task_kind": "rubric",
        "input": "responses.csv",
        "rubric": {
            "rubric_id": "r",
            "name": "Synthetic",
            "traits": [
                {
                    "trait_id": name,
                    "name": name,
                    "description": "Judge synthetic structure.",
                    "levels": [{"label": 1}, {"label": 2}, {"label": 3}],
                }
                for name in ("a", "b", "c")
            ],
        },
        "provider": "synthetic",
        "endpoint": "categorical",
        "model": "unknown-model",
    }
    data.update(updates)
    return RunSpec.model_validate(data)


def capabilities(**updates: object) -> GenerationCapabilities:
    data: dict[str, object] = {
        "provider": "synthetic",
        "endpoint": "categorical",
        "model": "unknown-model",
        "temperature": {"supported": True},
        "top_p": {"supported": True},
        "max_output_tokens": {"supported": True},
    }
    data.update(updates)
    return GenerationCapabilities.model_validate(data)


@pytest.mark.parametrize(
    "settings",
    [
        {"temperature": 0},
        {"temperature": 100.25},
        {"top_p": 0},
        {"top_p": 1},
        {"top_p": 0.123456789},
        {"max_output_tokens": 1},
    ],
)
def test_explicit_values_survive_without_defaults(settings: dict[str, object]) -> None:
    generation = GenerationSpec.model_validate(settings)
    assert generation.model_dump(mode="json") == settings
    assert json.loads(generation.model_dump_json()) == settings
    assert (
        GenerationSpec.model_validate_json(generation.model_dump_json()) == generation
    )
    spec = run_spec(generation=settings)
    assert spec.model_dump(mode="json")["generation"] == settings
    assert spec.generation is not None
    for key, value in settings.items():
        assert type(getattr(spec.generation, key)) is type(value)


@pytest.mark.parametrize(
    "settings",
    [
        {"temperature": -0.01},
        {"temperature": True},
        {"temperature": "0.5"},
        {"temperature": float("nan")},
        {"temperature": float("inf")},
        {"top_p": -0.01},
        {"top_p": 1.01},
        {"top_p": False},
        {"top_p": "1"},
        {"top_p": float("-inf")},
        {"max_output_tokens": 0},
        {"max_output_tokens": -1},
        {"max_output_tokens": 1.0},
        {"max_output_tokens": True},
        {"max_output_tokens": "256"},
        {"max_output_tokens": float("inf")},
        {"top_k": 2},
        {"stop_sequences": ["END"]},
        {"frequency_penalty": 0.1},
        {"presence_penalty": 0.1},
        {"seed": 42},
        {"reasoning": {"effort": "high"}},
        {"thinking": {"budget": 100}},
        {"provider_options": {"anything": 1}},
        {"unknown": None},
    ],
)
def test_invalid_or_unimplemented_controls_are_rejected(
    settings: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        run_spec(generation=settings)


@pytest.mark.parametrize(
    "settings",
    [
        None,
        {},
        {"temperature": None},
        {"temperature": None, "top_p": None, "max_output_tokens": None},
    ],
)
def test_unspecified_preserves_exact_milestone_one_contract(settings: object) -> None:
    legacy = run_spec()
    spec = run_spec(generation=settings)
    assert spec.generation is None
    assert spec.model_dump() == legacy.model_dump()
    assert spec.model_dump_json() == legacy.model_dump_json()
    assert "generation" not in spec.model_dump()
    assert semantic_digest("run-config", spec) == (
        "219ac52d67f5ed210a174305982aa154d9e0873610aaa486b1e100b2a98a195e"
    )
    response = ResponseItem(item_id="001", text="Synthetic response")
    requests = list(iter_requests(spec, [response]))
    assert [request.request_uid for request in requests] == [
        "request-c5e4842ca182ee73ce002abe034689203a682a06ee01931b150cd7b841772351",
        "request-698735e0adbf1f178a7907e30dc29c2a3a1e19ac6af742a45916034dec1c2041",
        "request-510418cb93413c2c0931176d10557656639fc697a7d3121f41f24a116d033355",
    ]
    assert [task.task_id for request in requests for task in request.tasks] == [
        "task-5dd5d534b7c898943f7a5b7546c592ed59e14a27b27d4362a43341005533ae82",
        "task-dd0bcbec681c064aa44b164078acb532d0ab42a5a5d0b9ded6eb5a563dc7f766",
        "task-cae68883c02cefa177bbaabf0dfe79ca9b271033c88c32d3e1ab8f1e47ad0a3e",
    ]
    assert validate_run(spec).model_dump() == {
        "issues": (
            {
                "code": "unknown_capability",
                "field": "execution.mode",
                "message": "Execution mode support is unknown; this is an offline structural plan",
                "severity": "warning",
            },
        )
    }
    spec.strict_capabilities = True
    assert validate_run(spec).issues[0].severity == "error"
    mismatched = capabilities(model="different", temperature={"supported": False})
    assert (
        validate_run(
            spec, capabilities=CapabilityProfile(modes=("live",), generation=mismatched)
        ).issues
        == ()
    )
    assert validate_generation(spec, capabilities=mismatched).issues == ()
    assert CapabilityProfile().model_dump() == {
        "explanations": None,
        "grouped": None,
        "ties": None,
        "modes": None,
        "max_levels": None,
        "max_traits_per_request": None,
    }


def test_sparse_configuration_loading_and_exact_source_provenance(
    tmp_path: Path,
) -> None:
    data = run_spec().model_dump(mode="json")
    source_hashes: set[str] = set()
    identities: set[str] = set()
    request_ids: set[str] = set()
    for index, generation in enumerate((None, {}, {"temperature": None})):
        data["generation"] = generation
        path = tmp_path / f"run-{index}.yaml"
        path.write_text(yaml.safe_dump(data), encoding="utf-8")
        spec = load_run_spec(path)
        source_hashes.add(bytes_digest(path.read_bytes()))
        identities.add(semantic_digest("run-config", spec))
        request_ids.add(
            next(
                iter_requests(spec, [ResponseItem(item_id="001", text="x")])
            ).request_uid
        )
    assert len(source_hashes) == 3
    assert len(identities) == len(request_ids) == 1
    data["generation"] = {"temperature": 0, "top_p": None, "max_output_tokens": 257}
    for suffix in ("json", "yaml"):
        path = tmp_path / f"explicit.{suffix}"
        path.write_text(
            json.dumps(data) if suffix == "json" else yaml.safe_dump(data),
            encoding="utf-8",
        )
        spec = load_run_spec(path)
        assert spec.generation == GenerationSpec(temperature=0, max_output_tokens=257)
        assert spec.model_dump(mode="json")["generation"] == {
            "temperature": 0,
            "max_output_tokens": 257,
        }
    data["generation"] = {"top_k": 5}
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ConfigurationError, match="generation.top_k"):
        load_run_spec(path)


def test_explicit_values_change_request_context_not_tasks_or_pair_orientation() -> None:
    response = ResponseItem(item_id="001", text="Synthetic response")
    specs = [
        run_spec(),
        *(
            run_spec(generation=settings)
            for settings in (
                {"temperature": 0},
                {"temperature": 0.5},
                {"top_p": 0},
                {"top_p": 0.5},
                {"max_output_tokens": 1},
                {"max_output_tokens": 2},
            )
        ),
    ]
    assert len({semantic_digest("run-config", spec) for spec in specs}) == len(specs)
    assert len(
        {next(iter_requests(spec, [response])).request_uid for spec in specs}
    ) == len(specs)
    assert all(
        list(iter_tasks(spec, [response])) == list(iter_tasks(specs[0], [response]))
        for spec in specs
    )
    pair = PairTask(
        pair_id="comparison",
        pair_uid="occurrence",
        left_item_id="left",
        right_item_id="right",
        trait_id="a",
    )
    pair_data: dict[str, object] = {
        "task_kind": "pairwise",
        "input": "responses.csv",
        "pairs": "pairs.jsonl",
        "criteria": [
            {"trait_id": "a", "name": "A", "description": "Compare synthetic texts."}
        ],
        "provider": "synthetic",
        "endpoint": "choice",
        "model": "unknown-model",
    }
    pair_spec = RunSpec.model_validate(pair_data)
    assert (
        next(iter_requests(pair_spec, [pair])).request_uid
        == "request-f95d63b7ac4cd849c4011db8d7950b00e1c8bccc2bc149c1105fba62e8cdc4ad"
    )
    base = list(prepare_pairs([pair], seed=42, include_reversals=True))
    pair_data.update(seed=42, generation={"temperature": 0})
    changed = RunSpec.model_validate(pair_data)
    assert (
        list(prepare_pairs([pair], seed=changed.seed, include_reversals=True)) == base
    )
    assert (
        next(iter_requests(changed, [pair])).request_uid
        != next(iter_requests(pair_spec, [pair])).request_uid
    )
    assert changed.generation is not None and not hasattr(changed.generation, "seed")
    with pytest.raises(ValidationError):
        run_spec(execution={"temperature": 0})


def test_only_requested_unknowns_and_existing_strict_policy() -> None:
    spec = run_spec(generation={"temperature": 0})
    report = validate_generation(spec)
    assert report.valid
    assert [(i.field, i.code, i.severity) for i in report.issues] == [
        ("generation.temperature", "unknown_capability", "warning")
    ]
    spec.strict_capabilities = True
    assert not validate_generation(spec).valid
    with pytest.raises(PreflightError, match="qualification"):
        plan_run(spec, capabilities=CapabilityProfile(modes=("live",)))
    assert plan_run(
        spec, capabilities=CapabilityProfile(modes=("live",), generation=capabilities())
    )
    assert not capabilities().fixture_tested and not capabilities().live_qualified


@pytest.mark.parametrize("name", ["provider", "endpoint", "model"])
def test_capability_scope_must_match_exactly(name: str) -> None:
    report = validate_generation(
        run_spec(generation={"top_p": 1}),
        capabilities=capabilities(**{name: "another"}),
    )
    assert not report.valid
    assert report.issues[0].code == "capability_scope_mismatch"


def test_known_restrictions_and_evidence_do_not_change_request_identity() -> None:
    spec = run_spec(generation={"temperature": 0, "top_p": 1, "max_output_tokens": 257})
    response = ResponseItem(item_id="001", text="Synthetic response")
    first = next(
        iter_requests(
            spec, [response], capabilities=CapabilityProfile(generation=capabilities())
        )
    )
    newer = capabilities(source="Synthetic fixture 2026-11-02", fixture_tested=True)
    assert (
        next(
            iter_requests(
                spec, [response], capabilities=CapabilityProfile(generation=newer)
            )
        ).request_uid
        == first.request_uid
    )
    unsupported = capabilities(temperature={"supported": False})
    assert (
        validate_generation(spec, capabilities=unsupported).issues[0].code
        == "unsupported"
    )
    with pytest.raises(PreflightError, match="unsupported"):
        next(
            iter_requests(
                spec, [response], capabilities=CapabilityProfile(generation=unsupported)
            )
        )
    bounded = capabilities(
        temperature={"supported": True, "minimum": 0.1},
        max_output_tokens={"supported": True, "maximum": 256},
    )
    assert [
        i.field for i in validate_generation(spec, capabilities=bounded).issues
    ] == ["generation.temperature", "generation.max_output_tokens"]
    bounded_issues = validate_generation(spec, capabilities=bounded).issues
    assert "minimum=0.1" in bounded_issues[0].message
    assert "maximum=256" in bounded_issues[1].message
    boundary = capabilities(
        temperature={"supported": True, "minimum": 0, "maximum": 0},
        top_p={"supported": True, "minimum": 1},
        max_output_tokens={"supported": True, "maximum": 257},
    )
    assert validate_generation(spec, capabilities=boundary).valid
    combinations = capabilities(incompatible_combinations=(("temperature", "top_p"),))
    assert (
        validate_generation(spec, capabilities=combinations).issues[0].code
        == "incompatible_generation"
    )
    assert validate_generation(
        run_spec(generation={"temperature": 0, "top_p": None}),
        capabilities=combinations,
    ).valid
    # Known limits are still enforceable when support itself remains unknown.
    assert not validate_generation(
        spec, capabilities=capabilities(max_output_tokens={"maximum": 256})
    ).valid


@pytest.mark.parametrize(
    "values",
    [
        {"minimum": 2, "maximum": 1},
        {"minimum": True},
        {"maximum": "1"},
        {"maximum": float("inf")},
        {"supported": "yes"},
        {"unknown": 1},
    ],
)
def test_capability_rules_are_strict(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        GenerationParameterCapability.model_validate(values)


@pytest.mark.parametrize(
    "combination",
    [("temperature",), ("temperature", "temperature"), ("temperature", "seed")],
)
def test_capability_combinations_are_typed(combination: tuple[str, ...]) -> None:
    with pytest.raises(ValidationError):
        capabilities(incompatible_combinations=(combination,))


def test_mutations_cannot_hide_controls_from_hash_or_validation(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        run_spec(generation=GenerationSpec().model_copy(update={"unknown": 1}))
    invalid = run_spec().model_copy(
        update={
            "generation": GenerationSpec(temperature=0).model_copy(
                update={"unknown": 1}
            )
        }
    )
    with pytest.raises(ValueError):
        canonical_bytes(invalid)
    with pytest.raises(ValueError):
        validate_generation(invalid)
    with pytest.raises(ValueError):
        write_jsonl(tmp_path / "invalid.jsonl", [invalid])
    repaired = run_spec().model_copy(update={"generation": GenerationSpec()})
    with pytest.raises(ValueError):
        canonical_bytes(repaired)
    mutated_facts = capabilities().model_copy(update={"untyped": {"anything": 1}})
    with pytest.raises(ValueError):
        validate_generation(
            run_spec(generation={"temperature": 0}), capabilities=mutated_facts
        )
    generation = GenerationSpec(temperature=0)
    with pytest.raises(ValidationError, match="frozen"):
        generation.temperature = 1
    with pytest.raises(ValidationError, match="frozen"):
        capabilities().temperature.supported = False


def test_generation_hashes_are_stable_in_fresh_processes() -> None:
    program = "from judgmentllm import GenerationSpec; from judgmentllm.persistence.hashing import semantic_digest; print(semantic_digest('generation-settings', GenerationSpec(temperature=0, top_p=0.95, max_output_tokens=257)))"
    expected = semantic_digest(
        "generation-settings",
        GenerationSpec(temperature=0, top_p=0.95, max_output_tokens=257),
    )
    for hash_seed in ("0", "random"):
        result = subprocess.run(
            [sys.executable, "-c", program],
            env={**os.environ, "PYTHONHASHSEED": hash_seed},
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        assert result.stdout.strip() == expected
