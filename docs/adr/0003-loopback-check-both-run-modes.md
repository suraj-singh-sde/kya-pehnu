# ADR-0003: Loopback check on the real client address in both run modes

Status: Superseded by 0018 (2026-10-13)

## Context
Folder import must work only from the same machine. In `dev`, every request reaches uvicorn from the Vite proxy on 127.0.0.1, so a plain peer check would let LAN clients through in `dev --lan`. Forwarded headers must not be spoofable.

## Decision
`serve` runs uvicorn with `proxy_headers=False`, so the socket peer is used. `dev` binds the API to 127.0.0.1, lets the Vite proxy set `X-Forwarded-For` (`xfwd: true`) and trusts forwarded headers from 127.0.0.1 only (`--forwarded-allow-ips`). A LAN client behind Vite is seen with its real address and gets 403. IPv4-mapped addresses are unwrapped before the check.

## Consequences
One test per mode. A user's own reverse proxy in front of `serve` would appear as loopback, so the README says not to use one.
