# ADR-0031: The start-up orphan sweep reconciles both stores

Status: Accepted (2026-10-26)

## Context
A picture is written before its row commits (so no row ever names a missing picture), which can leave an object with no row after a crash. A pending-write table or a periodic job were the alternatives.

## Decision
Keep `sweep_orphans` at start-up, with the 1 hour minimum age, but list through the store (S3 `LastModified`, local mtime). Writers also delete what they wrote when a commit fails. Guards against a wrong database (empty, failed restore, a drill stack pointed at production):
- A user who has objects but no picture-referencing rows (photo, item, ingest piece, outfit) is skipped: `orphan_sweep_skipped reason=no_rows_for_user`.
- More than `KYAPEHNU_SWEEP_MAX_DELETES` candidates (default 500) deletes nothing: `orphan_sweep_skipped reason=over_limit`. The operator raises the cap for one restart after checking.
- Store down at start-up: `reason=store_down`, and the app still starts.

## Consequences
No schema change. Orphans live until the next restart, as today. A user whose every row is gone keeps leftover objects until the operator removes them. A drill stack still needs its own bucket: the guards are a last line of defence.
