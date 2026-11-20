"""Open-Meteo client: geocoding, hourly forecast, air quality and archive, bounded and cached.

One call budget covers a whole summary, bodies are capped, redirects are never followed (the shared `httpx.Client`
has `follow_redirects=False`), and a successful reply is cached for an hour. Nothing that identifies the place (name,
coordinates, URL, parameters) or an exception text reaches a log line or a `WeatherFetchError`.
"""

import hashlib
import json
import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
from typing import Any, Final, cast

import httpx
import redis
from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

log = logging.getLogger(__name__)

GEOCODE_URL: Final = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL: Final = "https://api.open-meteo.com/v1/forecast"
AIR_URL: Final = "https://air-quality-api.open-meteo.com/v1/air-quality"
ARCHIVE_URL: Final = "https://archive-api.open-meteo.com/v1/archive"
ENDPOINT_NAMES: Final = {
    GEOCODE_URL: "geocode",
    FORECAST_URL: "forecast",
    AIR_URL: "air_quality",
    ARCHIVE_URL: "archive",
}
CONNECT_TIMEOUT_S: Final = 5.0
READ_TIMEOUT_S: Final = 8.0
BUDGET_S: Final = 20.0
MIN_CALL_S: Final = 1.0  # less than this left: do not start another call
MAX_BODY_BYTES: Final = 1_048_576  # decoded bytes
CACHE_TTL_S: Final = 3600  # seconds, as the Valkey key's expiry
MAX_CACHED_BODY_BYTES: Final = 262_144  # a larger body is used once and not cached: Valkey has 128 MB for all keys
MIN_SUGGEST_CHARS: Final = 4  # shorter stems match half the world
SUGGEST_FETCH: Final = 10  # matches asked for per stem, before ranking
SUGGEST_STEMS: Final = 5  # how many shorter stems to try: one geocode call each (cached an hour)
BIG_CITY: Final = 10_000_000  # a city this size gets the full population bonus
# Names people still use for cities the geocoder knows by their current names.
OLD_NAMES: Final = {
    "bangalore": "Bengaluru",
    "bombay": "Mumbai",
    "madras": "Chennai",
    "calcutta": "Kolkata",
    "gurgaon": "Gurugram",
    "poona": "Pune",
    "baroda": "Vadodara",
    "trivandrum": "Thiruvananthapuram",
    "cochin": "Kochi",
    "mysore": "Mysuru",
    "mangalore": "Mangaluru",
    "allahabad": "Prayagraj",
    "delhi": "New Delhi",
}
OLD_NAME_MATCH: Final = 0.8  # how close the typed name must be to an old name to look its new name up


def _likeness(typed: str, name: str, population: int) -> float:
    """How much a place looks like what was typed, with a nudge (up to 0.1) for big cities: "Mumba" (a village)
    spells closer to "Mumbay" than "Mumbai" does, and the city is the one people mean."""
    nudge = 0.1 * min(population, BIG_CITY) / BIG_CITY
    return SequenceMatcher(None, typed, name.casefold()).ratio() + nudge


Hour = tuple[datetime, dict[str, float | None]]  # naive local time, values by variable

_TIMES = TypeAdapter(list[datetime])
# json.loads accepts NaN and Infinity; they must not reach the arithmetic or the response
_FINITE: Final = ConfigDict(allow_inf_nan=False)
_VALUES = TypeAdapter(list[float | None], config=_FINITE)


class WeatherFetchError(Exception):
    """A weather call failed. `outcome` is a short word for the log; it never leaves the weather package."""

    def __init__(self, outcome: str) -> None:
        super().__init__(outcome)
        self.outcome = outcome


@dataclass(frozen=True)
class Place:
    latitude: float
    longitude: float
    timezone: str


class _GeoResult(BaseModel):
    model_config = _FINITE

    latitude: float
    longitude: float
    timezone: str
    name: str = ""
    admin1: str = ""
    country: str = ""
    population: int = 0


class _GeoBody(BaseModel):
    results: list[_GeoResult] | None = None


class Budget:
    """The time left for all the calls of one summary."""

    def __init__(self, deadline: float, clock: Callable[[], float]) -> None:
        self._deadline, self._clock = deadline, clock

    def remaining(self) -> float:
        return self._deadline - self._clock()


def _cache_key(url: str, params: dict[str, str]) -> str:
    """One key per request: the hash keeps place names and coordinates out of Valkey (R1)."""
    return "weather:" + hashlib.sha256((url + json.dumps(sorted(params.items()))).encode()).hexdigest()


class WeatherClient:
    def __init__(self, http: httpx.Client, kv: redis.Redis | None, clock: Callable[[], float] = time.monotonic) -> None:
        self._http, self._kv, self._clock = http, kv, clock  # no Valkey (tests): nothing is cached

    def budget(self) -> Budget:
        return Budget(self._clock() + BUDGET_S, self._clock)

    def close(self) -> None:
        self._http.close()

    def geocode(self, name: str, budget: Budget) -> Place | None:
        """The first match for the name, or None when there is none."""
        params = {"name": name.strip().casefold(), "count": "1", "language": "en", "format": "json"}
        body = self._get_json(GEOCODE_URL, params, budget)
        try:
            results = _GeoBody.model_validate(body).results
        except ValidationError as e:
            raise WeatherFetchError("malformed") from e
        if not results:
            return None
        first = results[0]
        return Place(first.latitude, first.longitude, first.timezone)

    def suggest(self, name: str, budget: Budget, limit: int = 5) -> list[str]:
        """Place names close to a misspelt one, "Gurugram, Haryana, India" style, closest first.

        The geocoder matches prefixes only, so the name is shortened from the end (down to MIN_SUGGEST_CHARS) and
        every match is ranked by how much it looks like what was typed, then by population."""
        typed = name.strip().casefold()
        seen: set[str] = set()
        found: list[tuple[float, int, str]] = []

        def add(stem: str, count: int, as_if: str = "") -> None:
            params = {"name": stem, "count": str(count), "language": "en", "format": "json"}
            try:
                results = _GeoBody.model_validate(self._get_json(GEOCODE_URL, params, budget)).results or []
            except ValidationError as e:
                raise WeatherFetchError("malformed") from e
            for r in results:
                label = ", ".join(p for p in (r.name, r.admin1, r.country) if p)
                if not r.name or label in seen:
                    continue
                seen.add(label)
                found.append((_likeness(typed, as_if or r.name, r.population), r.population, label))

        for old, new in OLD_NAMES.items():  # "Banglore" is Bengaluru to the geocoder
            if SequenceMatcher(None, typed, old).ratio() >= OLD_NAME_MATCH:
                add(new.casefold(), 1, as_if=old)
        for end in range(len(typed), max(MIN_SUGGEST_CHARS, len(typed) - SUGGEST_STEMS) - 1, -1):
            add(typed[:end], SUGGEST_FETCH)
        found.sort(key=lambda t: (-t[0], -t[1]))
        return [label for _, _, label in found[:limit]]

    def hourly(self, url: str, params: dict[str, str], variables: Sequence[str], budget: Budget) -> list[Hour]:
        body = self._get_json(url, params, budget)
        try:
            block = body["hourly"]
            times = _TIMES.validate_python(block["time"])
            columns = [_VALUES.validate_python(block[v]) for v in variables]
        except (ValidationError, KeyError, TypeError) as e:
            raise WeatherFetchError("malformed") from e
        if any(len(column) != len(times) for column in columns):
            raise WeatherFetchError("malformed")
        return [(t, {v: column[i] for v, column in zip(variables, columns, strict=True)}) for i, t in enumerate(times)]

    def _get_json(self, url: str, params: dict[str, str], budget: Budget) -> dict[str, Any]:
        endpoint, started, key = ENDPOINT_NAMES[url], self._clock(), _cache_key(url, params)
        cached = self._lookup(key)
        if cached is not None:
            self._log(endpoint, "cache_hit", started)
            return cached
        try:
            body = self._fetch(url, params, budget)
        except WeatherFetchError as e:
            self._log(endpoint, e.outcome, started)
            raise
        self._store(key, body)
        self._log(endpoint, "ok", started)
        return body

    def _fetch(self, url: str, params: dict[str, str], budget: Budget) -> dict[str, Any]:
        remaining = budget.remaining()
        if remaining < MIN_CALL_S:
            raise WeatherFetchError("budget_exhausted")
        timeout = httpx.Timeout(min(READ_TIMEOUT_S, remaining), connect=min(CONNECT_TIMEOUT_S, remaining))
        try:
            with self._http.stream("GET", url, params=params, timeout=timeout) as response:
                if not response.is_success:  # a 3xx is never followed
                    raise WeatherFetchError("http_error")
                raw = self._read_capped(response, budget)
        except httpx.TimeoutException as e:
            raise WeatherFetchError("timeout") from e
        except httpx.HTTPError as e:
            raise WeatherFetchError("network_error") from e
        try:
            body = json.loads(raw)
        except (ValueError, RecursionError) as e:  # deeply nested JSON exhausts the parser's stack
            raise WeatherFetchError("malformed") from e
        if not isinstance(body, dict):
            raise WeatherFetchError("malformed")
        return body

    @staticmethod
    def _read_capped(response: httpx.Response, budget: Budget) -> bytes:
        declared = response.headers.get("Content-Length", "")
        if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
            raise WeatherFetchError("too_large")
        chunks: list[bytes] = []
        size = 0
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > MAX_BODY_BYTES:
                raise WeatherFetchError("too_large")
            if budget.remaining() <= 0:
                raise WeatherFetchError("budget_exhausted")
            chunks.append(chunk)
        return b"".join(chunks)

    def _lookup(self, key: str) -> dict[str, Any] | None:
        """The cached body, or None. Valkey down or a damaged value is a miss: the weather is simply fetched."""
        if self._kv is None:
            return None
        try:
            raw = cast(str | None, self._kv.get(key))  # decode_responses=True
            body = json.loads(raw) if raw is not None else None
        except (redis.RedisError, ValueError):
            return None
        return body if isinstance(body, dict) else None

    def _store(self, key: str, body: dict[str, Any]) -> None:
        if self._kv is None:
            return
        value = json.dumps(body)
        if len(value) > MAX_CACHED_BODY_BYTES:
            return
        try:
            self._kv.set(key, value, ex=CACHE_TTL_S)
        except redis.RedisError:
            pass  # not cached; the next lookup fetches again

    def _log(self, endpoint: str, outcome: str, started: float) -> None:
        latency_ms = int((self._clock() - started) * 1000)
        log.info("event=weather_fetch endpoint=%s outcome=%s latency_ms=%d", endpoint, outcome, latency_ms)
