"""Explicit-cache contracts through the CLI, durable run and pinned SDK."""

# pyright: reportPrivateUsage=false
from __future__ import annotations

import json
import socket
from importlib.metadata import PackageNotFoundError
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal

import httpx2
import pytest
from durable_evidence_fixture import specification
from native_transport_fixture import context_for
from test_openai_lifecycle import envelope
from typer.testing import CliRunner

from judgmentllm import (
    cache_advisory,
    lifecycle,
    production_prompts,
    production_transport,
)
from judgmentllm.cache_policy import validate_openai_explicit_scope
from judgmentllm.cli import app
from judgmentllm.config import RunSpecV3, load_run_spec
from judgmentllm.output_contracts import RubricOutputContract, wire_winner_ids
from judgmentllm.persistence.artifacts import ArtifactStore
from judgmentllm.persistence.hashing import (
    bytes_digest,
    canonical_bytes,
    semantic_digest,
)
from judgmentllm.persistence.repository import ReadOnlyRepository
from judgmentllm.persistence.transport_receipts import transport_reference
from judgmentllm.planning import PlannedRequest
from judgmentllm.preparation import PreparedRun, open_prepared_run, prepare_run
from judgmentllm.preparation.freezing import PreparationError
from judgmentllm.production_transport import (
    PreparedRequestV4,
    TransportCommitmentV2,
    production_context,
)
from judgmentllm.prompts import PromptError
from judgmentllm.providers import openai
from judgmentllm.results import PairwiseResult, RubricResult
from judgmentllm.transport import TransportIntegrityError

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


def _config(
    tmp_path: Path,
    *,
    pairwise: bool = False,
    capture: Literal["full", "minimal"] = "minimal",
) -> Path:
    config = specification(
        tmp_path / "source", capture=capture, pairwise=pairwise, explanations=False
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


def _counting_fault(text: str) -> int:
    if _METADATA in text and _RUBRIC not in text:
        raise ValueError("local encoding failed at one block")
    return 1025


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


def _prepare(config: Path, store: Path) -> PreparedRun:
    spec = load_run_spec(config)
    assert isinstance(spec, RunSpecV3)
    return prepare_run(
        config,
        store=store,
        run_id=_RUN_ID,
        capabilities=openai.OpenAIAdapter(spec.endpoint, spec.model).capabilities(spec),
        transport_context=production_context(spec),
    )


def _manifest_bytes(prepared: PreparedRun) -> bytes:
    manifest = prepared.store.reference_path("manifest.json").read_bytes()
    assert manifest == canonical_bytes(prepared.manifest)
    assert bytes_digest(manifest) == prepared.manifest_sha256
    return manifest


def _seal(
    store: Path, spec: RunSpecV3
) -> tuple[bytes, tuple[TransportCommitmentV2, ...]]:
    with open_prepared_run(
        _RUN_ID,
        store=store,
        spec=spec,
        transport_context=production_context(spec),
        capability_resolver=openai.OpenAIAdapter(
            spec.endpoint, spec.model
        ).capabilities,
    ) as prepared:
        manifest = _manifest_bytes(prepared)
        attempts = tuple(prepared.repository.iter_attempts(_RUN_ID))
        receipts = tuple(prepared.transport_for_attempt(x.attempt_id) for x in attempts)
    assert all(isinstance(receipt, TransportCommitmentV2) for receipt in receipts)
    return manifest, tuple(
        receipt for receipt in receipts if isinstance(receipt, TransportCommitmentV2)
    )


@pytest.mark.parametrize(
    "estimator, first_warning",
    [
        (_estimated(1023, 1023, second_long=True), ["short"]),
        (_estimated(1024, 1024), ["borderline"]),
        (_estimated(1025, 1025), ["borderline"]),
        (_estimated(1022, 1025), ["short"]),
        (("asset_unavailable", None), ["unknown"]),
        (("ready", _counting_fault), ["borderline"]),
    ],
    ids=[
        "short-then-long",
        "borderline",
        "near-minimum",
        "disagreement",
        "unknown",
        "count-fault",
    ],
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
    assert report["requests"][0]["estimator_version"] == 2
    markers = report["requests"][0]["markers"]
    assert markers[0]["warnings"] == first_warning
    if estimator[1] is not None:
        expected = estimator[1](_RUBRIC)
        assert markers[0]["joined_estimate"] == markers[0]["block_estimate"] == expected
    if estimator[1] is _counting_fault:
        assert markers[0]["joined_estimate"] == markers[0]["block_estimate"] == 1025
        assert markers[1]["joined_estimate"] is markers[1]["block_estimate"] is None
        assert markers[1]["warnings"] == ["unknown"]
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
    assert _seal(first_store, spec) == _seal(second_store, spec)


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
    baseline = tmp_path / "baseline"
    with _prepare(config, baseline):
        pass
    baseline_seal = _seal(baseline, spec)
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
    before = _seal(store, spec)
    assert before == baseline_seal
    _, receipts_before = before
    for wire, body in calls:
        _assert_wire(wire, body)
        assert wire.transport_commitment in receipts_before
    with ReadOnlyRepository(store) as repo:
        (failed,) = [
            row.task_id for row in repo.iter_results(_RUN_ID) if row.status == "invalid"
        ]
    _cli(["resume", "--run", _RUN_ID, *options], expected=3)
    assert len(calls) == 2 and _seal(store, spec) == before

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
    assert _seal(store, spec)[0] == before[0]
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
    "fault, status",
    [
        ("missing", "asset_unavailable"),
        ("unreadable", "asset_unavailable"),
        ("wrong-version", "tokenizer_incompatible"),
        ("missing-tokenizer", "tokenizer_unavailable"),
        ("broken-ranks", "asset_invalid"),
    ],
)
def test_local_rank_faults_return_no_estimate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str, status: str
) -> None:
    asset = tmp_path / "fb374d419588a4632f3f557e76b4b70aebbca790"
    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(tmp_path))
    ranks = b"not trusted rank syntax" if fault == "broken-ranks" else b"YQ== 0\n"
    if fault != "missing":
        asset.write_bytes(ranks)
        # Deliberately isolate parser faults from the earlier digest check.
        monkeypatch.setattr(cache_advisory, "_ASSET_SHA256", bytes_digest(ranks))
    original_open = Path.open

    def open_path(path: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if fault == "unreadable" and path == asset:
            raise PermissionError("synthetic unreadable local rank")
        return original_open(path, mode, *args, **kwargs)

    def installed(_package: str) -> str:
        if fault == "missing-tokenizer":
            raise PackageNotFoundError("tiktoken")
        return "0.13.0" if fault == "wrong-version" else "0.14.0"

    with monkeypatch.context() as local:
        local.setattr(Path, "open", open_path)
        local.setattr(cache_advisory, "version", installed)
        local.setattr(cache_advisory, "import_module", _forbidden)
        assert cache_advisory._estimator() == (status, None)
    if fault != "missing":
        assert asset.read_bytes() == ranks


def test_companion_bounds_and_canonical_encoding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _prepare(_config(tmp_path), tmp_path / "run") as prepared:
        first = _frozen_wires(prepared)[0].transport_commitment
        path = prepared.store.reference_path(
            f"cache-advisories/{first.commitment_sha256}.json"
        )
        original = path.read_bytes()
        monkeypatch.setattr(cache_advisory, "_MAX_REPORTS", 1)
        for malformed in (
            original + b"\n",
            b"x" * (cache_advisory._MAX_COMPANION_BYTES + 1),
        ):
            path.write_bytes(malformed)
            report = cache_advisory.cache_advisory_report(prepared)
            assert (
                report is not None
                and report.physical_requests == 2
                and report.truncated
            )
            (unavailable,) = report.requests
            assert unavailable.asset_status == "advisory_unavailable"
            assert all(
                marker.warnings == ("unknown",) for marker in unavailable.markers
            )


def test_advisory_iteration_failure_does_not_fail_plain_preview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)

    def unavailable(_prepared: PreparedRun) -> Any:
        raise OSError("private diagnostic iteration failure")

    monkeypatch.setattr(cache_advisory, "_initial_receipts", unavailable)
    result = CliRunner().invoke(
        app, ["templates", "preview", "--config", str(config)], catch_exceptions=False
    )
    assert result.exit_code == 0 and result.stderr == ""
    report = json.loads(result.stdout)
    assert report["physical_requests"] == 2 and report["truncated"]
    assert report["requests"] == []
    selected = CliRunner().invoke(
        app,
        ["templates", "preview", "--config", str(config), "--item-id", _IDS[0]],
        catch_exceptions=False,
    )
    assert selected.exit_code == 2 and selected.stdout == ""
    assert selected.stderr == "Error: Invalid configuration or prompt selection.\n"


def test_wire_rejects_damaged_cache_layout_and_scope(tmp_path: Path) -> None:
    with _prepare(_config(tmp_path), tmp_path / "run") as prepared:
        wire = _frozen_wires(prepared)[0]
        receipt, output = wire.transport_commitment, wire.output_commitment
        layout = receipt.explicit_cache
        assert layout is not None
        first, second = layout.prefixes

        # A self-consistent attempt still requires the correct marks and text.
        for damage in (
            None,
            layout.model_copy(
                update={
                    "prefixes": (
                        first.model_copy(update={"anchor": "output_instructions"}),
                        second,
                    )
                }
            ),
            layout.model_copy(
                update={
                    "prefixes": (
                        first.model_copy(update={"prefix_sha256": "f" * 64}),
                        second,
                    )
                }
            ),
        ):
            with pytest.raises(TransportIntegrityError):
                production_transport.build_openai_request_body(
                    endpoint=wire.endpoint,
                    model=wire.model,
                    prompt=wire.rendered_text,
                    output=output,
                    generation=wire.generation,
                    cache=damage,
                )
        for update in ({"cache_breakpoints": None}, {"service_policy": None}):
            with pytest.raises(ValueError):
                type(output).model_validate({**dict(output), **update})
        for update in ({"explicit_cache": None}, {"bulk_live": True}):
            damaged = {**dict(receipt), **update}
            damaged.pop("commitment_sha256")
            damaged["commitment_sha256"] = semantic_digest(
                "transport-commitment-v2", damaged
            )
            with pytest.raises(ValueError):
                TransportCommitmentV2.model_validate(damaged)

        with pytest.raises(ValueError, match="requires exact Luna Responses"):
            validate_openai_explicit_scope(
                receipt.scope.model_copy(update={"sdk_version": "3.42.0"})
            )

        spec = prepared.spec
        assert isinstance(spec, RunSpecV3) and spec.prompt.cache_breakpoints is not None
        with pytest.raises(PreparationError, match="requires the OpenAI transport"):
            prepare_run(
                spec,
                store=tmp_path / "foreign-context",
                run_id=_RUN_ID,
                capabilities=prepared.capabilities,
                transport_context=context_for("openai.responses.text.format"),
            )
        with pytest.raises(ValueError):
            type(prepared.manifest).model_validate(
                {
                    **dict(prepared.manifest),
                    "execution": spec.execution.model_copy(
                        update={"mode": "bulk_live"}
                    ),
                }
            )
        probe = production_transport._qualification_context(
            spec, source_commit="a" * 40
        )
        with pytest.raises(TransportIntegrityError):
            production_transport.build_output_commitment_v4(
                spec, wire.logical_request, probe
            )
        config = spec.model_copy(deep=True)
        assert config.prompt.cache_breakpoints is not None
        config.prompt.cache_breakpoints.reverse()
        with pytest.raises(PromptError):
            production_prompts.render_production_request(
                config,
                None,
                wire.logical_request,
                lambda _, value: prepared.get_response(value),
                output,
            )


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
        responses = Path(data["input"])
        records = [
            json.loads(line)
            for line in responses.read_text(encoding="utf-8").splitlines()
        ]
        for record in records:
            record["text"] = _STUDENTS[0]
        responses.write_text(
            "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
        )
        template = tmp_path / "unsafe.jinja"
        template.write_text(
            "{{ response }}\n{{ rubric }}\n{{ exemplars }}\n{{ output_instructions }}",
            encoding="utf-8",
        )
        data["prompt"]["file"] = str(template)
    config.write_text(json.dumps(data), encoding="utf-8")
    if variant == "cohort":
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
    if variant == "student":
        result = CliRunner().invoke(
            app,
            ["templates", "preview", "--config", str(config)],
            catch_exceptions=False,
        )
        assert result.exit_code == 2 and result.stdout == ""
        assert result.stderr == "Error: Explicit cache preview could not be prepared.\n"


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


def _frozen_wires(prepared: PreparedRun) -> tuple[PreparedRequestV4, ...]:
    wires: list[PreparedRequestV4] = []
    for attempt in prepared.repository.iter_attempts(_RUN_ID):
        receipt = prepared.transport_for_attempt(attempt.attempt_id)
        assert isinstance(receipt, TransportCommitmentV2)
        wire = prepared.prepare_transport_attempt(
            prepared.dependency_for_request(attempt.request_uid),
            receipt.identity,
            frozen=receipt,
        )
        assert isinstance(wire, PreparedRequestV4)
        wires.append(wire)
    return tuple(wires)


@pytest.mark.parametrize("damage", ["none", "layout", "wrong-sdk", "missing-sdk"])
def test_full_capture_replay_uses_exact_seal_without_estimator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    config = _config(tmp_path, capture="full")
    store = tmp_path / "run"
    calls = _sdk_fixture(monkeypatch)
    with _prepare(config, store) as prepared:
        wires = _frozen_wires(prepared)
        manifest_path = prepared.store.reference_path("manifest.json")
        manifest = _manifest_bytes(prepared)
        receipt = wires[0].transport_commitment
        receipt_path = prepared.store.reference_path(
            transport_reference(receipt.identity.attempt_id)
        )
        repaired = receipt_path.read_bytes()
        advisory = prepared.store.reference_path(
            f"cache-advisories/{receipt.commitment_sha256}.json"
        )
        advisory.unlink()
        if damage == "layout":
            data = json.loads(repaired)
            data["explicit_cache"]["prefixes"][1]["prefix_bytes"] = data[
                "explicit_cache"
            ]["prefixes"][0]["prefix_bytes"]
            # Keep the outer commitment consistent so geometry is rejected on its own.
            data.pop("commitment_sha256")
            data["commitment_sha256"] = semantic_digest("transport-commitment-v2", data)
            receipt_path.write_bytes(canonical_bytes(data))
    config.parent.rename(tmp_path / "originals-unavailable")

    def forbid_estimation() -> cache_advisory.Estimator:
        pytest.fail("replay must not initialize the advisory estimator")

    def installed(_package: str) -> str:
        if damage == "missing-sdk":
            raise PackageNotFoundError("openai")
        return "3.42.0"

    monkeypatch.setattr(cache_advisory, "_estimator", forbid_estimation)
    resume = [
        "resume",
        "--run",
        _RUN_ID,
        "--store",
        str(store),
        "--use-frozen-snapshot",
    ]
    if damage != "none":
        with monkeypatch.context() as unavailable:
            if damage != "layout":
                unavailable.setattr(production_transport, "version", installed)
            _cli(resume, expected=1)
        assert calls == []
        status = lifecycle.get_run(_RUN_ID, store=store)
        assert status.attempts == len(wires) and status.unresolved_submissions == 0
        if damage == "layout":
            receipt_path.write_bytes(repaired)
    assert _cli(resume)["run_status"] == "completed"
    assert [(wire.payload, wire.transport_commitment) for wire, _ in calls] == [
        (wire.payload, wire.transport_commitment) for wire in wires
    ]
    assert manifest_path.read_bytes() == manifest and not advisory.exists()


def test_failed_changed_subset_leaves_no_new_intent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    spec = load_run_spec(config)
    assert isinstance(spec, RunSpecV3)
    store = tmp_path / "run"
    calls = _sdk_fixture(monkeypatch, fail_first_group=True)
    options = ["--store", str(store), "--config", str(config)]
    _cli(["run", *options], expected=3)
    before = _seal(store, spec)
    before_status = lifecycle.get_run(_RUN_ID, store=store)
    with ReadOnlyRepository(store) as repo:
        (failed,) = [
            row for row in repo.iter_results(_RUN_ID) if row.status == "invalid"
        ]
        directory = repo.run_directory(_RUN_ID)
    assert isinstance(failed, RubricResult)
    files = {path.name for path in (directory / "transports").iterdir()}
    original_render = production_prompts._render_text_v3
    changed: list[tuple[str, ...]] = []

    def misplaced(
        spec: RunSpecV3,
        template: str | None,
        request: PlannedRequest,
        *args: Any,
        **kwargs: Any,
    ) -> str:
        if request.trait_ids == (failed.trait_id,):
            changed.append(request.trait_ids)
            template = "{{ response }}\n{{ rubric }}\n{{ exemplars }}\n{{ output_instructions }}"
        return original_render(spec, template, request, *args, **kwargs)

    retry = [
        "resume",
        "--run",
        _RUN_ID,
        *options,
        "--retry-task",
        failed.task_id,
        "--retry-id",
        "changed-subset",
    ]
    with monkeypatch.context() as invalid:
        invalid.setattr(production_prompts, "_render_text_v3", misplaced)
        _cli(retry, expected=2)
    assert changed == [(failed.trait_id,)]
    assert len(calls) == 2 and _seal(store, spec) == before
    after = lifecycle.get_run(_RUN_ID, store=store)
    assert after.retry_operations == before_status.retry_operations == 0
    assert after.submission_certainty == before_status.submission_certainty
    assert {path.name for path in (directory / "transports").iterdir()} == files

    def unavailable() -> cache_advisory.Estimator:
        raise OSError("private retry-estimator initialization failure")

    monkeypatch.setattr(cache_advisory, "_estimator", unavailable)
    assert _cli(retry)["run_status"] == "completed" and len(calls) == 3
    new_receipt = calls[-1][0].transport_commitment
    assert not (
        directory / "cache-advisories" / f"{new_receipt.commitment_sha256}.json"
    ).exists()


@pytest.mark.asyncio
async def test_sdk_final_transport_guard_denies_explicit_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _prepare(_config(tmp_path), tmp_path / "run") as prepared:
        wire = _frozen_wires(prepared)[0]
    adapter = openai.OpenAIAdapter(
        wire.endpoint, wire.model, http_transport=httpx2.MockTransport(_forbidden)
    )
    identity = wire.transport_commitment.identity
    try:
        with pytest.raises(
            openai.OpenAIConfigurationError, match="execution.cache_policy"
        ):
            await adapter.submit(
                wire,
                submission_id=identity.submission_id,
                correlation_id=identity.correlation_id,
            )
        assert adapter._client is None

        def synthetic_credentials(_provider: str) -> SimpleNamespace:
            return SimpleNamespace(_api_key=lambda: "synthetic-sdk-key")

        monkeypatch.setattr(openai, "_resolve_credentials", synthetic_credentials)
        # Exercise the SDK transport directly, leaving the real deny installed.
        client = await adapter._execution_client()
        state: dict[str, Any] = {"invoked": False}
        invoking = openai._INVOKING.set(wire)
        invocation_state = openai._INVOCATION_STATE.set(state)
        try:
            with pytest.raises(
                openai.OpenAIConfigurationError, match="execution.cache_policy"
            ):
                await client.responses.with_raw_response.create(
                    **json.loads(wire.payload)
                )
        finally:
            openai._INVOCATION_STATE.reset(invocation_state)
            openai._INVOKING.reset(invoking)
        assert state == {"invoked": False} and adapter.actual_call_count == 0

        deny = openai._require_explicit_cache_authority

        def allow_only_before_serialization() -> None:
            if openai._INVOKING.get() is not None:
                deny()

        monkeypatch.setattr(
            openai, "_require_explicit_cache_authority", allow_only_before_serialization
        )
        with pytest.raises(
            openai.OpenAIConfigurationError, match="execution.cache_policy"
        ):
            await adapter.submit(
                wire,
                submission_id=identity.submission_id,
                correlation_id=identity.correlation_id,
            )
        assert adapter.actual_call_count == 0
    finally:
        await adapter.aclose()


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
