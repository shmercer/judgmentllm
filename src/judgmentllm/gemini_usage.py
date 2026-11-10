"""Documented Developer usage counters; no totals, prices, or zeros inferred."""

from __future__ import annotations

from judgmentllm.evidence._base import EvidenceModel
from judgmentllm.evidence.usage import (
    ReportedUsageInput,
    TokenCount,
    _breakdown,
)

# pyright: reportPrivateUsage=false


class GeminiUsageInput(EvidenceModel):
    """Original REST names, with candidate output separate from thinking output."""

    promptTokenCount: TokenCount | None = None
    candidatesTokenCount: TokenCount | None = None
    thoughtsTokenCount: TokenCount | None = None
    cachedContentTokenCount: TokenCount | None = None
    totalTokenCount: TokenCount | None = None
    toolUsePromptTokenCount: TokenCount | None = None


def interpret_gemini_usage(usage: GeminiUsageInput) -> ReportedUsageInput:
    """Map reported facts once per physical observation, independent of traits.

    Prompt includes cached input; candidate output excludes thought tokens.
    Total, when reported, includes prompt, candidates, thoughts and tool prompts.
    Missing counters stay unknown. No precise monetary cost is inferred.
    """
    usage = GeminiUsageInput.model_validate(dict(usage))
    return ReportedUsageInput(
        usage_source="gemini.generate_content.usageMetadata",
        input_tokens=usage.promptTokenCount,
        output_tokens=usage.candidatesTokenCount,
        total_tokens=usage.totalTokenCount,
        reasoning_tokens=usage.thoughtsTokenCount,
        cache_read_tokens=usage.cachedContentTokenCount,
        breakdown=_breakdown(
            "gemini.generate_content.usageMetadata",
            (
                ("promptTokenCount", usage.promptTokenCount),
                ("candidatesTokenCount", usage.candidatesTokenCount),
                ("totalTokenCount", usage.totalTokenCount),
                ("thoughtsTokenCount", usage.thoughtsTokenCount),
                ("cachedContentTokenCount", usage.cachedContentTokenCount),
                ("toolUsePromptTokenCount", usage.toolUsePromptTokenCount),
            ),
        ),
    )
