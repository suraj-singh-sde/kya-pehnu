"""Open-Meteo client and the event weather summary (LLD 3.4, 3.5): parsing, window, forecast or typical, failures."""

import json
import logging
import re
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from kyapehnu.core.config import WeatherPrefs
from kyapehnu.core.errors import PlaceNotFoundError
from kyapehnu.weather import openmeteo, summary
from tests.fake_openmeteo import GURUGRAM

DATA = Path(__file__).parent / "fixtures" / "openmeteo"
PREFS = WeatherPrefs()
TODAY = date(2026, 10, 1)
EVENT_DAY = date(2026, 10, 6)  # five days after TODAY: forecast and air quality
SECRET_PLACE = "Zzyzx Hideout"
BOOM = "secret-boom-text"
HOURLY_VARIABLES = (
    "temperature_2m,apparent_temperature,relative_humidity_2m,precipitation_probability,wind_speed_10m,uv_index"
)
ARCHIVE_VARIABLES = "apparent_temperature,relative_humidity_2m,precipitation,wind_speed_10m"


def load(name: str) -> dict:
    return json.loads((DATA / name).read_text())


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def client(fake_weather, clock, kv):
    with httpx.Client(transport=httpx.MockTransport(fake_weather), follow_redirects=False) as http:
        yield openmeteo.WeatherClient(http, kv, clock)


@pytest.fixture
def fixtures(fake_weather):
    """The recorded-format responses: every hour of 2026-10-06 and a 2024 archive day."""
    fake_weather.script("geocode", load("geocode.json"))
    fake_weather.script("forecast", load("forecast.json"))
    fake_weather.script("air_quality", load("air_quality.json"))
    fake_weather.script("archive", load("archive.json"))
    return fake_weather


def at(hour: int, minute: int = 0, day: date = EVENT_DAY) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute)


def summarise(client, starts_at, ends_at=None, today=TODAY, place="Gurugram"):
    return summary.for_event(client, place, starts_at, ends_at, today, PREFS)


# --- parsing (AC-41) ---------------------------------------------------------------------------------------------


def test_geocode_uses_the_first_result(client, fixtures):
    place = client.geocode("Gurugram", client.budget())

    assert place == openmeteo.Place(latitude=28.4595, longitude=77.0266, timezone="Asia/Kolkata")


@pytest.mark.parametrize("body", [{"generationtime_ms": 0.1}, {"results": []}])
def test_geocode_without_results_is_none(client, fake_weather, body):
    fake_weather.script("geocode", body)

    assert client.geocode("Nowhereville", client.budget()) is None


@pytest.mark.parametrize(
    "reply", [{"results": [{"latitude": "north", "longitude": 77.0, "timezone": "Asia/Kolkata"}]}, {"results": [{}]}]
)
def test_geocode_rejects_a_malformed_result(client, fake_weather, reply):
    fake_weather.script("geocode", reply)

    with pytest.raises(openmeteo.WeatherFetchError) as e:
        client.geocode("Gurugram", client.budget())

    assert e.value.outcome == "malformed"


@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity"])
def test_geocode_rejects_a_non_finite_coordinate(client, fake_weather, token):
    fake_weather.script(
        "geocode", f'{{"results": [{{"latitude": {token}, "longitude": 77.0, "timezone": "Asia/Kolkata"}}]}}'.encode()
    )

    with pytest.raises(openmeteo.WeatherFetchError) as e:
        client.geocode("Gurugram", client.budget())

    assert e.value.outcome == "malformed"


def test_geocode_request_is_https_with_the_normalised_name_and_ordered_params(client, fake_weather):
    client.geocode("  Gurugram ", client.budget())

    (request,) = fake_weather.requests
    assert (request.url.scheme, request.url.host, request.url.path) == (
        "https",
        "geocoding-api.open-meteo.com",
        "/v1/search",
    )
    assert request.url.params.multi_items() == [
        ("name", "gurugram"),
        ("count", "1"),
        ("language", "en"),
        ("format", "json"),
    ]


@pytest.mark.parametrize(
    ("fixture", "url", "variables", "first_hour", "expected"),
    [
        (
            "forecast.json",
            openmeteo.FORECAST_URL,
            ["apparent_temperature", "uv_index"],
            at(18),
            {"apparent_temperature": 27.0, "uv_index": 2.5},
        ),
        ("air_quality.json", openmeteo.AIR_URL, ["us_aqi"], at(19), {"us_aqi": 120}),
        (
            "archive.json",
            openmeteo.ARCHIVE_URL,
            ["precipitation", "wind_speed_10m"],
            datetime(2024, 10, 6, 19),
            {"precipitation": 1.2, "wind_speed_10m": 9.5},
        ),
    ],
)
def test_hourly_parses_the_recorded_series(client, fixtures, fixture, url, variables, first_hour, expected):
    fixtures.script(
        {"forecast.json": "forecast", "air_quality.json": "air_quality", "archive.json": "archive"}[fixture],
        load(fixture),
    )

    hours = client.hourly(url, {"latitude": "28.46"}, variables, client.budget())

    by_time = dict(hours)
    assert len(hours) == 24
    assert by_time[first_hour] == expected


@pytest.mark.parametrize(
    "reply",
    [
        {"timezone": "Asia/Kolkata"},  # no hourly block
        {"hourly": {"time": ["2026-10-06T00:00"]}},  # a requested variable is missing
        {"hourly": {"time": ["2026-10-06T00:00", "2026-10-06T01:00"], "us_aqi": [1.0]}},  # length mismatch
        {"hourly": {"time": ["not a time"], "us_aqi": [1.0]}},
        {"hourly": {"time": ["2026-10-06T00:00"], "us_aqi": ["high"]}},
        b"{not json",
        [1, 2, 3],
    ],
)
def test_hourly_rejects_a_malformed_body(client, fake_weather, reply):
    fake_weather.script("air_quality", reply)

    with pytest.raises(openmeteo.WeatherFetchError) as e:
        client.hourly(openmeteo.AIR_URL, {}, ["us_aqi"], client.budget())

    assert e.value.outcome == "malformed"


@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity"])
def test_hourly_rejects_a_non_finite_value(client, fake_weather, token):
    fake_weather.script("air_quality", f'{{"hourly": {{"time": ["2026-10-06T00:00"], "us_aqi": [{token}]}}}}'.encode())

    with pytest.raises(openmeteo.WeatherFetchError) as e:
        client.hourly(openmeteo.AIR_URL, {}, ["us_aqi"], client.budget())

    assert e.value.outcome == "malformed"


# --- the event window (AC-42) ------------------------------------------------------------------------------------


def test_event_window_ends_at_the_given_end():
    assert summary.event_window(at(18), at(22), 3) == (at(18), at(22))


def test_event_window_without_an_end_lasts_the_default_hours():
    assert summary.event_window(at(18, 30), None, 3) == (at(18, 30), at(21, 30))


# --- forecast (AC-42, AC-44) -------------------------------------------------------------------------------------


def test_forecast_summarises_only_the_event_hours(client, fixtures):
    result = summarise(client, at(18), at(22))

    assert result == summary.WeatherSummary(
        source="forecast",
        feels_like_min_c=22.5,
        feels_like_max_c=27.0,
        humidity_mean_pct=66.0,
        rain_probability_max_pct=35.0,
        wind_max_kmh=15.5,
        uv_max=2.5,
        aqi_max=120.0,
    )


def test_forecast_without_an_end_covers_the_default_three_hours(client, fixtures):
    result = summarise(client, at(19))  # 19, 20, 21

    assert result == summary.WeatherSummary(
        source="forecast",
        feels_like_min_c=22.5,
        feels_like_max_c=25.5,
        humidity_mean_pct=68.0,
        rain_probability_max_pct=35.0,
        wind_max_kmh=15.5,
        uv_max=0.5,
        aqi_max=120.0,
    )


def test_forecast_includes_the_hour_the_event_starts_in(client, fixtures):
    assert summarise(client, at(18, 30), at(22)) == summarise(client, at(18), at(22))


def test_forecast_requests_have_the_documented_params_in_order(client, fixtures):
    summarise(client, at(18), at(22))

    forecast, air = fixtures.calls("forecast"), fixtures.calls("air_quality")
    assert [(r.url.scheme, r.url.path) for r in (*forecast, *air)] == [
        ("https", "/v1/forecast"),
        ("https", "/v1/air-quality"),
    ]
    assert forecast[0].url.params.multi_items() == [
        ("latitude", "28.46"),
        ("longitude", "77.03"),
        ("timezone", "Asia/Kolkata"),
        ("start_date", "2026-10-06"),
        ("end_date", "2026-10-06"),
        ("hourly", HOURLY_VARIABLES),
    ]
    assert air[0].url.params.multi_items() == [
        ("latitude", "28.46"),
        ("longitude", "77.03"),
        ("timezone", "Asia/Kolkata"),
        ("start_date", "2026-10-06"),
        ("end_date", "2026-10-06"),
        ("hourly", "us_aqi"),
    ]


def test_only_the_geocode_request_carries_the_place(client, fixtures):
    summarise(client, at(18), at(22), place=SECRET_PLACE)

    for endpoint in ("forecast", "air_quality"):
        assert all(SECRET_PLACE.casefold() not in str(r.url).casefold() for r in fixtures.calls(endpoint))
    assert fixtures.calls("geocode")[0].url.params["name"] == SECRET_PLACE.casefold()


def test_event_over_midnight_spans_both_days(client, fake_weather):
    fake_weather.values["apparent_temperature"] = lambda t: 10.0 if t.day == 6 else 30.0

    result = summarise(client, at(22), at(1, day=EVENT_DAY + timedelta(days=1)))  # 22, 23, 00

    assert (result.feels_like_min_c, result.feels_like_max_c) == (10.0, 30.0)
    assert [(r.url.params["start_date"], r.url.params["end_date"]) for r in fake_weather.calls("forecast")] == [
        ("2026-10-06", "2026-10-07")
    ]


def test_event_ending_at_midnight_does_not_reach_the_next_day(client, fake_weather):
    summarise(client, at(22), at(0, day=EVENT_DAY + timedelta(days=1)))

    assert fake_weather.calls("forecast")[0].url.params["end_date"] == "2026-10-06"


def test_generated_series_fill_every_metric(client, fake_weather):
    result = summarise(client, at(10), at(12))

    assert result == summary.WeatherSummary(
        source="forecast",
        feels_like_min_c=26.0,
        feels_like_max_c=26.0,
        humidity_mean_pct=60.0,
        rain_probability_max_pct=10.0,
        wind_max_kmh=10.0,
        uv_max=5.0,
        aqi_max=80.0,
    )


def test_a_metric_without_values_is_none_and_the_rest_is_kept(client, fake_weather):
    fake_weather.values["relative_humidity_2m"] = lambda t: None

    result = summarise(client, at(10), at(12))

    assert (result.humidity_mean_pct, result.wind_max_kmh) == (None, 10.0)


def test_forecast_without_any_feels_like_value_is_unavailable(client, fake_weather):
    fake_weather.values["apparent_temperature"] = lambda t: None

    assert summarise(client, at(10), at(12)) is None


def test_event_ten_days_out_uses_the_forecast_without_air_quality(client, fake_weather):
    result = summarise(client, at(10), at(12), today=EVENT_DAY - timedelta(days=10))

    assert (result.source, result.aqi_max) == ("forecast", None)
    assert fake_weather.calls("air_quality") == []
    assert len(fake_weather.calls("forecast")) == 1


@pytest.mark.parametrize(
    ("days_ahead", "source", "air_requested"),
    [
        (6, "forecast", True),
        (7, "forecast", False),
        (14, "forecast", False),
        (15, "typical", False),
        (-92, "forecast", True),
        (-93, "typical", False),
    ],
)
def test_source_and_air_quality_limits_follow_the_event_day(client, fake_weather, days_ahead, source, air_requested):
    result = summarise(client, at(10), at(12), today=EVENT_DAY - timedelta(days=days_ahead))

    assert result.source == source
    assert bool(fake_weather.calls("air_quality")) is air_requested


def test_event_whose_last_day_is_beyond_the_forecast_horizon_is_typical(client, fake_weather):
    today = EVENT_DAY - timedelta(days=14)

    result = summarise(client, at(22), at(2, day=EVENT_DAY + timedelta(days=1)), today=today)

    assert result.source == "typical"


# --- typical weather (AC-43) -------------------------------------------------------------------------------------


@pytest.fixture
def typical_values(fake_weather):
    """Hour, year and day leave a trace in each value so the averages can be computed by hand."""
    fake_weather.values["apparent_temperature"] = lambda t: t.hour + (t.year - 2021)
    fake_weather.values["relative_humidity_2m"] = lambda t: 2.0 * t.day
    fake_weather.values["precipitation"] = lambda t: 1.0 if t.day == 26 else 0.0
    fake_weather.values["wind_speed_10m"] = lambda t: t.hour / 2 + (t.year - 2021)
    return fake_weather


def test_typical_averages_five_years_and_three_days_per_hour(client, typical_values):
    today = date(2026, 10, 6)

    result = summarise(client, at(18, day=date(2026, 10, 26)), at(22, day=date(2026, 10, 26)), today=today)

    assert result == summary.WeatherSummary(
        source="typical",
        feels_like_min_c=20.0,  # hour 18 + mean year offset 2
        feels_like_max_c=23.0,
        humidity_mean_pct=52.0,  # days 25, 26, 27 -> 50, 52, 54
        rain_probability_max_pct=33.3,  # rain on 26 Oct only: 1 of 3 shifted days
        wind_max_kmh=12.5,  # hour 21 / 2 + 2
        uv_max=None,
        aqi_max=None,
    )


def test_typical_asks_the_archive_once_per_year_with_a_day_of_slack(client, typical_values):
    summarise(client, at(18, day=date(2026, 10, 26)), at(22, day=date(2026, 10, 26)), today=date(2026, 10, 6))

    archive = typical_values.calls("archive")
    assert [(r.url.params["start_date"], r.url.params["end_date"]) for r in archive] == [
        (f"{y}-10-25", f"{y}-10-27") for y in range(2021, 2026)
    ]
    assert archive[0].url.params.multi_items()[:3] == [
        ("latitude", "28.46"),
        ("longitude", "77.03"),
        ("timezone", "Asia/Kolkata"),
    ]
    assert archive[0].url.params["hourly"] == ARCHIVE_VARIABLES
    assert typical_values.calls("forecast") == [] and typical_values.calls("air_quality") == []


def test_typical_maps_29_february_to_28_february(client, fake_weather):
    summarise(client, datetime(2028, 2, 29, 10), datetime(2028, 2, 29, 12), today=date(2028, 2, 9))

    assert [(r.url.params["start_date"], r.url.params["end_date"]) for r in fake_weather.calls("archive")] == [
        (f"{y}-02-27", f"{y}-02-29" if y == 2024 else f"{y}-03-01")
        for y in range(2023, 2028)  # 28 Feb + 1 day
    ]


def test_typical_for_an_event_over_new_year_reads_the_following_year(client, fake_weather):
    fake_weather.values["apparent_temperature"] = lambda t: float(t.hour)

    result = summarise(client, datetime(2026, 12, 31, 22), datetime(2027, 1, 1, 2), today=date(2026, 12, 1))

    assert (result.source, result.feels_like_min_c, result.feels_like_max_c) == ("typical", 0.0, 23.0)
    assert [(r.url.params["start_date"], r.url.params["end_date"]) for r in fake_weather.calls("archive")][0] == (
        "2021-12-30",
        "2022-01-02",
    )


def test_typical_uses_only_the_hours_the_archive_returned(client, fixtures):
    """The recorded archive day is 2024-10-06 (18:00 and 19:00 filled); every other sample position is absent."""
    result = summarise(client, at(18), at(20), today=date(2026, 9, 1))

    assert result == summary.WeatherSummary(
        source="typical",
        feels_like_min_c=25.2,
        feels_like_max_c=26.1,
        humidity_mean_pct=56.5,
        rain_probability_max_pct=50.0,  # 0.0 mm and 1.2 mm
        wind_max_kmh=9.5,
        uv_max=None,
        aqi_max=None,
    )


def test_typical_without_any_sample_is_unavailable(client, fake_weather):
    fake_weather.values["apparent_temperature"] = lambda t: None

    assert summarise(client, at(10), at(12), today=date(2026, 9, 1)) is None


# --- failures degrade to None (AC-46, AC-77) ---------------------------------------------------------------------


def _too_large_stream(consumed: list[int]) -> httpx.Response:
    def body():
        for _ in range(20):  # 20 x 64 KiB, the cap is 1 MiB
            consumed.append(1)
            yield b" " * 65536

    return httpx.Response(200, content=body())


STREAMED: list[int] = []
FAILURES = {
    "timeout": lambda request: httpx.ReadTimeout(BOOM),
    "connect_timeout": lambda request: httpx.ConnectTimeout(BOOM),
    "network": lambda request: httpx.ConnectError(BOOM),
    "http_500": lambda request: httpx.Response(500, json={"reason": BOOM}),
    "http_429": lambda request: httpx.Response(429),
    "malformed_json": lambda request: httpx.Response(200, content=b"{not json"),
    "not_an_object": lambda request: httpx.Response(200, json=[1, 2]),
    "redirect": lambda request: httpx.Response(302, headers={"Location": "https://evil.example/steal"}),
    "too_large_by_header": lambda request: httpx.Response(
        200, headers={"Content-Length": str(openmeteo.MAX_BODY_BYTES + 1)}, content=b"{}"
    ),
    "too_large_by_stream": lambda request: _too_large_stream(STREAMED),
}


@pytest.fixture(autouse=True)
def _reset_streamed():
    STREAMED.clear()


@pytest.mark.parametrize("failing", ["geocode", "forecast", "archive"])
@pytest.mark.parametrize("mode", list(FAILURES))
def test_a_failing_endpoint_gives_no_weather(client, fake_weather, failing, mode):
    fake_weather.script(failing, FAILURES[mode])
    today = date(2026, 9, 1) if failing == "archive" else TODAY  # archive: a typical-weather event

    assert summarise(client, at(18), at(22), today=today) is None


def test_a_redirect_is_not_followed(client, fake_weather):
    fake_weather.script("forecast", FAILURES["redirect"])

    summarise(client, at(18), at(22))

    assert [r.url.host for r in fake_weather.requests] == ["geocoding-api.open-meteo.com", "api.open-meteo.com"]


def test_an_oversized_stream_is_abandoned_once_over_the_cap(client, fake_weather):
    fake_weather.script("forecast", FAILURES["too_large_by_stream"])

    assert summarise(client, at(18), at(22)) is None
    assert len(STREAMED) < 20


@pytest.mark.parametrize(
    ("script", "outcome"),
    [("timeout", "timeout"), ("network", "network_error"), ("http_500", "http_error"), ("malformed_json", "malformed")],
)
def test_the_client_names_the_outcome_of_a_failed_call(client, fake_weather, script, outcome):
    fake_weather.script("forecast", FAILURES[script])

    with pytest.raises(openmeteo.WeatherFetchError) as e:
        client.hourly(openmeteo.FORECAST_URL, {}, ["uv_index"], client.budget())

    assert e.value.outcome == outcome


@pytest.mark.parametrize("script", ["timeout", "http_500", "too_large_by_header"])
def test_a_failing_air_quality_call_only_drops_the_aqi(client, fake_weather, script):
    fake_weather.script("air_quality", FAILURES[script])

    result = summarise(client, at(10), at(12))

    assert (result.aqi_max, result.uv_max) == (None, 5.0)


def test_one_failing_archive_year_gives_no_weather(client, fake_weather):
    def fifth_fails(request):
        return httpx.Response(500) if len(fake_weather.calls("archive")) == 5 else None

    original = fake_weather._hourly  # the generated reply for the four good years
    fake_weather.script("archive", lambda r: fifth_fails(r) or original(r))

    assert summarise(client, at(10), at(12), today=date(2026, 9, 1)) is None
    assert len(fake_weather.calls("archive")) == 5


def test_no_place_found_raises_and_asks_for_no_weather(client, fake_weather):
    fake_weather.script("geocode", {"results": []})

    with pytest.raises(PlaceNotFoundError):
        summarise(client, at(18), at(22))

    assert len(fake_weather.requests) == 1


def test_a_failing_geocode_is_unavailable_weather_not_an_unknown_place(client, fake_weather):
    fake_weather.script("geocode", FAILURES["timeout"])

    assert summarise(client, at(18), at(22)) is None


def test_no_client_gives_no_weather():
    assert summarise(None, at(18), at(22)) is None


# --- budget (AC-77) ----------------------------------------------------------------------------------------------


def test_budget_counts_down_from_its_deadline(clock):
    budget = openmeteo.Budget(deadline=clock() + 20.0, clock=clock)

    clock.advance(7.5)

    assert budget.remaining() == pytest.approx(12.5)


def test_a_new_budget_lasts_twenty_seconds(client, clock):
    assert client.budget().remaining() == 20.0
    clock.advance(5)
    assert client.budget().remaining() == 20.0


def test_call_timeouts_are_the_defaults_while_there_is_budget(client, fake_weather):
    client.geocode("Gurugram", client.budget())

    timeout = fake_weather.requests[0].extensions["timeout"]
    assert (timeout["connect"], timeout["read"]) == (5.0, 8.0)


def test_call_timeouts_shrink_to_the_remaining_budget(client, fake_weather, clock):
    budget = client.budget()
    clock.advance(17)

    client.geocode("Gurugram", budget)

    timeout = fake_weather.requests[0].extensions["timeout"]
    assert (timeout["connect"], timeout["read"]) == (3.0, 3.0)


@pytest.mark.parametrize("spent", [19.5, 20.0, 25.0])
def test_less_than_a_second_left_makes_no_call(client, fake_weather, clock, spent):
    budget = client.budget()
    clock.advance(spent)

    with pytest.raises(openmeteo.WeatherFetchError) as e:
        client.geocode("Gurugram", budget)

    assert (e.value.outcome, fake_weather.requests) == ("budget_exhausted", [])


def test_a_whole_summary_stops_calling_when_the_budget_runs_out(client, fake_weather, clock):
    fake_weather.on_call = lambda endpoint: clock.advance(5)  # geocode + 3 archive calls use the 20 s

    assert summarise(client, at(18), at(22), today=date(2026, 9, 1)) is None
    assert [r.url.host for r in fake_weather.requests] == ["geocoding-api.open-meteo.com"] + [
        "archive-api.open-meteo.com"
    ] * 3


def test_a_body_that_outlasts_the_budget_is_cut_off(client, fake_weather, clock):
    def slow_body():
        yield b'{"resu'
        clock.advance(25)
        yield b'lts": []}'

    fake_weather.script("geocode", lambda request: httpx.Response(200, content=slow_body()))

    with pytest.raises(openmeteo.WeatherFetchError) as e:
        client.geocode("Gurugram", client.budget())

    assert e.value.outcome == "budget_exhausted"


# --- cache (AC-78) -----------------------------------------------------------------------------------------------


def test_cache_lives_in_valkey_for_an_hour_under_a_weather_key(client, fake_weather, kv):
    summarise(client, at(18), at(22))
    assert len(fake_weather.requests) == 3  # geocode, forecast, air quality

    keys = kv.keys("*")

    assert len(keys) == 3 and all(re.fullmatch(r"weather:[0-9a-f]{64}", k) for k in keys)
    assert all(3590 < kv.ttl(k) <= 3600 for k in keys)
    summarise(client, at(18), at(22))
    assert len(fake_weather.requests) == 3  # within the hour: served from Valkey


def test_a_body_over_the_cache_cap_is_used_but_not_cached(client, fake_weather, kv):
    fake_weather.script("geocode", {**load("geocode.json"), "padding": "x" * openmeteo.MAX_CACHED_BODY_BYTES})

    for _ in range(2):
        assert client.geocode("Pune", client.budget()) is not None

    assert kv.keys("*") == []
    assert len(fake_weather.requests) == 2


def test_an_expired_entry_is_fetched_again(client, fake_weather, kv):
    summarise(client, at(18), at(22))

    kv.flushall()  # the hour is up (or Valkey was flushed, AC-24)
    summarise(client, at(18), at(22))

    assert len(fake_weather.requests) == 6


def test_two_clients_on_one_valkey_share_the_cache_even_after_a_restart(fake_weather, kv):
    def fresh() -> openmeteo.WeatherClient:
        return openmeteo.WeatherClient(
            httpx.Client(transport=httpx.MockTransport(fake_weather), follow_redirects=False), kv
        )

    summarise(fresh(), at(18), at(22))
    assert len(fake_weather.requests) == 3

    summarise(fresh(), at(18), at(22))  # another worker, or the backend after a restart

    assert len(fake_weather.requests) == 3


def test_the_key_and_the_value_hold_no_place_name(client, kv):
    client.geocode(SECRET_PLACE, client.budget())

    (key,) = kv.keys("*")

    assert SECRET_PLACE.casefold() not in key.casefold()


def test_valkey_down_is_a_miss_not_a_failure(client, fake_weather, valkey_fault):
    valkey_fault()

    first = summarise(client, at(18), at(22))
    second = summarise(client, at(18), at(22))

    assert first.source == second.source == "forecast"
    assert len(fake_weather.requests) == 6  # nothing could be cached, nothing failed


def test_a_cached_value_that_is_not_json_is_a_miss(client, fake_weather, kv):
    client.geocode("Pune", client.budget())
    (key,) = kv.keys("*")
    kv.set(key, "not json {", ex=60)

    assert client.geocode("Pune", client.budget()) is not None
    assert len(fake_weather.requests) == 2


def test_without_a_valkey_every_call_is_fetched(fake_weather):
    with httpx.Client(transport=httpx.MockTransport(fake_weather), follow_redirects=False) as http:
        bare = openmeteo.WeatherClient(http, None)
        bare.geocode("Pune", bare.budget())
        bare.geocode("Pune", bare.budget())

    assert len(fake_weather.requests) == 2


def test_cache_key_is_the_normalised_name_and_the_params(client, fake_weather):
    summarise(client, at(18), at(22), place="Gurugram")
    summarise(client, at(18), at(22), place="  gurugram ")
    assert len(fake_weather.calls("geocode")) == 1

    summarise(client, at(18), at(22, day=EVENT_DAY + timedelta(days=1)), place="Gurugram")  # other dates
    assert (len(fake_weather.calls("geocode")), len(fake_weather.calls("forecast"))) == (1, 2)


def test_a_failure_is_not_cached(client, fake_weather):
    fake_weather.script("forecast", FAILURES["http_500"])
    assert summarise(client, at(18), at(22)) is None

    fake_weather.script("forecast", None)
    assert summarise(client, at(18), at(22)).source == "forecast"


def test_a_place_with_no_results_is_cached_too(client, fake_weather):
    fake_weather.script("geocode", {"results": []})

    for _ in range(2):
        assert client.geocode("Nowhereville", client.budget()) is None

    assert len(fake_weather.requests) == 1


# --- logging (AC-76) ---------------------------------------------------------------------------------------------


def test_fetches_are_logged_without_place_coordinates_or_url(app, fake_weather, caplog):
    caplog.set_level(logging.DEBUG)
    fake_weather.script("forecast", FAILURES["timeout"])
    ctx = app.state.ctx

    summarise(ctx.weather, at(18), at(22), place=SECRET_PLACE)
    summarise(ctx.weather, at(18), at(22), place=SECRET_PLACE)  # geocode is a cache hit now

    for line in (
        r"event=weather_fetch endpoint=geocode outcome=ok latency_ms=\d+",
        r"event=weather_fetch endpoint=geocode outcome=cache_hit latency_ms=\d+",
        r"event=weather_fetch endpoint=forecast outcome=timeout latency_ms=\d+",
        r"event=weather_unavailable outcome=timeout",
    ):
        assert re.search(line, caplog.text), line
    for secret in (SECRET_PLACE, SECRET_PLACE.casefold(), "28.4", "77.0", "open-meteo", "latitude", "start_date", BOOM):
        assert secret not in caplog.text


@pytest.mark.parametrize("mode", ["http_500", "malformed_json", "redirect", "too_large_by_header"])
def test_failure_logs_name_the_outcome_only(app, fake_weather, caplog, mode):
    caplog.set_level(logging.DEBUG)
    fake_weather.script("geocode", FAILURES[mode])
    ctx = app.state.ctx

    summarise(ctx.weather, at(18), at(22), place=SECRET_PLACE)

    assert "event=weather_unavailable outcome=" in caplog.text
    for secret in (SECRET_PLACE.casefold(), "evil.example", "open-meteo", "steal", BOOM):
        assert secret not in caplog.text


# --- wiring ------------------------------------------------------------------------------------------------------


def test_app_builds_a_weather_client_on_the_given_transport(app, fake_weather):
    ctx = app.state.ctx

    place = ctx.weather.geocode("Gurugram", ctx.weather.budget())

    assert place == openmeteo.Place(GURUGRAM["latitude"], GURUGRAM["longitude"], "Asia/Kolkata")
    assert len(fake_weather.requests) == 1


def test_app_weather_client_does_not_follow_redirects(app, fake_weather):
    fake_weather.script("geocode", FAILURES["redirect"])
    ctx = app.state.ctx

    with pytest.raises(openmeteo.WeatherFetchError) as e:
        ctx.weather.geocode("Gurugram", ctx.weather.budget())

    assert (e.value.outcome, len(fake_weather.requests)) == ("http_error", 1)


def test_app_closes_the_weather_client_on_shutdown(app):
    ctx = app.state.ctx
    with TestClient(app, base_url="http://localhost"):
        pass

    with pytest.raises(RuntimeError, match="closed"):
        ctx.weather.geocode("Another place", ctx.weather.budget())


# --- suggest: "did you mean" for a misspelt place --------------------------------------------------------------------


def test_suggest_shortens_the_name_until_the_geocoder_matches(client, fake_weather):
    seen: list[str] = []

    def by_prefix(request):
        name = request.url.params["name"]
        seen.append(name)
        results = (
            [{"name": "Gurugram", "admin1": "Haryana", "country": "India", **GURUGRAM}] if name == "gurugr" else []
        )
        return {"results": results}

    fake_weather.script("geocode", by_prefix)

    assert client.suggest("Gurugrm", client.budget()) == ["Gurugram, Haryana, India"]
    assert seen == ["gurugrm", "gurugr", "gurug", "guru"]  # every stem down to four letters, ranked together


def test_suggest_gives_up_below_four_letters(client, fake_weather):
    fake_weather.script("geocode", {"results": []})

    assert client.suggest("Zqx", client.budget()) == []
    assert client.suggest("Zqxwv", client.budget()) == []
    assert [r.url.params["name"] for r in fake_weather.calls("geocode")] == ["zqxwv", "zqxw"]


def test_suggest_tries_at_most_six_stems_for_a_long_name(client, fake_weather):
    fake_weather.script("geocode", {"results": []})

    client.suggest("Zyxwvutsrqponm", client.budget())

    assert len(fake_weather.calls("geocode")) == 6


def test_suggest_ranks_by_likeness_to_the_typed_name_then_population(client, fake_weather):
    def by_prefix(request):
        name = request.url.params["name"]
        if name == "bang":
            return {
                "results": [
                    {**GURUGRAM, "name": "Bangkok", "country": "Thailand", "population": 5_000_000},
                    {
                        **GURUGRAM,
                        "name": "Bangalore",
                        "admin1": "Karnataka",
                        "country": "India",
                        "population": 5_000_000,
                    },
                ]
            }
        if name == "banglor":
            return {"results": [{**GURUGRAM, "name": "Banglor", "country": "Liberia", "population": 100}]}
        return {"results": []}

    fake_weather.script("geocode", by_prefix)

    assert client.suggest("Banglore", client.budget())[:2] == ["Bangalore, Karnataka, India", "Banglor, Liberia"]


def test_an_old_city_name_finds_the_city_under_its_current_name(client, fake_weather):
    def by_name(request):
        name = request.url.params["name"]
        if name == "bengaluru":
            return {"results": [{**GURUGRAM, "name": "Bengaluru", "admin1": "Karnataka", "country": "India"}]}
        return {"results": []}

    fake_weather.script("geocode", by_name)

    assert client.suggest("Banglore", client.budget()) == ["Bengaluru, Karnataka, India"]
    assert fake_weather.calls("geocode")[0].url.params["name"] == "bengaluru"


def test_a_big_city_outranks_a_village_that_spells_slightly_closer(client, fake_weather):
    fake_weather.script(
        "geocode",
        {
            "results": [
                {**GURUGRAM, "name": "Mumba", "country": "Zambia", "population": 900},
                {**GURUGRAM, "name": "Mumbai", "admin1": "Maharashtra", "country": "India", "population": 12_000_000},
            ]
        },
    )

    assert client.suggest("Mumbay", client.budget())[0] == "Mumbai, Maharashtra, India"


def test_suggest_lists_results_with_their_region_and_country(client, fake_weather):
    fake_weather.script(
        "geocode",
        {
            "results": [
                {**GURUGRAM, "name": "Pune", "admin1": "Maharashtra", "country": "India"},
                {**GURUGRAM, "name": "Puno", "country": "Peru"},
                {**GURUGRAM, "name": ""},
            ]
        },
    )

    assert client.suggest("Pune", client.budget()) == ["Pune, Maharashtra, India", "Puno, Peru"]
