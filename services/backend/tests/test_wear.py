"""Wear log, saved outfits, history and the collage (AC-2, 3, 53-57, 63, 65, and the data side of AC-4 and 24-29).

The services run against a real SQLite database (the `engine` fixture) and a scripted Open-Meteo (`fake_weather`).
`today` is passed in, so none of these tests depends on the clock.
"""

import logging
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from PIL import Image
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlmodel import Session, select

from kyapehnu.core.config import RepetitionPrefs
from kyapehnu.core.errors import NotFoundError
from kyapehnu.files.local import LocalStore
from kyapehnu.files.public import StoreUnavailableError
from kyapehnu.wardrobe import items
from kyapehnu.wardrobe.models import Item, Outfit, WearLog
from kyapehnu.wardrobe.outfits import collage, wear
from kyapehnu.wardrobe.outfits.schemas import WearIn
from kyapehnu.wardrobe.outfits.slots import Slot
from kyapehnu.weather.openmeteo import WeatherClient
from tests.fakes import OWNER_ID, add_item, add_wear
from tests.wear_fixtures import (
    BACKGROUND,
    BLUE,
    GREEN,
    PURPLE,
    RED,
    TODAY,
    YELLOW,
    add_event,
    count,
    pictured,
    stored,
    three_items,
    write_picture,
)

COLLAGE_PATH = re.compile(r"images/1/collage/[0-9a-f]{32}\.webp")
DAY = timedelta(days=1)
FORECAST_SNAPSHOT = {  # what the default FakeOpenMeteo gives for an event tomorrow evening (19:00 to 22:00)
    "source": "forecast",
    "feels_like_min_c": 26.0,
    "feels_like_max_c": 26.0,
    "humidity_mean_pct": 60.0,
    "rain_probability_max_pct": 10.0,
    "wind_max_kmh": 10.0,
    "uv_max": 5.0,
    "aqi_max": 80.0,
}


@pytest.fixture
def s(engine):
    with Session(engine) as session:
        yield session


@pytest.fixture
def ctx(make_ctx, fake_weather):
    """A pipeline Ctx over the test database with a weather client behind the scripted Open-Meteo."""
    c = make_ctx([])
    c.weather = WeatherClient(httpx.Client(transport=httpx.MockTransport(fake_weather), follow_redirects=False), None)
    yield c
    c.weather.close()


@pytest.fixture
def data_dir(tmp_path):
    LocalStore(tmp_path)  # creates images/
    return tmp_path


def _wear(ctx, s, item_ids, today=TODAY, **fields):
    return wear.wear(OWNER_ID, ctx, s, WearIn(item_ids=item_ids, **fields), today)


def _fresh(engine, model):
    with Session(engine) as other:
        return list(other.exec(select(model).order_by(model.id)).all())


def _footwear_first(s, data_dir):
    """Footwear, bottom, top: the ids ascend in the opposite order of the slots."""
    shoes = pictured(s, data_dir, "Brown loafers", GREEN, category="footwear")
    bottom = pictured(s, data_dir, "Navy chinos", BLUE, category="bottom")
    top = pictured(s, data_dir, "White shirt", RED, category="top")
    return top, bottom, shoes


# --- item_key -----------------------------------------------------------------------------------------------


def test_item_key_sorts_the_ids_numerically_not_as_text():
    assert wear.item_key([40, 3, 12]) == "3,12,40"


# --- wear: a new outfit (AC-53) -------------------------------------------------------------------------------


def test_wearing_a_new_set_saves_one_outfit_with_its_items_in_slot_order(ctx, s, data_dir):
    top, bottom, shoes = _footwear_first(s, data_dir)
    ids = sorted([top.id, bottom.id, shoes.id])

    out = _wear(ctx, s, [shoes.id, bottom.id, top.id], reason="Looks sharp")

    [row] = _fresh(ctx.engine, Outfit)
    assert row.id == out.id
    assert row.item_ids == [top.id, bottom.id, shoes.id]
    assert row.item_key == ",".join(str(i) for i in ids)
    assert (row.name, row.created_by, row.occasion_tags, row.reason) == ("Outfit · 6 Oct", "agent", [], "Looks sharp")


def test_wearing_logs_one_wear_for_today_with_no_event_audience_or_weather(ctx, s, data_dir):
    top, bottom, shoes = _footwear_first(s, data_dir)

    out = _wear(ctx, s, [shoes.id, bottom.id, top.id])

    [log] = _fresh(ctx.engine, WearLog)
    assert (log.outfit_id, log.item_ids, log.worn_on) == (out.id, [top.id, bottom.id, shoes.id], TODAY)
    assert (log.event_id, log.audience_tags, log.weather_snapshot) == (None, [], None)


def test_the_returned_outfit_describes_the_saved_one(ctx, s, data_dir):
    top, bottom, shoes = _footwear_first(s, data_dir)

    out = _wear(ctx, s, [shoes.id, bottom.id, top.id])

    assert out.item_ids == [top.id, bottom.id, shoes.id]
    assert [i.id for i in out.items] == out.item_ids
    assert (out.incomplete, out.times_worn, out.last_worn, out.audiences) == (False, 1, TODAY, [])
    assert COLLAGE_PATH.fullmatch(out.collage_path)
    assert (data_dir / out.collage_path).is_file()
    assert _fresh(ctx.engine, Outfit)[0].collage_path == out.collage_path


def test_wearing_for_an_event_records_its_audience_and_the_weather_snapshot(ctx, s, data_dir, fake_weather):
    top, bottom, shoes = three_items(s, data_dir)
    event = add_event(s, audience_tags=["Priya", "Ravi"])

    _wear(ctx, s, [top.id, bottom.id, shoes.id], event_id=event.id)

    [log] = _fresh(ctx.engine, WearLog)
    assert (log.event_id, log.audience_tags) == (event.id, ["Priya", "Ravi"])
    assert log.weather_snapshot == FORECAST_SNAPSHOT
    assert len(fake_weather.calls("forecast")) == 1


def test_a_new_outfit_for_an_event_is_named_and_tagged_after_it(ctx, s, data_dir):
    top, bottom, shoes = three_items(s, data_dir)
    event = add_event(s, type="wedding")

    _wear(ctx, s, [top.id, bottom.id, shoes.id], event_id=event.id)

    [row] = _fresh(ctx.engine, Outfit)
    assert (row.name, row.occasion_tags) == ("Wedding · 6 Oct", ["wedding"])


def test_a_past_worn_on_is_recorded_as_given(ctx, s, data_dir):
    top, bottom, shoes = three_items(s, data_dir)

    _wear(ctx, s, [top.id, bottom.id, shoes.id], worn_on=TODAY - 3 * DAY)

    assert [log.worn_on for log in _fresh(ctx.engine, WearLog)] == [TODAY - 3 * DAY]


@pytest.mark.parametrize(
    ("endpoint", "reply"),
    [
        ("forecast", 500),
        ("geocode", {"results": []}),
        ("geocode", httpx.ReadTimeout("slow")),
    ],
    ids=["forecast_fails", "place_not_found", "geocode_times_out"],
)
def test_a_wear_is_logged_without_a_snapshot_when_the_weather_is_unavailable(
    ctx, s, data_dir, fake_weather, endpoint, reply
):
    top, bottom, shoes = three_items(s, data_dir)
    event = add_event(s, audience_tags=["Priya"])
    fake_weather.script(endpoint, reply)

    _wear(ctx, s, [top.id, bottom.id, shoes.id], event_id=event.id)

    [log] = _fresh(ctx.engine, WearLog)
    assert (log.weather_snapshot, log.audience_tags, log.event_id) == (None, ["Priya"], event.id)


# --- wear: reuse by item_key (AC-2) --------------------------------------------------------------------------


def test_wearing_the_same_items_in_another_order_reuses_the_saved_outfit(ctx, s, data_dir):
    a, b, c = three_items(s, data_dir)
    saved = add_wear(s, [a.id, b.id, c.id], TODAY - 3 * DAY).outfit_id

    out = _wear(ctx, s, [c.id, a.id, b.id])

    assert out.id == saved
    assert [row.id for row in _fresh(ctx.engine, Outfit)] == [saved]
    assert [log.outfit_id for log in _fresh(ctx.engine, WearLog)] == [saved, saved]
    assert (out.times_worn, out.last_worn) == (2, TODAY)


def test_a_reused_outfit_without_a_collage_gets_one(ctx, s, data_dir):
    a, b, c = three_items(s, data_dir)
    add_wear(s, [a.id, b.id, c.id], TODAY - DAY)  # the helper saves the outfit without a collage

    out = _wear(ctx, s, [a.id, b.id, c.id])

    assert COLLAGE_PATH.fullmatch(out.collage_path)
    assert _fresh(ctx.engine, Outfit)[0].collage_path == out.collage_path
    assert stored(data_dir) == [Path(out.collage_path).name]


def test_a_reused_outfit_keeps_its_collage_and_draws_no_new_one(ctx, s, data_dir, monkeypatch):
    a, b, c = three_items(s, data_dir)
    first = _wear(ctx, s, [a.id, b.id, c.id], worn_on=TODAY - DAY)
    monkeypatch.setattr(collage, "render", lambda *_a, **_k: pytest.fail("a second collage was drawn"))

    again = _wear(ctx, s, [c.id, b.id, a.id])

    assert again.collage_path == first.collage_path
    assert stored(data_dir) == [Path(first.collage_path).name]


def test_a_concurrent_wear_of_the_same_set_reuses_the_winner_and_drops_its_own_collage(ctx, s, data_dir, monkeypatch):
    a, b, c = three_items(s, data_dir)
    winner_rel = write_picture(data_dir, "images/1/collage/winner.webp")
    ours_rel = write_picture(data_dir, "images/1/collage/ours.webp")

    def racing_render(_data_dir, _user_id, _items):  # another request saves the same set while this one draws
        with Session(ctx.engine) as other:
            key = ",".join(str(i) for i in sorted([a.id, b.id, c.id]))
            other.add(
                Outfit(
                    user_id=OWNER_ID, item_ids=[a.id, b.id, c.id], item_key=key, name="Winner", collage_path=winner_rel
                )
            )
            other.commit()
        return ours_rel

    monkeypatch.setattr(collage, "render", racing_render)

    out = _wear(ctx, s, [a.id, b.id, c.id])

    [row] = _fresh(ctx.engine, Outfit)
    assert (out.id, row.name, row.collage_path) == (row.id, "Winner", winner_rel)
    assert [log.outfit_id for log in _fresh(ctx.engine, WearLog)] == [row.id]
    assert stored(data_dir) == ["winner.webp"]


def test_a_concurrent_wear_whose_winner_has_no_collage_gives_it_ours_and_keeps_the_file(ctx, s, data_dir, monkeypatch):
    a, b, c = three_items(s, data_dir)
    ours_rel = write_picture(data_dir, "images/1/collage/ours.webp")

    def racing_render(
        _data_dir, _user_id, _items
    ):  # another request saves the same set, without a collage, while this draws
        with Session(ctx.engine) as other:
            key = ",".join(str(i) for i in sorted([a.id, b.id, c.id]))
            other.add(Outfit(user_id=OWNER_ID, item_ids=[a.id, b.id, c.id], item_key=key, name="Winner"))
            other.commit()
        return ours_rel

    monkeypatch.setattr(collage, "render", racing_render)

    out = _wear(ctx, s, [a.id, b.id, c.id])

    [row] = _fresh(ctx.engine, Outfit)
    assert (out.id, row.name, row.collage_path) == (row.id, "Winner", ours_rel)
    assert out.collage_path == ours_rel
    assert [log.outfit_id for log in _fresh(ctx.engine, WearLog)] == [row.id]
    assert stored(data_dir) == ["ours.webp"]


# --- wear: nothing is written on failure (AC-54) -----------------------------------------------------------------


@pytest.mark.parametrize("unknown", ["item", "event"])
def test_an_unknown_item_or_event_is_not_found_and_nothing_is_written(ctx, s, data_dir, unknown):
    a, b, _ = three_items(s, data_dir)
    fields = {"item_ids": [a.id, 9999]} if unknown == "item" else {"item_ids": [a.id, b.id], "event_id": 9999}

    with pytest.raises(NotFoundError):
        wear.wear(OWNER_ID, ctx, s, WearIn(**fields), TODAY)

    assert (count(s, Outfit), count(s, WearLog)) == (0, 0)
    assert stored(data_dir) == []


def test_a_failed_commit_removes_the_collage_it_drew(ctx, s, data_dir, monkeypatch):
    a, b, c = three_items(s, data_dir)

    def failing_commit(_self):
        raise OperationalError("COMMIT", {}, Exception("disk I/O error"))

    with monkeypatch.context() as patched:
        patched.setattr(Session, "commit", failing_commit)
        with pytest.raises(OperationalError):
            _wear(ctx, s, [a.id, b.id, c.id])

    assert stored(data_dir) == []
    assert (len(_fresh(ctx.engine, Outfit)), len(_fresh(ctx.engine, WearLog))) == (0, 0)


def test_a_commit_failing_for_another_reason_than_the_race_is_raised_and_removes_the_collage(
    ctx, s, data_dir, monkeypatch
):
    a, b, c = three_items(s, data_dir)

    def failing_commit(_self):  # a constraint failure, but no outfit with this set exists: not the item_key race
        raise IntegrityError("COMMIT", {}, Exception("FOREIGN KEY constraint failed"))

    with monkeypatch.context() as patched:
        patched.setattr(Session, "commit", failing_commit)
        with pytest.raises(IntegrityError):
            _wear(ctx, s, [a.id, b.id, c.id])

    assert stored(data_dir) == []
    assert (len(_fresh(ctx.engine, Outfit)), len(_fresh(ctx.engine, WearLog))) == (0, 0)


def test_a_failed_commit_raises_the_original_error_when_the_collage_cannot_be_removed(
    ctx, s, data_dir, monkeypatch, caplog
):
    a, b, c = three_items(s, data_dir)
    real_delete = LocalStore.delete

    def failing_commit(_self):
        raise OperationalError("COMMIT", {}, Exception("disk I/O error"))

    def delete(self, key):
        if "/collage/" in key:
            raise PermissionError("in use")
        real_delete(self, key)

    caplog.set_level(logging.WARNING)
    with monkeypatch.context() as patched:
        patched.setattr(Session, "commit", failing_commit)
        patched.setattr(LocalStore, "delete", delete)
        with pytest.raises(OperationalError, match="disk I/O error"):
            _wear(ctx, s, [a.id, b.id, c.id])

    assert any("event=files_delete_failed" in m and "PermissionError" in m for m in caplog.messages)
    assert (len(_fresh(ctx.engine, Outfit)), len(_fresh(ctx.engine, WearLog))) == (0, 0)


def test_a_wear_succeeds_with_no_collage_when_no_item_has_a_picture(ctx, s, data_dir):
    a = add_item(s, name="Plain top", category="top")
    b = add_item(s, name="Plain bottom", category="bottom")

    out = _wear(ctx, s, [a.id, b.id])

    assert out.collage_path is None
    assert (count(s, Outfit), count(s, WearLog)) == (1, 1)
    assert stored(data_dir) == []


# --- worn_today (AC-55) ---------------------------------------------------------------------------------------


def test_worn_today_logs_one_item_with_no_outfit_no_event_and_no_snapshot(s, engine):
    item = add_item(s)

    out, created = wear.worn_today(OWNER_ID, s, item.id, TODAY)
    assert created

    assert (out.item_ids, out.outfit_id, out.event_id, out.worn_on, out.audience_tags) == (
        [item.id],
        None,
        None,
        TODAY,
        [],
    )
    [log] = _fresh(engine, WearLog)
    assert (log.id, log.weather_snapshot) == (out.id, None)
    assert count(s, Outfit) == 0


def test_worn_today_for_an_unknown_item_is_not_found_and_writes_nothing(s):
    with pytest.raises(NotFoundError):
        wear.worn_today(OWNER_ID, s, 9999, TODAY)

    assert count(s, WearLog) == 0


# --- list_outfits (AC-56) -------------------------------------------------------------------------------------


def test_an_outfit_worn_twice_shows_the_count_the_last_date_and_both_audiences(s):
    a, b = add_item(s), add_item(s)
    add_wear(s, [a.id, b.id], TODAY - 10 * DAY, audience=["Priya"])
    add_wear(s, [a.id, b.id], TODAY - 3 * DAY, audience=["Ravi"])

    [out] = wear.list_outfits(OWNER_ID, s)

    assert (out.times_worn, out.last_worn, out.audiences) == (2, TODAY - 3 * DAY, ["Ravi", "Priya"])


def test_audiences_ignore_case_and_keep_the_spelling_of_the_most_recent_wear(s):
    a, b = add_item(s), add_item(s)
    add_wear(s, [a.id, b.id], TODAY - 5 * DAY, audience=["priya", "Ravi"])
    add_wear(s, [a.id, b.id], TODAY - DAY, audience=["PRIYA"])

    [out] = wear.list_outfits(OWNER_ID, s)

    assert out.audiences == ["PRIYA", "Ravi"]


def test_an_item_only_wear_does_not_count_for_the_outfit(s):
    a, b = add_item(s), add_item(s)
    add_wear(s, [a.id, b.id], TODAY - 7 * DAY)
    add_wear(s, [a.id, b.id], TODAY - DAY, outfit=False)

    [out] = wear.list_outfits(OWNER_ID, s)

    assert (out.times_worn, out.last_worn) == (1, TODAY - 7 * DAY)


def test_outfits_are_listed_by_last_worn_then_newest_with_never_worn_ones_last(s):
    item = add_item(s)

    def saved(key: str, created_day: int, worn: int | None) -> None:
        row = Outfit(
            user_id=OWNER_ID,
            item_ids=[item.id],
            item_key=key,
            name=key,
            created_at=datetime(2026, 10, created_day, tzinfo=UTC),
        )
        s.add(row)
        s.flush()
        if worn is not None:
            s.add(WearLog(user_id=OWNER_ID, outfit_id=row.id, item_ids=[item.id], worn_on=TODAY - worn * DAY))

    saved("long ago", 1, worn=10)
    saved("recent", 2, worn=2)
    saved("never, newer", 5, worn=None)
    saved("never, older", 3, worn=None)
    s.commit()

    assert [o.name for o in wear.list_outfits(OWNER_ID, s)] == ["recent", "long ago", "never, newer", "never, older"]


def test_a_never_worn_outfit_has_no_last_worn_and_zero_wears(s):
    item = add_item(s)
    s.add(Outfit(user_id=OWNER_ID, item_ids=[item.id], item_key=str(item.id), name="Unworn"))
    s.commit()

    [out] = wear.list_outfits(OWNER_ID, s)

    assert (out.times_worn, out.last_worn, out.audiences) == (0, None, [])


# --- item_history, last_worn_by_item (AC-57, 58) ---------------------------------------------------------------


def test_item_history_has_the_wears_of_the_item_newest_first_and_the_outfits_it_is_in(s):
    a, b, c = add_item(s), add_item(s), add_item(s)
    add_wear(s, [a.id, b.id], TODAY - 9 * DAY, audience=["Priya"])
    wear.worn_today(OWNER_ID, s, a.id, TODAY - 2 * DAY)
    add_wear(s, [b.id, c.id], TODAY - DAY)  # not about a

    wears, outfits = wear.item_history(OWNER_ID, s, a.id)

    assert [(w.worn_on, w.outfit_id is None, w.item_ids) for w in wears] == [
        (TODAY - 2 * DAY, True, [a.id]),
        (TODAY - 9 * DAY, False, [a.id, b.id]),
    ]
    assert wears[1].audience_tags == ["Priya"]
    assert [o.item_ids for o in outfits] == [[a.id, b.id]]


def test_item_history_of_an_item_never_worn_is_empty(s):
    item = add_item(s)

    assert wear.item_history(OWNER_ID, s, item.id) == ([], [])


def test_last_worn_by_item_takes_the_latest_wear_of_each_item_item_only_wears_included(s):
    a, b, never = add_item(s), add_item(s), add_item(s)
    add_wear(s, [a.id, b.id], TODAY - 30 * DAY)
    add_wear(s, [a.id], TODAY - 4 * DAY, outfit=False)

    assert wear.last_worn_by_item(OWNER_ID, s) == {a.id: TODAY - 4 * DAY, b.id: TODAY - 30 * DAY}
    assert never.id not in wear.last_worn_by_item(OWNER_ID, s)


# --- load_history (AC-24 to AC-29, data side) --------------------------------------------------------------------


def _history(s, audience=()):
    return wear.load_history(OWNER_ID, s, audience, TODAY, RepetitionPrefs())


def test_history_last_worn_covers_every_wear_item_only_wears_included(s):
    a, b = add_item(s), add_item(s)
    add_wear(s, [a.id, b.id], TODAY - 20 * DAY)
    add_wear(s, [a.id], TODAY - 2 * DAY, outfit=False)

    assert _history(s).last_worn == {a.id: TODAY - 2 * DAY, b.id: TODAY - 20 * DAY}


def test_history_outfit_wears_leave_out_item_only_wears_and_keep_the_latest_date_per_set(s):
    a, b, c = add_item(s), add_item(s), add_item(s)
    add_wear(s, [a.id, b.id], TODAY - 20 * DAY)
    add_wear(s, [b.id, a.id], TODAY - 8 * DAY)
    add_wear(s, [a.id, b.id], TODAY - DAY, outfit=False)  # AC-28: no effect on the outfit's own history
    add_wear(s, [c.id], TODAY - 3 * DAY, outfit=False)

    assert _history(s).outfit_last_worn == {frozenset({a.id, b.id}): TODAY - 8 * DAY}


def test_history_audience_wears_are_the_outfit_wears_that_share_a_name_with_the_event(s):
    a, b, c = add_item(s), add_item(s), add_item(s)
    add_wear(s, [a.id, b.id], TODAY - 5 * DAY, audience=["Priya"])
    add_wear(s, [b.id, c.id], TODAY - 6 * DAY, audience=["Ravi"])
    add_wear(s, [a.id, c.id], TODAY - 7 * DAY)
    add_wear(s, [a.id], TODAY - DAY, audience=["Priya"], outfit=False)

    assert _history(s, ["Priya"]).audience_wears == [(frozenset({a.id, b.id}), TODAY - 5 * DAY)]


def test_history_matches_names_ignoring_case_and_surrounding_spaces(s):
    a, b = add_item(s), add_item(s)
    add_wear(s, [a.id, b.id], TODAY - 5 * DAY, audience=["PRIYA"])

    assert _history(s, ["  priya "]).audience_wears == [(frozenset({a.id, b.id}), TODAY - 5 * DAY)]


def test_history_lists_a_wear_once_when_several_names_match(s):
    a, b = add_item(s), add_item(s)
    add_wear(s, [a.id, b.id], TODAY - 5 * DAY, audience=["Priya", "Ravi"])

    assert _history(s, ["Priya", "Ravi"]).audience_wears == [(frozenset({a.id, b.id}), TODAY - 5 * DAY)]


@pytest.mark.parametrize(("age_days", "kept"), [(90, True), (91, False)])
def test_history_audience_window_is_ninety_days_inclusive(s, age_days, kept):
    a, b = add_item(s), add_item(s)
    add_wear(s, [a.id, b.id], TODAY - age_days * DAY, audience=["Priya"])

    assert bool(_history(s, ["Priya"]).audience_wears) is kept


def test_history_has_no_audience_wears_when_the_event_names_nobody(s):
    a, b = add_item(s), add_item(s)
    add_wear(s, [a.id, b.id], TODAY - 5 * DAY, audience=["Priya"])

    assert _history(s, []).audience_wears == []


def test_history_audience_window_follows_the_prefs(s):
    a, b = add_item(s), add_item(s)
    add_wear(s, [a.id, b.id], TODAY - 31 * DAY, audience=["Priya"])

    narrow = wear.load_history(OWNER_ID, s, ["Priya"], TODAY, RepetitionPrefs(audience_window_days=30))

    assert narrow.audience_wears == []


# --- incomplete_keys (AC-4, data side) ---------------------------------------------------------------------------


def test_incomplete_keys_are_the_remaining_ids_of_outfits_that_lost_an_item(s):
    a, b, c = add_item(s), add_item(s), add_item(s)
    add_wear(s, [a.id, b.id, c.id], TODAY - DAY)  # complete
    add_wear(s, [a.id, 9999, c.id], TODAY - DAY)  # 9999 was deleted

    assert wear.incomplete_keys(OWNER_ID, s, {a.id, b.id, c.id}) == {frozenset({a.id, c.id})}


def test_incomplete_keys_are_empty_when_every_outfit_is_complete(s):
    a, b = add_item(s), add_item(s)
    add_wear(s, [a.id, b.id], TODAY - DAY)

    assert wear.incomplete_keys(OWNER_ID, s, {a.id, b.id}) == set()


# --- hard delete (AC-3, AC-65) ---------------------------------------------------------------------------------


def _worn_outfit(ctx, s, data_dir):
    a, b, c = three_items(s, data_dir)
    ids = (a.id, b.id, c.id)
    return (*ids, _wear(ctx, s, list(ids)))


def test_hard_delete_keeps_the_outfit_and_its_wear_and_lists_the_outfit_as_incomplete(ctx, s, data_dir):
    a, b, c, out = _worn_outfit(ctx, s, data_dir)

    items.hard_delete(s, s.get(Item, b), ctx.files)

    [listed] = wear.list_outfits(OWNER_ID, s)
    assert (listed.id, listed.incomplete, listed.item_ids) == (out.id, True, [a, c])
    assert [i.id for i in listed.items] == [a, c]
    assert listed.times_worn == 1
    assert (count(s, Outfit), count(s, WearLog)) == (1, 1)


def test_hard_delete_clears_and_removes_the_collage_of_outfits_that_show_the_item_only(ctx, s, data_dir):
    a, b, c, with_b = _worn_outfit(ctx, s, data_dir)
    without_b = _wear(ctx, s, [a, c])
    assert stored(data_dir) == sorted(Path(o.collage_path).name for o in (with_b, without_b))

    items.hard_delete(s, s.get(Item, b), ctx.files)

    rows = {row.id: row for row in _fresh(ctx.engine, Outfit)}
    assert rows[with_b.id].collage_path is None
    assert not (data_dir / with_b.collage_path).exists()
    assert rows[without_b.id].collage_path == without_b.collage_path
    assert (data_dir / without_b.collage_path).is_file()


def test_hard_delete_matches_item_ids_exactly_so_item_1_leaves_an_outfit_holding_item_11_alone(ctx, s, data_dir):
    for n in range(10):
        add_item(s, name=f"Filler {n}")
    one = s.exec(select(Item).order_by(Item.id)).first()
    top = pictured(s, data_dir, "Eleven top", RED, category="top")
    bottom = pictured(s, data_dir, "Twelve bottom", BLUE, category="bottom")
    assert (one.id, top.id, bottom.id) == (1, 11, 12)
    out = _wear(ctx, s, [top.id, bottom.id])

    items.hard_delete(s, s.get(Item, one.id), ctx.files)

    [row] = _fresh(ctx.engine, Outfit)
    assert row.collage_path == out.collage_path
    assert (data_dir / out.collage_path).is_file()


def test_hard_delete_still_completes_when_a_collage_file_cannot_be_removed(ctx, s, data_dir, monkeypatch, caplog):
    a, b, c, out = _worn_outfit(ctx, s, data_dir)
    real_delete = LocalStore.delete

    def delete(self, key):
        if "/collage/" in key:
            raise PermissionError("in use")
        real_delete(self, key)

    monkeypatch.setattr(LocalStore, "delete", delete)
    caplog.set_level(logging.WARNING)

    items.hard_delete(s, s.get(Item, b), ctx.files)

    assert s.get(Item, b) is None
    assert _fresh(ctx.engine, Outfit)[0].collage_path is None
    assert any("event=files_delete_failed" in m and "PermissionError" in m for m in caplog.messages)


def test_detach_item_returns_the_collage_files_and_leaves_the_commit_and_the_files_to_the_caller(ctx, s, data_dir):
    a, b, c, out = _worn_outfit(ctx, s, data_dir)
    _wear(ctx, s, [a, c])

    rels = wear.detach_item(OWNER_ID, s, b)

    assert rels == [out.collage_path]
    assert (data_dir / out.collage_path).is_file()
    assert s.get(Outfit, out.id).collage_path is None
    assert _fresh(ctx.engine, Outfit)[0].collage_path == out.collage_path  # not committed


def test_a_new_item_never_takes_the_id_of_a_hard_deleted_one(ctx, s, data_dir):
    a = pictured(s, data_dir, "A top", RED, category="top")
    c = pictured(s, data_dir, "C shoes", GREEN, category="footwear")
    b = pictured(s, data_dir, "B bottom", BLUE, category="bottom")  # the highest id
    a_id, b_id, c_id = a.id, b.id, c.id
    _wear(ctx, s, [a_id, b_id, c_id])
    items.hard_delete(s, s.get(Item, b_id), ctx.files)

    n = add_item(s, name="N", category="bottom")

    assert n.id > b_id
    [outfit] = wear.list_outfits(OWNER_ID, s)
    assert (outfit.incomplete, outfit.item_ids) == (True, [a_id, c_id])
    assert wear.item_history(OWNER_ID, s, n.id) == ([], [])
    assert n.id not in wear.last_worn_by_item(OWNER_ID, s)
    stored_ids = {i for row in (*s.exec(select(Outfit)), *s.exec(select(WearLog))) for i in row.item_ids}
    assert stored_ids <= {a_id, b_id, c_id}


# --- collage (AC-63) ---------------------------------------------------------------------------------------------


def _slotted(data_dir, *specs):
    """(slot, colour) pairs as unsaved items with a solid thumbnail, in the order given."""
    out = []
    for n, (slot, rgb) in enumerate(specs, start=1):
        rel = write_picture(data_dir, f"images/1/thumb/p{n}.webp", rgb)
        out.append((slot, Item(id=n, name=f"p{n}", category="top", thumb_path=rel)))
    return out


def _pixel(data_dir, rel, xy):
    with Image.open(data_dir / rel) as im:
        return im.convert("RGB").getpixel(xy)


def test_the_collage_is_an_800_by_800_webp_in_the_collage_folder(data_dir):
    rel = collage.render(LocalStore(data_dir), OWNER_ID, _slotted(data_dir, (Slot.top, RED)))

    assert COLLAGE_PATH.fullmatch(rel)
    with Image.open(data_dir / rel) as im:
        assert (im.format, im.size) == ("WEBP", (800, 800))
    assert stored(data_dir) == [Path(rel).name]


def test_the_collage_puts_each_slot_in_its_cell(data_dir):
    pieces = _slotted(
        data_dir,
        (Slot.top, RED),
        (Slot.bottom, BLUE),
        (Slot.outerwear, YELLOW),
        (Slot.footwear, GREEN),
        (Slot.accessory, PURPLE),
    )

    rel = collage.render(LocalStore(data_dir), OWNER_ID, pieces)

    cells = {  # left column: outerwear above top; right column: bottom, footwear, then the accessory
        (200, 200): YELLOW,
        (200, 600): RED,
        (600, 150): BLUE,
        (600, 450): GREEN,
        (600, 700): PURPLE,
    }
    for xy, rgb in cells.items():
        assert _pixel(data_dir, rel, xy) == pytest.approx(rgb, abs=14), xy
    assert _pixel(data_dir, rel, (3, 3)) == pytest.approx(BACKGROUND, abs=6)


def test_without_outerwear_or_accessory_the_columns_split_the_full_height(data_dir):
    pieces = _slotted(data_dir, (Slot.top, RED), (Slot.bottom, BLUE), (Slot.footwear, GREEN))

    rel = collage.render(LocalStore(data_dir), OWNER_ID, pieces)

    for xy, rgb in {(200, 400): RED, (600, 200): BLUE, (600, 600): GREEN}.items():
        assert _pixel(data_dir, rel, xy) == pytest.approx(rgb, abs=14), xy


def test_a_full_piece_takes_the_place_of_the_top(data_dir):
    rel = collage.render(LocalStore(data_dir), OWNER_ID, _slotted(data_dir, (Slot.full, YELLOW)))

    assert _pixel(data_dir, rel, (200, 400)) == pytest.approx(YELLOW, abs=14)


def test_the_collage_shows_the_thumbnail_and_falls_back_to_the_cutout(data_dir):
    thumb = write_picture(data_dir, "images/1/thumb/t.webp", RED)
    cutout = write_picture(data_dir, "images/1/cutout/c.png", BLUE)
    both = Item(id=1, name="both", category="top", thumb_path=thumb, cutout_path=cutout)
    only_cutout = Item(id=2, name="cutout only", category="bottom", cutout_path=cutout)

    rel = collage.render(LocalStore(data_dir), OWNER_ID, [(Slot.top, both), (Slot.bottom, only_cutout)])

    assert _pixel(data_dir, rel, (200, 400)) == pytest.approx(RED, abs=14)
    assert _pixel(data_dir, rel, (600, 400)) == pytest.approx(BLUE, abs=14)


def test_transparent_parts_of_a_cutout_show_the_background(data_dir):
    cutout = Image.new("RGBA", (200, 200), (255, 0, 0, 255))
    cutout.paste((255, 0, 0, 0), (0, 0, 100, 200))  # the left half is see-through
    (data_dir / "images/1/cutout").mkdir(parents=True, exist_ok=True)
    cutout.save(data_dir / "images/1/cutout/c.png")
    item = Item(id=1, name="cutout", category="top", cutout_path="images/1/cutout/c.png")

    rel = collage.render(LocalStore(data_dir), OWNER_ID, [(Slot.top, item)])

    assert _pixel(data_dir, rel, (100, 400)) == pytest.approx(BACKGROUND, abs=6)
    assert _pixel(data_dir, rel, (300, 400)) == pytest.approx((255, 0, 0), abs=14)


@pytest.mark.parametrize("path", [None, "images/1/thumb/missing.webp"], ids=["no_path", "file_missing"])
def test_an_item_without_a_usable_picture_is_skipped_and_the_others_are_drawn(data_dir, path):
    pieces = _slotted(data_dir, (Slot.top, RED), (Slot.bottom, BLUE))
    pieces[0][1].thumb_path = path

    rel = collage.render(LocalStore(data_dir), OWNER_ID, pieces)

    assert _pixel(data_dir, rel, (200, 400)) == pytest.approx(BACKGROUND, abs=6)
    assert _pixel(data_dir, rel, (600, 400)) == pytest.approx(BLUE, abs=14)


def test_nothing_is_drawn_when_no_item_has_a_picture(data_dir):
    item = Item(id=1, name="plain", category="top")

    assert collage.render(LocalStore(data_dir), OWNER_ID, [(Slot.top, item)]) is None
    assert stored(data_dir) == []


def test_a_truncated_picture_is_skipped_and_the_others_are_drawn(data_dir):
    pieces = _slotted(data_dir, (Slot.top, RED), (Slot.bottom, BLUE))
    noise = Image.effect_noise((200, 200), 80).convert("RGB")
    broken = data_dir / "images/1/thumb/broken.png"
    noise.save(broken)
    broken.write_bytes(broken.read_bytes()[: broken.stat().st_size // 2])
    pieces[0][1].thumb_path = "images/1/thumb/broken.png"

    rel = collage.render(LocalStore(data_dir), OWNER_ID, pieces)

    assert _pixel(data_dir, rel, (600, 400)) == pytest.approx(BLUE, abs=14)


def test_a_picture_that_is_too_large_to_open_is_skipped(data_dir, monkeypatch):
    pieces = _slotted(data_dir, (Slot.top, RED))

    def too_big(*_a, **_k):
        raise Image.DecompressionBombError("too many pixels")

    monkeypatch.setattr(Image, "open", too_big)

    assert collage.render(LocalStore(data_dir), OWNER_ID, pieces) is None
    assert stored(data_dir) == []


def test_a_failing_encode_returns_none_and_stores_nothing(data_dir, monkeypatch, caplog):
    pieces = _slotted(data_dir, (Slot.top, RED))

    def failing_save(_image, _fp, *_a, **_k):
        raise ValueError("encoder failed")

    monkeypatch.setattr(Image.Image, "save", failing_save)
    caplog.set_level(logging.INFO)

    assert collage.render(LocalStore(data_dir), OWNER_ID, pieces) is None
    assert stored(data_dir) == []
    assert any("event=collage_failed" in m and "ValueError" in m for m in caplog.messages)


def test_a_store_that_cannot_take_the_collage_returns_none_instead_of_failing_the_wear(data_dir, monkeypatch, caplog):
    pieces = _slotted(data_dir, (Slot.top, RED))

    def down(self, key, body):
        raise StoreUnavailableError

    monkeypatch.setattr(LocalStore, "put", down)
    caplog.set_level(logging.INFO)

    assert collage.render(LocalStore(data_dir), OWNER_ID, pieces) is None
    assert stored(data_dir) == []
    assert any("event=collage_failed" in m and "StoreUnavailableError" in m for m in caplog.messages)


def test_a_store_that_cannot_be_read_ends_the_drawing_at_the_first_item(data_dir, monkeypatch, caplog):
    pieces = _slotted(data_dir, (Slot.top, RED), (Slot.bottom, BLUE))
    caplog.set_level(logging.INFO)

    def down(self, key):
        raise StoreUnavailableError

    monkeypatch.setattr(LocalStore, "open", down)

    assert collage.render(LocalStore(data_dir), OWNER_ID, pieces) is None
    assert stored(data_dir) == []
    assert not any("event=collage_item_skipped" in m for m in caplog.messages)
    assert sum("event=collage_failed" in m and "StoreUnavailableError" in m for m in caplog.messages) == 1
