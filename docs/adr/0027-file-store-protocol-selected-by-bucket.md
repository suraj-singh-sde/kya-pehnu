# ADR-0027: A file store protocol with two adapters, selected by `KYAPEHNU_FILES_BUCKET`

Status: Accepted (2026-10-26)

## Context
Pictures must live in S3 in production and on the local disk on laptops, with no cloud needed to run Kya Pehnu. A second setting such as `KYAPEHNU_FILES_STORE` would repeat what the bucket setting already says.

## Decision
The `files` domain defines a `FileStore` protocol (`kyapehnu/files/store.py`) with `LocalStore` and `S3Store`. A bucket set means S3; empty means local. The store is built once in the composition root (`core/app.py`, `core/cli.py`) and reaches callers through `Ctx.files`; there is no global. Other code imports `kyapehnu.files.public` only, and `tests/test_domain_boundaries.py` rejects picture file I/O and `boto3` outside `kyapehnu/files/`.

## Consequences
Two real implementations justify the protocol, and a missing bucket can never silently mean S3. A laptop never imports boto3. An S3 endpoint override (MinIO, a moto server) is not added.
