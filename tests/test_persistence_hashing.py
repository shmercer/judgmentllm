"""Golden semantic identities stay distinct from structural IDs and byte hashes."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from judgmentllm.models import Level, ResponseItem, Trait
from judgmentllm.persistence.hashing import (
    bytes_digest,
    canonical_bytes,
    semantic_digest,
)
from judgmentllm.persistence.models import ArtifactIdentity
from judgmentllm.planning import stable_id


def test_canonical_golden_unicode_metadata_null_and_order() -> None:
    value = {"z": [None, True, 7], "a": {"é": "cafe\u0301", "β": "\r\n"}}
    expected = '{"a":{"é":"cafe\u0301","β":"\\r\\n"},"z":[null,true,7]}'
    assert canonical_bytes(value) == expected.encode("utf-8")
    assert canonical_bytes({"z": value["z"], "a": value["a"]}) == canonical_bytes(value)
    assert canonical_bytes([1, 2]) != canonical_bytes([2, 1])
    assert canonical_bytes("café") != canonical_bytes("cafe\u0301")
    assert canonical_bytes("\r\n") != canonical_bytes("\n")


def test_model_defaults_null_and_nonuniform_values_are_included() -> None:
    item = ResponseItem(item_id="001", text="Synthetic", metadata={"nested": [None, 3]})
    assert canonical_bytes(item) == canonical_bytes(
        {
            "schema_version": 1,
            "item_id": "001",
            "text": "Synthetic",
            "metadata": {"nested": [None, 3]},
        }
    )
    trait = Trait(
        trait_id="quality",
        name="Quality",
        description="Synthetic scale",
        levels=[
            Level(label="low", numeric_value=1),
            Level(label="middle", numeric_value=3),
            Level(label="high", numeric_value=8),
        ],
    )
    data = json.loads(canonical_bytes(trait))
    assert [level["numeric_value"] for level in data["levels"]] == [1.0, 3.0, 8.0]
    assert all(level["source_label"] is not None for level in data["levels"])
    assert canonical_bytes({"missing": None}) != canonical_bytes({})


def test_versioned_domain_separation_and_exact_bytes() -> None:
    value = {"identity": "001", "nullable": None}
    envelope = b'[1,"input",{"identity":"001","nullable":null}]'
    assert semantic_digest("input", value) == hashlib.sha256(envelope).hexdigest()
    assert semantic_digest("input", value) != semantic_digest("prompt", value)
    assert semantic_digest("input", value) != bytes_digest(canonical_bytes(value))
    assert bytes_digest(b"\r\n") != bytes_digest(b"\n")
    assert bytes_digest(b"\x00\xff") == hashlib.sha256(b"\x00\xff").hexdigest()
    # Foundation identities must leave the established structural algorithm alone.
    assert (
        stable_id("task", "001", "quality")
        == "task-" + hashlib.sha256(b'[1,"task","001","quality"]').hexdigest()
    )


@pytest.mark.parametrize(
    "value",
    [
        float("nan"),
        float("inf"),
        float("-inf"),
        {"nested": [float("nan")]},
        {1: "number key"},
        {"nested": {None: 1}},
        {"tuple": (1, 2)},
        object(),
        Path("private"),
        "\ud800",
    ],
)
def test_reject_invalid_json_without_exposing_values(value: object) -> None:
    with pytest.raises(ValueError, match="cannot be canonically encoded"):
        canonical_bytes(value)


def test_reject_cycles() -> None:
    cyclic: list[object] = []
    cyclic.append(cyclic)
    with pytest.raises(ValueError, match="cannot be canonically encoded"):
        canonical_bytes(cyclic)


def test_scalar_subclasses_do_not_bypass_canonical_validation() -> None:
    class String(str):
        pass

    class Integer(int):
        pass

    class Number(float):
        pass

    for value in (String("private"), Integer(1), Number(1), Number("nan")):
        for data in (value, {"nested": [value]}):
            with pytest.raises(ValueError, match="cannot be canonically encoded"):
                canonical_bytes(data)


@pytest.mark.parametrize("mutation", ["unknown", "wrong_type", "nonfinite", "nested"])
def test_revalidate_current_model_state(mutation: str) -> None:
    item = ResponseItem(item_id="001", text="Sensitive-sentinel")
    if mutation == "unknown":
        item = item.model_copy(update={"unrequested": "Sensitive-sentinel"})
    elif mutation == "wrong_type":
        item = item.model_copy(update={"text": 42})
    elif mutation == "nonfinite":
        item.metadata["bad"] = float("nan")
    else:
        item.metadata["bad"] = {"nested": float("inf")}
    with pytest.raises(ValueError) as error:
        canonical_bytes(item)
    assert "Sensitive-sentinel" not in str(error.value)


def test_frozen_model_copy_is_revalidated() -> None:
    artifact = ArtifactIdentity(sha256="a" * 64, byte_length=3)
    invalid = artifact.model_copy(
        update={"byte_length": -1, "unrequested": "Sensitive-sentinel"}
    )
    with pytest.raises(ValueError) as error:
        canonical_bytes(invalid)
    assert "Sensitive-sentinel" not in str(error.value)


@pytest.mark.parametrize(
    "domain", ["", "  ", " leading", "trailing ", "line\n", "control\x7f", None, 1]
)
def test_invalid_domain(domain: Any) -> None:
    with pytest.raises(ValueError, match="domain"):
        semantic_digest(domain, {})


def test_exact_hash_rejects_nonbytes() -> None:
    with pytest.raises(TypeError, match="requires bytes"):
        bytes_digest("private text")  # type: ignore[arg-type]


def test_hashes_are_deterministic_in_independent_processes() -> None:
    program = "from judgmentllm.persistence.hashing import semantic_digest; print(semantic_digest('input', {'β': [None, 1, 3, 8], 'a': 'é\\r\\n'}))"
    expected = semantic_digest("input", {"β": [None, 1, 3, 8], "a": "é\r\n"})
    for _ in range(2):
        result = subprocess.run(
            [sys.executable, "-c", program], capture_output=True, text=True, check=True
        )
        assert result.stdout.strip() == expected
        assert result.stderr == ""
