"""Provider-free, immutable all-attempt exports over a bounded result snapshot."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Literal, cast

from pydantic import Field

from judgmentllm.io import Result, export_results_csv, write_results_jsonl
from judgmentllm.persistence.artifacts import ArtifactStore
from judgmentllm.persistence.hashing import semantic_digest
from judgmentllm.persistence.models import (
    ArtifactIdentity,
    ExecutionModel,
    IntegrityError,
    RunState,
    utc_now,
)
from judgmentllm.persistence.repository import Repository
from judgmentllm.r_exports import (
    _validate_na,  # pyright: ignore[reportPrivateUsage]
    export_pairwise_r_csv,
    export_rubric_r_csv,
)
from judgmentllm.results import PairwiseResult, RubricResult

ExportFault = Callable[[str], None]


class ExportReport(ExecutionModel):
    """Published identity plus current lifecycle status; no inferred missing rows."""

    run_id: str
    export_id: str
    format: Literal["jsonl", "csv"]
    view: Literal["canonical", "r"]
    result_high_water_mark: int = Field(ge=0)
    row_count: int = Field(ge=0)
    artifact: ArtifactIdentity
    artifact_path: str
    run_state: RunState
    run_complete: bool


def _chunks(path: Path) -> Iterator[bytes]:
    with path.open("rb") as stream:
        while chunk := stream.read(64 * 1024):
            yield chunk


def _rubric(records: Iterator[Result]) -> Iterator[RubricResult]:
    for record in records:
        if not isinstance(record, RubricResult):
            raise IntegrityError("rubric export encountered a different result kind")
        yield record


def _pairwise(records: Iterator[Result]) -> Iterator[PairwiseResult]:
    for record in records:
        if not isinstance(record, PairwiseResult):
            raise IntegrityError("pairwise export encountered a different result kind")
        yield record


def export_run(
    run_id: str,
    *,
    store: str | Path,
    format: Literal["jsonl", "csv"] = "jsonl",
    view: Literal["canonical", "r"] = "canonical",
    high_water_mark: int | None = None,
    page_size: int = 128,
    na_string: str = "NA",
    fault: ExportFault | None = None,
) -> ExportReport:
    """Stream a fixed all-attempt snapshot and publish once without provider access.

    Published files live in the existing immutable content-addressed artifact
    store. Only a committed ``exports`` row makes a publication visible. A crash
    may leave an unreferenced object, never a partially published export. The
    returned lifecycle status is current at reporting time, independent of the
    immutable selected result snapshot.
    """
    if format not in {"jsonl", "csv"} or view not in {"canonical", "r"}:
        raise ValueError("unsupported export format or view")
    if format == "jsonl" and view != "canonical":
        raise ValueError("R-compatible export requires CSV")
    if type(page_size) is not int or page_size < 1:
        raise ValueError("page size must be positive")
    if view == "r":
        _validate_na(na_string)
    elif na_string != "NA":
        raise ValueError("na_string applies only to the R-compatible CSV view")

    def checkpoint(stage: str) -> None:
        if fault is not None:
            fault(stage)

    with Repository(store) as repository:
        run = repository.get_run(run_id)
        maximum = repository.connection.execute(
            "SELECT COALESCE(MAX(result_seq),0) FROM results WHERE run_id=?",
            (run_id,),
        ).fetchone()
        assert maximum is not None
        selected = cast(int, maximum[0]) if high_water_mark is None else high_water_mark
        if type(selected) is not int or not 0 <= selected <= maximum[0]:
            raise ValueError("high-water mark must select an existing result snapshot")
        export_id = semantic_digest(
            "result-export",
            [run_id, format, view, selected, na_string if view == "r" else None],
        )
        root = repository.run_directory(run_id)
        artifact_store = ArtifactStore(root)
        checkpoint("after_export_snapshot")
        existing = repository.connection.execute(
            "SELECT * FROM exports WHERE run_id=? AND export_id=?",
            (run_id, export_id),
        ).fetchone()
        if existing is None:
            expected = repository.connection.execute(
                "SELECT COUNT(*) FROM results WHERE run_id=? AND result_seq<=?",
                (run_id, selected),
            ).fetchone()
            assert expected is not None

            def records() -> Iterator[Result]:
                for number, record in enumerate(
                    repository.iter_results(
                        run_id, page_size=page_size, high_water_mark=selected
                    )
                ):
                    if number % page_size == 0:
                        checkpoint("during_export_page")
                    yield record

            # Writers use their existing schemas, coding and missing-value rules.
            # Temporary bytes contain only authorized normalized result data.
            with tempfile.TemporaryDirectory(prefix=".export-", dir=root) as temporary:
                path = Path(temporary) / "results"
                if format == "jsonl":
                    count = write_results_jsonl(path, records())
                elif view == "canonical":
                    count = export_results_csv(path, records())
                elif run.task_kind == "rubric":
                    count = export_rubric_r_csv(
                        path, _rubric(records()), na_string=na_string
                    )
                else:
                    count = export_pairwise_r_csv(
                        path, _pairwise(records()), na_string=na_string
                    )
                if count != expected[0]:
                    raise IntegrityError(
                        "export row count differs from selected snapshot"
                    )
                with path.open("rb") as stream:
                    os.fsync(stream.fileno())
                checkpoint("after_export_serialization")
                artifact = artifact_store.put_stream(
                    _chunks(path),
                    "application/x-ndjson" if format == "jsonl" else "text/csv",
                )
            checkpoint("after_export_artifact")
            with repository.transaction():
                # Concurrent exporters converge on the same immutable identity.
                registered = repository.connection.execute(
                    "SELECT byte_length FROM artifacts WHERE run_id=? AND sha256=?",
                    (run_id, artifact.sha256),
                ).fetchone()
                if registered is None:
                    # Publication already verified the bytes. Avoid file I/O
                    # inside the short metadata transaction.
                    repository._register_artifact(run_id, artifact)  # pyright: ignore[reportPrivateUsage]
                elif registered["byte_length"] != artifact.byte_length:
                    raise IntegrityError("export hash has conflicting byte length")
                # A source with identical bytes may already have a different
                # media type or hash-only retention. Its metadata stays frozen;
                # export publication independently authorizes normalized bytes.
                prior = repository.connection.execute(
                    "SELECT * FROM exports WHERE run_id=? AND export_id=?",
                    (run_id, export_id),
                ).fetchone()
                if prior is None:
                    repository.connection.execute(
                        "INSERT INTO exports(run_id,export_id,format,view,result_high_water_mark,row_count,artifact_sha256,state,created_at) VALUES (?,?,?,?,?,?,?,'published',?)",
                        (
                            run_id,
                            export_id,
                            format,
                            view,
                            selected,
                            count,
                            artifact.sha256,
                            utc_now(),
                        ),
                    )
                elif (
                    prior["artifact_sha256"] != artifact.sha256
                    or prior["row_count"] != count
                    or prior["format"] != format
                    or prior["view"] != view
                    or prior["result_high_water_mark"] != selected
                    or prior["state"] != "published"
                ):
                    raise IntegrityError(
                        "export identity conflicts with published bytes"
                    )
                checkpoint("during_export_transaction")
            checkpoint("after_export_commit")
            existing = repository.connection.execute(
                "SELECT * FROM exports WHERE run_id=? AND export_id=?",
                (run_id, export_id),
            ).fetchone()
            assert existing is not None
            if (
                existing["artifact_sha256"] != artifact.sha256
                or existing["row_count"] != count
            ):
                raise IntegrityError("export identity conflicts with published bytes")
        if (
            existing["format"] != format
            or existing["view"] != view
            or existing["result_high_water_mark"] != selected
            or existing["state"] != "published"
        ):
            raise IntegrityError("export identity conflicts with published metadata")
        registered = repository.connection.execute(
            "SELECT byte_length FROM artifacts WHERE run_id=? AND sha256=?",
            (run_id, existing["artifact_sha256"]),
        ).fetchone()
        if registered is None:
            raise IntegrityError("published export lacks byte identity metadata")
        digest = existing["artifact_sha256"]
        artifact = ArtifactIdentity(
            sha256=digest,
            byte_length=registered["byte_length"],
            media_type="application/x-ndjson" if format == "jsonl" else "text/csv",
            relative_path=f"objects/{digest[:2]}/{digest}",
        )
        path = artifact_store.verify(artifact)
        if path is None:
            raise IntegrityError("published export requires retained bytes")
        current = repository.summary(run_id)
        return ExportReport(
            run_id=run_id,
            export_id=export_id,
            format=format,
            view=view,
            result_high_water_mark=selected,
            row_count=existing["row_count"],
            artifact=artifact,
            artifact_path=str(path),
            run_state=current.state,
            run_complete=current.state == "completed"
            and not current.unresolved_submissions,
        )
