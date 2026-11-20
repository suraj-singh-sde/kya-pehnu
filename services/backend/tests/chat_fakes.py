"""Helpers for the chat tests (LLD 8.1): scripted model replies, a fixed clock, a seeded wardrobe, the privacy markers.

The chat-capable transport itself is `tests.fakes.FakeTransport` (`chat_replies`, `chat_calls`, `call_chat`). The
stand-in SDK clients are in `tests/sdk_fakes.py` (WP-2). The `unconfigured_client` fixture is in `tests/conftest.py`.
"""

import json
from dataclasses import asdict, dataclass, is_dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Final

import pytest
from sqlalchemy import Engine
from sqlmodel import Session

from kyapehnu.agent.llm.base import ChatResult, ToolCallRequest
from kyapehnu.agent.models import ChatSession, ToolCall
from kyapehnu.files.local import LocalStore
from kyapehnu.user.models import UserProfile
from kyapehnu.user.profile import PROFILE_ID
from kyapehnu.wardrobe.models import Event, WearLog
from kyapehnu.wardrobe.seed import seed
from tests.fakes import FakeTransport, add_item, ensure_owner

INPUT_TOKENS: Final = 10
OUTPUT_TOKENS: Final = 5

# AC-35 and AC-50: none of these may ever reach the provider, a log line, an LlmCall row or (after delete) a chat table.
HOME_CITY_MARKER: Final = "Zedtown Distinct"
BUDGET_MARKERS: Final = ("1234567", "7654321")
PRICE_MARKER: Final = "9876543"
ITEM_NOTE_MARKER: Final = "NOTE-MARKER-4H8T"
EVENT_TITLE_MARKER: Final = "Dinner with Priya Distinct"
EVENT_NOTES_MARKER: Final = "EVNOTE-MARKER-2P6R"
AUDIENCE_MARKER: Final = "Priya Distinct"
PLACE_MARKER: Final = "PLACE-MARKER-5R2D"
MARKERS: Final = (
    HOME_CITY_MARKER,
    *BUDGET_MARKERS,
    PRICE_MARKER,
    ITEM_NOTE_MARKER,
    EVENT_TITLE_MARKER,
    EVENT_NOTES_MARKER,
    AUDIENCE_MARKER,
    PLACE_MARKER,
)


def say(text: str) -> ChatResult:
    """A reply with text and no tool call."""
    return ChatResult(text, (), INPUT_TOKENS, OUTPUT_TOKENS)


def ask(*calls: tuple[str, dict[str, Any]], text: str = "") -> ChatResult:
    """A reply asking for these tool calls, (name, arguments) each, with the ids c1, c2, ... in this reply."""
    requests = tuple(ToolCallRequest(f"c{n}", name, args) for n, (name, args) in enumerate(calls, start=1))
    return ChatResult(text, requests, INPUT_TOKENS, OUTPUT_TOKENS)


def fixed_now(monkeypatch: pytest.MonkeyPatch, moment: datetime) -> None:
    """The chat and weather routes read the local time through `kyapehnu.core.deps.local_now`."""
    monkeypatch.setattr("kyapehnu.core.deps.local_now", lambda: moment)


def seed_items(engine: Engine, data_dir: Path) -> int:
    """The 32 seed items, as `kyapehnu seed` makes them. Returns how many."""
    with Session(engine) as s:
        owner = ensure_owner(s)
        s.commit()
    return seed(owner, engine, LocalStore(data_dir))


def _plain(value: object) -> object:
    return asdict(value) if is_dataclass(value) and not isinstance(value, type) else str(value)


def sent_text(fake: FakeTransport) -> str:
    """Everything the stand-in provider was sent, as one string: every chat request and every JSON (pick) request."""
    return json.dumps([fake.chat_calls, fake.calls], default=_plain)


@dataclass(frozen=True)
class MarkerWorld:
    top_id: int
    bottom_id: int
    shoes_id: int
    form_event_id: int  # made on the Outfits form: no ToolCall row, so never a session event
    earlier_event_id: int  # made by `create_event` in `earlier_session_id`
    place_event_id: int  # made by `create_event` in `earlier_session_id`, place PLACE_MARKER
    earlier_session_id: int


def _event(s: Session, **fields: Any) -> Event:
    event = Event(
        **{
            "user_id": ensure_owner(s),
            "type": "party",
            "place": HOME_CITY_MARKER,
            "audience_tags": [AUDIENCE_MARKER],
            **fields,
        }
    )
    s.add(event)
    s.commit()
    s.refresh(event)
    return event


def _created_by_chat(s: Session, session_id: int, event: Event, step: int) -> None:
    s.add(
        ToolCall(
            user_id=1,
            session_id=session_id,
            turn=1,
            step=step,
            call_id=f"c{step}",
            name="create_event",
            args={},
            result="{}",
            latency_ms=1,
            event_id=event.id,
        )
    )
    s.commit()


def marker_world(s: Session) -> MarkerWorld:
    """The data of AC-35 and AC-50: private values everywhere they could leak from.

    Profile: home city HOME_CITY_MARKER, budget 1234567 to 7654321. Three available items (a top, a bottom, shoes) with
    purchase price 9876543 and an item note. Three events, each with a wear logged: one made on the Outfits form (a
    party on 2026-10-10, title EVENT_TITLE_MARKER, notes EVENT_NOTES_MARKER, place the home city, audience
    AUDIENCE_MARKER); one made by chat in an earlier session (same text, 2026-10-03); one made by chat in that session
    with place PLACE_MARKER (2026-10-02).
    """
    profile = s.get(UserProfile, PROFILE_ID) or UserProfile(id=PROFILE_ID, user_id=ensure_owner(s))
    profile.home_city, profile.budget_min, profile.budget_max = HOME_CITY_MARKER, 1234567, 7654321
    s.add(profile)
    s.commit()

    common = {"formality": 3, "warmth": 2, "purchase_price": 9876543, "notes": ITEM_NOTE_MARKER}
    top = add_item(s, name="White shirt", category="top", **common)
    bottom = add_item(s, name="Navy chinos", category="bottom", **common)
    shoes = add_item(s, name="Brown loafers", category="footwear", **common)
    items = [top.id, bottom.id, shoes.id]

    text = {"title": EVENT_TITLE_MARKER, "notes": EVENT_NOTES_MARKER}
    form = _event(s, starts_at=datetime(2026, 10, 10, 19), **text)
    earlier = _event(s, starts_at=datetime(2026, 10, 3, 19), **text)
    elsewhere = _event(s, starts_at=datetime(2026, 10, 2, 19), title="Brunch", place=PLACE_MARKER)
    earlier_session = ChatSession(user_id=1, title="Earlier chat")
    s.add(earlier_session)
    s.commit()
    s.refresh(earlier_session)
    _created_by_chat(s, earlier_session.id, earlier, step=1)
    _created_by_chat(s, earlier_session.id, elsewhere, step=2)

    for event, worn_on in ((form, date(2026, 10, 5)), (earlier, date(2026, 10, 3)), (elsewhere, date(2026, 10, 2))):
        s.add(
            WearLog(
                user_id=ensure_owner(s),
                item_ids=items,
                worn_on=worn_on,
                event_id=event.id,
                audience_tags=[AUDIENCE_MARKER],
            )
        )
    s.commit()
    return MarkerWorld(top.id, bottom.id, shoes.id, form.id, earlier.id, elsewhere.id, earlier_session.id)
