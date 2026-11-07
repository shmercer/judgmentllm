"""Provider evidence crosses the actual public lifecycle, journal and recovery.

Every service response is synthetic. The adapter preserves the established mock
wire contract and adds only a separate in-memory evidence extension.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Literal
from uuid import uuid4

import pytest
from durable_evidence_fixture import (
    EvidenceFixtureAdapter,
    specification,
)
from test_auth_cli import private_failure

from judgmentllm import (
    LifecycleError,
    export_run_results,
    get_run,
    list_runs,
    resume_run,
    run_live,
)
from judgmentllm.evidence import ReasoningAuthorization
from judgmentllm.persistence.artifacts import ArtifactStore
from judgmentllm.persistence.evidence import (
    EvidenceReceipt,
    get_evidence_for_observation,
    iter_evidence,
)
from judgmentllm.persistence.repository import ReadOnlyRepository, Repository
from judgmentllm.providers import MockScenario


def use_adapter(
    monkeypatch: pytest.MonkeyPatch, adapter: EvidenceFixtureAdapter
) -> None:
    def factory(*args: object, **kwargs: object) -> EvidenceFixtureAdapter:
        return adapter

    monkeypatch.setattr("judgmentllm.lifecycle._mock_adapter", factory)


def observation_evidence(
    repository: Repository, run_id: str, observation_id: str
) -> EvidenceReceipt:
    receipt = get_evidence_for_observation(repository, run_id, observation_id)
    assert receipt is not None
    return receipt


def retained_bytes(store: Path) -> bytes:
    return b"\n".join(p.read_bytes() for p in store.rglob("*") if p.is_file())


@pytest.mark.parametrize("capture", ["full", "minimal"])
@pytest.mark.parametrize("order", ["together", "scores_first", "explanations_first"])
@pytest.mark.parametrize("grouping", ["independent", "grouped"])
def test_public_layout_execution_records_usage_per_submission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capture: Literal["full", "minimal"],
    order: str,
    grouping: str,
) -> None:
    source = specification(
        tmp_path / "inputs", capture=capture, order=order, grouping=grouping
    )
    store = tmp_path / "runtime"
    adapter = EvidenceFixtureAdapter(store / "mock-service.sqlite3")
    use_adapter(monkeypatch, adapter)
    report = run_live(source, store=store)
    assert report.completed_tasks == 2 and report.outcome == "ok"
    calls = 1 if grouping == "grouped" else 2
    assert adapter.ledger.acceptance_count() == calls
    with ReadOnlyRepository(store) as repository:
        usage = [e.usage for e in iter_evidence(repository, report.run_id, page_size=1)]
        assert len(usage) == calls
        assert all(
            (u.input_tokens, u.output_tokens, u.total_tokens) == (11, 7, 18)
            for u in usage
        )
        assert len({u.submission_id for u in usage}) == calls
        for submission in repository.iter_submissions(report.run_id):
            observation = repository.get_observation_for_submission(
                report.run_id, submission.submission_id
            )
            assert observation is not None
            order_record = observation_evidence(
                repository, report.run_id, observation.observation_id
            ).order
            assert (
                order_record is not None and order_record.order_adherence == "adherent"
            )
            assert order_record.observation_id == observation.observation_id
            assert order_record.submission_id == submission.submission_id
        rows = list(repository.iter_results(report.run_id))
        assert len(rows) == 2
        assert {r.attempt_id for r in rows} == {u.attempt_id for u in usage}
        assert all(r.input_tokens is None and r.output_tokens is None for r in rows)
    canonical = export_run_results(report.run_id, store=store)
    original_export = Path(canonical.files[0].path).read_bytes()
    resumed = resume_run(report.run_id, store=store)
    assert resumed.results == 2 and adapter.ledger.acceptance_count() == calls
    assert (
        Path(export_run_results(report.run_id, store=store).files[0].path).read_bytes()
        == original_export
    )
    with ReadOnlyRepository(store) as repository:
        assert [
            e.usage for e in iter_evidence(repository, report.run_id, page_size=1)
        ] == usage


@pytest.mark.parametrize("capture", ["full", "minimal"])
@pytest.mark.parametrize("pairwise", [False, True])
def test_scores_only_and_pairwise_keep_existing_assessment_exports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capture: Literal["full", "minimal"],
    pairwise: bool,
) -> None:
    source = specification(
        tmp_path / "inputs", capture=capture, explanations=False, pairwise=pairwise
    )
    store = tmp_path / "runtime"
    adapter = EvidenceFixtureAdapter(store / "mock-service.sqlite3")
    use_adapter(monkeypatch, adapter)
    report = run_live(source, store=store)
    assert report.outcome == "ok"
    with ReadOnlyRepository(store) as repository:
        usage = [e.usage for e in iter_evidence(repository, report.run_id)]
        assert len(usage) == 1
        observation = repository.get_observation_for_submission(
            report.run_id, usage[0].submission_id
        )
        assert observation is not None
        order = observation_evidence(
            repository, report.run_id, observation.observation_id
        ).order
        assert order is not None
        assert order.order_adherence == ("unknown" if pairwise else "adherent")
        assert order.unknown_reason == ("not_applicable" if pairwise else None)
    variants: tuple[tuple[Literal["jsonl", "csv"], Literal["canonical", "r"]], ...] = (
        ("jsonl", "canonical"),
        ("csv", "canonical"),
        ("csv", "r"),
    )
    for format, view in variants:
        exported = export_run_results(
            report.run_id, store=store, format=format, view=view
        )  # type: ignore[arg-type]
        text = Path(exported.files[0].path).read_text()
        assert "synthetic.reported_usage" not in text and "reasoning" not in text
        assert "001" in text


@pytest.mark.parametrize(
    "outcome", ["invalid", "refusal", "provider_error", "confirmed_cancellation"]
)
def test_unsuccessful_observations_keep_one_usage_fact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    source = specification(tmp_path / "inputs")
    store = tmp_path / "runtime"
    adapter = EvidenceFixtureAdapter(
        store / "mock-service.sqlite3",
        scenario=MockScenario.model_validate({"outcome": outcome}),
    )
    use_adapter(monkeypatch, adapter)
    report = run_live(source, store=store)
    assert report.results == 2
    with ReadOnlyRepository(store) as repository:
        usage = [e.usage for e in iter_evidence(repository, report.run_id)]
        assert len(usage) == 1 and usage[0].input_tokens == 11
        assert (
            repository.get_observation_for_submission(
                report.run_id, usage[0].submission_id
            )
            is not None
        )
    assert adapter.submit_calls == adapter.ledger.acceptance_count() == 1


@pytest.mark.parametrize("capture", ["full", "minimal"])
@private_failure
def test_reasoning_filter_precedes_any_body_persistence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capture: Literal["full", "minimal"],
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    __tracebackhide__ = True
    secret = "synthetic-credential-" + uuid4().hex
    thinking = ("synthetic-unauthorized-reasoning-" + uuid4().hex).encode()
    source = specification(tmp_path / "inputs", capture=capture)
    store = tmp_path / "runtime"
    adapter = EvidenceFixtureAdapter(
        store / "mock-service.sqlite3", reasoning=thinking, credential=secret
    )
    use_adapter(monkeypatch, adapter)
    report = run_live(source, store=store)
    assert report.outcome == "ok"
    exposed = (
        report.model_dump_json()
        + get_run(report.run_id, store=store).model_dump_json()
        + caplog.text
        + str(capsys.readouterr())
    )
    assert secret not in exposed and thinking.decode() not in exposed
    persisted = retained_bytes(store)
    assert secret.encode() not in persisted and thinking not in persisted
    with ReadOnlyRepository(store) as repository:
        usage = [e.usage for e in iter_evidence(repository, report.run_id)]
        observation = repository.get_observation_for_submission(
            report.run_id, usage[0].submission_id
        )
        assert observation is not None
        reasoning = observation_evidence(
            repository, report.run_id, observation.observation_id
        ).reasoning
        assert len(reasoning) == 1
        assert (
            reasoning[0].availability == "not_retained"
            and reasoning[0].artifact is None
        )
        assert reasoning[0].source_bytes_sha256 is None
        companion = observation_evidence(
            repository, report.run_id, observation.observation_id
        )
        assert companion.original is None
        assert companion.response_retention == "redacted"
        assert companion.retained is not None
        assert bool(companion.retained.relative_path) == (capture == "full")
    adapter.retrieval_available = False
    assert resume_run(report.run_id, store=store).results == 2
    assert adapter.submit_calls == 1
    assert secret.encode() not in retained_bytes(
        store
    ) and thinking not in retained_bytes(store)


@private_failure
def test_explicit_synthetic_authorization_retains_only_protected_reasoning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    __tracebackhide__ = True
    thinking = ("synthetic-authorized-reasoning-" + uuid4().hex).encode()
    secret = "synthetic-credential-" + uuid4().hex
    source = specification(tmp_path / "inputs", capture="full")
    store = tmp_path / "runtime"
    adapter = EvidenceFixtureAdapter(
        store / "mock-service.sqlite3",
        reasoning=thinking,
        credential=secret,
        authorization=ReasoningAuthorization(capture="full", retention="summary"),
    )
    use_adapter(monkeypatch, adapter)
    report = run_live(source, store=store)
    assert report.outcome == "ok" and thinking.decode() not in report.model_dump_json()
    with ReadOnlyRepository(store) as repository:
        usage = [e.usage for e in iter_evidence(repository, report.run_id)]
        observation = repository.get_observation_for_submission(
            report.run_id, usage[0].submission_id
        )
        assert observation is not None
        reasoning = observation_evidence(
            repository, report.run_id, observation.observation_id
        ).reasoning
        assert len(reasoning) == 1 and reasoning[0].availability == "retained"
        artifact = reasoning[0].artifact
        assert artifact is not None
        identity = repository.get_artifact(report.run_id, artifact.sha256)
        assert (
            ArtifactStore(repository.run_directory(report.run_id)).read(identity)
            == thinking
        )
        protected = repository.run_directory(report.run_id) / str(
            identity.relative_path
        )
    for path in store.rglob("*"):
        if path.is_file():
            content = path.read_bytes()
            assert secret.encode() not in content
            if thinking in content:
                assert path == protected
    adapter.retrieval_available = False
    assert resume_run(report.run_id, store=store).results == 2
    assert adapter.submit_calls == 1


@pytest.mark.parametrize("capture", ["full", "minimal"])
def test_adapter_without_evidence_keeps_unknown_facts_and_withholds_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capture: Literal["full", "minimal"]
) -> None:
    source = specification(tmp_path / "inputs", capture=capture)
    store = tmp_path / "runtime"
    adapter = EvidenceFixtureAdapter(
        store / "mock-service.sqlite3",
        evidence_available=False,
        reasoning=b"synthetic-unavailable-reasoning",
        credential="synthetic-unavailable-credential",
    )
    use_adapter(monkeypatch, adapter)
    report = run_live(source, store=store)
    assert report.outcome == "ok"
    with ReadOnlyRepository(store) as repository:
        usage = [e.usage for e in iter_evidence(repository, report.run_id)]
        assert len(usage) == 1
        assert (
            usage[0].input_tokens
            is usage[0].output_tokens
            is usage[0].total_tokens
            is None
        )
        observation = repository.get_observation_for_submission(
            report.run_id, usage[0].submission_id
        )
        assert observation is not None
        order = observation_evidence(
            repository, report.run_id, observation.observation_id
        ).order
        assert order is not None and order.order_adherence == "unknown"
        assert order.unknown_reason == "sdk_object_only"
        companion = observation_evidence(
            repository, report.run_id, observation.observation_id
        )
        assert companion.original is None and companion.response_retention == "omitted"
        assert companion.retained is not None and companion.retained.byte_length == 0
        if capture == "minimal":
            assert companion.retained.relative_path is None
    assert b"synthetic-unavailable-reasoning" not in retained_bytes(store)
    assert b"synthetic-unavailable-credential" not in retained_bytes(store)


def changed_emission(case: str) -> Callable[[bytes], bytes]:
    """Generate fixture wire bytes; extraction still reads these exact bytes."""

    def transform(raw: bytes) -> bytes:
        output = json.loads(raw)
        if case == "root_reordered":
            output = dict(reversed(tuple(output.items())))
        elif case == "member_reordered":
            output["scores"][0] = dict(reversed(tuple(output["scores"][0].items())))
        elif case == "trait_array_reordered":
            output["scores"].reverse()
        elif case == "unsafe_key":
            output["fixture_unknown_property"] = True
        elif case == "unknown_trait":
            output["scores"][0]["trait_id"] = "fixture-unknown-trait"
        elif case == "missing_output":
            return b"null"
        elif case == "malformed":
            return b"{fixture-malformed"
        elif case == "duplicate_key":
            return raw.replace(b'"trait_id":', b'"trait_id":"01","trait_id":', 1)
        return json.dumps(output, separators=(",", ":")).encode()

    return transform


@pytest.mark.parametrize("capture", ["full", "minimal"])
@pytest.mark.parametrize(
    "case,adherence,reason",
    [
        ("root_reordered", "mismatch", None),
        ("member_reordered", "mismatch", None),
        ("trait_array_reordered", "adherent", None),
        ("unsafe_key", "unknown", "unsafe_key_names"),
        ("unknown_trait", "unknown", "incomplete_evidence"),
        ("missing_output", "unknown", "malformed_output"),
        ("malformed", "unknown", "malformed_output"),
        ("duplicate_key", "unknown", "malformed_output"),
    ],
)
def test_exact_emitted_order_is_durable_diagnostic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capture: Literal["full", "minimal"],
    case: str,
    adherence: str,
    reason: str | None,
) -> None:
    source = specification(tmp_path / "inputs", capture=capture, order="scores_first")
    store = tmp_path / "runtime"
    adapter = EvidenceFixtureAdapter(
        store / "mock-service.sqlite3", transform=changed_emission(case)
    )
    use_adapter(monkeypatch, adapter)
    report = run_live(source, store=store)
    with ReadOnlyRepository(store) as repository:
        evidence = list(iter_evidence(repository, report.run_id))
        assert len(evidence) == 1
        order = evidence[0].order
        assert order is not None
        assert order.order_adherence == adherence and order.unknown_reason == reason
        if case == "unknown_trait":
            assert order.member_key_orders[0].trait_id is None
        if case == "unsafe_key":
            assert "fixture_unknown_property" not in order.model_dump_json()
        if case == "trait_array_reordered":
            assert [
                (o.index, o.trait_id)
                for o in order.member_key_orders
                if o.array == "scores"
            ] == [(0, "02"), (1, "01")]
    # A mismatch remains diagnostic and cannot invalidate otherwise valid scores.
    if case in {"root_reordered", "member_reordered", "trait_array_reordered"}:
        assert report.completed_tasks == 2 and report.outcome == "ok"
    adapter.retrieval_available = False
    resume_run(report.run_id, store=store)
    with ReadOnlyRepository(store) as repository:
        assert list(iter_evidence(repository, report.run_id)) == evidence
    assert adapter.submit_calls == adapter.ledger.acceptance_count() == 1


_PROCESS = r"""
import json, os, sys
from pathlib import Path
import judgmentllm.lifecycle as lifecycle
from judgmentllm import list_runs, run_live, resume_run
from durable_evidence_fixture import EvidenceFixtureAdapter

source, runtime, stage, retrieve = sys.argv[1:]
store = Path(runtime)
adapter = EvidenceFixtureAdapter(store / 'mock-service.sqlite3', retrieval_available=retrieve == 'yes')
def factory(*args, **kwargs):
    return adapter
lifecycle._mock_adapter = factory
original = lifecycle.execute_run
def fault(current, uid):
    if current == stage.removeprefix('recover:'):
        os._exit(79)
async def execute(*args, **kwargs):
    return await original(*args, **kwargs, fault=fault)
lifecycle.execute_run = execute
lifecycle.recover_run = execute
if stage and not stage.startswith('recover:'):
    run_live(source, store=store)
else:
    run_id = list_runs(store=store).runs[0].run_id
    print(resume_run(run_id, store=store).model_dump_json())
"""


def process(
    source: Path, store: Path, stage: str = "", *, retrieve: bool = True
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        (str(Path(__file__).parent), str(Path(__file__).parents[1] / "src"))
    )
    return subprocess.run(
        [
            sys.executable,
            "-c",
            _PROCESS,
            str(source),
            str(store),
            stage,
            "yes" if retrieve else "no",
        ],
        capture_output=True,
        text=True,
        env=environment,
        timeout=30,
    )


@pytest.mark.parametrize("capture", ["full", "minimal"])
@pytest.mark.parametrize(
    "stage",
    [
        "after_response",
        "after_evidence_extraction",
        "after_evidence_artifacts",
        "after_receipt",
        "after_evidence_receipt",
        "during_evidence_transaction",
        "after_evidence_commit",
        "after_observation",
        "after_normalization",
        "during_result_transaction",
        "after_result",
    ],
)
def test_process_death_public_recovery_preserves_one_physical_evidence_fact(
    tmp_path: Path, capture: Literal["full", "minimal"], stage: str
) -> None:
    source = specification(
        tmp_path / "inputs", capture=capture, order="explanations_first"
    )
    store = tmp_path / "runtime"
    crashed = process(source, store, stage)
    assert crashed.returncode == 79, crashed.stderr
    assert not crashed.stdout and not crashed.stderr
    adapter = EvidenceFixtureAdapter(store / "mock-service.sqlite3")
    assert adapter.ledger.acceptance_count() == 1
    run_id = list_runs(store=store).runs[0].run_id
    with ReadOnlyRepository(store) as repository:
        original = tuple(repository.iter_submissions(run_id))
        manifest = repository.get_manifest(run_id)
    recovered = process(source, store)
    assert recovered.returncode == 0, recovered.stderr
    assert json.loads(recovered.stdout)["completed_tasks"] == 2
    with ReadOnlyRepository(store) as repository:
        evidence = tuple(iter_evidence(repository, run_id, page_size=1))
        assert len(evidence) == 1 and evidence[0].usage.total_tokens == 18
        assert (
            evidence[0].order is not None
            and evidence[0].order.order_adherence == "adherent"
        )
        assert (
            len(
                tuple(
                    repository.usage_revision_history(
                        run_id, original[0].submission_id, page_size=1
                    )
                )
            )
            == 1
        )
        assert tuple(
            (s.submission_id, s.correlation_id, s.payload_sha256)
            for s in repository.iter_submissions(run_id)
        ) == tuple(
            (s.submission_id, s.correlation_id, s.payload_sha256) for s in original
        )
        assert repository.get_manifest(run_id) == manifest
        assert len(tuple(repository.iter_results(run_id))) == 2
    replayed = process(source, store, retrieve=False)
    assert replayed.returncode == 0, replayed.stderr
    assert adapter.ledger.acceptance_count() == 1
    with ReadOnlyRepository(store) as repository:
        assert tuple(iter_evidence(repository, run_id)) == evidence


@pytest.mark.parametrize("capture", ["full", "minimal"])
def test_published_companion_recovers_metadata_without_remote_retrieval(
    tmp_path: Path, capture: Literal["full", "minimal"]
) -> None:
    source = specification(tmp_path / "inputs", capture=capture)
    store = tmp_path / "runtime"
    crashed = process(source, store, "after_evidence_receipt")
    assert crashed.returncode == 79, crashed.stderr
    resumed = process(source, store, retrieve=False)
    assert resumed.returncode == 0, resumed.stderr
    status = json.loads(resumed.stdout)
    assert status["completed_tasks"] == (2 if capture == "full" else 0)
    run_id = list_runs(store=store).runs[0].run_id
    with ReadOnlyRepository(store) as repository:
        evidence = tuple(iter_evidence(repository, run_id))
        assert len(evidence) == 1 and evidence[0].usage.total_tokens == 18
        assert evidence[0].order is not None
        assert evidence[0].order.order_adherence == "adherent"
        assert len(tuple(repository.iter_results(run_id))) == (
            2 if capture == "full" else 0
        )
    assert (
        EvidenceFixtureAdapter(store / "mock-service.sqlite3").ledger.acceptance_count()
        == 1
    )
    final = process(source, store)
    assert final.returncode == 0 and json.loads(final.stdout)["completed_tasks"] == 2
    with ReadOnlyRepository(store) as repository:
        assert tuple(iter_evidence(repository, run_id)) == evidence


@pytest.mark.parametrize("capture", ["full", "minimal"])
def test_second_process_death_during_committed_evidence_recovery(
    tmp_path: Path, capture: Literal["full", "minimal"]
) -> None:
    source = specification(tmp_path / "inputs", capture=capture)
    store = tmp_path / "runtime"
    assert process(source, store, "after_observation").returncode == 79
    run_id = list_runs(store=store).runs[0].run_id
    with ReadOnlyRepository(store) as repository:
        original = tuple(iter_evidence(repository, run_id))
        assert len(original) == 1
    interrupted = process(source, store, "recover:during_evidence_recovery")
    assert interrupted.returncode == 79, interrupted.stderr
    with ReadOnlyRepository(store) as repository:
        assert tuple(iter_evidence(repository, run_id)) == original
    resumed = process(source, store)
    assert (
        resumed.returncode == 0 and json.loads(resumed.stdout)["completed_tasks"] == 2
    )
    with ReadOnlyRepository(store) as repository:
        assert tuple(iter_evidence(repository, run_id)) == original
    assert (
        EvidenceFixtureAdapter(store / "mock-service.sqlite3").ledger.acceptance_count()
        == 1
    )


@pytest.mark.parametrize("tamper", ["missing", "modified", "authorization"])
@private_failure
def test_reasoning_artifact_and_authorization_tampering_fail_safely_on_reopening(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tamper: str
) -> None:
    __tracebackhide__ = True
    thinking = ("synthetic-authorized-reasoning-" + uuid4().hex).encode()
    source = specification(tmp_path / "inputs", capture="full")
    store = tmp_path / "runtime"
    adapter = EvidenceFixtureAdapter(
        store / "mock-service.sqlite3",
        reasoning=thinking,
        authorization=ReasoningAuthorization(capture="full", retention="summary"),
    )
    use_adapter(monkeypatch, adapter)
    report = run_live(source, store=store)
    with ReadOnlyRepository(store) as repository:
        receipt = next(iter_evidence(repository, report.run_id))
        artifact = receipt.reasoning[0].artifact
        assert artifact is not None
        protected = (
            repository.run_directory(report.run_id)
            / "objects"
            / artifact.sha256[:2]
            / artifact.sha256
        )
    if tamper == "missing":
        protected.unlink()
    elif tamper == "modified":
        protected.write_bytes(b"changed synthetic fixture")
    else:
        adapter.evidence_authorization = ReasoningAuthorization(capture="full")
    with pytest.raises(LifecycleError) as caught:
        resume_run(report.run_id, store=store)
    assert thinking.decode() not in str(caught.value)
    assert adapter.submit_calls == adapter.ledger.acceptance_count() == 1


@pytest.mark.parametrize("capture", ["full", "minimal"])
@pytest.mark.parametrize("location", ["property", "trait"])
@private_failure
def test_unique_untrusted_output_identifiers_never_enter_durable_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capture: Literal["full", "minimal"],
    location: str,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    __tracebackhide__ = True
    secret = "synthetic-private-" + uuid4().hex

    def emission(raw: bytes) -> bytes:
        output = json.loads(raw)
        if location == "property":
            output[secret] = "Synthetic unexpected property."
        else:
            output["scores"][0]["trait_id"] = secret
        return json.dumps(output).encode()

    source = specification(tmp_path / "inputs", capture=capture)
    store = tmp_path / "runtime"
    adapter = EvidenceFixtureAdapter(store / "mock-service.sqlite3", transform=emission)
    use_adapter(monkeypatch, adapter)
    report = run_live(source, store=store)
    with ReadOnlyRepository(store) as repository:
        evidence = tuple(repository.iter_evidence(report.run_id))
        assert len(evidence) == 1
        receipt = evidence[0]
        assert receipt.order is not None and receipt.order.order_adherence == "unknown"
        assert receipt.original is None and receipt.response_retention == "omitted"
        assert secret not in receipt.model_dump_json()
    assert secret not in report.model_dump_json() + caplog.text + str(
        capsys.readouterr()
    )
    assert secret.encode() not in retained_bytes(store)
    assert secret not in "\n".join(str(p.relative_to(store)) for p in store.rglob("*"))
    resume_run(report.run_id, store=store)
    assert adapter.submit_calls == adapter.ledger.acceptance_count() == 1
    assert secret.encode() not in retained_bytes(store)
