"""Synthetic offline prompts, inclusion guarantees, and sandbox boundaries."""

import hashlib
from pathlib import Path

import pytest
from pydantic import ValidationError

from judgmentllm.models import (
    Exemplar,
    Level,
    PairTask,
    PairwiseCriterion,
    ResponseItem,
    Rubric,
    Trait,
)
from judgmentllm.prompts import (
    PromptError,
    RenderLimits,
    _environment,  # pyright: ignore[reportPrivateUsage]
    _namespace,  # pyright: ignore[reportPrivateUsage]
    _protect,  # pyright: ignore[reportPrivateUsage]
    _verify_and_unwrap,  # pyright: ignore[reportPrivateUsage]
    get_template,
    init_template,
    list_templates,
    render_prompt,
)


@pytest.fixture
def rubric() -> Rubric:
    return Rubric(
        rubric_id="writing",
        name="Synthetic writing",
        description="Assess organization and evidence separately.",
        traits=[
            Trait(
                trait_id="organization",
                name="Organization",
                description="Judge coherence.",
                levels=[
                    Level(label="Beginning", description="Few connections."),
                    Level(label="Proficient", description="Clear connections."),
                ],
            ),
            Trait(
                trait_id="evidence",
                name="Evidence",
                description="Judge relevant support.",
                levels=[
                    Level(label="1", numeric_value=0.0),
                    Level(label="2", numeric_value=5.0),
                    Level(label="3", numeric_value=20.0),
                ],
            ),
        ],
    )


@pytest.fixture
def response() -> ResponseItem:
    return ResponseItem(item_id="essay_1", text="A synthetic robot describes a garden.")


@pytest.fixture
def exemplars() -> list[Exemplar]:
    return [
        Exemplar(
            exemplar_id="anchor_b",
            text="First synthetic anchor.\nIt has two lines.",
            scores={"organization": "Beginning"},
            rationales={"organization": "Connections are sparse."},
            metadata={"source": "synthetic", "index": 2},
        ),
        Exemplar(
            exemplar_id="anchor_a",
            text="Second synthetic anchor with café punctuation.",
            scores={"evidence": "3"},
            rationales={"evidence": "Relevant invented support."},
        ),
    ]


@pytest.fixture
def pair() -> PairTask:
    return PairTask(
        pair_id="pair_1",
        pair_uid="pair_occurrence_1",
        left_item_id="left",
        right_item_id="right",
        trait_id="quality",
        presented_order=["right", "left"],
    )


@pytest.fixture
def criterion() -> PairwiseCriterion:
    return PairwiseCriterion(
        trait_id="quality",
        name="Quality",
        description="Prefer clear invented reasoning.",
    )


def test_presets_and_safe_initialization(tmp_path: Path) -> None:
    presets = list_templates()
    assert [entry.preset for entry in presets] == [
        "rubric_scores_only",
        "rubric_scores_with_explanations",
        "pairwise_forced_choice",
    ]
    for entry in presets:
        assert entry.description
        assert "{{ output_instructions }}" in get_template(entry.preset)
    pair_template = get_template("pairwise_forced_choice")
    assert "{{ first_response }}" in pair_template
    assert "{{ second_response }}" in pair_template
    assert "{{ left_response }}" not in pair_template
    assert "{{ right_response }}" not in pair_template
    path = init_template("rubric_scores_only", tmp_path / "prompt.txt")
    assert path.read_text() == get_template("rubric_scores_only")
    with pytest.raises(FileExistsError):
        init_template("rubric_scores_only", path)
    with pytest.raises(PromptError, match="Unknown prompt preset"):
        get_template("unknown")


@pytest.mark.parametrize("count", [0, 1, 2, 200])
def test_builtin_complete_content_and_prefix(
    rubric: Rubric, response: ResponseItem, count: int
) -> None:
    exemplars = [
        Exemplar(exemplar_id=f"anchor_{index}", text=f"Complete anchor number {index}.")
        for index in range(count)
    ]
    rendered = render_prompt(rubric=rubric, response=response, exemplars=exemplars)
    assert rendered.exemplar_ids == [entry.exemplar_id for entry in exemplars]
    assert "JUDGMENTLLM_" not in rendered.text
    assert rubric.description in rendered.text
    assert response.text in rendered.text
    for trait in rubric.traits:
        assert trait.description in rendered.text
        for level in trait.levels:
            assert level.label in rendered.text
    cursor = 0
    for exemplar in exemplars:
        cursor = rendered.text.index(exemplar.text, cursor) + len(exemplar.text)
    assert cursor < rendered.text.index(response.text)
    assert rendered.output_schema["required"] == ["scores"]
    next_render = render_prompt(
        rubric=rubric,
        response=ResponseItem(item_id="essay_2", text="A second synthetic response."),
        exemplars=exemplars,
    )
    assert (
        rendered.text.split("## Student Essay")[0]
        == next_render.text.split("## Student Essay")[0]
    )


def test_exemplar_annotations_and_selected_group(
    rubric: Rubric, response: ResponseItem, exemplars: list[Exemplar]
) -> None:
    rendered = render_prompt(
        rubric=rubric,
        traits=[rubric.traits[0]],
        response=response,
        exemplars=exemplars,
    )
    assert "Judge relevant support." not in rendered.text
    assert '"evidence": "3"' in rendered.text
    assert "Connections are sparse." in rendered.text
    assert '"index": 2' in rendered.text
    assert "café" in rendered.text
    assert rendered.text.index("anchor_b") < rendered.text.index("anchor_a")


def test_explanations_change_output_contract(
    rubric: Rubric, response: ResponseItem
) -> None:
    with_explanation = render_prompt(
        rubric=rubric, response=response, explanations=True
    )
    assert '"explanation"' in with_explanation.text
    scores_only = render_prompt(rubric=rubric, response=response)
    assert '"explanation"' not in scores_only.text
    assert "**Category `3`**" in scores_only.text
    assert "20.0" not in scores_only.text
    assert rubric.traits[1].numeric_values == [0.0, 5.0, 20.0]


def test_simple_template_and_standalone_traits(
    rubric: Rubric, response: ResponseItem, exemplars: list[Exemplar]
) -> None:
    rendered = render_prompt(
        traits=[rubric.traits[0]],
        response=response,
        exemplars=[exemplars[0]],
        template="Custom assessor\n{{ trait }}\n{{ exemplars }}\n{{ response }}\n{{ output_instructions }}",
    )
    assert rendered.text.startswith("Custom assessor")
    assert "Judge coherence." in rendered.text


def test_advanced_loops_conditions_and_filters(
    rubric: Rubric, response: ResponseItem, exemplars: list[Exemplar]
) -> None:
    rendered = render_prompt(
        rubric=rubric,
        response=response,
        exemplars=exemplars,
        explanations=True,
        advanced=True,
        template=(
            "{% if explanations and not allow_ties %}Explain{% else %}Score{% endif %}\n"
            "{% for entry in traits %}{{ entry['trait_id']|upper }}\n{{ entry['block'] }}\n{% endfor %}"
            "{% for entry in exemplar_items %}{{ entry['block'] }}\n{% endfor %}"
            "{% if exemplar_items|length > 0 %}Anchors included{% endif %}\n"
            "{{ response }}\n{{ output_instructions }}"
        ),
    )
    assert rendered.text.startswith("Explain")
    assert "ORGANIZATION" in rendered.text
    assert "Anchors included" in rendered.text
    assert "Assess organization and evidence separately." in rendered.text
    assert rendered.exemplar_ids == ["anchor_b", "anchor_a"]
    conditional = render_prompt(
        rubric=rubric,
        response=response,
        advanced=True,
        template=(
            "{% if explanations or allow_ties %}Explain{% else %}Score{% endif %}"
            "{{ rubric }}{{ response }}{{ output_instructions }}"
        ),
    )
    assert conditional.text.startswith("Score")


@pytest.mark.parametrize(
    ("template", "match"),
    [
        ("{{ unknown }}", "Unknown prompt placeholder"),
        ("{{ response }}", "each required"),
        ("{{ rubric }}{{ output_instructions }}", "each required"),
        ("{{ response }}{{ output_instructions }}", "each required"),
        ("{{ rubric }}{{ response }}{{ output_instructions }}", "each required"),
        (
            "{{ rubric }}{{ exemplars }}{{ exemplars }}{{ response }}{{ output_instructions }}",
            "each required",
        ),
        ("{{ response|upper }}", "Simple templates"),
        ("{% if response %}yes{% endif %}", "Simple templates"),
        ("{{ response", "Cannot render"),
    ],
)
def test_invalid_simple_templates(
    rubric: Rubric,
    response: ResponseItem,
    exemplars: list[Exemplar],
    template: str,
    match: str,
) -> None:
    with pytest.raises(PromptError, match=match):
        render_prompt(
            rubric=rubric,
            response=response,
            exemplars=exemplars,
            template=template,
        )


@pytest.mark.parametrize(
    "attack",
    [
        "{{ response.__class__ }}",
        "{{ response.upper() }}",
        "{{ cycler.__init__.__globals__ }}",
        "{{ self }}",
        "{{ _private }}",
        "{{ response['__class__'] }}",
        "{% include '/etc/passwd' %}",
        "{% import '/etc/passwd' as leak %}",
        "{% extends '/etc/passwd' %}",
        "{% macro danger() %}x{% endmacro %}",
        "{% set leak = response %}",
        "{{ 10 ** 999999999 }}",
        "{{ 'x' * 999999999 }}",
        "{{ response|attr('__class__') }}",
        "{{ response|safe }}",
        "{{ response|tojson }}",
        "{% for entry in traits %}{{ loop['index'] }}{% endfor %}",
        "{% for entry in traits recursive %}{{ entry['block'] }}{% endfor %}",
        "{% for entry in traits %}{% for e in exemplar_items %}x{% endfor %}{% endfor %}",
        "{% for response in traits %}x{% endfor %}",
        "{% for entry in response %}x{% endfor %}",
        "{% for a, b in traits %}x{% endfor %}",
        "{{ traits[allow_ties] }}",
    ],
)
def test_advanced_sandbox_rejects_unsafe_operations(
    rubric: Rubric, response: ResponseItem, attack: str
) -> None:
    with pytest.raises(PromptError):
        render_prompt(rubric=rubric, response=response, template=attack, advanced=True)


@pytest.mark.parametrize(
    "template",
    [
        "{{ response|upper(1) }}",
        "{{ explanations|length }}",
        "{{ response < 1 }}",
    ],
)
def test_advanced_runtime_type_errors_are_actionable(
    rubric: Rubric, response: ResponseItem, template: str
) -> None:
    with pytest.raises(PromptError, match="Cannot render prompt template"):
        render_prompt(
            rubric=rubric, response=response, template=template, advanced=True
        )


def test_advanced_cannot_omit_or_reorder_exemplars(
    rubric: Rubric, response: ResponseItem, exemplars: list[Exemplar]
) -> None:
    base = "{{ rubric }}{{ response }}{{ output_instructions }}"
    with pytest.raises(PromptError, match="exemplar order"):
        render_prompt(
            rubric=rubric,
            response=response,
            exemplars=exemplars,
            advanced=True,
            template=base
            + "{{ exemplar_items[1]['block'] }}{{ exemplar_items[0]['block'] }}",
        )
    with pytest.raises(PromptError, match="each required"):
        render_prompt(
            rubric=rubric,
            response=response,
            exemplars=exemplars,
            advanced=True,
            template=base
            + "{% for entry in exemplar_items %}{% if entry['exemplar_id'] == 'anchor_b' %}"
            "{{ entry['block'] }}{% endif %}{% endfor %}",
        )


def test_identical_and_empty_content_still_requires_distinct_blocks(
    rubric: Rubric,
) -> None:
    response = ResponseItem(item_id="empty", text="")
    exemplars = [
        Exemplar(exemplar_id="first", text=""),
        Exemplar(exemplar_id="second", text=""),
    ]
    result = render_prompt(rubric=rubric, response=response, exemplars=exemplars)
    assert result.exemplar_ids == ["first", "second"]
    with pytest.raises(PromptError, match="each required"):
        render_prompt(
            rubric=rubric,
            response=response,
            exemplars=exemplars,
            advanced=True,
            template="{{ rubric }}{{ response }}{{ output_instructions }}{{ exemplar_items[0]['block'] }}",
        )


@pytest.mark.parametrize(
    ("limits", "match", "template"),
    [
        (RenderLimits(max_source_bytes=1), "source size", None),
        (RenderLimits(max_output_bytes=1), "output size", None),
        (RenderLimits(max_ast_nodes=1), "AST size/depth", None),
        (RenderLimits(max_ast_depth=1), "AST size/depth", None),
        (
            RenderLimits(max_loop_iterations=1),
            "loop iteration",
            "{% for entry in traits %}{% if false %}{{ entry['block'] }}{% endif %}{% endfor %}",
        ),
    ],
)
def test_rendering_safeguards_fail_without_truncation(
    rubric: Rubric,
    response: ResponseItem,
    limits: RenderLimits,
    match: str,
    template: str | None,
) -> None:
    with pytest.raises(PromptError, match=match):
        render_prompt(
            rubric=rubric,
            response=response,
            limits=limits,
            template=template,
            advanced=True,
        )


def test_render_limit_configuration_is_strict() -> None:
    with pytest.raises(ValidationError):
        RenderLimits(max_ast_nodes=0)
    with pytest.raises(ValidationError):
        RenderLimits.model_validate({"unknown": 1})
    with pytest.raises(ValidationError):
        RenderLimits.model_validate({"max_output_bytes": True})


def test_pairwise_presentation_and_explanations(
    pair: PairTask, criterion: PairwiseCriterion
) -> None:
    result = render_prompt(
        pair=pair,
        criterion=criterion,
        left=ResponseItem(item_id="left", text="Original left synthetic text."),
        right=ResponseItem(item_id="right", text="Original right synthetic text."),
        explanations=True,
        exemplars=[
            Exemplar(
                exemplar_id="pair_anchor",
                text="Synthetic comparison anchor.",
                rationales={"quality": "Prefer clarity."},
            )
        ],
    )
    assert result.text.index("Original right") < result.text.index("Original left")
    assert "essay_01:\nOriginal right" in result.text
    assert '"explanation"' in result.text
    assert result.output_schema["required"] == ["winner_id", "explanation"]


def test_pairwise_position_names_preserve_legacy_alias_behavior(
    pair: PairTask, criterion: PairwiseCriterion
) -> None:
    template = (
        "{{ criterion }}\n{{ first_response }}\n{{ second_response }}\n"
        "{{ output_instructions }}"
    )
    left = ResponseItem(item_id="left", text="Original left synthetic text.")
    right = ResponseItem(item_id="right", text="Original right synthetic text.")
    explicit = render_prompt(
        pair=pair, criterion=criterion, left=left, right=right, template=template
    )
    legacy = render_prompt(
        pair=pair,
        criterion=criterion,
        left=left,
        right=right,
        template=template.replace("first_response", "left_response").replace(
            "second_response", "right_response"
        ),
    )
    assert explicit.text == legacy.text
    assert pair.original_order == ["left", "right"]
    assert pair.presented_order == ["right", "left"]
    assert explicit.text.index("essay_01:\nOriginal right") < explicit.text.index(
        "essay_02:\nOriginal left"
    )


def test_pairwise_custom_tie_contract(
    pair: PairTask, criterion: PairwiseCriterion
) -> None:
    with pytest.raises(PromptError, match="does not allow ties"):
        render_prompt(
            pair=pair,
            criterion=criterion,
            left=ResponseItem(item_id="left", text="Left synthetic text."),
            right=ResponseItem(item_id="right", text="Right synthetic text."),
            allow_ties=True,
        )
    result = render_prompt(
        pair=pair,
        criterion=criterion,
        left=ResponseItem(item_id="left", text="Left synthetic text."),
        right=ResponseItem(item_id="right", text="Right synthetic text."),
        allow_ties=True,
        template="{{ criterion }}{{ first_response }}{{ second_response }}{{ output_instructions }}",
    )
    assert "oneOf" in result.output_schema
    assert '"winner_id":{"const":null}' in result.text


def test_incompatible_arguments_and_presets(
    rubric: Rubric,
    response: ResponseItem,
    pair: PairTask,
    criterion: PairwiseCriterion,
) -> None:
    with pytest.raises(PromptError, match="preset or custom"):
        render_prompt(preset="rubric_scores_only", template="custom")
    with pytest.raises(PromptError, match="require a response"):
        render_prompt()
    with pytest.raises(PromptError, match="require a response"):
        render_prompt(response=response)
    with pytest.raises(PromptError, match="at least one trait"):
        render_prompt(response=response, traits=[])
    with pytest.raises(PromptError, match="distinct trait"):
        render_prompt(response=response, traits=[rubric.traits[0], rubric.traits[0]])
    different = rubric.traits[0].model_copy(update={"description": "Changed criteria."})
    with pytest.raises(PromptError, match="match traits stored"):
        render_prompt(rubric=rubric, traits=[different], response=response)
    with pytest.raises(PromptError, match="pairwise arguments"):
        render_prompt(rubric=rubric, response=response, allow_ties=True)
    with pytest.raises(PromptError, match="preset does not match"):
        render_prompt(rubric=rubric, response=response, preset="pairwise_forced_choice")
    with pytest.raises(PromptError, match="explanations setting"):
        render_prompt(
            rubric=rubric,
            response=response,
            preset="rubric_scores_with_explanations",
        )
    with pytest.raises(PromptError, match="cannot include rubric"):
        render_prompt(pair=pair, response=response)
    with pytest.raises(PromptError, match="require left, right"):
        render_prompt(pair=pair)
    with pytest.raises(PromptError, match="must match the pair IDs"):
        render_prompt(pair=pair, criterion=criterion, left=response, right=response)


def test_bad_exemplar_assignments_are_rejected(
    rubric: Rubric, response: ResponseItem, exemplars: list[Exemplar]
) -> None:
    for selected in (
        [exemplars[0], exemplars[0]],
        [Exemplar(exemplar_id="unknown_trait", text="Synthetic", scores={"bad": "1"})],
        [
            Exemplar(
                exemplar_id="unknown_category",
                text="Synthetic",
                scores={"organization": "bad"},
            )
        ],
    ):
        with pytest.raises(PromptError):
            render_prompt(rubric=rubric, response=response, exemplars=selected)


def test_student_text_is_never_reinterpreted_as_template(rubric: Rubric) -> None:
    text = "Synthetic {{ unknown }} {% include '/etc/passwd' %} __class__ text."
    rendered = render_prompt(
        rubric=rubric, response=ResponseItem(item_id="literal", text=text)
    )
    assert text in rendered.text


def test_block_integrity_rejects_marker_tampering_and_changed_content() -> None:
    blocks = _protect([("exemplar", "synthetic", "Original synthetic text.")], "audit")
    wrapped = blocks[0].wrapped
    assert (
        _verify_and_unwrap(wrapped, blocks, "audit") == "\nOriginal synthetic text.\n"
    )
    for malformed in (
        wrapped.replace("BEGIN_0", "BEGIN_1"),
        wrapped.replace("END_0", "END_1"),
        wrapped.replace("BEGIN_0", "END_0"),
    ):
        with pytest.raises(PromptError, match="malformed assessment blocks"):
            _verify_and_unwrap(malformed, blocks, "audit")
    with pytest.raises(PromptError, match="changed protected exemplar"):
        _verify_and_unwrap(wrapped.replace("Original", "Altered"), blocks, "audit")
    duplicated = _protect(
        [("exemplar", "first", "First."), ("exemplar", "second", "Second.")],
        "audit",
    )
    with pytest.raises(PromptError, match="duplicated"):
        _verify_and_unwrap(duplicated[0].wrapped * 2, duplicated, "audit")


def test_marker_namespace_collision_and_sandbox_runtime_defenses() -> None:
    digest = hashlib.sha256(b"synthetic").hexdigest()[:16]
    assert _namespace(["synthetic"], f"<<JUDGMENTLLM_{digest}_0_BEGIN_0>>") == (
        digest + "_1"
    )
    environment = _environment()
    assert not environment.is_safe_callable(str)
    assert not environment.is_safe_attribute("text", "upper", str.upper)
