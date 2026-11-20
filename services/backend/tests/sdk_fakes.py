"""Stand-ins for the two SDK clients (LLD 8.1), so the transports are tested without a network.

`FakeAnthropicClient(*script)` and `FakeOpenAIClient(*script)` answer the one method each transport calls
(`messages.create`, `chat.completions.create`). Every script entry is a reply built with the helpers below, an
exception (`sdk_error(...)` builds the SDK's real error classes), or a callable run first that returns one of those.
Every request is recorded (a deep copy of the keyword arguments) in `.calls`; an empty script fails the test.
"""

import copy
import importlib
from types import SimpleNamespace
from typing import Any

import httpx

from kyapehnu.agent.llm.openai_compat import OpenAICompatTransport

OLLAMA_URL = "http://localhost:11434/v1"
LM_STUDIO_URL = "http://localhost:1234/v1"
_FROM_MESSAGE: Any = object()


def _next(script: list[Any], kw: dict[str, Any], calls: list[dict[str, Any]], retries: list[Any], n: Any = None) -> Any:
    calls.append(copy.deepcopy(kw))
    retries.append(n)
    if not script:
        raise AssertionError("unexpected SDK call")
    reply = script.pop(0)
    if callable(reply):
        reply = reply()
    if isinstance(reply, Exception):
        raise reply
    return reply


class _FakeClient:
    """`retries` holds, per request, the `max_retries` a `with_options` view set (None: the client's own default)."""

    def __init__(self, *script: Any) -> None:
        self.script, self.calls, self.retries, self._set = list(script), [], [], None

    def _create(self, **kw: Any) -> Any:
        return _next(self.script, kw, self.calls, self.retries, self._set)

    def with_options(self, *, max_retries: int) -> Any:
        view = copy.copy(self)  # shares the script and the lists, so the view's requests are recorded here
        view._set = max_retries
        view._wire()
        return view

    def _wire(self) -> None:
        raise NotImplementedError


class FakeAnthropicClient(_FakeClient):
    def __init__(self, *script: Any) -> None:
        super().__init__(*script)
        self._wire()

    def _wire(self) -> None:
        self.messages = SimpleNamespace(create=self._create)


class FakeOpenAIClient(_FakeClient):
    def __init__(self, *script: Any) -> None:
        super().__init__(*script)
        self._wire()

    def _wire(self) -> None:
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))


# --- Anthropic replies ---------------------------------------------------------------------------------------------


def text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def tool_use_block(id: str, name: str, input: Any) -> SimpleNamespace:  # noqa: A002 - the SDK's own field names
    return SimpleNamespace(type="tool_use", id=id, name=name, input=input)


def anthropic_reply(*blocks: Any, usage: tuple[int, int] = (7, 3)) -> SimpleNamespace:
    return SimpleNamespace(content=list(blocks), usage=SimpleNamespace(input_tokens=usage[0], output_tokens=usage[1]))


# --- OpenAI replies ------------------------------------------------------------------------------------------------


def function_call(id: str, name: str, arguments: Any) -> SimpleNamespace:  # noqa: A002
    return SimpleNamespace(id=id, type="function", function=SimpleNamespace(name=name, arguments=arguments))


def custom_call(id: str, name: str, input: str = "raw") -> SimpleNamespace:  # noqa: A002
    """A `custom` tool call: the SDK object has no `function` attribute at all."""
    return SimpleNamespace(id=id, type="custom", custom=SimpleNamespace(name=name, input=input))


def call_without_function(id: str) -> SimpleNamespace:  # noqa: A002
    return SimpleNamespace(id=id, type="function", function=None)


def openai_reply(text: str | None = None, *calls: Any, usage: tuple[int, int] | None = (3, 2)) -> SimpleNamespace:
    message = SimpleNamespace(content=text, tool_calls=list(calls) or None)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message)],
        usage=None if usage is None else SimpleNamespace(prompt_tokens=usage[0], completion_tokens=usage[1]),
    )


def no_choices_reply() -> SimpleNamespace:
    return SimpleNamespace(choices=[], usage=None)


# --- errors and the app --------------------------------------------------------------------------------------------


def sdk_error(module: str, cls_name: str, status: int, message: str, body: Any = _FROM_MESSAGE) -> Exception:
    """The SDK's own error class (`anthropic` or `openai`), built as the SDK builds it from an HTTP error."""
    cls = getattr(importlib.import_module(module), cls_name)
    response = httpx.Response(status, request=httpx.Request("POST", "http://stub/v1"))
    return cls(message, response=response, body={"error": {"message": message}} if body is _FROM_MESSAGE else body)


def openai_app(
    settings: Any, fake_weather: Any, client: Any, *, model: str = "m", base_url: str = OLLAMA_URL, kv: Any = None
) -> Any:
    """An app whose AI provider is an OpenAI-compatible transport on this stand-in client (Ollama's URL by default)."""
    from kyapehnu.core.app import create_app  # lazy: keeps the test modules importable on their own

    transport = OpenAICompatTransport(base_url, "ollama", model, client=client)
    return create_app(
        settings=settings, transport=transport, weather_transport=httpx.MockTransport(fake_weather), kv=kv
    )
