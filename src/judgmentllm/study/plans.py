"""Complete engine-generated offline plans and independently sealable proposals."""

from __future__ import annotations

# pyright: reportPrivateUsage=false
import importlib.metadata
import json
from pathlib import Path
from typing import Any, Literal, cast

from judgmentllm.config import InputSpec, RunSpecV3, validate_run_spec_json
from judgmentllm.persistence.hashing import bytes_digest, canonical_bytes
from judgmentllm.planning import stable_id
from judgmentllm.preparation import open_prepared_run, prepare_run
from judgmentllm.qualification import _immutable, _source_commit
from judgmentllm.study.admission import candidate_context, study_generation
from judgmentllm.study.definitions import (
    HOLISTIC,
    MODELS,
    ROUTES,
    STUDY_VERSION,
    SUITES,
    TRAIT9,
    configuration,
    library,
)
from judgmentllm.study.records import (
    Case,
    Eligibility,
    Price,
    Proposal,
    Selection,
    SuiteName,
)
from judgmentllm.transport import physical_identity


def suite_selections(suite: SuiteName) -> tuple[Selection, ...]:
    selections: list[Selection] = []
    for provider, model in MODELS:
        if suite == "native_pilot":
            tier = "standard_only" if provider == "anthropic" else "flex"
            cases = [
                ("grouped_scores", None, False),
                ("grouped_explanations", None, False),
                ("independent_scores", TRAIT9, False),
                ("independent_explanations", TRAIT9, False),
                ("cj", TRAIT9, False),
                ("cj", HOLISTIC, False),
                ("cj", HOLISTIC, True),
            ]
            tiers = (tier,)
        elif suite == "tier_feasibility":
            if provider == "anthropic":
                continue
            cases = [("grouped_scores", None, False), ("cj", HOLISTIC, False)]
            tiers = (
                ("default", "flex", "fast")
                if provider == "openai"
                else ("standard", "flex", "priority")
            )
        else:
            raise ValueError("Unknown study suite")
        for tier in tiers:
            for scenario, trait, reverse in cases:
                selections.append(
                    Selection(
                        provider=cast(Any, provider),
                        model=model,
                        scenario=cast(Any, scenario),
                        tier=tier,
                        trait_id=trait,
                        reversed=reverse,
                    )
                )
    return tuple(selections)


def prepare(spec: RunSpecV3, source: str, store: Path, run_id: str) -> Any:
    _, _, sdk, version, _ = ROUTES[spec.provider]
    if importlib.metadata.version(sdk) != version:
        raise ValueError("Pinned candidate SDK differs")
    context = candidate_context(spec, source)
    from judgmentllm.planning import CapabilityProfile

    profile = CapabilityProfile(
        modes=("live",),
        grouped=True,
        explanations=True,
        ties=False,
        generation=study_generation(spec, context.scope),
    )
    if (store / "registry.sqlite3").exists():
        return open_prepared_run(
            run_id,
            spec=spec,
            store=store,
            transport_context=context,
            capabilities=profile,
        )
    return prepare_run(
        spec,
        store=store,
        run_id=run_id,
        transport_context=context,
        capabilities=profile,
    )


def request_for(prepared: Any, selection: Selection | None = None) -> Any:
    for dep in prepared.iter_request_dependencies():
        request = dep.request
        if selection is not None and (
            (
                selection.trait_id is not None
                and request.trait_ids != (selection.trait_id,)
            )
            or (
                request.response is not None
                and request.response.item_id != "essay_demo_001"
            )
        ):
            continue
        identity = physical_identity(
            prepared.manifest.run_id,
            request.request_uid,
            stable_id("attempt", prepared.manifest.run_id, request.request_uid, 1),
        )
        yield prepared.prepare_transport_attempt(dep, identity)


def build_proposal(
    package: Path,
    directory: Path,
    suite: SuiteName,
    *,
    account_scope: str = "study-account-unbound",
    eligibilities: tuple[Eligibility, ...] = (),
    prices: tuple[Price, ...] = (),
    proofs: Path | None = None,
    capture: Literal["full", "minimal"] = "full",
) -> Proposal:
    """No credential access. Unknown account/pricing produces a blocked proposal."""
    source = _source_commit()
    if (eligibilities or prices) and proofs is None:
        raise ValueError("Independent account/price proofs are required before sealing")
    directory = directory.resolve()
    root = Path(__file__).resolve().parents[3]
    if directory.is_relative_to(root):
        raise ValueError("Proposals must be outside the tracked checkout")
    directory.mkdir(parents=True, exist_ok=False)
    calls: list[Case] = []
    for index, selection in enumerate(suite_selections(suite)):
        config_id, spec = configuration(
            package,
            selection.provider,
            selection.model,
            selection.scenario,
            selection.tier,
            account_scope,
            capture,
        )
        case_id = f"case-{index + 1:02d}"
        if selection.reversed:
            spec.pairs = InputSpec(
                path=package.resolve() / "sources/cj/pairs_reversal_demo.jsonl"
            )
        run_id = stable_id(
            "study-run", source, suite, config_id, selection.model_dump(mode="json")
        )
        with prepare(
            spec, source, directory / "preparation" / case_id, run_id
        ) as prepared:
            wires = list(request_for(prepared, selection))
            if len(wires) != 1:
                raise ValueError(
                    "Pilot selection must identify exactly one physical request"
                )
            wire = wires[0]
            transport = wire.transport_commitment
            output = wire.output_commitment
            identity = transport.identity
            policy = output.service_policy
            assert policy is not None and spec.generation is not None
            price = next(
                (
                    p
                    for p in prices
                    if p.selection == selection and p.account_scope == account_scope
                ),
                None,
            )
            eligibility = next(
                (
                    e
                    for e in eligibilities
                    if e.selection == selection and e.account_scope == account_scope
                ),
                None,
            )
            raw_spec = canonical_bytes(spec)
            calls.append(
                Case(
                    case_id=case_id,
                    configuration_id=config_id,
                    selection=selection,
                    run_id=run_id,
                    request_uid=wire.request_uid,
                    task_ids=wire.task_ids,
                    attempt_id=identity.attempt_id,
                    submission_id=identity.submission_id,
                    correlation_id=identity.correlation_id,
                    specification_json=raw_spec.decode(),
                    specification_sha256=bytes_digest(raw_spec),
                    request_body_utf8=wire.payload.decode(),
                    payload_sha256=wire.payload_sha256,
                    prompt_sha256=wire.prompt_sha256,
                    schema_sha256=bytes_digest(output.schema_text.encode()),
                    projection_sha256=bytes_digest(canonical_bytes(output.projection)),
                    transport_sha256=transport.commitment_sha256,
                    dependency_sha256=wire.dependency_sha256,
                    features=output.selection.features,
                    sdk_version=ROUTES[spec.provider][3],
                    api_version=ROUTES[spec.provider][4],
                    mechanism=transport.mechanism,
                    account_scope=account_scope,
                    max_output_tokens=cast(int, spec.generation.max_output_tokens),
                    operation_deadline_seconds=policy.operation_deadline_seconds,
                    eligibility=eligibility,
                    price=price,
                    reserved_usd=None
                    if price is None
                    else price.reservation(32768, 8192),
                    blockers=(
                        ()
                        if eligibility
                        else ("account_native_tier_and_token_bounds_unknown",)
                    )
                    + (() if price else ("account_prices_unknown",)),
                )
            )
    count, dollars, seconds = SUITES[suite]
    from decimal import Decimal

    if all(not c.blockers for c in calls):
        from judgmentllm.study.execution import _verify_proofs

        assert proofs is not None
        for case in calls:
            _verify_proofs(case, proofs, credentials=False)
            if case.reserved_usd is None or case.reserved_usd > Decimal(dollars):
                raise ValueError(
                    "Suite ceiling cannot cover a complete per-call reservation"
                )

    proposal = Proposal(
        suite=suite,
        source_commit=source,
        study_version=STUDY_VERSION,
        max_calls=count,
        ceiling_usd=Decimal(dollars),
        elapsed_seconds=seconds,
        calls=tuple(calls),
        status="blocked" if any(c.blockers for c in calls) else "sealed",
        capture=capture,
    )
    _immutable(directory / "proposal.json", canonical_bytes(proposal))
    _immutable(directory / "proposal.sha256", (proposal.digest + "\n").encode())
    return proposal


def load_proposal(directory: Path, digest: str) -> Proposal:
    path = directory / "proposal.json"
    if path.is_symlink():
        raise ValueError("Proposal cannot be a symlink")
    proposal = Proposal.model_validate_json(path.read_bytes())
    if (
        proposal.digest != digest
        or path.read_bytes() != canonical_bytes(proposal)
        or (directory / "proposal.sha256").read_text().strip() != digest
        or proposal.source_commit != _source_commit()
    ):
        raise ValueError("Proposal/source commitment differs")
    if tuple(c.selection for c in proposal.calls) != suite_selections(proposal.suite):
        raise ValueError("Proposal scope/order differs")
    for index, case in enumerate(proposal.calls):
        expected_run = stable_id(
            "study-run",
            proposal.source_commit,
            proposal.suite,
            case.configuration_id,
            case.selection.model_dump(mode="json"),
        )
        if case.case_id != f"case-{index + 1:02d}" or case.run_id != expected_run:
            raise ValueError("Case or physical run identity differs")
        _, _, sdk, version, _ = ROUTES[case.selection.provider]
        if importlib.metadata.version(sdk) != version:
            raise ValueError("Pinned SDK differs")
        spec = validate_run_spec_json(case.specification_json)
        assert isinstance(spec, RunSpecV3)
        assert spec.prompt.file is not None
        identity, expected = configuration(
            spec.prompt.file.parents[3],
            case.selection.provider,
            case.selection.model,
            case.selection.scenario,
            case.selection.tier,
            case.account_scope,
            proposal.capture,
        )
        if case.selection.reversed:
            expected.pairs = InputSpec(
                path=spec.prompt.file.parents[3]
                / "sources/cj/pairs_reversal_demo.jsonl"
            )
        if identity != case.configuration_id or canonical_bytes(
            spec
        ) != canonical_bytes(expected):
            raise ValueError("Sealed study source/configuration differs")
        with prepare(
            spec,
            proposal.source_commit,
            directory / "preparation" / case.case_id,
            case.run_id,
        ) as prepared:
            matching = list(request_for(prepared, case.selection))
            if len(matching) != 1:
                raise ValueError("Sealed selection differs")
            verify_wire(case, matching[0])
    return proposal


def verify_wire(case: Case, wire: Any) -> None:
    output = wire.output_commitment
    identity = wire.transport_commitment.identity
    scope = output.scope
    if (
        scope.provider,
        scope.model,
        scope.endpoint,
        scope.sdk_package,
        scope.sdk_version,
        scope.api_version,
        ROUTES[scope.provider][4],
        output.service_policy.account_scope,
        wire.generation.max_output_tokens,
    ) != (
        case.selection.provider,
        case.selection.model,
        ROUTES[case.selection.provider][0],
        ROUTES[case.selection.provider][2],
        case.sdk_version,
        None if case.selection.provider == "openai" else case.api_version,
        case.api_version,
        case.account_scope,
        case.max_output_tokens,
    ):
        raise ValueError("Case model/endpoint/SDK/account/output-cap scope differs")
    if (
        identity.run_id,
        identity.attempt_id,
        identity.submission_id,
        identity.correlation_id,
        bytes_digest(output.schema_text.encode()),
        bytes_digest(canonical_bytes(output.projection)),
        output.selection.features,
        wire.transport_commitment.mechanism,
    ) != (
        case.run_id,
        case.attempt_id,
        case.submission_id,
        case.correlation_id,
        case.schema_sha256,
        case.projection_sha256,
        case.features,
        case.mechanism,
    ):
        raise ValueError("Case identity/schema/feature commitment differs")
    if (
        wire.payload,
        wire.payload_sha256,
        wire.prompt_sha256,
        wire.request_uid,
        wire.task_ids,
        wire.dependency_sha256,
        wire.transport_commitment.commitment_sha256,
    ) != (
        case.request_body_utf8.encode(),
        case.payload_sha256,
        case.prompt_sha256,
        case.request_uid,
        case.task_ids,
        case.dependency_sha256,
        case.transport_sha256,
    ):
        raise ValueError("Prepared request differs from the sealed case")


def full_plan(package: Path, output: Path, source: str) -> dict[str, object]:
    """All 50 configurations: original synthetic inputs; reversal is separate."""
    output.mkdir(parents=True, exist_ok=False)
    rows: list[dict[str, Any]] = []
    entries = library(package)
    for config_id, scenario, spec, main in entries:
        with prepare(
            spec,
            source,
            output / "preparation" / config_id,
            stable_id("study-preview", source, config_id),
        ) as prepared:
            for wire in request_for(prepared):
                commitment = wire.output_commitment
                rows.append(
                    {
                        "configuration_id": config_id,
                        "scenario": scenario,
                        "main_selection": main,
                        "model": spec.model,
                        "tier": spec.execution.tier,
                        "request_uid": wire.request_uid,
                        "task_ids": list(wire.task_ids),
                        "payload_sha256": wire.payload_sha256,
                        "prompt_sha256": wire.prompt_sha256,
                        "schema_sha256": bytes_digest(commitment.schema_text.encode()),
                        "projection_sha256": bytes_digest(
                            canonical_bytes(commitment.projection)
                        ),
                        "files": {
                            "00.payload.json": wire.payload.decode(),
                            "00.prompt.txt": prepared.render(
                                prepared.dependency_for_request(wire.request_uid)
                            ).text,
                            "00.schema.json": commitment.schema_text,
                            "00.projection.json": canonical_bytes(
                                commitment.projection
                            ).decode(),
                        },
                    }
                )
    _immutable(
        output / "requests.jsonl",
        b"".join(canonical_bytes(row) + b"\n" for row in rows),
    )
    result: dict[str, object] = {
        "configurations": len(entries),
        "main_selections": sum(entry[3] for entry in entries),
        "requests": len(rows),
        "rubric_requests_per_essay_per_model": 26,
        "cj_requests_per_pair_per_model": 13,
        "reversal_audit_included": False,
        "source_commit": source,
    }
    (output / "summary.json").write_text(json.dumps(result, indent=2) + "\n")
    return result
