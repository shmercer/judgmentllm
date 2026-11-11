"""Synthetic Vertex promotion compatibility outputs for the strict R gate.

Only this offline fixture substitutes synthetic route hashes into private copies
of historical reviewed profiles. Their feature sets remain unchanged: revision 2
must use text until separately qualified. Real credentials are never needed.
"""

from __future__ import annotations

# pyright: reportPrivateUsage=false
import json
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal
from unittest.mock import patch

import httpx2
from vertex_offline_fixture import FixtureTransport, offline, specification

from judgmentllm import vertex_registry as registry
from judgmentllm.assessment_inspection import inspect_assessment_output
from judgmentllm.execution import execute_run, recover_run
from judgmentllm.io import write_results_jsonl
from judgmentllm.persistence.artifacts import ArtifactStore
from judgmentllm.persistence.hashing import canonical_bytes
from judgmentllm.persistence.repository import ReadOnlyRepository
from judgmentllm.persistence.transport_receipts import read_transport_commitment
from judgmentllm.preparation import prepare_run
from judgmentllm.r_exports import export_pairwise_r_csv, export_rubric_r_csv
from judgmentllm.results import PairwiseResult, RubricResult
from judgmentllm.vertex_config import VertexRuntimeConfiguration
from judgmentllm.vertex_transport import (
    TransportCommitmentV5,
    production_context,
    production_context_digest,
    production_context_for_identity,
)

PROJECT = "synthetic-accepted-project"
MODEL = "gemini-3.1-flash-lite"
MECHANISM = "vertex.generate_content.response_json_schema"


@contextmanager
def promoted(mode: Literal["adc", "api_key"]) -> Generator[None]:
    profiles = tuple(
        p.model_copy(
            update={
                "route_sha256": VertexRuntimeConfiguration(
                    project=PROJECT, location="global", auth_mode=p.authentication_mode
                ).route_sha256
            }
        )
        for p in registry._QUALIFIED_PROFILES
    )
    with (
        offline(),
        patch.object(registry, "_QUALIFIED_PROFILES", profiles),
        patch.dict(
            "os.environ",
            {
                "GOOGLE_CLOUD_PROJECT": PROJECT,
                "GOOGLE_CLOUD_LOCATION": "global",
                "JUDGMENTLLM_VERTEX_AUTH_MODE": mode,
            },
        ),
    ):
        from google import auth
        from google.auth.credentials import Credentials

        class SyntheticCredentials(Credentials):
            def __init__(self) -> None:
                super().__init__()
                self.token = "SYNTHETIC_ADC_ACCESS_TOKEN"
                self._quota_project_id = PROJECT

            def refresh(self, request: Any) -> None:
                raise AssertionError("Offline fixture forbids credential refresh")

        credentials = SyntheticCredentials()
        with patch.object(auth, "default", return_value=(credentials, PROJECT)):
            yield


class PromotedTransport(FixtureTransport):
    def respond(self, request: httpx2.Request) -> httpx2.Response:
        response = super().respond(request)
        body = json.loads(response.content)
        body["modelVersion"] = MODEL
        return httpx2.Response(200, json=body, request=request)


async def persist_vertex_promotion(output: Path) -> None:
    output.mkdir(parents=True)
    for mode in ("adc", "api_key"):
        with promoted(mode):
            for capture in ("full", "minimal"):
                for kind in ("rubric", "pairwise"):
                    run_id = f"vertex-promoted-{mode}-{capture}-{kind}"
                    spec = specification(
                        output / "inputs" / run_id,
                        "models.generate_content",
                        kind,
                        traits=1,
                        capture=capture,
                    )
                    spec.model = MODEL
                    spec.execution.grouping = "independent"
                    spec.prompt.structured_output = "auto"
                    spec.prompt.native_mechanism = MECHANISM
                    transport = PromotedTransport("models.generate_content")
                    transport.omit_usage = capture == "minimal"
                    adapter = transport.adapter(spec)
                    try:
                        with prepare_run(
                            spec,
                            store=output / "store",
                            run_id=run_id,
                            transport_context=production_context(spec),
                        ):
                            pass
                        result = await execute_run(
                            run_id, store=output / "store", spec=spec, adapter=adapter
                        )
                        assert not result.recovery_required
                        await recover_run(
                            run_id, store=output / "store", spec=spec, adapter=adapter
                        )
                        assert transport.calls == adapter.actual_call_count == 1
                    finally:
                        await adapter.aclose()


def export_vertex_promotion(output: Path) -> None:
    rubric: list[RubricResult] = []
    pairs: list[PairwiseResult] = []
    evidence: list[dict[str, Any]] = []
    transports: list[dict[str, Any]] = []
    inspections: list[dict[str, Any]] = []
    for mode in ("adc", "api_key"):
        with promoted(mode), ReadOnlyRepository(output / "store") as repository:
            for capture in ("full", "minimal"):
                for kind in ("rubric", "pairwise"):
                    run_id = f"vertex-promoted-{mode}-{capture}-{kind}"
                    receipts = tuple(repository.iter_evidence(run_id))
                    assert len(receipts) == 1
                    receipt = receipts[0]
                    artifacts = ArtifactStore(
                        repository.run_directory(run_id), create=False
                    )
                    transport = read_transport_commitment(
                        artifacts, receipt.identity.attempt_id
                    )
                    assert isinstance(transport, TransportCommitmentV5)
                    runtime = VertexRuntimeConfiguration(
                        project=PROJECT, location="global", auth_mode=mode
                    )
                    profile = registry._profile_for(
                        transport.scope,
                        MECHANISM,
                        route_sha256=runtime.route_sha256,
                        authentication_mode=mode,
                        location="global",
                    )
                    assert profile is not None
                    context = production_context_for_identity(
                        "vertex", "models.generate_content", MODEL, runtime=runtime
                    )
                    assert transport.context_sha256 == production_context_digest(
                        context
                    )
                    assert (
                        transport.presentation_version == 2
                        and transport.selection.authority_kind == "reviewed"
                        and transport.selection.kind == "text"
                        and transport.mechanism == "text"
                        and transport.selection.native_support == "unknown"
                        and "assessment_presentation_v2" in transport.selection.features
                        and "assessment_presentation_v2"
                        not in profile.supported_features
                    )
                    transports.append(
                        {
                            "run_id": run_id,
                            "authentication_mode": mode,
                            "route_sha256": transport.route_sha256,
                            "context_sha256": transport.context_sha256,
                            "mechanism": transport.mechanism,
                            "presentation_version": transport.presentation_version,
                            "qualified_features": list(profile.supported_features),
                            "selection": transport.selection.model_dump(mode="json"),
                            "profile_authority_sha256": profile.authority_sha256,
                            "qualification_source_commit": profile.source_commit,
                            "independent_live_sha256": profile.independent_live_sha256,
                        }
                    )
                    for row in repository.iter_results(run_id):
                        if isinstance(row, RubricResult):
                            rubric.append(row)
                        else:
                            pairs.append(row)
                    evidence.append(receipt.model_dump(mode="json"))
                    observation = receipt.identity.observation_id
                    assert observation is not None
                    inspected = inspect_assessment_output(
                        run_id, observation, store=output / "store", include_text=True
                    )
                    assert (inspected.output_text is not None) == (capture == "full")
                    inspections.append(inspected.model_dump(mode="json"))
    export_rubric_r_csv(output / "rubric.csv", rubric)
    export_pairwise_r_csv(output / "pairs.csv", pairs)
    write_results_jsonl(output / "results.jsonl", [*rubric, *pairs])
    for name, records in (
        ("evidence", evidence),
        ("transports", transports),
        ("inspections", inspections),
    ):
        (output / (name + ".jsonl")).write_bytes(
            b"".join(canonical_bytes(r) + b"\n" for r in records)
        )
