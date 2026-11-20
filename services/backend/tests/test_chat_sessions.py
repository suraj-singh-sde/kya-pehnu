"""Chat sessions over REST (AC-16, 17, 18, 19, 53; LLD 3.1, 5.4): create, list, open with cards, delete.

The rows a turn leaves behind are built by hand (`tests/chat_rows.py`): the turn loop is another package's.
"""

import json
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import insert, text
from sqlmodel import Session, select

from kyapehnu.agent.chat import store
from kyapehnu.agent.models import ChatMessage, ChatSession, LlmCall, ToolCall
from kyapehnu.wardrobe.models import Event, Feedback, Outfit, WearLog
from kyapehnu.wardrobe.outfits.schemas import WeatherOut
from kyapehnu.weather.summary import WeatherSummary
from tests.chat_fakes import fixed_now
from tests.chat_rows import (
    add_call,
    add_llm_call,
    add_message,
    add_session,
    log_wear_ui,
    outfits_ui,
    proposal,
    suggestion,
)
from tests.fakes import OWNER_ID, add_item
from tests.wear_fixtures import add_event, count, three_items

NOW = datetime(2026, 10, 7, 9, 30)  # a Wednesday
USER_MARKER = "MARKER-USER-7Q3X"
REPLY_MARKER = "MARKER-REPLY-9K2W"
SESSIONS = "/api/chat/sessions"


def engine_of(client):
    return client.app.state.ctx.engine


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    fixed_now(monkeypatch, NOW)


def rows_holding(engine, *needles: str) -> list[str]:
    """Every table that has a row whose values contain one of the needles."""
    found = []
    with engine.connect() as conn:
        tables = [r[0] for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type = 'table'"))]
        for table in tables:
            for row in conn.execute(text(f'SELECT * FROM "{table}"')):  # noqa: S608 - names come from sqlite_master
                if any(needle in json.dumps(list(row), default=str) for needle in needles):
                    found.append(table)
    return found


# --- AC-19: delete (written first) -----


def chat_world(client) -> dict[str, int]:
    """Session 1 holds both markers in every table a turn writes; session 10 and an Outfits-page pick hold others."""
    engine = engine_of(client)
    event = Event(
        user_id=OWNER_ID, type="party", title="Dinner", starts_at=datetime(2026, 10, 10, 19), place="Gurugram"
    )
    with Session(engine) as s:
        s.add(event)
        s.commit()
        event_id = event.id
    target = add_session(engine, "Dinner plans")
    other = add_session(engine, "Other chat", id=10)
    add_message(engine, target, 1, 0, "user", f"please help {USER_MARKER}")
    add_message(engine, target, 1, 2, "assistant", f"here you go {REPLY_MARKER}")
    add_call(
        engine,
        target,
        "create_event",
        args={"notes": USER_MARKER},
        result=f'{{"notes":"{USER_MARKER}"}}',
        ui={"kind": "event", "summary": REPLY_MARKER},
        event_id=event_id,
    )
    add_message(engine, other, 1, 0, "user", "OTHER-USER-TEXT")
    add_call(engine, other, "search_wardrobe", result="OTHER-RESULT")
    # the nested pick of a chat holds the session events' text; the chat rows hold counts only
    add_llm_call(engine, "outfit_pick", f"session={target} images=[] bytes\nNotes: {USER_MARKER} {REPLY_MARKER}")
    add_llm_call(engine, "chat", f"session={target} messages=2 tools=9", response="2 tool calls, reply 21 characters")
    add_llm_call(engine, "outfit_pick", f"session={other} images=[] bytes\nNotes: OTHER-PICK")
    add_llm_call(engine, "outfit_pick", "images=[] bytes\nNotes: OUTFITS-PAGE-PICK")
    return {"target": target, "other": other, "event": event_id}


def test_delete_removes_text_and_pick_rows_keeps_chat_rows_and_other_sessions(client):
    world = chat_world(client)
    engine = engine_of(client)
    assert {"chatmessage", "toolcall", "llmcall"} <= set(rows_holding(engine, USER_MARKER, REPLY_MARKER))

    r = client.delete(f"{SESSIONS}/{world['target']}")

    assert r.status_code == 204
    assert rows_holding(engine, USER_MARKER, REPLY_MARKER) == []
    with Session(engine) as s:
        assert s.get(ChatSession, world["target"]) is None
        assert list(s.exec(select(ChatMessage).where(ChatMessage.session_id == world["target"]))) == []
        assert list(s.exec(select(ToolCall).where(ToolCall.session_id == world["target"]))) == []
        kept = sorted((c.purpose, c.request_summary.splitlines()[0]) for c in s.exec(select(LlmCall)))
        assert s.get(Event, world["event"]) is not None  # the user's event is theirs, not the chat's
    assert kept == [
        ("chat", f"session={world['target']} messages=2 tools=9"),
        ("outfit_pick", "images=[] bytes"),
        ("outfit_pick", f"session={world['other']} images=[] bytes"),
    ]
    assert [c["id"] for c in client.get(SESSIONS).json()] == [world["other"]]
    assert client.get(f"{SESSIONS}/{world['target']}").status_code == 404


def test_delete_leaves_the_other_sessions_rows_untouched(client):
    world = chat_world(client)

    client.delete(f"{SESSIONS}/{world['target']}")

    opened = client.get(f"{SESSIONS}/{world['other']}").json()
    assert [m["content"] for m in opened["messages"]] == ["OTHER-USER-TEXT"]
    assert [c["name"] for c in opened["tool_calls"]] == ["search_wardrobe"]


def test_the_chat_llm_rows_of_a_deleted_session_are_still_listed_in_debug(client):
    world = chat_world(client)
    client.delete(f"{SESSIONS}/{world['target']}")

    listed = client.get("/api/debug/llm-calls", params={"session_id": world["target"], "purpose": "chat"}).json()

    assert [c["request_summary"] for c in listed] == [f"session={world['target']} messages=2 tools=9"]


def test_delete_of_an_unknown_session_is_404_and_changes_nothing(client):
    chat_world(client)

    r = client.delete(f"{SESSIONS}/999")

    assert (r.status_code, r.json()["code"]) == (404, "not_found")
    assert len(client.get(SESSIONS).json()) == 2
    assert rows_holding(engine_of(client), USER_MARKER) != []


def test_delete_while_a_turn_runs_is_409_and_keeps_every_row(client):
    world = chat_world(client)

    with client.app.state.turns.hold(world["target"]):
        r = client.delete(f"{SESSIONS}/{world['target']}")

    assert (r.status_code, r.json()["code"]) == (409, "turn_running")
    assert {"chatmessage", "toolcall", "llmcall"} <= set(rows_holding(engine_of(client), USER_MARKER))
    assert client.delete(f"{SESSIONS}/{world['target']}").status_code == 204  # the guard was released


def test_the_id_of_a_deleted_newest_session_is_not_given_to_the_next_one(client):
    first = client.post(SESSIONS).json()["id"]
    newest = client.post(SESSIONS).json()["id"]
    assert client.delete(f"{SESSIONS}/{newest}").status_code == 204

    after = client.post(SESSIONS).json()["id"]

    assert (first, newest, after) == (first, first + 1, first + 2)


# --- create and list (AC-18) -----


def test_create_makes_an_empty_titled_session_listed_first(client):
    r = client.post(SESSIONS)

    assert r.status_code == 201
    body = r.json()
    assert (body["title"], body["id"]) == ("", 1)
    assert [c["id"] for c in client.get(SESSIONS).json()] == [body["id"]]


def test_list_newest_first_with_titles_open_and_continue(client):
    engine = engine_of(client)
    oldest = add_session(engine, "Office look", updated_at=datetime(2026, 10, 1, 9, tzinfo=UTC))
    middle = add_session(engine, "Dinner with Priya", updated_at=datetime(2026, 10, 2, 9, tzinfo=UTC))
    newest = add_session(engine, "Gym kit", updated_at=datetime(2026, 10, 3, 9, tzinfo=UTC))
    add_message(engine, oldest, 1, 0, "user", "what to wear to the office")
    add_message(engine, oldest, 1, 2, "assistant", "Try the navy shirt.")
    add_call(engine, oldest, "search_wardrobe", step=1, ui={"kind": "read", "summary": "Found 12 items"})

    listed = client.get(SESSIONS).json()
    opened = client.get(f"{SESSIONS}/{oldest}").json()

    assert [(c["id"], c["title"]) for c in listed] == [
        (newest, "Gym kit"),
        (middle, "Dinner with Priya"),
        (oldest, "Office look"),
    ]
    assert [(m["turn"], m["step"], m["role"]) for m in opened["messages"]] == [(1, 0, "user"), (1, 2, "assistant")]
    assert [(c["name"], c["summary"]) for c in opened["tool_calls"]] == [("search_wardrobe", "Found 12 items")]

    # sending continues the same session: the next turn is stored after the first and the chat moves to the top
    turn = store.start_turn(1, engine, oldest, "and for Saturday?")
    store.finish_turn(engine, oldest)
    again = client.get(f"{SESSIONS}/{oldest}").json()
    assert turn == 2
    assert [(m["turn"], m["step"], m["content"]) for m in again["messages"]][-1] == (2, 0, "and for Saturday?")
    assert client.get(SESSIONS).json()[0]["id"] == oldest


def test_501_chats_list_the_newest_500_and_do_not_error(client):
    """AC-8: one more than the cap. The oldest chat is the one left out."""
    first = datetime(2026, 1, 1, tzinfo=UTC)
    with Session(engine_of(client)) as s:
        s.execute(
            insert(ChatSession),
            [
                {
                    "id": n,
                    "user_id": OWNER_ID,
                    "title": f"Chat {n}",
                    "created_at": first,
                    "updated_at": first + timedelta(minutes=n),
                }
                for n in range(1, 502)
            ],
        )
        s.commit()

    r = client.get(SESSIONS)

    assert r.status_code == 200
    assert [c["id"] for c in r.json()] == list(range(501, 1, -1))


def test_opening_an_unknown_session_is_404(client):
    r = client.get(f"{SESSIONS}/999")

    assert (r.status_code, r.json()["code"]) == (404, "not_found")


def test_a_failed_tool_call_is_listed_with_its_code_and_no_card(client):
    engine = engine_of(client)
    chat = add_session(engine)
    add_call(engine, chat, "get_weather", error="place_not_found", result='{"error":{}}')

    [call] = client.get(f"{SESSIONS}/{chat}").json()["tool_calls"]

    assert (call["name"], call["error"], call["card"], call["proposal"], call["event"]) == (
        "get_weather",
        "place_not_found",
        None,
        None,
        None,
    )


def test_an_event_the_chat_created_is_shown_as_the_calls_event(client):
    engine = engine_of(client)
    chat = add_session(engine)
    with Session(engine) as s:
        event_id = add_event(s, title="Dinner with Priya").id
    add_call(engine, chat, "create_event", event_id=event_id, ui={"kind": "event", "summary": "Event created"})

    [call] = client.get(f"{SESSIONS}/{chat}").json()["tool_calls"]

    assert (call["event"]["id"], call["event"]["title"], call["card"]) == (event_id, "Dinner with Priya", None)


# --- cards (AC-16, AC-17, AC-53) -----


def card_world(client):
    """Six available items, a laundry item, an event and a chat with one generate_outfits call of three suggestions."""
    engine = engine_of(client)
    with Session(engine) as s:
        top, bottom, shoes = three_items(s, client.app.state.ctx.data_dir)
        other_top = add_item(s, name="Blue tee", category="top")
        other_bottom = add_item(s, name="Grey jeans", category="bottom")
        laundry = add_item(s, name="Black hoodie", category="top", status="laundry")
        event = add_event(s)
        top_id, bottom_id, shoes_id = top.id, bottom.id, shoes.id
        other_top_id, other_bottom_id, laundry_id, event_id = other_top.id, other_bottom.id, laundry.id, event.id
    chat = add_session(engine)
    summary = WeatherSummary(
        source="forecast",
        feels_like_min_c=18.0,
        feels_like_max_c=24.0,
        humidity_mean_pct=None,
        rain_probability_max_pct=60.0,
        wind_max_kmh=None,
        uv_max=None,
        aqi_max=None,
    )
    weather = WeatherOut(summary=summary, notes=["Rain likely: rain-safe footwear."]).model_dump(mode="json")
    sets = [[top_id, bottom_id, shoes_id], [other_top_id, bottom_id, shoes_id], [top_id, other_bottom_id, shoes_id]]
    add_message(engine, chat, 1, 2, "assistant", f"Take item {laundry_id}, the black hoodie, it is warm.")
    add_call(
        engine,
        chat,
        "generate_outfits",
        ui=outfits_ui([suggestion(ids, reason=f"Look {n}") for n, ids in enumerate(sets)], event_id, weather=weather),
    )
    return chat, sets, event_id, laundry_id


def test_three_suggestions_become_one_card_and_reply_ids_add_nothing(client):
    chat, sets, event_id, laundry_id = card_world(client)

    opened = client.get(f"{SESSIONS}/{chat}").json()

    [call] = opened["tool_calls"]
    card = call["card"]
    assert (call["name"], call["summary"]) == ("generate_outfits", "3 outfits")
    assert [s["item_ids"] for s in card["suggestions"]] == sets
    assert [s["reason"] for s in card["suggestions"]] == ["Look 0", "Look 1", "Look 2"]
    assert [[i["id"] for i in s["items"]] for s in card["suggestions"]] == sets
    assert all(s["wearable"] and s["rating"] is None and s["notes"] for s in card["suggestions"])
    assert card["event"]["id"] == event_id
    assert card["weather"]["notes"] == ["Rain likely: rain-safe footwear."]
    assert card["problem"] is None
    # the reply names the laundry item, but a card holds only what the call proposed
    assert laundry_id not in {i["id"] for s in card["suggestions"] for i in s["items"]}
    assert [m["content"] for m in opened["messages"]] == [f"Take item {laundry_id}, the black hoodie, it is warm."]


def test_a_problem_card_has_no_suggestions_and_keeps_the_problem(client):
    engine = engine_of(client)
    chat = add_session(engine)
    problem = {"slot": "footwear", "cause": "missing", "message": "No footwear in the wardrobe"}
    add_call(engine, chat, "generate_outfits", ui=outfits_ui([], None, problem=problem))

    [call] = client.get(f"{SESSIONS}/{chat}").json()["tool_calls"]

    assert call["card"]["suggestions"] == []
    assert call["card"]["problem"] == problem
    assert call["card"]["event"] is None


def test_a_card_shows_the_latest_thumb_of_the_saved_outfit_with_these_items(client):
    chat, sets, _, _ = card_world(client)
    with Session(engine_of(client)) as s:
        key = ",".join(str(i) for i in sorted(sets[0]))
        outfit = Outfit(user_id=OWNER_ID, item_ids=sets[0], item_key=key, name="Saved")
        s.add(outfit)
        s.commit()
        s.add_all(
            [
                Feedback(user_id=OWNER_ID, outfit_id=outfit.id, rating=-1),
                Feedback(user_id=OWNER_ID, outfit_id=outfit.id, rating=1),
            ]
        )
        s.commit()

    card = client.get(f"{SESSIONS}/{chat}").json()["tool_calls"][0]["card"]

    assert [s["rating"] for s in card["suggestions"]] == [1, None, None]


def test_card_item_ids_wear_like_the_outfits_page(client):
    chat, sets, event_id, _ = card_world(client)
    card = client.get(f"{SESSIONS}/{chat}").json()["tool_calls"][0]["card"]
    chosen = card["suggestions"][0]["item_ids"]

    first = client.post("/api/outfits/wear", json={"item_ids": chosen, "event_id": card["event"]["id"]})
    again = client.post("/api/outfits/wear", json={"item_ids": chosen[::-1], "event_id": event_id})

    assert (first.status_code, again.status_code) == (201, 201)
    assert first.json()["id"] == again.json()["id"]  # the saved outfit is reused, whatever the order
    assert first.json()["collage_path"] is not None
    with Session(engine_of(client)) as s:
        assert count(s, Outfit) == 1
        wears = list(s.exec(select(WearLog)))
    assert [(w.item_ids, w.worn_on, w.event_id) for w in wears] == [(chosen, date.today(), event_id)] * 2


def test_reopened_card_shows_placeholder_and_status_and_disables_wear(client):
    chat, sets, _, _ = card_world(client)
    engine = engine_of(client)
    first, second, _third = sets
    a, b, c = first
    with Session(engine) as s:
        outfit = Outfit(
            user_id=OWNER_ID, item_ids=first, item_key=",".join(str(i) for i in sorted(first)), name="Saved"
        )
        s.add(outfit)
        s.commit()
        s.add(Feedback(user_id=OWNER_ID, outfit_id=outfit.id, rating=-1))
        s.commit()

    assert client.delete(f"/api/items/{a}", params={"hard": True}).status_code == 204
    assert client.post(f"/api/items/{b}/status", json={"status": "laundry"}).status_code == 200
    added = add_item_after_delete(engine)
    card = client.get(f"{SESSIONS}/{chat}").json()["tool_calls"][0]["card"]

    stale, ok, _ = card["suggestions"]
    assert stale["item_ids"] == [a, b, c]  # as proposed
    assert stale["items"][0] is None
    assert [(i["id"], i["status"]) for i in stale["items"][1:]] == [(b, "laundry"), (c, "available")]
    assert stale["wearable"] is False
    assert stale["rating"] == -1
    assert ok["wearable"] is False  # it holds B too
    assert added not in {a, b, c}
    assert added > max(a, b, c)  # the deleted id is not handed out again


def add_item_after_delete(engine) -> int:
    with Session(engine) as s:
        return add_item(s, name="Newcomer").id


def test_a_card_whose_items_are_all_available_is_wearable_and_one_with_a_deleted_swap_has_no_swap(client):
    engine = engine_of(client)
    with Session(engine) as s:
        top, bottom, shoes = three_items(s, client.app.state.ctx.data_dir)
        spare = add_item(s, name="Spare top", category="top")
        ids, spare_id = [top.id, bottom.id, shoes.id], spare.id
    chat = add_session(engine)
    add_call(engine, chat, "generate_outfits", ui=outfits_ui([suggestion(ids, swap_item_id=spare_id)]))
    assert client.delete(f"/api/items/{spare_id}", params={"hard": True}).status_code == 204

    [only] = client.get(f"{SESSIONS}/{chat}").json()["tool_calls"][0]["card"]["suggestions"]

    assert (only["wearable"], only["swap_item"]) == (True, None)


# --- proposals in the opened session -----


def test_a_proposal_of_an_earlier_day_opens_as_expired_and_not_actionable(client):
    engine = engine_of(client)
    chat = add_session(engine)
    with Session(engine) as s:
        item_id = add_item(s, name="Shirt").id
    add_call(engine, chat, "log_wear", **proposal(log_wear_ui([item_id]), date(2026, 10, 6)))
    add_call(engine, chat, "log_wear", step=2, **proposal(log_wear_ui([item_id]), date(2026, 10, 7)))

    old, today = (c["proposal"] for c in client.get(f"{SESSIONS}/{chat}").json()["tool_calls"])

    assert (old["state"], old["actionable"]) == ("expired", False)
    assert (today["state"], today["actionable"]) == ("pending", True)
