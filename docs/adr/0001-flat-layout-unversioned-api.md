# ADR-0001: Keep the flat module layout and unversioned `/api` paths

Status: Accepted (2026-10-05)

## Context
The Python standard prescribes a `src/` layout with `core/ services/ repositories/ api/v1/`, `/v1` paths, cursor pagination and health probes. The approved plan uses `backend/kyapehnu/{items.py, ingest/, llm/, api/}` with `/api/*`.

## Decision
Keep the plan's layout and paths. Domain modules (`items.py`, `ingest/*`) are the service layer and own their queries, with no repository layer. `api/` stays thin and SDKs stay in `llm/`. No `/v1`: the only client is the bundled SPA, built from generated types in the same commit. No cursor pagination: lists are bounded. No health endpoints: nothing orchestrates the process. `/docs` and `/openapi.json` stay on.

## Consequences
The approved plan stays executable as written. A public API in a later phase would revisit versioning.
