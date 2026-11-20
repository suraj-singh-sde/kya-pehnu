"""The instance's LLM key is used for everyone and shown to no one (AC-42; LLD 9.2): whoever chats, the provider is
called with the key; no response body or header of any parameterless `GET /api/*` route, the settings page or an
error path of the settings checks holds it, for the admin and for another account alike. Two sources: the
environment and the admin's saved `secrets.json`."""

import json
import re

import httpx
import openai
import pytest
from limits.storage import MemoryStorage

from kyapehnu.agent.llm import store
from kyapehnu.agent.llm.openai_compat import OpenAICompatTransport
from kyapehnu.core.app import create_app
from kyapehnu.core.config import Settings
from tests.chat_helpers import MESSAGES
from tests.conftest import CLIENT_ID, CLIENT_SECRET, sign_in

KEY = "sk-MARKER-7f3a9c1e5d2b8a40"
BASE = "http://localhost:11434/v1"
SKIP_PREFIXES = ("/api/auth/google",)  # the Google round trip needs Google


class Upstream:
    """The model server: records the Authorization header of every request and either answers or echoes the key."""

    def __init__(self) -> None:
        self.authorization: list[str | None] = []
        self.echo_key = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.authorization.append(request.headers.get("authorization"))
        if self.echo_key:
            return httpx.Response(401, json={"error": {"message": f"Incorrect API key provided: {KEY}"}})
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"object": "list", "data": [{"id": "m", "object": "model"}]})
        message = {"role": "assistant", "content": "Hello there."}
        return httpx.Response(
            200,
            json={
                "id": "x",
                "object": "chat.completion",
                "created": 0,
                "model": "m",
                "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )


def transport_over(upstream: Upstream) -> OpenAICompatTransport:
    sdk = openai.OpenAI(
        base_url=BASE, api_key=KEY, max_retries=0, http_client=httpx.Client(transport=httpx.MockTransport(upstream))
    )
    return OpenAICompatTransport(BASE, KEY, "m", client=sdk)


def settings_for(source: str, tmp_path) -> Settings:
    base = {
        "_env_file": None,
        "data_dir": tmp_path / "data",
        "config_file": tmp_path / "missing.yaml",
        "public_url": "http://localhost",
        "google_client_id": CLIENT_ID,
        "google_client_secret": CLIENT_SECRET,
    }
    if source == "env":
        return Settings(**base, llm_provider="openai_compatible", openai_api_key=KEY, openai_base_url=BASE)
    (tmp_path / "data").mkdir()
    saved = store.LlmStore(
        active="custom", providers={"custom": store.ProviderSecret(api_key=KEY, model="m", base_url=BASE)}
    )
    store.save(tmp_path / "data", saved)
    return Settings(**base)


@pytest.fixture(params=["env", "secrets.json"])
def world(request, tmp_path, fake_weather, kv, monkeypatch):
    upstream = Upstream()
    app = create_app(
        settings=settings_for(request.param, tmp_path),
        transport=transport_over(upstream),
        weather_transport=httpx.MockTransport(fake_weather),
        kv=kv,
        rate_limit_storage=MemoryStorage(),
    )
    # the settings checks build their own transport from the draft: put it on the same model server
    monkeypatch.setattr("kyapehnu.agent.llm_settings.transport_for", lambda *_a, **_k: transport_over(upstream))
    admin_id, admin = sign_in(app, "sub-a")
    other_id, other = sign_in(app, "sub-b")
    assert (admin_id, other_id) == (1, 2)
    return upstream, app, {"admin": admin, "other": other}


def leaks(response: httpx.Response) -> bool:
    return KEY in response.text or any(KEY in f"{k}: {v}" for k, v in response.headers.items())


def get_routes(app) -> list[str]:
    return sorted(
        path
        for path, methods in app.openapi()["paths"].items()
        if "get" in methods and "{" not in path and path.startswith("/api/") and not path.startswith(SKIP_PREFIXES)
    )


@pytest.mark.parametrize("who", ["admin", "other"])
def test_no_get_route_shows_the_key(world, who):
    _upstream, app, people = world
    paths = get_routes(app)

    responses = [people[who].get(path) for path in paths]

    assert "/api/settings" in paths and len(paths) >= 8
    assert [p for p, r in zip(paths, responses, strict=True) if leaks(r)] == []


@pytest.mark.parametrize("who", ["admin", "other"])
def test_the_error_paths_of_the_settings_checks_never_echo_the_key(world, who):
    upstream, _app, people = world
    upstream.echo_key = True
    draft = {"provider": "custom", "base_url": BASE, "model": "m"}

    responses = [
        people[who].post("/api/settings/llm/models", json=draft),
        people[who].post("/api/settings/llm/test", json=draft),
        people[who].put("/api/settings/llm", json=draft),
        people[who].post("/api/settings/test-llm"),
        people[who].get("/api/settings"),
    ]

    statuses = [r.status_code for r in responses]
    if who == "other":
        assert statuses == [403, 403, 403, 403, 200]
    else:  # the environment owns the settings (409), or the checks run against the model server and fail (400)
        assert statuses in ([409, 409, 409, 200, 200], [200, 200, 400, 200, 200])
    assert [r for r in responses if leaks(r)] == []


def test_a_chat_by_either_account_calls_the_provider_with_the_instance_key(world):
    upstream, _app, people = world
    sent = []

    for who in ("admin", "other"):
        chat = people[who].post("/api/chat/sessions").json()["id"]
        r = people[who].post(MESSAGES.format(chat), json={"text": "hello"})
        sent.append((r.status_code, r.json()["outcome"], leaks(r)))

    assert sent == [(200, "ok", False)] * 2
    assert upstream.authorization == [f"Bearer {KEY}"] * 2
    assert not re.search(re.escape(KEY), json.dumps(sent))
