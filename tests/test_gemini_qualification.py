"""Immutable offline proposal, actual SDK source identity, and execution denial."""

from __future__ import annotations

# pyright: reportPrivateUsage=false
import hashlib
import json
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest

from judgmentllm import gemini_qualification as qualification
from judgmentllm.persistence.hashing import bytes_digest, canonical_bytes


def _files(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): bytes_digest(p.read_bytes())
        for p in root.rglob("*")
        if p.is_file()
    }


@pytest.fixture
def plan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(qualification, "_source_commit", lambda: "a" * 40)
    from judgmentllm import _auth

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(
            "Proposal must not resolve credentials or import an SDK client"
        )

    monkeypatch.setattr(_auth, "_resolve_credentials", forbidden)
    directory = tmp_path / "proposal"
    proposal = qualification.build_plan(directory=directory, source_commit="a" * 40)
    assert not proposal.execution_enabled and proposal.requests_authorized_now == 0
    assert proposal.maximum_authorized_requests == 4 and proposal.automatic_retries == 0
    assert proposal.automatic_continuation is False and proposal.concurrency == 1
    return directory


def test_complete_reviewable_plan_and_readonly_report(plan: Path) -> None:
    before = _files(plan)
    result = qualification.report_plan(plan)
    assert _files(plan) == before
    assert result["source_commit"] == "a" * 40 and result["executed_requests"] == 0
    assert result["reserved_input_tokens"] == 32768
    assert result["reserved_output_tokens_including_thinking"] == 2048
    assert Decimal(str(result["reserved_estimated_usd"])) == Decimal("0.011264")
    assert not result["live_qualified"] and not result["execution_enabled"]
    assert {"exact_plan_sha256", "exact_source_commit"} <= set(
        cast(list[str], result["authorization_required"])
    )
    raw = (plan / "plan.json").read_bytes()
    body = json.loads(raw)
    assert body["sdk_version"] == "2.34.0" and body["api_version"] == "v1beta"
    assert len(body["calls"]) == 4
    for call in body["calls"]:
        assert (
            bytes_digest(call["request_body_utf8"].encode()) == call["payload_sha256"]
        )
        request = json.loads(call["request_body_utf8"])
        assert request["generationConfig"]["maxOutputTokens"] == 512
        assert "tools" not in request and "continuationToken" not in request
    with pytest.raises(FileExistsError):
        qualification.build_plan(directory=plan, source_commit="a" * 40)


@pytest.mark.parametrize(
    "mutation",
    [
        "digest",
        "duplicate",
        "budget",
        "body",
        "version",
        "source",
        "config",
        "requests",
        "matrix",
    ],
)
def test_tampered_rehashed_proposal_fails_closed(plan: Path, mutation: str) -> None:
    path = plan / "plan.json"
    body = json.loads(path.read_bytes())
    if mutation == "digest":
        (plan / "plan.sha256").write_text("0" * 64 + "\n")
    elif mutation == "config":
        (plan / "rubric-text.json").write_bytes(b"{}")
    else:
        if mutation == "budget":
            body["proposed_budget_usd"] = "0.001"
        elif mutation == "requests":
            body["maximum_authorized_requests"] = True
        elif mutation == "matrix":
            body["calls"] = body["calls"][:3]
        elif mutation == "version":
            body["plan_version"] = 1.0
        elif mutation == "source":
            body["source_commit"] = "b" * 40
        elif mutation == "body":
            call = body["calls"][0]
            request = json.loads(call["request_body_utf8"])
            request["tools"] = [{"functionDeclarations": [{"name": "forbidden"}]}]
            call["request_body_utf8"] = json.dumps(request)
            call["payload_sha256"] = bytes_digest(call["request_body_utf8"].encode())
            allowance, evidence = qualification._input_allowance(
                call["request_body_utf8"]
            )
            call["reserved_input_tokens"] = allowance
            call["input_allowance_evidence"] = evidence
        raw = canonical_bytes(body)
        if mutation == "duplicate":
            raw = b'{"plan_version":1,' + raw[1:]
        path.write_bytes(raw)
        if mutation != "digest":
            (plan / "plan.sha256").write_text(bytes_digest(raw) + "\n")
    with pytest.raises((ValueError, qualification.QualificationError)):
        qualification.report_plan(plan)


@pytest.mark.parametrize(
    "settings",
    [
        {"source_commit": "b" * 40},
        {"max_output_tokens": 513},
        {"max_output_tokens": True},
        {"max_total_reserved_tokens": 1},
        {"proposed_budget_usd": Decimal("0.001")},
    ],
)
def test_invalid_reservations_fail_before_preparation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, settings: dict[str, Any]
) -> None:
    monkeypatch.setattr(qualification, "_source_commit", lambda: "a" * 40)
    values: dict[str, Any] = (
        dict(directory=tmp_path / "proposal", source_commit="a" * 40) | settings
    )
    with pytest.raises(ValueError):
        qualification.build_plan(**values)
    assert not (tmp_path / "proposal").exists()


def test_cli_has_no_execute_or_authorization_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", ["qualify_gemini.py", "execute"])
    with pytest.raises(SystemExit) as failure:
        qualification.main()
    assert failure.value.code == 2


def test_python_plan_ingress_preserves_exact_integer_tokens(plan: Path) -> None:
    record = qualification.GeminiQualificationPlanV1.model_validate_json(
        (plan / "plan.json").read_bytes()
    )
    with pytest.raises(ValueError):
        qualification.GeminiQualificationPlanV1.model_validate(
            dict(record) | {"plan_version": 1.0}
        )


def test_cli_plan_report_and_safe_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(qualification, "_source_commit", lambda: "a" * 40)
    directory = tmp_path / "proposal"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "qualify_gemini.py",
            "plan",
            "--source-commit",
            "a" * 40,
            "--evidence-dir",
            str(directory),
        ],
    )
    qualification.main()
    assert json.loads(capsys.readouterr().out)["executed_requests"] == 0
    before = _files(directory)
    monkeypatch.setattr(
        sys, "argv", ["qualify_gemini.py", "report", "--evidence-dir", str(directory)]
    )
    qualification.main()
    assert json.loads(capsys.readouterr().out)["execution_enabled"] is False
    assert _files(directory) == before
    (directory / "plan.sha256").write_text("0" * 64)
    with pytest.raises(SystemExit) as failure:
        qualification.main()
    assert failure.value.code == 2
    assert "no provider operation" in capsys.readouterr().err


@pytest.mark.parametrize("violation", ["extra_dependencies", "foreign_transport"])
def test_proposal_preparation_cannot_expand_physical_call_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, violation: str
) -> None:
    from collections.abc import Generator, Iterator
    from contextlib import contextmanager

    monkeypatch.setattr(qualification, "_source_commit", lambda: "a" * 40)
    original = qualification.prepare_run

    @contextmanager
    def tampered(*args: Any, **kwargs: Any) -> Generator[Any]:
        with original(*args, **kwargs) as prepared:
            if violation == "extra_dependencies":
                dependencies = tuple(prepared.iter_request_dependencies())

                def extra(self: Any) -> Iterator[Any]:
                    return iter(dependencies * 2)

                monkeypatch.setattr(
                    type(prepared),
                    "iter_request_dependencies",
                    extra,
                )
            else:

                def foreign(*args: Any) -> object:
                    return object()

                monkeypatch.setattr(
                    type(prepared), "prepare_transport_attempt", foreign
                )
            yield prepared

    monkeypatch.setattr(qualification, "prepare_run", tampered)
    with pytest.raises(qualification.QualificationError):
        qualification.build_plan(
            directory=tmp_path / "proposal", source_commit="a" * 40
        )
    assert not (tmp_path / "proposal" / "plan.json").exists()


def test_reviewed_sdk_and_http_source_bytes_remain_pinned() -> None:
    from importlib.metadata import distribution

    profile = json.loads(
        (
            Path(__file__).parent / "fixtures/production_gemini/sdk-profile.json"
        ).read_bytes()
    )
    for package, relative, field in (
        ("google-genai", "google/genai", "sdk_sources"),
        ("httpx2", "httpx2", "http_sources"),
    ):
        dist = distribution(package)
        assert (
            dist.version
            == profile["sdk_version" if package == "google-genai" else "http_version"]
        )
        for name, digest in profile[field].items():
            assert (
                hashlib.sha256(
                    Path(str(dist.locate_file(relative + "/" + name))).read_bytes()
                ).hexdigest()
                == digest
            )
    historical = json.loads(
        (
            Path(__file__).parent / "fixtures/native_google/documentation.json"
        ).read_bytes()
    )
    assert all(
        profile["sdk_sources"][name] == source["sha256"]
        for name, source in historical["source_files"].items()
    )
