# ADR-0033: No per-user storage quota with S3 pictures

Status: Accepted (2026-10-26). Extends ADR-0026.

## Context
ADR-0026 accepted no quota because the disk was the ceiling and the stack was local. S3 removes that ceiling, and the standards ask for a quota or a named enforcer. A per-user byte cap needs a column and a migration; an AWS Budgets alert needs billing permissions.

## Decision
No quota. The AWS pilot is invite-only (under 10 users). The guards are the existing per-request limits (25 MB a file, 200 files a request) and a cost check 30 days after cut-over (Cost Explorer, service S3, tag `Project=kyapehnu`, at most about $2 for the month).

## Consequences
One user could grow the bill until that review. Add a per-user cap in its own change if the check fails or the pilot opens up.
