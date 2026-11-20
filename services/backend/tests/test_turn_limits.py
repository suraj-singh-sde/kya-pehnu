"""R10: how long a chat turn can last, and the lock that outlives it."""

import json

import httpx
from pydantic import BaseModel
from sqlmodel import Session

from kyapehnu.agent.chat.limits import PICK_CALLS, TURN_BUDGET_S, TURN_LOCK_TTL_S, TURN_MAX_S
from kyapehnu.agent.llm import anthropic, openai_compat
from kyapehnu.agent.llm.anthropic import AnthropicTransport
from kyapehnu.agent.llm.base import LLMProvider
from kyapehnu.agent.llm.openai_compat import OpenAICompatTransport
from kyapehnu.core.app import create_app
from tests.chat_fakes import fixed_now, seed_items
from tests.chat_helpers import MESSAGES, NOW, PICK, engine_of
from tests.conftest import sign_in
from tests.fakes import OWNER_ID, FakeTransport
from tests.sdk_fakes import (
    OLLAMA_URL,
    FakeAnthropicClient,
    FakeOpenAIClient,
    anthropic_reply,
    function_call,
    openai_reply,
    text_block,
    tool_use_block,
)
from tests.wear_fixtures import add_event


def test_the_longest_turn_is_the_budget_plus_the_slowest_pick_calls():
    slowest_call = max(openai_compat.REQUEST_TIMEOUT_SECONDS, anthropic.REQUEST_TIMEOUT_SECONDS)

    assert TURN_MAX_S == TURN_BUDGET_S + (PICK_CALLS + 1) * slowest_call == 1500  # + the judge's parallel round


def test_the_lock_outlives_the_longest_turn_by_a_minute():
    assert TURN_LOCK_TTL_S == TURN_MAX_S + 60 == 1560


# --- R10: no SDK retries inside a turn, on either transport -----------------------------------------------------------


def run_generate_turn(settings, fake_weather, kv, monkeypatch, transport, client, chat_reply, pick_reply):

    fixed_now(monkeypatch, NOW)
    app = create_app(settings=settings, transport=transport, weather_transport=httpx.MockTransport(fake_weather), kv=kv)
    _uid, web = sign_in(app, "sub-a")
    engine = engine_of(web)
    seed_items(engine, settings.data_dir)
    with Session(engine) as s:
        event_id = add_event(s).id
    chat = web.post("/api/chat/sessions").json()["id"]
    client.script.extend(chat_reply(event_id, pick_reply))
    r = web.post(MESSAGES.format(chat), json={"text": "outfits please"})
    assert r.status_code == 200 and r.json()["outcome"] == "ok", r.text
    return client


def test_every_chat_and_pick_call_of_a_turn_has_sdk_retries_off_on_openai(settings, fake_weather, kv, monkeypatch):

    client = FakeOpenAIClient()

    def script(event_id, pick):
        return [
            openai_reply(None, function_call("c1", "generate_outfits", json.dumps({"event_id": event_id}))),
            openai_reply(json.dumps(pick)),
            openai_reply("Here you go."),
        ]

    transport = OpenAICompatTransport(OLLAMA_URL, "ollama", "m", client=client)
    run_generate_turn(settings, fake_weather, kv, monkeypatch, transport, client, script, PICK)

    assert len(client.calls) == 2  # chat, the outfit pick; the turn ends on the looks
    assert client.retries == [0, 0]


def test_every_chat_and_pick_call_of_a_turn_has_sdk_retries_off_on_anthropic(settings, fake_weather, kv, monkeypatch):

    client = FakeAnthropicClient()

    def script(event_id, pick):
        return [
            anthropic_reply(tool_use_block("c1", "generate_outfits", {"event_id": event_id})),
            anthropic_reply(tool_use_block("p1", "OutfitPicks", pick)),
            anthropic_reply(text_block("Here you go.")),
        ]

    transport = AnthropicTransport("key", "m", client=client)
    run_generate_turn(settings, fake_weather, kv, monkeypatch, transport, client, script, PICK)

    assert len(client.calls) == 2
    assert client.retries == [0, 0]


def test_outside_a_turn_the_sdk_keeps_its_own_retries():

    fake = FakeTransport(replies=[{"ok": True}, {"ok": True}])
    provider = LLMProvider(fake)

    class Ping(BaseModel):
        ok: bool

    provider.complete_json(system="s", prompt="p", images=[], schema=Ping, purpose="t", user_id=OWNER_ID)
    provider.with_max_retries(0).complete_json(
        system="s", prompt="p", images=[], schema=Ping, purpose="t", user_id=OWNER_ID
    )

    assert [c["max_retries"] for c in fake.calls] == [None, 0]
