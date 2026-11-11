"""Adversarial source-bound Vertex qualification over synthetic SDK HTTP only."""

from __future__ import annotations

import asyncio

# pyright: reportPrivateUsage=false
import json
import os
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from contextlib import closing
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import httpx2
import pytest
from vertex_offline import adc as _adc
from vertex_offline import envelope, isolate

from judgmentllm import vertex_qualification as q
from judgmentllm import vertex_qualification_execution as e
from judgmentllm.persistence.hashing import bytes_digest, canonical_bytes
from judgmentllm.providers import vertex as provider
from scripts import qualify_vertex as cli

PROJECT = "synthetic-private-project"


def adc(monkeypatch: pytest.MonkeyPatch) -> Any:
    credentials, calls = _adc(monkeypatch)
    credentials._quota_project_id = PROJECT
    return credentials, calls


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    isolate(monkeypatch)
    monkeypatch.setattr(q, "_source_commit", lambda: "a" * 40)
    monkeypatch.setattr(e, "_source_commit", lambda: "a" * 40)
    from google.auth import _cloud_sdk, _default
    from google.auth.compute_engine import _metadata

    def deny(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Local ADC files, metadata and keyring are forbidden")

    monkeypatch.setattr(_cloud_sdk, "get_application_default_credentials_path", deny)
    monkeypatch.setattr(_default, "load_credentials_from_file", deny)
    monkeypatch.setattr(_metadata, "ping", deny)
    monkeypatch.setattr(_metadata, "get", deny)
    from types import SimpleNamespace

    monkeypatch.setitem(sys.modules, "keyring", SimpleNamespace(get_password=deny))


@pytest.fixture(params=["api_key", "adc"])
def planned(
    tmp_path: Path, request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, q.VertexQualificationPlanV2, dict[str, Any]]:
    directory = tmp_path / "plan"
    plan = q.build_plan(
        directory=directory,
        source_commit="a" * 40,
        project=PROJECT,
        authentication_mode=request.param,
    )
    if request.param == "adc":
        adc(monkeypatch)
    arguments = dict(
        authorized_plan_sha256=bytes_digest((directory / "plan.json").read_bytes()),
        authorized_source_commit=plan.source_commit,
        authorized_project=PROJECT,
        authorized_authentication_mode=plan.authentication_mode,
        authorized_location="global",
        reviewed_request_count=4,
        reviewed_reserved_estimated_usd=plan.reserved_estimated_usd,
        reviewed_estimated_ceiling_usd=Decimal("0.02"),
        accept_billable_calls=True,
        accept_full_synthetic_capture=True,
    )
    return directory, plan, arguments


def install(
    monkeypatch: pytest.MonkeyPatch,
    directory: Path,
    plan: q.VertexQualificationPlanV2,
    customize: Callable[[dict[str, Any], int], Any] | None = None,
) -> list[bytes]:
    sent: list[bytes] = []

    def respond(request: Any) -> Any:
        index = next(
            i
            for i, c in enumerate(plan.calls)
            if c.request_body_utf8.encode() == request.content
        )
        call = plan.calls[index]
        assert request.method == "POST"
        assert (
            str(request.url)
            == "https://aiplatform.googleapis.com/v1beta1/projects/synthetic-private-project/locations/global/publishers/google/models/gemini-3.1-flash-lite:generateContent"
        )
        if plan.authentication_mode == "api_key":
            assert request.headers["x-goog-api-key"] == "SYNTHETIC_SELECTED_VERTEX_KEY"
            assert "authorization" not in request.headers
        else:
            assert (
                request.headers["authorization"] == "Bearer SYNTHETIC_ADC_ACCESS_TOKEN"
            )
            assert "x-goog-api-key" not in request.headers
            assert request.headers["x-goog-user-project"] == PROJECT
        assert e._consumed(directory, plan, bytes_digest(canonical_bytes(plan))) >= {
            call.case_id
        }
        sent.append(request.content)
        with (e._state_directory(directory) / "independent-forwards").open(
            "ab", buffering=0
        ) as stream:
            stream.write((call.transport.identity.submission_id + "\n").encode())
            os.fsync(stream.fileno())
        assessment = (
            {"scores": [{"trait_id": "01", "category_label": "high"}]}
            if call.task_kind == "rubric"
            else {"winner_id": "002"}
        )
        body = envelope(
            json.dumps(assessment),
            responseId="synthetic-" + call.case_id,
            modelVersion=plan.model,
            usageMetadata={
                "promptTokenCount": 20,
                "candidatesTokenCount": 11,
                "thoughtsTokenCount": 3,
                "totalTokenCount": 34,
                "cachedContentTokenCount": 0,
            },
        )
        if customize is not None:
            response = customize(body, index)
            if response is not None:
                return response
        return httpx2.Response(200, json=body)

    def transport(**kwargs: Any) -> Any:
        return httpx2.MockTransport(respond)

    monkeypatch.setattr(httpx2, "AsyncHTTPTransport", transport)
    return sent


def files(directory: Path) -> dict[str, bytes]:
    content = {
        prefix + str(p.relative_to(root)): p.read_bytes()
        for prefix, root in (
            ("plan/", directory),
            ("state/", e._state_directory(directory)),
        )
        for p in root.rglob("*")
        if p.is_file()
    }
    anchor = e._authorization_anchor(directory)
    if anchor.exists():
        content["authorization-anchor"] = anchor.read_bytes()
    return content


@pytest.mark.asyncio
async def test_all_four_cases_one_shot_deterministic_readonly_and_no_promotion(
    planned: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from judgmentllm import vertex_registry

    reviewed_profiles = vertex_registry._QUALIFIED_PROFILES
    directory, plan, arguments = planned
    sent = install(monkeypatch, directory, plan)
    raw = (directory / "plan.json").read_bytes()
    result = cast(dict[str, Any], await e.execute_plan(directory, **arguments))
    assert len(sent) == result["physical_send_attempts"] == 4
    assert (
        len(
            (e._state_directory(directory) / "independent-forwards")
            .read_text()
            .splitlines()
        )
        == 4
    )
    assert (
        result["live_qualified"]
        and result["overall_qualification_result"] == "qualified"
    )
    assert len(result["successful_cases"]) == 4 and not result["unsuccessful_cases"]
    assert all(
        row["assessment_valid"] and row["response_model_identity_verified"]
        for row in result["calls"]
    )
    assert result["reported_usage_totals"] == dict(
        input_tokens=80,
        output_tokens=44,
        reasoning_tokens=12,
        total_tokens=136,
        cache_read_tokens=0,
    )
    assert result["estimated_actual_usd"] == "0.000104"
    assert (directory / "plan.json").read_bytes() == raw
    consent = json.loads(
        (e._state_directory(directory) / "execution-authorization.json").read_bytes()
    )
    assert consent["plan_sha256"] == arguments["authorized_plan_sha256"]
    assert (
        consent["full_synthetic_assessment_capture_authorized"]
        and not consent["monetary_cap_provider_enforced"]
    )
    before = files(directory)
    assert (
        e.report_plan(
            directory, expected_plan_sha256=arguments["authorized_plan_sha256"]
        )
        == e.report_plan(
            directory, expected_plan_sha256=arguments["authorized_plan_sha256"]
        )
        == result
    )
    assert files(directory) == before

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Credentials/client forbidden during repeated inspection")

    monkeypatch.setattr(provider, "_resolve_credentials", forbidden)
    assert await e.execute_plan(directory, **arguments) == result
    assert len(sent) == 4
    assert vertex_registry._QUALIFIED_PROFILES == reviewed_profiles
    encoded = json.dumps(result)
    assert "SYNTHETIC_SELECTED" not in encoded and "SYNTHETIC_SECONDARY" not in encoded
    assert all(b"SYNTHETIC_SELECTED" not in raw for raw in files(directory).values())


@pytest.mark.parametrize(
    "change",
    [
        "digest",
        "missing_digest",
        "source",
        "current_source",
        "billable",
        "capture",
        "truthy",
        "sdk",
        "ceiling",
        "project",
        "location",
        "mode",
        "count",
        "count_bool",
        "reservation",
        "reservation_float",
        "reviewed_ceiling",
    ],
)
@pytest.mark.asyncio
async def test_authorization_before_credentials_or_clients(
    planned: Any, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    directory, _plan, arguments = planned

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Credentials/client forbidden before authorization")

    monkeypatch.setattr(provider, "_resolve_credentials", forbidden)
    monkeypatch.setattr(provider, "_sdk", forbidden)
    if change == "digest":
        arguments["authorized_plan_sha256"] = "0" * 64
    elif change == "missing_digest":
        arguments["authorized_plan_sha256"] = ""
    elif change == "source":
        arguments["authorized_source_commit"] = "b" * 40
    elif change == "current_source":
        monkeypatch.setattr(e, "_source_commit", lambda: "b" * 40)
    elif change == "billable":
        arguments["accept_billable_calls"] = False
    elif change == "capture":
        arguments["accept_full_synthetic_capture"] = False
    elif change == "truthy":
        arguments["accept_full_synthetic_capture"] = 1
    elif change == "sdk":
        monkeypatch.setattr(
            q,
            "installed_sdk_evidence",
            lambda: (_ for _ in ()).throw(ValueError("SDK differs")),
        )
    elif change in {
        "project",
        "location",
        "mode",
        "count",
        "count_bool",
        "reservation",
        "reservation_float",
        "reviewed_ceiling",
    }:
        field, value = {
            "project": ("authorized_project", "synthetic-other-project"),
            "location": ("authorized_location", "us-central1"),
            "mode": (
                "authorized_authentication_mode",
                "adc" if _plan.authentication_mode == "api_key" else "api_key",
            ),
            "count": ("reviewed_request_count", 5),
            "count_bool": ("reviewed_request_count", True),
            "reservation": ("reviewed_reserved_estimated_usd", Decimal("0.01")),
            "reservation_float": ("reviewed_reserved_estimated_usd", 0.00896),
            "reviewed_ceiling": ("reviewed_estimated_ceiling_usd", Decimal("0.03")),
        }[change]
        arguments[field] = value
    else:
        plan_data = json.loads((directory / "plan.json").read_bytes())
        plan_data["proposed_estimated_authorization_ceiling_usd"] = "0.03"
        raw = canonical_bytes(plan_data)
        (directory / "plan.json").write_bytes(raw)
        (directory / "plan.sha256").write_text(bytes_digest(raw) + "\n")
        arguments["authorized_plan_sha256"] = bytes_digest(raw)
    with pytest.raises(q.QualificationError):
        await e.execute_plan(directory, **arguments)
    assert not (e._state_directory(directory) / "execution-authorization.json").exists()


@pytest.mark.parametrize(
    "kind",
    [
        "malformed",
        "fence",
        "duplicate",
        "category",
        "missing",
        "refusal",
        "truncated",
        "unsupported",
        "multiple",
        "missing_model",
        "alias",
        "prefix",
        "model_substitution",
        "missing_response",
        "unsafe",
        "usage_missing",
        "usage_partial",
        "cache",
        "thinking",
    ],
)
@pytest.mark.asyncio
async def test_assessment_identity_retention_and_usage(
    planned: Any, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    directory, plan, arguments = planned

    def customize(body: dict[str, Any], index: int) -> None:
        if index != 0:
            return
        part = body["candidates"][0]["content"]["parts"][0]
        candidate = body["candidates"][0]
        if kind == "malformed":
            part["text"] = "{broken"
        elif kind == "fence":
            part["text"] = "```json\n" + part["text"] + "\n```"
        elif kind == "duplicate":
            part["text"] = '{"scores":[],"scores":[]}'
        elif kind == "category":
            part["text"] = '{"scores":[{"trait_id":"01","category_label":"invented"}]}'
        elif kind == "missing":
            part["text"] = '{"scores":[]}'
        elif kind == "refusal":
            candidate["finishReason"] = "SAFETY"
        elif kind == "truncated":
            candidate["finishReason"] = "MAX_TOKENS"
        elif kind == "unsupported":
            candidate["finishReason"] = "OTHER"
        elif kind == "multiple":
            candidate["content"]["parts"].append({"text": part["text"]})
        elif kind == "missing_model":
            body.pop("modelVersion")
        elif kind == "alias":
            body["modelVersion"] = plan.model + "-001"
        elif kind == "prefix":
            body["modelVersion"] = "models/" + plan.model
        elif kind == "model_substitution":
            body["modelVersion"] = "unexpected-model"
        elif kind == "missing_response":
            body.pop("responseId")
        elif kind == "unsafe":
            part["text"] = (
                "SYNTHETIC_SELECTED_VERTEX_KEY"
                if plan.authentication_mode == "api_key"
                else "SYNTHETIC_ADC_ACCESS_TOKEN"
            )
        elif kind == "usage_missing":
            body.pop("usageMetadata")
        elif kind == "usage_partial":
            body["usageMetadata"] = {"promptTokenCount": 20}
        elif kind == "cache":
            body["usageMetadata"]["cachedContentTokenCount"] = 3
        elif kind == "thinking":
            candidate["content"]["parts"].insert(
                0,
                {
                    "thought": True,
                    "text": "PRIVATE_THOUGHT_CANARY",
                    "thoughtSignature": "PRIVATE_SIGNATURE_CANARY",
                },
            )

    sent = install(monkeypatch, directory, plan, customize)
    result = cast(dict[str, Any], await e.execute_plan(directory, **arguments))
    row = result["calls"][0]
    terminal_invalid = kind in {
        "malformed",
        "fence",
        "duplicate",
        "category",
        "missing",
    }
    fully_valid = kind in {"usage_missing", "usage_partial", "cache", "thinking"}
    assert len(sent) == (4 if terminal_invalid or fully_valid else 1)
    assert row["assessment_valid"] is fully_valid
    assert result["live_qualified"] is fully_valid
    if terminal_invalid:
        assert row["assessment_continuation_allowed"] and not row["canonical_valid"]
        assert row["diagnostic"]["retention"] == "retained"
    if kind in {"missing_model", "alias", "prefix", "model_substitution"}:
        assert not row["response_model_identity_verified"] and row["canonical_valid"]
        assert row["response_model_diagnostic"] == (
            "reported_model_missing"
            if kind == "missing_model"
            else "reported_model_mismatch"
        )
    if kind in {"usage_missing", "usage_partial", "cache"}:
        assert result["estimated_actual_usd"] is None
    if kind == "usage_partial":
        assert (
            row["usage"]["input_tokens"] == 20
            and row["usage"]["reasoning_tokens"] is None
        )
    before = files(directory)
    assert (
        e.report_plan(
            directory, expected_plan_sha256=arguments["authorized_plan_sha256"]
        )
        == result
        and files(directory) == before
    )
    assert all(
        b"PRIVATE_THOUGHT" not in raw
        and b"PRIVATE_SIGNATURE" not in raw
        and b"SYNTHETIC_SELECTED" not in raw
        for raw in before.values()
    )
    assert await e.execute_plan(directory, **arguments) == result
    assert len(sent) <= 4


@pytest.mark.parametrize(
    "failure", [400, 401, 403, 404, 422, 429, 500, "timeout", "drop"]
)
@pytest.mark.asyncio
async def test_rejections_and_uncertain_calls_never_repeat(
    planned: Any, monkeypatch: pytest.MonkeyPatch, failure: Any
) -> None:
    directory, plan, arguments = planned

    def customize(body: dict[str, Any], index: int) -> Any:
        if failure == "timeout":
            raise httpx2.ReadTimeout("PRIVATE_EXCEPTION_CANARY")
        if failure == "drop":
            raise httpx2.ConnectError("PRIVATE_EXCEPTION_CANARY")
        return httpx2.Response(
            failure,
            json={
                "error": {
                    "code": failure,
                    "message": "PRIVATE_EXCEPTION_CANARY",
                    "status": "UNAVAILABLE",
                }
            },
        )

    sent = install(monkeypatch, directory, plan, customize)
    result = cast(dict[str, Any], await e.execute_plan(directory, **arguments))
    assert len(sent) == result["physical_send_attempts"] == 1
    row = result["calls"][0]
    assert (
        row["invocation_failure_code"] is not None
        if isinstance(failure, int) and failure < 500
        else row["status"] == "unresolved"
    )
    assert "PRIVATE_EXCEPTION_CANARY" not in json.dumps(result)
    assert not result["live_qualified"]
    assert await e.execute_plan(directory, **arguments) == result and len(sent) == 1


def wire_for_plan(
    directory: Path, plan: q.VertexQualificationPlanV2, case_index: int = 0
) -> Any:
    # Candidate preparation seals predate its final privacy chmod. Reconstruct
    # purely, as the independent verifier does; never reopen/mutate that store.
    calls: list[Any] = []
    original = q.sdk_request

    def capture(wire: Any, runtime: Any) -> Any:
        calls.append(wire)
        return original(wire, runtime)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(q, "sdk_request", capture)
        q._reconstruct(directory, plan, plan.calls[case_index])
    return calls[0]


@pytest.mark.parametrize(
    "mutation",
    [
        "payload",
        "payload_sha256",
        "prompt_sha256",
        "request_uid",
        "task_ids",
        "dependency_sha256",
        "receipt",
        "source",
        "plan",
        "context",
    ],
)
def test_boundary_rechecks_exact_committed_authority(
    planned: Any, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    directory, plan, arguments = planned
    e._authorize(directory, plan, arguments["authorized_plan_sha256"])
    guard = e._QualificationSubmissionGuard(
        directory, plan, plan.calls[0], token=e._EXECUTOR_TOKEN
    )
    wire = wire_for_plan(directory, plan)
    if mutation in {
        "payload",
        "payload_sha256",
        "prompt_sha256",
        "request_uid",
        "task_ids",
        "dependency_sha256",
    }:
        object.__setattr__(
            wire,
            mutation,
            {"payload": b"{}", "task_ids": ("other",)}.get(mutation, "0" * 64),
        )
    elif mutation == "receipt":
        object.__setattr__(wire.transport_commitment, "mechanism", "text-other")
    elif mutation == "source":
        monkeypatch.setattr(e, "_source_commit", lambda: "b" * 40)
    elif mutation == "plan":
        (directory / "plan.sha256").write_text("0" * 64)
    else:
        from judgmentllm.vertex_transport import production_context

        spec = q._specification(directory, "rubric", "text", 512)
        with pytest.raises(provider.ProviderIntegrityError):
            guard.verify_context(production_context(spec))
        return
    with pytest.raises(provider.ProviderIntegrityError):
        guard(wire)
    assert not list((e._state_directory(directory) / "forward-intents").iterdir())


def test_guard_token_duplicate_and_ledger_reset_cannot_replenish(planned: Any) -> None:
    directory, plan, arguments = planned
    with pytest.raises(q.QualificationError):
        e._QualificationSubmissionGuard(directory, plan, plan.calls[0], token=object())
    e._authorize(directory, plan, arguments["authorized_plan_sha256"])
    guard = e._QualificationSubmissionGuard(
        directory, plan, plan.calls[0], token=e._EXECUTOR_TOKEN
    )
    wire = wire_for_plan(directory, plan)
    guard(wire)
    with pytest.raises(provider.ProviderIntegrityError):
        guard(wire)
    with closing(
        sqlite3.connect(e._state_directory(directory) / "qualification.sqlite3")
    ) as connection:
        connection.execute("UPDATE calls SET consumed_at=NULL")
        connection.commit()
    assert e._consumed(directory, plan, arguments["authorized_plan_sha256"]) == {
        "rubric-text"
    }
    with pytest.raises(provider.ProviderIntegrityError):
        guard(wire)
    (e._state_directory(directory) / "qualification.sqlite3").unlink()
    with pytest.raises(q.QualificationError):
        e._authorize(directory, plan, arguments["authorized_plan_sha256"])


@pytest.mark.parametrize(
    "mutation",
    [
        "authorization",
        "marker",
        "row",
        "plan_row",
        "missing_intents",
        "extra_intent",
        "corrupt_intent",
        "row_without_intent",
        "symlink",
        "missing_anchor",
        "changed_anchor",
        "missing_state",
        "unknown_state",
        "orphan",
    ],
)
def test_damaged_authorization_is_not_reconstructed(
    planned: Any, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    directory, plan, arguments = planned
    if mutation in {"orphan", "unknown_state"}:
        e._state_directory(directory).mkdir()
        (
            e._state_directory(directory)
            / ("ledger-created" if mutation == "orphan" else "unreviewed-file")
        ).write_bytes(b"orphan")
    else:
        e._authorize(directory, plan, arguments["authorized_plan_sha256"])
        if mutation == "authorization":
            (
                e._state_directory(directory) / "execution-authorization.json"
            ).write_bytes(b"{}")
        elif mutation == "marker":
            (e._state_directory(directory) / "ledger-created").write_bytes(b"wrong")
        elif mutation in {"row", "row_without_intent", "plan_row"}:
            with closing(
                sqlite3.connect(e._state_directory(directory) / "qualification.sqlite3")
            ) as connection:
                connection.execute(
                    {
                        "row": "DELETE FROM calls WHERE case_id='rubric-text'",
                        "row_without_intent": "UPDATE calls SET consumed_at='synthetic'",
                        "plan_row": "UPDATE authorization SET source_commit='wrong'",
                    }[mutation]
                )
                connection.commit()
        elif mutation == "missing_intents":
            (e._state_directory(directory) / "forward-intents").rmdir()
        elif mutation == "extra_intent":
            (
                e._state_directory(directory) / "forward-intents" / "extra.json"
            ).write_bytes(b"{}")
        elif mutation == "corrupt_intent":
            (
                e._state_directory(directory) / "forward-intents" / "rubric-text.json"
            ).write_bytes(b"{}")
        elif mutation == "missing_anchor":
            e._authorization_anchor(directory).unlink()
        elif mutation == "changed_anchor":
            e._authorization_anchor(directory).write_bytes(b"{}")
        elif mutation == "missing_state":
            import shutil

            shutil.rmtree(e._state_directory(directory))
        elif mutation == "symlink":
            target = directory / "plan.sha256"
            target.rename(directory / "original-digest")
            try:
                target.symlink_to(directory / "original-digest")
            except OSError:
                # Windows may deny symlink creation. Exercise the same admission
                # predicate without introducing an unexpected qualification skip.
                target.write_bytes((directory / "original-digest").read_bytes())
                original_symlink = Path.is_symlink

                def symbolic(path: Path) -> bool:
                    return path == target or original_symlink(path)

                monkeypatch.setattr(Path, "is_symlink", symbolic)
    with pytest.raises((q.QualificationError, OSError)):
        e.report_plan(
            directory, expected_plan_sha256=arguments["authorized_plan_sha256"]
        )
    if mutation in {
        "orphan",
        "authorization",
        "missing_anchor",
        "changed_anchor",
        "missing_state",
        "unknown_state",
    }:
        with pytest.raises((q.QualificationError, OSError)):
            e._authorize(directory, plan, arguments["authorized_plan_sha256"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "damage",
    [
        "diagnostic",
        "response",
        "transport",
        "results",
        "observation",
        "manifest",
        "source_snapshot",
        "specification",
    ],
)
async def test_retained_evidence_integrity_blocks_qualification(
    planned: Any, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    from judgmentllm.persistence.artifacts import ArtifactStore
    from judgmentllm.persistence.repository import ReadOnlyRepository
    from judgmentllm.persistence.transport_receipts import transport_reference

    directory, plan, arguments = planned
    sent = install(monkeypatch, directory, plan)
    await e.execute_plan(directory, **arguments)
    case = plan.calls[0]
    store = e._state_directory(directory) / "execution" / case.case_id
    with ReadOnlyRepository(store, preserve_files=True) as repository:
        receipt = next(repository.iter_evidence(case.run_id))
        artifacts = ArtifactStore(repository.run_directory(case.run_id), create=False)
        if damage in {
            "diagnostic",
            "response",
            "transport",
            "manifest",
            "source_snapshot",
            "specification",
        }:
            if damage == "diagnostic":
                assert (
                    receipt.assessment_diagnostic is not None
                    and receipt.assessment_diagnostic.artifact is not None
                )
                path = artifacts.verify(receipt.assessment_diagnostic.artifact)
                assert path is not None
            elif damage == "response":
                assert receipt.retained is not None
                path = artifacts.verify(receipt.retained)
                assert path is not None
            elif damage in {"manifest", "source_snapshot", "specification"}:
                from judgmentllm.preparation import _load_manifest

                manifest = _load_manifest(repository, case.run_id, unsealed=False)
                if damage == "manifest":
                    path = artifacts.root / "manifest.json"
                else:
                    identity = (
                        manifest.specification
                        if damage == "specification"
                        else next(
                            source.snapshot
                            for source in manifest.sources
                            if source.snapshot is not None
                        )
                    )
                    path = artifacts.verify(identity)
                    assert path is not None
            else:
                path = artifacts.reference_path(
                    transport_reference(case.transport.identity.attempt_id)
                )
            path.write_bytes(b"PRIVATE_CORRUPTION_CANARY")
    if damage in {"results", "observation"}:
        with closing(sqlite3.connect(store / "registry.sqlite3")) as connection:
            table = "results" if damage == "results" else "observations"
            for (trigger,) in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=?",
                (table,),
            ).fetchall():
                connection.execute('DROP TRIGGER "' + trigger.replace('"', '""') + '"')
            connection.execute(
                "DELETE FROM " + ("results" if damage == "results" else "observations")
            )
            connection.commit()
    result = cast(
        dict[str, Any],
        e.report_plan(
            directory, expected_plan_sha256=arguments["authorized_plan_sha256"]
        ),
    )
    assert (
        not result["live_qualified"]
        and not result["calls"][0]["assessment_continuation_allowed"]
    )
    assert "PRIVATE_CORRUPTION_CANARY" not in json.dumps(result)
    before = files(directory)
    assert (
        e.report_plan(
            directory, expected_plan_sha256=arguments["authorized_plan_sha256"]
        )
        == result
        and files(directory) == before
    )
    try:
        await e.execute_plan(directory, **arguments)
    except (ValueError, RuntimeError):
        pass
    assert len(sent) == 4


_CHILD = r"""
import asyncio, json, os, socket, sys
from decimal import Decimal
from pathlib import Path
import pytest
sys.path.insert(0, str(Path.cwd() / 'tests'))
from vertex_offline import isolate
from test_vertex_qualification_execution import install
from judgmentllm import vertex_qualification as q, vertex_qualification_execution as e
from judgmentllm import execution
m=pytest.MonkeyPatch()
isolate(m)
q._source_commit=e._source_commit=lambda:'a'*40
directory=Path(sys.argv[1])
boundary, mode=sys.argv[2:]
plan,digest=e._load_plan(directory,(directory/'plan.sha256').read_text().strip())
if plan.authentication_mode == 'adc':
    from test_vertex_qualification_execution import adc
    adc(m)
def customize(body,index):
    if boundary == 'after_forward' and mode == 'execute': os._exit(73)
install(m,directory,plan,customize)
original=e._durable
def durable(path,raw):
    original(path,raw)
    if mode == 'execute' and boundary == 'before_forward' and path.parent.name == 'forward-intents': os._exit(73)
e._durable=durable
original_execute=execution.execute_run
def fault(stage,request):
    if mode == 'execute' and boundary == stage: os._exit(73)
async def execute(*args,**kwargs):
    return await original_execute(*args,**kwargs,fault=fault)
execution.execute_run=execute
async def main():
    result=await e.execute_plan(directory, authorized_plan_sha256=digest, authorized_source_commit=plan.source_commit, authorized_project=plan.project, authorized_authentication_mode=plan.authentication_mode, authorized_location=plan.location, reviewed_request_count=4, reviewed_reserved_estimated_usd=plan.reserved_estimated_usd, reviewed_estimated_ceiling_usd=Decimal('0.02'), accept_billable_calls=True, accept_full_synthetic_capture=True)
    print(json.dumps(result,sort_keys=True))
asyncio.run(main())
"""


@pytest.mark.parametrize(
    "boundary,expected_initial,expected_final",
    [
        ("before_forward", 0, 0),
        ("after_forward", 1, 1),
        ("after_evidence_receipt", 1, 4),
    ],
)
def test_hard_crash_and_fresh_process_recovery_do_not_reset_allocation(
    planned: Any, boundary: str, expected_initial: int, expected_final: int
) -> None:
    directory, _plan, _arguments = planned
    environment = {
        name: os.environ[name]
        for name in ("PATH", "HOME", "SystemRoot", "WINDIR", "TEMP", "TMP", "LANG")
        if name in os.environ
    }
    command = [sys.executable, "-B", "-c", _CHILD, str(directory), boundary]
    initial = subprocess.run(
        [*command, "execute"],
        env=environment,
        text=True,
        capture_output=True,
        timeout=40,
    )
    assert initial.returncode == 73, initial.stderr
    log = e._state_directory(directory) / "independent-forwards"
    assert (
        len(log.read_text().splitlines()) if log.exists() else 0
    ) == expected_initial
    for _ in range(2):
        resumed = subprocess.run(
            [*command, "recover"],
            env=environment,
            text=True,
            capture_output=True,
            timeout=40,
            check=True,
        )
        result = json.loads(resumed.stdout)
        forwards: list[str] = log.read_text().splitlines() if log.exists() else []
        assert len(forwards) == len(set(forwards)) == expected_final
        assert result["physical_send_attempts"] == (4 if expected_final == 4 else 1)
        assert result["live_qualified"] is (expected_final == 4)


@pytest.mark.parametrize(
    "field",
    [
        "authorization_version",
        "maximum_physical_requests",
        "maximum_output_tokens_per_call",
    ],
)
def test_authorization_versions_and_limits_require_exact_integers(field: str) -> None:
    with pytest.raises(ValueError):
        e._Authorization.model_validate(
            dict(
                e._Authorization(
                    plan_sha256="a" * 64,
                    source_commit="b" * 40,
                    authentication_mode="api_key",
                    credential_source="JUDGMENTLLM_VERTEX_API_KEY",
                    route_sha256="c" * 64,
                    maximum_output_tokens_per_call=512,
                    reserved_input_tokens=23552,
                    reserved_output_tokens_including_thinking=2048,
                    reserved_estimated_usd="0.008960",
                )
            )
            | {field: True}
        )


@pytest.mark.asyncio
async def test_cli_execute_requires_all_flags_and_reports_sanitized_failures(
    planned: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The CLI uses asyncio.run, so invoke it from a separate sync helper below.
    directory, plan, _arguments = planned
    sent = install(monkeypatch, directory, plan)
    assert not sent
    monkeypatch.setattr(
        sys,
        "argv",
        ["qualify_provider.py", "execute", "--evidence-dir", str(directory)],
    )
    with pytest.raises(SystemExit):
        cli.main()
    assert "required" in capsys.readouterr().err and not sent


def test_cli_executes_only_explicit_plan_and_capture_consent(
    planned: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    directory, plan, arguments = planned
    sent = install(monkeypatch, directory, plan)
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", PROJECT)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "qualify_provider.py",
            "execute",
            "--evidence-dir",
            str(directory),
            "--authorize-plan-sha256",
            arguments["authorized_plan_sha256"],
            "--authorize-source-commit",
            plan.source_commit,
            "--auth-mode",
            plan.authentication_mode,
            "--location",
            "global",
            "--reviewed-request-count",
            "4",
            "--reviewed-reserved-estimated-usd",
            str(plan.reserved_estimated_usd),
            "--reviewed-estimated-ceiling-usd",
            "0.02",
            "--accept-billable-calls",
            "--accept-full-synthetic-capture",
        ],
    )
    cli.main()
    assert json.loads(capsys.readouterr().out)["live_qualified"] and len(sent) == 4


@pytest.mark.asyncio
async def test_observed_estimate_above_ceiling_stops_remaining_allocations(
    planned: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory, plan, arguments = planned

    def expensive(body: dict[str, Any], index: int) -> None:
        body["usageMetadata"]["promptTokenCount"] = 100000
        body["usageMetadata"]["totalTokenCount"] = 100014

    sent = install(monkeypatch, directory, plan, expensive)
    result = cast(dict[str, Any], await e.execute_plan(directory, **arguments))
    assert len(sent) == 1 and float(result["estimated_exposure_usd"]) > 0.02
    case = plan.calls[1]
    guard = e._QualificationSubmissionGuard(
        directory, plan, case, token=e._EXECUTOR_TOKEN
    )

    wire = wire_for_plan(directory, plan, 1)
    with pytest.raises(provider.ProviderIntegrityError):
        guard(wire)
    assert len(sent) == 1


@pytest.mark.parametrize("mutation", ["input", "dependency", "body"])
@pytest.mark.asyncio
async def test_modified_rehashed_plan_or_inputs_fail_before_credential_access(
    planned: Any, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    directory, _plan, arguments = planned

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Credential access before plan validation")

    monkeypatch.setattr(provider, "_resolve_credentials", forbidden)
    if mutation == "input":
        (directory / "responses.jsonl").write_bytes(b"PRIVATE_MODIFIED_INPUT")
    else:
        data = json.loads((directory / "plan.json").read_bytes())
        if mutation == "dependency":
            data["calls"][0]["transport"]["dependency_sha256"] = "0" * 64
        else:
            call = data["calls"][0]
            call["request_body_utf8"] = call["request_body_utf8"].replace(
                "The seed grew", "A different seed grew"
            )
            call["payload_sha256"] = bytes_digest(call["request_body_utf8"].encode())
            call["transport"]["payload_sha256"] = bytes_digest(
                call["request_body_utf8"].encode()
            )
        raw = canonical_bytes(data)
        (directory / "plan.json").write_bytes(raw)
        (directory / "plan.sha256").write_text(bytes_digest(raw))
        arguments["authorized_plan_sha256"] = bytes_digest(raw)
    with pytest.raises(ValueError):
        await e.execute_plan(directory, **arguments)
    assert not (e._state_directory(directory) / "execution-authorization.json").exists()


def test_out_of_order_guard_has_no_remaining_case_authority(planned: Any) -> None:
    directory, plan, arguments = planned
    e._authorize(directory, plan, arguments["authorized_plan_sha256"])
    guard = e._QualificationSubmissionGuard(
        directory, plan, plan.calls[1], token=e._EXECUTOR_TOKEN
    )
    with pytest.raises(provider.ProviderIntegrityError):
        guard(wire_for_plan(directory, plan, 1))
    assert not e._consumed(directory, plan, arguments["authorized_plan_sha256"])


@pytest.mark.asyncio
async def test_preflight_verifies_every_prepared_allocation_before_credentials(
    planned: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from collections.abc import Generator
    from contextlib import contextmanager

    from judgmentllm import preparation

    directory, _plan, arguments = planned
    original = preparation.prepare_run

    @contextmanager
    def tampered(*args: Any, **kwargs: Any) -> Generator[Any]:
        with original(*args, **kwargs) as prepared:
            deps = tuple(prepared.iter_request_dependencies())

            def extra(**kwargs: Any) -> Any:
                return iter(deps * 2)

            prepared.iter_request_dependencies = extra
            yield prepared

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("Credential access before prepared-request validation")

    monkeypatch.setattr(preparation, "prepare_run", tampered)
    monkeypatch.setattr(provider, "_resolve_credentials", forbidden)
    with pytest.raises(q.QualificationError):
        await e.execute_plan(directory, **arguments)
    assert not list((e._state_directory(directory) / "forward-intents").iterdir())


def test_concurrent_guards_cannot_consume_the_same_allocation_twice(
    planned: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    directory, plan, arguments = planned
    e._authorize(directory, plan, arguments["authorized_plan_sha256"])
    guard = e._QualificationSubmissionGuard(
        directory, plan, plan.calls[0], token=e._EXECUTOR_TOKEN
    )
    wire = wire_for_plan(directory, plan)
    barrier = Barrier(2)
    original = e._consumed

    def synchronized(*args: Any, **kwargs: Any) -> set[str]:
        result = original(*args, **kwargs)
        barrier.wait(timeout=10)
        return result

    monkeypatch.setattr(e, "_consumed", synchronized)

    def attempt() -> bool:
        try:
            guard(wire)
        except provider.ProviderIntegrityError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = [
            future.result(timeout=15)
            for future in (pool.submit(attempt), pool.submit(attempt))
        ]
    assert sorted(outcomes) == [False, True]
    assert original(directory, plan, arguments["authorized_plan_sha256"]) == {
        "rubric-text"
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "inconsistency",
    [
        "missing_transport",
        "extra_attempt",
        "missing_evidence",
        "unconsumed",
        "missing_response",
        "observation_identity",
        "missing_diagnostic",
        "usage_sdk",
        "validation_flag",
    ],
)
async def test_report_rejects_inconsistent_independent_durable_views(
    planned: Any, monkeypatch: pytest.MonkeyPatch, inconsistency: str
) -> None:
    from judgmentllm.persistence.repository import ReadOnlyRepository

    directory, plan, arguments = planned
    install(monkeypatch, directory, plan)
    await e.execute_plan(directory, **arguments)

    def unavailable(*args: Any, **kwargs: Any) -> None:
        return None

    if inconsistency == "missing_transport":
        monkeypatch.setattr(e, "read_transport_commitment", unavailable)
    elif inconsistency == "extra_attempt":
        original_summary = ReadOnlyRepository.summary

        def summary(self: Any, *args: Any, **kwargs: Any) -> Any:
            return original_summary(self, *args, **kwargs).model_copy(
                update={"attempts": 2}
            )

        monkeypatch.setattr(ReadOnlyRepository, "summary", summary)
    elif inconsistency in {
        "missing_evidence",
        "missing_diagnostic",
        "usage_sdk",
        "validation_flag",
    }:
        original_evidence = ReadOnlyRepository.iter_evidence

        def evidence(self: Any, *args: Any, **kwargs: Any) -> Any:
            if inconsistency == "missing_evidence":
                return iter(())
            forged: list[Any] = []
            for receipt in original_evidence(self, *args, **kwargs):
                if inconsistency == "usage_sdk":
                    updated = receipt.model_copy(
                        update={
                            "usage": receipt.usage.model_copy(
                                update={"sdk_version": "2.35.0"}
                            )
                        }
                    )
                elif inconsistency == "validation_flag":
                    diagnostic = receipt.assessment_diagnostic
                    assert diagnostic is not None
                    updated = receipt.model_copy(
                        update={
                            "assessment_diagnostic": diagnostic.model_copy(
                                update={
                                    "canonical_valid": False,
                                    "parser_diagnostic": "json_syntax",
                                }
                            )
                        }
                    )
                else:
                    updated = receipt.model_copy(update={"assessment_diagnostic": None})
                forged.append(updated)
            return iter(forged)

        monkeypatch.setattr(ReadOnlyRepository, "iter_evidence", evidence)
    elif inconsistency == "missing_response":
        monkeypatch.setattr(e, "read_receipt", unavailable)
    elif inconsistency == "observation_identity":
        original_response = e.read_receipt

        def response(*args: Any, **kwargs: Any) -> Any:
            receipt = original_response(*args, **kwargs)
            assert receipt is not None
            return receipt.model_copy(update={"observation_key": "unexpected"})

        monkeypatch.setattr(e, "read_receipt", response)
    row = e._case_report(directory, plan, plan.calls[0], inconsistency != "unconsumed")
    assert row["evidence_integrity_failure"] and not row["assessment_valid"]
    assert not row["assessment_continuation_allowed"] and not row["live_qualified"]
    before = files(directory)
    assert (
        e._case_report(directory, plan, plan.calls[0], inconsistency != "unconsumed")
        == row
    )
    assert files(directory) == before


@pytest.mark.parametrize(
    "context_kind,grant_kind",
    [("production", "exact"), ("production", "generic"), ("candidate", "generic")],
)
def test_qualification_authority_cannot_be_borrowed_by_production_or_generic_guards(
    planned: Any,
    monkeypatch: pytest.MonkeyPatch,
    context_kind: str,
    grant_kind: str,
) -> None:
    from judgmentllm.vertex_transport import _qualification_context, production_context

    directory, plan, arguments = planned
    e._authorize(directory, plan, arguments["authorized_plan_sha256"])
    spec = q._specification(
        directory, "rubric", "text", plan.max_output_tokens_per_case
    )
    context = (
        production_context(spec)
        if context_kind == "production"
        else _qualification_context(
            spec, source_commit=plan.source_commit, runtime=plan.runtime()
        )
    )

    def generic_guard(prepared: Any) -> None:
        return None

    guard = (
        e._QualificationSubmissionGuard(
            directory, plan, plan.calls[0], token=e._EXECUTOR_TOKEN
        )
        if grant_kind == "exact"
        else generic_guard
    )

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(
            "Unauthorized authority must fail before credentials or SDK"
        )

    monkeypatch.setattr(provider, "_resolve_credentials", forbidden)
    monkeypatch.setattr(provider, "_sdk", forbidden)
    adapter = provider.VertexAdapter(
        spec.endpoint,
        spec.model,
        context=context,
        concurrency=1,
        submission_guard=guard,
    )
    with pytest.raises(provider.ProviderIntegrityError):
        adapter.validate_credentials()
    assert not e._consumed(directory, plan, arguments["authorized_plan_sha256"])
    assert not (e._state_directory(directory) / "independent-forwards").exists()


@pytest.mark.asyncio
async def test_readonly_report_digests_and_private_plan_preservation(
    planned: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from test_vertex_qualification import no_credentials

    directory, plan, arguments = planned
    untouched = files(directory)
    with no_credentials(monkeypatch):
        before = e.report_plan(
            directory, expected_plan_sha256=arguments["authorized_plan_sha256"]
        )
    assert before["authorized_requests"] == before["attempted_requests"] == 0
    assert all(
        value is None
        for value in cast(dict[str, Any], before["reported_usage_totals"]).values()
    )
    assert before["estimated_actual_usd"] is None
    install(monkeypatch, directory, plan)
    result = await e.execute_plan(directory, **arguments)
    retained = files(directory)
    assert {k: v for k, v in retained.items() if k.startswith("plan/")} == untouched
    stamps = {
        str(p): p.stat().st_mtime_ns for p in directory.parent.rglob("*") if p.is_file()
    }
    with no_credentials(monkeypatch):
        assert (
            e.report_plan(
                directory, expected_plan_sha256=arguments["authorized_plan_sha256"]
            )
            == result
        )
    assert files(directory) == retained
    assert stamps == {
        str(p): p.stat().st_mtime_ns for p in directory.parent.rglob("*") if p.is_file()
    }
    content = dict(result)
    digest = content.pop("report_sha256")
    assert digest == bytes_digest(canonical_bytes(content))
    for row in cast(list[dict[str, Any]], result["calls"]):
        fields = dict(row)
        assert fields.pop("provenance_sha256") == bytes_digest(canonical_bytes(fields))
        assert row["assessment_sha256"] and row["response_receipt_sha256"]
        assert row["evidence_receipt_sha256"] and row["retained_response_sha256"]
        if row["case_id"].startswith("pairwise"):
            assert row["original_order"] == ["001", "002"]
            assert row["presented_order"] == ["002", "001"]
    assert result["confirmed_accepted_requests"] == result["actual_http_forwards"] == 4
    assert result["successful_assessments"] == result["exact_model_matches"] == 4
    assert result["successful_text_cases"] == result["successful_native_cases"] == 2
    assert PROJECT not in json.dumps(result)
    assert "The seed grew" not in json.dumps(result)


@pytest.mark.asyncio
async def test_plan_lock_excludes_concurrent_executors(
    planned: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory, plan, arguments = planned
    sent = install(monkeypatch, directory, plan)
    entered, release = asyncio.Event(), asyncio.Event()
    original = e._execute_authorized

    async def paused(*args: Any, **kwargs: Any) -> dict[str, object]:
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(e, "_execute_authorized", paused)
    task = asyncio.create_task(e.execute_plan(directory, **arguments))
    await asyncio.wait_for(entered.wait(), timeout=10)
    try:
        with pytest.raises(q.QualificationError):
            await e.execute_plan(directory, **arguments)
        assert not sent
    finally:
        release.set()
    assert (await asyncio.wait_for(task, timeout=30))["live_qualified"]
    assert len(sent) == 4


@pytest.mark.asyncio
async def test_cancelled_forward_stays_uncertain_without_replacement(
    planned: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory, _plan, arguments = planned
    entered = asyncio.Event()
    never = asyncio.Event()
    sent: list[bytes] = []

    async def blocked(request: Any) -> Any:
        sent.append(request.content)
        entered.set()
        await never.wait()
        raise AssertionError("Cancelled transport cannot complete")

    def transport(**kwargs: Any) -> Any:
        return httpx2.MockTransport(blocked)

    monkeypatch.setattr(httpx2, "AsyncHTTPTransport", transport)
    task = asyncio.create_task(e.execute_plan(directory, **arguments))
    await asyncio.wait_for(entered.wait(), timeout=10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=10)
    result = await e.execute_plan(directory, **arguments)
    assert (
        len(sent) == result["attempted_requests"] == result["uncertain_outcomes"] == 1
    )
    assert (
        result["actual_http_forwards"] is None
        and result["confirmed_accepted_requests"] == 0
    )
    assert not result["live_qualified"] and result["estimated_actual_usd"] is None
    assert (
        cast(list[dict[str, Any]], result["calls"])[0]["terminal_state"]
        == "uncertain_submission"
    )


@pytest.mark.asyncio
async def test_missing_selected_credential_cannot_borrow_another_source(
    planned: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from google import auth

    directory, plan, arguments = planned
    sent = install(monkeypatch, directory, plan)
    if plan.authentication_mode == "api_key":
        monkeypatch.delenv("JUDGMENTLLM_VERTEX_API_KEY")
    else:

        def unavailable(*args: Any, **kwargs: Any) -> Any:
            raise ValueError("PRIVATE_ADC_FAILURE")

        monkeypatch.setattr(auth, "default", unavailable)
    try:
        await e.execute_plan(directory, **arguments)
    except Exception as error:
        assert "PRIVATE_ADC_FAILURE" not in str(error)
    assert not sent
    assert not e._consumed(directory, plan, arguments["authorized_plan_sha256"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    ["body", "url", "method", "x-goog-api-key", "authorization", "x-goog-user-project"],
)
async def test_final_http_guard_denies_altered_bytes_route_or_credentials(
    planned: Any, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    directory, plan, arguments = planned
    sent = install(monkeypatch, directory, plan)
    original = provider.VertexAdapter._execution_client

    async def client(self: Any) -> Any:
        result = await original(self)

        async def mutate(request: Any) -> None:
            if change == "body":
                request._content = b"{}"
            elif change == "url":
                request.url = httpx2.URL("https://forbidden.invalid/")
            elif change == "method":
                request.method = "GET"
            else:
                request.headers[change] = "PRIVATE_MISMATCH"

        result._judgmentllm_http_client.event_hooks["request"].append(mutate)
        return result

    monkeypatch.setattr(provider.VertexAdapter, "_execution_client", client)
    with pytest.raises(Exception) as failure:
        await e.execute_plan(directory, **arguments)
    assert "PRIVATE_MISMATCH" not in str(failure.value)
    assert not sent and not e._consumed(
        directory, plan, arguments["authorized_plan_sha256"]
    )


def test_authority_is_not_transferable_between_authentication_modes(
    planned: Any, tmp_path: Path
) -> None:
    directory, plan, arguments = planned
    alternative = tmp_path / "alternative"
    other = q.build_plan(
        directory=alternative,
        source_commit=plan.source_commit,
        project=PROJECT,
        authentication_mode="adc"
        if plan.authentication_mode == "api_key"
        else "api_key",
    )
    assert bytes_digest(canonical_bytes(other)) != arguments["authorized_plan_sha256"]
    assert other.route_sha256 != plan.route_sha256
    e._authorize(directory, plan, arguments["authorized_plan_sha256"])
    guard = e._QualificationSubmissionGuard(
        directory, plan, plan.calls[0], token=e._EXECUTOR_TOKEN
    )
    with pytest.raises(provider.ProviderIntegrityError):
        guard(wire_for_plan(alternative, other))
    with pytest.raises(provider.ProviderIntegrityError):
        guard.verify_context(
            e._qualification_context(
                q._specification(alternative, "rubric", "text", 512),
                source_commit=plan.source_commit,
                runtime=other.runtime(),
            )
        )
    assert not e._consumed(directory, plan, arguments["authorized_plan_sha256"])


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["api_key", "adc"])
@pytest.mark.parametrize("change", ["dirty", "wrong_head", "after_forward"])
async def test_actual_git_source_admission_and_boundary_recheck(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, change: str
) -> None:
    from judgmentllm import qualification

    root = tmp_path / "synthetic-git"
    root.mkdir()
    environment = {
        name: os.environ[name]
        for name in ("PATH", "SystemRoot", "WINDIR", "TEMP", "TMP")
        if name in os.environ
    }
    environment.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_AUTHOR_NAME="Synthetic Offline Operator",
        GIT_AUTHOR_EMAIL="synthetic@example.invalid",
        GIT_COMMITTER_NAME="Synthetic Offline Operator",
        GIT_COMMITTER_EMAIL="synthetic@example.invalid",
    )

    def git(*arguments: str) -> str:
        return subprocess.run(
            ["git", *arguments],
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    git("init", "-q")
    sentinel = root / "synthetic.txt"
    sentinel.write_text("original synthetic committed source")
    git("add", "synthetic.txt")
    git("commit", "-qm", "Synthetic qualification source")
    source = git("rev-parse", "HEAD")
    monkeypatch.setattr(
        qualification,
        "__file__",
        str(root / "src" / "judgmentllm" / "qualification.py"),
    )
    monkeypatch.setattr(q, "_source_commit", qualification._source_commit)
    monkeypatch.setattr(e, "_source_commit", qualification._source_commit)
    directory = tmp_path / "actual-source-plan"
    plan = q.build_plan(
        directory=directory,
        source_commit=source,
        project=PROJECT,
        authentication_mode=cast(Any, mode),
    )
    if mode == "adc":
        adc(monkeypatch)
    arguments: dict[str, Any] = dict(
        authorized_plan_sha256=bytes_digest(canonical_bytes(plan)),
        authorized_source_commit=source,
        authorized_project=PROJECT,
        authorized_authentication_mode=mode,
        authorized_location="global",
        reviewed_request_count=4,
        reviewed_reserved_estimated_usd=plan.reserved_estimated_usd,
        reviewed_estimated_ceiling_usd=Decimal("0.02"),
        accept_billable_calls=True,
        accept_full_synthetic_capture=True,
    )

    def changed(body: dict[str, Any], index: int) -> None:
        if change == "after_forward":
            sentinel.write_text("modified after first HTTP forward")

    sent = install(monkeypatch, directory, plan, changed)
    if change in {"dirty", "wrong_head"}:
        sentinel.write_text("changed committed source")
        if change == "wrong_head":
            git("add", "synthetic.txt")
            git("commit", "-qm", "Different synthetic source")
    with pytest.raises((ValueError, ExceptionGroup)):
        await e.execute_plan(directory, **arguments)
    assert len(sent) == (1 if change == "after_forward" else 0)
    assert not (
        e._state_directory(directory) / "forward-intents" / "rubric-native.json"
    ).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "damage", ["rubric_selection", "pairwise_selection", "provider", "type"]
)
async def test_rehashed_valid_but_inconsistent_normalized_results_fail_inspection(
    planned: Any, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    from judgmentllm.persistence.repository import ReadOnlyRepository
    from judgmentllm.results import PairwiseResult, RubricResult

    directory, plan, arguments = planned
    install(monkeypatch, directory, plan)
    await e.execute_plan(directory, **arguments)
    original = ReadOnlyRepository.iter_results

    def forged(self: Any, *args: Any, **kwargs: Any) -> Any:
        for result in original(self, *args, **kwargs):
            if damage == "rubric_selection" and isinstance(result, RubricResult):
                result = result.model_copy(
                    update={
                        "category_label": "low",
                        "numeric_category_value": 1.0,
                        "primary_score": 1.0,
                    }
                )
            elif damage == "pairwise_selection" and isinstance(result, PairwiseResult):
                result = result.model_copy(update={"winner_id": "001"})
            elif damage == "provider":
                result = result.model_copy(update={"provider": "gemini"})
            elif damage == "type" and isinstance(result, RubricResult):
                result = PairwiseResult(
                    run_id=result.run_id,
                    task_id=result.task_id,
                    request_uid=result.request_uid,
                    attempt_id=result.attempt_id,
                    provider="vertex",
                    model=plan.model,
                    endpoint=plan.endpoint,
                    pair_id="001",
                    pair_uid="001",
                    trait_id="01",
                    left_item_id="001",
                    right_item_id="002",
                    original_order=["001", "002"],
                    presented_order=["002", "001"],
                    winner_id="002",
                    tie=False,
                )
            yield result

    monkeypatch.setattr(ReadOnlyRepository, "iter_results", forged)
    report = e.report_plan(
        directory, expected_plan_sha256=arguments["authorized_plan_sha256"]
    )
    assert not report["live_qualified"]
    assert any(
        row["evidence_integrity_failure"]
        for row in cast(list[dict[str, Any]], report["calls"])
    )


def test_load_race_and_sidecar_symlinks_fail_without_credentials(
    planned: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory, _plan, arguments = planned
    original = e.verify_plan

    def changed(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        (directory / "plan.json").write_bytes(b"{}")
        return result

    monkeypatch.setattr(e, "verify_plan", changed)
    with pytest.raises(q.QualificationError):
        e.report_plan(
            directory, expected_plan_sha256=arguments["authorized_plan_sha256"]
        )
    original_symlink = Path.is_symlink
    anchor = directory.with_name(directory.name + ".authorization.json")
    state = directory.with_name(directory.name + ".execution")
    for target in (anchor, state):

        def symbolic(path: Path, selected: Path = target) -> bool:
            return path == selected or original_symlink(path)

        monkeypatch.setattr(Path, "is_symlink", symbolic)
        with pytest.raises(q.QualificationError):
            (e._authorization_anchor if target == anchor else e._state_directory)(
                directory
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("quota", [None, "synthetic-other-quota-project"])
async def test_adc_quota_project_requires_exact_reviewed_project(
    planned: Any, monkeypatch: pytest.MonkeyPatch, quota: str | None
) -> None:
    directory, plan, arguments = planned
    credentials, _ = adc(monkeypatch)
    credentials._quota_project_id = quota
    sent = install(monkeypatch, directory, plan)
    if plan.authentication_mode == "adc":
        with pytest.raises(ExceptionGroup):
            await e.execute_plan(directory, **arguments)
        assert not sent and not e._consumed(
            directory, plan, arguments["authorized_plan_sha256"]
        )
    else:
        assert (await e.execute_plan(directory, **arguments))["live_qualified"]
        assert len(sent) == 4


def test_nested_sidecar_symlinks_and_dirty_report_end_fail_closed(
    planned: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory, plan, arguments = planned
    monkeypatch.setattr(e, "_source_commit", lambda: "b" * 40)
    with pytest.raises(q.QualificationError):
        e.report_plan(
            directory, expected_plan_sha256=arguments["authorized_plan_sha256"]
        )

    e._authorize(directory, plan, arguments["authorized_plan_sha256"])
    target = e._state_directory(directory) / "nested-link"
    target.write_bytes(b"untrusted")
    original = Path.is_symlink

    def symbolic(path: Path) -> bool:
        return path == target or original(path)

    monkeypatch.setattr(Path, "is_symlink", symbolic)
    with pytest.raises(q.QualificationError):
        e.report_plan(
            directory, expected_plan_sha256=arguments["authorized_plan_sha256"]
        )


def test_valid_assessment_without_corresponding_normalized_result_is_rejected(
    planned: Any,
) -> None:
    directory, plan, _arguments = planned
    with pytest.raises(q.QualificationError):
        e._verify_assessment(
            directory,
            plan,
            plan.calls[0],
            b'{"scores":[{"trait_id":"01","category_label":"high"}]}',
            (),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["reasoning", "cache", "all_but_input"])
async def test_known_usage_overage_stops_even_with_incomplete_charge_metadata(
    planned: Any, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    directory, plan, arguments = planned

    def expensive(body: dict[str, Any], index: int) -> None:
        usage = body["usageMetadata"]
        usage["promptTokenCount"] = 100000
        usage.pop("totalTokenCount")
        if missing == "reasoning":
            usage.pop("thoughtsTokenCount")
        elif missing == "cache":
            usage.pop("cachedContentTokenCount")
        else:
            body["usageMetadata"] = {"promptTokenCount": 100000}

    sent = install(monkeypatch, directory, plan, expensive)
    report = await e.execute_plan(directory, **arguments)
    assert len(sent) == 1
    assert report["estimated_actual_usd"] is None
    assert Decimal(str(report["estimated_exposure_usd"])) > Decimal("0.02")
    assert (
        cast(dict[str, Any], report["reported_usage_totals"])["input_tokens"] == 100000
    )
