# ADR-0013: No-tools detection from the first error, remembered in process

Status: Accepted (2026-10-06); storage superseded by 0021 (Valkey, same TTL semantics)

## Context
Local servers differ in whether they accept tools, and some ignore them silently.

## Decision
Only for `openai_compatible`, and only on the first request of a turn: a 400, 404 or 422 whose message mentions "tool" means the model has no tool support. Retry once with no tool structures (earlier tool activity becomes one plain line), and remember the `(base_url, model)` pair in a process-level set. Anthropic never does this. The reply tells the user the model cannot see the wardrobe and to pick another model in Settings.

## Alternatives
A probe in Settings "Test connection" (an extra call, still unreliable for LM Studio). A per-model toggle (rejected in requirements).

## Consequences
A restart forgets the result. Servers that ignore tools without an error (such as LM Studio) are not detected. Ambiguous 400s (Gemini) stay plain errors.
