"""Gemini's additive shared manifest, dispatch and public validation boundaries."""

from __future__ import annotations

# pyright: reportPrivateUsage=false
from pathlib import Path
from typing import Any, cast

import pytest
from gemini_fixture import wire_for
from gemini_offline import isolate
from test_gemini_lifecycle import production_spec, setup_case

from judgmentllm import lifecycle, transport_dispatch
from judgmentllm.config import GenerationSpec
from judgmentllm.persistence.hashing import canonical_bytes
from judgmentllm.preparation import open_prepared_run
from judgmentllm.preparation.manifest import ExecutableRunManifestV6


def gemini_rendering(root: Path) -> Any:
    from judgmentllm.gemini_prompts import RenderedGeminiPromptV1
    from judgmentllm.output_contracts import generate_output_schema

    wire, _, _, _ = wire_for(root)
    return RenderedGeminiPromptV1(
        text=wire.rendered_text,
        exemplar_ids=[],
        output_schema=generate_output_schema(wire.output_commitment.contract),
        output_commitment=wire.output_commitment,
    )


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    isolate(monkeypatch)


def manifest(root: Path) -> ExecutableRunManifestV6:
    spec, store, adapter, _ = setup_case(root, "models.generate_content")
    with open_prepared_run(
        "001", store=store, spec=spec, transport_context=adapter.transport_context
    ) as prepared:
        assert isinstance(prepared.manifest, ExecutableRunManifestV6)
        return prepared.manifest


def test_gemini_manifest_ingress_keeps_exact_version_and_sanitized_values(
    tmp_path: Path,
) -> None:
    record = manifest(tmp_path)
    raw = canonical_bytes(record)
    assert ExecutableRunManifestV6.model_validate_json(bytearray(raw)) == record
    assert ExecutableRunManifestV6.model_validate_json(raw.decode()) == record
    inputs: tuple[object, ...] = (
        None,
        [],
        6,
        b'{"schema_version":6,"schema_version":6}',
    )
    for input_value in inputs:
        with pytest.raises(ValueError) as failure:
            ExecutableRunManifestV6.model_validate_json(cast(Any, input_value))
        assert "schema_version" not in str(failure.value)
    for values in (
        {"schema_version": 5},
        {"schema_version": True},
        {"serializer_version": True},
        {"run_spec_version": 3.0},
        {"transport_protocol": "PRIVATE_PROTOCOL"},
        {"PRIVATE_EXTRA_FIELD": "PRIVATE_VALUE"},
    ):
        with pytest.raises(ValueError) as failure:
            ExecutableRunManifestV6.model_validate(dict(record) | values)
        assert "PRIVATE_" not in str(failure.value)
        with pytest.raises(ValueError) as failure:
            ExecutableRunManifestV6.model_validate_json(
                canonical_bytes(record.model_dump(mode="json") | values)
            )
        assert "PRIVATE_" not in str(failure.value)


@pytest.mark.parametrize("field", ["frozen", "parent"])
def test_shared_gemini_dispatch_rejects_foreign_transport_types(
    tmp_path: Path, field: str
) -> None:
    wire, spec, dep, context = wire_for(tmp_path)
    settings: dict[str, Any] = {field: object()}
    with pytest.raises(ValueError, match="Gemini commitments"):
        transport_dispatch.prepare_request(
            spec, dep, wire.transport_plan.identity, context, **settings
        )
    with pytest.raises(ValueError, match="Gemini parent"):
        transport_dispatch.build_output(spec, dep.request, context, cast(Any, object()))


@pytest.mark.parametrize(
    "endpoint,mock", [("messages", None), ("models.generate_content", object())]
)
def test_public_execution_has_no_cross_endpoint_or_mock_escape(
    tmp_path: Path, endpoint: str, mock: object | None
) -> None:
    with pytest.raises(lifecycle.LifecycleError, match="Gemini Developer"):
        lifecycle._execution_adapter(
            "gemini", endpoint, "unknown-model", tmp_path, cast(Any, mock), resume=False
        )


def test_offline_public_validation_keeps_generation_errors_actionable(
    tmp_path: Path,
) -> None:
    spec = production_spec(tmp_path / "inputs", explanations=False)
    spec.generation = GenerationSpec(temperature=3)
    report = lifecycle.validate_inputs(spec)
    assert report.valid is False
    assert any(issue.field == "generation.temperature" for issue in report.issues)
    with pytest.raises(lifecycle.LifecycleError, match="generation.temperature"):
        lifecycle.estimate_run(spec)


def test_gemini_rendering_cannot_gain_historical_mock_authority(tmp_path: Path) -> None:
    from judgmentllm.config import RunSpec
    from judgmentllm.gemini_prompts import RenderedGeminiPromptV1
    from judgmentllm.output_contracts import generate_output_schema
    from judgmentllm.providers.mock import MockAdapter
    from judgmentllm.providers.protocol import ProviderError

    wire, spec, dependency, _ = wire_for(tmp_path)
    historical = RunSpec.model_validate(
        dict(spec)
        | {
            "schema_version": 1,
            "provider": "mock",
            "endpoint": "synthetic",
            "prompt": {},
        }
    )
    rendered = RenderedGeminiPromptV1(
        text=wire.rendered_text,
        exemplar_ids=[],
        output_schema=generate_output_schema(wire.output_commitment.contract),
        output_commitment=wire.output_commitment,
    )
    adapter = MockAdapter(tmp_path / "mock.sqlite3")
    adapter.validate(historical)
    with pytest.raises(ProviderError):
        adapter.prepare(historical, dependency, rendered)
    assert adapter.submit_calls == 0


def test_subset_rendering_and_shared_dispatch_preserve_gemini_identity(
    tmp_path: Path,
) -> None:
    from judgmentllm.execution.retry import subset_dependency
    from judgmentllm.gemini_prompts import RenderedGeminiPromptV1
    from judgmentllm.output_contracts import RubricOutputContract
    from judgmentllm.persistence.hashing import bytes_digest

    spec, store, adapter, _ = setup_case(tmp_path, "models.generate_content")
    with open_prepared_run(
        "001", store=store, spec=spec, transport_context=adapter.transport_context
    ) as prepared:
        dep = next(prepared.iter_request_dependencies())
        selected = subset_dependency(dep, (dep.request.tasks[0].task_id,))
        rendered = prepared.render_attempt(selected)
        assert isinstance(rendered, RenderedGeminiPromptV1)
        assert isinstance(rendered.output_commitment.contract, RubricOutputContract)
        assert len(rendered.output_commitment.contract.traits) == 1
    wire, spec, dep, _ = wire_for(tmp_path / "dispatch")

    def lookup(kind: Any, identifier: str) -> Any:
        assert dep.request.response is not None
        return dep.request.response

    dispatched = transport_dispatch.render_request(
        spec, None, dep.request, lookup, wire
    )
    assert isinstance(dispatched, RenderedGeminiPromptV1)
    assert bytes_digest(dispatched.text.encode()) == wire.prompt_sha256


def test_sealed_gemini_request_cannot_be_scheduled_twice(tmp_path: Path) -> None:
    from judgmentllm.execution.session import ExecutionSession
    from judgmentllm.persistence.models import IntegrityError

    spec, store, adapter, _ = setup_case(tmp_path, "models.generate_content")
    session = ExecutionSession("001", store, adapter, spec, False, None)
    try:
        assert session.prepared is not None
        dependency = next(session.prepared.iter_request_dependencies())
        session.existing = iter(())
        session.dependencies = iter((dependency,))
        with pytest.raises(IntegrityError, match="scheduled twice"):
            session.next_work()
        assert adapter.actual_call_count == 0
    finally:
        session.close()


def test_historical_session_rejects_foreign_renderer_before_preparation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from test_preparation_rendering import specification

    from judgmentllm.execution.session import ExecutionSession
    from judgmentllm.persistence.models import IntegrityError
    from judgmentllm.preparation import prepare_run
    from judgmentllm.providers.mock import MockAdapter

    spec = specification(tmp_path / "old", responses=1, traits=1)
    spec.provider, spec.endpoint = "mock", "synthetic"
    store = tmp_path / "store"
    with prepare_run(spec, store=store, run_id="001"):
        pass
    adapter = MockAdapter(tmp_path / "ledger.sqlite3")
    session = ExecutionSession("001", store, adapter, spec, False, None)
    try:
        assert session.prepared is not None
        dependency = next(session.prepared.iter_request_dependencies())
        rendered = gemini_rendering(tmp_path / "foreign")

        def foreign(*args: Any, **kwargs: Any) -> Any:
            return rendered

        monkeypatch.setattr(session.prepared, "render", foreign)
        with pytest.raises(
            IntegrityError, match="historical attempt acquired native rendering"
        ):
            session.prepare_attempt(dependency, "attempt-001", initial=True)
        assert adapter.submit_calls == 0
    finally:
        session.close()


@pytest.mark.asyncio
async def test_historical_retry_rejects_foreign_renderer_without_submission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from test_execution_retry import partial_group

    from judgmentllm.execution.retry import authorize_subset_retry
    from judgmentllm.persistence.models import IntegrityError
    from judgmentllm.preparation import PreparedRun
    from judgmentllm.providers.mock import MockAdapter

    spec, store, ledger, tasks = await partial_group(tmp_path / "old")
    rendered = gemini_rendering(tmp_path / "foreign")

    def foreign(*args: Any, **kwargs: Any) -> Any:
        return rendered

    monkeypatch.setattr(PreparedRun, "render_attempt", foreign)
    adapter = MockAdapter(ledger)
    with pytest.raises(
        IntegrityError, match="historical retry acquired native rendering"
    ):
        authorize_subset_retry(
            "001",
            store=store,
            adapter=adapter,
            spec=spec,
            task_ids=(tasks[2],),
            operation_id="retry-foreign",
        )
    assert adapter.submit_calls == 0 and adapter.ledger.acceptance_count() == 1
