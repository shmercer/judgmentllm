"""Nonsecret, explicit Vertex execution routing; no credential discovery or SDK.

Project is transient. Only its domain-separated route commitment enters evidence.
Changing any routing or authentication choice prevents frozen-run reconstruction.
"""

from __future__ import annotations

# pyright: reportPrivateUsage=false
import os
import re
from dataclasses import dataclass
from typing import Literal

from judgmentllm._auth import (
    _AUTHENTICATION_POLICY_VERSION,
    VertexAuthMode,
    _vertex_auth_mode,
)
from judgmentllm.persistence.hashing import semantic_digest

# Reviewed regional host forms. This is a transport boundary, not a model catalog
# or a claim that an account/model is eligible in any of these locations.
_LOCATIONS = frozenset(
    {
        "global",
        "us-central1",
        "us-east4",
        "europe-west1",
        "europe-west4",
        "asia-southeast1",
    }
)


class VertexSetupError(ValueError):
    """Safe configuration diagnostics never include environment values."""

    def __init__(
        self,
        code: Literal[
            "missing_project",
            "invalid_project",
            "missing_location",
            "invalid_location",
            "unsupported_authentication_context",
        ],
    ) -> None:
        messages = {
            "missing_project": "Set GOOGLE_CLOUD_PROJECT explicitly for Vertex execution and planning",
            "invalid_project": "GOOGLE_CLOUD_PROJECT must be a Google Cloud project ID or project number",
            "missing_location": "Set GOOGLE_CLOUD_LOCATION explicitly for Vertex execution and planning",
            "invalid_location": "Choose a reviewed GOOGLE_CLOUD_LOCATION listed in docs/vertex.md",
            "unsupported_authentication_context": "Set JUDGMENTLLM_VERTEX_AUTH_MODE to adc or api_key explicitly; no fallback is available",
        }
        super().__init__(messages[code])
        self.diagnostic_code = code


@dataclass(frozen=True, repr=False)
class VertexRuntimeConfiguration:
    project: str
    location: str
    auth_mode: VertexAuthMode

    def __post_init__(self) -> None:
        if self.auth_mode not in ("adc", "api_key"):
            raise VertexSetupError("unsupported_authentication_context")
        if not self.project:
            raise VertexSetupError("missing_project")
        if not re.fullmatch(
            r"(?:[a-z][a-z0-9-]{4,28}[a-z0-9]|[0-9]{6,20})", self.project
        ):
            raise VertexSetupError("invalid_project")
        if not self.location:
            raise VertexSetupError("missing_location")
        if self.location not in _LOCATIONS:
            raise VertexSetupError("invalid_location")

    def __repr__(self) -> str:
        return "VertexRuntimeConfiguration(<private project; explicit route>)"

    @property
    def base_url(self) -> str:
        host = (
            "aiplatform.googleapis.com"
            if self.location == "global"
            else self.location + "-aiplatform.googleapis.com"
        )
        return "https://" + host + "/"

    def resource_path(self, model: str) -> str:
        if (
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}", model)
            or ".." in model
        ):
            raise ValueError("Vertex requires a bare Google publisher model identifier")
        return f"projects/{self.project}/locations/{self.location}/publishers/google/models/{model}"

    @property
    def route_sha256(self) -> str:
        return semantic_digest(
            "vertex-route-v1",
            {
                "auth_policy_version": _AUTHENTICATION_POLICY_VERSION,
                "auth_mode": self.auth_mode,
                "project": self.project,
                "location": self.location,
                "base_url": self.base_url,
                "api_version": "v1beta1",
                "endpoint": "models.generate_content",
                "sdk_package": "google-genai",
                "sdk_version": "2.34.0",
                "resource_template": "projects/{project}/locations/{location}/publishers/google/models/{model}:generateContent",
            },
        )


def runtime_configuration() -> VertexRuntimeConfiguration:
    """Inspect nonsecret process configuration only, never any ADC path/key."""
    try:
        mode = _vertex_auth_mode()
    except ValueError:
        raise VertexSetupError("unsupported_authentication_context") from None
    return VertexRuntimeConfiguration(
        project=os.environ.get("GOOGLE_CLOUD_PROJECT", ""),
        location=os.environ.get("GOOGLE_CLOUD_LOCATION", ""),
        auth_mode=mode,
    )
