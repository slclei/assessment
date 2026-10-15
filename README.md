# Agentic SDLC Assessment Prototype

This dependency-free Python prototype turns a bounded URL-shortener request into reviewable implementation, test, documentation, and release artifacts. It demonstrates governed workflow execution; it is not a general-purpose autonomous engineer or a production deployment platform.

## Setup

Requires Python 3.10 or newer. No third-party packages are required.

```powershell
cd agentic_sdlc_platform
python -m unittest discover -s tests -v
```

To run the interactive scenarios, use an isolated working directory if you need to preserve a local `urls.db`:

```powershell
python main.py greenfield
python main.py brownfield
python main.py ambiguous --prompt "Shorten URLs, redirect them, track clicks, and support custom aliases."
```

Each scenario asks for approval before its high-impact action. Outputs are written to `output_artifacts/`; run state and metrics are written under `data/`. Each scenario writes a separate `*_run_manifest.json` with its run ID, request, requirements, task trace, approvals, test results, final decision, and hashes for its generated artifacts.

## Supported Scope

The ambiguous workflow supports URL shortening, redirects, click analytics, custom aliases, bounded reliability controls, and link expiration with a 24-hour default lifetime and maximum. Explicit expiry timestamps must be timezone-aware and cannot extend the configured maximum (up to 8,760 hours). Each capability gets its own verification node, and release checks the tested source hash. The workflow emits `implementation.diff` for review and `replanning_demo.json` showing an approved upstream policy change, compensation, dependent invalidation, and rerun. Authentication and authorization are not implemented; those requests fail closed. Unknown capability terms also fail closed. Local retry limits are not a high-availability guarantee.

The WSGI service uses a configured canonical origin for returned `short_url` values; set `public_base_url` when constructing the app for deployments other than the local default. Request `Host` headers are not used to construct public links.

By default the local analyst and planner use deterministic rules, keeping runs reproducible. An opt-in OpenAI-compatible provider can drive analysis, planning, and implementation; the implementation provider receives the original request, normalized requirements, assumptions, and explicit constraints. Model output is schema-checked, DAG-validated, syntax-checked, and policy-screened before validation. Configure `AGENT_PROVIDER=openai-compatible`, `AGENT_BASE_URL` (including its `/v1` path), `AGENT_MODEL`, and `AGENT_API_KEY` through environment variables to enable it. Provider manifests record a sanitized provider name, model, and endpoint, never the API key. Brownfield accepts a Python module with a vulnerable `lookup_url` function, records an AST-based audit, requires approval, replaces only that function, and preserves other source while never importing or executing caller code.

For example, after configuring the provider variables in the shell, run `python main.py ambiguous --prompt "Build a URL shortener and make the default lifetime seven days."`. The generated service and acceptance report must both show a 168-hour default; if the implementation provider cannot satisfy an explicit constraint, the implementation step rejects the response.

## Architecture and Controls

- The orchestrator executes a dependency graph, parallelizes ready tasks, and serializes policy checks and approvals before dispatch.
- Bounded retries, one configured failure-replan, compensation callbacks, and cooperative cancellation provide recovery and safe-stop behavior. After a short cancellation drain, rollback is skipped if a worker is still active to avoid racing its side effects; such a run records incomplete safe-stop and may require manual reconciliation.
- File writes are confined to scenario output directories. Generated Python acceptance tests run in a timeout-bounded child process with a reduced environment and blocked Python socket APIs. This is defense-in-depth, not an OS sandbox; the child still runs with the current user's filesystem permissions. Do not run untrusted generated code or treat its acceptance report as a security certification.
- JSON state snapshots, append-only `audit_lineage.jsonl` events, and run history support local audit and metrics; they do not support workflow resume or robust multi-process coordination.

## Scenarios and Artifacts

- `greenfield`: architecture, runnable service source, OpenAPI 3.1 contract, SQLite schema/migration, WSGI acceptance report, operations documentation, and release-readiness report.
- `brownfield`: AST audit, approved source-preserving parameterized lookup replacement, compile check, and normal/injection-shaped lookup tests.
- `ambiguous`: normalized requirements, unresolved questions and assumptions, validated task graph, generated service, requirement-specific tests, documentation, approval, and release report. Unsupported prompts produce an explicit block report and no release artifact.

The test suite runs scenario outputs in temporary directories so tests do not alter a developer's database or checked-in artifacts. Each scenario's `*_run_manifest.json` contains a run ID and immutable artifact snapshots under `run_artifacts/<run_id>/`, so later scenarios cannot overwrite its evidence.

## Metrics and Limitations

Metrics include end-to-end latency, success rate, retry count, rollback count, and MTTR. Worker actions must honor cancellation; Python cannot safely terminate a running thread. The local JSON store is not a transactional multi-process database. There is no MCP integration, authentication, rate limiting, distributed persistence, or production availability guarantee. The optional model path depends on an externally configured OpenAI-compatible service. These are explicit prototype limitations, not release claims.