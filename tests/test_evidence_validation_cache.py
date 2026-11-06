"""Compiled JSON field schemas are reused; every supplied fact is revalidated."""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError, create_model
from test_auth_cli import private_failure

from judgmentllm.evidence._base import (
    EvidenceIdentity,
    EvidenceModel,
    _json_field_adapter,  # pyright: ignore[reportPrivateUsage]
)
from judgmentllm.evidence.reasoning import ReasoningAuthorization
from judgmentllm.evidence.usage import ProviderUsage


def usage() -> ProviderUsage:
    return ProviderUsage(
        run_id="001",
        request_uid="002",
        attempt_id="003",
        submission_id="004",
        observation_id="005",
        provider="openai",
        endpoint="chat.completions",
        requested_model="synthetic-model",
        observation_kind="response",
        usage_source="openai.chat.usage",
        input_tokens=40,
        output_tokens=20,
        total_tokens=60,
        reasoning_tokens=10,
    )


def assert_safe(error: ValidationError, secret: str) -> None:
    assert secret not in str(error) + repr(error) + repr(error.errors()) + error.json()
    assert all(row["input"] is None for row in error.errors())


@pytest.mark.parametrize(
    "invalid", ["counter", "extra", "source", "cross_field", "version"]
)
@private_failure
def test_warm_usage_schema_still_rejects_invalid_changed_evidence(invalid: str) -> None:
    __tracebackhide__ = True
    secret = "synthetic-private-" + uuid4().hex
    original = usage()
    assert ProviderUsage.model_validate_json(original.model_dump_json()) == original
    changed: dict[str, Any] = original.model_dump(mode="json")
    if invalid == "counter":
        changed["input_tokens"] = secret
    elif invalid == "extra":
        changed[secret] = secret
    elif invalid == "source":
        changed["usage_source"] = secret
    elif invalid == "cross_field":
        changed.update(reasoning_tokens=21, requested_model=secret)
    else:
        changed.update(schema_version=True, requested_model=secret)
    with pytest.raises(ValidationError) as caught:
        ProviderUsage.model_validate_json(json.dumps(changed))
    assert_safe(caught.value, secret)
    # A rejected payload cannot poison the compiled schema or replace old facts.
    assert ProviderUsage.model_validate_json(original.model_dump_json()) == original


@pytest.mark.parametrize("value", [True, -1, 1.0, "1", None])
def test_cached_required_counter_keeps_strict_validation(value: object) -> None:
    original = usage()
    assert ProviderUsage.model_validate_json(original.model_dump_json()) == original
    record = original.model_dump(mode="json")
    record["input_tokens"] = value
    if value is None:
        assert (
            ProviderUsage.model_validate_json(json.dumps(record)).input_tokens is None
        )
    else:
        with pytest.raises(ValidationError):
            ProviderUsage.model_validate_json(json.dumps(record))


@private_failure
def test_cached_authorization_never_reuses_a_previous_retention_grant() -> None:
    __tracebackhide__ = True
    grant = ReasoningAuthorization(capture="full", retention="summary")
    assert ReasoningAuthorization.model_validate_json(grant.model_dump_json()) == grant
    denied = {"capture": "minimal", "retention": "summary"}
    with pytest.raises(ValidationError):
        ReasoningAuthorization.model_validate_json(json.dumps(denied))
    # The same compiled fields accept a distinct, valid policy unchanged.
    ordinary = ReasoningAuthorization.model_validate_json(
        '{"capture":"minimal","retention":"none"}'
    )
    assert ordinary.capture == "minimal" and ordinary.retention == "none"
    assert ReasoningAuthorization.model_validate_json(grant.model_dump_json()) == grant


def test_changed_valid_values_are_not_cached_as_evidence_facts() -> None:
    original = usage()
    ProviderUsage.model_validate_json(original.model_dump_json())
    changed = original.model_dump(mode="json")
    changed.update(
        run_id="0001",
        request_uid="0002",
        attempt_id="0003",
        submission_id="0004",
        observation_id="0005",
        input_tokens=41,
        output_tokens=22,
        total_tokens=63,
    )
    observed = ProviderUsage.model_validate_json(json.dumps(changed))
    assert (
        observed.run_id,
        observed.request_uid,
        observed.attempt_id,
        observed.submission_id,
        observed.observation_id,
    ) == ("0001", "0002", "0003", "0004", "0005")
    assert (observed.input_tokens, observed.output_tokens, observed.total_tokens) == (
        41,
        22,
        63,
    )
    assert observed != original
    assert ProviderUsage.model_validate_json(original.model_dump_json()) == original


def test_compiled_adapter_reuse_still_validates_each_json_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _json_field_adapter.cache_clear()
    adapter = _json_field_adapter(EvidenceIdentity, "run_id")
    original_validate = adapter.validate_json
    calls = 0

    def validate(data: str | bytes | bytearray, **options: Any) -> Any:
        nonlocal calls
        calls += 1
        return original_validate(data, **options)

    monkeypatch.setattr(adapter, "validate_json", validate)
    base = {
        "run_id": "001",
        "request_uid": "002",
        "attempt_id": "003",
        "submission_id": "004",
    }
    first = EvidenceIdentity.model_validate_json(json.dumps(base))
    second = EvidenceIdentity.model_validate_json(
        json.dumps({**base, "run_id": "0001"})
    )
    with pytest.raises(ValidationError):
        EvidenceIdentity.model_validate_json(json.dumps({**base, "run_id": 1}))
    assert calls == 3
    assert first.run_id == "001" and second.run_id == "0001"
    assert _json_field_adapter(EvidenceIdentity, "run_id") is adapter
    assert _json_field_adapter.cache_info().hits >= 3


def test_cache_cardinality_depends_on_schemas_not_payload_values() -> None:
    _json_field_adapter.cache_clear()
    for index in range(300):
        record = EvidenceIdentity.model_validate_json(
            json.dumps(
                {
                    "run_id": f"{index:04d}",
                    "request_uid": "002",
                    "attempt_id": "003",
                    "submission_id": "004",
                }
            )
        )
        assert record.run_id == f"{index:04d}"
    info = _json_field_adapter.cache_info()
    assert info.maxsize == 256 and info.currsize == 4
    assert info.hits >= 299 * 4


def test_compiler_cache_is_bounded_across_distinct_schema_fields() -> None:
    _json_field_adapter.cache_clear()
    models = [
        create_model(
            f"SyntheticCacheScope{index:03d}",
            __base__=EvidenceModel,
            counter=(int, ...),
        )
        for index in range(257)
    ]
    first = _json_field_adapter(models[0], "counter")
    for model in models[1:]:
        assert (
            _json_field_adapter(model, "counter").validate_json("1", strict=True) == 1
        )
    info = _json_field_adapter.cache_info()
    assert info.maxsize == info.currsize == 256
    assert _json_field_adapter(models[0], "counter") is not first
    # Eviction only affects compilation work; strict validation remains mandatory.
    with pytest.raises(ValidationError):
        _json_field_adapter(models[0], "counter").validate_json('"1"', strict=True)
    _json_field_adapter.cache_clear()
