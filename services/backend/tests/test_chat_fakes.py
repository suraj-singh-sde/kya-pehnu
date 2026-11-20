"""The chat test helpers themselves (LLD 8.1): the chat-capable FakeTransport, say/ask, fixed_now, marker_world, ..."""

import threading
from datetime import datetime

import pytest
from sqlmodel import Session, select

from kyapehnu.agent.llm.base import ChatResult, LLMUnavailableError, ToolSpec, UserMsg
from kyapehnu.agent.models import ChatSession, ToolCall
from kyapehnu.user.models import UserProfile
from kyapehnu.wardrobe.models import Event, Item, WearLog
from kyapehnu.wardrobe.seed import SEED_ITEMS
from tests.chat_fakes import (
    AUDIENCE_MARKER,
    EVENT_NOTES_MARKER,
    EVENT_TITLE_MARKER,
    HOME_CITY_MARKER,
    ITEM_NOTE_MARKER,
    MARKERS,
    PLACE_MARKER,
    PRICE_MARKER,
    ask,
    fixed_now,
    marker_world,
    say,
    seed_items,
    sent_text,
)
from tests.fakes import FakeTransport, ensure_owner

NOW = datetime(2026, 10, 7, 9, 30)


# --- FakeTransport.call_chat -------------------------------------------------------------------------------------


def test_call_chat_returns_the_scripted_replies_in_order_and_records_each_request():
    fake = FakeTransport(chat_replies=[say("one"), say("two")])
    tools = [ToolSpec("get_item", "One item", {"type": "object"})]

    first = fake.call_chat(system="S1", messages=[UserMsg("hi")], tools=tools)
    second = fake.call_chat(system="S2", messages=[UserMsg("again")], tools=[])

    assert (first.text, second.text) == ("one", "two")
    assert fake.chat_calls == [
        {"system": "S1", "messages": [UserMsg("hi")], "tools": tools},
        {"system": "S2", "messages": [UserMsg("again")], "tools": []},
    ]
    assert fake.chat_replies == []


def test_call_chat_with_nothing_scripted_fails_the_test_instead_of_answering():
    fake = FakeTransport()

    with pytest.raises(AssertionError, match="unexpected chat call"):
        fake.call_chat(system="S", messages=[UserMsg("hi")], tools=[])

    assert len(fake.chat_calls) == 1  # the request is recorded even when it was not expected


def test_call_chat_raises_a_scripted_exception():
    fake = FakeTransport(chat_replies=[LLMUnavailableError("down"), say("fine")])

    with pytest.raises(LLMUnavailableError, match="down"):
        fake.call_chat(system="S", messages=[], tools=[])

    assert fake.call_chat(system="S", messages=[], tools=[]).text == "fine"


def test_call_chat_runs_a_callable_first_and_uses_what_it_returns():
    fake = FakeTransport()
    entered, release = threading.Event(), threading.Event()

    def blocked() -> ChatResult:
        entered.set()
        assert release.wait(timeout=5)
        return say("late")

    fake.chat_replies.append(blocked)
    fake.chat_replies.append(lambda: LLMUnavailableError("slow then down"))
    answers: list[str] = []
    worker = threading.Thread(target=lambda: answers.append(fake.call_chat(system="S", messages=[], tools=[]).text))
    worker.start()

    assert entered.wait(timeout=5)
    assert answers == []  # still blocked inside the call
    release.set()
    worker.join(timeout=5)
    assert answers == ["late"]
    with pytest.raises(LLMUnavailableError, match="slow then down"):
        fake.call_chat(system="S", messages=[], tools=[])


def test_the_fake_still_answers_json_calls_so_phase_2_scripts_are_unchanged():
    fake = FakeTransport(replies=[{"ok": True}], chat_replies=[say("x")])

    assert fake.call_json(system="s", prompt="p", images=[], schema={}, name="n").text == '{"ok": true}'
    assert fake.chat_replies != [] and fake.chat_calls == []


# --- say, ask ------------------------------------------------------------------------------------------------------


def test_say_is_text_only_with_ten_and_five_tokens():
    assert say("hello") == ChatResult("hello", (), 10, 5)


def test_ask_numbers_the_calls_c1_c2_and_carries_the_arguments():
    reply = ask(("search_wardrobe", {"q": "shirt"}), ("get_item", {"item_id": 3}))

    assert [(c.id, c.name, c.arguments) for c in reply.tool_calls] == [
        ("c1", "search_wardrobe", {"q": "shirt"}),
        ("c2", "get_item", {"item_id": 3}),
    ]
    assert (reply.text, reply.input_tokens, reply.output_tokens) == ("", 10, 5)


def test_ask_can_carry_text_with_the_calls():
    assert ask(("get_item", {"item_id": 1}), text="Looking").text == "Looking"


# --- fixed_now, seed_items, sent_text --------------------------------------------------------------------------


def test_fixed_now_makes_the_chat_clock_read_that_moment(monkeypatch):
    from kyapehnu.core import deps

    fixed_now(monkeypatch, NOW)

    assert deps.local_now() == NOW


def test_seed_items_adds_the_32_seed_items(engine, tmp_path):
    assert seed_items(engine, tmp_path) == len(SEED_ITEMS) == 32

    with Session(engine) as s:
        assert sorted(i.id for i in s.exec(select(Item))) == list(range(1, 33))


def test_sent_text_holds_every_chat_request_and_every_json_request():
    fake = FakeTransport(replies=[{"pick": 1}], chat_replies=[say("a")])
    fake.call_chat(system="SYSTEM-TEXT", messages=[UserMsg("USER-TEXT")], tools=[ToolSpec("TOOL-NAME", "d", {})])
    fake.call_json(system="s", prompt="PICK-PROMPT", images=[], schema={}, name="n")

    text = sent_text(fake)

    assert all(part in text for part in ("SYSTEM-TEXT", "USER-TEXT", "TOOL-NAME", "PICK-PROMPT"))


def test_sent_text_of_an_untouched_fake_holds_no_marker():
    assert not any(marker in sent_text(FakeTransport()) for marker in MARKERS)


# --- marker_world (AC-35, AC-50) ---------------------------------------------------------------------------------


@pytest.fixture
def world(engine):
    with Session(engine) as s:
        yield marker_world(s)


def test_marker_world_sets_the_profile_private_values(engine, world):
    with Session(engine) as s:
        profile = s.get(UserProfile, 1)

    assert (profile.home_city, profile.budget_min, profile.budget_max) == (HOME_CITY_MARKER, 1234567, 7654321)


def test_marker_world_items_are_available_and_carry_a_price_and_a_note(engine, world):
    with Session(engine) as s:
        items = [s.get(Item, i) for i in (world.top_id, world.bottom_id, world.shoes_id)]

    assert [i.category for i in items] == ["top", "bottom", "footwear"]
    assert all(i.status == "available" and i.notes == ITEM_NOTE_MARKER for i in items)
    assert all(str(int(i.purchase_price)) == PRICE_MARKER for i in items)


def test_marker_world_events_hold_the_private_text(engine, world):
    with Session(engine) as s:
        form, earlier, elsewhere = (
            s.get(Event, i) for i in (world.form_event_id, world.earlier_event_id, world.place_event_id)
        )

    for event in (form, earlier):
        assert (event.title, event.notes, event.place, event.audience_tags) == (
            EVENT_TITLE_MARKER,
            EVENT_NOTES_MARKER,
            HOME_CITY_MARKER,
            [AUDIENCE_MARKER],
        )
    assert elsewhere.place == PLACE_MARKER


def test_marker_world_session_events_are_only_the_two_made_by_chat_in_the_earlier_session(engine, world):
    with Session(engine) as s:
        made = {(tc.session_id, tc.event_id) for tc in s.exec(select(ToolCall).where(ToolCall.name == "create_event"))}
        assert s.get(ChatSession, world.earlier_session_id) is not None

    assert made == {
        (world.earlier_session_id, world.earlier_event_id),
        (world.earlier_session_id, world.place_event_id),
    }


def test_marker_world_logs_a_wear_for_every_event(engine, world):
    with Session(engine) as s:
        wears = list(s.exec(select(WearLog)))

    assert sorted(w.event_id for w in wears) == sorted(
        (world.form_event_id, world.earlier_event_id, world.place_event_id)
    )
    assert all(w.item_ids == [world.top_id, world.bottom_id, world.shoes_id] for w in wears)


def test_marker_world_on_a_database_that_already_has_a_profile_row_updates_it(engine):
    with Session(engine) as s:
        s.add(UserProfile(id=1, user_id=ensure_owner(s), home_city="Pune"))
        s.commit()
        marker_world(s)
        assert s.get(UserProfile, 1).home_city == HOME_CITY_MARKER
