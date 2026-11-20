"""Valkey down (AC-27; LLD 4.2): a route that needs a session answers 503 with Retry-After, never 200 and never a
401 that would sign people out; `/readyz` says not ready; the routes that need no session keep working."""

import redis
from fastapi.testclient import TestClient

from tests.conftest import SESSION_COOKIE, sign_in

LOGIN = "/api/auth/google/login"


def dead_valkey():
    return redis.Redis(host="127.0.0.1", port=1, socket_connect_timeout=0.2, decode_responses=True)


def test_a_session_route_is_503_with_retry_after_while_valkey_is_down(make_app):
    c = TestClient(make_app(kv_client=dead_valkey()), base_url="http://localhost")
    c.cookies.set(SESSION_COOKIE, "some-session-cookie")

    r = c.get("/api/items")

    assert r.status_code == 503
    assert r.headers["Retry-After"] == "1"
    assert r.json()["code"] == "service_unavailable"
    assert "some-session-cookie" not in r.text


def test_a_request_without_a_cookie_is_still_401_and_asks_nothing_of_valkey(make_app):
    c = TestClient(make_app(kv_client=dead_valkey()), base_url="http://localhost")

    r = c.get("/api/items")

    assert (r.status_code, r.json()["code"]) == (401, "not_authenticated")


def test_readyz_is_503_while_valkey_is_down(make_app):
    c = TestClient(make_app(kv_client=dead_valkey()), base_url="http://localhost")

    r = c.get("/readyz")

    assert r.status_code == 503
    assert r.json() == {"status": "unavailable", "failed": ["valkey"]}


def test_a_session_survives_a_valkey_outage(app, valkey_fault):
    _, c = sign_in(app, "sub-a")
    assert c.get("/api/items").status_code == 200
    valkey_fault()

    down = c.get("/api/items")
    valkey_fault.heal()

    assert (down.status_code, down.json()["code"]) == (503, "service_unavailable")
    assert c.get("/api/items").status_code == 200  # the same cookie, no new sign-in


def test_sign_out_while_valkey_is_down_is_503_and_keeps_the_cookie_valid(app, valkey_fault):
    _, c = sign_in(app, "sub-a")
    valkey_fault()

    r = c.post("/api/auth/logout")
    valkey_fault.heal()

    assert (r.status_code, r.json()["code"]) == (503, "service_unavailable")
    assert not any(h.startswith(SESSION_COOKIE) for h in r.headers.get_list("set-cookie"))
    assert c.get("/api/auth/me").status_code == 200  # nothing was half done: the user can retry


def test_a_failed_session_delete_during_sign_out_is_503_and_changes_nothing(app, valkey_fault, kv):
    _, c = sign_in(app, "sub-a")
    valkey_fault("DEL", "UNLINK")

    r = c.post("/api/auth/logout")
    valkey_fault.heal()

    assert (r.status_code, r.json()["code"]) == (503, "service_unavailable")
    assert len(kv.keys("session:*")) == 1
    assert c.get("/api/auth/me").status_code == 200


def test_the_callback_cannot_issue_a_session_while_valkey_cannot_store_it(app, google, valkey_fault, kv):
    from tests.fake_google import complete_sign_in

    c = TestClient(app, base_url="http://localhost")
    valkey_fault("SET", "SETEX", "PSETEX")

    r = complete_sign_in(c, google)
    valkey_fault.heal()

    assert (r.status_code, r.json()["code"]) == (503, "service_unavailable")
    assert r.headers["Retry-After"] == "1"
    assert SESSION_COOKIE not in c.cookies
    assert kv.keys("session:*") == []


def test_the_liveness_check_and_the_sign_in_page_do_not_need_valkey(make_app, google):
    c = TestClient(make_app(kv_client=dead_valkey()), base_url="http://localhost")

    assert c.get("/livez").status_code == 200
    assert c.get(LOGIN, follow_redirects=False).status_code == 302  # the limiter here is in memory
