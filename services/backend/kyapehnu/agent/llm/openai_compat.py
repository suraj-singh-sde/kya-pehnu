import base64
import json
from collections.abc import Sequence
from typing import Any, Final

import openai

from kyapehnu.agent.llm.base import (
    AssistantMsg,
    ChatResult,
    LLMOutputError,
    LLMUnavailableError,
    Msg,
    RawResult,
    ToolCallRequest,
    ToolResultMsg,
    ToolSpec,
    ToolsUnsupportedError,
    UserMsg,
    arguments_json,
    media_type,
    provider_message,
    with_distinct_ids,
)

REQUEST_TIMEOUT_SECONDS: Final = 300  # local vision models are slow
SDK_RETRIES: Final = 2
NO_RETRIES: Final = 0  # chat calls happen inside turns, which bound their own retries
SCHEMA_REJECTION_WORDS: Final = ("response_format", "json_schema")
NO_TOOLS_ERRORS: Final = (openai.BadRequestError, openai.NotFoundError, openai.UnprocessableEntityError)


def _object(arguments: Any) -> dict[str, Any] | None:
    """The arguments of a call as the JSON object they should be; servers send a string, some an object already."""
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except (ValueError, RecursionError):  # deeply nested junk overflows the parser's stack
            return None
    return arguments if isinstance(arguments, dict) else None


def _message(m: Msg) -> dict[str, Any]:
    match m:
        case UserMsg():
            return {"role": "user", "content": m.text}
        case AssistantMsg():
            out: dict[str, Any] = {"role": "assistant", "content": m.text or None}
            if m.tool_calls:
                out["tool_calls"] = [
                    {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": arguments_json(c)}}
                    for c in m.tool_calls
                ]
            return out
        case ToolResultMsg():
            return {"role": "tool", "tool_call_id": m.call_id, "content": m.content}


class OpenAICompatTransport:
    """Any server speaking the OpenAI chat API: Ollama, OpenAI, Gemini, OpenRouter, LM Studio."""

    provider = "openai_compatible"
    decision_model = "gpt-6-luna"  # the only model the decisions endpoint takes

    def __init__(self, base_url: str, api_key: str, model: str, client: Any = None) -> None:
        self.base_url, self.api_key, self.model, self._client = base_url, api_key, model, client
        self._strict_schema = True  # flips to False (per process) if the server rejects json_schema

    def _get_client(self) -> Any:
        if self._client is None:
            self._client = openai.OpenAI(
                base_url=self.base_url, api_key=self.api_key, timeout=REQUEST_TIMEOUT_SECONDS, max_retries=SDK_RETRIES
            )
        return self._client

    def _client_with(self, max_retries: int | None) -> Any:
        """The SDK client, with its own retries replaced when a caller asks (chat turns do, R10)."""
        client = self._get_client()
        return client if max_retries is None else client.with_options(max_retries=max_retries)

    def decide_image(
        self, *, image: bytes, text: str, questions: list[dict[str, Any]], max_retries: int | None = None
    ) -> list[dict[str, Any]]:
        """Typed answers about a picture from the decisions endpoint: one answer per question, in order."""
        data_url = f"data:{media_type(image)};base64,{base64.b64encode(image).decode()}"
        body = {
            "model": self.decision_model,
            "input": [
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": text}, {"type": "input_image", "image_url": data_url}],
                }
            ],
            "questions": questions,
        }
        try:  # the installed SDK predates `client.decisions`; the endpoint itself is plain JSON
            r = self._client_with(max_retries).post("/decisions", body=body, cast_to=object)
        except openai.APIError as e:
            raise LLMUnavailableError(provider_message(e)) from e
        answers = r.get("answers") if isinstance(r, dict) else None
        if not isinstance(answers, list):
            raise LLMOutputError("the provider returned no answers")
        return [a for a in answers if isinstance(a, dict)]

    def edit_image(self, *, image: bytes, prompt: str, model: str) -> bytes:
        """Ask an image model to redraw `image` as the prompt says. Returns the picture's bytes."""
        client = self._get_client()
        source = ("garment.png", image, "image/png")
        try:
            try:
                r = client.images.edit(
                    model=model, image=source, prompt=prompt, background="transparent", output_format="png"
                )
            except openai.BadRequestError:
                # Not every image model takes the extras; the caller cuts the background out itself if needed.
                r = client.images.edit(model=model, image=source, prompt=prompt)
        except openai.APIError as e:
            raise LLMUnavailableError(provider_message(e)) from e
        encoded = r.data[0].b64_json if r.data else None
        if not encoded:
            raise LLMOutputError("the provider returned no picture")
        return base64.b64decode(encoded)

    def call_chat(self, *, system: str, messages: Sequence[Msg], tools: Sequence[ToolSpec]) -> ChatResult:
        """Only model, messages and tools are sent: never `tool_choice`, `parallel_tool_calls`, `stream` or
        `response_format`, which servers differ on (AC-46)."""
        defs = [
            {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
            for t in tools
        ]
        try:
            r = self._client_with(NO_RETRIES).chat.completions.create(
                model=self.model,
                messages=[{"role": "system", "content": system}, *map(_message, messages)],
                **({"tools": defs} if defs else {}),
            )
        except NO_TOOLS_ERRORS as e:
            if defs and "tool" in f"{provider_message(e)} {e}".lower():
                raise ToolsUnsupportedError(provider_message(e)) from e
            raise LLMUnavailableError(provider_message(e)) from e
        except openai.APIError as e:
            raise LLMUnavailableError(provider_message(e)) from e
        message = r.choices[0].message if r.choices else None
        calls = (
            ToolCallRequest(tc.id or "", tc.function.name, _object(tc.function.arguments))
            for tc in (message.tool_calls or [] if message else [])
            if getattr(tc, "type", None) == "function" and getattr(tc, "function", None) is not None
        )
        usage = r.usage
        return ChatResult(
            (message.content if message else None) or "",
            with_distinct_ids(calls),
            usage.prompt_tokens if usage else None,
            usage.completion_tokens if usage else None,
        )

    def list_models(self) -> list[str]:
        # ponytail: uses the chat timeout (300 s); give listing its own short timeout if a dead server annoys people
        try:
            return sorted(m.id for m in self._get_client().models.list())
        except openai.APIError as e:
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
        image_parts = [
            {"type": "image_url", "image_url": {"url": f"data:{media_type(b)};base64,{base64.b64encode(b).decode()}"}}
            for b in images
        ]

        def messages(text: str) -> list[dict[str, Any]]:
            return [
                {"role": "system", "content": system},
                {"role": "user", "content": [{"type": "text", "text": text}, *image_parts]},
            ]

        client = self._client_with(max_retries)
        try:
            r = None
            if self._strict_schema:
                try:
                    r = client.chat.completions.create(
                        model=self.model,
                        messages=messages(prompt),
                        response_format={"type": "json_schema", "json_schema": {"name": name, "schema": schema}},
                    )
                except openai.BadRequestError as e:
                    # Remember the rejection only when it is about the format; any other 400 is just this request.
                    self._strict_schema = not any(word in str(e) for word in SCHEMA_REJECTION_WORDS)
            if r is None:
                r = client.chat.completions.create(
                    model=self.model,
                    response_format={"type": "json_object"},
                    messages=messages(
                        f"{prompt}\n\nReply with only a JSON object matching this JSON schema:\n{json.dumps(schema)}"
                    ),
                )
        except openai.APIError as e:
            raise LLMUnavailableError(provider_message(e)) from e
        usage = r.usage
        text = (r.choices[0].message.content if r.choices else None) or ""
        return RawResult(text, usage.prompt_tokens if usage else None, usage.completion_tokens if usage else None)
