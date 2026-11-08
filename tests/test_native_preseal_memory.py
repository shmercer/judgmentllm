"""Fresh-process native pre-seal memory stays bounded as request counts grow."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import cast

from test_native_transport_integration import offline as offline

_SCRIPT = r"""
import builtins, gc, io, json, os, socket, sys, time, tracemalloc
from pathlib import Path
original_import = builtins.__import__
def forbidden(*args, **kwargs):
    raise RuntimeError("native memory fixture blocked offline")
def guarded_import(name, *args, **kwargs):
    if name.split(".")[0] in {"openai", "anthropic", "google", "together", "ollama", "keyring"}:
        return forbidden()
    return original_import(name, *args, **kwargs)
builtins.__import__ = guarded_import
socket.create_connection = forbidden
socket.getaddrinfo = forbidden
original_socket = socket.socket
class GuardedSocket(original_socket):
    def __init__(self, family=socket.AF_INET, *args, **kwargs):
        if family in (socket.AF_INET, socket.AF_INET6): forbidden()
        super().__init__(family, *args, **kwargs)
socket.socket = GuardedSocket
original_environment = type(os.environ).__getitem__
def guarded_environment(environment, key):
    if os.fsdecode(key) in {"OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY", "TOGETHER_API_KEY", "TYPESAFE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS"}:
        return forbidden()
    return original_environment(environment, key)
type(os.environ).__getitem__ = guarded_environment
original_open, original_io_open = builtins.open, io.open
def private_path(path):
    if isinstance(path, (str, bytes, os.PathLike)):
        normalized = os.fsdecode(path).replace("\\", "/").lower()
        return any(marker in normalized for marker in ("/.aws/", "/.config/gcloud/", "application_default_credentials.json", "/.netrc", "/.env"))
    return False
def guarded_open(path, *args, **kwargs):
    if private_path(path): return forbidden()
    return original_open(path, *args, **kwargs)
def guarded_io_open(path, *args, **kwargs):
    if private_path(path): return forbidden()
    return original_io_open(path, *args, **kwargs)
builtins.open, io.open = guarded_open, guarded_io_open
for probe in (lambda: __import__("openai"), lambda: socket.socket(), lambda: socket.getaddrinfo("metadata.google.internal", 80), lambda: os.environ["OPENAI_API_KEY"], lambda: open(Path.home() / ".aws" / "credentials")):
    try: probe()
    except RuntimeError: pass
    else: raise RuntimeError("offline guard did not reject a prohibited operation")
from native_transport_fixture import MECHANISMS, context_for, specification
from judgmentllm.preparation import prepare_run
sys.path.insert(0, str(Path(sys.argv[3]) / "scripts"))
from preparation_memory_fixture import native_peak_bytes
root = Path(sys.argv[1]); count = int(sys.argv[2])
source = specification(root / "inputs", MECHANISMS[0], capture="minimal", traits=1, grouping="independent")
document = json.loads(source.read_text(encoding="utf-8"))
responses = Path(document["input"])
with responses.open("w", encoding="utf-8") as stream:
    for index in range(count):
        stream.write(json.dumps({"item_id":f"{index:06d}","text":"x" * 4096}) + "\n")
context = context_for(MECHANISMS[0])
started = time.perf_counter()
tracemalloc.start()
# CPython versions parse/intern path metadata differently. Force leaf parsing
# and explicitly initialize the bounded intern table while tracing, so later
# compaction frees and allocations are both tracked. No request content or
# preparation is warmed by this fixed metadata-only setup.
for startup_index in range(32768):
    startup_path = Path("/judgmentllm-resource-startup/objects") / f"{startup_index:064x}"
    sys.intern(startup_path.name)
del startup_path, startup_index
baseline_current = tracemalloc.get_traced_memory()[0]
tracemalloc.reset_peak()
with prepare_run(source, store=root / "runtime", run_id="memory", transport_context=context) as prepared:
    expected = count
    assert prepared.manifest.initial_transport_count == expected
    assert prepared.repository.summary("memory").attempts == expected
    assert not tuple(prepared.repository.iter_submissions("memory"))
    raw_peak = tracemalloc.get_traced_memory()[1]
    raw_live = tracemalloc.get_traced_memory()[0]
    peak = raw_peak - baseline_current
    live = raw_live - baseline_current
del prepared; gc.collect()
raw_remaining = tracemalloc.get_traced_memory()[0]
remaining = raw_remaining - baseline_current
tracemalloc.stop()
print(json.dumps({"requests":expected, "python_peak_bytes":peak, "python_live_bytes":live,
    "python_after_gc_bytes":remaining, "native_peak_bytes":native_peak_bytes(),
    "python_baseline_bytes":baseline_current, "python_raw_peak_bytes":raw_peak,
    "python_raw_live_bytes":raw_live, "python_raw_after_gc_bytes":raw_remaining,
    "elapsed_ms":int((time.perf_counter() - started) * 1000)}))
"""


def measurement(directory: Path, count: int) -> dict[str, int]:
    root = Path(__file__).resolve().parents[1]
    # Copy only known process/runtime settings; never enumerate or read ambient
    # provider credentials while preparing a supposedly offline subprocess.
    allowed = (
        "PATH",
        "SystemRoot",
        "SYSTEMROOT",
        "WINDIR",
        "HOME",
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
        "TEMP",
        "TMP",
        "TMPDIR",
        "LANG",
        "LC_ALL",
        "PYTHONIOENCODING",
        "PYTHONUTF8",
        "COVERAGE_PROCESS_START",
        "COVERAGE_FILE",
        "COV_CORE_SOURCE",
        "COV_CORE_CONFIG",
        "COV_CORE_DATAFILE",
        "COV_CORE_BRANCH",
        "COV_CORE_CONTEXT",
    )
    environment = {key: os.environ[key] for key in allowed if key in os.environ}
    environment["PYTHONPATH"] = os.pathsep.join(
        (str(root / "src"), str(root / "tests"))
    )
    completed = subprocess.run(
        [sys.executable, "-c", _SCRIPT, str(directory), str(count), str(root)],
        env=environment,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert completed.returncode == 0, completed.stderr
    measured = cast(dict[str, int], json.loads(completed.stdout))
    (directory / "measurements.json").write_text(
        json.dumps(measured, indent=2), encoding="utf-8"
    )
    return measured


def test_native_preseal_request_product_uses_bounded_memory(tmp_path: Path) -> None:
    small = measurement(tmp_path / "small", 32)
    large = measurement(tmp_path / "large", 256)
    mib = 1024 * 1024
    assert large["requests"] == 8 * small["requests"]
    for result in (small, large):
        assert 0 < result["native_peak_bytes"] < 256 * mib
        assert result["python_raw_peak_bytes"] < 32 * mib
        assert result["python_peak_bytes"] < 32 * mib
        assert result["python_after_gc_bytes"] < result["python_peak_bytes"]
        assert (
            result["python_peak_bytes"]
            == result["python_raw_peak_bytes"] - result["python_baseline_bytes"]
        )
        assert (
            result["python_after_gc_bytes"]
            == result["python_raw_after_gc_bytes"] - result["python_baseline_bytes"]
        )
    assert large["python_peak_bytes"] < small["python_peak_bytes"] + 3 * mib, (
        small,
        large,
    )
    assert large["native_peak_bytes"] < small["native_peak_bytes"] + 48 * mib, (
        small,
        large,
    )
    assert large["python_after_gc_bytes"] < small["python_after_gc_bytes"] + mib, (
        small,
        large,
    )

    # Keep the original raw allocation growth budgets alongside baseline deltas.
    assert large["python_raw_peak_bytes"] < small["python_raw_peak_bytes"] + 3 * mib, (
        small,
        large,
    )
    assert (
        large["python_raw_after_gc_bytes"] < small["python_raw_after_gc_bytes"] + mib
    ), (small, large)
