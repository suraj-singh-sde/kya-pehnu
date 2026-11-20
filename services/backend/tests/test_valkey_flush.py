"""AC-24: Valkey is flushed in the middle of use. At most people sign in again and the weather is fetched again; no
wardrobe, chat, event or account data is lost (integration: real Valkey)."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from tests.chat_fakes import say
from tests.chat_helpers import send
from tests.conftest import sign_in
from tests.fake_google import Person, complete_sign_in
from tests.qa_flow import WEATHER, run_full_flow

pytestmark = pytest.mark.integration


def database(app) -> dict[str, list[tuple]]:
    """Every row of every table. The sign-in time of an account is left out: signing in again moves it."""
    out = {}
    with app.state.ctx.engine.connect() as conn:
        names = [r[0] for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"))]
        for name in names:
            cols = [r[1] for r in conn.execute(text(f'PRAGMA table_info("{name}")')) if r[1] != "last_sign_in_at"]
            quoted = ", ".join(f'"{c}"' for c in cols)
            out[name] = [tuple(r) for r in conn.execute(text(f'SELECT {quoted} FROM "{name}" ORDER BY 1'))]  # noqa: S608
    return out


def test_a_flushed_valkey_ends_the_sessions_and_loses_no_data(make_app, google, fake, fake_weather, kv, monkeypatch):
    flow = run_full_flow(make_app, google, fake, fake_weather, kv, monkeypatch)
    app = flow.app
    assert flow.a.get("/api/items").json() and flow.chat
    forecasts = len(fake_weather.calls("forecast"))
    assert flow.a.get("/api/weather", params=WEATHER).status_code == 200
    assert len(fake_weather.calls("forecast")) == forecasts  # served from the cache while Valkey holds it
    before = database(app)
    assert before["item"] and before["chatmessage"] and before["user"]

    kv.flushdb()

    # sessions end: a 401 (sign in again), never a 500
    assert flow.a.get("/api/auth/me").status_code == 401
    assert flow.b.get("/api/items").status_code == 401
    assert database(app) == before

    # the same people sign in again and find their own, unchanged data
    again = TestClient(app, base_url="http://localhost", client=("203.0.113.5", 40000))
    assert complete_sign_in(again, google, Person(sub="g-a", email="a@example.com", name="Asha")).status_code == 302
    assert again.get("/api/auth/me").json()["id"] == flow.a_id  # the same account, not a second one
    assert [i["id"] for i in again.get("/api/items").json()] == [flow.item]
    assert [c["id"] for c in again.get("/api/chat/sessions").json()] == [flow.chat]
    assert len(again.get(f"/api/chat/sessions/{flow.chat}").json()["messages"]) > 0
    b_id, b = sign_in(app, "g-b", email="b@example.com", name="Bela")
    assert b_id == flow.b_id and b.get("/api/items").json() == []
    assert database(app) == before

    # the weather is fetched again, the chat accepts a message (no stale turn lock), refusals are forgotten
    assert again.get("/api/weather", params=WEATHER).status_code == 200
    assert len(fake_weather.calls("forecast")) > forecasts
    assert send(again, fake, flow.chat, say("Still here.")).status_code == 200
    assert not [k for k in kv.scan_iter("notools:*")]
