"""Optional OpenAI proxy estimates. Nothing here supplies execution authority."""

from __future__ import annotations

import os
from base64 import b64decode
from collections.abc import Callable, Iterator
from hashlib import sha1
from importlib import import_module
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Literal, cast

from pydantic import Field

from judgmentllm.cache_policy import CacheAnchor
from judgmentllm.persistence.artifacts import ArtifactStore
from judgmentllm.persistence.hashing import bytes_digest, canonical_bytes
from judgmentllm.persistence.models import SHA256, ArtifactIdentity, ExecutionModel
from judgmentllm.planning import stable_id
from judgmentllm.preparation.session import PreparedRun, RequestDependency
from judgmentllm.production_transport import PreparedRequestV4, TransportCommitmentV2

_TOKENIZER = "0.14.0"
_ASSET_URL = "https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken"
_ASSET_SHA256 = "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d"
_ASSET_LIMIT = 8 * 1024 * 1024
_REFERENCE_TOKENS = 1024
_MAX_REPORTS = 100
_MAX_COMPANION_BYTES = 16_384

# o200k_base pretokenizer from OpenAI tiktoken 0.14.0.
# MIT License
# Copyright (c) 2022 OpenAI, Shantanu Jain
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
_PATTERN = "|".join(
    (
        r"[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}]*[\p{Ll}\p{Lm}\p{Lo}\p{M}]+(?i:'s|'t|'re|'ve|'m|'ll|'d)?",
        r"[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}]+[\p{Ll}\p{Lm}\p{Lo}\p{M}]*(?i:'s|'t|'re|'ve|'m|'ll|'d)?",
        r"\p{N}{1,3}",
        r" ?[^\s\p{L}\p{N}]+[\r\n/]*",
        r"\s*[\r\n]+",
        r"\s+(?!\S)",
        r"\s+",
    )
)

AssetStatus = Literal[
    "ready",
    "asset_unavailable",
    "asset_invalid",
    "tokenizer_unavailable",
    "tokenizer_incompatible",
    "advisory_unavailable",
]
WarningCode = Literal["short", "borderline", "disagreement", "unknown"]
Estimator = tuple[AssetStatus, Callable[[str], int] | None]


class MarkerAdvisory(ExecutionModel):
    anchor: CacheAnchor
    prefix_bytes: int = Field(gt=0)
    prefix_sha256: SHA256
    marker_ordinal: int = Field(ge=1, le=4)
    joined_estimate: int | None = Field(default=None, ge=0)
    block_estimate: int | None = Field(default=None, ge=0)
    warnings: tuple[WarningCode, ...]


class CacheAdvisory(ExecutionModel):
    """Safe per-attempt evidence of an optional estimate, including unknowns."""

    estimator_version: Literal[1, 2] = 2
    tokenizer_version: Literal["0.14.0"] = _TOKENIZER
    encoding: Literal["o200k_base"] = "o200k_base"
    encoding_sha256: SHA256 = _ASSET_SHA256
    asset_status: AssetStatus
    cohort_ordinal: int = Field(gt=0)
    request_ordinal: int = Field(gt=0)
    attempt_number: int = Field(gt=0)
    markers: tuple[MarkerAdvisory, ...] = Field(min_length=1, max_length=4)


class CacheAdvisoryReport(ExecutionModel):
    """Bounded offline preview of the prefix diagnostics and candidate status."""

    qualification: Literal["offline_candidate"] = "offline_candidate"
    provider_reference_tokens: Literal[1024] = _REFERENCE_TOKENS
    physical_requests: int = Field(ge=0)
    truncated: bool
    requests: tuple[CacheAdvisory, ...] = Field(max_length=_MAX_REPORTS)


def _estimator() -> Estimator:
    directory = os.environ.get("TIKTOKEN_CACHE_DIR")
    if not directory:
        return "asset_unavailable", None
    path = (
        Path(directory) / sha1(_ASSET_URL.encode(), usedforsecurity=False).hexdigest()
    )
    try:
        if path.is_symlink() or not path.is_file():
            return "asset_unavailable", None
        with path.open("rb") as stream:
            asset = stream.read(_ASSET_LIMIT + 1)
    except (OSError, ValueError):
        return "asset_unavailable", None
    if len(asset) > _ASSET_LIMIT or bytes_digest(asset) != _ASSET_SHA256:
        return "asset_invalid", None
    try:
        if version("tiktoken") != _TOKENIZER:
            return "tokenizer_incompatible", None
        ranks = {
            b64decode(key, validate=True): int(rank)
            for key, rank in (line.split() for line in asset.splitlines())
        }
        encoder = import_module("tiktoken").Encoding(
            name="o200k_base",
            pat_str=_PATTERN,
            mergeable_ranks=ranks,
            special_tokens={"<|endoftext|>": 199999, "<|endofprompt|>": 200018},
        )
    except (PackageNotFoundError, ImportError):
        return "tokenizer_unavailable", None
    except Exception:
        return "asset_invalid", None
    encode = cast(Callable[[str], list[int]], encoder.encode_ordinary)
    return "ready", lambda text: len(encode(text))


def _warnings(joined: int | None, blocks: int | None) -> tuple[WarningCode, ...]:
    if joined is None or blocks is None:
        return ("unknown",)
    codes: list[WarningCode] = []
    if max(joined, blocks) < _REFERENCE_TOKENS:
        codes.append("short")
    elif min(joined, blocks) <= _REFERENCE_TOKENS + 1:
        codes.append("borderline")
    if joined != blocks:
        codes.append("disagreement")
    return tuple(codes)


def _advisory(
    receipt: TransportCommitmentV2,
    cohort: int,
    ordinal: int,
    attempt: int,
    estimate: Estimator,
    text: str = "",
) -> CacheAdvisory:
    layout = receipt.explicit_cache
    assert layout is not None
    status, count = estimate
    blocks = layout.blocks(text)[:-1] if count is not None else ()
    markers: list[MarkerAdvisory] = []
    for index, prefix in enumerate(layout.prefixes, 1):
        joined = separate = None
        if count is not None:
            try:
                joined = count("".join(blocks[:index]))
                separate = sum(count(block) for block in blocks[:index])
            except Exception:
                joined = separate = None
        markers.append(
            MarkerAdvisory(
                **prefix.model_dump(),
                marker_ordinal=index,
                joined_estimate=joined,
                block_estimate=separate,
                warnings=_warnings(joined, separate),
            )
        )
    return CacheAdvisory(
        asset_status=status,
        cohort_ordinal=cohort,
        request_ordinal=ordinal,
        attempt_number=attempt,
        markers=tuple(markers),
    )


def _reference(receipt: TransportCommitmentV2) -> str:
    return f"cache-advisories/{receipt.commitment_sha256}.json"


def _publish(
    store: ArtifactStore, receipt: TransportCommitmentV2, advisory: CacheAdvisory
) -> None:
    try:
        artifact = store.put(canonical_bytes(advisory), "application/json")
        store.publish_reference(artifact, _reference(receipt))
    except Exception:
        # Optional metadata cannot participate in a seal, intent, or recovery.
        pass


def _read(store: ArtifactStore, receipt: TransportCommitmentV2) -> CacheAdvisory | None:
    try:
        path = store.reference_path(_reference(receipt))
        if not path.is_file():
            return None
        with path.open("rb") as stream:
            raw = stream.read(_MAX_COMPANION_BYTES + 1)
        if len(raw) > _MAX_COMPANION_BYTES:
            return None
        record = CacheAdvisory.model_validate_json(raw)
        if canonical_bytes(record) != raw:
            return None
        sha256 = bytes_digest(raw)
        store.verify(
            ArtifactIdentity(
                sha256=sha256,
                byte_length=len(raw),
                media_type="application/json",
                relative_path=f"objects/{sha256[:2]}/{sha256}",
            )
        )
        return record
    except Exception:
        return None


def _initial_receipts(
    prepared: PreparedRun,
) -> Iterator[tuple[int, int, RequestDependency, TransportCommitmentV2]]:
    groups = {
        group: ordinal
        for ordinal, group in enumerate(prepared.manifest.trait_groups, 1)
    }
    run_id = prepared.manifest.run_id
    for ordinal, dependency in enumerate(prepared.iter_request_dependencies(), 1):
        attempt = stable_id("attempt", run_id, dependency.request.request_uid, 1)
        receipt = prepared.transport_for_attempt(attempt)
        if (
            isinstance(receipt, TransportCommitmentV2)
            and receipt.explicit_cache is not None
        ):
            yield ordinal, groups[dependency.request.trait_ids], dependency, receipt


def publish_initial_cache_advisories(prepared: PreparedRun) -> None:
    """Publish after the necessary seal and its ownership have completed."""
    try:
        estimate = _estimator()
        for ordinal, cohort, dependency, receipt in _initial_receipts(prepared):
            text = ""
            if estimate[1] is not None:
                wire = prepared.prepare_transport_attempt(
                    dependency, receipt.identity, frozen=receipt
                )
                assert isinstance(wire, PreparedRequestV4)
                text = wire.rendered_text
            _publish(
                prepared.store,
                receipt,
                _advisory(receipt, cohort, ordinal, 1, estimate, text),
            )
    except Exception:
        # A later inspection can report missing estimates from the verified receipts.
        pass


def publish_retry_cache_advisory(
    prepared: PreparedRun,
    wire: PreparedRequestV4,
    original: RequestDependency,
    attempt: int,
) -> None:
    """Publish after the retry transaction, outside any admission machinery."""
    try:
        receipt = wire.transport_commitment
        cohort = prepared.manifest.trait_groups.index(original.request.trait_ids) + 1
        request = prepared.repository.get_request(
            receipt.identity.run_id, wire.request_uid
        )
        advisory = _advisory(
            receipt,
            cohort,
            request.scheduling_ordinal + 1,
            attempt,
            _estimator(),
            wire.rendered_text,
        )
        _publish(prepared.store, receipt, advisory)
    except Exception:
        pass


def cache_advisory_report(prepared: PreparedRun) -> CacheAdvisoryReport | None:
    if prepared.manifest.cache_policy_version != 3:
        return None
    from judgmentllm.preparation.manifest import ExecutableRunManifestV4

    assert isinstance(prepared.manifest, ExecutableRunManifestV4)
    reports: list[CacheAdvisory] = []
    try:
        for ordinal, cohort, _, receipt in _initial_receipts(prepared):
            if len(reports) == _MAX_REPORTS:
                break
            record = _read(prepared.store, receipt)
            fallback = _advisory(
                receipt, cohort, ordinal, 1, ("advisory_unavailable", None)
            )
            if record is not None and (
                record.cohort_ordinal == cohort
                and record.request_ordinal == ordinal
                and record.attempt_number == 1
                and tuple(
                    (x.anchor, x.prefix_bytes, x.prefix_sha256, x.marker_ordinal)
                    for x in record.markers
                )
                == tuple(
                    (x.anchor, x.prefix_bytes, x.prefix_sha256, x.marker_ordinal)
                    for x in fallback.markers
                )
            ):
                reports.append(record)
            else:
                reports.append(fallback)
    except Exception:
        pass
    return CacheAdvisoryReport(
        physical_requests=prepared.manifest.initial_transport_count,
        truncated=len(reports) < prepared.manifest.initial_transport_count,
        requests=tuple(reports),
    )
