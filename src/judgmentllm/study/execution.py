"""Guarded study orchestration over the existing adapters, engine and evidence."""

from __future__ import annotations

# pyright: reportPrivateUsage=false
import importlib
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from judgmentllm.config import RunSpecification, RunSpecV3, validate_run_spec_json
from judgmentllm.execution import execute_run
from judgmentllm.execution.exports import export_run
from judgmentllm.execution.retry import RetryPolicy
from judgmentllm.native_schema import NativeModel
from judgmentllm.persistence.artifacts import ArtifactStore
from judgmentllm.persistence.hashing import bytes_digest, canonical_bytes
from judgmentllm.persistence.models import AttemptRecord
from judgmentllm.persistence.receipts import read_receipt
from judgmentllm.persistence.repository import ReadOnlyRepository
from judgmentllm.providers.protocol import PreparedRequest, ProviderIntegrityError
from judgmentllm.qualification import _source_commit
from judgmentllm.study.admission import candidate_context, spec_digest, study_generation
from judgmentllm.study.budget import BudgetLedger, BudgetStop, immutable
from judgmentllm.study.definitions import ROUTES
from judgmentllm.study.plans import load_proposal, verify_wire
from judgmentllm.study.records import Authorization, Case, Proposal

_STUDY_TOKEN = object()


class CredentialBinding(NativeModel):
    """Private independent proof; only its hash and source name enter a proposal.

    The fingerprint binds the later resolved key to the independently established
    account. Creating this record is an operator step outside the offline study.
    """

    provider: str
    account_scope: str
    credential_source: str
    credential_fingerprint_sha256: str
    account_evidence_sha256: str
    verified_on: datetime
    reviewed_by: str


def _proof(root: Path, digest: str) -> bytes:
    path = root / (digest + ".json")
    if path.is_symlink() or bytes_digest(path.read_bytes()) != digest:
        raise ValueError("Independent evidence commitment differs")
    return path.read_bytes()


def _verify_proofs(case: Case, proofs: Path, *, credentials: bool) -> None:
    if case.eligibility is None or case.price is None:
        raise ValueError("Account eligibility and pricing remain unknown")
    eligibility = case.eligibility
    _proof(proofs, eligibility.evidence_sha256)
    _proof(proofs, case.price.evidence_sha256)
    binding = CredentialBinding.model_validate_json(
        _proof(proofs, eligibility.binding_sha256)
    )
    if (
        (binding.provider, binding.account_scope, binding.credential_source)
        != (case.selection.provider, case.account_scope, eligibility.credential_source)
        or binding.account_evidence_sha256 != eligibility.evidence_sha256
        or not binding.reviewed_by
        or binding.verified_on > datetime.now(UTC)
        or eligibility.recorded_on > datetime.now(UTC).date()
        or case.price.recorded_on > datetime.now(UTC).date()
    ):
        raise ValueError("Account/credential binding differs")
    expected = {
        "openai": ("OPENAI_API_KEY",),
        "anthropic": ("ANTHROPIC_API_KEY",),
        "gemini": ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
    }[case.selection.provider]
    if eligibility.credential_source not in expected:
        raise ValueError("Credential source differs from the exact provider")
    if credentials:
        from judgmentllm._auth import _resolve_credentials

        resolved = _resolve_credentials(case.selection.provider)
        fingerprint = bytes_digest(
            canonical_bytes(
                ["study-account-binding-v1", binding.account_scope, resolved._api_key()]
            )
        )
        if (
            resolved.source != binding.credential_source
            or fingerprint != binding.credential_fingerprint_sha256
        ):
            raise ProviderIntegrityError(
                "Resolved credential differs from the independently bound account"
            )


class _StudyGuard:
    """Exact private type checked at adapter admission and actual HTTP forwarding."""

    def __init__(
        self,
        spec: RunSpecV3,
        source: str,
        *,
        token: object,
        proposal: Proposal | None = None,
        case: Case | None = None,
        directory: Path | None = None,
        ledger: BudgetLedger | None = None,
        proofs: Path | None = None,
    ) -> None:
        if token is not _STUDY_TOKEN:
            raise ProviderIntegrityError("Study guard authority is unavailable")
        self.spec_sha256 = spec_digest(spec)
        self.source = source
        self.context = candidate_context(spec, source)
        self.proposal, self.case, self.directory, self.ledger, self.proofs = (
            proposal,
            case,
            directory,
            ledger,
            proofs,
        )

    def verify_spec(self, context: Any, spec: RunSpecification) -> None:
        if context != self.context or spec_digest(spec) != self.spec_sha256:
            raise ProviderIntegrityError("Study source/configuration scope differs")
        study_generation(spec, context.scope)

    def verify_execution(self, context: Any) -> None:
        proposal, case = self.proposal, self.case
        if (
            context != self.context
            or proposal is None
            or case is None
            or self.directory is None
            or self.ledger is None
            or self.proofs is None
            or case not in proposal.calls
            or proposal.source_commit != self.source
            or _source_commit() != self.source
        ):
            raise ProviderIntegrityError(
                "Study execution has no exact-source authorization"
            )
        raw = (self.directory / "proposal.json").read_bytes()
        if raw != canonical_bytes(proposal):
            raise ProviderIntegrityError("Study proposal bytes differ")
        self.ledger._require_lock()
        authorization = Authorization.model_validate_json(
            (
                self.ledger.root / "authorizations" / (proposal.suite + ".json")
            ).read_bytes()
        )
        authorization.verify(proposal)
        immutable(
            self.directory / "campaign-binding.json",
            canonical_bytes(
                {
                    "proposal_sha256": proposal.digest,
                    "source_commit": self.source,
                    "campaign_root": str(self.ledger.root),
                }
            ),
        )
        _verify_proofs(case, self.proofs, credentials=True)

    def __call__(self, prepared: PreparedRequest) -> None:
        self.verify_execution(self.context)
        assert (
            self.case is not None
            and self.proposal is not None
            and self.ledger is not None
        )
        verify_wire(self.case, prepared)
        self.ledger.reserve(self.proposal, self.case)


def selected_attempts(
    adapter: Any, attempts: Iterator[AttemptRecord]
) -> Iterator[AttemptRecord]:
    """Only this plan's selected physical request may leave a complete study plan."""
    guard = adapter._submission_guard
    if type(guard) is not _StudyGuard or guard.case is None:
        raise ProviderIntegrityError("Study request selection has no private authority")
    guard.verify_spec(
        adapter.transport_context, validate_run_spec_json(guard.case.specification_json)
    )
    return (
        attempt for attempt in attempts if attempt.request_uid == guard.case.request_uid
    )


def adapter_for(
    spec: RunSpecV3, guard: _StudyGuard, *, http_transport: Any = None
) -> Any:
    module = importlib.import_module("judgmentllm.providers." + spec.provider)
    kind = {
        "openai": "OpenAIAdapter",
        "anthropic": "AnthropicAdapter",
        "gemini": "GeminiAdapter",
    }[spec.provider]
    return getattr(module, kind)(
        spec.endpoint,
        spec.model,
        context=guard.context,
        concurrency=1,
        submission_guard=guard,
        http_transport=http_transport,
    )


def case_report(directory: Path, case: Case) -> dict[str, Any]:
    """Read verified physical evidence once; grouped trait rows never multiply usage."""
    row: dict[str, Any] = {
        "case_id": case.case_id,
        "configuration_id": case.configuration_id,
        "requested_model": case.selection.model,
        "provider": case.selection.provider,
        "endpoint": ROUTES[case.selection.provider][0],
        "reported_model": None,
        "requested_tier": case.selection.tier,
        "account_scope": case.account_scope,
        "sdk_version": case.sdk_version,
        "api_version": case.api_version,
        "mechanism": case.mechanism,
        "request_uid": case.request_uid,
        "attempt_id": case.attempt_id,
        "payload_sha256": case.payload_sha256,
        "prompt_sha256": case.prompt_sha256,
        "schema_sha256": case.schema_sha256,
        "projection_sha256": case.projection_sha256,
        "continuation_allowed": False,
        "reason": "unrun",
        "usage": None,
        "canonical_valid": None,
        "service_tier": None,
        "result_count": 0,
        "live_qualified": False,
        "blockers": list(case.blockers),
        "reserved_usd": str(case.reserved_usd)
        if case.reserved_usd is not None
        else None,
    }
    store = directory / "preparation" / case.case_id
    with ReadOnlyRepository(store, preserve_files=True) as repo:
        attempt = repo.get_attempt(case.run_id, case.attempt_id)
        submission = repo.get_submission_for_attempt(case.run_id, case.attempt_id)
        row.update(
            attempt_state=attempt.state,
            submission_certainty="not_started"
            if submission is None
            else submission.certainty,
        )
        receipts = list(repo.iter_evidence(case.run_id))
        results = list(repo.iter_results(case.run_id))
        if len(receipts) > 1 or any(r.attempt_id != case.attempt_id for r in results):
            raise ValueError("Study execution has extra physical evidence")
        if not receipts:
            if results:
                raise ValueError("Study result has no physical evidence")
            if attempt.state in {"not_submitted", "cancelled"}:
                row["reason"] = (
                    "proven_not_submitted"
                    if attempt.state == "not_submitted"
                    else "cancelled_before_handoff"
                )
            return row
        receipt = receipts[0]
        if (
            receipt.identity.run_id,
            receipt.identity.request_uid,
            receipt.identity.attempt_id,
            receipt.identity.submission_id,
            receipt.payload_sha256,
        ) != (
            case.run_id,
            case.request_uid,
            case.attempt_id,
            case.submission_id,
            case.payload_sha256,
        ):
            raise ValueError("Study evidence identity differs")
        usage = receipt.usage
        diagnostic = receipt.assessment_diagnostic
        tier = receipt.service_tier
        row.update(
            reported_model=usage.reported_model,
            usage=usage.model_dump(mode="json"),
            service_tier=None if tier is None else tier.model_dump(mode="json"),
            canonical_valid=None if diagnostic is None else diagnostic.canonical_valid,
            result_count=len(results),
            reason="uncertain_submission",
        )
        if receipt.identity.observation_id is None:
            row["reason"] = (
                "documented_terminal_nonacceptance"
                if submission is not None
                and submission.certainty == "not_submitted"
                and receipt.invocation_acceptance is None
                else "uncertain_submission"
            )
            row["evidence_sha256"] = bytes_digest(canonical_bytes(receipt))
            return row
        artifacts = ArtifactStore(repo.run_directory(case.run_id), create=False)
        response = read_receipt(artifacts, case.submission_id)
        submission = repo.get_submission(case.run_id, case.submission_id)
        if response is None or receipt.response_receipt_sha256 != bytes_digest(
            canonical_bytes(response)
        ):
            raise ValueError("Response commitment differs")
        response.verify_submission(submission)
        artifacts.verify(response.raw)
        if diagnostic is not None and diagnostic.artifact is not None:
            artifacts.verify(diagnostic.artifact)
        output = usage.output_tokens
        if case.selection.provider == "gemini":
            output = (
                None
                if output is None or usage.reasoning_tokens is None
                else output + usage.reasoning_tokens
            )
        inputs = usage.input_tokens
        if case.selection.provider == "anthropic":
            inputs = (
                None
                if inputs is None
                or usage.cache_read_tokens is None
                or usage.cache_write_tokens is None
                else inputs + usage.cache_read_tokens + usage.cache_write_tokens
            )
        row["billable_output_tokens"] = output
        row["billable_input_tokens"] = inputs
        row["original_and_presented_orders"] = [
            r.model_dump(
                mode="json", include={"original_order", "presented_order", "winner_id"}
            )
            for r in results
            if r.task_kind == "pairwise"
        ]
        if usage.reported_model != case.selection.model:
            row["reason"] = "reported_model_integrity_mismatch"
        elif (inputs is not None and inputs > case.reserved_input_tokens) or (
            output is not None and output > case.reserved_output_tokens
        ):
            row["reason"] = "token_reservation_exceeded"
        elif tier is None or tier.requires_tier_review:
            row["reason"] = "served_tier_review_required"
        elif tier.outcome == "unexpected":
            row["reason"] = "served_tier_integrity_mismatch"
        elif (
            diagnostic is None
            or usage.provider_outcome != "completed"
            or len(results) != len(case.task_ids)
            or {r.task_id for r in results} != set(case.task_ids)
            or any(r.status not in {"complete", "invalid"} for r in results)
        ):
            row["reason"] = "terminal_failure_or_unresolved_assessment"
        else:
            row.update(
                continuation_allowed=True,
                reason="expected_premium_downgrade"
                if tier.outcome == "downgraded"
                else "terminal_assessment",
            )
        row["evidence_sha256"] = bytes_digest(canonical_bytes(receipt))
    return row


def report(
    directory: Path, digest: str, campaign: Path | None = None
) -> dict[str, Any]:
    proposal = load_proposal(directory, digest)
    rows = [case_report(directory, case) for case in proposal.calls]
    journal_unknown = False
    try:
        events = (
            BudgetLedger(campaign).events()
            if campaign is not None and campaign.exists()
            else []
        )
    except BudgetStop:
        events = []
        journal_unknown = True
    reservations = [e for e in events if e.kind == "reserve"]
    for row in rows:
        selected = [
            e
            for e in events
            if e.suite == proposal.suite and e.case_id == row["case_id"]
        ]
        row["allocation_consumed"] = (
            None if journal_unknown else any(e.kind == "reserve" for e in selected)
        )
        if row["reason"] == "unrun" and row["allocation_consumed"]:
            row["reason"] = "uncertain_handoff_reservation_retained"
        elif row["reason"] == "unrun":
            stops = [e.reason for e in events if e.kind in {"hold", "cancel"}]
            row["reason"] = (
                stops[-1]
                if stops
                else "blocked_unresolved_eligibility_or_prices"
                if row["blockers"]
                else "not_yet_authorized_or_executed"
            )
        if journal_unknown and not row["continuation_allowed"]:
            row["reason"] = "unreconciled_forwarding_intent_reservations_held"
    return {
        "suite": proposal.suite,
        "proposal_sha256": digest,
        "source_commit": proposal.source_commit,
        "proposal_status": proposal.status,
        "authorized_calls_in_proposal": 0,
        "calls": rows,
        "incomplete_coverage": any(not r["continuation_allowed"] for r in rows),
        "campaign_reserved_calls": None if journal_unknown else len(reservations),
        "campaign_reserved_usd": None
        if journal_unknown
        else str(sum(e.reserved_usd for e in reservations)),
        "campaign_reserved_active_seconds": None
        if journal_unknown
        else sum(e.seconds for e in reservations),
        "campaign_journal_uncertain": journal_unknown,
        "campaign_review_holds": [
            e.reason for e in events if e.kind in {"hold", "cancel"}
        ],
        "live_qualified": False,
    }


async def execute(
    directory: Path,
    digest: str,
    campaign: Path,
    authorization: Authorization,
    proofs: Path,
    *,
    recover_only: bool = False,
) -> dict[str, Any]:
    """No replacement calls on recovery. A proposal alone always authorizes zero."""
    proposal = load_proposal(directory, digest)
    authorization.verify(proposal)
    for case in proposal.calls:
        _verify_proofs(case, proofs, credentials=False)
    ledger = BudgetLedger(campaign)
    with ledger.locked():
        ledger.authorize(proposal, authorization)
        for case in proposal.calls:
            events = ledger.events()
            consumed = any(
                e.kind == "reserve"
                and e.suite == proposal.suite
                and e.case_id == case.case_id
                for e in events
            )
            terminal = any(
                e.kind == "terminal"
                and e.suite == proposal.suite
                and e.case_id == case.case_id
                for e in events
            )
            if terminal:
                continue
            if (
                any(e.kind in {"hold", "cancel"} for e in events)
                and not (recover_only and consumed)
            ) or (recover_only and not consumed):
                break
            if not consumed:
                try:
                    ledger.reserve(proposal, case, commit=False)
                except BudgetStop as error:
                    ledger.stop(proposal, case, str(error))
                    break
            spec = validate_run_spec_json(case.specification_json)
            assert isinstance(spec, RunSpecV3)
            guard = _StudyGuard(
                spec,
                proposal.source_commit,
                token=_STUDY_TOKEN,
                proposal=proposal,
                case=case,
                directory=directory,
                ledger=ledger,
                proofs=proofs,
            )
            adapter = adapter_for(spec, guard)
            execution_failed = False
            try:
                guard.verify_execution(guard.context)
                await execute_run(
                    case.run_id,
                    store=directory / "preparation" / case.case_id,
                    spec=spec,
                    adapter=adapter,
                    retry_policy=RetryPolicy(max_attempts=1),
                )
            except BudgetStop as error:
                ledger.stop(proposal, case, str(error))
                break
            except ExceptionGroup:
                execution_failed = True
            finally:
                await adapter.aclose()
            selected = case_report(directory, case)
            if any(
                e.kind == "reserve"
                and e.suite == proposal.suite
                and e.case_id == case.case_id
                for e in ledger.events()
            ):
                ledger.finish(
                    proposal,
                    case,
                    terminal=selected["continuation_allowed"] and not execution_failed,
                    reason="execution_failed_review_required"
                    if execution_failed and selected["continuation_allowed"]
                    else selected["reason"],
                    evidence=selected.get("evidence_sha256")
                    or bytes_digest(canonical_bytes(selected)),
                )
            else:
                ledger.stop(
                    proposal,
                    case,
                    "execution_failed_before_handoff"
                    if execution_failed
                    else selected["reason"],
                )
            if execution_failed or not selected["continuation_allowed"]:
                break
    return report(directory, digest, campaign)


def exports(directory: Path, digest: str) -> list[dict[str, object]]:
    proposal = load_proposal(directory, digest)
    results: list[dict[str, object]] = []
    formats: tuple[tuple[Literal["jsonl", "csv"], Literal["canonical", "r"]], ...] = (
        ("jsonl", "canonical"),
        ("csv", "canonical"),
        ("csv", "r"),
    )
    for case in proposal.calls:
        for format, view in formats:
            export = export_run(
                case.run_id,
                store=directory / "preparation" / case.case_id,
                format=format,
                view=view,
            )
            results.append(export.model_dump(mode="json"))
    return results
