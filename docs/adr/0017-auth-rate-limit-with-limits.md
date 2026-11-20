# ADR-0017: Sign-in rate limit with `limits` on Valkey

Status: Accepted (2026-10-13)

## Context
Login and callback allow 20 attempts per minute per address, answer with Retry-After and must hold across backend restarts (NFR-3, AC-16).

## Decision
`limits.MovingWindowRateLimiter` with `RedisStorage`, applied as a dependency on the two auth routes. A refusal raises `RateLimitedError` (429, `Retry-After` from the window statistics) in the standard error format. The rate is `KYAPEHNU_AUTH_RATE_LIMIT`, validated with `limits.parse`. Unit tests inject `MemoryStorage`. The limit is rechecked end to end by the compose smoke (forged `X-Forwarded-For` values do not lift it).

## Consequences
`limits` owns a second connection pool to Valkey. Rejected: nginx `limit_req` (leaky bucket, no Retry-After, absent when the backend runs alone, and the design keeps the limit state in Valkey), `slowapi` (its decorator returns its own error body) and a hand-rolled INCR.
