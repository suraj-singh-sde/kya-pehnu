# ADR-0008: Slow work never runs inside a write transaction

Status: Accepted (2026-10-06)

## Context
SQLite allows one writer. A weather or AI call can take tens of seconds.

## Decision
Suggest fetches weather first, commits the event row, then calls the AI. Wear renders the collage and gets weather first, then does one short transaction. No service calls out between a write and its commit.

## Consequences
A suggest that fails after the event insert leaves an event row. It is harmless: it is reused by `event_id` or just listed. One transaction around the whole suggest was rejected because it would block every wear and log write during the AI call.
