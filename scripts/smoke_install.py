"""Install built distributions into clean uv environments and verify the core CLI.

Run after syncing development dependencies and building distributions. Installation
may download packages at the core dependency versions exported from uv.lock.
Constraint export and installed-package checks remain offline.
"""

from __future__ import annotations

import argparse
import glob
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from click import unstyle

VERIFY_INSTALLED = """
import importlib.metadata as metadata
import importlib.resources as resources
from pathlib import Path
import sys

import judgmentllm
from judgmentllm import Rubric, ResponseItem, render_prompt, normalize_rubric_result
from judgmentllm.prompts import get_template, list_templates
import json

rubric = Rubric.model_validate({"rubric_id":"smoke", "name":"Synthetic", "traits":[
    {"trait_id":"quality", "name":"Quality", "description":"Synthetic criteria",
     "levels":[{"label":"low"},{"label":"high"}]}]})
response = ResponseItem(item_id="synthetic", text="Synthetic installation response.")
assert len(list_templates()) == 3
for info in list_templates():
    assert get_template(info.preset)
assert "Synthetic installation response." in render_prompt(rubric=rubric, response=response).text
score = normalize_rubric_result(rubric.traits[0], run_id="smoke", task_id="task",
    request_uid="request", attempt_id="attempt", item_id="synthetic", category_label="high")
assert score.primary_score == 1 and score.expected_value is None
from judgmentllm import export_rubric_r_csv, export_pairwise_r_csv, PairTask, normalize_pairwise_result
assert export_rubric_r_csv("rubric_r.csv", [score]) == 1
pair = PairTask(pair_id="smoke-pair", pair_uid="smoke-occurrence", left_item_id="001", right_item_id="002", trait_id="quality", presented_order=["002","001"])
pair_result = normalize_pairwise_result(pair, run_id="smoke", task_id="task-pair", request_uid="request-pair", attempt_id="attempt", winner_id="001")
assert export_pairwise_r_csv("pairwise_r.csv", [pair_result]) == 1
score.primary_score = 123
try:
    export_rubric_r_csv("invalid.csv", [score])
except ValueError:
    pass
else:
    raise AssertionError("Mutated result escaped installed export validation")
Path("responses.csv").write_text("item_id,text\\nsynthetic,Synthetic installation response.\\n".replace("\\n", chr(10)), encoding="utf-8")
Path("run.json").write_text(json.dumps({"schema_version":1,"task_kind":"rubric",
    "input":"responses.csv","rubric":rubric.model_dump(mode="json"),
    "provider":"synthetic","endpoint":"synthetic","model":"synthetic"}), encoding="utf-8")

from judgmentllm import GenerationSpec, GenerationCapabilities, load_run_spec
from judgmentllm.persistence.generation import build_generation_commitment, validate_generation_compatibility
spec = load_run_spec("run.json")
assert "generation" not in spec.model_dump(mode="json")
spec.generation = GenerationSpec(temperature=0, max_output_tokens=257)
assert spec.generation.model_dump(mode="json") == {"temperature":0,"max_output_tokens":257}
facts = GenerationCapabilities.model_validate({"provider":"synthetic",
    "endpoint":"synthetic","model":"synthetic",
    "temperature":{"supported":True},"max_output_tokens":{"supported":True}})
commitment = build_generation_commitment(spec, capabilities=facts)
assert validate_generation_compatibility(commitment, spec, capabilities=facts).valid

# Preparation remains provider-free and works after its source process exits.
from judgmentllm.preparation import prepare_run, open_prepared_run
with prepare_run("run.json", store="prepared-registry", run_id="001") as prepared:
    assert prepared.manifest.kind == "executable_run_manifest"
    assert prepared.get_response("synthetic").text == "Synthetic installation response."
    prepared_hash = prepared.manifest_sha256
    assert len(list(prepared.iter_request_dependencies())) == 1
with open_prepared_run("001", store="prepared-registry") as prepared:
    assert prepared.manifest_sha256 == prepared_hash
    assert prepared.get_response("synthetic").item_id == "synthetic"

# Execute and recover a real synthetic observation through the installed core.
import asyncio
from judgmentllm.execution import execute_run, recover_run
from judgmentllm.providers.mock import MockAdapter, MockScenario
mock_document = json.loads(Path("run.json").read_text(encoding="utf-8"))
mock_document.update(provider="mock", endpoint="synthetic", capture="minimal",
    generation={"temperature":0,"max_output_tokens":127})
Path("mock-run.json").write_text(json.dumps(mock_document), encoding="utf-8")
mock = MockAdapter(Path("mock-ledger.sqlite3"), scenario=MockScenario(probabilities=True))
mock_spec = load_run_spec("mock-run.json")
with prepare_run("mock-run.json", store="execution-registry", run_id="001",
    capabilities=mock.capabilities(mock_spec)):
    pass
report = asyncio.run(execute_run("001", store="execution-registry", adapter=mock))
assert report.summary.results == report.summary.successful_results == 1
assert not report.recovery_required and mock.ledger.acceptance_count() == 1
report = asyncio.run(recover_run("001", store="execution-registry", adapter=mock))
assert report.summary.results == 1 and mock.ledger.acceptance_count() == 1

# Exercise installed registry, immutable artifacts, and local OS ownership.
from judgmentllm.persistence.artifacts import ArtifactStore
from judgmentllm.persistence.hashing import bytes_digest, semantic_digest
from judgmentllm.persistence.models import RunRecord, ManifestReference
from judgmentllm.persistence.ownership import ExecutorOwnership
from judgmentllm.persistence.repository import Repository
with Repository("registry") as registry:
    registry.create_run(RunRecord(run_id="smoke", task_kind="rubric",
        provider="synthetic", endpoint="offline", model="fixture"))
    store = ArtifactStore(registry.run_directory("smoke"))
    artifact = store.put(b'{"synthetic":true}', "application/json")
    registry.seal_run("smoke", ManifestReference(artifact=artifact))
    with ExecutorOwnership(registry, "smoke"):
        assert registry.get_owner("smoke") is not None
    assert registry.get_owner("smoke") is None
with Repository("registry") as registry:
    assert registry.get_manifest("smoke").artifact.sha256 == bytes_digest(b'{"synthetic":true}')
assert semantic_digest("smoke", {"a":None}) != bytes_digest(b'{"a":null}')

distribution = metadata.distribution("judgmentllm")
assert judgmentllm.__version__ == distribution.version
assert Path(judgmentllm.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
assert resources.files("judgmentllm").joinpath("py.typed").is_file()
assert distribution.metadata["License-Expression"] == "MIT"
assert any(path.name == "LICENSE" for path in distribution.files or [])
entries = list(metadata.entry_points(group="console_scripts", name="judgmentllm"))
assert len(entries) == 1 and callable(entries[0].load())
for name in (
    "openai", "anthropic", "google-genai", "together", "ollama",
    "typesafe-sdk", "pyarrow", "fastapi", "uvicorn",
):
    try:
        metadata.distribution(name)
    except metadata.PackageNotFoundError:
        continue
    raise AssertionError(f"Optional dependency installed in the core environment: {name}")
print(distribution.version)
"""


def run(command: list[str], *, cwd: Path, offline: bool = True) -> str:
    """Run offline by default, retaining diagnostics if verification fails."""
    result = subprocess.run(
        command,
        cwd=cwd,
        env={
            **os.environ,
            "UV_OFFLINE": "1" if offline else "0",
            "NO_COLOR": "1",
            "TERM": "dumb",
        },
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(
            f"Command failed ({result.returncode}): {command!r}\n"
            f"{result.stdout}{result.stderr}"
        )
    return result.stdout


def smoke_install(uv: str, artifact: Path, constraints: Path, directory: Path) -> None:
    """Verify one non-editable artifact installation outside the checkout."""
    environment = directory / "venv"
    run([uv, "venv", "--python", sys.executable, str(environment)], cwd=directory)
    binaries = environment / ("Scripts" if os.name == "nt" else "bin")
    python = binaries / ("python.exe" if os.name == "nt" else "python")
    console = binaries / ("judgmentllm.exe" if os.name == "nt" else "judgmentllm")
    run(
        [
            uv,
            "pip",
            "install",
            "--python",
            str(python),
            "--constraint",
            str(constraints),
            str(artifact),
        ],
        cwd=directory,
        offline=False,
    )
    version = run([str(python), "-I", "-c", VERIFY_INSTALLED], cwd=directory).strip()
    help_text = unstyle(run([str(console), "--help"], cwd=directory))
    if "--help" not in help_text or "--version" not in help_text:
        raise RuntimeError("Installed CLI help does not expose the supported options")
    version_text = run([str(console), "--version"], cwd=directory).strip()
    if version_text != f"judgmentllm {version}":
        raise RuntimeError(f"Installed CLI version mismatch: {version_text!r}")
    presets = run([str(console), "templates", "list"], cwd=directory)
    if "rubric_scores_only" not in presets:
        raise RuntimeError("Installed templates are unavailable")
    run(
        [
            str(console),
            "templates",
            "init",
            "--preset",
            "rubric_scores_only",
            "--output",
            "prompt.txt",
        ],
        cwd=directory,
    )
    preview = run(
        [
            str(console),
            "templates",
            "preview",
            "--config",
            "run.json",
            "--item-id",
            "synthetic",
            "--trait",
            "quality",
        ],
        cwd=directory,
    )
    if "Synthetic installation response." not in preview:
        raise RuntimeError("Installed offline preview failed")
    print(f"Smoke installation passed: {artifact.name} (version {version})")


def main() -> None:
    """Resolve artifact globs on every platform and smoke-test each artifact."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifacts", nargs="+", help="Wheel or sdist paths/globs")
    patterns: list[str] = parser.parse_args().artifacts
    artifacts: set[Path] = set()
    for pattern in patterns:
        matches = glob.glob(pattern)
        if not matches:
            parser.error(f"No distribution matches {pattern!r}")
        for match in matches:
            path = Path(match).resolve()
            if not path.is_file() or not path.name.endswith((".whl", ".tar.gz")):
                parser.error(f"Expected a wheel or source distribution: {path}")
            artifacts.add(path)
    uv = shutil.which("uv")
    if uv is None:
        parser.error("uv must be installed and available on PATH")

    repository = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="judgmentllm-install-") as temporary:
        temporary_path = Path(temporary)
        constraints = temporary_path / "core-constraints.txt"
        run(
            [
                uv,
                "export",
                "--locked",
                "--no-default-groups",
                "--no-emit-project",
                "--no-hashes",
                "--output-file",
                str(constraints),
            ],
            cwd=repository,
        )
        for index, artifact in enumerate(sorted(artifacts)):
            directory = temporary_path / str(index)
            directory.mkdir()
            smoke_install(uv, artifact, constraints, directory)


if __name__ == "__main__":
    main()
