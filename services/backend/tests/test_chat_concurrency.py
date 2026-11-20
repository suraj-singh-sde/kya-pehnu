"""A turn does not lock the app (AC-39; LLD 4.2, H6): a second message to the chat and a delete get 409 while it runs,
everything else keeps working, and the guard is released whatever happens to the turn.
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest
from sqlmodel import Session

from kyapehnu.agent.chat.limits import TURN_LOCK_TTL_S
from kyapehnu.agent.turns import TurnLock
from kyapehnu.core.errors import ServiceUnavailableError, TurnRunningError
from tests.chat_fakes import ask, fixed_now, say
from tests.chat_helpers import (
    MESSAGES,
    NOW,
    engine_of,
    failing,
    llm_rows,
    message_rows,
    new_chat,
    pin_rest_today,
    quiet,
    tool_rows,
    turn,
)
from tests.fakes import add_item

TURN_RUNNING = "A reply is still being worked on in this chat. Wait for it, then try again."
WAIT = 20


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    fixed_now(monkeypatch, NOW)
    pin_rest_today(monkeypatch)


@pytest.fixture
def engine(client):
    return engine_of(client)


def wait_inside(slow: "SlowModel", turn_future) -> None:
    """Wait (a bounded poll) until the turn is inside the provider call; fail fast when the turn ended instead."""
    deadline = time.monotonic() + WAIT
    while not slow.entered.wait(timeout=0.05):
        assert not turn_future.done(), "the turn ended before it reached the provider"
        assert time.monotonic() < deadline, "the turn never reached the provider"


class SlowModel:
    """A first reply that waits inside the provider call until the test lets it go."""

    def __init__(self, reply) -> None:
        self.entered, self.release, self.reply = threading.Event(), threading.Event(), reply

    def __call__(self):
        self.entered.set()
        assert self.release.wait(timeout=WAIT)
        return self.reply


@contextmanager
def turn_blocked_in_the_provider(client, chat: int, slow: SlowModel):
    """Run a message in a thread and yield once its turn waits inside the provider call; always let it go at the end."""
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(client.post, MESSAGES.format(chat), json={"text": "one"})
        try:
            wait_inside(slow, first)
            yield first
        finally:
            slow.release.set()


def test_blocked_turn_holds_no_lock_second_message_409_rows_kept(client, fake, engine):
    with Session(engine, expire_on_commit=False) as s:
        shirt, scarf = add_item(s, name="Shirt"), add_item(s, name="Scarf", category="accessory")
    chat, other = new_chat(client), new_chat(client)
    slow = SlowModel(ask(("search_wardrobe", {})))
    fake.chat_replies.extend([slow, say("All done.")])

    with turn_blocked_in_the_provider(client, chat, slow) as first:
        wear = client.post("/api/outfits/wear", json={"item_ids": [shirt.id]})
        status = client.post(f"/api/items/{scarf.id}/status", json={"status": "laundry"})
        second = client.post(MESSAGES.format(chat), json={"text": "two"})
        read = client.get(f"/api/chat/sessions/{chat}")
        blocked_rows = [(m.role, m.content) for m in message_rows(engine, chat)]
        slow.release.set()
        finished = first.result(timeout=WAIT)

    assert (wear.status_code, status.status_code) == (201, 200)  # both succeed while the turn waits
    assert (second.status_code, second.json()["code"], second.json()["detail"]) == (409, "turn_running", TURN_RUNNING)
    assert read.status_code == 200 and blocked_rows == [("user", "one")]  # the refused message was not stored
    assert finished.status_code == 200 and finished.json()["outcome"] == "ok"
    assert [(m.role, m.content) for m in message_rows(engine, chat)] == [("user", "one"), ("assistant", "All done.")]
    assert [r.name for r in tool_rows(engine, chat)] == ["search_wardrobe"]  # its rows are all written
    assert len(llm_rows(engine, "chat")) == 2
    assert message_rows(engine, other) == []  # the other chat never had a turn


def test_a_chat_that_is_not_running_takes_a_message_while_another_runs(client, fake):
    running, free = new_chat(client), new_chat(client)
    slow = SlowModel(say("slow answer"))
    fake.chat_replies.extend([slow, say("quick answer")])

    with turn_blocked_in_the_provider(client, running, slow):
        elsewhere = client.post(MESSAGES.format(free), json={"text": "hello"})

    assert elsewhere.status_code == 200 and elsewhere.json()["outcome"] == "ok"


def test_delete_during_turn_is_409(client, fake, engine):
    chat = new_chat(client)
    slow = SlowModel(say("An answer."))
    fake.chat_replies.append(slow)

    with turn_blocked_in_the_provider(client, chat, slow) as first:
        refused = client.delete(f"/api/chat/sessions/{chat}")
        slow.release.set()
        finished = first.result(timeout=WAIT)

    assert (refused.status_code, refused.json()["code"]) == (409, "turn_running")
    assert finished.status_code == 200
    assert [(m.role, m.content) for m in message_rows(engine, chat)] == [("user", "one"), ("assistant", "An answer.")]
    assert client.delete(f"/api/chat/sessions/{chat}").status_code == 204  # nothing runs now


def test_the_lock_is_released_after_an_exception_in_the_turn(client, fake, engine):
    chat = new_chat(client)
    fake.chat_replies.append(say("a reply that cannot be stored"))

    # the first INSERT is the user's message; the second one, the reply, fails with an unexpected error
    with failing(engine, r"INSERT INTO chatmessage", lambda: RuntimeError("boom-detail"), skip=1):
        failed = quiet(client).post(MESSAGES.format(chat), json={"text": "one"})

    assert (failed.status_code, failed.json()["code"]) == (500, "internal") and "boom-detail" not in failed.text
    assert [(m.role, m.content) for m in message_rows(engine, chat)] == [("user", "one")]  # the user's message stays
    assert turn(client, fake, chat, say("fine"), text="two")["outcome"] == "ok"  # the guard is free again


def test_the_lock_is_released_after_a_provider_failure_and_after_a_limit(client, fake):
    chat = new_chat(client)
    greedy = [ask(("search_wardrobe", {}))] * 8

    limited = turn(client, fake, chat, *greedy)
    failed = turn(client, fake, chat, RuntimeError("provider exploded"), text="again")
    fine = turn(client, fake, chat, say("fine"), text="and again")

    assert (limited["outcome"], failed["outcome"], fine["outcome"]) == ("limit_reached", "provider_error", "ok")


# --- TurnLock (LLD 3.3, AC-23) ----------------------------------------------------------------------------------------


def test_a_chat_can_be_held_once_at_a_time_and_the_key_expires(kv):
    lock = TurnLock(kv)

    with lock.hold(1):
        assert 0 < kv.ttl("turnlock:1") <= TURN_LOCK_TTL_S
        with pytest.raises(TurnRunningError), lock.hold(1):
            pytest.fail("a second turn of the same chat started")
        with pytest.raises(TurnRunningError), lock.hold(1):  # the refusal did not release the holder
            pytest.fail("a second turn of the same chat started")
    assert kv.exists("turnlock:1") == 0


def test_two_chats_hold_together_and_two_lock_objects_share_the_valkey_lock(kv):
    with TurnLock(kv).hold(1), TurnLock(kv).hold(2):
        with pytest.raises(TurnRunningError), TurnLock(kv).hold(1):  # another worker or process
            pytest.fail("the lock is not shared")


def test_the_hold_is_released_when_the_turn_raises(kv):
    lock = TurnLock(kv)
    with pytest.raises(RuntimeError), lock.hold(1):
        raise RuntimeError("turn failed")

    with lock.hold(1):
        pass


def test_a_lock_that_expired_during_the_turn_is_not_an_error_and_not_stolen_from_its_new_holder(kv):
    lock = TurnLock(kv)
    with lock.hold(1):
        kv.delete("turnlock:1")  # the TTL ran out and nobody holds it
    with lock.hold(1):
        kv.delete("turnlock:1")
        kv.set("turnlock:1", "another-token")  # another turn took it over
    assert kv.get("turnlock:1") == "another-token"


def test_valkey_down_is_service_unavailable_on_hold(kv, valkey_fault):
    valkey_fault()

    with pytest.raises(ServiceUnavailableError), TurnLock(kv).hold(1):
        pytest.fail("the body ran without a lock")


def test_valkey_down_at_release_keeps_the_result_of_the_turn(kv, valkey_fault):
    with TurnLock(kv).hold(1):
        valkey_fault("EVALSHA", "EVAL", "DEL")  # the release fails; the key expires by itself
    valkey_fault.heal()

    assert kv.ttl("turnlock:1") > 0


def test_message_route_answers_503_when_valkey_is_down_for_the_lock(client, fake, valkey_fault):
    chat = new_chat(client)
    valkey_fault("SET")  # the session check reads; only the lock write fails
    r = client.post(MESSAGES.format(chat), json={"text": "hello"})

    assert (r.status_code, r.json()["code"]) == (503, "service_unavailable")
    assert message_rows(engine_of(client), chat) == []  # nothing was stored


def test_a_lock_held_by_another_process_is_a_409_on_the_route(client, kv, fake):
    chat = new_chat(client)
    kv.set(f"turnlock:{chat}", "other-process", ex=60)

    r = client.post(MESSAGES.format(chat), json={"text": "hello"})
    deleted = client.delete(f"/api/chat/sessions/{chat}")

    assert [(r.status_code, r.json()["code"]), (deleted.status_code, deleted.json()["code"])] == [
        (409, "turn_running"),
        (409, "turn_running"),
    ]
    kv.delete(f"turnlock:{chat}")  # the TTL ran out
    assert turn(client, fake, chat, say("fine"))["outcome"] == "ok"
