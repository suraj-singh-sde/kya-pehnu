"""What the chat logs (AC-37; LLD 7): ids, counts, codes and class names, never text, titles, places, audiences,
arguments or a key; and the `event=` lines the design names.
"""

import logging
import re

import pytest
from sqlmodel import Session

from kyapehnu.agent.llm.base import LLMUnavailableError
from kyapehnu.wardrobe.models import Item
from tests.chat_fakes import ask, fixed_now, say
from tests.chat_helpers import (
    NOW,
    engine_of,
    failing,
    llm_rows,
    new_chat,
    party_wardrobe,
    pin_rest_today,
    running_app,
    send,
    tool_rows,
    turn,
)
from tests.fakes import FakeTransport, add_item

KEY = "sk-ant-LEAKYKEY-4242"
TYPED = "LOGMARK-MESSAGE-71"
TITLE = "LOGMARK-TITLE-72"
NOTES = "LOGMARK-NOTES-73"
PLACE = "LOGMARK-PLACE-74"
AUDIENCE = "LOGMARK-AUDIENCE-75"
REPLY = "LOGMARK-REPLY-76"
ITEM = "LOGMARK-ITEM-77"
ARGUMENT = "LOGMARK-ARGUMENT-78"
MARKS = (TYPED, TITLE, NOTES, PLACE, AUDIENCE, REPLY, ITEM, ARGUMENT, KEY)
EVENT = {
    "type": "party",
    "title": TITLE,
    "notes": NOTES,
    "place": PLACE,
    "audience_tags": [AUDIENCE],
    "starts_at": "2026-10-10T20:00",
}


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    fixed_now(monkeypatch, NOW)
    pin_rest_today(monkeypatch)


@pytest.fixture
def engine(client):
    return engine_of(client)


def lines(caplog, event: str) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith(f"event={event} ")]


def no_text(caplog) -> None:
    assert [m for m in MARKS if m in caplog.text] == []


def success(client, fake, engine):
    with Session(engine, expire_on_commit=False) as s:
        party_wardrobe(s)
        add_item(s, name=ITEM)
    return turn(
        client,
        fake,
        new_chat(client),
        ask(("create_event", EVENT)),
        ask(("search_wardrobe", {"q": ITEM})),
        ask(("generate_outfits", {"event_id": 1})),  # the turn ends on the looks: the reply is never asked for
        say(REPLY),
        text=f"dinner please {TYPED}",
    )


def validation_failure(client, fake, engine):
    return turn(
        client,
        fake,
        new_chat(client),
        ask(
            ("create_event", {**EVENT, "starts_at": "not a time"}),
            ("get_item", {"item_id": ARGUMENT}),
            (f"{ARGUMENT}-tool", {"q": TYPED}),
        ),
        say(REPLY),
        text=TYPED,
    )


@pytest.mark.parametrize("case", ["success", "validation"])
def test_logs_hold_no_text_and_no_key(client, fake, engine, caplog, case):
    caplog.set_level(logging.DEBUG)

    {"success": success, "validation": validation_failure}[case](client, fake, engine)

    no_text(caplog)
    assert lines(caplog, "chat_turn")  # and the turn did log


def test_logs_hold_no_text_and_no_key_when_the_provider_fails(tmp_path, fake_weather, caplog):
    caplog.set_level(logging.DEBUG)
    stand_in = FakeTransport()
    stand_in.api_key = KEY
    with running_app(tmp_path, fake_weather, stand_in, anthropic_api_key=KEY) as client:
        chat = new_chat(client)

        r = send(client, stand_in, chat, LLMUnavailableError(f"401 {KEY} for {TYPED}"), text=TYPED)

        assert r.json()["outcome"] == "provider_error"
        leaked = [m for m in (TITLE, NOTES, PLACE, AUDIENCE, REPLY, ITEM, ARGUMENT, KEY) if m in caplog.text]
        assert leaked == [] and TYPED not in caplog.text
        assert KEY not in r.text  # nor in the notice the page shows
        engine = engine_of(client)
        assert KEY not in "".join(f"{x.request_summary}{x.response}{x.error}" for x in llm_rows(engine))
        assert KEY not in "".join(f"{t.result}{t.args}" for t in tool_rows(engine, chat))
        assert any(
            line.startswith("event=chat_turn ") and "outcome=provider_error" in line
            for line in lines(caplog, "chat_turn")
        )


# --- the lines the design names -------------------------------------------------------------------------------------


def test_a_turn_logs_one_chat_turn_line_and_one_tool_call_line_per_call(client, fake, engine, caplog):
    caplog.set_level(logging.INFO)

    success(client, fake, engine)

    (turn_line,) = lines(caplog, "chat_turn")
    assert re.fullmatch(
        r"event=chat_turn session_id=1 turn=1 model_calls=\d+ tool_calls=3 outcome=ok latency_ms=\d+", turn_line
    )
    assert [re.sub(r" latency_ms=\d+", "", x) for x in lines(caplog, "tool_call")] == [
        "event=tool_call session_id=1 name=create_event outcome=ok",
        "event=tool_call session_id=1 name=search_wardrobe outcome=ok",
        "event=tool_call session_id=1 name=generate_outfits outcome=ok",
    ]
    assert all(re.search(r" latency_ms=\d+$", x) for x in lines(caplog, "tool_call"))


def test_a_failed_call_logs_its_error_code_and_an_unknown_tool_is_logged_as_unknown(client, fake, engine, caplog):
    caplog.set_level(logging.INFO)

    validation_failure(client, fake, engine)

    outcomes = [re.sub(r" latency_ms=\d+", "", x) for x in lines(caplog, "tool_call")]
    assert outcomes == [
        "event=tool_call session_id=1 name=create_event outcome=invalid_arguments",
        "event=tool_call session_id=1 name=get_item outcome=invalid_arguments",
        "event=tool_call session_id=1 name=unknown outcome=unknown_tool",
    ]


def test_a_proposal_is_logged_as_a_proposal_and_its_decision_with_the_state(client, fake, engine, caplog):
    caplog.set_level(logging.INFO)
    with Session(engine, expire_on_commit=False) as s:
        shirt = add_item(s, name="Shirt")
    chat = new_chat(client)
    turn(
        client,
        fake,
        chat,
        ask(("update_item_status", {"item_id": shirt.id, "status": "laundry"}), ("log_wear", {"item_ids": [shirt.id]})),
        say("ok"),
    )
    first, second = (t.id for t in tool_rows(engine, chat))

    assert client.post(f"/api/chat/proposals/{first}/confirm").status_code == 200
    with Session(engine, expire_on_commit=False) as s:  # the shirt is in laundry now: the second proposal is stale
        assert s.get(Item, shirt.id).status == "laundry"
    assert client.post(f"/api/chat/proposals/{second}/confirm").status_code == 409

    assert [x.split(" latency_ms")[0] for x in lines(caplog, "tool_call")] == [
        f"event=tool_call session_id={chat} name=update_item_status outcome=proposal",
        f"event=tool_call session_id={chat} name=log_wear outcome=proposal",
    ]
    assert lines(caplog, "proposal_decided") == [
        f"event=proposal_decided tool_call_id={first} state=confirmed",
        f"event=proposal_decided tool_call_id={second} state=expired",
    ]


def test_a_tool_that_fails_unexpectedly_is_logged_by_class_only(client, fake, engine, caplog):
    caplog.set_level(logging.DEBUG)
    with Session(engine, expire_on_commit=False) as s:
        add_item(s, name=ITEM)

    with failing(engine, r"FROM item\b", lambda: RuntimeError(f"{TYPED} exploded")):
        turn(client, fake, new_chat(client), ask(("search_wardrobe", {"q": ARGUMENT})), say(REPLY))

    assert lines(caplog, "tool_failed") == ["event=tool_failed name=search_wardrobe error=RuntimeError"]
    no_text(caplog)
