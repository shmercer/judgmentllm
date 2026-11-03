"""Preparation checks static render contracts without an exhaustive task product."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Literal

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
from judgmentllm.prompts import RenderedPrompt, get_template, render_prompt


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
    ) -> RenderedPrompt:
        requests.append(request)
        return original(spec, template, request, lookup)

    monkeypatch.setattr(capture, "_render_request", render)
    return requests


@pytest.mark.parametrize("pairwise", [False, True])
def test_normal_preflight_render_count_is_independent_of_input_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pairwise: bool
) -> None:
    rendered = count_renders(monkeypatch)
    for count in (3, 30):
        rendered.clear()
        spec = specification(
            tmp_path / f"inputs-{count}", responses=count, traits=4, pairwise=pairwise
        )
        with prepare_run(
            spec, store=tmp_path / f"runtime-{count}", run_id="001"
        ) as prepared:
            assert len(rendered) == 4
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
    tmp_path: Path, pairwise: bool, exhaustive: bool
) -> None:
    spec = specification(tmp_path / "inputs", responses=0, pairwise=pairwise)
    assert validate_inputs(spec, exhaustive_rendering=exhaustive).valid
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
