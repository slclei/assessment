# Final Engineering Summary

## Plan and Rationale

Deliver a bounded, reproducible URL-shortener SDLC workflow rather than claim arbitrary prompt-to-code autonomy. The local analyst recognizes supported capabilities, records assumptions and open questions, and blocks unsupported requirements. A dependency graph sequences design, implementation, requirement-specific tests, documentation, and release. High-impact operations require human approval.

## Reviewable Artifacts

- Greenfield: `output_artifacts/main.py`, `openapi.json`, `schema.sql`, architecture, validation, operations, and release-readiness reports.
- Brownfield: `legacy_audit.json`, `secure_lookup.py`, and `brownfield_validation.json`.
- Ambiguous: normalized intent, task graph, generated service source, acceptance report, operations documentation, approval, release report, and final validation report.
- Each scenario includes a run-specific manifest with request, normalized requirements, DAG, approval events, task inputs and outputs, test reports, decision, metrics, and immutable SHA-256-verified artifact snapshots.
- The service provides URL creation, redirects, aggregate click analytics, custom aliases, bounded collision retries, and request-size limits.

## Assessment Deliverables

- Working prototype: run `python main.py greenfield`, `python main.py brownfield`, or `python main.py ambiguous --prompt "..."` from the project directory.
- Architecture overview: `phase1.md`, `README.md`, and scenario-generated `architecture.md` artifacts describe components, orchestration, control flow, and key decisions.
- Three scenarios: greenfield generates a service/API/schema; brownfield audits and patches a SQL-injection-shaped lookup; ambiguous normalizes a request and produces a requirement-specific plan, implementation, tests, and release report.
- Setup instructions: `README.md` lists the Python prerequisite, test command, scenario commands, and optional provider configuration.
- Testing, limitations, and trade-offs: this summary and `README.md` record test coverage, known risks, and prototype boundaries.

## Latest Scenario Evidence

- Greenfield `8ed2c6de73bb4f49b48f51814e82edc0`: passed API, schema, shorten, uniqueness, redirect, analytics, and not-found validation; migration found the existing tables and created none.
- Brownfield `7520f0c0a1584367981c81c07d2dc35d`: passed AST, compile, parameterized-query, normal-lookup, and injection-shaped-input checks.
- Ambiguous `3f31484d346c42fd96d843c89da47ff8`: passed for “Set the default lifetime to 7 days for new links in my URL shortener”; acceptance verified a 168-hour default and the approval was recorded.

## Validation

- `python -m unittest discover -s tests -q`: 54 tests pass.
- Greenfield WSGI acceptance checks cover URL creation, unique codes, redirect behavior, analytics, unknown codes, and generated artifacts.
- Brownfield checks cover AST parsing, syntax, parameterized SQL, normal lookup behavior, and injection-shaped input. A regression test confirms caller-supplied top-level code is not executed.
- Ambiguous release requires all planned requirement IDs to have passing acceptance checks and verifies the tested source hash has not changed. Unsupported authentication and mixed unsupported capabilities produce a block report and no release artifact.
- Orchestrator tests cover approval serialization and timeout, bounded retries/replanning, rollback, audit events, and reliability metrics. If a worker remains active after cancellation, the workflow records incomplete safe-stop and skips rollback to avoid racing side effects.

## Risks, Trade-offs, and Assumptions

- In the default local run, the analyst and planner use deterministic rules and the implementation artifact is capability-configured from maintained code. An optional OpenAI-compatible provider can drive analysis, planning, and implementation, but that live-provider path is not part of the demonstrated run. Requirement-specific verification branches run in parallel, and explicit output revisions invalidate and rerun completed dependents under normal gates. Unknown capability terms fail closed, which may require users to rephrase valid requests.
- JSON state and event files are suitable for this local prototype, not durable resume or robust multi-process coordination.
- AST policy checks are defense-in-depth, not a security sandbox. Untrusted or unbounded tools need process/container isolation.
- Python cannot forcibly stop an active worker thread. A non-cooperative worker can continue after the workflow reports incomplete safe-stop, and its side effects may require manual reconciliation. Bounded retries and request limits do not establish a production availability target.
- Authentication, authorization, rate limiting, MCP integrations, and distributed persistence are outside the implemented scope. A live external-provider run has not been demonstrated.

This is a runnable assessment prototype with explicit boundaries, not production-ready deployment software.