"""The limits of a turn and of what the model reads (AC-38, gov low 5; LLD 4.2, 6): each limit that is hit gives the
outcome `limit_reached` and a stored notice, and the turn still answers 200.
"""

import json

import pytest
from sqlmodel import Session

from kyapehnu.agent.chat import orchestrator
from kyapehnu.agent.chat.limits import (
    LIMIT_NOTICE,
    MAX_MODEL_CALLS,
    MAX_RESULT_CHARS,
    MAX_SEARCH_RESULTS,
    MAX_TOOL_CALLS,
    TURN_BUDGET_S,
)
from kyapehnu.agent.chat.tools import TurnBudget
from kyapehnu.agent.llm.base import UserMsg
from kyapehnu.core.limits import MAX_EXCLUDE, MAX_INCLUDE
from kyapehnu.wardrobe.models import Item
from tests.chat_fakes import ask, fixed_now, say
from tests.chat_helpers import (
    NOW,
    add_party,
    engine_of,
    llm_rows,
    message_rows,
    new_chat,
    party_wardrobe,
    pin_rest_today,
    raw_results,
    result,
    tool_rows,
    turn,
    watching,
)
from tests.chat_rows import add_message
from tests.fakes import add_item


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    fixed_now(monkeypatch, NOW)
    pin_rest_today(monkeypatch)


@pytest.fixture
def engine(client):
    return engine_of(client)


def notices(engine, chat: int) -> list[str]:
    return [m.content for m in message_rows(engine, chat) if m.role == "notice"]


class Clock:
    """A clock the test moves: the turn reads it through `run_turn(clock=...)`."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# --- the budget of a turn -------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("used", "runs", "allowed", "after"),
    [
        (0, 0, True, (2, 1)),
        (4, 1, True, (6, 2)),
        (6, 1, True, (8, 2)),
        (7, 0, False, (7, 0)),
        (8, 0, False, (8, 0)),
        (0, 2, False, (0, 2)),
        (2, 3, False, (2, 3)),
    ],
)
def test_a_pick_is_charged_two_model_calls_and_at_most_two_runs_fit(used, runs, allowed, after):
    budget = TurnBudget(deadline=100.0, clock=lambda: 0.0, model_calls=used, generate_runs=runs)

    got = budget.reserve_pick()

    assert got is allowed and (budget.model_calls, budget.generate_runs) == after  # a refusal changes nothing


@pytest.mark.parametrize(
    ("used", "now", "expected"), [(0, 0.0, True), (7, 99.9, True), (8, 0.0, False), (3, 100.0, False)]
)
def test_a_model_call_needs_a_call_left_and_time_left(used, now, expected):
    budget = TurnBudget(deadline=100.0, clock=lambda: now, model_calls=used)

    assert budget.can_call_model() is expected
    assert budget.time_left() is (now < 100.0)  # at the deadline there is no time left


# --- AC-38: the limits of calls and time ----------------------------------------------------------------------------


def test_three_calls_per_reply_stops_at_12_toolcalls_within_8_calls(client, fake, engine):
    chat = new_chat(client)
    greedy = ask(("search_wardrobe", {}), ("search_wardrobe", {}), ("search_wardrobe", {}))

    out = turn(client, fake, chat, *[greedy] * 5)  # the model never stops asking

    assert out["outcome"] == "limit_reached"
    assert len(tool_rows(engine, chat)) == MAX_TOOL_CALLS  # the thirteenth call was neither run nor stored
    assert len(fake.chat_calls) == 5 and len(llm_rows(engine, "chat")) == 5 and len(fake.chat_calls) <= MAX_MODEL_CALLS
    assert notices(engine, chat) == [LIMIT_NOTICE]
    assert [m["content"] for m in out["session"]["messages"]][-1] == LIMIT_NOTICE


def test_one_call_per_reply_stops_at_8(client, fake, engine):
    chat = new_chat(client)

    out = turn(client, fake, chat, *[ask(("search_wardrobe", {}))] * 8)

    assert out["outcome"] == "limit_reached"
    assert len(fake.chat_calls) == MAX_MODEL_CALLS and len(tool_rows(engine, chat)) == 8
    assert fake.chat_replies == [] and notices(engine, chat) == [LIMIT_NOTICE]


def test_third_generate_and_pick_budget_are_limit_reached(client, fake, engine):
    with Session(engine, expire_on_commit=False) as s:
        made = party_wardrobe(s)
        event = add_party(s)
    chat = new_chat(client)
    # two tops cannot share a look: a must_include problem, no looks, so the turn goes on to the next generate
    two_tops = [made["top"][0].id, made["top"][1].id]
    generate = ask(("generate_outfits", {"event_id": event.id, "include": two_tops}))

    out = turn(client, fake, chat, ask(("search_wardrobe", {})), generate, generate, generate)

    first, second, third = (r for r in tool_rows(engine, chat) if r.name == "generate_outfits")
    assert (first.error, second.error, third.error) == (None, None, "limit_reached")
    assert json.loads(third.result) == {
        "error": {"code": "limit_reached", "message": "No more outfit generations in this turn."}
    }
    assert out["outcome"] == "limit_reached" and len(fake.chat_calls) == 4  # each generate cost two model calls
    assert notices(engine, chat) == [LIMIT_NOTICE]


def test_a_generate_with_fewer_than_two_model_calls_left_is_refused_and_the_turn_goes_on(client, fake, engine):
    with Session(engine, expire_on_commit=False) as s:
        party_wardrobe(s)
        event = add_party(s)
    chat = new_chat(client)

    out = turn(
        client,
        fake,
        chat,
        *[ask(("search_wardrobe", {}))] * 6,
        ask(("generate_outfits", {"event_id": event.id})),  # the seventh request: one call is left
        say("I could not make outfits this time."),
    )

    refused = tool_rows(engine, chat)[-1]
    assert (refused.name, refused.error) == ("generate_outfits", "limit_reached")
    assert out["outcome"] == "ok" and len(fake.chat_calls) == MAX_MODEL_CALLS and notices(engine, chat) == []


@pytest.mark.parametrize(
    ("seconds", "outcome", "ran"),
    [(TURN_BUDGET_S - 0.1, "ok", 1), (TURN_BUDGET_S, "limit_reached", 0)],
    ids=["just inside the budget", "at the budget"],
)
def test_budget_600s_with_fake_clock(client, fake, engine, seconds, outcome, ran):
    ctx, chat, clock = client.app.state.ctx, new_chat(client), Clock()

    def slow_model():
        clock.advance(seconds)  # the model took this long; a call in flight is never interrupted
        return ask(("search_wardrobe", {}))

    fake.chat_replies.extend([slow_model, say("Done.")])
    out = orchestrator.run_turn(1, ctx, ctx.provider, chat, "hello", NOW, clock=clock)

    assert out.outcome == outcome and len(tool_rows(engine, chat)) == ran
    assert (notices(engine, chat) == [LIMIT_NOTICE]) is (outcome == "limit_reached")
    assert len(fake.chat_calls) == (2 if outcome == "ok" else 1)


def test_time_that_runs_out_during_a_tool_stops_the_loop_before_the_next_model_call(client, fake, engine):
    with Session(engine, expire_on_commit=False) as s:
        add_item(s, name="Shirt")
    ctx, chat, clock = client.app.state.ctx, new_chat(client), Clock()
    fake.chat_replies.extend([ask(("search_wardrobe", {})), say("never asked")])

    with watching(engine, r"FROM item\b", lambda: clock.advance(TURN_BUDGET_S)):
        out = orchestrator.run_turn(1, ctx, ctx.provider, chat, "hello", NOW, clock=clock)

    assert out.outcome == "limit_reached" and len(fake.chat_calls) == 1
    assert [r.name for r in tool_rows(engine, chat)] == ["search_wardrobe"]  # it ran: the call began in time
    assert notices(engine, chat) == [LIMIT_NOTICE]


def test_calls_after_the_time_is_up_are_not_run_and_not_stored(client, fake, engine):
    with Session(engine, expire_on_commit=False) as s:
        add_item(s, name="Shirt")
    ctx, chat, clock = client.app.state.ctx, new_chat(client), Clock()
    fake.chat_replies.append(ask(("search_wardrobe", {}), ("search_wardrobe", {}), ("search_wardrobe", {})))

    with watching(engine, r"FROM item\b", lambda: clock.advance(TURN_BUDGET_S)):  # the first call uses it all
        out = orchestrator.run_turn(1, ctx, ctx.provider, chat, "hello", NOW, clock=clock)

    assert out.outcome == "limit_reached" and len(tool_rows(engine, chat)) == 1


# --- AC-38: what the model reads ------------------------------------------------------------------------------------


def seed_history(engine, chat: int, turns: int, user: str, reply: str) -> None:
    for n in range(1, turns + 1):
        add_message(engine, chat, n, 0, "user", user.format(n))
        add_message(engine, chat, n, 1, "assistant", reply.format(n))


def test_history_41_messages(client, fake, engine):
    chat = new_chat(client)
    seed_history(engine, chat, 20, "question {}", "answer {}")  # 40 stored messages, and a 41st arrives now

    turn(client, fake, chat, say("ok"), text="the newest question")

    sent = fake.chat_calls[0]["messages"]
    assert len(sent) <= 40 and sent[-1] == UserMsg("the newest question")  # the newest message is always there
    assert (len(sent), sent[0]) == (39, UserMsg("question 2"))  # whole turns only: the oldest turn went


def test_history_60000_chars(client, fake, engine):
    chat = new_chat(client)
    seed_history(engine, chat, 10, "q{} " + "x" * 96, "a" * 6000)  # 10 turns of about 6,100 characters

    turn(client, fake, chat, say("ok"), text="and now?")

    sent = fake.chat_calls[0]["messages"]
    assert sum(len(m.text) for m in sent) <= 60_000 and sent[-1] == UserMsg("and now?")
    assert len(sent) == 2 * 9 + 1  # nine whole earlier turns fit, the tenth would not


def item_result(client, fake, engine, item_id: int, chat: int, tag_length: int):
    """The `get_item` result the model gets for an item with one tag of this many characters, and its stored row."""
    with Session(engine, expire_on_commit=False) as s:
        item = s.get(Item, item_id)
        item.tags = ["t" * tag_length]
        s.add(item)
        s.commit()
    turn(client, fake, chat, ask(("get_item", {"item_id": item_id})), say("ok"))
    return raw_results(fake)[-1], tool_rows(engine, chat)[-1]


# the extra sizes sit where the number in "[cut: N characters]" gains a digit (N = 99 -> 100) and where it has five
@pytest.mark.parametrize("size", [MAX_RESULT_CHARS, MAX_RESULT_CHARS + 1, 12_079, 12_080, 99_999])
def test_result_cut_at_12000(client, fake, engine, size):
    with Session(engine, expire_on_commit=False) as s:
        item = add_item(s, name="Shirt")
    chat = new_chat(client)
    probe, _ = item_result(client, fake, engine, item.id, chat, 100)  # the JSON grows one character per tag character
    tag_length = 100 + size - len(probe.content)  # so this tag makes the whole result exactly `size` characters

    sent, row = item_result(client, fake, engine, item.id, chat, tag_length)

    assert len(sent.content) <= MAX_RESULT_CHARS and row.result == sent.content and not sent.is_error
    if size == MAX_RESULT_CHARS:  # exactly the limit is not cut
        assert len(sent.content) == MAX_RESULT_CHARS and row.truncated is False and "[cut:" not in sent.content
        return
    suffix = sent.content[sent.content.rindex("[cut: ") :]
    kept = len(sent.content) - len(suffix)
    assert suffix == f"[cut: {size - kept} characters]" and row.truncated is True  # it says how much was cut
    assert MAX_RESULT_CHARS - 10 <= len(sent.content) and sent.content.startswith(
        '{"item":{"id":'
    )  # as long as it may be


def test_search_80_returns_50_total_80(client, fake, engine):
    with Session(engine, expire_on_commit=False) as s:
        for n in range(80):
            add_item(s, name=f"Tee {n:02d}")

    turn(client, fake, new_chat(client), ask(("search_wardrobe", {})), say("ok"))

    shown = result(fake)
    listed = client.get("/api/items").json()
    assert (shown["total"], len(shown["items"])) == (80, MAX_SEARCH_RESULTS) and len(listed) == 80
    assert [i["id"] for i in shown["items"]] == [i["id"] for i in listed][:MAX_SEARCH_RESULTS]


@pytest.mark.parametrize(
    ("days", "ok"),
    [(0, False), (1, True), (3650, True), (3651, False)],
)
def test_days_0_and_3651_are_errors(client, fake, engine, days, ok):
    with Session(engine, expire_on_commit=False) as s:
        item = add_item(s, name="Shirt")

    turn(client, fake, new_chat(client), ask(("get_wear_history", {"item_id": item.id, "days": days})), say("ok"))

    shown = result(fake)
    assert ("wears" in shown) is ok and (shown.get("error", {}).get("code") == "invalid_arguments") is not ok


def values(field: str, n: int) -> list:
    return [[i] for i in range(1, n + 1)] if field == "exclude" else list(range(1, n + 1))


@pytest.mark.parametrize(
    ("field", "limit"),
    [("exclude", MAX_EXCLUDE), ("include", MAX_INCLUDE)],
    ids=["50 excluded sets", "6 included items"],
)
def test_generate_takes_up_to_the_limit_of_excluded_sets_and_included_items(client, fake, engine, field, limit):
    with Session(engine, expire_on_commit=False) as s:
        party_wardrobe(s)
        event = add_party(s)
    chat = new_chat(client)

    turn(client, fake, chat, ask(("generate_outfits", {"event_id": event.id, field: values(field, limit)})), say("ok"))
    turn(client, fake, chat, text="and?")  # a turn that showed looks ends on them: the model reads them now
    at_limit = result(fake)
    over_args = {"event_id": event.id, field: values(field, limit + 1)}
    turn(client, fake, chat, ask(("generate_outfits", over_args)), say("ok"), text="more")
    over = result(fake)

    assert "error" not in at_limit  # a problem with the outfits, if any, is a normal result
    assert over["error"] == {"code": "invalid_arguments", "message": f"{field} (too_long)"}
