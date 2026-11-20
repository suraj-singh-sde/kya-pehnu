# ADR-0015: Server-side sessions in Valkey

Status: Accepted (2026-10-13)

## Context
Signing out and revoking one session must take effect at once (FR-5, NFR-2, AC-7). Valkey must not hold anything that works as a cookie if it leaks (AC-46).

## Decision
`auth/sessions.py` on redis-py: `create`, `get_and_touch` (`GETEX ... EX`, so every use slides the idle time) and `delete`. The browser holds a random token in an HttpOnly cookie; the key is `session:<sha256(token)>` and the value is a small JSON with the user id and the creation time. Starlette's `SessionMiddleware` is not used for sessions: it is a signed client-side cookie with no server-side revocation. `starsessions` stores by the raw session id (no hashing) and adds a dependency for about 40 lines.

## Consequences
A Valkey restart signs everybody out (accepted, AC-24). Every authenticated request costs one Valkey round trip; when Valkey is down the API answers 503 with Retry-After, never 401 (AC-27).
