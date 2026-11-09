"""Explicit qualification authorization, real SDK bytes and durable lifecycle."""

from __future__ import annotations

import asyncio
import json
import socket
import sqlite3
import subprocess
import sys
from collections.abc import Generator
from contextlib import closing
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, TypedDict, cast

import pytest
from fixture_artifacts import snapshot

from judgmentllm import anthropic_qualification as q
from judgmentllm.persistence.hashing import bytes_digest, canonical_bytes

# pyright: reportPrivateUsage=false


class _Authorization(TypedDict):
    authorized_plan_sha256: str
    authorized_source_commit: str
    accept_billable_calls: bool


def pricing() -> q._PricingProfile:
    return q._PricingProfile(
        model="synthetic-model",
        endpoints=("messages",),
        reviewed_on=date(2026, 11, 7),
        context_tokens=100_000,
        maximum_output_tokens=1_000,
        input_usd_per_million=Decimal("0.1"),
        output_usd_per_million=Decimal("0.4"),
        documentation_urls=(
            "https://platform.claude.com/docs/en/about-claude/pricing",
        ),
        output_bound_includes_reasoning=True,
    )


def _build_plan(directory: Path) -> q._QualificationPlan:
    return q.build_plan(
        directory=directory,
        source_commit="a" * 40,
        provider="anthropic",
        endpoints=("messages",),
        model="synthetic-model",
        pricing=pricing(),
        max_requests=4,
        max_output_tokens=256,
        max_total_tokens=802_048,
        max_estimated_usd=Decimal("1"),
    )


@pytest.fixture
def planned(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, q._QualificationPlan]:
    monkeypatch.setattr(q, "_source_commit", lambda: "a" * 40)
    directory = tmp_path_factory.mktemp("oq") / "plan"
    return directory, _build_plan(directory)


@pytest.fixture(scope="module")
def unsubmitted_plan(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[Path, q._QualificationPlan]:
    # The complete four-case builder still runs under its original offline guard.
    # Only tests proven to leave every file/model unchanged share this artifact.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(q, "_source_commit", lambda: "a" * 40)
        directory = tmp_path_factory.mktemp("oq") / "plan"
        return directory, _build_plan(directory)


@pytest.fixture
def readonly_plan(
    unsubmitted_plan: tuple[Path, q._QualificationPlan], monkeypatch: pytest.MonkeyPatch
) -> Generator[tuple[Path, q._QualificationPlan]]:
    monkeypatch.setattr(q, "_source_commit", lambda: "a" * 40)
    directory, plan = unsubmitted_plan
    before, model = snapshot(directory), canonical_bytes(plan)
    try:
        yield directory, plan
    finally:
        assert snapshot(directory) == before
        assert canonical_bytes(plan) == model


def test_plan_is_offline_exact_and_has_no_execution_authority(
    readonly_plan: tuple[Path, q._QualificationPlan],
) -> None:
    directory, plan = readonly_plan
    assert len(plan.calls) == 4
    assert not (directory / "qualification.sqlite3").exists()
    assert not (directory / "execution-authorization.json").exists()
    assert plan.synthetic_items == q._SYNTHETIC_ITEMS
    assert plan.api_version == "2026-10-21"
    assert plan.reasoning_retention == "none"
    for call in plan.calls:
        body = json.loads(call.request_body_utf8)
        assert "store" not in body
        assert body["service_tier"] == "standard_only"
        assert body["stream"] is False
        assert "api_key" not in body
        assert call.payload_sha256 == bytes_digest(call.request_body_utf8.encode())
    report = q.report_plan(directory)
    assert report["physical_send_attempts"] == 0
    assert report["matrix_execution_complete"] is False
    assert report["all_assessments_valid"] is False
    assert report["valid_assessment_count"] == report["invalid_assessment_count"] == 0
    assert all(
        row["case_status"] == "not_attempted"
        for row in cast(list[dict[str, object]], report["calls"])
    )
    assert report["independent_review_required"] is True


@pytest.mark.parametrize("changed", ["source", "digest", "consent"])
@pytest.mark.asyncio
async def test_execute_requires_exact_explicit_authorization(
    readonly_plan: tuple[Path, q._QualificationPlan], changed: str
) -> None:
    directory, plan = readonly_plan
    with pytest.raises(q.QualificationError):
        await q.execute_plan(
            directory,
            authorized_plan_sha256="b" * 64
            if changed == "digest"
            else bytes_digest(canonical_bytes(plan)),
            authorized_source_commit="b" * 40
            if changed == "source"
            else plan.source_commit,
            accept_billable_calls=changed != "consent",
        )
    assert not (directory / "qualification.sqlite3").exists()


@pytest.mark.parametrize(
    "changed", ["count", "tokens", "spend", "model", "scope", "url"]
)
def test_unreviewed_or_excessive_plan_fails_before_materializing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: str
) -> None:
    monkeypatch.setattr(q, "_source_commit", lambda: "a" * 40)
    profile = pricing()
    if changed == "url":
        with pytest.raises(ValueError):
            q._PricingProfile.model_validate(
                dict(profile)
                | {"documentation_urls": ("https://attacker.invalid/pricing",)}
            )
        return
    directory = tmp_path / "evidence"
    with pytest.raises(q.QualificationError):
        q.build_plan(
            directory=directory,
            source_commit="a" * 40,
            provider="anthropic",
            endpoints=() if changed == "scope" else ("messages",),
            model="different-model" if changed == "model" else "synthetic-model",
            pricing=profile,
            max_requests=5 if changed == "count" else 4,
            max_output_tokens=256,
            max_total_tokens=1 if changed == "tokens" else 802_048,
            max_estimated_usd=Decimal("0.0001") if changed == "spend" else Decimal("1"),
        )
    assert not directory.exists()


def test_readonly_report_and_duplicate_json_rejection(
    planned: tuple[Path, q._QualificationPlan],
) -> None:
    directory, _ = planned

    def snapshot() -> dict[str, bytes]:
        return {
            str(p.relative_to(directory)): p.read_bytes()
            for p in directory.rglob("*")
            if p.is_file()
        }

    before = snapshot()
    q.report_plan(directory)
    assert snapshot() == before
    plan_path = directory / "plan.json"
    original = plan_path.read_bytes()
    plan_path.write_bytes(original[:-1] + b',"provider":"anthropic"}')
    with pytest.raises(ValueError):
        q.report_plan(directory)


def _forbidden_network(*_args: object, **_kwargs: object) -> Any:
    raise AssertionError("External network is forbidden")


async def _forbidden_async_network(*_args: object, **_kwargs: object) -> Any:
    raise AssertionError("External network is forbidden")


def _install_transport(
    monkeypatch: pytest.MonkeyPatch,
    plan: q._QualificationPlan,
    *,
    timeout: bool = False,
    rejection_status: int | None = None,
    reject_case_id: str | None = None,
    connection_drop: bool = False,
    cancel_after_forward: bool = False,
    provider_failure: bool = False,
    completion_status: str | None = None,
    assessment_output: str | None = None,
    cache_counters: dict[str, int] | None = None,
) -> list[bytes]:
    import httpx2 as httpx

    import judgmentllm.providers.anthropic as provider

    sent: list[bytes] = []
    expected = {call.payload_sha256: call for call in plan.calls}

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.content)
        case = expected[bytes_digest(request.content)]
        assert request.content == case.request_body_utf8.encode()
        if cancel_after_forward:
            raise asyncio.CancelledError
        if timeout:
            raise httpx.ReadTimeout("synthetic timeout", request=request)
        if connection_drop:
            raise httpx.ReadError("PRIVATE_CONNECTION_DROP_CANARY", request=request)
        if rejection_status is not None and (
            reject_case_id is None or case.case_id == reject_case_id
        ):
            return httpx.Response(
                rejection_status,
                json={"error": {"message": "PRIVATE_REJECTION_BODY_CANARY"}},
            )
        output = json.dumps(
            {"scores": [{"trait_id": "01", "category_label": "high"}]}
            if case.task_kind == "rubric"
            else {"winner_id": "001"}
        )
        if assessment_output is not None:
            output = assessment_output
        body: dict[str, Any] = {
            "id": "msg_synthetic",
            "type": "message",
            "role": "assistant",
            "model": "synthetic-reported-model",
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "content": [{"type": "text", "text": output}],
            "usage": {"input_tokens": 100, "output_tokens": 10},
        }
        if cache_counters is not None:
            body["usage"].update(cache_counters)
        if completion_status is not None:
            body["stop_reason"] = (
                "refusal" if completion_status == "refusal" else "max_tokens"
            )
        if provider_failure:
            body["stop_reason"] = "unknown"
        return httpx.Response(200, json=body)

    original = provider.AnthropicAdapter

    def factory(*args: Any, **kwargs: Any) -> provider.AnthropicAdapter:
        return original(*args, **kwargs, http_transport=httpx.MockTransport(handler))

    monkeypatch.setattr(provider, "AnthropicAdapter", factory)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-qualification-credential")

    # Proactor wakeups inspect existing sockets with isinstance(). Keep the
    # socket class intact and reject every connection path before OS networking.
    monkeypatch.setattr(socket, "getaddrinfo", _forbidden_network)
    monkeypatch.setattr(socket, "create_connection", _forbidden_network)
    monkeypatch.setattr(socket.socket, "connect", _forbidden_network)
    monkeypatch.setattr(socket.socket, "connect_ex", _forbidden_network)
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "create_connection", _forbidden_async_network)
    monkeypatch.setattr(loop, "sock_connect", _forbidden_async_network)
    return sent


def _readonly_report(
    directory: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, object]:
    import judgmentllm.providers.anthropic as provider

    def snapshot() -> dict[str, bytes]:
        return {
            str(path.relative_to(directory)): path.read_bytes()
            for path in directory.rglob("*")
            if path.is_file()
        }

    before = snapshot()

    def forbidden(*_args: object, **_kwargs: object) -> Any:
        raise AssertionError(
            "Report must not construct a client or discover credentials"
        )

    with monkeypatch.context() as guard:
        guard.setattr(provider, "AnthropicAdapter", forbidden)
        guard.setattr(provider, "_resolve_credentials", forbidden)
        report = q.report_plan(directory)
    assert snapshot() == before
    return report


@pytest.mark.asyncio
async def test_offline_guard_preserves_sockets_and_denies_connections(
    planned: tuple[Path, q._QualificationPlan], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, plan = planned
    socket_class = socket.socket
    with socket_class() as connection:
        sent = _install_transport(monkeypatch, plan)
        assert socket.socket is socket_class
        assert isinstance(connection, socket.socket)
        # Pin guard identity before each canary so a regression cannot reach OS
        # connection or name-resolution code, even for a numeric address.
        assert socket.getaddrinfo is _forbidden_network
        with pytest.raises(AssertionError, match="External network is forbidden"):
            socket.getaddrinfo("192.0.2.1", 443)
        assert socket.create_connection is _forbidden_network
        with pytest.raises(AssertionError, match="External network is forbidden"):
            socket.create_connection(("192.0.2.1", 443))
        assert socket.socket.connect is _forbidden_network
        with pytest.raises(AssertionError, match="External network is forbidden"):
            connection.connect(("192.0.2.1", 443))
        assert socket.socket.connect_ex is _forbidden_network
        with pytest.raises(AssertionError, match="External network is forbidden"):
            connection.connect_ex(("192.0.2.1", 443))
        loop = asyncio.get_running_loop()
        assert loop.create_connection is _forbidden_async_network
        with pytest.raises(AssertionError, match="External network is forbidden"):
            await loop.create_connection(asyncio.Protocol, "192.0.2.1", 443)
        assert loop.sock_connect is _forbidden_async_network
        with pytest.raises(AssertionError, match="External network is forbidden"):
            await loop.sock_connect(connection, ("192.0.2.1", 443))
        # Existing event-loop self-pipe sockets must continue waking the loop.
        assert await asyncio.wait_for(asyncio.to_thread(lambda: "awake"), 10) == "awake"
        assert not sent


@pytest.mark.asyncio
async def test_four_call_matrix_real_sdk_and_safe_continuation(
    planned: tuple[Path, q._QualificationPlan], monkeypatch: pytest.MonkeyPatch
) -> None:
    directory, plan = planned
    sent = _install_transport(monkeypatch, plan)
    arguments = _Authorization(
        authorized_plan_sha256=bytes_digest(canonical_bytes(plan)),
        authorized_source_commit=plan.source_commit,
        accept_billable_calls=True,
    )
    report = await q.execute_plan(directory, **arguments)
    assert len(sent) == 4
    assert report["physical_send_attempts"] == 4
    assert report["matrix_execution_complete"] is True
    assert report["all_assessments_valid"] is True
    assert report["valid_assessment_count"] == 4
    assert report["invalid_assessment_count"] == 0
    assert report["reported_usage_totals"] == {
        "input_tokens": 400,
        "output_tokens": 40,
        "total_tokens": None,
    }
    calls = cast(list[dict[str, object]], report["calls"])
    assert all(row["local_assessment_valid"] is True for row in calls)
    assert all(
        row["case_status"] == "provider_completed_assessment_valid" for row in calls
    )
    assert all(row["durable_evidence_consistent"] is True for row in calls)
    assert all(row["live_qualified"] is False for row in calls)
    assert all(row["provider_accepted"] is True for row in calls)
    assert all(row["acceptance_outcome"] == "accepted" for row in calls)
    assert all(row["provider_response_observed"] is True for row in calls)
    assert all(row["submission_certainty"] == "accepted" for row in calls)
    assert all(
        row["provider_outcome"] == row["completion_status"] == "completed"
        for row in calls
    )
    assert all(
        row["invocation_failure_code"] is row["invocation_acceptance"] is None
        for row in calls
    )
    assert "synthetic-qualification-credential" not in json.dumps(report)
    assert "resp_synthetic" not in json.dumps(report)
    assert "msg_synthetic" not in json.dumps(report)
    assert _readonly_report(directory, monkeypatch)["calls"] == report["calls"]
    again = await q.execute_plan(directory, **arguments)
    assert len(sent) == 4
    assert again["physical_send_attempts"] == 4
    # Deleting the ledger must not recreate an empty budget.
    (directory / "qualification.sqlite3").unlink()
    with pytest.raises(q.QualificationError):
        await q.execute_plan(directory, **arguments)
    assert len(sent) == 4


@pytest.mark.asyncio
async def test_timeout_consumes_allocation_without_replacement(
    planned: tuple[Path, q._QualificationPlan], monkeypatch: pytest.MonkeyPatch
) -> None:
    directory, plan = planned
    sent = _install_transport(monkeypatch, plan, timeout=True)
    arguments = _Authorization(
        authorized_plan_sha256=bytes_digest(canonical_bytes(plan)),
        authorized_source_commit=plan.source_commit,
        accept_billable_calls=True,
    )
    report = await q.execute_plan(directory, **arguments)
    assert len(sent) == 1
    assert report["physical_send_attempts"] == 1
    first = cast(list[dict[str, object]], report["calls"])[0]
    assert first["provider_accepted"] is None
    assert first["acceptance_outcome"] == "unresolved"
    assert first["case_status"] == "submitted_acceptance_unresolved"
    assert report["matrix_execution_complete"] is False
    assert report["all_assessments_valid"] is False
    assert first["provider_response_observed"] is False
    assert first["submission_certainty"] == "unknown"
    assert first["invocation_acceptance"] == "unresolved"
    assert first["completion_status"] == "not_observed"
    assert first["provider_outcome"] is first["invocation_failure_code"] is None
    assert _readonly_report(directory, monkeypatch)["calls"] == report["calls"]
    again = await q.execute_plan(directory, **arguments)
    assert len(sent) == 1
    assert again["physical_send_attempts"] == 1
    with closing(sqlite3.connect(directory / "qualification.sqlite3")) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM calls WHERE consumed_at IS NOT NULL"
            ).fetchone()[0]
            == 1
        )


@pytest.mark.asyncio
async def test_changed_synthetic_sources_or_configuration_never_submit(
    planned: tuple[Path, q._QualificationPlan], monkeypatch: pytest.MonkeyPatch
) -> None:
    directory, plan = planned
    sent = _install_transport(monkeypatch, plan)
    source = directory / "responses.jsonl"
    original = source.read_bytes()
    source.write_bytes(b'{"item_id":"001","text":"Unapproved input"}\n')
    arguments = _Authorization(
        authorized_plan_sha256=bytes_digest(canonical_bytes(plan)),
        authorized_source_commit=plan.source_commit,
        accept_billable_calls=True,
    )
    with pytest.raises(q.QualificationError):
        await q.execute_plan(directory, **arguments)
    assert not sent
    assert not (directory / "qualification.sqlite3").exists()
    source.write_bytes(original)
    config = directory / (plan.calls[0].case_id + ".json")
    content = json.loads(config.read_bytes())
    content["capture"] = "minimal"
    config.write_text(json.dumps(content), encoding="utf-8")
    with pytest.raises(q.QualificationError):
        await q.execute_plan(directory, **arguments)
    assert not sent


def test_caller_cannot_create_private_guard_without_executor_token(
    readonly_plan: tuple[Path, q._QualificationPlan],
) -> None:
    directory, plan = readonly_plan
    with pytest.raises(q.QualificationError):
        q._QualificationSubmissionGuard(directory, plan, plan.calls[0], token=object())


@pytest.mark.asyncio
async def test_ledger_mutation_cannot_replenish_a_consumed_allocation(
    planned: tuple[Path, q._QualificationPlan], monkeypatch: pytest.MonkeyPatch
) -> None:
    directory, plan = planned
    sent = _install_transport(monkeypatch, plan, timeout=True)
    arguments = _Authorization(
        authorized_plan_sha256=bytes_digest(canonical_bytes(plan)),
        authorized_source_commit=plan.source_commit,
        accept_billable_calls=True,
    )
    await q.execute_plan(directory, **arguments)
    with closing(sqlite3.connect(directory / "qualification.sqlite3")) as connection:
        connection.execute("UPDATE calls SET consumed_at=NULL")
        connection.commit()
    # An independent append-only forwarding intent conservatively preserves the
    # spent allocation even if an otherwise well-formed SQLite row is reset.
    assert q.report_plan(directory)["physical_send_attempts"] == 1
    await q.execute_plan(directory, **arguments)
    assert len(sent) == 1


@pytest.mark.parametrize(
    "status,code", [(401, "credentials_rejected"), (429, "rate_limited")]
)
@pytest.mark.asyncio
async def test_report_distinguishes_forwarded_rejection_and_remains_readonly(
    planned: tuple[Path, q._QualificationPlan],
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    code: str,
) -> None:
    import judgmentllm.providers.anthropic as provider

    directory, plan = planned
    sent = _install_transport(monkeypatch, plan, rejection_status=status)
    report = await q.execute_plan(
        directory,
        authorized_plan_sha256=bytes_digest(canonical_bytes(plan)),
        authorized_source_commit=plan.source_commit,
        accept_billable_calls=True,
    )
    assert len(sent) == report["physical_send_attempts"] == 1
    first = cast(list[dict[str, object]], report["calls"])[0]
    assert first["physical_send_attempted"] is True
    assert first["provider_accepted"] is False
    assert first["acceptance_outcome"] == "rejected"
    assert first["case_status"] == "provider_rejected_request"
    assert first["invocation_failure_code"] == code
    assert first["submission_certainty"] == "not_submitted"
    assert first["invocation_acceptance"] is None
    assert first["provider_outcome"] is None
    assert first["provider_response_observed"] is False
    assert first["completion_status"] == "not_observed"
    assert first["assessment_behavior_verified"] is False
    pending = cast(list[dict[str, object]], report["calls"])[1]
    assert pending["provider_accepted"] is None
    assert pending["acceptance_outcome"] == "not_attempted"
    assert pending["submission_certainty"] == "not_started"
    assert pending["completion_status"] == "not_attempted"
    assert "PRIVATE_REJECTION_BODY_CANARY" not in json.dumps(report)

    def snapshot() -> dict[str, bytes]:
        return {
            str(path.relative_to(directory)): path.read_bytes()
            for path in directory.rglob("*")
            if path.is_file()
        }

    before = snapshot()

    def forbidden(*_args: object, **_kwargs: object) -> Any:
        raise AssertionError(
            "Reporting must not discover credentials or construct clients"
        )

    monkeypatch.setattr(provider, "AnthropicAdapter", forbidden)
    monkeypatch.setattr(provider, "_resolve_credentials", forbidden)
    reread = q.report_plan(directory)
    assert snapshot() == before
    assert reread["calls"] == report["calls"]
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_failed_provider_observation_is_not_assessment_acceptance(
    planned: tuple[Path, q._QualificationPlan],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory, plan = planned
    sent = _install_transport(monkeypatch, plan, provider_failure=True)
    report = await q.execute_plan(
        directory,
        authorized_plan_sha256=bytes_digest(canonical_bytes(plan)),
        authorized_source_commit=plan.source_commit,
        accept_billable_calls=True,
    )
    first = cast(list[dict[str, object]], report["calls"])[0]
    assert len(sent) == 1
    assert first["submission_certainty"] == "accepted"
    assert first["provider_response_observed"] is True
    assert (
        first["provider_outcome"] == first["completion_status"] == "unsupported_output"
    )
    assert first["provider_accepted"] is True
    assert first["acceptance_outcome"] == "accepted"
    assert first["case_status"] == "provider_accepted_no_usable_observation"
    assert first["assessment_behavior_verified"] is False
    assert "PRIVATE_PROVIDER_FAILURE_CANARY" not in json.dumps(report)


@pytest.mark.parametrize("status", ["refusal", "output_limit"])
@pytest.mark.asyncio
async def test_report_separates_request_acceptance_from_unsuccessful_completion(
    planned: tuple[Path, q._QualificationPlan],
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    directory, plan = planned
    sent = _install_transport(monkeypatch, plan, completion_status=status)
    report = await q.execute_plan(
        directory,
        authorized_plan_sha256=bytes_digest(canonical_bytes(plan)),
        authorized_source_commit=plan.source_commit,
        accept_billable_calls=True,
    )
    first = cast(list[dict[str, object]], report["calls"])[0]
    assert len(sent) == 1
    assert first["provider_accepted"] is True
    assert first["acceptance_outcome"] == "accepted"
    assert first["provider_response_observed"] is True
    assert first["provider_outcome"] == first["completion_status"] == status
    assert first["case_status"] == (
        "provider_refused" if status == "refusal" else "provider_truncated_output"
    )
    assert first["assessment_behavior_verified"] is False
    assert first["local_assessment_valid"] is False
    assert "PRIVATE_REFUSAL_BODY_CANARY" not in json.dumps(report)


@pytest.mark.parametrize(
    "output",
    [
        "{",
        '{"scores":[],"scores":[]}',
        "{}",
        '{"scores":[{"trait_id":"01","category_label":"unknown"}]}',
    ],
)
@pytest.mark.asyncio
async def test_completed_terminal_invalid_assessments_continue_without_replacement(
    planned: tuple[Path, q._QualificationPlan],
    monkeypatch: pytest.MonkeyPatch,
    output: str,
) -> None:
    directory, plan = planned
    sent = _install_transport(monkeypatch, plan, assessment_output=output)
    arguments = _Authorization(
        authorized_plan_sha256=bytes_digest(canonical_bytes(plan)),
        authorized_source_commit=plan.source_commit,
        accept_billable_calls=True,
    )
    report = await q.execute_plan(directory, **arguments)
    calls = cast(list[dict[str, object]], report["calls"])
    assert len(sent) == report["physical_send_attempts"] == 4
    assert report["matrix_execution_complete"] is True
    assert report["all_assessments_valid"] is False
    assert report["valid_assessment_count"] == 0
    assert report["invalid_assessment_count"] == 4
    assert all(
        row["case_status"] == "provider_completed_assessment_invalid" for row in calls
    )
    assert all(row["outcome"] == "partial" for row in calls)
    assert all(row["normalized_outcomes"] == ["invalid"] for row in calls)
    assert all(row["assessment_terminal"] is True for row in calls)
    assert all(row["assessment_continuation_allowed"] is True for row in calls)
    assert all(row["http_response_received"] is True for row in calls)
    assert all(row["durable_evidence_consistent"] is True for row in calls)
    assert all(row["assessment_diagnostic_available"] is True for row in calls)
    assert all(row["canonical_valid"] is False for row in calls)
    assert all(
        row["assessment_sha256"] == bytes_digest(output.encode()) for row in calls
    )
    assert all(row["assessment_byte_length"] == len(output.encode()) for row in calls)
    assert all(row["assessment_retention"] == "retained" for row in calls)
    assert all(row["recovery_required"] is False for row in calls)
    assert all(row["assessment_behavior_verified"] is False for row in calls)
    assert all(row["live_qualified"] is False for row in calls)
    assert _readonly_report(directory, monkeypatch)["calls"] == calls
    # A separate continuation inspects the same invalid results, never replacing
    # any of the eight consumed physical submission identities.
    assert (await q.execute_plan(directory, **arguments))["calls"] == calls
    assert len(sent) == 4


def test_new_plan_identities_are_source_bound_and_legacy_plan_remains_readable(
    planned: tuple[Path, q._QualificationPlan],
    tmp_path: Path,
) -> None:
    from judgmentllm.transport import physical_identity

    _, plan = planned
    assert plan.plan_version == 2
    legacy_calls: list[q._PlannedCall] = []
    for call in plan.calls:
        assert call.run_id == f"qualification-{plan.source_commit}-{call.case_id}"
        run_id = "qualification-" + call.case_id
        attempt_id = q.stable_id("attempt", run_id, call.request_uid, 1)
        identity = physical_identity(run_id, call.request_uid, attempt_id)
        legacy_calls.append(
            q._PlannedCall.model_validate(
                dict(call)
                | {
                    "run_id": run_id,
                    "attempt_id": attempt_id,
                    "submission_id": identity.submission_id,
                    "correlation_id": identity.correlation_id,
                }
            )
        )
    legacy = q._QualificationPlan.model_validate(
        dict(plan) | {"plan_version": 1, "calls": tuple(legacy_calls)}
    )
    assert not (
        {c.submission_id for c in legacy.calls} & {c.submission_id for c in plan.calls}
    )
    # Historical bytes retain their version and exact canonical digest.
    directory = tmp_path / "old"
    directory.mkdir()
    original = canonical_bytes(legacy)
    (directory / "plan.json").write_bytes(original)
    loaded, digest = q._load_plan(directory)
    assert loaded.plan_version == 1
    assert canonical_bytes(loaded) == original
    assert digest == bytes_digest(original)
    with pytest.raises(ValueError):
        q._QualificationPlan.model_validate(dict(legacy) | {"plan_version": 2})
    with pytest.raises(ValueError):
        q._QualificationPlan.model_validate(dict(plan) | {"source_commit": "b" * 40})


@pytest.mark.parametrize(
    "missing", ["receipt", "results", "task", "observation", "diagnostic"]
)
@pytest.mark.asyncio
async def test_partial_or_missing_durable_evidence_cannot_allow_continuation(
    planned: tuple[Path, q._QualificationPlan],
    monkeypatch: pytest.MonkeyPatch,
    missing: str,
) -> None:
    from judgmentllm.persistence.repository import ReadOnlyRepository

    directory, plan = planned
    sent = _install_transport(monkeypatch, plan, assessment_output="{}")

    # Model a positive HTTP observation whose local interpretation/commit is
    # incomplete. The report must require every independent durable component.
    def empty_records(*_args: Any, **_kwargs: Any) -> Any:
        return iter(())

    if missing == "receipt":
        monkeypatch.setattr(ReadOnlyRepository, "iter_evidence", empty_records)
    elif missing == "results":
        monkeypatch.setattr(ReadOnlyRepository, "iter_results", empty_records)
    elif missing == "task":
        original = ReadOnlyRepository.get_task

        def unfinished_task(self: Any, *args: Any, **kwargs: Any) -> Any:
            return original(self, *args, **kwargs).model_copy(
                update={"state": "active"}
            )

        monkeypatch.setattr(ReadOnlyRepository, "get_task", unfinished_task)
    else:
        original_receipts = ReadOnlyRepository.iter_evidence

        def no_observation(self: Any, *args: Any, **kwargs: Any) -> Any:
            for receipt in original_receipts(self, *args, **kwargs):
                if missing == "diagnostic":
                    yield receipt.model_copy(update={"assessment_diagnostic": None})
                else:
                    yield receipt.model_copy(
                        update={
                            "identity": receipt.identity.model_copy(
                                update={"observation_id": None}
                            )
                        }
                    )

        monkeypatch.setattr(ReadOnlyRepository, "iter_evidence", no_observation)
    arguments = _Authorization(
        authorized_plan_sha256=bytes_digest(canonical_bytes(plan)),
        authorized_source_commit=plan.source_commit,
        accept_billable_calls=True,
    )
    report = await q.execute_plan(directory, **arguments)
    assert len(sent) == 1
    first = cast(list[dict[str, object]], report["calls"])[0]
    assert first["assessment_continuation_allowed"] is False
    assert first["assessment_behavior_verified"] is False


def test_changed_source_builds_disjoint_new_physical_submission_identities(
    planned: tuple[Path, q._QualificationPlan],
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, original = planned
    monkeypatch.setattr(q, "_source_commit", lambda: "b" * 40)
    changed = q.build_plan(
        directory=tmp_path_factory.mktemp("oqn") / "new",
        source_commit="b" * 40,
        provider="anthropic",
        endpoints=original.endpoints,
        model=original.model,
        pricing=original.pricing,
        max_requests=original.max_requests,
        max_output_tokens=original.max_output_tokens,
        max_total_tokens=original.max_total_tokens,
        max_estimated_usd=original.max_estimated_usd,
    )
    assert len(changed.calls) == 4
    assert {call.submission_id for call in changed.calls}.isdisjoint(
        call.submission_id for call in original.calls
    )
    assert {call.attempt_id for call in changed.calls}.isdisjoint(
        call.attempt_id for call in original.calls
    )
    assert {call.correlation_id for call in changed.calls}.isdisjoint(
        call.correlation_id for call in original.calls
    )
    # Source-scoped execution identities change even when the synthetic provider
    # request bodies deliberately exercise the identical eight API operations.
    assert [call.payload_sha256 for call in changed.calls] == [
        call.payload_sha256 for call in original.calls
    ]


@pytest.mark.parametrize("damage", ["corrupt", "missing"])
@pytest.mark.asyncio
async def test_corrupt_or_missing_committed_receipt_stops_and_reports_readonly(
    planned: tuple[Path, q._QualificationPlan],
    monkeypatch: pytest.MonkeyPatch,
    damage: str,
) -> None:
    import judgmentllm.lifecycle as lifecycle
    from judgmentllm.persistence.artifacts import ArtifactStore
    from judgmentllm.persistence.evidence import _reference
    from judgmentllm.persistence.repository import ReadOnlyRepository

    directory, plan = planned
    sent = _install_transport(monkeypatch, plan)
    first = plan.calls[0]
    original_export = lifecycle.export_run_results

    def damage_evidence(*args: Any, **kwargs: Any) -> Any:
        result = original_export(*args, **kwargs)
        store = directory / "execution" / first.case_id
        with ReadOnlyRepository(store) as repository:
            artifacts = ArtifactStore(
                repository.run_directory(first.run_id), create=False
            )
            reference = artifacts.reference_path(_reference(first.submission_id))
        if damage == "missing":
            reference.unlink()
        else:
            reference.write_bytes(b"PRIVATE_CORRUPT_RECEIPT_CANARY")
        return result

    monkeypatch.setattr(lifecycle, "export_run_results", damage_evidence)
    arguments = _Authorization(
        authorized_plan_sha256=bytes_digest(canonical_bytes(plan)),
        authorized_source_commit=plan.source_commit,
        accept_billable_calls=True,
    )
    report = await q.execute_plan(directory, **arguments)
    assert len(sent) == report["physical_send_attempts"] == 1
    row = cast(list[dict[str, object]], report["calls"])[0]
    assert row["evidence_integrity_failure"] is True
    assert row["outcome"] == row["acceptance_outcome"] == "evidence_integrity_failure"
    assert row["case_status"] == "evidence_integrity_failure"
    assert report["matrix_execution_complete"] is False
    assert row["assessment_continuation_allowed"] is False
    assert row["recovery_required"] is True
    assert "PRIVATE_CORRUPT_RECEIPT_CANARY" not in json.dumps(report)
    assert _readonly_report(directory, monkeypatch)["calls"] == report["calls"]
    # A fresh adapter is never constructed to repair corrupt evidence by calling.
    assert (await q.execute_plan(directory, **arguments))["calls"] == report["calls"]
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_evidence_publication_failure_stops_without_replacing_forward(
    planned: tuple[Path, q._QualificationPlan],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import judgmentllm.execution.durable_evidence as durable

    directory, plan = planned
    sent = _install_transport(monkeypatch, plan)

    def cannot_publish(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("PRIVATE_PUBLICATION_FAILURE_CANARY")

    monkeypatch.setattr(durable, "publish_evidence_receipt", cannot_publish)
    arguments = _Authorization(
        authorized_plan_sha256=bytes_digest(canonical_bytes(plan)),
        authorized_source_commit=plan.source_commit,
        accept_billable_calls=True,
    )
    with pytest.raises(ExceptionGroup):
        await q.execute_plan(directory, **arguments)
    report = _readonly_report(directory, monkeypatch)
    assert len(sent) == report["physical_send_attempts"] == 1
    assert (
        cast(list[dict[str, object]], report["calls"])[0][
            "assessment_continuation_allowed"
        ]
        is False
    )
    assert "PRIVATE_PUBLICATION_FAILURE_CANARY" not in json.dumps(report)
    # Failed local publication never replenishes the allocation. A fresh
    # adapter cannot reconstruct the lost response by submitting again.
    resumed = await q.execute_plan(directory, **arguments)
    assert resumed["physical_send_attempts"] == 1
    assert (
        cast(list[dict[str, object]], resumed["calls"])[0][
            "assessment_continuation_allowed"
        ]
        is False
    )
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_native_rejection_stops_after_successful_text_without_fallback(
    planned: tuple[Path, q._QualificationPlan],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory, plan = planned
    native = plan.calls[1]
    assert plan.calls[0].mode == "text" and native.mode == "native"
    sent = _install_transport(
        monkeypatch, plan, rejection_status=400, reject_case_id=native.case_id
    )
    report = await q.execute_plan(
        directory,
        authorized_plan_sha256=bytes_digest(canonical_bytes(plan)),
        authorized_source_commit=plan.source_commit,
        accept_billable_calls=True,
    )
    assert len(sent) == report["physical_send_attempts"] == 2
    rows = cast(list[dict[str, object]], report["calls"])
    assert rows[0]["assessment_behavior_verified"] is True
    assert rows[1]["acceptance_outcome"] == "rejected"
    assert rows[1]["assessment_continuation_allowed"] is False
    assert all(row["physical_send_attempted"] is False for row in rows[2:])
    assert [bytes_digest(body) for body in sent] == [
        c.payload_sha256 for c in plan.calls[:2]
    ]


@pytest.mark.asyncio
async def test_connection_drop_after_forwarding_stops_and_cannot_repeat(
    planned: tuple[Path, q._QualificationPlan],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory, plan = planned
    sent = _install_transport(monkeypatch, plan, connection_drop=True)
    arguments = _Authorization(
        authorized_plan_sha256=bytes_digest(canonical_bytes(plan)),
        authorized_source_commit=plan.source_commit,
        accept_billable_calls=True,
    )
    report = await q.execute_plan(directory, **arguments)
    assert len(sent) == report["physical_send_attempts"] == 1
    row = cast(list[dict[str, object]], report["calls"])[0]
    assert row["invocation_acceptance"] == "unresolved"
    assert row["assessment_continuation_allowed"] is False
    assert "PRIVATE_CONNECTION_DROP_CANARY" not in json.dumps(report)
    await q.execute_plan(directory, **arguments)
    assert len(sent) == 1


@pytest.mark.parametrize("damage", ["deleted", "altered"])
@pytest.mark.asyncio
async def test_damaged_shared_ledger_cannot_replenish_or_continue(
    planned: tuple[Path, q._QualificationPlan],
    monkeypatch: pytest.MonkeyPatch,
    damage: str,
) -> None:
    directory, plan = planned
    sent = _install_transport(monkeypatch, plan, timeout=True)
    arguments = _Authorization(
        authorized_plan_sha256=bytes_digest(canonical_bytes(plan)),
        authorized_source_commit=plan.source_commit,
        accept_billable_calls=True,
    )
    await q.execute_plan(directory, **arguments)
    ledger = directory / "qualification.sqlite3"
    if damage == "deleted":
        ledger.unlink()
    else:
        with closing(sqlite3.connect(ledger)) as connection:
            connection.execute("UPDATE calls SET payload_sha256=?", ("b" * 64,))
            connection.commit()
    with pytest.raises(q.QualificationError):
        q.report_plan(directory)
    with pytest.raises(q.QualificationError):
        await q.execute_plan(directory, **arguments)
    assert len(sent) == 1


def test_duplicate_physical_submission_plan_is_not_admissible(
    readonly_plan: tuple[Path, q._QualificationPlan],
) -> None:
    _, plan = readonly_plan
    with pytest.raises(ValueError):
        q._QualificationPlan.model_validate(
            dict(plan) | {"calls": (plan.calls[0],) * 4}
        )


def test_terminal_invalid_harness_continues_in_fresh_process_without_resend(
    planned: tuple[Path, q._QualificationPlan],
) -> None:
    directory, _ = planned
    repository = Path(q.__file__).resolve().parents[2]
    code = r"""
import asyncio
import json
import sys
from collections.abc import Generator
from pathlib import Path
sys.path.insert(0, sys.argv[1] + "/src")
sys.path.insert(0, sys.argv[1] + "/tests")
import pytest
from fixture_artifacts import snapshot
from judgmentllm import anthropic_qualification as q
from test_anthropic_qualification import _install_transport

async def main():
    patch = pytest.MonkeyPatch()
    directory = Path(sys.argv[2])
    plan, digest = q._load_plan(directory)
    patch.setattr(q, "_source_commit", lambda: plan.source_commit)
    sent = _install_transport(patch, plan, assessment_output="{}")
    if sys.argv[3] == "interrupt":
        import judgmentllm.lifecycle as lifecycle
        original = lifecycle.export_run_results
        def stop_after_first(*args, **kwargs):
            result = original(*args, **kwargs)
            raise RuntimeError("synthetic terminal process interruption")
        patch.setattr(lifecycle, "export_run_results", stop_after_first)
    try:
        try:
            report = await q.execute_plan(
                directory, authorized_plan_sha256=digest,
                authorized_source_commit=plan.source_commit, accept_billable_calls=True,
            )
        except RuntimeError:
            if sys.argv[3] != "interrupt":
                raise
            report = q.report_plan(directory)
        print(json.dumps({"sent": len(sent), "report": report}))
    finally:
        patch.undo()
asyncio.run(main())
"""
    first = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            code,
            str(repository),
            str(directory),
            "interrupt",
        ],
        cwd=directory.parent,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    initial = json.loads(first.stdout)
    assert initial["sent"] == initial["report"]["physical_send_attempts"] == 1
    assert initial["report"]["calls"][0]["assessment_continuation_allowed"] is True
    resumed = subprocess.run(
        [sys.executable, "-B", "-c", code, str(repository), str(directory), "resume"],
        cwd=directory.parent,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    final = json.loads(resumed.stdout)
    assert final["sent"] == 3
    assert final["report"]["physical_send_attempts"] == 4
    assert all(
        row["normalized_outcomes"] == ["invalid"] for row in final["report"]["calls"]
    )
    assert all(
        row["assessment_continuation_allowed"] is True
        for row in final["report"]["calls"]
    )
    assert all(row["live_qualified"] is False for row in final["report"]["calls"])


@pytest.mark.asyncio
async def test_legacy_plan_is_report_only_and_never_creates_execution_authority(
    planned: tuple[Path, q._QualificationPlan],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import judgmentllm.providers.anthropic as provider
    from judgmentllm.transport import physical_identity

    directory, plan = planned
    calls: list[q._PlannedCall] = []
    for call in plan.calls:
        run_id = "qualification-" + call.case_id
        attempt_id = q.stable_id("attempt", run_id, call.request_uid, 1)
        identity = physical_identity(run_id, call.request_uid, attempt_id)
        calls.append(
            q._PlannedCall.model_validate(
                dict(call)
                | {
                    "run_id": run_id,
                    "attempt_id": attempt_id,
                    "submission_id": identity.submission_id,
                    "correlation_id": identity.correlation_id,
                }
            )
        )
    legacy = q._QualificationPlan.model_validate(
        dict(plan) | {"plan_version": 1, "calls": tuple(calls)}
    )
    original = canonical_bytes(legacy)
    (directory / "plan.json").write_bytes(original)
    report = _readonly_report(directory, monkeypatch)
    assert report["physical_send_attempts"] == 0

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("Historical plan must not construct an execution adapter")

    monkeypatch.setattr(provider, "AnthropicAdapter", forbidden)
    with pytest.raises(q.QualificationError):
        await q.execute_plan(
            directory,
            authorized_plan_sha256=bytes_digest(original),
            authorized_source_commit=legacy.source_commit,
            accept_billable_calls=True,
        )
    assert not (directory / "execution-authorization.json").exists()
    assert not (directory / "qualification.sqlite3").exists()
    assert (directory / "plan.json").read_bytes() == original


@pytest.mark.parametrize("window", ["before_forward", "after_forward"])
@pytest.mark.asyncio
async def test_harness_cancellation_stops_matrix_without_replacing_request(
    planned: tuple[Path, q._QualificationPlan],
    monkeypatch: pytest.MonkeyPatch,
    window: str,
) -> None:
    directory, plan = planned
    sent = _install_transport(
        monkeypatch, plan, cancel_after_forward=window == "after_forward"
    )
    original = q._QualificationSubmissionGuard.__call__
    if window == "before_forward":

        def cancel_before_allocation(*_args: Any, **_kwargs: Any) -> Any:
            raise asyncio.CancelledError

        monkeypatch.setattr(
            q._QualificationSubmissionGuard, "__call__", cancel_before_allocation
        )
    arguments = _Authorization(
        authorized_plan_sha256=bytes_digest(canonical_bytes(plan)),
        authorized_source_commit=plan.source_commit,
        accept_billable_calls=True,
    )
    with pytest.raises(asyncio.CancelledError):
        await q.execute_plan(directory, **arguments)
    monkeypatch.setattr(q._QualificationSubmissionGuard, "__call__", original)
    report = _readonly_report(directory, monkeypatch)
    expected = int(window == "after_forward")
    assert len(sent) == report["physical_send_attempts"] == expected
    rows = cast(list[dict[str, object]], report["calls"])
    assert rows[0]["assessment_continuation_allowed"] is False
    assert all(row["physical_send_attempted"] is False for row in rows[1:])
    await q.execute_plan(directory, **arguments)
    assert len(sent) == expected


@pytest.mark.parametrize("valid", [False, True])
@pytest.mark.asyncio
async def test_legacy_terminal_assessment_reporting_never_grants_continuation(
    planned: tuple[Path, q._QualificationPlan],
    monkeypatch: pytest.MonkeyPatch,
    valid: bool,
) -> None:
    import judgmentllm.providers.anthropic as provider
    from judgmentllm.anthropic_transport import production_context_for_identity
    from judgmentllm.execution import execute_run
    from judgmentllm.execution.retry import RetryPolicy
    from judgmentllm.preparation import prepare_run
    from judgmentllm.transport import physical_identity

    directory, current = planned
    calls: list[q._PlannedCall] = []
    for call in current.calls:
        run_id = "qualification-" + call.case_id
        attempt_id = q.stable_id("attempt", run_id, call.request_uid, 1)
        identity = physical_identity(run_id, call.request_uid, attempt_id)
        calls.append(
            q._PlannedCall.model_validate(
                dict(call)
                | {
                    "run_id": run_id,
                    "attempt_id": attempt_id,
                    "submission_id": identity.submission_id,
                    "correlation_id": identity.correlation_id,
                }
            )
        )
    legacy = q._QualificationPlan.model_validate(
        dict(current) | {"plan_version": 1, "calls": tuple(calls)}
    )
    raw = canonical_bytes(legacy)
    (directory / "plan.json").write_bytes(raw)
    digest = bytes_digest(raw)
    sent = _install_transport(
        monkeypatch, legacy, assessment_output=None if valid else "{}"
    )

    # Construct new synthetic old-style evidence without running the rejected
    # v1 harness. The old adapter did not supply the additive text diagnostic.
    def no_component(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(provider, "_extract_assessment_component", no_component)
    case = legacy.calls[0]
    spec = q._verify_case(directory, case)
    context = production_context_for_identity("anthropic", case.endpoint, case.model)
    store = directory / "execution" / case.case_id
    q._authorize(directory, legacy, digest)
    with prepare_run(
        spec, store=store, run_id=case.run_id, transport_context=context
    ) as prepared:
        dependency = next(prepared.iter_request_dependencies())
        identity = physical_identity(case.run_id, case.request_uid, case.attempt_id)
        wire = prepared.prepare_transport_attempt(dependency, identity)
        # This test fixture records the prior physical intent before the mocked
        # submission; this private operation is not v1 execute authorization.
        q._QualificationSubmissionGuard(
            directory, legacy, case, token=q._EXECUTOR_TOKEN
        )(wire)
    adapter = provider.AnthropicAdapter(case.endpoint, case.model, context=context)
    try:
        await execute_run(
            case.run_id,
            store=store,
            adapter=adapter,
            spec=spec,
            retry_policy=RetryPolicy(max_attempts=1),
        )
    finally:
        await adapter.aclose()
    report = _readonly_report(directory, monkeypatch)
    row = cast(list[dict[str, object]], report["calls"])[0]
    assert len(sent) == report["physical_send_attempts"] == 1
    assert row["case_status"] == (
        "provider_completed_assessment_valid"
        if valid
        else "provider_completed_assessment_invalid"
    )
    assert row["assessment_terminal"] is True
    assert row["assessment_diagnostic_available"] is False
    assert row["parser_diagnostic"] is row["canonical_valid"] is None
    assert row["local_assessment_valid"] is valid
    assert row["assessment_behavior_verified"] is valid
    assert row["assessment_continuation_allowed"] is False
    assert row["live_qualified"] is False
    assert report["valid_assessment_count"] == int(valid)
    assert report["invalid_assessment_count"] == int(not valid)
    assert report["matrix_execution_complete"] is False
    assert report["all_assessments_valid"] is False
    with pytest.raises(q.QualificationError):
        await q.execute_plan(
            directory,
            authorized_plan_sha256=digest,
            authorized_source_commit=legacy.source_commit,
            accept_billable_calls=True,
        )
    assert len(sent) == 1
    assert (directory / "plan.json").read_bytes() == raw


def test_input_allowances_account_for_complete_bodies_and_schema_repetition(
    planned: tuple[Path, q._QualificationPlan],
) -> None:
    _, plan = planned
    for call in plan.calls:
        body = json.loads(call.request_body_utf8)
        allowance, evidence = q._input_allowance(call.request_body_utf8)
        assert allowance == call.reserved_input_tokens == 8192
        assert evidence["complete_body_utf8_bytes"] == len(
            call.request_body_utf8.encode("utf-8")
        )
        assert (evidence["additional_schema_utf8_bytes"] != 0) == (
            "output_config" in body
        )
        assert evidence["exact_provider_tokenizer_verified"] is False
        assert evidence["provider_enforced_input_limit"] is False
    large = json.dumps({"messages": [{"role": "user", "content": "A" * 20000}]})
    assert q._input_allowance(large)[0] > 8192


def test_cli_plan_and_readonly_report_with_exact_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import sys

    profile = pricing()
    pricing_path = tmp_path / "pricing.json"
    pricing_path.write_bytes(canonical_bytes(profile))
    directory = tmp_path / "cli-plan"
    monkeypatch.setattr(q, "_source_commit", lambda: "a" * 40)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "qualify_anthropic.py",
            "plan",
            "--provider",
            "anthropic",
            "--endpoint",
            "messages",
            "--model",
            profile.model,
            "--source-commit",
            "a" * 40,
            "--pricing-profile",
            str(pricing_path),
            "--max-requests",
            "4",
            "--max-output-tokens",
            "256",
            "--max-total-tokens",
            "802048",
            "--max-estimated-usd",
            "1",
            "--evidence-dir",
            str(directory),
        ],
    )
    q.main()
    report = json.loads(capsys.readouterr().out)
    assert report["physical_send_attempts"] == 0 and len(report["calls"]) == 4
    before = (directory / "plan.json").read_bytes()
    monkeypatch.setattr(
        sys,
        "argv",
        ["qualify_anthropic.py", "report", "--evidence-dir", str(directory)],
    )
    q.main()
    assert json.loads(capsys.readouterr().out)["plan_sha256"] == bytes_digest(before)
    assert (directory / "plan.json").read_bytes() == before
    assert not (directory / "authorization.json").exists()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "qualify_anthropic.py",
            "execute",
            "--evidence-dir",
            str(directory),
            "--authorize-plan-sha256",
            "f" * 64,
            "--authorize-source-commit",
            "a" * 40,
            "--accept-billable-calls",
        ],
    )
    with pytest.raises(SystemExit) as error:
        q.main()
    assert error.value.code == 2
    assert "Qualification operation failed" in capsys.readouterr().err
    assert not (directory / "authorization.json").exists()


def test_source_authorization_requires_clean_exact_git_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = tmp_path / "owned-checkout"
    checkout.mkdir()
    subprocess.run(
        ["git", "init", "-q", str(checkout)], check=True, capture_output=True
    )
    source = checkout / "source.txt"
    source.write_text("synthetic source\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(checkout), "add", "source.txt"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(checkout),
            "-c",
            "user.name=Offline",
            "-c",
            "user.email=offline@example.invalid",
            "commit",
            "-qm",
            "synthetic source",
        ],
        check=True,
        capture_output=True,
    )
    monkeypatch.setattr(
        q,
        "__file__",
        str(checkout / "src" / "judgmentllm" / "anthropic_qualification.py"),
    )
    head = subprocess.check_output(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True
    ).strip()
    assert q._source_commit() == head
    source.write_text("changed source\n", encoding="utf-8")
    with pytest.raises(q.QualificationError):
        q._source_commit()
    source.write_text("synthetic source\n", encoding="utf-8")
    (checkout / "unreviewed.txt").write_text("untracked", encoding="utf-8")
    with pytest.raises(q.QualificationError):
        q._source_commit()


def _boundary_carrier(call: q._PlannedCall) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(
        provider=call.provider,
        endpoint=call.endpoint,
        model=call.model,
        request_uid=call.request_uid,
        task_ids=call.task_ids,
        payload_sha256=call.payload_sha256,
        payload=call.request_body_utf8.encode(),
        prompt_sha256=call.prompt_sha256,
        dependency_sha256=call.dependency_sha256,
        transport_commitment=SimpleNamespace(
            identity=SimpleNamespace(
                submission_id=call.submission_id,
                attempt_id=call.attempt_id,
                correlation_id=call.correlation_id,
            ),
            mechanism=call.mechanism,
        ),
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("provider", "openai"),
        ("request_uid", "foreign"),
        ("task_ids", ("foreign",)),
        ("payload", b"{}"),
        ("prompt_sha256", "f" * 64),
        ("dependency_sha256", "f" * 64),
        ("transport_commitment", None),
    ],
)
def test_exact_plan_boundary_rejects_forged_physical_content_without_consuming_budget(
    planned: tuple[Path, q._QualificationPlan], field: str, value: Any
) -> None:
    directory, plan = planned
    digest = bytes_digest(canonical_bytes(plan))
    q._authorize(directory, plan, digest)
    call = plan.calls[0]
    guard = q._QualificationSubmissionGuard(
        directory, plan, call, token=q._EXECUTOR_TOKEN
    )
    carrier = _boundary_carrier(call)
    setattr(carrier, field, value)
    with pytest.raises(q.QualificationError):
        guard(carrier)
    assert q._consumed(directory) == set()
    assert list((directory / "forward-intents").iterdir()) == []


@pytest.mark.parametrize("mutation", ["authorization", "row_identity", "all_consumed"])
def test_plan_boundary_revalidates_shared_budget_at_forwarding(
    planned: tuple[Path, q._QualificationPlan], mutation: str
) -> None:
    directory, plan = planned
    digest = bytes_digest(canonical_bytes(plan))
    q._authorize(directory, plan, digest)
    guard = q._QualificationSubmissionGuard(
        directory, plan, plan.calls[0], token=q._EXECUTOR_TOKEN
    )
    with closing(sqlite3.connect(directory / "qualification.sqlite3")) as connection:
        statement = {
            "authorization": "UPDATE authorization SET source_commit='foreign'",
            "row_identity": "UPDATE calls SET payload_sha256='foreign'",
            "all_consumed": "UPDATE calls SET consumed_at='synthetic'",
        }[mutation]
        connection.execute(statement)
        connection.commit()
    with pytest.raises(q.QualificationError):
        guard(_boundary_carrier(plan.calls[0]))
    assert list((directory / "forward-intents").iterdir()) == []


@pytest.mark.asyncio
async def test_cost_report_requires_explicit_zero_cache_counters_without_fabricated_total(
    planned: tuple[Path, q._QualificationPlan], monkeypatch: pytest.MonkeyPatch
) -> None:
    directory, plan = planned
    sent = _install_transport(
        monkeypatch,
        plan,
        cache_counters={"cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
    )
    report = cast(
        dict[str, Any],
        await q.execute_plan(
            directory,
            authorized_plan_sha256=bytes_digest(canonical_bytes(plan)),
            authorized_source_commit=plan.source_commit,
            accept_billable_calls=True,
        ),
    )
    assert len(sent) == 4
    assert report["reported_usage_totals"]["total_tokens"] is None
    assert Decimal(report["estimated_usd"]) == Decimal("0.000056")
    assert all(
        Decimal(row["estimated_usd"]) == Decimal("0.000014") for row in report["calls"]
    )


@pytest.mark.parametrize(
    "change",
    [
        {"payload_sha256": "f" * 64},
        {"mechanism": "text"},
        {"attempt_id": "foreign-attempt"},
    ],
)
def test_plan_call_cannot_change_committed_body_mechanism_or_attempt(
    readonly_plan: tuple[Path, q._QualificationPlan], change: dict[str, Any]
) -> None:
    _, plan = readonly_plan
    native = next(call for call in plan.calls if call.mode == "native")
    with pytest.raises(ValueError):
        q._PlannedCall.model_validate_json(
            canonical_bytes(native.model_dump(mode="json") | change)
        )


@pytest.mark.parametrize("model", ["pricing", "plan"])
@pytest.mark.parametrize("version", [True, 1.0, 2.0])
def test_plan_and_pricing_versions_are_strict_integer_tokens(
    readonly_plan: tuple[Path, q._QualificationPlan], model: str, version: Any
) -> None:
    _, plan = readonly_plan
    record = pricing() if model == "pricing" else plan
    field = "profile_version" if model == "pricing" else "plan_version"
    with pytest.raises(ValueError):
        type(record).model_validate_json(
            canonical_bytes(record.model_dump(mode="json") | {field: version})
        )


@pytest.mark.parametrize("consent", [1, "yes"])
@pytest.mark.asyncio
async def test_truthy_values_cannot_replace_explicit_billable_boolean(
    planned: tuple[Path, q._QualificationPlan], consent: Any
) -> None:
    directory, plan = planned
    with pytest.raises(q.QualificationError):
        await q.execute_plan(
            directory,
            authorized_plan_sha256=bytes_digest(canonical_bytes(plan)),
            authorized_source_commit=plan.source_commit,
            accept_billable_calls=consent,
        )
    assert not (directory / "authorization.json").exists()
