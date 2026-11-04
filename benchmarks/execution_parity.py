"""Compare two implementations using identical source files and restored stores.

Development only. Exact synthetic prompts, payloads, responses and exports pass
through child-process pipes for comparison in memory; only hashes/counts are
written to the report. Neither revision's minimal store receives those bytes.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Literal, cast

from provenance import runtime_identity, source_fingerprint


def fixtures(root: Path) -> None:
    inputs = root / "inputs"
    inputs.mkdir(parents=True)
    (inputs / "responses.jsonl").write_text(
        "".join(
            json.dumps(
                {"item_id": f"{i:03d}", "text": f"Synthetic résumé {i}.\nLine two."}
            )
            + "\n"
            for i in range(3)
        ),
        encoding="utf-8",
    )
    (inputs / "pairs.jsonl").write_text(
        "".join(
            json.dumps(
                {
                    "pair_id": "001",
                    "pair_uid": f"{i:03d}",
                    "left_item_id": "000",
                    "right_item_id": "001",
                    "trait_id": "000",
                    "presented_order": ["001", "000"] if i else ["000", "001"],
                }
            )
            + "\n"
            for i in range(2)
        ),
        encoding="utf-8",
    )
    (inputs / "advanced.txt").write_text(
        "{% for entry in traits %}{{ entry['block'] }}\n{% endfor %}"
        "{{ exemplars }}\n{{ response }}\n{{ output_instructions }}\n",
        encoding="utf-8",
    )
    for capture in ("full", "minimal"):
        for kind in ("independent", "grouped", "retry", "pairwise", "advanced"):
            spec: dict[str, Any] = {
                "task_kind": "pairwise" if kind == "pairwise" else "rubric",
                "input": "responses.jsonl",
                "provider": "mock",
                "endpoint": "synthetic",
                "model": "fixture",
                "capture": capture,
                "generation": {"temperature": 0, "max_output_tokens": 512},
                "execution": {"concurrency": 1, "explanations": True},
                "exemplars": [{"exemplar_id": "001", "text": "Synthetic exemplar."}],
            }
            if kind == "pairwise":
                spec["pairs"] = "pairs.jsonl"
                spec["criteria"] = [
                    {"trait_id": "000", "name": "Quality", "description": "Compare."}
                ]
            else:
                spec["rubric"] = {
                    "rubric_id": "001",
                    "name": "Synthetic rubric",
                    "traits": [
                        {
                            "trait_id": f"{i:03d}",
                            "name": "Quality",
                            "description": f"Assess trait {i}.",
                            "levels": [
                                {"label": label, "numeric_value": value}
                                for label, value in (
                                    ("low", 1),
                                    ("middle", 3),
                                    ("high", 8),
                                )
                            ],
                        }
                        for i in range(3)
                    ],
                }
                if kind != "independent":
                    spec["execution"]["grouping"] = "grouped"
                    spec["execution"]["groups"] = [["002", "000", "001"]]
                if kind == "advanced":
                    spec["prompt"] = {"file": "advanced.txt", "advanced": True}
            (inputs / f"{capture}-{kind}.json").write_text(
                json.dumps(spec, ensure_ascii=False), encoding="utf-8"
            )


def child(root: Path, phase: str) -> object:
    import asyncio

    from judgmentllm.config import load_run_spec
    from judgmentllm.execution import execute_run, recover_run
    from judgmentllm.execution.exports import export_run
    from judgmentllm.execution.retry import authorize_subset_retry
    from judgmentllm.persistence.repository import Repository
    from judgmentllm.preparation import RequestDependency, prepare_run
    from judgmentllm.prompts import RenderedPrompt
    from judgmentllm.providers import (
        MockAdapter,
        MockMemberScenario,
        MockScenario,
        PreparedRequest,
        ProviderResponse,
    )

    ledger = root / "ledger.sqlite3"
    if phase == "seed":
        MockAdapter(ledger)
        return None
    store = root / "store"
    cases: dict[str, object] = {}
    for path in sorted((root / "inputs").glob("full-*.json")) + sorted(
        (root / "inputs").glob("minimal-*.json")
    ):
        run_id = path.stem
        spec = load_run_spec(path)
        calls: dict[str, str] = {}
        responses: dict[str, str] = {}

        class RecordingAdapter(MockAdapter):
            payloads: dict[str, str]
            raw_responses: dict[str, str]

            def prepare(
                self,
                spec: Any,
                dependency: RequestDependency,
                rendered: RenderedPrompt,
            ) -> PreparedRequest:
                result = super().prepare(spec, dependency, rendered)
                self.payloads[result.payload_sha256] = base64.b64encode(
                    result.payload
                ).decode()
                return result

            async def retrieve(
                self,
                prepared: PreparedRequest,
                *,
                submission_id: str,
                correlation_id: str,
            ) -> ProviderResponse:
                result = await super().retrieve(
                    prepared, submission_id=submission_id, correlation_id=correlation_id
                )
                self.raw_responses[result.observation_key] = base64.b64encode(
                    result.raw
                ).decode()
                return result

        adapter = RecordingAdapter(
            ledger,
            scenario=MockScenario(
                probabilities=True,
                category_index=0,
                reverse_members=True,
                members={"001": MockMemberScenario(outcome="invalid_category")}
                if run_id.endswith("retry")
                else {},
            ),
        )
        adapter.payloads, adapter.raw_responses = calls, responses
        initial_acceptances = adapter.ledger.acceptance_count()
        if phase == "prepare":
            with prepare_run(
                path,
                store=store,
                run_id=run_id,
                capabilities=adapter.capabilities(spec),
            ) as prepared:
                dependencies: list[dict[str, object]] = []
                for dependency in prepared.iter_request_dependencies():
                    rendered = prepared.render(dependency)
                    adapter.prepare(spec, dependency, rendered)
                    dependencies.append(
                        {
                            "dependency": dependency.model_dump(mode="json"),
                            "prompt": rendered.text,
                        }
                    )
                cases[run_id] = {
                    "manifest": prepared.manifest.model_dump(mode="json"),
                    "dependencies": dependencies,
                    "payloads": calls,
                }
            continue
        asyncio.run(execute_run(run_id, store=store, adapter=adapter, spec=spec))
        if run_id.endswith("retry"):
            with Repository(store) as repository:
                failed = [
                    r for r in repository.iter_results(run_id) if r.status != "complete"
                ]
            adapter.scenario = MockScenario(
                probabilities=True, category_index=0, reverse_members=True
            )
            for result in failed:
                authorize_subset_retry(
                    run_id,
                    store=store,
                    adapter=adapter,
                    task_ids=[result.task_id],
                    operation_id="retry-" + result.task_id,
                    spec=spec,
                )
            asyncio.run(recover_run(run_id, store=store, adapter=adapter, spec=spec))
        completed_calls = adapter.submit_calls
        completed_acceptances = adapter.ledger.acceptance_count()
        asyncio.run(recover_run(run_id, store=store, adapter=adapter, spec=spec))
        assert adapter.submit_calls == completed_calls
        assert adapter.ledger.acceptance_count() == completed_acceptances
        with Repository(store) as repository:
            requests = [
                {
                    **r.model_dump(mode="json"),
                    "members": repository.request_task_ids(run_id, r.request_uid),
                }
                for r in repository.iter_requests(run_id)
            ]
            attempts = [
                {
                    **a.model_dump(mode="json"),
                    "members": repository.attempt_task_ids(run_id, a.attempt_id),
                }
                for a in repository.iter_attempts(run_id)
            ]
            submissions = [
                s.model_dump(mode="json") for s in repository.iter_submissions(run_id)
            ]
            observations = [
                o.model_dump(mode="json")
                for s in repository.iter_submissions(run_id)
                if (
                    o := repository.get_observation_for_submission(
                        run_id, s.submission_id
                    )
                )
                is not None
            ]
            acceptances = [
                a.model_dump(mode="json")
                for s in repository.iter_submissions(run_id)
                if (a := adapter.ledger.get(s.submission_id)) is not None
            ]
            assert len(acceptances) == len(submissions)
            assert completed_acceptances - initial_acceptances == sum(
                a["acceptance_count"] for a in acceptances
            )
            results = [
                r.model_dump(mode="json") for r in repository.iter_results(run_id)
            ]
            retries = [
                dict(r)
                for r in repository.connection.execute(
                    "SELECT * FROM retry_operations WHERE run_id=? ORDER BY operation_id",
                    (run_id,),
                )
            ]
        exports = {}
        combinations: tuple[
            tuple[Literal["jsonl", "csv"], Literal["canonical", "r"]], ...
        ] = (
            ("jsonl", "canonical"),
            ("csv", "canonical"),
            ("csv", "r"),
        )
        for format, view in combinations:
            exported = export_run(
                run_id, store=store, format=format, view=view, page_size=2
            )
            exports[f"{format}-{view}"] = base64.b64encode(
                Path(exported.artifact_path).read_bytes()
            ).decode()
        cases[run_id] = {
            "requests": requests,
            "attempts": attempts,
            "submissions": submissions,
            "observations": observations,
            "acceptances": acceptances,
            "accepted_delta": completed_acceptances - initial_acceptances,
            "submit_calls": completed_calls,
            "results": results,
            "retries": retries,
            "exports": exports,
            "payloads": calls,
            "responses": responses,
        }
    return cases


def operational_times(value: Any) -> Any:
    """Only these declared operational timestamps are excluded from comparison."""
    if isinstance(value, dict):
        return {
            k: operational_times(v)
            for k, v in cast(dict[str, Any], value).items()
            if k
            not in {
                "created_at",
                "updated_at",
                "occurred_at",
                "received_at",
                "acknowledged_at",
            }
        }
    if isinstance(value, list):
        return [operational_times(v) for v in cast(list[Any], value)]
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--child", choices=("seed", "prepare", "execute"))
    options = parser.parse_args()
    root = options.directory.resolve()
    if options.child:
        print(json.dumps(child(root, options.child), ensure_ascii=False))
        return
    if options.baseline is None or options.candidate is None or root.exists():
        parser.error("baseline/candidate and a new directory are required")
    fixtures(root)

    def invoke(source: Path, phase: str) -> Any:
        environment = {**os.environ, "PYTHONPATH": str(source.resolve() / "src")}
        result = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--directory",
                str(root),
                "--child",
                phase,
            ],
            env=environment,
            capture_output=True,
            text=True,
            timeout=180,
            check=True,
        )
        assert not result.stderr, result.stderr
        return json.loads(result.stdout)

    invoke(options.baseline, "seed")
    backup = root / "initial-ledger.sqlite3"
    shutil.copyfile(root / "ledger.sqlite3", backup)
    baseline_prepare = invoke(options.baseline, "prepare")
    shutil.copytree(root / "store", root / "initial-store")
    shutil.rmtree(root / "store")
    candidate_prepare = invoke(options.candidate, "prepare")
    assert baseline_prepare == candidate_prepare, "prepared requests differ"
    shutil.rmtree(root / "store")
    shutil.copytree(root / "initial-store", root / "store")
    baseline = operational_times(invoke(options.baseline, "execute"))
    shutil.rmtree(root / "store")
    shutil.copytree(root / "initial-store", root / "store")
    for path in root.glob("ledger.sqlite3*"):
        path.unlink()
    shutil.copyfile(backup, root / "ledger.sqlite3")
    candidate = operational_times(invoke(options.candidate, "execute"))
    assert baseline == candidate, "execution history or exact bytes differ"
    encoded = json.dumps(candidate, ensure_ascii=False, sort_keys=True).encode()
    report = {
        "cases": len(candidate),
        "equal": True,
        "comparison_sha256": hashlib.sha256(encoded).hexdigest(),
        "baseline_source_sha256": source_fingerprint(options.baseline.resolve()),
        "candidate_source_sha256": source_fingerprint(options.candidate.resolve()),
        "parity_harness_sha256": hashlib.sha256(
            Path(__file__).read_bytes()
        ).hexdigest(),
        "runtime": runtime_identity(),
    }
    (root / "parity.json").write_text(json.dumps(report), encoding="utf-8")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
