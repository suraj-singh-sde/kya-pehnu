# ADR-0002: Host allowlist and same-origin check instead of CSRF tokens or CORS

Status: Accepted (2026-10-05), amended after the design gate; amended by 0018 (the allowlist comes from KYAPEHNU_PUBLIC_URL)

## Context
There is no auth. A web page in the user's browser can reach 127.0.0.1 (CSRF) or rebind a domain to it. The loopback check does not help, because those requests come from loopback.

## Options
- CSRF tokens: needs a session, which the app does not have.
- CORS allowlist: does not block simple requests such as a form POST.
- Host allowlist plus a same-origin `Origin` check.

## Decision
The third. `RequestGuardMiddleware` (`backend/kyapehnu/middleware.py`) rejects a Host outside `localhost`, `127.0.0.1` and (in LAN mode) the machine's own IP with a 400. For POST/PUT/PATCH/DELETE, an `Origin` header must equal `http://<Host>`, otherwise 403. A missing Origin is allowed (CLI, curl). No CORS headers are emitted. The Host check is custom, not Starlette's `TrustedHostMiddleware`, so a rejection uses the standard JSON error envelope. `::1` is not allowed because the server never listens on IPv6.

## Consequences
LAN users must open the printed IP URL. A `.local` hostname gets 400.
