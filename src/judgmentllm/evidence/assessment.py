"""Protected assessment-output diagnostics, separate from scoring and envelopes."""

from __future__ import annotations

from typing import Annotated, Literal, Self, cast

from pydantic import Field, ModelWrapValidatorHandler, field_validator, model_validator

from judgmentllm.assessment_diagnostics import (
    MAX_ASSESSMENT_COMPONENT_BYTES,
    AssessmentComponentMetadata,
    AssessmentDiagnosticCode,
)
from judgmentllm.evidence._base import EvidenceIdentity, EvidenceModel
from judgmentllm.models import ID
from judgmentllm.persistence.models import SHA256, ArtifactIdentity


class AssessmentDiagnosticV1(EvidenceModel):
    """Versioned content-free receipt for an explicitly identified text component.

    The artifact is untrusted model output. Its presence never grants scoring,
    recovery, retry, or qualification authority. Text is never embedded here.
    """

    diagnostic_version: Literal[1] = 1
    identity: EvidenceIdentity
    provider: Literal["openai"] = "openai"
    endpoint: Literal["responses", "chat.completions"]
    provider_outcome: (
        Literal[
            "completed",
            "refusal",
            "content_filter",
            "output_limit",
            "incomplete",
            "provider_failure",
            "missing_output",
            "multiple_outputs",
            "unsupported_output",
            "tool_call",
        ]
        | None
    ) = None
    requested_model: ID
    reported_model: ID | None = None
    remote_response_id: ID
    component: AssessmentComponentMetadata
    parser_diagnostic: AssessmentDiagnosticCode
    canonical_valid: bool
    capture: Literal["full", "minimal"]
    retention: Literal["retained", "hash_only", "withheld"]
    artifact: ArtifactIdentity | None = None
    content_sha256: SHA256 | None = None
    byte_length: Annotated[int, Field(ge=0)] | None = None

    @model_validator(mode="wrap")
    @classmethod
    def strict_version_token(
        cls, value: object, handler: ModelWrapValidatorHandler[Self]
    ) -> Self:
        # Check before EvidenceModel's JSON-mode field adapters restore tuples
        # and dates: Literal[1] alone may otherwise normalize true/1.0 to 1.
        if isinstance(value, dict) and "diagnostic_version" in value:
            token = cast(dict[str, object], value)["diagnostic_version"]
            if type(token) is not int or token != 1:
                raise ValueError("unsupported assessment diagnostic version")
        return handler(value)

    @field_validator("diagnostic_version", mode="before")
    @classmethod
    def strict_diagnostic_version(cls, value: object) -> object:
        if type(value) is not int or value != 1:
            raise ValueError("unsupported assessment diagnostic version")
        return value

    @model_validator(mode="after")
    def consistent(self) -> AssessmentDiagnosticV1:
        if (
            self.identity.observation_id is None
            or self.component.endpoint != self.endpoint
        ):
            raise ValueError("diagnostic requires a matching observation")
        if self.byte_length != self.component.byte_length:
            raise ValueError("diagnostic component length differs")
        if self.retention == "withheld":
            if self.artifact is not None or self.content_sha256 is not None:
                raise ValueError("withheld output cannot retain text or its digest")
        else:
            if (
                self.component.extraction_status != "extracted"
                or self.byte_length is None
                or self.byte_length > MAX_ASSESSMENT_COMPONENT_BYTES
                or self.content_sha256 is None
                or self.artifact is None
                or self.artifact.sha256 != self.content_sha256
                or self.artifact.byte_length != self.byte_length
                or self.artifact.media_type != "text/plain; charset=utf-8"
            ):
                raise ValueError("diagnostic text identity is inconsistent")
            expected_path = f"objects/{self.content_sha256[:2]}/{self.content_sha256}"
            if self.retention == "retained":
                if (
                    self.capture != "full"
                    or self.artifact.relative_path != expected_path
                ):
                    raise ValueError("retained text requires full capture")
            elif self.capture != "minimal" or self.artifact.relative_path is not None:
                raise ValueError("minimal diagnostic requires hash-only identity")
        if (self.parser_diagnostic == "valid") != self.canonical_valid:
            raise ValueError("parser status conflicts with canonical validity")
        if self.canonical_valid and self.component.extraction_status != "extracted":
            raise ValueError("validation requires an extracted assessment component")
        return self
