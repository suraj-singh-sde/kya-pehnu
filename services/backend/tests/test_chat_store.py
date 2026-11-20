"""The chat store (LLD 3.3, 4.4, 5.4): turns, the session view, proposal state, the delete (AC-16, 18, 19, 53)."""

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import event, text
from sqlmodel import Session, select

from kyapehnu.agent.chat import store
from kyapehnu.agent.models import ChatMessage, ChatRole, ChatSession, ProposalState, ToolCall
from kyapehnu.core.errors import NotFoundError
from tests.chat_rows import (
    add_call,
    add_llm_call,
    add_message,
    add_session,
    log_wear_ui,
    outfits_ui,
    proposal,
    status_ui,
    suggestion,
    with_owner,
)
from tests.fakes import add_item
from tests.wear_fixtures import add_event

USER = 1
TODAY = date(2026, 10, 7)
YESTERDAY = date(2026, 10, 6)
TOMORROW = date(2026, 10, 8)
pending, confirmed, cancelled, expired = (
    ProposalState.pending,
    ProposalState.confirmed,
    ProposalState.cancelled,
    ProposalState.expired,
)


@pytest.fixture
def engine(engine):
    return with_owner(engine)


def tool_call(engine, tool_call_id: int) -> ToolCall:
    with Session(engine) as s:
        row = s.get(ToolCall, tool_call_id)
        s.expunge(row)
        return row


def stored_state(engine, tool_call_id: int) -> tuple[str, datetime | None, dict | None]:
    row = tool_call(engine, tool_call_id)
    return row.proposal_state, row.decided_at, row.outcome


# --- sessions, turns and rows -----


def test_create_session_is_empty_and_ids_only_grow(engine):
    first = store.create_session(USER, engine)
    second = store.create_session(USER, engine)

    assert (first.title, second.title) == ("", "")
    assert second.id == first.id + 1
    assert first.created_at <= first.updated_at


def test_start_turn_numbers_turns_stores_the_user_message_at_step_0_and_titles_the_chat_once(engine):
    chat = add_session(engine)
    other = add_session(engine)
    add_message(engine, other, 5, 0, "user", "another chat is on turn 5")

    first = store.start_turn(USER, engine, chat, "What to wear\nto a wedding?")  # as MessageIn leaves it: cleaned
    second = store.start_turn(USER, engine, chat, "And shoes?")

    assert (first, second) == (1, 2)
    with Session(engine) as s:
        assert s.get(ChatSession, chat).title == "What to wear to a wedding?"
        rows = s.exec(select(ChatMessage).where(ChatMessage.session_id == chat).order_by(ChatMessage.id)).all()
    assert [(m.turn, m.step, m.role, m.content) for m in rows] == [
        (1, 0, "user", "What to wear\nto a wedding?"),
        (2, 0, "user", "And shoes?"),
    ]


def test_the_title_is_the_cleaned_first_60_characters(engine):
    chat = add_session(engine)

    store.start_turn(USER, engine, chat, "x" * 100)

    with Session(engine) as s:
        assert s.get(ChatSession, chat).title == "x" * 60


def test_start_turn_on_an_unknown_session_is_not_found_and_stores_nothing(engine):
    with pytest.raises(NotFoundError):
        store.start_turn(USER, engine, 99, "hello")

    with Session(engine) as s:
        assert s.exec(select(ChatMessage)).all() == []


def test_add_message_and_add_tool_call_store_rows_and_the_tool_call_id_is_returned(engine):
    chat = add_session(engine)
    store.add_message(USER, engine, chat, 1, 2, ChatRole.assistant, "Here you go")
    row = ToolCall(
        user_id=1, session_id=chat, turn=1, step=1, call_id="c1", name="search_wardrobe", result="{}", latency_ms=3
    )

    tool_call_id = store.add_tool_call(engine, row)

    with Session(engine) as s:
        message = s.exec(select(ChatMessage)).one()
        stored = s.get(ToolCall, tool_call_id)
    assert (message.session_id, message.turn, message.step, message.role, message.content) == (
        chat,
        1,
        2,
        "assistant",
        "Here you go",
    )
    assert (stored.name, stored.call_id, stored.session_id) == ("search_wardrobe", "c1", chat)


def test_finish_turn_moves_the_chat_to_the_top_of_the_list(engine):
    older = add_session(engine, updated_at=datetime(2026, 10, 1, tzinfo=UTC))
    newer = add_session(engine, updated_at=datetime(2026, 10, 2, tzinfo=UTC))

    store.finish_turn(engine, older)

    with Session(engine) as s:
        assert [c.id for c in store.list_sessions(USER, s)] == [older, newer]


def test_session_event_ids_are_the_events_this_chat_created(engine):
    chat, other = add_session(engine), add_session(engine)
    with Session(engine) as s:
        mine, theirs, form = (add_event(s).id for _ in range(3))
    add_call(engine, chat, "create_event", event_id=mine)
    add_call(engine, chat, "create_event", error="invalid_arguments")  # no event came of it
    add_call(engine, chat, "generate_outfits", event_id=form)  # made the event inline: it is this chat's too
    add_call(engine, chat, "generate_outfits")  # read an event made elsewhere: the tool stores no event id
    add_call(engine, other, "create_event", event_id=theirs)

    with Session(engine) as s:
        assert store.session_event_ids(USER, s, chat) == {mine, form}
        assert store.session_event_ids(USER, s, other) == {theirs}
        assert store.session_event_ids(USER, s, 999) == set()


# --- session_view: batched reads -----


def selects_run_by(engine, work) -> int:
    statements: list[str] = []

    def record(_conn, _cursor, statement, _params, _context, _many) -> None:
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        work()
    finally:
        event.remove(engine, "before_cursor_execute", record)
    return len(statements)


def test_opening_a_session_reads_in_batches_whatever_the_number_of_cards_and_items(engine):
    with Session(engine) as s:
        ids = [add_item(s, name=f"Item {n}").id for n in range(12)]
        event_ids = [add_event(s).id for _ in range(3)]
    small, large = add_session(engine), add_session(engine)
    add_call(engine, small, "generate_outfits", ui=outfits_ui([suggestion(ids[:2])], event_ids[0]))
    for n in range(3):
        sets = [suggestion(ids[n : n + 3], swap_item_id=ids[n + 5]) for n in range(3)]
        add_call(engine, large, "generate_outfits", step=n + 1, ui=outfits_ui(sets, event_ids[n]))
    add_call(engine, large, "log_wear", step=9, **proposal(log_wear_ui(ids[:4], event_ids[0]), TODAY))

    def open_session(session_id: int):
        with Session(engine) as s:
            store.session_view(USER, s, session_id, TODAY)

    assert selects_run_by(engine, lambda: open_session(large)) == selects_run_by(engine, lambda: open_session(small))


def test_session_view_orders_messages_and_calls_by_turn_step_and_id(engine):
    chat = add_session(engine)
    add_message(engine, chat, 2, 0, "user", "second")
    add_message(engine, chat, 1, 2, "assistant", "answer")
    add_message(engine, chat, 1, 0, "user", "first")
    add_call(engine, chat, "get_item", turn=2, step=1)
    add_call(engine, chat, "get_item", turn=1, step=1, call_id="a")
    add_call(engine, chat, "get_item", turn=1, step=1, call_id="b")

    with Session(engine) as s:
        view = store.session_view(USER, s, chat, TODAY)

    assert [(m.turn, m.step) for m in view.messages] == [(1, 0), (1, 2), (2, 0)]
    assert [(c.turn, c.step) for c in view.tool_calls] == [(1, 1), (1, 1), (2, 1)]
    assert view.session.id == chat


def test_session_view_of_an_unknown_session_is_not_found(engine):
    with Session(engine) as s, pytest.raises(NotFoundError):
        store.session_view(USER, s, 99, TODAY)


# --- proposal_view -----


def view_of(engine, tool_call_id: int, today: date = TODAY):
    with Session(engine) as s:
        return store.proposal_view(USER, s, s.get(ToolCall, tool_call_id), today)


@pytest.fixture
def wardrobe(engine):
    with Session(engine) as s:
        top = add_item(s, name="Shirt")
        laundry = add_item(s, name="Hoodie", status="laundry")
        event_id = add_event(s).id
        ids = (top.id, laundry.id)
    return {"top": ids[0], "laundry": ids[1], "event": event_id, "chat": add_session(engine)}


def log_wear_call(engine, w, item_ids, state="pending", on=TODAY, **fields) -> int:
    return add_call(engine, w["chat"], "log_wear", **proposal(log_wear_ui(item_ids, w["event"]), on, state), **fields)


def test_a_pending_log_wear_of_today_with_available_items_is_actionable(engine, wardrobe):
    view = view_of(engine, log_wear_call(engine, wardrobe, [wardrobe["top"]]))

    assert (view.kind, view.state, view.actionable, view.result) == ("log_wear", "pending", True, None)
    assert (view.item_ids, [i.name for i in view.items], view.proposed_on) == ([wardrobe["top"]], ["Shirt"], TODAY)
    assert (view.event.id, view.status_from, view.status_to, view.decided_at) == (wardrobe["event"], None, None, None)


def test_a_pending_proposal_of_an_earlier_day_shows_as_expired_and_is_stored_as_pending(engine, wardrobe):
    tool_call_id = log_wear_call(engine, wardrobe, [wardrobe["top"]], on=YESTERDAY)

    view = view_of(engine, tool_call_id)

    assert (view.state, view.actionable) == ("expired", False)
    assert stored_state(engine, tool_call_id)[0] == "pending"  # the next Confirm or Cancel persists it


def test_a_pending_proposal_of_a_later_day_is_not_expired(engine, wardrobe):
    view = view_of(engine, log_wear_call(engine, wardrobe, [wardrobe["top"]], on=TOMORROW))

    assert (view.state, view.actionable) == ("pending", True)


@pytest.mark.parametrize(
    ("what", "item_key"),
    [("laundry", "laundry"), ("deleted", None)],
)
def test_a_log_wear_with_an_unavailable_or_deleted_item_is_not_actionable(engine, wardrobe, what, item_key):
    gone = 9999
    ids = [wardrobe["top"], wardrobe[item_key] if item_key else gone]
    view = view_of(engine, log_wear_call(engine, wardrobe, ids))

    assert (view.state, view.actionable) == ("pending", False)
    assert [i is None for i in view.items] == [False, what == "deleted"]
    assert view.item_ids == ids


def test_a_status_proposal_is_actionable_until_the_status_changes(engine, wardrobe):
    chat = wardrobe["chat"]
    ui = status_ui(wardrobe["top"], "available", "laundry")
    unchanged = add_call(engine, chat, "update_item_status", **proposal(ui, TODAY))
    changed_ui = status_ui(wardrobe["laundry"], "available", "retired")  # it is in laundry now, not available
    changed = add_call(engine, chat, "update_item_status", step=2, **proposal(changed_ui, TODAY))

    ok, stale = view_of(engine, unchanged), view_of(engine, changed)

    assert (ok.kind, ok.status_from, ok.status_to, ok.actionable) == (
        "update_item_status",
        "available",
        "laundry",
        True,
    )
    assert [i.name for i in ok.items] == ["Shirt"]
    assert (stale.state, stale.actionable) == ("pending", False)


@pytest.mark.parametrize(
    ("kind", "outcome", "result"),
    [
        ("log_wear", {"outfit_id": 4, "worn_on": "2026-10-07"}, "Logged as worn on 2026-10-07"),
        ("update_item_status", {"item_id": 1, "status": "laundry"}, "Marked as laundry"),
        ("log_wear", None, "Result unknown, check Wear history"),
        ("update_item_status", None, "Result unknown, check Wear history"),
    ],
)
def test_a_confirmed_proposal_tells_what_was_done_or_that_the_result_is_unknown(
    engine, wardrobe, kind, outcome, result
):
    ui = log_wear_ui([wardrobe["top"]]) if kind == "log_wear" else status_ui(wardrobe["top"], "available", "laundry")
    decided = datetime(2026, 10, 7, 9, 31, tzinfo=UTC)
    tool_call_id = add_call(
        engine, wardrobe["chat"], kind, **proposal(ui, TODAY, "confirmed", outcome=outcome, decided_at=decided)
    )

    view = view_of(engine, tool_call_id)

    assert (view.state, view.actionable, view.result) == ("confirmed", False, result)
    assert view.decided_at == decided


@pytest.mark.parametrize("state", ["cancelled", "expired"])
def test_a_cancelled_or_expired_proposal_has_no_result_and_is_not_actionable(engine, wardrobe, state):
    view = view_of(engine, log_wear_call(engine, wardrobe, [wardrobe["top"]], state))

    assert (view.state, view.actionable, view.result) == (state, False, None)


# --- transition and set_outcome -----


def test_the_second_claim_of_a_pending_proposal_loses(engine, wardrobe):
    tool_call_id = log_wear_call(engine, wardrobe, [wardrobe["top"]])

    first = store.transition(engine, tool_call_id, pending, confirmed)
    second = store.transition(engine, tool_call_id, pending, confirmed)

    assert (first, second) == (True, False)
    state, decided_at, _ = stored_state(engine, tool_call_id)
    assert (state, decided_at is not None) == ("confirmed", True)


def test_two_threads_claiming_one_proposal_have_exactly_one_winner(engine, wardrobe):
    tool_call_id = log_wear_call(engine, wardrobe, [wardrobe["top"]])
    gate = threading.Barrier(2, timeout=10)

    def claim() -> bool:
        gate.wait()
        return store.transition(engine, tool_call_id, pending, confirmed)

    with ThreadPoolExecutor(2) as pool:
        results = sorted(pool.map(lambda _: claim(), range(2)))

    assert results == [False, True]


def test_a_transition_from_a_state_the_proposal_is_not_in_changes_nothing(engine, wardrobe):
    tool_call_id = log_wear_call(engine, wardrobe, [wardrobe["top"]], "cancelled")

    assert store.transition(engine, tool_call_id, pending, expired) is False
    assert stored_state(engine, tool_call_id)[0] == "cancelled"


def test_an_unknown_proposal_cannot_be_claimed(engine):
    assert store.transition(engine, 999, pending, confirmed) is False


def test_a_cancel_and_an_expiry_record_when_they_were_decided(engine, wardrobe):
    one = log_wear_call(engine, wardrobe, [wardrobe["top"]])
    two = log_wear_call(engine, wardrobe, [wardrobe["top"]])

    assert store.transition(engine, one, pending, cancelled) and store.transition(engine, two, pending, expired)

    (state_one, decided_one, _), (state_two, decided_two, _) = stored_state(engine, one), stored_state(engine, two)
    assert (state_one, state_two) == ("cancelled", "expired")
    assert decided_one is not None and decided_two is not None


@pytest.mark.parametrize("stored_as", ["json_null", "sql_null"])
def test_the_compensation_puts_a_claim_without_an_outcome_back_to_pending(engine, wardrobe, stored_as):
    # the ORM writes an unset JSON column as the text 'null', a hand-made row may hold SQL NULL: both are "no outcome"
    tool_call_id = log_wear_call(engine, wardrobe, [wardrobe["top"]])
    if stored_as == "sql_null":
        with engine.begin() as conn:
            conn.execute(text("UPDATE toolcall SET outcome = NULL WHERE id = :id"), {"id": tool_call_id})
    store.transition(engine, tool_call_id, pending, confirmed)

    assert store.transition(engine, tool_call_id, confirmed, pending) is True

    assert stored_state(engine, tool_call_id) == ("pending", None, None)


def test_the_compensation_never_undoes_a_proposal_whose_outcome_is_recorded(engine, wardrobe):
    tool_call_id = log_wear_call(engine, wardrobe, [wardrobe["top"]])
    store.transition(engine, tool_call_id, pending, confirmed)
    store.set_outcome(engine, tool_call_id, {"outfit_id": 3, "worn_on": "2026-10-07"})

    assert store.transition(engine, tool_call_id, confirmed, pending) is False

    state, decided_at, outcome = stored_state(engine, tool_call_id)
    assert (state, decided_at is not None, outcome) == ("confirmed", True, {"outfit_id": 3, "worn_on": "2026-10-07"})


def test_a_transition_can_record_the_outcome_with_the_state(engine, wardrobe):
    tool_call_id = log_wear_call(engine, wardrobe, [wardrobe["top"]])

    assert store.transition(engine, tool_call_id, pending, confirmed, outcome={"item_id": 1, "status": "laundry"})

    assert stored_state(engine, tool_call_id)[2] == {"item_id": 1, "status": "laundry"}


# --- delete_session -----


def seed_chat(engine) -> int:
    chat = add_session(engine)
    add_message(engine, chat, 1, 0, "user", "hello")
    add_call(engine, chat, "get_item")
    add_llm_call(engine, "outfit_pick", f"session={chat} images=[] bytes\nx")
    return chat


def counts(engine) -> list[int]:
    with engine.connect() as conn:
        return [
            conn.execute(text(f"SELECT count(*) FROM {t}")).scalar_one()
            for t in ("chatsession", "chatmessage", "toolcall", "llmcall")
        ]


def test_delete_session_of_an_unknown_id_is_not_found_and_changes_nothing(engine):
    seed_chat(engine)

    with pytest.raises(NotFoundError):
        store.delete_session(USER, engine, 99)

    assert counts(engine) == [1, 1, 1, 1]


def test_delete_session_removes_the_chat_rows_and_the_pick_rows_of_its_prefix_only(engine):
    chat = seed_chat(engine)
    add_llm_call(engine, "outfit_pick", f"session={chat}0 images=[] bytes\nlooks like it")  # another chat's prefix
    add_llm_call(engine, "chat", f"session={chat} messages=1 tools=9")

    store.delete_session(USER, engine, chat)

    assert counts(engine) == [0, 0, 0, 2]


class DiskGone(Exception):
    pass


def test_delete_session_is_one_transaction(engine):
    chat = seed_chat(engine)
    deletes: list[str] = []

    def refuse_the_last(_conn, _cursor, statement, _params, _context, _many) -> None:
        if statement.startswith("DELETE"):
            deletes.append(statement.split()[2])
        if statement.startswith("DELETE FROM chatsession"):
            raise DiskGone

    event.listen(engine, "before_cursor_execute", refuse_the_last)
    try:
        with pytest.raises(DiskGone):
            store.delete_session(USER, engine, chat)
    finally:
        event.remove(engine, "before_cursor_execute", refuse_the_last)

    assert set(deletes) == {"toolcall", "chatmessage", "llmcall", "chatsession"}  # all ran, then all came back
    assert counts(engine) == [1, 1, 1, 1]
