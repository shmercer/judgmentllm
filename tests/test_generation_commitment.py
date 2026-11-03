"""Frozen generation intent without claiming executable manifests or resume."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from judgmentllm.config import GenerationSpec, RunSpec
from judgmentllm.generation import (
    GenerationCapabilities,
    GenerationParameterCapability,
)
from judgmentllm.persistence.artifacts import ArtifactStore
from judgmentllm.persistence.generation import (
    GenerationCommitment,
    build_generation_commitment,
    validate_generation_compatibility,
)
from judgmentllm.persistence.hashing import bytes_digest, canonical_bytes
from judgmentllm.persistence.models import IntegrityError, ManifestReference, RunRecord
from judgmentllm.persistence.repository import Repository
from judgmentllm.planning import PreflightError


def spec(generation: object = None) -> RunSpec:
    return RunSpec.model_validate(
        {
            "task_kind": "rubric",
            "input": "synthetic.csv",
            "provider": "fixture",
            "endpoint": "generative",
            "model": "synthetic-v1",
            "generation": generation,
            "rubric": {
                "rubric_id": "001",
                "name": "Synthetic rubric",
                "traits": [
                    {
                        "trait_id": "001",
                        "name": "Quality",
                        "description": "Synthetic criterion",
                        "levels": [{"label": "low"}, {"label": "high"}],
                    }
                ],
            },
        }
    )


def supported(**updates: Any) -> GenerationCapabilities:
    return GenerationCapabilities.model_validate(
        {
            "provider": "fixture",
            "endpoint": "generative",
            "model": "synthetic-v1",
            "temperature": {"supported": True},
            "top_p": {"supported": True},
            "max_output_tokens": {"supported": True},
            "source": "synthetic-offline-facts",
            **updates,
        }
    )


def test_sparse_roundtrip_and_immutable_snapshot() -> None:
    current = spec({"temperature": 0, "top_p": 0.9, "max_output_tokens": 127})
    original = supported()
    commitment = build_generation_commitment(current, capabilities=original)
    data = json.loads(commitment.model_dump_json())
    assert data["settings"] == {
        "temperature": 0,
        "top_p": 0.9,
        "max_output_tokens": 127,
    }
    assert (
        GenerationCommitment.model_validate_json(commitment.model_dump_json())
        == commitment
    )
    assert commitment.capabilities == original
    assert commitment.capabilities is not original
    assert commitment.settings is not current.generation
    assert commitment.capabilities is not None
    assert not commitment.capabilities.fixture_tested
    assert not commitment.capabilities.live_qualified
    with pytest.raises(ValidationError, match="frozen"):
        commitment.model = "other"
    assert commitment.settings is not None
    with pytest.raises(ValidationError, match="frozen"):
        commitment.settings.temperature = 1
    with pytest.raises(ValidationError, match="frozen"):
        commitment.capabilities.temperature.supported = False


@pytest.mark.parametrize("generation", [None, {}, {"temperature": None, "top_p": None}])
def test_omitted_null_empty_have_same_normalized_commitment(generation: object) -> None:
    expected = build_generation_commitment(spec())
    actual = build_generation_commitment(spec(generation))
    assert actual == expected
    assert json.loads(actual.model_dump_json())["settings"] == {}
    assert actual.settings is None
    assert actual.capabilities is None
    assert validate_generation_compatibility(expected, spec(generation)).valid
    raw = json.dumps({"generation": generation}).encode()
    assert bytes_digest(raw) != bytes_digest(b"{}")


def test_generation_identity_excludes_execution_orientation_and_capability_evidence() -> (
    None
):
    current = spec({"temperature": 0})
    original = build_generation_commitment(current, capabilities=supported())
    current.seed = 23
    current.execution.concurrency = 9
    fresh_facts = supported(source="updated-offline-facts", fixture_tested=True)
    updated = build_generation_commitment(current, capabilities=fresh_facts)
    assert original.requested_sha256 == updated.requested_sha256
    assert canonical_bytes(original) != canonical_bytes(updated)
    assert validate_generation_compatibility(
        original, current, capabilities=fresh_facts
    ).valid
    assert original.capabilities is not None
    assert original.capabilities.source == "synthetic-offline-facts"
    assert not original.capabilities.fixture_tested


@pytest.mark.parametrize(
    "generation",
    [{}, {"temperature": 1}, {"temperature": 0, "top_p": 1}, {"max_output_tokens": 1}],
)
def test_changed_or_removed_settings_block_compatibility(generation: object) -> None:
    original = build_generation_commitment(spec({"temperature": 0}))
    report = validate_generation_compatibility(original, spec(generation))
    assert not report.valid
    assert any(issue.code == "generation_settings_changed" for issue in report.issues)
    changed = build_generation_commitment(spec(generation))
    assert changed.requested_sha256 != original.requested_sha256


@pytest.mark.parametrize("field", ["provider", "endpoint", "model"])
def test_changed_scope_blocks_compatibility(field: str) -> None:
    original = build_generation_commitment(spec({"temperature": 0}))
    changed = spec({"temperature": 0}).model_copy(update={field: "other"})
    report = validate_generation_compatibility(original, changed)
    assert not report.valid
    assert any(issue.code == "generation_scope_changed" for issue in report.issues)


def test_current_restrictions_override_original_capability_snapshot() -> None:
    current = spec({"temperature": 1, "top_p": 1})
    frozen = build_generation_commitment(current, capabilities=supported())
    profiles = [
        supported(temperature={"supported": False}),
        supported(temperature={"supported": True, "maximum": 0.5}),
        supported(incompatible_combinations=(("temperature", "top_p"),)),
        supported(model="other-model"),
    ]
    for profile in profiles:
        assert not validate_generation_compatibility(
            frozen, current, capabilities=profile
        ).valid
        with pytest.raises(PreflightError):
            build_generation_commitment(current, capabilities=profile)
    assert frozen.capabilities == supported()
    unknown = validate_generation_compatibility(frozen, current)
    assert unknown.valid
    assert all(issue.code == "unknown_capability" for issue in unknown.issues)
    assert len(unknown.issues) == 2
    current.strict_capabilities = True
    assert not validate_generation_compatibility(frozen, current).valid
    with pytest.raises(PreflightError, match="unknown"):
        build_generation_commitment(current)


def test_reject_inconsistent_digest_scope_and_copied_mutations() -> None:
    current = spec({"temperature": 0})
    frozen = build_generation_commitment(current, capabilities=supported())
    with pytest.raises(ValidationError, match="digest"):
        GenerationCommitment.model_validate(
            {**frozen.model_dump(), "requested_sha256": "a" * 64}
        )
    with pytest.raises(ValidationError, match="exact scope"):
        GenerationCommitment.model_validate(
            {
                **frozen.model_dump(),
                "capabilities": supported(model="other").model_dump(),
            }
        )
    invalid_settings = GenerationSpec(temperature=0).model_copy(
        update={"unknown": "private-sentinel"}
    )
    invalid_facts = supported().model_copy(update={"unknown": "private-sentinel"})
    invalid_rule = GenerationParameterCapability(supported=True).model_copy(
        update={"maximum": "invalid"}
    )
    for bad in [
        frozen.model_copy(update={"unknown": "private-sentinel"}),
        frozen.model_copy(update={"settings": invalid_settings}),
        frozen.model_copy(update={"capabilities": invalid_facts}),
        frozen.model_copy(
            update={
                "capabilities": supported().model_copy(
                    update={"temperature": invalid_rule}
                )
            }
        ),
    ]:
        with pytest.raises(ValueError):
            validate_generation_compatibility(bad, current)
        with pytest.raises(ValueError, match="canonically") as error:
            canonical_bytes(bad)
        assert "private-sentinel" not in str(error.value)
    with pytest.raises(ValueError):
        build_generation_commitment(
            current.model_copy(update={"generation": invalid_settings})
        )
    with pytest.raises(ValueError):
        build_generation_commitment(current, capabilities=invalid_facts)


def test_manifest_section_survives_sealing_and_reopening(tmp_path: Path) -> None:
    commitment = build_generation_commitment(spec({"max_output_tokens": 63}))
    fixture = {"kind": "generation_manifest_fixture", "generation": commitment}
    data = canonical_bytes(fixture)
    with Repository(tmp_path) as repository:
        repository.create_run(
            RunRecord(
                run_id="001",
                task_kind="rubric",
                provider="fixture",
                endpoint="generative",
                model="synthetic-v1",
            )
        )
        identity = ArtifactStore(repository.run_directory("001")).put(
            data, "application/json"
        )
        repository.seal_run("001", ManifestReference(artifact=identity))
        with pytest.raises(IntegrityError):
            repository.seal_run("001", ManifestReference(artifact=identity))
    with Repository(tmp_path) as repository:
        retained = repository.get_manifest("001").artifact
        path = ArtifactStore(repository.run_directory("001")).verify(retained)
        assert path is not None
        saved = json.loads(path.read_bytes())
        assert saved["kind"] == "generation_manifest_fixture"
        restored = GenerationCommitment.model_validate_json(
            json.dumps(saved["generation"])
        )
        assert restored == commitment
        assert validate_generation_compatibility(
            restored, spec({"max_output_tokens": 63})
        ).valid
        assert not validate_generation_compatibility(
            restored, spec({"max_output_tokens": 64})
        ).valid
        assert list(repository.iter_results("001")) == []


def test_requested_digest_is_stable_across_processes() -> None:
    current = spec({"temperature": 0, "top_p": 0.8})
    expected = build_generation_commitment(current).requested_sha256
    program = (
        "import sys; from judgmentllm.config import RunSpec; "
        "from judgmentllm.persistence.generation import build_generation_commitment; "
        "print(build_generation_commitment(RunSpec.model_validate_json(sys.stdin.read())).requested_sha256)"
    )
    completed = subprocess.run(
        [sys.executable, "-c", program],
        input=current.model_dump_json(),
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    assert completed.stdout.strip() == expected
    assert completed.stderr == ""
