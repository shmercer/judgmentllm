"""Independent Vertex auth policy V2 and strictly offline status contracts."""

from __future__ import annotations

# pyright: reportPrivateUsage=false
import json
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
from typer.testing import CliRunner
from vertex_fixture import wire_for
from vertex_offline import adc, isolate

from judgmentllm._auth import _inspect_credentials, _resolve_credentials
from judgmentllm.auth import CredentialStatus, CredentialStatusV2, credential_status
from judgmentllm.cli import app
from judgmentllm.providers.vertex import VertexAdapter
from judgmentllm.vertex_config import (
    VertexSetupError,
    runtime_configuration,
)
from judgmentllm.vertex_transport import production_context_digest


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    isolate(monkeypatch)


@pytest.mark.parametrize(
    "key,state,code",
    [
        (None, "missing", "missing_api_key"),
        ("", "missing", "missing_api_key"),
        (" \t", "missing", "missing_api_key"),
        ("SYNTHETIC_KEY", "configured", None),
        (" bad key", "missing", "malformed_api_key"),
        ("key\u200bvalue", "missing", "malformed_api_key"),
    ],
)
def test_vertex_key_status_resolver_and_cli_agree(
    monkeypatch: pytest.MonkeyPatch, key: str | None, state: str, code: str | None
) -> None:
    if key is None:
        monkeypatch.delenv("JUDGMENTLLM_VERTEX_API_KEY")
    else:
        monkeypatch.setenv("JUDGMENTLLM_VERTEX_API_KEY", key)
    status = credential_status(providers=("vertex",))[0]
    assert type(status) is CredentialStatusV2
    assert (
        status.status == state
        and status.diagnostic_code == code
        and status.auth_type == "api_key"
    )
    assert status.source == (
        "JUDGMENTLLM_VERTEX_API_KEY" if state == "configured" else None
    )
    assert CredentialStatusV2.model_validate_json(status.model_dump_json()) == status
    if state == "configured":
        assert _resolve_credentials("vertex")._api_key() == key
    else:
        with pytest.raises(ValueError):
            _resolve_credentials("vertex")
    report = CliRunner().invoke(
        app, ["auth", "status", "--require", "vertex", "--json"]
    )
    assert report.exit_code == (0 if state == "configured" else 2)
    row = next(
        x
        for x in json.loads(report.stdout)["data"]["providers"]
        if x["provider"] == "vertex"
    )
    assert row == status.model_dump(mode="json")
    assert (
        "SYNTHETIC_SELECTED_VERTEX_KEY" not in report.output
        and "synthetic-project" not in report.output
    )


def test_default_adc_status_keeps_historical_five_fields_and_never_discovers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("JUDGMENTLLM_VERTEX_AUTH_MODE")
    # All alternative key/ADC paths exist only as conspicuous fake environment values.
    status = credential_status(providers=("vertex",))[0]
    assert type(status) is CredentialStatus
    assert status.model_dump() == dict(
        provider="vertex",
        auth_type="application_default_credentials",
        source="google_adc",
        status="unknown",
        diagnostic_code="offline_identity_unknown",
    )
    assert _resolve_credentials("vertex").status == "unknown"
    with pytest.raises(ValueError):
        _resolve_credentials("vertex")._api_key()
    wire, spec, _, context = wire_for(tmp_path)
    adapter = VertexAdapter(spec.endpoint, spec.model, context=context)
    adapter.validate_credentials()
    assert adapter._client is None
    assert b"forbidden/credential" not in wire.payload


@pytest.mark.parametrize("mode", ["", "ADC", "express", "gemini", "bad\nmode"])
def test_unknown_mode_is_explicit_unsupported_without_fallback(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    monkeypatch.setenv("JUDGMENTLLM_VERTEX_AUTH_MODE", mode)
    status = credential_status(providers=("vertex",))[0]
    assert type(status) is CredentialStatusV2 and status.status == "unsupported"
    with pytest.raises(VertexSetupError) as failure:
        runtime_configuration()
    assert failure.value.diagnostic_code == "unsupported_authentication_context"
    assert mode not in str(failure.value) or mode == ""
    with pytest.raises(ValueError):
        _resolve_credentials("vertex")


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("GOOGLE_CLOUD_PROJECT", "", "missing_project"),
        ("GOOGLE_CLOUD_PROJECT", "PRIVATE_PROJECT/invalid", "invalid_project"),
        ("GOOGLE_CLOUD_LOCATION", "", "missing_location"),
        ("GOOGLE_CLOUD_LOCATION", "https://forbidden.invalid", "invalid_location"),
        ("GOOGLE_CLOUD_LOCATION", "us", "invalid_location"),
    ],
)
def test_route_diagnostics_do_not_disclose_values(
    monkeypatch: pytest.MonkeyPatch, field: str, value: str, code: str
) -> None:
    monkeypatch.setenv(field, value)
    with pytest.raises(VertexSetupError) as failure:
        runtime_configuration()
    assert failure.value.diagnostic_code == code
    assert "PRIVATE_PROJECT" not in str(
        failure.value
    ) and "forbidden.invalid" not in str(failure.value)
    # Status remains entirely independent of route validity.
    assert credential_status(providers=("vertex",))[0].status == "configured"


@pytest.mark.parametrize(
    "change",
    [{"auth_mode": "adc"}, {"project": "other-project"}, {"location": "global"}],
)
def test_all_routing_and_auth_choices_are_immutable(
    tmp_path: Path, change: dict[str, Any]
) -> None:
    wire, _, _, context = wire_for(tmp_path)
    altered = replace(context, runtime=replace(context.runtime, **change))
    assert production_context_digest(context) != production_context_digest(altered)
    assert wire.transport_commitment.route_sha256 == context.runtime.route_sha256
    assert context.runtime.project not in repr(context.runtime)
    assert context.runtime.project not in wire.model_dump_json()


@pytest.mark.parametrize(
    "changed",
    [
        {"provider": "gemini"},
        {"auth_type": "none"},
        {"status": "configured", "source": "GOOGLE_API_KEY"},
        {"status_version": 2.0},
        {"status_version": True},
        {"source": "PRIVATE_SECRET"},
        {"extra": "PRIVATE_SECRET"},
        {"status": "unsupported", "source": "JUDGMENTLLM_VERTEX_API_KEY"},
    ],
)
def test_key_status_version_and_cross_provider_forgery_fail_safely(
    changed: dict[str, Any],
) -> None:
    data = credential_status(providers=("vertex",))[0].model_dump() | changed
    with pytest.raises(ValueError) as failure:
        CredentialStatusV2.model_validate(data)
    assert "PRIVATE_SECRET" not in str(failure.value)
    with pytest.raises(ValueError):
        CredentialStatus.model_validate(data)


def test_explicit_resolver_modes_cannot_be_used_for_other_providers() -> None:
    with pytest.raises(ValueError):
        _inspect_credentials("gemini", vertex_mode="api_key")
    assert (
        _inspect_credentials("vertex", endpoint_scope="remote_api").status
        == "unsupported"
    )
    # Dedicated key presence never changes explicitly requested ADC.
    assert (
        _resolve_credentials("vertex", vertex_mode="adc").auth_type
        == "application_default_credentials"
    )


def test_api_key_handles_are_nonserializable() -> None:
    import pickle

    resolved = _resolve_credentials("vertex")
    assert "SYNTHETIC" not in repr(resolved)
    with pytest.raises(TypeError):
        pickle.dumps(resolved)


@pytest.mark.asyncio
async def test_offline_diagnostics_are_unsupported_without_sdk_or_adc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from judgmentllm import provider_diagnostics as d
    from judgmentllm import vertex_diagnostics as v

    _, calls = adc(monkeypatch)
    for mode in ("adc", "api_key"):
        monkeypatch.setenv("JUDGMENTLLM_VERTEX_AUTH_MODE", mode)
        assert (await v.averify_authentication()).verification_status == "unsupported"
        assert (
            await d.averify_authentication(provider="vertex")
        ).verification_status == "unsupported"
        assert (
            await d.adiscover_models(provider="vertex", model="unknown")
        ).sdk_calls == 0
        assert (
            d.discover_models(provider="vertex").verification.verification_status
            == "unsupported"
        )
        assert d.verify_authentication(provider="vertex").source is None
        for command in (["auth", "verify"], ["providers", "models"]):
            result = CliRunner().invoke(
                app, [*command, "--provider", "vertex", "--json"]
            )
            assert result.exit_code == 2
            data = json.loads(result.stdout)["data"]
            assert data["provider"] == "vertex" and "SYNTHETIC" not in result.output
    assert not calls
    with pytest.raises(ValueError):
        v.verify_authentication(provider="gemini")


def test_selector_read_failure_and_invalid_private_mode_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from judgmentllm import _auth

    def unavailable(name: str, default: str | None = None) -> str | None:
        raise RuntimeError("PRIVATE_ENVIRONMENT_DETAIL")

    with monkeypatch.context() as patch:
        patch.setattr(_auth.os.environ, "get", unavailable)
        status = _inspect_credentials("vertex")
    assert status.status == "unsupported"
    assert status.diagnostic_code == "unsupported_authentication_context"
    assert "PRIVATE_ENVIRONMENT_DETAIL" not in repr(status)
    invalid = _inspect_credentials("vertex", vertex_mode=cast(Any, "unsupported"))
    assert invalid.status == "unsupported" and invalid.source is None


def test_key_status_cannot_be_reinterpreted_as_adc() -> None:
    data = credential_status(providers=("vertex",))[0].model_dump()
    with pytest.raises(ValueError):
        CredentialStatusV2.model_validate(
            data | {"auth_type": "application_default_credentials"}
        )
