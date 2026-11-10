"""Lazy concrete Gemini types for shared annotations and protocol dispatch.

The stub exposes exact types to static checkers. Loading this namespace does not
load the provider transport, avoiding preparation/evidence import cycles. Runtime
dispatch resolves only the requested concrete type through normal Python imports.
"""

from __future__ import annotations

from importlib import import_module

_MODULES = {
    "GeminiProductionContext": "gemini_transport",
    "OutputCommitmentV6": "gemini_transport",
    "PreparedRequestV6": "gemini_transport",
    "TransportCommitmentV4": "gemini_transport",
    "RenderedGeminiPromptV1": "gemini_prompts",
    "GeminiAssessmentComponentV3": "gemini_assessment",
    "GeminiAssessmentComponentMetadataV3": "gemini_assessment",
    "GeminiAssessmentDiagnosticV3": "gemini_assessment",
    "ExecutableRunManifestV6": "preparation.manifest",
}


def __getattr__(name: str) -> object:
    module = _MODULES.get(name)
    if module is None:
        raise AttributeError("Unknown Gemini type")
    return getattr(import_module("judgmentllm." + module), name)
