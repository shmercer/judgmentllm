"""Immutable content-addressed byte storage; no source or prompt ingestion."""

from __future__ import annotations

import errno
import hashlib
import os
import tempfile
from pathlib import Path

# Shared M1 validation also rejects mutations of frozen model copies.
from judgmentllm.io import _revalidate_record  # pyright: ignore[reportPrivateUsage]
from judgmentllm.persistence.hashing import bytes_digest
from judgmentllm.persistence.models import (
    ArtifactIdentity,
    ArtifactIntegrityError,
    PersistenceError,
)


def _fsync_directory(path: Path) -> None:
    """Flush directory entries on platforms supporting directory descriptors."""
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        try:
            os.fsync(descriptor)
        except OSError as error:
            if error.errno not in (errno.EINVAL, errno.ENOTSUP):
                raise
    finally:
        os.close(descriptor)


class ArtifactStore:
    """Retain exact bytes by digest without replacing published objects.

    A successful publication is flushed before its identity is returned for a
    later SQLite transaction. A process death may leave an unreferenced object
    or temporary file, never a deliberately overwritten earlier object.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        try:
            missing: list[Path] = []
            parent = self.root
            while not parent.exists():
                missing.append(parent)
                parent = parent.parent
            self.root.mkdir(parents=True, exist_ok=True)
            for created in reversed(missing):
                _fsync_directory(created.parent)
        except OSError:
            raise PersistenceError("artifact store could not be opened") from None

    @staticmethod
    def hash_only(
        sha256: str,
        byte_length: int,
        media_type: str = "application/octet-stream",
    ) -> ArtifactIdentity:
        """Describe intentionally unretained bytes without creating a file."""
        return ArtifactIdentity(
            sha256=sha256, byte_length=byte_length, media_type=media_type
        )

    def _path(self, identity: ArtifactIdentity) -> Path | None:
        try:
            current = _revalidate_record(identity)
        except ValueError:
            raise ArtifactIntegrityError("artifact identity is invalid") from None
        if current.relative_path is None:
            return None
        expected = f"objects/{current.sha256[:2]}/{current.sha256}"
        if current.relative_path != expected:
            raise ArtifactIntegrityError("artifact location does not match its digest")
        candidate = self.root / expected
        if candidate.is_symlink() or not candidate.resolve().is_relative_to(self.root):
            raise ArtifactIntegrityError("artifact location is outside its store")
        return candidate

    def verify(self, identity: ArtifactIdentity) -> Path | None:
        """Verify retained content; hash-only identities intentionally return None."""
        path = self._path(identity)
        if path is None:
            return None
        try:
            digest = hashlib.sha256()
            length = 0
            with path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    length += len(chunk)
                    digest.update(chunk)
        except OSError:
            raise ArtifactIntegrityError(
                "retained artifact is missing or unreadable"
            ) from None
        if digest.hexdigest() != identity.sha256 or length != identity.byte_length:
            raise ArtifactIntegrityError(
                "retained artifact failed content verification"
            )
        return path

    def read(self, identity: ArtifactIdentity) -> bytes:
        """Read verified retained bytes; hash-only content cannot be reconstructed."""
        path = self.verify(identity)
        if path is None:
            raise ArtifactIntegrityError(
                "artifact bytes were intentionally not retained"
            )
        try:
            data = path.read_bytes()
        except OSError:
            raise ArtifactIntegrityError(
                "retained artifact is missing or unreadable"
            ) from None
        # Protect the returned content against a change between verification and
        # the second open. A digest is never evidence for absent original bytes.
        if len(data) != identity.byte_length or bytes_digest(data) != identity.sha256:
            raise ArtifactIntegrityError("retained artifact changed while being read")
        return data

    def put(
        self, data: bytes, media_type: str = "application/octet-stream"
    ) -> ArtifactIdentity:
        """Flush, verify and atomically publish bytes without clobbering a target."""
        digest = bytes_digest(data)
        identity = ArtifactIdentity(
            sha256=digest,
            byte_length=len(data),
            media_type=media_type,
            relative_path=f"objects/{digest[:2]}/{digest}",
        )
        destination = self._path(identity)
        assert destination is not None
        temporary: Path | None = None
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            # Validate again after mkdir in case a preexisting directory redirects
            # the object tree. The selected root itself is resolved on opening.
            self._path(identity)
            _fsync_directory(destination.parent.parent)
            _fsync_directory(self.root)
            descriptor, name = tempfile.mkstemp(
                prefix=".pending-", dir=destination.parent
            )
            temporary = Path(name)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            if bytes_digest(temporary.read_bytes()) != digest:
                raise ArtifactIntegrityError(
                    "temporary artifact failed content verification"
                )
            try:
                # Same-filesystem hard-link publication is atomic and fails when
                # the destination exists, unlike replacement-based renaming.
                os.link(temporary, destination)
            except FileExistsError:
                self.verify(identity)
            _fsync_directory(destination.parent)
            return identity
        except ArtifactIntegrityError:
            raise
        except OSError:
            raise PersistenceError("artifact could not be durably published") from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    raise PersistenceError(
                        "temporary artifact could not be removed"
                    ) from None
