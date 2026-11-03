"""File-generation checks and bounded hashing for immutable preparation."""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Callable, Iterator
from functools import lru_cache
from pathlib import Path
from typing import BinaryIO

from judgmentllm.persistence.models import ExecutionModel, IntegrityError

CHUNK_SIZE = 1024 * 1024
_WINDOWS = os.name == "nt"


class PreparationError(IntegrityError):
    """Safe preparation diagnostic, deliberately excluding source content."""


class FileSignature(ExecutionModel):
    """File identity/generation metadata, never an exact content identity.

    ``ctime_ns`` means metadata change time, including on Windows. It does not
    inherit the different creation/change-time meanings of Windows stat/fstat.
    """

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


@lru_cache(maxsize=1)
def _windows_change_time_reader() -> Callable[[int], int]:
    """Bind the stdlib-only Win32 metadata query once, without reading content."""
    import ctypes
    import msvcrt

    class FileBasicInfo(ctypes.Structure):
        _fields_ = [
            ("CreationTime", ctypes.c_int64),
            ("LastAccessTime", ctypes.c_int64),
            ("LastWriteTime", ctypes.c_int64),
            ("ChangeTime", ctypes.c_int64),
            ("FileAttributes", ctypes.c_uint32),
        ]

    # These attributes exist only on Windows; late binding keeps Unix imports
    # independent from Windows DLL/CRT availability.
    kernel = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)  # noqa: B009
    query = kernel.GetFileInformationByHandleEx
    query.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    query.restype = ctypes.c_int

    def read_change_time(descriptor: int) -> int:
        info = FileBasicInfo()
        handle = int(getattr(msvcrt, "get_osfhandle")(descriptor))  # noqa: B009
        # FileBasicInfo=0; Windows timestamps count 100ns from 1601-01-01.
        if not query(handle, 0, ctypes.byref(info), ctypes.sizeof(info)):
            raise PreparationError("source change-time metadata is unavailable")
        change_time = int(info.ChangeTime)
        if change_time <= 0:
            raise PreparationError("source filesystem has no usable change time")
        return (change_time - 116444736000000000) * 100

    return read_change_time


def _windows_signature(stream: BinaryIO) -> FileSignature:
    """Read stable descriptor identity plus native metadata change time."""
    current = FileSignature.from_stat(os.fstat(stream.fileno()))
    return current.model_copy(
        update={"ctime_ns": _windows_change_time_reader()(stream.fileno())}
    )


def signature(path: Path, stream: BinaryIO | None = None) -> FileSignature:
    """Require a stable path identity and, when supplied, matching open handle."""
    try:
        if _WINDOWS:
            # CPython 3.11 path stat may fall back to directory metadata while
            # a file is open, losing its device/inode. On newer CPython, path
            # stat's ctime is birthtime while fstat's can be ChangeTime. Query
            # an independent path handle through the same descriptor boundary.
            with path.open("rb") as probe:
                current = _windows_signature(probe)
                if stream is not None and current != _windows_signature(stream):
                    raise PreparationError("source path and open descriptor differ")
                if current != _windows_signature(probe):
                    raise PreparationError(
                        "source generation changed during verification"
                    )
            return current
        current = FileSignature.from_stat(path.stat())
        if stream is not None and current != FileSignature.from_stat(
            os.fstat(stream.fileno())
        ):
            raise PreparationError("source path and open descriptor differ")
        return current
    except (OSError, ValueError):
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
