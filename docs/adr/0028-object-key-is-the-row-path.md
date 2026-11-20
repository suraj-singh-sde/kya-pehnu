# ADR-0028: The object key is today's relative row path

Status: Accepted (2026-10-26)

## Context
Rows already store `images/<user_id>/<kind>/<name>` (ADR-0022). Content-addressed or sharded keys were the alternatives.

## Decision
Keep that path as the key in both stores. No schema change and no row migration. A backup restores across stores unchanged.

## Consequences
The layout does not shard directories, against the usual standard. Per-user, per-kind prefixes stay far below S3 and filesystem limits at pilot scale (about 5,000 pictures).
