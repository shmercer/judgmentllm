"""Cross-platform descriptor identity and Windows change-time regressions."""

from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, BinaryIO, Literal, cast

import pytest

from judgmentllm.config import RunSpec
from judgmentllm.persistence.hashing import bytes_digest
from judgmentllm.preparation import freezing, open_prepared_run, prepare_run
from judgmentllm.preparation.freezing import PreparationError


def _spec(path: Path, capture: Literal["full", "minimal"]) -> RunSpec:
    return RunSpec.model_validate(
        {
            "task_kind": "rubric",
            "input": str(path),
            "capture": capture,
            "provider": "fixture",
            "endpoint": "text",
            "model": "test",
            "rubric": {
                "rubric_id": "001",
                "name": "Synthetic",
                "traits": [
                    {
                        "trait_id": "quality",
                        "name": "Quality",
                        "description": "Criterion",
                        "levels": [{"label": "low"}, {"label": "high"}],
                    }
                ],
            },
        }
    )


def test_native_path_and_open_descriptor_have_same_signature(tmp_path: Path) -> None:
    path = tmp_path / "source.bin"
    data = b"synthetic source"
    path.write_bytes(data)
    before = freezing.signature(path)
    with path.open("rb") as stream:
        assert freezing.signature(path, stream) == before
        freezing.require_signature(path, before, stream)
        assert stream.read() == data
        freezing.require_signature(path, before, stream)
    freezing.verify_original(path, before, bytes_digest(data), len(data))
    assert set(before.model_dump()) == {
        "schema_version",
        "device",
        "inode",
        "size",
        "mtime_ns",
        "ctime_ns",
    }


def test_native_changed_descriptor_cannot_match_same_bytes_or_timestamps(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.bin"
    different = tmp_path / "different.bin"
    source.write_bytes(b"same bytes")
    different.write_bytes(b"same bytes")
    original = source.stat()
    os.utime(different, ns=(original.st_atime_ns, original.st_mtime_ns))
    with different.open("rb") as wrong_stream:
        with pytest.raises(PreparationError, match="descriptor differ"):
            freezing.signature(source, wrong_stream)


def test_native_replacement_preserving_bytes_length_and_mtime_is_rejected(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.bin"
    replacement = tmp_path / "replacement.bin"
    source.write_bytes(b"same bytes")
    original_stat = source.stat()
    expected = freezing.signature(source)
    replacement.write_bytes(b"same bytes")
    os.utime(replacement, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    replacement.replace(source)
    with pytest.raises(PreparationError, match="generation changed"):
        freezing.require_signature(source, expected)


@pytest.mark.parametrize("restore_mtime", [False, True])
def test_native_same_length_edit_is_rejected_around_existing_descriptor(
    tmp_path: Path, restore_mtime: bool
) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"original")
    original_stat = source.stat()
    with source.open("rb") as stream:
        expected = freezing.signature(source, stream)
        source.write_bytes(b"modified")
        if restore_mtime:
            os.utime(source, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        with pytest.raises(PreparationError):
            freezing.require_signature(source, expected, stream)
    with pytest.raises(PreparationError):
        freezing.verify_original(source, expected, bytes_digest(b"original"), 8)


@pytest.mark.parametrize("capture", ["full", "minimal"])
def test_native_source_session_and_reopen_reject_restored_mtime_edit(
    tmp_path: Path, capture: Literal["full", "minimal"]
) -> None:
    source = tmp_path / "responses.csv"
    source.write_bytes(b"item_id,text\n001,original\n")
    spec = _spec(source, capture)
    store = tmp_path / "runtime"
    original_stat = source.stat()
    with prepare_run(spec, store=store, run_id="001") as prepared:
        assert prepared.get_response("001").text == "original"
        source.write_bytes(b"item_id,text\n001,modified\n")
        os.utime(source, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        with pytest.raises(PreparationError):
            prepared.get_response("001")
    with pytest.raises(PreparationError):
        open_prepared_run("001", store=store, spec=spec)
    if capture == "full":
        with open_prepared_run("001", store=store, use_frozen_snapshot=True) as frozen:
            assert frozen.get_response("001").text == "original"
    else:
        with pytest.raises(PreparationError):
            open_prepared_run("001", store=store, spec=spec, use_frozen_snapshot=True)


def test_windows_probe_uses_descriptor_identity_instead_of_path_stat_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"synthetic")
    original_fstat = os.fstat
    monkeypatch.setattr(freezing, "_WINDOWS", True)

    def native_change(descriptor: int) -> int:
        return original_fstat(descriptor).st_ctime_ns

    monkeypatch.setattr(freezing, "_windows_change_time_reader", lambda: native_change)
    with source.open("rb") as stream:
        with monkeypatch.context() as patch:

            def unavailable_path_stat(
                *args: object, **kwargs: object
            ) -> os.stat_result:
                raise AssertionError("CPython Windows Path.stat may lack file identity")

            patch.setattr(Path, "stat", unavailable_path_stat)
            signature = freezing.signature(source, stream)
            assert signature.inode == original_fstat(stream.fileno()).st_ino
            assert signature.device == original_fstat(stream.fileno()).st_dev


def test_windows_probe_keeps_native_change_time_when_raw_fstat_ctime_differs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"synthetic")
    original_fstat = os.fstat
    monkeypatch.setattr(freezing, "_WINDOWS", True)
    native_change_time = 1234567890000000000

    def native_change(_: int) -> int:
        return native_change_time

    monkeypatch.setattr(freezing, "_windows_change_time_reader", lambda: native_change)
    with source.open("rb") as stream:
        supplied_fd = stream.fileno()

        def different_raw_ctime(descriptor: int) -> os.stat_result:
            value = original_fstat(descriptor)
            return cast(
                os.stat_result,
                SimpleNamespace(
                    st_mode=value.st_mode,
                    st_dev=value.st_dev,
                    st_ino=value.st_ino,
                    st_size=value.st_size,
                    st_mtime_ns=value.st_mtime_ns,
                    st_ctime_ns=1 if descriptor == supplied_fd else 2,
                ),
            )

        monkeypatch.setattr(os, "fstat", different_raw_ctime)
        signature = freezing.signature(source, stream)
        assert signature.ctime_ns == native_change_time


def test_windows_probe_rechecks_its_own_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"synthetic")
    monkeypatch.setattr(freezing, "_WINDOWS", True)
    changes = iter((100, 200))

    def changed(_: int) -> int:
        return next(changes)

    monkeypatch.setattr(freezing, "_windows_change_time_reader", lambda: changed)
    with pytest.raises(PreparationError, match="generation changed"):
        freezing.signature(source)


def test_windows_native_query_is_typed_and_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Query:
        argtypes: list[object] = []
        restype: object = None
        value = 116444736000000007
        succeeds = True

        def __call__(
            self, handle: int, information_class: int, pointer: Any, size: int
        ) -> int:
            assert handle == 2**40 + 77
            assert information_class == 0
            assert size == 40
            ctypes.cast(pointer, ctypes.POINTER(ctypes.c_int64))[3] = self.value
            return int(self.succeeds)

    query = Query()
    fake_msvcrt = ModuleType("msvcrt")

    def fake_handle(descriptor: int) -> int:
        return 2**40 + descriptor

    def fake_dll(*args: object, **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(GetFileInformationByHandleEx=query)

    monkeypatch.setattr(fake_msvcrt, "get_osfhandle", fake_handle, raising=False)
    monkeypatch.setitem(sys.modules, "msvcrt", fake_msvcrt)
    monkeypatch.setattr(
        ctypes,
        "WinDLL",
        fake_dll,
        raising=False,
    )
    freezing._windows_change_time_reader.cache_clear()  # pyright: ignore[reportPrivateUsage]
    try:
        read = freezing._windows_change_time_reader()  # pyright: ignore[reportPrivateUsage]
        assert read(77) == 700
        query.succeeds = False
        with pytest.raises(PreparationError, match="unavailable"):
            read(77)
        query.succeeds = True
        query.value = 0
        with pytest.raises(PreparationError, match="no usable change time"):
            read(77)
    finally:
        freezing._windows_change_time_reader.cache_clear()  # pyright: ignore[reportPrivateUsage]


def test_exact_hash_still_rejects_content_when_metadata_appears_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"original")
    expected = freezing.signature(source)
    source.write_bytes(b"modified")

    def unchanged(path: Path, stream: BinaryIO | None = None) -> freezing.FileSignature:
        return expected

    monkeypatch.setattr(freezing, "signature", unchanged)
    with pytest.raises(PreparationError, match="content differs"):
        freezing.verify_original(source, expected, bytes_digest(b"original"), 8)


def test_legacy_windows_creation_time_signature_is_not_silently_reinterpreted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"original")
    monkeypatch.setattr(freezing, "_WINDOWS", True)

    def changed(_: int) -> int:
        return 1234567890000000000

    monkeypatch.setattr(freezing, "_windows_change_time_reader", lambda: changed)
    expected = freezing.signature(source)
    legacy = expected.model_copy(update={"ctime_ns": expected.ctime_ns - 100})
    with pytest.raises(PreparationError, match="generation changed"):
        freezing.require_signature(source, legacy)
