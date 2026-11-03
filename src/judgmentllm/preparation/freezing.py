"""File-generation checks and bounded hashing for immutable preparation."""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO

from judgmentllm.persistence.models import ExecutionModel, IntegrityError

CHUNK_SIZE = 1024 * 1024


class PreparationError(IntegrityError):
    """Safe preparation diagnostic, deliberately excluding source content."""


class FileSignature(ExecutionModel):
    """Available descriptor/path generation metadata, never a content identity."""

    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int

    @classmethod
    def from_stat(cls, value: os.stat_result) -> FileSignature:
        if not stat.S_ISREG(value.st_mode):
            raise PreparationError("source must be a regular file")
        return cls(
            device=value.st_dev,
            inode=value.st_ino,
            size=value.st_size,
            mtime_ns=value.st_mtime_ns,
            ctime_ns=value.st_ctime_ns,
        )


def signature(path: Path, stream: BinaryIO | None = None) -> FileSignature:
    """Require a stable path identity and, when supplied, matching open handle."""
    try:
        current = FileSignature.from_stat(path.stat())
        if stream is not None and current != FileSignature.from_stat(
            os.fstat(stream.fileno())
        ):
            raise PreparationError("source path and open descriptor differ")
        return current
    except OSError:
        raise PreparationError("required source is missing or unreadable") from None


def require_signature(
    path: Path, expected: FileSignature, stream: BinaryIO | None = None
) -> None:
    if signature(path, stream) != expected:
        raise PreparationError("source generation changed; start a new preparation")


def chunks(stream: BinaryIO) -> Iterator[bytes]:
    while block := stream.read(CHUNK_SIZE):
        yield block


def hash_stream(stream: BinaryIO) -> tuple[str, int]:
    digest = hashlib.sha256()
    length = 0
    for block in chunks(stream):
        digest.update(block)
        length += len(block)
    return digest.hexdigest(), length


def verify_original(
    path: Path, expected: FileSignature, sha256: str, byte_length: int
) -> None:
    """Independently reopen/hash bytes between descriptor/path generation checks."""
    try:
        with path.open("rb") as stream:
            require_signature(path, expected, stream)
            actual = hash_stream(stream)
            require_signature(path, expected, stream)
    except OSError:
        raise PreparationError("required source is missing or unreadable") from None
    if actual != (sha256, byte_length):
        raise PreparationError("source content differs from its frozen identity")


class HashingReader:
    """Read-only scan facade; hash exactly the bytes consumed by the indexer."""

    def __init__(self, stream: BinaryIO) -> None:
        self.stream = stream
        self.digest = hashlib.sha256()
        self.length = 0

    def read(self, size: int = CHUNK_SIZE) -> bytes:
        if size < 0:
            raise PreparationError("source scans must use bounded reads")
        block = self.stream.read(size)
        self.digest.update(block)
        self.length += len(block)
        return block

    def tell(self) -> int:
        return self.stream.tell()

    def seek(self, offset: int, whence: int = 0) -> int:
        if offset != 0 or whence != 0 or self.length:
            raise PreparationError("hashing scan cannot revisit consumed bytes")
        return self.stream.seek(0)
