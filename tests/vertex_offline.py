"""Offline-only Vertex test isolation and deliberately private fixture authority."""

from __future__ import annotations

# pyright: reportPrivateUsage=false
import socket
from typing import Any

import pytest


def isolate(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in {
        "OPENAI_API_KEY": "SYNTHETIC_OPENAI_KEY",
        "ANTHROPIC_API_KEY": "SYNTHETIC_ANTHROPIC_KEY",
        "TOGETHER_API_KEY": "SYNTHETIC_TOGETHER_KEY",
        "TYPESAFE_API_KEY": "SYNTHETIC_TYPESAFE_KEY",
        "JUDGMENTLLM_VERTEX_AUTH_MODE": "api_key",
        "JUDGMENTLLM_VERTEX_API_KEY": "SYNTHETIC_SELECTED_VERTEX_KEY",
        "GOOGLE_API_KEY": "SYNTHETIC_SELECTED_GEMINI_KEY",
        "GEMINI_API_KEY": "SYNTHETIC_SECONDARY_GEMINI_KEY",
        "GOOGLE_GENAI_USE_VERTEXAI": "false",
        "GOOGLE_GENAI_USE_ENTERPRISE": "false",
        "GOOGLE_CLOUD_PROJECT": "synthetic-project",
        "GOOGLE_CLOUD_LOCATION": "us-central1",
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
        raise AssertionError("External network/ADC forbidden in Vertex tests")

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


def adc(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, list[object]]:
    """Exercise the SDK's official ADC call with an entirely synthetic result."""
    from google import auth
    from google.auth.credentials import Credentials

    class SyntheticCredentials(Credentials):
        def __init__(self) -> None:
            super().__init__()
            self.token = "SYNTHETIC_ADC_ACCESS_TOKEN"
            self._quota_project_id = "synthetic-quota-project"

        def refresh(self, request: Any) -> None:
            raise AssertionError("No token refresh authorized in offline tests")

    credentials = SyntheticCredentials()
    calls: list[object] = []

    def default(*args: Any, **kwargs: Any) -> Any:
        calls.append(kwargs)
        return credentials, "synthetic-project"

    monkeypatch.setenv("JUDGMENTLLM_VERTEX_AUTH_MODE", "adc")
    monkeypatch.setattr(auth, "default", default)
    return credentials, calls
