# ADR-0010: Chat state lives in four new tables; ToolCall carries cards, event links and proposals

Status: Accepted (2026-10-06)

## Context
Phase 3 allows four new tables. Cards, the "created in this chat" set of events and the state of a proposal (Confirm or Cancel) each need a home.

## Decision
- Tables: `chatsession`, `chatmessage`, `toolcall`, `feedback`.
- `toolcall` gains `event_id`, `ui` (the local payload for cards and chips), `truncated` and the proposal fields (`proposal_state`, `proposed_on`, `decided_at`, `outcome`). `chatmessage` holds text only. A proposal is its `toolcall` row.

## Alternatives
- A separate Proposal table: not allowed by the four-table limit.
- Cards stored as JSON on the assistant message: the reply text is written last, so cards would be lost if a turn failed half-way.
- An `Event.session_id` column: changes a Phase 2 table.

## Consequences
One row per tool call explains the thread and the Debug page. Deleting a chat removes all of it. Events created in a chat are kept (they are not owned by the chat).
