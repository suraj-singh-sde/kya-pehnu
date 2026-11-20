"""What the weather will be like during an event, summarised for outfit constraints."""

import logging
from collections import defaultdict
from collections.abc import Sequence
from datetime import date, datetime, time, timedelta
from statistics import fmean
from typing import Final, Literal

from pydantic import BaseModel, field_validator

from kyapehnu.core.config import WeatherPrefs
from kyapehnu.core.errors import PlaceNotFoundError
from kyapehnu.weather.openmeteo import (
    AIR_URL,
    ARCHIVE_URL,
    FORECAST_URL,
    Budget,
    Place,
    WeatherClient,
    WeatherFetchError,
)

log = logging.getLogger(__name__)

ROUND_DIGITS = 1
FORECAST_PAST_DAYS: Final = 92  # how far back the forecast endpoint reaches
FORECAST_AHEAD_DAYS: Final = 14
AIR_AHEAD_DAYS: Final = 6
TYPICAL_YEARS: Final = 5
TYPICAL_SHIFTS: Final = (timedelta(days=-1), timedelta(0), timedelta(days=1))  # neighbouring days widen the sample
FORECAST_VARIABLES: Final = (
    "temperature_2m",
    "apparent_temperature",
    "relative_humidity_2m",
    "precipitation_probability",
    "wind_speed_10m",
    "uv_index",
)
AIR_VARIABLES: Final = ("us_aqi",)
ARCHIVE_VARIABLES: Final = ("apparent_temperature", "relative_humidity_2m", "precipitation", "wind_speed_10m")
ONE_HOUR: Final = timedelta(hours=1)


class WeatherSummary(BaseModel):
    source: Literal["forecast", "typical"]
    feels_like_min_c: float
    feels_like_max_c: float
    humidity_mean_pct: float | None
    rain_probability_max_pct: float | None
    wind_max_kmh: float | None
    uv_max: float | None
    aqi_max: float | None

    @field_validator("*", mode="after")
    @classmethod
    def _round(cls, v: object) -> object:
        return round(v, ROUND_DIGITS) if isinstance(v, float) else v


def event_window(starts_at: datetime, ends_at: datetime | None, default_hours: int) -> tuple[datetime, datetime]:
    return starts_at, ends_at if ends_at is not None else starts_at + timedelta(hours=default_hours)


def for_event(
    client: WeatherClient | None,
    place: str,
    starts_at: datetime,
    ends_at: datetime | None,
    today: date,
    prefs: WeatherPrefs,
) -> WeatherSummary | None:
    """The weather during the event: a forecast when the dates are in reach, else the typical weather of past years.

    None when there is no client or any weather call fails. An unknown place raises PlaceNotFoundError.
    """
    if client is None:
        return None
    start, end = event_window(starts_at, ends_at, prefs.default_event_hours)
    hours = _event_hours(start, end)
    first, last = start.date(), (end - timedelta(microseconds=1)).date()
    budget = client.budget()
    try:
        found = client.geocode(place, budget)
        if found is None:
            raise PlaceNotFoundError()
        if _in_reach(first, last, today, FORECAST_AHEAD_DAYS):
            return _forecast(client, found, first, last, hours, today, budget)
        return _typical(client, found, first, last, hours, today, budget)
    except WeatherFetchError as e:
        log.warning("event=weather_unavailable outcome=%s", e.outcome)
        return None


def _event_hours(start: datetime, end: datetime) -> list[datetime]:
    """The whole hours that touch the event: 18:00 to 22:00 gives 18, 19, 20, 21."""
    hours: list[datetime] = []
    t = start.replace(minute=0, second=0, microsecond=0)
    while t < end:
        hours.append(t)
        t += ONE_HOUR
    return hours


def _in_reach(first: date, last: date, today: date, ahead_days: int) -> bool:
    return first >= today - timedelta(days=FORECAST_PAST_DAYS) and last <= today + timedelta(days=ahead_days)


def _params(place: Place, first: date, last: date, variables: Sequence[str]) -> dict[str, str]:
    return {
        "latitude": f"{place.latitude:.2f}",
        "longitude": f"{place.longitude:.2f}",
        "timezone": place.timezone,
        "start_date": first.isoformat(),
        "end_date": last.isoformat(),
        "hourly": ",".join(variables),
    }


def _forecast(
    client: WeatherClient, place: Place, first: date, last: date, hours: list[datetime], today: date, budget: Budget
) -> WeatherSummary:
    forecast = dict(
        client.hourly(FORECAST_URL, _params(place, first, last, FORECAST_VARIABLES), FORECAST_VARIABLES, budget)
    )
    aqi: list[float] = []
    if _in_reach(first, last, today, AIR_AHEAD_DAYS):
        try:
            air = dict(client.hourly(AIR_URL, _params(place, first, last, AIR_VARIABLES), AIR_VARIABLES, budget))
        except WeatherFetchError as e:  # air quality is optional: the rest of the summary stays
            log.warning("event=air_quality_unavailable outcome=%s", e.outcome)
        else:
            aqi = _present(air, hours, "us_aqi")

    def values(variable: str) -> list[float]:
        return _present(forecast, hours, variable)

    feels = values("apparent_temperature")
    if not feels:
        raise WeatherFetchError("malformed")
    humidity = values("relative_humidity_2m")
    return WeatherSummary(
        source="forecast",
        feels_like_min_c=min(feels),
        feels_like_max_c=max(feels),
        humidity_mean_pct=fmean(humidity) if humidity else None,
        rain_probability_max_pct=_max(values("precipitation_probability")),
        wind_max_kmh=_max(values("wind_speed_10m")),
        uv_max=_max(values("uv_index")),
        aqi_max=_max(aqi),
    )


def _present(series: dict[datetime, dict[str, float | None]], hours: list[datetime], variable: str) -> list[float]:
    found = (series[t][variable] for t in hours if t in series)
    return [v for v in found if v is not None]


def _max(values: list[float]) -> float | None:
    return max(values) if values else None


def _same_day_in(d: date, year: int) -> date:
    return d.replace(year=year, day=28) if (d.month, d.day) == (2, 29) else d.replace(year=year)


def _typical(
    client: WeatherClient, place: Place, first: date, last: date, hours: list[datetime], today: date, budget: Budget
) -> WeatherSummary:
    """Average the same hours of the last five years, and the days around them, as a stand-in for a forecast."""
    feels: defaultdict[int, list[float]] = defaultdict(list)  # by position in the event
    wind: defaultdict[int, list[float]] = defaultdict(list)
    humidity: list[float] = []
    precipitation: list[float] = []
    for year in range(today.year - TYPICAL_YEARS, today.year):
        mapped = [_same_day_in(d, year + d.year - first.year) for d in (first, last)]
        archive = dict(
            client.hourly(
                ARCHIVE_URL,
                _params(place, min(mapped) - timedelta(days=1), max(mapped) + timedelta(days=1), ARCHIVE_VARIABLES),
                ARCHIVE_VARIABLES,
                budget,
            )
        )
        for position, t in enumerate(hours):
            day = _same_day_in(t.date(), year + t.year - first.year)
            for shift in TYPICAL_SHIFTS:
                sample = archive.get(datetime.combine(day + shift, time(t.hour)))
                if sample is None:
                    continue
                _add(feels[position], sample["apparent_temperature"])
                _add(wind[position], sample["wind_speed_10m"])
                _add(humidity, sample["relative_humidity_2m"])
                _add(precipitation, sample["precipitation"])
    feels_by_hour = [fmean(v) for v in feels.values() if v]
    if not feels_by_hour:
        raise WeatherFetchError("malformed")
    return WeatherSummary(
        source="typical",
        feels_like_min_c=min(feels_by_hour),
        feels_like_max_c=max(feels_by_hour),
        humidity_mean_pct=fmean(humidity) if humidity else None,
        rain_probability_max_pct=100 * sum(p > 0 for p in precipitation) / len(precipitation)
        if precipitation
        else None,
        wind_max_kmh=_max([fmean(v) for v in wind.values() if v]),
        uv_max=None,
        aqi_max=None,
    )


def _add(samples: list[float], value: float | None) -> None:
    if value is not None:
        samples.append(value)
