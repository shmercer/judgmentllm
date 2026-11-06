"""Standalone v2 output semantics leave v1 execution and exact artifacts intact."""

from __future__ import annotations

import hashlib
import json
import socket
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from judgmentllm import LifecycleError, cli, run_live
from judgmentllm.config import RunSpec
from judgmentllm.models import (
    Level,
    PairTask,
    PairwiseCriterion,
    ResponseItem,
    Rubric,
    Trait,
)
from judgmentllm.output_contracts import (
    PairwiseOutputContract,
    RubricOutputContract,
    generate_output_schema,
    serialize_output_schema,
    validate_pairwise_contract_output,
    validate_rubric_contract_output,
)
from judgmentllm.persistence.hashing import canonical_bytes, semantic_digest
from judgmentllm.persistence.repository import Repository
from judgmentllm.preparation import prepare_run
from judgmentllm.preparation.freezing import PreparationError
from judgmentllm.prompts import render_prompt
from judgmentllm.providers import MockPayload
from judgmentllm.results import validate_categorical_output, validate_pairwise_output


def rubric() -> Rubric:
    return Rubric(
        rubric_id="001",
        name="Synthetic",
        traits=[
            Trait(
                trait_id="01",
                name="Quality",
                description="Synthetic.",
                levels=[
                    Level(label="low", numeric_value=1),
                    Level(label="high", numeric_value=8),
                ],
            )
        ],
    )


def pair() -> PairTask:
    return PairTask(
        pair_id="001",
        pair_uid="001-r",
        left_item_id="001",
        right_item_id="002",
        trait_id="01",
        presented_order=["002", "001"],
    )


def run_document() -> dict[str, Any]:
    return {
        "task_kind": "rubric",
        "input": "responses.jsonl",
        "rubric": rubric().model_dump(),
        "provider": "mock",
        "endpoint": "synthetic",
        "model": "fixture",
    }


@pytest.mark.parametrize(
    "explanations,prompt_hash,schema_hash",
    [
        (
            False,
            "ce139f31892f79a980b66d7863b4b9fee0efafff45b0bdefb93d3e019fb10401",
            "0122ea448650e0c517e3be3b93313bb1dc4b57e361e0b62d64b1fa3bcf3e7151",
        ),
        (
            True,
            "763e8234bc0d9aeeda1151ffd38eb0fd0e6b7ba2f09df2fdbfce5ed37e5cdb25",
            "6b2bb6d7252f86a97e45ebb870937381697d15c333aa145927d1e8cb9f6f68f3",
        ),
    ],
)
def test_frozen_v1_rubric_prompt_and_schema_bytes(
    explanations: bool, prompt_hash: str, schema_hash: str
) -> None:
    # These digests were captured from the unmodified 6d79503 baseline renderer.
    rendered = render_prompt(
        rubric=rubric(),
        response=ResponseItem(item_id="001", text="Synthetic response."),
        explanations=explanations,
    )
    assert hashlib.sha256(rendered.text.encode()).hexdigest() == prompt_hash
    assert semantic_digest("legacy-schema", rendered.output_schema) == schema_hash
    assert "Reference exemplars:" in rendered.text


def test_frozen_v1_pairwise_prompt_keeps_historical_explicit_tie_contract() -> None:
    rendered = render_prompt(
        pair=pair(),
        left=ResponseItem(item_id="001", text="Synthetic response."),
        right=ResponseItem(item_id="002", text="Other synthetic response."),
        criterion=PairwiseCriterion(
            trait_id="01", name="Quality", description="Compare."
        ),
    )
    assert (
        hashlib.sha256(rendered.text.encode()).hexdigest()
        == "5983c000119f580392cfeba5e968a2ae6d77498a3c1ef0506f10ba0ff65033d0"
    )
    assert (
        semantic_digest("legacy-schema", rendered.output_schema)
        == "aef728999bf9ff4c371cf9db0223bfc4bf9c317c186654e4187a7ff65483eb33"
    )
    assert rendered.output_schema["required"] == ["winner_id", "tie"]
    with pytest.raises(ValueError):
        validate_pairwise_output({"winner_id": "002"}, pair())
    assert (
        validate_pairwise_output({"winner_id": "002", "tie": False}, pair()).tie
        is False
    )
    assert (
        validate_pairwise_contract_output(
            {"winner_id": "002"}, PairwiseOutputContract(pair=pair())
        ).tie
        is False
    )


def test_legacy_whole_validator_still_rejects_separated_layouts() -> None:
    data = {
        "scores": [{"trait_id": "01", "category_label": "high"}],
        "explanations": [{"trait_id": "01", "text": "Synthetic."}],
    }
    with pytest.raises(ValueError):
        validate_categorical_output(data, rubric().traits, explanations=True)
    configured = RubricOutputContract(
        traits=tuple(rubric().traits), explanations=True, output_order="scores_first"
    )
    assert (
        validate_rubric_contract_output(data, configured).scores[0].explanation
        == "Synthetic."
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"schema_version": 2},
        {"output_contract": {}},
        {"contract_version": 2},
        {"prompt": {"output_order": "scores_first"}},
        {"prompt": {"output_order": "together"}},
        {"prompt": {"structured_output": "text"}},
        {"execution": {"output_order": "scores_first", "explanations": True}},
    ],
)
def test_v1_run_documents_reject_future_activation_controls(
    changes: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        RunSpec.model_validate(run_document() | changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"schema_version": 3},
        {"prompt": {"output_order": "scores_first"}},
        {"output_contract": {"contract_version": 2}},
    ],
)
def test_unsupported_configuration_cannot_reach_cli_or_lifecycle_submission(
    tmp_path: Path, changes: dict[str, object]
) -> None:
    document = run_document() | changes
    source = tmp_path / "run.json"
    source.write_text(json.dumps(document), encoding="utf-8")
    store = tmp_path / "store"
    with pytest.raises(LifecycleError) as error:
        run_live(source, store=store)
    assert error.value.category == "validation"
    result = CliRunner().invoke(
        cli.app, ["run", "--config", str(source), "--store", str(store), "--json"]
    )
    assert result.exit_code == 2
    control = json.loads(result.stdout)
    assert control["schema_version"] == 1
    assert control["status"] == "error"
    assert control["error"]["category"] == "validation"
    assert not store.exists()


def test_mock_wire_protocol_has_no_uncommitted_contract_controls() -> None:
    assert MockPayload.model_fields["protocol"].default == "judgmentllm-mock-v1"
    assert (
        not {"output_order", "output_contract", "contract_version", "structured_output"}
        & MockPayload.model_fields.keys()
    )


def test_ordered_contract_serialization_does_not_change_canonical_semantic_hashing() -> (
    None
):
    selected = tuple(rubric().traits)
    first = RubricOutputContract(
        traits=selected, explanations=True, output_order="scores_first"
    )
    last = RubricOutputContract(
        traits=selected, explanations=True, output_order="explanations_first"
    )
    first_bytes, last_bytes = (
        serialize_output_schema(first),
        serialize_output_schema(last),
    )
    assert first_bytes != last_bytes
    assert list(json.loads(first_bytes)["properties"]) == ["scores", "explanations"]
    assert list(json.loads(last_bytes)["properties"]) == ["explanations", "scores"]
    value = {"z": [None, True, 7], "a": {"é": "cafe\u0301", "β": "\r\n"}}
    expected = '{"a":{"é":"cafe\u0301","β":"\\r\\n"},"z":[null,true,7]}'.encode()
    assert (
        canonical_bytes(value)
        == canonical_bytes(dict(reversed(tuple(value.items()))))
        == expected
    )
    assert (
        semantic_digest("input", value)
        == hashlib.sha256(b'[1,"input",' + expected + b"]").hexdigest()
    )
    assert b'"properties":{"scores":' in first_bytes
    assert b'"properties":{"explanations":' in last_bytes


def test_contract_operations_need_no_credentials_network_or_provider_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("provider-neutral contract operations attempted network access")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    for key in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GOOGLE_API_KEY",
        "TOGETHER_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    configured = RubricOutputContract(traits=tuple(rubric().traits))
    assert generate_output_schema(configured)
    assert serialize_output_schema(configured)
    assert (
        validate_rubric_contract_output(
            '{"scores":[{"trait_id":"01","category_label":"high"}]}', configured
        )
        .scores[0]
        .category_label
        == "high"
    )
    assert (
        validate_pairwise_contract_output(
            '{"winner_id":"002"}', PairwiseOutputContract(pair=pair())
        ).winner_id
        == "002"
    )


@pytest.mark.parametrize("mutation", ["output_order", "output_contract", "version"])
def test_preparation_rejects_copied_v2_controls_before_any_run_or_commitment(
    tmp_path: Path, mutation: str
) -> None:
    spec = RunSpec.model_validate(run_document())
    if mutation == "output_order":
        spec = spec.model_copy(
            update={
                "prompt": spec.prompt.model_copy(
                    update={"output_order": "scores_first"}
                )
            }
        )
    elif mutation == "output_contract":
        spec = spec.model_copy(
            update={
                "output_contract": RubricOutputContract(traits=tuple(rubric().traits))
            }
        )
    else:
        spec = spec.model_copy(update={"schema_version": 2})
    store = tmp_path / "store"
    with pytest.raises(PreparationError):
        prepare_run(spec, store=store, run_id="unsupported")
    # Existing preparation initializes metadata before validating configuration.
    # It must not create a run, immutable manifest, request, or provider observation.
    with Repository(store) as repository:
        for table in ("runs", "requests", "tasks", "attempts", "submissions"):
            assert (
                repository.connection.execute(
                    f"SELECT COUNT(*) FROM {table}"
                ).fetchone()[0]
                == 0
            )
    assert not list(store.rglob("manifest.json"))
    assert not (store / "mock-service.sqlite3").exists()


@pytest.mark.parametrize(
    "control",
    ["output_order", "output_contract", "contract_version", "structured_output"],
)
def test_mock_payload_rejects_new_contract_controls(control: str) -> None:
    data: dict[str, object] = {
        "service_identity": "synthetic-service",
        "model": "fixture",
        "request_uid": "request",
        "dependency_sha256": "a" * 64,
        "prompt": "Synthetic prompt.",
        "output_schema": {},
        "members": (
            {"task_id": "task", "trait_id": "01", "category_labels": ("low", "high")},
        ),
        "task_kind": "rubric",
        "explanations": False,
        "allow_ties": False,
        "cache_policy": "off",
        "fixture_sha256": "b" * 64,
    }
    assert MockPayload.model_validate(data).protocol == "judgmentllm-mock-v1"
    with pytest.raises(ValueError):
        MockPayload.model_validate(data | {control: "unsupported"})
