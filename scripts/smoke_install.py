"""Install built distributions into clean uv environments and verify the core CLI.

Run after syncing development dependencies and building distributions. Installation
may download packages at the core dependency versions exported from uv.lock.
Constraint export and installed-package checks remain offline.
Use --offline after preparation to require cached installation dependencies too.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from click import unstyle

AUTH_ENVIRONMENT = frozenset(
    {
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
        "TOGETHER_API_KEY",
        "TYPESAFE_API_KEY",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_CLOUD_PROJECT",
        "GOOGLE_CLOUD_LOCATION",
        "CLOUDSDK_CONFIG",
    }
)


def offline_environment() -> dict[str, str]:
    """Keep developer authentication settings outside clean-install smokes."""
    return {
        key: value
        for key, value in os.environ.items()
        if key.upper() not in AUTH_ENVIRONMENT
    }


VERIFY_INSTALLED = """
import importlib.metadata as metadata
import importlib.resources as resources
from pathlib import Path
import sys

import judgmentllm
import importlib.util
assert importlib.util.find_spec("judgmentllm.study") is None
from judgmentllm.auth import credential_status
assert [status.provider for status in credential_status()] == [
    "openai", "anthropic", "gemini", "vertex", "together", "ollama", "typesafe"]
assert credential_status(providers=()) == ()
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

# Installed grouped processing, explicit subset history and immutable exports.
from judgmentllm.execution.retry import authorize_subset_retry
from judgmentllm.execution.exports import export_run
from judgmentllm.providers import MockMemberScenario
from judgmentllm.persistence.repository import Repository
group_document = dict(mock_document)
group_rubric = rubric.model_dump(mode="json")
group_rubric["traits"].append(dict(group_rubric["traits"][0], trait_id="quality2"))
group_document.update(rubric=group_rubric, execution={"grouping":"grouped", "concurrency":1})
Path("group-run.json").write_text(json.dumps(group_document), encoding="utf-8")
group_spec = load_run_spec("group-run.json")
group_mock = MockAdapter("group-ledger.sqlite3", scenario=MockScenario(
    members={"quality2":MockMemberScenario(outcome="invalid_category")}))
with prepare_run("group-run.json", store="group-registry", run_id="001",
    capabilities=group_mock.capabilities(group_spec)):
    pass
report = asyncio.run(execute_run("001", store="group-registry", adapter=group_mock))
assert report.summary.results == 2 and report.summary.logical_completed_tasks == 1
with Repository("group-registry") as registry:
    failed_task = next(result.task_id for result in registry.iter_results("001") if result.status == "invalid")
retry_mock = MockAdapter("group-ledger.sqlite3")
authorize_subset_retry("001", store="group-registry", adapter=retry_mock,
    task_ids=(failed_task,), operation_id="smoke-retry")
report = asyncio.run(recover_run("001", store="group-registry", adapter=retry_mock))
assert report.summary.state == "completed" and report.summary.results == 3
assert retry_mock.ledger.acceptance_count() == 2
for output_format, output_view in (("jsonl","canonical"),("csv","canonical"),("csv","r")):
    exported = export_run("001", store="group-registry", format=output_format, view=output_view)
    assert exported.row_count == 3 and Path(exported.artifact_path).is_file()

# Public v2 config, shared exact preview, durable mock execution and R export.
from judgmentllm import RunSpecV2, preview_prompt, run_live, export_run_results
v2_document = json.loads(json.dumps(group_document))
v2_document.update(schema_version=2,
    execution={"grouping":"grouped", "concurrency":1, "explanations":True},
    prompt={"output_order":"explanations_first", "structured_output":"auto"})
Path("v2-run.json").write_text(json.dumps(v2_document), encoding="utf-8")
v2_spec = load_run_spec("v2-run.json")
assert isinstance(v2_spec, RunSpecV2)
v2_preview = preview_prompt(v2_spec, item_id="synthetic", trait_id="quality")
assert v2_preview.renderer_version == 2
assert list(v2_preview.output_schema["properties"]) == ["explanations", "scores"]
assert "Reference exemplars:" not in v2_preview.text
assert not Path("v2-registry").exists()
v2_summary = run_live("v2-run.json", store="v2-registry")
assert v2_summary.completed_tasks == 2 and v2_summary.results == 2
v2_export = export_run_results(v2_summary.run_id, store="v2-registry",
    format="csv", view="r")
assert v2_export.row_count == 2 and Path(v2_export.files[0].path).is_file()

# Durable evidence is one submission fact for this two-trait group. It does not
# add assessment columns or fabricate missing usage, including after reopening.
import csv
from judgmentllm import resume_run
from judgmentllm.persistence.repository import ReadOnlyRepository
v2_r_path = Path(v2_export.files[0].path)
v2_r_bytes = v2_r_path.read_bytes()
with Path("rubric_r.csv").open(encoding="utf-8", newline="") as stream:
    original_r_columns = next(csv.reader(stream))
with v2_r_path.open(encoding="utf-8", newline="") as stream:
    assert next(csv.reader(stream)) == original_r_columns
with ReadOnlyRepository("v2-registry") as registry:
    assert registry.evidence_supported
    evidence_before = tuple(registry.iter_evidence(v2_summary.run_id, page_size=1))
    assert len(evidence_before) == 1
    assert evidence_before[0].order.order_adherence == "adherent"
    assert evidence_before[0].usage.input_tokens is None
    assert evidence_before[0].usage.reported_cost is None
    results = tuple(registry.iter_results(v2_summary.run_id))
    assert len(results) == 2
    assert all(row.attempt_id == evidence_before[0].identity.attempt_id for row in results)
    assert all(row.input_tokens is None and row.output_tokens is None for row in results)
assert resume_run(v2_summary.run_id, store="v2-registry").results == 2
with ReadOnlyRepository("v2-registry") as registry:
    assert tuple(registry.iter_evidence(v2_summary.run_id, page_size=1)) == evidence_before
assert Path(export_run_results(v2_summary.run_id, store="v2-registry",
    format="csv", view="r").files[0].path).read_bytes() == v2_r_bytes

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
            **offline_environment(),
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


def verify_auth_console(console: Path, *, cwd: Path) -> None:
    """Check installed auth JSON and exits without retaining synthetic values."""
    expected_order = [
        "openai",
        "anthropic",
        "gemini",
        "vertex",
        "together",
        "ollama",
        "typesafe",
    ]
    synthetic = "SyntheticInstalledOfflineCredential-éλ"

    def require(condition: bool) -> None:
        if not condition:
            raise AssertionError("Installed offline authentication smoke failed")

    for required, configured, expected_exit in (
        ([], False, 0),
        (["openai"], False, 2),
        (["vertex"], False, 2),
        (["ollama"], False, 0),
        (["ollama,openai", "openai"], True, 0),
    ):
        environment = {**offline_environment(), "NO_COLOR": "1", "PYTHONUTF8": "1"}
        if configured:
            environment["OPENAI_API_KEY"] = synthetic
        arguments = [str(console), "auth", "status", "--json"]
        for provider in required:
            arguments.extend(("--require", provider))
        result = subprocess.run(
            arguments,
            cwd=cwd,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=False,
        )
        require(result.returncode == expected_exit)
        require(synthetic not in result.stdout + result.stderr)
        try:
            value = json.loads(result.stdout)
        except (ValueError, TypeError):
            raise AssertionError(
                "Installed auth status did not emit one JSON object"
            ) from None
        require(
            set(value)
            == {
                "schema_version",
                "command",
                "status",
                "run_id",
                "run_status",
                "artifacts",
                "data",
                "error",
            }
        )
        require(value["schema_version"] == 1 and value["command"] == "auth status")
        require(
            value["run_id"] is None
            and value["run_status"] is None
            and value["artifacts"] == {}
        )
        data = value["data"]
        require([status["provider"] for status in data["providers"]] == expected_order)
        require(data["requirements_met"] == (expected_exit == 0))
        require(value["status"] == ("error" if expected_exit else "ok"))
        if expected_exit:
            require(value["error"]["category"] == "validation")
        if configured:
            require(data["required_providers"] == ["openai", "ollama"])
            require(data["providers"][0]["status"] == "configured")


def smoke_install(
    uv: str,
    artifact: Path,
    constraints: Path,
    directory: Path,
    *,
    offline: bool = False,
) -> None:
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
        offline=offline,
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
    verify_auth_console(console, cwd=directory)
    # Exercise the installed public lifecycle from independent console processes.
    validated = json.loads(
        run(
            [str(console), "validate", "--config", "mock-run.json", "--json"],
            cwd=directory,
        )
    )
    assert validated["schema_version"] == 1 and validated["data"]["valid"]
    started = json.loads(
        run(
            [
                str(console),
                "run",
                "--config",
                "mock-run.json",
                "--store",
                "cli-registry",
                "--json",
            ],
            cwd=directory,
        )
    )
    identity = ["--run", started["run_id"], "--store", "cli-registry"]
    resumed = json.loads(
        run([str(console), "resume", *identity, "--json"], cwd=directory)
    )
    assert resumed["run_status"] == "completed" and resumed["data"]["results"] == 1
    exported = json.loads(
        run(
            [
                str(console),
                "results",
                "export",
                *identity,
                "--format",
                "csv",
                "--view",
                "r",
                "--json",
            ],
            cwd=directory,
        )
    )
    assert Path(exported["artifacts"]["export"]).is_absolute()
    assert Path(exported["artifacts"]["export"]).is_file()
    print(f"Smoke installation passed: {artifact.name} (version {version})")


def main() -> None:
    """Resolve artifact globs on every platform and smoke-test each artifact."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifacts", nargs="+", help="Wheel or sdist paths/globs")
    parser.add_argument(
        "--offline", action="store_true", help="Install only from the existing uv cache"
    )
    options = parser.parse_args()
    patterns: list[str] = options.artifacts
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
            smoke_install(uv, artifact, constraints, directory, offline=options.offline)


if __name__ == "__main__":
    main()
