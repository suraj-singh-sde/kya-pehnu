"""The /api/auth routes (LLD 3.1, 3.2, 4.2; AC-4 to AC-7, AC-9, AC-10, AC-12, AC-13, AC-15).

Google is the in-process fake of tests/fake_google.py, so authlib's real flow runs (state, nonce, PKCE, ID-token
checks). The failures the fake cannot produce (an exception type authlib might raise) are injected by patching
authlib's `authorize_access_token` / `load_server_metadata`."""

import hashlib
import logging
import re
import sqlite3
import time
import zipfile
from urllib.parse import parse_qs, urlsplit

import httpx
import httpx2
import itsdangerous.timed
import pytest
from authlib.integrations.base_client.errors import MismatchingStateError, OAuthError
from authlib.integrations.starlette_client.apps import StarletteOAuth2App
from fastapi.testclient import TestClient
from joserfc.errors import BadSignatureError, DecodeError
from pydantic import SecretStr

from kyapehnu.core.backup import backup
from tests.conftest import OAUTH_COOKIE, SESSION_COOKIE, sign_in
from tests.fake_google import CLIENT_ID, CLIENT_SECRET, FakeGoogle, Person, complete_sign_in

CALLBACK = "/api/auth/google/callback"
LOGIN = "/api/auth/google/login"
SESSION_30_DAYS = 30 * 24 * 3600


def cookie_attrs(response, name):
    """(value, {attribute: value}) of the Set-Cookie header for `name`; attributes are lower case."""
    for header in response.headers.get_list("set-cookie"):
        first, *rest = [part.strip() for part in header.split(";")]
        if first.startswith(name + "="):
            attrs = {}
            for part in rest:
                key, _, value = part.partition("=")
                attrs[key.lower()] = value
            return first.split("=", 1)[1], attrs
    raise AssertionError(f"no Set-Cookie for {name}: {response.headers.get_list('set-cookie')}")


def users(app):
    with app.state.ctx.engine.connect() as conn:
        return conn.exec_driver_sql('SELECT id, email, is_admin FROM "user" ORDER BY id').all()


def me(client):
    r = client.get("/api/auth/me")
    assert r.status_code == 200, r.text
    return r.json()


def start(client, next=None):
    """Sign-in start; returns (state, the callback URL Google would send the browser to is built by the caller)."""
    r = client.get(LOGIN, params={} if next is None else {"next": next}, follow_redirects=False)
    assert r.status_code == 302
    return r.headers["location"]


def assert_refused(response, reason, client, kv, app, caplog, markers=()):
    """A failed sign-in: a redirect to the sign-in page with the reason, no account, no session, state cookie gone."""
    assert response.status_code == 302
    assert response.headers["location"] == f"/signin?error={reason}"
    assert SESSION_COOKIE not in client.cookies
    assert OAUTH_COOKIE not in client.cookies
    assert kv.keys("session:*") == []
    assert users(app) == []
    assert f"event=signin_failed reason={reason}" in caplog.text
    for marker in markers:
        assert marker not in caplog.text


@pytest.fixture(autouse=True)
def debug_logs(caplog):
    caplog.set_level(logging.DEBUG)


# ---- sign-in start ---------------------------------------------------------------------------------------------


def test_sign_in_start_sends_the_browser_to_google_with_state_nonce_and_pkce(anon_client, google):
    url = start(anon_client)

    parts = urlsplit(url)
    q = {k: v[0] for k, v in parse_qs(parts.query).items()}
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == "https://accounts.google.com/o/oauth2/v2/auth"
    assert q["client_id"] == CLIENT_ID
    assert q["redirect_uri"] == "http://localhost/api/auth/google/callback"
    assert q["response_type"] == "code"
    assert set(q["scope"].split()) == {"openid", "email", "profile"}
    assert q["code_challenge_method"] == "S256"
    assert len(q["state"]) >= 16 and len(q["nonce"]) >= 16 and len(q["code_challenge"]) >= 43
    assert CLIENT_SECRET not in url


def test_the_state_cookie_is_http_only_lax_scoped_to_the_google_routes_and_lives_ten_minutes(anon_client, google):
    r = anon_client.get(LOGIN, follow_redirects=False)

    _, attrs = cookie_attrs(r, OAUTH_COOKIE)

    assert attrs["path"] == "/api/auth/google"
    assert attrs["max-age"] == "600"
    assert attrs["samesite"].lower() == "lax"
    assert "httponly" in attrs
    assert "secure" not in attrs


def test_an_https_deployment_marks_both_cookies_secure(make_app, monkeypatch, kv):
    origin = "https://kyapehnu.example"
    google = FakeGoogle(redirect_uri=f"{origin}{CALLBACK}")
    google.install(monkeypatch)
    app = make_app(public_url=origin)
    c = TestClient(app, base_url=origin)

    start_response = c.get(LOGIN, follow_redirects=False)
    state_attrs = cookie_attrs(start_response, OAUTH_COOKIE)[1]
    sent = parse_qs(urlsplit(start_response.headers["location"]).query)
    assert sent["redirect_uri"] == [f"{origin}{CALLBACK}"]
    done = c.get(google.approve(start_response.headers["location"]), follow_redirects=False)

    assert "secure" in state_attrs
    assert "secure" in cookie_attrs(done, SESSION_COOKIE)[1]


def test_start_when_google_is_not_configured_is_a_redirect_with_a_reason_not_a_500(make_app):
    app = make_app(google_client_id=None, google_client_secret=None)
    c = TestClient(app, base_url="http://localhost")

    r = c.get(LOGIN, follow_redirects=False)

    assert r.status_code == 302
    assert r.headers["location"] == "/signin?error=auth_not_configured"


@pytest.mark.parametrize(
    ("overrides", "missing"),
    [
        ({"google_client_id": None}, "KYAPEHNU_GOOGLE_CLIENT_ID"),
        ({"google_client_secret": None}, "KYAPEHNU_GOOGLE_CLIENT_SECRET"),
        ({"google_client_secret": SecretStr("")}, "KYAPEHNU_GOOGLE_CLIENT_SECRET"),
        (
            {"google_client_id": None, "google_client_secret": None},
            "KYAPEHNU_GOOGLE_CLIENT_ID,KYAPEHNU_GOOGLE_CLIENT_SECRET",
        ),
    ],
)
def test_the_startup_log_names_the_missing_google_setting(make_app, caplog, overrides, missing):
    make_app(**overrides)

    assert f"event=auth_not_configured missing={missing}\n" in caplog.text + "\n"


def test_a_configured_google_logs_no_warning(make_app, caplog):
    make_app()

    assert "auth_not_configured" not in caplog.text


def test_me_when_google_is_not_configured_is_401_auth_not_configured(make_app):
    c = TestClient(make_app(google_client_id=None), base_url="http://localhost")

    r = c.get("/api/auth/me")

    assert r.status_code == 401
    assert r.json()["code"] == "auth_not_configured"


# ---- H1: Google unreachable at the start -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "exc_name"),
    [("connect_error", "ConnectError"), ("http_500", "HTTPStatusError"), ("not_json", None)],
)
def test_start_when_google_discovery_fails_is_503_google_unreachable(anon_client, google, caplog, mode, exc_name):
    google.discovery = mode

    r = anon_client.get(LOGIN, params={"next": "/NEXTMARKER"}, follow_redirects=False)

    assert r.status_code == 503
    assert r.headers["Retry-After"] == "1"
    body = r.json()
    assert body["code"] == "google_unreachable"
    assert set(body) == {"detail", "code", "request_id"}
    assert CLIENT_ID not in r.text and CLIENT_SECRET not in r.text
    assert CLIENT_ID not in str(r.headers) and CLIENT_SECRET not in str(r.headers)
    assert OAUTH_COOKIE not in anon_client.cookies
    assert "NEXTMARKER" not in caplog.text
    logged = re.search(r"event=signin_failed reason=google_unreachable exc=(\w+)", caplog.text)
    assert logged, caplog.text
    if exc_name is not None:
        assert logged.group(1) == exc_name


@pytest.mark.parametrize("lib", [httpx, httpx2], ids=["httpx", "httpx2"])
def test_start_treats_an_http_error_of_either_httpx_flavour_as_google_unreachable(anon_client, monkeypatch, lib):
    # authlib 1.8 raises httpx2 errors, which are not httpx.HTTPError: both must map to the same answer.
    async def broken(self, *args, **kwargs):
        raise lib.ConnectError("no route")

    monkeypatch.setattr(StarletteOAuth2App, "load_server_metadata", broken)

    r = anon_client.get(LOGIN, follow_redirects=False)

    assert (r.status_code, r.json()["code"]) == (503, "google_unreachable")


def test_start_works_again_once_google_is_back(anon_client, google):
    google.discovery = "connect_error"
    assert anon_client.get(LOGIN, follow_redirects=False).status_code == 503

    google.discovery = "ok"

    assert anon_client.get(LOGIN, follow_redirects=False).status_code == 302


# ---- a good sign-in --------------------------------------------------------------------------------------------


def test_a_first_sign_in_creates_the_admin_account_a_session_and_an_onboarding_profile(anon_client, google, kv, caplog):
    r = complete_sign_in(anon_client, google, Person(sub="g-1", email="ada@example.com", name="Ada Lovelace"))

    assert r.status_code == 302
    assert r.headers["location"] == "/"
    who = me(anon_client)
    assert who == {
        "id": who["id"],
        "display_name": "Ada Lovelace",
        "email": "ada@example.com",
        "is_admin": True,
        "avatar_url": None,
    }
    profile = anon_client.get("/api/profile").json()
    assert profile["gender"] == ""  # the SPA shows onboarding
    assert profile["user_id"] == who["id"]
    assert f"event=signin_ok user_id={who['id']}" in caplog.text


def test_the_session_cookie_is_http_only_lax_and_long_lived_and_the_state_cookie_is_cleared(anon_client, google):
    r = complete_sign_in(anon_client, google)

    value, attrs = cookie_attrs(r, SESSION_COOKIE)

    assert attrs["path"] == "/"
    assert attrs["max-age"] == "34560000"
    assert attrs["samesite"].lower() == "lax"
    assert "httponly" in attrs
    assert "secure" not in attrs
    assert value
    assert OAUTH_COOKIE not in anon_client.cookies


def test_the_session_lives_in_valkey_under_a_hash_for_the_configured_idle_time(anon_client, google, kv):
    r = complete_sign_in(anon_client, google)
    value, _ = cookie_attrs(r, SESSION_COOKIE)

    key = "session:" + hashlib.sha256(value.encode()).hexdigest()

    assert kv.keys("*") == [key]
    assert SESSION_30_DAYS - 10 <= kv.ttl(key) <= SESSION_30_DAYS
    assert value not in kv.get(key)


def test_signing_in_again_ends_the_session_the_browser_carried(anon_client, google, kv):
    first = cookie_attrs(complete_sign_in(anon_client, google), SESSION_COOKIE)[0]

    second = cookie_attrs(complete_sign_in(anon_client, google), SESSION_COOKIE)[0]

    assert second != first
    assert kv.keys("*") == ["session:" + hashlib.sha256(second.encode()).hexdigest()]


def test_the_idle_time_follows_the_setting(make_app, monkeypatch, kv):
    google = FakeGoogle(redirect_uri=f"http://localhost{CALLBACK}")
    google.install(monkeypatch)
    c = TestClient(make_app(session_idle_days=2), base_url="http://localhost")

    complete_sign_in(c, google)

    (key,) = kv.keys("session:*")
    assert 2 * 86400 - 10 <= kv.ttl(key) <= 2 * 86400


def test_the_second_account_is_not_an_admin(app, google, kv):
    first = TestClient(app, base_url="http://localhost")
    second = TestClient(app, base_url="http://localhost")

    complete_sign_in(first, google, Person(sub="g-1", email="a@example.com"))
    complete_sign_in(second, google, Person(sub="g-2", email="b@example.com"))

    assert me(first)["is_admin"] is True
    assert me(second)["is_admin"] is False
    assert me(first)["id"] != me(second)["id"]


def test_the_same_google_account_with_a_new_email_is_the_same_account(app, google):
    first = TestClient(app, base_url="http://localhost")
    complete_sign_in(first, google, Person(sub="g-1", email="old@example.com"))
    again = TestClient(app, base_url="http://localhost")

    complete_sign_in(again, google, Person(sub="g-1", email="new@example.com"))

    assert me(again)["id"] == me(first)["id"]
    assert me(again)["email"] == "new@example.com"
    assert len(users(app)) == 1


@pytest.mark.parametrize(
    ("next", "landing"),
    [
        ("/wardrobe", "/wardrobe"),
        ("/wardrobe?tab=2", "/wardrobe?tab=2"),
        (None, "/"),
        ("//evil.example", "/"),
        ("https://evil.example/x", "/"),
        ("/\\evil.example", "/"),
    ],
)
def test_the_return_target_is_kept_only_when_it_is_a_local_path(anon_client, google, next, landing):
    r = complete_sign_in(anon_client, google, next=next)

    assert r.headers["location"] == landing


# ---- sign-in refused -------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "grant",
    [
        {"person": Person(email_verified=False)},
        {"drop": ("email_verified",)},
        {"claims": {"email_verified": "true"}},
    ],
    ids=["false", "missing", "a string"],
)
def test_an_email_google_has_not_verified_is_refused(app, anon_client, google, kv, caplog, grant):
    r = complete_sign_in(anon_client, google, grant.pop("person", None), **grant)

    assert_refused(r, "unverified_email", anon_client, kv, app, caplog)


def test_a_callback_carrying_a_google_error_is_refused(app, anon_client, google, kv, caplog):
    start(anon_client)

    r = anon_client.get(CALLBACK, params={"error": "access_denied"}, follow_redirects=False)

    assert_refused(r, "google_error", anon_client, kv, app, caplog)


def test_a_google_error_with_a_state_is_still_a_google_error(app, anon_client, google, kv, caplog):
    callback = google.approve(start(anon_client))
    state = parse_qs(urlsplit(callback).query)["state"][0]

    r = anon_client.get(CALLBACK, params={"error": "access_denied", "state": state}, follow_redirects=False)

    assert_refused(r, "google_error", anon_client, kv, app, caplog)


def test_a_callback_without_a_state_is_refused_before_google_is_asked(app, anon_client, google, kv, caplog):
    start(anon_client)
    asked = len(google.requests)

    r = anon_client.get(CALLBACK, params={"code": "CODEMARKER"}, follow_redirects=False)

    assert_refused(r, "state", anon_client, kv, app, caplog, markers=["CODEMARKER"])
    assert len(google.requests) == asked


def test_a_callback_with_an_empty_state_is_refused(app, anon_client, google, kv, caplog):
    start(anon_client)

    r = anon_client.get(CALLBACK, params={"code": "c", "state": ""}, follow_redirects=False)

    assert_refused(r, "state", anon_client, kv, app, caplog)


def test_a_wrong_state_is_refused(app, anon_client, google, kv, caplog):
    callback = google.approve(start(anon_client))

    r = anon_client.get(callback + "tampered", follow_redirects=False)

    assert_refused(r, "state", anon_client, kv, app, caplog, markers=["tampered"])


def test_a_callback_from_another_browser_is_refused(app, anon_client, google, kv, caplog):
    callback = google.approve(start(anon_client))
    other = TestClient(app, base_url="http://localhost")

    r = other.get(callback, follow_redirects=False)

    assert_refused(r, "state", other, kv, app, caplog)


def test_a_callback_used_twice_is_refused_the_second_time(app, anon_client, google, kv, caplog):
    callback = google.approve(start(anon_client))
    assert anon_client.get(callback, follow_redirects=False).status_code == 302

    r = anon_client.get(callback, follow_redirects=False)

    assert r.headers["location"] == "/signin?error=state"
    assert "event=signin_failed reason=state" in caplog.text


def test_a_callback_after_ten_minutes_is_refused(app, anon_client, google, kv, caplog, monkeypatch):
    callback = google.approve(start(anon_client))
    real = itsdangerous.timed.TimestampSigner.get_timestamp
    monkeypatch.setattr(itsdangerous.timed.TimestampSigner, "get_timestamp", lambda self: real(self) + 601)

    r = anon_client.get(callback, follow_redirects=False)

    assert_refused(r, "state", anon_client, kv, app, caplog)


def test_a_callback_just_inside_ten_minutes_still_works(app, anon_client, google, monkeypatch):
    callback = google.approve(start(anon_client))
    real = itsdangerous.timed.TimestampSigner.get_timestamp
    monkeypatch.setattr(itsdangerous.timed.TimestampSigner, "get_timestamp", lambda self: real(self) + 590)

    assert anon_client.get(callback, follow_redirects=False).headers["location"] == "/"


@pytest.mark.parametrize(
    ("grant", "google_setting", "reason"),
    [
        ({"nonce": "not-the-nonce-we-sent"}, {}, "token"),
        ({"claims": {"iss": "https://evil.example"}}, {}, "token"),
        ({"claims": {"aud": "someone-else", "azp": "someone-else"}}, {}, "token"),
        ({"claims": {"exp": 1}}, {}, "token"),
        ({"drop": ("sub",)}, {}, "token"),
        ({}, {"jwks": "other_key"}, "token"),
        ({}, {"token": "invalid_grant"}, "token"),
        ({}, {"token": "connect_error"}, "google_unreachable"),
        ({}, {"token": "http_500"}, "google_unreachable"),
        ({}, {"jwks": "connect_error"}, "google_unreachable"),
        ({}, {"jwks": "http_500"}, "google_unreachable"),
    ],
    ids=[
        "wrong nonce",
        "wrong issuer",
        "token for another client",
        "expired id token",
        "no sub",
        "bad signature",
        "invalid_grant",
        "token endpoint down",
        "token endpoint 500",
        "jwks down",
        "jwks 500",
    ],
)
def test_a_real_google_flow_that_fails_is_a_redirect_with_the_reason(
    app, anon_client, google, kv, caplog, grant, google_setting, reason
):
    for name, value in google_setting.items():
        setattr(google, name, value)

    r = complete_sign_in(anon_client, google, **grant)

    assert_refused(r, reason, anon_client, kv, app, caplog)


@pytest.mark.parametrize(
    ("raised", "reason"),
    [
        (lambda: MismatchingStateError(), "state"),
        (lambda: OAuthError(error="invalid_grant"), "token"),
        (lambda: BadSignatureError(), "token"),
        (lambda: DecodeError("garbage"), "token"),
        (lambda: RuntimeError("Missing state value"), "token"),
        (lambda: ValueError("malformed jwks"), "token"),
        (lambda: KeyError("keys"), "token"),
        (lambda: httpx.ConnectError("down"), "google_unreachable"),
        (lambda: httpx2.ConnectError("down"), "google_unreachable"),
        (lambda: httpx2.ReadTimeout("slow"), "google_unreachable"),
    ],
    ids=[
        "mismatching state",
        "invalid_grant",
        "bad signature",
        "decode error",
        "missing state",
        "value error",
        "key error",
        "httpx connect",
        "httpx2 connect",
        "httpx2 timeout",
    ],
)
def test_every_exception_authlib_may_raise_maps_to_its_reason(
    app, anon_client, google, kv, caplog, monkeypatch, raised, reason
):
    async def boom(self, request, **kwargs):
        raise raised()

    monkeypatch.setattr(StarletteOAuth2App, "authorize_access_token", boom)
    start(anon_client)

    r = anon_client.get(CALLBACK, params={"code": "CODEMARKER", "state": "STATEMARKER"}, follow_redirects=False)

    assert_refused(r, reason, anon_client, kv, app, caplog, markers=["CODEMARKER", "STATEMARKER"])


def userinfo(**overrides):
    base = {"sub": "g-1", "email": "ada@example.com", "email_verified": True, "name": "Ada"}
    base.update(overrides)
    return {k: v for k, v in base.items() if v is not ...}


@pytest.mark.parametrize(
    "token",
    [
        {"access_token": "a"},
        {"access_token": "a", "id_token": "x"},
        {"access_token": "a", "userinfo": None},
        {"userinfo": userinfo(sub=...)},
        {"userinfo": userinfo(sub="")},
        {"userinfo": userinfo(sub="s" * 256)},
        {"userinfo": userinfo(sub="s" * 300)},
        {"userinfo": userinfo(sub=12345)},
        {"userinfo": userinfo(email=...)},
        {"userinfo": userinfo(email="")},
        {"userinfo": userinfo(email=["a@example.com"])},
    ],
    ids=[
        "no userinfo",
        "id_token only",
        "userinfo none",
        "no sub",
        "empty sub",
        "256-char sub",
        "300-char sub",
        "sub not text",
        "no email",
        "empty email",
        "email not text",
    ],
)
def test_a_token_without_a_usable_sub_or_email_is_refused_as_token(
    app, anon_client, google, kv, caplog, monkeypatch, token
):
    async def returns(self, request, **kwargs):
        return dict(token)

    monkeypatch.setattr(StarletteOAuth2App, "authorize_access_token", returns)
    start(anon_client)

    r = anon_client.get(CALLBACK, params={"code": "c", "state": "s"}, follow_redirects=False)

    assert_refused(r, "token", anon_client, kv, app, caplog)


def test_a_sub_of_255_characters_is_accepted(app, anon_client, google, monkeypatch):
    async def returns(self, request, **kwargs):
        return {"access_token": "a", "userinfo": userinfo(sub="s" * 255)}

    monkeypatch.setattr(StarletteOAuth2App, "authorize_access_token", returns)
    start(anon_client)

    r = anon_client.get(CALLBACK, params={"code": "c", "state": "s"}, follow_redirects=False)

    assert r.headers["location"] == "/"
    assert len(users(app)) == 1


def test_an_unexpected_failure_in_the_callback_is_a_500_that_names_neither_query_nor_message(
    app, google, caplog, monkeypatch
):
    async def boom(self, request, **kwargs):
        raise ZeroDivisionError("MESSAGEMARKER")

    monkeypatch.setattr(StarletteOAuth2App, "authorize_access_token", boom)
    c = TestClient(app, base_url="http://localhost", raise_server_exceptions=False)
    c.get(LOGIN, follow_redirects=False)

    r = c.get(CALLBACK, params={"code": "CODEMARKER", "state": "STATEMARKER"}, follow_redirects=False)

    assert (r.status_code, r.json()["code"]) == (500, "internal")
    for marker in ("MESSAGEMARKER", "CODEMARKER", "STATEMARKER"):
        assert marker not in r.text and marker not in caplog.text


# ---- me and sign-out -------------------------------------------------------------------------------------------


def test_me_describes_the_caller(client_a, client_b):
    assert me(client_a) == {
        "id": me(client_a)["id"],
        "display_name": "Asha",
        "email": "sub-a@example.com",
        "is_admin": True,
        "avatar_url": None,
    }
    assert me(client_b)["is_admin"] is False
    assert me(client_b)["display_name"] == "Bela"


def test_me_without_a_session_is_401_not_authenticated(anon_client):
    r = anon_client.get("/api/auth/me")

    assert r.status_code == 401
    assert r.json()["code"] == "not_authenticated"
    assert set(r.json()) == {"detail", "code", "request_id"}


def test_a_garbage_session_cookie_is_401_and_logged_as_rejected(anon_client, caplog):
    anon_client.cookies.set(SESSION_COOKIE, "garbage")

    r = anon_client.get("/api/auth/me")

    assert (r.status_code, r.json()["code"]) == (401, "not_authenticated")
    assert "event=session_rejected" in caplog.text
    assert "garbage" not in caplog.text


def test_a_session_idle_past_the_expiry_is_401(app, client_a, kv):
    (key,) = kv.keys("session:*")
    kv.pexpire(key, 1)
    time.sleep(0.05)

    assert client_a.get("/api/auth/me").status_code == 401


def test_each_request_slides_the_session_expiry(client_a, kv):
    (key,) = kv.keys("session:*")
    kv.expire(key, 100)

    client_a.get("/api/auth/me")

    assert kv.ttl(key) > 100


def test_a_session_whose_account_is_gone_is_401_and_is_deleted(app, client_a, kv):
    user_id = me(client_a)["id"]
    with app.state.ctx.engine.begin() as conn:
        conn.exec_driver_sql("DELETE FROM userprofile WHERE user_id = ?", (user_id,))
        conn.exec_driver_sql('DELETE FROM "user" WHERE id = ?', (user_id,))

    r = client_a.get("/api/auth/me")

    assert (r.status_code, r.json()["code"]) == (401, "not_authenticated")
    assert kv.keys("session:*") == []


def test_sign_out_ends_that_session_and_only_that_one(app, kv, caplog):
    user_id, first = sign_in(app, "sub-a")
    _, second = sign_in(app, "sub-a")  # the same person on a second device
    old_cookie = first.cookies[SESSION_COOKIE]

    r = first.post("/api/auth/logout")

    assert r.status_code == 204
    assert r.content == b""
    _, attrs = cookie_attrs(r, SESSION_COOKIE)  # the response expires the cookie
    assert attrs.get("max-age") == "0" or "1970" in attrs.get("expires", "")
    assert f"event=signout user_id={user_id}" in caplog.text
    replay = TestClient(app, base_url="http://localhost")
    replay.cookies.set(SESSION_COOKIE, old_cookie)
    assert replay.get("/api/auth/me").status_code == 401
    assert second.get("/api/auth/me").status_code == 200


def test_sign_out_without_a_session_is_401(anon_client):
    assert anon_client.post("/api/auth/logout").status_code == 401


def test_sign_out_from_another_origin_is_refused_and_keeps_the_session(client_a):
    r = client_a.post("/api/auth/logout", headers={"Origin": "http://evil.example"})

    assert (r.status_code, r.json()["code"]) == (403, "cross_origin")
    assert client_a.get("/api/auth/me").status_code == 200


def test_sign_out_is_post_only(client_a):
    assert client_a.get("/api/auth/logout").status_code == 405


# ---- AC-13: nothing from Google is stored or shown --------------------------------------------------------------


def valkey_text(kv):
    parts = []
    for key in kv.scan_iter("*"):
        parts.append(key)
        kind = kv.type(key)
        if kind == "string":
            parts.append(kv.get(key))
        elif kind == "list":
            parts += kv.lrange(key, 0, -1)
        elif kind == "hash":
            parts += [f"{k}={v}" for k, v in kv.hgetall(key).items()]
        elif kind == "set":
            parts += list(kv.smembers(key))
        elif kind == "zset":
            parts += [m for m, _ in kv.zrange(key, 0, -1, withscores=True)]
    return "\n".join(parts)


def test_no_google_secret_or_token_is_stored_logged_or_shown(app, anon_client, google, kv, caplog):
    callback_response = complete_sign_in(anon_client, google)
    assert callback_response.status_code == 302
    shown = [str(callback_response.headers), callback_response.text]
    for path in ("/api/auth/me", "/api/profile", "/api/settings"):
        r = anon_client.get(path)
        shown += [str(r.headers), r.text]
    failed = TestClient(app, base_url="http://localhost")
    google.token = "invalid_grant"
    refused = complete_sign_in(failed, google)  # an error path is swept too
    shown += [str(refused.headers), refused.text]
    data_dir = app.state.ctx.data_dir
    db = sqlite3.connect(data_dir / "kyapehnu.db")
    dump = "\n".join(db.iterdump())
    db.close()
    archive = backup(data_dir, app.state.ctx.files)
    with zipfile.ZipFile(archive) as z:
        members = {name: z.read(name) for name in z.namelist()}

    secrets = google.secrets
    assert len(secrets) == 4  # the client secret and the access, refresh and ID tokens of the one good sign-in
    for secret in secrets:
        assert secret not in dump
        assert secret not in valkey_text(kv)
        assert secret not in caplog.text
        for text in shown:
            assert secret not in text
        for name, data in members.items():
            assert secret.encode() not in data, name
    assert "kyapehnu.db" in members


def test_the_sweep_would_notice_a_leak(app, anon_client, google, kv):
    complete_sign_in(anon_client, google)
    kv.set("oops", google.issued[0]["refresh_token"])

    assert google.issued[0]["refresh_token"] in valkey_text(kv)


def test_the_session_value_is_in_no_body_header_or_log_line(anon_client, google, caplog):
    r = complete_sign_in(anon_client, google)
    value, _ = cookie_attrs(r, SESSION_COOKIE)
    shown = [r.text, r.headers["location"], str(me(anon_client)), anon_client.get("/api/auth/me").text]

    assert all(value not in text for text in shown)
    assert value not in caplog.text


def test_the_google_picture_claim_reaches_me(anon_client, google):
    complete_sign_in(anon_client, google, claims={"picture": "https://lh3.googleusercontent.com/a/ada=s96-c"})

    assert me(anon_client)["avatar_url"] == "https://lh3.googleusercontent.com/a/ada=s96-c"
