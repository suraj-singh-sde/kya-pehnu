"""What WP-1 adds to llm/base.py: `complete_json(log_prefix=)` (LLD 3.4, 3.6) and the chat message types and constants.

`LLMProvider.chat` and the SDK mappings come with the LLM chat layer package, not here.
"""

import dataclasses

import pytest
from pydantic import BaseModel

from kyapehnu.agent.llm import base
from kyapehnu.agent.llm.base import (
    AssistantMsg,
    ChatResult,
    LLMOutputError,
    LLMProvider,
    LLMUnavailableError,
    ToolCallRequest,
    ToolResultMsg,
    ToolSpec,
    ToolsUnsupportedError,
    Transport,
    UserMsg,
)
from tests.fakes import OWNER_ID, FakeTransport


class Ping(BaseModel):
    ok: bool


def _provider(replies):
    logs = []
    return LLMProvider(FakeTransport(replies), log=logs.append), logs


def _ask(provider, **kw):
    return provider.complete_json(
        system="s", prompt="the prompt", images=[], schema=Ping, purpose="t", **{"user_id": OWNER_ID, **kw}
    )


# --- log_prefix ------------------------------------------------------------------------------------------------


def test_a_prefix_and_one_space_start_the_request_summary():
    provider, logs = _provider([{"ok": True}])

    _ask(provider, log_prefix="session=7")

    assert logs[0].request_summary == "session=7 images=[] bytes\nthe prompt"


def test_without_a_prefix_the_request_summary_is_what_it_was():
    provider, logs = _provider([{"ok": True}])

    _ask(provider)

    assert logs[0].request_summary == "images=[] bytes\nthe prompt"


def test_an_explicit_none_prefix_is_no_prefix():
    provider, logs = _provider([{"ok": True}])

    _ask(provider, log_prefix=None)

    assert logs[0].request_summary == "images=[] bytes\nthe prompt"


def test_the_prefix_never_reaches_the_provider():
    provider, _ = _provider([{"ok": True}])

    _ask(provider, log_prefix="session=7")

    sent = provider.transport.calls[0]
    assert sent["prompt"] == "the prompt" and sent["system"] == "s"


def test_the_trailing_space_keeps_session_1_apart_from_session_12():
    provider, logs = _provider([{"ok": True}, {"ok": True}])

    _ask(provider, log_prefix="session=1")
    _ask(provider, log_prefix="session=12")

    assert logs[0].request_summary.startswith("session=1 ")
    assert not logs[1].request_summary.startswith("session=1 ")  # what `LIKE 'session=1 %'` would match


def test_every_attempt_after_invalid_output_carries_the_prefix():
    provider, logs = _provider(['{"ok": "maybe"}', {"ok": True}])

    _ask(provider, log_prefix="session=3")

    assert len(logs) == 2
    assert all(call.request_summary.startswith("session=3 images=[] bytes\n") for call in logs)


def test_a_failed_call_is_logged_with_the_prefix_too():
    provider, logs = _provider([LLMUnavailableError("down")])

    with pytest.raises(LLMUnavailableError):
        _ask(provider, log_prefix="session=3")

    assert logs[0].error and logs[0].request_summary.startswith("session=3 ")


def test_invalid_output_twice_is_still_an_output_error_with_the_prefix_logged():
    provider, logs = _provider(["nope", "still nope"])

    with pytest.raises(LLMOutputError):
        _ask(provider, log_prefix="session=3")

    assert [call.request_summary.startswith("session=3 ") for call in logs] == [True, True]


# --- message types and constants (LLD 3.4) -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cls", "fields"),
    [
        (ToolSpec, ["name", "description", "parameters"]),
        (ToolCallRequest, ["id", "name", "arguments"]),
        (UserMsg, ["text"]),
        (AssistantMsg, ["text", "tool_calls"]),
        (ToolResultMsg, ["call_id", "name", "content", "is_error"]),
        (ChatResult, ["text", "tool_calls", "input_tokens", "output_tokens"]),
    ],
)
def test_message_types_are_frozen_dataclasses_with_the_specified_fields(cls, fields):
    assert dataclasses.is_dataclass(cls)
    assert [f.name for f in dataclasses.fields(cls)] == fields
    assert cls.__dataclass_params__.frozen is True


def test_an_assistant_message_has_no_calls_by_default_and_a_call_may_have_no_arguments_object():
    assert AssistantMsg("hi").tool_calls == ()
    assert ToolCallRequest("c1", "get_item", None).arguments is None


def test_a_message_is_immutable():
    with pytest.raises(dataclasses.FrozenInstanceError):
        UserMsg("hi").text = "changed"  # type: ignore[misc]


def test_tools_unsupported_is_an_unavailable_error_so_old_handlers_still_catch_it():
    assert issubclass(ToolsUnsupportedError, LLMUnavailableError)


def test_chat_constants():
    assert (base.CHAT_PURPOSE, base.CHAT_ERROR_CHARS, base.TOOL_NAME_CHARS) == ("chat", 300, 64)


def test_the_transport_protocol_names_call_chat():
    assert hasattr(Transport, "call_chat")
