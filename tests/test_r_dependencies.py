"""Required failures, optional skips and probe errors stay distinct."""

from __future__ import annotations

import subprocess
from unittest.mock import Mock, call

import pytest
import r_dependencies


@pytest.mark.parametrize("strict", [None, "", "0", "true", "01", "1"])
@pytest.mark.parametrize(
    ("executable", "returncode", "dependency"),
    [(None, 0, "Rscript"), ("Rscript", 1, "jsonlite"), ("Rscript", 2, "jsonlite")],
)
def test_unavailable_dependencies_fail_only_in_strict_mode(
    monkeypatch: pytest.MonkeyPatch,
    strict: str | None,
    executable: str | None,
    returncode: int,
    dependency: str,
) -> None:
    monkeypatch.delenv("JUDGMENTLLM_R_STRICT", raising=False)
    if strict is not None:
        monkeypatch.setenv("JUDGMENTLLM_R_STRICT", strict)
    which = Mock(return_value=executable)
    probe = Mock(return_value=subprocess.CompletedProcess[str]([], returncode))
    monkeypatch.setattr(r_dependencies.shutil, "which", which)
    monkeypatch.setattr(r_dependencies.subprocess, "run", probe)
    outcome = pytest.fail.Exception if strict == "1" else pytest.skip.Exception
    message = (
        f"Required {dependency} is unavailable"
        if strict == "1"
        else f"{dependency} unavailable locally; required local validation does not skip"
    )
    with pytest.raises(outcome, match=f"^{message}$"):
        r_dependencies.require_rscript()
    which.assert_called_once_with("Rscript")
    assert probe.call_count == (0 if executable is None else 1)


@pytest.mark.parametrize("strict", ["0", "1"])
@pytest.mark.parametrize(
    "error",
    [
        FileNotFoundError("missing"),
        OSError("probe failed"),
        subprocess.TimeoutExpired("Rscript", 30),
    ],
)
def test_probe_errors_propagate(
    monkeypatch: pytest.MonkeyPatch, strict: str, error: Exception
) -> None:
    monkeypatch.setenv("JUDGMENTLLM_R_STRICT", strict)
    monkeypatch.setattr(r_dependencies.shutil, "which", Mock(return_value="Rscript"))
    monkeypatch.setattr(r_dependencies.subprocess, "run", Mock(side_effect=error))
    with pytest.raises(type(error)) as caught:
        r_dependencies.require_rscript()
    assert caught.value is error


def test_dependencies_are_rechecked_with_the_original_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JUDGMENTLLM_R_STRICT", "1")
    which = Mock(
        side_effect=["/first/Rscript", "/first/Rscript", "/second/Rscript", None]
    )
    probe = Mock(
        side_effect=[subprocess.CompletedProcess[str]([], code) for code in (0, 1, 0)]
    )
    monkeypatch.setattr(r_dependencies.shutil, "which", which)
    monkeypatch.setattr(r_dependencies.subprocess, "run", probe)
    assert r_dependencies.require_rscript() == "/first/Rscript"
    with pytest.raises(
        pytest.fail.Exception, match="^Required jsonlite is unavailable$"
    ):
        r_dependencies.require_rscript()
    assert r_dependencies.require_rscript() == "/second/Rscript"
    with pytest.raises(
        pytest.fail.Exception, match="^Required Rscript is unavailable$"
    ):
        r_dependencies.require_rscript()
    assert which.call_args_list == [call("Rscript")] * 4
    assert probe.call_args_list == [
        call(
            [
                executable,
                "--vanilla",
                "-e",
                "quit(status=if(requireNamespace('jsonlite',quietly=TRUE)) 0L else 1L)",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        for executable in ("/first/Rscript", "/first/Rscript", "/second/Rscript")
    ]
