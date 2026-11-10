"""Registry changes preserve source-bound identities by rejecting changed authority."""

from __future__ import annotations

# pyright: reportPrivateUsage=false
from pathlib import Path
from typing import Any

import pytest
from gemini_offline import isolate
from test_gemini_lifecycle import production_spec
from test_gemini_production_acceptance import MODEL, prepare
from test_gemini_qualification_execution import files, install

from judgmentllm import gemini_qualification as q
from judgmentllm import gemini_qualification_execution as e
from judgmentllm import gemini_registry as registry
from judgmentllm.gemini_transport import (
    _qualification_context,
    prepare_production_request,
    production_context,
    production_context_digest,
)
from judgmentllm.persistence.hashing import bytes_digest, canonical_bytes
from judgmentllm.planning import stable_id
from judgmentllm.preparation import PreparationError, open_prepared_run, prepare_run
from judgmentllm.transport import TransportIntegrityError, physical_identity


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    isolate(monkeypatch)


@pytest.mark.parametrize("policy", ["auto", "text"])
def test_frozen_unqualified_requests_require_original_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, policy: str
) -> None:
    profiles = registry._QUALIFIED_PROFILES
    monkeypatch.setattr(registry, "_QUALIFIED_PROFILES", ())
    old, spec, dep, context = prepare(tmp_path, policy=policy)
    old_context = production_context_digest(context)
    old_candidate = production_context_digest(
        _qualification_context(spec, source_commit="a" * 40)
    )
    assert old.transport_commitment.mechanism == "text"
    monkeypatch.setattr(registry, "_QUALIFIED_PROFILES", profiles)
    assert production_context_digest(context) != old_context
    assert (
        production_context_digest(_qualification_context(spec, source_commit="a" * 40))
        != old_candidate
    )
    fresh, _, _, _ = prepare(tmp_path, policy=policy)
    assert fresh.transport_commitment.mechanism == (
        "text" if policy == "text" else profiles[0].mechanism
    )
    if policy == "text":
        # Explicit text keeps complete request bytes, but its authority is new.
        assert fresh.payload == old.payload
    assert fresh.transport_commitment != old.transport_commitment
    with pytest.raises(TransportIntegrityError):
        prepare_production_request(
            spec,
            dep,
            old.transport_plan.identity,
            context,
            frozen=old.transport_commitment,
        )
    monkeypatch.setattr(registry, "_QUALIFIED_PROFILES", ())
    assert (
        prepare_production_request(
            spec,
            dep,
            old.transport_plan.identity,
            context,
            frozen=old.transport_commitment,
        )
        == old
    )


def test_other_models_keep_frozen_identities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before, _, _, _ = prepare(
        tmp_path, model="different-unqualified-model", policy="auto"
    )
    monkeypatch.setattr(registry, "_QUALIFIED_PROFILES", ())
    after, _, _, _ = prepare(
        tmp_path, model="different-unqualified-model", policy="auto"
    )
    assert before == after


def test_old_manifest_cannot_be_resumed_under_promoted_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = production_spec(
        tmp_path / "inputs", traits=1, grouping="independent", explanations=False
    )
    spec.model = MODEL
    store = tmp_path / "store"
    profiles = registry._QUALIFIED_PROFILES
    monkeypatch.setattr(registry, "_QUALIFIED_PROFILES", ())
    with prepare_run(
        spec, run_id="001", store=store, transport_context=production_context(spec)
    ) as run:
        manifest = canonical_bytes(run.manifest)
        dep = next(run.iter_request_dependencies())
        ident = physical_identity(
            "001",
            dep.request.request_uid,
            stable_id("attempt", "001", dep.request.request_uid, 1),
        )
        wire = run.prepare_transport_attempt(dep, ident)
    before = files(store)
    monkeypatch.setattr(registry, "_QUALIFIED_PROFILES", profiles)
    with pytest.raises(PreparationError):
        with open_prepared_run(
            "001", spec=spec, store=store, transport_context=production_context(spec)
        ):
            pytest.fail("Changed authority must never resume a frozen run")
    assert files(store) == before
    monkeypatch.setattr(registry, "_QUALIFIED_PROFILES", ())
    with open_prepared_run(
        "001", spec=spec, store=store, transport_context=production_context(spec)
    ) as run:
        assert canonical_bytes(run.manifest) == manifest
        assert run.prepare_transport_attempt(dep, ident) == wire


@pytest.mark.asyncio
async def test_historical_executed_report_requires_original_source_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # This is an intercepted synthetic execution, never the external live store.
    profiles = registry._QUALIFIED_PROFILES
    monkeypatch.setattr(registry, "_QUALIFIED_PROFILES", ())
    monkeypatch.setattr(q, "_source_commit", lambda: "a" * 40)
    monkeypatch.setattr(e, "_source_commit", lambda: "a" * 40)
    directory = tmp_path / "historical"
    plan = q.build_plan(directory=directory, source_commit="a" * 40)
    sent = install(monkeypatch, directory, plan)
    result = await e.execute_plan(
        directory,
        authorized_plan_sha256=bytes_digest((directory / "plan.json").read_bytes()),
        authorized_source_commit=plan.source_commit,
        accept_billable_calls=True,
        accept_full_synthetic_capture=True,
    )
    assert result["overall_qualification_result"] == "qualified" and len(sent) == 4
    assert result["executor_status"] == plan.executor_status
    assert result["live_qualified"] and not plan.execution_enabled
    before = files(directory)
    monkeypatch.setattr(registry, "_QUALIFIED_PROFILES", profiles)

    def deny(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(
            "Reporting must not construct clients or resolve credentials"
        )

    from judgmentllm.providers import gemini

    monkeypatch.setattr(gemini, "_resolve_credentials", deny)
    with pytest.raises(q.QualificationError):
        q.report_plan(directory)
    assert files(directory) == before and len(sent) == 4
    monkeypatch.setattr(registry, "_QUALIFIED_PROFILES", ())
    assert q.report_plan(directory) == result
    assert files(directory) == before and len(sent) == 4
