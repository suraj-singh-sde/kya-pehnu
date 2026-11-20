"""QA of Phase 3, across packages: one stylist journey over the real OpenAI transport (stand-in SDK client), the
privacy scan of that journey, and a backup of a chat. No package owns these (work plan, QA hand-offs).
"""

import json
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from kyapehnu.agent.llm.base import RawResult
from kyapehnu.agent.models import ChatMessage, ChatSession, LlmCall, ToolCall
from kyapehnu.core.app import create_app
from kyapehnu.core.backup import backup
from kyapehnu.core.config import Settings
from kyapehnu.wardrobe.models import Event, Feedback, Item, WearLog
from kyapehnu.wardrobe.outfits import preference
from tests.chat_fakes import ask, fixed_now, say, seed_items
from tests.chat_helpers import (
    DINNER,
    MESSAGES,
    NOW,
    PICK,
    llm_rows,
    new_chat,
    party_wardrobe,
    pin_rest_today,
    rows_holding,
    tool_rows,
)
from tests.conftest import LOOPBACK_PEER
from tests.fake_openmeteo import FakeOpenMeteo
from tests.fakes import OWNER_ID, add_item
from tests.sdk_fakes import FakeOpenAIClient, function_call, openai_app, openai_reply

USER_MARKER, REPLY_MARKER = "MARKER-USER-7Q3X", "MARKER-REPLY-9K2W"


def calls(*pairs: tuple[str, dict[str, Any]]) -> Any:
    return openai_reply(
        None, *(function_call(f"call-{n}", name, json.dumps(args)) for n, (name, args) in enumerate(pairs))
    )


def pick() -> Any:
    return openai_reply(json.dumps(PICK))


@pytest.fixture
def journey(settings, fake_weather, monkeypatch):
    """A real app on the OpenAI-compatible transport (Ollama's URL) over a stand-in SDK client, 32 seed items."""
    fixed_now(monkeypatch, NOW)
    pin_rest_today(monkeypatch)
    sdk = FakeOpenAIClient()
    app = openai_app(settings, fake_weather, sdk)
    with TestClient(app, base_url="http://localhost", client=LOOPBACK_PEER) as web:
        seed_items(app.state.ctx.engine, app.state.ctx.data_dir)
        yield web, sdk, app.state.ctx.engine


def talk(sdk: FakeOpenAIClient, web: TestClient, chat: int, text: str, *script: Any) -> dict[str, Any]:
    sdk.script.extend(script)
    r = web.post(f"/api/chat/sessions/{chat}/messages", json={"text": text})
    assert r.status_code == 200, r.text
    assert sdk.script == [], "the model was scripted for calls the turn never made"
    return dict(r.json())


def test_the_whole_stylist_journey_from_a_message_to_a_preference_and_a_delete(journey):
    web, sdk, engine = journey
    chat = web.post("/api/chat/sessions").json()["id"]

    # --- a message: search, create an event, generate outfits (with the AI pick inside), then the reply ------------
    out = talk(
        sdk,
        web,
        chat,
        f"{USER_MARKER} dinner with Priya on Saturday at 8 in Khan Market, smart casual",
        calls(("search_wardrobe", {"category": "top", "status": "available"}), ("create_event", DINNER)),
        calls(("generate_outfits", {"event_id": 1})),
        pick(),  # the turn ends on the looks: no reply is asked for
    )
    assert out["outcome"] == "ok"
    search, created, generated = out["session"]["tool_calls"]
    assert [c["name"] for c in (search, created, generated)] == ["search_wardrobe", "create_event", "generate_outfits"]
    assert created["event"]["audience_tags"] == ["Priya"] and created["event"]["dress_code"] == "smart casual"
    assert datetime.fromisoformat(created["event"]["starts_at"]) == datetime(2026, 10, 10, 20)
    [event] = web.get("/api/events").json()  # the form's list: the chat made the same kind of row
    assert event["id"] == created["event"]["id"] and event["place"] == "Khan Market"
    cards = generated["card"]["suggestions"]
    assert len(cards) >= 1 and generated["card"]["weather"] is not None
    assert all(c["wearable"] and None not in c["items"] and c["rating"] is None for c in cards)
    assert [m["role"] for m in out["session"]["messages"]] == ["user"]

    # one request per model call; the real transport sent tools, never tool_choice nor streaming (an Ollama URL)
    chat_requests = [c for c in sdk.calls if "tools" in c]
    assert len(chat_requests) == 2 and len(sdk.calls) == 3  # two chat calls and one nested pick
    assert all({"tool_choice", "parallel_tool_calls", "stream"}.isdisjoint(c) for c in sdk.calls)
    # the model's second request holds the first request's tool results: the loop fed them back
    second = json.dumps(chat_requests[1]["messages"])
    assert '"role": "tool"' in second and "Khan Market" in second

    # --- the first card is rated down through REST: a rating is not a wear -----------------------------------------
    first = cards[0]
    sdk.script.append(pick())
    baseline = web.post("/api/outfits/suggest", json={"event_id": event["id"], "exclude": []})
    assert baseline.status_code == 200
    assert all(s["parts"]["preference"] == 0.5 for s in baseline.json()["suggestions"])  # nothing rated yet
    r = web.post("/api/feedback", json={"item_ids": first["item_ids"], "rating": -1, "reason": "too shiny"})
    assert r.status_code == 201
    with Session(engine) as s:
        assert s.exec(select(WearLog)).all() == []
    reopened = web.get(f"/api/chat/sessions/{chat}").json()["tool_calls"][2]["card"]["suggestions"]
    assert reopened[0]["rating"] == -1 and [c["rating"] for c in reopened[1:]] == [None] * (len(cards) - 1)

    # --- the preference now acts on the ranking ---------------------------------------------------------------------
    sdk.script.append(pick())
    after = web.post("/api/outfits/suggest", json={"event_id": event["id"], "exclude": []}).json()["suggestions"]
    assert all(s["parts"]["preference"] <= 0.5 for s in after)  # only a thumb down exists: nothing can lean liked
    assert any(s["parts"]["preference"] < 0.5 for s in after)  # and the seed wardrobe shares tags with it
    with Session(engine) as s:
        by_id = {i.id: i for i in s.exec(select(Item)).all()}
        learned = preference.load(OWNER_ID, s, by_id)
    assert learned.part([by_id[i] for i in first["item_ids"]]) < 0.5

    # --- a wear is proposed by the model and written only by Confirm, once -----------------------------------------
    talk(
        sdk,
        web,
        chat,
        "I'll take the second",
        calls(("log_wear", {"item_ids": cards[-1]["item_ids"], "event_id": event["id"]})),
        openai_reply("Press Confirm to log it."),
    )
    with Session(engine) as s:
        assert s.exec(select(WearLog)).all() == []
    [proposal] = [c["proposal"] for c in web.get(f"/api/chat/sessions/{chat}").json()["tool_calls"] if c["proposal"]]
    assert proposal["state"] == "pending" and proposal["actionable"]
    confirm = f"/api/chat/proposals/{proposal['tool_call_id']}/confirm"
    assert [web.post(confirm).json()["state"] for _ in range(2)] == ["confirmed", "confirmed"]
    with Session(engine) as s:
        [wear] = s.exec(select(WearLog)).all()
    assert sorted(wear.item_ids) == sorted(cards[-1]["item_ids"])
    assert wear.event_id == event["id"] and wear.audience_tags == ["Priya"] and wear.worn_on == NOW.date()
    wears = web.get("/api/wears", params={"item_id": cards[-1]["item_ids"][0]}).json()
    assert [w["id"] for w in wears] == [wear.id]

    # --- the Debug panel lists the session's tool calls and its chat and pick calls ---------------------------------
    tool_calls = web.get("/api/debug/tool-calls", params={"session_id": chat}).json()
    assert {c["name"] for c in tool_calls} == {"search_wardrobe", "create_event", "generate_outfits", "log_wear"}
    llm = web.get("/api/debug/llm-calls", params={"session_id": chat}).json()
    assert {c["purpose"] for c in llm} == {"chat", "outfit_pick"}

    # --- delete: the chat's text goes, the shared debug rows and the ratings stay; no marker is left anywhere ------
    assert rows_holding(engine, USER_MARKER, REPLY_MARKER) != []  # it was stored, so the scan below proves something
    assert web.delete(f"/api/chat/sessions/{chat}").status_code == 204
    assert rows_holding(engine, USER_MARKER, REPLY_MARKER) == []
    with Session(engine) as s:
        assert s.exec(select(ChatSession)).all() == [] and s.exec(select(ChatMessage)).all() == []
        assert s.exec(select(ToolCall)).all() == []
        assert len(s.exec(select(LlmCall)).all()) >= 4  # D9: the debug log outlives the chat
        assert len(s.exec(select(Feedback)).all()) == 1 and len(s.exec(select(WearLog)).all()) == 1
        assert len(s.exec(select(Event)).all()) == 1  # the event the chat made is the user's now
    assert web.get("/api/chat/sessions").json() == []


def test_a_chat_goes_into_a_backup_and_a_restored_copy_opens_it(settings, fake_weather, monkeypatch, tmp_path):
    fixed_now(monkeypatch, NOW)
    sdk = FakeOpenAIClient()
    app = openai_app(settings, fake_weather, sdk)
    with TestClient(app, base_url="http://localhost", client=LOOPBACK_PEER) as web:
        seed_items(app.state.ctx.engine, app.state.ctx.data_dir)
        chat = web.post("/api/chat/sessions").json()["id"]
        talk(sdk, web, chat, "what is the weather like?", calls(("get_weather", {})), openai_reply("Mild."))
        archive = backup(app.state.ctx.data_dir, app.state.ctx.files)

    restored = tmp_path / "restored"
    with zipfile.ZipFile(archive) as z:
        z.extractall(restored)
    copy = Settings(_env_file=None, data_dir=restored, config_file=tmp_path / "missing.yaml", llm_provider="anthropic")
    again = create_app(settings=copy, weather_transport=httpx.MockTransport(FakeOpenMeteo()))
    with TestClient(again, base_url="http://localhost", client=LOOPBACK_PEER) as web:
        [listed] = web.get("/api/chat/sessions").json()
        opened = web.get(f"/api/chat/sessions/{listed['id']}").json()
        assert [m["content"] for m in opened["messages"]] == ["what is the weather like?", "Mild."]
        assert [c["name"] for c in opened["tool_calls"]] == ["get_weather"]
        new = web.post("/api/chat/sessions").json()["id"]
        assert new > listed["id"]  # ids go on counting in the restored copy


def test_a_turn_waiting_in_the_nested_outfit_pick_holds_no_write_lock(client, fake, monkeypatch):
    """AC-39 for the other provider wait of a turn: the AI pick inside `generate_outfits` (the chat call is covered
    in test_chat_concurrency). A wear and a status change posted meanwhile succeed, and every row is written."""
    fixed_now(monkeypatch, NOW)
    pin_rest_today(monkeypatch)
    engine = client.app.state.ctx.engine
    with Session(engine, expire_on_commit=False) as s:
        made = party_wardrobe(s)
        scarf = add_item(s, name="Scarf", category="accessory")
    chat = new_chat(client)
    entered, release = threading.Event(), threading.Event()

    def slow_pick(**_kwargs: Any) -> RawResult:
        entered.set()
        assert release.wait(20)
        return RawResult(json.dumps(PICK), 10, 5)

    fake.call_json = slow_pick
    fake.chat_replies.extend(
        [ask(("create_event", DINNER)), ask(("generate_outfits", {"event_id": 1})), say("Here you go.")]
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        waiting = pool.submit(client.post, MESSAGES.format(chat), json={"text": "dinner"})
        try:
            deadline = time.monotonic() + 20
            while not entered.wait(0.05):
                assert not waiting.done() and time.monotonic() < deadline, "the turn never reached the pick"
            started = time.monotonic()
            wear = client.post("/api/outfits/wear", json={"item_ids": [made["top"][0].id, made["bottom"][0].id]})
            status = client.post(f"/api/items/{scarf.id}/status", json={"status": "laundry"})
            quick = time.monotonic() - started
        finally:
            release.set()
        finished = waiting.result(timeout=30)

    assert (wear.status_code, status.status_code) == (201, 200) and quick < 5
    assert finished.status_code == 200 and finished.json()["outcome"] == "ok"
    assert [r.name for r in tool_rows(engine, chat)] == ["create_event", "generate_outfits"]
    purposes = [r.purpose for r in llm_rows(engine)]
    assert [p for p in purposes if p != "outfit_judge"] == ["chat", "chat", "outfit_pick"]  # the turn ends on looks
    assert [r.error for r in llm_rows(engine)] == [None] * len(purposes)
