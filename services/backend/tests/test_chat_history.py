"""The message history of a chat (LLD 3.3 and 4.2; AC-12 half, AC-38 history limits, AC-51 flattening, gov low 7).

`load_turns` rebuilds the earlier turns from the stored rows. `window` cuts them to 40 messages and 60,000 characters,
whole turns first, and flattens the tool structures when the model has no tools.
"""

import re
from collections.abc import Sequence

import pytest
from sqlmodel import Session

from kyapehnu.agent.chat.history import load_turns, window
from kyapehnu.agent.llm.anthropic import AnthropicTransport
from kyapehnu.agent.llm.base import AssistantMsg, Msg, ToolCallRequest, ToolResultMsg, UserMsg
from kyapehnu.agent.models import ChatMessage, ChatRole, ChatSession, ToolCall
from tests.chat_rows import with_owner
from tests.sdk_fakes import FakeAnthropicClient, anthropic_reply, text_block

OMITTED = "[earlier result omitted]"
ID_PATTERN = re.compile(r"[A-Za-z0-9_-]+")


# --- stored rows ----------------------------------------------------------------------------------------------------


@pytest.fixture
def s(engine):
    with Session(engine) as session:
        yield session


@pytest.fixture
def engine(engine):
    return with_owner(engine)


def new_session(s: Session) -> int:
    chat = ChatSession(user_id=1)
    s.add(chat)
    s.commit()
    s.refresh(chat)
    return chat.id


def say(s: Session, sid: int, turn: int, step: int, role: ChatRole, content: str) -> None:
    s.add(ChatMessage(user_id=1, session_id=sid, turn=turn, step=step, role=role, content=content))
    s.commit()


def called(
    s: Session, sid: int, turn: int, step: int, call_id: str, name: str, args, result: str, error: str | None = None
) -> None:
    s.add(
        ToolCall(
            user_id=1,
            session_id=sid,
            turn=turn,
            step=step,
            call_id=call_id,
            name=name,
            args=args,
            result=result,
            error=error,
            latency_ms=1,
        )
    )
    s.commit()


def call_ids(turns: Sequence[Sequence[Msg]]) -> list[str]:
    return [c.id for turn in turns for m in turn if isinstance(m, AssistantMsg) for c in m.tool_calls]


def result_ids(turns: Sequence[Sequence[Msg]]) -> list[str]:
    return [m.call_id for turn in turns for m in turn if isinstance(m, ToolResultMsg)]


# --- load_turns -----------------------------------------------------------------------------------------------------


def test_load_turns_rebuilds_each_turn_as_user_assistant_with_calls_then_their_results_in_order(s):
    sid = new_session(s)
    called(s, sid, 1, 1, "c1", "search_wardrobe", {"q": "shirt"}, "R1")  # rows are inserted out of order on purpose
    called(s, sid, 1, 1, "c2", "get_item", {"id": 3}, "R2", error="not_found")
    say(s, sid, 1, 2, ChatRole.assistant, "Try the navy shirt.")
    say(s, sid, 1, 0, ChatRole.user, "Dinner on Saturday?")
    say(s, sid, 1, 1, ChatRole.assistant, "Let me look.")
    say(s, sid, 2, 0, ChatRole.user, "thanks")
    say(s, sid, 2, 1, ChatRole.assistant, "You're welcome.")

    turns = load_turns(s, sid, before_turn=3)

    assert turns == [
        [
            UserMsg("Dinner on Saturday?"),
            AssistantMsg(
                "Let me look.",
                (
                    ToolCallRequest("c1", "search_wardrobe", {"q": "shirt"}),
                    ToolCallRequest("c2", "get_item", {"id": 3}),
                ),
            ),
            ToolResultMsg("c1", "search_wardrobe", "R1", False),
            ToolResultMsg("c2", "get_item", "R2", True),
            AssistantMsg("Try the navy shirt."),
        ],
        [UserMsg("thanks"), AssistantMsg("You're welcome.")],
    ]


def test_load_turns_takes_only_earlier_turns_of_this_session_and_never_notices(s):
    sid, other = new_session(s), new_session(s)
    say(s, sid, 1, 0, ChatRole.user, "first")
    say(s, sid, 1, 1, ChatRole.assistant, "reply")
    say(s, sid, 1, 2, ChatRole.notice, "This model cannot use tools, so it cannot see your wardrobe.")
    say(s, sid, 2, 0, ChatRole.user, "the current message")  # the turn being run is stored already
    say(s, other, 1, 0, ChatRole.user, "another chat")

    assert load_turns(s, sid, before_turn=2) == [[UserMsg("first"), AssistantMsg("reply")]]
    assert load_turns(s, sid, before_turn=1) == []


def test_a_turn_whose_reply_failed_keeps_its_user_message_alone(s):
    sid = new_session(s)
    say(s, sid, 1, 0, ChatRole.user, "hello?")
    say(s, sid, 1, 1, ChatRole.notice, "The AI provider is unavailable.")

    assert load_turns(s, sid, before_turn=2) == [[UserMsg("hello?")]]


def test_a_step_with_calls_and_no_text_has_empty_text_and_a_step_with_nothing_is_skipped(s):
    sid = new_session(s)
    say(s, sid, 1, 0, ChatRole.user, "q")
    called(s, sid, 1, 1, "c1", "get_item", {"id": 1}, "R1")
    say(s, sid, 1, 2, ChatRole.assistant, "")  # no text and no calls
    say(s, sid, 1, 3, ChatRole.assistant, "Done")

    (turn,) = load_turns(s, sid, before_turn=2)

    assert turn == [
        UserMsg("q"),
        AssistantMsg("", (ToolCallRequest("c1", "get_item", {"id": 1}),)),
        ToolResultMsg("c1", "get_item", "R1", False),
        AssistantMsg("Done"),
    ]


def test_arguments_that_were_not_an_object_stay_none_and_a_12000_character_result_is_replayed_verbatim(s):
    sid = new_session(s)
    result = "r" * 12_000
    say(s, sid, 1, 0, ChatRole.user, "q")
    called(s, sid, 1, 1, "c1", "get_item", None, result)

    (turn,) = load_turns(s, sid, before_turn=2)

    assert turn[1].tool_calls == (ToolCallRequest("c1", "get_item", None),)
    assert turn[2].content == result


def test_valid_unique_ids_are_replayed_as_stored(s):
    sid = new_session(s)
    say(s, sid, 1, 0, ChatRole.user, "q")
    called(s, sid, 1, 1, "toolu_01A-b_c", "get_item", {}, "R1")
    called(s, sid, 1, 1, "call_xyz", "get_item", {}, "R2")

    turns = load_turns(s, sid, before_turn=2)

    assert call_ids(turns) == ["toolu_01A-b_c", "call_xyz"] == result_ids(turns)


def test_an_empty_dotted_or_repeated_stored_id_is_replayed_as_a_valid_unique_id_the_same_for_call_and_result(s):
    sid = new_session(s)
    say(s, sid, 1, 0, ChatRole.user, "q1")
    called(s, sid, 1, 1, "", "get_item", {"id": 1}, "R-empty")  # turn 1, step 1
    called(s, sid, 1, 2, "functions.get_item:0", "get_item", {"id": 2}, "R-dotted")  # turn 1, step 2
    say(s, sid, 2, 0, ChatRole.user, "q2")
    called(s, sid, 2, 1, "c1", "get_item", {"id": 3}, "R-first-c1")
    say(s, sid, 3, 0, ChatRole.user, "q3")
    called(s, sid, 3, 1, "c1", "get_item", {"id": 4}, "R-second-c1")  # repeated across the conversation
    called(s, sid, 3, 2, "d", "get_item", {"id": 5}, "R-first-d")
    called(s, sid, 3, 2, "d", "get_item", {"id": 6}, "R-second-d")  # repeated inside one step

    turns = load_turns(s, sid, before_turn=4)

    ids = call_ids(turns)
    assert len(ids) == 6 and len(set(ids)) == 6 and all(ID_PATTERN.fullmatch(i) for i in ids)
    assert result_ids(turns) == ids  # each result carries the id of the call it answers
    assert re.fullmatch(r"call_1_1_\d+", ids[0]) and re.fullmatch(r"call_1_2_\d+", ids[1])
    assert re.fullmatch(r"call_3_1_\d+", ids[3]) and re.fullmatch(r"call_3_2_\d+", ids[5])
    contents = [m.content for turn in turns for m in turn if isinstance(m, ToolResultMsg)]
    assert contents == ["R-empty", "R-dotted", "R-first-c1", "R-second-c1", "R-first-d", "R-second-d"]


def test_the_replayed_ids_are_the_same_every_time(s):
    sid = new_session(s)
    say(s, sid, 1, 0, ChatRole.user, "q")
    called(s, sid, 1, 1, "", "get_item", {}, "R1")
    called(s, sid, 1, 1, "a.b", "get_item", {}, "R2")

    assert load_turns(s, sid, before_turn=2) == load_turns(s, sid, before_turn=2)


def test_a_stored_conversation_from_another_provider_replays_as_a_valid_anthropic_request(s):
    sid = new_session(s)
    say(s, sid, 1, 0, ChatRole.user, "q1")
    called(s, sid, 1, 1, "functions.get_item:0", "get_item", {"id": 1}, "R1")  # an OpenAI-compatible server's id
    say(s, sid, 1, 2, ChatRole.assistant, "one")
    say(s, sid, 2, 0, ChatRole.user, "q2")
    called(s, sid, 2, 1, "", "get_item", {"id": 2}, "R2")
    called(s, sid, 2, 2, "", "get_item", {"id": 3}, "R3")
    say(s, sid, 2, 3, ChatRole.assistant, "two")
    client = FakeAnthropicClient(anthropic_reply(text_block("ok")))

    messages = window(load_turns(s, sid, before_turn=3), [UserMsg("q3")], tools_on=True)
    AnthropicTransport("k", "claude-x", client=client).call_chat(system="S", messages=messages, tools=[])

    sent = client.calls[0]["messages"]
    uses = [b["id"] for m in sent if m["role"] == "assistant" for b in m["content"] if b["type"] == "tool_use"]
    results = [
        b["tool_use_id"]
        for m in sent
        if m["role"] == "user" and isinstance(m["content"], list)
        for b in m["content"]
        if b["type"] == "tool_result"
    ]
    assert len(uses) == 3 and len(set(uses)) == 3 and all(ID_PATTERN.fullmatch(i) for i in uses)
    assert results == uses


# --- window: the limits ---------------------------------------------------------------------------------------------


def chars(messages: Sequence[Msg]) -> int:
    return sum(len(m.content if isinstance(m, ToolResultMsg) else m.text) for m in messages)


def pair(n: int) -> list[Msg]:
    return [UserMsg(f"question {n}"), AssistantMsg(f"answer {n}")]


def flat(turns: Sequence[Sequence[Msg]]) -> list[Msg]:
    return [m for turn in turns for m in turn]


def test_the_window_is_the_earlier_turns_oldest_first_then_the_current_messages():
    earlier, current = [pair(1), pair(2)], [UserMsg("now")]

    assert window(earlier, current, tools_on=True) == [*flat(earlier), *current]


def test_the_window_keeps_40_messages_and_drops_whole_oldest_turns_first():
    earlier, current = [pair(n) for n in range(20)], [UserMsg("now"), AssistantMsg("ok")]  # 40 + 2 messages

    out = window(earlier, current, tools_on=True)

    assert out == [*flat(earlier[1:]), *current] and len(out) == 40  # turn 0 went, whole; the rest is untouched


def test_exactly_40_messages_all_fit():
    earlier, current = [pair(n) for n in range(19)], [UserMsg("now"), AssistantMsg("ok")]  # 38 + 2

    assert window(earlier, current, tools_on=True) == [*flat(earlier), *current]


def test_a_turn_is_never_split_and_older_turns_do_not_fill_the_gap_behind_a_turn_that_does_not_fit():
    big_turn = [UserMsg("q")] + [AssistantMsg(f"step {n}") for n in range(38)]  # 39 messages
    earlier, current = [pair(0), big_turn], [UserMsg("now"), AssistantMsg("ok")]

    assert window(earlier, current, tools_on=True) == current  # 39 + 2 > 40, and the small turn behind it is not taken


def test_a_turn_of_38_messages_fits_beside_two_current_messages():
    turn = [UserMsg("q")] + [AssistantMsg(f"step {n}") for n in range(37)]  # 38 messages
    current = [UserMsg("now"), AssistantMsg("ok")]

    assert window([pair(0), turn], current, tools_on=True) == [*turn, *current]


@pytest.mark.parametrize(("reply_chars", "fits"), [(29_999, True), (30_000, False)])
def test_the_window_keeps_60000_characters_and_drops_the_turn_that_would_pass_it(reply_chars, fits):
    current = [UserMsg("x")]  # 1 character
    newest = [UserMsg("u" * 30_000), AssistantMsg("a" * reply_chars)]  # 59,999 or 60,000
    earlier = [pair(0), newest]

    out = window(earlier, current, tools_on=True)

    assert out == (
        [*newest, *current] if fits else current
    )  # 60,000 in all is the limit; the small turn does not squeeze in
    assert chars(out) <= 60_000


def test_a_turn_that_does_not_fit_stops_the_walk_even_when_older_turns_would():
    earlier = [pair(0), [UserMsg("u" * 61_000), AssistantMsg("a")], pair(2)]

    assert window(earlier, [UserMsg("now")], tools_on=True) == [*pair(2), UserMsg("now")]


def test_a_tool_result_counts_towards_the_characters_of_its_turn():
    heavy = [
        UserMsg("q"),
        AssistantMsg("", (ToolCallRequest("c1", "get_item", {}),)),
        ToolResultMsg("c1", "get_item", "r" * 59_000, False),
        AssistantMsg("a" * 2_000),
    ]
    current = [UserMsg("now")]

    assert window([heavy], current, tools_on=True) == current
    assert window([heavy[:3]], current, tools_on=True) == [*heavy[:3], *current]


def _results(sizes: Sequence[int]) -> list[Msg]:
    calls = tuple(ToolCallRequest(f"c{n}", "search_wardrobe", {}) for n in range(len(sizes)))
    results = [ToolResultMsg(f"c{n}", "search_wardrobe", str(n) * size, n == 1) for n, size in enumerate(sizes)]
    return [UserMsg("q"), AssistantMsg("", calls), *results]


def test_a_current_turn_over_60000_characters_loses_its_oldest_tool_result_first():
    current = _results([30_000, 30_000, 5_000])  # 65,001 in all

    out = window([], current, tools_on=True)

    omitted = ToolResultMsg("c0", "search_wardrobe", OMITTED, False)  # keeps its call id, name and flag
    assert out == [current[0], current[1], omitted, *current[3:]]  # only the oldest result changes
    assert chars(out) <= 60_000


def test_more_results_are_replaced_oldest_first_until_it_fits_and_no_further():
    current = _results([35_000, 35_000, 35_000])  # 105,001

    out = window([], current, tools_on=True)

    assert [m.content for m in out if isinstance(m, ToolResultMsg)] == [OMITTED, OMITTED, "2" * 35_000]
    assert [m.is_error for m in out if isinstance(m, ToolResultMsg)] == [False, True, False]  # flags are kept
    assert chars(out) <= 60_000


def test_a_current_turn_that_fits_is_left_alone():
    current = _results([20_000, 20_000, 19_000])  # 59,001

    assert window([], current, tools_on=True) == current


def test_calls_without_a_result_are_dropped_and_an_assistant_message_left_empty_goes_with_them():
    c1, c2 = ToolCallRequest("c1", "get_item", {}), ToolCallRequest("c2", "get_item", {})
    earlier = [
        [UserMsg("q1"), AssistantMsg("", (c1,)), UserMsg("q1b")]
    ]  # c1 never got an answer: no text, so no message
    current = [UserMsg("q2"), AssistantMsg("Looking", (c1, c2)), ToolResultMsg("c1", "get_item", "R1", False)]

    out = window(earlier, current, tools_on=True)

    assert out == [
        UserMsg("q1"),
        UserMsg("q1b"),
        UserMsg("q2"),
        AssistantMsg("Looking", (c1,)),
        ToolResultMsg("c1", "get_item", "R1", False),
    ]


# --- window: a model without tools ----------------------------------------------------------------------------------


def test_without_tools_assistant_messages_become_text_naming_the_tools_used_and_results_are_dropped():
    c = [
        ToolCallRequest(f"c{n}", name, {}) for n, name in enumerate(("search_wardrobe", "get_item", "generate_outfits"))
    ]
    earlier = [
        [
            UserMsg("q1"),
            AssistantMsg("", (c[0],)),
            ToolResultMsg("c0", "search_wardrobe", "R0", False),
            AssistantMsg("Found it"),
        ]
    ]
    current = [
        UserMsg("q2"),
        AssistantMsg("Checking", (c[1], c[2])),
        ToolResultMsg("c1", "get_item", "R1", False),
        ToolResultMsg("c2", "generate_outfits", "R2", False),
    ]

    out = window(earlier, current, tools_on=False)

    assert out[0] == UserMsg("q1") and out[2:4] == [AssistantMsg("Found it"), UserMsg("q2")]
    assert out[1].tool_calls == () and "(used search_wardrobe)" in out[1].text
    assert out[4] == AssistantMsg("Checking\n(used get_item, generate_outfits)")
    assert len(out) == 5 and not any(isinstance(m, ToolResultMsg) for m in out)
    assert all(not getattr(m, "tool_calls", ()) for m in out)


def test_with_tools_the_structures_are_kept_as_they_are():
    kept = [
        UserMsg("q"),
        AssistantMsg("", (ToolCallRequest("c0", "get_item", {"id": 1}),)),
        ToolResultMsg("c0", "get_item", "R0", False),
    ]

    assert window([kept], [UserMsg("now")], tools_on=True) == [*kept, UserMsg("now")]
