"""Immutable content-addressed byte storage; no source or prompt ingestion."""

from __future__ import annotations

import errno
import hashlib
import os
import tempfile
from collections.abc import Generator, Iterable
from contextlib import contextmanager
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


def _file_digest(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    length = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            length += len(chunk)
    return digest.hexdigest(), length


class StagedArtifact:
    """Private verified bytes; publication remains explicit until context exit."""

    def __init__(self, store: ArtifactStore, path: Path, identity: ArtifactIdentity):
        self.store = store
        self.path = path
        self.identity = identity

    def verify(self) -> Path:
        """Reread the exact staging bytes without materializing the file."""
        try:
            if _file_digest(self.path) != (
                self.identity.sha256,
                self.identity.byte_length,
            ):
                raise ArtifactIntegrityError(
                    "temporary artifact failed content verification"
                )
        except OSError:
            raise ArtifactIntegrityError("temporary artifact is unavailable") from None
        return self.path

    def publish(self) -> ArtifactIdentity:
        """Link a verified staging file into its immutable object location."""
        self.verify()
        destination = self.store._path(self.identity)  # pyright: ignore[reportPrivateUsage]
        assert destination is not None
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            self.store._path(self.identity)  # pyright: ignore[reportPrivateUsage]
            _fsync_directory(destination.parent.parent)
            _fsync_directory(self.store.root)
            try:
                os.link(self.path, destination)
            except FileExistsError:
                self.store.verify(self.identity)
            _fsync_directory(destination.parent)
            self.store.verify(self.identity)
            return self.identity
        except OSError:
            raise PersistenceError("artifact could not be durably published") from None


class ArtifactStore:
    """Retain exact bytes by digest without replacing published objects.

    A successful publication is flushed before its identity is returned for a
    later SQLite transaction. A process death may leave an unreferenced object
    or temporary file, never a deliberately overwritten earlier object.
    """

    def __init__(self, root: str | Path, *, create: bool = True) -> None:
        self.root = Path(root).resolve()
        if not create:
            try:
                if not self.root.is_dir():
                    raise OSError
                self._root_identity = self.root.stat()
            except OSError:
                raise PersistenceError("artifact store could not be opened") from None
            return
        try:
            missing: list[Path] = []
            parent = self.root
            while not parent.exists():
                missing.append(parent)
                parent = parent.parent
            self.root.mkdir(parents=True, exist_ok=True)
            for created in reversed(missing):
                _fsync_directory(created.parent)
            self._root_identity = self.root.stat()
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
        # The leaf is a validated digest. Check its directory without depending
        # on final-name spelling while other publishers create hard links.
        if candidate.is_symlink() or not self._contains(candidate.parent):
            raise ArtifactIntegrityError("artifact location is outside its store")
        return candidate

    def _contains(self, parent: Path) -> bool:
        """Accept equivalent directory spellings, never a replaced store root."""
        try:
            if not os.path.samestat(self.root.stat(), self._root_identity):
                return False
            resolved = parent.resolve()
            if resolved.is_relative_to(self.root):
                return True
            # Windows can spell the same directory with different path prefixes.
            # Uncreated components are expected before first publication.
            for ancestor in (resolved, *resolved.parents):
                try:
                    if os.path.samestat(ancestor.stat(), self._root_identity):
                        return True
                except FileNotFoundError:
                    continue
        except OSError:
            return False
        return False

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

    @contextmanager
    def staged_stream(
        self,
        chunks: Iterable[bytes],
        media_type: str = "application/octet-stream",
        *,
        expected_sha256: str | None = None,
        expected_length: int | None = None,
    ) -> Generator[StagedArtifact]:
        """Hash and stage bounded writes without publishing any content.

        The context removes only its own temporary file. Crash leftovers remain
        inert, unreferenced private files; they are never promoted by discovery.
        No source text is staged by privacy-minimal preparation.
        """
        temporary: Path | None = None
        try:
            if not self._contains(self.root):
                raise ArtifactIntegrityError("artifact location is outside its store")
            descriptor, name = tempfile.mkstemp(prefix=".pending-", dir=self.root)
            temporary = Path(name)
            digest = hashlib.sha256()
            length = 0
            with os.fdopen(descriptor, "wb") as stream:
                for chunk in chunks:
                    if type(chunk) is not bytes:
                        raise TypeError("artifact chunks must be bytes")
                    digest.update(chunk)
                    length += len(chunk)
                    view = memoryview(chunk)
                    for start in range(0, len(view), 1024 * 1024):
                        stream.write(view[start : start + 1024 * 1024])
                stream.flush()
                os.fsync(stream.fileno())
            sha256 = digest.hexdigest()
            if (expected_sha256 is not None and sha256 != expected_sha256) or (
                expected_length is not None and length != expected_length
            ):
                raise ArtifactIntegrityError(
                    "streamed artifact differs from expected identity"
                )
            identity = ArtifactIdentity(
                sha256=sha256,
                byte_length=length,
                media_type=media_type,
                relative_path=f"objects/{sha256[:2]}/{sha256}",
            )
            staged = StagedArtifact(self, temporary, identity)
            staged.verify()
            yield staged
        except OSError:
            raise PersistenceError("artifact could not be durably staged") from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    raise PersistenceError(
                        "temporary artifact could not be removed"
                    ) from None

    def put_stream(
        self,
        chunks: Iterable[bytes],
        media_type: str = "application/octet-stream",
        *,
        expected_sha256: str | None = None,
        expected_length: int | None = None,
    ) -> ArtifactIdentity:
        """Stage, verify and atomically publish a stream of exact bytes."""
        with self.staged_stream(
            chunks,
            media_type,
            expected_sha256=expected_sha256,
            expected_length=expected_length,
        ) as staged:
            return staged.publish()

    def put(
        self, data: bytes, media_type: str = "application/octet-stream"
    ) -> ArtifactIdentity:
        """Publish existing in-memory bytes using the same streaming protocol."""
        if type(data) is not bytes:
            raise TypeError("exact byte storage requires bytes")
        return self.put_stream((data,), media_type)

    def publish_manifest(self, identity: ArtifactIdentity) -> Path:
        """Publish the fixed manifest name without replacing an earlier file."""
        source = self.verify(identity)
        if source is None:
            raise ArtifactIntegrityError("manifest bytes must be retained")
        target = self.root / "manifest.json"
        try:
            if target.is_symlink():
                raise ArtifactIntegrityError("fixed manifest must not be a symlink")
            try:
                os.link(source, target)
            except FileExistsError:
                if _file_digest(target) != (identity.sha256, identity.byte_length):
                    raise ArtifactIntegrityError(
                        "fixed manifest already has different content"
                    ) from None
            _fsync_directory(self.root)
            if _file_digest(target) != (identity.sha256, identity.byte_length):
                raise ArtifactIntegrityError(
                    "fixed manifest failed content verification"
                )
        except OSError:
            raise PersistenceError(
                "fixed manifest could not be durably published"
            ) from None
        return target

    def reference_path(self, relative_path: str) -> Path:
        """Resolve a safe immutable metadata reference inside this store.

        Named references identify durable receipts before SQLite binds their
        objects. They never permit symlink indirection, even within the store.
        """
        try:
            ArtifactIdentity.safe_path(relative_path)
        except ValueError:
            raise ArtifactIntegrityError("reference location is invalid") from None
        target = self.root / relative_path
        if not self._contains(target.parent):
            raise ArtifactIntegrityError("reference location is outside its store")
        current = target
        while current != self.root:
            if current.is_symlink():
                raise ArtifactIntegrityError("reference location must not be a symlink")
            current = current.parent
        return target

    def publish_reference(self, identity: ArtifactIdentity, relative_path: str) -> Path:
        """Atomically publish a named verified object without replacing evidence."""
        source = self.verify(identity)
        if source is None:
            raise ArtifactIntegrityError("reference requires retained metadata bytes")
        target = self.reference_path(relative_path)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target = self.reference_path(relative_path)
            _fsync_directory(target.parent.parent)
            try:
                os.link(source, target)
            except FileExistsError:
                if _file_digest(target) != (identity.sha256, identity.byte_length):
                    raise ArtifactIntegrityError(
                        "reference already has conflicting content"
                    ) from None
            _fsync_directory(target.parent)
            if _file_digest(target) != (identity.sha256, identity.byte_length):
                raise ArtifactIntegrityError("reference failed content verification")
        except OSError:
            raise PersistenceError("reference could not be durably published") from None
        return target
