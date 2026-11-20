"""The no-tools detector (LLD 3.4; AC-5, AC-44, AC-45, AC-51).

The retry itself (a second request without tools, the notice, the stored messages) is the turn loop's job (WP-5). Here:
which refusals are recognised, what is remembered and for which (base URL, model), what a request without tools carries.
"""

import hashlib
import json
import logging
from datetime import date, datetime, time, timedelta

import httpx
import openai
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from kyapehnu.agent.chat.history import window
from kyapehnu.agent.llm.anthropic import AnthropicTransport
from kyapehnu.agent.llm.base import (
    AssistantMsg,
    LLMProvider,
    LLMUnavailableError,
    ToolCallRequest,
    ToolResultMsg,
    ToolSpec,
    ToolsUnsupportedError,
    UserMsg,
)
from kyapehnu.agent.llm.openai_compat import OpenAICompatTransport
from kyapehnu.agent.models import LlmCall
from tests.fakes import FakeTransport, add_item
from tests.sdk_fakes import (
    LM_STUDIO_URL,
    OLLAMA_URL,
    FakeAnthropicClient,
    FakeOpenAIClient,
    anthropic_reply,
    openai_app,
    openai_reply,
    sdk_error,
    text_block,
)

SPEC = ToolSpec("get_item", "One item", {"type": "object", "properties": {"id": {"type": "integer"}}})
HI = UserMsg("hi")
LOOPBACK_PEER = ("127.0.0.1", 50000)
ERROR_CLASS = {400: "BadRequestError", 404: "NotFoundError", 422: "UnprocessableEntityError"}
REFUSALS = [
    (400, "registry.ollama.ai/library/gemma does not support tools"),
    (404, "No endpoints found that support tool use"),
    (422, "Tools are not available for this model"),
]
REFUSAL_IDS = ["400 ollama", "404 openrouter", "422 Tools"]


def refusal(status: int, message: str) -> Exception:
    return sdk_error("openai", ERROR_CLASS[status], status, message)


class Shared:
    """The Valkey every provider of the running test shares, as the app's providers share the app's."""

    kv = None


@pytest.fixture(autouse=True)
def shared_valkey(kv):
    Shared.kv = kv
    return kv


def ollama(*script, url: str = OLLAMA_URL, model: str = "m", log=None, kv=...):
    """(provider, stand-in client) on an OpenAI-compatible transport."""
    client = FakeOpenAIClient(*script)
    transport = OpenAICompatTransport(url, "k", model, client=client)
    sink = log.append if log is not None else lambda _call: None
    return LLMProvider(transport, log=sink, kv=Shared.kv if kv is ... else kv), client


def ask(provider, *, tools=(SPEC,), first_call=True, messages=(HI,)):
    return provider.chat(
        system="SYS",
        messages=list(messages),
        tools=list(tools),
        label="session=1 turn=1",
        first_call=first_call,
        user_id=1,
    )


# --- AC-5: which refusals are recognised and what is remembered ----------------------------------------------------


@pytest.mark.parametrize(("status", "message"), REFUSALS, ids=REFUSAL_IDS)
def test_tool_refusal_is_a_tools_unsupported_error_logged_and_remembered(status, message, caplog):
    rows: list[LlmCall] = []
    provider, client = ollama(refusal(status, message), log=rows)

    with caplog.at_level(logging.INFO), pytest.raises(ToolsUnsupportedError) as e:
        ask(provider)

    assert message in str(e.value)
    assert provider.tools_disabled() is True
    assert "event=chat_no_tools provider=openai_compatible" in caplog.text
    assert "tools" in client.calls[0]  # the refused request was the one that carried tools
    (row,) = rows
    assert row.purpose == "chat" and message in row.error and row.response is None


@pytest.mark.parametrize(("status", "message"), REFUSALS, ids=REFUSAL_IDS)
def test_the_retry_without_tools_is_answered_and_remembers_nothing_more(status, message):
    provider, client = ollama(refusal(status, message), openai_reply("Here is some general advice."))
    with pytest.raises(ToolsUnsupportedError):
        ask(provider)

    result = ask(provider, tools=())

    assert result.text == "Here is some general advice."
    assert "tools" not in client.calls[1] and provider.tools_disabled() is True


def test_a_refusal_is_recognised_in_the_error_text_when_the_body_has_none():
    failure = sdk_error("openai", "BadRequestError", 400, "this model does not support tools", body=None)
    provider, _ = ollama(failure)

    with pytest.raises(ToolsUnsupportedError):
        ask(provider)

    assert provider.tools_disabled() is True


def test_a_real_sdk_error_parsed_from_the_wire_is_recognised():
    def handler(request):
        return httpx.Response(
            400, json={"error": {"message": "registry.ollama.ai/library/gemma does not support tools"}}
        )

    client = openai.OpenAI(
        base_url="http://test/v1",
        api_key="k",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    provider = LLMProvider(OpenAICompatTransport("http://test/v1", "k", "gemma", client=client), kv=Shared.kv)

    with pytest.raises(ToolsUnsupportedError, match="does not support tools"):
        ask(provider)

    assert provider.tools_disabled() is True


def test_after_a_refusal_the_outfits_page_still_suggests(settings, fake_weather):
    client = FakeOpenAIClient(refusal(400, "does not support tools"))
    app = openai_app(settings, fake_weather, client)
    pick = {"picks": [{"candidate_number": 1, "reason": "Calm.", "swap_slot": None, "swap_item_id": None}]}
    with TestClient(app, base_url="http://localhost", client=LOOPBACK_PEER) as web:
        with Session(app.state.ctx.engine) as s:
            for category in ("top", "bottom", "footwear"):
                add_item(
                    s, name=category, category=category, formality=3, warmth=2, thumb_path=f"images/{category}.webp"
                )
        with pytest.raises(ToolsUnsupportedError):
            ask(app.state.ctx.provider)
        client.script.append(openai_reply(json.dumps(pick)))
        starts = datetime.combine(date.today() + timedelta(days=1), time(19, 0)).isoformat()

        r = web.post(
            "/api/outfits/suggest", json={"event": {"type": "party", "starts_at": starts, "place": "Gurugram"}}
        )

    assert r.status_code == 200 and len(r.json()["suggestions"]) >= 1
    assert app.state.ctx.provider.tools_disabled() is True


# --- AC-44: the memory is per (base URL, model) -------------------------------------------------------------------


def test_remembered_pair_is_disabled_but_another_model_or_base_url_is_not():
    provider, _ = ollama(refusal(400, "does not support tools"))
    with pytest.raises(ToolsUnsupportedError):
        ask(provider)
    assert provider.tools_disabled() is True

    provider.set_transport(OpenAICompatTransport(OLLAMA_URL, "k", "other-model", client=FakeOpenAIClient()))
    assert provider.tools_disabled() is False  # model N

    provider.set_transport(OpenAICompatTransport(LM_STUDIO_URL, "k", "m", client=FakeOpenAIClient()))
    assert provider.tools_disabled() is False  # base URL B

    provider.set_transport(OpenAICompatTransport(OLLAMA_URL, "k", "m", client=FakeOpenAIClient()))
    assert provider.tools_disabled() is True  # back to (A, M)


def test_nothing_is_disabled_before_any_refusal():
    provider, _ = ollama()

    assert provider.tools_disabled() is False


# --- AC-45: anything else is a normal failure, a text reply is a normal answer ------------------------------------


@pytest.mark.parametrize("message", ["Request contains an invalid argument", "The model is overloaded, try later"])
def test_a_400_without_tool_is_a_plain_failure_not_remembered(message):
    rows: list[LlmCall] = []
    provider, client = ollama(refusal(400, message), log=rows)

    with pytest.raises(LLMUnavailableError) as e:
        ask(provider)

    assert type(e.value) is LLMUnavailableError and message in str(e.value)
    assert provider.tools_disabled() is False and len(client.calls) == 1  # no retry here either
    assert len(rows) == 1 and message in rows[0].error


@pytest.mark.parametrize(
    ("cls_name", "status"),
    [
        ("AuthenticationError", 401),
        ("PermissionDeniedError", 403),
        ("RateLimitError", 429),
        ("InternalServerError", 500),
    ],
)
def test_only_400_404_and_422_can_mean_no_tools(cls_name, status):
    provider, _ = ollama(sdk_error("openai", cls_name, status, "tools are rate limited"))

    with pytest.raises(LLMUnavailableError) as e:
        ask(provider)

    assert type(e.value) is LLMUnavailableError and provider.tools_disabled() is False


def test_an_error_mentioning_tools_is_plain_when_the_request_carried_none():
    provider, _ = ollama(refusal(400, "does not support tools"))

    with pytest.raises(LLMUnavailableError) as e:
        ask(provider, tools=())

    assert type(e.value) is LLMUnavailableError and provider.tools_disabled() is False


def test_plain_text_reply_with_tools_offered_is_a_normal_answer():
    rows: list[LlmCall] = []
    provider, _ = ollama(openai_reply("I can't call tools, but a navy blazer works."), log=rows)

    result = ask(provider, first_call=True)

    assert result.text == "I can't call tools, but a navy blazer works." and result.tool_calls == ()
    assert provider.tools_disabled() is False and rows[0].error is None


# --- AC-51: Anthropic never, later requests never, a restart forgets, the retry carries no tool parts -------------


@pytest.mark.parametrize(
    ("cls_name", "status", "message"),
    [
        ("BadRequestError", 400, "messages.2.content.0: tool_use ids were found without tool_result blocks"),
        ("NotFoundError", 404, "tool_result block not found"),
        ("UnprocessableEntityError", 422, "tools are invalid"),
    ],
)
def test_anthropic_tool_use_400_never_detects_and_the_next_request_still_sends_tools(cls_name, status, message):
    client = FakeAnthropicClient(sdk_error("anthropic", cls_name, status, message), anthropic_reply(text_block("ok")))
    provider = LLMProvider(AnthropicTransport("k", "claude-x", client=client))

    with pytest.raises(LLMUnavailableError) as e:
        ask(provider)
    result = ask(provider)

    assert type(e.value) is LLMUnavailableError and message in str(e.value)
    assert provider.tools_disabled() is False and result.text == "ok"
    assert "tools" in client.calls[1]


def test_a_refusal_after_the_first_request_of_a_turn_is_raised_but_not_remembered():
    provider, client = ollama(refusal(400, "invalid tool schema"), openai_reply("fine"))

    with pytest.raises(ToolsUnsupportedError):  # same error class: the turn loop decides what to do with it
        ask(provider, first_call=False)
    result = ask(provider, first_call=True)

    assert provider.tools_disabled() is False
    assert "tools" in client.calls[1] and result.text == "fine"  # the next turn's first request carries tools


def test_a_refusal_of_a_request_without_tools_is_not_remembered_even_as_the_first_request():
    transport = FakeTransport(chat_replies=[ToolsUnsupportedError("tools off")])
    transport.provider, transport.base_url = "openai_compatible", OLLAMA_URL
    provider = LLMProvider(transport)

    with pytest.raises(ToolsUnsupportedError):
        ask(provider, tools=())

    assert provider.tools_disabled() is False


def test_a_transport_with_no_base_url_still_gets_its_refusal_re_raised_and_nothing_remembered():
    provider = LLMProvider(FakeTransport(chat_replies=[ToolsUnsupportedError("tools off")]))  # provider "fake"

    with pytest.raises(ToolsUnsupportedError):
        ask(provider)

    assert provider.tools_disabled() is False  # the detector belongs to the openai_compatible transport only


def test_what_one_provider_remembers_every_provider_on_the_same_valkey_knows_even_after_a_restart(
    settings, fake_weather, kv
):
    first, _ = ollama(refusal(400, "does not support tools"))
    with pytest.raises(ToolsUnsupportedError):
        ask(first)

    other_process, _ = ollama()
    other_model, _ = ollama(model="n")
    restarted = openai_app(settings, fake_weather, FakeOpenAIClient(), kv=kv)

    assert other_process.tools_disabled() is True
    assert other_model.tools_disabled() is False
    assert restarted.state.ctx.provider.tools_disabled() is True


def test_a_flushed_valkey_forgets_the_refusal():
    provider, _ = ollama(refusal(400, "does not support tools"))
    with pytest.raises(ToolsUnsupportedError):
        ask(provider)

    Shared.kv.flushall()

    assert provider.tools_disabled() is False


def test_the_refusal_is_one_hashed_key_that_expires_in_a_day(kv):
    provider, _ = ollama(refusal(400, "does not support tools"), url=OLLAMA_URL, model="m")
    with pytest.raises(ToolsUnsupportedError):
        ask(provider)

    (key,) = kv.keys("*")

    assert key == "notools:" + hashlib.sha256(f"{OLLAMA_URL}\nm".encode()).hexdigest()
    assert 86_000 < kv.ttl(key) <= 86_400
    assert OLLAMA_URL not in key


def test_without_a_valkey_nothing_is_remembered_and_nothing_fails():
    provider, _ = ollama(refusal(400, "does not support tools"), kv=None)

    with pytest.raises(ToolsUnsupportedError):
        ask(provider)

    assert provider.tools_disabled() is False


def test_valkey_down_reads_as_not_remembered(valkey_fault):
    provider, _ = ollama(refusal(400, "does not support tools"))
    with pytest.raises(ToolsUnsupportedError):
        ask(provider)
    valkey_fault()

    assert provider.tools_disabled() is False


def test_valkey_down_at_the_write_still_raises_the_providers_refusal_and_logs_the_row(valkey_fault):
    rows: list[LlmCall] = []
    provider, _ = ollama(refusal(400, "does not support tools"), log=rows)
    valkey_fault("SET")

    with pytest.raises(ToolsUnsupportedError):
        ask(provider)

    assert len(rows) == 1 and rows[0].error


def test_the_retry_has_no_tool_definitions_or_tool_call_or_tool_result_parts():
    earlier = [
        UserMsg("What goes with navy?"),
        AssistantMsg("", (ToolCallRequest("c1", "search_wardrobe", {"q": "navy"}),)),
        ToolResultMsg("c1", "search_wardrobe", '{"items": []}', False),
        AssistantMsg("Nothing yet."),
    ]
    current = [UserMsg("And for dinner?")]
    provider, client = ollama(openai_reply("General advice."))

    ask(provider, tools=(), messages=window([earlier], current, tools_on=False))

    (kw,) = client.calls
    assert set(kw) == {"model", "messages"}
    assert [m["role"] for m in kw["messages"]] == ["system", "user", "assistant", "assistant", "user"]
    assert all("tool_calls" not in m and "tool_call_id" not in m for m in kw["messages"])
    assert "search_wardrobe" in kw["messages"][2]["content"]  # told as text what was used, never as a call
