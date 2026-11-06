"""Whole physical membership reuses strict rendering without discarded work."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Literal

import pytest
from test_preparation_rendering import specification

from judgmentllm.config import RunSpec
from judgmentllm.execution.retry import subset_dependency
from judgmentllm.models import PairTask, ResponseItem
from judgmentllm.persistence.models import RequestRecord, TaskRecord
from judgmentllm.planning import PlannedRequest
from judgmentllm.preparation import PreparedRun, RequestDependency, prepare_run, session
from judgmentllm.preparation.freezing import PreparationError
from judgmentllm.prompts import RenderedAssessmentPrompt


def seal_request(prepared: PreparedRun, dependency: RequestDependency) -> None:
    """Freeze the same durable original membership used by the executor."""
    request = dependency.request
    with prepared.repository.transaction():
        for ordinal, task in enumerate(request.tasks):
            prepared.repository.create_task(
                TaskRecord(
                    run_id=prepared.manifest.run_id,
                    scheduling_ordinal=ordinal,
                    **task.model_dump(exclude={"schema_version"}),
                )
            )
        prepared.repository.create_request(
            RequestRecord(
                run_id=prepared.manifest.run_id,
                request_uid=request.request_uid,
                scheduling_ordinal=0,
                dependency_sha256=dependency.dependency_sha256,
            ),
            tuple(task.task_id for task in request.tasks),
        )


def counted_renders(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    original = session._render_request  # pyright: ignore[reportPrivateUsage]
    rendered: list[tuple[str, ...]] = []

    def render(
        spec: RunSpec,
        template: str | None,
        request: PlannedRequest,
        lookup: Callable[[Literal["response", "pair"], str], ResponseItem | PairTask],
    ) -> RenderedAssessmentPrompt:
        rendered.append(tuple(task.task_id for task in request.tasks))
        return original(spec, template, request, lookup)

    monkeypatch.setattr(session, "_render_request", render)
    return rendered


@pytest.mark.parametrize("kind", ["independent", "grouped", "pairwise"])
def test_complete_attempt_matches_strict_render_and_renders_only_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    spec = specification(tmp_path / "inputs", traits=3, pairwise=kind == "pairwise")
    if kind == "grouped":
        spec.execution.grouping = "grouped"
        spec.execution.groups = [["002", "000", "001"]]
    with prepare_run(spec, store=tmp_path / "store", run_id="001") as prepared:
        dependency = next(prepared.iter_request_dependencies())
        seal_request(prepared, dependency)
        expected = prepared.render(dependency)
        rendered = counted_renders(monkeypatch)

        def unnecessary_parent_lookup(_: str) -> RequestDependency:
            raise AssertionError("whole membership must use the strict original path")

        monkeypatch.setattr(
            prepared, "dependency_for_request", unnecessary_parent_lookup
        )
        actual = prepared.render_attempt(dependency)
        assert actual == expected
        assert rendered == [tuple(task.task_id for task in dependency.request.tasks)]


@pytest.mark.parametrize(
    "change",
    [
        "source",
        "configuration",
        "category",
        "dependency",
        "trait_order",
        "task_trait",
        "response",
    ],
)
def test_complete_attempt_fast_path_preserves_strict_fail_closed_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    spec = specification(tmp_path / "inputs", responses=1, traits=3)
    spec.execution.grouping = "grouped"
    with prepare_run(spec, store=tmp_path / "store", run_id="001") as prepared:
        dependency = next(prepared.iter_request_dependencies())
        seal_request(prepared, dependency)
        original_ids = tuple(task.task_id for task in dependency.request.tasks)
        if change == "source":
            spec.input.path.write_text(
                '{"item_id":"000","text":"changed"}\n', encoding="utf-8"
            )
        elif change == "configuration":
            assert prepared.spec.rubric is not None
            prepared.spec.rubric.traits[0].description = "changed"
        elif change == "category":
            assert prepared.spec.rubric is not None
            prepared.spec.rubric.traits[0].levels[0].label = "changed"
        elif change == "dependency":
            dependency = dependency.model_copy(update={"dependency_sha256": "0" * 64})
        elif change == "trait_order":
            dependency = dependency.model_copy(
                update={
                    "request": dependency.request.model_copy(
                        update={
                            "trait_ids": tuple(reversed(dependency.request.trait_ids))
                        }
                    )
                }
            )
        elif change == "task_trait":
            tasks = dependency.request.tasks
            dependency = dependency.model_copy(
                update={
                    "request": dependency.request.model_copy(
                        update={
                            "tasks": (
                                tasks[0].model_copy(update={"trait_id": "other"}),
                                *tasks[1:],
                            )
                        }
                    )
                }
            )
        else:
            response = dependency.request.response
            assert response is not None
            dependency = dependency.model_copy(
                update={
                    "request": dependency.request.model_copy(
                        update={
                            "response": response.model_copy(update={"text": "changed"})
                        }
                    )
                }
            )
        assert tuple(task.task_id for task in dependency.request.tasks) == original_ids
        rendered = counted_renders(monkeypatch)
        with pytest.raises(PreparationError):
            prepared.render_attempt(dependency)
        assert not rendered


@pytest.mark.parametrize("change", ["reordered", "duplicate", "foreign"])
def test_changed_membership_cannot_enter_complete_attempt_fast_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    spec = specification(tmp_path / "inputs", responses=1, traits=3)
    spec.execution.grouping = "grouped"
    with prepare_run(spec, store=tmp_path / "store", run_id="001") as prepared:
        parent = next(prepared.iter_request_dependencies())
        seal_request(prepared, parent)
        tasks = parent.request.tasks
        changed = (
            tuple(reversed(tasks))
            if change == "reordered"
            else (tasks[0], tasks[0], *tasks[2:])
            if change == "duplicate"
            else (tasks[0].model_copy(update={"task_id": "foreign"}), *tasks[1:])
        )
        supplied = parent.model_copy(
            update={
                "request": parent.request.model_copy(
                    update={
                        "tasks": changed,
                        "trait_ids": tuple(task.trait_id for task in changed),
                    }
                )
            }
        )
        rendered = counted_renders(monkeypatch)
        with pytest.raises(PreparationError):
            prepared.render_attempt(supplied)
        original_members = tuple(task.task_id for task in tasks)
        assert rendered == [original_members]
        assert (
            prepared.repository.request_task_ids("001", parent.request.request_uid)
            == original_members
        )


def test_proper_subset_keeps_original_parent_validation_and_selected_rendering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = specification(tmp_path / "inputs", responses=1, traits=3)
    spec.execution.grouping = "grouped"
    spec.execution.groups = [["002", "000", "001"]]
    with prepare_run(spec, store=tmp_path / "store", run_id="001") as prepared:
        parent = next(prepared.iter_request_dependencies())
        seal_request(prepared, parent)
        selected = (parent.request.tasks[1].task_id,)
        subset = subset_dependency(parent, selected)
        rendered = counted_renders(monkeypatch)
        prompt = prepared.render_attempt(subset)
        assert prompt.text and subset.request.trait_ids == ("000",)
        assert rendered == [
            tuple(task.task_id for task in parent.request.tasks),
            selected,
        ]
        assert subset.request.request_uid == parent.request.request_uid


def test_complete_attempt_checks_durable_parent_commitment_before_rendering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = specification(tmp_path / "inputs", responses=1, traits=1)
    with prepare_run(spec, store=tmp_path / "store", run_id="001") as prepared:
        dependency = next(prepared.iter_request_dependencies())
        seal_request(prepared, dependency)
        original = prepared.repository.get_request(
            "001", dependency.request.request_uid
        )

        def conflicting_parent(_: str, __: str) -> RequestRecord:
            return original.model_copy(update={"dependency_sha256": "0" * 64})

        monkeypatch.setattr(prepared.repository, "get_request", conflicting_parent)
        rendered = counted_renders(monkeypatch)
        with pytest.raises(PreparationError, match="parent request dependency changed"):
            prepared.render_attempt(dependency)
        assert not rendered
