"""Deterministic authentication policy tests; all credentials are synthetic."""

from __future__ import annotations

# pyright: reportPrivateUsage=false
import copy
import json
import os
import pickle
from collections.abc import Iterator
from dataclasses import FrozenInstanceError
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

import judgmentllm
from judgmentllm._auth import (  # pyright: ignore[reportPrivateUsage]
    _REGISTRY,
    _CredentialResolutionError,
    _inspect_credentials,
    _resolve_credentials,
    _select_providers,
)
from judgmentllm.auth import CredentialStatus, credential_status

PROVIDERS = (
    "openai",
    "anthropic",
    "gemini",
    "vertex",
    "together",
    "ollama",
    "typesafe",
)
ENV_SOURCES = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GOOGLE_API_KEY",
    "GEMINI_API_KEY",
    "TOGETHER_API_KEY",
    "TYPESAFE_API_KEY",
)


@pytest.fixture(autouse=True)
def synthetic_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for source in ENV_SOURCES:
        monkeypatch.delenv(source, raising=False)
    yield


def same_material(left: str, right: str) -> None:
    # Do not put secret-bearing operands into pytest assertion introspection.
    if left != right:
        pytest.fail("Selected synthetic credential changed.")


def assert_safe(value: object, material: str) -> None:
    leaked = material in str(value)
    assert not leaked, "Synthetic credential leaked."


def test_registry_contract_and_registration_independence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert tuple(entry.provider for entry in _REGISTRY) == PROVIDERS
    assert tuple(entry.auth_type for entry in _REGISTRY) == (
        "api_key",
        "api_key",
        "api_key",
        "application_default_credentials",
        "api_key",
        "none",
        "api_key",
    )
    assert tuple(entry.sources for entry in _REGISTRY) == (
        ("OPENAI_API_KEY",),
        ("ANTHROPIC_API_KEY",),
        ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
        ("google_adc",),
        ("TOGETHER_API_KEY",),
        ("local_service",),
        ("TYPESAFE_API_KEY",),
    )
    assert tuple(entry.offline_status_supported for entry in _REGISTRY) == (
        True,
        True,
        True,
        False,
        True,
        True,
        True,
    )
    assert tuple(entry.endpoint_scope for entry in _REGISTRY) == (
        "remote_api",
        "remote_api",
        "remote_api",
        "google_cloud",
        "remote_api",
        "local_service",
        "remote_api",
    )
    with pytest.raises(FrozenInstanceError):
        _REGISTRY[0].provider = "anthropic"  # type: ignore[misc]
    monkeypatch.setenv("UNREGISTERED_API_KEY", "unregistered-synthetic")
    assert tuple(record.provider for record in credential_status()) == PROVIDERS
    assert judgmentllm.CredentialStatus is CredentialStatus
    assert judgmentllm.credential_status is credential_status


def test_selection_order_duplicates_and_empty_no_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert tuple(
        record.provider
        for record in credential_status(
            providers=("ollama", "openai", "ollama", "gemini")
        )
    ) == ("openai", "gemini", "ollama")

    def forbidden(*args: object, **kwargs: object) -> str:
        raise AssertionError("Empty selection inspected environment.")

    monkeypatch.setattr(os.environ, "get", forbidden)
    assert credential_status(providers=()) == ()
    assert credential_status(providers=[]) == ()
    assert _select_providers(None) == PROVIDERS


@pytest.mark.parametrize(
    "selection",
    [
        ("unknown",),
        ("OpenAI",),
        (" openai",),
        ("openai ",),
        ("",),
        ("openai\n",),
        ("openai,anthropic",),
        (1,),
        "openai",
        b"openai",
        {"openai"},
    ],
    ids=[
        "unknown",
        "case",
        "leading",
        "trailing",
        "empty",
        "control",
        "comma",
        "nonstr",
        "scalar",
        "bytes",
        "set",
    ],
)
def test_invalid_selection_is_safe(selection: Any) -> None:
    with pytest.raises(ValueError, match="registered lowercase"):
        credential_status(providers=selection)


@pytest.mark.parametrize(
    "source,provider",
    [
        ("OPENAI_API_KEY", "openai"),
        ("ANTHROPIC_API_KEY", "anthropic"),
        ("GOOGLE_API_KEY", "gemini"),
        ("GEMINI_API_KEY", "gemini"),
        ("TOGETHER_API_KEY", "together"),
        ("TYPESAFE_API_KEY", "typesafe"),
    ],
)
@pytest.mark.parametrize(
    "kind",
    [
        "absent",
        "empty",
        "whitespace",
        "configured",
        "unicode",
        "leading",
        "trailing",
        "embedded",
        "tab",
        "newline",
        "control",
        "del",
        "format",
        "surrogate",
    ],
)
def test_api_key_policy_and_private_resolver_parity(
    source: str,
    provider: str,
    kind: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    material = "synthetic-preserved-key-α-🙂"
    values = {
        "empty": "",
        "whitespace": " \t\n\u2003",
        "configured": material,
        "unicode": "秘密-κλειδί-🙂",
        "leading": " " + material,
        "trailing": material + " ",
        "embedded": material + " space",
        "tab": material + "\tvalue",
        "newline": material + "\nvalue",
        "control": material + "\x01",
        "del": material + "\x7f",
        "format": material + "\u200b",
        "surrogate": material + "\ud800",
    }
    if kind != "absent":
        # POSIX and Windows reject some environment encodings; represent the
        # synthetic surrogate at the accessor boundary rather than in OS state.
        if kind == "surrogate":
            original_get = os.environ.get

            def get(name: str, default: str | None = None) -> str | None:
                return values[kind] if name == source else original_get(name, default)

            monkeypatch.setattr(os.environ, "get", get)
        else:
            monkeypatch.setenv(source, values[kind])
    status = credential_status(providers=(provider,))[0]
    if kind in {"configured", "unicode"}:
        assert status.status == "configured"
        assert status.source == source
        assert status.diagnostic_code is None
        resolved = _resolve_credentials(provider)
        same_material(resolved._api_key(), values[kind])
        assert resolved.source == status.source
    else:
        assert status.status == "missing"
        assert status.source is None
        expected = (
            "missing_api_key"
            if kind in {"absent", "empty", "whitespace"}
            else "malformed_api_key"
        )
        assert status.diagnostic_code == expected
        with pytest.raises(_CredentialResolutionError) as error:
            _resolve_credentials(provider)
        assert error.value.diagnostic_code == expected
        assert_safe(error.value, material)
    if kind != "absent" and kind != "surrogate":
        same_material(os.environ[source], values[kind])
    assert_safe(status.model_dump_json(), material)


@pytest.mark.parametrize(
    "case",
    [
        "missing",
        "google",
        "gemini",
        "both",
        "blank_google",
        "whitespace_google",
        "malformed_google",
        "identical",
        "malformed_shadowed",
    ],
)
def test_gemini_precedence(case: str, monkeypatch: pytest.MonkeyPatch) -> None:
    google, gemini = "synthetic-google-α", "synthetic-gemini-β"
    if case in {"google", "both", "identical", "malformed_shadowed"}:
        monkeypatch.setenv("GOOGLE_API_KEY", google)
    if case in {
        "gemini",
        "both",
        "blank_google",
        "whitespace_google",
        "malformed_google",
        "malformed_shadowed",
    }:
        monkeypatch.setenv("GEMINI_API_KEY", gemini)
    if case == "identical":
        monkeypatch.setenv("GEMINI_API_KEY", google)
    if case == "blank_google":
        monkeypatch.setenv("GOOGLE_API_KEY", "")
    if case == "whitespace_google":
        monkeypatch.setenv("GOOGLE_API_KEY", " \t\u2003")
    if case == "malformed_google":
        monkeypatch.setenv("GOOGLE_API_KEY", google + " ")
    if case == "malformed_shadowed":
        monkeypatch.setenv("GEMINI_API_KEY", gemini + " ")
    status = credential_status(providers=("gemini",))[0]
    if case in {"missing", "malformed_google"}:
        assert status.status == "missing" and status.source is None
        with pytest.raises(_CredentialResolutionError):
            _resolve_credentials("gemini")
    else:
        expected = (
            "GEMINI_API_KEY"
            if case in {"gemini", "blank_google", "whitespace_google"}
            else "GOOGLE_API_KEY"
        )
        assert status.source == expected
        resolved = _resolve_credentials("gemini")
        assert resolved.source == status.source
        same_material(
            resolved._api_key(), gemini if expected == "GEMINI_API_KEY" else google
        )


def test_multiple_sources_do_not_mutate_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for index, source in enumerate(ENV_SOURCES):
        monkeypatch.setenv(source, f"synthetic-source-{index}")
    before = dict(os.environ)
    statuses = credential_status()
    assert tuple(status.status for status in statuses) == (
        "configured",
        "configured",
        "configured",
        "unknown",
        "configured",
        "not_required",
        "configured",
    )
    unchanged = dict(os.environ) == before
    assert unchanged, "Credential inspection mutated the environment."


def test_non_api_markers_do_not_inspect_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> str:
        raise AssertionError("Non-API auth inspected the environment.")

    monkeypatch.setattr(os.environ, "get", forbidden)
    vertex, ollama = credential_status(providers=("ollama", "vertex"))
    assert vertex.model_dump() == {
        "provider": "vertex",
        "auth_type": "application_default_credentials",
        "source": "google_adc",
        "status": "unknown",
        "diagnostic_code": "offline_identity_unknown",
    }
    assert ollama.status == "not_required" and ollama.source == "local_service"
    for provider in ("vertex", "ollama"):
        marker = _resolve_credentials(provider)
        with pytest.raises(_CredentialResolutionError):
            marker._api_key()


def test_unsupported_context_is_distinct_from_missing_sdk() -> None:
    status = _inspect_credentials("ollama", endpoint_scope="remote_api")
    assert status.status == "unsupported"
    assert status.source is None
    assert status.diagnostic_code == "unsupported_authentication_context"
    with pytest.raises(_CredentialResolutionError):
        _resolve_credentials("ollama", endpoint_scope="remote_api")
    assert credential_status(providers=("ollama",))[0].status == "not_required"


def test_environment_exception_is_not_exposed(monkeypatch: pytest.MonkeyPatch) -> None:
    material = "synthetic-accessor-exception-sentinel"

    def failed(*args: object, **kwargs: object) -> str:
        raise RuntimeError(material)

    monkeypatch.setattr(os.environ, "get", failed)
    status = credential_status(providers=("openai",))[0]
    assert status.status == "missing"
    assert status.diagnostic_code == "credential_source_unavailable"
    assert status.source is None
    with pytest.raises(_CredentialResolutionError) as error:
        _resolve_credentials("openai")
    assert_safe(error.value, material)
    assert_safe(repr(error.value), material)
    assert_safe(status.model_dump_json(), material)


def test_private_material_cannot_serialize_or_repr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    material = "synthetic-wrapper-sentinel-α"
    monkeypatch.setenv("OPENAI_API_KEY", material)
    resolved = _resolve_credentials("openai")
    same_material(resolved._api_key(), material)
    assert_safe(repr(resolved), material)
    assert_safe(str(resolved), material)
    assert not hasattr(resolved, "__dict__")
    for operation in (pickle.dumps, copy.copy, copy.deepcopy, json.dumps):
        with pytest.raises(TypeError) as error:
            operation(resolved)
        assert_safe(error.value, material)


def test_public_status_is_frozen_strict_and_safe_on_invalid_inputs() -> None:
    valid = credential_status(providers=("openai",))[0]
    assert valid.model_copy() == valid
    assert valid.model_copy(deep=True) == valid
    assert CredentialStatus.model_validate_json(valid.model_dump_json()) == valid
    with pytest.raises(ValidationError):
        valid.status = "configured"  # type: ignore[misc]
    material = "synthetic-public-input-sentinel-α"
    invalid_documents: list[dict[str, Any]] = []
    for field in (
        "provider",
        "auth_type",
        "source",
        "status",
        "diagnostic_code",
        material,
    ):
        data: dict[str, Any] = valid.model_dump()
        data[field] = material
        invalid_documents.append(data)
    invalid_documents += [
        {
            **valid.model_dump(),
            "source": "ANTHROPIC_API_KEY",
            "status": "configured",
            "diagnostic_code": None,
        },
        {
            **valid.model_dump(),
            "status": "unknown",
            "source": "google_adc",
            "diagnostic_code": "offline_identity_unknown",
        },
        {
            **valid.model_dump(),
            "status": "not_required",
            "source": "local_service",
            "diagnostic_code": None,
        },
        {**valid.model_dump(), "status": "missing", "diagnostic_code": None},
        {
            **valid.model_dump(),
            "status": "configured",
            "source": "OPENAI_API_KEY",
            "diagnostic_code": "missing_api_key",
        },
        {**valid.model_dump(), "auth_type": "none"},
    ]
    for document in invalid_documents:
        with pytest.raises(ValidationError) as error:
            CredentialStatus.model_validate(document)
        assert_safe(error.value, material)
        assert_safe(error.value.errors(), material)
        assert_safe(error.value.json(), material)
        assert all("ctx" not in entry for entry in error.value.errors())
        assert all(entry.get("input") is None for entry in error.value.errors())
    with pytest.raises(ValidationError) as error:
        valid.model_copy(update={"source": material})
    assert_safe(error.value.json(), material)
    with pytest.raises(ValidationError) as error:
        construction: dict[str, Any] = valid.model_dump()
        construction["source"] = material
        CredentialStatus.model_construct(**construction)
    assert_safe(error.value.json(), material)
    with pytest.raises(ValidationError) as error:
        valid.source = material  # type: ignore[misc]
    assert_safe(error.value.json(), material)


@pytest.mark.parametrize("provider", ["vertex", "ollama"])
def test_public_non_api_status_rejects_unsupported(provider: str) -> None:
    status = credential_status(providers=(provider,))[0]
    data = status.model_dump()
    data.update(
        source=None,
        status="unsupported",
        diagnostic_code="unsupported_authentication_context",
    )
    with pytest.raises(ValidationError):
        CredentialStatus.model_validate(data)


def test_public_api_key_unsupported_state_is_allowlisted() -> None:
    data = credential_status(providers=("openai",))[0].model_dump()
    data.update(
        status="unsupported", diagnostic_code="unsupported_authentication_context"
    )
    assert CredentialStatus.model_validate(data).status == "unsupported"


def test_all_model_owned_validation_paths_exclude_untrusted_input() -> None:
    material = "synthetic-json-input-sentinel-α"
    data = credential_status(providers=("openai",))[0].model_dump()
    data["source"] = material
    adapter = TypeAdapter(CredentialStatus)
    invalid_json = json.dumps(data)
    operations = (
        lambda: CredentialStatus(**data),
        lambda: CredentialStatus.model_validate(data),
        lambda: CredentialStatus.model_validate_json(invalid_json),
        lambda: CredentialStatus.model_validate_json("{" + material),
        lambda: CredentialStatus.model_validate_strings(data),
        lambda: CredentialStatus.model_validate_strings(material),
        lambda: adapter.validate_python(data),
        lambda: adapter.validate_json(invalid_json),
    )
    for operation in operations:
        with pytest.raises(ValidationError) as error:
            operation()
        for representation in (
            str(error.value),
            repr(error.value),
            error.value.errors(),
            error.value.json(),
        ):
            assert_safe(representation, material)
        assert all("ctx" not in entry for entry in error.value.errors())


def test_status_only_reads_registered_api_environment_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inspected: list[str] = []

    def get(name: str, default: str | None = None) -> str | None:
        inspected.append(name)
        return default

    monkeypatch.setattr(os.environ, "get", get)
    assert len(credential_status()) == 7
    assert tuple(inspected) == ENV_SOURCES


@pytest.mark.parametrize("extra", ["allow", "ignore"])
def test_model_configuration_overrides_cannot_admit_unknown_fields(extra: Any) -> None:
    data = credential_status(providers=("openai",))[0].model_dump()
    material = "synthetic-extra-input-sentinel"
    data["unknown"] = material
    with pytest.raises(ValidationError) as error:
        CredentialStatus.model_validate(data, extra=extra)
    assert_safe(error.value.json(), material)
    assert error.value.__context__ is None


@pytest.mark.parametrize("operation", ["raw", "file", "copy"])
def test_deprecated_unsafe_parsers_and_copy_are_rejected(operation: str) -> None:
    material = "synthetic-deprecated-parser-sentinel"
    valid = credential_status(providers=("openai",))[0]
    with pytest.raises(ValueError) as error:
        if operation == "raw":
            CredentialStatus.parse_raw("{" + material)
        elif operation == "file":
            CredentialStatus.parse_file(material)
        else:
            valid.copy(update={"source": material})
    assert_safe(str(error.value), material)
    assert_safe(repr(error.value), material)
