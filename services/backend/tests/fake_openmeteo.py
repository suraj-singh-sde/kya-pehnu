"""A scripted stand-in for the four Open-Meteo hosts, to be used as an `httpx.MockTransport` handler.

    fake = FakeOpenMeteo()
    fake.script("forecast", 500)                      # an HTTP status
    fake.script("archive", httpx.ReadTimeout("slow")) # raised by the transport
    fake.script("geocode", {"results": []})           # a JSON body (200)
    fake.values["uv_index"] = lambda t: 3.0           # what the generated hourly replies hold
    transport = httpx.MockTransport(fake)

Unscripted hosts answer with a Gurugram geocoding result or generated hourly series for the requested dates.
"""

from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any

import httpx

HOSTS = {
    "geocode": "geocoding-api.open-meteo.com",
    "forecast": "api.open-meteo.com",
    "air_quality": "air-quality-api.open-meteo.com",
    "archive": "archive-api.open-meteo.com",
}
GURUGRAM = {"latitude": 28.4595, "longitude": 77.0266, "timezone": "Asia/Kolkata", "name": "Gurugram"}
DEFAULT_VALUES: dict[str, float] = {
    "temperature_2m": 28.0,
    "apparent_temperature": 26.0,
    "relative_humidity_2m": 60.0,
    "precipitation_probability": 10.0,
    "precipitation": 0.0,
    "wind_speed_10m": 10.0,
    "uv_index": 5.0,
    "us_aqi": 80.0,
}

Reply = (
    Any  # dict | list (JSON, 200) | int (status) | bytes (200 body) | Exception (raised) | httpx.Response | callable
)


class FakeOpenMeteo:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.on_call: Callable[[str], None] | None = None  # called with the endpoint name before each reply
        self.values: dict[str, Any] = dict(DEFAULT_VALUES)  # variable -> number, or a function of the hour (datetime)
        self._scripts: dict[str, Reply] = {}

    def script(self, endpoint: str, reply: Reply) -> None:
        """Set the reply for an endpoint; None goes back to the default."""
        if reply is None:
            self._scripts.pop(endpoint, None)
        else:
            self._scripts[endpoint] = reply

    def calls(self, endpoint: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.host == HOSTS[endpoint]]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        endpoint = next((name for name, host in HOSTS.items() if host == request.url.host), None)
        if endpoint is None:
            raise AssertionError(f"request to a host that is not Open-Meteo: {request.url.host}")
        self.requests.append(request)
        if self.on_call is not None:
            self.on_call(endpoint)
        reply = self._scripts.get(endpoint)
        if reply is None:
            reply = {"results": [GURUGRAM]} if endpoint == "geocode" else self._hourly(request)
        return self._response(reply, request)

    def _response(self, reply: Reply, request: httpx.Request) -> httpx.Response:
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, httpx.Response):
            return reply
        if callable(reply):
            return self._response(reply(request), request)
        if isinstance(reply, bool):
            raise TypeError("a reply is not a bool")
        if isinstance(reply, int):
            return httpx.Response(reply)
        if isinstance(reply, bytes):
            return httpx.Response(200, content=reply)
        return httpx.Response(200, json=reply)

    def _hourly(self, request: httpx.Request) -> httpx.Response:
        q = request.url.params
        first, last = date.fromisoformat(q["start_date"]), date.fromisoformat(q["end_date"])
        hours = [
            datetime.combine(first + timedelta(days=d), datetime.min.time()) + timedelta(hours=h)
            for d in range((last - first).days + 1)
            for h in range(24)
        ]
        hourly: dict[str, Any] = {"time": [t.strftime("%Y-%m-%dT%H:%M") for t in hours]}
        for variable in q["hourly"].split(","):
            value = self.values.get(variable, 0.0)
            hourly[variable] = [value(t) if callable(value) else value for t in hours]
        return httpx.Response(200, json={"timezone": q["timezone"], "hourly": hourly})
