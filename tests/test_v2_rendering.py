"""Exact v2 prompt examples, strict version dispatch and protected rendering."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from judgmentllm import (
    RunSpec,
    RunSpecV2,
    load_run_spec,
    preview_prompt,
    validate_run_spec,
    validate_run_spec_json,
)
from judgmentllm.cli import app
from judgmentllm.config import ConfigurationError
from judgmentllm.models import Exemplar, PairTask, ResponseItem, Rubric
from judgmentllm.output_contracts import serialize_output_schema
from judgmentllm.planning import PreflightError, iter_requests, validate_run
from judgmentllm.prompts import PromptError, RenderedPromptV2

FIXTURES = Path(__file__).parent / "fixtures" / "v2-prompts"
DOC = Path(__file__).parents[1] / "docs" / "assessment-prompt-examples.md"


def rubric() -> Rubric:
    return Rubric.model_validate(
        {
            "rubric_id": "writing_demo",
            "name": "Synthetic writing rubric",
            "description": "Assess only the stated dimensions.",
            "traits": [
                {
                    "trait_id": trait_id,
                    "name": name,
                    "description": description,
                    "levels": [
                        {"label": label, "description": text}
                        for label, text in [
                            ("low", "Limited control."),
                            ("medium", "Adequate control with an error."),
                            ("high", "Consistent control."),
                        ]
                    ],
                }
                for trait_id, name, description in [
                    ("cohesion", "Cohesion", "How logically ideas progress."),
                    (
                        "grammar",
                        "Grammar",
                        "How consistently grammar supports clarity.",
                    ),
                ]
            ],
        }
    )


def example_spec(tmp_path: Path, number: int = 3) -> RunSpecV2:
    source = tmp_path / "responses.jsonl"
    responses = [
        ResponseItem(
            item_id="essay_001",
            text="The class planted seeds. Next, we measured the seedlings. The results shows steady growth.",
        )
    ]
    if number > 4:
        responses = [
            ResponseItem(
                item_id="essay_001", text="Seeds grew. We did a thing. Results."
            ),
            ResponseItem(
                item_id="essay_002",
                text="We planted seeds, measured the seedlings each week, and recorded steady growth.",
            ),
        ]
    source.write_text(
        "".join(item.model_dump_json() + "\n" for item in responses), encoding="utf-8"
    )
    template = tmp_path / "template.j2"
    template.write_text(
        (FIXTURES / f"{number}.j2").read_text(encoding="utf-8"), encoding="utf-8"
    )
    data: dict[str, Any] = {
        "schema_version": 2,
        "task_kind": "rubric",
        "input": source,
        "rubric": rubric(),
        "provider": "mock",
        "endpoint": "synthetic",
        "model": "synthetic-v2",
        "execution": {"grouping": "grouped", "explanations": number in (2, 3, 4)},
        "prompt": {
            "file": template,
            "structured_output": "text",
            "output_order": {3: "scores_first", 4: "explanations_first"}.get(
                number, "together"
            ),
        },
    }
    if number > 4:
        pair = PairTask(
            pair_id="001",
            pair_uid="001",
            left_item_id="essay_001",
            right_item_id="essay_002",
            trait_id="overall",
        )
        pairs = tmp_path / "pairs.jsonl"
        pairs.write_text(pair.model_dump_json() + "\n", encoding="utf-8")
        data.update(
            task_kind="pairwise",
            rubric=None,
            pairs=pairs,
            criteria=[
                {
                    "trait_id": "overall",
                    "name": "Overall effectiveness",
                    "description": "Choose the response that communicates the sequence most clearly.",
                }
            ],
            execution={"grouping": "independent", "explanations": False},
            allow_ties=number == 6,
        )
    return RunSpecV2.model_validate(data)


def render(spec: RunSpecV2) -> RenderedPromptV2:
    prompt = (
        preview_prompt(spec, item_id="essay_001")
        if spec.task_kind == "rubric"
        else preview_prompt(spec, pair_uid="001")
    )
    assert isinstance(prompt, RenderedPromptV2)
    return prompt


@pytest.mark.parametrize("number", range(1, 7))
def test_six_authoritative_examples_exact_text_and_ordered_schema(
    tmp_path: Path, number: int
) -> None:
    spec = example_spec(tmp_path, number)
    actual = render(spec)
    expected = (FIXTURES / f"{number}.txt").read_bytes()
    schema = (FIXTURES / f"{number}.schema.json").read_bytes()
    assert actual.text.encode("utf-8") == expected
    assert serialize_output_schema(actual.output_commitment.contract) == schema
    assert actual.output_commitment.schema_text.encode("utf-8") == schema
    documentation = DOC.read_text(encoding="utf-8")
    assert (
        re.findall(r"```text\n(.*?)```", documentation, re.S)[number - 1].encode(
            "utf-8"
        )
        == expected
    )
    assert spec.prompt.file is not None
    assert re.findall(r"```jinja2\n(.*?)```", documentation, re.S)[
        number - 1
    ] == spec.prompt.file.read_text(encoding="utf-8")


@pytest.mark.parametrize("version", [None, 1, 2])
def test_root_version_dispatch_preserves_v1(
    tmp_path: Path, version: int | None
) -> None:
    spec = example_spec(tmp_path, 1)
    document = spec.model_dump(mode="json")
    document.pop("schema_version")
    document["prompt"] = {"preset": "rubric_scores_only"}
    if version is not None:
        document["schema_version"] = version
    checked = validate_run_spec(document)
    assert type(checked) is (RunSpecV2 if version == 2 else RunSpec)
    assert checked.schema_version == (version or 1)
    assert type(validate_run_spec_json(json.dumps(document))) is type(checked)
    path = tmp_path / "run.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    assert type(load_run_spec(path)) is type(checked)


@pytest.mark.parametrize("version", [True, False, 0, 3, "2", 2.0, None])
def test_invalid_explicit_version(tmp_path: Path, version: object) -> None:
    data = example_spec(tmp_path).model_dump(mode="python")
    data["schema_version"] = version
    with pytest.raises((ConfigurationError, ValidationError)):
        validate_run_spec(data)


@pytest.mark.parametrize(
    "field,value", [("output_order", "scores_first"), ("structured_output", "text")]
)
def test_v1_rejects_v2_controls(tmp_path: Path, field: str, value: str) -> None:
    data = example_spec(tmp_path).model_dump(mode="python")
    data["schema_version"] = 1
    data["prompt"] = {field: value}
    with pytest.raises(ValidationError):
        validate_run_spec(data)
    with pytest.raises(ValidationError):
        RunSpec.model_validate(example_spec(tmp_path))


@pytest.mark.parametrize(
    "changes",
    [
        {"execution": {"explanations": False}},
        {"prompt": {"output_order": "invalid"}},
        {"prompt": {"structured_output": "json"}},
        {"prompt": {"output_schema": {"type": "object"}}},
        {"generation": {"reasoning": {"effort": "high"}}},
        {"generation": {"top_k": 3}},
    ],
)
def test_v2_invalid_configuration_combinations(
    tmp_path: Path, changes: dict[str, object]
) -> None:
    data = example_spec(tmp_path).model_dump(mode="python")
    data.update(changes)
    with pytest.raises(ValidationError):
        validate_run_spec(data)


def test_pairwise_rejects_nondefault_layout(tmp_path: Path) -> None:
    data = example_spec(tmp_path, 5).model_dump(mode="python")
    data["prompt"]["output_order"] = "explanations_first"
    with pytest.raises(ValidationError, match="pairwise"):
        validate_run_spec(data)


@pytest.mark.parametrize(
    "mutation", ["copied_field", "nested_field", "repair", "wrong_version"]
)
def test_mutated_models_are_revalidated(tmp_path: Path, mutation: str) -> None:
    spec = example_spec(tmp_path)
    if mutation == "copied_field":
        spec = spec.model_copy(update={"arbitrary_json": {}})
    elif mutation == "nested_field":
        spec.prompt = spec.prompt.model_copy(update={"output_order": "unknown"})
    elif mutation == "repair":
        spec.prompt.preset = None
        spec.prompt.file = None
    else:
        spec = spec.model_copy(update={"schema_version": 1})
    with pytest.raises((ConfigurationError, ValidationError)):
        validate_run_spec(spec)


def test_structured_output_selection_and_strict_diagnostics(tmp_path: Path) -> None:
    spec = example_spec(tmp_path)
    spec.prompt.structured_output = "auto"
    spec.strict_capabilities = True
    assert validate_run(spec).valid
    prompt = render(spec)
    assert prompt.output_commitment.selection.mechanism == "text"
    assert (
        prompt.output_commitment.selection.reason == "native_unavailable_text_selected"
    )
    spec.prompt.structured_output = "native"
    assert not validate_run(spec).valid
    with pytest.raises(PreflightError, match="native"):
        render(spec)
    spec.provider = "unknown"
    spec.prompt.structured_output = "auto"
    assert not validate_run(spec).valid


@pytest.mark.parametrize("count", [0, 1, 2])
@pytest.mark.parametrize("advanced", [False, True])
def test_v2_exemplars_complete_ordered_and_zero_omission(
    tmp_path: Path, count: int, advanced: bool
) -> None:
    spec = example_spec(tmp_path)
    spec.exemplars = [
        Exemplar(
            exemplar_id=f"{index:03d}",
            text="same\nFull café reference",
            scores={"grammar": "high"},
            rationales={"grammar": "Complete evidence."},
            metadata={"source": "synthetic"},
        )
        for index in range(count)
    ]
    if advanced:
        assert spec.prompt.file is not None
        spec.prompt.advanced = True
        spec.prompt.file.write_text(
            "{{ rubric }}\n{% if exemplar_items %}REFERENCE EXAMPLES\n{% for example in exemplar_items %}{{ example['block'] }}\n{% endfor %}{% endif %}{{ output_instructions }}\n{{ response }}",
            encoding="utf-8",
        )
    else:
        spec.prompt.file = None
        spec.prompt.preset = "rubric_scores_with_explanations"
    text = render(spec).text
    assert text.count("same\nFull café reference") == count
    assert ("exemplars:" in text or "REFERENCE EXAMPLES" in text) == bool(count)
    assert [exemplar.exemplar_id for exemplar in spec.exemplars] == render(
        spec
    ).exemplar_ids
    if count:
        assert text.index("Exemplar 000") < text.index("Response essay_001")
        assert text.count('"grammar": "high"') == count
        assert text.count("Complete evidence.") == count
        assert text.count('"source": "synthetic"') == count


@pytest.mark.parametrize(
    "bad",
    [
        "{{ exemplar_items[1]['block'] }}{{ exemplar_items[0]['block'] }}",
        "{{ exemplar_items[0]['block'] }}",
        "{{ exemplars }}{{ exemplars }}",
        "{{ exemplars|upper }}",
    ],
)
def test_v2_rejects_missing_duplicated_reordered_altered_exemplars(
    tmp_path: Path, bad: str
) -> None:
    spec = example_spec(tmp_path)
    spec.exemplars = [
        Exemplar(exemplar_id="001", text=""),
        Exemplar(exemplar_id="002", text=""),
    ]
    spec.prompt.advanced = True
    assert spec.prompt.file is not None
    spec.prompt.file.write_text(
        "{{ rubric }}{{ response }}{{ output_instructions }}" + bad, encoding="utf-8"
    )
    with pytest.raises(PromptError):
        render(spec)


@pytest.mark.parametrize(
    "output",
    [
        "",
        "{{ output_instructions }}{{ output_instructions }}",
        "{{ output_instructions|upper }}",
    ],
)
def test_v2_output_block_remains_protected(tmp_path: Path, output: str) -> None:
    spec = example_spec(tmp_path)
    spec.prompt.advanced = True
    assert spec.prompt.file is not None
    spec.prompt.file.write_text("{{ rubric }}{{ response }}" + output, encoding="utf-8")
    with pytest.raises(PromptError):
        render(spec)


def test_v2_identity_retains_output_order_distinction(tmp_path: Path) -> None:
    spec = example_spec(tmp_path)
    first_request = next(iter_requests(spec))
    first_schema = render(spec).output_commitment.schema_text
    spec.prompt.output_order = "explanations_first"
    second_request = next(iter_requests(spec))
    second_schema = render(spec).output_commitment.schema_text
    assert first_request.request_uid != second_request.request_uid
    assert first_schema != second_schema
    first_semantic, second_semantic = (
        json.loads(first_schema),
        json.loads(second_schema),
    )
    first_semantic["required"].sort()
    second_semantic["required"].sort()
    assert first_semantic == second_semantic


def test_cli_preview_json_metadata_and_exact_plain_bytes(tmp_path: Path) -> None:
    spec = example_spec(tmp_path)
    spec.prompt.structured_output = "auto"
    config = tmp_path / "run.json"
    config.write_text(spec.model_dump_json(), encoding="utf-8")
    arguments = [
        "templates",
        "preview",
        "--config",
        str(config),
        "--item-id",
        "essay_001",
    ]
    runner = CliRunner()
    plain = runner.invoke(app, arguments)
    assert plain.exit_code == 0, plain.output
    assert plain.stdout == render(spec).text
    machine = runner.invoke(app, [*arguments, "--json"])
    assert machine.exit_code == 0, machine.output
    envelope = json.loads(machine.stdout)
    assert envelope["schema_version"] == 1
    assert envelope["command"] == "templates preview"
    assert envelope["data"]["text"] == plain.stdout
    assert envelope["data"]["output_commitment"]["selection"]["mechanism"] == "text"
    assert all(
        json.loads(line)["event"] == "diagnostic"
        for line in machine.stderr.splitlines()
    )
    assert not (tmp_path / "run-data").exists()
    bad = runner.invoke(
        app, ["templates", "preview", "--json", "--config", str(config), "--unknown"]
    )
    assert bad.exit_code == 2
    assert json.loads(bad.stdout)["error"]["category"] == "usage"
    assert "The class planted" not in bad.stdout + bad.stderr


@pytest.mark.parametrize("grouping", ["independent", "grouped", "auto"])
@pytest.mark.parametrize("order", ["together", "scores_first", "explanations_first"])
def test_real_planned_groups_have_exact_ordered_unequal_contracts(
    tmp_path: Path,
    grouping: str,
    order: Literal["together", "scores_first", "explanations_first"],
) -> None:
    spec = example_spec(tmp_path)
    data = spec.model_dump(mode="python")
    data["rubric"]["traits"][0]["trait_id"] = "001"
    data["rubric"]["traits"][1]["trait_id"] = "002"
    data["rubric"]["traits"][0]["levels"] = [
        {"label": "début", "numeric_value": 1.0},
        {"label": "進歩", "numeric_value": 7.0},
    ]
    data["rubric"]["traits"][1]["levels"] = [
        {"label": "0", "numeric_value": 0.0},
        {"label": "1", "numeric_value": 2.0},
        {"label": "2", "numeric_value": 20.0},
    ]
    execution: dict[str, Any] = {"grouping": grouping, "explanations": True}
    if grouping == "auto":
        execution["max_traits_per_request"] = 1
    elif grouping == "grouped":
        execution["groups"] = [["002", "001"]]
    data["execution"] = execution
    data["prompt"]["output_order"] = order
    spec = RunSpecV2.model_validate(data)
    requests = list(iter_requests(spec))
    assert len(requests) == (1 if grouping == "grouped" else 2)
    for request in requests:
        actual = preview_prompt(
            spec, item_id="essay_001", trait_id=request.trait_ids[0]
        )
        assert isinstance(actual, RenderedPromptV2)
        contract = actual.output_commitment.contract
        assert contract.task_kind == "rubric"
        assert tuple(trait.trait_id for trait in contract.traits) == request.trait_ids
        assert contract.traits[0].numeric_values == (
            [0.0, 2.0, 20.0] if request.trait_ids[0] == "002" else [1.0, 7.0]
        )
        assert actual.output_commitment.root_properties == (
            ("explanations", "scores")
            if order == "explanations_first"
            else ("scores", "explanations")
            if order == "scores_first"
            else ("scores",)
        )
    assert spec.rubric is not None
    assert [trait.trait_id for trait in spec.rubric.traits] == ["001", "002"]


@pytest.mark.parametrize("order", ["together", "scores_first", "explanations_first"])
def test_v2_builtin_empty_exemplars_omit_entire_section(
    tmp_path: Path, order: Literal["together", "scores_first", "explanations_first"]
) -> None:
    spec = example_spec(tmp_path)
    spec.prompt.file = None
    spec.prompt.preset = "rubric_scores_with_explanations"
    spec.prompt.output_order = order
    actual = render(spec)
    assert "Reference exemplars" not in actual.text
    assert "empty" not in actual.text.lower()
    assert "placeholder" not in actual.text.lower()


@pytest.mark.parametrize("number", [5, 6])
def test_pairwise_reversed_orientation_and_required_explanation(
    tmp_path: Path, number: int
) -> None:
    spec = example_spec(tmp_path, number)
    assert spec.pairs is not None
    pair = PairTask.model_validate_json(spec.pairs.path.read_text(encoding="utf-8"))
    pair.presented_order = ["essay_002", "essay_001"]
    spec.pairs.path.write_text(pair.model_dump_json() + "\n", encoding="utf-8")
    spec.execution.explanations = True
    actual = render(spec)
    assert actual.text.index("First response essay_002") < actual.text.index(
        "Second response essay_001"
    )
    contract = actual.output_commitment.contract
    assert contract.task_kind == "pairwise"
    assert contract.pair.original_order == ["essay_001", "essay_002"]
    assert contract.pair.presented_order == ["essay_002", "essay_001"]
    assert actual.output_commitment.root_properties == (
        ("winner_id", "tie", "explanation")
        if number == 6
        else ("winner_id", "explanation")
    )
    assert "nonblank explanation" in actual.text


@pytest.mark.parametrize(
    "attack",
    [
        "{{ response.__class__ }}",
        "{{ response.upper() }}",
        "{% macro m() %}x{% endmacro %}",
        "{{ response|attr('__class__') }}",
        "{% for a in traits %}{% for b in exemplar_items %}x{% endfor %}{% endfor %}",
    ],
)
def test_v2_keeps_existing_sandbox_boundary(tmp_path: Path, attack: str) -> None:
    spec = example_spec(tmp_path)
    spec.prompt.advanced = True
    assert spec.prompt.file is not None
    spec.prompt.file.write_text(
        attack + "{{ rubric }}{{ response }}{{ output_instructions }}", encoding="utf-8"
    )
    with pytest.raises(PromptError):
        render(spec)


def test_v2_resource_limit_fails_without_exemplar_truncation(tmp_path: Path) -> None:
    spec = example_spec(tmp_path)
    spec.exemplars = [Exemplar(exemplar_id="001", text="complete exemplar" * 1000)]
    spec.prompt.file = None
    spec.prompt.preset = "rubric_scores_with_explanations"
    spec.prompt.limits.max_output_bytes = 4000
    with pytest.raises(PromptError, match="safeguard"):
        render(spec)
    assert len(spec.exemplars[0].text) == 17000


def test_cli_preview_load_errors_do_not_echo_content_and_alias_help(
    tmp_path: Path,
) -> None:
    config = tmp_path / "run.yaml"
    config.write_text(
        "schema_version: 2\nPRIVATE_ASSESSMENT_SENTINEL: first\nPRIVATE_ASSESSMENT_SENTINEL: second\n",
        encoding="utf-8",
    )
    runner = CliRunner()
    for machine in (False, True):
        arguments = [
            "templates",
            "preview",
            "--config",
            str(config),
            "--item-id",
            "001",
        ]
        result = runner.invoke(app, [*arguments, *(["--json"] if machine else [])])
        assert result.exit_code == 2
        assert "PRIVATE_ASSESSMENT_SENTINEL" not in result.stdout + result.stderr
        if machine:
            assert json.loads(result.stdout)["error"]["category"] == "validation"
    result = runner.invoke(app, ["runs", "resume", "--help", "--json"])
    assert result.exit_code == 0
    envelope = json.loads(result.stdout)
    assert envelope["schema_version"] == 1
    assert envelope["command"] == "runs resume"


@pytest.mark.parametrize("target_version", [1, 2])
def test_mutated_model_class_cannot_change_version_identity(
    tmp_path: Path, target_version: int
) -> None:
    v2 = example_spec(tmp_path, 1)
    v1_data = v2.model_dump(mode="python")
    v1_data["schema_version"] = 1
    v1_data["prompt"] = {"preset": "rubric_scores_only"}
    v1 = RunSpec.model_validate(v1_data)
    source, target = (v2, v1) if target_version == 1 else (v1, v2)
    changed = source.model_copy(
        update={"schema_version": target_version, "prompt": target.prompt}
    )
    with pytest.raises(ConfigurationError, match="typed model"):
        validate_run_spec(changed)
    assert type(validate_run_spec(target.model_dump(mode="python"))) is type(target)


@pytest.mark.parametrize(
    "mutation", ["schema", "property_order", "renderer_version", "schema_version"]
)
def test_rendered_v2_metadata_rejects_schema_or_version_substitution(
    tmp_path: Path, mutation: str
) -> None:
    actual = render(example_spec(tmp_path))
    data = actual.model_dump(mode="python")
    if mutation == "schema":
        data["output_schema"] = {"type": "string"}
    elif mutation == "property_order":
        properties = data["output_schema"]["properties"]
        data["output_schema"]["properties"] = dict(reversed(list(properties.items())))
    else:
        data[mutation] = 2.0
    with pytest.raises(ValidationError):
        RenderedPromptV2.model_validate(data)
