"""Synthetic study qualification fixtures; no credential or external network access."""

from __future__ import annotations

import importlib
import json
import shutil

# pyright: reportPrivateUsage=false
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, cast

import httpx2

from judgmentllm.config import RunSpecV3, validate_run_spec_json
from judgmentllm.persistence.hashing import bytes_digest, canonical_bytes
from judgmentllm.study.execution import CredentialBinding
from judgmentllm.study.records import (
    Authorization,
    Case,
    Eligibility,
    OutcomePrice,
    Price,
    Proposal,
)
from judgmentllm.study.sources import archive

PACKAGE = Path(__file__).resolve().parents[1] / "examples/study-native"
SOURCE = "a" * 40
KEY = "SYNTHETIC_STUDY_KEY"


def original_files(source: Path) -> None:
    """Reconstruct both synthetic originals, including their exact manifests."""
    for kind in ("rubric", "cj"):
        shutil.copytree(
            PACKAGE / "sources" / kind, source / f"judgmentllm_{kind}_study_renderer_v3"
        )
        for script in (source / f"judgmentllm_{kind}_study_renderer_v3/scripts").glob(
            "*.py.txt"
        ):
            script.rename(script.with_suffix(""))
    for entry in archive(PACKAGE / "original-support-files.jsonl.gz.base64"):
        path = source / f"judgmentllm_{entry['kind']}_study_renderer_v3" / entry["name"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(entry["data_utf8"].encode())
    for entry in archive(PACKAGE / "original-previews.jsonl.gz.base64"):
        directory = (
            source / f"judgmentllm_{entry['kind']}_study_renderer_v3/rendered_samples"
        )
        directory.mkdir(parents=True, exist_ok=True)
        (directory / (entry["name"] + ".txt")).write_bytes(entry["text"].encode())
        (directory / (entry["name"] + ".schema.json")).write_bytes(
            entry["schema_text"].encode()
        )


def seal(proposal: Proposal, proofs: Path) -> Proposal:
    """Independent synthetic account/pricing evidence, never production grants."""
    proofs.mkdir(parents=True, exist_ok=True)

    def proof(value: object) -> str:
        raw = canonical_bytes(value)
        digest = bytes_digest(raw)
        (proofs / (digest + ".json")).write_bytes(raw)
        return digest

    calls: list[Case] = []
    for case in proposal.calls:
        source = cast(
            Literal["OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY"],
            {
                "openai": "OPENAI_API_KEY",
                "anthropic": "ANTHROPIC_API_KEY",
                "gemini": "GOOGLE_API_KEY",
            }[case.selection.provider],
        )
        account_evidence = proof(
            {
                "synthetic": True,
                "selection": case.selection.model_dump(mode="json"),
                "account": case.account_scope,
            }
        )
        binding = CredentialBinding(
            provider=case.selection.provider,
            account_scope=case.account_scope,
            credential_source=source,
            credential_fingerprint_sha256=bytes_digest(
                canonical_bytes(["study-account-binding-v1", case.account_scope, KEY])
            ),
            account_evidence_sha256=account_evidence,
            verified_on=datetime(2026, 11, 12, tzinfo=UTC),
            reviewed_by="synthetic-reviewer",
        )
        eligibility = Eligibility(
            selection=case.selection,
            account_scope=case.account_scope,
            sdk_version=case.sdk_version,
            api_version=case.api_version,
            mechanism=case.mechanism,
            features=case.features,
            credential_source=source,
            recorded_on=datetime(2026, 11, 12).date(),
            reviewed_by="synthetic-reviewer",
            evidence_sha256=account_evidence,
            binding_sha256=proof(binding),
            input_bound_verified=True,
            billable_output_bound=8192,
            served_outcomes_verified=True,
            billable_served_tiers=("standard", "priority")
            if case.selection.provider == "anthropic"
            else ("standard", "flex", "priority"),
        )
        price = Price(
            selection=case.selection,
            account_scope=case.account_scope,
            recorded_on=datetime(2026, 11, 12).date(),
            outcomes=tuple(
                OutcomePrice(
                    served_tier=t,
                    input_per_million=Decimal("0.1"),
                    billable_output_per_million=Decimal("0.2"),
                )
                for t in ("standard", "flex", "priority")
            ),
            evidence_sha256=proof({"synthetic_price": True}),
            reviewed_by="synthetic-reviewer",
        )
        calls.append(
            case.model_copy(
                update={
                    "eligibility": eligibility,
                    "price": price,
                    "reserved_usd": price.reservation(32768, 8192),
                    "blockers": (),
                }
            )
        )
    return Proposal.model_validate_json(
        canonical_bytes(
            proposal.model_copy(update={"calls": tuple(calls), "status": "sealed"})
        )
    )


def authorization(proposal: Proposal) -> Authorization:
    return Authorization(
        proposal_sha256=proposal.digest,
        source_commit=proposal.source_commit,
        suite=proposal.suite,
        maximum_calls=proposal.max_calls,
        ceiling_usd=proposal.ceiling_usd,
        elapsed_seconds=proposal.elapsed_seconds,
        accept_billable_calls=True,
        accept_synthetic_capture=True,
        authorized_by="synthetic-reviewer",
        authorized_at=datetime(2026, 11, 12, tzinfo=UTC),
    )


def spec_for(proposal: Proposal, index: int = 0) -> RunSpecV3:
    spec = validate_run_spec_json(proposal.calls[index].specification_json)
    assert isinstance(spec, RunSpecV3)
    return spec


def assessment(spec: RunSpecV3, traits: tuple[str, ...] | None = None) -> str:
    if spec.task_kind == "pairwise":
        return '{"winner_id":"essay_01"}'
    assert spec.rubric is not None
    ids = traits or tuple(t.trait_id for t in spec.rubric.traits)
    rows: list[dict[str, Any]] = []
    for trait in ids:
        row = {"trait_id": trait, "category_label": "2"}
        if spec.execution.explanations:
            row["explanation"] = "The synthetic essay fulfills this criterion."
        rows.append(row)
    return canonical_bytes({"scores": rows}).decode()


def case_files(
    root: Path, pilot: tuple[Path, Proposal], index: int
) -> tuple[Path, Path, Proposal, Case, RunSpecV3]:
    proofs = root / "proofs"
    proposal = seal(pilot[1], proofs)
    case = proposal.calls[index]
    spec = spec_for(proposal, index)
    directory = root / "proposal"
    directory.mkdir()
    shutil.copytree(
        pilot[0] / "preparation" / case.case_id,
        directory / "preparation" / case.case_id,
    )
    proposal = Proposal.model_validate_json(
        canonical_bytes(
            proposal.model_copy(
                update={
                    "calls": (case,) + tuple(c for c in proposal.calls if c != case)
                }
            )
        )
    )
    (directory / "proposal.json").write_bytes(canonical_bytes(proposal))
    return directory, proofs, proposal, case, spec


async def sdk_bytes(spec: RunSpecV3, wires: list[Any]) -> None:
    """Actual installed SDK HTTP serialization with the production argument builders."""
    from reasoning_fixture import response_body

    module = importlib.import_module("judgmentllm.providers." + spec.provider)
    sdk, http = module._sdk()
    seen: list[bytes] = []

    def handler(request: Any) -> Any:
        expected = wires[len(seen)]
        assert request.method == "POST"
        if spec.provider == "anthropic":
            assert request.headers["anthropic-version"] == "2026-10-21"
            assert request.url.path == "/v1/messages"
        elif spec.provider == "openai":
            assert request.url.path == "/v1/responses"
        else:
            assert request.url.path == f"/v1beta/models/{spec.model}:generateContent"
        assert request.content == expected.payload
        seen.append(bytes(request.content))
        body, headers = response_body(
            spec,
            '{"winner_id":"essay_01"}'
            if spec.task_kind == "pairwise"
            else '{"scores":[]}',
        )
        return httpx2.Response(200, json=body, headers=headers, request=request)

    async with http.AsyncClient(
        transport=http.MockTransport(handler), trust_env=False
    ) as transport:
        if spec.provider == "gemini":
            client = module._protected_client(sdk, http, key=KEY, http_client=transport)
        else:
            client = module._protected_client(
                sdk,
                api_key=KEY,
                http_client=transport,
                max_retries=0,
                base_url="https://api.openai.com/v1"
                if spec.provider == "openai"
                else "https://api.anthropic.com",
            )
        try:
            for wire in wires:
                if spec.provider == "gemini":
                    await client.aio.models.generate_content(
                        **module._sdk_arguments(sdk, wire)
                    )
                elif spec.provider == "openai":
                    await client.responses.with_raw_response.create(
                        **json.loads(wire.payload)
                    )
                else:
                    await client.messages.with_raw_response.create(
                        **json.loads(wire.payload)
                    )
        finally:
            if spec.provider == "gemini":
                await module._close_client(client)
            else:
                await client.close()
    assert seen == [wire.payload for wire in wires]
