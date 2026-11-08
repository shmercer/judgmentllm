"""Fresh-process native interruption and independently opened/copy recovery."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Literal

import pytest
from native_transport_fixture import NativeFixtureAdapter
from test_native_transport_integration import offline as offline
from test_native_transport_integration import prepared_case

from judgmentllm.execution import execute_run
from judgmentllm.persistence.repository import ReadOnlyRepository

_CHILD = r"""
import asyncio, builtins, io, json, os, socket, sys
from pathlib import Path
real_import = builtins.__import__
real_getitem = type(os.environ).__getitem__
real_connect = socket.socket.connect
real_socketpair = socket.socketpair
real_open = io.open
selfpipe=False
def forbidden(*args, **kwargs):
    raise RuntimeError("fresh process remains offline")
def guarded_import(name, *args, **kwargs):
    if name.split('.')[0] in {'openai','anthropic','google','together','ollama','keyring'}:
        return forbidden()
    return real_import(name,*args,**kwargs)
def guarded_environment(self,key):
    if os.fsdecode(key) in {'OPENAI_API_KEY','ANTHROPIC_API_KEY','ANTHROPIC_AUTH_TOKEN','GOOGLE_API_KEY','GEMINI_API_KEY','GOOGLE_APPLICATION_CREDENTIALS','TOGETHER_API_KEY','OLLAMA_API_KEY'}:
        return forbidden()
    return real_getitem(self,key)
builtins.__import__=guarded_import
type(os.environ).__getitem__=guarded_environment
socket.create_connection=forbidden
socket.getaddrinfo=forbidden
def guarded_connect(current,address):
    if selfpipe and isinstance(address,tuple) and address[0] in {'127.0.0.1','::1'}:
        return real_connect(current,address)
    return forbidden()
def local_socketpair(*args,**kwargs):
    global selfpipe
    selfpipe=True
    try:return real_socketpair(*args,**kwargs)
    finally:selfpipe=False
def guarded_open(file,*args,**kwargs):
    if isinstance(file,(str,bytes,os.PathLike)) and os.fsdecode(file).replace('\\','/').rsplit('/',1)[-1] in {'.env','.netrc','credentials.json','application_default_credentials.json','service_account.json'}:
        return forbidden()
    return real_open(file,*args,**kwargs)
socket.socket.connect=guarded_connect
socket.socketpair=local_socketpair
io.open=guarded_open
for operation in (lambda:socket.getaddrinfo('metadata.google.internal',80),lambda:__import__('openai'),lambda:os.environ['OPENAI_API_KEY']):
    try: operation()
    except RuntimeError: pass
    else: raise AssertionError('offline guard ineffective')
from native_transport_fixture import NativeFixtureAdapter
from judgmentllm.execution import execute_run
store=Path(sys.argv[1]);stage=sys.argv[2]
adapter=NativeFixtureAdapter(store/'synthetic-service.sqlite3')
def fault(current,uid):
    if current=='after_submit' and stage=='interrupt':
        os._exit(79)
report=asyncio.run(execute_run('001',store=store,adapter=adapter,fault=fault))
assert report.summary.successful_results==2
assert adapter.ledger.acceptance_count()==1
assert adapter.submit_calls==0
print('original native transport recovered in fresh process')
"""


@pytest.mark.parametrize("capture", ["full", "minimal"])
def test_interrupted_acceptance_recovers_in_fresh_process_without_resend(
    tmp_path: Path, capture: Literal["full", "minimal"]
) -> None:
    _, store, _ = prepared_case(tmp_path, capture=capture)
    tests = Path(__file__).resolve().parent
    environment = {
        key: os.environ[key]
        for key in ("PATH", "PYTHONPATH", "SystemRoot", "TEMP", "TMP")
        if key in os.environ
    }
    # Preserve the candidate package location and expose fixture helpers in both
    # installed qualification environments and editable development checkouts.
    environment["PYTHONPATH"] = str(tests) + (
        os.pathsep + environment["PYTHONPATH"] if environment.get("PYTHONPATH") else ""
    )
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    first = subprocess.run(
        [sys.executable, "-B", "-c", _CHILD, str(store), "interrupt"],
        env=environment,
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert first.returncode == 79, (
        "synthetic child did not reach durable acceptance boundary"
    )
    second = subprocess.run(
        [sys.executable, "-B", "-c", _CHILD, str(store), "recover"],
        env=environment,
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert second.returncode == 0, (
        "fresh process did not verify original native request"
    )
    assert "original native transport recovered" in second.stdout
    with ReadOnlyRepository(store) as repository:
        assert len(tuple(repository.iter_attempts("001"))) == 1
        assert len(tuple(repository.iter_results("001"))) == 2
        assert len(tuple(repository.iter_submissions("001"))) == 1


@pytest.mark.asyncio
async def test_full_capture_copied_repository_uses_original_authorized_snapshots(
    tmp_path: Path,
) -> None:
    _, store, _ = prepared_case(tmp_path, capture="full")
    copied = tmp_path / "fresh-repository"
    shutil.copytree(store, copied)
    shutil.rmtree(tmp_path / "inputs")
    adapter = NativeFixtureAdapter(copied / "synthetic-service.sqlite3")
    report = await execute_run(
        "001", store=copied, adapter=adapter, use_frozen_snapshot=True
    )
    assert report.summary.successful_results == 2
    assert adapter.submit_calls == adapter.ledger.acceptance_count() == 1
