# ADR-0021: Services layout and explicit paths

Status: Accepted (2026-10-13)

Supersedes the storage part of ADR 0005 (the weather cache moves to Valkey, same TTL semantics) and of ADR 0013 (the no-tools memory moves to Valkey, 24 h TTL).

## Context
`config.py` derived the root, the home folder and the data directory from folder depth and from how it was installed. That breaks under `services/` and in a container.

## Decision
`backend/` and `frontend/` move to `services/backend` and `services/frontend` with `git mv`, and each builds its own image from its own folder. Paths are settings: `KYAPEHNU_DATA_DIR` (default `/data`; `make dev` sets `<repo>/data`), `KYAPEHNU_CONFIG_FILE` (default `<data_dir>/config.yaml`, optional) and `KYAPEHNU_PUBLIC_URL`. Settings read no `.env` file (compose passes the environment; `make dev` uses `uv run --env-file`). Gone: `ROOT`, `HOME`, `IS_CHECKOUT`, `app_home`, `lan`, `frontend_dir`, the SPA static mount, the installers and the wheel. The OpenAPI docs are off unless `KYAPEHNU_DOCS_ENABLED` is set. Process-level caches (weather, no-tools) must work with one worker today and survive a restart, so they live in Valkey.

## Consequences
Tests set paths with `tmp_path`. The backend serves no SPA: the frontend image (nginx) does.
