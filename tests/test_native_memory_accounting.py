"""Fresh workload peaks exclude parent pre-exec memory and retain child peaks."""

from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

FIXTURE = (
    Path(__file__).resolve().parents[1] / "scripts" / "preparation_memory_fixture.py"
)
MIB = 1024 * 1024

_CHILD = r"""
import gc
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]).parent))
sys.path.insert(0, str(Path(sys.argv[1]).parent.parent / "src"))
from preparation_memory_fixture import native_peak_bytes
before = native_peak_bytes()
allocation = bytearray(64 * 1024 * 1024)
for position in range(0, len(allocation), 4096):
    allocation[position] = 1
while_live = native_peak_bytes()
del allocation
gc.collect()
after_release = native_peak_bytes()
print(json.dumps(dict(before=before, while_live=while_live, after_release=after_release)))
"""

_PARENT = r'''
import json
import resource
import subprocess
import sys
allocation = bytearray(300 * 1024 * 1024)
for position in range(0, len(allocation), 4096):
    allocation[position] = 1
child = r"""
import json
import resource
import sys
from pathlib import Path
sys.path.insert(0, str(Path(sys.argv[1]).parent))
sys.path.insert(0, str(Path(sys.argv[1]).parent.parent / "src"))
from preparation_memory_fixture import native_peak_bytes
print(json.dumps(dict(native_peak_bytes=native_peak_bytes(), ru_maxrss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024)))
"""
result = subprocess.run([sys.executable, "-c", child, sys.argv[1]], capture_output=True, text=True, check=True, timeout=30)
print(json.dumps(dict(parent_ru_maxrss_bytes=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024, child=json.loads(result.stdout))))
'''


def _run(script: str) -> dict[str, Any]:
    result = subprocess.run(
        [sys.executable, "-c", script, str(FIXTURE)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert not result.stderr
    return cast(dict[str, Any], json.loads(result.stdout))


@pytest.mark.resource
def test_child_native_peak_keeps_transient_touched_allocation(tmp_path: Path) -> None:
    result = _run(_CHILD)
    (tmp_path / "transient-peak-measurement.json").write_text(
        json.dumps(result), encoding="utf-8"
    )
    assert result["while_live"] >= result["before"] + 56 * MIB, result
    assert result["after_release"] >= result["while_live"], result


@pytest.mark.resource
@pytest.mark.skipif(
    sys.platform != "linux", reason="Linux pre-exec RSS accounting regression"
)
def test_linux_fresh_exec_peak_excludes_large_parent(tmp_path: Path) -> None:
    result = _run(_PARENT)
    (tmp_path / "parent-child-measurement.json").write_text(
        json.dumps(result), encoding="utf-8"
    )
    assert result["parent_ru_maxrss_bytes"] >= 300 * MIB, result
    # This is a tiny fresh interpreter, not a 300-MiB workload. Its real peak
    # must be below the unchanged 256-MiB workload gates, with ample headroom.
    assert 0 < result["child"]["native_peak_bytes"] < 128 * MIB, result


@pytest.mark.skipif(sys.platform != "linux", reason="Linux procfs parser admission")
@pytest.mark.parametrize(
    "status",
    ["", "VmHWM: 0 kB\n", "VmHWM: -1 kB\n", "VmHWM: 42 B\n", "VmHWM: unknown kB\n"],
)
def test_linux_missing_or_malformed_peak_fails_closed(
    monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    specification = importlib.util.spec_from_file_location(
        "_accounting_fixture", FIXTURE
    )
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    native_peak_bytes = cast(Callable[[], int], module.native_peak_bytes)

    def status_file(*_args: object, **_kwargs: object) -> io.StringIO:
        return io.StringIO(status)

    monkeypatch.setattr(Path, "open", status_file)
    with pytest.raises(OSError, match="native process memory measurement failed"):
        native_peak_bytes()
