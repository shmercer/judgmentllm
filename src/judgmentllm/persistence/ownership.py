"""Single-machine executor ownership: OS locks are authoritative, never leases.

The lock file is permanent. Removing or replacing it would allow different
processes to lock different inodes for the same run. SQLite metadata describes
an owner but cannot grant permission to replace a live OS lock.

Descriptors are noninheritable across exec. A fork-only child can retain the
inherited lock until it closes its handle or exits; this conservatively delays
takeover rather than authorizing concurrent executors. Child methods must never
unlock the parent's shared open description.
"""

from __future__ import annotations

import errno
import os
import socket
import stat
import sys
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO

from judgmentllm.persistence.models import OwnerInfo, OwnershipError, utc_now

if TYPE_CHECKING:
    from judgmentllm.persistence.repository import Repository


class ExecutorOwnership:
    """Hold a nonblocking advisory lock for one run until explicit release.

    Use one instance per executor lifetime. A heartbeat is diagnostic only;
    neither age nor a PID lookup permits takeover. Acquiring the OS lock allows
    the repository to replace stale metadata left by a dead process.
    """

    def __init__(self, repository: Repository, run_id: str) -> None:
        self.repository = repository
        self.run_id = run_id
        self.owner: OwnerInfo | None = None
        self._file: BinaryIO | None = None
        self._pid: int | None = None

    def acquire(self) -> OwnerInfo:
        """Acquire without waiting, then commit ownership diagnostics."""
        if self._file is not None:
            raise OwnershipError("executor ownership is already acquired")
        self.repository.get_run(self.run_id)
        path = self._lock_path()
        stream = self._open_file(path)
        try:
            _lock(stream)
        except OSError as exc:
            stream.close()
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise OwnershipError("another executor owns this run") from None
            raise OwnershipError("executor lock could not be acquired") from None
        except BaseException:
            stream.close()
            raise
        try:
            now = utc_now()
            owner = OwnerInfo(
                token=uuid.uuid4().hex,
                pid=os.getpid(),
                host=socket.gethostname(),
                acquired_at=now,
                heartbeat_at=now,
            )
            self.repository.set_owner(self.run_id, owner)
        except BaseException:
            try:
                _unlock(stream)
            finally:
                stream.close()
            raise
        self._file = stream
        self._pid = os.getpid()
        self.owner = owner
        return owner

    def heartbeat(self) -> None:
        """Refresh diagnostics while retaining the same owner token and lock."""
        self._require_owner()
        assert self.owner is not None
        self.repository.heartbeat_owner(self.run_id, self.owner.token)
        self.owner = self.repository.get_owner(self.run_id)

    def release(self) -> None:
        """Clear this token's metadata before releasing its OS lock.

        A database failure still releases the lock; the next executor can repair
        the stale diagnostics after acquiring it. Never unlink the lock file.
        """
        if self._file is None:
            return
        if self._pid != os.getpid():
            # A fork inherits the open description. Close only the child's copy;
            # flock(LOCK_UN) here would release the parent's lock too.
            self._file.close()
            self._file = None
            self.owner = None
            raise OwnershipError("executor ownership belongs to another process")
        stream = self._file
        try:
            assert self.owner is not None
            self.repository.clear_owner(self.run_id, self.owner.token)
        finally:
            self._file = None
            self.owner = None
            self._pid = None
            try:
                _unlock(stream)
            finally:
                stream.close()

    def _require_owner(self) -> None:
        if self._file is None or self.owner is None:
            raise OwnershipError("executor ownership is not acquired")
        if self._pid != os.getpid():
            raise OwnershipError("executor ownership belongs to another process")

    def _lock_path(self) -> Path:
        directory = self.repository.run_directory(self.run_id)
        root = self.repository.root.resolve()
        try:
            relative = directory.relative_to(root)
        except ValueError:
            raise OwnershipError(
                "executor lock location is outside the store"
            ) from None
        current = root
        for part in relative.parts:
            current /= part
            if current.is_symlink():
                raise OwnershipError("executor lock location must not be a symlink")
            try:
                current.mkdir(exist_ok=True)
            except OSError:
                raise OwnershipError("executor lock directory is unavailable") from None
        return directory / "executor.lock"

    @staticmethod
    def _open_file(path: Path) -> BinaryIO:
        try:
            if path.is_symlink():
                raise OwnershipError("executor lock file must not be a symlink")
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(path, flags, 0o600)
            try:
                os.set_inheritable(fd, False)
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode):
                    raise OwnershipError("executor lock file must be a regular file")
                # msvcrt.locking requires an existing byte range. Initializing
                # before locking does not replace the permanent file identity.
                if info.st_size == 0:
                    os.write(fd, b"\0")
                stream = os.fdopen(fd, "r+b", buffering=0)
            except BaseException:
                os.close(fd)
                raise
            return stream
        except OSError:
            raise OwnershipError("executor lock file is unavailable") from None

    def __enter__(self) -> ExecutorOwnership:
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.release()


def _lock(stream: BinaryIO) -> None:
    stream.seek(0)
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(stream: BinaryIO) -> None:
    stream.seek(0)
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def executor_is_active(repository: Repository, run_id: str) -> bool | None:
    """Probe the existing OS lock without modifying ownership or creating files.

    The answer describes the instant of the probe, not a transferable lease.
    Stale heartbeat/PID metadata never determines executor presence.
    """
    directory = repository.run_directory(run_id)
    root = repository.root.resolve()
    try:
        relative = directory.relative_to(root)
    except ValueError:
        raise OwnershipError("executor lock location is outside the store") from None
    current = root
    for part in (*relative.parts, "executor.lock"):
        current /= part
        if current.is_symlink():
            raise OwnershipError("executor lock location must not be a symlink")
    # Match executor path checks, but inspection never creates directories or
    # initializes a lock file for a run whose preparation is still incomplete.
    path = directory / "executor.lock"
    try:
        stream = path.open("r+b", buffering=0)
    except FileNotFoundError:
        return False
    except OSError:
        return None
    try:
        try:
            _lock(stream)
        except OSError as error:
            if error.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                return True
            raise OwnershipError("executor lock could not be inspected") from None
        _unlock(stream)
        return False
    finally:
        stream.close()
