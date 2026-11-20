"""POST /api/outfits/suggest and /swap end to end (AC-4, 30-40, 45, 46, 50, 51, 75, 76, 80, 82, 85 and the data the
Outfits page shows, AC-66 and 67).

The AI is a scripted `FakeTransport` and Open-Meteo is `FakeOpenMeteo`: no test touches the network.
"""

import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from datetime import time as clock

import httpx
import pytest
from sqlmodel import Session, select

from kyapehnu.agent.chat.privacy import ChatView
from kyapehnu.agent.llm.base import LLMUnavailableError, RawResult
from kyapehnu.agent.models import LlmCall
from kyapehnu.wardrobe.models import Event, Feedback, Item, Outfit, WearLog
from kyapehnu.wardrobe.outfits import service
from kyapehnu.wardrobe.outfits.schemas import SuggestIn
from tests.fake_openmeteo import HOSTS
from tests.fakes import OWNER_ID, add_item, add_wear
from tests.wear_fixtures import add_event, count

DAY = timedelta(days=1)
RAIN_NOTE = "Rain likely: rain-safe footwear."
PICK_LINE = re.compile(r"^#(\d+) .*$", re.MULTILINE)
SEVEN_PARTS = {"colour", "pattern", "formality", "weather", "repetition", "forgotten", "preference"}


def starts(days: int = 1) -> str:
    return datetime.combine(date.today() + days * DAY, clock(19, 0)).isoformat()


def event_body(**fields) -> dict:
    return {"type": "party", "starts_at": starts(), "place": "Gurugram", **fields}


@pytest.fixture
def db(client):
    with Session(client.app.state.ctx.engine) as s:
        yield s


@pytest.fixture
def plain(client, monkeypatch):
    """The same app with no AI provider configured (AC-34)."""
    monkeypatch.setattr(client.app.state.ctx, "settings", None)
    return client


def wardrobe(db, tops: int = 3, bottoms: int = 3, shoes: int = 3, **common) -> dict[str, list[Item]]:
    """Available items every party in mild weather accepts, so tops x bottoms x shoes outfits exist."""
    fields = {"formality": 3, "warmth": 2, **common}
    made = {"top": [], "bottom": [], "footwear": []}
    for category, n in (("top", tops), ("bottom", bottoms), ("footwear", shoes)):
        for k in range(n):
            made[category].append(
                add_item(
                    db,
                    name=f"{category} {k}",
                    category=category,
                    thumb_path=f"images/thumb/{category}{k}.webp",
                    colors=[{"hex": "#ffffff", "name": "white"}],
                    **fields,
                )
            )
    return made


def pick_reply(*picks: tuple) -> dict:
    """picks: (candidate_number, reason) or (candidate_number, reason, swap_slot, swap_item_id)."""
    padded = [(*p, *(None,) * (4 - len(p))) for p in picks]
    return {
        "picks": [
            {"candidate_number": n, "reason": reason, "swap_slot": slot, "swap_item_id": swap}
            for n, reason, slot, swap in padded
        ]
    }


def prompt_candidates(prompt: str) -> dict[int, set[int]]:
    """The item ids of every numbered candidate line in a prompt."""
    return {int(m.group(1)): set(map(int, re.findall(r"\[id (\d+)\]", m.group(0)))) for m in PICK_LINE.finditer(prompt)}


def shown(body: dict) -> list[list[int]]:
    return [s["item_ids"] for s in body["suggestions"]]


def suggest(client, **body):
    return client.post("/api/outfits/suggest", json=body)


# --- the response (AC-51, 66) --------------------------------------------------------------------------------------


def test_suggest_creates_the_event_and_returns_three_outfits_with_everything_a_card_shows(plain, db, fake_weather):
    made = wardrobe(db)

    r = suggest(plain, event=event_body(title="Dinner", audience_tags=["Priya"], notes="rooftop"))

    body = r.json()
    assert r.status_code == 200
    assert [e.title for e in db.exec(select(Event))] == ["Dinner"]
    assert (body["event"]["id"], body["event"]["audience_tags"]) == (db.exec(select(Event)).one().id, ["Priya"])
    assert body["weather"]["summary"]["source"] == "forecast" and body["weather"]["notes"] == []
    assert body["problem"] is None and len(body["suggestions"]) == 3
    available = {i.id for items in made.values() for i in items}
    for s in body["suggestions"]:
        assert set(s) == {"item_ids", "items", "score", "parts", "reason", "swap", "notes", "rating"}
        assert set(s["parts"]) == SEVEN_PARTS
        assert s["rating"] is None and s["parts"]["preference"] == 0.5  # nothing rated yet: neutral
        assert set(s["item_ids"]) <= available and [i["id"] for i in s["items"]] == s["item_ids"]
        assert all(i["thumb_path"] for i in s["items"]) and 0 <= s["score"] <= 1
        assert s["reason"].startswith("Picked because") and s["swap"] is None and s["notes"] == []
    assert len({tuple(sorted(i)) for i in shown(body)}) == 3


def test_an_outfit_lists_its_items_in_slot_order(plain, db):
    wardrobe(db)

    body = suggest(plain, event=event_body()).json()

    for s in body["suggestions"]:
        assert [i["category"] for i in s["items"]] == ["top", "bottom", "footwear"]


def test_suggest_with_an_event_id_reuses_the_event(plain, db):
    wardrobe(db)
    first = suggest(plain, event=event_body()).json()["event"]

    r = suggest(plain, event_id=first["id"])

    assert r.status_code == 200 and r.json()["event"] == first and count(db, Event) == 1


def test_an_unknown_event_id_is_404_and_nothing_is_fetched_or_written(client, db, fake, fake_weather):
    r = suggest(client, event_id=999)

    assert (r.status_code, r.json()["code"]) == (404, "not_found")
    assert count(db, Event) == 0 and fake_weather.requests == [] and fake.calls == []


def test_the_weather_notes_and_unsafe_footwear_are_shown_and_sent_to_the_model(client, db, fake, fake_weather):
    wardrobe(db, shoes=0)
    add_item(db, name="Canvas shoes", category="footwear", subcategory="sneakers", fabric="canvas", warmth=2)
    fake_weather.values["precipitation_probability"] = 60.0
    fake.replies = [pick_reply((1, "ok"))]

    body = suggest(client, event=event_body()).json()

    assert RAIN_NOTE in body["weather"]["notes"]
    assert all(s["notes"] == ["footwear_not_rain_safe"] for s in body["suggestions"])  # AC-50
    assert RAIN_NOTE in fake.calls[0]["prompt"]


# --- the pick: the model, and the fallback when it fails (AC-30 to 35, 82, 85) -------------------------------------


def test_the_models_valid_picks_become_the_suggestions_with_their_reasons_and_the_call_is_logged(client, db, fake):
    wardrobe(db)
    fake.replies = [pick_reply((2, "Navy anchors it."), (5, "Calm colours."), (9, "Easy."))]

    r = suggest(client, event=event_body())

    candidates = prompt_candidates(fake.calls[0]["prompt"])
    body = r.json()
    assert len(candidates) == 5  # the shortlist; #9 is no candidate, so that pick is dropped and nothing fills in
    assert [set(i) for i in shown(body)] == [candidates[2], candidates[5]]
    assert [s["reason"] for s in body["suggestions"]] == ["Navy anchors it.", "Calm colours."]
    [row] = [r for r in db.exec(select(LlmCall)).all() if r.purpose == "outfit_pick"]
    assert (row.purpose, row.error) == ("outfit_pick", None) and "Navy anchors it." in row.response


def test_an_unknown_and_a_duplicate_number_are_dropped_and_nothing_fills_in(client, db, fake):
    wardrobe(db)
    fake.replies = [pick_reply((2, "a"), (2, "again"), (99, "nope"))]

    body = suggest(client, event=event_body()).json()

    candidates = prompt_candidates(fake.calls[0]["prompt"])
    assert len(body["suggestions"]) == 1 and set(shown(body)[0]) == candidates[2]


def test_a_swap_naming_an_item_in_laundry_is_dropped_and_every_item_is_available(client, db, fake):
    made = wardrobe(db)
    laundry = add_item(db, name="Tee in laundry", category="top", status="laundry", formality=3, warmth=2)
    retired = add_item(db, name="Old tee", category="top", status="retired", formality=3, warmth=2)
    fake.replies = [pick_reply((1, "a", "top", laundry.id), (2, "b", "top", retired.id), (3, "c", "top", 12345))]

    body = suggest(client, event=event_body()).json()

    used = {i for s in body["suggestions"] for i in s["item_ids"]}
    assert all(s["swap"] is None for s in body["suggestions"])
    assert used <= {i.id for items in made.values() for i in items} and laundry.id not in used


def test_a_valid_swap_is_shown_as_a_hint_and_not_applied(client, db, fake):
    made = wardrobe(db)
    fake.replies = [pick_reply((1, "a"))]
    first = suggest(client, event=event_body()).json()
    own = set(first["suggestions"][0]["item_ids"])
    spare = next(t for t in made["top"] if t.id not in own)
    fake.replies = [pick_reply((1, "a", "top", spare.id))]

    body = suggest(client, event_id=first["event"]["id"]).json()

    hint = body["suggestions"][0]["swap"]
    assert hint["slot"] == "top" and hint["item_id"] == spare.id and hint["item"]["id"] == spare.id
    assert spare.id not in body["suggestions"][0]["item_ids"]


@pytest.mark.parametrize(
    "replies",
    [[LLMUnavailableError("down")], [RuntimeError("boom")], ["not json", '{"picks": 3}']],
    ids=["unavailable", "unexpected_error", "invalid_output_twice"],
)
def test_a_failing_provider_still_gives_three_outfits_and_a_failed_call_row(client, db, fake, replies):
    wardrobe(db)
    fake.replies = list(replies)

    r = suggest(client, event=event_body())

    assert r.status_code == 200 and len(r.json()["suggestions"]) == 3
    assert all(s["reason"].startswith("Picked because") for s in r.json()["suggestions"])
    rows = db.exec(select(LlmCall)).all()
    assert rows and {row.purpose for row in rows} == {"outfit_pick"} and all(row.error for row in rows)  # AC-82


def test_with_no_provider_configured_three_outfits_come_back_and_no_call_is_attempted(plain, db, fake):
    wardrobe(db)

    r = suggest(plain, event=event_body())

    assert r.status_code == 200 and len(r.json()["suggestions"]) == 3
    assert fake.calls == [] and count(db, LlmCall) == 0


# --- weather (AC-45, 46) -------------------------------------------------------------------------------------------


def test_an_unknown_place_is_422_with_a_code_and_no_event_or_ai_call(client, db, fake, fake_weather):
    wardrobe(db)
    fake_weather.script("geocode", {"results": []})

    r = suggest(client, event=event_body(place="Nowhereville"))

    assert (r.status_code, r.json()["code"]) == (422, "place_not_found")
    assert count(db, Event) == 0 and fake.calls == []


@pytest.mark.parametrize(
    ("endpoint", "reply"),
    [
        ("forecast", 500),
        ("forecast", httpx.ReadTimeout("slow")),
        ("forecast", b"not json"),
        ("geocode", httpx.ConnectTimeout("slow")),
    ],
    ids=["forecast_500", "forecast_timeout", "forecast_malformed", "geocode_timeout"],
)
def test_a_weather_failure_gives_a_200_with_no_weather_and_the_event_is_kept(plain, db, fake_weather, endpoint, reply):
    wardrobe(db)
    fake_weather.script(endpoint, reply)

    r = suggest(plain, event=event_body())

    assert r.status_code == 200 and r.json()["weather"] is None and len(r.json()["suggestions"]) == 3
    assert count(db, Event) == 1


def test_an_event_beyond_the_forecast_gets_typical_weather_labelled_as_such(plain, db, fake_weather):
    wardrobe(db)

    body = suggest(plain, event=event_body(starts_at=starts(40))).json()

    assert body["weather"]["summary"]["source"] == "typical" and len(body["suggestions"]) == 3
    assert fake_weather.calls("archive") and not fake_weather.calls("forecast")


def test_without_weather_there_is_no_warmth_filter(plain, db, fake_weather):
    wardrobe(db, warmth=5)  # far too warm for a mild day

    with_weather = suggest(plain, event=event_body()).json()
    fake_weather.script("forecast", 500)
    without = suggest(plain, event=event_body(starts_at=starts(2))).json()  # another day: not in the weather cache

    assert len(with_weather["suggestions"]) == 3 and with_weather["problem"]["cause"] == "filtered"
    assert "closest looks" in with_weather["problem"]["message"]
    assert len(without["suggestions"]) == 3 and without["problem"] is None


# --- no outfit can be built (AC-4, 38) -----------------------------------------------------------------------------


def test_when_nothing_meets_the_dress_level_the_closest_looks_come_with_a_note_saying_so(plain, db):
    wardrobe(db, formality=1)  # gym wear only, for a wedding (formality 4 to 5)

    body = suggest(plain, event=event_body(type="wedding")).json()

    assert len(body["suggestions"]) == 3
    assert body["problem"] == {
        "slot": None,
        "cause": "filtered",
        "message": "Nothing in your wardrobe is quite right for this wedding (dress level or weather), so these are "
        "the closest looks you have.",
    }


def test_the_closest_looks_still_leave_out_pieces_that_are_not_available(plain, db):
    made = wardrobe(db, formality=1)
    for shoe in made["footwear"][1:]:
        shoe.status = "laundry"
    db.add_all(made["footwear"][1:])
    db.commit()

    body = suggest(plain, event=event_body(type="wedding")).json()

    shoes = {i["id"] for s in body["suggestions"] for i in s["items"] if i["category"] == "footwear"}
    assert body["problem"]["cause"] == "filtered" and shoes == {made["footwear"][0].id}


def test_an_empty_wardrobe_says_which_slot_is_missing(plain):
    body = suggest(plain, event=event_body()).json()

    assert body["suggestions"] == []
    assert body["problem"] == {
        "slot": "top",
        "cause": "missing",
        "message": "Add at least one top to get outfit suggestions",
    }


def test_regenerating_never_repeats_an_excluded_set_and_makes_no_weather_request(plain, db, fake_weather):
    wardrobe(db)
    first = suggest(plain, event=event_body()).json()
    requests_so_far = len(fake_weather.requests)

    again = suggest(plain, event_id=first["event"]["id"], exclude=shown(first)).json()

    before = {frozenset(i) for i in shown(first)}
    assert len(again["suggestions"]) == 3 and not {frozenset(i) for i in shown(again)} & before
    assert len(fake_weather.requests) == requests_so_far  # AC-37: the weather came from the cache


def test_fewer_than_three_valid_outfits_remain_returns_what_is_left_and_none_explains_why(plain, db):
    made = wardrobe(db, tops=1, bottoms=1, shoes=3)  # exactly three outfits exist
    key = lambda shoe: [made["top"][0].id, made["bottom"][0].id, shoe.id]  # noqa: E731
    first = suggest(plain, event=event_body()).json()
    assert len(first["suggestions"]) == 1  # without a model the rules pick, and looks a shoe apart are one look
    event_id = first["event"]["id"]

    two_left = suggest(plain, event_id=event_id, exclude=[key(made["footwear"][0])]).json()
    none_left = suggest(plain, event_id=event_id, exclude=[key(s) for s in made["footwear"]]).json()

    assert len(two_left["suggestions"]) == 1 and two_left["problem"] is None
    assert none_left["suggestions"] == []
    assert none_left["problem"] == {
        "slot": None,
        "cause": "exhausted",
        "message": "No new outfits can be built for this event. Change the event or add items.",
    }


def test_an_outfit_that_lost_an_item_is_never_suggested_as_it_is(plain, db):
    top = add_item(db, name="Shirt", category="top", formality=3, warmth=2)
    bottom = add_item(db, name="Chinos", category="bottom", formality=3, warmth=2)
    add_wear(db, [top.id, bottom.id, 999], date.today() - DAY)  # 999 was deleted later

    body = suggest(plain, event=event_body()).json()

    assert body["suggestions"] == [] and body["problem"]["cause"] == "exhausted"


# --- the repetition rules reach the suggestions (AC-85: the audience penalty still applies) ------------------------


def test_the_audience_penalty_applies_although_the_names_are_never_sent(client, db, fake):
    made = wardrobe(db, tops=1, bottoms=1, shoes=1)  # the one look: its penalty is measured, not its rank
    worn = [made["top"][0].id, made["bottom"][0].id, made["footwear"][0].id]
    add_wear(db, worn, date.today() - 29 * DAY, audience=["Priya Distinct"])
    fake.replies = [pick_reply((1, "a")), pick_reply((1, "a"))]

    def repetition_of_worn(audience: list[str]) -> float:
        body = suggest(client, event=event_body(audience_tags=audience)).json()
        return next(s["parts"]["repetition"] for s in body["suggestions"] if sorted(s["item_ids"]) == sorted(worn))

    with_priya = repetition_of_worn(["Priya Distinct", "Ravi Distinct"])
    nobody = repetition_of_worn([])

    assert with_priya < nobody
    for call in fake.calls:
        assert "Priya" not in call["prompt"] and "Ravi" not in call["prompt"]


@pytest.mark.parametrize("failure", [None, LLMUnavailableError("down")], ids=["success", "provider_down"])
def test_no_audience_name_reaches_the_prompt_or_any_stored_call(client, db, fake, failure):
    wardrobe(db)
    fake.replies = [pick_reply((1, "a"), (2, "b"), (3, "c")) if failure is None else failure]

    suggest(client, event=event_body(audience_tags=["Priya Distinct", "Ravi Distinct"]))

    stored = " ".join(f"{c.request_summary} {c.response} {c.error}" for c in db.exec(select(LlmCall)))
    sent = " ".join(c["prompt"] + c["system"] for c in fake.calls)
    assert fake.calls and stored
    for name in ("Priya", "Ravi"):
        assert name not in sent and name not in stored


# --- what leaves the machine, and what the logs hold (AC-75, 76) ---------------------------------------------------

SECRETS = ("Zyx-Audience", "Zyx-Title", "Zyx-Notes", "Zyx-Style", "Zyx-Home", "Zyx-Body")


def test_every_weather_request_is_https_to_open_meteo_and_holds_no_personal_text(client, db, fake, fake_weather):
    wardrobe(db)
    client.put(
        "/api/profile",
        json={"style_prefs": "Zyx-Style", "home_city": "Zyx-Home", "body_notes": "Zyx-Body", "colors_loved": ["navy"]},
    ).raise_for_status()
    fake.replies = [pick_reply((1, "a"))]

    r = suggest(
        client,
        event=event_body(place="Gurugram", title="Zyx-Title", notes="Zyx-Notes", audience_tags=["Zyx-Audience"]),
    )

    assert r.status_code == 200 and fake_weather.requests
    for request in fake_weather.requests:
        assert request.url.scheme == "https" and request.url.host in HOSTS.values()
        assert not any(secret in str(request.url) + request.content.decode() for secret in SECRETS)
        assert "Authorization" not in request.headers


@pytest.mark.parametrize("scenario", ["success", "weather_down", "geocode_down", "place_not_found", "invalid"])
def test_logs_hold_no_personal_text_coordinates_or_weather_query(client, db, fake_weather, caplog, scenario):
    wardrobe(db)
    client.app.state.ctx.settings = None
    caplog.set_level(logging.DEBUG)
    fields = {"title": "Zyx-Title", "notes": "Zyx-Notes", "audience_tags": ["Zyx-Audience"], "place": "Zyx-Place"}
    match scenario:
        case "weather_down":
            fake_weather.script("forecast", httpx.ReadTimeout("slow"))
        case "geocode_down":
            fake_weather.script("geocode", httpx.ConnectTimeout("slow"))
        case "place_not_found":
            fake_weather.script("geocode", {"results": []})
        case "invalid":
            fields["title"] = "Zyx-Title" + "t" * 200

    r = suggest(client, event=event_body(**fields))

    # Not vacuous: the secrets reached the app, and the app itself logged (its own lines, not the test client's).
    # The invalid request is refused by validation, which the app does not log; its proof is the 422.
    if scenario == "invalid":
        assert r.status_code == 422
    else:
        assert any(rec.name.startswith("kyapehnu") for rec in caplog.records)
    for forbidden in (*SECRETS, "Zyx-Place", "28.46", "77.03", "latitude", "start_date", "hourly"):
        assert forbidden not in caplog.text


# --- a slow provider blocks nothing (AC-80) ------------------------------------------------------------------------


def test_a_suggest_waiting_on_the_provider_does_not_block_a_wear_or_worn_today(client, db, fake):
    made = wardrobe(db)
    entered, release = threading.Event(), threading.Event()

    def slow_call(**_kwargs) -> RawResult:
        entered.set()
        release.wait(10)
        return RawResult(json.dumps(pick_reply((1, "late"))), 10, 5)

    fake.call_json = slow_call
    outfit = [made["top"][0].id, made["bottom"][0].id]
    with ThreadPoolExecutor(max_workers=1) as pool:
        waiting = pool.submit(suggest, client, event=event_body())
        try:
            assert entered.wait(10)
            started = time.monotonic()
            wear = client.post("/api/outfits/wear", json={"item_ids": outfit})
            worn = client.post(f"/api/items/{made['top'][1].id}/worn-today")
            quick = time.monotonic() - started
        finally:
            release.set()
        result = waiting.result(timeout=20)

    assert (wear.status_code, worn.status_code) == (201, 201) and quick < 5
    assert result.status_code == 200 and result.json()["suggestions"][0]["reason"] == "late"
    [row] = db.exec(select(LlmCall)).all()
    assert row.purpose == "outfit_pick" and row.error is None
    assert (count(db, Outfit), count(db, WearLog)) == (1, 2)


# --- swap (AC-39, 40) ----------------------------------------------------------------------------------------------


def swap(client, event_id, item_ids, item_id, exclude=()):
    return client.post(
        "/api/outfits/swap",
        json={"event_id": event_id, "item_ids": item_ids, "item_id": item_id, "exclude_item_ids": list(exclude)},
    )


@pytest.fixture
def tops_of_rank(db):
    """An outfit (top, bottom, shoes) and three other tops, best to worst for a party in mild weather."""
    outfit = wardrobe(db, tops=1, bottoms=1, shoes=1)
    best = add_item(db, name="Best", category="top", formality=3, warmth=2)
    middle = add_item(db, name="Middle", category="top", formality=3, warmth=3)
    worst = add_item(db, name="Worst", category="top", formality=2, warmth=1)
    return outfit, best, middle, worst


def a_party(db) -> int:
    return add_event(db, starts_at=datetime.fromisoformat(starts())).id


def ids_of(outfit) -> list[int]:
    return [outfit[k][0].id for k in ("top", "bottom", "footwear")]


def test_swap_changes_only_the_named_item_to_the_best_valid_alternative_without_the_ai(client, db, fake, tops_of_rank):
    outfit, best, *_ = tops_of_rank
    ids = ids_of(outfit)

    r = swap(client, a_party(db), ids, ids[0])

    body = r.json()
    assert r.status_code == 200 and body["item_ids"] == [best.id, ids[1], ids[2]]
    assert [i["id"] for i in body["items"]] == body["item_ids"] and body["swap"] is None and body["notes"] == []
    assert body["reason"].startswith("Picked because") and 0 <= body["score"] <= 1
    assert set(body["parts"]) == SEVEN_PARTS and body["rating"] is None
    assert fake.calls == [] and count(db, LlmCall) == 0


def test_swap_skips_excluded_items_and_makes_no_further_weather_request(client, db, fake_weather, tops_of_rank):
    outfit, best, middle, worst = tops_of_rank
    ids = ids_of(outfit)
    event_id = a_party(db)
    swap(client, event_id, ids, ids[0])  # fills the weather cache
    requests_so_far = len(fake_weather.requests)

    second = swap(client, event_id, ids, ids[0], exclude=[best.id]).json()
    third = swap(client, event_id, ids, ids[0], exclude=[best.id, middle.id]).json()

    assert second["item_ids"][0] == middle.id and third["item_ids"][0] == worst.id
    assert len(fake_weather.requests) == requests_so_far  # AC-38


def test_swap_with_no_alternative_left_is_a_409_that_names_the_slot(client, db, tops_of_rank):
    outfit, best, middle, worst = tops_of_rank
    ids = ids_of(outfit)

    r = swap(client, a_party(db), ids, ids[0], exclude=[best.id, middle.id, worst.id])

    assert (r.status_code, r.json()["code"]) == (409, "no_alternative")
    assert r.json()["detail"] == "No other top fits this event"


def test_swap_ignores_laundry_retired_and_unfit_alternatives(client, db, tops_of_rank):
    outfit, best, middle, worst = tops_of_rank
    ids = ids_of(outfit)
    for item in (best, middle, worst):
        item.status = "laundry"
        db.add(item)
    add_item(db, name="Too formal", category="top", formality=5, warmth=2)  # a party takes 2 to 4
    db.commit()

    r = swap(client, a_party(db), ids, ids[0])

    assert (r.status_code, r.json()["code"]) == (409, "no_alternative")


def test_with_two_items_in_one_slot_only_the_named_one_is_replaced(client, db, tops_of_rank):
    outfit, best, *_ = tops_of_rank
    top, bottom = outfit["top"][0], outfit["bottom"][0]
    layer = add_item(db, name="Second shirt", category="top", formality=3, warmth=2)

    body = swap(client, a_party(db), [top.id, layer.id, bottom.id], layer.id).json()

    assert sorted(body["item_ids"]) == sorted([top.id, best.id, bottom.id]) and layer.id not in body["item_ids"]


def test_swapping_an_item_that_is_not_in_the_outfit_is_422_before_anything_is_loaded(client, db, fake_weather):
    made = wardrobe(db)
    ids = [made["top"][0].id, made["bottom"][0].id]

    r = swap(client, 1, ids, made["top"][2].id)

    assert (r.status_code, r.json()["code"]) == (422, "item_not_in_outfit")
    assert fake_weather.requests == []


def test_swap_with_an_unknown_event_or_item_is_404(client, db):
    made = wardrobe(db)
    ids = [made["top"][0].id, made["bottom"][0].id]
    event_id = a_party(db)

    no_event = swap(client, 999, ids, ids[0])
    no_item = swap(client, event_id, [ids[0], 99999], ids[0])

    assert (no_event.status_code, no_event.json()["code"]) == (404, "not_found")
    assert (no_item.status_code, no_item.json()["code"]) == (404, "not_found")


def test_swap_carries_the_pool_notes_of_the_event(client, db):
    top, bottom, spare = (
        add_item(db, name=n, category=c, formality=3, warmth=2)
        for n, c in (("Shirt", "top"), ("Chinos", "bottom"), ("Other shirt", "top"))
    )

    body = swap(client, a_party(db), [top.id, bottom.id], top.id).json()

    assert body["item_ids"] == [spare.id, bottom.id] and body["notes"] == ["no_footwear"]


def test_swap_never_rebuilds_an_outfit_that_lost_an_item(client, db):
    top, bottom, spare = (
        add_item(db, name=n, category=c, formality=3, warmth=2)
        for n, c in (("Shirt", "top"), ("Chinos", "bottom"), ("Other shirt", "top"))
    )
    add_wear(db, [spare.id, bottom.id, 999], date.today() - DAY)  # 999 was deleted later

    r = swap(client, a_party(db), [top.id, bottom.id], top.id)

    assert (r.status_code, r.json()["code"]) == (409, "no_alternative")


def test_swap_breaks_a_tie_towards_the_lower_id(client, db):
    top, bottom, first, second = (
        add_item(db, name=n, category=c, formality=3, warmth=2)
        for n, c in (("Shirt", "top"), ("Chinos", "bottom"), ("Twin A", "top"), ("Twin B", "top"))
    )

    body = swap(client, a_party(db), [top.id, bottom.id], top.id).json()

    assert body["item_ids"] == [first.id, bottom.id] and first.id < second.id


# --- Phase 3: the learned preference, include and the chat view (AC-26, 47, 35, 50)


def silky_and_matte(db):
    """Two tops that differ only by user tag, two bottoms and two pairs of shoes. Returns silky, matte and the rest."""
    plain = {"formality": 3, "warmth": 2, "colors": [{"hex": "#ffffff", "name": "white"}]}
    silky = add_item(db, name="Silky top", category="top", tags=["silky"], **plain)
    matte = add_item(db, name="Matte top", category="top", tags=["matte"], **plain)
    rest = [add_item(db, name=f"{c} {n}", category=c, **plain) for c in ("bottom", "footwear") for n in range(2)]
    return silky, matte, rest


def dislike(db, ids: list[int]) -> None:
    key = ",".join(str(i) for i in sorted(ids))
    outfit = Outfit(user_id=OWNER_ID, item_ids=ids, item_key=key, name="Disliked")
    db.add(outfit)
    db.flush()
    db.add(Feedback(user_id=OWNER_ID, outfit_id=outfit.id, rating=-1))
    db.commit()


def test_a_thumbs_down_moves_the_outfits_sharing_its_tags_down_the_suggestions(plain, db):
    silky, matte, rest = silky_and_matte(db)
    before = suggest(plain, event=event_body()).json()
    dislike(db, [matte.id, rest[0].id, rest[2].id])

    after = suggest(plain, event=event_body()).json()

    assert all(x["parts"]["preference"] == 0.5 for x in before["suggestions"])
    assert after["suggestions"][0]["item_ids"][0] == silky.id
    assert all(x["parts"]["preference"] < 0.5 for x in after["suggestions"])


def test_swap_prefers_the_item_whose_tags_were_not_disliked(client, db):
    plain_top = wardrobe(db, tops=1, bottoms=1, shoes=1)
    matte = add_item(db, name="Matte top", category="top", formality=3, warmth=2, tags=["matte"])
    silky = add_item(db, name="Silky top", category="top", formality=3, warmth=2, tags=["silky"])
    ids = ids_of(plain_top)
    event_id = a_party(db)

    tied = swap(client, event_id, ids, ids[0]).json()["item_ids"][0]  # no preference yet: the lower id wins the tie
    dislike(db, [matte.id, *ids[1:]])
    liked = swap(client, event_id, ids, ids[0]).json()

    assert tied == matte.id
    assert liked["item_ids"][0] == silky.id and liked["parts"]["preference"] < 0.5


def test_a_view_hides_a_stored_events_title_notes_and_place_from_the_pick_and_marks_the_call(client, db, fake):
    wardrobe(db)
    event_id = add_event(db, title="TITLE-MARKER", notes="NOTES-MARKER", place="PLACE-MARKER-5R2D").id
    fake.replies = [pick_reply((1, "ok"))]
    view = ChatView(session_id=4, session_event_ids=set(), home_city="Gurugram")

    out = service.suggest(OWNER_ID, client.app.state.ctx, db, SuggestIn(event_id=event_id), date.today(), view)

    sent = fake.calls[0]["prompt"]
    assert len(out.suggestions) == 1  # the model's one pick
    for private in ("TITLE-MARKER", "NOTES-MARKER", "PLACE-MARKER-5R2D", "Priya"):
        assert private not in sent
    assert "Place: outdoors" not in sent and "Place: indoors\n" in sent
    [row] = db.exec(select(LlmCall).where(LlmCall.purpose == "outfit_pick")).all()
    assert row.request_summary.startswith("session=4 images=[")


def test_a_view_keeps_a_session_events_text_and_names_the_home_city_as_your_home_city(client, db, fake):
    wardrobe(db)
    event_id = add_event(db, title="TITLE-MARKER", notes="NOTES-MARKER", place="Gurugram").id
    fake.replies = [pick_reply((1, "ok"))]
    view = ChatView(session_id=4, session_event_ids={event_id}, home_city=" gurugram ")

    service.suggest(OWNER_ID, client.app.state.ctx, db, SuggestIn(event_id=event_id), date.today(), view)

    sent = fake.calls[0]["prompt"]
    assert "Event: party, TITLE-MARKER\n" in sent and "Notes: NOTES-MARKER\n" in sent
    assert "Place: your home city, indoors\n" in sent and "Gurugram" not in sent and "Priya" not in sent


def test_without_a_view_the_pick_prompt_and_log_are_the_outfits_pages(client, db, fake):
    wardrobe(db)
    event_id = add_event(db, title="TITLE-MARKER", place="Gurugram").id
    fake.replies = [pick_reply((1, "ok"))]

    service.suggest(OWNER_ID, client.app.state.ctx, db, SuggestIn(event_id=event_id), date.today())

    assert (
        "Event: party, TITLE-MARKER\n" in fake.calls[0]["prompt"]
        and "Place: Gurugram, indoors\n" in fake.calls[0]["prompt"]
    )
    [row] = db.exec(select(LlmCall).where(LlmCall.purpose == "outfit_pick")).all()
    assert row.request_summary.startswith("images=[] bytes\n")


@pytest.mark.usefixtures("cool_evening")
def test_the_provider_is_called_with_no_open_transaction_for_include_and_a_view(client, db, fake):
    red = add_item(db, name="Red jacket", category="outerwear", formality=3, warmth=3)
    wardrobe(db)
    seen: list[bool] = []

    def call_json(**_kwargs) -> RawResult:
        seen.append(db.in_transaction())
        return RawResult(json.dumps(pick_reply((1, "ok"))), 10, 5)

    fake.call_json = call_json
    body = SuggestIn(event=event_body(), include=[red.id])

    out = service.suggest(OWNER_ID, client.app.state.ctx, db, body, date.today(), ChatView(1, set(), "Gurugram"))

    assert seen == [False] and all(red.id in x.item_ids for x in out.suggestions)


@pytest.fixture
def cool_evening(fake_weather):
    fake_weather.values["apparent_temperature"] = lambda t: 20.0


def test_swap_never_parts_a_saree_from_its_blouse(client, db):
    saree = add_item(db, name="Saree", category="ethnic", subcategory="saree", formality=3, warmth=2)
    blouse = add_item(db, name="Blouse", category="ethnic", subcategory="blouse", formality=3, warmth=2)
    heels = add_item(db, name="Heels", category="footwear", formality=3, warmth=2)
    dress = add_item(db, name="Dress", category="dress", formality=3, warmth=2)

    for_saree = swap(client, a_party(db), [saree.id, blouse.id, heels.id], saree.id)
    for_dress = swap(client, a_party(db), [dress.id, heels.id], dress.id)

    assert for_saree.status_code == 409 and "No other full" in for_saree.text  # the dress would lose the blouse
    assert for_dress.status_code == 409 and "No other full" in for_dress.text  # the saree would have none
