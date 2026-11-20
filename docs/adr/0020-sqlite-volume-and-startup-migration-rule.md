# ADR-0020: SQLite on a named volume, and the startup migration rule

Status: Accepted (2026-10-13)

## Context
Valkey and the backend are containers now. WAL is safe on a named volume (VM-local ext4) and unsafe on host bind mounts. Old (Phase 3) databases have no owner column and must never be upgraded in place (ADR 0023).

## Decision
- A named volume `kyapehnu-data` at `/data`; WAL, `synchronous=NORMAL`, `busy_timeout=5000`. Busy or locked after 5 s answers 503 `db_busy` with Retry-After (NFR-12, AC-43).
- `BEGIN IMMEDIATE` starts exactly two transactions: `upsert_google_user` (the first-admin race) and the `import-phase3` row transaction. Every other session stays deferred, so the write lock is never held across slow work (ADR 0008). A deferred transaction that reads and then writes may get `SQLITE_BUSY` at once; it maps to the same 503.
- One backend replica, `--workers 1`, `--no-access-log`. CLI commands that write (`import-*`, `restore`, `seed`) run with `docker compose run` or `exec` on the same volume. Backups use the SQLite backup API, never a file copy.
- At startup and in `kyapehnu migrate`, `check_revision` reads the stamp through a read-only connection first. A missing or empty file, or a stamp at the ownership revision or later, is upgraded to head. A stamp in `PHASE3_REVISIONS` (`971397b5ec4d` to `a7d2e4f39c60`) is refused without writing, with a message that points to `import-phase3`. An unknown or missing stamp, or a file that is not SQLite, is refused. This is the only place migrations run, and AC-48 tests each of the seven Phase 3 revisions (the file's sha256 stays identical).

## Consequences
Bind mounts for the data are unsupported (the README says so). Horizontal scale would need a server database.
