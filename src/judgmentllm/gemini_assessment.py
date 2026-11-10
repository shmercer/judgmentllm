"""Content-free diagnostics for exact, separately recognized assessment text.

These diagnostics do not repair output, authorize retries, or replace canonical
assessment validation. Provider envelopes and reasoning are never parser input.
"""

from __future__ import annotations

from typing import Annotated, Literal, Self, cast

from pydantic import Field, ModelWrapValidatorHandler, model_validator

from judgmentllm.assessment_errors import (
    AssessmentDiagnosticCode,
)
from judgmentllm.evidence._base import EvidenceIdentity, EvidenceModel
from judgmentllm.models import ID
from judgmentllm.persistence.models import SHA256, ArtifactIdentity

MAX_ASSESSMENT_COMPONENT_BYTES = 1_048_576
ExtractionStatus = Literal[
    "extracted",
    "no_text",
    "multiple_outputs",
    "unsupported_components",
    "invalid_utf8",
    "oversized",
]
AssessmentSource = Literal[
    "gemini.generate_content.candidates.content.parts.text",
    "unavailable",
]


class GeminiAssessmentComponentMetadataV3(EvidenceModel):
    """Safe extraction facts without generated text or arbitrary field values."""

    endpoint: Literal["models.generate_content"]
    component_version: Literal[3] = 3
    finish_reason: (
        Literal[
            "STOP",
            "MAX_TOKENS",
            "SAFETY",
            "RECITATION",
            "LANGUAGE",
            "OTHER",
            "BLOCKLIST",
            "PROHIBITED_CONTENT",
            "SPII",
            "MALFORMED_FUNCTION_CALL",
            "IMAGE_SAFETY",
            "UNEXPECTED_TOOL_CALL",
            "TOO_MANY_TOOL_CALLS",
            "IMAGE_PROHIBITED_CONTENT",
            "NO_IMAGE",
            "IMAGE_RECITATION",
            "IMAGE_OTHER",
            "CONTINUATION",
            "FINISH_REASON_UNSPECIFIED",
            "unknown",
        ]
        | None
    ) = None
    prompt_block_reason: (
        Literal[
            "BLOCKED_REASON_UNSPECIFIED",
            "SAFETY",
            "OTHER",
            "BLOCKLIST",
            "PROHIBITED_CONTENT",
            "IMAGE_SAFETY",
            "unknown",
        ]
        | None
    ) = None
    source: AssessmentSource = "unavailable"
    extraction_status: ExtractionStatus
    byte_length: int | None = Field(default=None, ge=0)
    item_index: int | None = Field(default=None, ge=0)
    content_index: int | None = Field(default=None, ge=0)
    item_count: int = Field(default=0, ge=0)
    item_types: tuple[Literal["message", "reasoning", "tool", "unsupported"], ...] = ()
    content_types: tuple[
        Literal["output_text", "refusal", "tool", "unsupported"], ...
    ] = ()
    message_count: int = Field(default=0, ge=0)
    content_component_count: int = Field(default=0, ge=0)
    text_component_count: int = Field(default=0, ge=0)
    reasoning_present: bool = False
    refusal_present: bool = False
    tool_present: bool = False
    unsupported_present: bool = False

    @model_validator(mode="wrap")
    @classmethod
    def strict_component_version_token(
        cls, value: object, handler: ModelWrapValidatorHandler[Self]
    ) -> Self:
        # Preserve the original token before EvidenceModel restores JSON fields.
        if isinstance(value, dict) and "component_version" in value:
            token = cast(dict[str, object], value)["component_version"]
            if type(token) is not int or token != 3:
                raise ValueError("unsupported assessment component version")
        return handler(value)

    @model_validator(mode="after")
    def _metadata_consistent(self) -> GeminiAssessmentComponentMetadataV3:
        expected_source = "gemini.generate_content.candidates.content.parts.text"
        located = self.extraction_status in {"extracted", "invalid_utf8", "oversized"}
        if (
            (self.source != "unavailable") != located
            or (located and self.source != expected_source)
            or self.message_count > self.item_count
            or self.text_component_count > self.content_component_count
            or self.item_types != tuple(sorted(set(self.item_types)))
            or self.content_types != tuple(sorted(set(self.content_types)))
            or len(self.item_types) > self.item_count
            or ("reasoning" in self.item_types and not self.reasoning_present)
            or ("tool" in self.item_types and not self.tool_present)
        ):
            raise ValueError("invalid assessment extraction metadata")
        if located:
            if (
                self.item_index is None
                or self.item_index >= self.item_count
                or self.text_component_count != 1
                or self.message_count != 1
                or (
                    self.endpoint == "models.generate_content"
                    and (
                        self.content_index is None
                        or self.content_index >= self.content_component_count
                    )
                )
            ):
                raise ValueError("invalid assessment extraction locator")
        elif (
            self.item_index is not None
            or self.content_index is not None
            or self.byte_length is not None
        ):
            raise ValueError("unlocated assessment extraction has a locator")
        if (
            (
                self.extraction_status == "extracted"
                and (
                    self.byte_length is None
                    or self.byte_length > MAX_ASSESSMENT_COMPONENT_BYTES
                )
            )
            or (
                self.extraction_status == "oversized"
                and (
                    self.byte_length is None
                    or self.byte_length <= MAX_ASSESSMENT_COMPONENT_BYTES
                )
            )
            or (
                self.extraction_status == "invalid_utf8"
                and self.byte_length is not None
            )
            or (self.extraction_status == "no_text" and self.text_component_count != 0)
        ):
            raise ValueError("invalid assessment extraction length or count")
        return self


class GeminiAssessmentComponentV3(GeminiAssessmentComponentMetadataV3):
    """Transient exact text; serialization exposes safe metadata only."""

    output_text: bytes | None = Field(default=None, exclude=True, repr=False)

    @model_validator(mode="after")
    def _consistent(self) -> GeminiAssessmentComponentV3:
        if self.extraction_status == "extracted":
            if self.output_text is None or self.byte_length != len(self.output_text):
                raise ValueError("invalid assessment extraction metadata")
        elif self.output_text is not None:
            raise ValueError("withheld assessment extraction contains text")
        return self


class GeminiAssessmentDiagnosticV3(EvidenceModel):
    """Versioned content-free receipt for an explicitly identified text component.

    The artifact is untrusted model output. Its presence never grants scoring,
    recovery, retry, or qualification authority. Text is never embedded here.
    """

    diagnostic_version: Literal[3] = 3
    identity: EvidenceIdentity
    provider: Literal["gemini"] = "gemini"
    endpoint: Literal["models.generate_content"]
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
    component: GeminiAssessmentComponentMetadataV3
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
            if type(token) is not int or token != 3:
                raise ValueError("unsupported assessment diagnostic version")
        return handler(value)

    @model_validator(mode="after")
    def consistent(self) -> GeminiAssessmentDiagnosticV3:
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
