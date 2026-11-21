"""Actual restarts preserve the operation journal and inspect unread sources."""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_batch_recovery import setup
from test_native_batch import service_counts

from judgmentllm import (
    BatchControlOptionsV1,
    collect_batch,
    get_batch_status,
    submit_batch,
)
from judgmentllm.batch_models import BatchCursorV1
from judgmentllm.persistence.native_batch import fact, latest
from judgmentllm.persistence.ownership import ExecutorOwnership
from judgmentllm.persistence.repository import ReadOnlyRepository, Repository
from judgmentllm.providers.batch_mock import MockBatchAdapter
from judgmentllm.providers.protocol import PreparedRequestV2

PROCESS = """
import json, os, sys
from pathlib import Path
from judgmentllm import BatchMockOptions, cancel_batch, load_run_spec, resume_batch, submit_batch
from judgmentllm.batch_models import BatchOperationIntentV1
from judgmentllm.providers.batch_mock import MockBatchAdapter
from judgmentllm.providers.mock import MockScenario
import judgmentllm.execution.native_batch as core
mode, directory = sys.argv[1:]
directory = Path(directory)
if mode.startswith('orphan-'):
    original = core.put
    def orphan(repository, run_id, model, **options):
        artifact = original(repository, run_id, model, **options)
        if isinstance(model, BatchOperationIntentV1) and model.kind == mode[7:]:
            os._exit(73)
        return artifact
    core.put = orphan
if mode == 'changed-prefix':
    original_index = MockBatchAdapter.result_index
    async def changed_index(self, *arguments):
        commitment, raw = await original_index(self, *arguments)
        data = json.loads(raw)
        data['prefix_sha256'][1] = 'f'*64
        return commitment, core.canonical_bytes(data)
    MockBatchAdapter.result_index = changed_index
if mode == 'duplicate-conflict':
    original_read = MockBatchAdapter.read_row
    reads = 0
    async def changed(self, *arguments):
        global reads
        raw = await original_read(self, *arguments)
        if raw is not None:
            reads += 1
            # The authenticated range starts with the unread duplicate.
            if reads == 1:
                data = json.loads(raw)
                data['usage']['input_tokens'] = 99
                return json.dumps(data).encode() + b'\\n'
        return raw
    MockBatchAdapter.read_row = changed
if mode == 'trailing-error':
    original_results = MockBatchAdapter.results
    async def trailing(self, submission, remote, source, members, cursor=None):
        async for chunk in original_results(self, submission, remote, source, members, cursor):
            yield chunk
        if source == 'error':
            key, position, wire = members[0]
            recipe = self.fixture
            try:
                self.fixture = recipe.model_copy(update={'rows': (MockScenario(outcome='provider_error'),)})
                raw = await self.read_row(wire, position, key, source)
            finally:
                self.fixture = recipe
            assert raw is not None
            yield raw
    MockBatchAdapter.results = trailing
function = (cancel_batch if mode == 'orphan-cancel' else submit_batch) if mode.startswith('orphan-') else resume_batch
result = function('001', store=directory/'store', spec=load_run_spec(directory/'run.json'),
    mock=BatchMockOptions(fixture_path=directory/'batch-fixture.json'))
print(result.model_dump_json())
"""


def process(directory: Path, mode: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-B", "-c", PROCESS, mode, str(directory)],
        cwd=directory,
        env={
            **os.environ,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def cursors(root: Path) -> list[tuple[int, str]]:
    with ReadOnlyRepository(root) as repository:
        return [
            (int(row[0]), str(row[1]))
            for row in repository.connection.execute(
                "SELECT fact_seq,record_json FROM batch_facts WHERE kind='cursor' ORDER BY fact_seq"
            )
        ]


def member_evidence(root: Path) -> list[str]:
    with ReadOnlyRepository(root) as repository:
        return [
            str(row[0])
            for row in repository.connection.execute(
                "SELECT record_json FROM batch_member_receipts ORDER BY member_key"
            )
        ]


@pytest.mark.parametrize("kind", ["upload", "create", "cancel"])
def test_death_before_intent_registration_does_not_charge_or_poison_instance(
    tmp_path: Path, kind: str
) -> None:
    spec, mock = setup(tmp_path)
    root = tmp_path / "store"
    if kind == "cancel":
        submit_batch("001", store=root, spec=spec, mock=mock)
    died = process(tmp_path, "orphan-" + kind)
    assert died.returncode == 73, (died.stdout, died.stderr)
    with ReadOnlyRepository(root) as repository:
        assert (
            repository.connection.execute(
                "SELECT count(*) FROM batch_operations WHERE kind=?", (kind,)
            ).fetchone()[0]
            == 0
        )
    before = service_counts(root)["forwards"]
    assert before == {"upload": 0, "create": 1, "cancel": 2}[kind]
    recovered = process(tmp_path, "resume")
    assert recovered.returncode == 0, recovered.stderr
    with ReadOnlyRepository(root) as repository:
        operations = repository.connection.execute(
            "SELECT sequence FROM batch_operations WHERE kind=?", (kind,)
        ).fetchall()
        assert [row[0] for row in operations] == [1]
        assert repository.summary("001").attempts == 2
    assert service_counts(root)["forwards"] == (3 if kind == "cancel" else 2)
    assert process(tmp_path, "resume").returncode == 0
    assert service_counts(root)["forwards"] == (3 if kind == "cancel" else 2)


@pytest.mark.parametrize("capture", ["full", "minimal"])
@pytest.mark.parametrize("mode", ["duplicate", "duplicate-conflict", "trailing-error"])
def test_complete_members_do_not_hide_unread_suffix_on_restart(
    tmp_path: Path, capture: str, mode: str
) -> None:
    # One scientific member suffices: every outcome is proven before the suffix.
    from test_native_batch import batch_spec, mock_fixture

    spec = batch_spec(tmp_path, capture=capture, responses=1)
    spec.output_path = tmp_path / "store"
    configuration = tmp_path / "run.json"
    configuration.write_text(spec.model_dump_json())
    mock = mock_fixture(
        tmp_path,
        duplicate_results=mode != "trailing-error",
        input_tokens=7,
        output_tokens=3,
    )
    from judgmentllm import prepare_batch

    prepare_batch(configuration, store=tmp_path / "store", run_id="001", mock=mock)
    submit_batch("001", store=tmp_path / "store", mock=mock)
    first = collect_batch(
        "001",
        store=tmp_path / "store",
        mock=mock,
        options=BatchControlOptionsV1(max_result_rows=1),
    )
    assert first.batch.missing_members == 0
    assert first.outcome == "partial" and first.batch.recovery_required
    before = member_evidence(tmp_path / "store")
    with ReadOnlyRepository(tmp_path / "store") as repository:
        original_results = tuple(repository.iter_results("001"))
    recovered = process(tmp_path, mode)
    assert recovered.returncode == 0, recovered.stderr
    result = json.loads(recovered.stdout)
    assert result["batch"]["conflicts"] == (0 if mode == "duplicate" else 1)
    assert result["outcome"] == ("ok" if mode == "duplicate" else "partial")
    assert member_evidence(tmp_path / "store") == before
    with ReadOnlyRepository(tmp_path / "store") as repository:
        assert tuple(repository.iter_results("001")) == original_results
        assert repository.summary("001").attempts == 1
    receipt = json.loads(before[0])
    assert (receipt["usage"]["input_tokens"], receipt["usage"]["output_tokens"]) == (
        7,
        3,
    )
    if receipt["retained"]["relative_path"] is not None:
        from judgmentllm.persistence.artifacts import ArtifactStore
        from judgmentllm.persistence.models import ArtifactIdentity

        with ReadOnlyRepository(tmp_path / "store") as repository:
            ArtifactStore(repository.run_directory("001"), create=False).verify(
                ArtifactIdentity.model_validate(receipt["retained"])
            )
    assert service_counts(tmp_path / "store")["forwards"] == 2


@pytest.mark.parametrize("fault", ["stored-prefix", "changed-prefix"])
def test_restart_verifies_prefix_before_admitting_suffix(
    tmp_path: Path, fault: str
) -> None:
    spec, mock = setup(tmp_path, input_tokens=7, output_tokens=3)
    root = tmp_path / "store"
    submit_batch("001", store=root, spec=spec, mock=mock)
    collect_batch(
        "001",
        store=root,
        spec=spec,
        mock=mock,
        options=BatchControlOptionsV1(max_result_rows=1),
    )
    if fault == "stored-prefix":
        with (
            Repository(root) as repository,
            ExecutorOwnership(repository, "001") as ownership,
        ):
            assert ownership.owner is not None
            entity = repository.connection.execute(
                "SELECT entity_id FROM batch_facts WHERE kind='cursor' ORDER BY fact_seq DESC LIMIT 1"
            ).fetchone()[0]
            cursor = latest(repository, "001", "cursor", entity, BatchCursorV1)
            assert cursor is not None
            fact(
                repository,
                "001",
                ownership.owner.token,
                "cursor",
                entity,
                cursor.model_copy(update={"prefix_sha256": "f" * 64}),
            )
    before, evidence = cursors(root), member_evidence(root)
    rejected = process(
        tmp_path, "changed-prefix" if fault == "changed-prefix" else "resume"
    )
    assert rejected.returncode != 0
    assert cursors(root) == before
    assert member_evidence(root) == evidence
    assert get_batch_status("001", store=root).batch.recovery_required
    with ReadOnlyRepository(root) as repository:
        assert repository.summary("001").results == 1
    assert service_counts(root)["forwards"] == 2


@pytest.mark.parametrize("bound", ["steps", "bytes", "deadline"])
def test_prefix_validation_uses_invocation_bounds_without_advancing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bound: str
) -> None:
    if bound == "bytes":
        # A full-size range record plus its authenticated metadata exceeds the
        # common byte budget without exceeding the per-record bound.
        from test_native_batch import batch_spec, mock_fixture

        from judgmentllm import prepare_batch

        spec = batch_spec(tmp_path, responses=4)
        spec.output_path = tmp_path / "store"
        (tmp_path / "run.json").write_text(spec.model_dump_json())
        mock = mock_fixture(tmp_path, input_tokens=7, output_tokens=3)
        prepare_batch(
            tmp_path / "run.json", store=tmp_path / "store", run_id="001", mock=mock
        )
    else:
        spec, mock = setup(tmp_path, input_tokens=7, output_tokens=3)
    root = tmp_path / "store"
    submit_batch("001", store=root, spec=spec, mock=mock)
    collect_batch(
        "001",
        store=root,
        spec=spec,
        mock=mock,
        options=BatchControlOptionsV1(max_result_rows=3 if bound == "bytes" else 1),
    )
    before, evidence = cursors(root), member_evidence(root)
    original = MockBatchAdapter.read_row

    async def limited(
        self: MockBatchAdapter,
        prepared: PreparedRequestV2,
        position: int,
        key: str,
        source: str,
    ) -> bytes | None:
        if bound == "deadline":
            await asyncio.sleep(2)
        raw = await original(self, prepared, position, key, source)
        if raw is not None and bound == "bytes":
            return b" " * (self.options.limits.max_record_bytes - len(raw)) + raw
        return raw

    monkeypatch.setattr(MockBatchAdapter, "read_row", limited)
    result = collect_batch(
        "001",
        store=root,
        spec=spec,
        mock=mock,
        options=BatchControlOptionsV1(
            max_result_rows=1,
            max_io_steps=2 if bound == "steps" else 1024,
            deadline_seconds=0.5 if bound == "deadline" else 30,
        ),
    )
    assert result.outcome == "partial"
    assert cursors(root) == before and member_evidence(root) == evidence
    recovered = process(tmp_path, "resume")
    assert recovered.returncode == 0, recovered.stderr
    assert not get_batch_status("001", store=root).batch.recovery_required
    assert service_counts(root)["forwards"] == 2
