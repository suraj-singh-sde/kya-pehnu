# ADR-0014: Tag-count preference squashed by tanh, outside the item pre-score

Status: Accepted (2026-10-06)

## Context
Thumbs up and down should steer outfits, and Phase 2 ranking must stay the same when there is no feedback.

## Decision
- Only the latest rating per outfit counts. An item's tags are its colours, pattern, fabric, category, subcategory and your tags (trimmed, case-folded), read from the current item.
- `net[tag]` is the sum of ratings of rated outfits that carry the tag. An outfit's preference is `0.5 + 0.5 * tanh(sum of net over its distinct tags / 4)`. No feedback gives 0.5.
- It is a seventh scoring part, `outfits.weights.preference` (default 0.10), scaled with the other six to add up to 1. The per-slot shortlist still uses the four Phase 2 parts.

## Alternatives
Per-item learned weights or embeddings (need data and dependencies). A Bayesian ratio per tag (more parameters, same order at this data size).

## Consequences
Explainable and cheap. With no feedback every outfit gets the same preference, so the order is unchanged, but printed scores change with the new scaling. Very common tags (such as "solid") shift all outfits alike. A liked item with a low pre-score can miss the shortlist on a large wardrobe. The divisor 4 is hand-tuned.
