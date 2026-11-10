"""Independent Vertex authority: no live-qualified production model or route.

Developer API evidence, local projections, SDK acceptance, credentials and model
catalogs supply no Vertex authority. A future reviewed change must define exact
model/region/auth/feature admission before this empty registry can be extended.
"""

from __future__ import annotations

from typing import Literal

from judgmentllm.generation import GenerationCapabilities, GenerationParameterCapability
from judgmentllm.native_schema import NativeProjectionScopeV1

VERTEX_PRODUCTION_SDK_VERSION = "2.34.0"
VERTEX_API_VERSION = "v1beta1"
VERTEX_API_SCOPE = "vertex-generate-content-v1beta1-v1"
VertexMechanism = Literal[
    "vertex.generate_content.response_schema",
    "vertex.generate_content.response_json_schema",
]

# Deliberately no import/registration hook or inferred positive profiles.
_QUALIFIED_PROFILES: tuple[()] = ()


def production_generation_capabilities(
    scope: NativeProjectionScopeV1,
) -> GenerationCapabilities:
    """Documented mappings and known incompatibilities, with no live qualification.

    Unknown model limits remain unknown. These facts do not establish model
    eligibility, thinking, caching, tier or candidate support.
    """
    # Current serving documentation supersedes older sampling examples for
    # these exact named models. Ignored controls violate omission/value fidelity.
    sampling_ignored = scope.model in {
        "gemini-3.6-flash",
        "gemini-3.8-flash",
        "gemini-4-carbon",
        "gemini-4-argon",
    }
    sampling_documented = scope.model in {"gemini-2.5-flash", "gemini-2.5-pro"}
    sampling = False if sampling_ignored else True if sampling_documented else None
    return GenerationCapabilities(
        provider="vertex",
        endpoint="models.generate_content",
        model=scope.model or "unknown",
        temperature=GenerationParameterCapability(
            supported=sampling, minimum=0, maximum=2
        ),
        top_p=GenerationParameterCapability(supported=sampling, minimum=0, maximum=1),
        max_output_tokens=GenerationParameterCapability(supported=True, minimum=1),
        source="vertex.generate-content.v1beta1",
        fixture_tested=True,
        live_qualified=False,
    )
