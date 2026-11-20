"""Behaviour changed in the end-of-engineering cleanup pass: the race with a hard delete, id and date limits."""

from datetime import date

import httpx
import pytest
from sqlmodel import Session

from kyapehnu.core.config import Weights
from kyapehnu.core.errors import NotFoundError
from kyapehnu.core.schemas import MAX_ROW_ID, clean_line
from kyapehnu.files.local import LocalStore
from kyapehnu.wardrobe import items
from kyapehnu.wardrobe.models import Outfit, WearLog
from kyapehnu.wardrobe.outfits import collage, wear
from kyapehnu.wardrobe.outfits.schemas import WearIn
from kyapehnu.weather import openmeteo
from kyapehnu.weather.openmeteo import WeatherClient
from tests.fakes import OWNER_ID, add_item, add_wear
from tests.wear_fixtures import TODAY, count, stored, three_items

TOO_BIG = 10**20


@pytest.fixture
def s(engine):
    with Session(engine) as session:
        yield session


@pytest.fixture
def ctx(make_ctx, fake_weather):
    c = make_ctx([])
    c.weather = WeatherClient(httpx.Client(transport=httpx.MockTransport(fake_weather), follow_redirects=False), None)
    yield c
    c.weather.close()


def test_cleanup_a_hard_delete_during_the_wear_leaves_no_row_and_no_collage_of_the_deleted_item(
    ctx, s, tmp_path, monkeypatch
):
    LocalStore(tmp_path)  # creates images/
    a, b, c = three_items(s, tmp_path)
    real_render = collage.render

    def render_then_delete(store, user_id, slotted):
        drawn = real_render(store, user_id, slotted)
        with Session(ctx.engine) as other:  # another request deletes an item while this one draws
            items.hard_delete(other, other.get(type(a), a.id), store)
        return drawn

    monkeypatch.setattr(collage, "render", render_then_delete)
    with pytest.raises(NotFoundError):
        wear.wear(OWNER_ID, ctx, s, WearIn(item_ids=[a.id, b.id, c.id]), TODAY)

    assert (count(s, Outfit), count(s, WearLog)) == (0, 0)
    assert stored(tmp_path) == []


def test_cleanup_item_history_is_capped(s, monkeypatch):
    monkeypatch.setattr(wear, "LIST_LIMIT", 2)
    add_item(s)  # item 1: the history of an item that is not there is a NotFoundError
    for day in range(1, 5):
        add_wear(s, [1], date(2026, 9, day))

    wears, outfits = wear.item_history(OWNER_ID, s, 1)

    assert [w.worn_on.day for w in wears] == [4, 3]
    assert len(outfits) == 1


@pytest.mark.parametrize(
    ("starts_at", "ends_at"),
    [
        ("9999-12-30T10:00:00", "9999-12-31T10:00:00"),
        ("9999-12-31T23:00:00", None),
        ("0001-01-01T00:00:00", None),
    ],
)
def test_cleanup_an_event_in_year_9999_or_1_is_a_422(client, starts_at, ends_at):
    event = {"type": "party", "starts_at": starts_at, "place": "Gurugram"}
    if ends_at:
        event["ends_at"] = ends_at

    r = client.post("/api/outfits/suggest", json={"event": event})

    assert r.status_code == 422


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/api/outfits/wear", {"item_ids": [TOO_BIG]}),
        ("POST", "/api/outfits/wear", {"item_ids": [1], "event_id": TOO_BIG}),
        ("POST", "/api/items/status", {"item_ids": [TOO_BIG], "status": "retired"}),
        ("POST", f"/api/items/{TOO_BIG}/worn-today", None),
        ("GET", f"/api/items/{TOO_BIG}", None),
    ],
)
def test_cleanup_an_id_beyond_the_database_range_is_a_422(client, method, path, body):
    r = client.request(method, path, json=body)

    assert r.status_code == 422


def test_cleanup_the_largest_database_id_is_still_a_404_for_a_missing_item(client):
    assert client.get(f"/api/items/{MAX_ROW_ID}").status_code == 404


def test_cleanup_a_worn_on_before_2000_is_a_422(client):
    r = client.post("/api/outfits/wear", json={"item_ids": [1], "worn_on": "0001-01-01"})

    assert r.status_code == 422
    assert any("worn_on cannot be before" in e["msg"] for e in r.json()["detail"])


def test_cleanup_an_infinite_weight_is_rejected():
    with pytest.raises(ValueError, match="finite"):
        Weights(colour=float("inf"))


@pytest.mark.parametrize("separator", ["\x0b", "\x0c", "\x85"])
def test_cleanup_vertical_tab_form_feed_and_next_line_separate_words(separator):
    assert clean_line(f"a{separator}b") == "a b"


def test_cleanup_deeply_nested_json_gives_no_weather(fake_weather):
    nested = b"[" * 100_000 + b"]" * 100_000
    fake_weather.script("forecast", lambda request: httpx.Response(200, content=nested))
    with httpx.Client(transport=httpx.MockTransport(fake_weather), follow_redirects=False) as http:
        weather = openmeteo.WeatherClient(http, None)
        with pytest.raises(openmeteo.WeatherFetchError) as e:
            weather.hourly(openmeteo.FORECAST_URL, {}, ["temperature_2m"], weather.budget())

    assert e.value.outcome == "malformed"
