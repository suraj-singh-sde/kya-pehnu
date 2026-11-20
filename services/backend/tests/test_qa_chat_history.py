"""QA gaps for the chat history ACs of run 20261026 (AC-4 and AC-6), read through the real routes.

AC-6: a reply sent to an older chat through the message route moves that chat to the top of the list.
AC-4: an opened chat holds messages, an outfit card and a confirmed proposal in turn order, with its result.
"""

from datetime import UTC, datetime

import pytest
from sqlmodel import Session

from kyapehnu.agent.models import ProposalState
from tests.chat_fakes import fixed_now, say
from tests.chat_helpers import NOW, engine_of, pin_rest_today, turn
from tests.chat_rows import add_call, add_message, add_session, log_wear_ui, outfits_ui, proposal, suggestion
from tests.wear_fixtures import three_items

SESSIONS = "/api/chat/sessions"


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    fixed_now(monkeypatch, NOW)
    pin_rest_today(monkeypatch)


def test_a_reply_in_an_older_chat_through_the_route_moves_it_to_the_top(client, fake):
    engine = engine_of(client)
    oldest = add_session(engine, "Office look", updated_at=datetime(2026, 10, 1, 9, tzinfo=UTC))
    newest = add_session(engine, "Gym kit", updated_at=datetime(2026, 10, 3, 9, tzinfo=UTC))
    assert [c["id"] for c in client.get(SESSIONS).json()] == [newest, oldest]

    out = turn(client, fake, oldest, say("Try the navy shirt."), text="and for Saturday?")

    assert out["outcome"] == "ok"
    reopened = client.get(f"{SESSIONS}/{oldest}").json()
    assert [(m["role"], m["content"]) for m in reopened["messages"]] == [
        ("user", "and for Saturday?"),
        ("assistant", "Try the navy shirt."),
    ]
    assert [c["id"] for c in client.get(SESSIONS).json()] == [oldest, newest]


def test_an_opened_chat_holds_messages_a_card_and_a_confirmed_proposal_in_turn_order(client):
    engine = engine_of(client)
    with Session(engine) as s:
        top, bottom, shoes = three_items(s, client.app.state.ctx.data_dir)
        ids = [top.id, bottom.id, shoes.id]
    chat = add_session(engine, "Dinner")
    add_message(engine, chat, 1, 0, "user", "what for dinner?")
    add_message(engine, chat, 1, 2, "assistant", "Here are two ideas.")
    card = add_call(engine, chat, "generate_outfits", turn=1, step=1, ui=outfits_ui([suggestion(ids)]))
    add_message(engine, chat, 2, 0, "user", "log the first one")
    add_message(engine, chat, 2, 2, "assistant", "Waiting on you.")
    wear = add_call(
        engine,
        chat,
        "log_wear",
        turn=2,
        step=1,
        call_id="c2",
        **proposal(log_wear_ui(ids), NOW.date()),
        result='{"status":"awaiting_confirmation"}',
    )
    assert client.post(f"/api/chat/proposals/{wear}/confirm").status_code == 200

    opened = client.get(f"{SESSIONS}/{chat}").json()

    assert [(m["turn"], m["step"], m["role"]) for m in opened["messages"]] == [
        (1, 0, "user"),
        (1, 2, "assistant"),
        (2, 0, "user"),
        (2, 2, "assistant"),
    ]
    assert [(c["id"], c["turn"], c["step"]) for c in opened["tool_calls"]] == [(card, 1, 1), (wear, 2, 1)]
    assert len(opened["tool_calls"][0]["card"]["suggestions"]) == 1
    shown = opened["tool_calls"][1]["proposal"]
    assert shown["state"] == ProposalState.confirmed.value
    assert shown["actionable"] is False
    assert shown["decided_at"] is not None
    assert shown["result"]
