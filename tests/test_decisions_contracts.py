"""Synthetic grouping, exact commitments, one-time schedules and source indexes."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from test_decisions_lifecycle import native_spec

from judgmentllm.config import RunSpecV4, validate_run_spec
from judgmentllm.decisions import PairPresentationInputV1
from judgmentllm.decisions.records import DecisionsLimitsV1, project_presentation
from judgmentllm.decisions.transport import (
    DecisionsContextV1,
    DecisionsDependencyV1,
    plan_source,
    serialize_request,
)
from judgmentllm.models import ResponseItem
from judgmentllm.persistence.hashing import (
    bytes_digest,
    canonical_bytes,
    semantic_digest,
)
from judgmentllm.persistence.models import RunRecord
from judgmentllm.persistence.repository import Repository
from judgmentllm.planning import stable_id
from judgmentllm.preparation import open_prepared_run, prepare_run


def presentation(seed: int = 41, criteria: int = 3) -> PairPresentationInputV1:
    if criteria == 3:
        data = json.loads(
            (Path(__file__).parent / "fixtures/decisions/sched1.json").read_text()
        )
        data["schedule"]["seed"] = seed
        return PairPresentationInputV1.model_validate_json(json.dumps(data))
    return PairPresentationInputV1.model_validate_json(
        json.dumps(
            dict(
                source_presentation_id="comparison-001",
                pair_id="pair-001",
                left_item_id="001",
                right_item_id="002",
                members=[
                    dict(pair_uid=f"criterion-{i:02d}", trait_id=f"{i:02d}")
                    for i in range(1, criteria + 1)
                ],
                schedule=dict(repetitions=2, include_reversals=True, seed=seed),
            )
        )
    )


def pair_spec(
    tmp_path: Path, *, seed: int = 41, criteria: int = 3, maximum: int | None = 32
) -> RunSpecV4:
    rubric = native_spec(tmp_path, sources=3)
    source = tmp_path / "presentations.jsonl"
    source.write_bytes(canonical_bytes(presentation(seed, criteria)) + b"\n")
    return RunSpecV4.model_validate(
        {
            **rubric.model_dump(),
            "task_kind": "pairwise",
            "rubric": None,
            "criteria": [
                dict(
                    trait_id=f"{i:02d}",
                    name=f"Criterion {i}",
                    description=f"Compare criterion {i} independently.",
                )
                for i in range(1, criteria + 1)
            ],
            "pair_presentations": source,
            "execution": {
                "request_grouping": dict(mode="auto", max_questions_per_request=maximum)
            }
            if maximum
            else {},
        }
    )


@pytest.mark.parametrize(
    "seed,orders,digits",
    [
        (
            41,
            [["002", "001"], ["001", "002"], ["002", "001"], ["001", "002"]],
            ["5", "b"],
        ),
        (
            42,
            [["001", "002"], ["002", "001"], ["002", "001"], ["001", "002"]],
            ["0", "d"],
        ),
    ],
)
def test_sched1_exact_orientation_and_criterion_projection(
    seed: int, orders: list[list[str]], digits: list[str]
) -> None:
    source = presentation(seed)
    result = list(project_presentation(source))
    assert [
        stable_id("presentation-orientation-v1", seed, "comparison-001", "pair-001", i)[
            -1
        ]
        for i in range(2)
    ] == digits
    assert [p.members[0].presented_order for p in result] == orders
    assert len({p.presentation_id for p in result}) == 4
    assert len({m.reversal_group for p in result for m in p.members}) == 2
    assert len({m.pair_uid for p in result for m in p.members}) == 12
    for p, order in zip(result, orders, strict=True):
        assert [m.trait_id for m in p.members] == ["01", "02", "03"]
        assert all(
            m.presented_order == order and m.original_order == ["001", "002"]
            for m in p.members
        )
        assert [m.source_pair_uid for m in p.members] == [
            "criterion-01",
            "criterion-02",
            "criterion-03",
        ]
    other = list(project_presentation(presentation(83 - seed)))
    assert [p.presentation_id for p in result] == [p.presentation_id for p in other]
    assert [m.pair_uid for p in result for m in p.members] == [
        m.pair_uid for p in other for m in p.members
    ]


@pytest.mark.parametrize("maximum,requests", [(32, 4), (2, 8), (None, 12)])
def test_sched1_indexed_prepare_reopen_counts(
    tmp_path: Path, maximum: int | None, requests: int
) -> None:
    spec = pair_spec(tmp_path, maximum=maximum)
    store = tmp_path / "store"
    with prepare_run(spec, store=store, run_id="001") as prepared:
        manifest = prepared.manifest
        assert manifest.schema_version == 8
        assert (
            manifest.presentation_count,
            manifest.logical_task_count,
            manifest.initial_transport_count,
        ) == (4, 12, requests)
    with open_prepared_run("001", store=store) as reopened:
        dependencies = list(reopened.iter_request_dependencies())
        assert len(dependencies) == requests
        assert len({t.task_id for d in dependencies for t in d.request.tasks}) == 12


@pytest.mark.parametrize("questions", [1, 2, 12, 13])
@pytest.mark.parametrize("maximum", [None, 2, 32])
def test_g1_g2_g12_g13_complete_questions(
    tmp_path: Path, questions: int, maximum: int | None
) -> None:
    spec = (
        pair_spec(tmp_path, criteria=questions, maximum=maximum)
        if questions == 13
        else native_spec(tmp_path, traits=questions, maximum=maximum)
    )
    with prepare_run(spec, store=tmp_path / "store", run_id="001") as prepared:
        dependencies = list(prepared.iter_request_dependencies())
        multiplier = 4 if questions == 13 else 1
        expected = (
            questions if maximum is None else (questions + maximum - 1) // maximum
        )
        assert len(dependencies) == multiplier * expected
        assert sum(len(d.request.tasks) for d in dependencies) == multiplier * questions
        for dependency in dependencies:
            assert isinstance(dependency, DecisionsDependencyV1)
            for q in dependency.questions:
                assert (
                    q.trait_id in q.instructions
                    and q.key.startswith("q_")
                    and len(q.key) == 62
                )
                assert "Synthetic source" not in q.instructions
                assert q.trait is not None or q.criterion is not None


@pytest.mark.parametrize(
    "field", ["max_questions", "max_body_bytes", "max_context_tokens"]
)
def test_known_caps_split_contiguous_prefixes(tmp_path: Path, field: str) -> None:
    spec = native_spec(tmp_path, traits=5)
    record = ResponseItem(item_id="001", text="Exact synthetic shared input.")
    context = DecisionsContextV1(service_identity="offline")
    whole = next(plan_source(spec, record, lambda _: record, context, None))
    payload2 = serialize_request(record.text, whole.questions[:2], spec, context)
    limits = DecisionsLimitsV1.model_validate(
        {
            field: 2 if field == "max_questions" else len(payload2),
            "counter": "synthetic_utf8_v1" if field == "max_context_tokens" else None,
        }
    )
    context = context.model_copy(update={"limits": limits})
    dependencies = list(plan_source(spec, record, lambda _: record, context, None))
    assert [len(d.questions) for d in dependencies] == [2, 2, 1]
    assert [q.key for d in dependencies for q in d.questions] == [
        q.key for q in whole.questions
    ]


def test_unknown_limits_missing_counter_oversized_and_control_scope(
    tmp_path: Path,
) -> None:
    spec = native_spec(tmp_path, traits=2)
    record = ResponseItem(item_id="001", text="Synthetic")
    unknown = DecisionsContextV1(service_identity="offline")
    assert (
        unknown.limits.max_questions
        is unknown.limits.max_body_bytes
        is unknown.limits.max_context_tokens
        is None
    )
    assert len(list(plan_source(spec, record, lambda _: record, unknown, None))) == 1
    for limits, match in [
        (DecisionsLimitsV1(max_context_tokens=100_000), "counter"),
        (DecisionsLimitsV1(max_body_bytes=1), "oversized"),
    ]:
        with pytest.raises(ValueError, match=match):
            list(
                plan_source(
                    spec,
                    record,
                    lambda _: record,
                    unknown.model_copy(update={"limits": limits}),
                    None,
                )
            )
    with pytest.raises(ValueError, match="unknown"):
        list(
            plan_source(
                RunSpecV4.model_validate(
                    {**spec.model_dump(), "strict_capabilities": True}
                ),
                record,
                lambda _: record,
                unknown,
                None,
            )
        )
    with pytest.raises(ValueError, match="tier"):
        list(
            plan_source(
                RunSpecV4.model_validate(
                    {**spec.model_dump(), "execution": {"tier": "unsupported"}}
                ),
                record,
                lambda _: record,
                unknown,
                None,
            )
        )


@pytest.mark.parametrize(
    "grouping",
    [
        {},
        {"max_questions_per_request": 2},
        {"mode": "auto", "max_questions_per_request": 0},
        {"mode": "auto", "max_questions_per_request": True},
        {"mode": "auto", "max_questions_per_request": "2"},
        {"mode": "single", "max_questions_per_request": 2},
        {"mode": "auto", "max_questions_per_request": 2, "extra": 1},
    ],
)
def test_strict_grouping_union(tmp_path: Path, grouping: dict[str, object]) -> None:
    spec = native_spec(tmp_path)
    with pytest.raises(ValueError):
        RunSpecV4.model_validate(
            {**spec.model_dump(), "execution": {"request_grouping": grouping}}
        )


def test_explicit_template_exemplar_map_and_no_ten_level_cap(tmp_path: Path) -> None:
    spec = native_spec(tmp_path, traits=2)
    data = spec.model_dump()
    data["exemplars"] = [
        dict(exemplar_id="example-01", text="Example one exact.", scores={"00": "00"}),
        dict(exemplar_id="example-02", text="Example two exact.", scores={"01": "01"}),
    ]
    data["prompt"] = {
        "exemplar_ids_by_trait": {"00": ["example-01"], "01": ["example-02"]}
    }
    complete = RunSpecV4.model_validate(data)
    with prepare_run(complete, store=tmp_path / "store", run_id="001") as prepared:
        dependency = next(prepared.iter_request_dependencies())
        assert isinstance(dependency, DecisionsDependencyV1)
        questions = dependency.questions
        assert (
            "Example one exact." in questions[0].instructions
            and "Example two exact." not in questions[0].instructions
        )
        assert (
            "Example two exact." in questions[1].instructions
            and "Example one exact." not in questions[1].instructions
        )
        assert complete.rubric is not None
        assert prepared.manifest.traits[0].sha256 == semantic_digest(
            "trait", complete.rubric.traits[0]
        )
        assert len(prepared.manifest.traits[0].categories) == 3
        assert [e.sha256 for e in prepared.manifest.exemplars] == [
            semantic_digest("exemplar", e) for e in complete.exemplars
        ]
        split = RunSpecV4.model_validate(
            {
                **data,
                "execution": {
                    "request_grouping": {"mode": "auto", "max_questions_per_request": 1}
                },
            }
        )
        record = next(prepared.iter_responses())
        assert [
            d.request.exemplar_ids
            for d in plan_source(
                split,
                record,
                lambda _: record,
                DecisionsContextV1(service_identity="offline"),
                None,
            )
        ] == [("example-01",), ("example-02",)]
    data["prompt"] = {
        "exemplar_ids_by_trait": {"00": ["example-02"], "01": ["example-01"]}
    }
    with pytest.raises(ValueError, match="annotated"):
        RunSpecV4.model_validate(data)
    assert spec.rubric is not None
    rubric = spec.rubric.model_dump()
    rubric["traits"][0]["levels"] = [
        dict(label=str(i), numeric_value=i) for i in range(21)
    ]
    expanded = validate_run_spec({**spec.model_dump(), "rubric": rubric})
    assert isinstance(expanded, RunSpecV4) and expanded.rubric is not None
    with prepare_run(expanded, store=tmp_path / "twenty-one", run_id="001") as prepared:
        first = next(prepared.repository.iter_attempts("001"))
        from judgmentllm.decisions.transport import DecisionsCommitmentV1

        commitment = prepared.transport_for_attempt(first.attempt_id)
        assert isinstance(commitment, DecisionsCommitmentV1)
        assert len(commitment.questions[0].scale_mapping) == 21


def test_manifest_context_cannot_be_replaced_by_recomputing_public_hash(
    tmp_path: Path,
) -> None:
    from judgmentllm.preparation import PreparationError

    spec = native_spec(tmp_path)
    with prepare_run(spec, store=tmp_path / "store", run_id="001") as prepared:
        prepared.manifest = prepared.manifest.model_copy(update={"seed": 999})
        prepared.manifest_sha256 = bytes_digest(canonical_bytes(prepared.manifest))
        with pytest.raises(PreparationError, match="seal"):
            prepared.verify_compatibility()


def test_presentation_tables_and_marker_are_immutable_and_frozen(
    tmp_path: Path,
) -> None:
    spec = pair_spec(tmp_path)
    store = tmp_path / "store"
    with prepare_run(spec, store=store, run_id="001"):
        pass
    with Repository(store) as repository:
        assert repository.decisions_version("001") == 4
        for sql in (
            "UPDATE runs SET decisions_version=NULL WHERE run_id='001'",
            "DELETE FROM runs WHERE run_id='001'",
            "UPDATE presentation_source_members SET trait_id='foreign'",
            "DELETE FROM presentation_inputs",
            "INSERT OR REPLACE INTO presentation_source_members SELECT * FROM presentation_source_members",
            "INSERT INTO presentation_source_members VALUES('001','comparison-001','late','late',99)",
        ):
            with pytest.raises(sqlite3.IntegrityError), repository.connection:
                repository.connection.execute(sql)
        repository.create_run(
            RunRecord(
                run_id="old",
                task_kind="rubric",
                provider="mock",
                endpoint="synthetic",
                model="legacy",
            )
        )
        with pytest.raises(sqlite3.IntegrityError), repository.connection:
            repository.connection.execute(
                "UPDATE runs SET decisions_version=4 WHERE run_id='old'"
            )
        assert repository.decisions_version("old") is None


def test_identical_text_never_groups_source_occurrences(tmp_path: Path) -> None:
    spec = native_spec(tmp_path, sources=2)
    spec.input.path.write_text(
        '{"item_id":"001","text":"same"}\n{"item_id":"002","text":"same"}\n',
        encoding="utf-8",
    )
    with prepare_run(spec, store=tmp_path / "store", run_id="001") as prepared:
        dependencies = list(prepared.iter_request_dependencies())
        assert len(dependencies) == 2
        assert {t.item_id for t in dependencies[0].request.tasks} == {"001"}
        assert {t.item_id for t in dependencies[1].request.tasks} == {"002"}


@pytest.mark.parametrize("capture", ["full", "minimal"])
def test_native_originals_and_explicit_snapshot_recovery(
    tmp_path: Path, capture: str
) -> None:
    from judgmentllm.preparation import PreparationError

    spec = pair_spec(tmp_path)
    spec = RunSpecV4.model_validate({**spec.model_dump(), "capture": capture})
    store = tmp_path / "store"
    with prepare_run(spec, store=store, run_id="001"):
        pass
    assert spec.pair_presentations is not None
    spec.pair_presentations.path.unlink()
    with pytest.raises(PreparationError):
        open_prepared_run("001", store=store, spec=spec)
    if capture == "minimal":
        with pytest.raises(PreparationError):
            open_prepared_run("001", store=store, spec=spec, use_frozen_snapshot=True)
    else:
        with open_prepared_run(
            "001", store=store, use_frozen_snapshot=True
        ) as recovered:
            assert len(list(recovered.iter_request_dependencies())) == 4


def test_legacy_criterion_rows_never_acquire_common_presentation(
    tmp_path: Path,
) -> None:
    from judgmentllm.models import PairTask
    from judgmentllm.planning import prepare_pairs

    spec = pair_spec(tmp_path)
    sources = [
        PairTask(
            pair_id="pair-001",
            pair_uid=f"criterion-{i:02d}",
            left_item_id="001",
            right_item_id="002",
            trait_id=f"{i:02d}",
        )
        for i in range(1, 4)
    ]
    rows = list(prepare_pairs(sources, repetitions=2, seed=41))
    assert [p.presented_order for p in rows if p.repetition_index == 1] == [
        ["002", "001"],
        ["001", "002"],
        ["001", "002"],
    ]
    path = tmp_path / "legacy-pairs.jsonl"
    path.write_text("".join(p.model_dump_json() + "\n" for p in rows), encoding="utf-8")
    spec = RunSpecV4.model_validate(
        {**spec.model_dump(), "pair_presentations": None, "pairs": path}
    )
    with prepare_run(spec, store=tmp_path / "store", run_id="001") as prepared:
        dependencies = list(prepared.iter_request_dependencies())
        assert len(dependencies) == 6 and all(
            len(d.request.tasks) == 1 for d in dependencies
        )


def test_unsealed_source_membership_has_real_uniqueness_and_foreign_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import judgmentllm.preparation as preparation

    spec = pair_spec(tmp_path)
    store = tmp_path / "store"

    def fail(stage: str, _source: str | None = None) -> None:
        if stage == "presentation_index_verified":
            raise RuntimeError("synthetic preparation interruption")

    monkeypatch.setattr(preparation, "_checkpoint", fail)
    with pytest.raises(RuntimeError):
        prepare_run(spec, store=store, run_id="001")
    with Repository(store) as repository:
        assert repository.get_run("001").state == "created"
        assert repository.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        cases = [
            "INSERT INTO presentation_inputs SELECT * FROM presentation_inputs",
            "INSERT OR REPLACE INTO presentation_inputs SELECT * FROM presentation_inputs",
            "INSERT INTO presentation_source_members VALUES('001','foreign','new','99',10)",
            "INSERT INTO presentation_source_members VALUES('001','comparison-001','new','01',10)",
            "INSERT INTO presentation_source_members VALUES('001','comparison-001','new','99',0)",
            "INSERT INTO presentation_source_members VALUES('001','comparison-001','criterion-01','99',10)",
        ]
        for sql in cases:
            with pytest.raises(sqlite3.IntegrityError), repository.connection:
                repository.connection.execute(sql)
        assert (
            repository.connection.execute(
                "SELECT count(*) FROM presentation_inputs"
            ).fetchone()[0]
            == 1
        )
        assert (
            repository.connection.execute(
                "SELECT count(*) FROM presentation_source_members"
            ).fetchone()[0]
            == 3
        )


def test_native_source_commitments_do_not_broaden_historical_manifest_authority(
    tmp_path: Path,
) -> None:
    from judgmentllm.preparation.manifest import (
        ExecutableRunManifestV8,
        PresentationSourceCommitmentV1,
        SourceCommitment,
    )

    spec = pair_spec(tmp_path)
    with prepare_run(spec, store=tmp_path / "store", run_id="001") as prepared:
        assert isinstance(prepared.manifest, ExecutableRunManifestV8)
        source = next(
            s
            for s in prepared.manifest.sources
            if isinstance(s, PresentationSourceCommitmentV1)
        )
        with pytest.raises(ValueError):
            SourceCommitment.model_validate_json(canonical_bytes(source))


def test_public_presentation_iterator_revalidates_and_detects_duplicate_layers() -> (
    None
):
    from judgmentllm.decisions import prepare_pair_presentations

    source = presentation()
    rows = list(prepare_pair_presentations(iter((source,))))
    assert len(rows) == 4 and sum(len(row.members) for row in rows) == 12
    for duplicate in (
        source,
        source.model_copy(update={"source_presentation_id": "other"}),
    ):
        with pytest.raises(ValueError, match="duplicate"):
            list(prepare_pair_presentations(iter((source, duplicate))))
    object.__setattr__(source.schedule, "repetitions", True)
    with pytest.raises(ValueError):
        list(prepare_pair_presentations(iter((source,))))


@pytest.mark.parametrize(
    "scope,field,value",
    [
        ("execution", "grouping", "grouped"),
        ("execution", "groups", [["00"]]),
        ("execution", "max_traits_per_request", 2),
        ("prompt", "preset", "rubric_scores_only"),
        ("prompt", "structured_output", "native"),
        ("prompt", "output_order", "together"),
        ("prompt", "native_mechanism", "openai.responses.text.format"),
    ],
)
def test_v4_rejects_mixed_legacy_authorities(
    tmp_path: Path, scope: str, field: str, value: object
) -> None:
    spec = native_spec(tmp_path)
    data = spec.model_dump()
    data[scope][field] = value
    with pytest.raises(ValueError):
        RunSpecV4.model_validate(data)


def test_opaque_question_key_collision_fails_instead_of_overwriting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import judgmentllm.decisions.records as records
    import judgmentllm.decisions.transport as transport

    def collision(_task: str) -> str:
        return "q_" + "0" * 60

    monkeypatch.setattr(records, "question_key", collision)
    monkeypatch.setattr(transport, "question_key", collision)
    spec = native_spec(tmp_path, traits=2)
    record = ResponseItem(item_id="001", text="Synthetic")
    with pytest.raises(ValueError, match="crosswalk"):
        list(
            plan_source(
                spec,
                record,
                lambda _: record,
                DecisionsContextV1(service_identity="offline"),
                None,
            )
        )
