"""Independent mock-service acceptance evidence, never an executor journal.

Only hashes, opaque IDs and numeric/enum observation recipes enter this SQLite
database or its WAL. Every operation opens its own connection, permitting calls
from a bounded executor's I/O threads. Acceptance commits before acknowledgment.
"""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import closing
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from judgmentllm.models import ID
from judgmentllm.persistence.artifacts import _fsync_directory  # pyright: ignore[reportPrivateUsage]
from judgmentllm.persistence.models import SHA256, ExecutionModel
from judgmentllm.providers.protocol import ProviderIntegrityError


class ObservationRecipe(ExecutionModel):
    """Non-sensitive accepted recipe; text-bearing fixture values stay outside."""

    fixture_sha256: SHA256
    outcome: Literal["valid", "invalid", "refusal", "provider_error"]
    selection_index: int = Field(ge=0)
    winner: Literal["first", "second", "tie"]
    probabilities: bool
    response_sha256: SHA256


class AcceptanceRecord(ExecutionModel):
    submission_id: ID
    correlation_id: ID
    payload_sha256: SHA256
    accepted: bool
    observation_key: ID | None
    recipe: ObservationRecipe | None
    acceptance_count: int = Field(ge=0, le=1)
    retrieval_status: Literal["available", "unavailable", "not_accepted"]

    @model_validator(mode="after")
    def established_outcome(self) -> AcceptanceRecord:
        if self.accepted:
            if (
                self.observation_key is None
                or self.recipe is None
                or self.acceptance_count != 1
                or self.retrieval_status == "not_accepted"
            ):
                raise ValueError("accepted evidence requires an observation recipe")
        elif (
            self.observation_key is not None
            or self.recipe is not None
            or self.acceptance_count != 0
            or self.retrieval_status != "not_accepted"
        ):
            raise ValueError("nonacceptance cannot claim an observation")
        return self


class MockAcceptanceLedger:
    """Durable authoritative service ledger independent of executor storage."""

    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()
        try:
            missing: list[Path] = []
            parent = self.path.parent
            while not parent.exists():
                missing.append(parent)
                parent = parent.parent
            self.path.parent.mkdir(parents=True, exist_ok=True)
            for created in reversed(missing):
                _fsync_directory(created.parent)
            with closing(self._connect()) as connection, connection:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS service_identity "
                    "(singleton INTEGER PRIMARY KEY CHECK(singleton=1), identity TEXT NOT NULL)"
                )
                connection.execute(
                    "INSERT OR IGNORE INTO service_identity VALUES(1,?)",
                    (uuid.uuid4().hex,),
                )
                connection.execute(
                    """CREATE TABLE IF NOT EXISTS acceptances (
                    submission_id TEXT PRIMARY KEY NOT NULL,
                    correlation_id TEXT UNIQUE NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    accepted INTEGER NOT NULL CHECK(accepted IN (0,1)),
                    observation_key TEXT UNIQUE,
                    recipe TEXT,
                    acceptance_count INTEGER NOT NULL CHECK(acceptance_count IN (0,1)),
                    retrieval_status TEXT NOT NULL,
                    CHECK((accepted=1 AND observation_key IS NOT NULL AND recipe IS NOT NULL
                      AND acceptance_count=1 AND retrieval_status IN ('available','unavailable'))
                    OR (accepted=0 AND observation_key IS NULL AND recipe IS NULL
                      AND acceptance_count=0 AND retrieval_status='not_accepted'))
                    )"""
                )
                connection.execute(
                    """CREATE TRIGGER IF NOT EXISTS immutable_acceptance
                    BEFORE UPDATE ON acceptances BEGIN
                      SELECT RAISE(ABORT,'immutable mock acceptance'); END"""
                )
                connection.execute(
                    """CREATE TRIGGER IF NOT EXISTS retain_acceptance
                    BEFORE DELETE ON acceptances BEGIN
                      SELECT RAISE(ABORT,'immutable mock acceptance'); END"""
                )
            _fsync_directory(self.path.parent)
        except (OSError, sqlite3.Error):
            raise ProviderIntegrityError(
                "mock acceptance ledger could not be opened"
            ) from None

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            return connection
        except BaseException:
            connection.close()
            raise

    @property
    def identity(self) -> str:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT identity FROM service_identity WHERE singleton=1"
            ).fetchone()
            if row is None:
                raise ProviderIntegrityError("mock service identity is unavailable")
            return str(row["identity"])

    @staticmethod
    def _record(row: sqlite3.Row) -> AcceptanceRecord:
        try:
            return AcceptanceRecord(
                submission_id=str(row["submission_id"]),
                correlation_id=str(row["correlation_id"]),
                payload_sha256=str(row["payload_sha256"]),
                accepted=bool(row["accepted"]),
                observation_key=str(row["observation_key"])
                if row["observation_key"] is not None
                else None,
                recipe=ObservationRecipe.model_validate_json(str(row["recipe"]))
                if row["recipe"] is not None
                else None,
                acceptance_count=int(row["acceptance_count"]),
                retrieval_status=row["retrieval_status"],
            )
        except ValueError:
            raise ProviderIntegrityError(
                "mock acceptance evidence is invalid"
            ) from None

    @staticmethod
    def _require_identity(connection: sqlite3.Connection, identity: str | None) -> None:
        if identity is None:
            return
        row = connection.execute(
            "SELECT identity FROM service_identity WHERE singleton=1"
        ).fetchone()
        if row is None or row["identity"] != identity:
            raise ProviderIntegrityError(
                "mock service identity conflicts with accepted namespace"
            )

    def get(
        self, submission_id: str, *, service_identity: str | None = None
    ) -> AcceptanceRecord | None:
        """Check namespace and authoritative presence in the same read snapshot."""
        try:
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN")
                self._require_identity(connection, service_identity)
                row = connection.execute(
                    "SELECT * FROM acceptances WHERE submission_id=?", (submission_id,)
                ).fetchone()
                return self._record(row) if row else None
        except sqlite3.Error:
            raise ProviderIntegrityError(
                "mock acceptance evidence could not be read"
            ) from None

    def establish(
        self, record: AcceptanceRecord, *, service_identity: str | None = None
    ) -> AcceptanceRecord:
        """One durable service decision; conflicting redelivery fails closed."""
        record = AcceptanceRecord.model_validate(dict(record))
        try:
            with closing(self._connect()) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                self._require_identity(connection, service_identity)
                existing = connection.execute(
                    "SELECT * FROM acceptances WHERE submission_id=? OR correlation_id=?",
                    (record.submission_id, record.correlation_id),
                ).fetchone()
                if existing is not None:
                    prior = self._record(existing)
                    if prior != record:
                        raise ProviderIntegrityError(
                            "mock submission identity has conflicting evidence"
                        )
                    return prior
                connection.execute(
                    "INSERT INTO acceptances VALUES(?,?,?,?,?,?,?,?)",
                    (
                        record.submission_id,
                        record.correlation_id,
                        record.payload_sha256,
                        int(record.accepted),
                        record.observation_key,
                        record.recipe.model_dump_json() if record.recipe else None,
                        record.acceptance_count,
                        record.retrieval_status,
                    ),
                )
                return record
        except sqlite3.Error:
            raise ProviderIntegrityError(
                "mock acceptance evidence could not be committed"
            ) from None

    def acceptance_count(self) -> int:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT COALESCE(SUM(acceptance_count),0) AS total FROM acceptances"
            ).fetchone()
            assert row is not None
            return int(row["total"])
