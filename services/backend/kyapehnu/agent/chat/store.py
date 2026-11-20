"""The only writer of the chat tables. Each function opens its own short session unless it takes `s`."""

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, Final, cast

from sqlalchemy import Engine, delete, func, or_, update
from sqlalchemy.orm import defer
from sqlmodel import Session, col, select

from kyapehnu.agent.chat.limits import SESSION_LIST_LIMIT, TITLE_CHARS
from kyapehnu.agent.chat.schemas import (
    CardSuggestion,
    MessageOut,
    OutfitsCard,
    ProposalView,
    SessionOut,
    SessionSummary,
    ToolCallView,
)
from kyapehnu.agent.models import ChatMessage, ChatRole, ChatSession, LlmCall, ProposalState, ToolCall
from kyapehnu.core.db import get_owned, utcnow
from kyapehnu.core.schemas import clean_line
from kyapehnu.wardrobe.public import (
    Event,
    Item,
    ItemStatus,
    Problem,
    Thumb,
    WeatherOut,
    find_events,
    find_items,
    item_key,
    ratings_by_key,
)

log = logging.getLogger(__name__)

RESULT_UNKNOWN: Final = "Result unknown, check Wear history"  # confirmed, but the crash came before the write was noted
LOGGED_AS_WORN: Final = "Logged as worn on {worn_on}"
MARKED_AS: Final = "Marked as {status}"

# A JSON column the ORM wrote from None holds the text 'null', not SQL NULL: both mean "no outcome yet".
_NO_OUTCOME = or_(col(ToolCall.outcome).is_(None), func.json_type(col(ToolCall.outcome)) == "null")


def _summary(row: ChatSession) -> SessionSummary:
    return SessionSummary.model_validate(row, from_attributes=True)


def create_session(user_id: int, engine: Engine) -> SessionSummary:
    with Session(engine) as s:
        chat = ChatSession(user_id=user_id)
        s.add(chat)
        s.commit()
        s.refresh(chat)
        return _summary(chat)


def require_session(user_id: int, s: Session, session_id: int) -> None:
    """NotFoundError unless the chat exists and is the caller's."""
    get_owned(user_id, s, ChatSession, session_id)


def list_sessions(user_id: int, s: Session) -> list[SessionSummary]:
    """The caller's newest chats first, at most SESSION_LIST_LIMIT."""
    stmt = (
        select(ChatSession)
        .where(col(ChatSession.user_id) == user_id)
        .order_by(col(ChatSession.updated_at).desc(), col(ChatSession.id).desc())
        .limit(SESSION_LIST_LIMIT)
    )
    return [_summary(row) for row in s.exec(stmt)]


def start_turn(user_id: int, engine: Engine, session_id: int, text: str) -> int:
    """Store the user's message as step 0 of the next turn (the chat is titled by its first message)."""
    with Session(engine) as s:
        chat = get_owned(user_id, s, ChatSession, session_id)
        latest = func.coalesce(func.max(col(ChatMessage.turn)), 0)
        turn = s.exec(select(latest + 1).where(col(ChatMessage.session_id) == session_id)).one()
        s.add(ChatMessage(user_id=user_id, session_id=session_id, turn=turn, step=0, role=ChatRole.user, content=text))
        if not chat.title:
            chat.title = str(clean_line(text))[:TITLE_CHARS].strip()
        s.commit()
        return turn


def add_message(
    user_id: int, engine: Engine, session_id: int, turn: int, step: int, role: ChatRole, content: str
) -> None:
    with Session(engine) as s:
        s.add(ChatMessage(user_id=user_id, session_id=session_id, turn=turn, step=step, role=role, content=content))
        s.commit()


def add_tool_call(engine: Engine, row: ToolCall) -> int:
    with Session(engine) as s:
        s.add(row)
        s.flush()
        tool_call_id = cast(int, row.id)  # flushed rows have one
        s.commit()
        return tool_call_id


def finish_turn(engine: Engine, session_id: int) -> None:
    with Session(engine) as s:
        s.exec(update(ChatSession).where(col(ChatSession.id) == session_id).values(updated_at=utcnow()))
        s.commit()


def session_event_ids(user_id: int, s: Session, session_id: int) -> set[int]:
    """The events this chat created (by create_event or an inline generate_outfits): the only events whose title,
    notes and place may reach the model (H1)."""
    stmt = select(col(ToolCall.event_id)).where(
        col(ToolCall.user_id) == user_id,
        col(ToolCall.session_id) == session_id,
        col(ToolCall.event_id).is_not(None),
    )
    return {event_id for event_id in s.exec(stmt) if event_id is not None}


def recent_tool_calls(user_id: int, s: Session, *, session_id: int | None, limit: int) -> list[ToolCall]:
    stmt = select(ToolCall).where(col(ToolCall.user_id) == user_id).order_by(col(ToolCall.id).desc()).limit(limit)
    if session_id is not None:
        stmt = stmt.where(col(ToolCall.session_id) == session_id)
    return list(s.exec(stmt).all())


# --- the session view: what the stored `ui` points at is read in a fixed number of queries ---------------------------


@dataclass(frozen=True)
class _Known:
    """The rows the stored calls refer to. A missing item (hard-deleted) is simply absent."""

    items: dict[int, Item]
    events: dict[int, Event]
    ratings: dict[str, int]

    def item(self, item_id: int | None) -> Item | None:
        return self.items.get(item_id) if item_id is not None else None

    def event(self, event_id: int | None) -> Event | None:
        return self.events.get(event_id) if event_id is not None else None


def _item_ids(tc: ToolCall) -> set[int]:
    ui = tc.ui or {}
    match ui.get("kind"):
        case "outfits":
            return {i for s in ui["suggestions"] for i in (*s["item_ids"], s["swap_item_id"]) if i is not None}
        case "log_wear":
            return set(ui["item_ids"])
        case "update_item_status":
            return {ui["item_id"]}
    return set()


def _event_ids(tc: ToolCall) -> set[int]:
    return {i for i in (tc.event_id, (tc.ui or {}).get("event_id")) if i is not None}


def _item_keys(tc: ToolCall) -> set[str]:
    ui = tc.ui or {}
    return {item_key(s["item_ids"]) for s in ui["suggestions"]} if ui.get("kind") == "outfits" else set()


def _load_known(user_id: int, s: Session, calls: Iterable[ToolCall]) -> _Known:
    calls = list(calls)
    item_ids = set().union(*map(_item_ids, calls))
    event_ids = set().union(*map(_event_ids, calls))
    keys = set().union(*map(_item_keys, calls))
    return _Known(
        find_items(user_id, s, item_ids), find_events(user_id, s, event_ids), ratings_by_key(user_id, s, keys)
    )


def _wearable(items: Sequence[Item | None]) -> bool:
    return all(i is not None and i.status == ItemStatus.available for i in items)


def _suggestion(raw: dict[str, Any], known: _Known) -> CardSuggestion:
    ids: list[int] = raw["item_ids"]
    items = [known.item(i) for i in ids]
    return CardSuggestion(
        item_ids=ids,
        items=items,
        reason=raw["reason"],
        notes=raw["notes"],
        swap_item=known.item(raw["swap_item_id"]),
        rating=cast(Thumb | None, known.ratings.get(item_key(ids))),
        wearable=_wearable(items),
    )


def _card(ui: dict[str, Any], known: _Known) -> OutfitsCard:
    return OutfitsCard(
        event=known.event(ui.get("event_id")),
        weather=WeatherOut.model_validate(ui["weather"]) if ui.get("weather") else None,
        problem=Problem.model_validate(ui["problem"]) if ui.get("problem") else None,
        suggestions=[_suggestion(raw, known) for raw in ui["suggestions"]],
    )


def _result(kind: str, state: ProposalState, outcome: dict[str, Any] | None) -> str | None:
    if state != ProposalState.confirmed:
        return None
    if not outcome:
        return RESULT_UNKNOWN
    if kind == "log_wear":
        return LOGGED_AS_WORN.format(worn_on=outcome["worn_on"])
    return MARKED_AS.format(status=outcome["status"])


def _can_apply(ui: dict[str, Any], items: Sequence[Item | None]) -> bool:
    """Everything the proposal names still exists, and still is as it was when proposed."""
    if ui["kind"] == "log_wear":
        return _wearable(items)
    (item,) = items
    return item is not None and item.status == ui["from"]


def _proposal(tc: ToolCall, known: _Known, today: date) -> ProposalView:
    ui = cast(dict[str, Any], tc.ui)
    kind = ui["kind"]
    stored = ProposalState(cast(str, tc.proposal_state))  # only proposals come here
    proposed_on = cast(date, tc.proposed_on)
    state = ProposalState.expired if stored == ProposalState.pending and proposed_on < today else stored
    is_wear = kind == "log_wear"
    item_ids: list[int] = ui["item_ids"] if is_wear else [ui["item_id"]]
    items = [known.item(i) for i in item_ids]
    event = known.event(ui.get("event_id")) if is_wear else None
    event_deleted = is_wear and ui.get("event_id") is not None and event is None
    return ProposalView(
        tool_call_id=cast(int, tc.id),
        kind=kind,
        state=state,
        proposed_on=proposed_on,
        decided_at=tc.decided_at,
        item_ids=item_ids,
        items=items,
        event=event,
        status_from=None if is_wear else ItemStatus(ui["from"]),
        status_to=None if is_wear else ItemStatus(ui["to"]),
        actionable=state == ProposalState.pending and not event_deleted and _can_apply(ui, items),
        result=_result(kind, stored, tc.outcome),
    )


def _call_view(tc: ToolCall, known: _Known, today: date) -> ToolCallView:
    ui = tc.ui or {}
    return ToolCallView(
        id=cast(int, tc.id),
        turn=tc.turn,
        step=tc.step,
        name=tc.name,
        error=tc.error,
        summary=ui.get("summary", ""),
        card=_card(ui, known) if ui.get("kind") == "outfits" else None,
        event=known.event(tc.event_id),
        proposal=_proposal(tc, known, today) if tc.proposal_state is not None else None,
    )


def session_view(user_id: int, s: Session, session_id: int, today: date) -> SessionOut:
    """The chat as it was, rebuilt from stored rows: cards, proposals and thumbs show the items as they are now."""
    chat = get_owned(user_id, s, ChatSession, session_id)
    messages = s.exec(
        select(ChatMessage)
        .where(col(ChatMessage.session_id) == session_id)
        .order_by(col(ChatMessage.turn), col(ChatMessage.step), col(ChatMessage.id))
    ).all()
    calls = s.exec(
        select(ToolCall)
        .options(defer(ToolCall.args), defer(ToolCall.result))  # type: ignore[arg-type]  # 12,000 characters each, never shown
        .where(col(ToolCall.session_id) == session_id)
        .order_by(col(ToolCall.turn), col(ToolCall.step), col(ToolCall.id))
    ).all()
    known = _load_known(user_id, s, calls)
    return SessionOut(
        session=_summary(chat),
        messages=[MessageOut.model_validate(m, from_attributes=True) for m in messages],
        tool_calls=[_call_view(tc, known, today) for tc in calls],
    )


def proposal_view(user_id: int, s: Session, tc: ToolCall, today: date) -> ProposalView:
    return _proposal(tc, _load_known(user_id, s, [tc]), today)


# --- proposal state: conditional UPDATEs, so a second claim finds nothing to change -----------------------------------


def transition(
    engine: Engine,
    tool_call_id: int,
    frm: ProposalState,
    to: ProposalState,
    *,
    outcome: dict[str, Any] | None = None,
) -> bool:
    """Move a proposal from `frm` to `to`. False when it was not in `frm` (someone else decided first).

    Going back from confirmed (the compensation) only works while no outcome is recorded: a finished write is final.
    """
    values: dict[str, Any] = {
        "proposal_state": to.value,
        "decided_at": None if to == ProposalState.pending else utcnow(),
    }
    if outcome is not None:
        values["outcome"] = outcome
    stmt = update(ToolCall).where(col(ToolCall.id) == tool_call_id, col(ToolCall.proposal_state) == frm.value)
    if frm == ProposalState.confirmed:
        stmt = stmt.where(_NO_OUTCOME)
    with Session(engine) as s:
        won = s.exec(stmt.values(**values)).rowcount == 1
        s.commit()
    return won


def set_outcome(engine: Engine, tool_call_id: int, outcome: dict[str, Any]) -> None:
    with Session(engine) as s:
        s.exec(update(ToolCall).where(col(ToolCall.id) == tool_call_id).values(outcome=outcome))
        s.commit()


def delete_session(user_id: int, engine: Engine, session_id: int) -> None:
    """Remove a chat, its messages, its tool calls and the nested outfit-pick rows that quote its events.

    The chat's own LlmCall rows hold counts only, so they stay in the Debug log. The caller holds the TurnLock.
    """
    pick_rows = (
        col(LlmCall.user_id) == user_id,
        col(LlmCall.purpose) == "outfit_pick",
        col(LlmCall.request_summary).like(f"session={session_id} %"),
    )
    with Session(engine) as s:
        get_owned(user_id, s, ChatSession, session_id)
        s.exec(delete(ToolCall).where(col(ToolCall.session_id) == session_id))
        s.exec(delete(ChatMessage).where(col(ChatMessage.session_id) == session_id))
        s.exec(delete(LlmCall).where(*pick_rows))
        s.exec(delete(ChatSession).where(col(ChatSession.id) == session_id))
        s.commit()
    log.info("event=chat_session_deleted session_id=%s", session_id)
