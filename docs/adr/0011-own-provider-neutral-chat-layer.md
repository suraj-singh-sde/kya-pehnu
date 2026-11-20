# ADR-0011: Own provider-neutral chat layer over the two SDKs, counts-only logging

Status: Accepted (2026-10-06)

## Context
Chat needs tool calling on two SDKs (`anthropic`, `openai`). The call log must not hold chat text.

## Decision
- `llm/base.py` defines the neutral types (`UserMsg`, `AssistantMsg`, `ToolResultMsg`, `ToolSpec`, `ToolCallRequest`, `ChatResult`). Each transport maps them in a small function.
- `chat()` records through `_record_chat`: counts, tool names and a redacted error cut to 300 characters. It never writes prompt or reply text.
- `openai_compatible` never sends `tool_choice`, `parallel_tool_calls` or `stream`, to any server. One rule, no per-server detection.

## Alternatives
- LiteLLM or an agent framework: a large new dependency for two mappings.
- Reusing `_record`: it stores prompt and reply text.
- The Anthropic SDK beta `tool_runner`: it runs tools itself, so it cannot hold `log_wear` and `update_item_status` back as Confirm proposals. It cannot enforce the per-turn limits or store a row per call as it happens. It is beta and Anthropic-only, while the OpenAI SDK has no equivalent. It cannot do the first-request-only no-tools retry or counts-only logging.

## Consequences
Two small mappings, tested with stand-in clients. No new dependency.
