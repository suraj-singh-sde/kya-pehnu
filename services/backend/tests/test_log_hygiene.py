"""Log hygiene (AC-36, AC-49 unit part; LLD 9.2): at DEBUG, no record written during sign-in or a weather lookup carries
a query value, an email, a Google name, a token or a session value. `create_app` keeps the `httpx`/`httpcore` loggers
at WARNING because they print full request URLs."""

import logging
from urllib.parse import parse_qs, urlsplit

from tests.conftest import OAUTH_COOKIE, SESSION_COOKIE
from tests.fake_google import Person

CALLBACK = "/api/auth/google/callback"
LOGIN = "/api/auth/google/login"
MARKERS = ["zzmark-next", "zzmark-code", "zzmark-state", "zzmark-error", "zzmark-description"]


def test_the_chatty_http_loggers_are_quiet(app):
    for name in ("httpx", "httpcore"):
        assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING


def test_sign_in_writes_no_query_value_email_name_token_or_session_to_the_logs(app, anon_client, google, caplog):
    caplog.set_level(logging.DEBUG)
    person = Person(sub="g-hygiene", email="hygiene.person@example.com", name="Hygiene Personname")

    start = anon_client.get(LOGIN, params={"next": "/zzmark-next"}, follow_redirects=False)
    oauth_cookie = anon_client.cookies[OAUTH_COOKIE]
    callback = google.approve(start.headers["location"], person)
    query = parse_qs(urlsplit(callback).query)
    done = anon_client.get(callback, follow_redirects=False)
    assert done.headers["location"] == "/zzmark-next"  # the sign-in worked, so its success path was logged too
    session = anon_client.cookies[SESSION_COOKIE]
    # failures that carry markers in every query value
    anon_client.get(LOGIN, params={"next": "/zzmark-next"}, follow_redirects=False)
    anon_client.get(CALLBACK, params={"code": "zzmark-code", "state": "zzmark-state"}, follow_redirects=False)
    anon_client.get(
        CALLBACK,
        params={"error": "zzmark-error", "error_description": "zzmark-description", "state": "zzmark-state"},
        follow_redirects=False,
    )
    anon_client.get("/api/auth/me")
    anon_client.post("/api/auth/logout")

    forbidden = [
        *MARKERS,
        person.email,
        person.name,
        session,
        oauth_cookie,
        query["code"][0],
        query["state"][0],
        *google.secrets,
    ]
    assert "event=signin_ok" in caplog.text  # the log was really written
    for value in forbidden:
        assert value not in caplog.text, value


def test_a_weather_lookup_for_a_place_leaves_the_place_out_of_the_logs(client_a, fake_weather, caplog):
    caplog.set_level(logging.DEBUG)

    client_a.get("/api/weather", params={"place": "zzmarkerplace"})

    assert any("zzmarkerplace" in str(r.url) for r in fake_weather.calls("geocode"))  # the lookup really happened
    assert "zzmarkerplace" not in caplog.text
