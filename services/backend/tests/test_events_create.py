"""`POST /api/events` and `events.create_draft` (LLD 3.1, 3.2; AC-14): an event with no place happens at home."""

import logging
from datetime import datetime

import pytest
from sqlmodel import Session, select

from kyapehnu.core.errors import InvalidInputError
from kyapehnu.user.models import UserProfile
from kyapehnu.user.profile import PROFILE_ID
from kyapehnu.wardrobe import events
from kyapehnu.wardrobe.events import EventDraft
from kyapehnu.wardrobe.models import Event
from tests.fakes import OWNER_ID, ensure_owner
from tests.wear_fixtures import count


@pytest.fixture(autouse=True)
def _owner(engine):
    with Session(engine) as s:
        ensure_owner(s)
        s.commit()


EVENTS = "/api/events"
DINNER = {
    "type": "party",
    "title": "Dinner with Priya",
    "starts_at": "2026-10-10T20:00",
    "place": "Khan Market",
    "dress_code": "smart casual",
    "audience_tags": ["Priya"],
    "notes": "Table for two",
}


def set_home_city(client, city: str) -> None:
    with Session(client.app.state.ctx.engine) as s:
        s.get(UserProfile, PROFILE_ID).home_city = city
        s.commit()


def rows(client) -> list[Event]:
    with Session(client.app.state.ctx.engine) as s:
        return list(s.exec(select(Event)))


def test_an_event_with_every_field_is_created_listed_and_returned_with_its_id(client):
    r = client.post(EVENTS, json=DINNER)

    assert r.status_code == 201
    body = r.json()
    assert (body["type"], body["title"], body["place"], body["dress_code"], body["audience_tags"], body["notes"]) == (
        "party",
        "Dinner with Priya",
        "Khan Market",
        "smart casual",
        ["Priya"],
        "Table for two",
    )
    assert body["starts_at"] == "2026-10-10T20:00:00"
    assert [e["id"] for e in client.get(EVENTS).json()] == [body["id"]]


@pytest.mark.parametrize("place", [None, "", "   "], ids=["omitted", "empty", "blank"])
def test_no_place_is_the_home_city(client, place):
    set_home_city(client, "Jaipur")
    body = DINNER | {"place": place} if place is not None else {k: v for k, v in DINNER.items() if k != "place"}

    r = client.post(EVENTS, json=body)

    assert (r.status_code, r.json()["place"]) == (201, "Jaipur")
    assert [e.place for e in rows(client)] == ["Jaipur"]


def test_a_given_place_is_kept_as_typed_even_when_it_is_the_home_city(client):
    set_home_city(client, "Jaipur")

    r = client.post(EVENTS, json=DINNER | {"place": "jaipur"})

    assert r.json()["place"] == "jaipur"


def test_with_no_place_and_no_home_city_the_event_is_refused_and_not_stored(client):
    set_home_city(client, "   ")

    r = client.post(EVENTS, json={k: v for k, v in DINNER.items() if k != "place"})

    assert (r.status_code, r.json()["code"]) == (400, "invalid_input")
    assert rows(client) == []


@pytest.mark.parametrize(
    "change",
    [
        {"type": "picnic"},
        {"starts_at": "2026-10-10T20:00:00+05:30"},
        {"starts_at": "2026-10-10T20:00", "ends_at": "2026-10-10T19:00"},
        {"title": "x" * 121},
        {"place": "x" * 121},
        {"audience_tags": ["a"] * 21},
        {"notes": "n" * 1001},
    ],
    ids=["type", "offset", "ends_before_start", "title", "place", "audience", "notes"],
)
def test_bad_input_is_422_and_nothing_is_stored(client, change):
    r = client.post(EVENTS, json=DINNER | change)

    assert r.status_code == 422
    assert rows(client) == []


def test_the_audience_is_distinct_and_the_text_is_cleaned(client):
    r = client.post(
        EVENTS, json=DINNER | {"audience_tags": ["Priya", "priya", " Ravi\n"], "title": "Dinner\nwith Priya"}
    )

    assert (r.json()["audience_tags"], r.json()["title"]) == (["Priya", "Ravi"], "Dinner with Priya")


def test_create_draft_stores_through_the_same_rules_and_commits(engine):
    draft = EventDraft(type="office", starts_at=datetime(2026, 10, 12, 9), place=None)

    with Session(engine) as s:
        event = events.create_draft(OWNER_ID, s, draft, "Gurugram")

    with Session(engine) as s:
        assert count(s, Event) == 1
        assert (s.get(Event, event.id).place, s.get(Event, event.id).type) == ("Gurugram", "office")


def test_create_draft_with_an_unusable_home_city_is_an_invalid_input_not_a_crash(engine):
    draft = EventDraft(type="office", starts_at=datetime(2026, 10, 12, 9))

    with Session(engine) as s, pytest.raises(InvalidInputError):
        events.create_draft(OWNER_ID, s, draft, "h" * 121)


def test_creating_an_event_logs_no_text(client, caplog):
    with caplog.at_level(logging.DEBUG):
        client.post(EVENTS, json=DINNER)

    assert not {"Priya", "Khan Market", "Table for two", "Dinner"} & {w for w in caplog.text.split()}


# --- a festival named in the draft makes it a festival, whatever type the chat chose ----------------------------------


def _draft(**fields) -> EventDraft:
    return EventDraft(**{"type": "party", "starts_at": datetime(2026, 10, 29, 18), **fields})


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"title": "Diwali party"}, "festival"),
        ({"notes": "it is EID, dinner at my aunt's"}, "festival"),
        ({"title": "Garba night", "type": "casual"}, "festival"),
        ({"title": "Christmas lunch at the office", "type": "office"}, "festival"),
        ({"title": "Holiday brunch"}, "party"),  # "holi" inside "holiday" is not Holi
        ({"title": "Rooftop dinner", "notes": "bring a jacket"}, "party"),
        ({"title": "Priya's wedding on Diwali", "type": "wedding"}, "wedding"),
    ],
    ids=["diwali title", "eid in notes", "garba over casual", "christmas over office", "holiday", "plain", "wedding"],
)
def test_a_festival_named_in_the_title_or_notes_makes_the_draft_a_festival_except_a_wedding(fields, expected):
    assert events.from_draft(_draft(**fields), "Gurugram").type == expected


def test_the_festival_words_are_whole_words_in_any_letter_case():
    assert events.festival_named("DIWALI!") and events.festival_named("pre-holi lunch")
    assert not events.festival_named("holiday") and not events.festival_named("")


@pytest.mark.parametrize(
    ("place", "expected"),
    [
        (None, "Gurugram"),
        ("", "Gurugram"),
        ("home", "Gurugram"),
        ("At Home", "Gurugram"),
        ("indoors", "Gurugram"),
        ("Outdoors", "Gurugram"),
        ("the venue", "Gurugram"),
        ("in city", "Gurugram"),
        ("home city", "Gurugram"),
        ("Mumbai", "Mumbai"),
        ("Homebush", "Homebush"),
    ],
)
def test_a_setting_word_as_the_place_means_the_home_city(place, expected):
    """The chat sometimes sends "indoors" or "home" as the place; the geocoder finds no such town."""
    assert events.from_draft(_draft(place=place), "Gurugram").place == expected
