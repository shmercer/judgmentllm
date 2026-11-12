"""Guarded synthetic SDK/static probes, never clients or serving qualification."""

from __future__ import annotations

import importlib.metadata
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = r"""
import ast
import importlib.metadata
import importlib.util
import io
import json
import logging
import sys
import types
import warnings
from hashlib import sha256
from pathlib import Path

sys.dont_write_bytecode = True
settings = json.loads(sys.stdin.read())
sys.path.insert(0, settings["tests"])
from native_sdk_guard import install_guards
guard = install_guards()
provider = settings["provider"]
directory = Path(settings["fixtures"])
profile = json.loads((directory / "profile.json").read_bytes())
dist = importlib.metadata.distribution(provider)
assert dist.version == profile["sdk_version"]
for relative, expected in profile["sdk_sources"].items():
    source = Path(dist.locate_file(provider + "/" + relative)).read_bytes()
    assert sha256(source).hexdigest() == expected
    ast.parse(source)

logs = io.StringIO()
logging.getLogger().addHandler(logging.StreamHandler(logs))
with warnings.catch_warnings(record=True) as observed:
    if provider == "anthropic":
        import anthropic
        guard.protect_client(anthropic.Anthropic)
        guard.protect_client(anthropic.AsyncAnthropic)
        from anthropic.types.json_output_format_param import JSONOutputFormatParam
        from anthropic._constants import ANTHROPIC_API_VERSION
        assert ANTHROPIC_API_VERSION == profile["api_version"]
        assert set(JSONOutputFormatParam.__annotations__) == {"schema", "type"}
        format_types = ast.parse(Path(dist.locate_file("anthropic/types/json_output_format_param.py")).read_bytes())
        assert {node.target.id for node in ast.walk(format_types)
                if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
                and isinstance(node.annotation, ast.Subscript)
                and isinstance(node.annotation.value, ast.Name)
                and node.annotation.value.id == "Required"} == {"schema", "type"}
        messages = ast.parse(Path(dist.locate_file("anthropic/resources/messages/messages.py")).read_bytes())
        # Raw-schema create passes the configuration directly, unlike parse's
        # automatic transform helper. Inspect the actual SDK body expression.
        assert any(isinstance(node, ast.keyword) and node.arg == "body"
                   and isinstance(node.value, ast.Dict)
                   and any(isinstance(key, ast.Constant) and key.value == "output_config"
                           and isinstance(value, ast.Name) and value.id == "output_config"
                           for key, value in zip(node.value.keys, node.value.values))
                   for node in ast.walk(messages))
        def convert(fragment):
            return fragment
    elif provider == "together":
        # Root __init__ imports filelock, whose platform probe writes temporary
        # files. Isolate the genuine converter with a package-path stub instead
        # of permitting those writes or loading any client/resource initializer.
        assert "together" not in sys.modules
        package = types.ModuleType("together")
        package.__path__ = [str(dist.locate_file("together"))]
        sys.modules["together"] = package
        from together._utils import maybe_transform
        from together.types.chat.completion_create_params import CompletionCreateParamsNonStreaming
        assert "together._client" not in sys.modules and "together.lib" not in sys.modules
        def convert(fragment):
            return maybe_transform(fragment, CompletionCreateParamsNonStreaming)
    else:
        # The ordinary package initializer constructs a Client. Load only the
        # pinned model file under a private name, without executing __init__.py.
        assert "ollama" not in sys.modules
        source_path = Path(dist.locate_file("ollama/_types.py"))
        initializer = ast.parse(Path(dist.locate_file("ollama/__init__.py")).read_bytes())
        assert any(isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
                   and isinstance(node.value.func, ast.Name) and node.value.func.id == "Client"
                   for node in initializer.body)
        spec = importlib.util.spec_from_file_location("_native_probe_ollama_types", source_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        def convert(fragment):
            result = module.ChatRequest(model="synthetic-model", format=fragment["format"]).model_dump(exclude_none=True)
            return {"format": result["format"]}
        assert "ollama" not in sys.modules and "ollama._client" not in sys.modules
    guard.assert_intact()
    previous = guard.events
    records = (directory / "projections.jsonl").read_bytes().splitlines()
    assert len(records) == 16
    for raw in records:
        record = json.loads(raw)
        fragment = json.loads(record["sdk_config_fragment_text"])
        converted = convert(fragment)
        exact = json.dumps(converted, ensure_ascii=False, separators=(",", ":"))
        assert exact == record["format_fragment_text"]
        assert sha256(exact.encode()).hexdigest() == record["format_fragment_sha256"]
        assert "description" not in exact
        assert "synthetic-private-native-v2-canary" not in exact
    assert previous == guard.events
    guard.assert_intact()
    assert not observed
    assert not logs.getvalue()
assert len(guard.checked) >= 8
print("sixteen offline SDK representations verified")
"""


# The genuine Anthropic import/AST probe takes 22–24s on Windows 3.11 before
# contention. Keep its fixed 30s deadline isolated in the serial correctness gate.
@pytest.mark.parametrize(
    "provider",
    [pytest.param("anthropic", marks=pytest.mark.serial), "together", "ollama"],
)
def test_pinned_sdk_preserves_sixteen_fragments_under_offline_guards(
    provider: str, tmp_path: Path
) -> None:
    try:
        importlib.metadata.version(provider)
    except importlib.metadata.PackageNotFoundError:
        if os.environ.get("JUDGMENTLLM_REQUIRE_NATIVE_SDK_PROBES") == "1":
            pytest.fail("Required pinned SDK converter probe dependency missing")
        pytest.skip(
            "Optional pinned SDK unavailable; all-extras qualification requires this probe"
        )
    tests = Path(__file__).resolve().parent
    environment = {
        key: value
        for key, value in os.environ.items()
        if key
        not in {
            "ANTHROPIC_API_KEY",
            "TOGETHER_API_KEY",
            "OLLAMA_API_KEY",
            "GOOGLE_API_KEY",
            "GEMINI_API_KEY",
            "OPENAI_API_KEY",
            "GOOGLE_APPLICATION_CREDENTIALS",
        }
    }
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    process = subprocess.run(
        [sys.executable, "-B", "-c", _SCRIPT],
        input=json.dumps(
            {
                "provider": provider,
                "tests": str(tests),
                "fixtures": str(tests / "fixtures" / ("native_" + provider)),
            }
        ),
        text=True,
        capture_output=True,
        env=environment,
        cwd=tmp_path,
        timeout=30,
        check=False,
    )
    assert process.returncode == 0, process.stderr
    assert process.stdout == "sixteen offline SDK representations verified\n"
    assert process.stderr == ""
    assert list(tmp_path.iterdir()) == []
