"""Durable native requests through real preparation, execution and recovery."""

# pyright: reportPrivateUsage=false
from __future__ import annotations

import builtins
import io
import json
import os
import socket
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import Any, Literal, cast

import pytest
from native_transport_fixture import (
    MECHANISMS,
    PRIVATE_LABEL,
    PRIVATE_TEXT,
    NativeFixtureAdapter,
    context_for,
    specification,
)

from judgmentllm.config import RunSpecV3, load_run_spec, validate_run_spec_json
from judgmentllm.execution import execute_run, recover_run
from judgmentllm.execution.retry import authorize_subset_retry
from judgmentllm.persistence.hashing import (
    bytes_digest,
    canonical_bytes,
)
from judgmentllm.persistence.models import IntegrityError
from judgmentllm.persistence.repository import ReadOnlyRepository
from judgmentllm.planning import CapabilityProfile, iter_requests
from judgmentllm.preparation import open_prepared_run, prepare_run
from judgmentllm.preparation.freezing import PreparationError
from judgmentllm.providers import (
    MockMemberScenario,
    MockScenario,
    ProviderError,
    ProviderIntegrityError,
)
from judgmentllm.results import PairwiseResult, RubricResult
from judgmentllm.transport import (
    TransportCommitmentV1,
    build_output_commitment_v3,
    physical_identity,
)


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Effective guards precede service calls; Unix event-loop sockets work."""
    from native_sdk_guard import _CREDENTIAL_KEYS

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("native integration operation blocked offline")

    original_socket = socket.socket
    original_connect = original_socket.connect
    original_socketpair = socket.socketpair
    original_open = builtins.open
    original_io_open = io.open
    selfpipe = False
    original_import = builtins.__import__
    original_environment = type(os.environ).__getitem__

    class GuardedSocket(original_socket):
        def __init__(
            self, family: int = socket.AF_INET, *args: Any, **kwargs: Any
        ) -> None:
            if family in (socket.AF_INET, socket.AF_INET6) and not selfpipe:
                forbidden()
            super().__init__(family, *args, **kwargs)

    def guarded_connect(current: Any, address: Any) -> Any:
        if (
            selfpipe
            and isinstance(address, tuple)
            and address[0] in {"127.0.0.1", "::1"}
        ):
            return original_connect(current, cast(Any, address))
        return forbidden()

    def local_socketpair(*args: Any, **kwargs: Any) -> Any:
        nonlocal selfpipe
        selfpipe = True
        try:
            return original_socketpair(*args, **kwargs)
        finally:
            selfpipe = False

    def guarded_open(original: Any, file: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(file, (str, bytes, os.PathLike)):
            normalized = os.fsdecode(cast(Any, file)).replace("\\", "/").lower()
            if normalized.rsplit("/", 1)[-1] in {
                ".env",
                ".netrc",
                "credentials.json",
                "application_default_credentials.json",
                "service_account.json",
            } or any(
                part in normalized
                for part in ("/.aws/", "/.config/gcloud/", "/.anthropic/")
            ):
                return forbidden()
        return original(file, *args, **kwargs)

    def guarded_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name.split(".")[0] in {
            "openai",
            "anthropic",
            "google",
            "together",
            "ollama",
            "keyring",
        }:
            return forbidden()
        return original_import(name, *args, **kwargs)

    def guarded_environment(environment: Any, key: Any) -> Any:
        if os.fsdecode(key) in _CREDENTIAL_KEYS:
            return forbidden()
        return original_environment(environment, key)

    monkeypatch.setattr(socket, "socket", GuardedSocket)
    monkeypatch.setattr(original_socket, "connect", guarded_connect)
    monkeypatch.setattr(socket, "socketpair", local_socketpair)
    monkeypatch.setattr(
        builtins,
        "open",
        partial(guarded_open, original_open),
    )
    monkeypatch.setattr(
        io,
        "open",
        partial(guarded_open, original_io_open),
    )
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(builtins, "__import__", guarded_import)
    monkeypatch.setattr(type(os.environ), "__getitem__", guarded_environment)
    for operation in (
        lambda: socket.socket(),
        lambda: socket.getaddrinfo("metadata.google.internal", 80),
        lambda: __import__("openai"),
        lambda: os.environ["OPENAI_API_KEY"],
        lambda: os.environ["GOOGLE_APPLICATION_CREDENTIALS"],
        lambda: Path("application_default_credentials.json").read_bytes(),
    ):
        with pytest.raises(RuntimeError, match="blocked offline"):
            operation()


def prepared_case(
    root: Path,
    mechanism: str = MECHANISMS[0],
    *,
    capture: Literal["full", "minimal"] = "minimal",
    scenario: MockScenario | None = None,
    retry_valid: bool = False,
    run_id: str = "001",
    **settings: Any,
) -> tuple[NativeFixtureAdapter, Path, Any]:
    source = specification(root / "inputs", mechanism, capture=capture, **settings)
    spec = load_run_spec(source)
    assert isinstance(spec, RunSpecV3)
    store = root / "runtime"
    adapter = NativeFixtureAdapter(
        store / "synthetic-service.sqlite3",
        mechanism,
        scenario=scenario,
        retry_valid=retry_valid,
    )
    with prepare_run(
        source,
        store=store,
        run_id=run_id,
        capabilities=adapter.capabilities(spec),
        transport_context=adapter.transport_context,
    ):
        pass
    return adapter, store, spec


def reopen(adapter: NativeFixtureAdapter, store: Path, **kwargs: Any) -> Any:
    return open_prepared_run(
        "001", store=store, transport_context=adapter.transport_context, **kwargs
    )


@pytest.mark.parametrize("mechanism", MECHANISMS)
@pytest.mark.parametrize("policy", ["native", "auto", "text"])
@pytest.mark.parametrize("supported", [True, False, None])
@pytest.mark.parametrize("strict", [False, True])
def test_request_policy_matrix(
    tmp_path: Path, mechanism: str, policy: str, supported: bool | None, strict: bool
) -> None:
    source = specification(tmp_path, mechanism, policy=policy, strict=strict)
    spec = load_run_spec(source)
    assert isinstance(spec, RunSpecV3)
    request = next(
        iter_requests(
            spec,
            capabilities=CapabilityProfile(
                explanations=True, grouped=True, ties=True, modes=("live",)
            ),
        )
    )
    context = context_for(mechanism, supported=supported)
    fails = (policy == "native" and supported is not True) or (
        policy == "auto" and strict and supported is None
    )
    if fails:
        with pytest.raises(ValueError):
            build_output_commitment_v3(spec, request, context)
        return
    output = build_output_commitment_v3(spec, request, context)
    native = policy != "text" and supported is True
    assert output.selection.mechanism == (mechanism if native else "text")
    assert (output.projection is not None) is native


@pytest.mark.parametrize("mechanism", MECHANISMS)
def test_caller_evidence_alone_cannot_authorize_native(
    tmp_path: Path, mechanism: str
) -> None:
    source = specification(tmp_path, mechanism)
    spec = load_run_spec(source)
    assert isinstance(spec, RunSpecV3)
    with pytest.raises(ValueError):
        build_output_commitment_v3(
            spec, next(iter_requests(spec)), context_for(mechanism, trusted=False)
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("mechanism", MECHANISMS)
@pytest.mark.parametrize("capture", ["full", "minimal"])
async def test_complete_seal_execute_reopen_recover(
    tmp_path: Path, mechanism: str, capture: Literal["full", "minimal"]
) -> None:
    adapter, store, _ = prepared_case(tmp_path, mechanism, capture=capture)
    with reopen(adapter, store) as prepared:
        assert prepared.manifest.schema_version == 3
        assert prepared.manifest.initial_transport_count == 1
        dependency = next(prepared.iter_request_dependencies())
        attempt = next(prepared.repository.iter_attempts("001"))
        receipt = prepared.transport_for_attempt(attempt.attempt_id)
        identity = physical_identity(
            "001", dependency.request.request_uid, attempt.attempt_id
        )
        wire = prepared.prepare_transport_attempt(dependency, identity, frozen=receipt)
        assert (
            wire.payload_sha256 == attempt.payload_sha256 == bytes_digest(wire.payload)
        )
        assert wire.prompt_sha256 == bytes_digest(wire.rendered_text.encode())
        assert receipt == wire.transport_commitment
        payload = json.loads(wire.payload)
        assert payload["protocol"] == "judgmentllm-native-offline-v1"
        assert payload["format_fragment"] == json.loads(
            wire.output_commitment.projection.format_fragment_text
        )
        assert (
            bytes_digest(wire.payload)
            != wire.output_commitment.projection.format_fragment_sha256
        )
        original = canonical_bytes(receipt)
    result = await execute_run("001", store=store, adapter=adapter)
    assert result.summary.successful_results == 2
    assert adapter.submit_calls == adapter.ledger.acceptance_count() == 1
    again = await recover_run("001", store=store, adapter=adapter)
    assert again.summary.results == 2 and adapter.submit_calls == 1
    with reopen(adapter, store) as prepared:
        attempt = next(prepared.repository.iter_attempts("001"))
        assert (
            canonical_bytes(prepared.transport_for_attempt(attempt.attempt_id))
            == original
        )
    with ReadOnlyRepository(store) as repository:
        results = tuple(repository.iter_results("001"))
        assert all(result.status == "complete" for result in results)
        assert all(
            result.provider == adapter.transport_context.scope.provider
            for result in results
        )
    if capture == "minimal":
        # Assessment category output is allowed; source text and complete schemas
        # are forbidden in registry, artifacts, receipts and service metadata.
        for path in store.rglob("*"):
            if path.is_file():
                assert PRIVATE_TEXT.encode() not in path.read_bytes()
        for path in store.glob("runs/*/transports/*.json"):
            assert PRIVATE_LABEL.encode() not in path.read_bytes()


@pytest.mark.asyncio
@pytest.mark.parametrize("mechanism", MECHANISMS)
@pytest.mark.parametrize("capture", ["full", "minimal"])
async def test_auto_freezes_mixed_transports_for_group_and_single_remainder(
    tmp_path: Path,
    mechanism: str,
    capture: Literal["full", "minimal"],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One run and its retry keep request-specific transport choices frozen."""
    from judgmentllm.execution.retry import subset_dependency

    source = specification(
        tmp_path / "inputs",
        mechanism,
        capture=capture,
        policy="auto",
        traits=3,
        explanations=False,
        strict=False,
    )
    document = json.loads(source.read_text(encoding="utf-8"))
    document["execution"]["groups"] = [["01", "02"], ["03"]]
    source.write_text(json.dumps(document), encoding="utf-8")
    spec = load_run_spec(source)
    assert isinstance(spec, RunSpecV3)
    context = context_for(mechanism)
    if context.openai_catalog is not None:
        context = replace(
            context,
            openai_catalog=replace(
                context.openai_catalog,
                profiles=tuple(
                    profile.model_copy(
                        update={
                            "features": tuple(
                                feature
                                for feature in profile.features
                                if feature != "rubric_grouped"
                            )
                        }
                    )
                    for profile in context.openai_catalog.profiles
                ),
            ),
        )
    else:
        assert context.provider_catalog is not None
        context = replace(
            context,
            provider_catalog=replace(
                context.provider_catalog,
                profiles=tuple(
                    profile.model_copy(
                        update={
                            "features": tuple(
                                feature
                                for feature in profile.features
                                if feature != "rubric_grouped"
                            )
                        }
                    )
                    for profile in context.provider_catalog.profiles
                ),
            ),
        )
    store = tmp_path / "runtime"
    ledger = store / "synthetic-service.sqlite3"
    scenario = MockScenario(
        members={"02": MockMemberScenario(outcome="invalid_category")}
    )
    adapter = NativeFixtureAdapter(
        ledger, mechanism, context=context, scenario=scenario, retry_valid=True
    )
    capabilities = adapter.capabilities(spec)
    assert capabilities.grouped is True

    def committed_attempts(prepared: Any) -> dict[str, bytes]:
        return {
            attempt.attempt_id: canonical_bytes(
                {
                    "request_uid": attempt.request_uid,
                    "attempt_number": attempt.attempt_number,
                    "task_ids": list(
                        prepared.repository.attempt_task_ids("001", attempt.attempt_id)
                    ),
                    "membership_sha256": attempt.membership_sha256,
                    "prompt_sha256": attempt.prompt_sha256,
                    "payload_sha256": attempt.payload_sha256,
                    "receipt": prepared.transport_for_attempt(attempt.attempt_id),
                }
            )
            for attempt in prepared.repository.iter_attempts("001")
        }

    with prepare_run(
        source,
        store=store,
        run_id="001",
        capabilities=capabilities,
        transport_context=adapter.transport_context,
    ) as prepared:
        assert prepared.manifest.schema_version == 3
        dependencies = tuple(prepared.iter_request_dependencies())
        assert tuple(d.request.trait_ids for d in dependencies) == (
            ("01", "02"),
            ("03",),
        )
        attempts = tuple(prepared.repository.iter_attempts("001"))
        assert len(tuple(prepared.repository.iter_requests("001"))) == 2
        assert len(attempts) == prepared.manifest.initial_transport_count == 2
        assert prepared.repository.summary("001").tasks == 3
        assert tuple(prepared.repository.iter_submissions("001")) == ()
        receipts = tuple(prepared.transport_for_attempt(a.attempt_id) for a in attempts)
        assert tuple(r.mechanism for r in receipts) == ("text", mechanism)
        assert all(r.selection.policy == "auto" for r in receipts)
        assert isinstance(receipts[0], TransportCommitmentV1)
        group_decision = (
            receipts[0].selection.openai_decision
            or receipts[0].selection.provider_decision
        )
        assert group_decision is not None
        assert group_decision.native_support == "unknown"
        assert group_decision.model_support == "supported"
        assert group_decision.diagnostic.reason == "trusted_fixture_missing"
        for dependency, attempt, receipt in zip(
            dependencies, attempts, receipts, strict=True
        ):
            wire = prepared.prepare_transport_attempt(
                dependency,
                physical_identity("001", attempt.request_uid, attempt.attempt_id),
                frozen=receipt,
            )
            assert wire.output_commitment.local_validation_required
            assert (wire.output_commitment.projection is not None) == (
                receipt.mechanism != "text"
            )
            assert bytes_digest(wire.payload) == attempt.payload_sha256
        originals = committed_attempts(prepared)
        group_request_uid = attempts[0].request_uid
        parent_receipt = receipts[0]
    initial = await execute_run("001", store=store, adapter=adapter)
    assert initial.summary.tasks == initial.summary.results == 3
    assert initial.summary.successful_results == 2
    assert initial.summary.unsuccessful_results == 1
    assert initial.summary.attempts == 2
    assert adapter.submit_calls == adapter.ledger.acceptance_count() == 2
    with ReadOnlyRepository(store) as repository:
        initial_results = tuple(repository.iter_results("001"))
        failed = next(
            result for result in initial_results if result.status == "invalid"
        )
        assert isinstance(failed, RubricResult) and failed.trait_id == "02"
        initial_outcomes = {
            (result.task_id, result.attempt_id): canonical_bytes(result)
            for result in initial_results
        }
    authorize_subset_retry(
        "001",
        store=store,
        adapter=adapter,
        spec=spec,
        operation_id="mixed-text-subset",
        task_ids=(failed.task_id,),
    )
    with reopen(adapter, store) as prepared:
        assert prepared.manifest.schema_version == 3
        retry = prepared.repository.get_attempt_for_request(
            "001", group_request_uid, attempt_number=2
        )
        assert retry is not None
        retry_members = prepared.repository.attempt_task_ids("001", retry.attempt_id)
        assert retry_members == (failed.task_id,)
        subset = subset_dependency(
            prepared.dependency_for_request(group_request_uid), retry_members
        )
        # This exact subset qualifies natively as a new physical request. Its
        # authorized retry must nevertheless retain the committed text family.
        newly_eligible = build_output_commitment_v3(
            spec, subset.request, adapter.transport_context
        )
        assert newly_eligible.selection.mechanism == mechanism
        assert newly_eligible.projection is not None
        child = prepared.transport_for_attempt(retry.attempt_id)
        assert child.mechanism == "text"
        assert child.selection.kind == "inherited-v1"
        assert child.parent_commitment_sha256 == parent_receipt.commitment_sha256
        assert child.neutral_schema_sha256 != parent_receipt.neutral_schema_sha256
        assert child.prompt_sha256 != parent_receipt.prompt_sha256
        assert child.payload_sha256 != parent_receipt.payload_sha256
        after_authorization = committed_attempts(prepared)
        assert len(after_authorization) == 3
        assert all(
            after_authorization[key] == value for key, value in originals.items()
        )
        assert prepared.manifest.initial_transport_count == 2

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("mixed committed transports must never reselect")

    monkeypatch.setattr("judgmentllm.transport._resolve_native_transport", forbidden)
    monkeypatch.setattr("judgmentllm.transport._resolve_native_transport_v2", forbidden)
    report = await execute_run("001", store=store, adapter=adapter)
    assert report.summary.tasks == report.summary.logical_completed_tasks == 3
    assert report.summary.successful_results == 3
    assert report.summary.results == 4 and report.summary.unsuccessful_results == 1
    assert report.summary.attempts == 3
    assert adapter.submit_calls == adapter.ledger.acceptance_count() == 3
    with ReadOnlyRepository(store) as repository:
        results = tuple(repository.iter_results("001"))
        retained = {
            (result.task_id, result.attempt_id): canonical_bytes(result)
            for result in results
        }
        assert all(retained[key] == value for key, value in initial_outcomes.items())
        complete = tuple(result for result in results if result.status == "complete")
        assert len({result.task_id for result in complete}) == len(complete) == 3
        assert all(
            isinstance(result, RubricResult)
            and result.category_label == PRIVATE_LABEL
            and result.numeric_category_value == 1.0
            for result in complete
        )
    fresh = NativeFixtureAdapter(
        ledger, mechanism, context=context, scenario=scenario, retry_valid=True
    )
    recovered = await recover_run("001", store=store, adapter=fresh)
    assert recovered.summary.successful_results == 3
    assert recovered.summary.results == 4 and recovered.summary.attempts == 3
    with reopen(fresh, store) as prepared:
        assert committed_attempts(prepared) == after_authorization
        assert len(tuple(prepared.repository.iter_requests("001"))) == 2
    assert fresh.submit_calls == 0
    assert fresh.ledger.acceptance_count() == adapter.ledger.acceptance_count() == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("mechanism", MECHANISMS)
@pytest.mark.parametrize("ties", [False, True])
async def test_pair_orientation_and_ties(
    tmp_path: Path, mechanism: str, ties: bool
) -> None:
    adapter, store, _ = prepared_case(
        tmp_path,
        mechanism,
        pairwise=True,
        ties=ties,
        scenario=MockScenario(winner="tie" if ties else "first"),
    )
    report = await execute_run("001", store=store, adapter=adapter)
    assert report.summary.successful_results == 1
    with ReadOnlyRepository(store) as repository:
        result = next(repository.iter_results("001"))
        assert isinstance(result, PairwiseResult)
        assert result.winner_id == (None if ties else "002")
        assert result.tie is ties
        assert tuple(result.presented_order) == ("002", "001")
        assert tuple(result.original_order) == ("001", "002")


@pytest.mark.asyncio
@pytest.mark.parametrize("mechanism", MECHANISMS)
@pytest.mark.parametrize("capture", ["full", "minimal"])
async def test_grouped_retry_has_own_schema_without_family_switch(
    tmp_path: Path, mechanism: str, capture: Literal["full", "minimal"]
) -> None:
    scenario = MockScenario(
        members={"02": MockMemberScenario(outcome="invalid_category")}
    )
    adapter, store, spec = prepared_case(
        tmp_path, mechanism, capture=capture, scenario=scenario, retry_valid=True
    )
    report = await execute_run("001", store=store, adapter=adapter)
    assert (
        report.summary.successful_results == 1
        and report.summary.unsuccessful_results == 1
    )
    with ReadOnlyRepository(store) as repository:
        original_attempt = next(repository.iter_attempts("001"))
        failed = next(
            result
            for result in repository.iter_results("001")
            if result.status == "invalid"
        )
        original_rows = tuple(repository.iter_results("001"))
    authorize_subset_retry(
        "001",
        store=store,
        adapter=adapter,
        operation_id="retry-001",
        task_ids=(failed.task_id,),
        spec=spec,
    )
    result = await execute_run("001", store=store, adapter=adapter)
    assert result.summary.successful_results == 2 and result.summary.results == 3
    assert adapter.ledger.acceptance_count() == 2
    with reopen(adapter, store) as prepared:
        attempts = tuple(prepared.repository.iter_attempts("001"))
        assert len(attempts) == 2
        parent = prepared.transport_for_attempt(original_attempt.attempt_id)
        child = prepared.transport_for_attempt(attempts[1].attempt_id)
        assert child != parent
        assert child.parent_commitment_sha256 == parent.commitment_sha256
        assert child.mechanism == parent.mechanism == mechanism
        assert attempts[1].payload_sha256 != original_attempt.payload_sha256
        assert prepared.repository.attempt_task_ids("001", attempts[1].attempt_id) == (
            failed.task_id,
        )
        assert tuple(prepared.repository.iter_results("001"))[:2] == original_rows
    await recover_run("001", store=store, adapter=adapter)
    assert adapter.ledger.acceptance_count() == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("accepted", [False, True])
async def test_uncertainty_never_resends_or_changes_mechanism(
    tmp_path: Path, accepted: bool
) -> None:
    adapter, store, _ = prepared_case(
        tmp_path,
        scenario=MockScenario(
            transport="timeout_after_acceptance"
            if accepted
            else "timeout_before_acceptance"
        ),
    )
    adapter.reconciliation_available = False
    report = await execute_run("001", store=store, adapter=adapter)
    assert report.summary.results == 0
    await recover_run("001", store=store, adapter=adapter)
    assert adapter.submit_calls == 1 and adapter.ledger.acceptance_count() == int(
        accepted
    )
    adapter.reconciliation_available = True
    recovered = await recover_run("001", store=store, adapter=adapter)
    assert recovered.summary.successful_results == (2 if accepted else 0)
    assert adapter.submit_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome,status",
    [
        ("refusal", "refused"),
        ("provider_error", "errored"),
        ("confirmed_cancellation", "cancelled"),
        ("invalid", "invalid"),
    ],
)
async def test_terminal_outcomes_never_fallback_or_rescore(
    tmp_path: Path, outcome: str, status: str
) -> None:
    adapter, store, _ = prepared_case(
        tmp_path, scenario=MockScenario.model_validate({"outcome": outcome})
    )
    report = await execute_run("001", store=store, adapter=adapter)
    assert report.summary.successful_results == 0
    with ReadOnlyRepository(store) as repository:
        assert {result.status for result in repository.iter_results("001")} == {status}
        assert all(
            isinstance(result, RubricResult) and result.primary_score is None
            for result in repository.iter_results("001")
        )
    await recover_run("001", store=store, adapter=adapter)
    assert adapter.submit_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    [
        "missing",
        "duplicate",
        "invalid_category",
        "malformed",
        "missing_explanation",
        "invalid_explanation",
    ],
)
async def test_local_member_validation_retains_partial_outcomes(
    tmp_path: Path, outcome: str
) -> None:
    adapter, store, _ = prepared_case(
        tmp_path,
        scenario=MockScenario(
            members={"02": MockMemberScenario.model_validate({"outcome": outcome})}
        ),
    )
    report = await execute_run("001", store=store, adapter=adapter)
    assert (
        report.summary.successful_results == 1
        and report.summary.unsuccessful_results == 1
    )
    assert adapter.submit_calls == adapter.ledger.acceptance_count() == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "corruption", ["invalid_json", "duplicate_root_key", "unexpected_root"]
)
async def test_raw_corruption_is_observable_failure(
    tmp_path: Path, corruption: str
) -> None:
    adapter, store, _ = prepared_case(
        tmp_path, scenario=MockScenario.model_validate({"wire_corruption": corruption})
    )
    report = await execute_run("001", store=store, adapter=adapter)
    assert report.summary.successful_results == 0
    assert adapter.submit_calls == adapter.ledger.acceptance_count() == 1


@pytest.mark.parametrize("capture", ["full", "minimal"])
@pytest.mark.parametrize(
    "damage",
    [
        "receipt_missing",
        "receipt_changed",
        "prompt_artifact",
        "payload_artifact",
        "format_artifact",
        "schema_artifact",
        "context_scope",
        "context_evidence",
        "context_fixture",
        "context_service",
    ],
)
def test_reopen_rejects_missing_changed_or_rehashed_evidence(
    tmp_path: Path, capture: Literal["full", "minimal"], damage: str
) -> None:
    adapter, store, _ = prepared_case(tmp_path, capture=capture)
    with reopen(adapter, store) as prepared:
        attempt = next(prepared.repository.iter_attempts("001"))
        dependency = next(prepared.iter_request_dependencies())
        receipt = prepared.transport_for_attempt(attempt.attempt_id)
        wire = prepared.prepare_transport_attempt(
            dependency,
            physical_identity("001", attempt.request_uid, attempt.attempt_id),
            frozen=receipt,
        )
        directory = prepared.repository.run_directory("001")
    if damage.startswith("context_"):
        context = adapter.transport_context
        if damage == "context_scope":
            context = replace(
                context, scope=context.scope.model_copy(update={"sdk_version": "0.0.0"})
            )
        elif damage == "context_evidence":
            assert context.evidence is not None
            context = replace(
                context,
                evidence=context.evidence.model_copy(update={"supported": False}),
            )
        elif damage == "context_fixture":
            context = replace(context, fixture_sha256="a" * 64)
        else:
            context = replace(context, service_identity="different-synthetic-service")
        adapter.transport_context = context
    elif damage.startswith("receipt_"):
        path = next(directory.glob("transports/*.json"))
        if damage == "receipt_missing":
            path.unlink()
        else:
            path.write_bytes(path.read_bytes() + b" ")
    elif capture == "minimal":
        # Hash-only artifacts cannot be replaced with retained content.
        with ReadOnlyRepository(store) as repository:
            assert (
                repository.get_artifact("001", wire.payload_sha256).relative_path
                is None
            )
        return
    else:
        key = {
            "prompt_artifact": "prompt",
            "payload_artifact": "payload",
            "format_artifact": "format_fragment",
            "schema_artifact": "native_schema",
        }[damage]
        content = wire.content_artifacts()[key]
        digest = bytes_digest(content)
        path = directory / "objects" / digest[:2] / digest
        path.write_bytes(content + b" ")
    with pytest.raises((ValueError, IntegrityError, PreparationError, ProviderError)):
        with reopen(adapter, store):
            pass
    assert adapter.submit_calls == 0


@pytest.mark.parametrize("mutation", ["modified", "missing"])
def test_minimal_original_content_required(tmp_path: Path, mutation: str) -> None:
    adapter, store, spec = prepared_case(tmp_path)
    path = spec.input.path
    if mutation == "missing":
        path.unlink()
    else:
        path.write_text('{"item_id":"001","text":"replacement"}\n', encoding="utf-8")
    with pytest.raises((ValueError, IntegrityError, PreparationError)):
        with reopen(adapter, store):
            pass


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", True),
        ("schema_version", "3"),
        ("schema_version", 3.0),
        ("unknown", PRIVATE_TEXT),
    ],
)
def test_config_version_boundary_rejects_unknown_and_coerced_tokens(
    tmp_path: Path, field: str, value: object
) -> None:
    source = specification(tmp_path)
    values = json.loads(source.read_text())
    values[field] = value
    with pytest.raises(ValueError):
        validate_run_spec_json(json.dumps(values))


@pytest.mark.parametrize("duplicate", ["identity", "selection", "commitment_sha256"])
def test_transport_json_duplicate_keys_are_rejected_without_disclosure(
    tmp_path: Path, duplicate: str
) -> None:
    adapter, store, _ = prepared_case(tmp_path)
    with reopen(adapter, store) as prepared:
        receipt = prepared.transport_for_attempt(
            next(prepared.repository.iter_attempts("001")).attempt_id
        )
    raw = receipt.model_dump_json().encode()
    if duplicate == "commitment_sha256":
        bad = raw[:-1] + b',"commitment_sha256":"' + PRIVATE_TEXT.encode() + b'"}'
    else:
        key = b'"identity":{' if duplicate == "identity" else b'"scope":{'
        field = b"run_id" if duplicate == "identity" else b"model"
        bad = raw.replace(
            key, key + b'"' + field + b'":"' + PRIVATE_TEXT.encode() + b'",', 1
        )
    with pytest.raises(ValueError) as failure:
        TransportCommitmentV1.model_validate_json(bad)
    assert PRIVATE_TEXT not in str(failure.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("transient", [False, True])
async def test_submission_failure_preserves_family_and_distinct_physical_payloads(
    tmp_path: Path, transient: bool
) -> None:
    from judgmentllm.execution.retry import RetryPolicy

    scenario = (
        MockScenario(transient_failures=1, retry_after=0)
        if transient
        else MockScenario(transport="not_submitted")
    )
    adapter, store, _ = prepared_case(tmp_path, scenario=scenario)
    report = await execute_run(
        "001", store=store, adapter=adapter, retry_policy=RetryPolicy(initial_backoff=0)
    )
    assert report.summary.successful_results == (2 if transient else 0)
    assert adapter.submit_calls == (2 if transient else 1)
    assert adapter.ledger.acceptance_count() == int(transient)
    with reopen(adapter, store) as prepared:
        attempts = tuple(prepared.repository.iter_attempts("001"))
        assert len(attempts) == (2 if transient else 1)
        assert len({attempt.payload_sha256 for attempt in attempts}) == len(attempts)
        assert {
            prepared.transport_for_attempt(attempt.attempt_id).mechanism
            for attempt in attempts
        } == {MECHANISMS[0]}


@pytest.mark.asyncio
@pytest.mark.parametrize("explanation", ["", " \t\n"])
async def test_native_does_not_exempt_nonblank_local_explanation_validation(
    tmp_path: Path, explanation: str
) -> None:
    adapter, store, _ = prepared_case(
        tmp_path, scenario=MockScenario(explanation=explanation)
    )
    report = await execute_run("001", store=store, adapter=adapter)
    assert report.summary.successful_results == 0
    assert report.summary.unsuccessful_results == 2
    await recover_run("001", store=store, adapter=adapter)
    assert adapter.submit_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mechanism", [MECHANISMS[0], MECHANISMS[3]])
async def test_auto_text_parent_subset_cannot_promote_to_native(
    tmp_path: Path, mechanism: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import judgmentllm.transport as transport

    source = specification(tmp_path / "inputs", mechanism, policy="auto")
    spec = load_run_spec(source)
    store = tmp_path / "runtime"
    adapter = NativeFixtureAdapter(
        store / "synthetic-service.sqlite3",
        mechanism,
        context=context_for(mechanism, supported=False),
        scenario=MockScenario(
            members={"02": MockMemberScenario(outcome="invalid_category")}
        ),
        retry_valid=True,
    )
    with prepare_run(
        source,
        store=store,
        run_id="001",
        capabilities=adapter.capabilities(spec),
        transport_context=adapter.transport_context,
    ):
        pass
    await execute_run("001", store=store, adapter=adapter)
    with ReadOnlyRepository(store) as repository:
        failed = next(
            result
            for result in repository.iter_results("001")
            if result.status == "invalid"
        )

    def no_projection(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("text-selected retry cannot gain native transport")

    monkeypatch.setattr(transport, "_project", no_projection)
    authorize_subset_retry(
        "001",
        store=store,
        adapter=adapter,
        task_ids=(failed.task_id,),
        operation_id="text-subset",
    )
    report = await execute_run("001", store=store, adapter=adapter)
    assert report.summary.successful_results == 2
    with reopen(adapter, store) as prepared:
        assert {
            prepared.transport_for_attempt(attempt.attempt_id).mechanism
            for attempt in prepared.repository.iter_attempts("001")
        } == {"text"}
    assert adapter.ledger.acceptance_count() == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad",
    [
        {"winner_id": "unrequested"},
        {"winner_id": None, "tie": False},
        {"winner_id": "001", "tie": True},
    ],
)
async def test_native_pairwise_membership_and_tie_relation_remain_local(
    tmp_path: Path, bad: dict[str, Any]
) -> None:
    adapter, store, _ = prepared_case(
        tmp_path, pairwise=True, ties=True, explanations=False
    )

    def invalid_pair(_raw: bytes) -> bytes:
        return json.dumps(bad).encode()

    adapter.transform = invalid_pair
    report = await execute_run("001", store=store, adapter=adapter)
    assert (
        report.summary.successful_results == 0
        and report.summary.unsuccessful_results == 1
    )
    assert adapter.submit_calls == adapter.ledger.acceptance_count() == 1


@pytest.mark.asyncio
async def test_conflicting_observation_cannot_be_accepted_after_reopening(
    tmp_path: Path,
) -> None:
    adapter, store, _ = prepared_case(tmp_path)
    adapter.retrieval_available = False
    report = await execute_run("001", store=store, adapter=adapter)
    assert report.summary.results == 0 and adapter.ledger.acceptance_count() == 1
    adapter.retrieval_available = True

    def conflicting_bytes(raw: bytes) -> bytes:
        return raw + b" "

    adapter.transform = conflicting_bytes
    with pytest.raises(ExceptionGroup) as failure:
        await recover_run("001", store=store, adapter=adapter)
    assert all(
        isinstance(error, ProviderIntegrityError) for error in failure.value.exceptions
    )
    assert adapter.submit_calls == 1 and adapter.ledger.acceptance_count() == 1
    with ReadOnlyRepository(store) as repository:
        assert len(tuple(repository.iter_results("001"))) == 0
