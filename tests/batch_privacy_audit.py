"""Independent, content-hiding privacy inspection for synthetic batch tests."""

import hashlib
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TypeVar

import pytest

from judgmentllm.batch_models import BatchOperationIntentV1, BatchOperationReceiptV1
from judgmentllm.evidence import ReportedUsageInput
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
        prohibited = (
            *MARKERS,
            *(hashlib.sha256(m).hexdigest().encode() for m in MARKERS),
            *(hashlib.sha256(body).hexdigest().encode() for body in self.originals),
        )
        for path in self.root.rglob("*"):
            if path.is_file():
                raw = path.read_bytes()
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
        try:
            return operation()
        finally:
            if self.failures:
                raise self.failures[0]
