"""Events made from the chat (AC-14, AC-15; LLD 3.5 `create_event`, H1): the resolved fields become a real Event, the
chat shows a chip, and `generate_outfits` for that event uses its audience.
"""

from datetime import date, datetime

import pytest
from sqlmodel import Session, select

from kyapehnu.wardrobe.models import Event
from tests.chat_fakes import ask, fixed_now, say
from tests.chat_helpers import NOW, count_rows, engine_of, new_chat, pin_rest_today, result, tool_rows, turn
from tests.fakes import add_item, add_wear

SATURDAY_EIGHT = {
    "type": "party",
    "title": "Dinner with Priya",
    "starts_at": "2026-10-10T20:00",
    "place": "Khan Market",
    "dress_code": "smart casual",
    "audience_tags": ["Priya"],
}


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    fixed_now(monkeypatch, NOW)
    pin_rest_today(monkeypatch)


@pytest.fixture
def engine(client):
    return engine_of(client)


def events(engine) -> list[Event]:
    with Session(engine, expire_on_commit=False) as s:
        return list(s.exec(select(Event).order_by(Event.id)).all())


def test_dinner_with_priya_creates_the_event_listed_and_chipped(client, fake, engine):
    chat = new_chat(client)

    out = turn(
        client,
        fake,
        chat,
        ask(("create_event", SATURDAY_EIGHT)),  # "Saturday at 8" resolved by the model against Wednesday 2026-10-07
        say("Saved. Want outfit ideas?"),
        text="dinner with Priya on Saturday at 8 in Khan Market, smart casual",
    )

    (made,) = events(engine)
    assert (made.type, made.starts_at, made.ends_at) == ("party", datetime(2026, 10, 10, 20, 0), None)
    assert (made.audience_tags, made.dress_code, made.place) == (["Priya"], "smart casual", "Khan Market")
    listed = client.get("/api/events").json()
    assert [e["id"] for e in listed] == [made.id]
    chip = out["session"]["tool_calls"][0]
    assert (chip["name"], chip["event"]["id"], chip["event"]["type"]) == ("create_event", made.id, "party")
    assert chip["event"]["audience_tags"] == ["Priya"]  # the page shows the whole event: only the model is not told
    assert client.get(f"/api/chat/sessions/{chat}").json()["tool_calls"][0]["event"] == chip["event"]
    (row,) = tool_rows(engine, chat)
    assert (row.event_id, row.error, row.proposal_state) == (made.id, None, None)
    assert row.ui == {"kind": "event", "summary": "Event created"}
    assert result(fake)["id"] == made.id  # the model gets the id it needs for generate_outfits


@pytest.mark.parametrize(
    "bad",
    [
        {"starts_at": "2026-10-10T20:00+05:30"},
        {"starts_at": "2026-10-10T20:00", "ends_at": "2026-10-10T19:00"},
        {"starts_at": "2026-10-10T20:00", "ends_at": "2026-10-20T20:00"},
        {"starts_at": "1999-12-31T20:00"},
    ],
    ids=["offset", "ends before start", "longer than 7 days", "year out of range"],
)
def test_an_event_that_breaks_a_rule_of_the_form_is_refused_and_nothing_is_saved(client, fake, engine, bad):
    turn(client, fake, new_chat(client), ask(("create_event", {**SATURDAY_EIGHT, **bad})), say("Sorry."))

    assert result(fake)["error"]["code"] == "invalid_arguments"
    assert events(engine) == [] and tool_rows(engine)[0].event_id is None


def _repeated_with_priya(client, fake, engine):
    """A white wardrobe of two tops, A (solid) and B (printed), over striped trousers in loafers or derbies; A worn
    with Priya in loafers 80 days ago. The striped trousers stand out in A, so every A look is penalised with her."""
    with Session(engine, expire_on_commit=False) as s:
        solid = add_item(s, name="Solid tee", category="top", pattern="solid", formality=3, warmth=2)
        printed = add_item(s, name="Print tee", category="top", pattern="print", formality=3, warmth=2)
        bottom = add_item(s, name="Striped trousers", category="bottom", pattern="stripe", formality=3, warmth=2)
        shoes = add_item(s, name="Loafers", category="footwear", pattern="solid", formality=3, warmth=2)
        derbies = add_item(s, name="Derbies", category="footwear", pattern="solid", formality=3, warmth=2)
        for item in (solid, printed, bottom, shoes, derbies):
            item.colors = [{"hex": "#ffffff", "name": "white"}]
            s.add(item)
        s.commit()
        add_wear(s, [solid.id, bottom.id, shoes.id], date(2026, 7, 19), audience=["Priya"])
        return solid.id, printed.id


# The model picks the first two looks shown: a solid-top look and a printed-top look, two items apart (D12).
BOTH_LOOKS = {
    "picks": [{"candidate_number": n, "reason": "ok", "swap_slot": None, "swap_item_id": None} for n in (1, 2)]
}


def test_generate_reuses_the_session_event_and_its_audience_penalises(client, fake, engine):
    solid, printed = _repeated_with_priya(client, fake, engine)
    chat = new_chat(client)

    turn(client, fake, chat, ask(("create_event", SATURDAY_EIGHT)), say("Saved."))
    (event_id,) = (e.id for e in events(engine))
    fake.replies.append(BOTH_LOOKS)
    turn(client, fake, chat, ask(("generate_outfits", {"event_id": event_id})), say("Here."), text="ideas?")
    turn(client, fake, chat, text="and?")  # the turn ended on the looks: the model reads them now
    with_priya = [s["item_ids"] for s in result(fake)["suggestions"]]
    without = {**SATURDAY_EIGHT, "audience_tags": [], "title": "Dinner"}
    turn(client, fake, chat, ask(("create_event", without)), say("Saved."), text="and without her")
    control_id = max(e.id for e in events(engine))
    fake.replies.append(BOTH_LOOKS)
    turn(client, fake, chat, ask(("generate_outfits", {"event_id": control_id})), say("Here."), text="ideas?")
    turn(client, fake, chat, text="and?")
    without_priya = [s["item_ids"] for s in result(fake)["suggestions"]]

    assert count_rows(engine, Event) == {"Event": 2}  # generate made no event of its own
    assert len(with_priya) == len(without_priya) == 2
    assert solid in without_priya[0] and printed in with_priya[0]  # the outfit worn with her moves down
    assert [e.audience_tags for e in events(engine)] == [["Priya"], []]
