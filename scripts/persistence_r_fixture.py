"""Offline persistence-to-R fixture, with separate persist/export processes.

These are fixed synthetic facts, not provider execution or grouped recovery.
No sources are ingested and no prompt is rendered. Run both phases before the
required examples/persistence_interoperability.R assertions.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Literal, TypeVar

from judgmentllm.io import export_results_csv, write_results_jsonl
from judgmentllm.models import Level, PairTask, Trait
from judgmentllm.persistence.artifacts import ArtifactStore
from judgmentllm.persistence.hashing import bytes_digest, semantic_digest
from judgmentllm.persistence.models import (
    AttemptRecord,
    FailureEvidence,
    ManifestReference,
    ObservationRecord,
    RequestRecord,
    RetryAuthorization,
    RunRecord,
    SubmissionRecord,
    TaskRecord,
)
from judgmentllm.persistence.repository import Repository
from judgmentllm.r_exports import export_pairwise_r_csv, export_rubric_r_csv
from judgmentllm.results import (
    PairwiseResult,
    RubricResult,
    normalize_pairwise_result,
    normalize_rubric_result,
)

RUBRIC_RUN = "foundation-rubric"
PAIR_RUN = "foundation-pairwise"
RecordT = TypeVar("RecordT", bound=RubricResult | PairwiseResult)


def trait(identifier: str) -> Trait:
    return Trait(
        trait_id=identifier,
        name="Synthetic",
        description="Offline persistence fixture",
        levels=[
            Level(label="1", numeric_value=1),
            Level(label="2", numeric_value=3),
            Level(label="3", numeric_value=8),
        ],
    )


def start_run(
    repository: Repository, run_id: str, kind: Literal["rubric", "pairwise"]
) -> None:
    repository.create_run(
        RunRecord(
            run_id=run_id,
            task_kind=kind,
            provider="fixture",
            endpoint="offline",
            model="synthetic-v1",
            capture="minimal",
        )
    )
    artifact = ArtifactStore(repository.run_directory(run_id)).put(
        b'{"fixture":"durable-foundation","schema_version":1}',
        "application/json",
    )
    repository.seal_run(run_id, ManifestReference(artifact=artifact))


def request(
    repository: Repository, run_id: str, uid: str, tasks: list[TaskRecord], ordinal: int
) -> None:
    for task in tasks:
        repository.create_task(task)
    repository.create_request(
        RequestRecord(
            run_id=run_id,
            request_uid=uid,
            scheduling_ordinal=ordinal,
            dependency_sha256=semantic_digest(
                "fixture-dependencies", [task.task_id for task in tasks]
            ),
        ),
        [task.task_id for task in tasks],
    )
    repository.transition("request", run_id, uid, "prepared")


def prepare(
    repository: Repository,
    run_id: str,
    uid: str,
    attempt_id: str,
    tasks: list[str],
    *,
    number: int = 1,
    authorization: str | None = None,
) -> str:
    # Only commitments are needed to test the repository. These bytes are not
    # rendered assessment prompts or requests sent to any provider.
    prompt = f"synthetic-preparation:{uid}:{attempt_id}".encode()
    payload = json.dumps([uid, attempt_id, tasks]).encode()
    for data in (prompt, payload):
        repository.register_artifact(
            run_id, ArtifactStore.hash_only(bytes_digest(data), len(data))
        )
    repository.create_attempt(
        AttemptRecord(
            run_id=run_id,
            attempt_id=attempt_id,
            request_uid=uid,
            attempt_number=number,
            prompt_sha256=bytes_digest(prompt),
            payload_sha256=bytes_digest(payload),
            membership_sha256=semantic_digest("attempt-membership", tasks),
            authorization_id=authorization,
        ),
        tasks,
    )
    submission = f"submission-{attempt_id}"
    repository.create_submission(
        SubmissionRecord(
            run_id=run_id,
            submission_id=submission,
            correlation_id=f"correlation-{run_id}-{attempt_id}",
            attempt_id=attempt_id,
            payload_sha256=bytes_digest(payload),
        )
    )
    repository.transition("attempt", run_id, attempt_id, "claimed")
    repository.transition("attempt", run_id, attempt_id, "submitting")
    repository.transition("submission", run_id, submission, "submitting")
    return submission


def accepted(
    repository: Repository,
    run_id: str,
    attempt_id: str,
    submission: str,
    kind: Literal["response", "refusal"] = "response",
) -> str:
    repository.transition(
        "submission",
        run_id,
        submission,
        "acknowledged",
        remote_id=f"remote-{attempt_id}",
    )
    repository.transition("attempt", run_id, attempt_id, "acknowledged")
    observation = f"observation-{attempt_id}"
    repository.record_observation(
        ObservationRecord(
            run_id=run_id,
            observation_id=observation,
            submission_id=submission,
            attempt_id=attempt_id,
            observation_key="primary",
            kind=kind,
        )
    )
    repository.transition("attempt", run_id, attempt_id, "response_saved")
    return observation


def provenance(repository: Repository, record: RecordT) -> RecordT:
    return type(record).model_validate(
        {
            **record.model_dump(),
            "provider": "fixture",
            "endpoint": "offline",
            "model": "synthetic-v1",
            "mode": "live",
            "manifest_sha256": repository.get_run(record.run_id).manifest_sha256,
        }
    )


def unsuccessful(
    task_id: str,
    identifier: str,
    attempt_id: str,
    uid: str,
    *,
    item_id: str = "001",
    status: Literal["invalid", "refused", "errored"] = "invalid",
    error_kind: str = "invalid_output",
) -> RubricResult:
    return RubricResult(
        run_id=RUBRIC_RUN,
        task_id=task_id,
        request_uid=uid,
        attempt_id=attempt_id,
        item_id=item_id,
        trait_id=identifier,
        resolved_levels=trait(identifier).resolved_levels,
        status=status,
        error_kind=error_kind,
        error_message="Synthetic unsuccessful outcome",
    )


def persist_rubric(repository: Repository) -> None:
    start_run(repository, RUBRIC_RUN, "rubric")
    members = ["evidence", "organization", "clarity"]
    request(
        repository,
        RUBRIC_RUN,
        "grouped-request",
        [
            TaskRecord(
                run_id=RUBRIC_RUN,
                task_id=identifier,
                trait_id=identifier,
                item_id="001",
                scheduling_ordinal=index,
            )
            for index, identifier in enumerate(members)
        ],
        0,
    )
    submission = prepare(
        repository, RUBRIC_RUN, "grouped-request", "grouped-1", members
    )
    observation = accepted(repository, RUBRIC_RUN, "grouped-1", submission)
    score = normalize_rubric_result(
        trait("evidence"),
        run_id=RUBRIC_RUN,
        task_id="evidence",
        request_uid="grouped-request",
        attempt_id="grouped-1",
        item_id="001",
        category_label="2",
        probabilities=[
            {"level_label": "1", "probability": 0.10},
            {"level_label": "2", "probability": 0.25},
            {"level_label": "3", "probability": 0.65},
        ],
        provider_expected_index=1.55,
    )
    with repository.transaction():
        for result in [
            score,
            unsuccessful(
                "organization", "organization", "grouped-1", "grouped-request"
            ),
            unsuccessful(
                "clarity",
                "clarity",
                "grouped-1",
                "grouped-request",
                error_kind="missing_output",
            ),
        ]:
            repository.append_result(
                provenance(repository, result), observation_id=observation
            )
        repository.transition("attempt", RUBRIC_RUN, "grouped-1", "normalized")
    operation = "retry-one-failed-member"
    repository.authorize_retry(
        RetryAuthorization(
            run_id=RUBRIC_RUN,
            operation_id=operation,
            selector_sha256=semantic_digest("fixture-selection", ["organization"]),
        )
    )
    submission = prepare(
        repository,
        RUBRIC_RUN,
        "grouped-request",
        "grouped-2",
        ["organization"],
        number=2,
        authorization=operation,
    )
    observation = accepted(repository, RUBRIC_RUN, "grouped-2", submission)
    retry = normalize_rubric_result(
        trait("organization"),
        run_id=RUBRIC_RUN,
        task_id="organization",
        request_uid="grouped-request",
        attempt_id="grouped-2",
        item_id="001",
        category_label="3",
    )
    repository.append_result(provenance(repository, retry), observation_id=observation)
    repository.transition("attempt", RUBRIC_RUN, "grouped-2", "normalized")
    for ordinal, label in enumerate(("refusal", "transport"), start=1):
        uid = f"request-{label}"
        attempt = f"attempt-{label}"
        item_id = f"00{ordinal + 1}"
        request(
            repository,
            RUBRIC_RUN,
            uid,
            [
                TaskRecord(
                    run_id=RUBRIC_RUN,
                    task_id=label,
                    trait_id="evidence",
                    item_id=item_id,
                    scheduling_ordinal=ordinal + 2,
                )
            ],
            ordinal,
        )
        submission = prepare(repository, RUBRIC_RUN, uid, attempt, [label])
        if label == "refusal":
            observation = accepted(
                repository, RUBRIC_RUN, attempt, submission, "refusal"
            )
            repository.append_result(
                provenance(
                    repository,
                    unsuccessful(
                        label,
                        "evidence",
                        attempt,
                        uid,
                        item_id=item_id,
                        status="refused",
                        error_kind="provider_refusal",
                    ),
                ),
                observation_id=observation,
            )
            repository.transition("attempt", RUBRIC_RUN, attempt, "normalized")
        else:
            repository.transition(
                "submission", RUBRIC_RUN, submission, "confirmed_not_submitted"
            )
            repository.transition("attempt", RUBRIC_RUN, attempt, "not_submitted")
            repository.record_failure(
                FailureEvidence(
                    run_id=RUBRIC_RUN,
                    attempt_id=attempt,
                    submission_id=submission,
                    kind="transport_not_submitted",
                )
            )
            repository.append_result(
                provenance(
                    repository,
                    unsuccessful(
                        label,
                        "evidence",
                        attempt,
                        uid,
                        item_id=item_id,
                        status="errored",
                        error_kind="transport_not_submitted",
                    ),
                ),
                failure_submission_id=submission,
            )


def persist_pairs(repository: Repository) -> None:
    start_run(repository, PAIR_RUN, "pairwise")
    for ordinal, label in enumerate(("reversed", "tie", "invalid")):
        pair = PairTask(
            pair_id=f"pair-{label}",
            pair_uid=f"occurrence-{label}",
            left_item_id="001",
            right_item_id="002",
            trait_id="quality",
            presented_order=["002", "001"] if label == "reversed" else ["001", "002"],
            source_pair_uid=f"source-{label}",
            repetition_index=1,
            is_reversal=label == "reversed",
            reversal_group="audit-group",
        )
        uid = f"request-{label}"
        attempt = f"attempt-{label}"
        request(
            repository,
            PAIR_RUN,
            uid,
            [
                TaskRecord(
                    run_id=PAIR_RUN,
                    task_id=label,
                    trait_id=pair.trait_id,
                    pair_uid=pair.pair_uid,
                    pair_id=pair.pair_id,
                    scheduling_ordinal=ordinal,
                )
            ],
            ordinal,
        )
        submission = prepare(repository, PAIR_RUN, uid, attempt, [label])
        observation = accepted(repository, PAIR_RUN, attempt, submission)
        if label == "invalid":
            result = PairwiseResult(
                run_id=PAIR_RUN,
                task_id=label,
                request_uid=uid,
                attempt_id=attempt,
                pair_id=pair.pair_id,
                pair_uid=pair.pair_uid,
                trait_id=pair.trait_id,
                left_item_id=pair.left_item_id,
                right_item_id=pair.right_item_id,
                original_order=pair.original_order,
                presented_order=pair.presented_order,
                source_pair_uid=pair.source_pair_uid,
                repetition_index=pair.repetition_index,
                is_reversal=pair.is_reversal,
                reversal_group=pair.reversal_group,
                status="invalid",
                error_kind="missing_output",
            )
        else:
            result = normalize_pairwise_result(
                pair,
                run_id=PAIR_RUN,
                task_id=label,
                request_uid=uid,
                attempt_id=attempt,
                winner_id="001" if label == "reversed" else None,
                tie=label == "tie",
                p_left=0.8 if label == "reversed" else 0.2,
                p_right=0.2 if label == "reversed" else 0.3,
                p_tie=0.5 if label == "tie" else None,
                allow_ties=label == "tie",
            )
        repository.append_result(
            provenance(repository, result), observation_id=observation
        )
        repository.transition("attempt", PAIR_RUN, attempt, "normalized")


def digests(repository: Repository) -> dict[str, list[str]]:
    return {
        run_id: [
            semantic_digest("fixture-result", result)
            for result in repository.iter_results(run_id)
        ]
        for run_id in (RUBRIC_RUN, PAIR_RUN)
    }


def persist(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    with Repository(output / "store") as repository:
        persist_rubric(repository)
        persist_pairs(repository)
        (output / "persisted_digests.json").write_text(
            json.dumps(digests(repository)), encoding="utf-8"
        )


def export(output: Path) -> None:
    with Repository(output / "store") as repository:
        assert digests(repository) == json.loads(
            (output / "persisted_digests.json").read_text(encoding="utf-8")
        )
        rubric = [
            result
            for result in repository.iter_results(RUBRIC_RUN)
            if isinstance(result, RubricResult)
        ]
        pairs = [
            result
            for result in repository.iter_results(PAIR_RUN)
            if isinstance(result, PairwiseResult)
        ]
        for name, records in (("rubric", rubric), ("pairwise", pairs)):
            write_results_jsonl(output / f"{name}.jsonl", records)
            export_results_csv(output / f"{name}.csv", records)
        export_rubric_r_csv(output / "rubric_r.csv", rubric)
        export_pairwise_r_csv(output / "pairwise_r.csv", pairs)
        connection = repository.connection
        index: dict[str, object] = {
            "schema_version": 1,
            "request_members": [
                row[0]
                for row in connection.execute(
                    "SELECT task_id FROM request_tasks WHERE run_id=? AND request_uid='grouped-request' ORDER BY position",
                    (RUBRIC_RUN,),
                )
            ],
            "retry_members": [
                row[0]
                for row in connection.execute(
                    "SELECT task_id FROM attempt_tasks WHERE run_id=? AND attempt_id='grouped-2' ORDER BY position",
                    (RUBRIC_RUN,),
                )
            ],
            "transport_observations": connection.execute(
                "SELECT COUNT(*) FROM observations WHERE run_id=? AND attempt_id='attempt-transport'",
                (RUBRIC_RUN,),
            ).fetchone()[0],
            "transport_failure_evidence": connection.execute(
                "SELECT COUNT(*) FROM submission_failures WHERE run_id=? AND attempt_id='attempt-transport'",
                (RUBRIC_RUN,),
            ).fetchone()[0],
            "result_digests": digests(repository),
        }
        (output / "persistence_index.json").write_text(
            json.dumps(index), encoding="utf-8"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", choices=("persist", "export"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    options = parser.parse_args()
    if options.phase == "persist":
        persist(options.output)
    else:
        export(options.output)


if __name__ == "__main__":
    main()
