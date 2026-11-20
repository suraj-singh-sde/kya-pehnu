"""Every /api route needs a session (AC-8) and the admin routes need the admin (AC-11).

`/images` is not swept here: until WP-3 replaces the static mount with the owner-checked route it is the WP-1 mount,
and WP-3's test_isolation_wardrobe.py asserts "signed out 401" for image URLs."""

import logging
import re

import pytest
from fastapi.testclient import TestClient

from tests.conftest import SESSION_COOKIE

OPEN_ROUTES = {
    ("GET", "/api/auth/google/login"),
    ("GET", "/api/auth/google/callback"),
    ("GET", "/api/auth/options"),
}
UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}
ADMIN_ROUTES = [
    ("POST", "/api/settings/llm/models"),
    ("POST", "/api/settings/llm/test"),
    ("PUT", "/api/settings/llm"),
    ("GET", "/api/debug/llm-calls"),
    ("GET", "/api/debug/tool-calls"),
]


def api_operations(app):
    """(method, concrete path) of every documented /api operation except the two sign-in routes."""
    ops = []
    for path, methods in app.openapi()["paths"].items():
        for method in methods:
            if path.startswith("/api/") and (method.upper(), path) not in OPEN_ROUTES:
                ops.append((method.upper(), re.sub(r"\{[^}]+\}", "1", path)))
    return ops


def call(client, method, path):
    return client.request(method, path, json={} if method in UNSAFE else None)


def test_the_sweep_covers_the_whole_api(app):
    ops = api_operations(app)

    assert len(ops) >= 40
    assert ("GET", "/api/items") in ops and ("POST", "/api/ingest") in ops and ("GET", "/api/auth/me") in ops
    assert ("POST", "/api/auth/logout") in ops


@pytest.mark.parametrize(
    "cookie", [None, "garbage", "x" * 43], ids=["no cookie", "unknown value", "well-formed unknown"]
)
def test_every_api_route_answers_401_without_a_live_session(app, cookie):
    c = TestClient(app, base_url="http://localhost")
    if cookie is not None:
        c.cookies.set(SESSION_COOKIE, cookie)

    wrong = []
    for method, path in api_operations(app):
        r = call(c, method, path)
        if r.status_code != 401 or r.json().get("code") != "not_authenticated":
            wrong.append((method, path, r.status_code))

    assert wrong == []


def test_every_api_route_says_so_when_sign_in_is_not_configured(make_app):
    c = TestClient(make_app(google_client_id=None), base_url="http://localhost")

    wrong = []
    for method, path in api_operations(c.app):
        r = call(c, method, path)
        if r.status_code != 401 or r.json().get("code") != "auth_not_configured":
            wrong.append((method, path, r.status_code))

    assert wrong == []


def test_the_health_checks_need_no_session(anon_client):
    assert anon_client.get("/livez").status_code == 200
    assert anon_client.get("/readyz").status_code == 200


def test_a_signed_in_user_gets_past_the_session_check(client_a):
    assert client_a.get("/api/items").status_code == 200


@pytest.mark.parametrize(("method", "path"), ADMIN_ROUTES)
def test_a_non_admin_is_403_on_the_admin_routes(client_b, method, path):
    r = call(client_b, method, path)

    assert r.status_code == 403
    assert r.json()["code"] == "permission_denied"
    assert r.json()["detail"] == "Only the admin can do this"


@pytest.mark.parametrize(("method", "path"), ADMIN_ROUTES)
def test_the_admin_is_let_through_the_admin_routes(client_a, method, path):
    r = call(client_a, method, path)

    assert r.status_code not in (401, 403)


def test_a_refused_admin_call_is_logged_with_the_user_id(app, client_b, caplog):
    caplog.set_level(logging.INFO)
    uid = client_b.get("/api/auth/me").json()["id"]

    client_b.get("/api/debug/llm-calls")

    assert f"event=admin_denied user_id={uid}" in caplog.text


def test_an_ordinary_route_is_open_to_a_non_admin(client_b):
    assert client_b.get("/api/items").status_code == 200
    assert client_b.get("/api/settings").status_code == 200


def test_the_folder_import_route_does_not_exist(client_a):
    # AC-11 allows 404 or 405; the router has no route at all under /api/ingest/folder, which is a 404.
    assert client_a.post("/api/ingest/folder", json={"path": "/tmp"}).status_code in (404, 405)


def test_auth_options_is_public_and_reports_the_two_sign_ins(app):
    r = TestClient(app, base_url="http://localhost").get("/api/auth/options")

    assert r.status_code == 200
    assert set(r.json()) == {"google", "local"}
