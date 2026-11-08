"""Explicit non-generative OpenAI authentication and model visibility services.

Each invocation performs at most one protected models-list read. Nothing here
runs during offline credential status, preparation, validation, or preview.
"""

from __future__ import annotations

# pyright: reportPrivateUsage=false
import asyncio
from datetime import UTC, datetime
from typing import Any, Literal, cast

from judgmentllm._auth import _resolve_credentials
from judgmentllm.evidence._base import EvidenceModel
from judgmentllm.models import ID
from judgmentllm.output_contracts import decode_assessment_output
from judgmentllm.providers.openai import _SDK_LOGGING, _mapping, _protected_client, _sdk

VerificationStatus = Literal[
    "authentication_accepted",
    "credentials_rejected",
    "permission_denied",
    "network_unavailable",
    "rate_limited",
    "unsupported",
    "inconclusive",
    "not_required",
]


class AuthVerification(EvidenceModel):
    """Sanitized result of one explicitly requested authenticated read."""

    provider: Literal["openai"] = "openai"
    source: Literal["OPENAI_API_KEY"] | None = None
    operation: Literal["openai.models.list"] = "openai.models.list"
    checked_at: datetime
    verification_status: VerificationStatus
    diagnostic_code: (
        Literal[
            "missing_api_key",
            "credentials_rejected",
            "permission_denied",
            "network_unavailable",
            "rate_limited",
            "inconclusive_response",
            "missing_openai_extra",
        ]
        | None
    ) = None


class ModelDiscovery(EvidenceModel):
    """One bounded model-list snapshot; visibility does not qualify capabilities."""

    provider: Literal["openai"] = "openai"
    checked_at: datetime
    verification: AuthVerification
    models: tuple[ID, ...] = ()
    requested_model: ID | None = None
    model_visibility: Literal["visible", "not_visible", "unknown", "not_requested"] = (
        "not_requested"
    )
    complete: bool = False
    sdk_calls: int = 0


async def adiscover_models(
    *,
    provider: str = "openai",
    model: str | None = None,
    http_transport: object | None = None,
) -> ModelDiscovery:
    """List visible OpenAI IDs through one SDK request, without pagination/retries.

    ``http_transport`` is a deterministic test seam. The URL remains fixed to
    OpenAI; credentials always come from the shared registered resolver.
    """
    if provider != "openai":
        raise ValueError("Model discovery currently supports provider openai only")
    checked = datetime.now(UTC)
    status: VerificationStatus = "inconclusive"
    diagnostic: Any = None
    source: Literal["OPENAI_API_KEY"] | None = None
    models: tuple[str, ...] = ()
    complete = False
    calls = 0
    client: Any = None
    logging_token = _SDK_LOGGING.set(True)
    try:
        try:
            key = _resolve_credentials("openai")._api_key()
        except ValueError:
            diagnostic = "missing_api_key"
            raise _DiagnosticUnavailable from None
        source = "OPENAI_API_KEY"
        try:
            sdk, http = _sdk()
        except RuntimeError:
            status = "unsupported"
            diagnostic = "missing_openai_extra"
            raise _DiagnosticUnavailable from None
        underlying: Any = http_transport or http.AsyncHTTPTransport(
            retries=0,
            limits=http.Limits(max_connections=1, max_keepalive_connections=1),
        )

        class BoundedModelsTransport(http.AsyncBaseTransport):
            async def handle_async_request(self, request: Any) -> Any:
                nonlocal calls
                if (
                    request.headers.get("authorization") != f"Bearer {key}"
                    or calls
                    or request.method != "GET"
                    or request.url != http.URL("https://api.openai.com/v1/models")
                ):
                    raise _DiagnosticUnavailable
                calls += 1
                response = await underlying.handle_async_request(request)
                data = bytearray()
                try:
                    async for chunk in response.aiter_bytes():
                        if len(data) + len(chunk) > 1_048_576:
                            raise _DiagnosticUnavailable
                        data.extend(chunk)
                finally:
                    await response.aclose()
                headers = {
                    key: value
                    for key, value in response.headers.items()
                    if key.lower() not in {"content-encoding", "content-length"}
                }
                return http.Response(
                    response.status_code,
                    content=bytes(data),
                    headers=headers,
                    request=request,
                )

            async def aclose(self) -> None:
                await underlying.aclose()

        http_client = http.AsyncClient(
            transport=BoundedModelsTransport(),
            timeout=http.Timeout(20.0, connect=5.0),
            trust_env=False,
            follow_redirects=False,
        )
        client = _protected_client(
            sdk,
            api_key=key,
            base_url="https://api.openai.com/v1",
            max_retries=0,
            timeout=20.0,
            http_client=http_client,
        )
        raw = await asyncio.wait_for(
            client.models.with_raw_response.list(), timeout=20.0
        )
        body = _mapping(decode_assessment_output(bytes(raw.content)))
        entries: Any = body.get("data")
        if not isinstance(entries, list) or len(cast(list[object], entries)) > 1000:
            raise _DiagnosticUnavailable
        identifiers: list[str] = []
        for entry in cast(list[object], entries):
            identifier = _mapping(entry).get("id")
            if (
                not isinstance(identifier, str)
                or not identifier.strip()
                or len(identifier) > 256
                or any(
                    character.isspace() or ord(character) < 32
                    for character in identifier
                )
            ):
                raise _DiagnosticUnavailable
            identifiers.append(identifier)
        models = tuple(dict.fromkeys(identifiers))
        complete = body.get("has_more", False) is False
        status = "authentication_accepted"
    except asyncio.CancelledError:
        raise
    except Exception as error:
        code = getattr(error, "status_code", None)
        if code in {401, 403, 429}:
            status = cast(
                VerificationStatus,
                {
                    401: "credentials_rejected",
                    403: "permission_denied",
                    429: "rate_limited",
                }[code],
            )
            diagnostic = status
        elif code is not None or not isinstance(
            error, (_DiagnosticUnavailable, ValueError)
        ):
            status = "network_unavailable"
            diagnostic = "network_unavailable"
        elif diagnostic is None:
            diagnostic = "inconclusive_response"
    finally:
        try:
            if client is not None:
                await client.close()
        except Exception:
            status = "network_unavailable"
            diagnostic = "network_unavailable"
        finally:
            _SDK_LOGGING.reset(logging_token)
    verification = AuthVerification(
        source=source,
        checked_at=checked,
        verification_status=status,
        diagnostic_code=diagnostic,
    )
    visibility: Literal["visible", "not_visible", "unknown", "not_requested"] = (
        "not_requested"
        if model is None
        else "visible"
        if model in models
        else "not_visible"
        if status == "authentication_accepted" and complete
        else "unknown"
    )
    return ModelDiscovery(
        checked_at=checked,
        verification=verification,
        models=models,
        requested_model=model,
        model_visibility=visibility,
        complete=complete,
        sdk_calls=calls,
    )


class _DiagnosticUnavailable(Exception):
    pass


async def averify_authentication(
    *, provider: str = "openai", http_transport: object | None = None
) -> AuthVerification:
    """Verify credentials with a non-generative protected models-list read."""
    return (
        await adiscover_models(provider=provider, http_transport=http_transport)
    ).verification


def _outside_loop() -> None:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise ValueError("Use the asynchronous diagnostics API inside an active event loop")


def verify_authentication(
    *, provider: str = "openai", http_transport: object | None = None
) -> AuthVerification:
    """Synchronous non-generative credential verification, explicitly online."""
    _outside_loop()
    return asyncio.run(
        averify_authentication(provider=provider, http_transport=http_transport)
    )


def discover_models(
    *,
    provider: str = "openai",
    model: str | None = None,
    http_transport: object | None = None,
) -> ModelDiscovery:
    """Synchronous one-read model discovery; no assessment calls are made."""
    _outside_loop()
    return asyncio.run(
        adiscover_models(provider=provider, model=model, http_transport=http_transport)
    )
