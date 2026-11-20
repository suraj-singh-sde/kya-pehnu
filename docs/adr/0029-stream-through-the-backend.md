# ADR-0029: Pictures stream through the backend, conditionals passed to S3

Status: Accepted (2026-10-26)

## Context
Presigned redirects were rejected: no address that works without the session may exist. A local thumbnail cache was the other alternative.

## Decision
`GET` and `HEAD /images/...` keep their address and owner check. The S3 store calls `GetObject` with `Range`, `If-None-Match` and `If-Modified-Since`, relays 304, 206 and 416, and streams the body in 64 KiB chunks, closing it when the client disconnects. `Cache-Control: private, no-cache` stays. An unavailable store answers 503 `files_unavailable`.

## Consequences
Every view costs one S3 request, including a 304, and adds some tens of milliseconds in-region. Build a cache only if the production check in `docs/operations.md` (200 thumbnails, p95 `time_starttransfer`) fails.
