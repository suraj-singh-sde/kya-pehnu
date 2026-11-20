# ADR-0030: Versioned pictures bucket with a 7-day noncurrent lifecycle

Status: Accepted (2026-10-26)

## Context
A deleted picture must be gone within 30 days. Versioning off would meet that, but gives no undo for a sweep or migration bug.

## Decision
The pictures bucket has versioning on and one lifecycle rule, `pictures-noncurrent-7d`: noncurrent versions expire after 7 days and expired delete markers are removed. The instance role may delete objects but not versions (`s3:DeleteObjectVersion` is absent), so a delete only adds a marker. `provision.sh` converges the bucket on every run and refuses a pictures policy that contains `DeleteObjectVersion` or `s3:*`.

## Consequences
A 7-day undo window that a compromised role cannot shorten. The lifecycle runs asynchronously (about a day of lag), still inside 30 days. Backup archives keep deleted pictures for 30 days, as before.
