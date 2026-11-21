"""Public native-batch lifecycle. The only executable adapter is offline mock."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypedDict, Unpack

from judgmentllm._lifecycle_store import discover_store, resolve_run
from judgmentllm.batch_models import (
    BatchControlOptionsV1,
    BatchMockOptions,
    BatchRunStatusV1,
)
from judgmentllm.config import RunSpecification
from judgmentllm.execution.native_batch import (
    BatchSession,
    BudgetExhausted,
    Fault,
    InvocationBudget,
)
from judgmentllm.lifecycle import _checked_spec, _errors, _no_event_loop  # pyright: ignore[reportPrivateUsage]
from judgmentllm.lifecycle_models import RetrySelection
from judgmentllm.persistence.native_batch import detail, record_stop, require_storage
from judgmentllm.persistence.ownership import ExecutorOwnership
from judgmentllm.persistence.repository import ReadOnlyRepository, Repository
from judgmentllm.preparation import _load_manifest, prepare_run  # pyright: ignore[reportPrivateUsage]
from judgmentllm.preparation.manifest import ExecutableRunManifestV12
from judgmentllm.providers.batch_mock import MockBatchAdapter, require_mock_batch

if TYPE_CHECKING:
    from judgmentllm.execution.batch_exports import BatchAuditReportV1


class BatchContinuationV1(TypedDict, total=False):
    store: str | Path | None
    spec: RunSpecification | None
    use_frozen_snapshot: bool
    mock: BatchMockOptions | None
    options: BatchControlOptionsV1 | None
    _fault: Fault | None
    retry: RetrySelection | None


def prepare_batch(
    spec_or_path: RunSpecification | str | Path,
    *,
    store: str | Path | None = None,
    run_id: str | None = None,
    mock: BatchMockOptions | None = None,
) -> BatchRunStatusV1:
    """Freeze all members and deterministic shards without a remote mutation."""
    with _errors():
        spec = _checked_spec(spec_or_path)
        require_mock_batch(spec)
        root = discover_store(store, spec)
        if (root / "registry.sqlite3").exists():
            with ReadOnlyRepository(root) as repository:
                require_storage(repository)
        options = mock or BatchMockOptions()
        adapter = MockBatchAdapter(
            options.ledger_path or root / "mock-batch-service.sqlite3",
            options,
            create=True,
        )
        identifier = run_id or uuid.uuid4().hex
        with prepare_run(
            spec_or_path,
            store=root,
            run_id=identifier,
            capabilities=adapter.capabilities(spec),
            exhaustive_rendering=True,
            _batch_adapter=adapter,
        ):
            pass
        return get_batch_status(identifier, store=root)


def get_batch_status(
    run_id: str | Path, *, store: str | Path | None = None
) -> BatchRunStatusV1:
    """Inspect a short local snapshot; no adapter or assessment source is opened."""
    with _errors():
        identifier, root = resolve_run(run_id, store)
    with _errors(identifier, root), ReadOnlyRepository(root) as repository:
        repository.connection.execute("BEGIN")
        try:
            manifest = _load_manifest(repository, identifier, unsealed=False)
            if not isinstance(manifest, ExecutableRunManifestV12):
                raise ValueError("run has no native batch manifest")
            state = repository.get_run(identifier).state
            batch_detail = detail(repository, identifier, manifest.batch_shard_count)
        finally:
            repository.connection.execute("ROLLBACK")
        directory = repository.run_directory(identifier)
        return BatchRunStatusV1(
            run_id=identifier,
            run_status=state,
            outcome="ok",
            batch=batch_detail,
            artifacts={
                "store": str(root),
                "run_directory": str(directory),
                "registry": str(root / "registry.sqlite3"),
                "manifest": str(directory / "manifest.json"),
            },
        )


async def _control(
    action: Literal["submit", "poll", "collect", "resume", "cancel", "stop"],
    run_id: str | Path,
    *,
    store: str | Path | None = None,
    spec: RunSpecification | None = None,
    use_frozen_snapshot: bool = False,
    mock: BatchMockOptions | None = None,
    options: BatchControlOptionsV1 | None = None,
    _fault: Fault | None = None,
    retry: RetrySelection | None = None,
) -> BatchRunStatusV1:
    with _errors():
        identifier, root = resolve_run(run_id, store)
        budget = InvocationBudget(options or BatchControlOptionsV1())
    get_batch_status(identifier, store=root)
    if action == "cancel":
        with _errors(identifier, root), Repository(root) as repository:
            with ExecutorOwnership(repository, identifier) as ownership:
                assert ownership.owner is not None
                record_stop(repository, identifier, ownership.owner.token, remote=True)
    exhausted = False
    with (
        _errors(identifier, root),
        BatchSession(
            identifier,
            root,
            MockBatchAdapter(
                (mock.ledger_path if mock else None)
                or root / "mock-batch-service.sqlite3",
                mock or BatchMockOptions(),
            ),
            budget,
            _fault,
        ) as session,
    ):
        try:
            if action in ("cancel", "stop"):
                session.stop(remote=action == "cancel")
            if action != "stop":
                await session.recover_operations()
                if action in ("submit", "collect", "resume"):
                    session.open_sources(spec, use_frozen_snapshot)
                    from judgmentllm.preparation.batch_retry import (
                        recover_retry_preparations,
                    )

                    recover_retry_preparations(session)
                if retry is not None:
                    if action != "resume":
                        raise ValueError(
                            "scientific retry requires explicit batch resume"
                        )
                    from judgmentllm.preparation.batch_retry import prepare_batch_retry

                    prepare_batch_retry(session, retry)
                if action in ("submit", "resume"):
                    await session.submit()
                if action in ("cancel", "resume"):
                    await session.cancel_known()
                if action in ("poll", "resume"):
                    await session.poll()
                if action in ("collect", "resume"):
                    await session.collect()
            session.finish()
        except (BudgetExhausted, TimeoutError, OSError):
            exhausted = True
        except asyncio.CancelledError:
            session.stop(remote=False)
            raise
    status = get_batch_status(identifier, store=root)
    partial = exhausted or status.batch.unresolved_operations or status.batch.conflicts
    if action in ("collect", "resume"):
        partial = partial or status.batch.missing_members
    if action == "submit":
        partial = partial or status.batch.accepted_jobs != status.batch.jobs
    return status.model_copy(update={"outcome": "partial" if partial else "ok"})


async def asubmit_batch(
    run_id: str | Path, **kwargs: Unpack[BatchContinuationV1]
) -> BatchRunStatusV1:
    return await _control("submit", run_id, **kwargs)


async def apoll_batch(
    run_id: str | Path, **kwargs: Unpack[BatchContinuationV1]
) -> BatchRunStatusV1:
    return await _control("poll", run_id, **kwargs)


async def acollect_batch(
    run_id: str | Path, **kwargs: Unpack[BatchContinuationV1]
) -> BatchRunStatusV1:
    return await _control("collect", run_id, **kwargs)


async def aresume_batch(
    run_id: str | Path, **kwargs: Unpack[BatchContinuationV1]
) -> BatchRunStatusV1:
    return await _control("resume", run_id, **kwargs)


async def acancel_batch(
    run_id: str | Path, **kwargs: Unpack[BatchContinuationV1]
) -> BatchRunStatusV1:
    return await _control("cancel", run_id, **kwargs)


def submit_batch(
    run_id: str | Path, **kwargs: Unpack[BatchContinuationV1]
) -> BatchRunStatusV1:
    _no_event_loop("submit_batch")
    return asyncio.run(asubmit_batch(run_id, **kwargs))


def poll_batch(
    run_id: str | Path, **kwargs: Unpack[BatchContinuationV1]
) -> BatchRunStatusV1:
    _no_event_loop("poll_batch")
    return asyncio.run(apoll_batch(run_id, **kwargs))


def collect_batch(
    run_id: str | Path, **kwargs: Unpack[BatchContinuationV1]
) -> BatchRunStatusV1:
    _no_event_loop("collect_batch")
    return asyncio.run(acollect_batch(run_id, **kwargs))


def resume_batch(
    run_id: str | Path, **kwargs: Unpack[BatchContinuationV1]
) -> BatchRunStatusV1:
    _no_event_loop("resume_batch")
    return asyncio.run(aresume_batch(run_id, **kwargs))


def cancel_batch(
    run_id: str | Path, **kwargs: Unpack[BatchContinuationV1]
) -> BatchRunStatusV1:
    _no_event_loop("cancel_batch")
    return asyncio.run(acancel_batch(run_id, **kwargs))


def stop_batch(
    run_id: str | Path, *, store: str | Path | None = None
) -> BatchRunStatusV1:
    with _errors():
        identifier, root = resolve_run(run_id, store)
    get_batch_status(identifier, store=root)
    with _errors(identifier, root), Repository(root) as repository:
        require_storage(repository)
        with ExecutorOwnership(repository, identifier) as ownership:
            assert ownership.owner is not None
            record_stop(repository, identifier, ownership.owner.token, remote=False)
    return get_batch_status(identifier, store=root)


def export_batch_provenance(
    run_id: str | Path, *, store: str | Path | None = None, bundle_id: str | None = None
) -> BatchAuditReportV1:
    """Publish or reopen the normalized/provenance bundle from one audit cut."""
    from judgmentllm.execution.batch_exports import export_batch_audit

    with _errors():
        identifier, root = resolve_run(run_id, store)
    with _errors(identifier, root):
        return export_batch_audit(identifier, root, bundle_id=bundle_id)
