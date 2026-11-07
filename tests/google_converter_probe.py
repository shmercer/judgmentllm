"""Isolated pinned Google converter probe; invoked only by synthetic tests."""

from __future__ import annotations

import copy
import importlib.metadata
import json
import sys
from types import SimpleNamespace
from typing import Any, cast

from native_sdk_guard import install_guards


def main() -> None:
    records = json.loads(sys.stdin.read())
    assert importlib.metadata.version("google-genai") == "2.34.0"
    guard = install_guards()
    from google import auth, genai
    from google.auth import _default
    from google.auth.credentials import Credentials
    from google.genai import _api_client, _common, models, types

    guard.protect_client(genai.Client)
    guard.protect_client(_api_client.BaseApiClient)
    for module, name in (
        (auth, "default"),
        (auth, "load_credentials_from_file"),
        (_default, "load_credentials_from_dict"),
    ):
        guard.protect_callable(module, name)
    # Refresh paths cannot perform discovery or transport even with stub inputs.
    Credentials.refresh = guard.deny
    guard.check("token_refresh", lambda: cast(Any, Credentials).refresh(None, None))
    sdk_models = cast(Any, models)
    guard.assert_intact()
    before = guard.events

    def convert(provider: str, config: dict[str, Any]) -> str:
        stub = SimpleNamespace(vertexai=provider == "vertex")
        converter = (
            sdk_models._GenerateContentConfig_to_vertex
            if stub.vertexai
            else sdk_models._GenerateContentConfig_to_mldev
        )
        fragment = converter(stub, copy.deepcopy(config))
        fragment = _common.encode_unserializable_types(
            _common.convert_to_dict(fragment)
        )
        return json.dumps(
            fragment, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        )

    tiny_schema = {
        "type": "object",
        "properties": {
            "winner_id": {"type": "string", "enum": ["002", "001"]},
            "tie": {"type": "boolean"},
        },
        "required": ["winner_id", "tie"],
        "additionalProperties": False,
        "propertyOrdering": ["winner_id", "tie"],
    }
    tiny: list[dict[str, Any]] = []
    for provider in ("gemini", "vertex"):
        for route, variant in (
            ("response_schema", "dictionary"),
            ("response_schema", "typed_schema"),
            ("response_json_schema", "dictionary"),
        ):
            schema = copy.deepcopy(tiny_schema)
            if variant == "typed_schema":
                schema = types.Schema.model_validate(schema)
            raw = convert(
                provider, {"response_mime_type": "application/json", route: schema}
            )
            tiny.append(
                {
                    "provider": provider,
                    "route": route,
                    "input": variant,
                    "fragment": raw,
                }
            )
    for record in records:
        assert convert(record["provider"], record["config"]) == record["expected"]
    assert guard.events == before, "guarded operation during schema conversion"
    guard.assert_intact()
    print(
        json.dumps(
            {
                "tiny": tiny,
                "full_schema_cases": len(records),
                "guard_checks": len(guard.checked),
                "guarded_operations": guard.events - before,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )


if __name__ == "__main__":
    main()
