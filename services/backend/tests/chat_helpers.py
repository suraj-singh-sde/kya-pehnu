"""Helpers of the chat core tests (tools, turn loop, proposals, privacy): drive a real turn through the route.

A turn is scripted with `FakeTransport.chat_replies` (`say`, `ask` from `tests/chat_fakes.py`), so a test names what
the model "decides" and reads back what the provider was sent, what was stored and what the page would show.
"""

import json
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import date, datetime
from typing import Any

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import Engine, event, text
from sqlmodel import Session, col, select

from kyapehnu.agent.llm.base import ToolResultMsg
from kyapehnu.agent.models import ChatMessage, LlmCall, ToolCall
from kyapehnu.core.config import Settings
from kyapehnu.wardrobe.models import Event, Item
from tests.conftest import LOOPBACK_PEER
from tests.fakes import FakeTransport, add_item, ensure_owner

NOW = datetime(2026, 10, 7, 9, 30)  # a Wednesday: the clock of every chat test
MESSAGES = "/api/chat/sessions/{}/messages"
PROPOSAL_TEXT = "Shown to the user as a card with Confirm and Cancel. Nothing changes until they press Confirm."
NINE = (
    "search_wardrobe",
    "get_item",
    "generate_outfits",
    "get_weather",
    "get_wear_history",
    "log_wear",
    "record_feedback",
    "update_item_status",
    "create_event",
)
PICK = {"picks": [{"candidate_number": 1, "reason": "Looks sharp.", "swap_slot": None, "swap_item_id": None}]}
DINNER = {
    "type": "party",
    "title": "Dinner with Priya",
    "starts_at": "2026-10-10T20:00",
    "place": "Khan Market",
    "dress_code": "smart casual",
    "audience_tags": ["Priya"],
}


class _FixedDate(date):
    @classmethod
    def today(cls) -> "_FixedDate":
        return cls(NOW.year, NOW.month, NOW.day)


def pin_rest_today(monkeypatch: Any) -> None:
    """The Outfits and Items routes read `date.today()`: make it the chat's day, so REST and a tool agree."""
    monkeypatch.setattr("kyapehnu.wardrobe.api.outfits.date", _FixedDate)
    monkeypatch.setattr("kyapehnu.wardrobe.api.items.date", _FixedDate)


class OpenAILikeTransport(FakeTransport):
    """A stand-in that looks like an Ollama server to the provider: the only kind whose refusals are remembered."""

    provider = "openai_compatible"
    model = "local-1"
    base_url = "http://localhost:11434/v1"
    api_key = "ollama"


def engine_of(client: TestClient) -> Engine:
    return client.app.state.ctx.engine  # type: ignore[no-any-return,attr-defined]


def new_chat(client: TestClient) -> int:
    r = client.post("/api/chat/sessions")
    assert r.status_code == 201
    return int(r.json()["id"])


def quiet(client: TestClient) -> TestClient:
    """The same app, answering an unexpected exception with its 500 response instead of raising it into the test."""
    return TestClient(client.app, base_url="http://localhost", client=LOOPBACK_PEER, raise_server_exceptions=False)


@contextmanager
def running_app(tmp_path: Any, fake_weather: Any, transport: Any, **settings: Any) -> Iterator[TestClient]:
    """An app with its own data folder, provider stand-in and Settings fields (a key, for the redaction tests)."""
    from kyapehnu.core.app import create_app  # lazy: keeps the test modules importable on their own

    fields = {"llm_provider": "anthropic", "anthropic_api_key": "test-key"} | settings
    config = Settings(_env_file=None, data_dir=tmp_path / "own-data", config_file=tmp_path / "own.yaml", **fields)
    app = create_app(settings=config, transport=transport, weather_transport=httpx.MockTransport(fake_weather))
    with TestClient(app, base_url="http://localhost", client=LOOPBACK_PEER) as c:
        yield c


def send(
    client: TestClient, fake: FakeTransport, session_id: int, *replies: Any, text: str = "hello"
) -> httpx.Response:
    """Script the model's replies, post one message to the chat, return the response as it is."""
    fake.chat_replies.extend(replies)
    return client.post(MESSAGES.format(session_id), json={"text": text})


def turn(
    client: TestClient, fake: FakeTransport, session_id: int, *replies: Any, text: str = "hello"
) -> dict[str, Any]:
    """Like `send`, for a turn that must succeed: the 200 body (`outcome`, `turn`, `session`)."""
    r = send(client, fake, session_id, *replies, text=text)
    assert r.status_code == 200, r.text
    return dict(r.json())


def tool_rows(engine: Engine, session_id: int = 1) -> list[ToolCall]:
    with Session(engine) as s:
        stmt = select(ToolCall).where(col(ToolCall.session_id) == session_id).order_by(col(ToolCall.id))
        return list(s.exec(stmt).all())


def message_rows(engine: Engine, session_id: int = 1) -> list[ChatMessage]:
    with Session(engine) as s:
        stmt = select(ChatMessage).where(col(ChatMessage.session_id) == session_id)
        return list(s.exec(stmt.order_by(col(ChatMessage.turn), col(ChatMessage.step), col(ChatMessage.id))).all())


def llm_rows(engine: Engine, purpose: str | None = None) -> list[LlmCall]:
    with Session(engine) as s:
        stmt = select(LlmCall).order_by(col(LlmCall.id))
        if purpose is not None:
            stmt = stmt.where(col(LlmCall.purpose) == purpose)
        return list(s.exec(stmt).all())


def count_rows(engine: Engine, *models: type) -> dict[str, int]:
    with Session(engine) as s:
        return {m.__name__: len(s.exec(select(m)).all()) for m in models}  # type: ignore[call-overload]


def raw_results(fake: FakeTransport, request: int = -1) -> list[ToolResultMsg]:
    """The tool results the provider was sent in one request (the last by default), exactly as sent."""
    return [m for m in fake.chat_calls[request]["messages"] if isinstance(m, ToolResultMsg)]


def results(fake: FakeTransport, request: int = -1) -> list[tuple[ToolResultMsg, Any]]:
    """The same results, each with its parsed JSON (a result that was cut is not valid JSON: use `raw_results`)."""
    return [(m, json.loads(m.content)) for m in raw_results(fake, request)]


def result(fake: FakeTransport, request: int = -1) -> Any:
    """The parsed JSON of the newest tool result in one request (the last by default)."""
    return results(fake, request)[-1][1]


def rows_holding(engine: Engine, *needles: str) -> list[str]:
    """Every table that has a row whose values contain one of the needles."""
    found = []
    with engine.connect() as conn:
        tables = [r[0] for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type = 'table'"))]
        for table in tables:
            for row in conn.execute(text(f'SELECT * FROM "{table}"')):  # noqa: S608 - names come from sqlite_master
                if any(needle in json.dumps(list(row), default=str) for needle in needles):
                    found.append(table)
    return found


@contextmanager
def watching(engine: Engine, when: str, action: Callable[[], None]) -> Iterator[list[int]]:
    """While open, `action()` runs just before every statement that matches the regex `when`. Yields the hit list."""
    pattern, hits = re.compile(when), []

    def hook(conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, executemany: bool) -> None:
        if pattern.search(statement):
            hits.append(1)
            action()

    event.listen(engine, "before_cursor_execute", hook)
    try:
        yield hits
    finally:
        event.remove(engine, "before_cursor_execute", hook)


@contextmanager
def failing(
    engine: Engine, when: str, error: Callable[[], BaseException], *, once: bool = True, skip: int = 0
) -> Iterator[list[int]]:
    """While open, a statement matching the regex `when` raises `error()`: the first one (`once`) or every one, after
    letting `skip` of them through.

    The failure happens inside the database layer, so it reaches the code under test whatever it calls to get there.
    Yields a list that gets one entry per failure.
    """
    seen, fired = [], []

    def action() -> None:
        seen.append(1)
        if len(seen) > skip and not (once and fired):
            fired.append(1)
            raise error()

    with watching(engine, when, action):
        yield fired


def party_wardrobe(s: Session, tops: int = 3, bottoms: int = 2, shoes: int = 2, **common: Any) -> dict[str, list[Item]]:
    """Available white items that every party in mild weather accepts: tops x bottoms x shoes outfits exist."""
    fields = {"formality": 3, "warmth": 2, **common}
    made: dict[str, list[Item]] = {"top": [], "bottom": [], "footwear": []}
    for category, n in (("top", tops), ("bottom", bottoms), ("footwear", shoes)):
        for k in range(n):
            made[category].append(
                add_item(
                    s,
                    name=f"{category} {k}",
                    category=category,
                    colors=[{"hex": "#ffffff", "name": "white"}],
                    **fields,
                )
            )
    return made


def add_party(s: Session, **fields: Any) -> Event:
    """A party on Saturday 2026-10-10 at 20:00 in Gurugram, for nobody in particular."""
    row = Event(
        **{
            "user_id": ensure_owner(s),
            "type": "party",
            "title": "Dinner",
            "starts_at": datetime(2026, 10, 10, 20),
            "place": "Gurugram",
            "audience_tags": [],
            **fields,
        }
    )
    s.add(row)
    s.commit()
    s.refresh(row)
    return row
