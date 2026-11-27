"""Persistent synthetic Message Batches served only through SDK MockTransport."""

from __future__ import annotations

import asyncio
import json
import runpy
import socket
import sqlite3
import sys
from collections.abc import AsyncIterator, Generator
from contextlib import closing, contextmanager, nullcontext
from inspect import currentframe
from pathlib import Path
from threading import local
from typing import Any, NoReturn
from unittest.mock import patch

import httpx2

from judgmentllm import native_batch
from judgmentllm.anthropic_batch_transport import BATCH_ORIGIN, BATCH_PATH
from judgmentllm.batch_models import AnthropicBatchContextV1
from judgmentllm.providers.anthropic_batch import AnthropicBatchAdapter

PRIVATE = "synthetic-assessment-private-canary"
SECRET = "synthetic-anthropic-batch-key"
REJECTION = json.dumps(
    {
        "type": "error",
        "request_id": "req_fixture",
        "error": {"type": "invalid_request_error", "message": f"{SECRET} {PRIVATE}"},
    },
    separators=(",", ":"),
).encode()


class Parts(httpx2.AsyncByteStream):
    def __init__(
        self,
        service: Service,
        parts: list[bytes],
        interrupt: bool,
        path: str = "/results",
        *,
        stall: bool = False,
    ):
        self.service, self.parts, self.interrupt = service, parts, interrupt
        self.path = path
        self.stall = stall

    async def __aiter__(self) -> AsyncIterator[bytes]:
        if self.path == "/results" and self.service.get("track_result_reads"):
            self.service.record("READ", self.path)
        for part in self.parts:
            yield part
        if self.interrupt:
            raise httpx2.ReadError("synthetic interrupted read")
        if self.stall or (self.path == "/results" and self.service.get("stall_eof")):
            await asyncio.Event().wait()

    async def aclose(self) -> None:
        self.service.record("CLOSE", self.path)


class Service:
    def __init__(self, path: Path):
        self.path = path
        with closing(sqlite3.connect(path)) as db, db:
            db.executescript(
                "CREATE TABLE IF NOT EXISTS jobs (id INTEGER PRIMARY KEY,body BLOB,canceled INTEGER DEFAULT 0);"
                "CREATE TABLE IF NOT EXISTS events (method TEXT,path TEXT);"
                "CREATE TABLE IF NOT EXISTS settings (name TEXT PRIMARY KEY,value TEXT);"
            )

    def set(self, name: str, value: Any) -> None:
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(
                "INSERT OR REPLACE INTO settings VALUES (?,?)",
                (name, json.dumps(value)),
            )

    def get(self, name: str, default: Any = None) -> Any:
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute(
                "SELECT value FROM settings WHERE name=?", (name,)
            ).fetchone()
        return json.loads(row[0]) if row is not None else default

    def record(self, method: str, path: str) -> None:
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT INTO events VALUES (?,?)", (method, path))

    def events(self) -> list[tuple[str, str]]:
        with closing(sqlite3.connect(self.path)) as db:
            return [
                (row[0], row[1])
                for row in db.execute("SELECT method,path FROM events ORDER BY rowid")
            ]

    def body(self, ordinal: int = 1) -> bytes:
        with closing(sqlite3.connect(self.path)) as db:
            return bytes(
                db.execute("SELECT body FROM jobs WHERE id=?", (ordinal,)).fetchone()[0]
            )

    def result_rows(
        self, ordinal: int, requests: list[dict[str, Any]], canceled: bool
    ) -> list[dict[str, Any]]:
        return [
            {
                "custom_id": member["custom_id"],
                "result": self.result(
                    ordinal, member["params"]["model"], offset, canceled
                ),
            }
            for offset, member in enumerate(requests)
        ][::-1]

    def result(
        self, ordinal: int, model: str, position: int, canceled: bool
    ) -> dict[str, Any]:
        if canceled and position == 0:
            return {"type": "canceled"}
        kind = (
            self.get("first_result", "succeeded")
            if ordinal == 1 and position == 0
            else "succeeded"
        )
        if kind == "expired":
            return {"type": "expired"}
        if kind == "errored":
            return {
                "type": "errored",
                "error": {
                    "type": "error",
                    "error": {"type": "invalid_request_error", "message": PRIVATE},
                },
            }
        text: dict[str, object] = (
            {"winner_id": "essay_01"}
            if self.get("pairwise")
            else {
                "scores": [
                    {"trait_id": trait, "category_label": "middle"}
                    for trait in self.get(f"traits_{ordinal}", ["01", "02"])
                ],
            }
        )
        if position == 0 and self.get("invalid_assessment"):
            text = {"scores": None}
        return {
            "type": "succeeded",
            "message": {
                "id": f"msg_{position}",
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [
                    {
                        "type": "text",
                        "text": self.get("message_override", json.dumps(text)),
                    }
                ],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 7, "output_tokens": 3},
            },
        }

    async def request(self, request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        self.record(request.method, path)
        headers = {
            "request-id": self.get("request_id", "req_fixture"),
            "content-type": "application/json",
        }
        if path == BATCH_PATH and request.method == "POST":
            if self.get("reject"):
                return httpx2.Response(
                    400,
                    stream=Parts(
                        self, [REJECTION[:11], REJECTION[11:]], False, BATCH_PATH
                    ),
                    headers=headers,
                )
            with closing(sqlite3.connect(self.path)) as db, db:
                ordinal = db.execute(
                    "INSERT INTO jobs(body) VALUES (?)", (request.content,)
                ).lastrowid
            assert ordinal is not None
            requests = json.loads(request.content)["requests"]
            return httpx2.Response(
                200,
                json=self.metadata(
                    self.get("duplicate_create_id", ordinal),
                    requests,
                    "in_progress",
                    False,
                ),
                headers=headers,
            )
        if not path.startswith(BATCH_PATH + "/msgbatch_"):
            raise AssertionError("unexpected synthetic Batch route")
        name, _, suffix = path[len(BATCH_PATH) + 1 :].partition("/")
        ordinal = int(name.removeprefix("msgbatch_"))
        with closing(sqlite3.connect(self.path)) as db, db:
            row = db.execute(
                "SELECT body,canceled FROM jobs WHERE id=?", (ordinal,)
            ).fetchone()
            assert row is not None
            canceled = bool(row[1]) or suffix == "cancel"
            if suffix == "cancel":
                db.execute("UPDATE jobs SET canceled=1 WHERE id=?", (ordinal,))
        requests = json.loads(row[0])["requests"]
        if suffix == "results":
            if self.get("stall_result_job") == ordinal:
                return httpx2.Response(
                    200, stream=Parts(self, [], False, stall=True), headers=headers
                )
            if self.get("interrupt_result_job") == ordinal:
                return httpx2.Response(
                    200, stream=Parts(self, [], True), headers=headers
                )
            records = self.result_rows(ordinal, requests, canceled)
            if self.get("swap"):
                records.reverse()
            if self.get("duplicate"):
                records.append(records[-1])
            if self.get("missing"):
                records.pop()
            size = self.get("frame_size")
            overrides = {
                0: self.get("first_wire_row"),
                len(records) - 1: self.get("last_wire_row"),
            }
            parts: list[bytes] = []
            for index, record in enumerate(records):
                forged = overrides.get(index)
                encoded = (
                    forged.encode()
                    if forged is not None
                    else json.dumps(record, separators=(",", ":")).encode()
                )
                target = size[index] if size is not None else len(encoded) + 1
                parts.append(encoded + b" " * (target - len(encoded) - 1) + b"\n")
            if self.get("cut_final_newline"):
                parts[-1] = parts[-1].removesuffix(b"\n")
            if self.get("interrupt"):
                parts = parts[:1]
            fragment = self.get("fragment_bytes")
            if fragment is not None:
                parts = [
                    part[i : i + fragment]
                    for part in parts
                    for i in range(0, len(part), fragment)
                ]
            if self.get("declared_length") is not None:
                headers["content-length"] = str(self.get("declared_length"))
            headers.update(self.get("result_headers", {}))
            code: int = self.get("result_status", 200)
            if self.get("buffer_result"):
                return httpx2.Response(code, content=b"".join(parts), headers=headers)
            if self.get("coalesce_results"):
                parts = [b"".join(parts)]
            return httpx2.Response(
                code,
                stream=Parts(self, parts, self.get("interrupt", False)),
                headers=headers,
            )
        if self.get("metadata_fault") == "oversized":
            return httpx2.Response(
                200, stream=Parts(self, [b" " * 65_536] * 17, False), headers=headers
            )
        status = "canceling" if suffix == "cancel" else self.get("status", "ended")
        metadata = self.metadata(ordinal, requests, status, canceled)
        drift = self.get("time_drift")
        if drift in ("created_at", "expires_at"):
            metadata[drift] = metadata[drift].replace("T00:00:00", "T00:01:00")
        if self.get("metadata_fault") == "unsafe":
            metadata["results_url"] = "https://invalid.test/secret"
        return httpx2.Response(200, json=metadata, headers=headers)

    def metadata(
        self, ordinal: int, requests: list[dict[str, Any]], status: str, canceled: bool
    ) -> dict[str, Any]:
        counts = {
            "processing": len(requests),
            "succeeded": 0,
            "errored": 0,
            "canceled": 0,
            "expired": 0,
        }
        if status == "ended":
            counts["processing"] = 0
            for row in self.result_rows(ordinal, requests, canceled):
                counts[row["result"]["type"]] += 1
        return dict(
            id=f"msgbatch_{ordinal}",
            type="message_batch",
            processing_status=status,
            created_at="2026-11-01T00:00:00Z",
            expires_at="2026-11-02T00:00:00Z",
            ended_at="2026-11-01T01:00:00Z" if status == "ended" else None,
            archived_at="2026-11-03T01:00:00Z" if self.get("archived") else None,
            cancel_initiated_at="2026-11-01T00:30:00Z" if canceled else None,
            results_url=f"{BATCH_ORIGIN}{BATCH_PATH}/msgbatch_{ordinal}/results"
            if status == "ended" and not self.get("pending_url")
            else None,
            request_counts=counts,
        )


@contextmanager
def installed(service: Service) -> Generator[None, None, None]:
    stdlib_socketpair = socket.socketpair
    original_connect = socket.socket.connect
    socketpair_code = (
        getattr(stdlib_socketpair, "__code__", None)
        if getattr(stdlib_socketpair, "__globals__", None) is vars(socket)
        else None
    )
    pair_scope = local()

    def no_network(*args: Any, **kwargs: Any) -> NoReturn:
        raise AssertionError("fixture attempted external network")

    def scoped_socketpair(
        *args: Any, **kwargs: Any
    ) -> tuple[socket.socket, socket.socket]:
        if args or kwargs:
            return stdlib_socketpair(*args, **kwargs)
        previous = getattr(pair_scope, "frame", None)
        pair_scope.frame = currentframe()
        try:
            return stdlib_socketpair()
        finally:
            pair_scope.frame = previous

    def guarded_connect(current: socket.socket, address: object) -> None:
        scope = getattr(pair_scope, "frame", None)
        if scope is None:
            no_network()
        # The Windows fallback must connect its own client to its own listener.
        caller = currentframe()
        caller = caller.f_back if caller is not None else None
        if (
            caller is None
            or caller.f_back is not scope
            or caller.f_code is not socketpair_code
            or caller.f_locals.get("csock") is not current
        ):
            no_network()
        listener: object = caller.f_locals.get("lsock")
        if (
            not isinstance(listener, socket.socket)
            or current.family != socket.AF_INET
            or current.type != socket.SOCK_STREAM
            or listener.family != socket.AF_INET
            or listener.type != socket.SOCK_STREAM
        ):
            no_network()
        endpoint: tuple[str, int] = listener.getsockname()
        if (
            endpoint[0] != "127.0.0.1"
            or not 0 < endpoint[1] <= 65_535
            or address != endpoint
        ):
            no_network()
        original_connect(current, endpoint)

    def adapter(context: AnthropicBatchContextV1) -> AnthropicBatchAdapter:
        return AnthropicBatchAdapter(
            context, fixture_transport=httpx2.MockTransport(service.request)
        )

    pair_patch = (
        patch.object(socket, "socketpair", scoped_socketpair)
        if sys.platform == "win32" and socketpair_code is not None
        else nullcontext()
    )
    with (
        patch.object(native_batch, "_admit_anthropic_fixture", adapter),
        pair_patch,
        patch.object(socket, "getaddrinfo", no_network),
        patch.object(socket, "create_connection", no_network),
        patch.object(socket.socket, "connect", guarded_connect),
        patch.object(socket.socket, "connect_ex", no_network),
    ):
        yield


def write_case(
    directory: Path, *, capture: str, pairwise: bool = False, count: int = 2
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    source = directory / "responses.jsonl"
    source.write_text(
        "".join(
            json.dumps(
                {"item_id": f"{i:03}", "text": f"Synthetic café sample {i}"},
                ensure_ascii=False,
            )
            + "\n"
            for i in range(1, count + 1)
        ),
        encoding="utf-8",
    )
    config: dict[str, Any] = dict(
        schema_version=3,
        task_kind="pairwise" if pairwise else "rubric",
        provider="anthropic",
        endpoint="messages",
        model="claude-sonnet-4-6",
        capture=capture,
        input=str(source),
        execution={
            "mode": "batch_native",
            "cache_policy": "off",
            "grouping": "independent" if pairwise else "grouped",
        },
        prompt={"structured_output": "text"},
    )
    if pairwise:
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
        config.update(
            pairs=str(pairs),
            criteria=[{"trait_id": "01", "name": "Quality", "description": "Compare"}],
        )
    else:
        config["rubric"] = dict(
            rubric_id="01",
            name="Synthetic",
            traits=[
                dict(
                    trait_id=trait,
                    name="Quality",
                    description="Assess",
                    levels=[
                        {"label": label, "numeric_value": value}
                        for label, value in (("001", 1), ("middle", 2), ("top", 7))
                    ],
                )
                for trait in ("01", "02")
            ],
        )
    path = directory / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    Service(directory / "service.sqlite").set("pairwise", pairwise)
    return path


def create(root: Path) -> None:
    write_case(root / "anthropic-batch-grouped-full", capture="full")
    write_case(root / "anthropic-batch-pair-minimal", capture="minimal", pairwise=True)


def main() -> None:
    command, directory, console, *arguments = sys.argv[1:]
    if command != "cli":
        raise SystemExit("unsupported synthetic fixture command")
    with installed(Service(Path(directory) / "service.sqlite")):
        if console == "-":
            from judgmentllm.cli import app

            sys.argv = ["judgmentllm", *arguments]
            app()
        else:
            sys.argv = [console, *arguments]
            runpy.run_path(console, run_name="__main__")


if __name__ == "__main__":
    main()
