"""Offline-only Gemini test isolation and deliberately private fixture authority."""

from __future__ import annotations

# pyright: reportPrivateUsage=false
import socket
from datetime import date
from typing import Any, cast

import pytest

from judgmentllm import gemini_registry
from judgmentllm.evidence.scope import EvidenceProvenance
from judgmentllm.gemini_registry import _QualifiedProfile
from judgmentllm.gemini_transport import GeminiProductionContext
from judgmentllm.service_tiers import ServiceQualificationScope


def isolate(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in {
        "GOOGLE_API_KEY": "SYNTHETIC_SELECTED_GEMINI_KEY",
        "GEMINI_API_KEY": "SYNTHETIC_SECONDARY_GEMINI_KEY",
        "GOOGLE_GENAI_USE_VERTEXAI": "true",
        "GOOGLE_GENAI_USE_ENTERPRISE": "true",
        "GOOGLE_CLOUD_PROJECT": "synthetic-project",
        "GOOGLE_CLOUD_LOCATION": "synthetic-location",
        "GOOGLE_APPLICATION_CREDENTIALS": "/forbidden/credential-file.json",
        "GOOGLE_GEMINI_BASE_URL": "https://forbidden.invalid/",
        "GOOGLE_VERTEX_BASE_URL": "https://forbidden.invalid/",
        "GOOGLE_GENAI_CLIENT_MODE": "record",
        "GOOGLE_GENAI_REPLAYS_DIRECTORY": "/forbidden/replays",
        "GOOGLE_GENAI_REPLAY_ID": "synthetic-replay",
        "SSL_CERT_FILE": "/forbidden/ca.pem",
        "SSL_CERT_DIR": "/forbidden/certs",
        "HTTPS_PROXY": "http://forbidden.invalid:8080",
        "ALL_PROXY": "http://forbidden.invalid:8080",
    }.items():
        monkeypatch.setenv(name, value)

    def deny(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("External network/ADC forbidden in Gemini tests")

    original_connect = socket.socket.connect

    def connect(current: socket.socket, address: Any) -> Any:
        if current.family in {socket.AF_INET, socket.AF_INET6} and address[0] not in {
            "127.0.0.1",
            "::1",
        }:
            return deny()
        return original_connect(current, address)

    monkeypatch.setattr(socket, "getaddrinfo", deny)
    monkeypatch.setattr(socket, "create_connection", deny)
    monkeypatch.setattr(socket.socket, "connect", connect)
    # Importing auth does not discover credentials. Any actual discovery fails.
    from google import auth
    from google.auth import _default
    from google.auth.credentials import Credentials

    monkeypatch.setattr(auth, "default", deny)
    monkeypatch.setattr(auth, "load_credentials_from_file", deny)
    monkeypatch.setattr(_default, "load_credentials_from_dict", deny)
    monkeypatch.setattr(Credentials, "refresh", deny)


def authorize_fixture(
    monkeypatch: pytest.MonkeyPatch,
    context: GeminiProductionContext,
    features: Any,
    mechanism: str,
) -> None:
    """Synthetic test-only authority, never persisted or written to production."""
    profile = _QualifiedProfile(
        scope=context.scope,
        service_scopes=(
            ServiceQualificationScope(
                account_scope="synthetic-account",
                tier="standard",
                features=tuple(features),
            ),
        ),
        mechanism=cast(Any, mechanism),
        schema_dialect="gemini.cloud-schema.v1"
        if mechanism.endswith(".response_schema")
        else "gemini.json-schema.v1",
        qualification_date=date(2026, 11, 10),
        source_commit="a" * 40,
        support_status="supported",
        request_acceptance="accepted",
        assessment_behavior="verified",
        native_schema_behavior="verified",
        supported_features=features,
        provenance=tuple(
            EvidenceProvenance(
                kind=cast(Any, kind),
                source_sha256=digest * 64,
                recorded_on=date(2026, 11, 10),
            )
            for kind, digest in (
                ("documentation", "a"),
                ("offline_fixture", "b"),
                ("independent_live", "c"),
            )
        ),
        documentation_sha256="a" * 64,
        offline_fixture_sha256="b" * 64,
        independent_live_sha256="c" * 64,
    )
    monkeypatch.setattr(gemini_registry, "_QUALIFIED_PROFILES", (profile,))


def envelope(output: str, **updates: Any) -> dict[str, Any]:
    return {
        "responseId": "synthetic-response-001",
        "modelVersion": "reported-fixture-model",
        "candidates": [
            {
                "index": 0,
                "finishReason": "STOP",
                "content": {"role": "model", "parts": [{"text": output}]},
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 20,
            "candidatesTokenCount": 10,
            "thoughtsTokenCount": 4,
            "cachedContentTokenCount": 3,
            "totalTokenCount": 34,
        },
        **updates,
    }
