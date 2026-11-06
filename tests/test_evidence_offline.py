"""Fresh-process offline admission and sanitized existing CLI privacy checks."""

from __future__ import annotations

import os
import subprocess
import sys
import warnings
from pathlib import Path
from uuid import uuid4

import pytest
from test_auth_cli import KEY_NAMES, exclude_secrets, private_failure

# The child installs admission guards before its first JudgmentLLM import.
# Secret material arrives through a pipe and is never written to a fixture file.
_OFFLINE_SCRIPT = r"""
import io
import json
import logging
import os
import sys
import warnings

sys.dont_write_bytecode = True
secret = sys.stdin.read()

def guard(event, args):
    if event.startswith("socket."):
        raise RuntimeError("Offline network operation forbidden")
    if event == "import":
        name = args[0]
        if name.split(".")[0] in {
            "openai", "anthropic", "together", "ollama", "typesafe", "google"
        }:
            raise RuntimeError("Provider SDK import forbidden")
    if event == "open":
        mode, flags = args[1], args[2]
        if (
            isinstance(mode, str) and any(c in mode for c in "wax+")
        ) or (
            isinstance(flags, int) and flags & (
                os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
            )
        ):
            raise RuntimeError("Evidence filesystem writes forbidden")
    if event in {"os.mkdir", "os.remove", "os.rename", "os.rmdir", "os.link", "os.symlink"}:
        raise RuntimeError("Evidence filesystem mutation forbidden")

sys.addaudithook(guard)
logs = io.StringIO()
handler = logging.StreamHandler(logs)
logging.getLogger().addHandler(handler)
with warnings.catch_warnings(record=True) as emitted_warnings:
    from hashlib import sha256
    from judgmentllm.evidence import (
        EvidenceIdentity, ProviderUsage, ReasoningAuthorization,
        ReasoningBlockMetadata, ReportedUsageInput, UsageContext,
        authorize_reasoning, extract_observation_metadata,
        extract_provider_usage, extract_reasoning_metadata,
    )
    from judgmentllm.models import Level, Trait
    from judgmentllm.output_commitments import (
        MemberPropertyOrder, OutputCommitmentV2, SchemaSelectionV2,
    )
    from judgmentllm.output_contracts import RubricOutputContract, serialize_output_schema

    identity = EvidenceIdentity(
        run_id="001", request_uid="002", attempt_id="003",
        submission_id="004", observation_id="005",
    )
    usage = extract_provider_usage(
        identity,
        UsageContext(
            provider="mock", endpoint="synthetic", requested_model="fixture",
            observation_kind="response",
        ),
        ReportedUsageInput(usage_source="synthetic.reported_usage", input_tokens=0),
    )
    assert usage.input_tokens == 0 and usage.total_tokens is None
    assert usage == ProviderUsage.model_validate_json(usage.model_dump_json())
    contract = RubricOutputContract(
        traits=(Trait(
            trait_id="01", name="Synthetic", description="Synthetic rubric",
            levels=[Level(label="low"), Level(label="high")],
        ),), explanations=True,
    )
    schema = serialize_output_schema(contract)
    committed = OutputCommitmentV2(
        contract=contract,
        selection=SchemaSelectionV2(
            policy="text", native_support="unsupported", reason="explicit_text",
        ),
        schema_text=schema.decode(), schema_sha256=sha256(schema).hexdigest(),
        root_properties=("scores",),
        member_properties=(MemberPropertyOrder(
            array="scores", trait_id="01",
            properties=("trait_id", "category_label", "explanation"),
        ),),
    )
    order = extract_observation_metadata(
        committed, identity=identity, evidence_source="raw_output_text",
        output=json.dumps({"scores": [{
            "trait_id": secret, "category_label": "high", "explanation": "Synthetic",
            secret: "Synthetic unexpected field",
        }]}),
    )
    assert order.order_adherence == "unknown"
    assert order.unknown_reason == "unsafe_key_names"
    assert order.member_key_orders[0].keys is None
    assert order.member_key_orders[0].trait_id is None
    policy = ReasoningAuthorization(capture="full")
    assert not authorize_reasoning(policy, "summary").retain_text
    assert authorize_reasoning(
        ReasoningAuthorization(capture="full", retention="summary"), "summary"
    ).retain_text
    reasoning = tuple(extract_reasoning_metadata(
        identity,
        (ReasoningBlockMetadata(
            kind="summary", provider_field="openai.responses.reasoning.summary",
            block_ordinal=0,
        ),), authorization=policy,
    ))
    assert reasoning[0].availability == "not_retained"
    for record in (usage, order, *reasoning):
        assert secret not in str(record.model_dump()) + record.model_dump_json() + repr(record)
    for model in (EvidenceIdentity, ReasoningBlockMetadata):
        try:
            model.model_validate({secret: secret})
        except ValueError as error:
            assert secret not in str(error) + repr(error) + repr(error.errors()) + error.json()
        else:
            raise AssertionError("Invalid evidence was accepted")
    assert not emitted_warnings
    assert secret not in logs.getvalue()
print("offline evidence verified")
"""


def snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


@private_failure
def test_fresh_import_and_extraction_forbid_sdks_network_and_writes(
    tmp_path: Path,
) -> None:
    __tracebackhide__ = True
    secret = "synthetic-private-" + uuid4().hex
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {*KEY_NAMES, "GOOGLE_APPLICATION_CREDENTIALS"}
    }
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    before = snapshot(tmp_path)
    result = subprocess.run(
        [sys.executable, "-c", _OFFLINE_SCRIPT],
        input=secret,
        text=True,
        capture_output=True,
        cwd=tmp_path,
        env=environment,
        timeout=30,
        check=False,
    )
    exclude_secrets(result.stdout + result.stderr, (secret,))
    assert result.returncode == 0
    assert result.stdout == "offline evidence verified\n"
    assert result.stderr == ""
    assert snapshot(tmp_path) == before


@private_failure
def test_existing_cli_stderr_warnings_and_files_exclude_synthetic_keys(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    __tracebackhide__ = True
    secret = "synthetic-private-" + uuid4().hex
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {*KEY_NAMES, "GOOGLE_APPLICATION_CREDENTIALS"}
    }
    # Whitespace makes this an unusable key. The existing required-provider
    # failure path must display only its fixed safe diagnostics.
    environment["OPENAI_API_KEY"] = secret + " " + secret
    environment["JUDGMENTLLM_RUN_HOME"] = str(tmp_path / "unused-store")
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONWARNINGS"] = "error"
    installed = Path(sys.executable).with_name(
        "judgmentllm.exe" if os.name == "nt" else "judgmentllm"
    )
    assert installed.is_file()
    before = snapshot(tmp_path)
    with warnings.catch_warnings(record=True) as emitted:
        # A fresh interpreter prevents pending finalizers or global Click/Typer
        # state from unrelated collected tests from entering this privacy check.
        # Warnings are errors in the actual CLI process as well as inspected in
        # this parent; stdout and stderr remain separate real process streams.
        result = subprocess.run(
            [
                str(installed),
                "auth",
                "status",
                "--json",
                "--require",
                "openai",
            ],
            text=True,
            capture_output=True,
            cwd=tmp_path,
            env=environment,
            timeout=30,
            check=False,
        )
    exclude_secrets(result.stdout + result.stderr + caplog.text, (secret,))
    assert result.returncode == 2
    assert not emitted
    assert snapshot(tmp_path) == before
    for name, content in snapshot(tmp_path).items():
        exclude_secrets(name + content.decode("utf-8", errors="replace"), (secret,))
