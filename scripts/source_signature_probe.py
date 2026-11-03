"""Print only synthetic file metadata to diagnose platform stat differences.

No input paths, assessment text or credentials are read or printed. The Windows
CI jobs retain these observations alongside the actual integrity assertions.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

from judgmentllm.preparation.freezing import signature


def fields(value: os.stat_result) -> dict[str, int | None]:
    return {
        "device": value.st_dev,
        "inode": value.st_ino,
        "size": value.st_size,
        "mtime_ns": value.st_mtime_ns,
        "ctime_ns": value.st_ctime_ns,
        "birthtime_ns": getattr(value, "st_birthtime_ns", None),
    }


def main() -> None:
    observations: dict[str, object] = {"platform": sys.platform, "python": sys.version}
    with tempfile.TemporaryDirectory(prefix="judgmentllm-stat-probe-") as temporary:
        path = Path(temporary) / "synthetic.bin"
        path.write_bytes(b"ABCD")
        initial = path.stat()
        observations["path_before_open"] = fields(initial)
        with path.open("rb") as source:
            observations["normalized_signature"] = signature(path, source).model_dump()
            observations["path_while_open"] = fields(path.stat())
            observations["descriptor_while_open"] = fields(os.fstat(source.fileno()))
            with path.open("rb") as probe:
                observations["independent_descriptor"] = fields(
                    os.fstat(probe.fileno())
                )
            with path.open("r+b") as writer:
                writer.write(b"WXYZ")
                writer.flush()
                os.fsync(writer.fileno())
            os.utime(path, ns=(initial.st_atime_ns, initial.st_mtime_ns))
            observations["path_after_restored_mtime_edit"] = fields(path.stat())
            observations["descriptor_after_restored_mtime_edit"] = fields(
                os.fstat(source.fileno())
            )
            observations["normalized_signature_after_edit"] = signature(
                path, source
            ).model_dump()
        observations["path_after_close"] = fields(path.stat())
    print(json.dumps(observations, sort_keys=True))


if __name__ == "__main__":
    main()
