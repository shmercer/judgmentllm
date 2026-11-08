"""Explicit qualification authorization, real SDK bytes and durable lifecycle."""

from __future__ import annotations

import json
import socket
import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, TypedDict, cast

import pytest

from judgmentllm import qualification as q
from judgmentllm.persistence.hashing import bytes_digest, canonical_bytes

# pyright: reportPrivateUsage=false


class _Authorization(TypedDict):
    authorized_plan_sha256: str
    authorized_source_commit: str
    accept_billable_calls: bool


def pricing() -> q._PricingProfile:
    return q._PricingProfile(
        model="synthetic-model",
        endpoints=("responses", "chat.completions"),
        reviewed_on=date(2026, 11, 7),
        context_tokens=100_000,
        maximum_output_tokens=1_000,
        input_usd_per_million=Decimal("0.1"),
        output_usd_per_million=Decimal("0.4"),
        documentation_urls=("https://openai.com/api/pricing/",),
        output_bound_includes_reasoning=True,
    )


@pytest.fixture
def planned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, q._QualificationPlan]:
    monkeypatch.setattr(q, "_source_commit", lambda: "a" * 40)
    directory = tmp_path / "qualification"
    plan = q.build_plan(
        directory=directory,
        source_commit="a" * 40,
        provider="openai",
        endpoints=("responses", "chat.completions"),
        model="synthetic-model",
        pricing=pricing(),
        max_requests=8,
        max_output_tokens=256,
        max_total_tokens=802_048,
        max_estimated_usd=Decimal("1"),
    )
    return directory, plan


def test_plan_is_offline_exact_and_has_no_execution_authority(
    planned: tuple[Path, q._QualificationPlan],
) -> None:
    directory, plan = planned
    assert len(plan.calls) == 8
    assert not (directory / "qualification.sqlite3").exists()
    assert not (directory / "execution-authorization.json").exists()
    assert plan.synthetic_items == q._SYNTHETIC_ITEMS
    assert plan.store is False
    assert plan.reasoning_retention == "none"
    for call in plan.calls:
        body = json.loads(call.request_body_utf8)
        assert body["store"] is False
        assert body["stream"] is False
        assert "api_key" not in body
        assert call.payload_sha256 == bytes_digest(call.request_body_utf8.encode())
    report = q.report_plan(directory)
    assert report["physical_send_attempts"] == 0
    assert report["independent_review_required"] is True


@pytest.mark.parametrize("changed", ["source", "digest", "consent"])
@pytest.mark.asyncio
async def test_execute_requires_exact_explicit_authorization(
    planned: tuple[Path, q._QualificationPlan], changed: str
) -> None:
    directory, plan = planned
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
            provider="openai",
            endpoints=("responses",)
            if changed == "scope"
            else ("responses", "chat.completions"),
            model="different-model" if changed == "model" else "synthetic-model",
            pricing=profile,
            max_requests=9 if changed == "count" else 8,
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
    plan_path.write_bytes(original[:-1] + b',"provider":"openai"}')
    with pytest.raises(ValueError):
        q.report_plan(directory)


def _install_transport(
    monkeypatch: pytest.MonkeyPatch,
    plan: q._QualificationPlan,
    *,
    timeout: bool = False,
    rejection_status: int | None = None,
    provider_failure: bool = False,
    completion_status: str | None = None,
) -> list[bytes]:
    import httpx

    import judgmentllm.providers.openai as provider

    sent: list[bytes] = []
    expected = {call.payload_sha256: call for call in plan.calls}

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.content)
        case = expected[bytes_digest(request.content)]
        assert request.content == case.request_body_utf8.encode()
        if timeout:
            raise httpx.ReadTimeout("synthetic timeout", request=request)
        if rejection_status is not None:
            return httpx.Response(
                rejection_status,
                json={"error": {"message": "PRIVATE_REJECTION_BODY_CANARY"}},
            )
        output = json.dumps(
            {"scores": [{"trait_id": "01", "category_label": "high"}]}
            if case.task_kind == "rubric"
            else {"winner_id": "001"}
        )
        if case.endpoint == "responses":
            body: dict[str, Any] = {
                "id": "resp_synthetic",
                "object": "response",
                "created_at": 1,
                "status": "completed",
                "model": "synthetic-reported-model",
                "output": [
                    {
                        "type": "message",
                        "id": "msg_synthetic",
                        "role": "assistant",
                        "status": "completed",
                        "content": [
                            {"type": "output_text", "text": output, "annotations": []}
                        ],
                    }
                ],
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 10,
                    "total_tokens": 110,
                    "input_tokens_details": {"cached_tokens": 5},
                    "output_tokens_details": {"reasoning_tokens": 0},
                },
            }
        else:
            body = {
                "id": "chatcmpl_synthetic",
                "object": "chat.completion",
                "created": 1,
                "model": "synthetic-reported-model",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": output},
                    }
                ],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 10,
                    "total_tokens": 110,
                },
            }
        if completion_status is not None:
            assert case.endpoint == "responses"
            if completion_status == "refusal":
                body["output"][0]["content"] = [
                    {"type": "refusal", "refusal": "PRIVATE_REFUSAL_BODY_CANARY"}
                ]
            else:
                body.update(
                    status="incomplete",
                    incomplete_details={"reason": "max_output_tokens"},
                )
        if provider_failure:
            assert case.endpoint == "responses"
            body.update(
                status="failed", error={"message": "PRIVATE_PROVIDER_FAILURE_CANARY"}
            )
        return httpx.Response(200, json=body)

    original = provider.OpenAIAdapter

    def factory(*args: Any, **kwargs: Any) -> provider.OpenAIAdapter:
        return original(*args, **kwargs, http_transport=httpx.MockTransport(handler))

    monkeypatch.setattr(provider, "OpenAIAdapter", factory)
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-qualification-credential")

    def forbidden(*_args: object, **_kwargs: object) -> Any:
        raise AssertionError("External network is forbidden")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    return sent


def _readonly_report(
    directory: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, object]:
    import judgmentllm.providers.openai as provider

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
        guard.setattr(provider, "OpenAIAdapter", forbidden)
        guard.setattr(provider, "_resolve_credentials", forbidden)
        report = q.report_plan(directory)
    assert snapshot() == before
    return report


@pytest.mark.asyncio
async def test_eight_call_matrix_real_sdk_and_safe_continuation(
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
    assert len(sent) == 8
    assert report["physical_send_attempts"] == 8
    assert report["reported_usage_totals"] == {
        "input_tokens": 800,
        "output_tokens": 80,
        "total_tokens": 880,
    }
    calls = cast(list[dict[str, object]], report["calls"])
    assert all(row["local_assessment_valid"] is True for row in calls)
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
    assert len(sent) == 8
    assert again["physical_send_attempts"] == 8
    # Deleting the ledger must not recreate an empty budget.
    (directory / "qualification.sqlite3").unlink()
    with pytest.raises(q.QualificationError):
        await q.execute_plan(directory, **arguments)
    assert len(sent) == 8


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
    assert first["provider_response_observed"] is False
    assert first["submission_certainty"] == "unknown"
    assert first["invocation_acceptance"] == "unresolved"
    assert first["completion_status"] == "not_observed"
    assert first["provider_outcome"] is first["invocation_failure_code"] is None
    assert _readonly_report(directory, monkeypatch)["calls"] == report["calls"]
    again = await q.execute_plan(directory, **arguments)
    assert len(sent) == 1
    assert again["physical_send_attempts"] == 1
    with sqlite3.connect(directory / "qualification.sqlite3") as connection:
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
    planned: tuple[Path, q._QualificationPlan],
) -> None:
    directory, plan = planned
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
    with sqlite3.connect(directory / "qualification.sqlite3") as connection:
        connection.execute("UPDATE calls SET consumed_at=NULL")
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
    import judgmentllm.providers.openai as provider

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

    monkeypatch.setattr(provider, "OpenAIAdapter", forbidden)
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
    assert first["provider_outcome"] == first["completion_status"] == "provider_failure"
    assert first["provider_accepted"] is None
    assert first["acceptance_outcome"] == "provider_failure"
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
    assert first["assessment_behavior_verified"] is False
    assert first["local_assessment_valid"] is False
    assert "PRIVATE_REFUSAL_BODY_CANARY" not in json.dumps(report)
