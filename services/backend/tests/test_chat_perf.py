"""AC-40 (chat half; LLD 8.5, NFR-7): 200 chats of 100 messages each list and open within 1 s, and a tool call that is
not `generate_outfits` adds at most 0.5 s on the same data.

Slow: these run in the full gate, not in the fast loop. Timing rule: one warm-up run is discarded, then the median of 3
runs must be within the limit.

AC-26 (chat history list): 1,000 chats of one user listed 100 times, p95 at most 300 ms. Small data, so it runs in the
fast loop (the module has no `slow` mark; each heavy test carries its own).
"""

import math
import statistics
import time
from collections.abc import Callable
from datetime import timedelta

import pytest
from sqlalchemy import Engine, insert
from sqlmodel import Session

from kyapehnu.agent.chat.privacy import ChatView
from kyapehnu.agent.chat.tools import ToolEnv, TurnBudget, run_call
from kyapehnu.agent.llm.base import ToolCallRequest
from kyapehnu.agent.models import ChatMessage, ChatSession, ToolCall
from kyapehnu.core.db import utcnow
from kyapehnu.wardrobe.models import Item, WearLog
from tests._legacy_owner_user import LEGACY_OWNER_ID
from tests.chat_fakes import fixed_now
from tests.chat_helpers import NOW, pin_rest_today
from tests.chat_rows import outfits_ui, suggestion
from tests.fakes import OWNER_ID

SESSIONS = 200
HISTORY_CHATS = 1000  # AC-26
HISTORY_CALLS = 100
HISTORY_P95_SECONDS = 0.3
TURNS = 50  # a user message and an assistant message each: 100 messages per chat
GENERATE_CALLS = 10  # `generate_outfits` calls per chat, 3 suggestions each
ITEMS = 1000
WEARS = 500
LIST_SECONDS = OPEN_SECONDS = 1.0
TOOL_SECONDS = 0.5
RUNS = 3
OPEN = 100  # the chat that is opened


def median_seconds(run: Callable[[], object]) -> float:
    run()  # warm-up
    times = []
    for _ in range(RUNS):
        start = time.perf_counter()
        run()
        times.append(time.perf_counter() - start)
    return statistics.median(times)


def seed(engine: Engine) -> None:
    """1,000 items, 200 chats, 20,000 messages and 2,000 generate_outfits calls, inserted with `executemany`.

    Bulk inserts skip the ORM events the legacy-owner stamper hooks, so the owner is set explicitly."""
    with Session(engine, expire_on_commit=False) as s:
        s.add_all(
            Item(
                user_id=OWNER_ID,
                name=f"Item {n}",
                category=("top", "bottom", "footwear", "outerwear")[n % 4],
                colors=[{"hex": "#336699", "name": "blue"}],
                tags=["linen", "casual"],
            )
            for n in range(ITEMS)
        )
        s.commit()
        s.add_all(
            WearLog(
                user_id=OWNER_ID,
                item_ids=[n % ITEMS + 1, (n + 1) % ITEMS + 1],
                worn_on=NOW.date() - timedelta(days=n % 90),
            )
            for n in range(WEARS)
        )
        s.commit()
        stamp = utcnow()
        s.execute(
            insert(ChatSession),
            [
                {"id": n, "user_id": LEGACY_OWNER_ID, "title": f"Chat {n}", "created_at": stamp, "updated_at": stamp}
                for n in range(1, SESSIONS + 1)
            ],
        )
        s.execute(
            insert(ChatMessage),
            [
                {
                    "user_id": LEGACY_OWNER_ID,
                    "session_id": chat,
                    "turn": turn,
                    "step": step,
                    "role": role,
                    "content": f"{role} text {turn}" * 5,
                    "created_at": stamp,
                }
                for chat in range(1, SESSIONS + 1)
                for turn in range(1, TURNS + 1)
                for step, role in ((0, "user"), (1, "assistant"))
            ],
        )
        s.execute(
            insert(ToolCall),
            [
                {
                    "user_id": LEGACY_OWNER_ID,
                    "session_id": chat,
                    "turn": turn,
                    "step": 1,
                    "call_id": "c1",
                    "name": "generate_outfits",
                    "args": {"event_id": 1},
                    "result": "{}",
                    "truncated": False,
                    "latency_ms": 5,
                    "ui": outfits_ui(
                        [suggestion([(chat + turn + k + j) % ITEMS + 1 for j in range(3)]) for k in range(3)],
                        event_id=None,
                    ),
                    "created_at": stamp,
                }
                for chat in range(1, SESSIONS + 1)
                for turn in range(1, GENERATE_CALLS + 1)
            ],
        )
        s.commit()


def seed_history(engine: Engine) -> None:
    """1,000 chats of one user, one `executemany`, each with its own activity time."""
    first = utcnow()
    with Session(engine) as s:
        s.execute(
            insert(ChatSession),
            [
                {
                    "id": n,
                    "user_id": LEGACY_OWNER_ID,
                    "title": f"Chat {n}",
                    "created_at": first,
                    "updated_at": first + timedelta(minutes=n),
                }
                for n in range(1, HISTORY_CHATS + 1)
            ],
        )
        s.commit()


def p95(times: list[float]) -> float:
    """Nearest rank: the value that 95% of the timings do not exceed."""
    return sorted(times)[math.ceil(0.95 * len(times)) - 1]


def test_listing_1000_chats_has_a_p95_of_at_most_300_ms(client):
    seed_history(client.app.state.ctx.engine)
    first = client.get("/api/chat/sessions")  # also the warm-up: the first call opens the pool
    assert first.status_code == 200 and len(first.json()) == 500

    times = []
    for _ in range(HISTORY_CALLS):
        start = time.perf_counter()
        reply = client.get("/api/chat/sessions")
        times.append(time.perf_counter() - start)
        assert reply.status_code == 200

    assert len(times) == HISTORY_CALLS
    assert p95(times) <= HISTORY_P95_SECONDS, f"list p95 {p95(times):.3f}s"


@pytest.fixture
def world(client, monkeypatch):
    fixed_now(monkeypatch, NOW)
    pin_rest_today(monkeypatch)
    seed(client.app.state.ctx.engine)
    return client


@pytest.mark.slow
def test_listing_chats_and_opening_one_take_at_most_a_second_each(world):
    listed = world.get("/api/chat/sessions")
    opened = world.get(f"/api/chat/sessions/{OPEN}")
    assert listed.status_code == 200 and len(listed.json()) == SESSIONS
    body = opened.json()
    assert (
        opened.status_code == 200 and len(body["messages"]) == 2 * TURNS and len(body["tool_calls"]) == GENERATE_CALLS
    )
    assert all(len(c["card"]["suggestions"]) == 3 for c in body["tool_calls"])

    list_time = median_seconds(lambda: world.get("/api/chat/sessions"))
    open_time = median_seconds(lambda: world.get(f"/api/chat/sessions/{OPEN}"))

    assert list_time <= LIST_SECONDS, f"list median {list_time:.2f}s"
    assert open_time <= OPEN_SECONDS, f"open median {open_time:.2f}s"


TOOL_CALLS = {
    "search_wardrobe": {},
    "get_item": {"item_id": 7},
    "get_weather": {"place": "Paris"},
    "get_wear_history": {"item_id": 7, "days": 3650},
    "log_wear": {"item_ids": [1, 2, 3]},
    "record_feedback": {"item_ids": [1, 2, 3], "rating": 1, "reason": "nice"},
    "update_item_status": {"item_id": 9, "status": "laundry"},
    "create_event": {"type": "party", "starts_at": "2026-10-10T20:00", "place": "Khan Market"},
}


@pytest.mark.slow
@pytest.mark.parametrize("name", list(TOOL_CALLS))
def test_a_tool_other_than_generate_adds_at_most_half_a_second(world, name):
    ctx = world.app.state.ctx
    env = ToolEnv(1, ctx, ChatView(1, set(), "Gurugram"), NOW, TurnBudget(deadline=time.monotonic() + 600))
    call = ToolCallRequest("c1", name, TOOL_CALLS[name])

    def once() -> None:
        reply = run_call(env, call, 1, 999, 1)
        assert not reply.is_error, reply.content

    assert median_seconds(once) <= TOOL_SECONDS
