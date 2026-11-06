"""Immutable generation manifest section and local compatibility foundation.

This section is not an executable run manifest. Source verification, complete
manifest construction, exact provider payloads and operational resume belong to
the remaining preparation and execution subphases.
"""

from __future__ import annotations

from typing import cast

from pydantic import field_serializer, field_validator, model_validator

from judgmentllm.config import GenerationSpec, RunSpecification
from judgmentllm.generation import GenerationCapabilities
from judgmentllm.io import _revalidate_record  # pyright: ignore[reportPrivateUsage]
from judgmentllm.models import ID
from judgmentllm.persistence.hashing import semantic_digest
from judgmentllm.persistence.models import SHA256, ExecutionModel
from judgmentllm.planning import (
    PreflightError,
    ValidationIssue,
    ValidationReport,
    validate_generation,
)


def _settings(value: GenerationSpec | None) -> dict[str, int | float]:
    """Project only requested values after a complete deep validation boundary."""
    if value is None:
        return {}
    current = _revalidate_record(value)
    return cast(dict[str, int | float], current.model_dump(exclude_none=True))


def _requested_digest(
    provider: str, endpoint: str, model: str, settings: GenerationSpec | None
) -> str:
    return semantic_digest(
        "generation-request-settings",
        {
            "provider": provider,
            "endpoint": endpoint,
            "model": model,
            "settings": _settings(settings),
        },
    )


class GenerationCommitment(ExecutionModel):
    """Frozen generation section, with requested identity separate from evidence.

    ``requested_sha256`` commits only to exact scope and normalized requested
    settings. The enclosing manifest bytes also retain the immutable original
    capability evidence. Unknown support is never a qualification claim.
    """

    provider: ID
    endpoint: ID
    model: ID
    settings: GenerationSpec | None = None
    capabilities: GenerationCapabilities | None = None
    requested_sha256: SHA256

    @field_validator("settings", mode="after")
    @classmethod
    def normalize_empty(cls, value: GenerationSpec | None) -> GenerationSpec | None:
        return value if _settings(value) else None

    @field_serializer("settings")
    def sparse_settings(self, value: GenerationSpec | None) -> dict[str, int | float]:
        return _settings(value)

    @model_validator(mode="after")
    def validate_commitments(self) -> GenerationCommitment:
        if self.capabilities is not None and (
            self.capabilities.provider,
            self.capabilities.endpoint,
            self.capabilities.model,
        ) != (self.provider, self.endpoint, self.model):
            raise ValueError("generation capability evidence must match exact scope")
        expected = _requested_digest(
            self.provider, self.endpoint, self.model, self.settings
        )
        if self.requested_sha256 != expected:
            raise ValueError("generation requested-settings digest is inconsistent")
        return self


def build_generation_commitment(
    spec: RunSpecification, *, capabilities: GenerationCapabilities | None = None
) -> GenerationCommitment:
    """Validate and snapshot the generation section before sealing a manifest.

    This performs no source freezing, request construction or submission. The
    returned section may be retained under either capture policy: its settings
    contain only numeric controls, never assessment text or credentials.
    """
    current = _revalidate_record(spec)
    profile = _revalidate_record(capabilities) if capabilities is not None else None
    report = validate_generation(current, capabilities=profile)
    if not report.valid:
        raise PreflightError(
            "; ".join(
                issue.message for issue in report.issues if issue.severity == "error"
            )
        )
    return GenerationCommitment(
        provider=current.provider,
        endpoint=current.endpoint,
        model=current.model,
        settings=current.generation,
        capabilities=profile,
        requested_sha256=_requested_digest(
            current.provider, current.endpoint, current.model, current.generation
        ),
    )


def validate_generation_compatibility(
    commitment: GenerationCommitment,
    spec: RunSpecification,
    *,
    capabilities: GenerationCapabilities | None = None,
) -> ValidationReport:
    """Compare frozen intent and apply current generation capability facts.

    Current capabilities are deliberately not inferred from frozen evidence.
    Newly known incompatibilities block further preparation; unknowns retain
    the run's strict-capabilities policy. Successful validation covers this
    generation section only. Full source-integrity checks remain a prerequisite
    for operational resume and are not implemented by this helper.
    """
    frozen = _revalidate_record(commitment)
    current = _revalidate_record(spec)
    profile = _revalidate_record(capabilities) if capabilities is not None else None
    issues = list(validate_generation(current, capabilities=profile).issues)
    scope = (current.provider, current.endpoint, current.model)
    if scope != (frozen.provider, frozen.endpoint, frozen.model):
        issues.append(
            ValidationIssue(
                code="generation_scope_changed",
                field="generation",
                message="Provider, endpoint or model differs from the frozen generation scope; start a new run",
                severity="error",
            )
        )
    elif _requested_digest(*scope, current.generation) != frozen.requested_sha256:
        issues.append(
            ValidationIssue(
                code="generation_settings_changed",
                field="generation",
                message="Requested generation settings differ from the frozen manifest; start a new run",
                severity="error",
            )
        )
    return ValidationReport(issues=tuple(issues))
