"""Original content, complete matrix, exact native SDK bodies and isolated admission."""

from __future__ import annotations

# pyright: reportPrivateUsage=false
import importlib
import json
import shutil
import socket
from pathlib import Path
from typing import Any, cast

import httpx2
import pytest
from reasoning_fixture import response_body
from study_fixture import (
    KEY,
    PACKAGE,
    SOURCE,
    assessment,
    authorization,
    case_files,
    sdk_bytes,
)

from judgmentllm.config import InputSpec, load_run_spec
from judgmentllm.execution import execute_run
from judgmentllm.execution.retry import RetryPolicy
from judgmentllm.persistence.hashing import bytes_digest
from judgmentllm.providers.protocol import ProviderError
from judgmentllm.study import plans
from judgmentllm.study.admission import (
    candidate_context,
    guarded_authority,
    guarded_capabilities,
    study_generation,
)
from judgmentllm.study.budget import BudgetLedger
from judgmentllm.study.cli import eligibility_matrix, main, verify_library
from judgmentllm.study.definitions import (
    HOLISTIC,
    MODELS,
    SCENARIOS,
    TRAIT9,
    configuration,
    library,
    write_library,
)
from judgmentllm.study.execution import (
    _STUDY_TOKEN,
    _StudyGuard,
    adapter_for,
    case_report,
)
from judgmentllm.study.plans import (
    build_proposal,
    prepare,
    request_for,
    suite_selections,
)
from judgmentllm.study.records import Proposal
from judgmentllm.study.sources import archive


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "judgmentllm.study.budget._global_lock_path",
        lambda: tmp_path / "study-global.lock",
    )
    for key in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
    ):
        monkeypatch.setenv(key, KEY)

    def deny(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("External network and real credentials are forbidden")

    monkeypatch.setattr(socket, "getaddrinfo", deny)
    monkeypatch.setattr(socket, "create_connection", deny)
    monkeypatch.setattr(plans, "_source_commit", lambda: SOURCE)
    monkeypatch.setattr("judgmentllm.study.execution._source_commit", lambda: SOURCE)
    monkeypatch.setattr("judgmentllm.study.cli._source_commit", lambda: SOURCE)


def test_original_content_and_all_312_previews() -> None:
    snapshots = {
        (r["kind"], r["name"]): r
        for r in archive(PACKAGE / "original-previews.jsonl.gz.base64")
    }
    manifests = json.loads((PACKAGE / "original-manifests.json").read_text())
    from judgmentllm.preview import preview_prompt

    count = 0
    for kind in ("rubric", "cj"):
        root = PACKAGE / "sources" / kind
        for path in root.rglob("*"):
            if path.is_file():
                assert (
                    bytes_digest(path.read_bytes())
                    == manifests[kind]["files"][path.relative_to(root).as_posix()]
                )
        for cfg in sorted((root / "configs").glob("*.yaml")):
            spec = load_run_spec(cfg)
            if kind == "rubric":
                assert spec.rubric is not None
                traits = (
                    spec.rubric.traits
                    if spec.execution.grouping == "independent"
                    else spec.rubric.traits[:1]
                )
                rendered = [
                    (
                        cfg.stem
                        + (
                            "__" + t.trait_id
                            if spec.execution.grouping == "independent"
                            else ""
                        ),
                        preview_prompt(
                            spec, item_id="essay_demo_001", trait_id=t.trait_id
                        ),
                    )
                    for t in traits
                ]
            else:
                rendered: list[tuple[str, Any]] = []
                for orientation in ("original", "reversed"):
                    current = (
                        spec
                        if orientation == "original"
                        else spec.model_copy(
                            update={
                                "pairs": InputSpec(
                                    path=root / "pairs_reversal_demo.jsonl"
                                )
                            }
                        )
                    )
                    rendered.extend(
                        (
                            cfg.stem + "__" + c.trait_id + "__" + orientation,
                            preview_prompt(
                                current, pair_uid=f"demo_{c.trait_id}_{orientation}"
                            ),
                        )
                        for c in spec.criteria
                    )
            for name, actual in rendered:
                expected = snapshots[kind, name]
                assert actual.text == expected["text"]
                assert actual.output_schema == json.loads(expected["schema_text"])
                count += 1
    assert count == len(snapshots) == 312


def test_library_identity_preservation_and_counts(tmp_path: Path) -> None:
    entries = library(PACKAGE)
    assert len(entries) == len({e[0] for e in entries}) == 50
    assert sum(e[3] for e in entries) == 30
    assert sum(spec.execution.tier == "flex" for _, _, spec, _ in entries) == 20
    assert verify_library(PACKAGE)["library_configurations"] == 50
    copy = tmp_path / "relocated"
    shutil.copytree(PACKAGE, copy)
    assert [e[0] for e in library(copy)] == [e[0] for e in entries]
    assert write_library(copy)["main_count"] == 30
    for _, scenario, spec, main_selection in entries:
        assert spec.generation is not None
        assert spec.generation.model_dump() == {
            "max_output_tokens": SCENARIOS[scenario][1]
        }
        assert spec.schema_version == 3 and spec.strict_capabilities
        assert (
            spec.prompt.structured_output == "native"
            and spec.prompt.output_order == "together"
        )
        assert spec.execution.concurrency == 1
        assert not main_selection or spec.execution.tier == (
            "standard_only" if spec.provider == "anthropic" else "flex"
        )
    assert (
        configuration(PACKAGE, "openai", "gpt-6-luna", "cj", "flex", "another-account")[
            0
        ]
        != configuration(PACKAGE, "openai", "gpt-6-luna", "cj", "flex")[0]
    )
    with pytest.raises((ValueError, ProviderError)):
        configuration(PACKAGE, "openai", "gpt-6-luna", "cj", "priority")
    (copy / "sources/rubric/prompts/all_12_score_only.txt").write_text(
        "rewritten rubric"
    )
    with pytest.raises(ValueError, match="original"):
        library(copy)


SDK_SELECTIONS = [
    (spec.provider, spec.model, scenario, spec.execution.tier)
    for _, scenario, spec, _ in library(PACKAGE)
] + [
    (p, m, scenario, "fast" if p == "openai" else "priority")
    for p, m in MODELS
    if p != "anthropic"
    for scenario in ("grouped_scores", "cj")
]


@pytest.mark.parametrize("provider,model,scenario,tier", SDK_SELECTIONS)
@pytest.mark.asyncio
async def test_complete_native_plans(
    tmp_path: Path, provider: str, model: str, scenario: str, tier: str
) -> None:
    config_id, spec = configuration(PACKAGE, provider, model, scenario, tier)
    goldens = {
        (r["configuration_id"], r["request_uid"]): r
        for r in archive(PACKAGE / "native-previews.jsonl.gz.base64")
    }
    with prepare(spec, SOURCE, tmp_path / "store", "001") as prepared:
        wires = list(request_for(prepared))
        assert (
            len(wires)
            == {
                "grouped_scores": 1,
                "grouped_explanations": 1,
                "independent_scores": 12,
                "independent_explanations": 12,
                "cj": 13,
            }[scenario]
        )
        for wire in wires:
            assert wire.output_commitment.selection.kind == "qualification_probe"
            assert wire.output_commitment.reasoning_policy is None
            assert wire.output_commitment.projection is not None
            assert wire.generation.model_dump() == {
                "max_output_tokens": SCENARIOS[scenario][1]
            }
            assert (
                b'"temperature"' not in wire.payload and b'"top_p"' not in wire.payload
            )
            assert (
                b'"thinking"' not in wire.payload and b'"reasoning"' not in wire.payload
            )
            assert wire.payload_sha256 == bytes_digest(wire.payload)
            if (config_id, wire.request_uid) in goldens:
                golden = goldens[(config_id, wire.request_uid)]
                assert wire.payload.decode() == golden["files"]["00.payload.json"]
                assert wire.prompt_sha256 == golden["prompt_sha256"]
                assert (
                    wire.output_commitment.schema_text
                    == golden["files"]["00.schema.json"]
                )
        await sdk_bytes(spec, wires)
        if scenario == "cj":
            assert set(c.trait_id for c in spec.criteria) == {
                d.request.trait_ids[0] for d in prepared.iter_request_dependencies()
            }
            for wire in wires:
                assert json.loads(wire.output_commitment.schema_text)["properties"][
                    "winner_id"
                ]["enum"] == ["essay_01", "essay_02"]
                prompt = prepared.render(
                    prepared.dependency_for_request(wire.request_uid)
                ).text
                assert "student_opaque_" not in prompt


@pytest.fixture(scope="module")
def pilot(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Proposal]:
    root = tmp_path_factory.mktemp("study-pilot")
    original = plans._source_commit
    plans._source_commit = lambda: SOURCE
    try:
        proposal = build_proposal(PACKAGE, root / "proposal", "native_pilot")
    finally:
        plans._source_commit = original
    return root / "proposal", proposal


@pytest.fixture(scope="module")
def tier(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Proposal]:
    root = tmp_path_factory.mktemp("study-tier")
    original = plans._source_commit
    plans._source_commit = lambda: SOURCE
    try:
        proposal = build_proposal(
            PACKAGE, root / "proposal", "tier_feasibility", capture="minimal"
        )
    finally:
        plans._source_commit = original
    return root / "proposal", proposal


def test_original_proposal_order_blockers_and_immutable_limits(
    pilot: tuple[Path, Proposal],
) -> None:
    directory, proposal = pilot
    assert (
        len(proposal.calls) == 42
        and proposal.ceiling_usd == 5
        and proposal.elapsed_seconds == 28800
    )
    assert proposal.status == "blocked" and proposal.authorized_calls == 0
    assert len(suite_selections("tier_feasibility")) == 24
    for i in range(0, 42, 7):
        cases = proposal.calls[i : i + 7]
        assert (
            cases[2].selection.trait_id
            == cases[3].selection.trait_id
            == cases[4].selection.trait_id
            == TRAIT9
        )
        assert cases[5].selection.trait_id == cases[6].selection.trait_id == HOLISTIC
        assert not cases[5].selection.reversed and cases[6].selection.reversed
    assert all(
        c.reserved_input_tokens == 32768
        and c.reserved_output_tokens == 8192
        and c.price is None
        and c.eligibility is None
        and c.blockers
        for c in proposal.calls
    )
    assert plans.load_proposal(directory, proposal.digest) == proposal
    for change in (
        {"max_calls": 43},
        {"ceiling_usd": "6"},
        {"elapsed_seconds": 30000},
        {"status": "sealed"},
    ):
        data = proposal.model_dump(mode="json") | change
        with pytest.raises((ValueError, ProviderError)):
            Proposal.model_validate_json(json.dumps(data))
    with pytest.raises((ValueError, ProviderError)):
        authorization(proposal).verify(proposal)


@pytest.mark.parametrize("index", [0, 1, 2, 3, 4, 5, 6, 14, 15, 18, 20, 28, 29, 32, 34])
@pytest.mark.asyncio
async def test_pinned_sdk_exact_bytes_and_normalization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pilot: tuple[Path, Proposal],
    index: int,
) -> None:
    directory, proofs, proposal, case, spec = case_files(tmp_path, pilot, index)
    body, headers = response_body(
        spec,
        assessment(
            spec,
            case.task_ids and tuple(t.trait_id for t in spec.rubric.traits)
            if spec.rubric and spec.execution.grouping == "grouped"
            else (TRAIT9,),
        ),
    )
    calls: list[bytes] = []

    def handler(request: Any) -> Any:
        assert request.content == case.request_body_utf8.encode()
        calls.append(bytes(request.content))
        return httpx2.Response(200, json=body, headers=headers, request=request)

    with BudgetLedger(tmp_path / "campaign").locked() as ledger:
        ledger.authorize(proposal, authorization(proposal))
        guard = _StudyGuard(
            spec,
            SOURCE,
            token=_STUDY_TOKEN,
            proposal=proposal,
            case=case,
            directory=directory,
            ledger=ledger,
            proofs=proofs,
        )
        adapter = adapter_for(spec, guard, http_transport=httpx2.MockTransport(handler))
        try:
            adapter.validate_new_run(spec)
            result = await execute_run(
                case.run_id,
                store=directory / "preparation" / case.case_id,
                spec=spec,
                adapter=adapter,
                retry_policy=RetryPolicy(max_attempts=1),
            )
            assert not result.recovery_required
            assert len(calls) == 1
            row = case_report(directory, case)
            assert row["continuation_allowed"] and row["canonical_valid"]
            assert row["reported_model"] == case.selection.model
            assert row["result_count"] == len(case.task_ids)
            if index == 0:
                from judgmentllm.study import execution

                with monkeypatch.context() as corrupted:

                    def missing_receipt(*args: Any) -> None:
                        return None

                    corrupted.setattr(execution, "read_receipt", missing_receipt)
                    with pytest.raises(ValueError, match="Response commitment"):
                        case_report(directory, case)
            if spec.task_kind == "pairwise":
                order = row["original_and_presented_orders"][0]
                assert order["winner_id"] == order["presented_order"][0]
                assert (
                    order["presented_order"] != order["original_order"]
                ) == case.selection.reversed
            await execute_run(
                case.run_id,
                store=directory / "preparation" / case.case_id,
                spec=spec,
                adapter=adapter,
                retry_policy=RetryPolicy(max_attempts=1),
            )
            assert len(calls) == 1
        finally:
            await adapter.aclose()


def test_guard_admission_does_not_change_production(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_credentials(*args: Any) -> Any:
        raise AssertionError("Preflight accessed credentials")

    monkeypatch.setattr("judgmentllm._auth._resolve_credentials", no_credentials)
    for provider, model in MODELS:
        _, spec = configuration(
            PACKAGE,
            provider,
            model,
            "grouped_scores",
            "standard_only" if provider == "anthropic" else "flex",
        )
        context = candidate_context(spec, SOURCE)
        with pytest.raises((ValueError, ProviderError)):
            guarded_authority(object(), context)
        with pytest.raises((ValueError, ProviderError)):
            guarded_capabilities(object(), context, spec)
        with pytest.raises((ValueError, ProviderError)):
            _StudyGuard(spec, SOURCE, token=object())
        guard = _StudyGuard(spec, SOURCE, token=_STUDY_TOKEN)
        caps = guarded_capabilities(guard, context, spec)
        assert caps.generation is not None and not caps.generation.live_qualified
        with pytest.raises((ValueError, ProviderError)):
            guarded_authority(guard, context)
        candidate = adapter_for(spec, guard)
        with pytest.raises((ValueError, ProviderError)):
            candidate.validate_credentials()
        assert spec.generation is not None
        for controls in ({"temperature": 0.0}, {"top_p": 1.0}):
            with pytest.raises((ValueError, ProviderError)):
                candidate_context(
                    spec.model_copy(
                        update={
                            "generation": spec.generation.model_copy(update=controls)
                        }
                    ),
                    SOURCE,
                )
        for update in (
            {"model": "unknown-model"},
            {"account_scope": "other"},
            {"strict_capabilities": False},
        ):
            with pytest.raises((ValueError, ProviderError)):
                guarded_capabilities(guard, context, spec.model_copy(update=update))
        module = importlib.import_module("judgmentllm.providers." + provider)
        ordinary = getattr(
            module,
            {
                "openai": "OpenAIAdapter",
                "anthropic": "AnthropicAdapter",
                "gemini": "GeminiAdapter",
            }[provider],
        )(spec.endpoint, spec.model)
        with pytest.raises((ValueError, ProviderError)):
            ordinary.validate_new_run(spec)
        with pytest.raises((ValueError, ProviderError)):
            study_generation(
                spec.model_copy(
                    update={
                        "prompt": spec.prompt.model_copy(
                            update={"structured_output": "text"}
                        )
                    }
                ),
                context.scope,
            )


def test_cli_and_eligibility_are_offline(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--package", str(PACKAGE), "verify"]) == 0
    assert json.loads(capsys.readouterr().out)["main_selections"] == 30
    matrix = cast(dict[str, Any], eligibility_matrix(PACKAGE))
    assert len(matrix["rows"]) == 18
    assert all(
        not r["live_observations"] and r["account_binding"] == "unknown"
        for r in matrix["rows"]
    )
    assert (
        main(
            [
                "--package",
                str(PACKAGE),
                "preview",
                "--model",
                "unknown",
                "--scenario",
                "cj",
                "--tier",
                "flex",
            ]
        )
        == 2
    )
