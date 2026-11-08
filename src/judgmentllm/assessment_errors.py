"""Fixed assessment error codes without provider or evidence dependencies."""

from __future__ import annotations

from typing import Literal

from pydantic import ValidationError

AssessmentDiagnosticCode = Literal[
    "valid",
    "no_text",
    "json_syntax",
    "root_type",
    "duplicate_keys",
    "invalid_utf8",
    "missing_fields",
    "extra_fields",
    "category_error",
    "trait_error",
    "winner_error",
    "invalid_structure",
    "incomplete",
    "refusal",
    "truncated",
    "unsupported_components",
    "multiple_outputs",
    "oversized",
]


class AssessmentOutputError(ValueError):
    """Existing public error message plus a fixed, non-content diagnostic code."""

    diagnostic_code: AssessmentDiagnosticCode

    def __init__(self, message: str, diagnostic_code: AssessmentDiagnosticCode) -> None:
        super().__init__(message)
        self.diagnostic_code = diagnostic_code


def contract_error_code(
    error: Exception, *, task_kind: Literal["rubric", "pairwise"] | None = None
) -> AssessmentDiagnosticCode:
    """Classify an already-rejected canonical output; never establish validity."""
    if isinstance(error, AssessmentOutputError):
        return error.diagnostic_code
    if isinstance(error, ValidationError):
        errors = error.errors(
            include_input=False, include_context=False, include_url=False
        )
        kinds = {item["type"] for item in errors}
        if "model_type" in kinds and any(not item["loc"] for item in errors):
            return "root_type"
        if "missing" in kinds:
            return "missing_fields"
        if "extra_forbidden" in kinds:
            return "extra_fields"
        locations = {part for item in errors for part in item["loc"]}
        if "too_short" in kinds and locations & {"scores", "explanations"}:
            return "trait_error"
        if "category_label" in locations:
            return "category_error"
        if "trait_id" in locations:
            return "trait_error"
        if "winner_id" in locations or "tie" in locations:
            return "winner_error"
        if task_kind == "pairwise" and "value_error" in kinds:
            return "winner_error"
        return "invalid_structure"
    if isinstance(error, KeyError):
        return "missing_fields"
    # Only compare fixed prefixes from existing canonical validators. No
    # exception message, offending value, path, or excerpt leaves this function.
    message = str(error)
    if message.startswith("unknown category "):
        return "category_error"
    if message.startswith(
        (
            "categorical output has duplicate trait IDs",
            "categorical output trait mismatch:",
            "explanations must cover",
            "unknown score trait",
        )
    ):
        return "trait_error"
    if message.startswith(("winner_id must be", "tie output requires")):
        return "winner_error"
    if message.startswith("explanation field is not permitted"):
        return "extra_fields"
    if message.startswith("explanation required"):
        return "missing_fields"
    return "invalid_structure"
