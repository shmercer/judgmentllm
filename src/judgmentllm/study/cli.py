"""Narrow CLI-first study workflow. Live commands require independent exact consent."""

from __future__ import annotations

# pyright: reportPrivateUsage=false
import argparse
import asyncio
import importlib
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from judgmentllm.config import InputSpec, RunSpecV3, load_run_spec
from judgmentllm.providers.protocol import ProviderError
from judgmentllm.qualification import _source_commit
from judgmentllm.study.budget import BudgetLedger
from judgmentllm.study.definitions import (
    MODELS,
    ROUTES,
    SCENARIOS,
    SUITES,
    configuration,
    library,
    write_library,
)
from judgmentllm.study.execution import execute, exports, report
from judgmentllm.study.plans import (
    build_proposal,
    full_plan,
    load_proposal,
    prepare,
    request_for,
    selected_configuration,
    suite_selections,
)
from judgmentllm.study.records import Authorization, Eligibility, Price
from judgmentllm.study.sources import verify_originals


def verify_library(package: Path) -> dict[str, object]:
    rows = json.loads((package / "configurations.json").read_text())["configurations"]
    expected = library(package)
    if (
        len(rows) != 50
        or len({r["configuration_id"] for r in rows}) != 50
        or sum(r["main_selection"] for r in rows) != 30
    ):
        raise ValueError("Study configuration counts differ")
    for row, (identity, scenario, spec, main) in zip(rows, expected, strict=True):
        actual = load_run_spec(package / row["file"])
        if (
            not isinstance(actual, RunSpecV3)
            or row["configuration_id"] != identity
            or row["scenario"] != scenario
            or row["main_selection"] != main
        ):
            raise ValueError("Study configuration identity differs")
        wanted = spec.model_dump(mode="json", exclude={"output_path"})
        if actual.model_dump(mode="json", exclude={"output_path"}) != wanted:
            raise ValueError("Study configuration content differs")
    return {
        "library_configurations": 50,
        "main_selections": 30,
        "optional_controls": "omitted",
    }


def eligibility_matrix(package: Path) -> dict[str, object]:
    """Documentation and offline construction never imply account entitlement."""
    rows: list[dict[str, Any]] = []
    for provider, model in MODELS:
        for tier in ("standard", "flex", "premium"):
            supported = provider != "anthropic" or tier == "standard"
            wire_tier = {
                "openai": {"standard": "default", "flex": "flex", "premium": "fast"},
                "gemini": {
                    "standard": "standard",
                    "flex": "flex",
                    "premium": "priority",
                },
                "anthropic": {"standard": "standard_only"},
            }[provider].get(tier)
            production = "unsupported" if not supported else "denied"
            if wire_tier is not None:
                _, spec = configuration(
                    package, provider, model, "grouped_scores", wire_tier
                )
                module = importlib.import_module("judgmentllm.providers." + provider)
                adapter = getattr(
                    module,
                    {
                        "openai": "OpenAIAdapter",
                        "gemini": "GeminiAdapter",
                        "anthropic": "AnthropicAdapter",
                    }[provider],
                )(spec.endpoint, model)
                try:
                    adapter.validate_new_run(spec)
                except (ValueError, ProviderError):
                    pass
                else:
                    raise ValueError("Unexpected production qualification")
            rows.append(
                {
                    "model": model,
                    "provider": provider,
                    "service": tier,
                    "wire_tier": wire_tier,
                    "endpoint": ROUTES[provider][0],
                    "mechanism": ROUTES[provider][1],
                    "sdk": ROUTES[provider][2],
                    "sdk_version": ROUTES[provider][3],
                    "api_version": ROUTES[provider][4],
                    "documentation": "documented_candidate"
                    if supported
                    else "unsupported",
                    "offline_sdk_bytes": "see_exact_source_test_evidence"
                    if supported
                    else "not_applicable",
                    "schema_projection": "local_validation_required"
                    if supported
                    else "not_applicable",
                    "account_entitlement": "unknown" if supported else "unsupported",
                    "account_binding": "unknown",
                    "pricing": "unknown",
                    "strict_production_preflight": production,
                    "live_observations": [],
                    "live_qualified": False,
                }
            )
    return {"matrix_version": 1, "rows": rows, "documentation": "documentation.json"}


def control_matrix(package: Path) -> dict[str, object]:
    """Complete experimental matrix, with documentation distinct from qualification."""
    from judgmentllm.providers.reasoning import documented_controls
    from judgmentllm.study.admission import candidate_context

    rows: list[dict[str, Any]] = []
    for suite in ("reasoning_comparison", "temperature_acceptance"):
        for selection in suite_selections(suite):
            identity, spec = selected_configuration(package, selection)
            facts = documented_controls(
                spec, candidate_context(spec, _source_commit()).scope
            )
            assert facts is not None and spec.generation is not None
            rows.append(
                {
                    "suite": suite,
                    "selection": selection.model_dump(mode="json"),
                    "configuration_id": identity,
                    "generation": spec.generation.model_dump(mode="json"),
                    "documented_default_effort": facts.documented_default_effort,
                    "documented_default_mode": facts.documented_default_mode,
                    "api_effort_field": facts.api_effort_field,
                    "sdk_effort_field": facts.sdk_effort_field,
                    "documentation_urls": facts.documentation_urls,
                    "account_control_eligibility": "unknown",
                    "strict_production_preflight": "denied",
                    "live_observations": [],
                    "live_qualified": False,
                }
            )
    return {
        "matrix_version": 1,
        "rows": rows,
        "documentation": "control-documentation.json",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, default=Path("examples/study-native"))
    sub = parser.add_subparsers(dest="operation", required=True)
    original = sub.add_parser(
        "verify-originals",
        help="Recheck both source manifests and all 312 previews offline",
    )
    original.add_argument("--source", type=Path, required=True)
    sub.add_parser("configure", help="Generate the 50 concrete configurations")
    sub.add_parser("verify", help="Verify all 50 identities and 30 main selections")
    sub.add_parser("eligibility", help="Inspect six-model Standard/Flex/Premium gates")
    sub.add_parser(
        "controls", help="Inspect all 24 reasoning and 8 temperature candidates"
    )
    for operation in ("preview", "schema", "preflight"):
        p = sub.add_parser(operation)
        p.add_argument("--model", required=True)
        p.add_argument("--scenario", choices=SCENARIOS, required=True)
        p.add_argument("--tier", required=True)
        p.add_argument("--trait")
        p.add_argument(
            "--reverse", action="store_true", help="Separate CJ presentation audit"
        )
    plan = sub.add_parser(
        "plan", help="Generate the complete offline matrix without submission"
    )
    plan.add_argument("--output", type=Path, required=True)
    proposal = sub.add_parser(
        "proposal",
        help="Generate one immutable unexecuted proposal, with unresolved gates",
    )
    proposal.add_argument("--suite", choices=SUITES, required=True)
    proposal.add_argument("--output", type=Path, required=True)
    proposal.add_argument("--account-scope", default="study-account-unbound")
    proposal.add_argument("--eligibilities", type=Path)
    proposal.add_argument("--prices", type=Path)
    proposal.add_argument("--proofs", type=Path)
    proposal.add_argument("--capture", choices=("full", "minimal"), default="full")
    for operation in ("status", "report", "export", "cancel", "execute", "recover"):
        p = sub.add_parser(operation)
        p.add_argument("--proposal", type=Path, required=True)
        p.add_argument("--proposal-sha256", required=True)
        p.add_argument("--campaign", type=Path)
        if operation in {"execute", "recover"}:
            p.add_argument("--authorization", type=Path, required=True)
            p.add_argument("--proofs", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result: Any
        if args.operation == "verify-originals":
            result = verify_originals(args.source, args.package)
        elif args.operation == "configure":
            result = write_library(args.package)
        elif args.operation == "verify":
            result = verify_library(args.package)
        elif args.operation == "eligibility":
            result = eligibility_matrix(args.package)
        elif args.operation == "controls":
            result = control_matrix(args.package)
        elif args.operation in {"preview", "schema", "preflight"}:
            provider = next(p for p, m in MODELS if m == args.model)
            _, spec = configuration(
                args.package, provider, args.model, args.scenario, args.tier
            )
            if args.reverse:
                if args.scenario != "cj":
                    raise ValueError("Reversal audits require comparative judgment")
                spec.pairs = InputSpec(
                    path=args.package.resolve() / "sources/cj/pairs_reversal_demo.jsonl"
                )
            with tempfile.TemporaryDirectory(prefix="study-preview-") as temp:
                with prepare(
                    spec, _source_commit(), Path(temp) / "store", "study-preview"
                ) as prepared:
                    wires = [
                        w
                        for w in request_for(prepared)
                        if args.trait is None
                        or args.trait
                        in prepared.dependency_for_request(
                            w.request_uid
                        ).request.trait_ids
                    ]
                    wire = wires[0]
                    output = wire.output_commitment
                    result = {
                        "canonical_schema": json.loads(output.schema_text),
                        "provider_projection": output.projection.model_dump(
                            mode="json"
                        ),
                        "payload_sha256": wire.payload_sha256,
                        "candidate_preflight": "passed_offline_only",
                        "production_preflight": "denied_unqualified",
                        "account_eligibility": "unknown",
                        "pricing": "unknown",
                    }
                    if args.operation == "preview":
                        result["prompt"] = prepared.render(
                            prepared.dependency_for_request(wire.request_uid)
                        ).text
        elif args.operation == "plan":
            result = full_plan(args.package, args.output, _source_commit())
        elif args.operation == "proposal":
            eligibilities = (
                tuple(
                    Eligibility.model_validate_json(json.dumps(r))
                    for r in json.loads(args.eligibilities.read_text())
                )
                if args.eligibilities
                else ()
            )
            prices = (
                tuple(
                    Price.model_validate_json(json.dumps(r))
                    for r in json.loads(args.prices.read_text())
                )
                if args.prices
                else ()
            )
            built = build_proposal(
                args.package,
                args.output,
                args.suite,
                account_scope=args.account_scope,
                eligibilities=eligibilities,
                prices=prices,
                proofs=args.proofs,
                capture=args.capture,
            )
            result = {
                "status": built.status,
                "proposal_sha256": built.digest,
                "calls": len(built.calls),
                "authorized_calls": 0,
                "blockers": sorted({b for c in built.calls for b in c.blockers}),
            }
        elif args.operation in {"status", "report"}:
            result = report(args.proposal, args.proposal_sha256, args.campaign)
        elif args.operation == "export":
            result = exports(args.proposal, args.proposal_sha256)
        elif args.operation == "cancel":
            loaded = load_proposal(args.proposal, args.proposal_sha256)
            if args.campaign is None:
                raise ValueError("Campaign directory is required")
            with BudgetLedger(args.campaign).locked() as ledger:
                ledger.cancel(loaded)
            result = {"cancelled": True}
        else:
            if args.campaign is None:
                raise ValueError("Campaign directory is required")
            authorization = Authorization.model_validate_json(
                args.authorization.read_bytes()
            )
            result = asyncio.run(
                execute(
                    args.proposal,
                    args.proposal_sha256,
                    args.campaign,
                    authorization,
                    args.proofs,
                    recover_only=args.operation == "recover",
                )
            )
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except Exception:
        sys.stderr.write(
            "Study operation blocked: verify exact source, configuration, proposal, account/pricing gates and retained evidence. No replacement submission is authorized.\n"
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
