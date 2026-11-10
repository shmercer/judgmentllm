"""Lazy concrete Vertex types for shared annotations and protocol dispatch.

The stub exposes exact types to static checkers. Loading this namespace does not
load the provider transport, avoiding preparation/evidence import cycles. Runtime
dispatch resolves only the requested concrete type through normal Python imports.
"""

from __future__ import annotations

from importlib import import_module

_MODULES = {
    "VertexProductionContext": "vertex_transport",
    "OutputCommitmentV7": "vertex_transport",
    "PreparedRequestV7": "vertex_transport",
    "TransportCommitmentV5": "vertex_transport",
    "RenderedVertexPromptV1": "vertex_prompts",
    "VertexAssessmentComponentV4": "vertex_assessment",
    "VertexAssessmentComponentMetadataV4": "vertex_assessment",
    "VertexAssessmentDiagnosticV4": "vertex_assessment",
    "ExecutableRunManifestV7": "preparation.manifest",
}


def __getattr__(name: str) -> object:
    module = _MODULES.get(name)
    if module is None:
        raise AttributeError("Unknown Vertex type")
    return getattr(import_module("judgmentllm." + module), name)
