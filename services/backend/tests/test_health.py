"""AC-27, AC-28: `/readyz` is 503 with the failed dependency named when SQLite or Valkey cannot be reached."""

import fakeredis
import httpx
import pytest
import redis
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from kyapehnu.core.app import create_app
from tests.fake_openmeteo import FakeOpenMeteo


def _client(settings, fake, kv):
    app = create_app(settings=settings, transport=fake, kv=kv, weather_transport=httpx.MockTransport(FakeOpenMeteo()))
    return app, TestClient(app, base_url="http://localhost")


def test_livez_stays_up_when_valkey_is_down(settings, fake):
    down = redis.Redis(host="127.0.0.1", port=1, socket_connect_timeout=0.2)
    _, client = _client(settings, fake, down)
    with client as c:
        assert c.get("/livez").status_code == 200


def test_readyz_names_valkey_when_it_cannot_be_reached(settings, fake):
    down = redis.Redis(host="127.0.0.1", port=1, socket_connect_timeout=0.2)
    _, client = _client(settings, fake, down)
    with client as c:
        r = c.get("/readyz")
    assert r.status_code == 503
    assert r.json() == {"status": "unavailable", "failed": ["valkey"]}


def test_readyz_names_the_database_when_it_cannot_be_opened(settings, fake):
    app, client = _client(settings, fake, fakeredis.FakeRedis(decode_responses=True))
    with client as c:
        app.state.ctx.engine.dispose()
        app.state.ctx.engine = create_engine("sqlite:////nonexistent-folder/kyapehnu.db")
        r = c.get("/readyz")
    assert r.status_code == 503
    assert r.json() == {"status": "unavailable", "failed": ["db"]}


@pytest.mark.parametrize("path", ["/readyz", "/livez"])
def test_the_health_routes_need_no_session(settings, fake, path):
    _, client = _client(settings, fake, fakeredis.FakeRedis(decode_responses=True))
    with client as c:
        assert c.get(path).status_code == 200
