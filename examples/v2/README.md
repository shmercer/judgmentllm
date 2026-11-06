# Runnable RunSpec v2 mock examples

Install the built wheel or the package into an environment providing the
`judgmentllm` console. Copy this directory and its parent input files together;
all references resolve against each configuration file. No development import
path, credentials, provider connection, or server is needed.

Choose a local runtime directory outside synchronized storage:

```bash
judgmentllm validate --config examples/v2/scores-first.json --json
judgmentllm plan --config examples/v2/scores-first.json --json
judgmentllm templates preview --config examples/v2/scores-first.json --item-id essay_001 --trait organization
judgmentllm run --config examples/v2/scores-first.json --store /tmp/judgmentllm-v2-scores --json
judgmentllm run --config examples/v2/explanations-first.json --store /tmp/judgmentllm-v2-explanations --json
judgmentllm run --config examples/v2/forced-choice.json --store /tmp/judgmentllm-v2-pairs --json
```

Record the `run_id` returned by each command. The existing version-1 CLI control
envelope and normalized result schema remain unchanged. For example, replacing
`RUN_ID` with the first returned run ID:

```bash
judgmentllm runs status --run RUN_ID --store /tmp/judgmentllm-v2-scores --json
judgmentllm resume --run RUN_ID --store /tmp/judgmentllm-v2-scores --json
judgmentllm results export --run RUN_ID --store /tmp/judgmentllm-v2-scores --format csv --view r --json
```

Pass the returned absolute `artifacts.export` path to base R:

```bash
Rscript --vanilla examples/v2/import.R /absolute/path/to/export.csv
```

A grouped partial outcome uses the same scoring call and salvages valid sibling
traits. This command exits 3 because the `evidence` explanation is missing:

```bash
judgmentllm run --config examples/v2/partial-group.json --store /tmp/judgmentllm-v2-partial --mock-fixture examples/v2/partial-fixture.json --json
```

Export the partial run to inspect unsuccessful task IDs. Authorize only those
members explicitly, replacing `RUN_ID` and `FAILED_TASK_ID`:

```bash
judgmentllm resume --run RUN_ID --store /tmp/judgmentllm-v2-partial --mock-fixture examples/v2/valid-fixture.json --retry-id repair-explanation-001 --retry-task FAILED_TASK_ID --json
```

Repeat `--retry-task` only for failed members of the same original request.
Use a distinct `--retry-id` for each original request.
Prior attempts and immutable exports remain available. Ordinary resume never
rescored invalid output; explanations come from the original assessment call.

The mock resolves `structured_output: auto` to textual schema presentation and
records that native enforcement is unavailable. `text` explicitly selects the
same mechanism. Both retain strict local validation. `native` fails preflight:
no provider-native translator or real model scoring is implemented in Phase 2.
The mock fixture document remains schema version 1 and is a typed synthetic
scenario, independent of RunSpec and the versioned mock request protocol.
