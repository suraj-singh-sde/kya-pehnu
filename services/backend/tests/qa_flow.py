"""QA: one run through everything that touches Valkey, shared by the key-kind (AC-44) and flush (AC-24) tests.

Sign in through the in-process Google (a `session:` key and `LIMITS` counts), sign a second person in, chat (a
`turnlock:` key exists only while the turn runs, so it is read from inside the turn), look up the weather (`weather:`)
and let an Ollama-like provider refuse tools (`notools:`).
"""

from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from kyapehnu.agent.llm.base import LLMProvider, ToolSpec, ToolsUnsupportedError, UserMsg
from kyapehnu.agent.llm.openai_compat import OpenAICompatTransport
from tests.chat_fakes import ask, fixed_now, say
from tests.chat_helpers import NOW, new_chat, turn
from tests.conftest import sign_in
from tests.fake_google import Person, complete_sign_in
from tests.fakes import add_item
from tests.sdk_fakes import OLLAMA_URL, FakeOpenAIClient, sdk_error

WEATHER = {"place": "Gurugram", "starts_at": "2026-10-10T20:00"}
ALLOWED_PREFIXES = ("session:", "LIMITS", "turnlock:", "weather:", "notools:")


@dataclass
class Flow:
    app: object
    a_id: int
    a: TestClient
    b_id: int
    b: TestClient
    chat: int
    item: int
    keys_in_turn: set[str] = field(default_factory=set)


def run_full_flow(make_app, google, fake, fake_weather, kv, monkeypatch) -> Flow:
    fixed_now(monkeypatch, NOW)
    app = make_app(rate_limit_storage=None)  # sign-in counts in Valkey, like production
    browser = TestClient(app, base_url="http://localhost", client=("203.0.113.5", 40000))
    assert complete_sign_in(browser, google, Person(sub="g-a", email="a@example.com", name="Asha")).status_code == 302
    for _ in range(2):  # more sign-in attempts: only the counter grows
        browser.get("/api/auth/google/login", follow_redirects=False)
    a_id = browser.get("/api/auth/me").json()["id"]
    b_id, b = sign_in(app, "g-b", email="b@example.com", name="Bela")

    with Session(app.state.ctx.engine) as s:
        item = add_item(s, user_id=a_id, name="Linen shirt").id
    seen: set[str] = set()

    def inside_the_turn():
        seen.update(kv.scan_iter("*"))  # the lock is held now
        return say("Wear the linen shirt.")

    chat = new_chat(browser)
    turn(browser, fake, chat, ask(("search_wardrobe", {})), inside_the_turn)
    assert browser.get("/api/weather", params=WEATHER).status_code == 200

    provider = LLMProvider(
        OpenAICompatTransport(
            OLLAMA_URL,
            "ollama",
            "m",
            client=FakeOpenAIClient(sdk_error("openai", "BadRequestError", 400, "m does not support tools")),
        ),
        kv=kv,
    )
    with pytest.raises(ToolsUnsupportedError):
        provider.chat(
            system="s",
            messages=[UserMsg("hi")],
            tools=[ToolSpec("get_item", "One item", {"type": "object", "properties": {}})],
            label="qa",
            first_call=True,
            user_id=a_id,
        )
    assert fake_weather.calls("forecast")
    return Flow(app, a_id, browser, b_id, b, chat, item, seen)
