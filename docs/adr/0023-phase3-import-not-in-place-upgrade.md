# ADR-0023: Phase 3 data enters by import, not by in-place upgrade

Status: Accepted (2026-10-13)

## Context
Rows of a Phase 3 database have no Google identity, and the ownership revision `b8e3f5a40d71` is irreversible and needs an empty database (waiver W1 covers exactly this revision).

## Decision
`kyapehnu import-phase3 --source DIR --email E` with the old data directory mounted read-only (`docker compose run --rm -v DIR:/import:ro backend kyapehnu import-phase3 --source /import --email E`). The importer:
1. resolves the account of the email (exactly one), copies the source database (and its write-ahead log) to a scratch folder and refuses a revision outside `PHASE3_REVISIONS`;
2. upgrades the copy to `a7d2e4f39c60`;
3. refuses if the account already owns data, if a source id exists on the stack, or if the stack's counters (`itemseq`, the `chatsession` sequence) are past a source id;
4. checks every image path of the source against `images/<kind>/<name>` (a crafted database is refused), copies the images to `images/<user_id>/<kind>/<name>`, and inserts all rows with `user_id` and their ids in one `BEGIN IMMEDIATE` transaction, rewrites the paths, overwrites the account's profile and bumps `itemseq`;
5. on any failure, rolls back and removes the copied files; prints `import_done user_id=<n> ...` with counts and never the email.

A second run for the same account is refused. The source stays byte-identical.

## Consequences
Installer users migrate once, by hand, after a backup. Secrets and `config.yaml` of the old install are not imported (the instance key is entered again in the app).
