"""Pure pinned-SDK serialization probe: no client, credentials, HTTP or discovery.

The installed SDK's converters and request builder run on an inert route record.
Actual HTTP equivalence is independently tested with a synthetic key and an
intercepted transport. No SDK request/send/authentication method runs here.
"""

from __future__ import annotations

# pyright: reportPrivateUsage=false
import importlib.metadata
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from judgmentllm.persistence.hashing import bytes_digest
from judgmentllm.vertex_config import VertexRuntimeConfiguration
from judgmentllm.vertex_transport import PreparedRequestV7

ROOT = Path(__file__).resolve().parents[2]


def installed_sdk_evidence() -> dict[str, str]:
    """Verify the existing reviewed SDK profile against installed source bytes."""
    raw = (ROOT / "tests/fixtures/production_vertex/sdk-profile.json").read_bytes()
    profile = json.loads(raw)
    distribution = importlib.metadata.distribution("google-genai")
    if distribution.version != "2.34.0" or profile["sdk_version"] != "2.34.0":
        raise ValueError("Vertex qualification requires the pinned SDK")
    sources = cast(dict[str, str], profile["source_sha256"])
    for name, digest in sources.items():
        if (
            bytes_digest(
                Path(str(distribution.locate_file("google/genai/" + name))).read_bytes()
            )
            != digest
        ):
            raise ValueError("Vertex qualification SDK source differs")
    return {"profile_sha256": bytes_digest(raw), **sources}


def sdk_request(
    wire: PreparedRequestV7, runtime: VertexRuntimeConfiguration
) -> tuple[str, bytes]:
    """Reconstruct the complete SDK URL/body without constructing any client."""
    from google import genai
    from google.genai import _api_client, _common, models, types

    from judgmentllm.providers.vertex import _sdk_arguments

    route = SimpleNamespace(
        vertexai=True,
        project=runtime.project,
        location=runtime.location,
        api_key=None,
        custom_base_url=True,
        _http_options=types.HttpOptions(
            base_url=runtime.base_url, api_version="v1beta1", headers={}
        ),
    )
    parameters = types._GenerateContentParameters(**_sdk_arguments(genai, wire))
    converted = models._GenerateContentParameters_to_vertex(
        cast(Any, route), parameters, None, parameters
    )
    path = converted["_url"]["model"] + ":generateContent"
    converted.pop("config", None)
    converted = _common.encode_unserializable_types(_common.convert_to_dict(converted))
    request = _api_client.BaseApiClient._build_request(
        cast(Any, route), "post", path, converted
    )
    # This is exactly the pinned SDK's nonstreaming HTTP JSON encoding. The
    # authenticated _async_request_once method is deliberately never invoked.
    return request.url, json.dumps(request.data).encode("utf-8")
