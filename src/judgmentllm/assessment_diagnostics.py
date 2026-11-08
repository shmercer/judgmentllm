"""Content-free diagnostics for exact, separately recognized assessment text.

These diagnostics do not repair output, authorize retries, or replace canonical
assessment validation. Provider envelopes and reasoning are never parser input.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from pydantic import Field, model_validator

from judgmentllm.assessment_errors import (
    AssessmentDiagnosticCode,
    contract_error_code,
)
from judgmentllm.evidence._base import EvidenceModel

if TYPE_CHECKING:
    from judgmentllm.output_contracts import AssessmentOutputContract

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
    "responses.output.message.content.output_text",
    "chat.completions.choices.message.content",
    "unavailable",
]


class AssessmentComponentMetadata(EvidenceModel):
    """Safe extraction facts without generated text or arbitrary field values."""

    endpoint: Literal["responses", "chat.completions"]
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

    @model_validator(mode="after")
    def _metadata_consistent(self) -> AssessmentComponentMetadata:
        expected_source = (
            "responses.output.message.content.output_text"
            if self.endpoint == "responses"
            else "chat.completions.choices.message.content"
        )
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
                    self.endpoint == "responses"
                    and (
                        self.content_index is None
                        or self.content_index >= self.content_component_count
                    )
                )
                or (
                    self.endpoint == "chat.completions"
                    and self.content_index is not None
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
            or (
                self.extraction_status in {"no_text", "unsupported_components"}
                and self.text_component_count != 0
            )
        ):
            raise ValueError("invalid assessment extraction length or count")
        return self


class AssessmentComponent(AssessmentComponentMetadata):
    """Transient exact text; serialization exposes safe metadata only."""

    output_text: bytes | None = Field(default=None, exclude=True, repr=False)

    @model_validator(mode="after")
    def _consistent(self) -> AssessmentComponent:
        if self.extraction_status == "extracted":
            if (
                self.output_text is None
                or self.byte_length is None
                or self.byte_length != len(self.output_text)
                or self.byte_length > MAX_ASSESSMENT_COMPONENT_BYTES
                or self.source == "unavailable"
                or self.item_index is None
                or self.text_component_count != 1
            ):
                raise ValueError("invalid assessment extraction metadata")
        elif self.output_text is not None:
            raise ValueError("withheld assessment extraction contains text")
        return self


@dataclass(frozen=True)
class AssessmentParseDiagnostic:
    code: AssessmentDiagnosticCode
    canonical_valid: bool


def diagnose_assessment_output(
    component: AssessmentComponent,
    contract: AssessmentOutputContract,
    *,
    provider_outcome: str | None = None,
) -> AssessmentParseDiagnostic:
    """Use the canonical complete-output validator without repair or rescoring."""
    from judgmentllm.output_contracts import (
        RubricOutputContract,
        validate_pairwise_contract_output,
        validate_rubric_contract_output,
    )

    outcomes: dict[str, AssessmentDiagnosticCode] = {
        "refusal": "refusal",
        "content_filter": "refusal",
        "output_limit": "truncated",
        "incomplete": "incomplete",
        "unsupported_output": "unsupported_components",
        "tool_call": "unsupported_components",
        "multiple_outputs": "multiple_outputs",
    }
    outcome_code = outcomes.get(provider_outcome or "")
    if component.extraction_status != "extracted":
        return AssessmentParseDiagnostic(
            outcome_code or component.extraction_status, False
        )
    raw = component.output_text
    if raw is None or not raw.strip():
        return AssessmentParseDiagnostic(outcome_code or "no_text", False)
    try:
        if isinstance(contract, RubricOutputContract):
            validate_rubric_contract_output(raw, contract)
        else:
            validate_pairwise_contract_output(raw, contract)
    except (ValueError, TypeError, RecursionError) as error:
        return AssessmentParseDiagnostic(
            outcome_code or contract_error_code(error), False
        )
    return AssessmentParseDiagnostic(outcome_code or "valid", True)
