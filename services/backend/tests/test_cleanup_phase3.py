"""End-of-engineering cleanup of Phase 3: the behaviour each fix of the review lists pins."""

import pytest
from sqlalchemy import event
from sqlmodel import Session

from kyapehnu.agent.chat.history import RESULT_OMITTED, load_turns, window
from kyapehnu.agent.chat.limits import MAX_HISTORY_CHARS, MAX_HISTORY_MESSAGES
from kyapehnu.agent.chat.store import session_view
from kyapehnu.agent.llm.anthropic import _messages
from kyapehnu.agent.llm.base import AssistantMsg, ToolCallRequest, ToolResultMsg, UserMsg
from kyapehnu.agent.llm.openai_compat import _object
from kyapehnu.agent.models import ChatMessage, ChatRole, ChatSession, ProposalState, ToolCall
from kyapehnu.wardrobe.models import Event
from tests.chat_fakes import ask, fixed_now, say
from tests.chat_helpers import NOW, add_party, engine_of, new_chat, pin_rest_today, result, turn
from tests.chat_rows import add_call, add_session, log_wear_ui, proposal
from tests.fakes import add_item


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    fixed_now(monkeypatch, NOW)
    pin_rest_today(monkeypatch)


@pytest.fixture
def engine(client):
    return engine_of(client)


# --- history ---------------------------------------------------------------------------------------------------------


def test_cleanup_load_turns_reads_only_the_turns_that_can_fit_the_window(engine):
    turns = 3 * MAX_HISTORY_MESSAGES
    with Session(engine) as s:
        chat = ChatSession(user_id=1)
        s.add(chat)
        s.commit()
        for t in range(1, turns + 1):
            s.add(ChatMessage(user_id=1, session_id=chat.id, turn=t, step=0, role=ChatRole.user, content=f"q{t}"))
            s.add(ChatMessage(user_id=1, session_id=chat.id, turn=t, step=1, role=ChatRole.assistant, content=f"a{t}"))
            s.add(
                ToolCall(
                    user_id=1,
                    session_id=chat.id,
                    turn=t,
                    step=1,
                    call_id="c",
                    name="get_item",
                    result="r",
                    latency_ms=1,
                )
            )
        s.commit()

        loaded = load_turns(s, chat.id, before_turn=turns + 1)

    assert [t[0] for t in loaded] == [UserMsg(f"q{n}") for n in range(turns + 1 - MAX_HISTORY_MESSAGES, turns + 1)]
    kept = window(loaded, [UserMsg("now")], tools_on=True)
    assert len(kept) <= MAX_HISTORY_MESSAGES and kept[-1] == UserMsg("now")


def test_cleanup_a_short_result_is_not_replaced_by_the_longer_marker():
    short = ToolResultMsg("c1", "get_item", "{}", False)
    big = ToolResultMsg("c2", "search_wardrobe", "x" * (MAX_HISTORY_CHARS + 10), False)
    current = [UserMsg("hi"), AssistantMsg("", (ToolCallRequest("c1", "get_item", {}), ToolCallRequest("c2", "s", {}))),
               short, big]  # fmt: skip

    kept = window([], current, tools_on=True)

    assert [m.content for m in kept if isinstance(m, ToolResultMsg)] == ["{}", RESULT_OMITTED]


# --- providers -------------------------------------------------------------------------------------------------------


def test_cleanup_anthropic_leaves_out_a_whitespace_only_assistant_text():
    call = ToolCallRequest("toolu_1", "get_item", {"item_id": 1})

    sent = _messages([UserMsg("hi"), AssistantMsg("\n\n", (call,)), ToolResultMsg("toolu_1", "get_item", "{}", False)])

    assert [b["type"] for b in sent[1]["content"]] == ["tool_use"]


def test_cleanup_openai_arguments_nested_too_deep_for_the_parser_are_none():
    assert _object("[" * 200_000) is None


# --- tools and proposals ---------------------------------------------------------------------------------------------


def test_cleanup_update_item_status_to_the_current_status_proposes_nothing(client, fake, engine):
    with Session(engine, expire_on_commit=False) as s:
        shirt = add_item(s, name="Shirt")

    turn(
        client,
        fake,
        new_chat(client),
        ask(("update_item_status", {"item_id": shirt.id, "status": "available"})),
        say("ok"),
    )

    assert result(fake) == {"error": {"code": "invalid_arguments", "message": "The item is already available."}}


def test_cleanup_a_wear_proposal_of_a_deleted_event_is_not_actionable_and_expires_on_confirm(client, engine):
    chat = add_session(engine)
    with Session(engine, expire_on_commit=False) as s:
        item, event = add_item(s, name="Shirt"), add_party(s)
    ui = log_wear_ui([item.id], event.id)
    call = add_call(engine, chat, "log_wear", **proposal(ui, NOW.date(), ProposalState.pending.value))
    with Session(engine) as s:
        s.delete(s.get(Event, event.id))
        s.commit()

    shown = next(c for c in client.get(f"/api/chat/sessions/{chat}").json()["tool_calls"] if c["id"] == call)[
        "proposal"
    ]
    r = client.post(f"/api/chat/proposals/{call}/confirm")

    assert (shown["state"], shown["actionable"]) == ("pending", False)
    assert (r.status_code, r.json()["code"]) == (409, "proposal_expired")
    assert "the event was deleted" in r.json()["detail"]


def test_cleanup_a_search_text_over_120_characters_is_refused(client):
    assert client.get("/api/items", params={"q": "x" * 121}).status_code == 422
    assert client.get("/api/items", params={"q": "x" * 120}).status_code == 200


def test_cleanup_the_session_view_of_a_long_chat_does_not_load_the_stored_arguments_and_results(engine):
    chat = add_session(engine)
    add_call(engine, chat, "get_item", args={"item_id": 1}, result="y" * 12_000)
    statements: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def record(_conn, _cursor, statement, *_rest):
        statements.append(statement)

    with Session(engine) as s:
        session_view(1, s, chat, NOW.date())
    event.remove(engine, "before_cursor_execute", record)

    (tool_select,) = (q for q in statements if "FROM toolcall" in q)
    assert "toolcall.result" not in tool_select and "toolcall.args" not in tool_select


def test_cleanup_a_tool_that_writes_reads_nothing_back_after_its_commit(client, fake, engine, monkeypatch):
    refresh = Session.refresh

    def refused(self, instance, *args, **kwargs):
        assert not isinstance(instance, Event), "read after the commit"
        return refresh(self, instance, *args, **kwargs)

    monkeypatch.setattr(Session, "refresh", refused)

    turn(
        client,
        fake,
        new_chat(client),
        ask(("create_event", {"type": "party", "starts_at": "2026-10-10T20:00"})),
        say("done"),
    )

    assert "error" not in result(fake)
    assert client.get("/api/events").json()[0]["type"] == "party"
