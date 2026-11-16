"""Strict version tokens for new native records; historical models stay intact."""

from __future__ import annotations

from pydantic import field_validator

from judgmentllm.persistence.models import ExecutionModel


class DecisionsModel(ExecutionModel):
    @field_validator(
        "decisions_commitment_version",
        "limit_version",
        "pair_presentation_version",
        "presentation_membership_version",
        "outcome_vector_version",
        "decisions_response_version",
        "question_provenance_version",
        "physical_provenance_version",
        "provenance_manifest_version",
        "provenance_report_version",
        "normalized_result_schema",
        "invocation_evidence_version",
        mode="before",
        check_fields=False,
    )
    @classmethod
    def exact_version_token(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("Decisions versions must be strict integers")
        return value
