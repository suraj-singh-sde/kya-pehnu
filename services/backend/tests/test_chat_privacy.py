"""What the model may be told (AC-35, AC-36, AC-50; ADR-0012; LLD 3.6): every view is an allow-list, and the markers of
`marker_world` never reach a provider request, a log row, an LlmCall row or the results the model was given.

The key set of each view is written out in full, so a new field on a domain object fails here until someone decides
that the model may see it.
"""

import json
import logging
from datetime import date, datetime
from typing import Any

import pytest
from sqlmodel import Session

from kyapehnu.agent.chat.privacy import (
    HOME_LABEL,
    ChatView,
    event_view,
    item_view,
    outfit_view,
    same_place,
    wear_view,
    weather_view,
)
from kyapehnu.wardrobe.models import Event, Item
from kyapehnu.wardrobe.outfits.schemas import OutfitOut, WearLogOut, WeatherOut
from kyapehnu.weather.summary import WeatherSummary
from tests.chat_fakes import (
    AUDIENCE_MARKER,
    EVENT_NOTES_MARKER,
    EVENT_TITLE_MARKER,
    HOME_CITY_MARKER,
    MARKERS,
    PLACE_MARKER,
    ask,
    fixed_now,
    marker_world,
    say,
    sent_text,
)
from tests.chat_helpers import (
    NOW,
    PICK,
    PROPOSAL_TEXT,
    engine_of,
    llm_rows,
    new_chat,
    party_wardrobe,
    pin_rest_today,
    results,
    rows_holding,
    tool_rows,
    turn,
)
from tests.fakes import OWNER_ID, add_wear

ITEM_KEYS = {
    "id",
    "name",
    "category",
    "subcategory",
    "colours",
    "pattern",
    "fabric",
    "formality",
    "warmth",
    "seasons",
    "status",
    "tags",
}
EVENT_BASE_KEYS = {"id", "type", "starts_at", "ends_at", "outdoors", "dress_code"}
SESSION_EVENT_KEYS = {*EVENT_BASE_KEYS, "title", "notes", "place"}
WEATHER_KEYS = {
    "source",
    "feels_like_min_c",
    "feels_like_max_c",
    "humidity_mean_pct",
    "rain_probability_max_pct",
    "wind_max_kmh",
    "uv_max",
    "aqi_max",
    "notes",
}
WEAR_KEYS = {"worn_on", "item_ids", "event"}
OUTFIT_KEYS = {"id", "item_ids", "name"}
SUGGESTION_KEYS = {"number", "item_ids", "items", "reason", "notes", "swap_item_id"}
GENERATE_KEYS = {"event_id", "event", "weather", "problem", "suggestions"}
PROPOSAL = {"status": "awaiting_confirmation", "message": PROPOSAL_TEXT}


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    fixed_now(monkeypatch, NOW)
    pin_rest_today(monkeypatch)


@pytest.fixture
def engine(client):
    return engine_of(client)


def view(*session_events: int, home: str = "Zedtown Distinct") -> ChatView:
    return ChatView(session_id=1, session_event_ids=set(session_events), home_city=home)


def event(**fields: Any) -> Event:
    return Event(
        **{
            "id": 3,
            "type": "party",
            "title": "Dinner with Priya Distinct",
            "starts_at": datetime(2026, 10, 10, 19),
            "ends_at": None,
            "place": "Khan Market",
            "outdoors": True,
            "dress_code": "smart casual",
            "audience_tags": ["Priya Distinct"],
            "notes": "EVNOTE-MARKER-2P6R",
        }
        | fields
    )


# --- the views, one key set each ----------------------------------------------------------------------------------


def test_an_item_view_has_exactly_the_allow_listed_fields_and_nothing_private():
    item = Item(
        id=7,
        photo_id=3,
        bbox=[0.1, 0.1, 0.9, 0.9],
        name="Navy chinos",
        category="bottom",
        subcategory="chinos",
        colors=[{"hex": "#000080", "name": "navy", "role": "primary"}, {"hex": "#ffffff", "name": "white"}],
        pattern="solid",
        fabric="cotton",
        formality=3,
        warmth=2,
        seasons=["summer", "autumn"],
        fit="slim-fit-PRIVATE",
        brand="BRAND-PRIVATE",
        purchase_price=9876543,
        purchase_date=date(2025, 1, 2),
        status="laundry",
        cutout_path="images/cutout/cutout-PRIVATE.png",
        thumb_path="images/thumb/thumb-PRIVATE.webp",
        alt_cutout_path="images/cutout/alt-PRIVATE.png",
        alt_thumb_path="images/thumb/alt-PRIVATE.webp",
        phash="phash-PRIVATE",
        tags=["linen", "favourite"],
        notes="NOTE-MARKER-4H8T",
        ai_tags={"name": "AI-PRIVATE"},
    )

    shown = item_view(item)

    assert set(shown) == ITEM_KEYS
    assert shown == {
        "id": 7,
        "name": "Navy chinos",
        "category": "bottom",
        "subcategory": "chinos",
        "colours": ["navy", "white"],
        "pattern": "solid",
        "fabric": "cotton",
        "formality": 3,
        "warmth": 2,
        "seasons": ["summer", "autumn"],
        "status": "laundry",
        "tags": ["linen", "favourite"],
    }
    assert "PRIVATE" not in json.dumps(shown) and "9876543" not in json.dumps(shown)


def test_a_session_event_view_has_title_notes_and_place_but_never_the_audience():
    shown = event_view(
        event(
            title="Rooftop dinner",
            notes="bring a scarf",
            audience_tags=["AUDIENCE-PRIVATE"],
            ends_at=datetime(2026, 10, 10, 23),
        ),
        view(3),
    )

    assert set(shown) == SESSION_EVENT_KEYS
    assert (shown["id"], shown["type"], shown["outdoors"], shown["dress_code"]) == (3, "party", True, "smart casual")
    assert (shown["title"], shown["notes"], shown["place"]) == ("Rooftop dinner", "bring a scarf", "Khan Market")
    assert datetime.fromisoformat(shown["starts_at"]) == datetime(2026, 10, 10, 19)
    assert datetime.fromisoformat(shown["ends_at"]) == datetime(2026, 10, 10, 23)
    assert "AUDIENCE-PRIVATE" not in json.dumps(shown)


def test_the_home_city_is_never_named_even_in_a_session_event():
    shown = event_view(event(place="Zedtown Distinct"), view(3))

    assert shown["place"] == HOME_LABEL == "your home city"
    assert "edtown" not in json.dumps(shown)


def test_an_event_of_another_chat_is_type_and_date_only():
    shown = event_view(event(), view())  # not made in this chat

    assert set(shown) == EVENT_BASE_KEYS
    assert shown["ends_at"] is None and shown["type"] == "party"
    dumped = json.dumps(shown)
    assert not any(secret in dumped for secret in ("Dinner", "Priya", "EVNOTE", "Khan Market"))


def test_an_event_of_another_chat_in_the_home_city_says_your_home_city():
    shown = event_view(event(place=" zedtown distinct"), view())

    assert set(shown) == {*EVENT_BASE_KEYS, "place"} and shown["place"] == HOME_LABEL
    assert "Dinner" not in json.dumps(shown) and "edtown" not in json.dumps(shown)


def test_a_weather_view_is_the_summary_fields_and_the_notes_or_the_word_unavailable():
    summary = WeatherSummary(
        source="typical",
        feels_like_min_c=18.5,
        feels_like_max_c=27.0,
        humidity_mean_pct=61.0,
        rain_probability_max_pct=40.0,
        wind_max_kmh=12.0,
        uv_max=6.0,
        aqi_max=88.0,
    )

    shown = weather_view(WeatherOut(summary=summary, notes=["Rain likely: rain-safe footwear."]))

    assert set(shown) == WEATHER_KEYS
    assert shown == {**summary.model_dump(), "notes": ["Rain likely: rain-safe footwear."]}
    assert weather_view(None) == "unavailable"


def test_a_wear_view_is_the_day_the_items_and_the_event_as_the_model_may_see_it():
    log = WearLogOut(
        id=11, outfit_id=5, item_ids=[1, 2], worn_on=date(2026, 10, 5), event_id=3, audience_tags=["AUDIENCE-PRIVATE"]
    )
    other = event(audience_tags=["AUDIENCE-PRIVATE"])

    shown = wear_view(log, {3: other}, view())
    session = wear_view(log, {3: other}, view(3))
    bare = wear_view(log.model_copy(update={"event_id": None}), {}, view())

    assert set(shown) == WEAR_KEYS and shown["item_ids"] == [1, 2] and shown["worn_on"] == "2026-10-05"
    assert shown["event"] == event_view(other, view()) and session["event"] == event_view(other, view(3))
    assert bare["event"] is None and set(bare) == WEAR_KEYS
    assert "AUDIENCE-PRIVATE" not in json.dumps([shown, session, bare])


def test_an_outfit_view_is_the_id_the_items_and_the_name_never_the_reason_or_the_audiences():
    outfit = OutfitOut(
        id=5,
        name="Outfit · 3 Oct",
        item_ids=[1, 2],
        items=[],
        incomplete=False,
        collage_path="images/collage/secret.webp",
        reason="REASON-PRIVATE",
        occasion_tags=["party"],
        created_at=datetime(2026, 10, 3, 12),
        times_worn=2,
        last_worn=date(2026, 10, 5),
        audiences=["Priya Distinct"],
        rating=1,
    )

    shown = outfit_view(outfit)

    assert set(shown) == OUTFIT_KEYS and shown == {"id": 5, "item_ids": [1, 2], "name": "Outfit · 3 Oct"}


# --- the same allow-lists on what a tool really returns -------------------------------------------------------------


def private_world(engine) -> Any:
    with Session(engine, expire_on_commit=False) as s:
        world = marker_world(s)
        add_wear(s, [world.top_id, world.bottom_id], date(2026, 10, 4))  # a saved outfit that holds the shirt
    return world


def test_every_read_tool_result_has_exactly_its_allow_listed_keys(client, fake, engine):
    world = private_world(engine)
    chat = new_chat(client)
    fake.replies.append(PICK)

    turn(
        client,
        fake,
        chat,
        ask(
            ("search_wardrobe", {}),
            ("get_item", {"item_id": world.top_id}),
            ("get_weather", {}),
            ("get_wear_history", {"item_id": world.top_id, "days": 3650}),
        ),
        ask(("generate_outfits", {"event_id": world.form_event_id})),
        say("done"),
    )
    turn(client, fake, chat, text="and?")  # the turn ended on the looks: the model reads them in this request

    search, item, weather, wear = (parsed for _, parsed in results(fake, 1))
    (generated,) = (parsed for _, parsed in results(fake, 2)[4:])
    assert set(search) == {"total", "items"} and all(set(i) == ITEM_KEYS for i in search["items"])
    assert set(item) == {"item", "wears", "outfits"} and set(item["item"]) == ITEM_KEYS
    assert item["wears"] and all(set(w) == WEAR_KEYS for w in item["wears"])
    assert item["outfits"] and all(set(o) == OUTFIT_KEYS for o in item["outfits"])
    assert set(weather) == {"place", "weather"} and set(weather["weather"]) == WEATHER_KEYS
    assert set(wear) == {"wears"} and wear["wears"] and all(set(w) == WEAR_KEYS for w in wear["wears"])
    assert set(generated) == GENERATE_KEYS and set(generated["event"]) == {*EVENT_BASE_KEYS, "place"}
    assert set(generated["weather"]) == WEATHER_KEYS and generated["problem"] is None
    assert generated["suggestions"] and all(set(s) == SUGGESTION_KEYS for s in generated["suggestions"])


def test_every_write_tool_result_has_exactly_its_allow_listed_keys(client, fake, engine):
    world = private_world(engine)
    ids = [world.top_id, world.bottom_id, world.shoes_id]
    chat = new_chat(client)

    turn(
        client,
        fake,
        chat,
        ask(
            ("create_event", {"type": "party", "starts_at": "2026-10-11T19:00", "title": "Rooftop", "notes": "n"}),
            ("log_wear", {"item_ids": ids}),
            ("update_item_status", {"item_id": world.top_id, "status": "laundry"}),
            ("record_feedback", {"item_ids": ids, "rating": -1, "reason": "too shiny"}),
        ),
        say("done"),
    )

    created, wear_proposal, status_proposal, rated = (parsed for _, parsed in results(fake))
    assert set(created) == SESSION_EVENT_KEYS and created["place"] == HOME_LABEL and created["title"] == "Rooftop"
    assert wear_proposal == status_proposal == PROPOSAL
    assert set(rated) == {"outfit_id", "rating"} and rated["rating"] == -1


def test_an_item_shows_its_user_tags_to_the_model(client, fake, engine):
    with Session(engine, expire_on_commit=False) as s:
        s.add(Item(user_id=OWNER_ID, name="Shirt", category="top", tags=["silky", "my favourite"]))
        s.commit()

    turn(client, fake, new_chat(client), ask(("search_wardrobe", {})), say("ok"))

    ((_, found),) = results(fake)
    assert found["items"][0]["tags"] == ["silky", "my favourite"]


# --- AC-35: no private value reaches the provider -------------------------------------------------------------------


def row_text(engine) -> str:
    return json.dumps(
        [(r.provider, r.model, r.purpose, r.request_summary, r.response, r.error) for r in llm_rows(engine)]
    )


def test_no_private_marker_reaches_the_provider_and_defaults_say_your_home_city(client, fake, engine, caplog):
    caplog.set_level(logging.DEBUG)
    world = private_world(engine)
    chat = new_chat(client)
    fake.replies.extend([PICK, PICK])  # the nested pick of each generate_outfits

    out = turn(
        client,
        fake,
        chat,
        ask(
            ("get_weather", {}),
            ("create_event", {"type": "party", "starts_at": "2026-10-11T19:00"}),  # no place: the home city
            ("get_wear_history", {"item_id": world.top_id, "days": 3650}),
            ("search_wardrobe", {}),
            ("get_item", {"item_id": world.top_id}),
        ),
        ask(("generate_outfits", {"event_id": world.place_event_id + 1})),  # the event just made: the turn ends
    )
    turn(client, fake, chat, ask(("generate_outfits", {"event_id": world.form_event_id})), text="and the other?")

    assert out["outcome"] == "ok"
    weather, created, *_ = (parsed for _, parsed in results(fake, 1))
    assert weather["place"] == HOME_LABEL and created["place"] == HOME_LABEL
    pick_prompts = [c["prompt"] for c in fake.calls]
    assert len(pick_prompts) == 2 and all("Place: your home city, indoors" in p for p in pick_prompts)
    everything = {
        "provider": sent_text(fake),
        "llm rows": row_text(engine),
        "tool results": json.dumps([r.result for r in tool_rows(engine, chat)]),
        "logs": caplog.text,
    }
    for where, text in everything.items():
        assert [m for m in MARKERS if m in text] == [], where


# --- AC-50: events of other chats are type and date only ------------------------------------------------------------


def test_other_events_are_type_and_date_only_and_rest_keeps_full_text(client, fake, engine):
    world = private_world(engine)
    chat = new_chat(client)
    fake.replies.append(PICK)

    turn(
        client,
        fake,
        chat,
        ask(
            ("get_wear_history", {"item_id": world.top_id, "days": 3650}),
            ("get_item", {"item_id": world.top_id}),
        ),
        ask(("generate_outfits", {"event_id": world.form_event_id})),
        say("ok"),
    )

    secrets = (EVENT_TITLE_MARKER, AUDIENCE_MARKER, EVENT_NOTES_MARKER, HOME_CITY_MARKER, PLACE_MARKER, "Brunch")
    assert [m for m in secrets if m in sent_text(fake)] == []
    history, item = (parsed for _, parsed in results(fake, 1)[:2])
    by_day = {w["worn_on"]: w["event"] for w in history["wears"]}
    assert set(by_day["2026-10-05"]) == {*EVENT_BASE_KEYS, "place"} and by_day["2026-10-05"]["place"] == HOME_LABEL
    assert set(by_day["2026-10-03"]) == {*EVENT_BASE_KEYS, "place"}  # an earlier chat's event, in the home city
    assert set(by_day["2026-10-02"]) == EVENT_BASE_KEYS  # an earlier chat's event, somewhere else: no place at all
    assert by_day["2026-10-02"]["type"] == "party"
    assert datetime.fromisoformat(by_day["2026-10-02"]["starts_at"]) == datetime(2026, 10, 2, 19)
    assert [w["event"] for w in item["wears"] if w["event"]]
    (prompt,) = (c["prompt"] for c in fake.calls)
    assert "Place: your home city, indoors" in prompt and EVENT_TITLE_MARKER not in prompt

    rest = client.post("/api/outfits/suggest", json={"event_id": world.form_event_id}).json()["event"]
    assert (rest["title"], rest["notes"], rest["place"]) == (EVENT_TITLE_MARKER, EVENT_NOTES_MARKER, HOME_CITY_MARKER)


def test_place_marker_of_an_earlier_session_never_sent(client, fake, engine):
    world = private_world(engine)
    fake.replies.append(PICK)
    chat = new_chat(client)

    turn(
        client,
        fake,
        chat,
        ask(("get_wear_history", {"item_id": world.top_id, "days": 3650}), ("get_weather", {"place": "Paris"})),
        ask(("generate_outfits", {"event_id": world.place_event_id})),
        say("ok"),
    )
    turn(client, fake, chat, text="and?")  # the turn ended on the looks: the model reads them in this request

    assert PLACE_MARKER not in sent_text(fake) and "Brunch" not in sent_text(fake)
    shown = results(fake, 2)[-1][1]
    assert set(shown["event"]) == EVENT_BASE_KEYS  # not this chat's, not in the home city: no place
    assert shown["suggestions"]
    (prompt,) = (c["prompt"] for c in fake.calls)
    assert "Place: indoors" in prompt and "Event: party\n" in prompt
    assert results(fake, 1)[1][1]["place"] == "Paris"  # a place the model itself named is a place it may see


def test_same_turn_created_event_reaches_tool_result_and_nested_pick(client, fake, engine):
    world = private_world(engine)
    chat = new_chat(client)
    fake.replies.extend([PICK, PICK, PICK])
    made = {
        "type": "party",
        "title": "Rooftop birthday",
        "notes": "bring the red scarf",
        "place": "Khan Market",
        "starts_at": "2026-10-11T19:00",
        "audience_tags": [AUDIENCE_MARKER],
    }
    new_event = world.place_event_id + 1

    # each turn ends on its looks: no reply is scripted after a generate
    turn(client, fake, chat, ask(("create_event", made)), ask(("generate_outfits", {"event_id": new_event})))
    turn(client, fake, chat, ask(("generate_outfits", {"event_id": new_event})), text="once more")
    elsewhere = new_chat(client)
    turn(client, fake, elsewhere, ask(("generate_outfits", {"event_id": new_event})), text="another chat")

    created = results(fake, 1)[0][1]
    assert (created["title"], created["notes"], created["place"]) == (
        "Rooftop birthday",
        "bring the red scarf",
        "Khan Market",
    )
    first, later, other_chat = (c["prompt"] for c in fake.calls)
    for prompt in (first, later):  # the event is this chat's in the same turn, and in every later turn
        assert "Rooftop birthday" in prompt and "bring the red scarf" in prompt and "Khan Market" in prompt
    assert (
        "Rooftop birthday" not in other_chat and "Khan Market" not in other_chat
    )  # another chat's event: type and date
    for text in (first, later, other_chat, json.dumps([parsed for _, parsed in results(fake, 2)])):
        assert AUDIENCE_MARKER not in text  # the audience is in the database, never in what the model is told
    rows = llm_rows(engine, "outfit_pick")
    assert [r.request_summary.split(" images=")[0] for r in rows] == [f"session={chat}"] * 2 + [f"session={elsewhere}"]


# --- AC-36: what the user typed is the user's -----------------------------------------------------------------------


def test_typed_names_reach_the_provider_as_typed_and_stored_audiences_never_added(client, fake, engine):
    world = private_world(engine)
    chat = new_chat(client)
    typed = f"with {AUDIENCE_MARKER} in {HOME_CITY_MARKER}"

    turn(
        client,
        fake,
        chat,
        ask(("get_wear_history", {"item_id": world.top_id, "days": 3650}), ("search_wardrobe", {})),
        say("ok"),
        text=typed,
    )
    turn(client, fake, chat, say("and then"), text="thanks")

    assert fake.chat_calls[0]["messages"][0].text == typed  # exactly as typed
    for call in fake.chat_calls:  # in the history of later requests as well
        assert call["messages"][0].text == typed
    for sent, _ in results(fake, 1):
        assert AUDIENCE_MARKER not in sent.content and HOME_CITY_MARKER not in sent.content
    assert [m for m in (EVENT_TITLE_MARKER, EVENT_NOTES_MARKER, PLACE_MARKER) if m in sent_text(fake)] == []


# --- the home city and the place of a weather request ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("args", "label"),
    [
        ({}, "your home city"),
        ({"place": ""}, "your home city"),
        ({"place": "Zedtown Distinct"}, "your home city"),
        ({"place": "  zedtown distinct  "}, "your home city"),
        ({"place": "Paris"}, "Paris"),
    ],
)
def test_the_weather_place_is_the_place_the_model_sent_or_your_home_city(client, fake, engine, args, label):
    private_world(engine)

    turn(client, fake, new_chat(client), ask(("get_weather", args)), say("ok"))

    ((_, shown),) = results(fake)
    assert shown["place"] == label


@pytest.mark.parametrize(
    ("args", "label"),
    [
        ({}, "your home city"),
        ({"place": " ZEDTOWN distinct"}, "your home city"),
        ({"place": "Khan Market"}, "Khan Market"),
    ],
)
def test_a_created_event_without_a_place_is_in_the_home_city_and_is_called_that(client, fake, engine, args, label):
    private_world(engine)
    body = {"type": "party", "starts_at": "2026-10-11T19:00", **args}

    turn(client, fake, new_chat(client), ask(("create_event", body)), say("ok"))

    ((_, shown),) = results(fake)
    assert shown["place"] == label
    newest = max(client.get("/api/events").json(), key=lambda e: e["id"])
    assert same_place(newest["place"], HOME_CITY_MARKER) is (label == HOME_LABEL)  # stored for real, shown as a label


# --- AC-19 with a real turn: the text of a chat is gone with the chat -----------------------------------------------


def test_deleting_a_chat_removes_the_text_of_a_real_turn_and_the_pick_rows_that_quote_its_events(client, fake, engine):
    user, reply, quoted = "MARKER-USER-7Q3X", "MARKER-REPLY-9K2W", "TITLE-IN-THE-PICK-3F9A"
    with Session(engine, expire_on_commit=False) as s:
        party_wardrobe(s)
    chat = new_chat(client)
    fake.replies.append(PICK)
    made = {"type": "party", "title": quoted, "starts_at": "2026-10-10T20:00", "place": "Khan Market"}

    turn(
        client,
        fake,
        chat,
        ask(("create_event", made)),
        ask(("generate_outfits", {"event_id": 1})),
        say(f"Here you go {reply}"),
        text=f"something for a party {user}",
    )
    before = set(rows_holding(engine, user, reply, quoted))
    chat_rows = len(llm_rows(engine, "chat"))

    assert client.delete(f"/api/chat/sessions/{chat}").status_code == 204

    assert {"chatmessage", "toolcall", "llmcall"} <= before  # the text was in the chat tables and in the pick row
    assert set(rows_holding(engine, user, reply)) == set()  # no row anywhere holds what was typed or answered
    assert set(rows_holding(engine, quoted)) == {
        "event"
    }  # the user's own event stays, with its title, in its own table
    assert len(llm_rows(engine, "chat")) == chat_rows and llm_rows(engine, "outfit_pick") == []  # counts stay in Debug
