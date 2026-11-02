# Local FastAPI interface (Milestone 7)

**Status:** approved development scope, **not yet implemented**. This is a proposed HTTP contract to be finalized against the stable Milestone 1-6 Pydantic models and library services. The local service is a **required late-stage development deliverable**, while installation and use of its optional `[api]` extra are not required by Python/CLI/R users.

## Purpose and sequencing

- Offer a thin JSON-over-HTTP interface so a local desktop or other application can use JudgmentLLM without importing Python directly.
- Begin **only after Milestones 0-6** have established public scoring schemas, execution services, provider adapters, durable job management, the CLI and R usage examples.
- The HTTP service must call the same validated library functions as CLI/Python and use the same SQLite run store, immutable manifests and normalized result files. No parallel implementation of scoring, provider requests, batch state machines or retries.
- This is **not** a web interface, a cloud service, or a native provider Batch API. A client can submit a provider-native batch through HTTP; the provider-managed job remains independent of the HTTP request's lifetime.

## Packaging and process behavior

- Optional install: `pip install 'judgmentllm[api]'` (future distribution). Core installs must not import FastAPI eagerly.
- CLI entry point (Milestone 7): `judgmentllm serve --host 127.0.0.1 --port 8765`.
- Bind **only** to loopback (default `127.0.0.1`). Reject attempted non-loopback binds, including `0.0.0.0`. No automatic remote exposure, user accounts or authentication in the local single-user scope.
- Requests involving slow LLM work must not hold open an HTTP response until scoring completes: submit and return a durable `run_id` promptly (typically `202 Accepted`); clients poll status or retrieve results afterward.
- Server shutdown or a disconnected client must not erase run/batch metadata. On restart, the existing run registry is loaded and unfinished tasks can be reconciled/resumed using the shared execution engine. Never blindly resubmit an ambiguously acknowledged provider operation.

## Proposed endpoints

All endpoint names are proposed and should be finalized when Milestone 7 is implemented. Stable contract version prefix: `/v1`.

| Endpoint | Purpose |
|---|---|
| `GET /v1/health` | Lightweight local readiness check, with no provider calls |
| `POST /v1/runs/validate` | Validate an incoming RunSpec (offline) and return structured validation results |
| `POST /v1/runs/plan` | Plan a run without submission, including request/task counts and costs with unknowns clearly marked |
| `POST /v1/runs` | Submit live, managed bulk-live, or native batch work, returning durable `run_id` without awaiting completion |
| `GET /v1/runs/{run_id}` | Retrieve persisted status, progress, errors and supported resume actions |
| `POST /v1/runs/{run_id}/resume` | Request safe resumption/reconciliation of incomplete work |
| `POST /v1/runs/{run_id}/collect` | Trigger available provider-batch result retrieval/normalization without assuming all jobs are complete |
| `POST /v1/runs/{run_id}/cancel` | Request cancellation and preserve its result/status |
| `GET /v1/runs/{run_id}/results` | Return paginated normalized results or an approved local export reference, not a memory-loaded full data set |

- Reuse the canonical RunSpec, validation errors, status values and output schemas; a change to those contracts must be reflected in Python/CLI documentation and tests.
- Input file paths in RunSpecs refer to **server-local** files. Resolve them only within the configured run/workspace root; reject traversal and unintended access outside that root. Inline data upload, filesystem browsing, arbitrary request JSON forwarding, and binary file transfer are out of scope for this first HTTP milestone unless separately approved.
- Reuse the exact provider/endpoint/model/caching/tier controls supported by the Python API. Do not allow API clients to bypass preflight validation or override provider credentials via request JSON.
- Use clear status codes and redacted structured error bodies. Never leak keys, environment variables, unfiltered provider HTTP headers, or traceback details.

## Local security boundaries

- An unauthenticated loopback API still needs basic browser-facing protections: reject unexpected Host values and inappropriate cross-origin Origin requests; use restrictive JSON content types and do not add wildcard CORS. Do not promise that loopback binding alone makes a server safe against all local or browser threats.
- Keep provider secrets in the **server process** environment/approved credential store, never in run configuration returned to clients.
- The local API can access private prompts/results stored under the configured workspace. Document filesystem permissions, retention and the risks of other processes on the same machine.
- Do **not** support binding to public/private LAN addresses, proxying through a public gateway, external multi-user clients or Internet deployment without a new explicit authentication and security design.

## Tests and acceptance

1. An offline mocked end-to-end test validates, plans, submits, polls, resumes and exports a synthetic rubric run; another covers pairwise results.
2. Tests assert exact task IDs, status transitions, validation semantics, result schemas and audit references match direct Python/CLI operation.
3. Submit returns a durable ID without waiting for provider work; stopping/restarting the server does not lose job metadata or duplicate accepted submissions.
4. Client errors (unknown run, invalid spec, unsupported endpoint, missing dependency) return clear redacted errors, not unhandled exceptions.
5. Binding to a non-loopback address is rejected; Host/Origin/CORS constraints are tested. No paid provider calls or credentials in regular CI.
6. With no `[api]` extra installed, the library and ordinary CLI commands still work; invoking `judgmentllm serve` produces a helpful installation message.

The required milestone and exact acceptance conditions are also captured in [`implementation-plan.md`](implementation-plan.md). This interface should not block earlier library/CLI prereleases but must be completed before declaring all planned JudgmentLLM features delivered.
