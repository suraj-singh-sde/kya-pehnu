import base64
import json
from collections import defaultdict, deque
from collections.abc import Sequence
from typing import Any, Final

import anthropic

from kyapehnu.agent.llm.base import (
    AssistantMsg,
    ChatResult,
    LLMUnavailableError,
    Msg,
    RawResult,
    ToolCallRequest,
    ToolResultMsg,
    ToolSpec,
    UserMsg,
    distinct_id,
    media_type,
    provider_message,
    with_distinct_ids,
)

REQUEST_TIMEOUT_SECONDS: Final = 120
SDK_RETRIES: Final = 2
NO_RETRIES: Final = 0  # chat calls happen inside turns, which bound their own retries
MAX_OUTPUT_TOKENS: Final = 4096
MODEL_LIST_LIMIT: Final = 1000


def _messages(messages: Sequence[Msg]) -> list[dict[str, Any]]:
    """Anthropic wants every tool-use id valid and unique, each answered by one tool_result of the next user message.
    Stored ids from another provider may be neither, so they are replaced here, call and result alike."""
    out: list[dict[str, Any]] = []
    taken: set[str] = set()
    sent: dict[str, deque[str]] = defaultdict(deque)  # the ids of the last assistant message, per stored id
    for m in messages:
        match m:
            case UserMsg():
                if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                    out[-1]["content"].append({"type": "text", "text": m.text})  # the last turn ended on tool results
                else:
                    out.append({"role": "user", "content": m.text})
            case AssistantMsg():
                sent.clear()
                blocks: list[dict[str, Any]] = [{"type": "text", "text": m.text}] if m.text.strip() else []
                for call in m.tool_calls:
                    sent[call.id].append(distinct_id(call.id, taken, strict=True))
                    blocks.append(
                        {"type": "tool_use", "id": sent[call.id][-1], "name": call.name, "input": call.arguments or {}}
                    )
                if blocks:
                    out.append({"role": "assistant", "content": blocks})
            case ToolResultMsg():
                ids = sent[m.call_id]
                block = {
                    "type": "tool_result",
                    "tool_use_id": ids.popleft() if ids else m.call_id,
                    "content": m.content,
                    "is_error": m.is_error,
                }
                if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                    out[-1]["content"].append(block)
                else:
                    out.append({"role": "user", "content": [block]})
    return out


class AnthropicTransport:
    provider = "anthropic"

    def __init__(self, api_key: str | None, model: str, client: Any = None) -> None:
        self.api_key, self.model, self._client = api_key, model, client

    def _get_client(self) -> Any:
        if self._client is None:
            if not self.api_key:
                raise LLMUnavailableError(
                    "No AI provider key is set. Add one in Settings (developers: ANTHROPIC_API_KEY in .env)"
                )
            self._client = anthropic.Anthropic(
                api_key=self.api_key, max_retries=SDK_RETRIES, timeout=REQUEST_TIMEOUT_SECONDS
            )
        return self._client

    def _client_with(self, max_retries: int | None) -> Any:
        """The SDK client, with its own retries replaced when a caller asks (chat turns do, R10)."""
        client = self._get_client()
        return client if max_retries is None else client.with_options(max_retries=max_retries)

    def call_chat(self, *, system: str, messages: Sequence[Msg], tools: Sequence[ToolSpec]) -> ChatResult:
        """Never `tool_choice` or `stream`, and never a `ToolsUnsupportedError`: Anthropic always has tools (AC-51)."""
        defs = [{"name": t.name, "description": t.description, "input_schema": t.parameters} for t in tools]
        try:
            r = self._client_with(NO_RETRIES).messages.create(
                model=self.model,
                max_tokens=MAX_OUTPUT_TOKENS,
                system=system,
                messages=_messages(messages),
                **({"tools": defs} if defs else {}),
            )
        except anthropic.APIError as e:
            raise LLMUnavailableError(provider_message(e)) from e
        calls = (
            ToolCallRequest(b.id, b.name, b.input if isinstance(b.input, dict) else None)
            for b in r.content
            if b.type == "tool_use"
        )
        text = "".join(b.text for b in r.content if b.type == "text")
        return ChatResult(text, with_distinct_ids(calls), r.usage.input_tokens, r.usage.output_tokens)

    def list_models(self) -> list[str]:
        try:
            return [m.id for m in self._get_client().models.list(limit=MODEL_LIST_LIMIT)]
        except anthropic.APIError as e:
            raise LLMUnavailableError(provider_message(e)) from e

    def call_json(
        self,
        *,
        system: str,
        prompt: str,
        images: list[bytes],
        schema: dict[str, Any],
        name: str,
        max_retries: int | None = None,
    ) -> RawResult:
        content: list[dict[str, Any]] = [
            {
                "type": "image",
                "source": {"type": "base64", "media_type": media_type(b), "data": base64.b64encode(b).decode()},
            }
            for b in images
        ]
        content.append({"type": "text", "text": prompt})
        try:
            r = self._client_with(max_retries).messages.create(
                model=self.model,
                max_tokens=MAX_OUTPUT_TOKENS,
                system=system,
                messages=[{"role": "user", "content": content}],
                tools=[{"name": name, "description": "Return the result.", "input_schema": schema}],
                tool_choice={"type": "tool", "name": name},
            )
        except anthropic.APIError as e:
            raise LLMUnavailableError(provider_message(e)) from e
        block = next((b for b in r.content if b.type == "tool_use"), None)
        return RawResult(json.dumps(block.input) if block else "", r.usage.input_tokens, r.usage.output_tokens)
