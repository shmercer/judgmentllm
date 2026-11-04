"""Run the credential-free public lifecycle with explicitly located local state."""

from __future__ import annotations

import argparse
from pathlib import Path

from judgmentllm import (
    estimate_run,
    export_run_results,
    get_run,
    run_live,
    validate_inputs,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", required=True, type=Path)
    options = parser.parse_args()
    configuration = Path(__file__).with_name("mock-run.yaml")
    validation = validate_inputs(configuration)
    print(validation.model_dump_json())
    if not validation.valid:
        raise SystemExit(2)
    print(estimate_run(configuration).model_dump_json())
    summary = run_live(configuration, store=options.store)
    print(summary.model_dump_json())
    print(get_run(summary.run_id, store=options.store).model_dump_json())
    print(export_run_results(summary.run_id, store=options.store).model_dump_json())


if __name__ == "__main__":
    main()
