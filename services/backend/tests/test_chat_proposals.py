"""Proposals: log_wear and update_item_status only propose, and write exactly once on Confirm (AC-48, AC-52; gov
medium 1; LLD 4.4, 4.7).

The proposal rows are built by hand (`tests/chat_rows.py`) where the test is about Confirm and Cancel, and made by a
scripted turn where it is about the tool.
"""

import errno
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, timedelta

import pytest
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, col, select

from kyapehnu.agent.chat import store
from kyapehnu.agent.models import ProposalState, ToolCall
from kyapehnu.wardrobe.models import Event, Feedback, Item, Outfit, WearLog
from tests.chat_fakes import ask, fixed_now, say
from tests.chat_helpers import (
    DINNER,
    NOW,
    add_party,
    engine_of,
    failing,
    party_wardrobe,
    pin_rest_today,
    quiet,
    tool_rows,
    turn,
)
from tests.chat_rows import add_call, add_session, log_wear_ui, proposal, status_ui
from tests.fakes import add_item

TODAY = NOW.date()
YESTERDAY = TODAY - timedelta(days=1)
UNKNOWN = "Result unknown, check Wear history"
CONFIRM = "/api/chat/proposals/{}/confirm"
CANCEL = "/api/chat/proposals/{}/cancel"


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    fixed_now(monkeypatch, NOW)
    pin_rest_today(monkeypatch)


@pytest.fixture
def engine(client):
    return engine_of(client)


@dataclass
class World:
    chat: int
    outfit: list[int]  # a top, a bottom and shoes
    other: list[int]  # another top with the same bottom and shoes
    scarf: int
    event: int


@pytest.fixture
def world(engine) -> World:
    chat = add_session(engine, "A chat")
    with Session(engine, expire_on_commit=False) as s:
        made = party_wardrobe(s, tops=2, bottoms=1, shoes=1)
        scarf = add_item(s, name="Scarf", category="accessory")
        event = add_party(s, audience_tags=["Priya"])
        return World(
            chat,
            [made["top"][0].id, made["bottom"][0].id, made["footwear"][0].id],
            [made["top"][1].id, made["bottom"][0].id, made["footwear"][0].id],
            scarf.id,
            event.id,
        )


def propose_wear(engine, w: World, ids=None, *, on: date = TODAY, event=True) -> int:
    ui = log_wear_ui(w.outfit if ids is None else ids, w.event if event else None)
    return add_call(engine, w.chat, "log_wear", **proposal(ui, on), result='{"status":"awaiting_confirmation"}')


def propose_status(engine, w: World, to: str = "laundry", *, on: date = TODAY, item: int | None = None) -> int:
    ui = status_ui(w.scarf if item is None else item, "available", to)
    return add_call(
        engine, w.chat, "update_item_status", **proposal(ui, on), result='{"status":"awaiting_confirmation"}'
    )


def wears(engine) -> list[WearLog]:
    with Session(engine, expire_on_commit=False) as s:
        return list(s.exec(select(WearLog).order_by(col(WearLog.id))).all())


def status_of(engine, item_id: int) -> str:
    with Session(engine, expire_on_commit=False) as s:
        item = s.get(Item, item_id)
        return str(item.status) if item else "deleted"


def stored(engine, tool_call_id: int) -> ToolCall:
    with Session(engine, expire_on_commit=False) as s:
        return s.get(ToolCall, tool_call_id)


def card(client, w: World, tool_call_id: int) -> dict:
    """The proposal as the page shows it when the chat is opened."""
    calls = client.get(f"/api/chat/sessions/{w.chat}").json()["tool_calls"]
    return next(c["proposal"] for c in calls if c["id"] == tool_call_id)


def confirm(client, tool_call_id: int):
    return client.post(CONFIRM.format(tool_call_id))


# --- AC-48: a proposal writes nothing until Confirm, then exactly what REST writes ----------------------------------


def test_wear_and_status_only_propose_then_confirm_writes_as_rest_and_cancel_writes_nothing(
    client, fake, engine, world
):
    out = turn(
        client,
        fake,
        world.chat,
        ask(
            ("log_wear", {"item_ids": world.outfit, "event_id": world.event}),
            ("update_item_status", {"item_id": world.scarf, "status": "laundry"}),
            ("log_wear", {"item_ids": world.other}),
        ),
        say("Waiting on you."),
    )

    assert wears(engine) == [] and status_of(engine, world.scarf) == "available"  # proposed, nothing written
    wear_card, status_card, cancel_card = (c["proposal"] for c in out["session"]["tool_calls"])
    assert [c["state"] for c in (wear_card, status_card, cancel_card)] == ["pending"] * 3
    assert all(c["actionable"] and c["decided_at"] is None and c["result"] is None for c in (wear_card, status_card))
    assert (wear_card["kind"], wear_card["item_ids"], wear_card["event"]["id"]) == (
        "log_wear",
        world.outfit,
        world.event,
    )
    assert (status_card["kind"], status_card["status_from"], status_card["status_to"]) == (
        "update_item_status",
        "available",
        "laundry",
    )

    worn = confirm(client, wear_card["tool_call_id"])
    changed = confirm(client, status_card["tool_call_id"])
    cancelled = client.post(CANCEL.format(cancel_card["tool_call_id"]))

    assert (worn.status_code, worn.json()["state"], worn.json()["result"]) == (
        200,
        "confirmed",
        "Logged as worn on 2026-10-07",
    )
    assert worn.json()["decided_at"] is not None and worn.json()["actionable"] is False
    assert (changed.status_code, changed.json()["state"], changed.json()["result"]) == (
        200,
        "confirmed",
        "Marked as laundry",
    )
    assert (cancelled.status_code, cancelled.json()["state"], cancelled.json()["result"]) == (200, "cancelled", None)
    (log,) = wears(engine)  # exactly what the Outfits page's wear writes: the set, the day, the event and its audience
    assert (log.item_ids, log.worn_on, log.event_id, log.audience_tags) == (world.outfit, TODAY, world.event, ["Priya"])
    assert log.outfit_id is not None and status_of(engine, world.scarf) == "laundry"
    reopened = [card(client, world, c["tool_call_id"])["state"] for c in (wear_card, status_card, cancel_card)]
    assert reopened == ["confirmed", "confirmed", "cancelled"]
    assert stored(engine, wear_card["tool_call_id"]).outcome == {"outfit_id": log.outfit_id, "worn_on": "2026-10-07"}
    assert stored(engine, status_card["tool_call_id"]).outcome == {"item_id": world.scarf, "status": "laundry"}


def test_create_event_and_feedback_write_at_once(client, fake, engine, world):
    turn(
        client,
        fake,
        world.chat,
        ask(("create_event", DINNER), ("record_feedback", {"item_ids": world.outfit, "rating": 1, "reason": "lovely"})),
        say("Saved."),
    )

    with Session(engine, expire_on_commit=False) as s:
        events = s.exec(select(Event).where(Event.title == "Dinner with Priya")).all()
        (rated,) = s.exec(select(Feedback)).all()
        assert len(events) == 1 and (rated.rating, rated.reason) == (1, "lovely")
        assert s.get(Outfit, rated.outfit_id).item_ids == sorted(world.outfit)
    created, feedback = tool_rows(engine, world.chat)
    assert (created.event_id, created.proposal_state, feedback.proposal_state) == (events[0].id, None, None)
    assert wears(engine) == []


# --- AC-52: exactly once --------------------------------------------------------------------------------------------


def test_a_second_confirm_returns_the_stored_state_and_writes_nothing(client, engine, world):
    tool_call = propose_wear(engine, world)

    first, second = confirm(client, tool_call), confirm(client, tool_call)

    assert (first.status_code, second.status_code) == (200, 200)
    assert second.json() == first.json() and second.json()["state"] == "confirmed"
    assert len(wears(engine)) == 1


def test_two_confirms_at_the_same_moment_write_one_wearlog(client, engine, world):
    tool_call = propose_wear(engine, world)
    gate = threading.Barrier(2, timeout=10)

    def press():
        gate.wait()
        return confirm(client, tool_call)

    with ThreadPoolExecutor(max_workers=2) as pool:
        both = [f.result(timeout=30) for f in [pool.submit(press), pool.submit(press)]]

    assert [r.status_code for r in both] == [200, 200]
    assert {r.json()["state"] for r in both} == {"confirmed"}
    assert len(wears(engine)) == 1 and card(client, world, tool_call)["state"] == "confirmed"


def test_yesterdays_proposal_is_expired_on_reopen_and_cannot_be_confirmed(client, engine, world):
    tool_call = propose_wear(engine, world, on=YESTERDAY)

    shown = card(client, world, tool_call)
    kept = stored(engine, tool_call).proposal_state
    r = confirm(client, tool_call)

    assert (shown["state"], shown["proposed_on"], shown["actionable"]) == ("expired", "2026-10-06", False)
    assert kept == "pending"  # the expiry is derived when the chat is read, and stored by the next Confirm or Cancel
    assert (r.status_code, r.json()["code"]) == (409, "proposal_expired")
    assert r.json()["detail"].startswith("Not done: ") and r.json()["detail"].endswith(". Nothing was changed.")
    assert wears(engine) == [] and stored(engine, tool_call).proposal_state == "expired"  # persisted by the Confirm
    again = confirm(client, tool_call)
    assert (again.status_code, again.json()["state"]) == (200, "expired")


def test_cancelling_yesterdays_proposal_expires_it_rather_than_cancelling_it(client, engine, world):
    tool_call = propose_wear(engine, world, on=YESTERDAY)

    r = client.post(CANCEL.format(tool_call))

    assert (r.status_code, r.json()["state"]) == (200, "expired")
    assert stored(engine, tool_call).proposal_state == "expired"


def test_a_proposal_confirmed_after_midnight_is_expired(client, engine, world, monkeypatch):
    tool_call = propose_wear(engine, world)
    fixed_now(monkeypatch, NOW + timedelta(days=1))

    r = confirm(client, tool_call)

    assert (r.status_code, r.json()["code"]) == (409, "proposal_expired") and wears(engine) == []


def test_a_stale_proposal_another_tab_decided_first_returns_that_decision_not_a_409(client, engine, world, monkeypatch):
    tool_call = propose_wear(engine, world, on=YESTERDAY)
    real = store.transition

    def other_tab_cancels_first(eng, call_id, frm, to, **kw):
        if (frm, to) == (ProposalState.pending, ProposalState.expired):
            real(eng, call_id, ProposalState.pending, ProposalState.cancelled)  # the other request wins the race
        return real(eng, call_id, frm, to, **kw)

    monkeypatch.setattr(store, "transition", other_tab_cancels_first)

    r = confirm(client, tool_call)

    assert (r.status_code, r.json()["state"]) == (200, "cancelled")
    assert wears(engine) == [] and stored(engine, tool_call).proposal_state == "cancelled"


def delete_item(client, item_id: int) -> None:
    assert client.delete(f"/api/items/{item_id}", params={"hard": "true"}).status_code == 204


def set_status(client, item_id: int, status: str) -> None:
    assert client.post(f"/api/items/{item_id}/status", json={"status": status}).status_code == 200


STALE = {
    "wear item in laundry": ("wear", lambda c, w: set_status(c, w.outfit[0], "laundry")),
    "wear item retired": ("wear", lambda c, w: set_status(c, w.outfit[1], "retired")),
    "wear item deleted": ("wear", lambda c, w: delete_item(c, w.outfit[2])),
    "status item deleted": ("status", lambda c, w: delete_item(c, w.scarf)),
    "status changed": ("status", lambda c, w: set_status(c, w.scarf, "retired")),
}


@pytest.mark.parametrize("case", list(STALE))
def test_stale_items_expire_on_confirm(client, engine, world, case):
    kind, change = STALE[case]
    tool_call = propose_wear(engine, world) if kind == "wear" else propose_status(engine, world)
    before_wears = len(wears(engine))
    change(client, world)
    seen = {i: status_of(engine, i) for i in (*world.outfit, world.scarf)}

    shown = card(client, world, tool_call)
    r = confirm(client, tool_call)

    assert (shown["state"], shown["actionable"]) == ("pending", False)  # the page already disables Confirm
    assert (r.status_code, r.json()["code"]) == (409, "proposal_expired")
    assert r.json()["detail"].startswith("Not done: ") and r.json()["detail"].endswith(". Nothing was changed.")
    assert len(wears(engine)) == before_wears
    assert {i: status_of(engine, i) for i in seen} == seen  # nothing was changed by the Confirm
    assert stored(engine, tool_call).proposal_state == "expired"
    assert card(client, world, tool_call)["state"] == "expired"


# --- Cancel and what is not a proposal ------------------------------------------------------------------------------


def test_cancel_writes_nothing_and_a_later_confirm_or_cancel_returns_the_cancelled_state(client, engine, world):
    tool_call = propose_wear(engine, world)

    cancelled = client.post(CANCEL.format(tool_call))
    confirmed_after = confirm(client, tool_call)
    cancelled_again = client.post(CANCEL.format(tool_call))

    assert (cancelled.status_code, cancelled.json()["state"]) == (200, "cancelled")
    assert cancelled.json()["decided_at"] is not None and cancelled.json()["actionable"] is False
    assert (confirmed_after.status_code, confirmed_after.json()) == (200, cancelled.json())
    assert (cancelled_again.status_code, cancelled_again.json()) == (200, cancelled.json())
    assert wears(engine) == []


def test_cancel_after_confirm_keeps_the_confirmed_state(client, engine, world):
    tool_call = propose_status(engine, world)
    confirmed = confirm(client, tool_call).json()

    r = client.post(CANCEL.format(tool_call))

    assert (r.status_code, r.json()) == (200, confirmed) and status_of(engine, world.scarf) == "laundry"


@pytest.mark.parametrize("route", [CONFIRM, CANCEL])
def test_something_that_is_not_a_proposal_is_404_not_found(client, engine, world, route):
    plain = add_call(engine, world.chat, "search_wardrobe")

    unknown = client.post(route.format(9999))
    not_a_proposal = client.post(route.format(plain))

    assert [(r.status_code, r.json()["code"]) for r in (unknown, not_a_proposal)] == [(404, "not_found")] * 2
    assert wears(engine) == []


# --- gov medium 1: put back to pending only when the write did not commit -------------------------------------------


def busy():
    return OperationalError("WRITE", {}, sqlite3.OperationalError("database is locked"))


WRITE_FAILS = {
    "disk full before the commit": (
        r"INSERT INTO outfit\b",
        lambda: OSError(errno.ENOSPC, "No space left on device"),
        507,
        "insufficient_storage",
    ),
    "database busy before the commit": (r"INSERT INTO outfit\b", busy, 503, "db_busy"),
}


@pytest.mark.parametrize("case", list(WRITE_FAILS))
def test_failed_write_compensates_back_to_pending(client, engine, world, case):
    when, error, status, code = WRITE_FAILS[case]
    tool_call = propose_wear(engine, world)

    with failing(engine, when, error):
        failed = confirm(client, tool_call)

    assert (failed.status_code, failed.json()["code"]) == (status, code)  # the service's own error
    assert wears(engine) == []
    shown = card(client, world, tool_call)
    assert (shown["state"], shown["actionable"], shown["decided_at"]) == ("pending", True, None)
    retried = confirm(client, tool_call)  # still there to confirm, and it writes once
    assert (retried.status_code, retried.json()["state"]) == (200, "confirmed") and len(wears(engine)) == 1


def test_a_status_write_that_fails_before_its_commit_goes_back_to_pending(client, engine, world):
    tool_call = propose_status(engine, world)

    with failing(engine, r"UPDATE item\b", busy):
        failed = confirm(client, tool_call)

    assert (failed.status_code, failed.json()["code"]) == (503, "db_busy")
    assert status_of(engine, world.scarf) == "available"
    assert card(client, world, tool_call)["state"] == "pending" and card(client, world, tool_call)["actionable"]
    assert confirm(client, tool_call).json()["state"] == "confirmed" and status_of(engine, world.scarf) == "laundry"


def test_a_wear_that_committed_but_could_not_be_presented_stays_confirmed_and_is_never_written_twice(
    client, engine, world, monkeypatch
):
    tool_call = propose_wear(engine, world)

    def broken(*args, **kwargs):
        raise RuntimeError("cannot present")

    with monkeypatch.context() as patched:
        patched.setattr("kyapehnu.wardrobe.outfits.wear._present", broken)
        quiet(client).post(CONFIRM.format(tool_call))  # whatever it answers, the wear is in the database

    again = confirm(client, tool_call)

    assert len(wears(engine)) == 1
    assert (again.status_code, again.json()["state"]) == (200, "confirmed")
    assert again.json()["result"] in {UNKNOWN, "Logged as worn on 2026-10-07"}
    assert stored(engine, tool_call).proposal_state == "confirmed"


@pytest.mark.parametrize("kind", ["wear", "status"])
def test_a_write_that_committed_but_whose_outcome_could_not_be_saved_stays_confirmed_with_result_unknown(
    client, engine, world, kind
):
    tool_call = propose_wear(engine, world) if kind == "wear" else propose_status(engine, world)

    with failing(engine, r"UPDATE toolcall SET outcome", busy, once=False):
        quiet(client).post(CONFIRM.format(tool_call))  # the answer is not pinned: the stored state is

    shown = card(client, world, tool_call)
    assert (shown["state"], shown["actionable"], shown["result"]) == ("confirmed", False, UNKNOWN)
    assert len(wears(engine)) == (1 if kind == "wear" else 0)
    assert status_of(engine, world.scarf) == ("available" if kind == "wear" else "laundry")
    again = confirm(client, tool_call)  # a second Confirm writes nothing more
    assert (again.status_code, again.json()["state"], again.json()["result"]) == (200, "confirmed", UNKNOWN)
    assert len(wears(engine)) == (1 if kind == "wear" else 0)
    assert not stored(engine, tool_call).outcome


def test_the_proposal_made_by_a_turn_is_the_one_that_confirms(client, fake, engine, world):
    out = turn(client, fake, world.chat, ask(("log_wear", {"item_ids": world.outfit})), say("Confirm on screen."))
    (tool_call,) = (c["id"] for c in out["session"]["tool_calls"])

    r = confirm(client, tool_call)

    assert (r.status_code, r.json()["state"], r.json()["tool_call_id"]) == (200, "confirmed", tool_call)
    (log,) = wears(engine)
    assert (log.event_id, log.audience_tags) == (None, [])  # no event was named
