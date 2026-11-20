"""Confirm or cancel a change the model proposed (LLD 4.4).

A proposal is claimed with one conditional UPDATE, so two presses write once. The claim is undone only when the write
raised before it committed; a write that committed is final, even when noting its outcome fails.
"""

import logging
from datetime import date
from typing import Any, cast

from sqlalchemy import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session

from kyapehnu.agent.chat import store
from kyapehnu.agent.chat.schemas import ProposalView
from kyapehnu.agent.models import ProposalState, ToolCall
from kyapehnu.core.context import Ctx
from kyapehnu.core.db import get_owned
from kyapehnu.core.errors import NotFoundError, ProposalExpiredError
from kyapehnu.wardrobe.public import ItemStatus, WearIn, find_events, find_items, set_status, wear

log = logging.getLogger(__name__)

DECIDED = "event=proposal_decided tool_call_id=%s state=%s"


def _proposal(user_id: int, s: Session, tool_call_id: int) -> ToolCall:
    """The caller's proposal. Another account's, a plain tool call and a missing id all look the same: not found."""
    tc = get_owned(user_id, s, ToolCall, tool_call_id)
    if tc.proposal_state is None:
        raise NotFoundError(f"Proposal {tool_call_id} not found")
    return tc


def _view(user_id: int, engine: Engine, tool_call_id: int, today: date) -> ProposalView:
    """The proposal as stored now."""
    with Session(engine) as s:
        return store.proposal_view(user_id, s, _proposal(user_id, s, tool_call_id), today)


def _stale(user_id: int, s: Session, tc: ToolCall, today: date) -> str | None:
    """Why the proposal can no longer be carried out, or None when it still can."""
    ui = cast(dict[str, Any], tc.ui)
    if cast(date, tc.proposed_on) < today:
        return "the day of this proposal has ended"
    wanted = ui["item_ids"] if ui["kind"] == "log_wear" else [ui["item_id"]]
    found = find_items(user_id, s, wanted)
    if len(found) < len(set(wanted)):
        return "an item was deleted"
    if ui["kind"] == "log_wear":
        if ui["event_id"] is not None and not find_events(user_id, s, [ui["event_id"]]):
            return "the event was deleted"
        return None if all(i.status == ItemStatus.available for i in found.values()) else "an item is not available"
    return None if found[wanted[0]].status == ui["from"] else "the item's status has changed"


def _apply(user_id: int, ctx: Ctx, ui: dict[str, Any], today: date) -> dict[str, Any]:
    """Write what was proposed and return what to remember of it. It returns only after the commit, and reads nothing
    back (the session does not expire its rows), so an exception from here means nothing was written."""
    with Session(ctx.engine, expire_on_commit=False) as s:
        if ui["kind"] == "log_wear":
            outfit = wear(user_id, ctx, s, WearIn(item_ids=ui["item_ids"], event_id=ui["event_id"]), today)
            return {"outfit_id": outfit.id, "worn_on": today.isoformat()}
        status = ItemStatus(ui["to"])
        set_status(user_id, s, ui["item_id"], status)
        return {"item_id": ui["item_id"], "status": status.value}


def _expire(user_id: int, ctx: Ctx, tool_call_id: int, reason: str, today: date) -> ProposalView:
    if not store.transition(ctx.engine, tool_call_id, ProposalState.pending, ProposalState.expired):
        return _view(user_id, ctx.engine, tool_call_id, today)  # someone else decided first
    log.info(DECIDED, tool_call_id, ProposalState.expired.value)
    raise ProposalExpiredError(f"Not done: {reason}. Nothing was changed.")


def confirm(user_id: int, ctx: Ctx, tool_call_id: int, today: date) -> ProposalView:
    """Carry the proposal out once. Decided already: the stored state, nothing written. Out of date: expired, 409."""
    with Session(ctx.engine) as s:
        tc = _proposal(user_id, s, tool_call_id)
        if tc.proposal_state != ProposalState.pending:
            return store.proposal_view(user_id, s, tc, today)
        ui, reason = cast(dict[str, Any], tc.ui), _stale(user_id, s, tc, today)
    if reason:
        return _expire(user_id, ctx, tool_call_id, reason, today)
    if not store.transition(ctx.engine, tool_call_id, ProposalState.pending, ProposalState.confirmed):
        return _view(user_id, ctx.engine, tool_call_id, today)  # a double press or a second tab: no write
    try:
        outcome = _apply(user_id, ctx, ui, today)
    except Exception:
        store.transition(ctx.engine, tool_call_id, ProposalState.confirmed, ProposalState.pending)  # nothing written
        raise
    log.info(DECIDED, tool_call_id, ProposalState.confirmed.value)
    try:
        store.set_outcome(ctx.engine, tool_call_id, outcome)
    except (SQLAlchemyError, OSError) as e:  # written, so it stays confirmed; the card says the result is unknown
        log.error("event=proposal_outcome_unsaved tool_call_id=%s error=%s", tool_call_id, type(e).__name__)
    return _view(user_id, ctx.engine, tool_call_id, today)


def cancel(user_id: int, engine: Engine, tool_call_id: int, today: date) -> ProposalView:
    """Drop a pending proposal (one of an earlier day expires instead). A decided one keeps its state."""
    with Session(engine) as s:
        tc = _proposal(user_id, s, tool_call_id)
        pending = tc.proposal_state == ProposalState.pending
        late = cast(date, tc.proposed_on) < today
    if pending:
        to = ProposalState.expired if late else ProposalState.cancelled
        if store.transition(engine, tool_call_id, ProposalState.pending, to):
            log.info(DECIDED, tool_call_id, to.value)
    return _view(user_id, engine, tool_call_id, today)
