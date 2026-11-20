# ADR-0009: Collages built on save with Pillow, and item ids never reused

Status: Accepted (2026-10-06)

## Context
Only saved outfits need a collage file. Outfits and wear logs keep item ids in JSON, so a reused id would credit old wears to a new item.

## Decision
- The collage is rendered in the "Wear this" request with Pillow, before the write transaction. An item with no picture is skipped. A render failure saves the outfit without a collage. Open-Meteo is called with plain `httpx`. No new dependency.
- Item ids come from a one-row `itemseq` table (a high-water mark) and a `before_insert` listener on `Item`. The migration seeds it with the largest item id, so ids are never reused after a hard delete. It is the fourth new table (events, outfits, wear logs, `itemseq`). No Phase 1 table is changed.
- A hard delete of an item removes every collage that shows it and clears those outfits' `collage_path`. The outfit and wear rows stay, and the outfit shows as incomplete.

## Consequences
No queue changes and no orphan collages from suggestions. Rolling back needs the tested downgrade or the pre-upgrade backup, and the Phase 1 release cannot open the upgraded database.
