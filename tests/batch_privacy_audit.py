"""Independent, content-hiding privacy inspection for synthetic batch tests."""

import errno
import hashlib
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TypeVar

import pytest
from test_execution import read_live_runtime_file

from judgmentllm.batch_models import BatchOperationIntentV1, BatchOperationReceiptV1
from judgmentllm.evidence import ReportedUsageInput
from judgmentllm.persistence.repository import ReadOnlyRepository
from judgmentllm.providers.batch_mock import MockBatchAdapter
from judgmentllm.providers.protocol import PreparedRequestV2, ProviderResponse

T = TypeVar("T")
MARKERS = (
    b"PROHIBITED_BATCH_CONTROL_SECRET",
    b"PROHIBITED_BATCH_RESULT_SECRET",
    b"PROHIBITED_BATCH_MEMBER_SECRET",
    b"PROHIBITED_BATCH_MARKER_SECRET",
)


class PrivacyAudit:
    def __init__(self, root: Path):
        self.root = root
        with ReadOnlyRepository(root) as repository:
            self.executor_lock = repository.run_directory("001") / "executor.lock"
            self.sqlite_shm = repository.root / "registry.sqlite3-shm"
        self.deferred: set[Path] = set()
        self.mapped: set[Path] = set()
        self._control_active = False
        self.originals: list[bytes] = []
        self.failures: list[BaseException] = []
        self.checkpoints: list[str] = []
        self.reads: list[tuple[str, int]] = []
        self.sources: list[str] = []

    def observe(self, raw: bytes) -> None:
        if any(marker in raw for marker in MARKERS):
            self.originals.append(raw)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        screen = MockBatchAdapter.screen_result
        decode = MockBatchAdapter.decode_receipt
        read = MockBatchAdapter.read_row
        source = MockBatchAdapter.result_source
        audit = self

        def screened(
            adapter: MockBatchAdapter, raw: bytes, keys: Mapping[str, PreparedRequestV2]
        ) -> tuple[str, ProviderResponse, ReportedUsageInput, bytes, bool] | None:
            audit.observe(raw)
            return screen(adapter, raw, keys)

        def decoded(
            adapter: MockBatchAdapter, raw: bytes, intent: BatchOperationIntentV1
        ) -> BatchOperationReceiptV1:
            audit.observe(raw)
            return decode(adapter, raw, intent)

        async def read_row(
            adapter: MockBatchAdapter,
            wire: PreparedRequestV2,
            position: int,
            key: str,
            kind: str,
        ) -> bytes | None:
            audit.reads.append((kind, position))
            return await read(adapter, wire, position, key, kind)

        async def result_source(
            adapter: MockBatchAdapter, submission: str, kind: str
        ) -> str:
            audit.sources.append(kind)
            return await source(adapter, submission, kind)

        monkeypatch.setattr(MockBatchAdapter, "screen_result", screened)
        monkeypatch.setattr(MockBatchAdapter, "decode_receipt", decoded)
        monkeypatch.setattr(MockBatchAdapter, "read_row", read_row)
        monkeypatch.setattr(MockBatchAdapter, "result_source", result_source)

    def scan(self) -> None:
        assert self.executor_lock.is_file() and not self.executor_lock.is_symlink()
        prohibited = (
            *MARKERS,
            *(hashlib.sha256(m).hexdigest().encode() for m in MARKERS),
            *(hashlib.sha256(body).hexdigest().encode() for body in self.originals),
        )
        for path in self.root.rglob("*"):
            if path.is_file():
                raw = read_live_runtime_file(
                    path,
                    root=self.root,
                    executor_lock=self.executor_lock,
                    deferred=self.deferred,
                    windows_locking=sys.platform == "win32" and self._control_active,
                    sqlite_shm=self.sqlite_shm,
                    mapped=self.mapped,
                )
                if raw is None:
                    assert sys.platform == "win32" and self._control_active
                    import msvcrt

                    # Probe the pinned byte without opening SQLite or changing it.
                    with self.executor_lock.open("r+b", buffering=0) as stream:
                        try:
                            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                        except OSError as error:
                            assert error.errno in {
                                errno.EACCES,
                                errno.EAGAIN,
                                errno.EDEADLK,
                            }, "unexpected executor lock probe failure"
                        else:
                            stream.seek(0)
                            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                            raise AssertionError("deferred executor lock is not held")
                    continue
                if path == self.executor_lock:
                    assert not path.is_symlink() and raw == b"\0"
                assert all(value not in raw for value in prohibited), (
                    "prohibited batch evidence or original digest was persisted"
                )

    def inspect(self, stage: str, identity: str) -> None:
        self.checkpoints.append(stage)
        try:
            self.scan()
        except BaseException as error:
            self.failures.append(error)
            raise

    def checked(self, operation: Callable[[], T]) -> T:
        assert not self._control_active
        self._control_active = True
        try:
            return operation()
        finally:
            self._control_active = False
            try:
                assert not self.executor_lock.is_symlink()
                assert self.executor_lock.read_bytes() == b"\0"
                self.inspect("after_control_release", "")
            finally:
                if self.failures:
                    raise self.failures[0]
