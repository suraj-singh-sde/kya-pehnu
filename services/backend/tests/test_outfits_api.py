"""Phase 2 HTTP API owned by the wear package: wear, worn-today, saved outfits, events, item history and filters, bulk
laundry, settings values, request limits and the request guards (AC-3, 52-61, 65, 68-69 and 71-74 data paths).

Suggest and swap are WP-5's logic. Here they are tested only as routes: they exist, they validate and limit their
input, and they hand the parsed body to `kyapehnu.wardrobe.outfits.service`. The service is replaced by a recorder.
"""

import logging
import re
from datetime import UTC, date, datetime, time, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import ValidationError
from sqlalchemy import event
from sqlmodel import Session, select

from kyapehnu.agent.models import LlmCall
from kyapehnu.core.config import Prefs
from kyapehnu.core.errors import NotFoundError
from kyapehnu.wardrobe import events
from kyapehnu.wardrobe.events import EventIn
from kyapehnu.wardrobe.models import Event, Item, Outfit, WearLog
from kyapehnu.wardrobe.outfits import service
from kyapehnu.wardrobe.outfits.schemas import Parts, SuggestionOut, SuggestOut
from tests.conftest import LOOPBACK_PEER
from tests.fakes import OWNER_ID, add_item, add_wear, ensure_owner
from tests.wear_fixtures import add_event, count, pictured, stored, three_items

COLLAGE_PATH = re.compile(r"images/1/collage/[0-9a-f]{32}\.webp")
DAY = timedelta(days=1)
EVENT = {"type": "party", "starts_at": "2026-10-10T19:00:00", "place": "Gurugram"}


def _today() -> date:
    return date.today()


@pytest.fixture
def db(client):
    with Session(client.app.state.ctx.engine) as s:
        yield s


@pytest.fixture
def s(engine):
    with Session(engine) as session:
        ensure_owner(session)
        session.commit()
        yield session


def _data_dir(client):
    return client.app.state.settings.data_dir


def _event_on(db, days_from_today: int = 1, **fields) -> Event:
    start = datetime.combine(_today() + days_from_today * DAY, time(19, 0))
    return add_event(db, starts_at=start, **fields)


def _post_wear(client, item_ids, **fields):
    return client.post("/api/outfits/wear", json={"item_ids": item_ids, **fields})


def _rows(client, model):
    """The rows of a table as another session sees them (so only committed data)."""
    with Session(client.app.state.ctx.engine) as other:
        return list(other.exec(select(model).order_by(model.id)).all())


def _no_wear_written(db, client):
    assert (count(db, Outfit), count(db, WearLog)) == (0, 0)
    assert stored(_data_dir(client)) == []


class Recorder:
    """Stands in for a service function: remembers its calls and returns a fixed result."""

    def __init__(self, result):
        self.result = result
        self.calls = []
        self.user_ids = []

    def __call__(self, user_id, ctx, s, body, today):
        self.user_ids.append(user_id)
        self.calls.append((ctx, s, body, today))
        return self.result


@pytest.fixture
def suggest_calls(monkeypatch):
    event = Event(id=7, type="party", title="Dinner", starts_at=datetime(2026, 10, 10, 19), place="Gurugram")
    recorder = Recorder(SuggestOut(event=event, weather=None, suggestions=[], problem=None))
    monkeypatch.setattr(service, "suggest", recorder)
    return recorder


@pytest.fixture
def swap_calls(monkeypatch):
    items = [Item(id=1, name="Shirt", category="top"), Item(id=3, name="Tee", category="top")]
    parts = Parts(colour=1, pattern=1, formality=1, weather=0.5, repetition=1, forgotten=0, preference=0.5)
    result = SuggestionOut(item_ids=[3, 2], items=items, score=0.8, parts=parts, reason="Swapped.", swap=None, notes=[])
    recorder = Recorder(result)
    monkeypatch.setattr(service, "swap", recorder)
    return recorder


# --- EventIn (AC-74): the event fields of a suggest --------------------------------------------------------------

INVALID_EVENT = {
    "unknown_type": {"type": "brunch"},
    "no_start": {"starts_at": None},
    "no_place": {"place": ""},
    "place_of_invisible_characters_only": {"place": "​‮ "},
    "title_over_120": {"title": "t" * 121},
    "place_over_120": {"place": "p" * 121},
    "dress_code_over_120": {"dress_code": "d" * 121},
    "notes_over_1000": {"notes": "n" * 1001},
    "tag_over_40": {"audience_tags": ["x" * 41]},
    "empty_tag": {"audience_tags": [""]},
    "blank_tag": {"audience_tags": ["   "]},
    "21_tags": {"audience_tags": [f"person {i}" for i in range(21)]},
    "start_with_offset": {"starts_at": "2026-10-10T19:00:00+05:30"},
    "start_in_utc": {"starts_at": "2026-10-10T19:00:00Z"},
    "end_with_offset": {"ends_at": "2026-10-10T21:00:00+05:30"},
    "end_equals_start": {"ends_at": "2026-10-10T19:00:00"},
    "end_before_start": {"ends_at": "2026-10-10T18:00:00"},
    "span_over_7_days": {"ends_at": "2026-10-17T19:00:01"},
}
MAX_EVENT = {
    "type": "interview",
    "title": "t" * 120,
    "starts_at": "2026-10-10T19:00:00",
    "ends_at": "2026-10-17T19:00:00",  # exactly 7 days
    "place": "p" * 120,
    "outdoors": True,
    "dress_code": "d" * 120,
    "audience_tags": [f"{'n' * 38}{i:02d}" for i in range(20)],  # 20 tags of 40 characters
    "notes": "n" * 1000,
}


@pytest.mark.parametrize("change", INVALID_EVENT.values(), ids=INVALID_EVENT)
def test_event_in_rejects_every_out_of_bounds_field(change):
    with pytest.raises(ValidationError):
        EventIn.model_validate({**EVENT, **change})


def test_event_in_accepts_every_limit_exactly():
    event = EventIn.model_validate(MAX_EVENT)

    assert (len(event.title), len(event.place), len(event.dress_code), len(event.notes)) == (120, 120, 120, 1000)
    assert len(event.audience_tags) == 20
    assert event.ends_at - event.starts_at == 7 * DAY


def test_event_in_accepts_an_event_that_ends_one_second_after_it_starts():
    event = EventIn.model_validate({**EVENT, "ends_at": "2026-10-10T19:00:01"})

    assert event.ends_at == datetime(2026, 10, 10, 19, 0, 1)


def test_event_in_defaults_are_an_indoor_event_with_no_title_code_audience_or_notes():
    event = EventIn.model_validate(EVENT)

    assert (event.title, event.ends_at, event.outdoors, event.dress_code, event.audience_tags, event.notes) == (
        "",
        None,
        False,
        "",
        [],
        "",
    )


def test_event_in_drops_repeated_names_ignoring_case_and_keeps_the_first_spelling_and_order():
    event = EventIn.model_validate({**EVENT, "audience_tags": ["Priya", " priya", "Ravi", "PRIYA", "ravi "]})

    assert event.audience_tags == ["Priya", "Ravi"]


@pytest.mark.parametrize(
    ("field", "sent", "clean"),
    [
        ("title", "Dinner​\nat Priya", "Dinner at Priya"),
        ("place", "  Gurugram‮ ", "Gurugram"),
        ("dress_code", "smart\tcasual​", "smart casual"),
        ("notes", " first\nsecond​ ", "first\nsecond"),
    ],
)
def test_event_in_cleans_text_before_it_is_kept_or_measured(field, sent, clean):
    assert getattr(EventIn.model_validate({**EVENT, field: sent}), field) == clean


def test_event_in_measures_the_length_after_cleaning():
    event = EventIn.model_validate({**EVENT, "title": "t" * 120 + "​​"})

    assert len(event.title) == 120


# --- events service (AC-52, 60) ----------------------------------------------------------------------------------


def test_create_stores_the_event_as_given_with_the_start_kept_as_a_wall_clock(s, engine):
    data = EventIn.model_validate(
        {**MAX_EVENT, "ends_at": "2026-10-10T22:00:00", "audience_tags": ["Priya", "priya", "Ravi"]}
    )

    created = events.create(OWNER_ID, s, data)

    with Session(engine) as other:
        row = other.get(Event, created.id)
    assert (row.type, row.place, row.outdoors, row.audience_tags) == ("interview", "p" * 120, True, ["Priya", "Ravi"])
    assert (row.starts_at, row.ends_at) == (datetime(2026, 10, 10, 19, 0), datetime(2026, 10, 10, 22, 0))
    assert (row.title, row.dress_code, row.notes) == ("t" * 120, "d" * 120, "n" * 1000)


def test_get_returns_the_event_and_an_unknown_id_is_not_found(s):
    created = events.create(OWNER_ID, s, EventIn.model_validate(EVENT))

    assert events.get(OWNER_ID, s, created.id).id == created.id
    with pytest.raises(NotFoundError):
        events.get(OWNER_ID, s, created.id + 1)


def test_list_events_puts_the_latest_start_first_and_the_newer_row_first_on_a_tie(s):
    early = add_event(s, starts_at=datetime(2026, 10, 1, 10))
    late_first = add_event(s, starts_at=datetime(2026, 10, 9, 10))
    late_second = add_event(s, starts_at=datetime(2026, 10, 9, 10))

    assert [e.id for e in events.list_events(OWNER_ID, s)] == [late_second.id, late_first.id, early.id]


# --- GET /api/events (AC-60) -------------------------------------------------------------------------------------


def test_events_list_returns_past_and_upcoming_events_with_their_audience(client, db):
    past = _event_on(db, -30, title="Old party", audience_tags=["Priya"])
    upcoming = _event_on(db, 10, title="New party", audience_tags=["Ravi", "Meena"], type="wedding")

    r = client.get("/api/events")

    assert r.status_code == 200
    assert [(e["id"], e["title"], e["type"], e["audience_tags"]) for e in r.json()] == [
        (upcoming.id, "New party", "wedding", ["Ravi", "Meena"]),
        (past.id, "Old party", "party", ["Priya"]),
    ]
    assert {"starts_at", "ends_at", "place", "outdoors", "dress_code", "notes", "created_at"} <= set(r.json()[0])


def test_events_list_is_empty_before_any_event(client):
    assert client.get("/api/events").json() == []


@pytest.mark.parametrize("total", [200, 201])
def test_events_list_returns_at_most_the_latest_200(client, db, total):
    base = datetime(2026, 1, 1)
    db.add_all(
        Event(user_id=OWNER_ID, type="party", title=f"e{i}", starts_at=base + timedelta(hours=i), place="Gurugram")
        for i in range(total)
    )
    db.commit()

    titles = [e["title"] for e in client.get("/api/events").json()]

    assert len(titles) == 200
    assert titles[0] == f"e{total - 1}"
    assert titles[-1] == f"e{total - 200}"


# --- POST /api/outfits/wear (AC-2, 53, 53a, 54) ------------------------------------------------------------------


def test_wear_saves_an_outfit_with_a_webp_collage_and_returns_it(client, db):
    top, bottom, shoes = three_items(db, _data_dir(client))

    r = _post_wear(client, [shoes.id, top.id, bottom.id])

    assert r.status_code == 201
    body = r.json()
    assert body["item_ids"] == [top.id, bottom.id, shoes.id]
    assert [i["id"] for i in body["items"]] == body["item_ids"]
    assert (body["incomplete"], body["times_worn"], body["last_worn"], body["audiences"]) == (
        False,
        1,
        _today().isoformat(),
        [],
    )
    assert body["name"] == f"Outfit · {_today().day} {_today():%b}"
    assert COLLAGE_PATH.fullmatch(body["collage_path"])
    with Image.open(_data_dir(client) / body["collage_path"]) as im:
        assert im.format == "WEBP"
    assert "location" not in r.headers  # no single-wear GET exists


def test_the_collage_is_served_with_the_other_images(client, db):
    top, bottom, shoes = three_items(db, _data_dir(client))
    path = _post_wear(client, [top.id, bottom.id, shoes.id]).json()["collage_path"]

    r = client.get(f"/{path}")

    assert (r.status_code, r.headers["content-type"]) == (200, "image/webp")


def test_wearing_the_same_items_in_another_order_reuses_the_outfit(client, db):
    a, b, c = three_items(db, _data_dir(client))

    first = _post_wear(client, [a.id, b.id, c.id])
    second = _post_wear(client, [c.id, a.id, b.id])

    assert (first.status_code, second.status_code) == (201, 201)
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["times_worn"] == 2
    assert (count(db, Outfit), count(db, WearLog)) == (1, 2)


def test_wear_for_an_event_records_its_audience_and_weather_and_names_the_outfit_after_it(client, db, fake_weather):
    top, bottom, shoes = three_items(db, _data_dir(client))
    event = _event_on(db, 1, audience_tags=["Priya"])

    r = _post_wear(client, [top.id, bottom.id, shoes.id], event_id=event.id, reason="Smart and cool")

    assert r.status_code == 201
    assert (r.json()["name"], r.json()["occasion_tags"], r.json()["reason"], r.json()["audiences"]) == (
        f"Party · {_today().day} {_today():%b}",
        ["party"],
        "Smart and cool",
        ["Priya"],
    )
    [log] = _rows(client, WearLog)
    assert (log.event_id, log.audience_tags, log.worn_on) == (event.id, ["Priya"], _today())
    assert log.weather_snapshot["source"] == "forecast"
    assert log.weather_snapshot["feels_like_max_c"] == 26.0
    assert len(fake_weather.calls("forecast")) == 1


@pytest.mark.parametrize(
    ("endpoint", "reply"),
    [("forecast", 500), ("geocode", {"results": []})],
    ids=["weather_fails", "place_not_found"],
)
def test_wear_is_logged_even_when_the_weather_cannot_be_had(client, db, fake_weather, endpoint, reply):
    top, bottom, shoes = three_items(db, _data_dir(client))
    event = _event_on(db, 1)
    fake_weather.script(endpoint, reply)

    r = _post_wear(client, [top.id, bottom.id, shoes.id], event_id=event.id)

    assert r.status_code == 201
    assert [log.weather_snapshot for log in _rows(client, WearLog)] == [None]


def test_wear_without_any_picture_still_succeeds_with_no_collage(client, db):
    a = add_item(db, name="Plain top", category="top")
    b = add_item(db, name="Plain bottom", category="bottom")

    r = _post_wear(client, [a.id, b.id])

    assert (r.status_code, r.json()["collage_path"]) == (201, None)
    assert (count(db, Outfit), count(db, WearLog)) == (1, 1)


def test_wear_records_a_past_date_and_accepts_today(client, db):
    top, bottom, shoes = three_items(db, _data_dir(client))
    ids = [top.id, bottom.id, shoes.id]

    yesterday = _post_wear(client, ids, worn_on=(_today() - DAY).isoformat())
    today = _post_wear(client, ids, worn_on=_today().isoformat())

    assert (yesterday.status_code, today.status_code) == (201, 201)
    assert [log.worn_on for log in _rows(client, WearLog)] == [_today() - DAY, _today()]


def test_wear_accepts_ten_items_and_a_reason_of_1000_characters(client, db):
    ids = [add_item(db, name=f"Item {i}", category="top").id for i in range(10)]

    r = _post_wear(client, ids, reason="r" * 1000)

    assert (r.status_code, len(r.json()["item_ids"]), len(r.json()["reason"])) == (201, 10, 1000)


@pytest.mark.parametrize("unknown", ["item", "event"])
def test_wear_of_an_unknown_item_or_event_is_404_and_writes_nothing(client, db, unknown):
    a, b, _ = three_items(db, _data_dir(client))
    body = {"item_ids": [a.id, 999999]} if unknown == "item" else {"item_ids": [a.id, b.id], "event_id": 999999}

    r = client.post("/api/outfits/wear", json=body)

    assert (r.status_code, r.json()["code"]) == (404, "not_found")
    _no_wear_written(db, client)


INVALID_WEAR = {
    "no_item_ids": {},
    "empty_item_ids": {"item_ids": []},
    "11_item_ids": {"item_ids": list(range(1, 12))},
    "repeated_item_ids": {"item_ids": [1, 1]},
    "zero_item_id": {"item_ids": [0]},
    "text_item_id": {"item_ids": ["a"]},
    "event_id_zero": {"item_ids": [1], "event_id": 0},
    "reason_over_1000": {"item_ids": [1], "reason": "r" * 1001},
    "worn_on_tomorrow": {"item_ids": [1], "worn_on": (_today() + DAY).isoformat()},
    "worn_on_far_future": {"item_ids": [1], "worn_on": "2999-01-01"},
    "worn_on_not_a_date": {"item_ids": [1], "worn_on": "yesterday"},
}


@pytest.mark.parametrize("body", INVALID_WEAR.values(), ids=INVALID_WEAR)
def test_wear_rejects_invalid_input_with_422_and_writes_nothing(client, db, fake_weather, body):
    three_items(db, _data_dir(client))  # item 1 exists, so a missing check would give 201, not 404

    r = client.post("/api/outfits/wear", json=body)

    assert r.status_code == 422
    assert isinstance(r.json()["detail"], list)  # FastAPI's own 422 body
    _no_wear_written(db, client)
    assert fake_weather.requests == []


def test_wear_with_a_future_date_says_so(client, db):
    three_items(db, _data_dir(client))

    r = _post_wear(client, [1], worn_on=(_today() + DAY).isoformat())

    assert any("worn_on cannot be in the future" in e["msg"] for e in r.json()["detail"])


def test_wear_logs_carry_no_audience_names_reason_notes_title_place_or_coordinates(client, db, caplog):
    top, bottom, shoes = three_items(db, _data_dir(client))
    event = _event_on(
        db, 1, audience_tags=["Priya Distinct"], title="Secret dinner", notes="private note", place="Gurugram"
    )
    caplog.set_level(logging.DEBUG)

    r = _post_wear(client, [top.id, bottom.id, shoes.id], event_id=event.id, reason="very private reason")

    assert r.status_code == 201
    logged = "\n".join(caplog.messages).lower()
    for secret in (
        "priya distinct",
        "very private reason",
        "secret dinner",
        "private note",
        "gurugram",
        "28.46",
        "77.03",
    ):
        assert secret not in logged


def _fail_new_writes(client, pragma: str) -> None:
    """Every connection opened from now on runs `pragma` (the pool is dropped so the next request gets one)."""
    engine = client.app.state.ctx.engine

    @event.listens_for(engine, "connect")
    def _limit(dbapi_conn, _record):
        dbapi_conn.execute(pragma)

    engine.dispose()


def test_a_failed_write_logs_no_personal_text_from_its_sql_parameters(client, db, caplog):
    top, bottom, shoes = three_items(db, _data_dir(client))
    event = _event_on(db, 1, audience_tags=["Priya Distinct"], title="Secret dinner", notes="private note")
    _fail_new_writes(client, "PRAGMA query_only=1")
    caplog.set_level(logging.DEBUG)

    r = _post_wear(client, [top.id, bottom.id, shoes.id], event_id=event.id, reason="very private reason")

    assert (r.status_code, r.json()["code"]) == (500, "internal")
    records = caplog.records
    logged = "\n".join([r.getMessage() for r in records] + [str(r.args) for r in records] + [caplog.text]).lower()
    assert "attempt to write a readonly database" in logged  # the failure was logged, only its parameters are not
    for secret in ("very private reason", "priya distinct"):
        assert secret not in logged


def test_wear_on_a_full_database_is_507_and_writes_nothing(client, db):
    top, bottom, shoes = three_items(db, _data_dir(client))
    audience = [f"guest {i} " + "n" * 100 for i in range(100)]  # a wear row of about 10 KB: it needs new pages
    event = _event_on(db, 1, audience_tags=audience)
    page_count = db.connection().exec_driver_sql("PRAGMA page_count").scalar_one()
    db.rollback()
    _fail_new_writes(client, f"PRAGMA max_page_count={page_count}")

    r = _post_wear(client, [top.id, bottom.id, shoes.id], event_id=event.id)

    assert (r.status_code, r.json()["code"]) == (507, "insufficient_storage")
    _no_wear_written(db, client)


# --- POST /api/items/{id}/worn-today (AC-55) ---------------------------------------------------------------------


def test_worn_today_logs_the_item_alone_for_today(client, db):
    item = add_item(db)

    r = client.post(f"/api/items/{item.id}/worn-today")

    assert r.status_code == 201
    body = r.json()
    assert (body["item_ids"], body["outfit_id"], body["event_id"], body["worn_on"], body["audience_tags"]) == (
        [item.id],
        None,
        None,
        _today().isoformat(),
        [],
    )
    assert "location" not in r.headers
    [log] = _rows(client, WearLog)
    assert (log.id, log.weather_snapshot, len(_rows(client, Outfit))) == (body["id"], None, 0)


def test_worn_today_twice_on_the_same_day_keeps_one_log(client, db):
    item = add_item(db)

    first = client.post(f"/api/items/{item.id}/worn-today")
    second = client.post(f"/api/items/{item.id}/worn-today")

    assert (first.status_code, second.status_code) == (201, 200)
    assert second.json()["id"] == first.json()["id"]
    assert len(_rows(client, WearLog)) == 1


def test_worn_today_for_an_unknown_item_is_404_and_writes_nothing(client, db):
    r = client.post("/api/items/999999/worn-today")

    assert (r.status_code, r.json()["code"]) == (404, "not_found")
    assert count(db, WearLog) == 0


# --- GET /api/outfits (AC-56, 68) --------------------------------------------------------------------------------


def test_saved_outfits_show_the_collage_the_times_worn_the_last_date_and_who_it_was_for(client, db):
    top, bottom, shoes = three_items(db, _data_dir(client))
    ids = [top.id, bottom.id, shoes.id]
    for days_ago, name in ((1, "Priya"), (0, "Ravi")):
        event = _event_on(db, -days_ago, audience_tags=[name])
        assert (
            _post_wear(client, ids, event_id=event.id, worn_on=(_today() - days_ago * DAY).isoformat()).status_code
            == 201
        )

    r = client.get("/api/outfits")

    assert r.status_code == 200
    [outfit] = r.json()
    assert (outfit["times_worn"], outfit["last_worn"], outfit["audiences"]) == (
        2,
        _today().isoformat(),
        ["Ravi", "Priya"],
    )
    assert COLLAGE_PATH.fullmatch(outfit["collage_path"])
    assert [i["name"] for i in outfit["items"]] == ["White shirt", "Navy chinos", "Brown loafers"]
    assert all(i["thumb_path"] for i in outfit["items"])


def test_saved_outfits_are_empty_before_any_wear(client):
    assert client.get("/api/outfits").json() == []


@pytest.mark.parametrize("total", [200, 201])
def test_saved_outfits_return_at_most_the_first_200_newest_first(client, db, total):
    item = add_item(db)
    base = datetime(2026, 1, 1, tzinfo=UTC)
    db.add_all(
        Outfit(
            user_id=OWNER_ID, item_ids=[item.id], item_key=f"k{i}", name=f"o{i}", created_at=base + timedelta(minutes=i)
        )
        for i in range(total)
    )
    db.commit()

    names = [o["name"] for o in client.get("/api/outfits").json()]

    assert len(names) == 200
    assert (names[0], names[-1]) == (f"o{total - 1}", f"o{total - 200}")


# --- hard delete: incomplete outfits, ids never reused (AC-3, 65) ------------------------------------------------


def test_hard_deleting_an_item_leaves_its_outfit_incomplete_and_its_id_is_never_reused(client, db):
    data_dir = _data_dir(client)
    top = pictured(db, data_dir, "Top", category="top")
    shoes = pictured(db, data_dir, "Shoes", category="footwear")
    bottom = pictured(db, data_dir, "Bottom", category="bottom")  # the highest id
    top_id, shoes_id, bottom_id = top.id, shoes.id, bottom.id
    outfit = _post_wear(client, [top_id, bottom_id, shoes_id]).json()
    assert (data_dir / outfit["collage_path"]).is_file()

    assert client.delete(f"/api/items/{bottom_id}", params={"hard": True}).status_code == 204

    [listed] = client.get("/api/outfits").json()
    assert (listed["id"], listed["incomplete"], listed["item_ids"]) == (outfit["id"], True, [top_id, shoes_id])
    assert [i["id"] for i in listed["items"]] == [top_id, shoes_id]
    assert (listed["times_worn"], listed["collage_path"]) == (1, None)
    assert stored(data_dir) == []
    newer = add_item(db, name="Newer", category="bottom")
    assert newer.id > bottom_id
    detail = client.get(f"/api/items/{newer.id}").json()
    assert (detail["wears"], detail["outfits"]) == ([], [])
    assert [w["item_ids"] for w in client.get(f"/api/items/{top_id}").json()["wears"]] == [[top_id, shoes_id]]


# --- GET /api/items: not_worn_days (AC-58) -----------------------------------------------------------------------


def _worn(db, item, days_ago, **kw):
    add_wear(db, [item.id], _today() - days_ago * DAY, **kw)


def test_not_worn_days_lists_never_worn_items_and_those_last_worn_that_many_days_ago_or_longer(client, db):
    for name, days_ago in (("10 days", 10), ("70 days", 70), ("60 days", 60), ("59 days", 59), ("never", None)):
        item = add_item(db, name=name)
        if days_ago is not None:
            _worn(db, item, days_ago)
    _worn(db, add_item(db, name="item only, 5 days"), 5, outfit=False)  # Worn today counts as a wear
    add_item(db, name="retired, never", status="retired")

    r = client.get("/api/items", params={"not_worn_days": 60})

    assert r.status_code == 200
    assert [i["name"] for i in r.json()] == ["never", "60 days", "70 days"]  # newest first, like the plain list


def test_not_worn_days_combines_with_the_other_filters(client, db):
    add_item(db, name="laundry top", category="top", status="laundry")
    _worn(db, add_item(db, name="worn laundry top", category="top", status="laundry"), 5)
    add_item(db, name="available top", category="top")
    add_item(db, name="laundry bottom", category="bottom", status="laundry")

    r = client.get("/api/items", params={"not_worn_days": 60, "status": "laundry", "category": "top"})

    assert [i["name"] for i in r.json()] == ["laundry top"]


@pytest.mark.parametrize(("value", "status"), [(0, 422), (-1, 422), (3651, 422), ("many", 422), (1, 200), (3650, 200)])
def test_not_worn_days_must_be_between_1_and_3650(client, value, status):
    assert client.get("/api/items", params={"not_worn_days": value}).status_code == status


# --- GET /api/items/{id}: wears and outfits (AC-57) --------------------------------------------------------------


def test_item_detail_has_the_wears_of_the_item_newest_first_and_the_outfits_it_is_in(client, db):
    a, b, c = add_item(db), add_item(db), add_item(db)
    outfit_wear = add_wear(db, [a.id, b.id], _today() - 9 * DAY, audience=["Priya"])
    _worn(db, a, 2, outfit=False)
    add_wear(db, [b.id, c.id], _today() - DAY)  # an outfit without a

    detail = client.get(f"/api/items/{a.id}").json()

    assert [(w["worn_on"], w["outfit_id"], w["item_ids"], w["audience_tags"]) for w in detail["wears"]] == [
        ((_today() - 2 * DAY).isoformat(), None, [a.id], []),
        ((_today() - 9 * DAY).isoformat(), outfit_wear.outfit_id, [a.id, b.id], ["Priya"]),
    ]
    assert set(detail["wears"][0]) == {"id", "outfit_id", "item_ids", "worn_on", "event_id", "audience_tags"}
    assert [o["id"] for o in detail["outfits"]] == [outfit_wear.outfit_id]
    assert {"item", "siblings", "corrections", "photo_path"} <= set(detail)


def test_item_detail_of_an_item_never_worn_has_no_wears_and_no_outfits(client, db):
    item = add_item(db)

    detail = client.get(f"/api/items/{item.id}").json()

    assert (detail["wears"], detail["outfits"]) == ([], [])


# --- POST /api/items/status (AC-59) ------------------------------------------------------------------------------


def _statuses(db):
    db.expire_all()
    return {i.name: i.status for i in db.exec(select(Item))}


def test_bulk_status_sets_every_item_and_returns_them(client, db):
    ids = [add_item(db, name=f"item {i}").id for i in range(3)]
    add_item(db, name="untouched")

    r = client.post("/api/items/status", json={"item_ids": ids, "status": "laundry"})

    assert r.status_code == 200
    assert sorted(i["id"] for i in r.json()) == sorted(ids)
    assert {i["status"] for i in r.json()} == {"laundry"}
    assert _statuses(db) == {"item 0": "laundry", "item 1": "laundry", "item 2": "laundry", "untouched": "available"}


def test_bulk_status_with_an_unknown_id_is_404_and_changes_nothing(client, db):
    ids = [add_item(db, name=f"item {i}").id for i in range(2)]

    r = client.post("/api/items/status", json={"item_ids": [*ids, 999999], "status": "laundry"})

    assert (r.status_code, r.json()["code"]) == (404, "not_found")
    assert set(_statuses(db).values()) == {"available"}


@pytest.mark.parametrize(
    "body",
    [
        {"item_ids": [1], "status": "bogus"},
        {"item_ids": [1]},
        {"status": "laundry"},
        {"item_ids": [], "status": "laundry"},
        {"item_ids": [1, 1], "status": "laundry"},
        {"item_ids": [0], "status": "laundry"},
        {"item_ids": list(range(1, 202)), "status": "laundry"},
    ],
    ids=["unknown_status", "no_status", "no_ids", "empty_ids", "repeated_ids", "zero_id", "201_ids"],
)
def test_bulk_status_rejects_invalid_input_with_422_and_changes_nothing(client, db, body):
    add_item(db, name="item 1")

    r = client.post("/api/items/status", json=body)

    assert r.status_code == 422
    assert set(_statuses(db).values()) == {"available"}


def test_bulk_status_accepts_200_ids(client, db):
    db.add_all(Item(user_id=OWNER_ID, name=f"item {i}", category="top") for i in range(200))
    db.commit()
    ids = [i.id for i in db.exec(select(Item))]

    r = client.post("/api/items/status", json={"item_ids": ids, "status": "tailor"})

    assert (r.status_code, len(r.json())) == (200, 200)


# --- GET /api/settings (AC-61, 72) -------------------------------------------------------------------------------


def test_settings_report_the_outfit_repetition_and_weather_values_in_effect(client):
    prefs = Prefs()

    body = client.get("/api/settings").json()

    assert body["outfits"] == prefs.outfits.model_dump(mode="json")
    assert body["repetition"] == prefs.repetition.model_dump(mode="json")
    assert body["weather"] == prefs.weather.model_dump(mode="json")
    assert body["home_city"] == prefs.home_city  # the Phase 1 values are still there


def test_settings_show_the_preference_weight_in_effect_normalised_with_the_other_six(client):
    weights = client.get("/api/settings").json()["outfits"]["weights"]

    assert weights["preference"] == pytest.approx(0.10 / 1.10)
    assert sum(weights.values()) == pytest.approx(1.0)


@pytest.fixture
def configured_client(settings, fake, fake_weather):
    from kyapehnu.core.app import create_app

    settings.config_file.write_text(
        "outfits:\n"
        "  weights: {colour: 2, pattern: 2, formality: 2, weather: 2, repetition: 1, forgotten: 1, preference: 1}\n"
        "  per_slot: 5\n"
        "repetition:\n"
        "  forgotten_days: 30\n"
        "weather:\n"
        "  rain_probability_pct: 55\n"
    )
    app = create_app(settings=settings, transport=fake, weather_transport=httpx.MockTransport(fake_weather))
    with TestClient(app, base_url="http://localhost", client=LOOPBACK_PEER) as c:
        yield c


def test_settings_report_config_overrides_with_the_weights_normalised(configured_client):
    body = configured_client.get("/api/settings").json()

    assert body["outfits"]["weights"] == pytest.approx(
        {
            "colour": 2 / 11,
            "pattern": 2 / 11,
            "formality": 2 / 11,
            "weather": 2 / 11,
            "repetition": 1 / 11,
            "forgotten": 1 / 11,
            "preference": 1 / 11,
        }
    )
    assert (body["outfits"]["per_slot"], body["outfits"]["max_candidates"]) == (5, 300)
    assert (body["repetition"]["forgotten_days"], body["repetition"]["half_life_days"]) == (30, 14)
    assert (body["weather"]["rain_probability_pct"], body["weather"]["uv_index"]) == (55, 8)


@pytest.mark.parametrize("method", ["PUT", "PATCH", "POST", "DELETE"])
def test_there_is_no_route_that_changes_the_settings_values(client, method):
    r = client.request(method, "/api/settings", json={"outfits": {"per_slot": 3}})

    assert (r.status_code, r.json()["code"]) == (405, "http_405")


# --- POST /api/outfits/suggest and /swap: the routes only (WP-5 owns the logic) ----------------------------------


def test_suggest_hands_the_parsed_body_the_context_a_session_and_today_to_the_service(client, suggest_calls):
    before = _today()

    r = client.post(
        "/api/outfits/suggest", json={"event": {**EVENT, "audience_tags": ["Priya", "priya"]}, "exclude": [[1, 2], [3]]}
    )

    after = _today()
    assert r.status_code == 200
    [(ctx, session, body, today)] = suggest_calls.calls
    assert ctx is client.app.state.ctx
    assert isinstance(session, Session)
    assert suggest_calls.user_ids == [OWNER_ID]  # the signed-in user, not a value from the request
    assert (body.event_id, body.exclude) == (None, [[1, 2], [3]])
    assert (body.event.audience_tags, body.event.starts_at) == (["Priya"], datetime(2026, 10, 10, 19))
    assert before <= today <= after
    assert (r.json()["event"]["id"], r.json()["suggestions"], r.json()["weather"], r.json()["problem"]) == (
        7,
        [],
        None,
        None,
    )


def test_suggest_with_an_event_id_passes_it_on(client, suggest_calls):
    client.post("/api/outfits/suggest", json={"event_id": 7})

    [(_, _, body, _)] = suggest_calls.calls
    assert (body.event_id, body.event, body.exclude) == (7, None, [])


def test_suggest_accepts_every_limit_exactly(client, suggest_calls):
    r = client.post("/api/outfits/suggest", json={"event": MAX_EVENT, "exclude": [[i] for i in range(1, 51)]})

    assert r.status_code == 200
    [(_, _, body, _)] = suggest_calls.calls
    assert (len(body.exclude), len(body.event.audience_tags)) == (50, 20)


INVALID_SUGGEST = {
    "neither_event_nor_event_id": {},
    "both_event_and_event_id": {"event": EVENT, "event_id": 1},
    "event_id_zero": {"event_id": 0},
    "51_exclude_lists": {"event_id": 1, "exclude": [[i] for i in range(1, 52)]},
    "empty_exclude_list": {"event_id": 1, "exclude": [[]]},
    "exclude_list_of_11_ids": {"event_id": 1, "exclude": [list(range(1, 12))]},
    "exclude_list_with_a_repeat": {"event_id": 1, "exclude": [[1, 1]]},
    "exclude_list_with_zero": {"event_id": 1, "exclude": [[0]]},
    **{f"event_{name}": {"event": {**EVENT, **change}} for name, change in INVALID_EVENT.items()},
}


@pytest.mark.parametrize("body", INVALID_SUGGEST.values(), ids=INVALID_SUGGEST)
def test_suggest_rejects_invalid_input_before_any_event_weather_or_ai_call(
    client, db, fake, fake_weather, suggest_calls, body
):
    r = client.post("/api/outfits/suggest", json=body)

    assert r.status_code == 422
    assert isinstance(r.json()["detail"], list)
    assert suggest_calls.calls == []
    assert count(db, Event) == 0
    assert fake_weather.requests == []
    assert (fake.calls, count(db, LlmCall)) == ([], 0)


SWAP = {"event_id": 3, "item_ids": [1, 2], "item_id": 2}


def test_swap_hands_the_parsed_body_to_the_service_and_returns_its_suggestion(client, swap_calls):
    before = _today()

    r = client.post("/api/outfits/swap", json={**SWAP, "exclude_item_ids": [5, 6]})

    after = _today()
    assert r.status_code == 200
    [(ctx, session, body, today)] = swap_calls.calls
    assert ctx is client.app.state.ctx
    assert isinstance(session, Session)
    assert (body.event_id, body.item_ids, body.item_id, body.exclude_item_ids) == (3, [1, 2], 2, [5, 6])
    assert before <= today <= after
    assert (r.json()["item_ids"], r.json()["reason"], r.json()["swap"], r.json()["notes"]) == (
        [3, 2],
        "Swapped.",
        None,
        [],
    )


def test_swap_accepts_ten_items_and_100_excluded_items(client, swap_calls):
    r = client.post(
        "/api/outfits/swap",
        json={"event_id": 3, "item_ids": list(range(1, 11)), "item_id": 10, "exclude_item_ids": list(range(11, 111))},
    )

    assert r.status_code == 200
    assert (len(swap_calls.calls[0][2].item_ids), len(swap_calls.calls[0][2].exclude_item_ids)) == (10, 100)


INVALID_SWAP = {
    "no_event_id": {"item_ids": [1, 2], "item_id": 2},
    "event_id_zero": {**SWAP, "event_id": 0},
    "no_item_ids": {"event_id": 3, "item_id": 2},
    "empty_item_ids": {**SWAP, "item_ids": []},
    "11_item_ids": {**SWAP, "item_ids": list(range(1, 12))},
    "repeated_item_ids": {**SWAP, "item_ids": [2, 2]},
    "zero_in_item_ids": {**SWAP, "item_ids": [0, 2]},
    "no_item_id": {"event_id": 3, "item_ids": [1, 2]},
    "item_id_zero": {**SWAP, "item_id": 0},
    "101_excluded_items": {**SWAP, "exclude_item_ids": list(range(10, 111))},
    "zero_excluded_item": {**SWAP, "exclude_item_ids": [0]},
}


@pytest.mark.parametrize("body", INVALID_SWAP.values(), ids=INVALID_SWAP)
def test_swap_rejects_invalid_input_before_any_weather_or_ai_call(client, db, fake, fake_weather, swap_calls, body):
    r = client.post("/api/outfits/swap", json=body)

    assert r.status_code == 422
    assert isinstance(r.json()["detail"], list)
    assert swap_calls.calls == []
    assert fake_weather.requests == []
    assert (fake.calls, count(db, LlmCall)) == ([], 0)


# --- request guards on every new POST route (AC-73) --------------------------------------------------------------

NEW_POSTS = {
    "suggest": ("/api/outfits/suggest", {"event_id": 1}),
    "swap": ("/api/outfits/swap", SWAP),
    "wear": ("/api/outfits/wear", {"item_ids": [1]}),
    "worn_today": ("/api/items/1/worn-today", None),
    "bulk_status": ("/api/items/status", {"item_ids": [1], "status": "laundry"}),
}
OVER_THE_CAP = b"x" * (1024 * 1024 + 1)


def _nothing_happened(client, db, suggest_calls, swap_calls):
    assert suggest_calls.calls == [] and swap_calls.calls == []
    assert (count(db, WearLog), count(db, Outfit), count(db, Event)) == (0, 0, 0)
    db.expire_all()
    assert db.get(Item, 1).status == "available"


@pytest.fixture
def guarded(client, db, suggest_calls, swap_calls):
    three_items(db, _data_dir(client))  # item 1 exists, so a request that got through would change something
    return client


@pytest.mark.parametrize(("path", "body"), NEW_POSTS.values(), ids=NEW_POSTS)
def test_a_foreign_origin_is_403_on_every_new_post_route(guarded, db, suggest_calls, swap_calls, path, body):
    r = guarded.post(path, json=body, headers={"origin": "http://evil.example"})

    assert (r.status_code, r.json()["code"]) == (403, "cross_origin")
    _nothing_happened(guarded, db, suggest_calls, swap_calls)


@pytest.mark.parametrize(("path", "body"), NEW_POSTS.values(), ids=NEW_POSTS)
def test_a_bad_host_is_400_on_every_new_post_route(guarded, db, suggest_calls, swap_calls, path, body):
    r = guarded.post(path, json=body, headers={"host": "evil.example"})

    assert (r.status_code, r.json()["code"]) == (400, "invalid_host")
    _nothing_happened(guarded, db, suggest_calls, swap_calls)


@pytest.mark.parametrize(("path", "body"), NEW_POSTS.values(), ids=NEW_POSTS)
def test_a_body_over_the_json_cap_is_413_on_every_new_post_route(guarded, db, suggest_calls, swap_calls, path, body):
    r = guarded.post(path, content=OVER_THE_CAP, headers={"content-type": "application/json"})

    assert (r.status_code, r.json()["code"]) == (413, "payload_too_large")
    _nothing_happened(guarded, db, suggest_calls, swap_calls)


# With an empty body each route answers with its own validation or lookup error, which proves it was reached.
LAN_ROUTES = {
    "suggest": ("POST", "/api/outfits/suggest", {}, 422),
    "swap": ("POST", "/api/outfits/swap", {}, 422),
    "wear": ("POST", "/api/outfits/wear", {}, 422),
    "worn_today": ("POST", "/api/items/999999/worn-today", None, 404),
    "bulk_status": ("POST", "/api/items/status", {}, 422),
    "list_outfits": ("GET", "/api/outfits", None, 200),
    "list_events": ("GET", "/api/events", None, 200),
}


@pytest.mark.parametrize(("method", "path", "body", "status"), LAN_ROUTES.values(), ids=LAN_ROUTES)
@pytest.mark.parametrize(("public", "expected"), [(True, None), (False, 400)], ids=["public_host", "other_host"])
def test_new_routes_are_reachable_from_the_public_host_only(
    settings, fake, fake_weather, kv, method, path, body, status, public, expected
):
    from kyapehnu.core.app import create_app

    configured = settings.model_copy(
        update={"public_url": "http://192.168.1.20:8000" if public else "http://localhost"}
    )
    app = create_app(settings=configured, transport=fake, weather_transport=httpx.MockTransport(fake_weather), kv=kv)

    with TestClient(app, base_url="http://localhost") as c:
        r = c.request(method, path, json=body, headers={"host": "192.168.1.20:8000"})

    assert r.status_code == (status if expected is None else expected)
    if expected is not None:
        assert r.json()["code"] == "invalid_host"
