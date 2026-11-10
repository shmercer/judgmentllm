"""Hard process exit at actual SDK submission and durable response boundaries."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_gemini_lifecycle import setup_case

_CHILD = r"""
import asyncio, json, os, socket, sys
from pathlib import Path
import httpx2
from judgmentllm.config import load_run_spec
from judgmentllm.execution import execute_run, recover_run
from judgmentllm.providers.gemini import GeminiAdapter
store, config, accepted, boundary, mode = sys.argv[1:]
os.environ['GOOGLE_API_KEY'] = 'SYNTHETIC_CHILD_ONLY'
os.environ['GEMINI_API_KEY'] = 'SYNTHETIC_SECONDARY_CHILD_ONLY'
def forbidden(*args, **kwargs):
    raise AssertionError('Real network access forbidden')
socket.getaddrinfo = socket.create_connection = forbidden
spec = load_run_spec(config)
def respond(request):
    if mode == 'recover':
        raise AssertionError('Recovery must never forward a replacement request')
    with Path(accepted).open('ab', buffering=0) as output:
        output.write(b'accepted\n')
        os.fsync(output.fileno())
    if boundary == 'physical_forward':
        os._exit(73)
    assessment = json.dumps({'scores': [{'trait_id': trait.trait_id, 'category_label': trait.levels[0].label} for trait in spec.rubric.traits]})
    return httpx2.Response(200, json={'responseId':'synthetic-owned-process', 'modelVersion':spec.model, 'candidates':[{'index':0,'finishReason':'STOP','content':{'role':'model','parts':[{'text':assessment}]}}], 'usageMetadata':{'promptTokenCount':7,'candidatesTokenCount':9}})
def fault(stage, request):
    if stage == boundary:
        os._exit(73)
async def main():
    adapter = GeminiAdapter('models.generate_content', spec.model, http_transport=httpx2.MockTransport(respond))
    try:
        report = await (recover_run if mode == 'recover' else execute_run)('001', store=store, spec=spec, adapter=adapter, fault=fault if mode != 'recover' else None)
        print(json.dumps({'report':json.loads(report.model_dump_json()), 'calls':adapter.actual_call_count}))
    finally:
        await adapter.aclose()
asyncio.run(main())
"""


@pytest.mark.parametrize(
    "boundary,recoverable",
    [
        ("physical_forward", False),
        ("after_acknowledgment_receipt", False),
        ("after_evidence_receipt", True),
        ("before_validation", True),
    ],
)
def test_hard_exit_recovery_never_duplicates_physical_submission(
    tmp_path: Path, boundary: str, recoverable: bool
) -> None:
    spec, store, _, bodies = setup_case(tmp_path, "models.generate_content")
    config = tmp_path / "configuration.json"
    config.write_text(spec.model_dump_json(), encoding="utf-8")
    accepted = tmp_path / "independent-service-acceptances"
    arguments = [
        sys.executable,
        "-B",
        "-c",
        _CHILD,
        str(store),
        str(config),
        str(accepted),
        boundary,
    ]
    environment = {
        key: os.environ[key]
        for key in ("PATH", "HOME", "SystemRoot", "WINDIR", "TEMP", "TMP", "LANG")
        if key in os.environ
    }
    first = subprocess.run(
        [*arguments, "execute"],
        capture_output=True,
        text=True,
        timeout=30,
        env=environment,
    )
    assert first.returncode == 73, first.stderr
    assert accepted.read_bytes() == b"accepted\n" and not bodies
    resumed = subprocess.run(
        [*arguments, "recover"],
        capture_output=True,
        text=True,
        timeout=30,
        env=environment,
        check=True,
    )
    result = json.loads(resumed.stdout)
    assert result["calls"] == 0
    assert result["report"]["recovery_required"] is not recoverable
    assert accepted.read_bytes() == b"accepted\n"
    again = subprocess.run(
        [*arguments, "recover"],
        capture_output=True,
        text=True,
        timeout=30,
        env=environment,
        check=True,
    )
    assert json.loads(again.stdout)["calls"] == 0
    assert accepted.read_bytes() == b"accepted\n"
