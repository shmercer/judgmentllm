"""Frozen OpenAI serializations and historical execution isolation after V2."""

from __future__ import annotations

import ast
import json
from hashlib import sha256
from pathlib import Path

from test_native_schema import cases, scope

from judgmentllm.native_selection import resolve_native_transport
from judgmentllm.providers.openai_schema import project_openai_schema

ROOT = Path(__file__).resolve().parents[1]


def test_complete_v1_serializations_remain_exact() -> None:
    fixture = json.loads(
        (
            ROOT / "tests/fixtures/native_v2_compatibility/v1-serialization.json"
        ).read_bytes()
    )
    actual: dict[str, dict[str, str]] = {}
    for endpoint in ("responses", "chat.completions"):
        for name, contract in cases().items():
            selected_scope = scope(endpoint)
            records = {
                "projection_json": project_openai_schema(
                    contract, scope=selected_scope
                ).model_dump_json(),
                "auto_json": resolve_native_transport(
                    contract, scope=selected_scope, policy="auto"
                ).model_dump_json(),
                "text_json": resolve_native_transport(
                    contract, scope=selected_scope, policy="text"
                ).model_dump_json(),
            }
            actual[endpoint + "/" + name] = {
                key: sha256(value.encode()).hexdigest()
                for key, value in records.items()
            }
    assert actual == fixture


def test_historical_services_do_not_import_v2_native_contracts() -> None:
    package = ROOT / "src/judgmentllm"
    paths = [
        package / name
        for name in (
            "planning.py",
            "prompts.py",
            "request_rendering.py",
            "output_commitments.py",
            "native_schema.py",
            "native_selection.py",
            "providers/openai_schema.py",
            "providers/protocol.py",
        )
    ]
    for directory in ("preparation", "execution", "persistence"):
        paths.extend((package / directory).rglob("*.py"))
    for path in paths:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom):
                assert node.module not in {
                    "judgmentllm.native_schema_v2",
                    "judgmentllm.native_selection_v2",
                }
            elif isinstance(node, ast.Import):
                assert not any(
                    name.name
                    in {
                        "judgmentllm.native_schema_v2",
                        "judgmentllm.native_selection_v2",
                    }
                    for name in node.names
                )


def test_synthetic_native_decision_serializations_remain_exact() -> None:
    from native_v2_cases import cases as v2_cases
    from test_native_v2 import ROUTES, catalog, evidence
    from test_native_v2 import scope as v2_scope

    from judgmentllm.native_selection_v2 import (
        _resolve_native_transport_v2,  # pyright: ignore[reportPrivateUsage]
    )

    records: list[bytes] = []
    for route in ROUTES:
        for contract in v2_cases().values():
            decision = _resolve_native_transport_v2(
                contract,
                scope=v2_scope(route),
                mechanism=route.mechanism,
                policy="native",
                catalog=catalog(route),
                evidence=evidence(route),
            )
            records.append(decision.model_dump_json().encode() + b"\n")
    assert len(records) == 112
    fixture = ROOT / "tests/fixtures/native_v2_compatibility/positive-decisions.jsonl"
    assert fixture.read_bytes() == b"".join(records)
