"""Public native-batch lifecycle. The only executable adapter is offline mock."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypedDict, Unpack

from judgmentllm._lifecycle_store import discover_store, resolve_run
from judgmentllm.batch_models import (
    BatchControlOptionsV1,
    BatchDiagnosticV1,
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
from judgmentllm.preparation import (
    _load_manifest,  # pyright: ignore[reportPrivateUsage]
    _preflight_v12_write,  # pyright: ignore[reportPrivateUsage]
    prepare_run,
)
from judgmentllm.preparation.manifest import (
    ExecutableRunManifestV12,
    ExecutableRunManifestV13,
)
from judgmentllm.providers.batch_mock import MockBatchAdapter, require_mock_batch
from judgmentllm.providers.batch_protocol import BatchEvidenceWithheld

if TYPE_CHECKING:
    from judgmentllm.execution.batch_exports import (
        BatchAuditReportV1,
        BatchAuditReportV2,
    )


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
        if spec.provider == "openai" and spec.schema_version == 3:
            from judgmentllm.openai_batch_transport import (
                batch_context,
                require_openai_batch,
            )
            from judgmentllm.providers.openai_batch import OpenAIBatchAdapter

            require_openai_batch(spec)
            if mock is not None:
                raise ValueError("mock controls cannot configure an OpenAI Batch run")
            adapter = OpenAIBatchAdapter(batch_context(spec))
            adapter.validate(spec)
        else:
            require_mock_batch(spec)
            adapter = None
        root = discover_store(store, spec)
        if (root / "registry.sqlite3").exists():
            with ReadOnlyRepository(root) as repository:
                require_storage(repository)
        options = mock or BatchMockOptions()
        if adapter is None:
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
        identifier, root = resolve_run(run_id, store, allow_v12_posix_read=True)
    return _batch_status(identifier, root, allow_v12_posix_read=True)


def _batch_status(
    identifier: str, root: Path, *, allow_v12_posix_read: bool = False
) -> BatchRunStatusV1:
    with _errors(identifier, root), ReadOnlyRepository(root) as repository:
        repository.connection.execute("BEGIN")
        try:
            if not allow_v12_posix_read:
                _preflight_v12_write(repository, identifier)
            manifest = _load_manifest(
                repository,
                identifier,
                unsealed=False,
                allow_v12_posix_read=allow_v12_posix_read,
            )
            if not isinstance(
                manifest, (ExecutableRunManifestV12, ExecutableRunManifestV13)
            ):
                raise ValueError("run has no native batch manifest")
            state = repository.get_run(identifier).state
            batch_detail = detail(repository, identifier, manifest.batch_shard_count)
            collection = None
            if isinstance(manifest, ExecutableRunManifestV13):
                from judgmentllm.execution.openai_batch import collection_summary

                collection = collection_summary(repository, identifier)
        finally:
            repository.connection.execute("ROLLBACK")
        directory = repository.run_directory(identifier)
        return BatchRunStatusV1(
            collection=collection,
            run_id=identifier,
            run_status=state,
            outcome="error"
            if collection is not None
            and (collection.failed_sources or collection.prelude_failed)
            else "ok",
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
    with _errors(identifier, root), ReadOnlyRepository(root) as repository:
        _preflight_v12_write(repository, identifier)
        manifest = _load_manifest(repository, identifier, unsealed=False)
        if not isinstance(
            manifest, (ExecutableRunManifestV12, ExecutableRunManifestV13)
        ):
            raise ValueError("run has no native batch manifest")
    if action == "cancel":
        with _errors(identifier, root), Repository(root) as repository:
            with ExecutorOwnership(repository, identifier) as ownership:
                assert ownership.owner is not None
                record_stop(repository, identifier, ownership.owner.token, remote=True)
    openai = isinstance(manifest, ExecutableRunManifestV13)
    from judgmentllm.execution.openai_batch import OpenAIBatchSession

    if openai:
        with _errors(identifier, root):
            from judgmentllm.openai_batch_transport import require_sdk
            from judgmentllm.providers.openai_batch import OpenAIBatchAdapter

            if mock is not None:
                raise ValueError("mock controls cannot configure an OpenAI Batch run")
            require_sdk()
            adapter = OpenAIBatchAdapter(manifest.batch_context, budget=budget)
            adapter.reconcile_uploads = action != "poll"
            session_type = OpenAIBatchSession
    else:
        adapter = MockBatchAdapter(
            (mock.ledger_path if mock else None) or root / "mock-batch-service.sqlite3",
            mock or BatchMockOptions(),
        )
        session_type = BatchSession
    exhausted = False
    with (
        _errors(identifier, root),
        session_type(identifier, root, adapter, budget, _fault) as session,
    ):
        try:
            if action in ("cancel", "stop"):
                session.stop(remote=action == "cancel")
            selected = False
            if isinstance(session, OpenAIBatchSession) and action in (
                "collect",
                "resume",
            ):
                selected = await session.collect_ready(spec, use_frozen_snapshot)
                if selected and retry is not None:
                    exhausted = (
                        True  # The caller's retry is deferred, never silently consumed.
                    )
            if action != "stop" and not selected:
                if openai and action in ("submit", "resume", "collect"):
                    session.open_sources(spec, use_frozen_snapshot)
                await session.recover_operations()
                if action in ("submit", "collect", "resume"):
                    if not openai:
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
                    if isinstance(session, OpenAIBatchSession):
                        await session.collect_ready(spec, use_frozen_snapshot)
                    else:
                        await session.collect()
            if budget.external_cancelled():
                raise asyncio.CancelledError
            session.finish()
        except BatchEvidenceWithheld:
            session.write_fact(
                "withheld", identifier, BatchDiagnosticV1(code="unsafe_evidence")
            )
            exhausted = True
        except (BudgetExhausted, TimeoutError, OSError):
            if budget.external_cancelled():
                session.stop(remote=False)
                raise asyncio.CancelledError from None
            if isinstance(session, OpenAIBatchSession) and action in (
                "collect",
                "resume",
            ):
                session.selection_pending = True
            exhausted = True
        except BaseException:
            if budget.external_cancelled():
                session.stop(remote=False)
            raise
        finally:
            if isinstance(session, OpenAIBatchSession):
                from typing import Any, cast

                from judgmentllm.lifecycle import _close_adapter  # pyright: ignore[reportPrivateUsage]

                await _close_adapter(cast(Any, session.adapter))
    status = get_batch_status(identifier, store=root)
    partial = exhausted or status.batch.unresolved_operations or status.batch.conflicts
    if action in ("collect", "resume"):
        partial = partial or status.batch.recovery_required
    if action == "submit":
        partial = partial or status.batch.accepted_jobs != status.batch.jobs
    if (
        isinstance(session, OpenAIBatchSession)
        and session.selection_pending
        and status.collection is not None
    ):
        codes = set(status.collection.codes)
        if session.collection_budget_unfit:
            codes.add("collection_budget_unfit")
        status = status.model_copy(
            update={
                "collection": status.collection.model_copy(
                    update={
                        "selection_pending": True,
                        "codes": tuple(sorted(codes)),
                    }
                )
            }
        )
        partial = True
    outcome = (
        "error"
        if status.collection is not None
        and (status.collection.failed_sources or status.collection.prelude_failed)
        else "partial"
        if partial
        else "ok"
    )
    return status.model_copy(update={"outcome": outcome})


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
    _batch_status(identifier, root)
    with _errors(identifier, root), Repository(root) as repository:
        require_storage(repository)
        with ExecutorOwnership(repository, identifier) as ownership:
            assert ownership.owner is not None
            record_stop(repository, identifier, ownership.owner.token, remote=False)
    return _batch_status(identifier, root)


def export_batch_provenance(
    run_id: str | Path, *, store: str | Path | None = None, bundle_id: str | None = None
) -> BatchAuditReportV1 | BatchAuditReportV2:
    """Publish or reopen the normalized/provenance bundle from one audit cut."""
    from judgmentllm.execution.batch_exports import export_batch_audit

    with _errors():
        identifier, root = resolve_run(
            run_id,
            store,
            allow_v12_posix_read=bundle_id is not None,
            preserve_files=bundle_id is not None,
        )
    with _errors(identifier, root):
        return export_batch_audit(identifier, root, bundle_id=bundle_id)
