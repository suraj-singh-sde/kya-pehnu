# ADR-0006: Rules decide, the AI model only picks numbered candidates

Status: Accepted (2026-10-06)

## Context
The model must never add an item that is not in the wardrobe. Reply shapes must be flat and unambiguous.

## Decision
Rules filter and score candidates. The prompt lists at most 15 numbered candidates. The reply (`OutfitPicks`) holds flat picks: `candidate_number`, `reason`, and an optional swap (`swap_slot`, `swap_item_id`). Types are loose on purpose, so one bad pick is dropped by validation instead of failing the whole reply. Reasons are cut to 400 characters. Gaps are filled from the rule-based order, which is also the whole answer when there is no provider or the reply is invalid.

## Consequences
Picks always come from the wardrobe. Quality depends on the scorer. The model chooses and explains.
