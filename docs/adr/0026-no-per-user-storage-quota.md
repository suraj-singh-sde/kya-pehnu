# ADR-0026: No per-user storage quota in this release

Status: Accepted (2026-10-13)

## Context
The standards ask for a quota, or a named enforcer, whenever users store data.

## Decision
No quota. The stack is bound to 127.0.0.1, so its users are people at that machine. The per-request caps hold (25 MB a file, 200 files, 5001 MiB a request) and a full disk answers 507 without losing data.

With pictures on S3 the disk is no longer the ceiling: see ADR-0033.

## Consequences
One person can fill the disk. The upgrade path is a per-user photo count limit once access beyond localhost is supported.
