"""Exact retained bytes are immutable and independently verifiable."""

import errno
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from judgmentllm.persistence import artifacts
from judgmentllm.persistence.artifacts import ArtifactStore
from judgmentllm.persistence.hashing import bytes_digest
from judgmentllm.persistence.models import ArtifactIntegrityError, PersistenceError


def test_publish_reopen_read_and_reuse_exact_bytes(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    data = "Synthetic é\r\n".encode() + b"\x00\xff"
    identity = store.put(data, "application/octet-stream")
    assert identity.sha256 == bytes_digest(data)
    assert identity.byte_length == len(data)
    assert identity.relative_path == f"objects/{identity.sha256[:2]}/{identity.sha256}"
    path = store.verify(identity)
    assert path is not None
    assert path.read_bytes() == data
    assert ArtifactStore(tmp_path).read(identity) == data
    assert store.put(data) == identity
    assert list(tmp_path.rglob(".pending-*")) == []


def test_empty_artifact_and_distinct_newlines(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    assert store.read(store.put(b"")) == b""
    assert store.put(b"\n").sha256 != store.put(b"\r\n").sha256


def test_store_creation_failure_has_safe_error(tmp_path: Path) -> None:
    target = tmp_path / "private-sentinel"
    target.write_bytes(b"existing file")
    with pytest.raises(PersistenceError, match="could not be opened") as error:
        ArtifactStore(target)
    assert "private-sentinel" not in str(error.value)
    assert target.read_bytes() == b"existing file"


def test_hash_only_retains_no_bytes(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    identity = store.hash_only(
        bytes_digest(b"private-sentinel"), len(b"private-sentinel")
    )
    assert identity.relative_path is None
    assert store.verify(identity) is None
    assert list(tmp_path.iterdir()) == []
    with pytest.raises(ArtifactIntegrityError, match="not retained"):
        store.read(identity)


def test_missing_corrupt_length_and_existing_objects_are_not_replaced(
    tmp_path: Path,
) -> None:
    store = ArtifactStore(tmp_path)
    identity = store.put(b"original")
    path = store.verify(identity)
    assert path is not None
    bad_length = identity.model_copy(update={"byte_length": 999})
    with pytest.raises(ArtifactIntegrityError, match="verification"):
        store.verify(bad_length)
    path.write_bytes(b"damaged")
    with pytest.raises(ArtifactIntegrityError, match="verification"):
        store.put(b"original")
    assert path.read_bytes() == b"damaged"
    assert list(tmp_path.rglob(".pending-*")) == []
    path.unlink()
    with pytest.raises(ArtifactIntegrityError, match="missing"):
        store.verify(identity)


@pytest.mark.parametrize(
    "update",
    [
        {"relative_path": "../private"},
        {"sha256": "bad"},
        {"byte_length": -1},
        {"unknown": "secret-sentinel"},
        {"relative_path": "other/location"},
    ],
)
def test_copied_invalid_identities_rejected(
    tmp_path: Path, update: dict[str, object]
) -> None:
    store = ArtifactStore(tmp_path)
    identity = store.put(b"original").model_copy(update=update)
    with pytest.raises(ArtifactIntegrityError) as error:
        store.verify(identity)
    assert "secret-sentinel" not in str(error.value)


def test_symlink_cannot_redirect_artifact(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "store")
    identity = store.put(b"original")
    path = store.verify(identity)
    assert path is not None
    outside = tmp_path / "outside"
    outside.write_bytes(b"original")
    path.unlink()
    try:
        path.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    with pytest.raises(ArtifactIntegrityError, match="outside"):
        store.verify(identity)
    with pytest.raises(ArtifactIntegrityError, match="outside"):
        store.put(b"original")
    assert outside.read_bytes() == b"original"


def test_simultaneous_publication_preserves_one_complete_object(tmp_path: Path) -> None:
    data = b"synthetic" * 8192

    def publish(_: int) -> artifacts.ArtifactIdentity:
        return ArtifactStore(tmp_path).put(data)

    with ThreadPoolExecutor(max_workers=4) as pool:
        identities = list(pool.map(publish, range(8)))
    assert all(identity == identities[0] for identity in identities)
    assert ArtifactStore(tmp_path).read(identities[0]) == data
    assert list(tmp_path.rglob(".pending-*")) == []


def test_interrupted_publication_has_no_partial_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ArtifactStore(tmp_path)
    data = b"private-sentinel"

    def interrupt(*args: Any, **kwargs: Any) -> None:
        raise OSError(errno.EIO, "private-sentinel")

    monkeypatch.setattr(os, "link", interrupt)
    with pytest.raises(PersistenceError) as error:
        store.put(data)
    assert "private-sentinel" not in str(error.value)
    assert list(tmp_path.rglob(bytes_digest(data))) == []
    assert list(tmp_path.rglob(".pending-*")) == []


def test_temporary_corruption_never_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ArtifactStore(tmp_path)
    original = Path.read_bytes

    def corrupt(path: Path) -> bytes:
        return b"corrupt" if path.name.startswith(".pending-") else original(path)

    monkeypatch.setattr(Path, "read_bytes", corrupt)
    with pytest.raises(ArtifactIntegrityError, match="temporary"):
        store.put(b"original")
    assert list(tmp_path.rglob(bytes_digest(b"original"))) == []


def test_change_between_verify_and_read_is_detected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ArtifactStore(tmp_path)
    identity = store.put(b"original")

    def changed(_: Path) -> bytes:
        return b"changed"

    monkeypatch.setattr(Path, "read_bytes", changed)
    with pytest.raises(ArtifactIntegrityError, match="changed"):
        store.read(identity)


def test_disappearance_between_verify_and_read_is_detected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ArtifactStore(tmp_path)
    identity = store.put(b"original")

    def unavailable(_: Path) -> bytes:
        raise OSError("private-sentinel")

    monkeypatch.setattr(Path, "read_bytes", unavailable)
    with pytest.raises(ArtifactIntegrityError, match="unreadable") as error:
        store.read(identity)
    assert "private-sentinel" not in str(error.value)


def test_cleanup_failure_is_safe_and_published_object_is_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ArtifactStore(tmp_path)

    def unavailable(_: Path, *, missing_ok: bool = False) -> None:
        raise OSError("private-sentinel")

    monkeypatch.setattr(Path, "unlink", unavailable)
    with pytest.raises(PersistenceError, match="could not be removed") as error:
        store.put(b"original")
    assert "private-sentinel" not in str(error.value)
    identity = store.hash_only(bytes_digest(b"original"), len(b"original"))
    retained = identity.model_copy(
        update={"relative_path": f"objects/{identity.sha256[:2]}/{identity.sha256}"}
    )
    assert store.read(retained) == b"original"


def test_directory_fsync_unsupported_and_real_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.name == "nt":
        artifacts._fsync_directory(tmp_path)  # pyright: ignore[reportPrivateUsage]
        return

    def unsupported(_: int) -> None:
        raise OSError(errno.EINVAL, "unsupported")

    monkeypatch.setattr(os, "fsync", unsupported)
    artifacts._fsync_directory(tmp_path)  # pyright: ignore[reportPrivateUsage]

    def broken(_: int) -> None:
        raise OSError(errno.EIO, "failure")

    monkeypatch.setattr(os, "fsync", broken)
    with pytest.raises(OSError):
        artifacts._fsync_directory(tmp_path)  # pyright: ignore[reportPrivateUsage]
