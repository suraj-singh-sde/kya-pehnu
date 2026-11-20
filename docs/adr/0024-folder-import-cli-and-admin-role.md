# ADR-0024: Folder import as a CLI command, and the admin role

Status: Accepted (2026-10-13)

## Context
The HTTP route that read a server path is gone: with accounts, a path chosen by a browser user would read the host's files.

## Decision
Folder import is `kyapehnu import-folder DIR --email E`, run with `docker compose run --rm -v DIR:/import:ro backend ...`. It runs the existing ingest pipeline synchronously in that process and prints `user_id`, the job id and counts, never the email. LLM settings writes and the Debug page need `AdminUser`; the first account to sign in is the admin (a partial unique index allows exactly one).

## Consequences
No HTTP surface reads a server path. If the backend restarts during an import, its worker may pick up the CLI's running job again and pieces may repeat: the README says not to restart it meanwhile.
