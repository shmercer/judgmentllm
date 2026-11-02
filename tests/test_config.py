"""Safe configuration loading and resolved version-1 public contracts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from judgmentllm.config import (
    ConfigurationError,
    ExecutionSpec,
    InputSpec,
    PromptSpec,
    RunSpec,
    load_rubric,
    load_run_spec,
    read_document,
)
from judgmentllm.models import Exemplar, Level, PairwiseCriterion, Rubric, Trait


def _rubric() -> Rubric:
    return Rubric(
        rubric_id="rubric",
        name="Synthetic",
        traits=[
            Trait(
                trait_id="trait",
                name="Trait",
                description="Compare detail.",
                levels=[Level(label="Low"), Level(label="High")],
            )
        ],
    )


def _run_document(tmp_path: Path) -> dict[str, object]:
    (tmp_path / "rubric.json").write_text(_rubric().model_dump_json(), encoding="utf-8")
    return {
        "schema_version": 1,
        "task_kind": "rubric",
        "input": "responses.csv",
        "rubric": "rubric.json",
        "provider": "synthetic",
        "endpoint": "offline",
        "model": "unknown-model",
    }


def _load(tmp_path: Path, data: dict[str, object]) -> RunSpec:
    path = tmp_path / "run.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return load_run_spec(path)


def test_paths_resolve_against_containing_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = _run_document(tmp_path)
    data["prompt"] = {
        "file": "templates/custom.txt",
        "advanced": True,
        "limits": {"max_output_bytes": 300},
    }
    data["output_path"] = "local-output"
    monkeypatch.chdir(tmp_path.parent)
    spec = _load(tmp_path, data)
    assert spec.input.path == tmp_path / "responses.csv"
    assert spec.prompt.file == tmp_path / "templates/custom.txt"
    assert spec.prompt.limits.max_output_bytes == 300
    assert spec.output_path == tmp_path / "local-output"
    assert not spec.output_path.exists()
    assert spec.execution.cache_policy == "auto"


def test_load_inline_rubric_and_typed_input(tmp_path: Path) -> None:
    data = _run_document(tmp_path)
    data["rubric"] = _rubric().model_dump(mode="json")
    data["input"] = {"path": "data.bin", "format": "jsonl", "allow_empty_text": True}
    data["normalization"] = {"probability_sum_tolerance": 0.001}
    spec = _load(tmp_path, data)
    assert spec.rubric == _rubric()
    assert spec.input.format == "jsonl"
    assert spec.input.allow_empty_text
    assert spec.normalization.probability_sum_tolerance == 0.001


def test_load_exemplar_document(tmp_path: Path) -> None:
    data = _run_document(tmp_path)
    data["exemplars"] = "exemplars.yaml"
    (tmp_path / "exemplars.yaml").write_text(
        "schema_version: 1\nexemplars:\n  - exemplar_id: anchor\n    text: Synthetic reference.\n    scores: {trait: High}\n",
        encoding="utf-8",
    )
    spec = _load(tmp_path, data)
    assert spec.exemplars[0].exemplar_id == "anchor"
    assert spec.exemplars[0].scores == {"trait": "High"}


def test_load_pairwise_criteria_document_and_defaults(tmp_path: Path) -> None:
    criterion = PairwiseCriterion(
        trait_id="quality", name="Quality", description="Prefer clearer detail."
    )
    (tmp_path / "criteria.json").write_text(
        json.dumps({"schema_version": 1, "criteria": [criterion.model_dump()]}),
        encoding="utf-8",
    )
    spec = _load(
        tmp_path,
        {
            "task_kind": "pairwise",
            "input": "responses.jsonl",
            "pairs": {"path": "pairs.csv"},
            "criteria": "criteria.json",
            "provider": "synthetic",
            "endpoint": "choice",
            "model": "unlisted",
            "execution": {"explanations": True},
        },
    )
    assert spec.criteria == [criterion]
    assert spec.pairs == InputSpec(path=tmp_path / "pairs.csv")
    assert spec.execution.cache_policy == "off"
    assert spec.prompt.preset == "pairwise_forced_choice"


def test_explanation_default_preset(tmp_path: Path) -> None:
    data = _run_document(tmp_path)
    data["execution"] = {"explanations": True}
    assert _load(tmp_path, data).prompt.preset == "rubric_scores_with_explanations"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("schema_version", True, "schema_version"),
        ("schema_version", "1", "schema_version"),
        ("schema_version", 2, "schema_version"),
        ("input", "data.tsv", "format"),
        ("provider", " ", "nonempty"),
        ("model", " padded ", "nonempty"),
        ("model", "bad\nmodel", "identifier"),
        ("output_path", None, "instance of Path"),
        ("output_path", "", "empty"),
        ("api_key", "synthetic-secret", "Extra inputs"),
        ("rubric", None, "requires rubric"),
        ("pairs", "pairs.csv", "pairwise-only"),
        ("allow_ties", True, "pairwise-only"),
        ("execution", {"unknown": 1}, "Extra inputs"),
        ("execution", {"concurrency": 0}, "greater than 0"),
        (
            "execution",
            {"grouping": "independent", "groups": [["trait"]]},
            "requires grouping",
        ),
        ("prompt", {"preset": "unknown"}, "unknown prompt preset"),
        ("prompt", {"preset": "rubric_scores_with_explanations"}, "incompatible"),
        ("prompt", {"preset": "pairwise_forced_choice"}, "incompatible"),
        ("prompt", {"file": "x.txt", "preset": "rubric_scores_only"}, "not both"),
        ("prompt", {"advanced": True}, "custom prompt.file"),
        ("prompt", {"file": ""}, "empty"),
        ("normalization", {"probability_sum_tolerance": -1}, "greater than or equal"),
    ],
)
def test_invalid_run_fields_fail_with_context(
    tmp_path: Path, field: str, value: object, message: str
) -> None:
    data = _run_document(tmp_path)
    data[field] = value
    with pytest.raises(ConfigurationError, match=message) as error:
        _load(tmp_path, data)
    assert "run.json" in str(error.value)
    assert "synthetic-secret" not in str(error.value)


@pytest.mark.parametrize(
    ("content", "suffix", "message"),
    [
        ("model: one\nmodel: two", ".yaml", "duplicate YAML key"),
        ('{"a":1,"a":2}', ".json", "duplicate JSON key"),
        ('{"a":{"b":1,"b":2}}', ".json", "duplicate JSON key"),
        ('{"a":NaN}', ".json", "nonfinite"),
        ("!!python/object/apply:os.system ['false']", ".yaml", "constructor"),
        ("a: [", ".yaml", "expected"),
        ("broken", ".json", "Expecting"),
        ("{}", ".txt", "extension"),
        ("? [a, b]\n: value", ".yaml", "scalar values"),
    ],
)
def test_unsafe_or_malformed_documents(
    tmp_path: Path, content: str, suffix: str, message: str
) -> None:
    source = tmp_path / f"document{suffix}"
    source.write_text(content, encoding="utf-8")
    with pytest.raises(ConfigurationError, match=message):
        read_document(source)


def test_missing_invalid_encoding_and_nonobject_documents(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="absent.yaml"):
        read_document(tmp_path / "absent.yaml")
    source = tmp_path / "bad.yaml"
    source.write_bytes(b"\xff")
    with pytest.raises(ConfigurationError):
        read_document(source)
    for content in ("[]", '{"input": {"path": 1}}'):
        source = tmp_path / "run.json"
        source.write_text(content, encoding="utf-8")
        with pytest.raises(ConfigurationError):
            load_run_spec(source)
    source = tmp_path / "run.yaml"
    source.write_text("1: value", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="string keys"):
        load_run_spec(source)


def test_invalid_referenced_rubric_and_exemplars(tmp_path: Path) -> None:
    data = _run_document(tmp_path)
    (tmp_path / "rubric.json").write_text('{"schema_version":2}', encoding="utf-8")
    with pytest.raises(ConfigurationError, match="rubric.json"):
        _load(tmp_path, data)
    data = _run_document(tmp_path)
    data["exemplars"] = "anchors.json"
    (tmp_path / "anchors.json").write_text(
        '{"schema_version":2,"exemplars":[]}', encoding="utf-8"
    )
    with pytest.raises(ConfigurationError, match="schema_version"):
        _load(tmp_path, data)


def test_pairwise_constraints_and_custom_ties() -> None:
    base: dict[str, object] = {
        "task_kind": "pairwise",
        "input": "responses.csv",
        "pairs": "pairs.csv",
        "criteria": [
            {"trait_id": "quality", "name": "Quality", "description": "Compare detail."}
        ],
        "provider": "synthetic",
        "endpoint": "choice",
        "model": "unknown",
    }
    updates: list[tuple[dict[str, object], str]] = [
        ({"rubric": _rubric()}, "uses criteria"),
        ({"criteria": []}, "requires criteria"),
        ({"criteria": [base["criteria"], base["criteria"]]}, "valid dictionary"),
        ({"execution": {"grouping": "grouped"}}, "independent"),
        ({"allow_ties": True}, "custom prompt"),
    ]
    for update, message in updates:
        with pytest.raises(ValidationError, match=message):
            RunSpec.model_validate({**base, **update})
    criterion = PairwiseCriterion(
        trait_id="quality", name="Quality", description="Compare detail."
    )
    with pytest.raises(ValidationError, match="duplicate"):
        RunSpec.model_validate({**base, "criteria": [criterion, criterion]})
    spec = RunSpec.model_validate(
        {**base, "allow_ties": True, "prompt": {"file": "ties.txt"}}
    )
    assert spec.allow_ties and spec.prompt.file == Path("ties.txt")


def test_nested_models_validation_and_model_roundtrip() -> None:
    with pytest.raises(ValidationError, match="empty"):
        InputSpec.model_validate({"path": ""})
    assert InputSpec(path=Path("INPUT.CSV")).format == "csv"
    assert ExecutionSpec(grouping="auto").max_traits_per_request is None
    assert PromptSpec(file=Path("custom.txt")).file == Path("custom.txt")
    spec = RunSpec(
        task_kind="rubric",
        input=InputSpec(path=Path("responses.csv")),
        rubric=_rubric(),
        provider="synthetic",
        endpoint="offline",
        model="unlisted",
        exemplars=[Exemplar(exemplar_id="anchor", text="Synthetic.")],
    )
    assert RunSpec.model_validate_json(spec.model_dump_json()) == spec


def test_bundled_rubric_loads() -> None:
    source = Path(__file__).resolve().parents[1] / "examples/rubric.yaml"
    assert len(load_rubric(source).traits) >= 2
