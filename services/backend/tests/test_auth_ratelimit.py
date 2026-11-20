"""Sign-in rate limit (LLD 3.2 `auth_rate_limit`; AC-16 unit part): 20 per minute per client address, counted on the
address the connection came from, never on a forwarding header; a dead Valkey fails closed."""

import time

import pytest
import redis
from fastapi.testclient import TestClient

LOGIN = "/api/auth/google/login"
CALLBACK = "/api/auth/google/callback"


@pytest.fixture
def browser(app, google):
    return TestClient(app, base_url="http://localhost", client=("203.0.113.5", 40000))


def hit(c, n, **kwargs):
    return [c.get(LOGIN, follow_redirects=False, **kwargs).status_code for _ in range(n)]


def test_twenty_requests_pass_and_the_21st_is_429_with_retry_after(browser):
    assert hit(browser, 20) == [302] * 20

    r = browser.get(LOGIN, follow_redirects=False)

    assert r.status_code == 429
    assert 1 <= int(r.headers["Retry-After"]) <= 60
    body = r.json()
    assert body["code"] == "rate_limited"
    assert body["detail"] == "Too many sign-in attempts. Try again shortly."
    assert set(body) == {"detail", "code", "request_id"}


def test_the_callback_counts_against_the_same_limit(browser):
    hit(browser, 20)

    r = browser.get(CALLBACK, params={"code": "c", "state": "s"}, follow_redirects=False)

    assert (r.status_code, r.json()["code"]) == (429, "rate_limited")


def test_a_forged_forwarding_header_neither_resets_nor_escapes_the_count(browser):
    spoofed = [
        {"X-Forwarded-For": f"10.0.0.{n}", "X-Real-IP": f"10.0.1.{n}", "Forwarded": f"for=10.0.2.{n}"}
        for n in range(20)
    ]
    assert [browser.get(LOGIN, headers=h, follow_redirects=False).status_code for h in spoofed] == [302] * 20

    for forged in ("127.0.0.1", "10.9.9.9", "203.0.113.6"):
        r = browser.get(LOGIN, headers={"X-Forwarded-For": forged}, follow_redirects=False)
        assert r.status_code == 429, forged


def test_another_client_address_has_its_own_count(app, browser):
    hit(browser, 20)
    other = TestClient(app, base_url="http://localhost", client=("203.0.113.99", 40000))

    assert other.get(LOGIN, follow_redirects=False).status_code == 302


def test_the_session_check_is_not_rate_limited(browser):
    hit(browser, 20)

    assert browser.get("/api/auth/me").status_code == 401  # not 429


def test_the_limit_follows_the_setting(make_app, google):
    c = TestClient(make_app(auth_rate_limit="3/minute"), base_url="http://localhost")

    assert hit(c, 4) == [302, 302, 302, 429]


def test_sign_in_works_again_after_the_window(browser, monkeypatch):
    hit(browser, 20)
    assert browser.get(LOGIN, follow_redirects=False).status_code == 429
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 61)

    assert browser.get(LOGIN, follow_redirects=False).status_code == 302


def test_without_an_injected_storage_the_counts_live_in_valkey(make_app, kv, google):
    c = TestClient(make_app(rate_limit_storage=None), base_url="http://localhost")

    assert hit(c, 21)[-2:] == [302, 429]
    assert any(key.startswith("LIMITS") for key in kv.keys("*"))


@pytest.mark.parametrize(("path", "params"), [(LOGIN, {}), (CALLBACK, {"code": "c", "state": "s"})])
def test_a_dead_valkey_fails_closed_with_503(make_app, google, path, params):
    dead = redis.Redis(host="127.0.0.1", port=1, socket_connect_timeout=0.2, decode_responses=True)
    c = TestClient(make_app(kv_client=dead, rate_limit_storage=None), base_url="http://localhost")

    r = c.get(path, params=params, follow_redirects=False)

    assert r.status_code == 503
    assert r.headers["Retry-After"] == "1"
    assert r.json()["code"] == "service_unavailable"
    assert google.requests == []  # nobody was sent to Google
