# ADR-0019: Valkey trust boundary

Status: Accepted (2026-10-13)

## Context
The Valkey image runs with protected mode off, and a session store is worth stealing (AC-46).

## Decision
- No published port, and the `store` network is `internal: true`.
- Session keys are hashed (ADR 0015). Persistence is off (`save ""`, `appendonly no`), there is no volume, and memory is capped (`maxmemory 128mb`, `volatile-lru`). Eviction could drop any key, so a `session:`, `turnlock:` or `LIMITS:` key as well as a weather cache entry may go early. This is accepted: only signed-in people can fill the store, sessions and locks are about 100 bytes each, and the weather cache is bounded by the Open-Meteo quota and by a size cap on each cached body (256 KB).
- The password comes from `KYAPEHNU_VALKEY_PASSWORD`. Compose fails fast when it is empty (`${KYAPEHNU_VALKEY_PASSWORD:?}`) and hands it to Valkey as a compose secret with an `environment` source, mounted at `/run/secrets/valkey_password`. The entrypoint is `/bin/sh -c` with a one-element command: it writes `requirepass` into a config file under `/tmp` and runs `exec valkey-server <conf>`, so the password is never on a command line. The healthcheck passes it to `valkey-cli` through the environment (`REDISCLI_AUTH`, which valkey-cli 8.1 reads; `VALKEYCLI_AUTH` is the name newer versions use).
- The backend reads the password from its environment and builds the client without putting it into a URL, so it is never in a logged URL.

## Consequences
One more `.env` value (the README gives `openssl rand -hex 32`; a value with a double quote would break the config line). A restart means signing in again, and locks and limits reset (all have short TTLs). The password is visible in `docker inspect` of the backend; only Valkey's own command line is protected. The smoke test checks the key names, the absence of the session value, the closed ports and the `-NOAUTH` reply.
