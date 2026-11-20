"""The WeatherSummary model (LLD 3.2). The client and the aggregation are tested in test_weather.py."""

import pytest
from pydantic import ValidationError

from kyapehnu.weather.summary import WeatherSummary

FIELDS = {
    "source": "forecast",
    "feels_like_min_c": 21.04,
    "feels_like_max_c": 30.96,
    "humidity_mean_pct": 61.26,
    "rain_probability_max_pct": 40.0,
    "wind_max_kmh": 12.34,
    "uv_max": 7.0,
    "aqi_max": 150.55,
}


def test_summary_rounds_every_value_to_one_decimal():
    s = WeatherSummary(**FIELDS)

    assert (s.feels_like_min_c, s.feels_like_max_c) == (21.0, 31.0)
    assert (s.humidity_mean_pct, s.wind_max_kmh) == (61.3, 12.3)


def test_summary_keeps_missing_metrics_as_none():
    s = WeatherSummary(**{**FIELDS, "uv_max": None, "aqi_max": None, "humidity_mean_pct": None})

    assert (s.uv_max, s.aqi_max, s.humidity_mean_pct) == (None, None, None)


@pytest.mark.parametrize("source", ["forecast", "typical"])
def test_summary_accepts_the_two_sources(source):
    assert WeatherSummary(**{**FIELDS, "source": source}).source == source


def test_summary_rejects_an_unknown_source():
    with pytest.raises(ValidationError):
        WeatherSummary(**{**FIELDS, "source": "average"})


@pytest.mark.parametrize("field", ["feels_like_min_c", "feels_like_max_c"])
def test_summary_requires_the_feels_like_range(field):
    data = {k: v for k, v in FIELDS.items() if k != field}

    with pytest.raises(ValidationError):
        WeatherSummary(**data)
