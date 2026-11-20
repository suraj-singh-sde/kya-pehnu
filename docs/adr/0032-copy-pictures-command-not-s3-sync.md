# ADR-0032: `kyapehnu copy-pictures` instead of `aws s3 sync`

Status: Accepted (2026-10-26)

## Context
`aws s3 sync` needs no code, but it overwrites files that differ, has no SHA-256 check, has no reverse rollback with the same rules, and cannot be tested offline.

## Decision
A CLI subcommand over the two stores, in both directions (`--to s3|local`, `--delete-source` only with `--to s3`). Every S3 put sends `ChecksumAlgorithm=SHA256` with the computed checksum, so S3 rejects corrupted bytes. The compare and the `--delete-source` re-check read it back with `HeadObject(ChecksumMode=ENABLED)`; the local store hashes the file. A target with an equal hash counts as present, a different hash counts as failed and is never overwritten, and a target without a SHA-256 checksum is read and hashed instead of rewritten. The command never opens the database and exits 1 when any key failed.

## Consequences
No hand-rolled hash convention, and the move is testable with moto. moto reads the checksum back but does not reject a wrong one, so the `BadDigest` path is tested with a stubbed client error only.
