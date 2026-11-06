"""Strictly offline, nonsensitive credential configuration diagnostics.

Configured means only that a plausible documented source is present. No status
verifies authentication, model access, endpoint support, or service availability.
"""

from __future__ import annotations

# pyright: reportPrivateUsage=false
from collections.abc import Mapping, Sequence
from typing import Any, Self, cast

from pydantic import (
    ConfigDict,
    Field,
    ModelWrapValidatorHandler,
    ValidationError,
    model_validator,
)
from pydantic_core import PydanticCustomError

from judgmentllm._auth import (
    _BY_PROVIDER,
    AuthType,
    CredentialSource,
    CredentialState,
    DiagnosticCode,
    ProviderId,
    _inspect_credentials,
    _select_providers,
)
from judgmentllm.models import StrictModel

__all__ = ["CredentialStatus", "credential_status"]


def _invalid_status() -> ValidationError:
    # Reconstruct rather than redact errors: pydantic's default errors()/json()
    # retain original input and validator context even with hide_input_in_errors.
    return ValidationError.from_exception_data(
        "CredentialStatus",
        [
            {
                "type": PydanticCustomError(
                    "credential_status_invalid", "Invalid credential status record."
                ),
                "loc": (),
                "input": None,
            }
        ],
    )


class CredentialStatus(StrictModel):
    """Frozen offline presence record containing only registered public labels.

    API-key missing/malformed states have no selected public source. ADC remains
    unknown without discovery. Local Ollama's no-auth status covers only the
    ordinary local service, not remote proxies or cloud services.
    """

    model_config = ConfigDict(frozen=True, hide_input_in_errors=True)

    provider: ProviderId = Field(description="Exact registered lowercase provider ID.")
    auth_type: AuthType = Field(description="Registered authentication mechanism.")
    source: CredentialSource | None = Field(
        description="Selected allowlisted source, or null when absent or unusable."
    )
    status: CredentialState = Field(
        description="Offline presence state; never authentication verification."
    )
    diagnostic_code: DiagnosticCode | None = Field(
        description="Fixed safe diagnostic code, or null."
    )

    @model_validator(mode="wrap")
    @classmethod
    def _safe_validation(
        cls, value: Any, handler: ModelWrapValidatorHandler[Self]
    ) -> Self:
        try:
            if isinstance(value, Mapping) and any(
                key not in cls.model_fields
                for key in cast(Mapping[object, object], value)
            ):
                raise ValueError("Invalid credential status record.")
            return handler(value)._registered_combination()
        except Exception:
            pass
        raise _invalid_status() from None

    def _registered_combination(self) -> Self:
        registration = _BY_PROVIDER[self.provider]
        valid = self.auth_type == registration.auth_type and not self.__pydantic_extra__
        if self.status == "unsupported" and self.auth_type == "api_key":
            valid = (
                valid
                and self.source is None
                and self.diagnostic_code == "unsupported_authentication_context"
            )
        elif self.auth_type == "api_key":
            valid = valid and (
                (
                    self.status == "configured"
                    and self.source in registration.sources
                    and self.diagnostic_code is None
                )
                or (
                    self.status == "missing"
                    and self.source is None
                    and self.diagnostic_code
                    in {
                        "missing_api_key",
                        "malformed_api_key",
                        "credential_source_unavailable",
                    }
                )
            )
        elif self.auth_type == "application_default_credentials":
            valid = valid and (
                self.status == "unknown"
                and self.source == "google_adc"
                and self.diagnostic_code == "offline_identity_unknown"
            )
        else:
            valid = valid and (
                self.status == "not_required"
                and self.source == "local_service"
                and self.diagnostic_code is None
            )
        if not valid:
            raise ValueError("Invalid credential status record.")
        return self

    @classmethod
    def model_validate_json(
        cls, json_data: str | bytes | bytearray, **kwargs: Any
    ) -> Self:
        """Sanitize JSON parser errors before they can expose raw input."""
        try:
            return super().model_validate_json(json_data, **kwargs)
        except Exception:
            pass
        raise _invalid_status() from None

    @classmethod
    def model_validate_strings(cls, obj: Any, **kwargs: Any) -> Self:
        """Sanitize string-mapping parser errors as well as model errors."""
        try:
            return super().model_validate_strings(obj, **kwargs)
        except Exception:
            pass
        raise _invalid_status() from None

    @classmethod
    def parse_raw(cls, *args: Any, **kwargs: Any) -> Self:
        """Deprecated raw parsing is unsupported; use model_validate_json()."""
        raise ValueError("Use model_validate_json() for credential status records.")

    @classmethod
    def parse_file(cls, *args: Any, **kwargs: Any) -> Self:
        """Never open files through a credential diagnostic model."""
        raise ValueError("File parsing is unsupported for credential status records.")

    def copy(self, *args: Any, **kwargs: Any) -> Self:
        """Deprecated unchecked copying is unsupported; use model_copy()."""
        raise ValueError("Use model_copy() for credential status records.")

    def __setattr__(self, name: str, value: Any) -> None:
        try:
            super().__setattr__(name, value)
        except ValidationError:
            pass
        raise _invalid_status() from None

    @classmethod
    def model_construct(
        cls, _fields_set: set[str] | None = None, **values: Any
    ) -> Self:
        """Keep the allowlisted boundary even for trusted-construction callers."""
        return cls.model_validate(values)

    def model_copy(
        self, *, update: Mapping[str, Any] | None = None, deep: bool = False
    ) -> Self:
        """Copy immutable scalar fields while validating requested changes."""
        values = self.model_dump()
        if update is not None:
            values.update(update)
        return type(self).model_validate(values)


def credential_status(
    *, providers: Sequence[str] | None = None
) -> tuple[CredentialStatus, ...]:
    """Return registry-ordered offline status; explicit empty selection is `()`.

    IDs must be exact registered lowercase strings. Unknown/malformed IDs are
    rejected, duplicates are removed, and caller ordering is ignored. Only the
    documented process environment variables are inspected, without mutation.
    """
    statuses: list[CredentialStatus] = []
    for provider in _select_providers(providers):
        resolved = _inspect_credentials(provider)
        statuses.append(
            CredentialStatus(
                provider=resolved.provider,
                auth_type=resolved.auth_type,
                source=resolved.source,
                status=resolved.status,
                diagnostic_code=resolved.diagnostic_code,
            )
        )
    return tuple(statuses)
