"""Offline generation manifest-section fixture with separate persistence/export.

The retained documents are explicitly generation_manifest_fixture records, not
executable manifests. This tests actual Python configuration and commitment
outputs without source freezing, provider builders, or submission.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Literal

from judgmentllm.config import RunSpec
from judgmentllm.generation import GenerationCapabilities, GenerationParameterCapability
from judgmentllm.io import write_jsonl
from judgmentllm.models import ID, ResponseItem
from judgmentllm.persistence.artifacts import ArtifactStore
from judgmentllm.persistence.generation import (
    GenerationCommitment,
    build_generation_commitment,
    validate_generation_compatibility,
)
from judgmentllm.persistence.hashing import (
    bytes_digest,
    canonical_bytes,
    semantic_digest,
)
from judgmentllm.persistence.models import (
    SHA256,
    ExecutionModel,
    ManifestReference,
    RunRecord,
)
from judgmentllm.persistence.repository import Repository
from judgmentllm.planning import iter_requests


class GenerationManifestFixture(ExecutionModel):
    """Synthetic wrapper, deliberately distinct from executable run manifests."""

    kind: Literal["generation_manifest_fixture"] = "generation_manifest_fixture"
    run_id: ID
    configuration_source_sha256: SHA256
    normalized_configuration_sha256: SHA256
    request_uid: ID
    generation: GenerationCommitment


def configuration(generation: object, *, omit: bool = False) -> bytes:
    data: dict[str, object] = {
        "task_kind": "rubric",
        "input": "synthetic.csv",
        "provider": "fixture",
        "endpoint": "generative",
        "model": "synthetic-v1",
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
    if not omit:
        data["generation"] = generation
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


def persist(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    generations: list[object] = [
        {"temperature": 0, "top_p": 0.8, "max_output_tokens": 127},
        {"temperature": 0},
        None,
        None,
        {},
        {"temperature": None, "top_p": None, "max_output_tokens": None},
    ]
    with Repository(output / "store") as repository:
        for index, generation in enumerate(generations, start=1):
            run_id = f"{index:03d}"
            source = configuration(generation, omit=index == 3)
            (output / f"config_{run_id}.json").write_bytes(source)
            spec = RunSpec.model_validate_json(source)
            profile = (
                GenerationCapabilities(
                    provider=spec.provider,
                    endpoint=spec.endpoint,
                    model=spec.model,
                    temperature=GenerationParameterCapability(supported=True),
                    top_p=GenerationParameterCapability(supported=True),
                    max_output_tokens=GenerationParameterCapability(supported=True),
                    source="offline-synthetic-fixture",
                    fixture_tested=True,
                )
                if index == 1
                else None
            )
            commitment = build_generation_commitment(spec, capabilities=profile)
            request = next(
                iter_requests(
                    spec, [ResponseItem(item_id="001", text="Synthetic response")]
                )
            )
            fixture = GenerationManifestFixture(
                run_id=run_id,
                configuration_source_sha256=bytes_digest(source),
                normalized_configuration_sha256=semantic_digest(
                    "fixture-run-config", spec
                ),
                request_uid=request.request_uid,
                generation=commitment,
            )
            repository.create_run(
                RunRecord(
                    run_id=run_id,
                    task_kind="rubric",
                    provider=spec.provider,
                    endpoint=spec.endpoint,
                    model=spec.model,
                )
            )
            retained = ArtifactStore(repository.run_directory(run_id)).put(
                canonical_bytes(fixture), "application/json"
            )
            repository.seal_run(run_id, ManifestReference(artifact=retained))


def export(output: Path) -> None:
    rows: list[GenerationManifestFixture] = []
    with Repository(output / "store") as repository:
        for index in range(1, 7):
            run_id = f"{index:03d}"
            retained = repository.get_manifest(run_id).artifact
            path = ArtifactStore(repository.run_directory(run_id)).verify(retained)
            assert path is not None
            fixture = GenerationManifestFixture.model_validate_json(path.read_bytes())
            source = (output / f"config_{run_id}.json").read_bytes()
            spec = RunSpec.model_validate_json(source)
            assert fixture.configuration_source_sha256 == bytes_digest(source)
            assert fixture.normalized_configuration_sha256 == semantic_digest(
                "fixture-run-config", spec
            )
            assert validate_generation_compatibility(fixture.generation, spec).valid
            rows.append(fixture)
    write_jsonl(output / "generation_manifests.jsonl", rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("persist", "export"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    options = parser.parse_args()
    if options.phase == "persist":
        persist(options.output)
    else:
        export(options.output)


if __name__ == "__main__":
    main()
