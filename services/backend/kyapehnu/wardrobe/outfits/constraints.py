"""What the weather asks of an outfit: warmth band, layer, fabrics to avoid, rain, and the notes shown to the user."""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

from kyapehnu.core.config import WeatherPrefs
from kyapehnu.wardrobe.models import Item
from kyapehnu.weather.summary import WeatherSummary

Layer = Literal["none", "optional", "wanted"]

HUMID_NOTE = "Hot and humid: breathable fabrics, no layers."
RAIN_NOTE = "Rain likely: rain-safe footwear."
SUN_NOTE = "Strong sun: cover up, cap or sunglasses."
AIR_NOTE = "Poor air quality: a mask-friendly outfit."
WIND_NOTE = "Windy: avoid loose layers."


@dataclass(frozen=True)
class Constraints:
    warmth_min: int
    warmth_max: int
    layer: Layer
    marked_down: frozenset[str]
    rain_likely: bool
    notes: tuple[str, ...]


def derive(summary: WeatherSummary, outdoors: bool, prefs: WeatherPrefs) -> Constraints:
    mid = (summary.feels_like_min_c + summary.feels_like_max_c) / 2
    band = next(b for b in prefs.bands if b.from_c is None or mid >= b.from_c)
    humidity, rain, wind = summary.humidity_mean_pct, summary.rain_probability_max_pct, summary.wind_max_kmh
    uv, aqi = summary.uv_max, summary.aqi_max  # a metric that is None never fires its rule

    humid = humidity is not None and humidity >= prefs.humid_pct and mid >= prefs.humid_feels_like_c
    rain_likely = rain is not None and rain > prefs.rain_probability_pct
    fired = (
        (humid, HUMID_NOTE),
        (rain_likely, RAIN_NOTE),
        (outdoors and uv is not None and uv >= prefs.uv_index, SUN_NOTE),
        (outdoors and aqi is not None and aqi > prefs.aqi, AIR_NOTE),
        (wind is not None and wind > prefs.wind_kmh, WIND_NOTE),
    )
    return Constraints(
        warmth_min=band.warmth_min,
        warmth_max=band.warmth_max,
        layer=band.layer,
        marked_down=frozenset(f.casefold() for f in prefs.humid_fabrics) if humid else frozenset(),
        rain_likely=rain_likely,
        notes=tuple(note for on, note in fired if on),
    )


@lru_cache(maxsize=32)
def _any_word(words: tuple[str, ...]) -> re.Pattern[str]:
    return re.compile(rf"\b(?:{'|'.join(re.escape(w) for w in words)})\b", re.I)


def _mentions(words: Sequence[str], text: str) -> bool:
    return bool(words) and _any_word(tuple(words)).search(text) is not None


def is_marked_down(item: Item, cons: Constraints) -> bool:
    return _mentions(sorted(cons.marked_down), item.fabric or "")


def is_rain_safe(item: Item, words: Sequence[str]) -> bool:
    return not _mentions(words, " ".join([item.fabric or "", item.name, item.subcategory, *item.tags]))
