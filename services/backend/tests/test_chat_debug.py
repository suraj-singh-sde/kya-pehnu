"""The Debug panel's chat data (AC-32; LLD 3.1, 5.4): the tool calls, and the LLM calls filtered by session."""

import pytest

from tests.chat_rows import add_call, add_llm_call, add_session

TOOL_CALLS = "/api/debug/tool-calls"
LLM_CALLS = "/api/debug/llm-calls"


@pytest.fixture
def world(client):
    """Chat 1: two calls (the second fails). Chat 10: one call. LLM rows: chat and nested pick of both, and one of the
    Outfits page."""
    engine = client.app.state.ctx.engine
    first, tenth = add_session(engine), add_session(engine, id=10)
    ok = add_call(engine, first, "search_wardrobe", args={"category": "top"}, result='{"total":3}', latency_ms=12)
    failed = add_call(
        engine, first, "get_weather", step=2, args={"place": "Nowhere"}, error="place_not_found", result="{}"
    )
    other = add_call(engine, tenth, "get_item", args={"item_id": 4})
    rows = {
        "chat_1": add_llm_call(engine, "chat", "session=1 messages=2 tools=9", input_tokens=40, output_tokens=7),
        "chat_10": add_llm_call(engine, "chat", "session=10 messages=2 tools=9"),
        "pick_1": add_llm_call(engine, "outfit_pick", "session=1 images=[] bytes\nPick", error="boom"),
        "page": add_llm_call(engine, "outfit_pick", "images=[] bytes\nPick"),
    }
    return {"ok": ok, "failed": failed, "other": other, **rows, "first": first, "tenth": tenth}


def test_tool_calls_are_listed_newest_first_with_session_name_arguments_result_error_and_latency(client, world):
    listed = client.get(TOOL_CALLS).json()

    assert [c["id"] for c in listed] == [world["other"], world["failed"], world["ok"]]
    by_id = {c["id"]: c for c in listed}
    ok, failed = by_id[world["ok"]], by_id[world["failed"]]
    assert (ok["session_id"], ok["name"], ok["args"], ok["result"], ok["error"], ok["latency_ms"]) == (
        world["first"],
        "search_wardrobe",
        {"category": "top"},
        '{"total":3}',
        None,
        12,
    )
    assert (failed["name"], failed["args"], failed["error"]) == ("get_weather", {"place": "Nowhere"}, "place_not_found")


def test_tool_calls_filter_by_session_and_the_limit_keeps_the_newest(client, world):
    first = client.get(TOOL_CALLS, params={"session_id": world["first"]}).json()
    tenth = client.get(TOOL_CALLS, params={"session_id": 10}).json()
    newest = client.get(TOOL_CALLS, params={"limit": 1}).json()
    nothing = client.get(TOOL_CALLS, params={"session_id": 77}).json()

    assert [c["id"] for c in first] == [world["failed"], world["ok"]]
    assert [c["id"] for c in tenth] == [world["other"]]
    assert [c["id"] for c in newest] == [world["other"]]
    assert nothing == []


def test_llm_calls_filter_by_session_prefix_with_tokens_and_not_by_a_longer_id(client, world):
    one = client.get(LLM_CALLS, params={"session_id": world["first"]}).json()
    ten = client.get(LLM_CALLS, params={"session_id": world["tenth"]}).json()

    assert [c["id"] for c in one] == [world["pick_1"], world["chat_1"]]  # not session=10, not the Outfits page
    assert [c["id"] for c in ten] == [world["chat_10"]]
    chat = next(c for c in one if c["purpose"] == "chat")
    assert (chat["input_tokens"], chat["output_tokens"]) == (40, 7)


def test_the_session_filter_combines_with_purpose_and_errors_only(client, world):
    chat = client.get(LLM_CALLS, params={"session_id": 1, "purpose": "chat"}).json()
    errors = client.get(LLM_CALLS, params={"session_id": 1, "errors_only": True}).json()

    assert [c["id"] for c in chat] == [world["chat_1"]]
    assert [c["id"] for c in errors] == [world["pick_1"]]


def test_without_a_session_the_llm_calls_are_all_listed_as_before(client, world):
    listed = client.get(LLM_CALLS).json()

    assert {c["id"] for c in listed} == {world["chat_1"], world["chat_10"], world["pick_1"], world["page"]}
