"""New OFF requests and genuine pre-change replay, entirely synthetic/offline."""

from __future__ import annotations

# pyright: reportPrivateUsage=false
import base64
import hashlib
import io
import json
import os
import socket
import subprocess
import sys
import tarfile
from copy import deepcopy
from dataclasses import replace
from importlib.metadata import PackageNotFoundError
from pathlib import Path
from typing import Any, cast

import httpx2
import pytest
from pydantic import TypeAdapter
from test_openai_lifecycle import envelope
from test_production_transport import _reviewed_profile, case

from judgmentllm import production_registry
from judgmentllm._lifecycle_store import load_resume_spec
from judgmentllm.cache_policy import CachePolicyVersion, validate_openai_off
from judgmentllm.config import (
    GenerationSpec,
    RunSpecV3,
    validate_run_spec,
    validate_run_spec_json,
)
from judgmentllm.evidence.usage import ReportedUsageInput, UsageBreakdown
from judgmentllm.execution import execute_run
from judgmentllm.execution.retry import authorize_subset_retry
from judgmentllm.models import ResponseItem
from judgmentllm.persistence.hashing import canonical_bytes
from judgmentllm.persistence.repository import ReadOnlyRepository
from judgmentllm.planning import CapabilityProfile, plan_record, validate_run
from judgmentllm.preparation import PreparationError, open_prepared_run, prepare_run
from judgmentllm.preparation.session import RequestDependency
from judgmentllm.production_transport import (
    TransportCommitmentV2,
    build_output_commitment_v4,
    prepare_production_request,
    production_context,
    production_context_for_identity,
)
from judgmentllm.providers.openai import OpenAIAdapter
from judgmentllm.providers.openai_response import _usage
from judgmentllm.reasoning import ReasoningQualificationScopeV1, ReasoningSpec
from judgmentllm.service_tiers import ServiceQualificationScope
from judgmentllm.transport import TransportIntegrityError, physical_identity


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "SYNTHETIC_CACHE_ONLY")

    def denied(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("external networking is forbidden")

    monkeypatch.setattr(socket, "getaddrinfo", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


def new_case(tmp_path: Path, **options: Any) -> tuple[RunSpecV3, RequestDependency]:
    spec, dependency, _ = case(tmp_path, **options)
    spec.model = "gpt-6-luna"
    return cast(RunSpecV3, validate_run_spec(spec, _cache_policy_version=2)), dependency


@pytest.mark.parametrize("autocrlf", ["false", "true"])
def test_historical_fixture_checkout_preserves_bytes(autocrlf: str) -> None:
    root = Path(__file__).resolve().parents[1]
    fixture = root / "tests/fixtures/cache-history"
    provenance = json.loads((fixture / "provenance.json").read_bytes())
    for filename, field in (
        ("producer.py.txt", "producer_script_sha256"),
        ("store.tar.gz.base64", "archive_sha256"),
    ):
        path = (fixture / filename).relative_to(root).as_posix()
        raw = subprocess.run(
            [
                "git",
                "-c",
                f"core.autocrlf={autocrlf}",
                "cat-file",
                "--filters",
                f"HEAD:{path}",
            ],
            cwd=root,
            capture_output=True,
            check=True,
            timeout=30,
        ).stdout
        content = (
            base64.b64decode(raw, validate=True)
            if filename.endswith(".base64")
            else raw
        )
        assert hashlib.sha256(content).hexdigest() == provenance[field]


@pytest.mark.parametrize("version", [1, 2, 3])
@pytest.mark.parametrize("pairwise", [False, True])
def test_new_defaults_and_legacy_only_auto(
    tmp_path: Path, version: int, pairwise: bool
) -> None:
    spec, _ = new_case(tmp_path, pairwise=pairwise)
    data = spec.model_dump(mode="json")
    data["schema_version"] = version
    if version < 3:
        data.pop("account_scope")
        data["prompt"].pop("native_mechanism")
    if version == 1:
        data["prompt"].pop("structured_output")
        data["prompt"].pop("output_order")
    data["execution"].pop("cache_policy")
    checked = validate_run_spec_json(json.dumps(data))
    assert checked.execution.cache_policy == "off"
    assert checked.cache_policy_version == 2
    assert (
        "auto"
        not in checked.model_json_schema()["$defs"]["ExecutionSpec"]["properties"][
            "cache_policy"
        ]["anyOf"][0]["enum"]
    )
    historical = validate_run_spec_json(json.dumps(data), _cache_policy_version=1)
    assert historical.execution.cache_policy == ("off" if pairwise else "auto")
    data["execution"]["cache_policy"] = "auto"
    with pytest.raises(ValueError, match="legacy-only.*off.*explicit"):
        validate_run_spec_json(json.dumps(data))
    data["execution"]["cache_policy"] = "off"
    data["cache_policy_version"] = 1
    with pytest.raises(ValueError):
        validate_run_spec_json(json.dumps(data))


def test_explicit_remains_unsupported_and_gemini_warning_is_nonblocking(
    tmp_path: Path,
) -> None:
    spec, _ = new_case(tmp_path)
    spec.prompt.native_mechanism = None
    for provider in ("openai", "anthropic", "gemini", "vertex", "mock"):
        spec.provider = provider
        spec.execution.cache_policy = "explicit"
        report = validate_run(spec)
        assert any(
            x.code == "unsupported_cache_policy" and x.severity == "error"
            for x in report.issues
        )
    spec.provider = "gemini"
    spec.execution.cache_policy = "off"
    spec.strict_capabilities = True
    report = validate_run(
        spec, capabilities=CapabilityProfile(modes=("live",), grouped=True)
    )
    assert report.valid
    warning = next(
        x for x in report.issues if x.code == "implicit_cache_uncontrollable"
    )
    assert warning.severity == "warning"
    assert "cannot be disabled" in warning.message


@pytest.mark.parametrize("version", [True, "2", 2.0, 0, 3])
def test_cache_revision_tokens_are_strict(version: Any) -> None:
    with pytest.raises(ValueError):
        TypeAdapter(CachePolicyVersion).validate_python(version)


def test_scope_rejects_untyped_values() -> None:
    with pytest.raises(ValueError):
        validate_openai_off(None)


@pytest.mark.parametrize("version", [True, "2", 2.0, 0, 3])
def test_configuration_rejects_unknown_cache_revision(
    tmp_path: Path, version: Any
) -> None:
    spec, _ = new_case(tmp_path)
    with pytest.raises(ValueError, match="cache policy version"):
        validate_run_spec(spec, _cache_policy_version=version)


def test_usage_versions_do_not_borrow_other_sources_or_legacy_semantics() -> None:
    with pytest.raises(ValueError):
        UsageBreakdown(
            semantics_version=2,
            usage_source="synthetic.reported_usage",
            field="input_tokens",
            value=1,
            relationship="reported",
        )
    with pytest.raises(ValueError):
        UsageBreakdown(
            semantics_version=1,
            usage_source="openai.responses.usage",
            field="input_tokens_details.cache_write_tokens",
            value=0,
            relationship="included_in",
            related_counter="input_tokens",
        )
    with pytest.raises(ValueError):
        ReportedUsageInput(
            semantics_version=2,
            usage_source="openai.responses.usage",
            input_tokens=1,
            cache_write_tokens=2,
        )


def test_direct_compilation_cannot_enable_explicit_caching(tmp_path: Path) -> None:
    spec, dependency = new_case(tmp_path)
    spec.execution.cache_policy = "explicit"
    with pytest.raises(TransportIntegrityError):
        build_output_commitment_v4(spec, dependency.request, production_context(spec))


def test_anthropic_new_explicit_does_not_reuse_the_legacy_breakpoint(
    tmp_path: Path,
) -> None:
    from anthropic_fixture import case as anthropic_case

    from judgmentllm.anthropic_transport import build_output_commitment_v5
    from judgmentllm.anthropic_transport import production_context as anthropic_context

    spec, dependency, _ = anthropic_case(tmp_path)
    spec.model = "claude-sonnet-4-6"
    spec.execution.cache_policy = "explicit"
    with pytest.raises(TransportIntegrityError):
        build_output_commitment_v5(spec, dependency.request, anthropic_context(spec))
    legacy = cast(RunSpecV3, validate_run_spec(spec, _cache_policy_version=1))
    legacy.model = "claude-haiku-4-5"
    with pytest.raises(TransportIntegrityError):
        build_output_commitment_v5(
            legacy, dependency.request, anthropic_context(legacy)
        )


@pytest.mark.parametrize(
    "model", ["gpt-4.1-mini-2025-04-14", "gpt-6-luna-other", "unknown-model"]
)
@pytest.mark.parametrize("endpoint", ["responses", "chat.completions"])
def test_unsupported_openai_off_fails_before_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model: str, endpoint: str
) -> None:
    import judgmentllm.providers.openai as provider

    def denied(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("credentials must not be accessed")

    monkeypatch.setattr(provider, "_resolve_credentials", denied)
    spec, _ = new_case(tmp_path, endpoint=endpoint)
    spec.model = model
    with pytest.raises(ValueError, match="exact OpenAI"):
        validate_openai_off(production_context(spec).scope)
    with pytest.raises(
        provider.OpenAIConfigurationError, match="execution.cache_policy"
    ):
        OpenAIAdapter(endpoint, model).validate_new_run(spec)
    legacy = validate_run_spec(spec, _cache_policy_version=1)
    OpenAIAdapter(endpoint, model).validate_new_run(legacy)
    supported, _ = new_case(tmp_path, endpoint=endpoint)
    with pytest.raises(
        provider.OpenAIConfigurationError, match="execution.cache_policy"
    ):
        context = production_context(supported)
        OpenAIAdapter(
            endpoint,
            supported.model,
            context=replace(
                context,
                scope=context.scope.model_copy(update={"sdk_version": "3.42.2"}),
            ),
        ).validate_new_run(supported)


@pytest.mark.parametrize(
    "change",
    [
        {"sdk_version": "3.42.2"},
        {"endpoint": "decisions"},
        {"model_scope": "unresolved", "model": None},
    ],
)
def test_off_scope_is_exact(change: dict[str, Any]) -> None:
    scope = production_context_for_identity("openai", "responses", "gpt-6-luna").scope
    with pytest.raises(ValueError):
        validate_openai_off(scope.model_copy(update=change))


@pytest.mark.parametrize(
    "unsupported",
    ["model", "endpoint", "sdk", "missing_sdk", "context_sdk", "synthetic_scope"],
)
def test_common_off_preflight_leaves_no_durable_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unsupported: str
) -> None:
    from judgmentllm import planning
    from judgmentllm.persistence.repository import Repository
    from judgmentllm.providers import openai as provider

    spec, _ = new_case(tmp_path)
    context = production_context(spec)
    spec.prompt.native_mechanism = None
    if unsupported == "model":
        spec.model = "unsupported-model"
    elif unsupported == "endpoint":
        spec.endpoint = "decisions"
    elif unsupported == "context_sdk":
        context = replace(
            context, scope=context.scope.model_copy(update={"sdk_version": "3.42.2"})
        )
    elif unsupported == "synthetic_scope":
        spec.model = "judgmentllm-synthetic-native-v1"

    def sdk_version(package: str) -> str:
        assert package == "openai"
        if unsupported == "missing_sdk":
            raise PackageNotFoundError(package)
        return "3.42.2" if unsupported == "sdk" else "3.42.1"

    def denied(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("preflight must precede credentials, SDK and run creation")

    monkeypatch.setattr(planning, "version", sdk_version)
    monkeypatch.setattr(provider, "_resolve_credentials", denied)
    monkeypatch.setattr("judgmentllm._auth._resolve_credentials", denied)
    monkeypatch.setattr(provider, "_sdk", denied)
    monkeypatch.setattr(Repository, "create_run", denied)
    profile = CapabilityProfile(modes=("live",), grouped=True)
    report = validate_run(spec, capabilities=profile)
    assert [issue.field for issue in report.issues if issue.severity == "error"] == (
        []
        if unsupported in {"context_sdk", "synthetic_scope"}
        else ["execution.cache_policy"]
    )
    legacy = validate_run_spec(spec, _cache_policy_version=1)
    assert validate_run(legacy, capabilities=profile).valid
    store = tmp_path / "store"
    with pytest.raises(PreparationError):
        prepare_run(
            spec,
            store=store,
            run_id="001",
            capabilities=profile,
            transport_context=context,
        )
    with ReadOnlyRepository(store) as repository:
        for table in ("runs", "requests", "attempts", "artifacts"):
            assert (
                repository.connection.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]
                == 0
            )
    assert not list(store.rglob("manifest.json"))


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["gpt-6-luna", "gpt-6.1-terra"])
@pytest.mark.parametrize("endpoint", ["responses", "chat.completions"])
@pytest.mark.parametrize("pairwise", [False, True])
async def test_actual_sdk_off_native_flex_reasoning_and_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    model: str,
    endpoint: str,
    pairwise: bool,
) -> None:
    spec, dependency = new_case(
        tmp_path, endpoint=endpoint, policy="native", pairwise=pairwise
    )
    spec.model = model
    spec.execution.tier = "flex"
    spec.generation = GenerationSpec(
        max_output_tokens=64,
        reasoning=ReasoningSpec(effort="none") if endpoint == "responses" else None,
    )
    context = production_context(spec)
    profile = _reviewed_profile(context)
    features = (*profile.supported_features, "pairwise_forced_choice")
    profile = profile.model_copy(
        update={
            "service_scopes": (
                ServiceQualificationScope(
                    account_scope="synthetic-account", tier="flex", features=features
                ),
            ),
            "supported_features": features,
            "reasoning_scopes": (
                ReasoningQualificationScopeV1(
                    account_scope="synthetic-account",
                    tier="flex",
                    reasoning=spec.generation.reasoning,
                    features=features,
                ),
            )
            if spec.generation.reasoning is not None
            else (),
        }
    )
    monkeypatch.setattr(production_registry, "_QUALIFIED_PROFILES", (profile,))
    record = dependency.request.pair if pairwise else dependency.request.response
    assert record is not None
    capabilities = OpenAIAdapter(endpoint, model).capabilities(spec)
    planned = next(plan_record(spec, record, capabilities=capabilities))
    dependency = RequestDependency(
        request=planned, dependency_sha256=dependency.dependency_sha256
    )
    responses = {
        "001": ResponseItem(item_id="001", text="Synthetic first."),
        "002": ResponseItem(item_id="002", text="Synthetic second."),
    }

    def lookup(kind: str, identifier: str) -> Any:
        return planned.pair if kind == "pair" else responses[identifier]

    wire = prepare_production_request(
        spec,
        dependency,
        physical_identity("001", planned.request_uid, "01"),
        context,
        lookup=lookup,
    )
    body = json.loads(wire.payload)
    assert body["prompt_cache_options"] == {"mode": "explicit"}
    assert "prompt_cache_breakpoint" not in wire.payload.decode()
    assert body["service_tier"] == "flex"
    assert (
        wire.output_commitment.cache_policy_version
        == wire.transport_commitment.cache_policy_version
        == 2
    )
    rebuilt = prepare_production_request(
        spec,
        dependency,
        wire.transport_plan.identity,
        context,
        frozen=wire.transport_commitment,
        lookup=lookup,
    )
    assert canonical_bytes(rebuilt) == canonical_bytes(wire)
    retry = prepare_production_request(
        spec,
        dependency,
        physical_identity("001", planned.request_uid, "02"),
        context,
        parent=wire.transport_commitment,
        lookup=lookup,
    )
    assert retry.payload == wire.payload
    seen: list[bytes] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        assert request.content == wire.payload
        seen.append(request.content)
        response = envelope(
            endpoint, '{"winner_id":"essay_01"}' if pairwise else '{"scores":[]}'
        )
        response["service_tier"] = "flex"
        return httpx2.Response(200, json=response, request=request)

    adapter = OpenAIAdapter(
        endpoint, model, context=context, http_transport=httpx2.MockTransport(respond)
    )
    try:
        await adapter.submit(
            wire,
            submission_id=wire.transport_plan.identity.submission_id,
            correlation_id=wire.transport_plan.identity.correlation_id,
        )
    finally:
        await adapter.aclose()
    assert seen == [wire.payload]


@pytest.mark.parametrize("endpoint", ["responses", "chat.completions"])
@pytest.mark.parametrize("written", [None, 0, 7])
def test_cache_write_usage_keeps_unknown_and_zero(
    endpoint: str, written: int | None
) -> None:
    details = {"cached_tokens": 0}
    if written is not None:
        details["cache_write_tokens"] = written
    usage = _usage(
        endpoint,
        {
            "input_tokens" if endpoint == "responses" else "prompt_tokens": 20,
            "input_tokens_details"
            if endpoint == "responses"
            else "prompt_tokens_details": details,
        },
    )
    assert usage is not None
    assert usage.cache_read_tokens == 0 and usage.cache_write_tokens == written
    assert usage.total_tokens is None and usage.reported_cost is None
    assert usage.semantics_version == (1 if written is None else 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("cache", ["auto", "off"])
@pytest.mark.parametrize("capture", ["full", "minimal"])
async def test_genuine_historical_requests_replay_without_new_control(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cache: str, capture: str
) -> None:
    fixture = Path(__file__).parent / "fixtures/cache-history"
    provenance = json.loads((fixture / "provenance.json").read_bytes())
    raw = base64.b64decode(
        (fixture / "store.tar.gz.base64").read_bytes(), validate=True
    )
    assert hashlib.sha256(raw).hexdigest() == provenance["archive_sha256"]
    assert (
        hashlib.sha256((fixture / "producer.py.txt").read_bytes()).hexdigest()
        == provenance["producer_script_sha256"]
    )
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
        archive.extractall(tmp_path, filter="data")
    monkeypatch.chdir(tmp_path)
    run_id = f"{cache}-{capture}"
    spec = validate_run_spec_json(
        (tmp_path / f"{run_id}.json").read_text(), _cache_policy_version=1
    )
    assert isinstance(spec, RunSpecV3)
    context = production_context(spec)
    original = (tmp_path / f"{run_id}-payload.bin").read_bytes()
    loaded = load_resume_spec(tmp_path / f"{run_id}.json", run_id, tmp_path / "store")
    assert loaded.execution.cache_policy == cache and loaded.cache_policy_version == 1
    if capture == "minimal":
        # Detached replay verifies bytes without waiving original-source checks.
        receipt = TransportCommitmentV2.model_validate_json(
            (tmp_path / f"{run_id}-receipt.json").read_bytes()
        )
        planned = next(
            plan_record(spec, ResponseItem(item_id="001", text="Synthetic seed grows."))
        )
        dependency = RequestDependency(
            request=planned, dependency_sha256=receipt.dependency_sha256
        )
        rebuilt = prepare_production_request(
            spec, dependency, receipt.identity, context, frozen=receipt
        )
        assert rebuilt.payload == original and rebuilt.transport_commitment == receipt
        return
    with open_prepared_run(
        run_id,
        store=tmp_path / "store",
        spec=spec,
        transport_context=context,
        use_frozen_snapshot=capture == "full",
    ) as prepared:
        assert (
            prepared.manifest.cache_policy_version
            == prepared.spec.cache_policy_version
            == 1
        )
        dependency = next(prepared.iter_request_dependencies())
        attempt = next(prepared.repository.iter_attempts(run_id))
        receipt = prepared.transport_for_attempt(attempt.attempt_id)
        assert isinstance(receipt, TransportCommitmentV2)
        assert (
            canonical_bytes(receipt)
            == (tmp_path / f"{run_id}-receipt.json").read_bytes()
        )
        rebuilt = prepared.prepare_transport_attempt(dependency, receipt.identity)
        assert rebuilt.payload == original
        import judgmentllm.transport_dispatch as dispatch

        def inconsistent(*args: Any, **kwargs: Any) -> Any:
            changed = deepcopy(rebuilt)
            object.__setattr__(changed.output_commitment, "cache_policy_version", 2)
            return changed

        with monkeypatch.context() as patch:
            patch.setattr(dispatch, "prepare_request", inconsistent)
            with pytest.raises(PreparationError, match="transport cache policy"):
                prepared.prepare_transport_attempt(dependency, receipt.identity)
        prepared.spec._cache_policy_version = 2
        with pytest.raises(PreparationError):
            prepared.verify_compatibility()
        prepared.spec._cache_policy_version = 1
        manifest_path = prepared.store.root / "manifest.json"
        manifest_bytes = manifest_path.read_bytes()
    seen: list[bytes] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        assert request.content == original
        assert b'"prompt_cache_options"' not in request.content
        seen.append(request.content)
        return httpx2.Response(
            200,
            json=envelope(
                "responses", '{"scores":[{"trait_id":"01","category_label":"high"}]}'
            ),
            request=request,
        )

    adapter = OpenAIAdapter(
        "responses", spec.model, http_transport=httpx2.MockTransport(respond)
    )

    def interrupted(stage: str, _: str) -> None:
        if stage == "after_evidence_commit":
            raise RuntimeError("synthetic interruption after original evidence")

    with pytest.raises(ExceptionGroup):
        await execute_run(
            run_id,
            store=tmp_path / "store",
            spec=spec,
            adapter=adapter,
            use_frozen_snapshot=True,
            fault=interrupted,
        )
    assert seen == [original]
    for _ in range(2):
        report = await execute_run(
            run_id,
            store=tmp_path / "store",
            spec=spec,
            adapter=adapter,
            use_frozen_snapshot=capture == "full",
        )
        assert report.summary.successful_results == 1
    assert len(seen) == 1 and manifest_path.read_bytes() == manifest_bytes
    with ReadOnlyRepository(tmp_path / "store") as repository:
        tasks = tuple(result.task_id for result in repository.iter_results(run_id))
    retry = authorize_subset_retry(
        run_id,
        store=tmp_path / "store",
        spec=spec,
        adapter=adapter,
        use_frozen_snapshot=True,
        task_ids=tasks,
        operation_id="archived-legacy-retry",
        allow_completed=True,
    )
    assert retry.attempt_number == 2
    await execute_run(
        run_id,
        store=tmp_path / "store",
        spec=spec,
        adapter=adapter,
        use_frozen_snapshot=True,
    )
    assert seen == [original, original]
    with open_prepared_run(
        run_id,
        store=tmp_path / "store",
        spec=spec,
        transport_context=context,
        use_frozen_snapshot=True,
    ) as prepared:
        child = prepared.transport_for_attempt(retry.attempt_id)
        assert isinstance(child, TransportCommitmentV2)
        assert child.cache_policy_version == 1
        assert child.parent_commitment_sha256 == receipt.commitment_sha256
        assert child.payload_sha256 == receipt.payload_sha256
        assert canonical_bytes(
            prepared.transport_for_attempt(attempt.attempt_id)
        ) == canonical_bytes(receipt)
    assert manifest_path.read_bytes() == manifest_bytes


@pytest.mark.asyncio
@pytest.mark.parametrize("cache", ["auto", "off"])
async def test_minimal_legacy_in_place_resume_and_interrupted_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cache: str
) -> None:
    fixture = Path(__file__).parent / "fixtures/cache-history"
    raw = base64.b64decode(
        (fixture / "store.tar.gz.base64").read_bytes(), validate=True
    )
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
        archive.extractall(tmp_path, filter="data")
    monkeypatch.chdir(tmp_path)
    run_id = f"{cache}-minimal"
    spec = validate_run_spec_json(
        (tmp_path / f"{run_id}.json").read_text(), _cache_policy_version=1
    )
    assert isinstance(spec, RunSpecV3)
    context = production_context(spec)
    original_body = (tmp_path / f"{run_id}-payload.bin").read_bytes()
    store = tmp_path / "in-place"
    source = tmp_path / "pre-change"
    source.mkdir()
    base = "34dd83f57525dc3898fd735530710c4be03fa2a9"
    archived_source = subprocess.run(
        ["git", "archive", "--format=tar", base, "src"],
        cwd=fixture.parents[2],
        capture_output=True,
        check=True,
        timeout=30,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(archived_source)) as archive:
        archive.extractall(source, filter="data")
    # Run the actual old producer in place; no consumer identity/policy is patched.
    subprocess.run(
        [
            sys.executable,
            str(fixture / "in_place_producer.py.txt"),
            cache,
            str(source / "src"),
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        timeout=60,
        env={
            **os.environ,
            "PYTHONPATH": str(source / "src"),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
    )

    with open_prepared_run(
        run_id, store=store, spec=spec, transport_context=context
    ) as prepared:
        assert (
            prepared.spec.cache_policy_version
            == prepared.manifest.cache_policy_version
            == 1
        )
        assert prepared.manifest.capture == "minimal"
        assert all(source.snapshot is None for source in prepared.manifest.sources)
        dependency = next(prepared.iter_request_dependencies())
        original_attempt = next(prepared.repository.iter_attempts(run_id))
        original_receipt = prepared.transport_for_attempt(original_attempt.attempt_id)
        original_bytes = canonical_bytes(original_receipt)
        manifest_path = prepared.store.root / "manifest.json"
        manifest_bytes = manifest_path.read_bytes()
        original_generation = prepared.manifest.generation
        original_configuration = prepared.manifest.configuration_sha256
        assert b'"cache_policy_version"' not in manifest_bytes
        assert b'"cache_policy_version"' not in original_bytes
        assert (
            prepared.prepare_transport_attempt(
                dependency, original_receipt.identity
            ).payload
            == original_body
        )

    seen: list[bytes] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        assert request.content == original_body
        assert b'"prompt_cache_options"' not in request.content
        seen.append(request.content)
        return httpx2.Response(
            200,
            json=envelope(
                "responses", '{"scores":[{"trait_id":"01","category_label":"high"}]}'
            ),
            request=request,
        )

    def interrupted(stage: str, _: str) -> None:
        if stage == "after_claim":
            raise RuntimeError("synthetic interruption after original claim")

    adapter = OpenAIAdapter(
        "responses", spec.model, http_transport=httpx2.MockTransport(respond)
    )
    try:
        with pytest.raises(ExceptionGroup):
            await execute_run(
                run_id, store=store, spec=spec, adapter=adapter, fault=interrupted
            )
        assert not seen
        report = await execute_run(run_id, store=store, spec=spec, adapter=adapter)
        assert report.summary.successful_results == 1 and len(seen) == 1
        with ReadOnlyRepository(store) as repository:
            tasks = tuple(result.task_id for result in repository.iter_results(run_id))
        retry = authorize_subset_retry(
            run_id,
            store=store,
            spec=spec,
            adapter=adapter,
            task_ids=tasks,
            operation_id="legacy-retry",
            allow_completed=True,
        )
        assert retry.attempt_number == 2
        await execute_run(run_id, store=store, spec=spec, adapter=adapter)
        assert seen == [original_body, original_body]
    finally:
        await adapter.aclose()
    with open_prepared_run(
        run_id, store=store, spec=spec, transport_context=context
    ) as prepared:
        receipt = prepared.transport_for_attempt(retry.attempt_id)
        assert isinstance(receipt, TransportCommitmentV2)
        assert receipt.cache_policy_version == 1
        assert receipt.parent_commitment_sha256 == original_receipt.commitment_sha256
        assert receipt.payload_sha256 == original_receipt.payload_sha256
        assert (
            canonical_bytes(prepared.transport_for_attempt(original_attempt.attempt_id))
            == original_bytes
        )
        assert prepared.manifest.configuration_sha256 == original_configuration
        assert prepared.manifest.generation == original_generation
        assert manifest_path.read_bytes() == manifest_bytes

    # Even identical replacement bytes cannot authorize a changed minimal source.
    source = tmp_path / "responses.jsonl"
    replacement = tmp_path / "replacement.jsonl"
    replacement.write_bytes(source.read_bytes())
    replacement.replace(source)
    with pytest.raises(PreparationError):
        open_prepared_run(run_id, store=store, spec=spec, transport_context=context)
    assert len(seen) == 2
