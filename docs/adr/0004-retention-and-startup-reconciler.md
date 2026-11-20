# ADR-0004: Retention rules and a startup reconciler for file and row writes

Status: Accepted (2026-10-05)

## Context
Photos live in files and rows live in SQLite, so the two can drift. Data must not be destroyed without a confirmed delete.

## Decision
- A confirmed hard delete of an item also removes its source photo (row, original, normalized file) when no other item or pending/error piece references it.
- Discarded pieces and their photos are kept until the user cleans the data dir.
- `LlmCall` rows are purged at startup when older than 30 days (`LLM_CALL_RETENTION_DAYS`).
- On create, files are written before the rows commit, and are unlinked if the commit fails. On delete, rows commit first and files are unlinked after.
- `housekeeping.sweep_orphans()` runs at startup, before the worker, and deletes only unreferenced files under `data/images/`.

## Consequences
Debug history is limited to 30 days. A bug in the reference set could delete user files, so the sweep is narrowly scoped and has a test proving referenced files survive. The README privacy section states these rules.
