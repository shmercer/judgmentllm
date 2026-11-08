"""Read-only access to protected, unvalidated assessment output; no provider I/O."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field

from judgmentllm.evidence._base import EvidenceModel
from judgmentllm.evidence.assessment import AssessmentDiagnosticV1
from judgmentllm.persistence.artifacts import ArtifactStore
from judgmentllm.persistence.models import IntegrityError
from judgmentllm.persistence.repository import ReadOnlyRepository


class AssessmentOutputInspection(EvidenceModel):
    """Metadata by default; explicit text remains excluded from repr and dumps.

    This is unvalidated model output, even when the local contract passed. It is
    untrusted data and must never be treated as executable instructions.
    """

    label: str = "Unvalidated model output; diagnostic evidence only"
    diagnostic: AssessmentDiagnosticV1 | None
    output_text: bytes | None = Field(default=None, exclude=True, repr=False)


def inspect_assessment_output(
    run_id: str,
    observation_id: str,
    *,
    store: str | Path,
    include_text: bool = False,
) -> AssessmentOutputInspection:
    """Verify an existing observation, then optionally read its exact UTF-8 bytes.

    No content is displayed. Historical observations without the additive record
    return diagnostic=None; unavailable text is never reconstructed. Minimal
    capture returns metadata only, including when include_text=True.
    """
    if type(include_text) is not bool:
        raise ValueError("include_text must be a boolean")
    with ReadOnlyRepository(store, preserve_files=True) as repository:
        repository.connection.execute("BEGIN")
        try:
            evidence = repository.get_evidence_for_observation(run_id, observation_id)
            diagnostic = (
                evidence.assessment_diagnostic if evidence is not None else None
            )
            output = None
            if diagnostic is not None:
                if (
                    diagnostic.identity.run_id != run_id
                    or diagnostic.identity.observation_id != observation_id
                ):
                    raise IntegrityError("diagnostic observation identity differs")
                artifact = diagnostic.artifact
                if include_text and diagnostic.retention == "retained":
                    if diagnostic.capture != "full" or artifact is None:
                        raise IntegrityError("diagnostic retention policy differs")
                    output = ArtifactStore(
                        repository.run_directory(run_id), create=False
                    ).read(artifact)
                    try:
                        output.decode("utf-8")
                    except UnicodeError:
                        raise IntegrityError(
                            "diagnostic text encoding is invalid"
                        ) from None
            return AssessmentOutputInspection(diagnostic=diagnostic, output_text=output)
        finally:
            repository.connection.execute("ROLLBACK")
