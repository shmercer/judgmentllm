"""Deterministic native batch preparation over the existing source freezer."""

from __future__ import annotations

import base64
from collections.abc import Callable, Iterator
from hashlib import sha256
from typing import TYPE_CHECKING

from judgmentllm.batch_models import (
    BatchContextV1,
    BatchMemberReceiptV1,
    BatchMemberV1,
    BatchShardV1,
)
from judgmentllm.models import ResponseItem
from judgmentllm.persistence.hashing import (
    bytes_digest,
    canonical_bytes,
    semantic_digest,
)
from judgmentllm.persistence.models import (
    ArtifactIdentity,
    AttemptRecord,
    IntegrityError,
    RequestRecord,
    TaskRecord,
    utc_now,
)
from judgmentllm.persistence.native_batch import put, read_reference, register, rows
from judgmentllm.planning import plan_record, stable_id
from judgmentllm.preparation.manifest import ExecutableRunManifestV12
from judgmentllm.preparation.native import _input_ids  # pyright: ignore[reportPrivateUsage]
from judgmentllm.preparation.session import _render_request, _request_dependency  # pyright: ignore[reportPrivateUsage]
from judgmentllm.providers.protocol import PreparedRequestV2

if TYPE_CHECKING:
    from judgmentllm.config import RunSpecV2
    from judgmentllm.preparation.capture import SourceBuilder
    from judgmentllm.preparation.session import PreparedRun, RequestDependency
    from judgmentllm.providers.batch_protocol import NativeBatchAdapter


def encode_row(member_key: str, wire: PreparedRequestV2) -> bytes:
    return (
        canonical_bytes(
            {
                "member_key": member_key,
                "payload": base64.b64encode(wire.payload).decode("ascii"),
            }
        )
        + b"\n"
    )


def content(builder: SourceBuilder, raw: bytes, media: str) -> ArtifactIdentity:
    identity = (
        builder.store.put(raw, media)
        if builder.capture == "full"
        else builder.store.hash_only(bytes_digest(raw), len(raw), media)
    )
    register(builder.repository, builder.run_id, identity)
    return identity


def preseal_batches(
    builder: SourceBuilder,
    spec: RunSpecV2,
    template: str | None,
    manifest: ExecutableRunManifestV12,
    adapter: NativeBatchAdapter,
    checkpoint: Callable[[str, str | None], None],
) -> tuple[int, int, str]:
    repository, run_id = builder.repository, builder.run_id
    index = sha256(b"judgmentllm.batch-index.v1\n")
    ordinal = task_ordinal = shard_ordinal = 0
    shard_data = bytearray()
    shard_members: list[BatchMemberV1] = []
    limits = manifest.batch_context.limits

    def flush() -> None:
        nonlocal shard_ordinal, shard_data, shard_members
        if not shard_members:
            return
        shard_id = stable_id("native-batch-shard", run_id, shard_ordinal)
        membership = semantic_digest(
            "batch-shard-members-v1", [m.member_key for m in shard_members]
        )
        source = content(builder, bytes(shard_data), "application/x-ndjson")
        shard = BatchShardV1(
            run_id=run_id,
            shard_id=shard_id,
            ordinal=shard_ordinal,
            member_count=len(shard_members),
            membership_sha256=membership,
            input=source,
            context=manifest.batch_context,
        )
        artifact = put(repository, run_id, shard)
        with repository.execution_transaction(run_id, builder.owner_token):
            register(repository, run_id, artifact)
            repository.connection.execute(
                "INSERT INTO batches(run_id,batch_uid,state,provider,mode,membership_sha256,created_at) VALUES (?,?,'prepared',?,'batch_native',?,?)",
                (run_id, shard_id, spec.provider, membership, utc_now()),
            )
            repository.connection.execute(
                "INSERT INTO batch_preparations VALUES (?,?,?,?,?)",
                (
                    run_id,
                    shard_id,
                    shard_ordinal,
                    artifact.sha256,
                    canonical_bytes(shard).decode(),
                ),
            )
            for offset, member in enumerate(shard_members):
                receipt = put(repository, run_id, member)
                register(repository, run_id, receipt)
                repository.connection.execute(
                    "INSERT INTO batch_members VALUES (?,?,?,?)",
                    (run_id, shard_id, member.attempt_id, offset),
                )
                repository.connection.execute(
                    "INSERT INTO batch_prepared_members VALUES (?,?,?,?,?,?,?)",
                    (
                        run_id,
                        shard_id,
                        member.member_key,
                        member.attempt_id,
                        member.position,
                        receipt.sha256,
                        canonical_bytes(member).decode(),
                    ),
                )
        index.update(canonical_bytes(shard) + b"\n")
        shard_ordinal += 1
        shard_data, shard_members = bytearray(), []

    with repository.execution_transaction(run_id, builder.owner_token):
        repository.connection.execute(
            "INSERT INTO batch_runs VALUES (?,?)",
            (run_id, canonical_bytes(manifest.batch_context).decode()),
        )
    kind = "response" if spec.task_kind == "rubric" else "pair"
    source_id = next(s.source_id for s in manifest.sources if s.role == kind)
    for record_id in _input_ids(repository, run_id, source_id, kind):
        record = builder.lookup(kind, record_id)
        for planned in plan_record(
            spec, record, template_text=template, capabilities=manifest.capabilities
        ):
            pair = planned.pair
            left = builder.lookup("response", pair.left_item_id) if pair else None
            right = builder.lookup("response", pair.right_item_id) if pair else None
            assert left is None or isinstance(left, ResponseItem)
            assert right is None or isinstance(right, ResponseItem)
            dependency = _request_dependency(manifest, planned, record, left, right)
            rendered = _render_request(
                spec,
                template,
                planned,
                builder.lookup,
                selection=manifest.output_selection,
            )
            wire = adapter.prepare(spec, dependency, rendered)
            if not isinstance(wire, PreparedRequestV2):  # pyright: ignore[reportUnnecessaryIsInstance]
                raise IntegrityError("batch compiler requires its V2 output commitment")
            attempt_id = stable_id("attempt", run_id, planned.request_uid, 1)
            key = semantic_digest(
                "batch-member-key-v1", [run_id, planned.request_uid, attempt_id]
            )
            row = encode_row(key, wire)
            if len(row) > limits.max_input_bytes:
                raise IntegrityError("one batch member exceeds the sealed input limit")
            if shard_members and (
                len(shard_members) >= limits.max_members
                or len(shard_data) + len(row) > limits.max_input_bytes
            ):
                flush()
            prompt = content(builder, rendered.text.encode("utf-8"), "text/plain")
            payload = content(builder, wire.payload, "application/json")
            member = BatchMemberV1(
                run_id=run_id,
                shard_id=stable_id("native-batch-shard", run_id, shard_ordinal),
                member_key=key,
                request_uid=planned.request_uid,
                attempt_id=attempt_id,
                position=ordinal,
                dependency_sha256=dependency.dependency_sha256,
                membership_sha256=semantic_digest(
                    "attempt-membership", list(wire.task_ids)
                ),
                prompt=prompt,
                payload=payload,
                row_sha256=bytes_digest(row),
                row_bytes=len(row),
            )
            with repository.execution_transaction(run_id, builder.owner_token):
                for task in planned.tasks:
                    repository.create_task(
                        TaskRecord(
                            run_id=run_id,
                            scheduling_ordinal=task_ordinal,
                            **task.model_dump(exclude={"schema_version"}),
                        )
                    )
                    task_ordinal += 1
                repository.create_request(
                    RequestRecord(
                        run_id=run_id,
                        request_uid=planned.request_uid,
                        scheduling_ordinal=ordinal,
                        dependency_sha256=dependency.dependency_sha256,
                    ),
                    wire.task_ids,
                )
                repository.create_attempt(
                    AttemptRecord(
                        run_id=run_id,
                        request_uid=planned.request_uid,
                        attempt_id=attempt_id,
                        attempt_number=1,
                        prompt_sha256=prompt.sha256,
                        payload_sha256=payload.sha256,
                        membership_sha256=member.membership_sha256,
                    ),
                    wire.task_ids,
                )
                repository.transition(
                    "request", run_id, planned.request_uid, "prepared"
                )
            shard_members.append(member)
            shard_data.extend(row)
            ordinal += 1
            checkpoint("after_batch_member_preparation", planned.request_uid)
    flush()
    return shard_ordinal, ordinal, index.hexdigest()


def members_for(prepared: PreparedRun, shard_id: str) -> Iterator[BatchMemberV1]:
    for row in prepared.repository.connection.execute(
        "SELECT record_json,receipt_sha256 FROM batch_prepared_members WHERE run_id=? AND shard_id=? ORDER BY position",
        (prepared.manifest.run_id, shard_id),
    ):
        member = BatchMemberV1.model_validate_json(row[0])
        if bytes_digest(canonical_bytes(member)) != row[1]:
            raise IntegrityError("batch member receipt differs from its index")
        prepared.store.verify(
            prepared.repository.get_artifact(prepared.manifest.run_id, row[1])
        )
        yield member


def reconstruct(
    prepared: PreparedRun, adapter: NativeBatchAdapter, member: BatchMemberV1
) -> tuple[RequestDependency, PreparedRequestV2]:
    dependency = prepared.dependency_for_request(member.request_uid)
    from judgmentllm.execution.retry import subset_dependency

    selected = prepared.repository.attempt_task_ids(
        prepared.manifest.run_id, member.attempt_id
    )
    dependency = subset_dependency(dependency, selected)
    wire = adapter.prepare(
        prepared.spec, dependency, prepared.render_attempt(dependency)
    )
    if not isinstance(wire, PreparedRequestV2) or (  # pyright: ignore[reportUnnecessaryIsInstance]
        wire.payload_sha256 != member.payload.sha256
        or wire.prompt_sha256 != member.prompt.sha256
        or dependency.dependency_sha256 != member.dependency_sha256
        or bytes_digest(encode_row(member.member_key, wire)) != member.row_sha256
    ):
        raise IntegrityError("batch member reconstruction differs from its seal")
    return dependency, wire


def verify_batches(prepared: PreparedRun) -> None:
    manifest = prepared.manifest
    if not isinstance(manifest, ExecutableRunManifestV12):
        return
    index = sha256(b"judgmentllm.batch-index.v1\n")
    count = shards = children = 0
    dependencies = iter(prepared.iter_request_dependencies())
    for shard in rows(
        prepared.repository, "batch_preparations", manifest.run_id, BatchShardV1
    ):
        if shard.ordinal >= manifest.batch_shard_count:
            if shard.ordinal != manifest.batch_shard_count + children:
                raise IntegrityError("retry shard order differs from authorization")
            verify_child_shard(prepared, shard, manifest)
            children += 1
            continue
        if shard.ordinal != shards:
            raise IntegrityError("batch shard order differs from seal")
        if shard.context != manifest.batch_context:
            raise IntegrityError("initial shard acquired a different context")
        prepared.store.verify(shard.input)
        members = tuple(members_for(prepared, shard.shard_id))
        if (
            len(members) != shard.member_count
            or semantic_digest(
                "batch-shard-members-v1", [m.member_key for m in members]
            )
            != shard.membership_sha256
        ):
            raise IntegrityError("batch shard membership differs from seal")
        for member in members:
            dependency = next(dependencies, None)
            if (
                dependency is None
                or dependency.request.request_uid != member.request_uid
            ):
                raise IntegrityError(
                    "batch member differs from the original request plan"
                )
            task_ids = tuple(task.task_id for task in dependency.request.tasks)
            if (
                prepared.repository.request_task_ids(
                    manifest.run_id, member.request_uid
                )
                != task_ids
                or prepared.repository.attempt_task_ids(
                    manifest.run_id, member.attempt_id
                )
                != task_ids
                or member.dependency_sha256 != dependency.dependency_sha256
                or member.member_key
                != semantic_digest(
                    "batch-member-key-v1",
                    [manifest.run_id, member.request_uid, member.attempt_id],
                )
            ):
                raise IntegrityError(
                    "batch member task crosswalk differs from the source plan"
                )
            if member.position != count:
                raise IntegrityError("batch member order differs from seal")
            attempt = prepared.repository.get_attempt(
                manifest.run_id, member.attempt_id
            )
            if (
                attempt.payload_sha256,
                attempt.prompt_sha256,
                attempt.membership_sha256,
            ) != (
                member.payload.sha256,
                member.prompt.sha256,
                member.membership_sha256,
            ):
                raise IntegrityError("batch attempt differs from sealed member")
            prepared.store.verify(member.prompt)
            prepared.store.verify(member.payload)
            count += 1
        index.update(canonical_bytes(shard) + b"\n")
        shards += 1
    if next(dependencies, None) is not None or (shards, count, index.hexdigest()) != (
        manifest.batch_shard_count,
        manifest.batch_member_count,
        manifest.batch_index_sha256,
    ):
        raise IntegrityError("batch preparation index differs from manifest")


def verify_child_shard(
    prepared: PreparedRun, shard: BatchShardV1, manifest: ExecutableRunManifestV12
) -> None:
    from judgmentllm.execution.retry import subset_dependency

    members = tuple(members_for(prepared, shard.shard_id))
    if shard.member_count != 1 or len(members) != 1:
        raise IntegrityError("retry shard must bind its one authorized attempt")
    member = members[0]
    attempt = prepared.repository.get_attempt(manifest.run_id, member.attempt_id)
    if attempt.attempt_number <= 1 or attempt.authorization_id is None:
        raise IntegrityError("retry shard lacks scientific authorization")
    reference = f"batch/retry-contexts/{bytes_digest(attempt.authorization_id.encode('utf-8'))}.json"
    context = read_reference(
        prepared.repository, manifest.run_id, reference, BatchContextV1
    )
    if context != shard.context or (
        shard.context.service_id,
        shard.context.protocol,
        shard.context.limits,
    ) != (
        manifest.batch_context.service_id,
        manifest.batch_context.protocol,
        manifest.batch_context.limits,
    ):
        raise IntegrityError("retry shard context differs from its authorization")
    tasks = prepared.repository.attempt_task_ids(manifest.run_id, member.attempt_id)
    dependency = subset_dependency(
        prepared.dependency_for_request(member.request_uid), tasks
    )
    initial = prepared.repository.get_attempt_for_request(
        manifest.run_id, member.request_uid, attempt_number=1
    )
    if initial is None or member.parent_receipt_sha256 is None:
        raise IntegrityError("retry shard lacks its original proven member")
    artifact = prepared.repository.get_artifact(
        manifest.run_id, member.parent_receipt_sha256
    )
    parent = BatchMemberReceiptV1.model_validate_json(prepared.store.read(artifact))
    if (
        parent.identity.run_id,
        parent.identity.request_uid,
        parent.identity.attempt_id,
    ) != (
        manifest.run_id,
        member.request_uid,
        initial.attempt_id,
    ) or not prepared.repository.connection.execute(
        "SELECT 1 FROM batch_member_receipts WHERE run_id=? AND attempt_id=? AND receipt_sha256=?",
        (manifest.run_id, initial.attempt_id, member.parent_receipt_sha256),
    ).fetchone():
        raise IntegrityError("retry parent receipt differs from original evidence")
    if (
        member.member_key
        != semantic_digest(
            "batch-member-key-v1",
            [manifest.run_id, member.request_uid, member.attempt_id],
        )
        or member.shard_id
        != stable_id(
            "native-batch-retry-shard", manifest.run_id, attempt.authorization_id
        )
        or member.position
        != manifest.batch_member_count + shard.ordinal - manifest.batch_shard_count
        or member.dependency_sha256 != dependency.dependency_sha256
        or member.membership_sha256
        != semantic_digest("attempt-membership", list(tasks))
        or shard.membership_sha256
        != semantic_digest("batch-shard-members-v1", [member.member_key])
        or (member.payload.sha256, member.prompt.sha256, member.membership_sha256)
        != (attempt.payload_sha256, attempt.prompt_sha256, attempt.membership_sha256)
    ):
        raise IntegrityError("retry member differs from its authorized crosswalk")
    for identity in (shard.input, member.prompt, member.payload):
        prepared.store.verify(identity)
