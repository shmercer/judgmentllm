"""Strict source/index metadata for immutable preparation, without source text."""

from __future__ import annotations

import json
from typing import Literal, cast

from pydantic import Field, field_validator, model_validator

from judgmentllm.config import parse_json
from judgmentllm.models import ID, PairTask
from judgmentllm.persistence.models import SHA256, ExecutionModel


class PairStructure(ExecutionModel):
    """Permitted pair references; arbitrary input metadata is never retained."""

    pair_id: ID
    pair_uid: ID
    left_item_id: ID
    right_item_id: ID
    trait_id: ID
    original_order: list[ID]
    presented_order: list[ID]
    source_pair_uid: ID | None = None
    repetition_index: int = Field(default=0, ge=0)
    is_reversal: bool = False
    reversal_group: ID | None = None

    @model_validator(mode="after")
    def valid_orientation(self) -> PairStructure:
        PairTask.model_validate(self.model_dump())
        return self


class SourceRecord(ExecutionModel):
    """Exact original-file provenance, separate from semantic configuration."""

    run_id: ID
    source_id: ID
    role: Literal[
        "configuration",
        "rubric",
        "criteria",
        "exemplars",
        "template",
        "responses",
        "pairs",
        "response",
        "pair",
    ]
    ordinal: int = Field(ge=0)
    original_location: str
    raw_sha256: SHA256
    byte_length: int = Field(ge=0)
    canonical_sha256: SHA256 | None = None
    format: Literal["yaml", "yml", "json", "csv", "jsonl", "text", "txt"]
    snapshot_sha256: SHA256 | None = None
    generation_json: str | None = None

    @field_validator("generation_json")
    @classmethod
    def safe_generation(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            data = parse_json(value)
        except ValueError:
            raise ValueError("source generation is invalid") from None
        permitted = {
            "dev",
            "ino",
            "device",
            "inode",
            "size",
            "mtime_ns",
            "ctime_ns",
            "st_dev",
            "st_ino",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        }
        if not isinstance(data, dict):
            raise ValueError(
                "source generation must contain only file identity numbers"
            )
        mapping = cast(dict[object, object], data)
        if any(
            type(key) is not str or key not in permitted or type(item) is not int
            for key, item in mapping.items()
        ):
            raise ValueError(
                "source generation must contain only file identity numbers"
            )
        return json.dumps(data, sort_keys=True, separators=(",", ":"))


class InputRecord(ExecutionModel):
    """A logical record's exact byte locator and canonical content commitment."""

    run_id: ID
    record_kind: Literal["response", "pair"]
    record_id: ID
    source_id: ID
    ordinal: int = Field(ge=0)
    record_sha256: SHA256
    byte_offset: int = Field(ge=0)
    byte_length: int = Field(gt=0)
    structural_json: str = "{}"
    snapshot_sha256: SHA256 | None = None

    @model_validator(mode="after")
    def safe_structure(self) -> InputRecord:
        try:
            data = parse_json(self.structural_json)
            if self.record_kind == "response":
                if data != {}:
                    raise ValueError("response index retains no content fields")
            else:
                pair = PairStructure.model_validate(data)
                if pair.pair_uid != self.record_id:
                    raise ValueError("pair index identity does not match references")
        except ValueError:
            raise ValueError("input record structure is invalid") from None
        return self
