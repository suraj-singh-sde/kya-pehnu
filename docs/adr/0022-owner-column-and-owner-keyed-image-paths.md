# ADR-0022: Direct owner column and owner-keyed image paths

Status: Accepted (2026-10-13)

## Context
Several people share one stack and must never see each other's data (FR-10 to FR-12, NFR-5). Item ids are enumerable (a global counter).

## Decision
`user_id` is NOT NULL on every user-owned table, children included, and every query filters on it; another person's row looks exactly like a missing one (404). Images live at `images/<user_id>/<kind>/<name>`. The image route compares the owner in the path with the caller (404 otherwise), validates `kind` (an enum) and the name (`[A-Za-z0-9_-]{1,200}` plus a known extension) and streams the file.

## Consequences
Wider tables and one more index per table. The Phase 3 importer and `restore` write and accept exactly this path shape. Rejected: ownership through parent joins (every query a join, easy to miss) and a database lookup per image. Directory sharding is skipped (per-user folders at laptop scale).
