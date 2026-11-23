"""Checked POSIX wire identity for sealed, full historical V2 preparations."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, cast

from pydantic import BaseModel

from judgmentllm.cache_policy import CachePolicyVersion
from judgmentllm.config import RunSpecV2, parse_json, validate_run_spec_json
from judgmentllm.io import _revalidate_record  # pyright: ignore[reportPrivateUsage]
from judgmentllm.persistence.hashing import (
    bytes_digest,
    canonical_bytes,
    semantic_digest,
)
from judgmentllm.preparation.freezing import PreparationError
from judgmentllm.preparation.manifest import (
    ExecutableRunManifestV2,
    assessment_digest,
)

_MANIFEST_PATHS = (("input", "path"), ("pairs", "path"))
_SPECIFICATION_PATHS = (*_MANIFEST_PATHS, ("prompt", "file"), ("output_path",))


def foreign_original(location: str) -> bool:
    """Classify provenance without resolving a consumer-filesystem alias."""
    posix = PurePosixPath(location).is_absolute()
    if os.name == "nt":
        return posix
    return not posix and PureWindowsPath(location).is_absolute()


def _parent(data: dict[str, Any], path: tuple[str, ...]) -> dict[str, Any] | None:
    for component in path[:-1]:
        if data.get(component) is None:
            return None
        if not isinstance(data[component], dict):
            raise PreparationError("historical path container is inconsistent")
        data = data[component]
    return data


def _posix(value: object, *, absolute: bool = False) -> str:
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise PreparationError("historical path spelling is invalid")
    path = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if (
        str(path) != value
        or windows.drive
        or windows.as_posix() != value
        or (absolute and path.root != "/")
    ):
        raise PreparationError("historical path spelling is not canonical POSIX")
    return value


@dataclass(frozen=True)
class _WireRecord:
    raw: bytes
    model_type: type[ExecutableRunManifestV2] | type[RunSpecV2]
    bindings: tuple[tuple[tuple[str, ...], str, str], ...]

    @classmethod
    def checked(
        cls, model: ExecutableRunManifestV2 | RunSpecV2, raw: bytes
    ) -> _WireRecord:
        if type(model) not in (ExecutableRunManifestV2, RunSpecV2):
            raise PreparationError("historical wire protocol is unsupported")
        decoded = parse_json(raw.decode("utf-8"))
        if not isinstance(decoded, dict):
            raise PreparationError("historical wire must be an object")
        data = cast(dict[str, Any], decoded)
        paths = (
            _MANIFEST_PATHS
            if type(model) is ExecutableRunManifestV2
            else _SPECIFICATION_PATHS
        )
        bindings: list[tuple[tuple[str, ...], str, str]] = []
        for path in paths:
            parent = _parent(data, path)
            if parent is None or parent.get(path[-1]) is None:
                continue
            original = _posix(parent[path[-1]])
            bindings.append((path, original, str(Path(original))))
        record = cls(raw, type(model), tuple(bindings))
        record.encode(model)
        return record

    def encode(self, model: BaseModel) -> bytes:
        if type(model) is not self.model_type:
            raise PreparationError("historical wire model differs")
        data = _revalidate_record(model).model_dump(mode="json")
        for path, original, native in self.bindings:
            parent = _parent(data, path)
            if parent is None or parent.get(path[-1]) != native:
                raise PreparationError("historical path was changed after verification")
            parent[path[-1]] = original
        if canonical_bytes(data) != self.raw:
            raise PreparationError("historical canonical wire differs")
        return self.raw


def manifest_wire(manifest: ExecutableRunManifestV2, raw: bytes) -> _WireRecord:
    if type(manifest) is not ExecutableRunManifestV2 or manifest.capture != "full":
        raise PreparationError("historical POSIX wire requires a full V2 manifest")
    for source in manifest.sources:
        _posix(source.original_location, absolute=True)
    return _WireRecord.checked(manifest, raw)


@dataclass(frozen=True)
class HistoricalWire:
    manifest: _WireRecord
    specification: _WireRecord
    cache_policy_version: CachePolicyVersion

    @classmethod
    def checked(
        cls, manifest: ExecutableRunManifestV2, raw: bytes, specification: bytes
    ) -> HistoricalWire:
        record = manifest_wire(manifest, raw)
        frozen = validate_run_spec_json(
            specification.decode("utf-8"),
            _cache_policy_version=manifest.cache_policy_version,
        )
        if type(frozen) is not RunSpecV2:
            raise PreparationError("historical snapshot requires RunSpecV2")
        spec_record = _WireRecord.checked(frozen, specification)
        if (bytes_digest(specification), len(specification)) != (
            manifest.specification.sha256,
            manifest.specification.byte_length,
        ) or (
            semantic_digest(
                "run-configuration", parse_json(specification.decode("utf-8"))
            )
            != manifest.configuration_sha256
            or assessment_digest(frozen) != manifest.assessment_sha256
        ):
            raise PreparationError("historical specification differs from its manifest")
        return cls(record, spec_record, manifest.cache_policy_version)

    def restore(self) -> RunSpecV2:
        # Parsing is independent of the native model retained by the caller.
        spec = validate_run_spec_json(
            self.specification.raw.decode("utf-8"),
            _cache_policy_version=self.cache_policy_version,
        )
        if type(spec) is not RunSpecV2:
            raise PreparationError("historical specification protocol differs")
        self.specification.encode(spec)
        return spec

    def configuration_digest(self, spec: BaseModel) -> str:
        raw = self.specification.encode(spec)
        return semantic_digest("run-configuration", parse_json(raw.decode("utf-8")))
