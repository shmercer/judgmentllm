"""Create an immutable deferred Vertex proposal offline; no execution command.

An eligible project/authentication mode and exact serving model have not been
established. Never invent executable requests or rates from Developer evidence.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Annotated, Literal

from pydantic import ConfigDict, Field

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from judgmentllm.models import StrictModel  # noqa: E402
from judgmentllm.persistence.hashing import bytes_digest, canonical_bytes  # noqa: E402
from judgmentllm.vertex_config import VertexRuntimeConfiguration  # noqa: E402


class DeferredProposal(StrictModel):
    """Independent version 1, zero requests authorized, no credentials/projects."""

    model_config = ConfigDict(frozen=True)
    proposal_version: Literal[1] = 1
    provider: Literal["vertex"] = "vertex"
    source_commit: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    status: Literal["deferred_setup_and_model_eligibility_unestablished"] = (
        "deferred_setup_and_model_eligibility_unestablished"
    )
    authentication_mode: Literal["adc", "api_key"]
    location: str
    api_version: Literal["v1beta1"] = "v1beta1"
    sdk_package: Literal["google-genai"] = "google-genai"
    sdk_version: Literal["2.34.0"] = "2.34.0"
    endpoint: Literal["models.generate_content"] = "models.generate_content"
    serving_model: Literal[None] = None
    candidate_native_mechanism: Literal[
        "vertex.generate_content.response_json_schema"
    ] = "vertex.generate_content.response_json_schema"
    proposed_cases: tuple[
        Literal["rubric-text", "rubric-native", "pairwise-text", "pairwise-native"], ...
    ] = ("rubric-text", "rubric-native", "pairwise-text", "pairwise-native")
    physical_requests: tuple[()] = ()
    physical_request_hashes: tuple[()] = ()
    maximum_proposed_requests: Literal[4] = 4
    maximum_proposed_output_tokens_per_request: Literal[512] = 512
    concurrency: Literal[1] = 1
    retries: Literal[0] = 0
    authorized_requests: Literal[0] = 0
    execution_enabled: Literal[False] = False
    input_reservation_tokens: Literal[None] = None
    estimated_cost_usd: Literal[None] = None
    input_usd_per_million: Literal[None] = None
    output_usd_per_million: Literal[None] = None
    cost_policy: Literal[
        "review_standard_rates_no_cache_or_batch_discount_include_reported_thinking"
    ] = "review_standard_rates_no_cache_or_batch_discount_include_reported_thinking"
    required_capture_authorization: Literal[
        "full_synthetic_assessment_capture_reasoning_none"
    ] = "full_synthetic_assessment_capture_reasoning_none"
    prerequisites: tuple[str, ...] = (
        "Establish eligible project, billing, selected authentication mode, IAM/key restrictions and location independently.",
        "Review current official exact serving-model availability and native mechanism eligibility for this route and API version.",
        "Prepare fresh source-bound complete physical requests/hashes using only synthetic items 001/002, ordinal trait 01 and forced-choice winners.",
        "Audit input framing/token reservations and current standard input/output/thinking prices; agree a conservative monetary estimate.",
        "Independently review executor implementation and obtain exact-plan/source billable and full synthetic capture authorization.",
    )


def source_commit(root: Path) -> str:
    """Read Git metadata only; require the exact clean committed local source."""
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=normal"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        if dirty:
            raise ValueError
        return head
    except Exception:
        raise ValueError(
            "Vertex proposal requires clean committed exact source"
        ) from None


def prepare_proposal(
    *,
    source: str,
    authentication_mode: Literal["adc", "api_key"],
    location: str,
    output: Path,
    root: Path,
) -> str:
    if source_commit(root) != source:
        raise ValueError("Vertex proposal source differs from current committed source")
    # Validate the explicitly supplied route without environment or credential inspection.
    VertexRuntimeConfiguration(
        project="synthetic-proposal", location=location, auth_mode=authentication_mode
    )
    proposal = DeferredProposal(
        source_commit=source, authentication_mode=authentication_mode, location=location
    )
    raw = canonical_bytes(proposal)
    digest = bytes_digest(raw)
    output.mkdir(parents=True, exist_ok=False)
    for name, body in (
        ("proposal.json", raw),
        ("proposal.sha256", (digest + "\n").encode()),
    ):
        with (output / name).open("xb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
    return digest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan",))
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--auth-mode", choices=("adc", "api_key"), required=True)
    parser.add_argument("--location", required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    options = parser.parse_args()
    try:
        digest = prepare_proposal(
            source=options.source_commit,
            authentication_mode=options.auth_mode,
            location=options.location,
            output=options.evidence_dir,
            root=Path(__file__).resolve().parents[1],
        )
    except (ValueError, OSError):
        parser.exit(
            2,
            "Vertex proposal requires clean exact source, valid explicit mode/location and a new evidence directory.\n",
        )
    print(digest)


if __name__ == "__main__":
    main()
