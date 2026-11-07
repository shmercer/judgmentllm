"""Fresh-process native projection admission, secret isolation and guard probes."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from test_auth_cli import KEY_NAMES, exclude_secrets, private_failure
from test_evidence_offline import snapshot

_SCRIPT = r"""
import io
import json
import logging
import os
import sys
import warnings

sys.dont_write_bytecode = True
canaries = json.loads(sys.stdin.read())
credential = canaries["credential"]
assessment = canaries["assessment"]
blocked_keys = {
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY",
    "TOGETHER_API_KEY", "TYPESAFE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS",
}
original_getitem = type(os.environ).__getitem__
def guarded_getitem(self, key):
    if key in blocked_keys:
        raise RuntimeError("Credential discovery forbidden")
    return original_getitem(self, key)
type(os.environ).__getitem__ = guarded_getitem

def guard(event, args):
    if event.startswith("socket."):
        raise RuntimeError("Network forbidden")
    if event == "import":
        if args[0].split(".")[0] in {
            "openai", "anthropic", "together", "ollama", "typesafe", "google",
            "keyring", "dotenv",
        }:
            raise RuntimeError("SDK or credential discovery forbidden")
    if event == "open":
        path, mode, flags = args[:3]
        if isinstance(path, (str, bytes)):
            name = os.fsdecode(path).replace("\\", "/").lower()
            if name.endswith(("/.env", "/.netrc", "/application_default_credentials.json")):
                raise RuntimeError("Credential file discovery forbidden")
        if (
            isinstance(mode, str) and any(c in mode for c in "wax+")
        ) or (
            isinstance(flags, int) and flags & (
                os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
            )
        ):
            raise RuntimeError("Filesystem writes forbidden")
    if event in {
        "os.mkdir", "os.remove", "os.rename", "os.rmdir", "os.link", "os.symlink",
        "subprocess.Popen", "os.system",
    }:
        raise RuntimeError("Mutation or subprocess forbidden")

sys.addaudithook(guard)
logs = io.StringIO()
logging.getLogger().addHandler(logging.StreamHandler(logs))
with warnings.catch_warnings(record=True) as observed:
    from judgmentllm.models import Level, Trait
    from judgmentllm.output_contracts import RubricOutputContract
    from judgmentllm.native_schema import (
        NativeProjectionScopeV1, NativeSchemaProjectionV1,
    )
    from judgmentllm.providers.openai_schema import project_openai_schema
    from judgmentllm.native_selection import TransportDecisionV1, resolve_native_transport

    contract = RubricOutputContract(
        traits=(Trait(
            trait_id="trait-" + assessment, name=assessment, description=credential,
            levels=[Level(label=assessment), Level(label="other")],
        ),), explanations=True,
    )
    for endpoint in ("responses", "chat.completions"):
        scope = NativeProjectionScopeV1(
            provider="openai", endpoint=endpoint, sdk_package="openai",
            sdk_version="3.42.1",
        )
        projection = project_openai_schema(contract, scope=scope)
        assert assessment in projection.native_schema_text
        assert credential not in projection.model_dump_json()
        assert assessment not in repr(projection)
        assert assessment not in repr(projection.source)
        assert assessment not in repr(projection.member_properties)
        assert projection == NativeSchemaProjectionV1.model_validate_json(
            projection.model_dump_json()
        )
        # No resolver path in the default catalog may discover credentials or SDKs.
        for policy in ("text", "auto", "native"):
            try:
                decision = resolve_native_transport(
                    contract, policy=policy, scope=scope,
                )
            except ValueError as error:
                assert policy == "native"
                assert assessment not in str(error) + repr(error)
            else:
                assert assessment not in repr(decision) + decision.model_dump_json()
        projection_json = projection.model_dump_json()
        scope_json = scope.model_dump_json()
        text_decision = resolve_native_transport(contract, scope=scope, policy="text")
        decision_json = text_decision.model_dump_json()
        for model, raw in (
            (NativeSchemaProjectionV1,
             '{"schema_version":' + json.dumps(credential) + ',' + projection_json[1:]),
            (NativeSchemaProjectionV1,
             projection_json.replace('"scope":{',
                 '"scope":{"model":' + json.dumps(credential) + ',', 1)),
            (NativeProjectionScopeV1,
             '{"model":' + json.dumps(assessment) + ',' + scope_json[1:]),
            (TransportDecisionV1,
             '{"native_support":' + json.dumps(credential) + ',' + decision_json[1:]),
            (NativeSchemaProjectionV1,
             '{' + json.dumps(assessment) + ':' + json.dumps(credential) + ','
             + json.dumps(assessment) + ':' + json.dumps(credential) + ','
             + projection_json[1:]),
        ):
            try:
                model.model_validate_json(raw)
            except ValueError as error:
                rendered = str(error) + repr(error)
                if hasattr(error, "errors"):
                    rendered += repr(error.errors()) + error.json()
                assert credential not in rendered and assessment not in rendered
            else:
                raise AssertionError("Duplicate native JSON accepted")
        bad_contract = contract.model_copy(update={"output_order": assessment})
        try:
            resolve_native_transport(bad_contract, scope=scope, policy="auto")
        except ValueError as error:
            assert assessment not in str(error) + repr(error)
        else:
            raise AssertionError("Mutated contract accepted")
        for operation in (
            lambda: NativeProjectionScopeV1.model_validate({credential: credential}),
            lambda: NativeSchemaProjectionV1.model_validate({assessment: credential}),
            lambda: projection.model_copy(update={assessment: credential}),
            lambda: projection.model_copy(update={"native_schema_text": credential}),
        ):
            try:
                operation()
            except ValueError as error:
                rendered = str(error) + repr(error)
                if hasattr(error, "errors"):
                    rendered += repr(error.errors()) + error.json()
                assert credential not in rendered and assessment not in rendered
            else:
                raise AssertionError("Invalid projection accepted")

    # These probes establish that a forbidden operation cannot silently pass.
    import socket
    for operation in (
        lambda: socket.getaddrinfo("offline.invalid", 443),
        lambda: socket.socket(),
        lambda: __import__("openai"),
        lambda: __import__("keyring"),
        lambda: os.environ["OPENAI_API_KEY"],
        lambda: open("forbidden-output", "w"),
        lambda: open("/synthetic/application_default_credentials.json"),
    ):
        try:
            operation()
        except RuntimeError:
            pass
        else:
            raise AssertionError("Offline admission guard ineffective")
    assert not observed
    assert credential not in logs.getvalue() and assessment not in logs.getvalue()
print("native offline privacy verified")
"""


@private_failure
def test_projection_and_selection_forbid_network_clients_discovery_and_writes(
    tmp_path: Path,
) -> None:
    __tracebackhide__ = True
    credential = "synthetic-credential-" + uuid4().hex
    assessment = "synthetic-assessment-" + uuid4().hex
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {*KEY_NAMES, "GOOGLE_APPLICATION_CREDENTIALS"}
    }
    environment["OPENAI_API_KEY"] = credential
    environment["GOOGLE_APPLICATION_CREDENTIALS"] = credential
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    before = snapshot(tmp_path)
    process = subprocess.run(
        [sys.executable, "-B", "-c", _SCRIPT],
        input=json.dumps({"credential": credential, "assessment": assessment}),
        text=True,
        capture_output=True,
        env=environment,
        cwd=tmp_path,
        timeout=30,
        check=False,
    )
    exclude_secrets(process.stdout + process.stderr, (credential, assessment))
    assert process.returncode == 0
    assert process.stdout == "native offline privacy verified\n"
    assert process.stderr == ""
    assert snapshot(tmp_path) == before
