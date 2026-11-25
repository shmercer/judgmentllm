"""Independent persistent synthetic service, exercised through the real SDK."""

from __future__ import annotations

import ctypes
import importlib.util
import json
import os
import stat
import subprocess
import sys
from os import lstat
from pathlib import Path, PureWindowsPath
from typing import Any

from test_native_batch import batch_spec

from judgmentllm.config import validate_run_spec


def specification(
    directory: Any,
    *,
    endpoint: Any = "responses",
    capture: Any = "full",
    responses: Any = 2,
    traits: Any = 1,
    explanations: Any = False,
) -> Any:
    data = batch_spec(
        directory,
        capture=capture,
        responses=responses,
        traits=traits,
        grouped=traits > 1,
    ).model_dump(mode="json")
    data.update(
        schema_version=3,
        provider="openai",
        endpoint=endpoint,
        model="gpt-6-luna",
        account_scope="synthetic-account",
    )
    data["prompt"].update(
        structured_output="text",
        preset="rubric_scores_with_explanations"
        if explanations
        else "rubric_scores_only",
    )
    data["execution"]["explanations"] = explanations
    return validate_run_spec(data)


_fixture_spec = importlib.util.spec_from_file_location(
    "_openai_batch_service_fixture",
    Path(__file__).resolve().parents[1] / "scripts" / "openai_batch_fixture.py",
)
assert _fixture_spec is not None and _fixture_spec.loader is not None
_fixture: Any = importlib.util.module_from_spec(_fixture_spec)
_fixture_spec.loader.exec_module(_fixture)
Service = _fixture.Service
install = _fixture.install


def child(
    root: Any,
    action: Any,
    *,
    options: Any = None,
    fault: Any = None,
    small_shards: Any = None,
    timeout: float = 180,
) -> Any:
    scratch = root / ("child-" + str(len(list(root.glob("child-*")))))
    scratch.mkdir()
    environment = {
        "PATH": os.defpath,
        "PYTHONPATH": os.pathsep.join(
            [
                str(Path(__file__).resolve().parents[1] / "src"),
                str(Path(__file__).resolve().parent),
            ]
        ),
        "TMPDIR": str(scratch),
        "TMP": str(scratch),
        "TEMP": str(scratch),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    if sys.platform == "win32" and (system_root := os.environ.get("SystemRoot")):
        windows_root = PureWindowsPath(system_root)
        if (
            system_root.strip()
            and "\0" not in system_root
            and windows_root.is_absolute()
            and len(windows_root.drive) == 2
            and windows_root.drive[0]
            in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
            and windows_root.drive[1] == ":"
            and not any(
                part == ".." or ":" in part or PureWindowsPath(part).is_reserved()
                for part in windows_root.parts[1:]
            )
        ):
            try:
                drive_type = getattr(ctypes, "windll").kernel32.GetDriveTypeW
                if drive_type(windows_root.anchor) in (2, 3, 5, 6):
                    for candidate in (
                        *reversed(windows_root.parents),
                        windows_root,
                    ):
                        info = lstat(str(candidate))
                        if not stat.S_ISDIR(info.st_mode) or getattr(
                            info,
                            "st_file_attributes",
                            stat.FILE_ATTRIBUTE_REPARSE_POINT,
                        ) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                            break
                    else:
                        environment["SystemRoot"] = system_root
            except (AttributeError, OSError, ValueError):
                pass
    command = [
        sys.executable,
        "-B",
        str(Path(__file__).resolve()),
        str(root),
        action,
        json.dumps(options or {}),
        fault or "",
        str(small_shards or ""),
    ]
    result = subprocess.run(
        command,
        env=environment,
        cwd=scratch,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if fault is not None:
        assert result.returncode == 73, result.stderr
        return None
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


if __name__ == "__main__":
    from pytest import MonkeyPatch

    from judgmentllm import native_batch
    from judgmentllm.batch_models import BatchControlOptionsV1
    from judgmentllm.config import load_run_spec

    root, action, options, fault, small = sys.argv[1:]
    root = Path(root)
    patch = MonkeyPatch()
    service = Service(root / "service.sqlite")
    install(patch, service)
    if service.get("byte_limit") is not None:
        from judgmentllm.execution.native_batch import InvocationBudget

        def byte_limit(_budget: Any) -> int:
            return int(service.get("byte_limit"))

        patch.setattr(InvocationBudget, "byte_limit", property(byte_limit))
    if service.get("expire_after_metadata"):
        import asyncio

        from judgmentllm.providers.openai_batch import OpenAIBatchAdapter

        original_metadata = OpenAIBatchAdapter.file_metadata

        async def expired_metadata(self: Any, *args: Any, **kwargs: Any) -> Any:
            result = await original_metadata(self, *args, **kwargs)
            self.budget.deadline = asyncio.get_running_loop().time() - 1
            return result

        patch.setattr(OpenAIBatchAdapter, "file_metadata", expired_metadata)
    if small:
        from judgmentllm import openai_batch_transport
        from judgmentllm.batch_models import BatchLimitsV1
        from judgmentllm.providers import openai_batch

        original_context = openai_batch_transport.batch_context

        def synthetic_context(spec: Any) -> Any:
            return original_context(spec).model_copy(
                update={"limits": BatchLimitsV1(max_members=int(small))}
            )

        patch.setattr(openai_batch_transport, "batch_context", synthetic_context)
        patch.setattr(openai_batch, "batch_context", synthetic_context)
    if service.get("expire_before_selection"):
        import asyncio

        from judgmentllm.execution.openai_batch import OpenAIBatchSession

        original_session = OpenAIBatchSession.__init__

        def expired(self: Any, *args: Any, **kwargs: Any) -> None:
            original_session(self, *args, **kwargs)
            self.budget.deadline = asyncio.get_running_loop().time() - 1

        patch.setattr(OpenAIBatchSession, "__init__", expired)
    spec = load_run_spec(root / "spec.json")
    if action == "prepare":
        result = native_batch.prepare_batch(spec, store=root / "store", run_id="001")
    else:

        def checkpoint(stage: Any, _identity: Any) -> Any:
            if stage == fault:
                os._exit(73)

        result = getattr(native_batch, action + "_batch")(
            "001",
            store=root / "store",
            spec=spec,
            options=BatchControlOptionsV1(**json.loads(options)),
            _fault=checkpoint,
        )
    print(result.model_dump_json())
