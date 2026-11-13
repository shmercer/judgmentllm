"""Verify original study bytes before migration, without writing to the originals."""

from __future__ import annotations

import base64
import gzip
import json
from pathlib import Path
from typing import Any

from judgmentllm.config import InputSpec, load_run_spec
from judgmentllm.persistence.hashing import bytes_digest
from judgmentllm.preview import preview_prompt
from judgmentllm.study.definitions import source_manifest


def archive(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in gzip.decompress(base64.b64decode(path.read_bytes())).splitlines()
    ]


def verify_originals(source: Path, package: Path) -> dict[str, object]:
    """Independently hash both inventories and compare all 312 actual previews."""
    manifests = source_manifest(package)
    snapshots = {
        (r["kind"], r["name"]): r
        for r in archive(package / "original-previews.jsonl.gz.base64")
    }
    matched = 0
    for kind, record in manifests.items():
        root = source / record["package"]
        raw = (root / "manifest_sha256.json").read_bytes()
        actual = {
            p.relative_to(root).as_posix(): bytes_digest(p.read_bytes())
            for p in sorted(root.rglob("*"))
            if p.is_file()
            and p.name != "manifest_sha256.json"
            and not {"actual_engine_previews", "__pycache__"}.intersection(
                p.relative_to(root).parts
            )
        }
        if (
            bytes_digest(raw) != record["manifest_sha256"]
            or json.loads(raw) != actual
            or actual != record["files"]
        ):
            raise ValueError("Original study manifest drift")
        for original in (package / "sources" / kind).rglob("*"):
            if (
                original.is_file()
                and original.read_bytes()
                != (
                    root / original.relative_to(package / "sources" / kind)
                ).read_bytes()
            ):
                raise ValueError("Migrated study source drift")
        for cfg in sorted((root / "configs").glob("*.yaml")):
            spec = load_run_spec(cfg)
            if kind == "rubric":
                assert spec.rubric is not None
                traits = (
                    spec.rubric.traits
                    if spec.execution.grouping == "independent"
                    else spec.rubric.traits[:1]
                )
                cases = [
                    (
                        cfg.stem
                        + (
                            "__" + t.trait_id
                            if spec.execution.grouping == "independent"
                            else ""
                        ),
                        preview_prompt(
                            spec, item_id="essay_demo_001", trait_id=t.trait_id
                        ),
                    )
                    for t in traits
                ]
            else:
                cases: list[tuple[str, Any]] = []
                for orientation in ("original", "reversed"):
                    current = (
                        spec
                        if orientation == "original"
                        else spec.model_copy(
                            update={
                                "pairs": InputSpec(
                                    path=root / "pairs_reversal_demo.jsonl"
                                )
                            }
                        )
                    )
                    cases.extend(
                        (
                            cfg.stem + "__" + c.trait_id + "__" + orientation,
                            preview_prompt(
                                current, pair_uid=f"demo_{c.trait_id}_{orientation}"
                            ),
                        )
                        for c in spec.criteria
                    )
            for name, rendered in cases:
                snapshot = snapshots[(kind, name)]
                text = (root / "rendered_samples" / (name + ".txt")).read_bytes()
                schema = (
                    root / "rendered_samples" / (name + ".schema.json")
                ).read_bytes()
                if (
                    text != rendered.text.encode()
                    or text != snapshot["text"].encode()
                    or schema != snapshot["schema_text"].encode()
                    or json.loads(schema) != rendered.output_schema
                ):
                    raise ValueError("Original study preview drift")
                matched += 1
    if matched != 312 or len(snapshots) != 312:
        raise ValueError("Incomplete original study previews")
    return {
        "manifests_verified": 2,
        "prompt_schema_pairs_matched": matched,
        "drift": [],
    }
