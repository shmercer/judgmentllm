"""Compose safe extraction with the existing artifact/receipt/repository engine."""

from __future__ import annotations

from typing import TYPE_CHECKING

from judgmentllm.evidence import (
    EvidenceIdentity,
    ReasoningAuthorization,
    ReportedUsageInput,
    UsageContext,
    extract_provider_usage,
)
from judgmentllm.execution.evidence import committed_authorization
from judgmentllm.persistence.artifacts import ArtifactStore
from judgmentllm.persistence.evidence import (
    EvidenceReceipt,
    publish_evidence_receipt,
    read_evidence_receipt,
)
from judgmentllm.persistence.hashing import bytes_digest, canonical_bytes
from judgmentllm.persistence.models import ArtifactIdentity, IntegrityError, utc_now
from judgmentllm.persistence.receipts import (
    ResponseReceipt,
    publish_receipt,
    read_acknowledgment,
    read_receipt,
)
from judgmentllm.planning import stable_id
from judgmentllm.providers import PreparedRequestV2, ProviderResponse
from judgmentllm.providers.evidence import prepare_evidence

if TYPE_CHECKING:
    from judgmentllm.execution.session import ExecutionSession, ExecutionWork


class DurableEvidence:
    def __init__(self, session: ExecutionSession) -> None:
        self.session = session
        assert session.prepared is not None
        self.store = session.prepared.store
        self.repository = session.repository
        self.enabled = self.repository.evidence_supported
        self.authorization = ReasoningAuthorization(capture=session.spec.capture)
        self.authorization_sha256: str | None = None
        if self.enabled:
            if (
                not self.store.reference_path("evidence-authorization.json").exists()
                and self.repository.connection.execute(
                    "SELECT 1 FROM submissions WHERE run_id=? AND state!='prepared' LIMIT 1",
                    (session.run_id,),
                ).fetchone()
                is not None
            ):
                raise IntegrityError("submitted execution lacks evidence authorization")
            self.authorization, self.authorization_sha256 = committed_authorization(
                self.store,
                run_id=session.run_id,
                manifest_sha256=session.prepared.manifest_sha256,
                provider=session.spec.provider,
                capture=session.spec.capture,
                adapter=session.adapter,
            )

    def register_response(self, artifact: ArtifactIdentity) -> None:
        """Shared content keeps its original registry metadata and capture policy.

        The response receipt independently commits its permitted body identity.
        Byte verification precedes the transaction through prepare_evidence_commit.
        """
        row = self.repository.connection.execute(
            "SELECT byte_length FROM artifacts WHERE run_id=? AND sha256=?",
            (self.session.run_id, artifact.sha256),
        ).fetchone()
        if row is None:
            self.repository._register_artifact(self.session.run_id, artifact)  # pyright: ignore[reportPrivateUsage]
        elif row["byte_length"] != artifact.byte_length:
            raise IntegrityError("shared response artifact length conflicts")

    def identity(
        self, work: ExecutionWork, observation_id: str | None
    ) -> EvidenceIdentity:
        return EvidenceIdentity(
            run_id=self.session.run_id,
            request_uid=work.prepared.request_uid,
            attempt_id=work.attempt_id,
            submission_id=work.submission_id,
            observation_id=observation_id,
        )

    def verify(self, evidence: EvidenceReceipt, work: ExecutionWork) -> None:
        session = self.session
        expected = self.identity(work, evidence.identity.observation_id)
        if (
            evidence.identity != expected
            or evidence.payload_sha256 != work.prepared.payload_sha256
            or evidence.correlation_id != work.correlation_id
            or evidence.capture != session.spec.capture
            or evidence.authorization != self.authorization
            or evidence.authorization_sha256 != self.authorization_sha256
        ):
            raise IntegrityError("evidence receipt identity or authorization conflicts")
        if evidence.response_receipt is not None:
            receipt = evidence.response_receipt
            session.verify_receipt(work, receipt)
            session.verify_observation_key(work, receipt.observation_key)
            ack = read_acknowledgment(self.store, work.submission_id)
            if ack is None or (
                bytes_digest(canonical_bytes(ack)) != evidence.acknowledgment_sha256
                or bytes_digest(canonical_bytes(receipt))
                != evidence.response_receipt_sha256
                or receipt.observation_id != evidence.identity.observation_id
            ):
                raise IntegrityError("evidence receipt acknowledgment conflicts")

    def restore(self, work: ExecutionWork) -> EvidenceReceipt | None:
        """Recover safe facts even when minimal capture cannot recover results."""
        if not self.enabled:
            return None
        evidence = read_evidence_receipt(self.store, work.submission_id)
        if evidence is None:
            return None
        self.verify(evidence, work)
        response = evidence.response_receipt
        if response is None:
            return evidence
        self.session.checkpoint("during_evidence_recovery", work.prepared.request_uid)
        ack = self.session.saved_acknowledgment(work)
        if ack is None:
            raise IntegrityError("evidence receipt lacks acknowledgment")
        self.session.acknowledge(work, ack)
        publish_receipt(self.store, response)
        verified = self.repository.prepare_evidence_commit(evidence)
        with self.session.transaction():
            self.register_response(response.raw)
            prior = self.repository.get_observation_for_submission(
                self.session.run_id, work.submission_id
            )
            if prior is None:
                self.repository.record_observation(response.observation())
            elif prior != response.observation():
                raise IntegrityError("evidence observation conflicts")
            self.repository.record_evidence(
                evidence, owner_token=self.session.token, prepared=verified
            )
        return evidence

    def prepare(
        self, work: ExecutionWork, response: ProviderResponse
    ) -> tuple[ArtifactIdentity, ResponseReceipt, EvidenceReceipt | None]:
        """No original response bytes enter artifact I/O before safe extraction."""
        session = self.session

        commitment = (
            work.prepared.output_commitment
            if isinstance(work.prepared, PreparedRequestV2)
            else None
        )
        if session.spec.schema_version == 3:
            from judgmentllm.transport import PreparedRequestV3

            if not isinstance(work.prepared, PreparedRequestV3):
                raise IntegrityError("native evidence lacks its output commitment")
            commitment = work.prepared.output_commitment

        if not self.enabled:
            historical = read_receipt(self.store, work.submission_id)
            if historical is not None:
                session.verify_receipt(work, historical)
                session.verify_observation_key(work, historical.observation_key)
                if (
                    bytes_digest(response.raw) != historical.raw.sha256
                    or len(response.raw) != historical.raw.byte_length
                ):
                    raise IntegrityError("historical response conflicts with receipt")
                return historical.raw, historical, None
        previous = (
            read_evidence_receipt(self.store, work.submission_id)
            if self.enabled
            else None
        )
        if previous is not None:
            self.verify(previous, work)
            receipt = previous.response_receipt
            if receipt is None:
                raise IntegrityError("observation conflicts with failure evidence")
            # Saved derivatives carry no provider boundary. Actual retrievals must
            # reproduce committed facts; a saved body is checked by exact digest.
            if (
                response.evidence is None
                and bytes_digest(response.raw) == receipt.raw.sha256
            ):
                return receipt.raw, receipt, previous
        observation_id = stable_id(
            "observation", work.submission_id, response.observation_key
        )
        prepared = prepare_evidence(
            identity=self.identity(work, observation_id),
            context=UsageContext.model_validate(
                {
                    "provider": session.spec.provider,
                    "endpoint": session.spec.endpoint,
                    "requested_model": session.spec.model,
                    "observation_kind": response.kind,
                }
            ),
            commitment=commitment,
            response=response,
            capture=session.spec.capture,
            authorization=self.authorization,
            transport_context=session.prepared.transport_context
            if session.prepared is not None and session.spec.schema_version == 3
            else None,
        )
        session.checkpoint("after_evidence_extraction", work.prepared.request_uid)
        if not self.enabled and prepared.disposition != "exact":
            raise IntegrityError(
                "privacy-filtered observations require a schema-2 store"
            )
        safe = (
            prepared.retained_response
            if prepared.retained_response is not None
            else b""
        )
        raw = (
            self.store.put(safe, "application/json")
            if session.spec.capture == "full"
            else ArtifactStore.hash_only(
                bytes_digest(safe), len(safe), "application/json"
            )
        )
        for _reference, text in prepared.reasoning_artifacts:
            self.store.put(text, "text/plain; charset=utf-8")
        session.checkpoint("after_evidence_artifacts", work.prepared.request_uid)
        existing = read_receipt(self.store, work.submission_id)
        receipt = ResponseReceipt(
            run_id=session.run_id,
            attempt_id=work.attempt_id,
            submission_id=work.submission_id,
            correlation_id=work.correlation_id,
            payload_sha256=work.prepared.payload_sha256,
            observation_id=observation_id,
            observation_key=response.observation_key,
            kind=response.kind,
            raw=raw,
            capture=session.spec.capture,
            received_at=existing.received_at if existing else utc_now(),
        )
        if previous is not None:
            assert previous.response_receipt is not None
            receipt = receipt.model_copy(
                update={"received_at": previous.response_receipt.received_at}
            )
        session.verify_receipt(work, receipt)
        if existing is not None and existing != receipt:
            raise IntegrityError("received observation conflicts with durable receipt")
        companion = None
        if self.enabled:
            ack = read_acknowledgment(self.store, work.submission_id)
            assert ack is not None and self.authorization_sha256 is not None
            companion = EvidenceReceipt(
                identity=self.identity(work, observation_id),
                correlation_id=work.correlation_id,
                payload_sha256=work.prepared.payload_sha256,
                acknowledgment_sha256=bytes_digest(canonical_bytes(ack)),
                response_receipt_sha256=bytes_digest(canonical_bytes(receipt)),
                response_receipt=receipt,
                observation_key=response.observation_key,
                remote_id=ack.remote_id,
                capture=session.spec.capture,
                authorization=self.authorization,
                authorization_sha256=self.authorization_sha256,
                usage=prepared.usage,
                order=prepared.order,
                reasoning=prepared.reasoning,
                original=ArtifactStore.hash_only(
                    prepared.original_sha256, len(response.raw), "application/json"
                )
                if prepared.original_sha256
                else None,
                retained=raw,
                response_retention="omitted"
                if prepared.disposition == "withheld"
                else prepared.disposition,
            )
            if previous is not None and previous != companion:
                raise IntegrityError("replayed evidence conflicts")
            publish_evidence_receipt(self.store, companion)
            session.checkpoint("after_evidence_receipt", work.prepared.request_uid)
        publish_receipt(self.store, receipt)
        session.checkpoint("after_receipt", work.prepared.request_uid)
        return raw, receipt, companion

    def failure(
        self,
        work: ExecutionWork,
        reported: ReportedUsageInput | None,
        *,
        transient: bool = False,
        retry_after: float | None = None,
    ) -> EvidenceReceipt | None:
        """Invoked definitive nonacceptance has no invented provider observation."""
        if not self.enabled:
            return None
        session = self.session
        previous = read_evidence_receipt(self.store, work.submission_id)
        if previous is not None:
            self.verify(previous, work)
            if previous.identity.observation_id is not None:
                raise IntegrityError("failure conflicts with observation evidence")
            if reported is not None:
                original = ReportedUsageInput.model_validate(
                    {
                        key: getattr(previous.usage, key)
                        for key in ReportedUsageInput.model_fields
                    }
                )
                if original != reported:
                    raise IntegrityError("failure usage conflicts")
            return previous
        identity = self.identity(work, None)
        usage = extract_provider_usage(
            identity,
            UsageContext.model_validate(
                {
                    "provider": session.spec.provider,
                    "endpoint": session.spec.endpoint,
                    "requested_model": session.spec.model,
                    "observation_kind": "invocation",
                    "definitive_failure_id": stable_id("failure", work.submission_id),
                }
            ),
            reported,
        )
        assert self.authorization_sha256 is not None
        evidence = EvidenceReceipt(
            identity=identity,
            correlation_id=work.correlation_id,
            payload_sha256=work.prepared.payload_sha256,
            capture=session.spec.capture,
            authorization=self.authorization,
            authorization_sha256=self.authorization_sha256,
            usage=usage,
            response_retention="omitted",
            failure_transient=transient,
            failure_retry_after=retry_after,
        )
        publish_evidence_receipt(self.store, evidence)
        session.checkpoint("after_failure_evidence_receipt", work.prepared.request_uid)
        return evidence
