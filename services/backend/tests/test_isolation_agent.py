"""Chats belong to the person who made them (AC-17, AC-19, AC-11): sessions, messages, tool calls and LLM calls carry
the caller's id, another account's ids answer 404 and stay unchanged, Debug lists only the caller's rows, the tools
pass the caller's id to the wardrobe, and the model is sent only the caller's profile."""

import pytest
from sqlmodel import Session, col, select

from kyapehnu.agent.chat import tools
from kyapehnu.agent.models import ChatMessage, ChatSession, LlmCall, ProposalState, ToolCall
from tests.chat_fakes import ask, fixed_now, say
from tests.chat_helpers import MESSAGES, NOW, engine_of, llm_rows, message_rows, tool_rows, turn
from tests.chat_rows import add_call, add_llm_call, add_message, add_session, log_wear_ui, proposal, status_ui
from tests.fakes import add_item

A, B = 1, 2
SESSIONS = "/api/chat/sessions"


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    fixed_now(monkeypatch, NOW)


@pytest.fixture
def engine(app, client_a, client_b):
    """Both accounts exist (A is 1, B is 2)."""
    return engine_of(client_a)


def snapshot(engine):
    with Session(engine) as s:
        return {
            m.__name__: [r.model_dump() for r in s.exec(select(m).order_by(col(m.id)))]  # type: ignore[attr-defined]
            for m in (ChatSession, ChatMessage, ToolCall, LlmCall)
        }


# --- the rows carry the caller's id ---


@pytest.mark.parametrize(("who", "uid"), [("a", A), ("b", B)])
def test_a_turn_stamps_every_row_with_the_callers_id(app, engine, fake, client_a, client_b, who, uid):
    me = {"a": client_a, "b": client_b}[who]
    chat = me.post(SESSIONS).json()["id"]
    with Session(engine) as s:
        add_item(s, name="Shirt")

    turn(me, fake, chat, ask(("search_wardrobe", {})), say("Here."))

    with Session(engine) as s:
        assert s.get(ChatSession, chat).user_id == uid
    assert {m.user_id for m in message_rows(engine, chat)} == {uid}
    assert [t.user_id for t in tool_rows(engine, chat)] == [uid]
    assert {r.user_id for r in llm_rows(engine, "chat")} == {uid}


def test_each_person_lists_only_their_own_chats(engine, client_a, client_b):
    mine, theirs = client_a.post(SESSIONS).json()["id"], client_b.post(SESSIONS).json()["id"]

    assert [s["id"] for s in client_a.get(SESSIONS).json()] == [mine]
    assert [s["id"] for s in client_b.get(SESSIONS).json()] == [theirs]


# --- another account's ids are 404 and nothing changes ----------------------------------------------------------------


def a_chat_with_a_proposal(engine):
    chat = add_session(engine, "A's chat", user_id=A)
    add_message(engine, chat, 1, 0, "user", "A's words", user_id=A)
    with Session(engine) as s:
        item = add_item(s, name="Shirt")
        item_id = item.id
    wear = add_call(engine, chat, "log_wear", **proposal(log_wear_ui([item_id]), NOW.date()))
    status = add_call(
        engine, chat, "update_item_status", **proposal(status_ui(item_id, "available", "laundry"), NOW.date())
    )
    return chat, wear, status


def test_b_gets_404_for_every_chat_route_naming_as_chat_or_proposal(engine, fake, client_b):
    chat, wear, status = a_chat_with_a_proposal(engine)
    before = snapshot(engine)

    responses = [
        client_b.get(f"{SESSIONS}/{chat}"),
        client_b.delete(f"{SESSIONS}/{chat}"),
        client_b.post(MESSAGES.format(chat), json={"text": "hi"}),
        client_b.post(f"/api/chat/proposals/{wear}/confirm"),
        client_b.post(f"/api/chat/proposals/{wear}/cancel"),
        client_b.post(f"/api/chat/proposals/{status}/confirm"),
    ]

    assert [(r.status_code, r.json()["code"]) for r in responses] == [(404, "not_found")] * len(responses)
    assert snapshot(engine) == before  # nothing deleted, stored, confirmed or cancelled
    assert fake.chat_calls == []  # the model was never asked


def test_a_confirms_their_own_proposal_after_b_was_refused(engine, client_a, client_b):
    _chat, wear, _status = a_chat_with_a_proposal(engine)
    assert client_b.post(f"/api/chat/proposals/{wear}/confirm").status_code == 404

    r = client_a.post(f"/api/chat/proposals/{wear}/confirm")

    assert (r.status_code, r.json()["state"]) == (200, ProposalState.confirmed.value)


def test_a_reads_and_deletes_their_own_chat(engine, client_a):
    chat, _wear, _status = a_chat_with_a_proposal(engine)

    assert client_a.get(f"{SESSIONS}/{chat}").status_code == 200
    assert client_a.delete(f"{SESSIONS}/{chat}").status_code == 204


def test_deleting_a_chat_removes_only_the_callers_pick_rows(engine, client_a, client_b):
    mine = add_session(engine, user_id=A)
    theirs = add_session(engine, user_id=B)
    add_llm_call(engine, "outfit_pick", f"session={mine} images=[]", user_id=A)
    add_llm_call(engine, "outfit_pick", f"session={mine} images=[]", user_id=B)  # same text, another person's row
    assert client_a.delete(f"{SESSIONS}/{mine}").status_code == 204

    assert [(r.user_id, r.purpose) for r in llm_rows(engine)] == [(B, "outfit_pick")]
    assert client_b.get(f"{SESSIONS}/{theirs}").status_code == 200


# --- Debug shows only the caller's rows -------------------------------------------------------------------------------


def test_debug_lists_only_the_admins_own_rows(engine, client_a):
    mine, theirs = add_session(engine, user_id=A), add_session(engine, user_id=B)
    add_call(engine, mine, "get_item", call_id="mine")
    add_call(engine, theirs, "get_item", call_id="theirs", user_id=B)
    add_llm_call(engine, "chat", "mine", user_id=A)
    add_llm_call(engine, "chat", "theirs", user_id=B)

    tool_calls = client_a.get("/api/debug/tool-calls").json()
    llm_calls = client_a.get("/api/debug/llm-calls").json()
    filtered = client_a.get(f"/api/debug/tool-calls?session_id={theirs}").json()

    assert [c["call_id"] for c in tool_calls] == ["mine"]
    assert [c["request_summary"] for c in llm_calls] == ["mine"]
    assert {c["user_id"] for c in tool_calls + llm_calls} == {A}
    assert filtered == []


# --- the tools and the model see the caller only ----------------------------------------------------------------------


def test_every_wardrobe_function_a_tool_calls_gets_the_callers_id(engine, fake, client_b, monkeypatch):
    seen: list[tuple[str, object]] = []

    def spy(name, fn):
        def wrapper(user_id, *args, **kwargs):
            seen.append((name, user_id))
            return fn(user_id, *args, **kwargs)

        return wrapper

    ported = [
        n
        for n in (
            "list_items",
            "get_item",
            "item_history",
            "weather_report",
            "list_wears",
            "require_owned_items",
            "give_feedback",
            "create_event",
            "get_event",
            "suggest",
            "find_events",
        )
        if hasattr(tools, n)
    ]
    for name in ported:
        monkeypatch.setattr(tools, name, spy(name, getattr(tools, name)))
    chat = client_b.post(SESSIONS).json()["id"]
    with Session(engine) as s:
        shirt = add_item(s, name="Shirt").id

    turn(
        client_b,
        fake,
        chat,
        ask(
            ("search_wardrobe", {}),
            ("get_item", {"item_id": shirt}),
            ("get_wear_history", {"item_id": shirt, "days": 30}),
            ("log_wear", {"item_ids": [shirt]}),
            ("create_event", {"type": "party", "starts_at": "2026-10-10T20:00", "place": "Khan Market"}),
        ),
        say("done"),
    )

    assert {n for n, _ in seen} >= {"list_items", "get_item", "list_wears", "require_owned_items", "create_event"}
    assert {uid for _, uid in seen} == {B}


def test_the_model_is_sent_only_the_callers_profile(engine, fake, client_a, client_b):
    client_a.put("/api/profile", json={"name": "Asha", "gender": "female", "style_prefs": "A-STYLE-MARKER"})
    client_b.put("/api/profile", json={"name": "Bela", "gender": "male", "style_prefs": "B-STYLE-MARKER"})
    chat = client_b.post(SESSIONS).json()["id"]

    turn(client_b, fake, chat, say("Hello."))

    system = fake.chat_calls[0]["system"]
    assert "B-STYLE-MARKER" in system and "A-STYLE-MARKER" not in system
