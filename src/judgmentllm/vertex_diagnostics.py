"""Vertex has no reviewed protected non-generative eligibility check.

Returning unsupported performs zero SDK, credential, catalog, refresh or inference
operations. Public publisher listing and token refresh cannot verify inference IAM.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from judgmentllm.evidence._base import EvidenceModel
from judgmentllm.models import ID


class AuthVerification(EvidenceModel):
    provider: Literal["vertex"] = "vertex"
    source: Literal[None] = None
    operation: Literal["unavailable"] = "unavailable"
    checked_at: datetime
    verification_status: Literal["unsupported"] = "unsupported"
    diagnostic_code: Literal["protected_nongenerative_operation_unqualified"] = (
        "protected_nongenerative_operation_unqualified"
    )


class ModelDiscovery(EvidenceModel):
    provider: Literal["vertex"] = "vertex"
    checked_at: datetime
    verification: AuthVerification
    models: tuple[()] = ()
    requested_model: ID | None = None
    model_visibility: Literal["unknown", "not_requested"] = "not_requested"
    complete: Literal[False] = False
    sdk_calls: Literal[0] = 0


def verify_authentication(
    *, provider: str = "vertex", http_transport: object | None = None
) -> AuthVerification:
    if provider != "vertex":
        raise ValueError("Select provider vertex for Vertex diagnostics")
    return AuthVerification(checked_at=datetime.now(UTC))


def discover_models(
    *,
    provider: str = "vertex",
    model: str | None = None,
    http_transport: object | None = None,
) -> ModelDiscovery:
    verification = verify_authentication(provider=provider)
    return ModelDiscovery(
        checked_at=verification.checked_at,
        verification=verification,
        requested_model=model,
        model_visibility="not_requested" if model is None else "unknown",
    )


async def averify_authentication(
    *, provider: str = "vertex", http_transport: object | None = None
) -> AuthVerification:
    return verify_authentication(provider=provider)


async def adiscover_models(
    *,
    provider: str = "vertex",
    model: str | None = None,
    http_transport: object | None = None,
) -> ModelDiscovery:
    return discover_models(provider=provider, model=model)
