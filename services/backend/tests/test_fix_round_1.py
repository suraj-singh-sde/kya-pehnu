"""Fixes after the Phase 3 security review and QA: invented tool names, colour length, log lines, provider notice."""

import json
import logging
from datetime import datetime

import pytest
from sqlmodel import Session

from kyapehnu.agent.chat.history import load_turns
from kyapehnu.agent.chat.orchestrator import provider_notice
from kyapehnu.agent.llm.base import AssistantMsg, LLMUnavailableError, Msg, ToolResultMsg
from kyapehnu.agent.models import ToolCall
from kyapehnu.wardrobe.items import MAX_QUERY_CHARS
from tests.chat_fakes import ask, fixed_now, say
from tests.chat_helpers import engine_of, new_chat, tool_rows, turn

INVENTED = "evil name!<script>"
NOW = datetime(2026, 10, 7, 9, 30)


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    fixed_now(monkeypatch, NOW)


@pytest.fixture
def engine(client):
    return engine_of(client)


def tool_names(messages: list[Msg]) -> set[str]:
    named = {m.name for m in messages if isinstance(m, ToolResultMsg)}
    return named | {c.name for m in messages if isinstance(m, AssistantMsg) for c in m.tool_calls}


def test_an_invented_tool_name_is_stored_and_replayed_as_unknown_tool(client, fake, engine):
    chat = new_chat(client)

    turn(client, fake, chat, ask((INVENTED, {})), say("ok"))
    turn(client, fake, chat, say("again"), text="next")

    (row,) = tool_rows(engine)
    assert (row.name, row.error) == ("unknown_tool", "unknown_tool")
    same_turn = fake.chat_calls[1]["messages"]
    assert tool_names(same_turn) == {"unknown_tool"}
    answer = next(m for m in same_turn if isinstance(m, ToolResultMsg))
    error = json.loads(answer.content)["error"]  # the model is still told which name it got wrong
    assert error["code"] == "unknown_tool" and error["message"].startswith(f"No tool named {INVENTED}.")
    assert tool_names(fake.chat_calls[2]["messages"]) == {"unknown_tool"}


def test_a_row_stored_with_an_invented_name_is_replayed_as_unknown_tool(client, fake, engine):
    chat = new_chat(client)
    turn(client, fake, chat, ask(("get_item", {"item_id": 1})), say("ok"))
    with Session(engine) as s:
        row = s.get(ToolCall, tool_rows(engine)[0].id)
        row.name = INVENTED
        s.add(row)
        s.commit()
        (history,) = load_turns(s, chat, 2)

    assert tool_names(history) == {"unknown_tool"}


def test_a_colour_longer_than_the_query_limit_is_422(client):
    assert client.get("/api/items", params={"color": "x" * MAX_QUERY_CHARS}).status_code == 200
    assert client.get("/api/items", params={"color": "x" * (MAX_QUERY_CHARS + 1)}).status_code == 422


def test_a_weather_request_logs_no_place(client, fake_weather, caplog):
    with caplog.at_level(logging.DEBUG):
        r = client.get("/api/weather", params={"place": "Paris"})

    assert r.status_code == 200
    ours = " ".join(r.getMessage() for r in caplog.records if r.name.startswith("kyapehnu"))  # not the test client's
    assert "Paris" not in ours


@pytest.mark.parametrize("reason", ["Rate limited.", "Rate limited", "Rate limited..."])
def test_the_provider_notice_has_one_full_stop_after_the_reason(reason):
    notice = provider_notice(LLMUnavailableError(reason), ())

    assert notice == "The AI provider failed: Rate limited. Your message is saved; send it again to retry."
