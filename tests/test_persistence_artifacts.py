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
        store = ArtifactStore(tmp_path)
        try:
            return store.put(data)
        except ArtifactIntegrityError as error:
            candidate = (
                store.root / "objects" / bytes_digest(data)[:2] / bytes_digest(data)
            )
            error.add_note(
                f"Synthetic publication: root={store.root!s}, "
                f"parent={candidate.parent.resolve()!s}, symlink={candidate.is_symlink()}"
            )
            raise

    with ThreadPoolExecutor(max_workers=4) as pool:
        identities = list(pool.map(publish, range(8)))
    assert all(identity == identities[0] for identity in identities)
    assert ArtifactStore(tmp_path).read(identities[0]) == data
    assert list(tmp_path.rglob(".pending-*")) == []


def test_containment_does_not_resolve_concurrently_published_leaf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ArtifactStore(tmp_path / "store")
    data = b"synthetic"
    digest = bytes_digest(data)
    original = Path.resolve

    def resolve(path: Path, strict: bool = False) -> Path:
        # Windows final-name resolution can spell a changing leaf differently.
        # Only the resolved directory and the validated digest locate this file.
        if path.name == digest:
            return tmp_path / "alternate-spelling" / digest
        return original(path, strict=strict)

    monkeypatch.setattr(Path, "resolve", resolve)
    identity = store.put(data)
    assert store.read(identity) == data
    assert store.put(data) == identity


def test_parent_symlink_cannot_redirect_artifact(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "store")
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (store.root / "objects").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    with pytest.raises(ArtifactIntegrityError, match="outside"):
        store.put(b"original")
    assert list(outside.iterdir()) == []


def test_equivalent_root_spelling_uses_directory_identity(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "store")
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(store.root, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    # Exercise the same directory with a spelling unlike its resolved parents.
    store.root = alias
    identity = store.put(b"synthetic")
    assert store.read(identity) == b"synthetic"


def test_replaced_or_missing_root_is_not_a_valid_alias(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "store")
    identity = store.put(b"original")
    store.root.rename(tmp_path / "original-store")
    with pytest.raises(ArtifactIntegrityError, match="outside"):
        store.verify(identity)
    store.root.mkdir()
    with pytest.raises(ArtifactIntegrityError, match="outside"):
        store.put(b"original")
    assert list(store.root.iterdir()) == []


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
    original = artifacts._file_digest  # pyright: ignore[reportPrivateUsage]

    def corrupt(path: Path) -> tuple[str, int]:
        return (
            (bytes_digest(b"corrupt"), 7)
            if path.name.startswith(".pending-")
            else original(path)
        )

    monkeypatch.setattr(artifacts, "_file_digest", corrupt)
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


def test_streaming_stage_defers_publication_and_cleans_private_file(
    tmp_path: Path,
) -> None:
    store = ArtifactStore(tmp_path)
    data = b"source-sentinel" * 200
    digest = bytes_digest(data)
    with store.staged_stream(
        (data[:100], data[100:]), expected_sha256=digest, expected_length=len(data)
    ) as stage:
        assert stage.path.read_bytes() == data
        assert stage.path.stat().st_mode & 0o077 == 0 if os.name != "nt" else True
        assert not (store.root / stage.identity.relative_path).exists()  # type: ignore[arg-type]
        identity = stage.publish()
        assert store.read(identity) == data
    assert not stage.path.exists()
    assert store.put_stream((data,)) == identity


def test_streaming_large_file_uses_bounded_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tracemalloc

    store = ArtifactStore(tmp_path)
    chunk = b"synthetic-source" * 4096

    def forbidden(_: Path) -> bytes:
        raise AssertionError("streaming publication must not call read_bytes")

    monkeypatch.setattr(Path, "read_bytes", forbidden)
    tracemalloc.start()
    try:
        identity = store.put_stream(chunk for _ in range(512))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert identity.byte_length == len(chunk) * 512
    assert peak < 6 * 1024 * 1024
    assert store.verify(identity) is not None


@pytest.mark.parametrize(
    "expected", [{"expected_sha256": "0" * 64}, {"expected_length": 2}]
)
def test_streaming_expected_identity_failure_removes_stage(
    tmp_path: Path, expected: dict[str, Any]
) -> None:
    store = ArtifactStore(tmp_path)
    with pytest.raises(ArtifactIntegrityError, match="expected"):
        store.put_stream((b"secret",), **expected)
    assert list(tmp_path.rglob(".pending-*")) == []
    assert list(tmp_path.rglob("objects")) == []


def test_streaming_exception_and_invalid_chunks_remove_stage(tmp_path: Path) -> None:
    from collections.abc import Iterator

    store = ArtifactStore(tmp_path)

    def interrupted() -> Iterator[bytes]:
        yield b"secret"
        raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        store.put_stream(interrupted())
    with pytest.raises(TypeError, match="bytes"):
        store.put_stream(("secret",))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="bytes"):
        store.put("secret")  # type: ignore[arg-type]
    assert list(tmp_path.rglob(".pending-*")) == []


def test_stage_cannot_publish_modified_or_removed_bytes(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path)
    with store.staged_stream((b"original",)) as stage:
        stage.path.write_bytes(b"modified")
        with pytest.raises(ArtifactIntegrityError, match="temporary"):
            stage.publish()
        stage.path.unlink()
        with pytest.raises(ArtifactIntegrityError, match="unavailable"):
            stage.publish()
    assert list(tmp_path.rglob("objects")) == []


def test_fixed_manifest_is_no_overwrite_and_verifies_existing_content(
    tmp_path: Path,
) -> None:
    store = ArtifactStore(tmp_path)
    identity = store.put(b'{"kind":"executable"}', "application/json")
    fixed = store.publish_manifest(identity)
    assert fixed.read_bytes() == store.read(identity)
    assert store.publish_manifest(identity) == fixed
    other = store.put(b"different", "application/json")
    with pytest.raises(ArtifactIntegrityError, match="different"):
        store.publish_manifest(other)
    assert fixed.read_bytes() == store.read(identity)
    with pytest.raises(ArtifactIntegrityError, match="retained"):
        store.publish_manifest(store.hash_only(bytes_digest(b"missing"), 7))


def test_fixed_manifest_rejects_symlink(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "store")
    identity = store.put(b"original")
    outside = tmp_path / "outside"
    outside.write_bytes(b"original")
    try:
        (store.root / "manifest.json").symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is unavailable")
    with pytest.raises(ArtifactIntegrityError, match="symlink"):
        store.publish_manifest(identity)


def test_orphan_staging_file_is_never_automatically_published(tmp_path: Path) -> None:
    import subprocess
    import sys

    script = """
import os, sys
from judgmentllm.persistence.artifacts import ArtifactStore
store = ArtifactStore(sys.argv[1])
with store.staged_stream((b'private-orphan',)):
    os._exit(71)
"""
    outcome = subprocess.run([sys.executable, "-c", script, str(tmp_path)], check=False)
    assert outcome.returncode == 71
    orphans = list(tmp_path.glob(".pending-*"))
    assert len(orphans) == 1
    store = ArtifactStore(tmp_path)
    store.put_stream((b"other",))
    assert list(tmp_path.glob(".pending-*")) == orphans
    assert not (
        tmp_path
        / "objects"
        / bytes_digest(b"private-orphan")[:2]
        / bytes_digest(b"private-orphan")
    ).exists()
