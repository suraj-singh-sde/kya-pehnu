"""The turn loop (AC-12, AC-28 data path, AC-30, and the turn half of AC-5, 44, 45, 51; gov medium 2; LLD 4.1, 4.2).

A turn is run through the real message route with the scripted provider; what the provider was sent, what was stored
and what the page gets back are all read from the outside.
"""

import errno
import sqlite3

import pytest
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, select

from kyapehnu.agent.chat.limits import EMPTY_REPLY_NOTICE, NO_TOOLS_NOTICE
from kyapehnu.agent.chat.tools import SPECS
from kyapehnu.agent.llm.base import (
    REDACTED,
    AssistantMsg,
    LLMOutputError,
    LLMUnavailableError,
    ToolCallRequest,
    ToolResultMsg,
    ToolsUnsupportedError,
    UserMsg,
)
from kyapehnu.agent.models import ChatMessage
from kyapehnu.user.profile import get_profile
from kyapehnu.wardrobe.ingest.pipeline import UNUSABLE_REPLY
from kyapehnu.wardrobe.models import Event, WearLog
from kyapehnu.wardrobe.outfits.pick import profile_line
from tests.chat_fakes import ask, fixed_now, say
from tests.chat_helpers import (
    DINNER,
    NOW,
    OpenAILikeTransport,
    add_party,
    count_rows,
    engine_of,
    failing,
    llm_rows,
    message_rows,
    new_chat,
    party_wardrobe,
    pin_rest_today,
    results,
    running_app,
    send,
    tool_rows,
    turn,
)
from tests.fakes import OWNER_ID, FakeTransport, add_item

KEY = "sk-ant-LEAKYKEY-4242"
FAILED = "The AI provider failed: {}. Your message is saved; send it again to retry."


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    fixed_now(monkeypatch, NOW)
    pin_rest_today(monkeypatch)


@pytest.fixture
def engine(client):
    return engine_of(client)


def shape(messages) -> list[tuple[int, int, str, str]]:
    return [(m.turn, m.step, m.role, m.content) for m in messages]


# --- AC-12: the order of a turn -------------------------------------------------------------------------------------


def test_search_then_generate_is_stored_in_order_the_turn_ends_on_the_looks_and_results_precede_the_next_call(
    client, fake, engine
):
    with Session(engine, expire_on_commit=False) as s:
        party_wardrobe(s)
        event = add_party(s)
    generate = {"event_id": event.id}

    out = turn(
        client,
        fake,
        new_chat(client),
        ask(("search_wardrobe", {})),
        ask(("generate_outfits", generate)),
        say("Here are three looks."),
    )

    assert (out["outcome"], out["turn"]) == ("ok", 1)
    assert shape(message_rows(engine)) == [(1, 0, "user", "hello")]  # the turn ended on the looks: no reply asked
    assert fake.chat_replies == [say("Here are three looks.")]
    first, second = tool_rows(engine)
    assert [(r.turn, r.step, r.name, r.call_id) for r in (first, second)] == [
        (1, 1, "search_wardrobe", "c1"),
        (1, 2, "generate_outfits", "c1"),
    ]
    assert first.result and second.result and first.id < second.id
    # the provider saw each tool result before it was asked again
    sent = [call["messages"] for call in fake.chat_calls]
    assert len(sent) == 2 and sent[0] == [UserMsg("hello")]
    assert sent[1] == [
        UserMsg("hello"),
        AssistantMsg("", (ToolCallRequest("c1", "search_wardrobe", {}),)),
        ToolResultMsg("c1", "search_wardrobe", first.result, False),
    ]
    # the looks ended the turn; the next message replays them to the provider before its first request
    turn(client, fake, 1, text="and?")
    replay = fake.chat_calls[2]["messages"]
    kinds = ["UserMsg", "AssistantMsg", "ToolResultMsg", "AssistantMsg", "ToolResultMsg", "UserMsg"]
    assert [type(m).__name__ for m in replay] == kinds
    assert replay[3].tool_calls[0].name == "generate_outfits" and replay[-1] == UserMsg("and?")
    assert (replay[4].name, replay[4].content, replay[4].is_error) == ("generate_outfits", second.result, False)


def test_the_text_of_a_reply_that_also_calls_tools_is_kept_at_its_step_and_sent_back(client, fake, engine):
    turn(client, fake, new_chat(client), ask(("search_wardrobe", {}), text="Let me look."), say("Found nothing."))

    assert shape(message_rows(engine)) == [
        (1, 0, "user", "hello"),
        (1, 1, "assistant", "Let me look."),
        (1, 2, "assistant", "Found nothing."),
    ]
    assistant = fake.chat_calls[1]["messages"][1]
    assert assistant == AssistantMsg("Let me look.", (ToolCallRequest("c1", "search_wardrobe", {}),))


def test_a_text_only_reply_ends_the_turn_with_one_model_call(client, fake, engine):
    out = turn(client, fake, new_chat(client), say("Hi there."))

    assert out["outcome"] == "ok" and len(fake.chat_calls) == 1 and tool_rows(engine) == []
    assert shape(message_rows(engine)) == [(1, 0, "user", "hello"), (1, 1, "assistant", "Hi there.")]
    assert [(m["role"], m["content"]) for m in out["session"]["messages"]] == [
        ("user", "hello"),
        ("assistant", "Hi there."),
    ]


def test_the_turn_answer_is_the_stored_chat_as_the_page_opens_it(client, fake):
    chat = new_chat(client)

    out = turn(client, fake, chat, ask(("search_wardrobe", {})), say("done"))

    assert out["session"] == client.get(f"/api/chat/sessions/{chat}").json()


def test_every_request_carries_the_nine_tools_the_clock_and_the_profile(client, fake, engine):
    with Session(engine, expire_on_commit=False) as s:
        profile = get_profile(OWNER_ID, s)
        profile.style_prefs = "smart casual"
        s.add(profile)
        s.commit()
        line = profile_line(profile)

    turn(client, fake, new_chat(client), ask(("search_wardrobe", {})), say("ok"))

    assert len(fake.chat_calls) == 2
    for call in fake.chat_calls:
        assert call["tools"] == list(SPECS) and len(call["tools"]) == 9
        assert "Today is Wednesday, 2026-10-07, and the local time is 09:30." in call["system"]
        assert (
            call["system"].endswith(f"Profile: {line}")
            and "Propose outfits only through generate_outfits" in call["system"]
        )


def test_a_later_turn_sends_the_earlier_ones_and_every_request_is_labelled_with_session_and_turn(client, fake, engine):
    chat = new_chat(client)

    first = turn(client, fake, chat, say("a"))
    second = turn(client, fake, chat, say("b"), text="again")

    assert (first["turn"], second["turn"]) == (1, 2)
    assert fake.chat_calls[1]["messages"] == [UserMsg("hello"), AssistantMsg("a"), UserMsg("again")]
    labels = [r.request_summary.split(" messages=")[0] for r in llm_rows(engine, "chat")]
    assert labels == [f"session={chat} turn=1", f"session={chat} turn=2"]


def test_one_chat_never_sees_another_chats_messages(client, fake):
    one, two = new_chat(client), new_chat(client)

    turn(client, fake, one, say("a"), text="first chat secret")
    turn(client, fake, two, say("b"), text="second chat")

    assert fake.chat_calls[1]["messages"] == [UserMsg("second chat")]


def test_an_empty_reply_is_stored_as_a_notice_and_the_turn_ends(client, fake, engine):
    out = turn(client, fake, new_chat(client), say(""))

    assert out["outcome"] == "ok"
    assert [(m.role, m.content) for m in message_rows(engine)] == [("user", "hello"), ("notice", EMPTY_REPLY_NOTICE)]


def test_the_first_message_titles_the_chat_and_a_turn_moves_it_to_the_top_of_the_list(client, fake):
    older, newer = new_chat(client), new_chat(client)
    assert [c["id"] for c in client.get("/api/chat/sessions").json()] == [newer, older]

    turn(client, fake, older, say("ok"), text="Dinner on Saturday")

    listed = client.get("/api/chat/sessions").json()
    assert [(c["id"], c["title"]) for c in listed] == [(older, "Dinner on Saturday"), (newer, "")]


def test_a_message_to_a_chat_that_does_not_exist_is_404_with_nothing_stored_or_asked(client, fake, engine):
    r = send(client, fake, 999, say("never used"))

    assert (r.status_code, r.json()["code"]) == (404, "not_found")
    assert fake.chat_calls == [] and count_rows(engine, ChatMessage) == {"ChatMessage": 0}


# --- AC-28 (data path): three cards, then a thumb down --------------------------------------------------------------


def test_turn_with_three_cards_then_thumbs_down_reflects(client, fake, engine):
    with Session(engine, expire_on_commit=False) as s:
        party_wardrobe(s)
    chat = new_chat(client)

    out = turn(
        client,
        fake,
        chat,
        ask(("create_event", DINNER)),
        ask(("generate_outfits", {"event_id": 1})),
        say("Three looks for you."),
    )

    created, generated = out["session"]["tool_calls"]
    assert created["event"]["id"] == 1 and created["card"] is None
    suggestions = generated["card"]["suggestions"]
    assert len(suggestions) == 3 and generated["card"]["event"]["id"] == 1
    assert all(s["rating"] is None and s["wearable"] and None not in s["items"] for s in suggestions)

    r = client.post("/api/feedback", json={"item_ids": suggestions[0]["item_ids"], "rating": -1, "reason": "too shiny"})
    assert r.status_code == 201

    after = client.get(f"/api/chat/sessions/{chat}").json()["tool_calls"][1]["card"]["suggestions"]
    assert [s["rating"] for s in after] == [-1, None, None]
    assert count_rows(engine, WearLog) == {"WearLog": 0}  # a thumb is not a wear


# --- AC-30: a provider failure is a notice, not an HTTP error -------------------------------------------------------


def test_provider_failure_is_a_redacted_notice_message_kept_and_retry_works(tmp_path, fake_weather):
    stand_in = FakeTransport()
    stand_in.api_key = KEY  # a real transport knows its key: the logged error is cleaned with it too
    with running_app(tmp_path, fake_weather, stand_in, anthropic_api_key=KEY) as client:
        engine = engine_of(client)
        chat = new_chat(client)

        failed = send(client, stand_in, chat, LLMUnavailableError(f"401 invalid key {KEY} for this model"))
        retried = turn(client, stand_in, chat, say("Welcome back."), text="hello again")

        assert failed.status_code == 200
        out = failed.json()
        assert out["outcome"] == "provider_error"
        notice = FAILED.format(f"401 invalid key {REDACTED} for this model")
        assert [(m["role"], m["content"]) for m in out["session"]["messages"]] == [
            ("user", "hello"),
            ("notice", notice),
        ]
        assert retried["outcome"] == "ok"
        assert [(m.turn, m.role, m.content) for m in message_rows(engine)] == [
            (1, "user", "hello"),
            (1, "notice", notice),
            (2, "user", "hello again"),
            (2, "assistant", "Welcome back."),
        ]
        # the failed message goes to the model again as history, the notice never does
        assert stand_in.chat_calls[1]["messages"] == [UserMsg("hello"), UserMsg("hello again")]
        rows = llm_rows(engine, "chat")
        assert [bool(r.error) for r in rows] == [True, False]
        assert KEY not in "".join(f"{r.error}{r.request_summary}{r.response}" for r in rows)


@pytest.mark.parametrize("known_to", ["the settings", "only the transport"])
def test_the_notice_redacts_a_key_known_to_the_settings_or_only_to_the_transport(tmp_path, fake_weather, known_to):
    stand_in = FakeTransport()
    settings = {"anthropic_api_key": KEY} if known_to == "the settings" else {}
    if known_to != "the settings":
        stand_in.api_key = KEY
    with running_app(tmp_path, fake_weather, stand_in, **settings) as client:
        out = send(client, stand_in, new_chat(client), LLMUnavailableError(f"bad key {KEY}")).json()

        assert KEY not in str(out) and FAILED.format(f"bad key {REDACTED}") in [
            m["content"] for m in out["session"]["messages"]
        ]


@pytest.mark.parametrize(
    ("error", "notice"),
    [
        (LLMUnavailableError("x" * 500), FAILED.format("x" * 300)),
        (LLMOutputError("the model said: PRIVATE-REPLY"), FAILED.format(UNUSABLE_REPLY)),
        (RuntimeError("PRIVATE-DETAIL"), "The AI provider's reply could not be used."),
    ],
    ids=["long text cut at 300", "output error hides the reply", "any other exception"],
)
def test_the_notice_says_no_more_than_a_plain_failure(client, fake, engine, error, notice):
    out = turn(client, fake, new_chat(client), error)

    assert out["outcome"] == "provider_error"
    assert [(m.role, m.content) for m in message_rows(engine)] == [("user", "hello"), ("notice", notice)]


def test_a_provider_that_fails_after_a_tool_result_keeps_what_already_ran(client, fake, engine):
    with Session(engine, expire_on_commit=False) as s:
        add_item(s, name="Shirt")

    out = turn(client, fake, new_chat(client), ask(("search_wardrobe", {})), LLMUnavailableError("overloaded"))

    assert out["outcome"] == "provider_error"
    assert [r.name for r in tool_rows(engine)] == ["search_wardrobe"]
    assert message_rows(engine)[-1].content == FAILED.format("overloaded")


# --- AC-5, 44, 45, 51 (turn half): a model that cannot use tools ----------------------------------------------------


@pytest.fixture
def local(tmp_path, fake_weather):
    """An app whose provider looks like an Ollama server: the only kind whose refusal of tools is remembered."""
    stand_in = OpenAILikeTransport()
    with running_app(tmp_path, fake_weather, stand_in) as client:
        yield client, stand_in


def test_a_refusal_of_tools_on_the_first_request_retries_once_without_tools_and_says_so(local):
    client, stand_in = local
    engine = engine_of(client)
    chat = new_chat(client)

    out = turn(
        client, stand_in, chat, ToolsUnsupportedError("this model does not support tools"), say("General advice.")
    )

    assert out["outcome"] == "no_tools"
    assert shape(message_rows(engine)) == [  # the retry takes the step of the refused request
        (1, 0, "user", "hello"),
        (1, 1, "assistant", "General advice."),
        (1, 2, "notice", NO_TOOLS_NOTICE),
    ]
    first, retry = stand_in.chat_calls
    assert len(first["tools"]) == 9 and retry["tools"] == []
    assert retry["messages"] == [UserMsg("hello")] and "You cannot see the wardrobe" in retry["system"]
    refused, answered = llm_rows(engine, "chat")
    assert refused.error and refused.request_summary.endswith("no_tools=False")
    assert answered.error is None and answered.request_summary.endswith("no_tools=True")


def test_the_next_turn_of_that_model_sends_no_tools_at_all_and_repeats_the_notice(local):
    client, stand_in = local
    engine = engine_of(client)
    chat = new_chat(client)
    turn(client, stand_in, chat, ToolsUnsupportedError("no tools"), say("First."))

    out = turn(client, stand_in, chat, say("Second."), text="and now?")

    assert out["outcome"] == "no_tools" and len(stand_in.chat_calls) == 3  # no refused request this time
    assert stand_in.chat_calls[2]["tools"] == []
    assert [m.content for m in message_rows(engine) if m.role == "notice"] == [NO_TOOLS_NOTICE, NO_TOOLS_NOTICE]


def test_tool_calls_in_a_reply_to_a_request_without_tools_are_not_run(local):
    client, stand_in = local
    engine = engine_of(client)
    chat = new_chat(client)
    turn(client, stand_in, chat, ToolsUnsupportedError("no tools"), say("First."))

    out = turn(client, stand_in, chat, ask(("search_wardrobe", {}), text="I would search."))

    assert out["outcome"] == "no_tools" and tool_rows(engine, chat) == []


def test_the_retry_in_a_chat_that_used_tools_before_has_no_tool_calls_or_results(local):
    client, stand_in = local
    chat = new_chat(client)
    turn(client, stand_in, chat, ask(("search_wardrobe", {})), say("Looked."))

    out = turn(client, stand_in, chat, ToolsUnsupportedError("no tools"), say("General advice."), text="and a tip?")

    retry = stand_in.chat_calls[-1]
    assert out["outcome"] == "no_tools" and retry["tools"] == []
    assert not [m for m in retry["messages"] if isinstance(m, ToolResultMsg)]
    assert not [m for m in retry["messages"] if isinstance(m, AssistantMsg) and m.tool_calls]
    assert any(isinstance(m, AssistantMsg) and "search_wardrobe" in m.text for m in retry["messages"])  # a plain line
    assert retry["messages"][-1] == UserMsg("and a tip?")


def test_a_refusal_after_the_first_request_is_a_plain_failure_and_nothing_is_remembered(local):
    client, stand_in = local
    engine = engine_of(client)
    chat = new_chat(client)

    out = turn(client, stand_in, chat, ask(("search_wardrobe", {})), ToolsUnsupportedError("tool schema is invalid"))

    assert out["outcome"] == "provider_error" and len(stand_in.chat_calls) == 2  # not retried
    assert message_rows(engine)[-1].content == FAILED.format("tool schema is invalid")
    later = turn(client, stand_in, chat, say("Fine."), text="try again")
    assert later["outcome"] == "ok" and len(stand_in.chat_calls[2]["tools"]) == 9


def test_a_refusal_of_the_retry_is_a_provider_failure_after_two_requests(local):
    client, stand_in = local
    engine = engine_of(client)

    out = turn(client, stand_in, new_chat(client), ToolsUnsupportedError("first"), ToolsUnsupportedError("second"))

    assert out["outcome"] == "provider_error" and len(stand_in.chat_calls) == 2
    assert FAILED.format("second") in [m.content for m in message_rows(engine)]


# --- gov medium 2: infrastructure errors stop the turn --------------------------------------------------------------


@pytest.mark.parametrize(
    ("number", "status", "code"),
    [(errno.ENOSPC, 507, "insufficient_storage"), (errno.EIO, 500, "internal")],
    ids=["disk full", "other OS error"],
)
def test_disk_full_stops_the_turn_with_507_keeps_the_user_message_and_releases_the_guard(
    client, fake, engine, number, status, code
):
    with Session(engine, expire_on_commit=False) as s:
        add_item(s, name="Shirt")
    chat = new_chat(client)

    with failing(engine, r"FROM item\b", lambda: OSError(number, "device trouble")):
        r = send(client, fake, chat, ask(("search_wardrobe", {})))

    assert (r.status_code, r.json()["code"]) == (status, code)
    assert [(m.role, m.content) for m in message_rows(engine)] == [("user", "hello")]  # kept, no reply, no notice
    assert tool_rows(engine) == [] and len(fake.chat_calls) == 1  # the error never went back to the model
    again = turn(client, fake, chat, say("Back again."), text="and now?")  # the guard was released
    assert again["outcome"] == "ok"


def test_sqlite_disk_full_inside_a_tool_is_507_with_no_event_kept_and_the_guard_released(client, fake, engine):
    full = sqlite3.OperationalError("database or disk is full")
    full.sqlite_errorcode = sqlite3.SQLITE_FULL  # what Python's sqlite3 sets for a real full disk
    chat = new_chat(client)
    saturday = {"type": "party", "starts_at": "2026-10-10T20:00", "place": "Gurugram"}

    with failing(engine, r"INSERT INTO event ", lambda: OperationalError("INSERT", {}, full)) as fired:
        r = send(client, fake, chat, ask(("create_event", saturday)))

    assert fired == [1]  # the statement the tool ran was the one that failed
    assert (r.status_code, r.json()["code"]) == (507, "insufficient_storage")
    assert [(m.role, m.content) for m in message_rows(engine)] == [("user", "hello")]
    assert tool_rows(engine) == [] and len(fake.chat_calls) == 1  # the error never went back to the model
    with Session(engine) as s:
        assert s.exec(select(Event)).all() == []
    assert turn(client, fake, chat, say("Back again."), text="and now?")["outcome"] == "ok"


def test_a_database_busy_outside_a_tool_is_503_db_busy_with_nothing_asked_and_the_guard_released(client, fake, engine):
    chat = new_chat(client)
    busy = OperationalError("INSERT", {}, sqlite3.OperationalError("database is locked"))

    with failing(engine, r"INSERT INTO chatmessage", lambda: busy):
        r = send(client, fake, chat, say("never asked"))

    assert (r.status_code, r.json()["code"], r.headers["Retry-After"]) == (503, "db_busy", "1")
    assert message_rows(engine) == [] and fake.chat_calls == []
    fake.chat_replies.clear()
    assert turn(client, fake, chat, say("ok now"))["outcome"] == "ok"


def test_the_results_the_model_gets_are_the_ones_stored(client, fake, engine):
    with Session(engine, expire_on_commit=False) as s:
        add_item(s, name="Shirt")
    turn(client, fake, new_chat(client), ask(("search_wardrobe", {}), ("get_item", {"item_id": 1})), say("ok"))

    sent = [m.content for m, _ in results(fake)]

    assert sent == [r.result for r in tool_rows(engine)]
