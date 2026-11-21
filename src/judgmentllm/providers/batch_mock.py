"""Independent, offline batch service. No live submission method is called."""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Mapping
from pathlib import Path
from typing import Literal, cast

from pydantic import Field, JsonValue

from judgmentllm.batch_models import (
    BatchContextV1,
    BatchCursorV1,
    BatchFenceV1,
    BatchMockOptions,
    BatchOperationIntentV1,
    BatchOperationReceiptV1,
    BatchStatusEvidenceV1,
)
from judgmentllm.config import RunSpecification, RunSpecV2, parse_json
from judgmentllm.evidence._base import EvidenceModel
from judgmentllm.evidence.usage import ReportedUsageInput
from judgmentllm.lifecycle_models import LifecycleError
from judgmentllm.persistence.hashing import (
    bytes_digest,
    canonical_bytes,
    semantic_digest,
)
from judgmentllm.persistence.models import IntegrityError
from judgmentllm.planning import CapabilityProfile
from judgmentllm.preparation.session import RequestDependency
from judgmentllm.prompts import RenderedAssessmentPrompt
from judgmentllm.providers.batch_protocol import BatchEvidenceWithheld
from judgmentllm.providers.mock import MockAdapter, MockPayloadV2, MockScenario
from judgmentllm.providers.protocol import PreparedRequestV2, ProviderResponse


class BatchMockFixtureV1(EvidenceModel):
    """Synthetic controls, never provider request JSON or persisted response text."""

    rows: tuple[MockScenario, ...] = ()
    missing_positions: tuple[int, ...] = ()
    reverse_results: bool = True
    duplicate_results: bool = False
    upload: Literal["accepted", "rejected", "uncertain"] = "accepted"
    create: Literal["accepted", "rejected", "uncertain"] = "accepted"
    cancel: Literal["accepted", "rejected", "uncertain"] = "accepted"
    status_failures: int = Field(default=0, ge=0)
    delay_seconds: float = Field(default=0, ge=0, allow_inf_nan=False)
    ack_delay_seconds: float = Field(default=0, ge=0, allow_inf_nan=False)
    unsafe_control: bool = False
    unsafe_result: bool = False
    unsafe_key: bool = False
    unsafe_cursor: bool = False
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)

    def commitment(self) -> str:
        # Fault/availability controls do not alter the scientific fixture.
        rows = [row.model_dump(exclude={"raw_marker"}) for row in self.rows]
        if unsafe_text(canonical_bytes(rows)):
            raise ValueError("synthetic fixture contains prohibited output material")
        return semantic_digest(
            "batch-mock-fixture-v1",
            [
                rows,
                list(self.missing_positions),
                self.reverse_results,
                self.duplicate_results,
                self.input_tokens,
                self.output_tokens,
                self.unsafe_control,
                self.unsafe_result,
                self.unsafe_key,
                self.unsafe_cursor,
            ],
        )


def require_mock_batch(spec: RunSpecification) -> None:
    if not (
        isinstance(spec, RunSpecV2)
        and (spec.provider, spec.endpoint) == ("mock", "synthetic")
        and spec.execution.mode == "batch_native"
        and spec.execution.cache_policy == "off"
        and spec.execution.tier is None
        and spec.execution.bulk_limits is None
    ):
        raise LifecycleError(
            "unsupported_capability",
            "Native batch currently requires V2 mock/synthetic with cache OFF and no tier or bulk limits",
        )


class MockBatchAdapter(MockAdapter):
    """Mock encoding/parser reuse with a separate job and operation ledger."""

    def __init__(self, path: Path, options: BatchMockOptions, *, create: bool = False):
        if not create and not path.is_file():
            raise IntegrityError("original batch service ledger is unavailable")
        if create:
            path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.options = BatchMockOptions.model_validate_json(canonical_bytes(options))
        self.fixture = (
            BatchMockFixtureV1.model_validate_json(options.fixture_path.read_bytes())
            if options.fixture_path is not None
            else BatchMockFixtureV1()
        )
        self.scenario = MockScenario()
        self.scenarios: dict[str, MockScenario] = {}
        with self.connect() as connection:
            if create:
                connection.executescript("""
                    CREATE TABLE IF NOT EXISTS service(identity TEXT PRIMARY KEY);
                    CREATE TABLE IF NOT EXISTS operations(
                        instance_id TEXT PRIMARY KEY, logical_id TEXT NOT NULL,
                        intent_sha256 TEXT NOT NULL, sender TEXT NOT NULL,
                        outcome TEXT NOT NULL, remote_id TEXT, fenced INTEGER NOT NULL,
                        forwards INTEGER NOT NULL CHECK(forwards BETWEEN 0 AND 1));
                    CREATE TABLE IF NOT EXISTS jobs(
                        remote_id TEXT PRIMARY KEY, instance_id TEXT NOT NULL UNIQUE,
                        cancelled INTEGER NOT NULL DEFAULT 0, status_reads INTEGER NOT NULL DEFAULT 0);
                """)
                if (
                    connection.execute("SELECT count(*) FROM service").fetchone()[0]
                    == 0
                ):
                    connection.execute(
                        "INSERT INTO service VALUES (?)",
                        ("mock_service_" + uuid.uuid4().hex,),
                    )
            identities = connection.execute("SELECT identity FROM service").fetchall()
            if len(identities) != 1:
                raise IntegrityError("batch service identity is invalid")
            self.identity = str(identities[0][0])
        # The inherited pure compiler reads only ledger.identity.
        self.ledger = self  # type: ignore[assignment]

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=0, isolation_level="IMMEDIATE")
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def context(self) -> BatchContextV1:
        return BatchContextV1(
            service_id=self.identity,
            fixture_sha256=self.fixture.commitment(),
            limits=self.options.limits,
        )

    def capabilities(self, spec: RunSpecification) -> CapabilityProfile:
        return self.capabilities_for(
            spec.provider, spec.endpoint, spec.model
        ).model_copy(update={"modes": ("batch_native",)})

    def validate(self, spec: RunSpecification) -> None:
        require_mock_batch(spec)
        self.validate_configuration(spec, capabilities=self.capabilities(spec))

    async def pause(self) -> None:
        await asyncio.sleep(self.fixture.delay_seconds)

    @staticmethod
    def remote(kind: str, instance: str) -> str:
        return "mock_" + ("job" if kind != "upload" else "file") + "_" + instance

    @staticmethod
    def check_remote(value: str) -> None:
        if not any(
            value.startswith(prefix)
            and len(value) == len(prefix) + 64
            and all(c in "0123456789abcdef" for c in value[len(prefix) :])
            for prefix in ("mock_job_", "mock_file_")
        ):
            raise IntegrityError("batch service returned an unsafe identity")

    def receipt(
        self, intent: BatchOperationIntentV1, row: sqlite3.Row
    ) -> BatchOperationReceiptV1:
        digest = bytes_digest(canonical_bytes(intent))
        if (row["intent_sha256"], row["sender"]) != (digest, intent.owner_token):
            raise IntegrityError("batch service operation identity conflicts")
        outcome = cast(
            Literal["accepted", "not_accepted", "unresolved"], row["outcome"]
        )
        fence = (
            BatchFenceV1(
                service_id=self.identity,
                instance_id=intent.instance_id,
                intent_sha256=digest,
                sender_token=intent.owner_token,
                proof_sha256=semantic_digest(
                    "batch-fence-v1",
                    [self.identity, intent.instance_id, digest, intent.owner_token],
                ),
            )
            if outcome == "not_accepted" and row["fenced"]
            else None
        )
        remote = row["remote_id"]
        if remote is not None:
            self.check_remote(remote)
        return BatchOperationReceiptV1(
            instance_id=intent.instance_id,
            intent_sha256=digest,
            outcome=outcome,
            remote_id=remote,
            fence=fence,
        )

    async def invoke(
        self,
        intent: BatchOperationIntentV1,
        payload: AsyncIterator[bytes],
        guard: Callable[[], None],
    ) -> bytes:
        await self.pause()
        from hashlib import sha256

        digest_input = sha256()
        async for chunk in payload:
            digest_input.update(chunk)
        if digest_input.hexdigest() != intent.payload_sha256:
            raise IntegrityError("batch operation body differs from immutable intent")
        guard()
        if intent.service_id != self.identity:
            raise IntegrityError("batch service was replaced")
        requested = getattr(self.fixture, intent.kind)
        digest = bytes_digest(canonical_bytes(intent))
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute(
                "SELECT * FROM operations WHERE instance_id=?", (intent.instance_id,)
            ).fetchone()
            if previous is not None:
                # A retired grant is rejected before forwarding, not deduplicated
                # after a second simulated side effect.
                raise IntegrityError(
                    "batch sender grant is retired or already forwarded"
                )
            remote = (
                intent.remote_id
                if intent.kind == "cancel"
                else self.remote(intent.kind, intent.instance_id)
            )
            assert remote is not None
            self.check_remote(remote)
            outcome = "not_accepted" if requested == "rejected" else "accepted"
            connection.execute(
                "INSERT INTO operations VALUES (?,?,?,?,?,?,?,1)",
                (
                    intent.instance_id,
                    intent.logical_id,
                    digest,
                    intent.owner_token,
                    outcome,
                    remote if outcome == "accepted" else None,
                    int(outcome == "not_accepted"),
                ),
            )
            if outcome == "accepted" and intent.kind == "create":
                connection.execute(
                    "INSERT INTO jobs(remote_id,instance_id) VALUES (?,?)",
                    (remote, intent.instance_id),
                )
            if outcome == "accepted" and intent.kind == "cancel":
                if (
                    connection.execute(
                        "UPDATE jobs SET cancelled=1 WHERE remote_id=?", (remote,)
                    ).rowcount
                    != 1
                ):
                    raise IntegrityError("cancel targeted an unknown job")
            row = connection.execute(
                "SELECT * FROM operations WHERE instance_id=?", (intent.instance_id,)
            ).fetchone()
            assert row is not None
            receipt = self.receipt(intent, row)
        if intent.kind == "create" and self.fixture.ack_delay_seconds:
            await asyncio.sleep(self.fixture.ack_delay_seconds)
        if requested == "uncertain":
            raise TimeoutError("synthetic operation acceptance is unresolved")
        data = receipt.model_dump(mode="json")
        if self.fixture.unsafe_control:
            data["api_key"] = "PROHIBITED_BATCH_CONTROL_SECRET"
        return canonical_bytes(data)

    def decode_receipt(
        self, raw: bytes, intent: BatchOperationIntentV1
    ) -> BatchOperationReceiptV1:
        data = parse_json(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise IntegrityError("batch control evidence was withheld")
        # Documented synthetic authentication field may be removed. Unknown keys
        # cannot acquire either a retained representation or a hash.
        fields = dict(cast(dict[str, JsonValue], data))
        fields.pop("api_key", None)
        try:
            receipt = BatchOperationReceiptV1.model_validate_json(
                canonical_bytes(fields)
            )
        except ValueError:
            raise IntegrityError("batch control evidence was withheld") from None
        if (receipt.instance_id, receipt.intent_sha256) != (
            intent.instance_id,
            bytes_digest(canonical_bytes(intent)),
        ):
            raise IntegrityError(
                "batch control receipt conflicts with immutable intent"
            )
        if receipt.remote_id is not None:
            self.check_remote(receipt.remote_id)
            if intent.kind != "cancel" and receipt.remote_id != self.remote(
                intent.kind, intent.instance_id
            ):
                raise IntegrityError(
                    "receipt remote identity differs from original operation"
                )
        if receipt.fence is not None and receipt.fence.proof_sha256 != semantic_digest(
            "batch-fence-v1",
            [
                self.identity,
                intent.instance_id,
                receipt.intent_sha256,
                intent.owner_token,
            ],
        ):
            raise IntegrityError(
                "receipt fence differs from authoritative operation identity"
            )
        if intent.kind == "cancel" and receipt.remote_id not in (
            None,
            intent.remote_id,
        ):
            raise IntegrityError("cancellation receipt names another job")
        return receipt

    async def reconcile_operation(self, intent: BatchOperationIntentV1) -> bytes | None:
        await self.pause()
        if not self.options.reconciliation_available:
            return None
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM operations WHERE instance_id=?", (intent.instance_id,)
            ).fetchone()
            return (
                canonical_bytes(self.receipt(intent, row)) if row is not None else None
            )

    async def fence_unsent(self, intent: BatchOperationIntentV1) -> bytes:
        """Synthetic authoritative retirement, used by stale-sender fault tests."""
        await self.pause()
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO operations VALUES (?,?,?,?, 'not_accepted',NULL,1,0)",
                (
                    intent.instance_id,
                    intent.logical_id,
                    bytes_digest(canonical_bytes(intent)),
                    intent.owner_token,
                ),
            )
            row = connection.execute(
                "SELECT * FROM operations WHERE instance_id=?", (intent.instance_id,)
            ).fetchone()
            assert row is not None
            return canonical_bytes(self.receipt(intent, row))

    async def status(self, submission_id: str, remote_id: str) -> BatchStatusEvidenceV1:
        await self.pause()
        self.check_remote(remote_id)
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE remote_id=? AND instance_id=?",
                (remote_id, submission_id),
            ).fetchone()
            if row is None:
                raise IntegrityError("status job differs from original creation")
            reads = int(row["status_reads"])
            connection.execute(
                "UPDATE jobs SET status_reads=status_reads+1 WHERE remote_id=?",
                (remote_id,),
            )
        if reads < self.fixture.status_failures:
            raise OSError("synthetic finalizing status 503")
        return BatchStatusEvidenceV1(
            submission_id=submission_id,
            remote_id=remote_id,
            state="cancelled" if row["cancelled"] else "completed",
        )

    def row_response(
        self, prepared: PreparedRequestV2, position: int, observation_key: str
    ) -> ProviderResponse:
        scenario = (
            self.fixture.rows[position % len(self.fixture.rows)]
            if self.fixture.rows
            else MockScenario()
        )
        payload = MockPayloadV2.model_validate_json(prepared.payload)
        return self._response(
            payload, scenario, observation_key, scenario.category_index or 0
        )

    async def read_row(
        self, prepared: PreparedRequestV2, position: int, member_key: str, source: str
    ) -> bytes | None:
        await self.pause()
        if not self.options.retrieval_available:
            raise OSError("synthetic result retrieval unavailable")
        if position in self.fixture.missing_positions:
            return None
        key = semantic_digest("batch-observation-v1", [member_key, source])
        response = self.row_response(prepared, position, key)
        error = response.kind in ("provider_error", "confirmed_cancellation", "refusal")
        if (source == "error") != error:
            return None
        data = {
            "member_key": member_key,
            "response": json.loads(response.raw),
            "usage": ReportedUsageInput(
                usage_source="synthetic.reported_usage",
                input_tokens=self.fixture.input_tokens if not error else None,
                output_tokens=self.fixture.output_tokens if not error else None,
            ).model_dump(mode="json"),
        }
        if self.fixture.unsafe_result:
            data["api_key"] = "PROHIBITED_BATCH_RESULT_SECRET"
        if self.fixture.unsafe_key:
            data["member_key"] = "PROHIBITED_BATCH_MEMBER_SECRET"
        return canonical_bytes(data) + b"\n"

    async def results(
        self,
        submission_id: str,
        remote_id: str,
        source: str,
        members: list[tuple[str, int, PreparedRequestV2]],
        cursor: BatchCursorV1 | None = None,
    ) -> AsyncGenerator[bytes, None]:
        self.check_remote(remote_id)
        with self.connect() as connection:
            if (
                connection.execute(
                    "SELECT 1 FROM jobs WHERE remote_id=? AND instance_id=?",
                    (remote_id, submission_id),
                ).fetchone()
                is None
            ):
                raise IntegrityError("result retrieval lacks original job acceptance")
        expected_source = semantic_digest(
            "batch-result-source-v1", [submission_id, source, self.fixture.commitment()]
        )
        if cursor is not None and (
            cursor.submission_id != submission_id or cursor.source_id != expected_source
        ):
            raise IntegrityError("result cursor differs from immutable source")
        ordered = reversed(members) if self.fixture.reverse_results else iter(members)
        emitted = 0
        for key, position, prepared in ordered:
            scenario = (
                self.fixture.rows[position % len(self.fixture.rows)]
                if self.fixture.rows
                else MockScenario()
            )
            error = scenario.outcome in (
                "refusal",
                "provider_error",
                "confirmed_cancellation",
            )
            if position in self.fixture.missing_positions or (
                (source == "error") != error
            ):
                continue
            for _ in range(2 if self.fixture.duplicate_results else 1):
                if cursor is not None and emitted < cursor.position:
                    emitted += 1
                    continue
                raw = await self.read_row(prepared, position, key, source)
                if raw is None:
                    raise IntegrityError("immutable result source changed")
                async for chunk in self.chunks(raw):
                    yield chunk
                emitted += 1
        if cursor is not None and emitted < cursor.position:
            raise IntegrityError("result cursor exceeds immutable source")

    async def chunks(self, raw: bytes) -> AsyncIterator[bytes]:
        for position in range(0, len(raw), 65_536):
            await self.pause()
            yield raw[position : position + 65_536]

    async def result_source(self, submission_id: str, source: str) -> str:
        await self.pause()
        if self.fixture.unsafe_cursor:
            raise BatchEvidenceWithheld("synthetic source identity was withheld")
        return semantic_digest(
            "batch-result-source-v1", [submission_id, source, self.fixture.commitment()]
        )

    def screen_result(
        self, raw: bytes, expected_keys: Mapping[str, PreparedRequestV2]
    ) -> tuple[str, ProviderResponse, ReportedUsageInput, bytes, bool] | None:
        return screen_row(raw, expected_keys)

    def prepare(
        self,
        spec: RunSpecification,
        dependency: RequestDependency,
        rendered: RenderedAssessmentPrompt,
    ) -> PreparedRequestV2:
        prepared = super().prepare(spec, dependency, rendered)
        if not isinstance(prepared, PreparedRequestV2):
            raise IntegrityError("batch mock requires its V2 compiler")
        return prepared


def unsafe_text(raw: bytes) -> bool:
    """Conservative synthetic privacy guard; no real credential is consulted."""
    return (
        re.search(
            rb"(?:sk-[A-Za-z0-9_-]{8,}|PROHIBITED_[A-Z_]+|Bearer\s+[^\s\"]+)", raw
        )
        is not None
    )


def screen_row(
    raw: bytes, expected_keys: Mapping[str, PreparedRequestV2]
) -> tuple[str, ProviderResponse, ReportedUsageInput, bytes, bool] | None:
    """Authorize a bounded synthetic row before hashing any original bytes."""
    try:
        fields = parse_json(raw.decode("utf-8"))
        if not isinstance(fields, dict):
            return None
        # Authentication-bearing root material has a documented derivative.
        derivative = dict(cast(dict[str, JsonValue], fields))
        derivative.pop("api_key", None)
        key = derivative.get("member_key")
        if (
            not isinstance(key, str)
            or key not in expected_keys
            or set(derivative) != {"member_key", "response", "usage"}
        ):
            return None
        body = derivative["response"]
        if not isinstance(body, dict):
            return None
        body = dict(cast(dict[str, JsonValue], body))
        marker = body.pop("raw_marker", None)
        from judgmentllm.providers.mock import MockResponseEnvelopeV2

        envelope = MockResponseEnvelopeV2.model_validate_json(canonical_bytes(body))
        response_bytes = canonical_bytes(envelope)
        from judgmentllm.providers.mock import _response_evidence  # pyright: ignore[reportPrivateUsage]

        payload = MockPayloadV2.model_validate_json(expected_keys[key].payload)
        documented_invalid = envelope.output == {"synthetic_invalid": True}
        if (
            not documented_invalid
            and _response_evidence(
                response_bytes, payload, terminal=envelope.kind != "response"
            ).safe_response
            is None
        ):
            return None
        known_traits = {member.trait_id for member in payload.members}
        if (
            envelope.member_probabilities is not None
            and set(envelope.member_probabilities) - known_traits
        ):
            return None
        if (
            envelope.member_expected_indices is not None
            and set(envelope.member_expected_indices) - known_traits
        ):
            return None
        if unsafe_text(response_bytes):
            return None
        usage = ReportedUsageInput.model_validate_json(
            canonical_bytes(derivative["usage"])
        )
        response = ProviderResponse(
            observation_key=envelope.observation_key,
            kind=envelope.kind,
            raw=response_bytes,
        )
        safe = canonical_bytes(
            {"member_key": key, "response": envelope, "usage": usage}
        )
        exact = (
            set(cast(dict[str, JsonValue], fields)) == set(derivative)
            and marker is None
            and not unsafe_text(raw)
        )
        return key, response, usage, safe, exact
    except (ValueError, TypeError, UnicodeError):
        return None
