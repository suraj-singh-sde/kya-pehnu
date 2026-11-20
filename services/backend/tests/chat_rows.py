"""Hand-built chat rows for the store, sessions and Debug tests: what the chat core writes, written directly."""

from collections.abc import Sequence
from datetime import date
from typing import Any

from sqlalchemy import Engine
from sqlmodel import Session

from kyapehnu.agent.models import ChatMessage, ChatSession, LlmCall, ToolCall
from kyapehnu.auth.accounts import upsert_google_user
from kyapehnu.core.db import utcnow


def with_owner(engine: Engine) -> Engine:
    """The engine, with account 1 in place: the owner of the rows these helpers write."""
    with Session(engine) as s:
        upsert_google_user(s, sub="sub-a", email="sub-a@example.com", name="sub-a", now=utcnow())
    return engine


def add_session(engine: Engine, title: str = "", user_id: int = 1, **fields: Any) -> int:
    with Session(engine) as s:
        chat = ChatSession(user_id=user_id, title=title, **fields)
        s.add(chat)
        s.commit()
        return chat.id


def add_message(
    engine: Engine, session_id: int, turn: int, step: int, role: str, content: str, user_id: int = 1
) -> int:
    with Session(engine) as s:
        row = ChatMessage(user_id=user_id, session_id=session_id, turn=turn, step=step, role=role, content=content)
        s.add(row)
        s.commit()
        return row.id


def add_call(engine: Engine, session_id: int, name: str, **fields: Any) -> int:
    """A stored tool call. Any column can be overridden; the defaults are an ordinary successful call."""
    with Session(engine) as s:
        row = ToolCall(
            **{
                "user_id": 1,
                "session_id": session_id,
                "turn": 1,
                "step": 1,
                "call_id": "c1",
                "name": name,
                "result": "{}",
                "latency_ms": 5,
            }
            | fields
        )
        s.add(row)
        s.commit()
        return row.id


def add_llm_call(engine: Engine, purpose: str, request_summary: str, **fields: Any) -> int:
    with Session(engine) as s:
        row = LlmCall(
            **{
                "user_id": 1,
                "provider": "fake",
                "model": "fake-1",
                "purpose": purpose,
                "request_summary": request_summary,
            }
            | {"latency_ms": 7}
            | fields
        )
        s.add(row)
        s.commit()
        return row.id


def suggestion(item_ids: Sequence[int], reason: str = "A good fit.", swap_item_id: int | None = None) -> dict[str, Any]:
    return {
        "item_ids": list(item_ids),
        "reason": reason,
        "notes": ["Rain likely: rain-safe footwear."],
        "swap_item_id": swap_item_id,
    }


def outfits_ui(suggestions: Sequence[dict[str, Any]], event_id: int | None = None, **extra: Any) -> dict[str, Any]:
    """The `ui` of a generate_outfits call (LLD 3.5)."""
    return {
        "kind": "outfits",
        "event_id": event_id,
        "weather": None,
        "problem": None,
        "suggestions": list(suggestions),
        "summary": f"{len(suggestions)} outfits",
    } | extra


def log_wear_ui(item_ids: Sequence[int], event_id: int | None = None) -> dict[str, Any]:
    return {
        "kind": "log_wear",
        "item_ids": list(item_ids),
        "event_id": event_id,
        "summary": "Waiting for your confirmation",
    }


def status_ui(item_id: int, status_from: str, status_to: str) -> dict[str, Any]:
    return {
        "kind": "update_item_status",
        "item_id": item_id,
        "from": status_from,
        "to": status_to,
        "summary": "Waiting for your confirmation",
    }


def proposal(ui: dict[str, Any], proposed_on: date, state: str = "pending", **fields: Any) -> dict[str, Any]:
    """Columns that make a stored call a proposal."""
    return {"ui": ui, "proposal_state": state, "proposed_on": proposed_on} | fields
