"""Offline Vertex seals: exact SDK bytes, no credentials, tamper/authority denial."""

from __future__ import annotations

# pyright: reportPrivateUsage=false
import asyncio
import json
import os
import sys
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, cast

import httpx2
import pytest
from vertex_offline import adc, envelope, isolate

from judgmentllm import _auth, gemini_qualification_execution
from judgmentllm import vertex_qualification as q
from judgmentllm import vertex_qualification_execution as executor
from judgmentllm import vertex_qualification_sdk as sdk_probe
from judgmentllm.persistence.hashing import bytes_digest, canonical_bytes
from judgmentllm.providers import vertex as adapter
from judgmentllm.providers.protocol import ProviderIntegrityError
from judgmentllm.vertex_transport import PreparedRequestV7, _qualification_context
from scripts import qualify_vertex as cli

PROJECT = "synthetic-private-project"
SOURCE = "a" * 40


def _files(root: Path) -> dict[str, tuple[str, int, int]]:
    return {
        str(p.relative_to(root)): (
            bytes_digest(p.read_bytes()),
            p.stat().st_mtime_ns,
            p.stat().st_size,
        )
        for p in root.rglob("*")
        if p.is_file()
    }


def _deny(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError(
        "No credentials, clients, discovery, subprocesses or provider I/O allowed"
    )


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    isolate(monkeypatch)
    monkeypatch.setattr(q, "_source_commit", lambda: SOURCE)
    monkeypatch.setattr(executor, "_source_commit", lambda: SOURCE)


@pytest.fixture(params=["api_key", "adc"])
def mode(request: pytest.FixtureRequest) -> Literal["api_key", "adc"]:
    return request.param


@contextmanager
def no_credentials(monkeypatch: pytest.MonkeyPatch) -> Generator[None]:
    from google import genai

    from judgmentllm import vertex_config

    original_getitem = type(os.environ).__getitem__

    def getitem(self: Any, name: str) -> str:
        if name in {
            "GOOGLE_API_KEY",
            "GEMINI_API_KEY",
            "JUDGMENTLLM_VERTEX_API_KEY",
            "GOOGLE_APPLICATION_CREDENTIALS",
        }:
            return _deny()
        return original_getitem(self, name)

    with monkeypatch.context() as guard:
        guard.setattr(type(os.environ), "__getitem__", getitem)
        guard.setattr(_auth, "_resolve_credentials", _deny)
        guard.setattr(vertex_config, "runtime_configuration", _deny)
        guard.setattr(genai, "Client", _deny)
        guard.setattr(cli.subprocess, "run", _deny)
        yield


@pytest.fixture
def plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: Literal["api_key", "adc"]
) -> Path:
    directory = tmp_path / mode
    with no_credentials(monkeypatch):
        q.build_plan(
            directory=directory,
            source_commit=SOURCE,
            project=PROJECT,
            authentication_mode=mode,
        )
    return directory


def _digest(directory: Path) -> str:
    return (directory / "plan.sha256").read_text().strip()


def test_complete_plan_and_private_readonly_report(
    plan: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    before = _files(plan)
    with no_credentials(monkeypatch):
        report = q.verify_plan(plan, expected_plan_sha256=_digest(plan))
    assert _files(plan) == before
    public = json.dumps(report)
    assert PROJECT not in public and str(plan) not in public
    assert report["authentication_mode"] == mode and report["location"] == "global"
    assert report["account_eligibility"] == "unverified"
    assert report["planned_requests"] == 4 and report["authorized_requests"] == 0
    assert report["actual_usage_and_cost"] is None
    assert not report["execution_enabled"] and not report["live_qualified"]
    assert Decimal(str(report["reserved_estimated_usd"])) <= Decimal("0.02")
    record = q.VertexQualificationPlanV2.model_validate_json(
        (plan / "plan.json").read_bytes()
    )
    assert (
        record.publisher_model_resource
        == f"projects/{PROJECT}/locations/global/publishers/google/models/{q.MODEL}"
    )
    assert record.reserved_output_tokens_including_thinking == 2048
    assert len({c.transport.identity.submission_id for c in record.calls}) == 4
    for call in record.calls:
        body = json.loads(call.request_body_utf8)
        config = body["generationConfig"]
        assert config["maxOutputTokens"] == 512 and config["candidateCount"] == 1
        assert set(body) == {"contents", "generationConfig"}
        assert ("responseJsonSchema" in config) == (call.mode == "native")
        assert "responseSchema" not in config and "thinkingConfig" not in config
        assert ('"additionalProperties":false' in call.rendered_prompt_utf8) == (
            call.mode == "text"
        )
        allowance = call.input_allowance
        assert allowance.complete_body_utf8_bytes == len(
            call.request_body_utf8.encode()
        )
        assert (
            allowance.estimated_input_tokens
            == (
                (
                    allowance.complete_body_utf8_bytes
                    + allowance.additional_schema_utf8_bytes
                    + 4096
                    + 1023
                )
                // 1024
            )
            * 1024
        )
        policy = call.transport.request_policy
        assert policy.sdk_retries == 0 and policy.automatic_continuation is False
        assert policy.automatic_function_calling is False
    if os.name != "nt":
        assert plan.stat().st_mode & 0o777 == 0o700
        assert all(
            p.stat().st_mode & 0o777 == 0o600 for p in plan.rglob("*") if p.is_file()
        )
    with pytest.raises(FileExistsError):
        q.build_plan(
            directory=plan,
            source_commit=SOURCE,
            project=PROJECT,
            authentication_mode=record.authentication_mode,
        )
    assert _files(plan) == before


def _rehash(plan: Path, data: dict[str, Any]) -> str:
    raw = canonical_bytes(data)
    (plan / "plan.json").write_bytes(raw)
    digest = bytes_digest(raw)
    (plan / "plan.sha256").write_text(digest + "\n")
    return digest


@pytest.mark.parametrize(
    "mutation",
    [
        "region",
        "project",
        "mode",
        "model",
        "api",
        "sdk",
        "provider",
        "endpoint",
        "schema",
        "payload",
        "prompt",
        "source",
        "tokens",
        "input",
        "budget",
        "output",
        "submission",
        "correlation",
        "attempt",
        "duplicate-physical",
        "duplicate-json",
        "config",
        "input-file",
        "authority",
        "calls",
        "sdk-source",
        "pricing",
        "digest",
        "credential-source",
        "run",
        "version",
        "candidate",
        "route",
        "url",
    ],
)
def test_rehashed_adversarial_plan_is_rejected(plan: Path, mutation: str) -> None:
    original_digest = _digest(plan)
    data = json.loads((plan / "plan.json").read_bytes())
    call = data["calls"][0]
    if mutation == "region":
        data["location"] = "us-central1"
    elif mutation == "project":
        data["project"] = "synthetic-other-project"
        data["publisher_model_resource"] = data["publisher_model_resource"].replace(
            PROJECT, data["project"]
        )
    elif mutation == "mode":
        data["authentication_mode"] = (
            "adc" if data["authentication_mode"] == "api_key" else "api_key"
        )
    elif mutation in {"model", "api", "sdk", "provider", "endpoint"}:
        data[{"api": "api_version", "sdk": "sdk_version"}.get(mutation, mutation)] = (
            "wrong"
        )
    elif mutation in {"schema", "payload", "candidate"}:
        target = data["calls"][1] if mutation == "schema" else call
        body = json.loads(target["request_body_utf8"])
        if mutation == "schema":
            body["generationConfig"]["responseJsonSchema"] = {"type": "object"}
        elif mutation == "candidate":
            body["generationConfig"]["candidateCount"] = 2
        else:
            body["contents"][0]["parts"][0]["text"] = "forged instruction"
        target["request_body_utf8"] = json.dumps(body)
        target["transport"]["payload_sha256"] = bytes_digest(
            target["request_body_utf8"].encode()
        )
    elif mutation == "prompt":
        call["rendered_prompt_utf8"] = "changed"
    elif mutation == "source":
        data["source_commit"] = "b" * 40
    elif mutation == "tokens":
        data["max_output_tokens_per_case"] = 513
    elif mutation == "input":
        data["reserved_input_tokens"] += 1024
    elif mutation == "budget":
        data["proposed_estimated_authorization_ceiling_usd"] = "0.03"
    elif mutation == "output":
        data["reserved_output_tokens_including_thinking"] += 1
    elif mutation in {"submission", "correlation", "attempt"}:
        call["transport"]["identity"][mutation + "_id"] = "forged"
    elif mutation == "duplicate-physical":
        data["calls"][1]["transport"]["identity"] = call["transport"]["identity"]
    elif mutation == "config":
        (plan / "rubric-text.json").write_bytes(b"{}")
    elif mutation == "input-file":
        (plan / "responses.jsonl").write_bytes(b"{}\n")
    elif mutation == "authority":
        data["authorized_requests"] = 4
    elif mutation == "calls":
        data["calls"] = data["calls"][:3]
    elif mutation == "sdk-source":
        data["installed_sdk_source_sha256"]["client.py"] = "0" * 64
    elif mutation == "pricing":
        data["pricing_evidence_sha256"] = "0" * 64
    elif mutation == "credential-source":
        data["credential_source"] = (
            "google_adc"
            if data["credential_source"] != "google_adc"
            else "JUDGMENTLLM_VERTEX_API_KEY"
        )
    elif mutation == "run":
        call["run_id"] = "forged"
    elif mutation == "version":
        data["plan_version"] = 2.0
    elif mutation == "route":
        data["route_sha256"] = "0" * 64
    elif mutation == "url":
        call["request_url_sha256"] = "0" * 64
    digest = _rehash(plan, data)
    if mutation == "digest":
        (plan / "plan.sha256").write_text("0" * 64 + "\n")
    if mutation == "duplicate-json":
        raw = (plan / "plan.json").read_bytes()
        raw = b'{"plan_version":2,' + raw[1:]
        (plan / "plan.json").write_bytes(raw)
        digest = bytes_digest(raw)
        (plan / "plan.sha256").write_text(digest + "\n")
    before = _files(plan)
    # Also supply a forged external digest: structural reconstruction still
    # rejects changes even if a caller wrongly trusts the adjacent digest file.
    with pytest.raises(ValueError):
        q.verify_plan(plan, expected_plan_sha256=digest)
    assert _files(plan) == before
    with pytest.raises(ValueError):
        q.verify_plan(plan, expected_plan_sha256=original_digest)


def test_modes_and_projects_have_independent_physical_identities(
    tmp_path: Path,
) -> None:
    plans = [
        q.build_plan(
            directory=tmp_path / str(index),
            source_commit=SOURCE,
            project=project,
            authentication_mode=cast(Literal["api_key", "adc"], mode),
        )
        for index, (project, mode) in enumerate(
            (
                (PROJECT, "api_key"),
                (PROJECT, "adc"),
                ("synthetic-other-project", "api_key"),
            )
        )
    ]
    assert len({p.route_sha256 for p in plans}) == 3
    assert (
        len({c.transport.identity.submission_id for p in plans for c in p.calls}) == 12
    )
    assert (
        len({c.transport.identity.correlation_id for p in plans for c in p.calls}) == 12
    )
    assert len({p.reserved_estimated_usd for p in plans}) == 1


@pytest.mark.asyncio
async def test_all_four_global_requests_equal_actual_intercepted_sdk_http(
    plan: Path, mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = q.VertexQualificationPlanV2.model_validate_json(
        (plan / "plan.json").read_bytes()
    )
    wires: list[PreparedRequestV7] = []
    original = q.sdk_request

    def capture(wire: PreparedRequestV7, runtime: Any) -> tuple[str, bytes]:
        wires.append(wire)
        return original(wire, runtime)

    monkeypatch.setattr(q, "sdk_request", capture)
    q.verify_plan(plan, expected_plan_sha256=_digest(plan))
    assert len(wires) == 4
    credentials = None
    if mode == "adc":
        credentials, _ = adc(monkeypatch)
    sdk, http = adapter._sdk()
    for wire in wires:
        sent: list[bytes] = []

        def handler(
            request: httpx2.Request,
            wire: PreparedRequestV7 = wire,
            sent: list[bytes] = sent,
        ) -> httpx2.Response:
            assert (
                str(request.url)
                == record.base_url
                + "v1beta1/"
                + record.publisher_model_resource
                + ":generateContent"
            )
            assert request.content == wire.payload
            if mode == "api_key":
                assert request.headers["x-goog-api-key"] == "SYNTHETIC_VERTEX_KEY"
                assert "authorization" not in request.headers
            else:
                assert (
                    request.headers["authorization"]
                    == "Bearer SYNTHETIC_ADC_ACCESS_TOKEN"
                )
                assert "x-goog-api-key" not in request.headers
            sent.append(request.content)
            return httpx2.Response(200, json=envelope('{"winner_id":"001"}'))

        client = adapter._protected_client(
            sdk,
            http,
            runtime=record.runtime(),
            key="SYNTHETIC_VERTEX_KEY" if mode == "api_key" else None,
            http_client=httpx2.AsyncClient(
                transport=httpx2.MockTransport(handler),
                trust_env=False,
                follow_redirects=False,
            ),
        )
        # Supply the synthetic ADC object directly. No discovery/refresh, even
        # synthetic discovery, is needed to prove route/body/header equivalence.
        client._api_client._credentials = credentials
        try:
            await asyncio.wait_for(
                client.aio.models.generate_content(**adapter._sdk_arguments(sdk, wire)),
                timeout=10,
            )
            assert sent == [wire.payload]
        finally:
            await adapter._close_client(client)


def test_candidate_cannot_execute_or_forge_production_native_authority(
    plan: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from judgmentllm import vertex_registry

    reviewed_profiles = vertex_registry._QUALIFIED_PROFILES
    assert len(reviewed_profiles) == 2
    assert {profile.authentication_mode for profile in reviewed_profiles} == {
        "adc",
        "api_key",
    }
    record = q.VertexQualificationPlanV2.model_validate_json(
        (plan / "plan.json").read_bytes()
    )
    wires: list[PreparedRequestV7] = []
    original = q.sdk_request

    def capture(wire: PreparedRequestV7, runtime: Any) -> tuple[str, bytes]:
        wires.append(wire)
        return original(wire, runtime)

    monkeypatch.setattr(q, "sdk_request", capture)
    q.verify_plan(plan, expected_plan_sha256=_digest(plan))
    assert vertex_registry._QUALIFIED_PROFILES == reviewed_profiles
    for wire, call in zip(wires, record.calls, strict=True):
        spec = q._specification(plan, call.task_kind, call.mode, 512)
        context = _qualification_context(
            spec, source_commit=SOURCE, runtime=record.runtime()
        )
        assert (
            vertex_registry._profile_for(
                context.scope,
                spec.prompt.native_mechanism,
                route_sha256=context.runtime.route_sha256,
                authentication_mode=context.runtime.auth_mode,
                location=context.runtime.location,
            )
            is None
        )
        contexts = [context]
        if call.mode == "native":
            contexts.append(
                replace(context, kind="vertex-production-v1", source_commit=None)
            )
        for candidate in contexts:
            provider = adapter.VertexAdapter(
                spec.endpoint, spec.model, context=candidate
            )
            with pytest.raises(ProviderIntegrityError):
                asyncio.run(
                    provider.submit(
                        wire,
                        submission_id=wire.transport_commitment.identity.submission_id,
                        correlation_id=wire.transport_commitment.identity.correlation_id,
                    )
                )
            assert provider._client is None and provider.actual_call_count == 0
    with pytest.raises(ValueError):
        asyncio.run(
            gemini_qualification_execution.execute_plan(
                plan,
                authorized_plan_sha256=_digest(plan),
                authorized_source_commit=SOURCE,
                accept_billable_calls=True,
                accept_full_synthetic_capture=True,
            )
        )
    assert not (plan / "execution-authorization.json").exists()


@pytest.mark.asyncio
async def test_plan_is_not_an_ordinary_run_or_resumable_production_store(
    plan: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from judgmentllm.lifecycle import LifecycleError, aresume_run, arun_live

    record = q.VertexQualificationPlanV2.model_validate_json(
        (plan / "plan.json").read_bytes()
    )
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", PROJECT)
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "global")
    monkeypatch.setenv("JUDGMENTLLM_VERTEX_AUTH_MODE", record.authentication_mode)
    monkeypatch.setattr(adapter.VertexAdapter, "validate_credentials", _deny)

    with pytest.raises(LifecycleError):
        await arun_live(plan / "plan.json", store=tmp_path / "ordinary")
    for call in record.calls:
        with pytest.raises(LifecycleError):
            await asyncio.wait_for(
                aresume_run(
                    call.run_id, store=plan / "offline-preparation" / call.case_id
                ),
                timeout=10,
            )


def test_original_strict_contracts_reject_ties_explanations_and_unknown_members(
    plan: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from judgmentllm.output_contracts import (
        PairwiseOutputContract,
        RubricOutputContract,
        validate_pairwise_contract_output,
        validate_rubric_contract_output,
    )

    wires: list[PreparedRequestV7] = []
    original = q.sdk_request

    def capture(wire: PreparedRequestV7, runtime: Any) -> tuple[str, bytes]:
        wires.append(wire)
        return original(wire, runtime)

    monkeypatch.setattr(q, "sdk_request", capture)
    q.verify_plan(plan, expected_plan_sha256=_digest(plan))
    for wire in wires:
        contract = wire.output_commitment.contract
        if isinstance(contract, RubricOutputContract):
            assert len(contract.traits) == 1 and contract.traits[0].trait_id == "01"
            validate_rubric_contract_output(
                '{"scores":[{"trait_id":"01","category_label":"high"}]}', contract
            )
            for output in (
                '{"scores":[]}',
                '{"scores":[{"trait_id":"02","category_label":"high"}]}',
                '{"scores":[{"trait_id":"01","category_label":"wrong"}]}',
                '{"scores":[{"trait_id":"01","category_label":"high","explanation":"forbidden"}]}',
            ):
                with pytest.raises(ValueError):
                    validate_rubric_contract_output(output, contract)
        else:
            assert isinstance(contract, PairwiseOutputContract)
            assert (
                contract.pair.presented_order == ["002", "001"]
                and not contract.allow_ties
            )
            validate_pairwise_contract_output('{"winner_id":"001"}', contract)
            for output in (
                '{"winner_id":null,"tie":true}',
                '{"winner_id":"003"}',
                '{"winner_id":"001","explanation":"forbidden"}',
            ):
                with pytest.raises(ValueError):
                    validate_pairwise_contract_output(output, contract)


@pytest.mark.parametrize(
    "settings",
    [
        {"source_commit": "b" * 40},
        {"authentication_mode": "express"},
        {"location": "us-central1"},
        {"model": "other-model"},
        {"project": ""},
        {"max_output_tokens": 513},
        {"max_output_tokens": True},
    ],
)
def test_invalid_admission_does_not_create_evidence(
    tmp_path: Path, settings: dict[str, Any]
) -> None:
    values: dict[str, Any] = (
        dict(
            directory=tmp_path / "bad",
            source_commit=SOURCE,
            project=PROJECT,
            authentication_mode="api_key",
        )
        | settings
    )
    with pytest.raises(ValueError):
        q.build_plan(**values)
    assert not (tmp_path / "bad").exists()


def test_source_and_sdk_identity_must_remain_exact(
    plan: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(q, "_source_commit", lambda: "b" * 40)
    with pytest.raises(ValueError):
        q.verify_plan(plan, expected_plan_sha256=_digest(plan))
    monkeypatch.setattr(q, "_source_commit", lambda: SOURCE)
    original = sdk_probe.importlib.metadata.distribution

    def changed(name: str) -> Any:
        item = original(name)
        return type(
            "WrongSDK", (), {"version": "2.35.0", "locate_file": item.locate_file}
        )()

    monkeypatch.setattr(sdk_probe.importlib.metadata, "distribution", changed)
    with pytest.raises(ValueError):
        q.verify_plan(plan, expected_plan_sha256=_digest(plan))


def test_sdk_source_corruption_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    original = Path.read_bytes

    def changed(path: Path) -> bytes:
        return b"changed source" if path.name == "client.py" else original(path)

    monkeypatch.setattr(Path, "read_bytes", changed)
    with pytest.raises(ValueError, match="source differs"):
        sdk_probe.installed_sdk_evidence()


def test_external_evidence_and_symlink_admission(plan: Path, tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        q.build_plan(
            directory=q.ROOT / "forbidden-plan",
            source_commit=SOURCE,
            project=PROJECT,
            authentication_mode="api_key",
        )
    if os.name == "nt":
        return  # Windows symlink privileges are not required by this test.
    link = tmp_path / "alias"
    link.symlink_to(plan, target_is_directory=True)
    with pytest.raises(ValueError):
        q.verify_plan(link, expected_plan_sha256=_digest(plan))
    (plan / "injected-link").symlink_to(plan / "plan.json")
    with pytest.raises(ValueError):
        q.verify_plan(plan, expected_plan_sha256=_digest(plan))


def test_budget_revision_cannot_silently_raise_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = q._input_allowance

    def large(body: bytes) -> q.InputAllowanceV1:
        return original(body).model_copy(update={"estimated_input_tokens": 100000})

    monkeypatch.setattr(q, "_input_allowance", large)
    with pytest.raises(
        q.ReservationRevisionRequired, match="ceiling was not increased"
    ):
        q.build_plan(
            directory=tmp_path / "over",
            source_commit=SOURCE,
            project=PROJECT,
            authentication_mode="adc",
        )
    assert not (tmp_path / "over" / "plan.json").exists()


def test_reconstruction_rejects_bytes_even_if_sdk_and_local_builder_disagree(
    plan: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forged(*args: Any) -> tuple[str, bytes]:
        return "https://forged.invalid/", b"{}"

    monkeypatch.setattr(q, "sdk_request", forged)
    with pytest.raises(ValueError):
        q.verify_plan(plan, expected_plan_sha256=_digest(plan))


@pytest.mark.parametrize("violation", ["grant", "artifact", "config", "input"])
def test_artifact_inventory_and_reconstruction_are_independent(
    plan: Path, violation: str
) -> None:
    data = json.loads((plan / "plan.json").read_bytes())
    if violation == "grant":
        (plan / "execution-authorization.json").write_bytes(
            b'{"authorized_requests":4}'
        )
    elif violation == "artifact":
        next((plan / "offline-preparation").rglob("*.sqlite3")).write_bytes(b"forged")
    else:
        name = "rubric-text.json" if violation == "config" else "responses.jsonl"
        (plan / name).write_bytes(b"{}")
        # Rehash the entire artifact inventory too: reconstruction still admits
        # only the original fixed synthetic configuration/input contracts.
        data["artifact_sha256"] = q._artifact_hashes(plan)
        _rehash(plan, data)
    before = _files(plan)
    with pytest.raises(ValueError):
        q.verify_plan(plan, expected_plan_sha256=_digest(plan))
    assert _files(plan) == before


def test_source_change_during_preparation_prevents_sealing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = iter([SOURCE, "b" * 40])
    monkeypatch.setattr(q, "_source_commit", lambda: next(source))
    with pytest.raises(ValueError):
        q.build_plan(
            directory=tmp_path / "changing",
            source_commit=SOURCE,
            project=PROJECT,
            authentication_mode="adc",
        )
    assert not (tmp_path / "changing" / "plan.json").exists()


def test_source_change_during_verification_fails_without_evidence_writes(
    plan: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = iter([SOURCE, "b" * 40])
    monkeypatch.setattr(q, "_source_commit", lambda: next(source))
    before = _files(plan)
    with pytest.raises(ValueError):
        q.verify_plan(plan, expected_plan_sha256=_digest(plan))
    assert _files(plan) == before


def test_python_integer_and_credential_source_ingress(plan: Path) -> None:
    model = q.VertexQualificationPlanV2.model_validate_json(
        (plan / "plan.json").read_bytes()
    )
    for field in (
        "plan_version",
        "authorized_requests",
        "planned_requests",
        "concurrency",
        "candidate_count",
    ):
        with pytest.raises(ValueError):
            q.VertexQualificationPlanV2.model_validate(
                dict(model) | {field: float(getattr(model, field))}
            )


def test_candidate_cli_digest_only_and_safe_report(
    plan: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", PROJECT)
    output = tmp_path / "cli-candidate"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "qualify_vertex.py",
            "candidate-plan",
            "--source-commit",
            SOURCE,
            "--auth-mode",
            "api_key",
            "--evidence-dir",
            str(output),
        ],
    )
    with no_credentials(monkeypatch):
        cli.main()
    assert capsys.readouterr().out.strip() == _digest(output)
    for command in ("verify", "report"):
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "qualify_vertex.py",
                command,
                "--evidence-dir",
                str(plan),
                "--plan-sha256",
                _digest(plan),
            ],
        )
        cli.main()
        public = capsys.readouterr().out
        assert PROJECT not in public and json.loads(public)["authorized_requests"] == 0
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "qualify_vertex.py",
            "verify",
            "--evidence-dir",
            str(plan),
            "--plan-sha256",
            "0" * 64,
        ],
    )
    with pytest.raises(SystemExit) as failure:
        cli.main()
    assert failure.value.code == 2 and PROJECT not in capsys.readouterr().err


def test_cli_budget_revision_and_no_execute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(**kwargs: Any) -> Any:
        raise q.ReservationRevisionRequired(Decimal("0.03"))

    monkeypatch.setattr(q, "build_plan", fail)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "qualify_vertex.py",
            "candidate-plan",
            "--source-commit",
            SOURCE,
            "--auth-mode",
            "adc",
            "--evidence-dir",
            str(tmp_path),
        ],
    )
    with pytest.raises(SystemExit):
        cli.main()
    assert "ceiling was not increased" in capsys.readouterr().err
    monkeypatch.setattr(sys, "argv", ["qualify_vertex.py", "execute"])
    with pytest.raises(SystemExit):
        cli.main()


@pytest.mark.parametrize("violation", ["extra_dependencies", "foreign_transport"])
def test_preparation_cannot_expand_case_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, violation: str
) -> None:
    original = q.prepare_run

    @contextmanager
    def tampered(*args: Any, **kwargs: Any) -> Generator[Any]:
        with original(*args, **kwargs) as prepared:
            if violation == "extra_dependencies":
                dependencies = tuple(prepared.iter_request_dependencies())

                def extra(self: Any) -> Iterator[Any]:
                    return iter(dependencies * 2)

                monkeypatch.setattr(
                    type(prepared),
                    "iter_request_dependencies",
                    extra,
                )
            else:

                def foreign(*args: Any) -> object:
                    return object()

                monkeypatch.setattr(
                    type(prepared), "prepare_transport_attempt", foreign
                )
            yield prepared

    monkeypatch.setattr(q, "prepare_run", tampered)
    with pytest.raises(ValueError):
        q.build_plan(
            directory=tmp_path / "bad",
            source_commit=SOURCE,
            project=PROJECT,
            authentication_mode="api_key",
        )
    assert not (tmp_path / "bad" / "plan.json").exists()
