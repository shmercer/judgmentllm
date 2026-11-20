"""Explicit-cache contracts through the CLI, durable run and pinned SDK."""

# pyright: reportPrivateUsage=false
from __future__ import annotations

import json
import socket
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx2
import pytest
from durable_evidence_fixture import specification
from test_openai_lifecycle import envelope
from typer.testing import CliRunner

from judgmentllm import cache_advisory, lifecycle
from judgmentllm.cli import app
from judgmentllm.config import RunSpecV3, load_run_spec
from judgmentllm.output_contracts import RubricOutputContract, wire_winner_ids
from judgmentllm.persistence.artifacts import ArtifactStore
from judgmentllm.persistence.hashing import bytes_digest
from judgmentllm.persistence.repository import ReadOnlyRepository
from judgmentllm.preparation import open_prepared_run, prepare_run
from judgmentllm.production_transport import (
    PreparedRequestV4,
    TransportCommitmentV2,
    production_context,
)
from judgmentllm.providers import openai
from judgmentllm.results import PairwiseResult

_STUDENTS = ("Student canary α e\u0301 雪", "Student canary β 👩🏽‍💻")
_IDS = ("source-student-0001", "source-student-0002")
_RUBRIC = "Criterion 雪"
_EXEMPLAR = "Frozen synthetic reference e\u0301 👩🏽‍💻"
_METADATA = "Approved public example metadata 雪"
_RUN_ID = "exact-advisory-fixture"


def _forbidden(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError("test must never reach credentials or the external network")


@pytest.fixture(autouse=True)
def blocked_network_and_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _forbidden)
    monkeypatch.setattr(socket.socket, "connect", _forbidden)
    monkeypatch.setattr(openai, "_resolve_credentials", _forbidden)
    monkeypatch.delenv("TIKTOKEN_CACHE_DIR", raising=False)


def _config(tmp_path: Path, *, pairwise: bool = False) -> Path:
    config = specification(
        tmp_path / "source", capture="minimal", pairwise=pairwise, explanations=False
    )
    data = json.loads(config.read_text(encoding="utf-8"))
    data.update(
        schema_version=3,
        provider="openai",
        endpoint="responses",
        model="gpt-6-luna",
        account_scope="synthetic-account",
        strict_capabilities=True,
        exemplars=[
            {
                "exemplar_id": "stable-example",
                "text": _EXEMPLAR,
                "metadata": {"public": _METADATA},
            }
        ],
    )
    data["execution"]["cache_policy"] = "explicit"
    data["prompt"].update(
        structured_output="native",
        native_mechanism="openai.responses.text.format",
        cache_breakpoints=[
            {"after": "assessment"},
            {"after": "exemplar", "exemplar_id": "stable-example"},
        ],
    )
    records = [
        {"item_id": ident, "text": value}
        for ident, value in zip(_IDS, _STUDENTS, strict=True)
    ]
    Path(data["input"]).write_text(
        "".join(json.dumps(row) + "\n" for row in records), encoding="utf-8"
    )
    if pairwise:
        pairs = Path(data["pairs"])
        standard = json.loads(pairs.read_text(encoding="utf-8"))
        standard.update(
            left_item_id=_IDS[0], right_item_id=_IDS[1], presented_order=list(_IDS)
        )
        reversed_pair = {
            **standard,
            "pair_uid": "private-reversed-occurrence",
            "source_pair_uid": standard["pair_uid"],
            "presented_order": list(reversed(_IDS)),
            "is_reversal": True,
            "reversal_group": "private-reversal-group",
        }
        pairs.write_text(
            json.dumps(standard) + "\n" + json.dumps(reversed_pair) + "\n",
            encoding="utf-8",
        )
        data["criteria"][0]["description"] = _RUBRIC
    else:
        data["rubric"]["description"] = _RUBRIC
    config.write_text(json.dumps(data), encoding="utf-8")
    return config


def _estimated(
    joined: int, blocks: int, *, second_long: bool = False
) -> cache_advisory.Estimator:
    def count(text: str) -> int:
        assert all(value not in text for value in (*_IDS, *_STUDENTS))
        if _METADATA in text and _RUBRIC not in text:
            return 1000 if second_long else blocks - joined
        if _METADATA in text:
            return 2023 if second_long else joined
        return joined

    return "ready", count


def _cli(args: list[str], *, expected: int = 0) -> dict[str, Any]:
    result = CliRunner().invoke(app, [*args, "--json"], catch_exceptions=False)
    assert result.exit_code == expected, result.output
    body = json.loads(result.stdout)
    serialized = json.dumps(body, ensure_ascii=False) + result.stderr
    assert all(s not in serialized for s in (*_STUDENTS, *_IDS, _EXEMPLAR, _METADATA))
    return body


def _sdk_fixture(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fail_first_group: bool = False,
    reject: int | None = None,
) -> list[tuple[PreparedRequestV4, dict[str, Any]]]:
    sdk_calls: list[tuple[PreparedRequestV4, dict[str, Any]]] = []

    def respond(request: httpx2.Request) -> httpx2.Response:
        wire = openai._INVOKING.get()
        assert isinstance(wire, PreparedRequestV4)
        assert request.url.path == "/v1/responses"
        assert request.content == wire.payload
        assert bytes_digest(request.content) == wire.transport_commitment.payload_sha256
        sdk_calls.append((wire, json.loads(request.content)))
        if reject is not None:
            return httpx2.Response(
                reject,
                json={"error": {"message": "synthetic provider rejection"}},
                request=request,
            )
        contract = wire.output_commitment.contract
        if isinstance(contract, RubricOutputContract):
            output = {
                "scores": [
                    {
                        "trait_id": trait.trait_id,
                        "category_label": "synthetic-invalid"
                        if fail_first_group and len(sdk_calls) == 1 and index == 1
                        else trait.levels[0].label,
                    }
                    for index, trait in enumerate(contract.traits)
                ]
            }
        else:
            output = {"winner_id": wire_winner_ids(contract)[0]}
        reply = envelope("responses", json.dumps(output))
        reply["service_tier"] = "default"
        reply["id"] = f"synthetic-{len(sdk_calls)}"
        details = reply["usage"]["input_tokens_details"]
        if len(sdk_calls) == 1:
            details["cached_tokens"] = 0
        else:
            details.pop("cached_tokens")
        if len(sdk_calls) > 1:
            details["cache_write_tokens"] = 0
        return httpx2.Response(200, json=reply, request=request)

    def adapter(
        provider: str, endpoint: str, model: str, *args: Any, **kwargs: Any
    ) -> openai.OpenAIAdapter:
        assert (provider, endpoint, model) == ("openai", "responses", "gpt-6-luna")
        return openai.OpenAIAdapter(
            endpoint, model, http_transport=httpx2.MockTransport(respond)
        )

    monkeypatch.setattr(lifecycle, "_execution_adapter", adapter)
    monkeypatch.setattr(
        lifecycle, "uuid", SimpleNamespace(uuid4=lambda: SimpleNamespace(hex=_RUN_ID))
    )

    def synthetic_credentials(_provider: str) -> SimpleNamespace:
        return SimpleNamespace(_api_key=lambda: "synthetic-sdk-key")

    monkeypatch.setattr(openai, "_resolve_credentials", synthetic_credentials)
    # Tests alone can exercise the exact SDK boundary while the shipped deny is intact.
    monkeypatch.setattr(openai, "_require_explicit_cache_authority", lambda: None)
    return sdk_calls


def _assert_wire(wire: PreparedRequestV4, body: dict[str, Any]) -> list[dict[str, Any]]:
    assert body["model"] == "gpt-6-luna"
    assert body["service_tier"] == "default"
    assert body["store"] is False and body["stream"] is False
    assert body["prompt_cache_options"] == {"mode": "explicit"}
    assert not any("retention" in key for key in body)
    assert len(body["input"]) == 1 and body["input"][0]["role"] == "user"
    blocks = body["input"][0]["content"]
    assert all(block["type"] == "input_text" for block in blocks)
    assert all(
        block["prompt_cache_breakpoint"] == {"mode": "explicit"}
        for block in blocks[:-1]
    )
    assert "prompt_cache_breakpoint" not in blocks[-1]
    assert "".join(block["text"] for block in blocks) == wire.rendered_text
    assert all(
        value not in "".join(b["text"] for b in blocks[:-1])
        for value in (*_STUDENTS, *_IDS)
    )
    projection = wire.output_commitment.projection
    assert projection is not None
    assert body["text"] == json.loads(projection.format_fragment_text)["text"]
    layout = wire.transport_commitment.explicit_cache
    assert layout is not None and layout.effective_ttl == "30m"
    for index, prefix in enumerate(layout.prefixes, 1):
        encoded = "".join(block["text"] for block in blocks[:index]).encode()
        assert prefix.prefix_bytes == len(encoded)
        assert prefix.prefix_sha256 == bytes_digest(encoded)
    return blocks


def _receipts(store: Path, spec: RunSpecV3) -> tuple[TransportCommitmentV2, ...]:
    with open_prepared_run(
        _RUN_ID,
        store=store,
        spec=spec,
        transport_context=production_context(spec),
        capability_resolver=openai.OpenAIAdapter(
            spec.endpoint, spec.model
        ).capabilities,
    ) as prepared:
        attempts = tuple(prepared.repository.iter_attempts(_RUN_ID))
        receipts = tuple(prepared.transport_for_attempt(x.attempt_id) for x in attempts)
    assert all(isinstance(receipt, TransportCommitmentV2) for receipt in receipts)
    return tuple(
        receipt for receipt in receipts if isinstance(receipt, TransportCommitmentV2)
    )


@pytest.mark.parametrize(
    "estimator, first_warning",
    [
        (_estimated(1023, 1023, second_long=True), ["short"]),
        (_estimated(1024, 1024), ["borderline"]),
        (_estimated(1022, 1025), ["short"]),
        (("asset_unavailable", None), ["unknown"]),
    ],
    ids=["short-then-long", "borderline", "disagreement", "unknown"],
)
def test_advisory_never_changes_wire_or_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    estimator: cache_advisory.Estimator,
    first_warning: list[str],
) -> None:
    config = _config(tmp_path)
    spec = load_run_spec(config)
    assert isinstance(spec, RunSpecV3)
    calls = _sdk_fixture(monkeypatch)
    monkeypatch.setattr(cache_advisory, "_estimator", lambda: estimator)
    report = _cli(["templates", "preview", "--config", str(config)])["data"]
    assert report["qualification"] == "offline_candidate"
    assert report["physical_requests"] == 2
    markers = report["requests"][0]["markers"]
    assert markers[0]["warnings"] == first_warning
    if first_warning == ["short"] and estimator[1] is not None:
        if estimator[1](_RUBRIC + _METADATA) == 2023:
            assert [marker["joined_estimate"] for marker in markers] == [1023, 2023]
            assert markers[1]["warnings"] == []
        else:
            assert markers[1]["warnings"] == ["borderline", "disagreement"]

    first_store = tmp_path / "first"
    run = ["run", "--config", str(config), "--store"]
    assert _cli([*run, str(first_store)])["run_status"] == "completed"
    first_calls = tuple(calls)
    assert len(first_calls) == 2
    for wire, body in first_calls:
        blocks = _assert_wire(wire, body)
        assert len(blocks) == 3
        assert blocks[0]["text"].endswith("**Category `low`**")
        assert _EXEMPLAR in blocks[1]["text"] and blocks[1]["text"].endswith('雪"}')
        assert any(student in blocks[2]["text"] for student in _STUDENTS)
    assert (
        first_calls[0][0].transport_commitment.explicit_cache
        == first_calls[1][0].transport_commitment.explicit_cache
    )

    monkeypatch.setattr(
        cache_advisory, "_estimator", lambda: ("tokenizer_unavailable", None)
    )
    second_store = tmp_path / "second"
    assert _cli([*run, str(second_store)])["run_status"] == "completed"
    assert {wire.request_uid: wire.payload for wire, _ in first_calls} == {
        wire.request_uid: wire.payload for wire, _ in calls[2:]
    }
    assert _receipts(first_store, spec) == _receipts(second_store, spec)


@pytest.mark.parametrize("fault", ["missing", "unreadable", "corrupt", "write"])
def test_optional_companion_faults_across_public_preflight_submit_and_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    config = _config(tmp_path)
    spec = load_run_spec(config)
    assert isinstance(spec, RunSpecV3)
    calls = _sdk_fixture(monkeypatch, fail_first_group=True)
    monkeypatch.setattr(cache_advisory, "_estimator", lambda: _estimated(1023, 1023))
    original_publish = ArtifactStore.publish_reference
    original_open = Path.open
    publications: list[tuple[str, bool, bool]] = []

    def publish(store: ArtifactStore, identity: Any, name: str) -> Path:
        if not name.startswith("cache-advisories/"):
            return original_publish(store, identity, name)
        with ReadOnlyRepository(store.root.parents[1]) as repo:
            (run_id,) = repo.connection.execute("SELECT run_id FROM runs").fetchone()
            sealed = repo.get_run(run_id).manifest_sha256 is not None
            published_retry = bool(
                repo.connection.execute(
                    "SELECT 1 FROM submissions JOIN attempts USING (run_id, attempt_id) "
                    "WHERE run_id = ? AND attempt_number = 2",
                    (run_id,),
                ).fetchone()
            )
        publications.append((name, sealed, published_retry))
        path = store.reference_path(name)
        if fault == "write":
            raise OSError("private advisory write failure")
        if fault == "missing":
            return path
        path = original_publish(store, identity, name)
        if fault == "corrupt":
            path.write_bytes(b"corrupt private diagnostic")
        return path

    def open_path(path: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if (
            fault == "unreadable"
            and mode == "rb"
            and path.parent.name == "cache-advisories"
        ):
            raise PermissionError("private diagnostic read failure")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(ArtifactStore, "publish_reference", publish)
    monkeypatch.setattr(Path, "open", open_path)
    for args in (["validate"], ["plan"], ["templates", "preview"]):
        data = _cli([*args, "--config", str(config)])["data"]
        assert data.get("valid", True) is True
        report = data.get("cache_advisory", data)
        assert len(report["requests"]) == 2
        for diagnostic in report["requests"]:
            assert diagnostic["asset_status"] == "advisory_unavailable"
            assert all(
                marker["warnings"] == ["unknown"] for marker in diagnostic["markers"]
            )
    assert calls == []

    store = tmp_path / "run"
    options = ["--store", str(store), "--config", str(config)]
    _cli(["run", *options], expected=3)
    assert len(calls) == 2
    receipts_before = _receipts(store, spec)
    for wire, body in calls:
        _assert_wire(wire, body)
        assert wire.transport_commitment in receipts_before
    with ReadOnlyRepository(store) as repo:
        (failed,) = [
            row.task_id for row in repo.iter_results(_RUN_ID) if row.status == "invalid"
        ]
    _cli(["resume", "--run", _RUN_ID, *options], expected=3)
    assert len(calls) == 2 and _receipts(store, spec) == receipts_before

    retry = [
        "resume",
        "--run",
        _RUN_ID,
        *options,
        "--retry-task",
        failed,
        "--retry-id",
        "one-subset",
    ]
    assert _cli(retry)["run_status"] == "completed"
    assert len(calls) == 3
    subset, body = calls[-1]
    _assert_wire(subset, body)
    prior = next(
        receipt
        for receipt in receipts_before
        if receipt.identity.request_uid == subset.request_uid
    )
    committed = subset.transport_commitment
    assert committed.identity.attempt_id != prior.identity.attempt_id
    assert committed.explicit_cache != prior.explicit_cache
    assert committed.payload_sha256 != prior.payload_sha256
    assert committed.parent_commitment_sha256 == prior.commitment_sha256
    assert all(sealed for _, sealed, _ in publications)
    name = f"cache-advisories/{committed.commitment_sha256}.json"
    assert [published for path, _, published in publications if path == name] == [True]
    assert _cli(retry)["run_status"] == "completed"
    assert len(calls) == 3
    with ReadOnlyRepository(store) as repo:
        evidence = tuple(repo.iter_evidence(_RUN_ID))
        assert len(evidence) == 3
        usage = [row.usage for row in evidence]
        assert all(row is not None for row in usage)
        assert [
            row.cache_read_tokens
            for row in usage
            if row and row.cache_read_tokens is not None
        ] == [0]
        assert sorted(
            row.cache_write_tokens
            for row in usage
            if row and row.cache_write_tokens is not None
        ) == [0, 0]


def test_reversal_native_parity_and_exports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, pairwise=True)
    calls = _sdk_fixture(monkeypatch)
    store = tmp_path / "run"
    _cli(["run", "--config", str(config), "--store", str(store)])
    first, second = (_assert_wire(wire, body) for wire, body in calls)
    assert first[:-1] == second[:-1]
    assert first[-1]["text"].index(_STUDENTS[0]) < first[-1]["text"].index(_STUDENTS[1])
    assert second[-1]["text"].index(_STUDENTS[0]) > second[-1]["text"].index(
        _STUDENTS[1]
    )
    for wire, body in calls:
        assert (
            "private-reversed-occurrence" not in body["input"][0]["content"][0]["text"]
        )
        assert "private-reversal-group" not in wire.rendered_text
    with ReadOnlyRepository(store) as repo:
        normal, reverse = tuple(repo.iter_results(_RUN_ID))
        assert isinstance(normal, PairwiseResult) and isinstance(
            reverse, PairwiseResult
        )
        assert normal.original_order == reverse.original_order == list(_IDS)
        assert normal.presented_order == list(_IDS)
        assert reverse.presented_order == list(reversed(_IDS))
        assert (normal.winner_id, reverse.winner_id) == _IDS
    assert lifecycle.export_run_results(
        _RUN_ID, store=store, format="csv", view="r"
    ).files


def test_four_markers_and_damaged_rank_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    data = json.loads(config.read_text(encoding="utf-8"))
    data["exemplars"].append(
        {"exemplar_id": "second-example", "text": "Another frozen exemplar"}
    )
    data["prompt"]["cache_breakpoints"].extend(
        [
            {"after": "exemplar", "exemplar_id": "second-example"},
            {"after": "output_instructions"},
        ]
    )
    config.write_text(json.dumps(data), encoding="utf-8")
    (tmp_path / "fb374d419588a4632f3f557e76b4b70aebbca790").write_bytes(
        b"bad rank file"
    )
    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(cache_advisory, "import_module", _forbidden)
    report = _cli(["templates", "preview", "--config", str(config)])["data"]
    first = report["requests"][0]
    assert first["asset_status"] == "asset_invalid"
    assert [(m["anchor"], m["warnings"]) for m in first["markers"]] == [
        (anchor, ["unknown"])
        for anchor in ("assessment", "exemplar", "exemplar", "output_instructions")
    ]
    calls = _sdk_fixture(monkeypatch)
    _cli(["run", "--config", str(config), "--store", str(tmp_path / "store")])
    assert len(calls) == 2
    for wire, body in calls:
        blocks = _assert_wire(wire, body)
        assert len(blocks) == 5
        assert "Another frozen exemplar" in blocks[2]["text"]
        assert blocks[3]["text"].startswith("\n\n## Output Format")
        assert any(student in blocks[4]["text"] for student in _STUDENTS)


@pytest.mark.parametrize(
    "setting, value",
    [
        ("model", "gpt-6.1-terra"),
        ("provider", "anthropic"),
        ("provider", "gemini"),
        ("endpoint", "chat.completions"),
        ("endpoint", "decisions"),
        ("execution.mode", "bulk_live"),
        ("execution.mode", "batch_native"),
        ("execution.tier", "flex"),
        ("prompt.structured_output", "auto"),
        ("prompt.advanced", True),
        ("prompt.cache_ttl", "2h"),
        ("prompt.cache_breakpoints", []),
        ("prompt.cache_breakpoints", [{"after": "response"}]),
        ("prompt.cache_breakpoints", [{"after": "exemplar"}]),
        ("prompt.cache_breakpoints", [{"after": "assessment"}] * 5),
        ("execution.cache_policy", "off"),
        ("execution.cache_policy", "auto"),
    ],
)
def test_outside_exact_tuple_fails_preflight(
    tmp_path: Path, setting: str, value: Any
) -> None:
    config = _config(tmp_path)
    data = json.loads(config.read_text(encoding="utf-8"))
    parent, _, child = setting.rpartition(".")
    (data[parent] if parent else data)[child] = value
    config.write_text(json.dumps(data), encoding="utf-8")
    _cli(["validate", "--config", str(config)], expected=2)


@pytest.mark.parametrize(
    "variant", ["missing", "order", "student", "duplicate", "cohort"]
)
def test_unsafe_structures_fail_before_required_seal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    variant: str,
) -> None:
    config = _config(tmp_path)
    data = json.loads(config.read_text(encoding="utf-8"))
    markers = data["prompt"]["cache_breakpoints"]
    if variant == "missing":
        markers[1]["exemplar_id"] = "missing-exemplar"
    elif variant == "order":
        markers.reverse()
    elif variant == "duplicate":
        markers.append(markers[0])
    elif variant == "student":
        template = tmp_path / "unsafe.jinja"
        template.write_text(
            "{{ response }}\n{{ rubric }}\n{{ exemplars }}\n{{ output_instructions }}",
            encoding="utf-8",
        )
        data["prompt"]["file"] = str(template)
    config.write_text(json.dumps(data), encoding="utf-8")
    if variant == "cohort":
        from judgmentllm import production_prompts

        original = production_prompts._render_text_v3
        count = 0

        def unstable(*args: Any, **kwargs: Any) -> str:
            nonlocal count
            rendered = original(*args, **kwargs)
            count += 1
            return rendered.replace(_RUBRIC, "Criterion 雨") if count == 2 else rendered

        monkeypatch.setattr(production_prompts, "_render_text_v3", unstable)

    def no_estimator() -> cache_advisory.Estimator:
        pytest.fail("unsafe preflight must fail before any estimator is called")

    monkeypatch.setattr(cache_advisory, "_estimator", no_estimator)
    _cli(["plan", "--config", str(config)], expected=2 if variant == "duplicate" else 1)


def test_installed_sdk_mismatch_and_actual_authority_deny(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from judgmentllm import planning

    config = _config(tmp_path)

    def wrong_sdk(_package: str) -> str:
        return "3.42.0"

    with monkeypatch.context() as bad_sdk:
        bad_sdk.setattr(planning, "version", wrong_sdk)
        _cli(["validate", "--config", str(config)], expected=2)
    _cli(["plan", "--config", str(config)])
    no_store = tmp_path / "no-store"
    _cli(["run", "--config", str(config), "--store", str(no_store)], expected=2)
    assert not no_store.exists()

    spec = load_run_spec(config)
    assert isinstance(spec, RunSpecV3)
    store = tmp_path / "prepared"
    with prepare_run(
        spec,
        store=store,
        run_id=_RUN_ID,
        transport_context=production_context(spec),
        capabilities=openai.OpenAIAdapter(spec.endpoint, spec.model).capabilities(spec),
    ):
        pass
    args = ["resume", "--run", _RUN_ID, "--config", str(config), "--store", str(store)]
    _cli(args, expected=2)
    status = lifecycle.get_run(_RUN_ID, store=store)
    assert status.attempts == 2 and status.run_status == "submitted"
    assert status.unresolved_submissions == 0


@pytest.mark.parametrize("http_status, unresolved", [(400, 0), (500, 2)])
def test_provider_rejection_and_ambiguous_submission_do_not_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, http_status: int, unresolved: int
) -> None:
    config = _config(tmp_path)
    calls = _sdk_fixture(monkeypatch, reject=http_status)
    store = tmp_path / "run"
    options = ["--store", str(store), "--config", str(config)]
    result = _cli(["run", *options], expected=3)
    assert result["data"]["unresolved_submissions"] == unresolved
    assert len(calls) == 2
    for wire, body in calls:
        _assert_wire(wire, body)
    resume = _cli(["resume", "--run", _RUN_ID, *options], expected=3)
    assert resume["data"]["unresolved_submissions"] == unresolved
    assert resume["data"]["attempts"] == 2 and len(calls) == 2
