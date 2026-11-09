"""Byte-immutable synthetic fixture artifacts shared by verified readers."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path


def snapshot(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


@contextmanager
def immutable_exports(root: Path, generate: Callable[[Path], None]) -> Generator[Path]:
    """Share only artifacts whose readers leave the complete tree unchanged."""
    generate(root)
    before = snapshot(root)
    try:
        yield root
    finally:
        assert snapshot(root) == before, "A reader mutated shared R fixture artifacts"
