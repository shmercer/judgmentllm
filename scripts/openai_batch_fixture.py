"""Synthetic SDK injection/inputs for the installed R CLI gate; no lifecycle calls.

R initiates every command. The dedicated CLI process runs its installed console
with actual SDK MockTransport and a synthetic resolver; no HTTP server or public
transport/configuration escape hatch is introduced.
"""

from __future__ import annotations

import asyncio
import json
import os
import runpy
import socket
import sqlite3
import sys
from collections.abc import AsyncIterator, Generator
from contextlib import ExitStack, contextmanager
from email.parser import BytesParser
from email.policy import default
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import httpx2

from judgmentllm.persistence.repository import ReadOnlyRepository


def envelope(endpoint: str, output: str) -> dict[str, Any]:
    if endpoint == "responses":
        return {
            "id": "resp_offline",
            "object": "response",
            "created_at": 1,
            "status": "completed",
            "model": "synthetic-reported-model",
            "output": [
                {
                    "id": "msg_offline",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {"type": "output_text", "text": output, "annotations": []}
                    ],
                }
            ],
            "usage": {
                "input_tokens": 20,
                "output_tokens": 10,
                "total_tokens": 30,
                "input_tokens_details": {"cached_tokens": 3},
                "output_tokens_details": {"reasoning_tokens": 2},
            },
        }
    return {
        "id": "chatcmpl_offline",
        "object": "chat.completion",
        "created": 1,
        "model": "synthetic-reported-model",
        "choices": [
            {
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": output, "refusal": None},
            }
        ],
        "usage": {
            "prompt_tokens": 20,
            "completion_tokens": 10,
            "total_tokens": 30,
            "prompt_tokens_details": {"cached_tokens": 3},
            "completion_tokens_details": {"reasoning_tokens": 2},
        },
    }


class Service:
    """Provider-owned SQLite ledger; never derived from client submission state."""

    def __init__(self, path: Path) -> None:
        self.path = path
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS files(id TEXT PRIMARY KEY,purpose TEXT,filename TEXT,body BLOB,declared INTEGER);
                CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,body TEXT);
                CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY,method TEXT,path TEXT);
                CREATE TABLE IF NOT EXISTS reads(file TEXT,byte_count INTEGER);
                CREATE TABLE IF NOT EXISTS outputs(member TEXT PRIMARY KEY,text TEXT);
                CREATE TABLE IF NOT EXISTS controls(name TEXT PRIMARY KEY,value TEXT);
            """)

    @contextmanager
    def connect(self) -> Generator[sqlite3.Connection]:
        db = sqlite3.connect(self.path)
        try:
            with db:
                yield db
        finally:
            db.close()

    def set(self, name: Any, value: Any) -> Any:
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO controls VALUES (?,?)",
                (name, json.dumps(value)),
            )

    def get(self, name: Any, default: Any = None) -> Any:
        with self.connect() as db:
            row = db.execute(
                "SELECT value FROM controls WHERE name=?", (name,)
            ).fetchone()
        return default if row is None else json.loads(row[0])

    def configure(self, store: Any, spec: Any, *, padding: Any = 0) -> Any:
        with ReadOnlyRepository(store) as repo, self.connect() as db:
            for key, attempt in repo.connection.execute(
                "SELECT member_key,attempt_id FROM batch_prepared_members ORDER BY position"
            ):
                tasks = [
                    repo.get_task("001", task)
                    for task in repo.attempt_task_ids("001", attempt)
                ]
                scores = [
                    {
                        "trait_id": task.trait_id,
                        "category_label": next(
                            t for t in spec.rubric.traits if t.trait_id == task.trait_id
                        )
                        .levels[0]
                        .label,
                    }
                    for task in tasks
                    if spec.task_kind == "rubric"
                ]
                if spec.execution.explanations:
                    for score in scores:
                        score["explanation"] = "Synthetic café " + "x" * padding
                output = (
                    {"winner_id": "essay_01"}
                    if spec.task_kind == "pairwise"
                    else {"scores": scores}
                )
                db.execute(
                    "INSERT OR REPLACE INTO outputs VALUES (?,?)",
                    (key, json.dumps(output, ensure_ascii=False)),
                )

    def events(self) -> Any:
        with self.connect() as db:
            return db.execute("SELECT method,path FROM events ORDER BY seq").fetchall()

    def file(self, file_id: Any, *, body: Any = None, declared: Any = None) -> Any:
        with self.connect() as db:
            if body is not None:
                db.execute("UPDATE files SET body=? WHERE id=?", (body, file_id))
            if declared is not None:
                db.execute(
                    "UPDATE files SET declared=? WHERE id=?", (declared, file_id)
                )
            return db.execute(
                "SELECT body,declared FROM files WHERE id=?", (file_id,)
            ).fetchone()

    def request(self, request: Any) -> Any:
        path = request.url.path
        with self.connect() as db:
            db.execute(
                "INSERT INTO events(method,path) VALUES (?,?)", (request.method, path)
            )
        if self.get("stall_poll") and path == "/v1/batches/" + self.get("stall_poll"):
            return httpx2.Response(503, stream=httpx2.ByteStream(b"{}"))
        if request.method == "POST" and path == "/v1/files":
            message = BytesParser(policy=default).parsebytes(
                b"Content-Type: "
                + request.headers["content-type"].encode()
                + b"\r\n\r\n"
                + request.content
            )
            part = next(
                p
                for p in message.iter_parts()
                if p.get_param("name", header="content-disposition") == "file"
            )
            body = part.get_payload(decode=True)
            with self.connect() as db:
                file_id = "file-input_" + str(
                    db.execute(
                        "SELECT count(*) FROM files WHERE purpose='batch'"
                    ).fetchone()[0]
                    + 1
                )
                db.execute(
                    "INSERT INTO files VALUES (?,?,?,?,?)",
                    (file_id, "batch", part.get_filename(), body, len(body)),
                )
            response = dict(
                id=file_id,
                object="file",
                filename=part.get_filename(),
                purpose="batch",
                bytes=len(body),
                created_at=1,
            )
            if self.get("lose_upload_ack"):
                raise OSError("synthetic lost upload acknowledgment")
        elif request.method == "POST" and path == "/v1/batches":
            values = json.loads(request.content)
            initial_status = self.get("initial_status", "completed")
            with self.connect() as db:
                ordinal = db.execute("SELECT count(*) FROM jobs").fetchone()[0] + 1
                job_id = "batch_" + str(ordinal)
                rows = (
                    db.execute(
                        "SELECT body FROM files WHERE id=?", (values["input_file_id"],)
                    )
                    .fetchone()[0]
                    .splitlines()
                )
                output: list[bytes] = []
                for line in rows:
                    member = json.loads(line)
                    text = db.execute(
                        "SELECT text FROM outputs WHERE member=?",
                        (member["custom_id"],),
                    ).fetchone()[0]
                    output.append(
                        json.dumps(
                            {
                                "id": "batch_req_" + member["custom_id"],
                                "custom_id": member["custom_id"],
                                "response": {
                                    "status_code": 200,
                                    "request_id": "req_offline",
                                    "body": envelope(
                                        "responses"
                                        if values["endpoint"] == "/v1/responses"
                                        else "chat.completions",
                                        text,
                                    ),
                                },
                                "error": None,
                            },
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ).encode()
                        + b"\n"
                    )
                output_data = b"".join(reversed(output))
                output_id, error_id = (
                    "file-output_" + str(ordinal),
                    "file-error_" + str(ordinal),
                )
                db.execute(
                    "INSERT INTO files VALUES (?,?,?,?,?)",
                    (
                        output_id,
                        "batch_output",
                        "output.jsonl",
                        output_data,
                        len(output_data),
                    ),
                )
                db.execute(
                    "INSERT INTO files VALUES (?,?,?,?,?)",
                    (error_id, "batch_output", "error.jsonl", b"", 0),
                )
                response = dict(
                    id=job_id,
                    object="batch",
                    endpoint=values["endpoint"],
                    input_file_id=values["input_file_id"],
                    metadata=values["metadata"],
                    status=initial_status,
                    output_file_id=output_id,
                    error_file_id=error_id,
                )
                db.execute(
                    "INSERT INTO jobs VALUES (?,?)", (job_id, json.dumps(response))
                )
            if self.get("lose_create_ack"):
                raise OSError("synthetic lost create acknowledgment")
        elif path.startswith("/v1/batches/"):
            job_id = path.split("/")[3]
            with self.connect() as db:
                response = json.loads(
                    db.execute(
                        "SELECT body FROM jobs WHERE id=?", (job_id,)
                    ).fetchone()[0]
                )
                if path.endswith("/cancel"):
                    response["status"] = "cancelled"
                    db.execute(
                        "UPDATE jobs SET body=? WHERE id=?",
                        (json.dumps(response), job_id),
                    )
            if path.endswith("/cancel") and self.get("lose_cancel_ack"):
                raise OSError("synthetic lost cancel acknowledgment")
        elif path == "/v1/batches":
            with self.connect() as db:
                response = {
                    "data": [
                        json.loads(r[0]) for r in db.execute("SELECT body FROM jobs")
                    ],
                    "has_more": False,
                    "object": "list",
                }
        elif path == "/v1/files":
            with self.connect() as db:
                response = {
                    "data": [
                        dict(id=r[0], purpose=r[1], filename=r[2], bytes=r[3])
                        for r in db.execute(
                            "SELECT id,purpose,filename,declared FROM files WHERE purpose='batch'"
                        )
                    ],
                    "has_more": False,
                    "object": "list",
                }
        else:
            file_id = path.split("/")[3]
            with self.connect() as db:
                row = db.execute(
                    "SELECT purpose,filename,body,declared FROM files WHERE id=?",
                    (file_id,),
                ).fetchone()
            if path.endswith("/content"):
                if self.get("stall_content") == file_id:
                    return httpx2.Response(503, stream=httpx2.ByteStream(b"{}"))
                assert "range" not in request.headers
                service = self
                raw = row[2]

                class Content(httpx2.AsyncByteStream):
                    async def __aiter__(self) -> AsyncIterator[bytes]:
                        step = service.get("fragment_bytes", 65_536)
                        for offset in range(0, len(raw), step):
                            await asyncio.sleep(service.get("content_delay", 0))
                            chunk = raw[offset : offset + step]
                            with service.connect() as db:
                                db.execute(
                                    "INSERT INTO reads VALUES (?,?)",
                                    (file_id, len(chunk)),
                                )
                            yield chunk
                        if service.get("eof_delay_file") == file_id:
                            await asyncio.sleep(service.get("eof_delay", 0))

                return httpx2.Response(
                    200,
                    stream=Content(),
                    headers={"content-type": "application/octet-stream"},
                )
            response = dict(
                id=file_id,
                object="file",
                purpose=row[0],
                filename=row[1],
                bytes=row[3],
                created_at=1,
            )
        return httpx2.Response(
            200,
            stream=httpx2.ByteStream(
                json.dumps(response, separators=(",", ":")).encode()
            ),
            headers={"content-type": "application/json"},
        )


def install(monkeypatch: Any, service: Any) -> Any:

    from judgmentllm.providers import openai_batch

    original = openai_batch.OpenAIBatchAdapter.__init__

    def initialize(self: Any, *args: Any, **kwargs: Any) -> None:
        kwargs["http_transport"] = httpx2.MockTransport(service.request)
        original(self, *args, **kwargs)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("external network is forbidden")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setenv("OPENAI_CUSTOM_HEADERS", "")

    def credentials(_provider: str) -> Any:
        return SimpleNamespace(_api_key=lambda: "synthetic-batch-key")

    monkeypatch.setattr(openai_batch, "_resolve_credentials", credentials)
    monkeypatch.setattr(openai_batch.OpenAIBatchAdapter, "__init__", initialize)


def create(root: Path) -> None:
    for name, endpoint, capture in (
        ("openai-batch-responses-full", "responses", "full"),
        ("openai-batch-chat-minimal", "chat.completions", "minimal"),
    ):
        directory = root / name
        directory.mkdir()
        source = directory / "responses.jsonl"
        source.write_text(
            '{"item_id":"001","text":"Synthetic first"}\n{"item_id":"002","text":"Synthetic second"}\n',
            encoding="utf-8",
        )
        config: dict[str, Any] = dict(
            schema_version=3,
            task_kind="rubric" if endpoint == "responses" else "pairwise",
            provider="openai",
            endpoint=endpoint,
            model="gpt-6-luna",
            account_scope="synthetic-account",
            capture=capture,
            input=str(source),
            execution={
                "mode": "batch_native",
                "cache_policy": "off",
                "grouping": "grouped" if endpoint == "responses" else "independent",
            },
            prompt={"structured_output": "text"},
        )
        if endpoint == "responses":
            config["rubric"] = {
                "rubric_id": "01",
                "name": "Synthetic",
                "traits": [
                    {
                        "trait_id": trait,
                        "name": "Quality",
                        "description": "Assess",
                        "levels": [
                            {"label": "001", "numeric_value": 1},
                            {"label": "middle", "numeric_value": 2},
                            {"label": "top", "numeric_value": 7},
                        ],
                    }
                    for trait in ("01", "02")
                ],
            }
        else:
            config["criteria"] = [
                {"trait_id": "01", "name": "Quality", "description": "Compare"}
            ]
            pairs = directory / "pairs.jsonl"
            pairs.write_text(
                "".join(
                    json.dumps(
                        dict(
                            pair_id="01",
                            pair_uid=uid,
                            left_item_id="001",
                            right_item_id="002",
                            trait_id="01",
                            presented_order=order,
                        )
                    )
                    + "\n"
                    for uid, order in (("01", ["002", "001"]), ("02", ["001", "002"]))
                ),
                encoding="utf-8",
            )
            config["pairs"] = str(pairs)
        (directory / "config.json").write_text(json.dumps(config), encoding="utf-8")
        Service(directory / "service.sqlite")


def main() -> None:
    command, path, *arguments = sys.argv[1:]
    root = Path(path)
    if command == "create":
        create(root)
    elif command == "configure":
        from judgmentllm.config import load_run_spec

        Service(root / "service.sqlite").configure(
            root / "store", load_run_spec(root / "config.json")
        )
    elif command == "cli":
        console, *options = arguments

        # This dedicated child never forwards ambient auth to the SDK.
        class Patches:
            def __init__(self, stack: ExitStack):
                self.stack = stack

            def setattr(self, obj: Any, name: str, value: Any) -> None:
                self.stack.enter_context(patch.object(obj, name, value))

            @staticmethod
            def setenv(name: str, value: str) -> None:
                os.environ[name] = value

        with ExitStack() as stack:
            install(Patches(stack), Service(root / "service.sqlite"))
            sys.argv = [console, *options]
            runpy.run_path(console, run_name="__main__")
    else:
        raise SystemExit("unsupported synthetic fixture command")


if __name__ == "__main__":
    main()
