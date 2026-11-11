"""Preparation checks static render contracts without an exhaustive task product."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Literal

import pytest

from judgmentllm.config import RunSpec
from judgmentllm.models import Exemplar, PairTask, ResponseItem
from judgmentllm.persistence.repository import Repository
from judgmentllm.planning import PlannedRequest, iter_requests
from judgmentllm.preparation import (
    capture,
    open_prepared_run,
    prepare_run,
    validate_inputs,
)
from judgmentllm.preparation.freezing import PreparationError
from judgmentllm.prompts import RenderedAssessmentPrompt, get_template, render_prompt


def specification(
    directory: Path, *, responses: int = 3, traits: int = 2, pairwise: bool = False
) -> RunSpec:
    directory.mkdir(parents=True)
    source = directory / "responses.jsonl"
    source.write_text(
        "".join(
            json.dumps({"item_id": f"{index:03d}", "text": "Synthetic response."})
            + "\n"
            for index in range(responses)
        ),
        encoding="utf-8",
    )
    data: dict[str, object] = {
        "task_kind": "pairwise" if pairwise else "rubric",
        "input": source,
        "capture": "minimal",
        "provider": "synthetic",
        "endpoint": "offline",
        "model": "fixture",
    }
    if pairwise:
        data["criteria"] = [
            {"trait_id": f"{index:03d}", "name": "Quality", "description": "Compare."}
            for index in range(traits)
        ]
        pairs = directory / "pairs.jsonl"
        pairs.write_text(
            "".join(
                json.dumps(
                    {
                        "pair_id": f"{index:03d}",
                        "pair_uid": f"{index:03d}",
                        "left_item_id": "000",
                        "right_item_id": "001",
                        "trait_id": f"{index % traits:03d}",
                    }
                )
                + "\n"
                for index in range(responses)
            ),
            encoding="utf-8",
        )
        data["pairs"] = pairs
    else:
        data["rubric"] = {
            "rubric_id": "001",
            "name": "Synthetic rubric",
            "traits": [
                {
                    "trait_id": f"{index:03d}",
                    "name": "Quality",
                    "description": "Assess.",
                    "levels": [{"label": "low"}, {"label": "high"}],
                }
                for index in range(traits)
            ],
        }
    return RunSpec.model_validate(data)


def count_renders(monkeypatch: pytest.MonkeyPatch) -> list[PlannedRequest]:
    original = capture._render_request  # pyright: ignore[reportPrivateUsage]
    requests: list[PlannedRequest] = []

    def render(
        spec: RunSpec,
        template: str | None,
        request: PlannedRequest,
        lookup: Callable[[Literal["response", "pair"], str], ResponseItem | PairTask],
    ) -> RenderedAssessmentPrompt:
        requests.append(request)
        return original(spec, template, request, lookup)

    monkeypatch.setattr(capture, "_render_request", render)
    return requests


def count_static_checks(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    original = capture.validate_prompt_template
    groups: list[tuple[str, ...]] = []

    def check(*, trait_ids: Sequence[str], **options: Any) -> None:
        groups.append(tuple(trait_ids))
        original(trait_ids=trait_ids, **options)

    monkeypatch.setattr(capture, "validate_prompt_template", check)
    return groups


@pytest.mark.parametrize("pairwise", [False, True])
def test_normal_preflight_render_count_is_independent_of_input_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pairwise: bool
) -> None:
    rendered = count_renders(monkeypatch)
    compiled = count_static_checks(monkeypatch)
    for count in (3, 30):
        rendered.clear()
        compiled.clear()
        spec = specification(
            tmp_path / f"inputs-{count}", responses=count, traits=4, pairwise=pairwise
        )
        with prepare_run(
            spec, store=tmp_path / f"runtime-{count}", run_id="001"
        ) as prepared:
            assert len(compiled) == 4
            assert len(rendered) == (min(count, 4) if pairwise else 4)
            assert all(
                request.response is None or request.response.item_id == "000"
                for request in rendered
            )
            assert (
                sum(
                    source.record_count
                    for source in prepared.manifest.sources
                    if source.role == "response"
                )
                == count
            )
            assert (
                prepared.get_response(f"{count - 1:03d}").item_id == f"{count - 1:03d}"
            )
            assert (
                prepared.repository.connection.execute(
                    "SELECT COUNT(*) FROM requests"
                ).fetchone()[0]
                == 0
            )


def test_fixed_groups_each_receive_one_upfront_representative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = specification(tmp_path / "inputs", responses=12, traits=4)
    spec.execution.grouping = "grouped"
    spec.execution.groups = [["002", "000"], ["003", "001"]]
    rendered = count_renders(monkeypatch)
    with prepare_run(spec, store=tmp_path / "runtime", run_id="001"):
        assert [request.trait_ids for request in rendered] == [
            ("002", "000"),
            ("003", "001"),
        ]


@pytest.mark.parametrize("pairwise", [False, True])
@pytest.mark.parametrize("exhaustive", [False, True])
def test_empty_sources_still_validate_template_contracts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pairwise: bool, exhaustive: bool
) -> None:
    spec = specification(tmp_path / "inputs", responses=0, pairwise=pairwise)
    rendered = count_renders(monkeypatch)
    compiled = count_static_checks(monkeypatch)
    assert validate_inputs(spec, exhaustive_rendering=exhaustive).valid
    assert len(compiled) == 2 and not rendered
    template = tmp_path / "invalid.txt"
    template.write_text("{{ unknown_source_variable }}", encoding="utf-8")
    spec.prompt.preset = None
    spec.prompt.file = template
    assert not validate_inputs(spec, exhaustive_rendering=exhaustive).valid
    with pytest.raises(PreparationError):
        prepare_run(
            spec,
            store=tmp_path / "runtime",
            run_id="001",
            exhaustive_rendering=exhaustive,
        )


def test_real_request_render_limits_are_checked_when_selected_and_on_exhaustive_validation(
    tmp_path: Path,
) -> None:
    spec = specification(tmp_path / "inputs", responses=2, traits=1)
    spec.input.path.write_text(
        '{"item_id":"000","text":"ok"}\n'
        + json.dumps({"item_id": "001", "text": "x" * 8000})
        + "\n",
        encoding="utf-8",
    )
    assert spec.rubric is not None
    representative = render_prompt(
        rubric=spec.rubric, response=ResponseItem(item_id="0", text="")
    )
    spec.prompt.limits.max_output_bytes = (
        len(representative.text.encode("utf-8")) + 1024
    )
    store = tmp_path / "runtime"
    with prepare_run(spec, store=store, run_id="001") as prepared:
        dependencies = prepared.iter_request_dependencies()
        assert "ok" in prepared.render(next(dependencies)).text
        with pytest.raises(PreparationError, match="could not be rendered"):
            prepared.render(next(dependencies))
    assert validate_inputs(spec).valid
    assert not validate_inputs(spec, exhaustive_rendering=True).valid
    with pytest.raises(PreparationError):
        prepare_run(
            spec, store=tmp_path / "exhaustive", run_id="001", exhaustive_rendering=True
        )
    with Repository(tmp_path / "exhaustive") as repository:
        assert repository.get_run("001").manifest_sha256 is None
    # The diagnostic option does not change compatibility or frozen identities.
    with open_prepared_run("001", store=store, spec=spec) as reopened:
        assert reopened.get_response("001").text == "x" * 8000


@pytest.mark.parametrize("pairwise", [False, True])
def test_exhaustive_preflight_is_explicit_and_preserves_request_identities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pairwise: bool
) -> None:
    spec = specification(tmp_path / "inputs", responses=3, traits=2, pairwise=pairwise)
    rendered = count_renders(monkeypatch)
    with prepare_run(spec, store=tmp_path / "normal", run_id="001") as normal:
        assert len(rendered) == 2
        normal_requests = [
            dependency.request for dependency in normal.iter_request_dependencies()
        ]
        normal_configuration = normal.manifest.configuration_sha256
        normal_manifest = normal.manifest_sha256
    rendered.clear()
    with prepare_run(
        spec, store=tmp_path / "exhaustive", run_id="001", exhaustive_rendering=True
    ) as exhaustive:
        assert len(rendered) == 2 + (3 if pairwise else 3 * 2)
        assert exhaustive.manifest.configuration_sha256 == normal_configuration
        assert exhaustive.manifest_sha256 == normal_manifest
        assert [
            dependency.request for dependency in exhaustive.iter_request_dependencies()
        ] == normal_requests
    assert normal_requests == list(iter_requests(spec))


@pytest.mark.parametrize("exhaustive", [False, True])
def test_representative_and_selected_prompts_are_not_retained_under_minimal_capture(
    tmp_path: Path, exhaustive: bool
) -> None:
    spec = specification(tmp_path / "inputs", responses=1, traits=1)
    student, exemplar, template_marker = (
        "STUDENT_RENDER_PRIVATE_71",
        "EXEMPLAR_RENDER_PRIVATE_82",
        "TEMPLATE_RENDER_PRIVATE_93",
    )
    spec.input.path.write_text(
        json.dumps({"item_id": "000", "text": student}) + "\n", encoding="utf-8"
    )
    spec.exemplars = [Exemplar(exemplar_id="001", text=exemplar)]
    template = tmp_path / "template.txt"
    template.write_text(
        template_marker + "\n" + get_template("rubric_scores_only"), encoding="utf-8"
    )
    spec.prompt.preset = None
    spec.prompt.file = template
    store = tmp_path / "runtime"
    with prepare_run(
        spec, store=store, run_id="001", exhaustive_rendering=exhaustive
    ) as prepared:
        prompt = prepared.render(next(prepared.iter_request_dependencies()))
        assert all(
            sentinel in prompt.text for sentinel in (student, exemplar, template_marker)
        )
        for path in store.rglob("*"):
            if path.is_file():
                assert all(
                    sentinel.encode() not in path.read_bytes()
                    for sentinel in (student, exemplar, template_marker)
                )


def test_real_response_condition_does_not_evaluate_invented_empty_text(
    tmp_path: Path,
) -> None:
    spec = specification(tmp_path / "inputs", responses=1, traits=2)
    spec.input.path.write_text(
        json.dumps({"item_id": "000", "text": "x" * 500}) + "\n", encoding="utf-8"
    )
    template = tmp_path / "conditional.txt"
    template.write_text(
        "{{ rubric }}\n{{ exemplars }}\n"
        "{% if response|length > 300 %}{{ response }}{% endif %}\n"
        "{{ output_instructions }}\n",
        encoding="utf-8",
    )
    spec.prompt.preset = None
    spec.prompt.file = template
    spec.prompt.advanced = True
    with prepare_run(spec, store=tmp_path / "runtime", run_id="001") as prepared:
        for dependency in prepared.iter_request_dependencies():
            assert "x" * 500 in prepared.render(dependency).text


def test_pair_representative_preserves_actual_ids_and_presented_orientation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = specification(tmp_path / "inputs", responses=2, traits=2, pairwise=True)
    assert spec.pairs is not None
    rows = [
        json.loads(line)
        for line in spec.pairs.path.read_text(encoding="utf-8").splitlines()
    ]
    for row in rows:
        row["presented_order"] = ["001", "000"]
    spec.pairs.path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )
    template = tmp_path / "pair-conditional.txt"
    template.write_text(
        "{{ criterion }}\n{{ exemplars }}\n"
        "{% if 'essay_01:' in first_response "
        "and 'essay_02:' in second_response %}"
        "{{ first_response }}\n{{ second_response }}{% endif %}\n"
        "{{ output_instructions }}\n",
        encoding="utf-8",
    )
    spec.prompt.preset = None
    spec.prompt.file = template
    spec.prompt.advanced = True
    rendered = count_renders(monkeypatch)
    with prepare_run(spec, store=tmp_path / "runtime", run_id="001") as prepared:
        assert len(rendered) == 2
        for request in rendered:
            assert request.pair is not None
            assert request.pair.original_order == ["000", "001"]
            assert request.pair.presented_order == ["001", "000"]
        for dependency in prepared.iter_request_dependencies():
            prompt = prepared.render(dependency).text
            assert prompt.index("essay_01:") < prompt.index("essay_02:")


@pytest.mark.parametrize("pairwise", [False, True])
def test_empty_sources_compile_conditional_context_without_invented_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pairwise: bool
) -> None:
    spec = specification(tmp_path / "inputs", responses=0, pairwise=pairwise)
    response = "first_response" if pairwise else "response"
    content = (
        "{{ first_response }}{{ second_response }}" if pairwise else "{{ response }}"
    )
    template = tmp_path / "conditional-empty.txt"
    template.write_text(
        "{{ rubric }}{{ exemplars }}"
        + "{% if "
        + response
        + "|length > 300 %}"
        + content
        + "{% endif %}"
        + "{{ output_instructions }}",
        encoding="utf-8",
    )
    spec.prompt.preset = None
    spec.prompt.file = template
    spec.prompt.advanced = True
    rendered = count_renders(monkeypatch)
    compiled = count_static_checks(monkeypatch)
    assert validate_inputs(spec).valid
    assert len(compiled) == 2 and not rendered


@pytest.mark.parametrize(
    "template_text",
    ["{{ response.__class__ }}", "{{ response | unknown_filter }}", "{% invalid %}"],
)
def test_empty_source_static_validation_reuses_sandbox_and_compiler_errors(
    tmp_path: Path, template_text: str
) -> None:
    spec = specification(tmp_path / "inputs", responses=0)
    template = tmp_path / "invalid-empty.txt"
    template.write_text(template_text, encoding="utf-8")
    spec.prompt.preset = None
    spec.prompt.file = template
    spec.prompt.advanced = True
    assert not validate_inputs(spec).valid
