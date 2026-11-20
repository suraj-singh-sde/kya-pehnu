"""The dev-only sign-in (`KYAPEHNU_DEV_LOGIN=1`): a session without Google, for local end-to-end testing.

Only an instance whose public URL is a loopback address may enable it; it is off by default and absent from the
routes then."""

import pytest
from fastapi.testclient import TestClient

from kyapehnu.core.errors import InvalidInputError
from tests.conftest import LOOPBACK_PEER, SESSION_COOKIE

DEV_LOGIN = "/api/auth/dev-login"


def _client(app):
    return TestClient(app, base_url="http://localhost", client=LOOPBACK_PEER)


def test_off_by_default_the_route_does_not_exist(anon_client):
    assert anon_client.post(DEV_LOGIN, json={"email": "a@example.com"}).status_code == 404


def test_refused_when_the_public_url_is_not_loopback(make_app):
    with pytest.raises(InvalidInputError, match="dev_login"):
        make_app(dev_login=True, public_url="https://kyapehnu.example.com")


def test_signs_in_first_account_as_admin_then_others_as_members(make_app):
    c = _client(make_app(dev_login=True))

    r = c.post(DEV_LOGIN, json={"email": "first@example.com"})
    assert r.status_code == 204 and SESSION_COOKIE in r.cookies
    me = c.get("/api/auth/me").json()
    assert me["email"] == "first@example.com" and me["is_admin"] is True

    d = _client(c.app)
    assert d.post(DEV_LOGIN, json={"email": "second@example.com"}).status_code == 204
    assert d.get("/api/auth/me").json()["is_admin"] is False

    # the same email again, in any case, is the same account, not a new one
    e = _client(c.app)
    e.post(DEV_LOGIN, json={"email": "First@Example.com"})
    assert e.get("/api/auth/me").json()["id"] == me["id"]


def test_is_rate_limited_like_the_google_routes(make_app):
    c = _client(make_app(dev_login=True, auth_rate_limit="2/minute"))
    codes = [c.post(DEV_LOGIN, json={"email": "a@example.com"}).status_code for _ in range(3)]
    assert codes == [204, 204, 429]


def test_bad_email_is_422(make_app):
    c = _client(make_app(dev_login=True))
    assert c.post(DEV_LOGIN, json={"email": "not-an-email"}).status_code == 422
    assert c.post(DEV_LOGIN, json={}).status_code == 422
