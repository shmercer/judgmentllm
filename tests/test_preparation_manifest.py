"""Complete manifest and verified rendering boundaries, without provider bodies."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from judgmentllm.config import RunSpec
from judgmentllm.persistence.artifacts import ArtifactStore
from judgmentllm.persistence.hashing import canonical_bytes
from judgmentllm.persistence.models import IntegrityError, ManifestReference, RunRecord
from judgmentllm.persistence.repository import Repository
from judgmentllm.preparation import open_prepared_run, prepare_run
from judgmentllm.preparation.manifest import ExecutableRunManifest


def _spec(tmp_path: Path, *, pairwise: bool, capture: str) -> RunSpec:
    responses = tmp_path / "responses.jsonl"
    responses.write_text(
        "\n".join(
            json.dumps({"item_id": identifier, "text": f"Private essay {identifier}"})
            for identifier in ("001", "002")
        )
        + "\n",
        encoding="utf-8",
    )
    data: dict[str, Any] = {
        "task_kind": "pairwise" if pairwise else "rubric",
        "input": str(responses),
        "provider": "synthetic",
        "endpoint": "offline",
        "model": "fixture",
        "capture": capture,
    }
    if pairwise:
        pairs = tmp_path / "pairs.jsonl"
        pairs.write_text(
            json.dumps(
                {
                    "pair_id": "001",
                    "pair_uid": "0001",
                    "left_item_id": "001",
                    "right_item_id": "002",
                    "trait_id": "quality",
                    "presented_order": ["002", "001"],
                }
            )
            + "\n"
        )
        data.update(
            pairs=str(pairs),
            criteria=[
                {"trait_id": "quality", "name": "Quality", "description": "Compare."}
            ],
        )
    else:
        data["rubric"] = {
            "rubric_id": "001",
            "name": "Rubric",
            "traits": [
                {
                    "trait_id": "quality",
                    "name": "Quality",
                    "description": "Assess.",
                    "levels": [{"label": "low"}, {"label": "high"}],
                }
            ],
        }
    return RunSpec.model_validate(data)


@pytest.mark.parametrize("pairwise", [False, True])
@pytest.mark.parametrize("capture", ["full", "minimal"])
def test_render_rechecks_dependency_commitment(
    tmp_path: Path, pairwise: bool, capture: str
) -> None:
    spec = _spec(tmp_path, pairwise=pairwise, capture=capture)
    with prepare_run(spec, store=tmp_path / "store", run_id="001") as prepared:
        dependency = next(prepared.iter_request_dependencies())
        rendered = prepared.render(dependency)
        assert "Private essay 001" in rendered.text
        if pairwise:
            assert rendered.text.index("Private essay 002") < rendered.text.index(
                "Private essay 001"
            )
        with pytest.raises(IntegrityError):
            prepared.render(
                dependency.model_copy(update={"dependency_sha256": "0" * 64})
            )
        dependency.request.trait_ids = ("other",)
        with pytest.raises(IntegrityError):
            prepared.render(dependency)


@pytest.mark.parametrize(
    "change", ["origin", "snapshot", "recovery", "sources", "traits", "kind"]
)
def test_complete_manifest_rejects_inconsistent_commitments(
    tmp_path: Path, change: str
) -> None:
    spec = _spec(tmp_path, pairwise=False, capture="full")
    with prepare_run(spec, store=tmp_path / "store", run_id="001") as prepared:
        data = json.loads(canonical_bytes(prepared.manifest))
    if change == "origin":
        data["configuration_origin"] = "file"
    elif change == "snapshot":
        data["sources"][0]["snapshot"]["sha256"] = "0" * 64
    elif change == "recovery":
        data["recovery_policy"] = "unchanged_originals"
    elif change == "sources":
        data["sources"] = []
    elif change == "traits":
        data["traits"] = []
    else:
        data["kind"] = "generation_manifest_fixture"
    with pytest.raises(ValidationError):
        ExecutableRunManifest.model_validate_json(json.dumps(data))


def test_generation_fixture_never_authorizes_executable_reopen(tmp_path: Path) -> None:
    with Repository(tmp_path) as repository:
        repository.create_run(
            RunRecord(
                run_id="001",
                task_kind="rubric",
                provider="fixture",
                endpoint="offline",
                model="fixture",
            )
        )
        artifacts = ArtifactStore(repository.run_directory("001"))
        identity = artifacts.put(
            b'{"kind":"generation_manifest_fixture"}', "application/json"
        )
        artifacts.publish_manifest(identity)
        repository.seal_run("001", ManifestReference(artifact=identity))
    with pytest.raises(IntegrityError):
        open_prepared_run("001", store=tmp_path)


def test_mutated_prepared_context_fails_with_safe_diagnostics(tmp_path: Path) -> None:
    spec = _spec(tmp_path, pairwise=False, capture="minimal")
    with prepare_run(spec, store=tmp_path / "store", run_id="001") as prepared:
        assert prepared.spec.rubric is not None
        prepared.spec.rubric.traits[0].levels.clear()
        with pytest.raises(IntegrityError) as error:
            next(prepared.iter_request_dependencies())
        assert "Private essay" not in str(error.value)


def test_mutated_nested_manifest_context_fails(tmp_path: Path) -> None:
    spec = _spec(tmp_path, pairwise=False, capture="minimal")
    with prepare_run(spec, store=tmp_path / "store", run_id="001") as prepared:
        prepared.manifest.input.allow_empty_text = True
        with pytest.raises(IntegrityError):
            next(prepared.iter_request_dependencies())
