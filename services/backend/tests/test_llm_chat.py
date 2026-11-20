"""`LLMProvider.chat` and the two `call_chat` mappings (LLD 3.4 and 8.3; AC-3, AC-4, AC-46; gov lows 6 and 7).

No network: the transports run on the SDK stand-ins of `tests/sdk_fakes.py`; the wire-level checks use the real SDK
clients over `httpx.MockTransport`.
"""

import json
import logging
import re
from pathlib import Path

import anthropic
import httpx
import httpx2
import openai
import pytest
from sqlmodel import Session, select

from kyapehnu.agent.llm.anthropic import MAX_OUTPUT_TOKENS, AnthropicTransport
from kyapehnu.agent.llm.base import (
    AssistantMsg,
    ChatResult,
    LLMProvider,
    LLMUnavailableError,
    ToolCallRequest,
    ToolResultMsg,
    ToolSpec,
    ToolsUnsupportedError,
    UserMsg,
)
from kyapehnu.agent.llm.logging import db_logger
from kyapehnu.agent.llm.openai_compat import OpenAICompatTransport
from kyapehnu.agent.models import LlmCall
from tests.chat_rows import with_owner
from tests.fakes import FakeTransport
from tests.sdk_fakes import (
    LM_STUDIO_URL,
    OLLAMA_URL,
    FakeAnthropicClient,
    FakeOpenAIClient,
    anthropic_reply,
    call_without_function,
    custom_call,
    function_call,
    no_choices_reply,
    openai_reply,
    sdk_error,
    text_block,
    tool_use_block,
)

FIXTURES = Path(__file__).parent / "fixtures" / "llm"
MARKER_USER, MARKER_REPLY, MARKER_ARGS, MARKER_RESULT, MARKER_SYSTEM = (
    "MARKER-USER-7Q3X",
    "MARKER-REPLY-9K2W",
    "MARKER-ARGS-3D8F",
    "MARKER-RESULT-6B1N",
    "MARKER-SYSTEM-2H5V",
)
MARKER_EXCEPTION = "MARKER-EXC-4X7Q"
LEAK_KEY = "sk-LEAK-0123456789"
OLD_KEY, NEW_KEY = "sk-OLD-111111", "sk-NEW-000000"
ID_PATTERN = re.compile(r"[A-Za-z0-9_-]+")
GET_ITEM = ToolSpec(
    "get_item", "One item", {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]}
)
SEARCH = ToolSpec("search_wardrobe", "Find items", {"type": "object", "properties": {"q": {"type": "string"}}})
HI = UserMsg("hi")
KINDS = pytest.mark.parametrize("kind", ["anthropic", "openai"])


def transport_for(kind: str, *script, key: str = "sk-test-KEY-123456"):
    """(transport, stand-in client) for either SDK."""
    if kind == "anthropic":
        client = FakeAnthropicClient(*script)
        return AnthropicTransport(key, "claude-x", client=client), client
    client = FakeOpenAIClient(*script)
    return OpenAICompatTransport(OLLAMA_URL, key, "qwen", client=client), client


def sdk_reply(kind: str, text: str = "", calls: tuple = (), usage: tuple[int, int] = (7, 3)):
    """A reply in the SDK's shape: calls are (id, name, arguments object)."""
    if kind == "anthropic":
        blocks = [text_block(text)] if text else []
        blocks += [tool_use_block(i, n, a) for i, n, a in calls]
        return anthropic_reply(*blocks, usage=usage)
    return openai_reply(text or None, *[function_call(i, n, json.dumps(a)) for i, n, a in calls], usage=usage)


def chat(transport, messages=(HI,), tools=(), system="SYS"):
    return transport.call_chat(system=system, messages=list(messages), tools=list(tools))


# --- AC-3: the reply, for both transports --------------------------------------------------------------------------

REPLY_CASES = {
    "text only": ("Hello there", (), ChatResult("Hello there", (), 7, 3)),
    "one call": (
        "",
        (("c1", "get_item", {"id": 3}),),
        ChatResult("", (ToolCallRequest("c1", "get_item", {"id": 3}),), 7, 3),
    ),
    "two calls and text": (
        "Looking.",
        (("c1", "search_wardrobe", {"q": "shirt"}), ("c2", "get_item", {"id": 3})),
        ChatResult(
            "Looking.",
            (ToolCallRequest("c1", "search_wardrobe", {"q": "shirt"}), ToolCallRequest("c2", "get_item", {"id": 3})),
            7,
            3,
        ),
    ),
}


@KINDS
@pytest.mark.parametrize("case", REPLY_CASES)
def test_call_chat_maps_text_one_call_two_calls(kind, case):
    text, calls, expected = REPLY_CASES[case]
    transport, _ = transport_for(kind, sdk_reply(kind, text, calls))

    assert chat(transport, tools=[GET_ITEM, SEARCH]) == expected


def test_openai_reply_without_usage_has_no_token_counts():
    transport, _ = transport_for("openai", openai_reply("Hi", usage=None))

    result = chat(transport)

    assert (result.text, result.input_tokens, result.output_tokens) == ("Hi", None, None)


def test_openai_reply_with_no_choices_or_no_content_is_empty_text_not_an_error():
    transport, _ = transport_for("openai", no_choices_reply(), openai_reply(None))

    assert chat(transport) == ChatResult("", (), None, None)
    assert chat(transport) == ChatResult("", (), 3, 2)


def test_anthropic_joins_text_blocks_and_ignores_other_block_types():
    reply = anthropic_reply(
        text_block("One. "),
        type("Thinking", (), {"type": "thinking", "thinking": "private"})(),
        text_block("Two."),
        tool_use_block("c1", "get_item", {"id": 1}),
    )
    transport, _ = transport_for("anthropic", reply)

    result = chat(transport)

    assert result.text == "One. Two." and [c.name for c in result.tool_calls] == ["get_item"]


def test_anthropic_tool_input_that_is_not_an_object_becomes_none():
    transport, _ = transport_for("anthropic", anthropic_reply(tool_use_block("c1", "get_item", "oops")))

    assert chat(transport).tool_calls == (ToolCallRequest("c1", "get_item", None),)


ARGUMENT_SHAPES = {
    "json string": ('{"id": 3}', {"id": 3}),
    "an object, as some servers send it": ({"id": 3}, {"id": 3}),
    "empty object string": ("{}", {}),
    "invalid json": ('{"id": ', None),
    "empty string": ("", None),
    "json that is not an object": ("[1, 2]", None),
    "json number": ("5", None),
    "json null": ("null", None),
    "missing": (None, None),
    "a list object": ([1, 2], None),
}


@pytest.mark.parametrize("shape", ARGUMENT_SHAPES)
def test_openai_arguments_as_json_string_object_or_junk(shape):
    arguments, expected = ARGUMENT_SHAPES[shape]
    transport, _ = transport_for("openai", openai_reply(None, function_call("c1", "get_item", arguments)))

    assert chat(transport).tool_calls == (ToolCallRequest("c1", "get_item", expected),)


def test_openai_drops_a_custom_call_and_a_call_without_function_and_keeps_the_others_in_order():
    reply = openai_reply(
        "Hm",
        function_call("c1", "search_wardrobe", '{"q": "a"}'),
        custom_call("c2", "freeform"),
        call_without_function("c3"),
        function_call("c4", "get_item", '{"id": 4}'),
    )
    transport, _ = transport_for("openai", reply)

    result = chat(transport)

    assert [(c.id, c.name) for c in result.tool_calls] == [("c1", "search_wardrobe"), ("c4", "get_item")]
    assert result.text == "Hm"


@KINDS
def test_an_empty_or_repeated_call_id_in_one_reply_is_replaced_before_the_reply_leaves_the_transport(kind):
    calls = (("dup", "search_wardrobe", {}), ("dup", "get_item", {"id": 1}), ("", "get_item", {"id": 2}))
    transport, _ = transport_for(kind, sdk_reply(kind, "", calls))

    result = chat(transport)

    ids = [c.id for c in result.tool_calls]
    assert [c.name for c in result.tool_calls] == ["search_wardrobe", "get_item", "get_item"]  # none lost or reordered
    assert [c.arguments for c in result.tool_calls] == [{}, {"id": 1}, {"id": 2}]
    assert ids[0] == "dup" and len(set(ids)) == 3
    assert all(re.fullmatch(r"call_\d+", i) for i in ids[1:])


@KINDS
def test_distinct_non_empty_ids_are_left_as_they_are(kind):
    calls = (("toolu_01A-b.c:0", "get_item", {"id": 1}), ("call_xyz", "get_item", {"id": 2}))
    transport, _ = transport_for(kind, sdk_reply(kind, "", calls))

    assert [c.id for c in chat(transport).tool_calls] == ["toolu_01A-b.c:0", "call_xyz"]


# --- the request: tools, never tool_choice / parallel_tool_calls / stream (AC-46) -----------------------------------

CONVERSATION = [
    UserMsg("hi"),
    AssistantMsg(
        "Let me look",
        (ToolCallRequest("c1", "search_wardrobe", {"q": "shirt"}), ToolCallRequest("c2", "get_item", {"id": 3})),
    ),
    ToolResultMsg("c1", "search_wardrobe", "R1", False),
    ToolResultMsg("c2", "get_item", "R2", True),
    AssistantMsg("", (ToolCallRequest("c3", "get_item", None),)),
    ToolResultMsg("c3", "get_item", "R3", False),
    AssistantMsg("Done"),
    UserMsg("thanks"),
]


def test_anthropic_request_holds_only_model_max_tokens_system_messages_and_tools():
    transport, client = transport_for("anthropic", sdk_reply("anthropic", "ok"))

    chat(transport, tools=[GET_ITEM])

    (kw,) = client.calls
    assert set(kw) == {"model", "max_tokens", "system", "messages", "tools"}  # never tool_choice, never stream
    assert (kw["model"], kw["max_tokens"], kw["system"]) == ("claude-x", MAX_OUTPUT_TOKENS, "SYS")
    assert kw["messages"] == [{"role": "user", "content": "hi"}]


def test_anthropic_tools_become_name_description_input_schema():
    transport, client = transport_for("anthropic", sdk_reply("anthropic", "ok"))

    chat(transport, tools=[GET_ITEM, SEARCH])

    assert client.calls[0]["tools"] == [
        {"name": "get_item", "description": "One item", "input_schema": GET_ITEM.parameters},
        {"name": "search_wardrobe", "description": "Find items", "input_schema": SEARCH.parameters},
    ]


def test_anthropic_without_tools_sends_no_tools_key():
    transport, client = transport_for("anthropic", sdk_reply("anthropic", "ok"))

    chat(transport, tools=[])

    assert set(client.calls[0]) == {"model", "max_tokens", "system", "messages"}


def test_anthropic_maps_every_message_kind_and_joins_consecutive_results_in_one_user_message():
    transport, client = transport_for("anthropic", sdk_reply("anthropic", "ok"))

    chat(transport, CONVERSATION, tools=[GET_ITEM])

    assert client.calls[0]["messages"] == [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Let me look"},
                {"type": "tool_use", "id": "c1", "name": "search_wardrobe", "input": {"q": "shirt"}},
                {"type": "tool_use", "id": "c2", "name": "get_item", "input": {"id": 3}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "c1", "content": "R1", "is_error": False},
                {"type": "tool_result", "tool_use_id": "c2", "content": "R2", "is_error": True},
            ],
        },
        {"role": "assistant", "content": [{"type": "tool_use", "id": "c3", "name": "get_item", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c3", "content": "R3", "is_error": False}]},
        {"role": "assistant", "content": [{"type": "text", "text": "Done"}]},
        {"role": "user", "content": "thanks"},
    ]


def test_anthropic_user_text_after_results_joins_their_user_message():
    """A turn that ended on the looks has no assistant text after its results: the next message joins them."""
    messages = [
        UserMsg("q"),
        AssistantMsg("", (ToolCallRequest("c1", "get_item", {"id": 1}),)),
        ToolResultMsg("c1", "get_item", "R1", False),
        UserMsg("next"),
    ]
    transport, client = transport_for("anthropic", sdk_reply("anthropic", "ok"))

    chat(transport, messages)

    sent = client.calls[0]["messages"]
    assert len(sent) == 3 and sent[2]["content"][0]["type"] == "tool_result"
    assert sent[2]["content"][1] == {"type": "text", "text": "next"}


def _tool_use_ids(sent: list[dict]) -> list[str]:
    return [b["id"] for m in sent if m["role"] == "assistant" for b in m["content"] if b["type"] == "tool_use"]


def _result_blocks(sent: list[dict]) -> list[dict]:
    return [
        b
        for m in sent
        if m["role"] == "user" and isinstance(m["content"], list)
        for b in m["content"]
        if b["type"] == "tool_result"
    ]


@pytest.mark.parametrize("bad_id", ["", "get.item:0", "call abc", "functions.get_item:0", "idé"])
def test_anthropic_replays_an_id_it_would_reject_under_a_valid_one_used_by_tool_use_and_tool_result(bad_id):
    messages = [
        UserMsg("q"),
        AssistantMsg("", (ToolCallRequest(bad_id, "get_item", {"id": 1}),)),
        ToolResultMsg(bad_id, "get_item", "R1", False),
    ]
    transport, client = transport_for("anthropic", sdk_reply("anthropic", "ok"))

    chat(transport, messages)

    sent = client.calls[0]["messages"]
    (use_id,) = _tool_use_ids(sent)
    assert ID_PATTERN.fullmatch(use_id) and [b["tool_use_id"] for b in _result_blocks(sent)] == [use_id]


def test_anthropic_replays_an_id_repeated_across_the_conversation_as_unique_ids_each_paired_with_its_result():
    messages = [
        UserMsg("q1"),
        AssistantMsg("", (ToolCallRequest("c1", "get_item", {"id": 1}),)),
        ToolResultMsg("c1", "get_item", "R-first", False),
        AssistantMsg("one"),
        UserMsg("q2"),
        AssistantMsg("", (ToolCallRequest("c1", "get_item", {"id": 2}),)),  # the stand-in sends c1 in every reply
        ToolResultMsg("c1", "get_item", "R-second", False),
    ]
    transport, client = transport_for("anthropic", sdk_reply("anthropic", "ok"))

    chat(transport, messages)

    sent = client.calls[0]["messages"]
    uses, results = _tool_use_ids(sent), _result_blocks(sent)
    assert len(set(uses)) == 2 and all(ID_PATTERN.fullmatch(i) for i in uses)
    assert [(b["tool_use_id"], b["content"]) for b in results] == [(uses[0], "R-first"), (uses[1], "R-second")]


def test_anthropic_valid_ids_go_out_unchanged():
    messages = [
        UserMsg("q"),
        AssistantMsg("", (ToolCallRequest("toolu_01A-b_c", "get_item", {"id": 1}),)),
        ToolResultMsg("toolu_01A-b_c", "get_item", "R1", False),
    ]
    transport, client = transport_for("anthropic", sdk_reply("anthropic", "ok"))

    chat(transport, messages)

    assert _tool_use_ids(client.calls[0]["messages"]) == ["toolu_01A-b_c"]


@pytest.mark.parametrize("base_url", [OLLAMA_URL, LM_STUDIO_URL], ids=["ollama", "lmstudio"])
def test_no_tool_choice_parallel_or_stream_in_any_chat_request(base_url):
    client = FakeOpenAIClient(sdk_reply("openai", "ok"), sdk_reply("openai", "ok"))
    transport = OpenAICompatTransport(base_url, "k", "qwen", client=client)

    chat(transport, CONVERSATION, tools=[GET_ITEM, SEARCH])
    chat(transport, [UserMsg("hi")], tools=[])

    with_tools, without = client.calls
    assert set(with_tools) == {"model", "messages", "tools"}
    assert set(without) == {"model", "messages"}
    assert with_tools["model"] == "qwen"


def test_openai_tools_are_nested_under_function_and_the_system_prompt_comes_first():
    transport, client = transport_for("openai", sdk_reply("openai", "ok"))

    chat(transport, tools=[GET_ITEM, SEARCH], system="THE SYSTEM")

    (kw,) = client.calls
    assert kw["tools"] == [
        {
            "type": "function",
            "function": {"name": "get_item", "description": "One item", "parameters": GET_ITEM.parameters},
        },
        {
            "type": "function",
            "function": {"name": "search_wardrobe", "description": "Find items", "parameters": SEARCH.parameters},
        },
    ]
    assert kw["messages"] == [{"role": "system", "content": "THE SYSTEM"}, {"role": "user", "content": "hi"}]


def test_openai_maps_every_message_kind_with_role_tool_messages_and_arguments_as_json_strings():
    transport, client = transport_for("openai", sdk_reply("openai", "ok"))

    chat(transport, CONVERSATION, tools=[GET_ITEM])

    sent = client.calls[0]["messages"]
    arguments = [c["function"]["arguments"] for m in sent for c in m.get("tool_calls", [])]
    assert all(isinstance(a, str) for a in arguments)
    assert [json.loads(a) for a in arguments] == [{"q": "shirt"}, {"id": 3}, {}]  # None goes out as an empty object
    for m in sent:
        for c in m.get("tool_calls", []):
            c["function"]["arguments"] = json.loads(c["function"]["arguments"])
    assert sent == [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": "Let me look",
            "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "search_wardrobe", "arguments": {"q": "shirt"}}},
                {"id": "c2", "type": "function", "function": {"name": "get_item", "arguments": {"id": 3}}},
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "R1"},
        {"role": "tool", "tool_call_id": "c2", "content": "R2"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "c3", "type": "function", "function": {"name": "get_item", "arguments": {}}}],
        },
        {"role": "tool", "tool_call_id": "c3", "content": "R3"},
        {"role": "assistant", "content": "Done"},  # no calls: the key is left out
        {"role": "user", "content": "thanks"},
    ]


def test_openai_replays_the_ids_as_they_are():
    messages = [
        UserMsg("q"),
        AssistantMsg("", (ToolCallRequest("call_abc.1:0", "get_item", {"id": 1}),)),
        ToolResultMsg("call_abc.1:0", "get_item", "R1", False),
    ]
    transport, client = transport_for("openai", sdk_reply("openai", "ok"))

    chat(transport, messages)

    sent = client.calls[0]["messages"]
    assert sent[2]["tool_calls"][0]["id"] == "call_abc.1:0" and sent[3]["tool_call_id"] == "call_abc.1:0"


# --- errors: the SDK's classes become one domain error (AC-51 for Anthropic; the detector is in test_chat_no_tools) ---

ANTHROPIC_ERRORS = [
    ("BadRequestError", 400, "messages.2: tool_use ids were found without tool_result blocks"),
    ("NotFoundError", 404, "model not found, tool_use unavailable"),
    ("UnprocessableEntityError", 422, "tool_result is invalid"),
    ("AuthenticationError", 401, "invalid x-api-key"),
    ("RateLimitError", 429, "slow down"),
    ("InternalServerError", 500, "boom"),
]


@pytest.mark.parametrize(("cls_name", "status", "message"), ANTHROPIC_ERRORS)
def test_every_anthropic_api_error_is_a_plain_unavailable_error_never_tools_unsupported(cls_name, status, message):
    transport, _ = transport_for("anthropic", sdk_error("anthropic", cls_name, status, message))

    with pytest.raises(LLMUnavailableError) as e:
        chat(transport, tools=[GET_ITEM])

    assert type(e.value) is LLMUnavailableError and str(e.value) == f"{message} (error {status})"


def test_anthropic_connection_and_timeout_errors_are_unavailable():
    request = httpx.Request("POST", "http://stub/v1/messages")
    transport, _ = transport_for(
        "anthropic", anthropic.APIConnectionError(request=request), anthropic.APITimeoutError(request)
    )

    for _ in range(2):
        with pytest.raises(LLMUnavailableError):
            chat(transport, tools=[GET_ITEM])


def test_anthropic_chat_without_a_key_says_where_to_set_it():
    with pytest.raises(LLMUnavailableError, match="Settings"):
        chat(AnthropicTransport(None, "m"))


OPENAI_PLAIN_ERRORS = [
    ("AuthenticationError", 401, "Incorrect API key provided."),
    ("PermissionDeniedError", 403, "forbidden"),
    ("RateLimitError", 429, "slow down"),
    ("InternalServerError", 500, "boom"),
]


@pytest.mark.parametrize(("cls_name", "status", "message"), OPENAI_PLAIN_ERRORS)
def test_other_openai_errors_are_plain_unavailable_errors_with_the_providers_sentence(cls_name, status, message):
    transport, _ = transport_for("openai", sdk_error("openai", cls_name, status, message))

    with pytest.raises(LLMUnavailableError) as e:
        chat(transport, tools=[GET_ITEM])

    assert type(e.value) is LLMUnavailableError and str(e.value) == f"{message} (error {status})"


def test_openai_connection_error_is_unavailable():
    transport, _ = transport_for("openai", openai.APIConnectionError(request=httpx.Request("POST", "http://stub/v1")))

    with pytest.raises(LLMUnavailableError):
        chat(transport, tools=[GET_ITEM])


def test_the_stand_in_errors_are_the_sdks_real_classes_with_status_and_body():
    refusal = sdk_error("openai", "UnprocessableEntityError", 422, "Tools are off")
    overloaded = sdk_error("anthropic", "RateLimitError", 429, "slow")

    assert isinstance(refusal, openai.UnprocessableEntityError) and refusal.status_code == 422
    assert refusal.body == {"error": {"message": "Tools are off"}}
    assert isinstance(overloaded, anthropic.RateLimitError) and overloaded.status_code == 429


# --- the real SDK clients on the wire -------------------------------------------------------------------------------


def test_openai_on_the_wire_sends_nothing_extra_and_parses_a_custom_call_away():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=json.loads((FIXTURES / "openai_tool_calls.json").read_text()))

    client = openai.OpenAI(
        base_url="http://test/v1",
        api_key="k",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    transport = OpenAICompatTransport("http://test/v1", "k", "m", client=client)

    result = chat(transport, CONVERSATION, tools=[GET_ITEM])

    assert set(seen[0]) == {"model", "messages", "tools"}
    assert result == ChatResult(
        "",
        (
            ToolCallRequest("call_1", "search_wardrobe", {"q": "shirt"}),
            ToolCallRequest("call_3", "get_item", {"id": 3}),
        ),
        11,
        4,
    )


# anthropic 1.11 speaks `httpx2`, openai 3.24 still takes `httpx`
def test_anthropic_on_the_wire_sends_nothing_extra_and_reads_tool_use_blocks():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx2.Response(200, json=json.loads((FIXTURES / "anthropic_tool_use.json").read_text()))

    client = anthropic.Anthropic(
        api_key="k", max_retries=0, http_client=httpx2.Client(transport=httpx2.MockTransport(handler))
    )
    transport = AnthropicTransport("k", "claude-x", client=client)

    result = chat(transport, CONVERSATION, tools=[GET_ITEM])

    assert set(seen[0]) == {"model", "max_tokens", "system", "messages", "tools"}
    assert result == ChatResult(
        "Let me look. ",
        (
            ToolCallRequest("toolu_1", "search_wardrobe", {"q": "shirt"}),
            ToolCallRequest("toolu_2", "get_item", {"id": 3}),
        ),
        11,
        4,
    )


# --- LLMProvider.chat: one counts-only LlmCall row per call (AC-4) -------------------------------------------------


def _provider(*replies, key: str | None = None):
    transport = FakeTransport(chat_replies=list(replies))
    if key:
        transport.api_key = key
    rows: list[LlmCall] = []
    return LLMProvider(transport, log=rows.append), rows


def _chat(provider, messages=(HI,), tools=(), label="session=1 turn=1", first_call=True, system="SYS"):
    return provider.chat(
        system=system, messages=list(messages), tools=list(tools), label=label, first_call=first_call, user_id=1
    )


def _answer(text="Fine", *calls):
    return ChatResult(text, tuple(calls), 10, 5)


def test_chat_returns_what_the_transport_answered_and_sends_it_the_request_as_given():
    provider, _ = _provider(_answer("Hello"))
    tools = [GET_ITEM]

    result = _chat(provider, CONVERSATION, tools, system="THE SYSTEM")

    assert result == _answer("Hello")
    assert provider.transport.chat_calls == [{"system": "THE SYSTEM", "messages": CONVERSATION, "tools": tools}]


def test_a_successful_chat_writes_one_row_with_counts_and_names_only():
    messages = [
        UserMsg("hello"),
        AssistantMsg("ok", (ToolCallRequest("c1", "get_item", {}),)),
        ToolResultMsg("c1", "get_item", "RESULT!!", False),
    ]
    reply = _answer("Fine", ToolCallRequest("c1", "get_item", {"id": 1}), ToolCallRequest("c2", "search_wardrobe", {}))
    provider, rows = _provider(reply)

    _chat(provider, messages, [GET_ITEM, SEARCH], label="session=7 turn=2")

    (row,) = rows
    assert (row.provider, row.model, row.purpose, row.error) == ("fake", "fake-1", "chat", None)
    # chars: system 3 + "hello" 5 + "ok" 2 + the arguments "{}" 2 + the result 8
    assert (
        row.request_summary == "session=7 turn=2 messages=3 chars=20 tools=2 [get_item,search_wardrobe] no_tools=False"
    )
    assert row.response == "tool_calls=[get_item,search_wardrobe] reply_chars=4"
    assert (row.input_tokens, row.output_tokens) == (10, 5)
    assert isinstance(row.latency_ms, int) and row.latency_ms >= 0


def test_a_chat_without_tools_says_so_and_a_plain_reply_lists_no_calls():
    provider, rows = _provider(_answer("Plain answer"))

    _chat(provider, tools=[], label="session=2 turn=1")

    assert rows[0].request_summary == "session=2 turn=1 messages=1 chars=5 tools=0 [] no_tools=True"
    assert rows[0].response == "tool_calls=[] reply_chars=12"


def test_the_character_count_includes_the_arguments_json():
    def chars(arguments):
        provider, rows = _provider(_answer())
        _chat(provider, [AssistantMsg("", (ToolCallRequest("c1", "get_item", arguments),))])
        return int(re.search(r"chars=(\d+)", rows[0].request_summary).group(1))

    assert 100 <= chars({"q": "x" * 100}) - chars({}) <= 120  # the JSON text of the arguments, however it is spaced


def test_a_called_tool_name_is_cut_to_64_characters_in_the_response():
    provider, rows = _provider(_answer("", ToolCallRequest("c1", "n" * 100, {})))

    _chat(provider)

    assert rows[0].response == f"tool_calls=[{'n' * 64}] reply_chars=0"


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (LLMUnavailableError("provider is down"), "provider is down"),
        (ToolsUnsupportedError("does not support tools"), "does not support tools"),
        (KeyError(MARKER_EXCEPTION), "KeyError"),
    ],
    ids=["unavailable", "tools unsupported", "unexpected"],
)
def test_a_failed_chat_writes_one_row_with_the_error_and_no_response_and_raises_the_same_error(failure, expected):
    provider, rows = _provider(failure)

    with pytest.raises(type(failure)):
        _chat(provider, tools=[GET_ITEM])

    (row,) = rows
    assert row.purpose == "chat" and row.response is None and row.error and expected in row.error
    assert (row.input_tokens, row.output_tokens) == (None, None)
    assert MARKER_EXCEPTION not in row.error  # an unexpected error is named by its class only


def test_a_long_error_is_cut_to_300_characters():
    provider, rows = _provider(LLMUnavailableError("e" * 1000))

    with pytest.raises(LLMUnavailableError):
        _chat(provider)

    assert len(rows[0].error) == 300


def test_the_key_is_redacted_before_the_cut_so_no_part_of_it_survives_at_the_boundary():
    for padding in range(250, 300):  # one of these puts the key across character 300
        provider, rows = _provider(LLMUnavailableError("x" * padding + LEAK_KEY + "y" * 400), key=LEAK_KEY)

        with pytest.raises(LLMUnavailableError):
            _chat(provider)

        assert LEAK_KEY[:4] not in rows[0].error and len(rows[0].error) <= 300


def test_a_key_echoed_by_the_provider_is_not_stored_when_the_real_transport_raises():
    failure = sdk_error("openai", "AuthenticationError", 401, f"Incorrect API key provided: {LEAK_KEY}.")
    transport, _ = transport_for("openai", failure, key=LEAK_KEY)
    rows: list[LlmCall] = []

    with pytest.raises(LLMUnavailableError):
        _chat(LLMProvider(transport, log=rows.append))

    assert LEAK_KEY not in rows[0].error and "Incorrect API key provided: ***" in rows[0].error


@pytest.fixture
def engine(engine):
    return with_owner(engine)


def test_no_row_and_no_log_line_holds_the_chat_text_or_the_key(engine, caplog):
    transport = FakeTransport(
        chat_replies=[
            _answer(MARKER_REPLY, ToolCallRequest("c1", "get_item", {"note": MARKER_ARGS})),
            LLMUnavailableError(f"401 invalid key {LEAK_KEY}"),
        ]
    )
    transport.api_key = LEAK_KEY
    provider = LLMProvider(transport, log=db_logger(engine))
    messages = [
        UserMsg(MARKER_USER),
        AssistantMsg(MARKER_REPLY, (ToolCallRequest("c0", "get_item", {"note": MARKER_ARGS}),)),
        ToolResultMsg("c0", "get_item", MARKER_RESULT, False),
    ]
    system = f"You are a stylist. {MARKER_SYSTEM}"

    with caplog.at_level(logging.DEBUG):
        _chat(provider, messages, [GET_ITEM], system=system)
        with pytest.raises(LLMUnavailableError):
            _chat(provider, messages, [GET_ITEM], system=system)

    with Session(engine) as s:
        rows = s.exec(select(LlmCall)).all()
    assert len(rows) == 2 and {r.purpose for r in rows} == {"chat"}
    everything = "\n".join(r.model_dump_json() for r in rows) + "\n" + caplog.text
    for leaked in (MARKER_USER, MARKER_REPLY, MARKER_ARGS, MARKER_RESULT, MARKER_SYSTEM, LEAK_KEY):
        assert leaked not in everything


def test_a_failing_log_sink_does_not_fail_the_chat_or_leak_into_the_log(caplog):
    def bad_sink(_call):
        raise RuntimeError(f"INSERT parameters: {MARKER_REPLY}")

    provider = LLMProvider(FakeTransport(chat_replies=[_answer(MARKER_REPLY)]), log=bad_sink)

    with caplog.at_level(logging.DEBUG):
        result = _chat(provider)

    assert result.text == MARKER_REPLY
    assert "event=llm_log_failed" in caplog.text and MARKER_REPLY not in caplog.text


def test_the_row_describes_the_transport_that_made_the_call_even_if_it_is_swapped_meanwhile():
    new = FakeTransport()
    new.api_key, new.model = NEW_KEY, "new-model"
    rows: list[LlmCall] = []

    class Swapping(FakeTransport):
        api_key = OLD_KEY

        def call_chat(self, **kw):
            provider.set_transport(new)  # the user saves new settings while this call is in flight
            raise LLMUnavailableError(f"401 invalid key {OLD_KEY}")

    provider = LLMProvider(Swapping(), log=rows.append)

    with pytest.raises(LLMUnavailableError):
        _chat(provider)

    assert OLD_KEY not in rows[0].error and "invalid key ***" in rows[0].error
    assert (rows[0].provider, rows[0].model) == ("fake", "fake-1")
