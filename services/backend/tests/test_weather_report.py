"""`GET /api/weather` and `weather.report` (LLD 3.1, 3.2; AC-9, AC-14, AC-50): one weather function for the REST route
and the chat tool, with the place named "your home city" when it is the user's."""

import dataclasses
import logging
from datetime import date, datetime

import pytest
from sqlmodel import Session

from kyapehnu.user.models import UserProfile
from kyapehnu.user.profile import PROFILE_ID
from kyapehnu.wardrobe.weather_report import WeatherQuery, report
from kyapehnu.weather import summary
from tests.chat_fakes import fixed_now
from tests.fake_openmeteo import GURUGRAM

NOW = datetime(2026, 10, 7, 9, 30)  # a Wednesday
SATURDAY_EVENING = "2026-10-10T19:00"
WEATHER = "/api/weather"
HOME_LABEL = "your home city"
PLACE_MARKER = "Zzyzx Hideout"


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    fixed_now(monkeypatch, NOW)


def set_home_city(client, city: str) -> None:
    with Session(client.app.state.ctx.engine) as s:
        s.get(UserProfile, PROFILE_ID).home_city = city
        s.commit()


def geocoded(fake_weather) -> list[str]:
    """The place names Open-Meteo was asked for, in order."""
    return [r.url.params["name"] for r in fake_weather.calls("geocode")]


def test_a_place_and_a_time_give_the_weather_the_event_summary_function_gives(client, fake_weather):
    ctx = client.app.state.ctx
    starts_at, ends_at = datetime(2026, 10, 10, 19), datetime(2026, 10, 10, 22)

    r = client.get(
        WEATHER, params={"place": "Jaipur", "starts_at": starts_at.isoformat(), "ends_at": ends_at.isoformat()}
    )

    assert r.status_code == 200
    body = r.json()
    expected = summary.for_event(ctx.weather, "Jaipur", starts_at, ends_at, NOW.date(), ctx.prefs.weather)
    assert body["place"] == "Jaipur"
    assert (body["starts_at"], body["ends_at"]) == (starts_at.isoformat(), ends_at.isoformat())
    assert body["weather"]["summary"] == expected.model_dump(mode="json")
    assert body["weather"]["summary"]["source"] == "forecast"
    assert geocoded(fake_weather)[0] == "jaipur"


@pytest.mark.parametrize("params", [{}, {"place": ""}, {"place": "  "}, {"place": "jaipur"}, {"place": " JAIPUR "}])
def test_no_place_or_the_home_city_is_called_your_home_city_and_looked_up_as_the_home_city(
    client, fake_weather, params
):
    set_home_city(client, "Jaipur")

    body = client.get(WEATHER, params={"starts_at": SATURDAY_EVENING} | params).json()

    assert body["place"] == HOME_LABEL
    assert set(geocoded(fake_weather)) == {"jaipur"}
    assert body["weather"] is not None


def test_no_start_time_means_now(client):
    body = client.get(WEATHER, params={"place": "Jaipur"}).json()

    assert (body["starts_at"], body["ends_at"]) == (NOW.isoformat(), None)


def test_an_end_alone_runs_from_now(client):
    body = client.get(WEATHER, params={"place": "Jaipur", "ends_at": "2026-10-07T18:00"}).json()

    assert (body["starts_at"], body["ends_at"]) == (NOW.isoformat(), "2026-10-07T18:00:00")


@pytest.mark.parametrize(("outdoors", "sun_note"), [(True, True), (False, False)])
def test_outdoors_decides_whether_the_sun_note_shows(client, fake_weather, outdoors, sun_note):
    fake_weather.values["uv_index"] = 11.0

    notes = client.get(WEATHER, params={"starts_at": SATURDAY_EVENING, "outdoors": outdoors}).json()["weather"]["notes"]

    assert any("sun" in note.lower() for note in notes) is sun_note


def test_a_place_that_is_not_found_is_422_place_not_found_and_never_logged(client, fake_weather, caplog):
    fake_weather.script("geocode", {"results": []})

    with caplog.at_level(logging.DEBUG):
        r = client.get(WEATHER, params={"place": PLACE_MARKER, "starts_at": SATURDAY_EVENING})

    assert (r.status_code, r.json()["code"]) == (422, "place_not_found")
    assert PLACE_MARKER not in caplog.text


@pytest.mark.parametrize("endpoint", ["forecast", "geocode"])
def test_weather_that_cannot_be_had_is_a_report_without_weather(client, fake_weather, endpoint):
    fake_weather.script(endpoint, 500)

    r = client.get(WEATHER, params={"place": "Jaipur", "starts_at": SATURDAY_EVENING})

    assert r.status_code == 200
    assert (r.json()["place"], r.json()["weather"]) == ("Jaipur", None)


@pytest.mark.parametrize(
    "params",
    [
        {"starts_at": "2026-10-10T19:00", "ends_at": "2026-10-10T18:00"},
        {"starts_at": "2026-10-10T19:00", "ends_at": "2026-10-18T19:00"},
    ],
    ids=["ends_before_start", "over_7_days"],
)
def test_bad_input_is_422_and_no_weather_is_asked_for(client, fake_weather, params):
    r = client.get(WEATHER, params=params)

    assert r.status_code == 422
    assert fake_weather.requests == []


def test_an_end_that_is_not_after_now_is_a_400_invalid_input_and_no_weather_is_asked_for(client, fake_weather):
    r = client.get(WEATHER, params={"ends_at": "2026-10-07T08:00"})

    assert (r.status_code, r.json()["code"]) == (400, "invalid_input")
    assert fake_weather.requests == []


def test_a_build_without_a_weather_client_reports_no_weather(client):
    ctx = dataclasses.replace(client.app.state.ctx, weather=None)

    with Session(ctx.engine) as s:
        result = report(1, ctx, s, WeatherQuery(place="Jaipur", starts_at=datetime(2026, 10, 10, 19)), NOW)

    assert (result.place, result.weather) == ("Jaipur", None)
    assert result.starts_at.date() == date(2026, 10, 10)


# --- /api/weather/places ---------------------------------------------------------------------------------------------


def test_places_suggests_names_close_to_a_misspelling(client, fake_weather):
    def by_prefix(req):
        hit = {**GURUGRAM, "admin1": "Haryana", "country": "India"}
        return {"results": [hit] if req.url.params["name"] == "gurugr" else []}

    fake_weather.script("geocode", by_prefix)

    r = client.get("/api/weather/places", params={"q": "Gurugrm"})

    assert r.status_code == 200
    assert r.json() == {"places": ["Gurugram, Haryana, India"]}


def test_places_is_empty_when_the_geocoder_is_down(client, fake_weather):
    fake_weather.script("geocode", 503)

    r = client.get("/api/weather/places", params={"q": "Gurugram"})

    assert r.status_code == 200
    assert r.json() == {"places": []}


def test_places_needs_a_query(client):
    assert client.get("/api/weather/places").status_code == 422
    assert client.get("/api/weather/places", params={"q": "x" * 121}).status_code == 422
