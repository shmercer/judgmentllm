"""Private, memory-only credential policy shared by diagnostics and adapters.

Importing this module never imports a provider SDK or discovers credentials.
The default endpoint scope is part of each fixed registration, not RunSpec.
"""

from __future__ import annotations

import os
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, NoReturn, SupportsIndex, cast

ProviderId = Literal[
    "openai", "anthropic", "gemini", "vertex", "together", "ollama", "typesafe"
]
AuthType = Literal["api_key", "application_default_credentials", "none"]
CredentialSource = Literal[
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
    "google_adc",
    "JUDGMENTLLM_VERTEX_API_KEY",
    "TOGETHER_API_KEY",
    "local_service",
    "TYPESAFE_API_KEY",
]
CredentialState = Literal[
    "configured", "missing", "unknown", "not_required", "unsupported"
]
DiagnosticCode = Literal[
    "missing_api_key",
    "malformed_api_key",
    "credential_source_unavailable",
    "offline_identity_unknown",
    "unsupported_authentication_context",
]
_EndpointScope = Literal["remote_api", "google_cloud", "local_service"]


@dataclass(frozen=True, slots=True)
class _AuthenticationRegistration:
    provider: ProviderId
    auth_type: AuthType
    # Ordered, public identifiers; API-key identifiers are exact environment names.
    sources: tuple[CredentialSource, ...]
    offline_status_supported: bool
    endpoint_scope: _EndpointScope


_REGISTRY = (
    _AuthenticationRegistration(
        "openai", "api_key", ("OPENAI_API_KEY",), True, "remote_api"
    ),
    _AuthenticationRegistration(
        "anthropic", "api_key", ("ANTHROPIC_API_KEY",), True, "remote_api"
    ),
    _AuthenticationRegistration(
        "gemini", "api_key", ("GOOGLE_API_KEY", "GEMINI_API_KEY"), True, "remote_api"
    ),
    _AuthenticationRegistration(
        "vertex",
        "application_default_credentials",
        ("google_adc",),
        False,
        "google_cloud",
    ),
    _AuthenticationRegistration(
        "together", "api_key", ("TOGETHER_API_KEY",), True, "remote_api"
    ),
    _AuthenticationRegistration(
        "ollama", "none", ("local_service",), True, "local_service"
    ),
    _AuthenticationRegistration(
        "typesafe", "api_key", ("TYPESAFE_API_KEY",), True, "remote_api"
    ),
)
_BY_PROVIDER = MappingProxyType({entry.provider: entry for entry in _REGISTRY})


# Policy 2 adds an explicitly selected Vertex key source. Historical ADC status
# keeps its five-field contract; key/unsupported-mode status uses status V2.
_AUTHENTICATION_POLICY_VERSION = 2
_VERTEX_KEY_REGISTRATION = _AuthenticationRegistration(
    "vertex", "api_key", ("JUDGMENTLLM_VERTEX_API_KEY",), True, "google_cloud"
)
VertexAuthMode = Literal["adc", "api_key"]


def _vertex_auth_mode() -> VertexAuthMode:
    try:
        mode = os.environ.get("JUDGMENTLLM_VERTEX_AUTH_MODE", "adc")
        if mode in ("adc", "api_key"):
            return mode
    except Exception:
        pass
    raise _CredentialResolutionError("unsupported_authentication_context") from None


def _select_providers(providers: Sequence[str] | None) -> tuple[ProviderId, ...]:
    """Validate without echoing input; retain fixed registry order."""
    if providers is None:
        return tuple(entry.provider for entry in _REGISTRY)
    selection: object = providers
    if isinstance(selection, (str, bytes)) or not isinstance(selection, Sequence):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise ValueError("Select registered lowercase provider identifiers.")
    try:
        selected: set[str] = set()
        for provider in providers:
            if type(provider) is not str or provider not in _BY_PROVIDER:
                raise ValueError
            selected.add(provider)
    except Exception:
        raise ValueError("Select registered lowercase provider identifiers.") from None
    return tuple(entry.provider for entry in _REGISTRY if entry.provider in selected)


class _CredentialResolutionError(ValueError):
    """Fixed diagnostics only; neither source exceptions nor values are attached."""

    def __init__(self, diagnostic_code: DiagnosticCode) -> None:
        super().__init__(
            "Credential material is unavailable under the registered policy."
        )
        self.diagnostic_code = diagnostic_code


class _ResolvedCredentials:
    """Ephemeral adapter boundary; deliberately neither a model nor a dataclass.

    Only explicit adapter code may request `_api_key()`. ADC markers defer to the execution-only
    official SDK chain; local markers contain no credential. This object
    is never passed into prepared requests, persistence, or public outputs.
    """

    __slots__ = (
        "provider",
        "auth_type",
        "source",
        "status",
        "diagnostic_code",
        "__key",
    )

    provider: ProviderId
    auth_type: AuthType
    source: CredentialSource | None
    status: CredentialState
    diagnostic_code: DiagnosticCode | None

    def __init__(
        self,
        registration: _AuthenticationRegistration,
        *,
        source: CredentialSource | None,
        status: CredentialState,
        diagnostic_code: DiagnosticCode | None = None,
        key: str | None = None,
    ) -> None:
        self.provider = registration.provider
        self.auth_type = registration.auth_type
        self.source = source
        self.status = status
        self.diagnostic_code = diagnostic_code
        self.__key = key

    def __repr__(self) -> str:
        return "<_ResolvedCredentials: private credential material>"

    def _api_key(self) -> str:
        if (
            self.auth_type != "api_key"
            or self.status != "configured"
            or self.__key is None
        ):
            raise _CredentialResolutionError("unsupported_authentication_context")
        return self.__key

    def __reduce__(self) -> NoReturn:
        raise TypeError("Credential material cannot be serialized.")

    def __reduce_ex__(self, protocol: SupportsIndex) -> NoReturn:
        raise TypeError("Credential material cannot be serialized.")

    def __getstate__(self) -> NoReturn:
        raise TypeError("Credential material cannot be serialized.")


def _inspect_credentials(
    provider: str,
    *,
    endpoint_scope: _EndpointScope | None = None,
    vertex_mode: VertexAuthMode | None = None,
) -> _ResolvedCredentials:
    """Inspect only fixed documented environment sources; perform no discovery."""
    selected = _select_providers((provider,))[0]
    registration = _BY_PROVIDER[selected]
    if selected == "vertex":
        try:
            mode = _vertex_auth_mode() if vertex_mode is None else vertex_mode
            if mode not in ("adc", "api_key"):
                raise ValueError
        except ValueError:
            return _ResolvedCredentials(
                registration,
                source=None,
                status="unsupported",
                diagnostic_code="unsupported_authentication_context",
            )
        if mode == "api_key":
            registration = _VERTEX_KEY_REGISTRATION
    elif vertex_mode is not None:
        raise _CredentialResolutionError("unsupported_authentication_context")
    if endpoint_scope is not None and endpoint_scope != registration.endpoint_scope:
        return _ResolvedCredentials(
            registration,
            source=None,
            status="unsupported",
            diagnostic_code="unsupported_authentication_context",
        )
    if registration.auth_type == "application_default_credentials":
        return _ResolvedCredentials(
            registration,
            source="google_adc",
            status="unknown",
            diagnostic_code="offline_identity_unknown",
        )
    if registration.auth_type == "none":
        return _ResolvedCredentials(
            registration, source="local_service", status="not_required"
        )
    for source in registration.sources:
        try:
            value = os.environ.get(source)
            if value is None or not value or value.isspace():
                continue
            malformed = any(
                character.isspace()
                or unicodedata.category(character) in {"Cc", "Cf", "Cs"}
                for character in value
            )
        except Exception:
            return _ResolvedCredentials(
                registration,
                source=None,
                status="missing",
                diagnostic_code="credential_source_unavailable",
            )
        if malformed:
            return _ResolvedCredentials(
                registration,
                source=None,
                status="missing",
                diagnostic_code="malformed_api_key",
            )
        return _ResolvedCredentials(
            registration, source=source, status="configured", key=value
        )
    return _ResolvedCredentials(
        registration,
        source=None,
        status="missing",
        diagnostic_code="missing_api_key",
    )


def _resolve_credentials(  # pyright: ignore[reportUnusedFunction]
    provider: str,
    *,
    endpoint_scope: _EndpointScope | None = None,
    vertex_mode: VertexAuthMode | None = None,
) -> _ResolvedCredentials:
    """Obtain selected material or a deferred ADC/local marker for an adapter.

    Missing/malformed sources fail before SDK construction. This function does
    not construct a client, execute ADC discovery, or verify authentication.
    """
    resolved = _inspect_credentials(
        provider, endpoint_scope=endpoint_scope, vertex_mode=vertex_mode
    )
    if resolved.status in {"missing", "unsupported"}:
        raise _CredentialResolutionError(
            cast(DiagnosticCode, resolved.diagnostic_code)
        ) from None
    return resolved
