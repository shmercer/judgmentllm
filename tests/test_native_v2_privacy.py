"""All V2 routes remain pure and preserve private schema ingress safeguards."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from test_auth_cli import KEY_NAMES, exclude_secrets, private_failure
from test_evidence_offline import snapshot
from test_native_schema_privacy import (
    _SCRIPT as V1_SCRIPT,  # pyright: ignore[reportPrivateUsage]
)

_SCRIPT = (
    V1_SCRIPT.split("with warnings.catch_warnings", 1)[0]
    + r"""
with warnings.catch_warnings(record=True) as observed:
    from judgmentllm.models import Level, Trait
    from judgmentllm.output_contracts import RubricOutputContract
    from judgmentllm.native_schema import NativeProjectionScopeV1
    from judgmentllm.native_schema_v2 import (
        _ROUTES_V2, NativeSchemaProjectionV2, project_native_schema_v2,
    )
    from judgmentllm.native_selection_v2 import (
        TransportDecisionV2, resolve_native_transport_v2,
    )
    contract = RubricOutputContract(traits=(Trait(
        trait_id="trait-" + assessment, name=credential, description=credential,
        levels=[Level(label=assessment), Level(label="other")],
    ),), explanations=True)
    for route in _ROUTES_V2:
        scope = NativeProjectionScopeV1(
            provider=route.provider, endpoint=route.endpoint,
            sdk_package=route.sdk_package, sdk_version=route.sdk_version,
            api_version=route.api_version,
        )
        projection = project_native_schema_v2(
            contract, scope=scope, mechanism=route.mechanism,
        )
        assert assessment in projection.native_schema_text
        assert credential not in projection.model_dump_json()
        assert assessment not in repr(projection) + repr(projection.source)
        assert assessment not in repr(projection.member_properties)
        assert projection == NativeSchemaProjectionV2.model_validate_json(
            projection.model_dump_json()
        )
        for coverage in (*projection.native_constraints, *projection.local_constraints):
            assert coverage.enforcement_verified is None
        for policy in ("text", "auto", "native"):
            try:
                decision = resolve_native_transport_v2(
                    contract, scope=scope, mechanism=route.mechanism, policy=policy,
                )
            except ValueError as error:
                assert policy == "native"
                assert credential not in str(error) and assessment not in str(error)
            else:
                assert credential not in repr(decision) + decision.model_dump_json()
                assert assessment not in repr(decision) + decision.model_dump_json()
        decision = resolve_native_transport_v2(
            contract, scope=scope, mechanism=route.mechanism, policy="text",
        )
        for model, valid in (
            (NativeSchemaProjectionV2, projection), (TransportDecisionV2, decision),
        ):
            raw = valid.model_dump_json()
            duplicates = (
                '{"schema_version":' + json.dumps(credential) + "," + raw[1:],
                raw.replace('"scope":{',
                    '"scope":{"model":' + json.dumps(credential) + ",", 1),
                "{" + json.dumps(assessment) + ":" + json.dumps(credential) + ","
                    + json.dumps(assessment) + ":null," + raw[1:],
            )
            for invalid in duplicates:
                try:
                    model.model_validate_json(invalid)
                except ValueError as error:
                    rendered = str(error) + repr(error)
                    if hasattr(error, "errors"):
                        rendered += repr(error.errors()) + error.json()
                    assert assessment not in rendered and credential not in rendered
                else:
                    raise AssertionError("duplicate JSON accepted")
            for update in ({assessment: credential}, {"schema_version": credential}):
                try:
                    valid.model_copy(update=update)
                except ValueError as error:
                    rendered = str(error) + repr(error)
                    if hasattr(error, "errors"):
                        rendered += repr(error.errors()) + error.json()
                    assert assessment not in rendered and credential not in rendered
                else:
                    raise AssertionError("invalid copy accepted")
    import socket
    for operation in (
        lambda: socket.getaddrinfo("offline.invalid", 443),
        lambda: socket.socket(),
        lambda: __import__("anthropic"),
        lambda: __import__("ollama"),
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
            raise AssertionError("offline guard ineffective")
    assert not observed
    assert credential not in logs.getvalue() and assessment not in logs.getvalue()
print("V2 native privacy verified")
"""
)


@private_failure
def test_all_v2_routes_are_offline_and_content_safe(tmp_path: Path) -> None:
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
        capture_output=True,
        text=True,
        env=environment,
        cwd=tmp_path,
        timeout=30,
        check=False,
    )
    exclude_secrets(process.stdout + process.stderr, (credential, assessment))
    assert process.returncode == 0
    assert process.stdout == "V2 native privacy verified\n"
    assert process.stderr == ""
    assert snapshot(tmp_path) == before
