# ADR-0018: Proxy trust and the client address

Status: Accepted (2026-10-13)

Amends ADR 0002 (the Host allowlist now comes from `KYAPEHNU_PUBLIC_URL`). Supersedes ADR 0003 (the loopback check is gone).

## Context
The backend sees only the proxy, and Docker Desktop hides client addresses behind the VM gateway (NFR-3, NFR-4). The folder import that needed a loopback check is now a CLI command, so no address-based authorization is left.

## Decision
- The frontend is published on `127.0.0.1:${KYAPEHNU_PORT:-8080}` only.
- nginx sets `X-Forwarded-For $remote_addr` (it overwrites and never appends, so a forged header in front is dropped) and `X-Forwarded-Proto $scheme`.
- uvicorn runs with `--proxy-headers --forwarded-allow-ips='*'`. That is safe because the backend publishes no port and sits only on the `edge` network (with nginx) and the `store` network (with Valkey, which sends no HTTP): nginx is its only HTTP peer. `make dev` uses `--forwarded-allow-ips=127.0.0.1` (the Vite proxy).
- The Host allowlist is {the host of `KYAPEHNU_PUBLIC_URL`, `localhost`, `127.0.0.1`}, and an unsafe-method `Origin` must equal the public origin.

## Consequences
On Docker Desktop every client shares one rate-limit bucket (all are local anyway). Use beyond localhost (a LAN, or a TLS proxy of your own in front, which would also make all clients share one bucket) is not supported in this release, and the README says so. Publishing the backend port would break the trust rule.
