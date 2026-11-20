# ADR-0005: In-memory one-hour weather cache

Status: Accepted (2026-10-06); storage superseded by 0021 (Valkey, same TTL semantics)

## Context
Regenerate and Swap must not refetch weather. Kya Pehnu is one process for one user.

## Decision
`WeatherClient` keeps a dict behind a `threading.Lock`. Key: the endpoint and its full parameters. Time to live 3600 s, at most 256 entries, oldest evicted first. Failures are not cached. It is not a SQLite table: that would add writes and a migration for data that is stale in an hour. No cache library is added.

## Consequences
A restart clears the cache. Two requests that miss at once may both fetch (single user; add per-key in-flight futures if Kya Pehnu is ever shared). An outage costs up to the 20 s weather budget on each suggest.
