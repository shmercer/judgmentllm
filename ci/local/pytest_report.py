"""Per-case receipts for collected and distributed qualification partitions."""

from __future__ import annotations

import json
import os
from collections.abc import Generator
from pathlib import Path
from typing import Any, cast

import pytest

from .preflight_provenance import RUN_ID_ENV, receipt_provenance, source_record

_items: list[dict[str, object]] = []
_reports: list[dict[str, object]] = []
_provenance: pytest.StashKey[dict[str, Any]] = pytest.StashKey()
_WINDOWS_COMPACT_TEMP = os.name == "nt"
_coverage: Any = None
_contributors: dict[str, dict[str, Any]] = {}
_coverage_metadata: dict[str, Any] = {}


@pytest.fixture
def tmp_path(tmp_path: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    # Full node IDs remain in receipts; compact private paths leave room for SDK receipts.
    if _WINDOWS_COMPACT_TEMP:
        return tmp_path_factory.mktemp("t")
    return tmp_path


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--ci-report", help="Write the complete case receipt here")
    parser.addoption("--ci-selection", help="Exact node IDs assigned to this partition")
    parser.addoption(
        "--ci-coverage",
        action="store_true",
        help="Require normal pytest-cov contributor completion",
    )


def pytest_configure(config: pytest.Config) -> None:
    global _coverage
    _items.clear()
    _reports.clear()
    _contributors.clear()
    _coverage_metadata.clear()
    _coverage = None
    if config.getoption("--ci-report") and not hasattr(config, "workerinput"):
        config.stash[_provenance] = receipt_provenance(
            source_record(config.rootpath), os.environ.get(RUN_ID_ENV)
        )


@pytest.hookimpl(trylast=True)
def pytest_sessionstart(session: pytest.Session) -> None:
    global _coverage
    if not session.config.getoption("--ci-coverage"):
        return
    from coverage import Coverage

    _coverage = Coverage.current()
    distributed = bool(getattr(session.config.option, "numprocesses", None))
    if distributed and not hasattr(session.config, "workerinput"):
        _coverage = None
        return
    if _coverage is None and (
        not distributed or hasattr(session.config, "workerinput")
    ):
        raise pytest.UsageError("Required coverage instrumentation is absent")
    if _coverage is not None:
        if _coverage.get_option("run:branch") is not True:
            raise pytest.UsageError("Required branch instrumentation is absent")
        worker = getattr(session.config, "workerinput", {}).get(
            "workerid", "controller"
        )
        _coverage.switch_context(
            f"ci:{os.environ.get('JUDGMENTLLM_CI_GROUP', '')}:{worker}:startup"
        )


@pytest.hookimpl(optionalhook=True)
def pytest_configure_node(node: Any) -> None:
    if node.config.getoption("--ci-coverage"):
        _contributors[node.gateway.id] = {"worker": node.gateway.id, "complete": False}


@pytest.hookimpl(optionalhook=True)
def pytest_testnodedown(node: Any, error: Any) -> None:
    if node.config.getoption("--ci-coverage"):
        item = getattr(node, "workeroutput", {}).get("judgmentllm_coverage")
        if (
            error is None
            and isinstance(item, dict)
            and cast(dict[str, Any], item).get("worker") == node.gateway.id
        ):
            _contributors[node.gateway.id] = cast(dict[str, Any], item)


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item: pytest.Item) -> None:
    if item.config.getoption("--ci-coverage") and _coverage is not None:
        # Phase switches can open coverage SQLite while security fixtures forbid it.
        _coverage.switch_context(item.nodeid)


@pytest.hookimpl(wrapper=True, trylast=True)
def pytest_runtestloop(session: pytest.Session) -> Generator[None, Any, Any]:
    result: Any = yield
    if session.config.getoption("--ci-coverage") and _coverage is not None:
        # Metadata only; pytest-cov retains ownership of saving and combination.
        worker = getattr(session.config, "workerinput", {}).get(
            "workerid", "controller"
        )
        _coverage_metadata.update(
            worker=worker,
            branch=_coverage.get_option("run:branch"),
            static_context=_coverage.get_option("run:context"),
            contexts=sorted(_coverage.get_data().measured_contexts()),
        )
    return result


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    selection_path = cast(str | None, config.getoption("--ci-selection"))
    if selection_path:
        raw = json.loads(Path(selection_path).read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise pytest.UsageError("Invalid qualification case selection")
        values = cast(list[object], raw)
        if not all(isinstance(node, str) for node in values) or len(values) != len(
            set(cast(list[str], values))
        ):
            raise pytest.UsageError("Invalid qualification case selection")
        selected = set(cast(list[str], values))
        rejected = [item for item in items if item.nodeid not in selected]
        items[:] = [item for item in items if item.nodeid in selected]
        config.hook.pytest_deselected(items=rejected)
        if {item.nodeid for item in items} != selected:
            raise pytest.UsageError("Qualification selection contains missing cases")
    distributed = bool(getattr(config.option, "numprocesses", None)) or hasattr(
        config, "workerinput"
    )
    if distributed and any(
        item.get_closest_marker("resource") or item.get_closest_marker("serial")
        for item in items
    ):
        raise pytest.UsageError(
            "Isolated cases require the CI runner's serial/resource partitions"
        )
    _items.extend(
        {
            "nodeid": item.nodeid,
            "markers": sorted({mark.name for mark in item.iter_markers()}),
        }
        for item in items
    )


@pytest.hookimpl(optionalhook=True)
def pytest_xdist_node_collection_finished(node: Any, ids: list[str]) -> None:
    # The xdist controller receives every test report but does not collect tests.
    # xdist independently rejects collection differences between workers.
    if not _items:
        _items.extend({"nodeid": nodeid, "markers": []} for nodeid in ids)


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    _reports.append(
        {
            "nodeid": report.nodeid,
            "phase": report.when,
            "outcome": report.outcome,
            "seconds": report.duration,
            "xfail": hasattr(report, "wasxfail"),
            "skip_reason": str(report.longrepr) if report.skipped else None,
        }
    )


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if session.config.getoption("--ci-coverage") and _coverage is not None:
        worker = getattr(session.config, "workerinput", {}).get(
            "workerid", "controller"
        )
        item = {**_coverage_metadata, "complete": int(exitstatus) == 0}
        if hasattr(session.config, "workerinput"):
            getattr(session.config, "workeroutput", {})["judgmentllm_coverage"] = item
        else:
            _contributors[worker] = item
    if hasattr(session.config, "workerinput"):
        return
    destination = cast(str | None, session.config.getoption("--ci-report"))
    if destination is None:
        return
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "provenance": session.config.stash[_provenance],
                "execution_group": os.environ.get("JUDGMENTLLM_CI_GROUP"),
                **(
                    {
                        "coverage_expected_contributors": [
                            f"gw{index}"
                            for index in range(
                                int(
                                    getattr(session.config.option, "numprocesses", 0)
                                    or 0
                                )
                            )
                        ]
                        or ["controller"],
                        "coverage_contributors": [
                            value for _, value in sorted(_contributors.items())
                        ],
                    }
                    if session.config.getoption("--ci-coverage")
                    else {}
                ),
                "exit_code": int(exitstatus),
                "collected": _items,
                "reports": _reports,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    temporary.replace(target)
