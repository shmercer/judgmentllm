"""Structural planning and portable pair scheduling with synthetic inputs."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import ValidationError

from judgmentllm.config import ExecutionSpec, InputSpec, RunSpec
from judgmentllm.models import PairTask, PairwiseCriterion, ResponseItem, Rubric, Trait
from judgmentllm.planning import (
    CapabilityProfile,
    PreflightError,
    iter_requests,
    iter_tasks,
    plan_run,
    prepare_pairs,
    stable_id,
    validate_run,
)


def rubric_spec(execution: ExecutionSpec | None = None) -> RunSpec:
    rubric = Rubric.model_validate(
        {
            "rubric_id": "r",
            "name": "Synthetic",
            "traits": [
                {
                    "trait_id": identifier,
                    "name": identifier,
                    "description": "Judge synthetic structure.",
                    "levels": [{"label": 1}, {"label": 2}, {"label": 3}],
                }
                for identifier in ("a", "b", "c")
            ],
        }
    )
    return RunSpec(
        task_kind="rubric",
        input=InputSpec(path=Path("responses.csv")),
        rubric=rubric,
        provider="synthetic",
        endpoint="categorical",
        model="unknown-model",
        execution=execution or ExecutionSpec(),
    )


def pair(uid: str = "occurrence") -> PairTask:
    return PairTask(
        pair_id="comparison",
        pair_uid=uid,
        left_item_id="left",
        right_item_id="right",
        trait_id="a",
    )


def pair_spec() -> RunSpec:
    return RunSpec(
        task_kind="pairwise",
        input=InputSpec(path=Path("responses.csv")),
        pairs=InputSpec(path=Path("pairs.jsonl")),
        criteria=[
            PairwiseCriterion(
                trait_id="a", name="A", description="Compare synthetic texts."
            )
        ],
        provider="synthetic",
        endpoint="choice",
        model="unknown-model",
    )


def test_independent_grouped_and_auto_preserve_tasks() -> None:
    response = ResponseItem(item_id="item", text="Synthetic response")
    independent = rubric_spec()
    grouped = rubric_spec(ExecutionSpec(grouping="grouped", groups=[["b", "a"], ["c"]]))
    automatic = rubric_spec(ExecutionSpec(grouping="auto", max_traits_per_request=2))
    assert plan_run(independent).trait_groups == (("a",), ("b",), ("c",))
    assert plan_run(grouped).trait_groups == (("b", "a"), ("c",))
    assert plan_run(automatic).trait_groups == (("a", "b"), ("c",))
    plans = [
        list(iter_requests(spec, [response]))
        for spec in (independent, grouped, automatic)
    ]
    assert [len(requests) for requests in plans] == [3, 2, 2]
    assert (
        len(
            {
                task.task_id
                for requests in plans
                for request in requests
                for task in request.tasks
            }
        )
        == 3
    )
    assert list(iter_requests(grouped, [response])) == plans[1]
    assert plans[0][0].request_uid != plans[1][0].request_uid
    assert plan_run(rubric_spec(ExecutionSpec(grouping="grouped"))).trait_groups == (
        ("a", "b", "c"),
    )


@pytest.mark.parametrize(
    "groups", [[["a", "a"], ["b", "c"]], [["a"]], [["a", "b", "unknown"]], [[]], []]
)
def test_invalid_fixed_groups(groups: list[list[str]]) -> None:
    with pytest.raises(PreflightError, match="partition"):
        plan_run(rubric_spec(ExecutionSpec(grouping="grouped", groups=groups)))


def test_auto_requires_known_limit_fixed_groups_never_split() -> None:
    spec = rubric_spec(ExecutionSpec(grouping="auto"))
    with pytest.raises(PreflightError, match="known"):
        plan_run(spec)
    assert plan_run(
        spec, capabilities=CapabilityProfile(max_traits_per_request=2)
    ).trait_groups == (("a", "b"), ("c",))
    with pytest.raises(PreflightError, match="cannot be split"):
        plan_run(
            rubric_spec(ExecutionSpec(grouping="grouped", max_traits_per_request=2))
        )
    with pytest.raises(ValidationError):
        CapabilityProfile(max_traits_per_request=0)


def test_known_and_unknown_capabilities() -> None:
    spec = rubric_spec(ExecutionSpec(explanations=True))
    assert validate_run(spec).valid
    assert any(
        issue.code == "unknown_capability" for issue in validate_run(spec).issues
    )
    spec.strict_capabilities = True
    assert not validate_run(spec).valid
    supported = CapabilityProfile(explanations=True, modes=("live",))
    assert validate_run(spec, capabilities=supported).valid
    assert not validate_run(
        spec,
        capabilities=CapabilityProfile(
            explanations=False, modes=("batch_native",), max_levels=2
        ),
    ).valid
    grouped = rubric_spec(ExecutionSpec(grouping="grouped"))
    assert not validate_run(
        grouped, capabilities=CapabilityProfile(grouped=False)
    ).valid
    ties = pair_spec()
    ties.allow_ties = True
    assert not validate_run(ties, capabilities=CapabilityProfile(ties=False)).valid
    assert validate_run(ties, capabilities=CapabilityProfile(ties=True)).valid


@pytest.mark.parametrize(
    ("provider", "endpoint"),
    [("openai", "decisions"), ("typesafe", "jev"), ("typesafe", "systemone")],
)
def test_score_only_explanations_fail(provider: str, endpoint: str) -> None:
    spec = rubric_spec(ExecutionSpec(explanations=True))
    spec.provider, spec.endpoint = provider, endpoint
    with pytest.raises(PreflightError, match="score-only"):
        plan_run(spec, capabilities=CapabilityProfile(explanations=True))


def test_structured_mixed_label_warning() -> None:
    spec = rubric_spec()
    assert spec.rubric is not None
    with pytest.warns(UserWarning):
        spec.rubric.traits[0] = Trait.model_validate(
            {
                "trait_id": "a",
                "name": "A",
                "description": "Synthetic",
                "levels": [{"label": "5"}, {"label": "Developing"}],
            }
        )
    assert any(issue.code == "mixed_label_code" for issue in validate_run(spec).issues)


def test_tasks_are_lazy_and_ids_are_canonical() -> None:
    consumed: list[int] = []

    def records() -> Iterator[ResponseItem]:
        for index in range(100_000):
            consumed.append(index)
            yield ResponseItem(item_id=f"item{index}", text="Synthetic")

    requests = iter_requests(rubric_spec(), records())
    first = next(requests)
    assert consumed == [0]
    assert first.response is not None
    assert first.response.item_id == "item0"
    assert len(list(iter_tasks(rubric_spec(), [first.response]))) == 3
    assert stable_id("test", {"b": 2, "a": 1}) == stable_id("test", {"a": 1, "b": 2})
    assert stable_id("test", "a:b", "c") != stable_id("test", "a", "b:c")


def test_duplicate_wrong_and_unknown_records_fail() -> None:
    item = ResponseItem(item_id="id", text="Synthetic")
    for function in (iter_tasks, iter_requests):
        with pytest.raises(ValueError, match="Duplicate"):
            list(function(rubric_spec(), [item, item]))
        with pytest.raises(ValueError, match="Duplicate"):
            list(function(pair_spec(), [pair(), pair()]))
        with pytest.raises(ValueError, match="Expected"):
            list(function(rubric_spec(), [pair()]))
        with pytest.raises(ValueError, match="Unknown pair criterion"):
            list(
                function(
                    pair_spec(), [pair().model_copy(update={"trait_id": "unknown"})]
                )
            )
    assert plan_run(pair_spec()).trait_groups == (("a",),)


def test_pair_repeats_reversals_and_portable_seed() -> None:
    source = [pair(), pair("second")]
    result = list(prepare_pairs(source, repetitions=3, include_reversals=True, seed=41))
    assert result == list(
        prepare_pairs(source, repetitions=3, include_reversals=True, seed=41)
    )
    assert len(result) == len({record.pair_uid for record in result}) == 12
    assert [record.source_pair_uid for record in result] == ["occurrence"] * 6 + [
        "second"
    ] * 6
    for index in range(0, len(result), 2):
        first, second = result[index : index + 2]
        assert first.repetition_index == second.repetition_index == (index % 6) // 2
        assert first.pair_id == second.pair_id == "comparison"
        assert not first.is_reversal and second.is_reversal
        assert first.reversal_group == second.reversal_group
        assert first.presented_order == second.presented_order[::-1]
    assert len({task.task_id for task in iter_tasks(pair_spec(), result)}) == 12
    assert len(list(iter_requests(pair_spec(), result))) == 12
    # A seed changes presentation only, not scheduled occurrence identities.
    another = list(
        prepare_pairs(source, repetitions=3, include_reversals=True, seed=42)
    )
    assert [record.pair_uid for record in another] == [
        record.pair_uid for record in result
    ]
    assert [record.presented_order for record in result] != [
        record.presented_order for record in another
    ]
    assert list(prepare_pairs([pair()]))[0].presented_order == ["left", "right"]
    assert (
        stable_id("orientation", 41, "occurrence", "comparison", 0)
        == "orientation-e67d94da2b0ab42ab5243e649ed51fa2368d0671c0133abe86f5a09d56ee920e"
    )


@pytest.mark.parametrize("repetitions", [0, -1, True, 1.5])
def test_invalid_repetitions(repetitions: object) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        list(prepare_pairs([pair()], repetitions=repetitions))  # pyright: ignore[reportArgumentType]


def test_invalid_pair_seed_and_duplicates() -> None:
    with pytest.raises(ValueError, match="seed"):
        list(prepare_pairs([pair()], seed=True))
    with pytest.raises(ValueError, match="Duplicate"):
        list(prepare_pairs([pair(), pair()]))


def test_reversal_flag_is_boolean() -> None:
    with pytest.raises(ValueError, match="include_reversals"):
        list(prepare_pairs([pair()], include_reversals="yes"))  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize(
    "changes",
    [
        {"is_reversal": True},
        {"repetition_index": 4},
        {"source_pair_uid": "original"},
        {"reversal_group": "audit"},
    ],
)
def test_already_scheduled_pair_metadata_is_never_overwritten(
    changes: dict[str, object],
) -> None:
    scheduled = PairTask.model_validate({**pair().model_dump(), **changes})
    with pytest.raises(ValueError, match="unscheduled"):
        list(prepare_pairs([scheduled], repetitions=2))
