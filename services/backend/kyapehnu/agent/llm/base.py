import hashlib
import itertools
import json
import logging
import re
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from typing import Any, Final, Protocol

import redis
from pydantic import BaseModel, ValidationError

from kyapehnu.agent.models import LlmCall

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 2  # one retry after invalid output
REQUEST_SUMMARY_CHARS = 2000
RESPONSE_LOG_CHARS = 20000
INVALID_OUTPUT = "invalid_output"
REDACTED: Final = "***"
CHAT_PURPOSE: Final = "chat"
CHAT_ERROR_CHARS: Final = 300
TOOL_NAME_CHARS: Final = 64
FAILED_CALL_LOG: Final = "event=llm_call_failed purpose=%s provider=%s error=%s"
OPENAI_COMPATIBLE: Final = "openai_compatible"
CALL_ID: Final = re.compile(r"[A-Za-z0-9_-]+")  # what Anthropic accepts as a tool-use id
NO_TOOLS_TTL_S: Final = 86_400  # a server that refused tools is asked again after a day


def redact(text: str, secrets: Iterable[str | None]) -> str:
    """Blank out configured API keys: provider error texts of unknown origin can echo the bearer token."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, REDACTED)
    return text


def provider_message(e: Exception) -> str:
    """The provider's own sentence when the SDK error carries one, else the error's text.

    SDK errors print as "Error code: 401 - {'error': {'message': ...}}"; people should read only the message.
    """
    body = getattr(e, "body", None)
    if isinstance(body, dict):
        inner = body["error"] if isinstance(body.get("error"), dict) else body
        message = inner.get("message")
        if isinstance(message, str) and message.strip():
            status = getattr(e, "status_code", None)
            return f"{message.strip()} (error {status})" if status else message.strip()
    return str(e)


class LLMError(Exception):
    pass


class LLMUnavailableError(LLMError):
    """Provider unreachable, misconfigured, or returned an API error."""


class LLMOutputError(LLMError):
    """Provider answered, but not with valid output after one retry."""


class ToolsUnsupportedError(LLMUnavailableError):
    """The server refused a request because it carried tools."""


@dataclass
class RawResult:
    text: str
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class ToolCallRequest:
    id: str
    name: str
    arguments: dict[str, Any] | None  # None: the model sent something that is not a JSON object


@dataclass(frozen=True)
class UserMsg:
    text: str


@dataclass(frozen=True)
class AssistantMsg:
    text: str
    tool_calls: tuple[ToolCallRequest, ...] = ()


@dataclass(frozen=True)
class ToolResultMsg:
    call_id: str
    name: str
    content: str
    is_error: bool


Msg = UserMsg | AssistantMsg | ToolResultMsg


@dataclass(frozen=True)
class ChatResult:
    text: str
    tool_calls: tuple[ToolCallRequest, ...]
    input_tokens: int | None
    output_tokens: int | None


def distinct_id(raw: str, taken: set[str], prefix: str = "call", *, strict: bool = False) -> str:
    """`raw` when it is non-empty (and, if `strict`, valid for Anthropic) and not taken yet, else the next free
    `<prefix>_<n>`. The result is added to `taken`."""
    if raw and raw not in taken and (not strict or CALL_ID.fullmatch(raw)):
        chosen = raw
    else:
        chosen = next(f"{prefix}_{n}" for n in itertools.count(1) if f"{prefix}_{n}" not in taken)
    taken.add(chosen)
    return chosen


def with_distinct_ids(calls: Iterable[ToolCallRequest]) -> tuple[ToolCallRequest, ...]:
    """A reply's calls with an empty or repeated id replaced, so every id is stored and replayed unambiguously."""
    taken: set[str] = set()
    return tuple(replace(c, id=distinct_id(c.id, taken)) for c in calls)


def arguments_json(call: ToolCallRequest) -> str:
    """What goes out as the arguments of a call: the JSON object, an empty one when the model sent none."""
    return json.dumps(call.arguments or {})


def message_chars(m: Msg) -> int:
    """The size of one message in characters: its text, its arguments' JSON or its result."""
    if isinstance(m, ToolResultMsg):
        return len(m.content)
    if isinstance(m, AssistantMsg):
        return len(m.text) + sum(len(arguments_json(c)) for c in m.tool_calls)
    return len(m.text)


def _chars(system: str, messages: Sequence[Msg]) -> int:
    """The size of a chat request in characters: the system prompt, the texts, the arguments' JSON, the results."""
    return len(system) + sum(map(message_chars, messages))


class Transport(Protocol):
    provider: str
    model: str

    def call_json(
        self,
        *,
        system: str,
        prompt: str,
        images: list[bytes],
        schema: dict[str, Any],
        name: str,
        max_retries: int | None = None,
    ) -> RawResult: ...

    def call_chat(self, *, system: str, messages: Sequence[Msg], tools: Sequence[ToolSpec]) -> ChatResult: ...

    def list_models(self) -> list[str]: ...


_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


def _strip_fences(text: str) -> str:
    m = _FENCE.match(text)
    return m.group(1) if m else text


def media_type(data: bytes) -> str:
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


class LLMProvider:
    """The only entry point the rest of the app uses for LLM calls."""

    def __init__(
        self,
        transport: Transport,
        log: Callable[[LlmCall], None] = lambda _c: None,
        kv: redis.Redis | None = None,
        max_retries: int | None = None,
    ) -> None:
        self.transport = transport
        self.kv = kv  # the shared Valkey holds the no-tools memory; None: nothing is remembered (tests)
        self.max_retries = max_retries  # None: the transport's own SDK retries
        self._log_sink = log

    def set_transport(self, transport: Transport) -> None:
        """Switch provider or model for every later call (a single attribute write: safe across threads)."""
        self.transport = transport

    def with_transport(self, transport: Transport) -> "LLMProvider":
        """A provider for trying out a candidate transport: same call log, the live transport untouched."""
        return LLMProvider(transport, log=self._log_sink, kv=self.kv, max_retries=self.max_retries)

    def with_max_retries(self, max_retries: int) -> "LLMProvider":
        """The same provider (call log, Valkey, transport) with its SDK retries fixed: chat turns use 0, so a model call
        is one request and a turn's length stays bounded (R10)."""
        return LLMProvider(self.transport, log=self._log_sink, kv=self.kv, max_retries=max_retries)

    @property
    def provider_name(self) -> str:
        return self.transport.provider

    @property
    def model(self) -> str:
        return self.transport.model

    def complete_json[T: BaseModel](
        self,
        *,
        system: str,
        prompt: str,
        images: list[bytes],
        schema: type[T],
        purpose: str,
        user_id: int,
        log_prefix: str | None = None,
        max_retries: int | None = None,
    ) -> T:
        """`log_prefix` starts the logged request_summary (then one space), so a session's rows can be found again."""
        json_schema, name = schema.model_json_schema(), schema.__name__
        transport = self.transport  # read once: settings saved mid-call must not relabel or un-redact this call
        retries = self.max_retries if max_retries is None else max_retries
        extra = {} if retries is None else {"max_retries": retries}  # only sent when set: a transport may not take it
        attempt_prompt, last_error = prompt, ""
        for _ in range(MAX_ATTEMPTS):
            sent, raw, error, failure, start = attempt_prompt, None, None, None, time.perf_counter()
            try:
                raw = transport.call_json(
                    system=system, prompt=sent, images=images, schema=json_schema, name=name, **extra
                )
                return schema.model_validate_json(_strip_fences(raw.text))
            except LLMUnavailableError as e:
                error, failure = f"unavailable: {e}", type(e).__name__
                raise
            except ValidationError as e:
                error = last_error = f"invalid output: {e}"
                failure = INVALID_OUTPUT
                attempt_prompt = (
                    f"{prompt}\n\nYour previous reply was invalid:\n{e}\n"
                    "Reply again with only valid JSON matching the schema."
                )
            except Exception as e:
                error, failure = f"unexpected: {type(e).__name__}", type(e).__name__
                raise
            finally:
                self._record(transport, purpose, sent, images, raw, error, failure, start, user_id, prefix=log_prefix)
        raise LLMOutputError(last_error)

    def tools_disabled(self) -> bool:
        key = self._no_tools_key(self.transport)
        if key is None or self.kv is None:
            return False
        try:
            return bool(self.kv.exists(key))
        except redis.RedisError:
            log.warning("event=valkey_unavailable where=no_tools_read")  # not remembered: tools are tried again
            return False

    @staticmethod
    def _no_tools_key(transport: Transport) -> str | None:
        """The Valkey key a refusal is remembered under, for the (base URL, model) pair. Only OpenAI-compatible servers
        have one (AC-51). The pair is hashed: a URL can hold a hostname or a path nobody wants in a key listing."""
        base_url = getattr(transport, "base_url", None)
        if transport.provider != OPENAI_COMPATIBLE or base_url is None:
            return None
        return "notools:" + hashlib.sha256(f"{base_url}\n{transport.model}".encode()).hexdigest()

    def _remember_refusal(self, transport: Transport, tools: Sequence[ToolSpec], first_call: bool) -> None:
        key = self._no_tools_key(transport)
        if not (first_call and tools and key is not None and self.kv is not None):
            return
        try:
            self.kv.set(key, "1", ex=NO_TOOLS_TTL_S)
        except redis.RedisError:
            log.warning("event=valkey_unavailable where=no_tools_write")  # asked again next turn, nothing worse
        log.info("event=chat_no_tools provider=%s", transport.provider)

    def chat(
        self,
        *,
        system: str,
        messages: Sequence[Msg],
        tools: Sequence[ToolSpec],
        label: str,
        first_call: bool,
        user_id: int,
    ) -> ChatResult:
        """One model request of a chat turn. Only the first request of a turn that carried tools can mark the
        (base URL, model) as unable to use tools; the row it logs holds counts and tool names, never chat text."""
        transport = self.transport  # read once: settings saved mid-call must not relabel or un-redact this call
        result, error, failure, start = None, None, None, time.perf_counter()
        try:
            result = transport.call_chat(system=system, messages=messages, tools=tools)
            return result
        except LLMUnavailableError as e:
            if isinstance(e, ToolsUnsupportedError):
                self._remember_refusal(transport, tools, first_call)
            error, failure = f"unavailable: {e}", type(e).__name__
            raise
        except Exception as e:
            error, failure = f"unexpected: {type(e).__name__}", type(e).__name__
            raise
        finally:
            self._record_chat(transport, label, system, messages, tools, result, error, failure, start, user_id)

    def _record_chat(
        self,
        transport: Transport,
        label: str,
        system: str,
        messages: Sequence[Msg],
        tools: Sequence[ToolSpec],
        result: ChatResult | None,
        error: str | None,
        failure: str | None,
        start: float,
        user_id: int,
    ) -> None:
        if failure:
            log.warning(FAILED_CALL_LOG, CHAT_PURPOSE, transport.provider, failure)
        if error:  # redacted first: a key cut in half at the limit would no longer match
            error = redact(error, (getattr(transport, "api_key", None),))[:CHAT_ERROR_CHARS]
        names = ",".join(t.name for t in tools)
        self._emit(
            LlmCall(
                user_id=user_id,
                provider=transport.provider,
                model=transport.model,
                purpose=CHAT_PURPOSE,
                request_summary=(
                    f"{label} messages={len(messages)} chars={_chars(system, messages)} "
                    f"tools={len(tools)} [{names}] no_tools={not tools}"
                ),
                response=None
                if result is None
                else (
                    f"tool_calls=[{','.join(c.name[:TOOL_NAME_CHARS] for c in result.tool_calls)}] "
                    f"reply_chars={len(result.text)}"
                ),
                latency_ms=int((time.perf_counter() - start) * 1000),
                input_tokens=result.input_tokens if result else None,
                output_tokens=result.output_tokens if result else None,
                error=error,
            )
        )

    @property
    def can_decide(self) -> bool:
        """Whether the provider answers typed questions about a picture (OpenAI's decision model does)."""
        return getattr(self.transport, "decide_image", None) is not None

    def decide(
        self,
        *,
        image: bytes,
        text: str,
        questions: list[dict[str, Any]],
        purpose: str,
        user_id: int,
        max_retries: int | None = None,
    ) -> list[dict[str, Any]]:
        """Typed answers about a picture. Only some transports can; the call is logged like any other.
        `max_retries` as in `complete_json`: the provider's own setting unless given, so a call inside a chat turn is
        one request."""
        transport, start, error, failure, out = self.transport, time.perf_counter(), None, None, None
        retries = self.max_retries if max_retries is None else max_retries
        extra = {} if retries is None else {"max_retries": retries}
        try:
            decide = getattr(transport, "decide_image", None)
            if decide is None:
                raise LLMUnavailableError("This AI provider cannot check pictures")
            out = list(decide(image=image, text=text, questions=questions, **extra))
            return out
        except LLMError as e:
            error, failure = f"{type(e).__name__}: {e}", type(e).__name__
            raise
        except Exception as e:
            error, failure = f"unexpected: {type(e).__name__}", type(e).__name__
            raise
        finally:
            raw = RawResult(json.dumps(out)) if out is not None else None
            model = getattr(transport, "decision_model", None)
            self._record(transport, purpose, text, [image], raw, error, failure, start, user_id, model=model)

    def edit_image(self, *, image: bytes, prompt: str, model: str, purpose: str, user_id: int) -> bytes:
        """Generate a picture from a picture. Only some transports can; the call is logged like any other."""
        transport, start, error, failure, out = self.transport, time.perf_counter(), None, None, None
        try:
            edit = getattr(transport, "edit_image", None)
            if edit is None:
                raise LLMUnavailableError("This AI provider cannot generate pictures")
            out = bytes(edit(image=image, prompt=prompt, model=model))
            return out
        except LLMError as e:
            error, failure = f"{type(e).__name__}: {e}", type(e).__name__
            raise
        except Exception as e:
            error, failure = f"unexpected: {type(e).__name__}", type(e).__name__
            raise
        finally:
            raw = RawResult(f"image, {len(out)} bytes") if out is not None else None
            self._record(transport, purpose, prompt, [image], raw, error, failure, start, user_id, model=model)

    def _record(
        self,
        transport: Transport,
        purpose: str,
        prompt: str,
        images: list[bytes],
        raw: RawResult | None,
        error: str | None,
        failure: str | None,
        start: float,
        user_id: int,
        model: str | None = None,
        prefix: str | None = None,
    ) -> None:
        if failure:
            # Class or "invalid_output" only: messages can echo prompts, replies or keys.
            log.warning(FAILED_CALL_LOG, purpose, transport.provider, failure)
        if error:  # provider error text can echo the key, and this row ends up in backups
            error = redact(error, (getattr(transport, "api_key", None),))
        head = f"{prefix} " if prefix else ""  # the space keeps `session=1 ` from matching `session=12 `
        call = LlmCall(
            user_id=user_id,
            provider=transport.provider,
            model=model or transport.model,
            purpose=purpose,
            request_summary=f"{head}images={[len(b) for b in images]} bytes\n{prompt[:REQUEST_SUMMARY_CHARS]}",
            response=raw.text[:RESPONSE_LOG_CHARS] if raw else None,
            latency_ms=int((time.perf_counter() - start) * 1000),
            input_tokens=raw.input_tokens if raw else None,
            output_tokens=raw.output_tokens if raw else None,
            error=error,
        )
        self._emit(call)

    def _emit(self, call: LlmCall) -> None:
        try:
            self._log_sink(call)
        except Exception as e:  # a failing sink must not fail the call; its message can hold the reply text
            log.warning("event=llm_log_failed error=%s", type(e).__name__)
